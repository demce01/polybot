# Polymarket Latency Arb Bot — Context

## Strategy
Detect the ~3–6 second window where Polymarket's BTC/ETH up-down markets are stale after a flash CEX move on Binance. Enter only when all 11 signal gates pass. No directional view — purely exploiting Polymarket's lag behind Binance.

## Files
| File | Purpose |
|---|---|
| `main.py` | Orchestrates 7 async tasks (Binance feed, Chainlink, scanner, opportunity monitor, position monitor, day reset, dashboard) |
| `config.py` | All thresholds and constants |
| `probability.py` | GBM model, fee calculation, 11-gate `evaluate_opportunity()` |
| `price_history.py` | Thread-safe rolling price buffer; `recent_change_pct()` for momentum detection |
| `market_finder.py` | Fetches BTC/ETH 5m/15m markets by slug from Gamma API; `ensure_price_to_beat()` via Chainlink → Binance fallback |
| `binance_feed.py` | Binance WebSocket aggTrade feed for BTC and ETH |
| `chainlink.py` | Polls Chainlink oracle on Polygon every 25s; `get_price_at(asset, ts)` for price-to-beat lookup |
| `models.py` | `MarketInfo`, `Position`, `OpportunitySignal`, `BotState`, `OrderBookSnapshot` |
| `portfolio.py` | Balance, position lifecycle, kill switch (20% daily drawdown) |
| `trader.py` | `PaperTrader` (instant fill simulation) and `LiveTrader` (py-clob-client FOK orders) |
| `dashboard.py` | Rich terminal UI: header, active markets, open positions, resolved positions, log |
| `backtest.py` | 720-window async backtest over 30 days; simulates OLD/NEW_EFFICIENT/NEW_FLASH strategies |

## Signal Gates (all 11 must pass)
1. `price_to_beat` is known (Chainlink oracle price at window start)
2. Inside measurement window
3. ≥ 60s elapsed since window opened (`MIN_WINDOW_ELAPSED_SECS`)
4. ≥ 7% of window time remaining (`MIN_TIME_REMAINING_FRACTION`)
5. Bid-ask spread ≤ 0.06 (`MAX_SPREAD`)
6. Orderbook price sanity (ask between 0.01–0.99)
7. Flash move: ≥ 0.08% BTC / 0.10% ETH move in last 30s in bet direction
8. No reversal: spike hasn't already faded ≥ 0.06% in last 15s
9. Polymarket lag ≥ 5pp behind GBM probability (`MIN_LAG_PROBABILITY_POINTS`)
10. GBM confidence ≥ 87% (`MIN_CONFIDENCE`)
11. Net edge after fees ≥ 8pp (`MIN_EDGE_AFTER_FEES`)

Plus 2 portfolio-level checks: sufficient balance, kill switch not active.

## Key Config Values
```python
MIN_LAG_PROBABILITY_POINTS = 0.05
MIN_EDGE_AFTER_FEES        = 0.08
MIN_CONFIDENCE             = 0.87
MAX_POSITION_FRACTION      = 0.04   # 4% of portfolio per trade
KELLY_FRACTION             = 0.5    # half-Kelly sizing
MAX_DAILY_DRAWDOWN         = 0.20   # kill switch
MOMENTUM_WINDOW_SECS       = 30
MIN_MOMENTUM_PCT           = {"BTC": 0.08, "ETH": 0.10}
MAX_REVERSAL_PCT           = {"BTC": 0.06, "ETH": 0.08}
MAX_SPREAD                 = 0.06
CRYPTO_FEE_RATE            = 0.10   # Polymarket fee: rate × price × (1 − price)
DEFAULT_VOL                = {"BTC": 0.70/sqrt(365×24×3600), "ETH": 0.85/...}
DASHBOARD_REFRESH          = 0.1    # 100ms
```

## GBM Model
```
P(S_T >= K | S_t = S) = N(d2)
d2 = (ln(S/K) − 0.5 × σ² × T) / (σ × √T)
```
- `S` = current BTC/ETH price (from Binance)
- `K` = price at window start (from Chainlink oracle)
- `T` = seconds remaining in window
- `σ` = realised vol per second (calculated from last 300s of price history)

## Fee Formula
```python
fee_per_share = 0.10 × price × (1 − price)
edge = cex_probability − polymarket_ask − fee_per_share(polymarket_ask)
```

## Markets
- BTC and ETH, 5-minute and 15-minute intervals
- Slug format: `{asset}-updown-{interval}-{unix_ts}`
- Resolution: Chainlink Data Streams oracle on Polygon
- Price-to-beat = Chainlink price at `slug_ts` (window start)

## Backtest Results (30 days, 720 windows)
- Old strategy (no momentum filter): 299 trades, 95% win rate, avg edge −0.018 (PM priced in already)
- New flash strategy: 0 trades (1m klines too coarse to detect 30s spikes)
- GBM calibration: consistently underestimates win rate by ~4pp
- Conclusion: direction signal is real but PM prices the premium in on slow drifts; flash-only strategy is correct

## Running
```bash
# Paper (default)
python main.py

# Live
python main.py --live --confirm-live --accept-risk

# Env vars needed for live
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
```

## Logs
Logs go to `bot.log` (never to stderr — prevents bleeding into Rich Live display).

## Known Issues / Decisions
- `polygon-rpc.com` returns 401; use `polygon.drpc.org` or `1rpc.io/matic` for Chainlink RPC
- Gamma API has no explicit price-to-beat field; Chainlink is the authoritative source
- GBM underestimates win rate (actual ~95% vs model ~91.6%) — model is conservative
- The flash-move filter is the primary gate; GBM confidence prevents entry on noisy signals
- Dashboard uses `screen=True`; all logging must go to file to avoid display corruption

## GitHub
`https://github.com/demce01/polymarket-bot` (private)
