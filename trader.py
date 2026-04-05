"""
Order execution — paper trading and live trading engines.

Paper mode:  simulates fills immediately at the best available ask.
Live mode:   uses py-clob-client to submit FOK market orders on Polygon.

Three CLI flags are required to enable live mode:
  --live  --confirm-live  --accept-risk

The live trader also syncs USDC balance from the CLOB before each trade
so that balance checks reflect the actual on-chain state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from config import CLOB_HOST, CLOB_API_RPS, MAX_RETRIES, RETRY_BACKOFF_BASE
from market_finder import MarketFinder, RateLimiter
from models import (
    MarketInfo,
    OpportunitySignal,
    OrderBookSnapshot,
    Position,
    Side,
)
from portfolio import Portfolio

log = logging.getLogger(__name__)


# ── Retry helper (sync, for py-clob-client calls in thread executor) ─────────────

def _retry_sync(fn, max_retries=MAX_RETRIES):
    import random
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            wait = min(RETRY_BACKOFF_BASE * (2 ** attempt), 60) + random.uniform(0, 1)
            log.warning("Attempt %d/%d failed: %s — retrying in %.1fs", attempt + 1, max_retries, exc, wait)
            time.sleep(wait)
    raise last_exc


# ── Order book fetcher (via CLOB REST, no auth needed) ──────────────────────────

async def fetch_order_book(token_id: str, rl: RateLimiter) -> Optional[OrderBookSnapshot]:
    """
    Fetch the best bid/ask for a token_id from the CLOB.
    Uses the /book endpoint which returns full order-book depth.
    """
    import httpx

    url = f"{CLOB_HOST}/book?token_id={token_id}"
    await rl.acquire()

    import random

    async def _do():
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    try:
        from market_finder import _retry
        data = await _retry(_do, max_retries=3)
    except Exception as exc:
        log.debug("Order book fetch failed for %s: %s", token_id, exc)
        return None

    bids = data.get("bids", [])   # [{"price": "0.55", "size": "100"}, ...]
    asks = data.get("asks", [])

    # CLOB sort order is non-standard (bids ascending, asks descending),
    # so we use max/min to safely find the best prices regardless of sort.
    best_bid = max(float(b["price"]) for b in bids) if bids else 0.01
    best_ask = min(float(a["price"]) for a in asks) if asks else 0.99
    mid = (best_bid + best_ask) / 2.0

    return OrderBookSnapshot(
        token_id=token_id,
        best_ask=best_ask,
        best_bid=best_bid,
        mid=mid,
    )


# ── Paper Trader ─────────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates order execution without touching the blockchain.
    Fills are assumed to happen at the signal's polymarket_ask price.
    """

    def __init__(self, portfolio: Portfolio, finder: MarketFinder) -> None:
        self._portfolio = portfolio
        self._finder = finder
        self._clob_rl = RateLimiter(CLOB_API_RPS)

    async def execute_signal(self, signal: OpportunitySignal) -> Optional[Position]:
        """
        Simulate placing an order.
        Returns the Position if successful, None otherwise.
        """
        ok, reason = self._portfolio.can_trade(signal.suggested_size_usd)
        if not ok:
            log.info("Paper trade skipped: %s", reason)
            return None

        if self._portfolio.already_has_open_position_on(signal.market):
            log.debug("Already have open position on %s, skipping", signal.market.slug)
            return None

        # Fetch live fee rate for accurate bookkeeping
        fee_rate_bps = await self._finder.get_fee_rate_bps(signal.token_id)
        fee_rate = fee_rate_bps / 10000.0

        pos = self._portfolio.open_position(
            signal=signal,
            actual_fill_price=signal.polymarket_ask,
            actual_size_usd=signal.suggested_size_usd,
            fee_rate=fee_rate,
            order_id=f"paper-{int(time.time() * 1000)}",
        )
        return pos

    async def sync_balance(self) -> None:
        """Paper mode: no-op (balance is tracked locally)."""
        pass


