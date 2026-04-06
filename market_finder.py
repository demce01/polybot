"""
Discovers and tracks active BTC/ETH 5-minute and 15-minute Polymarket markets.

Market slug formula (confirmed from Polymarket/py-clob-client issue #244):
  5-min:  {asset}-updown-5m-{int(time.time() // 300) * 300}
  15-min: {asset}-updown-15m-{int(time.time() // 900) * 900}

The slug timestamp equals the measurement window START.
endDate = slug_ts + interval_secs (the window END / resolution time).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from binance_feed import BinanceFeed
from chainlink import ChainlinkFeed
from config import (
    ASSETS,
    CLOB_API_RPS,
    CLOB_HOST,
    GAMMA_API_HOST,
    GAMMA_API_RPS,
    INTERVALS,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
)
from models import MarketInfo, Side

log = logging.getLogger(__name__)

BINANCE_REST = "https://api.binance.com/api/v3"


# ── Binance REST price helper ────────────────────────────────────────────────────

async def fetch_binance_open_at(symbol: str, timestamp_sec: int) -> Optional[float]:
    """
    Return the Binance 1-minute kline OPEN price for the minute that starts
    at timestamp_sec (UTC).  This is the closest public approximation to the
    Chainlink oracle "price to beat" used by Polymarket for resolution.

    slug_ts is always minute-aligned (divisible by 60), so the 1m open is exact.
    """
    url = f"{BINANCE_REST}/klines"
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": timestamp_sec * 1000,         # ms
        "endTime": (timestamp_sec + 60) * 1000,    # ms
        "limit": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        if data:
            return float(data[0][1])  # [open_time, open, high, low, close, ...]
    except Exception as exc:
        log.debug("Binance kline fetch failed (%s @ %d): %s", symbol, timestamp_sec, exc)
    return None


# ── Rate limiter ────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, calls_per_second: float) -> None:
        self._min_interval = 1.0 / calls_per_second
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.time()


# ── Retry helper ────────────────────────────────────────────────────────────────

async def _retry(coro_fn, max_retries: int = MAX_RETRIES):
    import random
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = min(RETRY_BACKOFF_BASE * (2 ** attempt), 60.0) + random.uniform(0, 1)
            log.warning("Attempt %d/%d failed: %s: %s — retrying in %.1fs", attempt + 1, max_retries, type(exc).__name__, exc, wait)
            await asyncio.sleep(wait)


# ── MarketFinder ────────────────────────────────────────────────────────────────

class MarketFinder:
    """
    Periodically generates the expected market slugs for all asset × interval
    combinations, fetches their data from the Gamma API, and keeps an
    up-to-date dict of known active markets.

    Also resolves market outcomes by re-fetching closed markets.
    """

    def __init__(self, feed: BinanceFeed, chainlink_feed: Optional[ChainlinkFeed] = None) -> None:
        self._feed = feed
        self._chainlink_feed = chainlink_feed
        self._gamma_rl = RateLimiter(GAMMA_API_RPS)
        self._clob_rl = RateLimiter(CLOB_API_RPS)
        # slug → MarketInfo
        self._markets: dict[str, MarketInfo] = {}
        self._lock = asyncio.Lock()

    # ── Public API ──────────────────────────────────────────────────────────────

    async def get_active_markets(self) -> list[MarketInfo]:
        """Return all currently tracked, un-closed markets."""
        async with self._lock:
            return [m for m in self._markets.values() if not m.is_closed]

    async def get_market(self, slug: str) -> Optional[MarketInfo]:
        async with self._lock:
            return self._markets.get(slug)

    async def scan_once(self) -> None:
        """Fetch current + next-window markets for all asset × interval pairs."""
        now = time.time()
        slugs_to_fetch: list[str] = []

        for asset in ASSETS:
            for interval, secs in INTERVALS.items():
                # Current measurement window
                current_ts = int(now // secs) * secs
                slugs_to_fetch.append(f"{asset}-updown-{interval}-{current_ts}")
                # Next window (pre-fetch so we're ready)
                next_ts = current_ts + secs
                slugs_to_fetch.append(f"{asset}-updown-{interval}-{next_ts}")

        for slug in slugs_to_fetch:
            await self._fetch_and_store(slug)

    async def resolve_closed_markets(self) -> list[MarketInfo]:
        """
        Check all tracked markets that should have resolved by now.
        Returns list of newly resolved markets.
        """
        resolved: list[MarketInfo] = []
        async with self._lock:
            candidates = [
                m for m in self._markets.values()
                if not m.is_closed and time.time() > m.measurement_end + 5
            ]

        for market in candidates:
            updated = await self._fetch_resolution(market)
            if updated and updated.is_closed:
                resolved.append(updated)

        return resolved

    async def ensure_price_to_beat(self, market: MarketInfo) -> None:
        """
        Fetch market.price_to_beat from Polymarket's Gamma API (eventMetadata.priceToBeat).

        This is the exact Chainlink Data Streams price Polymarket uses for resolution.
        No Binance or Chainlink RPC fallback — if Gamma hasn't set it yet (first ~60s
        of a new window) we leave price_to_beat as None and retry next cycle.
        The MIN_WINDOW_ELAPSED=60s gate ensures we never trade before it's available.
        """
        if market.price_to_beat is not None:
            return  # already set

        try:
            url = f"{GAMMA_API_HOST}/events?slug={market.slug}"
            await self._gamma_rl.acquire()
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            if data:
                meta = data[0].get("eventMetadata") or {}
                raw_ptb = meta.get("priceToBeat")
                if raw_ptb is not None:
                    market.price_to_beat = float(raw_ptb)
                    log.info("%s price_to_beat=%.2f (Gamma eventMetadata)", market.slug, market.price_to_beat)
                    return
        except Exception as exc:
            log.debug("%s Gamma fetch failed: %s", market.slug, exc)

        log.debug("%s price_to_beat not yet available from Gamma — will retry next cycle", market.slug)

    # ── Gamma API fetchers ──────────────────────────────────────────────────────

    async def _fetch_and_store(self, slug: str) -> Optional[MarketInfo]:
        """Fetch a market by slug and cache it."""
        existing = self._markets.get(slug)
        # Don't re-fetch markets we already know about unless they might have resolved
        if existing and not existing.is_closed:
            if time.time() < existing.measurement_end:
                return existing  # still open, cached data is fine
        try:
            market = await self._fetch_by_slug(slug)
        except Exception as exc:
            log.debug("Could not fetch %s: %s", slug, exc)
            return None

        if market:
            async with self._lock:
                self._markets[slug] = market
        return market

    async def _fetch_by_slug(self, slug: str) -> Optional[MarketInfo]:
        url = f"{GAMMA_API_HOST}/events?slug={slug}"
        await self._gamma_rl.acquire()

        async def _do():
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.json()

        data = await _retry(_do)

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        event = data[0]
        markets = event.get("markets", [])
        if not markets:
            return None

        m = markets[0]
        raw_ids = m.get("clobTokenIds", [])
        # Gamma API returns clobTokenIds as a JSON-encoded string, not a list
        if isinstance(raw_ids, str):
            try:
                import json as _json
                raw_ids = _json.loads(raw_ids)
            except Exception:
                raw_ids = []
        clob_ids = raw_ids
        if len(clob_ids) < 2:
            log.debug("Skipping %s: missing clobTokenIds", slug)
            return None

        # Parse slug to get asset, interval, ts
        parts = slug.split("-")
        # e.g. ['btc', 'updown', '5m', '1766161800']
        if len(parts) < 4:
            return None
        asset = parts[0].upper()
        interval = parts[2]          # "5m" or "15m"
        slug_ts = int(parts[3])
        interval_secs = INTERVALS.get(interval, 300)

        end_date = _parse_iso(m.get("endDate", event.get("endDate", "")))
        if end_date is None:
            # Derive from slug
            end_date = datetime.fromtimestamp(slug_ts + interval_secs, tz=timezone.utc)

        # outcomes may also be a JSON-encoded string in the Gamma API
        outcomes = m.get("outcomes", ["Up", "Down"])
        if isinstance(outcomes, str):
            try:
                import json as _json
                outcomes = _json.loads(outcomes)
            except Exception:
                outcomes = ["Up", "Down"]
        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        down_idx = outcomes.index("Down") if "Down" in outcomes else 1

        question_text = m.get("question", "") or ""

        # Primary source: eventMetadata.priceToBeat — the exact Chainlink Data Streams
        # price that Polymarket uses for resolution.  Available on both open and
        # closed markets as soon as the window starts.
        price_to_beat: Optional[float] = None
        event_meta = event.get("eventMetadata") or {}
        if isinstance(event_meta, dict):
            raw_ptb = event_meta.get("priceToBeat")
            if raw_ptb is not None:
                try:
                    price_to_beat = float(raw_ptb)
                    log.debug("%s price_to_beat from eventMetadata: %.2f", slug, price_to_beat)
                except (TypeError, ValueError):
                    pass

        # Fallback: parse from question/description text (older market format)
        if price_to_beat is None:
            desc_text = m.get("description", "") or ""
            price_to_beat = _parse_reference_price(question_text + " " + desc_text, asset)
            if price_to_beat is not None:
                log.debug("%s price_to_beat from question text: %.2f", slug, price_to_beat)

        return MarketInfo(
            event_id=str(event.get("id", "")),
            market_id=str(m.get("id", "")),
            slug=slug,
            question=question_text,
            asset=asset,
            interval=interval,
            interval_secs=interval_secs,
            condition_id=m.get("conditionId", ""),
            up_token_id=clob_ids[up_idx],
            down_token_id=clob_ids[down_idx],
            end_date=end_date,
            slug_ts=slug_ts,
            resolution_source=m.get("resolutionSource", ""),
            is_closed=bool(m.get("closed", False)),
            price_to_beat=price_to_beat,
        )

    async def _fetch_resolution(self, market: MarketInfo) -> Optional[MarketInfo]:
        """Re-fetch a market to check if it has resolved and which side won."""
        url = f"{GAMMA_API_HOST}/events?slug={market.slug}"
        await self._gamma_rl.acquire()

        try:
            async def _do():
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    return r.json()

            data = await _retry(_do)
        except Exception as exc:
            log.debug("Resolution fetch failed for %s: %s", market.slug, exc)
            return None

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        event = data[0]
        markets = event.get("markets", [])
        if not markets:
            return None

        m = markets[0]
        if not m.get("closed", False):
            return None  # not yet resolved

        import json as _json2

        outcomes = m.get("outcomes", ["Up", "Down"])
        if isinstance(outcomes, str):
            try:
                outcomes = _json2.loads(outcomes)
            except Exception:
                outcomes = ["Up", "Down"]

        outcome_prices = m.get("outcomePrices", ["0.5", "0.5"])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = _json2.loads(outcome_prices)
            except Exception:
                outcome_prices = ["0.5", "0.5"]

        resolved_side = None
        for i, (outcome, op) in enumerate(zip(outcomes, outcome_prices)):
            try:
                if abs(float(op) - 1.0) < 0.01:
                    resolved_side = Side.UP if outcome == "Up" else Side.DOWN
                    break
            except ValueError:
                pass

        market.is_closed = True
        market.resolved_side = resolved_side
        async with self._lock:
            self._markets[market.slug] = market

        log.info(
            "Market resolved: %s → %s",
            market.slug,
            resolved_side.value if resolved_side else "unknown",
        )
        return market

    # ── CLOB fee rate ───────────────────────────────────────────────────────────

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """
        Fetch the current fee rate in basis points for a token from the CLOB.
        Falls back to 720 (7.2%) on error.
        """
        url = f"{CLOB_HOST}/fee-rate?token_id={token_id}"
        await self._clob_rl.acquire()

        try:
            async def _do():
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    return r.json()

            data = await _retry(_do, max_retries=2)
            # Response: {"base_fee": "1000"} — base_fee is the feeRate coefficient in bps
            fee_rate = data.get("base_fee", data.get("fee_rate", data.get("feeRate", "720")))
            return int(fee_rate)
        except Exception as exc:
            log.debug("fee-rate fetch failed for %s: %s — using 720 bps", token_id, exc)
            return 720


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python 3.11+ handles Z suffix natively; earlier versions need manual fix
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def current_market_slug(asset: str, interval: str) -> str:
    """Generate the slug for the currently-active measurement window."""
    secs = INTERVALS[interval]
    ts = int(time.time() // secs) * secs
    return f"{asset.lower()}-updown-{interval}-{ts}"


def _parse_reference_price(text: str, asset: str) -> Optional[float]:
    """
    Extract the Chainlink reference price embedded in a Polymarket market
    question or description, e.g.:
      "Will BTC be above $83,525.00 at 9:05 PM?"
      "Reference price: $2,415.32"
      "above or below 0.5234"   (XRP/SOL — small numbers, no $ sign)

    Returns None if no plausible price is found.
    """
    import re

    # Expected price ranges per asset (sanity filter)
    _ranges = {
        "BTC": (1_000,   500_000),
        "ETH": (100,     30_000),
        "XRP": (0.01,    50),
        "SOL": (1,       2_000),
    }
    lo, hi = _ranges.get(asset, (0.001, 1_000_000))

    # Pattern 1: dollar sign followed by number (BTC / ETH)
    for raw in re.findall(r'\$\s*([\d,]+(?:\.\d+)?)', text):
        try:
            v = float(raw.replace(',', ''))
            if lo <= v <= hi:
                return v
        except ValueError:
            continue

    # Pattern 2: plain number (XRP / SOL — no $ sign, small values)
    for raw in re.findall(r'\b([\d,]+\.\d{2,6})\b', text):
        try:
            v = float(raw.replace(',', ''))
            if lo <= v <= hi:
                return v
        except ValueError:
            continue

    return None
