"""
Data models shared across all modules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(Enum):
    UP = "Up"
    DOWN = "Down"

    @property
    def token_index(self) -> int:
        """clobTokenIds[0] = Up, clobTokenIds[1] = Down."""
        return 0 if self == Side.UP else 1

    def opposite(self) -> "Side":
        return Side.DOWN if self == Side.UP else Side.UP


class PositionStatus(Enum):
    OPEN = "open"
    RESOLVED_WIN = "win"
    RESOLVED_LOSS = "loss"
    CANCELLED = "cancelled"


@dataclass
class MarketInfo:
    """One Polymarket event (e.g. 'BTC Up or Down - 5M')."""
    event_id: str
    market_id: str
    slug: str
    question: str
    asset: str          # "BTC" or "ETH"
    interval: str       # "5m" or "15m"
    interval_secs: int  # 300 or 900
    condition_id: str
    up_token_id: str
    down_token_id: str
    end_date: datetime       # UTC; measurement window end
    slug_ts: int             # Unix timestamp = measurement window START
    resolution_source: str   # e.g. https://data.chain.link/streams/btc-usd
    # Set when measurement window starts:
    price_to_beat: Optional[float] = None
    # Set when market resolves:
    resolved_side: Optional[Side] = None
    is_closed: bool = False

    def token_id_for(self, side: Side) -> str:
        return self.up_token_id if side == Side.UP else self.down_token_id

    @property
    def measurement_start(self) -> float:
        """Unix timestamp of measurement window start."""
        return float(self.slug_ts)

    @property
    def measurement_end(self) -> float:
        """Unix timestamp of measurement window end."""
        return float(self.slug_ts + self.interval_secs)

    def time_remaining(self) -> float:
        """Seconds until market closes (negative if already closed)."""
        return self.measurement_end - time.time()

    def is_in_measurement_window(self) -> bool:
        now = time.time()
        return self.measurement_start <= now <= self.measurement_end

    def seconds_into_window(self) -> float:
        return max(0.0, time.time() - self.measurement_start)


@dataclass
class OrderBookSnapshot:
    token_id: str
    best_ask: float         # lowest ask (price to buy the token)
    best_bid: float         # highest bid
    mid: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class Position:
    """A placed trade (paper or live)."""
    position_id: str
    market: MarketInfo
    side: Side
    token_id: str
    entry_price: float       # price per share paid
    size_shares: float       # number of shares purchased
    cost_usd: float          # entry_price * size_shares (before fee)
    fee_paid_usd: float      # fee paid at entry
    total_spent_usd: float   # cost_usd + fee_paid_usd
    entry_time: float = field(default_factory=time.time)
    order_id: Optional[str] = None    # CLOB order ID (live only)
    status: PositionStatus = PositionStatus.OPEN
    pnl_usd: Optional[float] = None   # set on resolution

    @property
    def potential_payout(self) -> float:
        """Maximum payout if we win (size_shares × $1)."""
        return self.size_shares

    def calculate_pnl(self, won: bool) -> float:
        if won:
            return self.size_shares - self.total_spent_usd
        else:
            return -self.total_spent_usd


@dataclass
class OpportunitySignal:
    """A detected latency-arbitrage opportunity."""
    market: MarketInfo
    side: Side                    # which side to bet
    token_id: str
    polymarket_ask: float         # price to pay per share
    cex_probability: float        # CEX-implied probability (GBM)
    raw_lag_pp: float             # |cex_p - polymarket_mid| in probability points
    edge_after_fees: float        # net edge in probability units after fees
    confidence: float             # max(cex_p, 1-cex_p); must be > 0.85
    kelly_fraction: float         # half-Kelly fraction
    suggested_size_usd: float     # USD to bet
    fee_estimate_usd: float       # estimated fee for this trade
    time_remaining: float         # seconds until market closes
    asset: str                    # "BTC" or "ETH"
    interval: str                 # "5m" or "15m"


@dataclass
class TradeResult:
    """Final outcome of a resolved position."""
    position: Position
    resolved_side: Side
    pnl_usd: float
    resolution_time: float = field(default_factory=time.time)


@dataclass
class BotState:
    """Mutable state passed to the dashboard renderer."""
    mode: str = "PAPER"               # "PAPER" or "LIVE"
    is_halted: bool = False
    halt_reason: str = ""
    balance_usdc: float = 0.0
    day_start_balance: float = 0.0
    daily_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    open_positions: list[Position] = field(default_factory=list)
    recent_signals: list[OpportunitySignal] = field(default_factory=list)
    market_snapshots: dict[str, dict] = field(default_factory=dict)  # slug → snapshot data
    log_lines: list[str] = field(default_factory=list)
    btc_price: float = 0.0
    eth_price: float = 0.0
    last_update: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        resolved = self.total_trades - len(self.open_positions)
        if resolved == 0:
            return 0.0
        return self.winning_trades / resolved

    def add_log(self, msg: str) -> None:
        from rich.markup import escape
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        # Escape both the timestamp brackets and any message content so Rich
        # does not misinterpret them as markup tags.
        self.log_lines.append(f"[dim]{ts}[/dim] {escape(msg)}")
        if len(self.log_lines) > 50:
            self.log_lines.pop(0)
