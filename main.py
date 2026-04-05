"""
Polymarket Latency Arbitrage Bot
─────────────────────────────────
Monitors BTC/ETH 5-minute and 15-minute up/down markets on Polymarket.
Compares CEX (Binance) prices with Polymarket implied odds to detect
latency-arbitrage opportunities and executes trades when edge > 5%.

Usage (paper trading — default, safe):
  python main.py

Usage (live trading — three explicit flags required):
  python main.py --live --confirm-live --accept-risk

Environment variables (copy .env.example → .env):
  POLYMARKET_PRIVATE_KEY     — required for live trading
  POLYMARKET_FUNDER_ADDRESS  — required for live trading
  PAPER_BALANCE              — starting paper balance (default 1000)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from binance_feed import BinanceFeed
from chainlink import ChainlinkFeed
from config import (
    ASSETS,
    DASHBOARD_REFRESH,
    INTERVALS,
    MARKET_SCAN_INTERVAL,
    OPPORTUNITY_CHECK_INTERVAL,
    POSITION_CHECK_INTERVAL,
)
from dashboard import Dashboard
from market_finder import MarketFinder, RateLimiter
from trader import fetch_order_book
from models import BotState, MarketInfo, OpportunitySignal, PositionStatus, Side
from portfolio import Portfolio
from probability import evaluate_opportunity
from trader import LiveTrader, PaperTrader

log = logging.getLogger(__name__)


# ── CLI ──────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket Latency Arbitrage Bot")
    p.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (flag 1 of 3)",
    )
    p.add_argument(
        "--confirm-live",
        action="store_true",
        dest="confirm_live",
        help="Confirm you understand live trading risks (flag 2 of 3)",
    )
    p.add_argument(
        "--accept-risk",
        action="store_true",
        dest="accept_risk",
        help="Accept that you may lose real money (flag 3 of 3)",
    )
    p.add_argument(
        "--paper-balance",
        type=float,
        default=float(os.getenv("PAPER_BALANCE", "1000")),
        dest="paper_balance",
        help="Starting balance for paper trading (default 1000 USDC)",
    )
    p.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        dest="log_level",
    )
    return p.parse_args()


# ── Main ─────────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    # ── Determine mode ──────────────────────────────────────────────────────────
    live_mode = args.live and args.confirm_live and args.accept_risk
    mode = "LIVE" if live_mode else "PAPER"

    if live_mode:
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
        if not private_key or not funder:
            print(
                "ERROR: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS must be set "
                "in .env for live trading.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("\n⚠  LIVE TRADING MODE ENABLED — real money will be used.\n")
    else:
        if args.live:
            missing = []
            if not args.confirm_live:
                missing.append("--confirm-live")
            if not args.accept_risk:
                missing.append("--accept-risk")
            print(
                f"Paper mode (missing live flags: {', '.join(missing)}). "
                "Pass all three flags to enable live trading."
            )
        else:
            print(f"Paper trading mode — starting balance: ${args.paper_balance:.2f} USDC")

    # ── Initialise components ───────────────────────────────────────────────────
    feed = BinanceFeed()
    chainlink = ChainlinkFeed()
    portfolio = Portfolio(starting_balance=args.paper_balance, mode=mode)
    finder = MarketFinder(feed, chainlink)

    if live_mode:
        trader = LiveTrader(
            portfolio=portfolio,
            finder=finder,
            private_key=private_key,
            funder_address=funder,
        )
    else:
        trader = PaperTrader(portfolio=portfolio, finder=finder)

    state = BotState(mode=mode)
    state.day_start_balance = args.paper_balance
    all_positions: list = []          # all positions ever; split by status for display

    clob_rl = RateLimiter(5.0)  # shared rate limiter for order-book fetches

    # ── Price callback → update state and log first connection ─────────────────
    _binance_connected = False

    def on_price(asset: str, price: float, ts: float) -> None:
        nonlocal _binance_connected
        if asset == "BTC":
            state.btc_price = price
        elif asset == "ETH":
            state.eth_price = price
        # XRP/SOL flow through market snapshots; not shown in header
        if not _binance_connected and state.btc_price and state.eth_price:
            _binance_connected = True
            state.add_log(f"Binance connected — BTC ${state.btc_price:,.2f}  ETH ${state.eth_price:,.2f}")

    feed.add_callback(on_price)

    # ── Spike queue: BinanceFeed fires immediately on flash move ───────────────
    # task_opportunity_monitor wakes instantly instead of waiting up to 2 s.
    spike_queue: asyncio.Queue[tuple[str, int, float]] = asyncio.Queue()

    def on_spike(asset: str, direction: int, pct_move: float) -> None:
        """Called synchronously from BinanceFeed._handle_message — must not block."""
        try:
            spike_queue.put_nowait((asset, direction, pct_move))
        except asyncio.QueueFull:
            pass  # queue shouldn't be full, but never block the WS read loop

    feed.add_spike_callback(on_spike)

    # ── Dashboard ───────────────────────────────────────────────────────────────
    dashboard = Dashboard()

    # ── Tasks ────────────────────────────────────────────────────────────────────

    async def task_binance() -> None:
        """Maintain Binance WebSocket price feed."""
        await feed.start()

    async def task_chainlink() -> None:
        """Poll Chainlink oracle on Polygon every ~25 s for BTC/ETH prices."""
        await chainlink.start()

    async def task_market_scanner() -> None:
        """Periodically fetch active BTC/ETH markets from Gamma API."""
        known_slugs: set = set()
        while True:
            try:
                await finder.scan_once()
                markets = await finder.get_active_markets()

                for market in markets:
                    # Log newly discovered markets
                    if market.slug not in known_slugs:
                        known_slugs.add(market.slug)
                        state.add_log(
                            f"Market found: {market.asset} {market.interval} | "
                            f"window {market.slug_ts} → closes in "
                            f"{market.time_remaining():.0f}s"
                        )

                    # Only track markets currently inside their measurement window
                    tr = market.time_remaining()
                    if not market.is_in_measurement_window():
                        continue

                    # Ensure price_to_beat is set (uses REST kline if needed)
                    await finder.ensure_price_to_beat(market)

                    cex_price = feed.latest_for(market.asset)
                    ptb = market.price_to_beat
                    delta_pct = ((cex_price - ptb) / ptb * 100) if (ptb and cex_price) else 0.0

                    if market.slug not in state.market_snapshots:
                        state.market_snapshots[market.slug] = {
                            "measurement_end": market.measurement_end,
                            "price_to_beat": ptb,
                            "cex_price": cex_price,
                            "delta_pct": delta_pct,
                            "up_ask": None,
                            "down_ask": None,
                            "edge": None,
                        }
                    else:
                        s = state.market_snapshots[market.slug]
                        s["measurement_end"] = market.measurement_end
                        if cex_price:
                            s["cex_price"] = cex_price
                        if ptb:
                            s["price_to_beat"] = ptb
                            s["delta_pct"] = delta_pct

                # Prune: remove markets whose window has ended
                import time as _time
                stale = [
                    slug for slug, snap in list(state.market_snapshots.items())
                    if _time.time() >= snap.get("measurement_end", 0)
                ]
                for slug in stale:
                    state.market_snapshots.pop(slug, None)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Market scanner error: %s", exc)
                state.add_log(f"Scanner error: {exc}")
            await asyncio.sleep(MARKET_SCAN_INTERVAL)

    async def _check_markets_for_asset(asset: str, snap: dict) -> None:
        """Fetch order books and evaluate signals for markets matching `asset`.
        If asset is None, checks all active markets (fallback poll path).
        """
        from config import DEFAULT_VOL, REALIZED_VOL_WINDOW
        from probability import estimate_p_up, net_edge, CRYPTO_FEE_RATE

        markets = await finder.get_active_markets()
        for market in markets:
            # On spike: only check the relevant asset's markets.
            # On fallback poll: check all.
            if asset is not None and market.asset != asset:
                continue

            if not market.is_in_measurement_window():
                continue

            await finder.ensure_price_to_beat(market)

            cex_price = feed.latest_for(market.asset)
            if cex_price is None:
                continue

            hist = getattr(feed, market.asset.lower(), feed.btc)
            vol = hist.realized_vol_per_second(
                REALIZED_VOL_WINDOW,
                DEFAULT_VOL.get(market.asset, 0.0001),
            )

            ob = await fetch_order_book(market.up_token_id, clob_rl)
            if ob is None:
                continue

            ptb = market.price_to_beat
            delta_pct = (cex_price - ptb) / ptb * 100 if ptb else 0.0
            down_ask = 1.0 - ob.best_bid

            tr = market.time_remaining()
            p_up = estimate_p_up(cex_price, ptb, tr, vol, market.asset) if ptb else 0.5
            display_edge = abs(net_edge(max(p_up, 1 - p_up), min(ob.best_ask, down_ask)))

            state.market_snapshots[market.slug] = {
                "measurement_end": market.measurement_end,
                "price_to_beat": ptb,
                "cex_price": cex_price,
                "delta_pct": delta_pct,
                "up_ask": ob.best_ask,
                "down_ask": down_ask,
                "edge": display_edge if display_edge > 0.001 else None,
            }

            signal = evaluate_opportunity(
                market=market,
                ob=ob,
                current_price=cex_price,
                vol_per_second=vol,
                portfolio_value=snap["balance"],
                price_hist=hist,
            )

            if signal is not None:
                state.add_log(
                    f"SIGNAL: {signal.market.slug} {signal.side.value} | "
                    f"edge={signal.edge_after_fees:.1%} | "
                    f"conf={signal.confidence:.1%} | "
                    f"${signal.suggested_size_usd:.2f}"
                )
                pos = await trader.execute_signal(signal)
                if pos is not None:
                    all_positions.append(pos)
                    state.add_log(
                        f"TRADE: {pos.market.slug} {pos.side.value} "
                        f"@ {pos.entry_price:.4f} | "
                        f"shares={pos.size_shares:.1f} | "
                        f"cost=${pos.total_spent_usd:.2f}"
                    )

    async def task_opportunity_monitor() -> None:
        """
        Event-driven opportunity monitor.

        Primary path: wakes immediately when BinanceFeed detects a flash spike,
        checks only markets for that asset.  Exploits the 2.7-second Polymarket
        lag window without waiting for a poll cycle.

        Fallback path: if no spike arrives within OPPORTUNITY_CHECK_INTERVAL
        seconds, scans all markets anyway (catches slow drifts and keeps the
        dashboard order-book columns fresh).
        """
        while True:
            try:
                if portfolio.is_halted:
                    await asyncio.sleep(OPPORTUNITY_CHECK_INTERVAL)
                    continue

                snap = portfolio.snapshot()

                # Wait for spike OR timeout — whichever comes first
                try:
                    asset, direction, pct_move = await asyncio.wait_for(
                        spike_queue.get(),
                        timeout=OPPORTUNITY_CHECK_INTERVAL,
                    )
                    log.info(
                        "Spike event: %s %s %.3f%% — checking markets immediately",
                        asset, "UP" if direction > 0 else "DOWN", pct_move,
                    )
                    state.add_log(
                        f"Spike: {asset} {'▲' if direction > 0 else '▼'} {pct_move:.2f}% — scanning"
                    )
                    await _check_markets_for_asset(asset, snap)

                    # Drain any queued spikes for the same asset so we don't
                    # pile up redundant checks within the same lag window.
                    while not spike_queue.empty():
                        try:
                            spike_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                except asyncio.TimeoutError:
                    # Fallback poll: refresh all markets
                    await _check_markets_for_asset(None, snap)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Opportunity monitor error: %s", exc)
                await asyncio.sleep(1.0)

    async def task_position_monitor() -> None:
        """
        Resolve expired positions and sync portfolio state.

        Paper mode: resolves immediately after measurement_end using the
        Binance 1m kline close price — no need to wait for oracle settlement.

        Live mode: also polls Gamma API for on-chain resolution confirmation.
        """
        from market_finder import fetch_binance_open_at
        from models import Side

        while True:
            try:
                # ── Primary resolution: Binance REST price comparison ───────────
                # Works for both paper and live mode.  Resolves as soon as the
                # measurement window ends (no oracle delay).
                for pos in portfolio.open_positions():
                    mend = pos.market.measurement_end
                    if time.time() < mend + 15:
                        continue  # window not over yet (15 s buffer)

                    ptb = pos.market.price_to_beat
                    if ptb is None:
                        # Try to set it now (will use REST kline)
                        await finder.ensure_price_to_beat(pos.market)
                        ptb = pos.market.price_to_beat
                    if ptb is None:
                        continue  # still can't determine start price

                    # Get Binance price at measurement_end
                    hist = getattr(feed, pos.market.asset.lower(), feed.btc)
                    end_price = hist.at(mend, tolerance_secs=30)

                    if end_price is None:
                        symbol = "BTCUSDT" if pos.market.asset == "BTC" else "ETHUSDT"
                        # The kline that CONTAINS mend starts at floor(mend/60)*60
                        kline_start = int(mend // 60) * 60
                        end_price = await fetch_binance_open_at(symbol, kline_start)

                    if end_price is None:
                        state.add_log(f"Cannot resolve {pos.market.slug} — no end price")
                        continue

                    resolved_side = Side.UP if end_price >= ptb else Side.DOWN
                    result = portfolio.resolve_position(pos.position_id, resolved_side)
                    if result:
                        won = result.pnl_usd > 0
                        state.add_log(
                            f"RESOLVED {pos.market.asset} {pos.market.interval} | "
                            f"end={end_price:,.2f} vs ptb={ptb:,.2f} → {resolved_side.value} | "
                            f"{'WIN' if won else 'LOSS'} ${result.pnl_usd:+.2f}"
                        )
                    # Remove from active market table
                    state.market_snapshots.pop(pos.market.slug, None)

                # ── Secondary: Gamma API (live mode authoritative confirmation) ──
                if mode == "LIVE":
                    resolved_markets = await finder.resolve_closed_markets()
                    for mkt in resolved_markets:
                        if mkt.resolved_side is None:
                            continue
                        for pos in portfolio.open_positions():
                            if pos.market.slug == mkt.slug:
                                result = portfolio.resolve_position(
                                    pos.position_id, mkt.resolved_side
                                )
                                if result:
                                    won = result.pnl_usd > 0
                                    state.add_log(
                                        f"CONFIRMED (chain) {mkt.slug} → {mkt.resolved_side.value} | "
                                        f"{'WIN' if won else 'LOSS'} ${result.pnl_usd:+.2f}"
                                    )

                # ── Portfolio state sync ────────────────────────────────────────
                snap = portfolio.snapshot()
                state.is_halted = snap["is_halted"]
                state.halt_reason = snap["halt_reason"]
                if state.is_halted and state.halt_reason not in state.log_lines[-3:]:
                    state.add_log(f"KILL SWITCH: {state.halt_reason}")

                if mode == "LIVE":
                    await trader.sync_balance()

                state.balance_usdc = snap["balance"]
                state.daily_pnl = snap["daily_pnl"]
                state.total_trades = snap["total_trades"]
                state.winning_trades = snap["winning_trades"]

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Position monitor error: %s", exc)

            await asyncio.sleep(POSITION_CHECK_INTERVAL)

    async def task_day_reset() -> None:
        """Reset daily P&L counter at midnight UTC."""
        while True:
            now = datetime.now(timezone.utc)
            # Seconds until next midnight
            secs_to_midnight = (
                (24 - now.hour) * 3600 - now.minute * 60 - now.second
            )
            await asyncio.sleep(secs_to_midnight)
            portfolio.new_trading_day()
            state.add_log("New trading day — daily P&L reset")

    async def task_dashboard() -> None:
        """Refresh terminal display."""
        while True:
            try:
                snap = portfolio.snapshot()
                state.balance_usdc = snap["balance"]
                state.daily_pnl = snap["daily_pnl"]
                state.total_trades = snap["total_trades"]
                state.winning_trades = snap["winning_trades"]
                state.is_halted = snap["is_halted"]

                # Evict expired snapshots every render cycle (100ms resolution)
                now = time.time()
                expired = [
                    slug for slug, s in list(state.market_snapshots.items())
                    if now >= s.get("measurement_end", 0)
                ]
                for slug in expired:
                    state.market_snapshots.pop(slug, None)

                open_pos = [p for p in all_positions if p.status == PositionStatus.OPEN]
                resolved_pos = [p for p in all_positions if p.status != PositionStatus.OPEN]
                dashboard.update(state, open_pos, resolved_pos)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            await asyncio.sleep(DASHBOARD_REFRESH)

    # ── Start everything ────────────────────────────────────────────────────────
    with dashboard:
        state.add_log(f"Bot started in {mode} mode")
        state.add_log("Connecting to Binance WebSocket...")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(task_binance(), name="binance")
            tg.create_task(task_chainlink(), name="chainlink")
            tg.create_task(task_market_scanner(), name="scanner")
            tg.create_task(task_opportunity_monitor(), name="opportunities")
            tg.create_task(task_position_monitor(), name="positions")
            tg.create_task(task_day_reset(), name="day_reset")
            tg.create_task(task_dashboard(), name="dashboard")


def main() -> None:
    args = parse_args()

    # Write logs to a file so they don't bleed through the Rich Live dashboard.
    # Tail bot.log in a separate terminal for raw debug output.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler("bot.log", encoding="utf-8")],
    )

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nBot stopped by user.")


if __name__ == "__main__":
    main()
