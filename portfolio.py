"""
Portfolio management: balance tracking, daily P&L, kill-switch enforcement,
and position lifecycle.
"""

from __future__ import annotations

import csv
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from config import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_DRAWDOWN, MIN_ORDER_USD
from models import (
    BotState,
    MarketInfo,
    OpportunitySignal,
    Position,
    PositionStatus,
    Side,
    TradeResult,
)

log = logging.getLogger(__name__)


class Portfolio:
    """
    Tracks cash balance, open positions, and daily P&L.

    Thread-safe (the dashboard and trading tasks access it concurrently).
    """

    def __init__(self, starting_balance: float, mode: str = "PAPER") -> None:
        self._lock = Lock()
        self._balance = starting_balance         # Available USDC
        self._day_start_balance = starting_balance
        self._day_start_ts = time.time()
        self._mode = mode                        # "PAPER" or "LIVE"

        self._positions: dict[str, Position] = {}  # position_id → Position
        self._resolved: list[TradeResult] = []
        self._daily_resolved_pnl = 0.0           # sum of today's resolved P&L
        self._total_trades = 0
        self._winning_trades = 0
        self._consecutive_losses = 0
        self._is_halted = False
        self._halt_reason = ""

        # CSV trade log
        self._csv_path = "trades.csv"
        self._ensure_csv()

    # ── CSV helpers ──────────────────────────────────────────────────────────────

    def _ensure_csv(self) -> None:
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "event_time", "event_type", "position_id",
                    "market_slug", "asset", "interval", "side",
                    "entry_price", "size_shares", "cost_usd", "fee_usd", "total_spent",
                    "resolved_side", "won", "pnl_usd",
                ])

    def _log_csv(self, row: list) -> None:
        try:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except Exception:
            pass

    # ── Public properties ────────────────────────────────────────────────────────

    @property
    def balance(self) -> float:
        with self._lock:
            return self._balance

    @property
    def is_halted(self) -> bool:
        with self._lock:
            return self._is_halted

    @property
    def halt_reason(self) -> str:
        with self._lock:
            return self._halt_reason

    @property
    def daily_pnl(self) -> float:
        with self._lock:
            return self._daily_resolved_pnl

    @property
    def win_rate(self) -> float:
        with self._lock:
            resolved = len(self._resolved)
            if resolved == 0:
                return 0.0
            return self._winning_trades / resolved

    # ── Pre-trade checks ─────────────────────────────────────────────────────────

    def can_trade(self, amount_usd: float) -> tuple[bool, str]:
        """
        Returns (True, "") if we can place a trade of given USD size,
        or (False, reason) otherwise.
        """
        with self._lock:
            if self._is_halted:
                return False, f"Kill switch active: {self._halt_reason}"

            if amount_usd < MIN_ORDER_USD:
                return False, f"Size ${amount_usd:.2f} below minimum ${MIN_ORDER_USD}"

            if self._balance < amount_usd * 1.05:  # 5% buffer for fee
                return False, f"Insufficient balance: ${self._balance:.2f} < ${amount_usd:.2f} needed"

            return True, ""

    def already_has_open_position_on(self, market: MarketInfo) -> bool:
        """True if we already have an open position for this market."""
        with self._lock:
            for p in self._positions.values():
                if p.market.slug == market.slug and p.status == PositionStatus.OPEN:
                    return True
            return False

    # ── Position creation ────────────────────────────────────────────────────────

    def open_position(
        self,
        signal: OpportunitySignal,
        actual_fill_price: float,
        actual_size_usd: float,
        fee_rate: float,
        order_id: Optional[str] = None,
    ) -> Position:
        """
        Record a new open position and deduct from cash balance.
        Call this AFTER an order is confirmed (or immediately in paper mode).
        """
        from probability import fee_per_share

        size_shares = actual_size_usd / actual_fill_price
        fee = size_shares * fee_per_share(actual_fill_price, fee_rate)
        total_spent = actual_size_usd + fee

        pos = Position(
            position_id=str(uuid.uuid4()),
            market=signal.market,
            side=signal.side,
            token_id=signal.token_id,
            entry_price=actual_fill_price,
            size_shares=size_shares,
            cost_usd=actual_size_usd,
            fee_paid_usd=fee,
            total_spent_usd=total_spent,
            order_id=order_id,
        )

        with self._lock:
            self._balance -= total_spent
            self._positions[pos.position_id] = pos
            self._total_trades += 1
            log.info(
                "[%s] Opened %s %s @ %.4f | size=%.2f shares | cost=$%.2f | fee=$%.2f",
                self._mode,
                signal.market.slug,
                signal.side.value,
                actual_fill_price,
                size_shares,
                actual_size_usd,
                fee,
            )

        self._log_csv([
            datetime.now(timezone.utc).isoformat(),
            "OPEN",
            pos.position_id,
            signal.market.slug,
            signal.market.asset,
            signal.market.interval,
            signal.side.value,
            f"{actual_fill_price:.6f}",
            f"{size_shares:.4f}",
            f"{actual_size_usd:.4f}",
            f"{fee:.4f}",
            f"{total_spent:.4f}",
            "", "", "",
        ])
        return pos

    # ── Position resolution ──────────────────────────────────────────────────────

    def resolve_position(self, position_id: str, resolved_side: Side) -> Optional[TradeResult]:
        """
        Close a position based on market resolution.
        Updates balance (add winnings) and applies kill-switch check.
        """
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None or pos.status != PositionStatus.OPEN:
                return None

            won = pos.side == resolved_side
            pnl = pos.calculate_pnl(won)

            pos.status = PositionStatus.RESOLVED_WIN if won else PositionStatus.RESOLVED_LOSS
            pos.pnl_usd = pnl

            # Add payout to balance (shares × $1 if won; $0 if lost)
            if won:
                self._balance += pos.size_shares
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1

            self._daily_resolved_pnl += pnl
            if won:
                self._winning_trades += 1

            result = TradeResult(
                position=pos,
                resolved_side=resolved_side,
                pnl_usd=pnl,
            )
            self._resolved.append(result)

            log.info(
                "[%s] Resolved %s → %s | P&L=$%.2f | daily P&L=$%.2f",
                self._mode,
                pos.market.slug,
                "WIN" if won else "LOSS",
                pnl,
                self._daily_resolved_pnl,
            )

            # Kill-switch check (only on resolved trades, as specified)
            self._check_kill_switch()

        self._log_csv([
            datetime.now(timezone.utc).isoformat(),
            "RESOLVE",
            pos.position_id,
            pos.market.slug,
            pos.market.asset,
            pos.market.interval,
            pos.side.value,
            f"{pos.entry_price:.6f}",
            f"{pos.size_shares:.4f}",
            f"{pos.cost_usd:.4f}",
            f"{pos.fee_paid_usd:.4f}",
            f"{pos.total_spent_usd:.4f}",
            resolved_side.value,
            "1" if won else "0",
            f"{pnl:.6f}",
        ])
        return result

    # ── Balance sync (live mode) ─────────────────────────────────────────────────

    def sync_balance(self, live_balance: float) -> None:
        """Overwrite balance with value fetched from the CLOB (live mode only)."""
        with self._lock:
            self._balance = live_balance

    # ── End-of-day reset ────────────────────────────────────────────────────────

    def new_trading_day(self) -> None:
        with self._lock:
            self._day_start_balance = self._balance
            self._day_start_ts = time.time()
            self._daily_resolved_pnl = 0.0
            # Do NOT reset is_halted automatically — operator must restart bot
            log.info("New trading day: day-start balance = $%.2f", self._day_start_balance)

    # ── Snapshot for dashboard ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "balance": self._balance,
                "day_start_balance": self._day_start_balance,
                "daily_pnl": self._daily_resolved_pnl,
                "total_trades": self._total_trades,
                "winning_trades": self._winning_trades,
                "open_positions": list(self._positions.values()),
                "is_halted": self._is_halted,
                "halt_reason": self._halt_reason,
            }

    def open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    # ── Kill switch ──────────────────────────────────────────────────────────────

    def _check_kill_switch(self) -> None:
        """Must be called under self._lock."""
        if self._is_halted:
            return
        if self._day_start_balance <= 0:
            return
        drawdown = -self._daily_resolved_pnl / self._day_start_balance
        if drawdown >= MAX_DAILY_DRAWDOWN:
            self._is_halted = True
            self._halt_reason = (
                f"Daily drawdown {drawdown:.1%} exceeded {MAX_DAILY_DRAWDOWN:.0%} limit"
            )
            log.warning("KILL SWITCH ACTIVATED: %s", self._halt_reason)
            return
        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self._is_halted = True
            self._halt_reason = (
                f"{self._consecutive_losses} consecutive losses — investigate before resuming"
            )
            log.warning("KILL SWITCH ACTIVATED: %s", self._halt_reason)
