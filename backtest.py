"""
Backtest: old strategy vs new strategy on 30 days of BTC/ETH 5m Polymarket markets.

Data sources:
  - Polymarket Gamma API: market outcome (UP/DOWN) for each resolved window
  - Binance REST klines: 1m and 5m candles to reconstruct intrabar price paths

Strategy definitions
--------------------
OLD  (original bot):
  Entry whenever GBM confidence >= 0.85 inside the [10s, 290s] remaining window.
  No momentum requirement; checks every simulated minute.

NEW  (revised bot):
  Entry only when:
    • >=60 s elapsed in window (established direction)
    • >=7 % of window remaining
    • 30-second price move >= 0.08% in bet direction  (flash-move detector)
    • Price has NOT reversed >= 0.06% in last 15 s   (spike-faded guard)
    • GBM confidence >= 0.87
    • Net edge after fees >= 0.08

Entry price assumption
-----------------------
For both strategies, at entry we assume:
  polymarket_ask = GBM_probability + SPREAD_ASSUMPTION
where SPREAD_ASSUMPTION = 0.015 (1.5pp, conservative — markets are efficient on drifts).

For the new strategy on flash-move entries we additionally test a GENUINE_LAG scenario:
  polymarket_ask = GBM_probability - 0.06  (market hasn't updated yet, 6pp lag)

P&L calculation
---------------
  invest = 1 USD (normalized)
  shares = 1 / ask
  fee    = 0.10 × ask × (1 − ask) × shares = 0.10 × (1 − ask)
  pnl    = (1/ask − 1 − 0.10×(1−ask))   if WIN
         = −(1 + 0.10×(1−ask))           if LOSS

Sampling
---------
720 windows sampled every 36 minutes (12th 5m window) over the last 30 days.
Gives good temporal distribution without hammering the APIs.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from scipy.stats import norm

# ─── Config ─────────────────────────────────────────────────────────────────────

GAMMA = "https://gamma-api.polymarket.com"
BINANCE = "https://api.binance.com/api/v3"

SAMPLE_EVERY_N = 12       # every 12th 5m window (= every 60 min)
LOOKBACK_DAYS = 30
FEE_RATE = 0.10
VOL_PER_SEC = 0.000111    # BTC annualised 70% → per-second
SPREAD = 0.015            # assumption: PM ask = GBM_p + SPREAD (efficient market)
LAG   = 0.06              # assumption for new-strategy flash: PM ask = GBM_p - LAG

# Old strategy
OLD_MIN_CONF = 0.85
OLD_MIN_ELAPSED = 0.0
OLD_MAX_REMAINING = 290
OLD_MIN_REMAINING = 10

# New strategy
NEW_MIN_CONF = 0.87
NEW_MIN_EDGE = 0.08
NEW_MIN_LAG_PP = 0.05
NEW_MIN_ELAPSED = 60      # seconds into window
NEW_MIN_REM_FRAC = 0.07   # fraction of interval
NEW_MOMENTUM_WINDOW = 30  # seconds — use 1m candle change as proxy
NEW_MIN_MOMENTUM_PCT = 0.08  # % move in 30s
NEW_MAX_REVERSAL_PCT = 0.06  # % reverse in 15s → skip

INTERVAL = 300            # 5m window in seconds
CONCURRENCY = 20          # parallel HTTP requests

# ─── Helpers ────────────────────────────────────────────────────────────────────

def p_up(s: float, k: float, t: float) -> float:
    if k <= 0 or s <= 0 or t <= 0:
        return 0.5
    st = VOL_PER_SEC * math.sqrt(t)
    if st < 1e-12:
        return 1.0 if s >= k else 0.0
    d2 = (math.log(s / k) - 0.5 * VOL_PER_SEC**2 * t) / st
    return float(norm.cdf(d2))


def fee(ask: float) -> float:
    return FEE_RATE * ask * (1.0 - ask)


def net_edge(cex_p: float, ask: float) -> float:
    return cex_p - ask - fee(ask)


def pnl(ask: float, won: bool) -> float:
    f = fee(ask)
    if won:
        return 1.0 / ask - 1.0 - f * (1.0 / ask)
    return -(1.0 + f * (1.0 / ask))


# ─── Data structures ─────────────────────────────────────────────────────────────

@dataclass
class WindowData:
    slug_ts: int
    ptb: float          # price to beat (Binance 5m open ≈ Chainlink)
    result: str         # "UP" or "DOWN"
    klines_1m: list     # list of (open,high,low,close) for each minute


@dataclass
class TradeRecord:
    slug_ts: int
    strategy: str       # "OLD" or "NEW_EFFICIENT" or "NEW_FLASH"
    direction: str      # "UP" or "DOWN"
    elapsed_secs: int
    t_remaining: int
    entry_ask: float
    gvm_conf: float
    edge: float
    won: bool
    pnl_val: float


# ─── Data fetching ───────────────────────────────────────────────────────────────

async def fetch_window(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    slug_ts: int,
) -> Optional[WindowData]:
    slug = f"btc-updown-5m-{slug_ts}"

    async with sem:
        # 1. Gamma API — get outcome
        try:
            r = await client.get(f"{GAMMA}/events?slug={slug}", timeout=8)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None

        if not data:
            return None
        m = data[0].get("markets", [{}])[0]
        if not m.get("closed"):
            return None  # not yet resolved

        try:
            outcomes = m["outcomes"]
            prices = m["outcomePrices"]
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            up_idx = outcomes.index("Up")
            final_up_price = float(prices[up_idx])
            result = "UP" if final_up_price > 0.9 else "DOWN"
        except Exception:
            return None

        # 2. Binance 1m klines — intrabar path (6 candles covers the 5m window)
        try:
            r2 = await client.get(
                f"{BINANCE}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": slug_ts * 1000,
                    "limit": 6,
                },
                timeout=8,
            )
            r2.raise_for_status()
            raw = r2.json()
        except Exception:
            return None

        if not raw or len(raw) < 3:
            return None

        # Price-to-beat = open of the first 1m candle at slug_ts (≈ Chainlink price)
        ptb = float(raw[0][1])
        klines = [
            (float(k[1]), float(k[2]), float(k[3]), float(k[4]))  # open,high,low,close
            for k in raw
        ]

        return WindowData(
            slug_ts=slug_ts,
            ptb=ptb,
            result=result,
            klines_1m=klines,
        )


# ─── Strategy simulation ──────────────────────────────────────────────────────────

def simulate_window(wd: WindowData) -> list[TradeRecord]:
    """
    For a single resolved window, determine whether the OLD and NEW strategies
    would have entered a trade (at each 1-minute check point) and what the
    outcome would have been.

    Returns a list of TradeRecord objects (one per triggered entry).
    """
    trades: list[TradeRecord] = []
    ptb = wd.ptb
    result = wd.result

    # Simulate checking at elapsed = 60, 120, 180, 240 s (after >=60s)
    check_times_elapsed = [60, 120, 180, 240]

    for elapsed in check_times_elapsed:
        t_remaining = INTERVAL - elapsed
        if t_remaining <= 0:
            continue

        # Approximate current price from 1m klines
        # elapsed=60 → end of 1st minute → klines_1m[0][3] (close of minute 0)
        # elapsed=120 → end of 2nd minute → klines_1m[1][3]
        candle_idx = elapsed // 60 - 1   # 0-indexed
        if candle_idx >= len(wd.klines_1m):
            continue
        current_price = wd.klines_1m[candle_idx][3]   # close of that minute

        # 30-second momentum: change from open of SAME candle to its close
        # (proxy for the 30-second move near check time)
        candle_open = wd.klines_1m[candle_idx][0]
        momentum_pct = (current_price - candle_open) / candle_open * 100.0

        # 15-second reversal proxy: compare candle close to its high/low
        # A large wick against our direction signals reversal
        c_high = wd.klines_1m[candle_idx][1]
        c_low  = wd.klines_1m[candle_idx][2]
        # For UP: how much did price drop from its high in this candle?
        reversal_against_up  = (c_high - current_price) / current_price * 100.0
        reversal_against_down = (current_price - c_low)  / current_price * 100.0

        # GBM probability
        pu = p_up(current_price, ptb, t_remaining)
        pd = 1.0 - pu
        if pu >= 0.5:
            side = "UP";   cex_p = pu; conf = pu
            reversal = reversal_against_up
        else:
            side = "DOWN"; cex_p = pd; conf = pd
            reversal = reversal_against_down

        won = (side == result)

        # ── OLD STRATEGY ───────────────────────────────────────────────────────
        if (
            OLD_MIN_REMAINING <= t_remaining <= OLD_MAX_REMAINING
            and elapsed >= OLD_MIN_ELAPSED
            and conf >= OLD_MIN_CONF
        ):
            ask_old = min(0.98, cex_p + SPREAD)
            e_old   = net_edge(cex_p, ask_old)
            p_old   = pnl(ask_old, won)
            trades.append(TradeRecord(
                slug_ts=wd.slug_ts,
                strategy="OLD",
                direction=side,
                elapsed_secs=elapsed,
                t_remaining=t_remaining,
                entry_ask=ask_old,
                gvm_conf=conf,
                edge=e_old,
                won=won,
                pnl_val=p_old,
            ))

        # ── NEW STRATEGY — efficient market assumption (PM tracks GBM) ────────
        mom_ok    = (side == "UP"   and momentum_pct >= NEW_MIN_MOMENTUM_PCT) or \
                    (side == "DOWN" and momentum_pct <= -NEW_MIN_MOMENTUM_PCT)
        rev_ok    = reversal < NEW_MAX_REVERSAL_PCT
        time_ok   = elapsed >= NEW_MIN_ELAPSED and t_remaining >= INTERVAL * NEW_MIN_REM_FRAC
        conf_ok   = conf >= NEW_MIN_CONF

        if time_ok and conf_ok and mom_ok and rev_ok:
            ask_eff = min(0.98, cex_p + SPREAD)
            e_eff   = net_edge(cex_p, ask_eff)
            if e_eff >= NEW_MIN_EDGE:
                trades.append(TradeRecord(
                    slug_ts=wd.slug_ts,
                    strategy="NEW_EFFICIENT",
                    direction=side,
                    elapsed_secs=elapsed,
                    t_remaining=t_remaining,
                    entry_ask=ask_eff,
                    gvm_conf=conf,
                    edge=e_eff,
                    won=won,
                    pnl_val=pnl(ask_eff, won),
                ))

            # ── NEW STRATEGY — genuine lag scenario (PM stale by 6pp) ─────────
            ask_lag = max(0.02, cex_p - LAG)
            e_lag   = net_edge(cex_p, ask_lag)
            if e_lag >= NEW_MIN_EDGE:
                trades.append(TradeRecord(
                    slug_ts=wd.slug_ts,
                    strategy="NEW_FLASH",
                    direction=side,
                    elapsed_secs=elapsed,
                    t_remaining=t_remaining,
                    entry_ask=ask_lag,
                    gvm_conf=conf,
                    edge=e_lag,
                    won=won,
                    pnl_val=pnl(ask_lag, won),
                ))

    return trades


# ─── Statistics ───────────────────────────────────────────────────────────────────

def print_stats(label: str, trades: list[TradeRecord]) -> None:
    if not trades:
        print(f"\n{label}: NO TRADES")
        return

    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    win_rate = wins / n
    total_pnl = sum(t.pnl_val for t in trades)
    avg_pnl = total_pnl / n
    avg_conf = sum(t.gvm_conf for t in trades) / n
    avg_ask  = sum(t.entry_ask for t in trades) / n
    avg_edge = sum(t.edge for t in trades) / n

    # P&L per $1000 portfolio (100 trades × $10 each)
    portfolio_pnl = total_pnl / n * 1000  # per $1000 invested across n trades

    # Running drawdown
    running = 0.0; peak = 0.0; max_dd = 0.0
    for t in trades:
        running += t.pnl_val
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    # Breakdown by elapsed time
    by_elapsed: dict[int, list] = {}
    for t in trades:
        by_elapsed.setdefault(t.elapsed_secs, []).append(t)

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  Trades:          {n:>6}")
    print(f"  Wins / Losses:   {wins:>4} / {n-wins}")
    print(f"  Win rate:        {win_rate:>6.1%}")
    print(f"  Avg GBM conf:    {avg_conf:>6.1%}")
    print(f"  Avg entry ask:   {avg_ask:>6.3f}")
    print(f"  Avg edge:        {avg_edge:>+6.3f}")
    print(f"  Total P&L:       {total_pnl:>+7.3f}  (normalized, 1 USD per trade)")
    print(f"  Avg P&L/trade:   {avg_pnl:>+7.4f}")
    print(f"  Max drawdown:    {max_dd:>+7.3f}")
    print(f"  Break-even wr:   {avg_ask / (avg_ask + (1-avg_ask)*(1-FEE_RATE*(1-avg_ask))):>6.1%}  (approx)")
    print()
    print(f"  By entry time:")
    for elapsed in sorted(by_elapsed):
        sub = by_elapsed[elapsed]
        wr = sum(1 for t in sub if t.won) / len(sub)
        avg_p = sum(t.pnl_val for t in sub) / len(sub)
        print(f"    T={300-elapsed:3d}s remaining ({elapsed:3d}s elapsed):  "
              f"n={len(sub):4d}  wr={wr:.1%}  avg_pnl={avg_p:+.4f}")


# ─── Main ─────────────────────────────────────────────────────────────────────────

async def run_backtest() -> None:
    now = int(time.time())
    # Generate sample timestamps: every SAMPLE_EVERY_N-th 5m window over LOOKBACK_DAYS
    all_ts = []
    lookback_secs = LOOKBACK_DAYS * 24 * 3600
    start_ts = (now // 300) * 300 - lookback_secs
    ts = start_ts
    while ts < now - 600:   # leave last 10 minutes (might not be resolved)
        all_ts.append(ts)
        ts += 300 * SAMPLE_EVERY_N

    print(f"Backtest: {len(all_ts)} sample windows over last {LOOKBACK_DAYS} days")
    print(f"Date range: {__import__('datetime').datetime.utcfromtimestamp(all_ts[0])} → "
          f"{__import__('datetime').datetime.utcfromtimestamp(all_ts[-1])}")
    print("Fetching data...")

    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=30, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [fetch_window(client, sem, ts) for ts in all_ts]
        results = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            wd = await coro
            results.append(wd)
            done += 1
            if done % 100 == 0:
                valid = sum(1 for r in results if r is not None)
                print(f"  {done}/{len(tasks)} fetched, {valid} valid...", flush=True)

    windows = [r for r in results if r is not None]
    print(f"\nFetched {len(windows)} resolved windows (out of {len(all_ts)} attempted)")

    if not windows:
        print("No data — exiting")
        return

    # ── Simulate strategies ────────────────────────────────────────────────────
    all_trades: list[TradeRecord] = []
    for wd in windows:
        all_trades.extend(simulate_window(wd))

    old_trades  = [t for t in all_trades if t.strategy == "OLD"]
    new_eff     = [t for t in all_trades if t.strategy == "NEW_EFFICIENT"]
    new_flash   = [t for t in all_trades if t.strategy == "NEW_FLASH"]

    # Deduplicate: only keep the FIRST entry per window per strategy
    def first_per_window(trades: list[TradeRecord]) -> list[TradeRecord]:
        seen: set = set()
        out = []
        for t in sorted(trades, key=lambda x: x.elapsed_secs):
            if t.slug_ts not in seen:
                seen.add(t.slug_ts)
                out.append(t)
        return out

    old_dedup   = first_per_window(old_trades)
    new_eff_d   = first_per_window(new_eff)
    new_flash_d = first_per_window(new_flash)

    # ── Print results ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  BACKTEST RESULTS  (30 days BTC 5m markets)")
    print(f"  Assumption: buy at GBM_prob + {SPREAD:.3f} (efficient market)")
    print(f"  Windows analysed: {len(windows)}")
    print(f"{'='*60}")

    print_stats("OLD STRATEGY (no momentum filter)", old_dedup)
    print_stats(
        f"NEW STRATEGY — efficient market (PM tracks GBM, spread+{SPREAD:.3f})",
        new_eff_d,
    )
    print_stats(
        f"NEW STRATEGY — genuine lag scenario (PM stale by {LAG:.2f} on flash moves)",
        new_flash_d,
    )

    # ── Overall window direction accuracy ──────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  DIRECTION ACCURACY: when new-strategy flash condition is met")
    print(f"{'─'*60}")
    # In windows where NEW_FLASH fires, what % was the final result correct?
    flash_windows = {t.slug_ts: t for t in new_flash_d}
    if flash_windows:
        correct = sum(1 for t in flash_windows.values() if t.won)
        total = len(flash_windows)
        print(f"  Flash-move windows: {total}")
        print(f"  Correct direction:  {correct}/{total} = {correct/total:.1%}")

    # ── GBM calibration check ─────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  GBM CALIBRATION (old strategy — all entry points)")
    print(f"{'─'*60}")
    buckets = [(0.85, 0.87), (0.87, 0.90), (0.90, 0.93), (0.93, 0.97), (0.97, 1.01)]
    for lo, hi in buckets:
        bucket_trades = [t for t in old_trades if lo <= t.gvm_conf < hi]
        if bucket_trades:
            wr = sum(1 for t in bucket_trades if t.won) / len(bucket_trades)
            avg_c = sum(t.gvm_conf for t in bucket_trades) / len(bucket_trades)
            print(f"  conf [{lo:.0%}-{hi:.0%}):  n={len(bucket_trades):5d}  "
                  f"actual_wr={wr:.1%}  (GBM claimed {avg_c:.1%})")


if __name__ == "__main__":
    asyncio.run(run_backtest())
