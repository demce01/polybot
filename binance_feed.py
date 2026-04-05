"""
Binance WebSocket price feed.
Subscribes to BTC/USDT and ETH/USDT aggTrade streams and feeds prices into
PriceHistory instances.  Auto-reconnects on disconnection or 23-hour session
limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets
import websockets.exceptions

from config import (
    BINANCE_PING_INTERVAL,
    BINANCE_SESSION_HOURS,
    BINANCE_STREAMS,
    BINANCE_WS_URL,
)
from price_history import PriceHistory

log = logging.getLogger(__name__)


class BinanceFeed:
    """
    Maintains a persistent WebSocket connection to Binance aggTrade streams
    for BTCUSDT and ETHUSDT.

    Prices are pushed into PriceHistory objects that other components read.
    """

    def __init__(self) -> None:
        self.btc = PriceHistory(max_age_secs=1800)
        self.eth = PriceHistory(max_age_secs=1800)
        self._running = False
        self._last_reconnect = 0.0
        # Callbacks fired on each new price: (asset, price, timestamp)
        self._callbacks: list[Callable[[str, float, float], None]] = []

    # ── Public API ──────────────────────────────────────────────────────────────

    def add_callback(self, fn: Callable[[str, float, float], None]) -> None:
        self._callbacks.append(fn)

    def latest_btc(self) -> Optional[float]:
        return self.btc.latest()

    def latest_eth(self) -> Optional[float]:
        return self.eth.latest()

    def btc_at(self, ts: float) -> Optional[float]:
        return self.btc.at(ts)

    def eth_at(self, ts: float) -> Optional[float]:
        return self.eth.at(ts)

    async def start(self) -> None:
        """Run forever — reconnects automatically."""
        self._running = True
        backoff = 1.0
        session_start = time.time()

        while self._running:
            try:
                await self._connect_and_stream(session_start)
                backoff = 1.0  # reset on clean exit
            except asyncio.CancelledError:
                log.info("BinanceFeed cancelled")
                break
            except Exception as exc:
                log.warning("BinanceFeed error: %s — retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

            # Force reconnect after 23 hours to stay under Binance's 24 h limit
            if time.time() - session_start > BINANCE_SESSION_HOURS * 3600:
                session_start = time.time()
                log.info("BinanceFeed: proactive reconnect after %d hours", BINANCE_SESSION_HOURS)

    def stop(self) -> None:
        self._running = False

    # ── Internal ────────────────────────────────────────────────────────────────

    async def _connect_and_stream(self, session_start: float) -> None:
        # Streams are embedded in the URL — no SUBSCRIBE message needed.
        # The /stream endpoint requires query-string subscription;
        # connecting to /stream without ?streams= causes immediate close.
        async with websockets.connect(
            BINANCE_WS_URL,
            ping_interval=20,     # library sends client-side pings every 20 s
            ping_timeout=60,      # drop connection if no pong within 60 s
            close_timeout=5,
            open_timeout=15,      # fail fast if connection hangs at handshake
        ) as ws:
            log.info("BinanceFeed connected")

            async for raw in ws:
                if not isinstance(raw, str):
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                self._handle_message(msg)

                # Proactive reconnect before 24 h session limit
                if time.time() - session_start > BINANCE_SESSION_HOURS * 3600:
                    log.info("BinanceFeed: session limit approaching, reconnecting")
                    return

    def _handle_message(self, msg: dict) -> None:
        """Parse aggTrade message and update price history."""
        data = msg.get("data", msg)  # combined stream wraps in {"data": {...}}
        if data.get("e") != "aggTrade":
            return

        symbol: str = data.get("s", "")
        price_str: str = data.get("p", "")
        trade_time_ms: int = data.get("T", 0)

        if not symbol or not price_str:
            return

        try:
            price = float(price_str)
        except ValueError:
            return

        ts = trade_time_ms / 1000.0 if trade_time_ms else time.time()
        asset = "BTC" if symbol.upper() == "BTCUSDT" else "ETH"

        if asset == "BTC":
            self.btc.add(price, ts)
        else:
            self.eth.add(price, ts)

        for cb in self._callbacks:
            try:
                cb(asset, price, ts)
            except Exception as exc:
                log.debug("Price callback error: %s", exc)
