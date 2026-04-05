"""
Thread-safe rolling price history buffer.
Stores (timestamp, price) tuples for a configurable lookback window.
Used to retrieve the Binance price at measurement window start (price_to_beat).
"""

from __future__ import annotations

import math
import time
from collections import deque
from threading import Lock
from typing import Optional


class PriceHistory:
    """
    Rolling buffer of (unix_timestamp, price) samples.

    One instance per asset (BTC, ETH).
    """

    def __init__(self, max_age_secs: float = 1800.0) -> None:
        self._buf: deque[tuple[float, float]] = deque()
        self._max_age = max_age_secs
        self._lock = Lock()

    # ── Writes ──────────────────────────────────────────────────────────────────

    def add(self, price: float, ts: Optional[float] = None) -> None:
        """Add a price sample.  ts defaults to now."""
        if ts is None:
            ts = time.time()
        with self._lock:
            self._buf.append((ts, price))
            self._evict()

    # ── Reads ───────────────────────────────────────────────────────────────────

    def latest(self) -> Optional[float]:
        """Most recent price, or None if buffer empty."""
        with self._lock:
            if not self._buf:
                return None
            return self._buf[-1][1]

    def at(self, target_ts: float, tolerance_secs: float = 60.0) -> Optional[float]:
        """
        Price closest to target_ts, or None if no sample is within tolerance.
        """
        with self._lock:
            if not self._buf:
                return None
            closest = min(self._buf, key=lambda x: abs(x[0] - target_ts))
            if abs(closest[0] - target_ts) <= tolerance_secs:
                return closest[1]
            return None

    def recent_change_pct(self, window_secs: float = 30.0) -> Optional[float]:
        """
        Percentage price change from window_secs ago to the latest sample.
        Positive = price went up, negative = price went down.
        Returns None if there is no sample within 2× window_secs.
        """
        with self._lock:
            if len(self._buf) < 2:
                return None
            latest_ts, latest_price = self._buf[-1]
            target_ts = latest_ts - window_secs
            # Find sample closest to target_ts
            best = min(self._buf, key=lambda x: abs(x[0] - target_ts))
            # Reject if the closest sample is more than 2× the window away
            if abs(best[0] - target_ts) > window_secs * 2:
                return None
            if best[1] <= 0:
                return None
            return (latest_price - best[1]) / best[1] * 100.0

    def realized_vol_per_second(
        self,
        window_secs: float = 300.0,
        default_vol: float = 0.0001,
    ) -> float:
        """
        Estimate realised volatility per second from log-returns.
        Falls back to default_vol when data is insufficient.
        """
        with self._lock:
            now = time.time()
            recent = [(t, p) for t, p in self._buf if now - t <= window_secs]

        if len(recent) < 10:
            return default_vol

        log_returns = [
            math.log(recent[i + 1][1] / recent[i][1])
            for i in range(len(recent) - 1)
            if recent[i][1] > 0 and recent[i + 1][1] > 0
        ]
        if len(log_returns) < 2:
            return default_vol

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        vol_per_sample = math.sqrt(variance)

        # Normalise to per-second
        time_span = recent[-1][0] - recent[0][0]
        if time_span <= 0:
            return default_vol
        avg_sample_period = time_span / len(log_returns)
        if avg_sample_period <= 0:
            return default_vol

        return vol_per_sample / math.sqrt(avg_sample_period)

    # ── Internal ────────────────────────────────────────────────────────────────

    def _evict(self) -> None:
        """Remove samples older than max_age.  Must be called under lock."""
        cutoff = time.time() - self._max_age
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
