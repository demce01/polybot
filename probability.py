"""
CEX-implied probability calculation and trade-signal evaluation.

Uses Geometric Brownian Motion (GBM) to estimate the probability that
price at measurement window end >= price at window start.

  P(S_T >= K | S_t = S) = N(d2)
  d2 = (ln(S/K) - 0.5 * σ² * T) / (σ * √T)

Research findings (April 2026):
  - Polymarket orderbook tracks GBM probability within 3–6 s on slow drifts
  - Genuine latency gaps only appear for ~5–15 s after flash CEX moves (≥0.08%)
  - Entry filters must require RECENT sharp move, not just a high GBM probability
"""

from __future__ import annotations

import math
import logging
from typing import Optional, TYPE_CHECKING

from scipy.stats import norm

from config import (
    CRYPTO_FEE_RATE,
    DEFAULT_VOL,
    MAX_REVERSAL_PCT,
    MAX_SPREAD,
    MIN_CONFIDENCE,
    MIN_EDGE_AFTER_FEES,
    MIN_LAG_PROBABILITY_POINTS,
    MIN_MOMENTUM_PCT,
    MIN_TIME_REMAINING_FRACTION,
    MIN_WINDOW_ELAPSED_SECS,
    MOMENTUM_WINDOW_SECS,
)
from models import MarketInfo, OpportunitySignal, OrderBookSnapshot, Side

if TYPE_CHECKING:
    from price_history import PriceHistory

log = logging.getLogger(__name__)


# ── Probability model ────────────────────────────────────────────────────────────

def estimate_p_up(
    current_price: float,
    start_price: float,
    time_remaining_secs: float,
    vol_per_second: Optional[float] = None,
    asset: str = "BTC",
) -> float:
    """
    Probability that the asset ends >= start_price given current_price.
    Returns 0.5 on edge cases (unknown start price, zero time).
    """
    if start_price is None or start_price <= 0 or current_price <= 0:
        return 0.5

    if time_remaining_secs <= 0:
        return 1.0 if current_price >= start_price else 0.0

    if vol_per_second is None:
        vol_per_second = DEFAULT_VOL.get(asset, 0.0001)

    sigma_sqrt_t = vol_per_second * math.sqrt(time_remaining_secs)
    if sigma_sqrt_t < 1e-12:
        return 1.0 if current_price >= start_price else 0.0

    try:
        d2 = (
            math.log(current_price / start_price)
            - 0.5 * (vol_per_second ** 2) * time_remaining_secs
        ) / sigma_sqrt_t
        return float(norm.cdf(d2))
    except (ValueError, ZeroDivisionError):
        return 0.5


# ── Fee calculation ──────────────────────────────────────────────────────────────

def fee_per_share(price: float, fee_rate: float = CRYPTO_FEE_RATE) -> float:
    """
    Polymarket taker fee per share:
        fee = fee_rate × price × (1 − price)
    """
    return fee_rate * price * (1.0 - price)


def total_cost_per_share(price: float, fee_rate: float = CRYPTO_FEE_RATE) -> float:
    return price + fee_per_share(price, fee_rate)


def net_edge(
    cex_probability: float,
    polymarket_ask: float,
    fee_rate: float = CRYPTO_FEE_RATE,
) -> float:
    """
    Net expected profit per share (in probability units / dollars):
        edge = cex_p − ask − fee_per_share(ask)

    Positive means we expect to profit; must exceed MIN_EDGE_AFTER_FEES.
    """
    return cex_probability - polymarket_ask - fee_per_share(polymarket_ask, fee_rate)


# ── Kelly criterion ──────────────────────────────────────────────────────────────

def half_kelly_fraction(
    probability: float,
    ask_price: float,
    fee_rate: float = CRYPTO_FEE_RATE,
) -> float:
    """
    Half-Kelly fraction of portfolio to bet.

    For a binary bet where payout is $1 per share:
        odds (b) = net_payout / cost = (1 − ask − fee) / (ask + fee)
        f_kelly  = (b * p − q) / b
        f_half   = f_kelly / 2

    Returns 0 if Kelly is negative (no edge).
    """
    cost = total_cost_per_share(ask_price, fee_rate)
    net_win = 1.0 - cost
    if net_win <= 0 or cost <= 0:
        return 0.0

    b = net_win / cost
    p = probability
    q = 1.0 - p

    kelly = (b * p - q) / b
    return max(0.0, kelly / 2.0)


# ── Flash-move / momentum helpers ────────────────────────────────────────────────

def _momentum_ok(
    hist: "PriceHistory",
    side: Side,
    asset: str,
) -> bool:
    """
    Returns True if there has been a significant RECENT price move in the
    direction of `side` (indicating a potential latency window is open).

    Uses MOMENTUM_WINDOW_SECS look-back and per-asset thresholds from config.
    If price history is unavailable, fail safe (return False).
    """
    change = hist.recent_change_pct(MOMENTUM_WINDOW_SECS)
    if change is None:
        return False  # no data → don't trade blind

    threshold = MIN_MOMENTUM_PCT.get(asset, 0.08)
    if side == Side.UP:
        return change >= threshold
    else:
        return change <= -threshold