# ── Live Trader ───────────────────────────────────────────────────────────────────

class LiveTrader(PaperTrader):
    """
    Places actual orders on Polymarket via py-clob-client.

    Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        finder: MarketFinder,
        private_key: str,
        funder_address: str,
    ) -> None:
        super().__init__(portfolio, finder)
        self._private_key = private_key
        self._funder = funder_address
        self._clob_client = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self):
        """Lazy-initialise py-clob-client (Level 2 auth)."""
        async with self._client_lock:
            if self._clob_client is not None:
                return self._clob_client

            from py_clob_client.client import ClobClient

            def _init():
                c = ClobClient(
                    host=CLOB_HOST,
                    key=self._private_key,
                    chain_id=137,
                    signature_type=0,   # standard EOA
                    funder=self._funder,
                )
                c.set_api_creds(c.create_or_derive_api_creds())
                return c

            self._clob_client = await asyncio.to_thread(_retry_sync, _init)
            log.info("CLOB client initialised (Level 2)")
            return self._clob_client

    async def execute_signal(self, signal: OpportunitySignal) -> Optional[Position]:
        """
        Place a live FOK market order on Polymarket.

        We use MarketOrderArgs with OrderType.FOK so the order either fills
        completely or is cancelled — no partial fills, no stale orders.
        """
        ok, reason = self._portfolio.can_trade(signal.suggested_size_usd)
        if not ok:
            log.info("Live trade skipped: %s", reason)
            return None

        if self._portfolio.already_has_open_position_on(signal.market):
            log.debug("Already have open position on %s, skipping", signal.market.slug)
            return None

        client = await self._get_client()

        fee_rate_bps = await self._finder.get_fee_rate_bps(signal.token_id)
        fee_rate = fee_rate_bps / 10000.0

        # Round price to nearest tick (0.01)
        rounded_ask = round(round(signal.polymarket_ask / 0.01) * 0.01, 4)

        log.info(
            "LIVE ORDER: %s %s @ %.4f | $%.2f",
            signal.market.slug,
            signal.side.value,
            rounded_ask,
            signal.suggested_size_usd,
        )

        def _place():
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            mo = MarketOrderArgs(
                token_id=signal.token_id,
                amount=signal.suggested_size_usd,   # USD amount to spend
                side=BUY,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            return client.post_order(signed, OrderType.FOK)

        try:
            resp = await asyncio.to_thread(_retry_sync, _place, 3)
        except Exception as exc:
            log.error("Order placement failed: %s", exc)
            return None

        # Check if the order filled
        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        status = resp.get("status", "") if isinstance(resp, dict) else ""

        if status not in ("matched", "live", "") and order_id is None:
            log.warning("Order not filled: %s", resp)
            return None

        # Derive actual fill price from the response if available
        fill_price = float(resp.get("price", rounded_ask)) if isinstance(resp, dict) else rounded_ask

        pos = self._portfolio.open_position(
            signal=signal,
            actual_fill_price=fill_price,
            actual_size_usd=signal.suggested_size_usd,
            fee_rate=fee_rate,
            order_id=order_id,
        )
        log.info("Live order placed: order_id=%s", order_id)
        return pos

    async def sync_balance(self) -> None:
        """
        Fetch actual USDC balance from the CLOB and sync Portfolio.
        """
        client = await self._get_client()

        def _get_balance():
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=0,
            )
            return client.get_balance_allowance(params)

        try:
            data = await asyncio.to_thread(_retry_sync, _get_balance, 3)
            if isinstance(data, dict):
                # Response: {"balance": "1000.50", ...}
                raw = data.get("balance", data.get("USDC", "0"))
                # USDC has 6 decimals on Polygon
                balance_usdc = float(raw) / 1e6 if float(raw) > 1000 else float(raw)
                self._portfolio.sync_balance(balance_usdc)
                log.debug("Balance synced: $%.2f USDC", balance_usdc)
        except Exception as exc:
            log.warning("Balance sync failed: %s", exc)
