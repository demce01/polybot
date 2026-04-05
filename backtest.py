"""
Backtest: simulate the current bot on 30 days of BTC/ETH 5m Polymarket markets.

Parameters match config.py exactly:
  - MOMENTUM_WINDOW_SECS = 10
  - REVERSAL_WINDOW_SECS = 5
  - MIN_MOMENTUM_PCT     = {"BTC": 0.05, "ETH": 0.07}
  - MAX_REVERSAL_PCT     = {"BTC": 0.04, "ETH": 0.05}
  - MIN_CONFIDENCE       = 0.87
  - MIN_EDGE_AFTER_FEES  = 0.05
  - MAX_POSITION_FRACTION = 0.04
  - KELLY_FRACTION       = 0.5

Data sources:
  - Polymarket Gamma API: market outcome (UP/DOWN) for resolved windows
  - Binance REST 1m kline: price-to-beat (open of window-start minute)
  - Binance REST 1s klines: intrabar path for momentum/reversal checks

Entry assumption:
  When the flash-spike fires at second T inside the window, we assume:
    polymarket_ask = GBM_probability - LAG
  where LAG = 0.06 (6pp stale lag — the genuine arbitrage scenario).
  This is the optimistic scenario the bot is built for.
  We also report the "no lag" scenario (ask = GBM_p + SPREAD) to show
  what happens if Polymarket has already repriced by the time the order lands.

Sampling:
  Every 6th 5m window (= every 30 min) over the last 30 days.
  ~1440 windows × 2 assets = ~2880 API calls.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from scipy.stats import norm

# ─── Parameters (must match config.py) ───────────────────────────────────────────

GAMMA_URL   = "https://gamma-api.polymarket.com"
BINANCE_URL = "https://api.binance.com/api/v3"

LOOKBACK_DAYS        = 30
SAMPLE_EVERY_N       = 6       # every 6th 5m window = every 30 min
CONCURRENCY          = 6
INTERVAL_SECS        = 300     # 5m window

# Bot signal filters
MOMENTUM_WINDOW_SECS  = 10
REVERSAL_WINDOW_SECS  = 5
MIN_MOMENTUM_PCT      = {"BTC": 0.05, "ETH": 0.07}
MAX_REVERSAL_PCT      = {"BTC": 0.04, "ETH": 0.05}
MIN_CONFIDENCE        = 0.87
MIN_EDGE_AFTER_FEES   = 0.05
MIN_LAG_PP            = 0.05
MIN_WINDOW_ELAPSED    = 60
MIN_TIME_REM_FRAC     = 0.07

# Sizing
MAX_POSITION_FRACTION = 0.04
KELLY_FRACTION        = 0.5
MIN_ORDER_USD         = 5.0
STARTING_BALANCE      = 50.0

# Entry scenarios
FEE_RATE    = 0.10
SPREAD      = 0.015   # efficient market: PM already priced in the move
LAG         = 0.06    # genuine lag: PM stale by 6pp (what the bot is hunting)

# Vol defaults (annualised → per-second)
_VOL = {
    "BTC": 0.70 / (365 * 24 * 3600) ** 0.5,
    "ETH": 0.85 / (365 * 24 * 3600) ** 0.5,
}
_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

ASSETS = ["BTC", "ETH"]

# Check times: every 5s starting at MIN_WINDOW_ELAPSED, stopping at 93% elapsed
_MAX_ELAPSED = int(INTERVAL_SECS * (1 - MIN_TIME_REM_FRAC))   # 279s
CHECK_TIMES  = list(range(MIN_WINDOW_ELAPSED, _MAX_ELAPSED, 5))  # every 5s

# Polymarket lag model: assume PM is still priced at GBM(price_10s_ago).
# The actual probability gap is derived from real price movement — no fixed LAG.
# We add a small SPREAD on top of the stale PM probability (normal market friction).
PM_SPREAD    = 0.005  # 0.5pp — PM ask is slightly above its own mid


# ─── Math helpers ─────────────────────────────────────────────────────────────────

def gbm_p_up(s: float, k: float, t: float, vol: float) -> float:
    if k <= 0 or s <= 0 or t <= 0:
        return 0.5
    st = vol * math.sqrt(t)
    if st < 1e-12:
        return 1.0 if s >= k else 0.0
    d2 = (math.log(s / k) - 0.5 * vol**2 * t) / st
    return float(norm.cdf(d2))


def fee_per_share(ask: float) -> float:
    return FEE_RATE * ask * (1.0 - ask)


def net_edge(cex_p: float, ask: float) -> float:
    return cex_p - ask - fee_per_share(ask)


def kelly_size(p: float, ask: float, balance: float) -> float:
    cost = ask + fee_per_share(ask)
    net_win = 1.0 - cost
    if net_win <= 0 or cost <= 0:
        return 0.0
    b = net_win / cost
    q = 1.0 - p
    k = (b * p - q) / b
    if k <= 0:
        return 0.0
    raw_usd = (k * KELLY_FRACTION) * balance
    size_usd = min(raw_usd, MAX_POSITION_FRACTION * balance)
    return max(size_usd, MIN_ORDER_USD) if size_usd >= MIN_ORDER_USD else 0.0


def trade_pnl(size_usd: float, ask: float, won: bool) -> float:
    """Actual dollar P&L for a size_usd bet at ask price."""
    shares = size_usd / ask
    fee_paid = shares * fee_per_share(ask)
    if won:
        return shares - size_usd - fee_paid  # payout(shares) - cost - fee
    return -(size_usd + fee_paid)            # lose cost + fee


# ─── Data structures ──────────────────────────────────────────────────────────────

@dataclass
class WindowData:
    slug_ts:   int
    asset:     str
    ptb:       float        # price to beat (Binance 1m open)
    result:    str          # "UP" or "DOWN"
    klines_1s: list         # list of (open, high, low, close) per second


@dataclass
class TradeRecord:
    slug_ts:      int
    asset:        str
    elapsed:      int       # seconds into window when signal fired
    direction:    str       # "UP" or "DOWN"
    ask:          float     # assumed entry ask
    edge:         float
    conf:         float
    momentum_pct: float
    won:          bool
    size_usd:     float     # Kelly-sized dollar bet
    pnl_usd:      float     # actual dollar P&L


# ─── Data fetching ────────────────────────────────────────────────────────────────

async def fetch_window(
    client: httpx.AsyncClient,
    sem:    asyncio.Semaphore,
    slug_ts: int,
    asset:   str,
) -> Optional[WindowData]:
    slug   = f"{asset.lower()}-updown-5m-{slug_ts}"
    symbol = _SYMBOL[asset]

    async with sem:
        # 1. Gamma API — resolved outcome
        try:
            r = await client.get(f"{GAMMA_URL}/events?slug={slug}", timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None

        if not data:
            return None
        m = data[0].get("markets", [{}])[0]
        if not m.get("closed"):
            return None

        try:
            outcomes = m["outcomes"]
            prices   = m["outcomePrices"]
            if isinstance(outcomes, str): outcomes = json.loads(outcomes)
            if isinstance(prices,   str): prices   = json.loads(prices)
            up_idx = outcomes.index("Up")
            result = "UP" if float(prices[up_idx]) > 0.9 else "DOWN"
        except Exception:
            return None

        # 2. Binance 1m — price-to-beat
        try:
            r2 = await client.get(
                f"{BINANCE_URL}/klines",
                params={"symbol": symbol, "interval": "1m",
                        "startTime": slug_ts * 1000, "limit": 1},
                timeout=8,
            )
            r2.raise_for_status()
            raw1m = r2.json()
        except Exception:
            return None
        if not raw1m:
            return None
        ptb = float(raw1m[0][1])   # open of first 1m candle

        # 3. Binance 1s klines — intrabar path
        try:
            r3 = await client.get(
                f"{BINANCE_URL}/klines",
                params={"symbol": symbol, "interval": "1s",
                        "startTime": slug_ts * 1000, "limit": 300},
                timeout=15,
            )
            r3.raise_for_status()
            raw1s = r3.json()
        except Exception:
            return None
        if not raw1s or len(raw1s) < MIN_WINDOW_ELAPSED + MOMENTUM_WINDOW_SECS + 5:
            return None

        klines = [(float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in raw1s]
        return WindowData(slug_ts=slug_ts, asset=asset, ptb=ptb,
                          result=result, klines_1s=klines)


# ─── Strategy simulation ──────────────────────────────────────────────────────────

def simulate_window(wd: WindowData, balance: float) -> Optional[TradeRecord]:
    """
    Scan the window for the FIRST valid flash-spike signal, return the trade.
    Only one trade per window (first entry wins).

    Polymarket lag model (realistic):
      At entry (elapsed T), PM is still priced at GBM(price_10s_ago, T+10).
      The probability gap is derived from actual price movement — no fixed LAG.
    """
    vol     = _VOL[wd.asset]
    min_mom = MIN_MOMENTUM_PCT[wd.asset]
    max_rev = MAX_REVERSAL_PCT[wd.asset]
    n       = len(wd.klines_1s)

    for elapsed in CHECK_TIMES:
        t_remaining = INTERVAL_SECS - elapsed
        if t_remaining < INTERVAL_SECS * MIN_TIME_REM_FRAC:
            break
        if elapsed >= n:
            break

        current_price = wd.klines_1s[elapsed - 1][3]

        # ── 10s momentum ──────────────────────────────────────────────────────
        mom_idx = elapsed - MOMENTUM_WINDOW_SECS - 1
        if mom_idx < 0:
            continue
        price_10s_ago = wd.klines_1s[mom_idx][3]
        momentum_pct  = (current_price - price_10s_ago) / price_10s_ago * 100.0

        # ── 5s reversal ───────────────────────────────────────────────────────
        rev_idx      = elapsed - REVERSAL_WINDOW_SECS - 1
        price_5s_ago = wd.klines_1s[rev_idx][3] if rev_idx >= 0 else current_price
        reversal_pct = (current_price - price_5s_ago) / price_5s_ago * 100.0

        # ── CEX probability (current price, time remaining) ───────────────────
        pu_now = gbm_p_up(current_price, wd.ptb, t_remaining, vol)
        conf   = max(pu_now, 1.0 - pu_now)
        if conf < MIN_CONFIDENCE:
            continue

        # ── Stale PM probability (price was 10s ago; PM hasn't updated yet) ──
        t_old  = t_remaining + MOMENTUM_WINDOW_SECS
        pu_old = gbm_p_up(price_10s_ago, wd.ptb, t_old, vol)

        if pu_now >= 0.5:
            side       = "UP"
            cex_p      = pu_now
            pm_stale_p = pu_old
            mom_ok     = momentum_pct >= min_mom
            rev_ok     = reversal_pct >= -max_rev
        else:
            side       = "DOWN"
            cex_p      = 1.0 - pu_now
            pm_stale_p = 1.0 - pu_old
            mom_ok     = momentum_pct <= -min_mom
            rev_ok     = reversal_pct <= max_rev

        if not mom_ok or not rev_ok:
            continue

        # PM ask = stale GBM probability + tiny spread (PM is a market)
        ask    = min(0.98, max(0.02, pm_stale_p + PM_SPREAD))
        lag_pp = cex_p - ask
        if lag_pp < MIN_LAG_PP:
            continue

        edge = net_edge(cex_p, ask)
        if edge < MIN_EDGE_AFTER_FEES:
            continue

        size_usd = kelly_size(cex_p, ask, balance)
        if size_usd <= 0:
            continue

        won = (side == wd.result)
        pnl = trade_pnl(size_usd, ask, won)

        return TradeRecord(
            slug_ts=wd.slug_ts, asset=wd.asset,
            elapsed=elapsed, direction=side,
            ask=ask, edge=edge, conf=conf,
            momentum_pct=abs(momentum_pct),
            won=won, size_usd=size_usd, pnl_usd=pnl,
        )

    return None


# ─── Statistics ───────────────────────────────────────────────────────────────────

def print_results(trades: list[TradeRecord], starting_balance: float) -> None:
    n = len(trades)
    if n == 0:
        print("\nNo trades generated.")
        return

    wins  = sum(1 for t in trades if t.won)
    wr    = wins / n
    total_pnl = sum(t.pnl_usd for t in trades)
    avg_pnl   = total_pnl / n
    total_bet = sum(t.size_usd for t in trades)
    roi       = total_pnl / total_bet if total_bet > 0 else 0.0

    # Running balance + drawdown (chronological order by slug_ts)
    balance = starting_balance
    peak    = balance
    max_dd  = 0.0
    max_dd_pct = 0.0
    consec_losses = 0
    max_consec    = 0

    for t in sorted(trades, key=lambda x: x.slug_ts):
        balance += t.pnl_usd
        if balance > peak:
            peak = balance
        dd     = peak - balance
        dd_pct = dd / peak if peak > 0 else 0.0
        max_dd     = max(max_dd,     dd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        if t.won:
            consec_losses = 0
        else:
            consec_losses += 1
            max_consec = max(max_consec, consec_losses)

    final_balance = starting_balance + total_pnl
    total_return  = total_pnl / starting_balance

    print(f"\n{'═'*62}")
    print(f"  30-DAY BACKTEST RESULTS  (flash-spike entry, genuine 6pp lag)")
    print(f"{'═'*62}")
    print(f"  Starting balance:    ${starting_balance:.2f}")
    print(f"  Final balance:       ${final_balance:.2f}  ({total_return:+.1%})")
    print(f"  Total P&L:           ${total_pnl:+.2f}")
    print(f"  Total capital bet:   ${total_bet:.2f}")
    print(f"  ROI on capital used: {roi:+.1%}")
    print()
    print(f"  Trades:              {n}")
    print(f"  Wins / Losses:       {wins} / {n - wins}")
    print(f"  Win rate:            {wr:.1%}")
    print(f"  Avg P&L / trade:     ${avg_pnl:+.4f}")
    print(f"  Avg entry ask:       {sum(t.ask for t in trades)/n:.3f}")
    print(f"  Avg edge:            {sum(t.edge for t in trades)/n:+.3f}")
    print(f"  Avg momentum:        {sum(t.momentum_pct for t in trades)/n:.3f}%")
    print()
    print(f"  Max drawdown:        ${max_dd:.2f}  ({max_dd_pct:.1%})")
    print(f"  Max consecutive loss:{max_consec}")
    print()

    # By asset
    print(f"  By asset:")
    for asset in ASSETS:
        sub = [t for t in trades if t.asset == asset]
        if not sub:
            continue
        sw = sum(1 for t in sub if t.won)
        sp = sum(t.pnl_usd for t in sub)
        print(f"    {asset}: {len(sub):3d} trades  wr={sw/len(sub):.1%}  P&L=${sp:+.2f}")
    print()

    # Monthly P&L curve (weekly bins)
    by_week: dict[int, list] = {}
    for t in trades:
        week = (t.slug_ts - min(t.slug_ts for t in trades)) // (7 * 24 * 3600)
        by_week.setdefault(week, []).append(t)
    print(f"  Weekly P&L:")
    running = starting_balance
    for w in sorted(by_week):
        week_trades = by_week[w]
        week_pnl    = sum(t.pnl_usd for t in week_trades)
        running    += week_pnl
        ww = sum(1 for t in week_trades if t.won)
        print(f"    Week {w+1}: {len(week_trades):3d} trades  "
              f"wr={ww/len(week_trades):.0%}  "
              f"P&L=${week_pnl:+.2f}  bal=${running:.2f}")

    # Entry timing breakdown
    print()
    print(f"  By entry time into window:")
    by_elapsed: dict[int, list] = {}
    for t in trades:
        bucket = (t.elapsed // 30) * 30
        by_elapsed.setdefault(bucket, []).append(t)
    for b in sorted(by_elapsed):
        sub = by_elapsed[b]
        sw  = sum(1 for t in sub if t.won)
        sp  = sum(t.pnl_usd for t in sub)
        print(f"    {b:3d}-{b+30}s elapsed:  {len(sub):3d} trades  "
              f"wr={sw/len(sub):.0%}  P&L=${sp:+.2f}")

    # GBM calibration
    print()
    print(f"  GBM calibration (actual win rate vs claimed confidence):")
    buckets = [(0.87, 0.90), (0.90, 0.93), (0.93, 0.97), (0.97, 1.01)]
    for lo, hi in buckets:
        sub = [t for t in trades if lo <= t.conf < hi]
        if sub:
            aw = sum(1 for t in sub if t.won) / len(sub)
            ac = sum(t.conf for t in sub) / len(sub)
            print(f"    conf [{lo:.0%}-{hi:.0%}): n={len(sub):4d}  "
                  f"GBM_claimed={ac:.1%}  actual_wr={aw:.1%}  "
                  f"{'OK' if abs(aw - ac) < 0.05 else 'OVERFIT'}")


# ─── Main ─────────────────────────────────────────────────────────────────────────

async def run_backtest() -> None:
    now        = int(time.time())
    start_ts   = (now // 300) * 300 - LOOKBACK_DAYS * 24 * 3600
    all_ts     = list(range(start_ts, now - 600, 300 * SAMPLE_EVERY_N))
    jobs       = [(ts, asset) for ts in all_ts for asset in ASSETS]

    print(f"Backtest: {len(all_ts)} windows × {len(ASSETS)} assets = {len(jobs)} jobs")
    print(f"Date range: {__import__('datetime').datetime.utcfromtimestamp(all_ts[0]).strftime('%Y-%m-%d')} "
          f"→ {__import__('datetime').datetime.utcfromtimestamp(all_ts[-1]).strftime('%Y-%m-%d')}")
    print(f"Starting balance: ${STARTING_BALANCE:.2f}")
    print("Fetching 1s klines — this takes 3-5 minutes...\n")

    sem    = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY + 2,
                          max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks   = [fetch_window(client, sem, ts, asset) for ts, asset in jobs]
        results = []
        done    = 0
        for coro in asyncio.as_completed(tasks):
            wd = await coro
            results.append(wd)
            done += 1
            if done % 200 == 0:
                valid = sum(1 for r in results if r is not None)
                pct   = done / len(tasks) * 100
                print(f"  {done}/{len(tasks)} ({pct:.0f}%)  valid: {valid}", flush=True)

    windows = [r for r in results if r is not None]
    print(f"\nFetched {len(windows)} resolved windows (out of {len(jobs)} attempted)")

    if not windows:
        print("No data — check API connectivity")
        return

    # ── Simulate with running balance (Kelly sizing updates each trade) ────────
    # Sort by timestamp so balance evolves chronologically
    windows.sort(key=lambda w: w.slug_ts)
    balance = STARTING_BALANCE
    all_trades: list[TradeRecord] = []

    for wd in windows:
        trade = simulate_window(wd, balance)
        if trade is not None:
            # Re-size with current balance at time of trade
            trade.size_usd = kelly_size(
                max(trade.conf, 1 - trade.conf),   # cex_p ≈ conf for flash entries
                trade.ask,
                balance,
            )
            if trade.size_usd <= 0:
                continue
            trade.pnl_usd = trade_pnl(trade.size_usd, trade.ask, trade.won)
            balance += trade.pnl_usd
            all_trades.append(trade)

    print_results(all_trades, STARTING_BALANCE)

    # ── No-lag comparison: what if PM had already repriced? ───────────────────
    print(f"\n{'─'*62}")
    print("  WORST CASE: Polymarket repriced BEFORE our order lands")
    print(f"  (PM ask = current GBM_p + {SPREAD*100:.1f}pp spread)")
    print(f"{'─'*62}")
    no_lag_trades: list[TradeRecord] = []
    balance2 = STARTING_BALANCE
    for wd in sorted(windows, key=lambda w: w.slug_ts):
        vol     = _VOL[wd.asset]
        min_mom = MIN_MOMENTUM_PCT[wd.asset]
        n       = len(wd.klines_1s)
        for elapsed in CHECK_TIMES:
            t_rem = INTERVAL_SECS - elapsed
            if t_rem < INTERVAL_SECS * MIN_TIME_REM_FRAC or elapsed >= n:
                break
            cur     = wd.klines_1s[elapsed - 1][3]
            mom_idx = elapsed - MOMENTUM_WINDOW_SECS - 1
            if mom_idx < 0:
                continue
            p10 = wd.klines_1s[mom_idx][3]
            mom = (cur - p10) / p10 * 100.0
            pu  = gbm_p_up(cur, wd.ptb, t_rem, vol)
            conf = max(pu, 1.0 - pu)
            if conf < MIN_CONFIDENCE:
                continue
            if pu >= 0.5:
                side = "UP";   cex_p = pu;        mom_ok = mom >= min_mom
            else:
                side = "DOWN"; cex_p = 1.0 - pu;  mom_ok = mom <= -min_mom
            if not mom_ok:
                continue
            # Worst case: PM already repriced, ask = current GBM + spread
            ask  = min(0.98, cex_p + SPREAD)
            edge = net_edge(cex_p, ask)
            if edge < MIN_EDGE_AFTER_FEES:
                continue
            sz = kelly_size(cex_p, ask, balance2)
            if sz <= 0:
                continue
            won = (side == wd.result)
            pnl = trade_pnl(sz, ask, won)
            balance2 += pnl
            no_lag_trades.append(TradeRecord(
                slug_ts=wd.slug_ts, asset=wd.asset, elapsed=elapsed,
                direction=side, ask=ask, edge=edge, conf=conf,
                momentum_pct=abs(mom), won=won, size_usd=sz, pnl_usd=pnl,
            ))
            break

    if no_lag_trades:
        nl_wins = sum(1 for t in no_lag_trades if t.won)
        nl_pnl  = sum(t.pnl_usd for t in no_lag_trades)
        print(f"  Trades: {len(no_lag_trades)}  wr={nl_wins/len(no_lag_trades):.1%}  "
              f"total P&L=${nl_pnl:+.2f}  final=${STARTING_BALANCE+nl_pnl:.2f}")
    else:
        print("  No trades (edge gate fails — SPREAD kills edge, confirms lag is required)")


if __name__ == "__main__":
    asyncio.run(run_backtest())