def _no_reversal(
    hist: "PriceHistory",
    side: Side,
    asset: str,
) -> bool:
    """
    Returns True if the price has NOT already reversed strongly against our
    bet direction in the last 15 seconds.

    A reversal means the flash spike has already faded — the lag is gone.
    """
    reversal_check_window = 15.0
    change_15s = hist.recent_change_pct(reversal_check_window)
    if change_15s is None:
        return True  # no data — don't block on uncertainty

    max_rev = MAX_REVERSAL_PCT.get(asset, 0.06)
    if side == Side.UP:
        # A strong DOWN move in the last 15 s means the spike already reversed
        return change_15s >= -max_rev
    else:
        return change_15s <= max_rev


# ── Opportunity evaluation ───────────────────────────────────────────────────────

def evaluate_opportunity(
    market: MarketInfo,
    ob: OrderBookSnapshot,
    current_price: float,
    vol_per_second: Optional[float],
    portfolio_value: float,
    price_hist: Optional["PriceHistory"] = None,
    fee_rate: float = CRYPTO_FEE_RATE,
) -> Optional[OpportunitySignal]:
    """
    Evaluate whether a tradeable latency-arb opportunity exists.

    Returns an OpportunitySignal if ALL filters pass, else None.

    Filters (in order):
      1.  price_to_beat must be known
      2.  Market must be inside its measurement window
      3.  Must be ≥ MIN_WINDOW_ELAPSED_SECS into the window (momentum builds)
      4.  Must have ≥ MIN_TIME_REMAINING_FRACTION of window left (not at end)
      5.  Bid-ask spread ≤ MAX_SPREAD (liquid, responsive market)
      6.  Flash-move detected: recent CEX move ≥ MIN_MOMENTUM_PCT in bet direction
      7.  No reversal: flash has not already faded in the last 15 s
      8.  CEX probability lag vs. Polymarket mid > MIN_LAG_PROBABILITY_POINTS
      9.  Confidence (max(p_up, 1-p_up)) > MIN_CONFIDENCE
      10. Net edge after fees > MIN_EDGE_AFTER_FEES
    """

    # 1. Need a known start price
    if market.price_to_beat is None:
        return None

    time_remaining = market.time_remaining()

    # 2. Must be inside the measurement window
    if not market.is_in_measurement_window():
        return None

    # 3. Too early — let the market establish a direction first
    secs_elapsed = market.seconds_into_window()
    if secs_elapsed < MIN_WINDOW_ELAPSED_SECS:
        return None

    # 4. Too close to the end — mean-reversion noise dominates
    min_remaining = market.interval_secs * MIN_TIME_REMAINING_FRACTION
    if time_remaining < min_remaining:
        return None

    # 5. Spread guard — avoid illiquid or very stale quotes
    spread = ob.best_ask - ob.best_bid
    if spread > MAX_SPREAD:
        return None

    # Guard against degenerate order book prices
    if ob.best_ask <= 0.01 or ob.best_ask >= 0.99:
        return None

    # 6-7. Flash-move and reversal checks (require price history)
    # Calculate CEX probability first to determine which side to bet
    p_up_cex = estimate_p_up(
        current_price=current_price,
        start_price=market.price_to_beat,
        time_remaining_secs=time_remaining,
        vol_per_second=vol_per_second,
        asset=market.asset,
    )

    if p_up_cex >= 0.5:
        side = Side.UP
        polymarket_ask = ob.best_ask
        cex_p = p_up_cex
        polymarket_mid = ob.mid
    else:
        side = Side.DOWN
        polymarket_ask = 1.0 - ob.best_bid
        cex_p = 1.0 - p_up_cex
        polymarket_mid = 1.0 - ob.mid

    if polymarket_ask <= 0.01 or polymarket_ask >= 0.99:
        return None

    if price_hist is not None:
        # 6. Flash-move filter: there must be a RECENT sharp CEX move in our direction.
        #    Without this, we are entering on a slow drift that Polymarket has already
        #    priced — we pay fees into an efficient market.
        if not _momentum_ok(price_hist, side, market.asset):
            return None

        # 7. Reversal guard: if the spike has already faded, the lag is gone.
        if not _no_reversal(price_hist, side, market.asset):
            return None

    # 8. Lag filter: Polymarket must not have fully priced in the move yet
    lag = cex_p - polymarket_mid
    if abs(lag) < MIN_LAG_PROBABILITY_POINTS:
        return None

    # 9. Confidence: GBM must be certain enough to overcome fees + uncertainty
    confidence = max(p_up_cex, 1.0 - p_up_cex)
    if confidence < MIN_CONFIDENCE:
        return None

    # 10. Net edge after fees
    edge = net_edge(cex_p, polymarket_ask, fee_rate)
    if edge < MIN_EDGE_AFTER_FEES:
        return None

    # Kelly sizing
    kf = half_kelly_fraction(cex_p, polymarket_ask, fee_rate)
    from config import MAX_POSITION_FRACTION, MIN_ORDER_USD
    raw_usd = kf * portfolio_value
    size_usd = min(raw_usd, MAX_POSITION_FRACTION * portfolio_value)
    size_usd = max(size_usd, MIN_ORDER_USD)

    token_id = market.token_id_for(side)
    size_shares = size_usd / polymarket_ask
    fee_est = size_shares * fee_per_share(polymarket_ask, fee_rate)

    return OpportunitySignal(
        market=market,
        side=side,
        token_id=token_id,
        polymarket_ask=polymarket_ask,
        cex_probability=cex_p,
        raw_lag_pp=lag,
        edge_after_fees=edge,
        confidence=confidence,
        kelly_fraction=kf,
        suggested_size_usd=size_usd,
        fee_estimate_usd=fee_est,
        time_remaining=time_remaining,
        asset=market.asset,
        interval=market.interval,
    )
