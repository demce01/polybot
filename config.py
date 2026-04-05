"""
Central configuration — all thresholds, URLs, and constants in one place.

Entry logic research summary (April 2026):
  - Typical 5m BTC window moves: 0.01–0.18% absolute
  - Polymarket orderbook updates within 3–6 s of a CEX move
  - Genuine latency edge only exists for 5–15 s after a flash move (≥0.08 % in 30 s)
  - At calm mid price 0.65, break-even win rate is 68.7 % — requires ≥90 % GBM confidence
  - Entering on slow drifts (no recent flash) pays fees into an already-efficient market

Strategy: only enter on FLASH MOVES where Polymarket is genuinely stale.
"""

# ─── API endpoints ──────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# ─── Binance WebSocket ──────────────────────────────────────────────────────────
_BINANCE_STREAM_LIST = "btcusdt@aggTrade/ethusdt@aggTrade"
BINANCE_WS_URL = f"wss://stream.binance.com:9443/stream?streams={_BINANCE_STREAM_LIST}"
BINANCE_STREAMS = ["btcusdt@aggTrade", "ethusdt@aggTrade"]
BINANCE_PING_INTERVAL = 20
BINANCE_SESSION_HOURS = 23

# ─── Fee structure ──────────────────────────────────────────────────────────────
# Formula: fee_per_share = CRYPTO_FEE_RATE × price × (1 − price)
# Live API returns base_fee=1000 → coefficient 0.10 (10%).
CRYPTO_FEE_RATE = 0.10

# ─── Core signal quality thresholds ────────────────────────────────────────────
# Raised vs. original: research showed the market is NOT lagging on slow drifts.
# Only genuine flash-move opportunities justify paying the taker fee.
MIN_LAG_PROBABILITY_POINTS = 0.05    # Polymarket must lag CEX by ≥5 pp (was 3)
MIN_EDGE_AFTER_FEES = 0.08           # net edge after fees ≥8 pp (was 5)
MIN_CONFIDENCE = 0.87                # GBM confidence ≥87 % — momentum filter is the primary
                                     # gate; confidence prevents entry on truly noisy signals

# ─── Position sizing ────────────────────────────────────────────────────────────
MAX_POSITION_FRACTION = 0.04         # max 4 % of portfolio per trade (was 8)
KELLY_FRACTION = 0.5                 # half-Kelly

# ─── Kill switch ───────────────────────────────────────────────────────────────
MAX_DAILY_DRAWDOWN = 0.20

# ─── Entry timing window ────────────────────────────────────────────────────────
# Only enter between MIN_WINDOW_ELAPSED_SECS into the window and
# (interval_secs × (1 - MIN_TIME_REMAINING_FRACTION)) elapsed.
# This avoids the chaotic first minute and the final 7 % (≈21 s for 5m).
MIN_WINDOW_ELAPSED_SECS = 60         # must wait ≥60 s after window opens
MIN_TIME_REMAINING_FRACTION = 0.07   # must have ≥7 % of window time left

# ─── Flash-move / momentum filter ──────────────────────────────────────────────
# A genuine latency opportunity requires a RECENT sharp CEX move that
# Polymarket has not yet fully priced in.
#
#   MOMENTUM_WINDOW_SECS: how far back to measure the flash move
#   MIN_MOMENTUM_PCT:     minimum price change in that window (% of price)
#                         for BTC 0.08 % ≈ ~$54 move; for ETH ≈ ~$1.63
#   MAX_REVERSAL_PCT:     if price has reversed by this much in the last 15 s
#                         AGAINST our bet direction, skip (spike already faded)
MOMENTUM_WINDOW_SECS = 30
MIN_MOMENTUM_PCT = {
    "BTC": 0.08,   # 0.08 % in 30 s ≈ 3-sigma under normal vol
    "ETH": 0.10,
}
MAX_REVERSAL_PCT = {
    "BTC": 0.06,   # spike reversed >0.06 % in last 15 s → skip
    "ETH": 0.08,
}

# ─── Orderbook quality filter ──────────────────────────────────────────────────
# If the bid-ask spread is wider than this, the market is illiquid / stale.
# A genuine lag in a liquid market shows a tight spread + good edge.
MAX_SPREAD = 0.06                    # max allowed (best_ask − best_bid)

# ─── Market parameters ─────────────────────────────────────────────────────────
INTERVALS = {"5m": 300, "15m": 900}
ASSETS = ["btc", "eth"]
MIN_ORDER_USD = 5.0
MARKET_SCAN_INTERVAL = 30
OPPORTUNITY_CHECK_INTERVAL = 2
POSITION_CHECK_INTERVAL = 10

# ─── Volatility defaults ────────────────────────────────────────────────────────
# Annualised vol ~70 % → per-second = 0.70 / sqrt(365×24×3600)
# These are fallbacks; realized vol is calculated from live price history.
DEFAULT_VOL = {
    "BTC": 0.70 / (365 * 24 * 3600) ** 0.5,   # ≈ 0.000111 /s
    "ETH": 0.85 / (365 * 24 * 3600) ** 0.5,   # ≈ 0.000135 /s
}
REALIZED_VOL_WINDOW = 300

# ─── Rate limits ───────────────────────────────────────────────────────────────
GAMMA_API_RPS = 5.0
CLOB_API_RPS = 5.0

# ─── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH = 0.1

# ─── Retry ─────────────────────────────────────────────────────────────────────
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 1.0
