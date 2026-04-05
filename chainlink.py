"""
Chainlink oracle integration for Polygon mainnet.

Two modes of operation:

  ChainlinkFeed  — background task that polls latestRoundData every ~25 s and
                   caches the result.  When a market starts its measurement
                   window, get_price_at() returns the correct price instantly
                   with zero additional RPC calls.

  fetch_chainlink_price_at — one-shot fallback using binary search (≤40 RPC
                   calls).  Used only if the cache is empty at startup.

Chainlink updates the BTC/USD feed on Polygon whenever the price moves ≥0.5 %
OR 27 seconds pass, so polling every 25 s captures every update reliably.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# Free Polygon RPC endpoints (no API key needed)
_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
]

# Chainlink aggregator contract addresses on Polygon mainnet
_AGGREGATOR_ADDRESSES: dict[str, str] = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6F",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}

_DECIMALS = 8          # all Chainlink USD feeds on Polygon use 8 decimals
_POLL_INTERVAL = 25    # seconds — safely below Chainlink's 27 s heartbeat
_MAX_CACHE = 500       # rounds to keep; 500 × 25 s ≈ 3.5 hours

_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",        "type": "uint80"},
            {"name": "answer",         "type": "int256"},
            {"name": "startedAt",      "type": "uint256"},
            {"name": "updatedAt",      "type": "uint256"},
            {"name": "answeredInRound","type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"name": "roundId",        "type": "uint80"},
            {"name": "answer",         "type": "int256"},
            {"name": "startedAt",      "type": "uint256"},
            {"name": "updatedAt",      "type": "uint256"},
            {"name": "answeredInRound","type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(slots=True)
class _Round:
    round_id: int
    price: float
    updated_at: int   # Unix seconds


# ── ChainlinkFeed ────────────────────────────────────────────────────────────────

class ChainlinkFeed:
    """
    Background polling task for Chainlink BTC/USD and ETH/USD on Polygon.

    Usage in main:
        chainlink = ChainlinkFeed()
        asyncio.create_task(chainlink.start())
        ...
        price = chainlink.get_price_at("BTC", slug_ts)
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[_Round]] = {"BTC": [], "ETH": []}
        self._w3: Optional[Any] = None
        self._contracts: dict[str, Any] = {}
        self._ready = False

    # ── Public API ──────────────────────────────────────────────────────────────

    def get_price_at(self, asset: str, target_ts: int) -> Optional[float]:
        """
        Return the Chainlink price that was active at target_ts:
        the latest round whose updatedAt ≤ target_ts.
        Returns None if the cache has no data before target_ts.
        """
        rounds = self._cache.get(asset, [])
        best: Optional[float] = None
        for r in rounds:
            if r.updated_at <= target_ts:
                best = r.price
        return best

    def latest_price(self, asset: str) -> Optional[float]:
        """Most recently polled Chainlink price."""
        rounds = self._cache.get(asset, [])
        return rounds[-1].price if rounds else None

    async def start(self) -> None:
        """Run forever, polling both feeds every _POLL_INTERVAL seconds."""
        while True:
            try:
                await asyncio.gather(
                    asyncio.to_thread(self._sync_poll, "BTC"),
                    asyncio.to_thread(self._sync_poll, "ETH"),
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("ChainlinkFeed poll error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    # ── Internal ────────────────────────────────────────────────────────────────

    def _ensure_web3(self) -> bool:
        if self._w3 is not None:
            return True
        try:
            from web3 import Web3
            for rpc_url in _POLYGON_RPCS:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                try:
                    w3.eth.block_number   # connectivity check
                except Exception:
                    continue
                self._w3 = w3
                for asset, addr in _AGGREGATOR_ADDRESSES.items():
                    self._contracts[asset] = w3.eth.contract(
                        address=Web3.to_checksum_address(addr),
                        abi=_ABI,
                    )
                self._ready = True
                log.debug("ChainlinkFeed connected via %s", rpc_url)
                return True
        except Exception as exc:
            log.debug("ChainlinkFeed: web3 init failed: %s", exc)
        return False

    def _sync_poll(self, asset: str) -> None:
        if not self._ensure_web3():
            return
        try:
            contract = self._contracts[asset]
            latest = contract.functions.latestRoundData().call()
            round_id: int = latest[0]
            price = latest[1] / (10 ** _DECIMALS)
            updated_at: int = latest[3]

            rounds = self._cache[asset]
            if not rounds or rounds[-1].round_id != round_id:
                rounds.append(_Round(round_id, price, updated_at))
                if len(rounds) > _MAX_CACHE:
                    rounds.pop(0)
                log.debug(
                    "ChainlinkFeed %s round %d updatedAt=%d price=%.2f",
                    asset, round_id, updated_at, price,
                )
        except Exception as exc:
            log.debug("ChainlinkFeed._sync_poll(%s): %s", asset, exc)


# ── One-shot fallback (binary search) ────────────────────────────────────────────

def _make_round_id(phase_id: int, agg_round: int) -> int:
    return (phase_id << 64) | agg_round


def _sync_fetch_price_at(asset: str, target_ts: int) -> Optional[float]:
    """
    Synchronous binary-search Chainlink lookup.
    Needs at most ~40 RPC calls; run via asyncio.to_thread.
    """
    try:
        from web3 import Web3
        w3 = None
        for rpc_url in _POLYGON_RPCS:
            candidate = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
            try:
                candidate.eth.block_number
                w3 = candidate
                break
            except Exception:
                continue
        if w3 is None:
            log.warning("Chainlink binary search: no reachable Polygon RPC")
            return None

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(_AGGREGATOR_ADDRESSES[asset]),
            abi=_ABI,
        )

        latest = contract.functions.latestRoundData().call()
        latest_round_id: int = latest[0]
        latest_updated_at: int = latest[3]
        phase_id = latest_round_id >> 64
        agg_latest = latest_round_id & 0xFFFFFFFFFFFFFFFF

        if latest_updated_at <= target_ts:
            price = latest[1] / (10 ** _DECIMALS)
            log.debug("Chainlink %s @ %d: latest round price=%.2f", asset, target_ts, price)
            return price

        lo, hi = 1, agg_latest
        best_data = None

        for _ in range(64):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            round_id = _make_round_id(phase_id, mid)
            try:
                data = contract.functions.getRoundData(round_id).call()
                updated_at: int = data[3]
            except Exception:
                hi = mid - 1
                continue

            if updated_at == 0:
                hi = mid - 1
                continue

            if updated_at <= target_ts:
                best_data = data
                lo = mid + 1
            else:
                hi = mid - 1

        if best_data is None:
            return None

        price = best_data[1] / (10 ** _DECIMALS)
        log.debug(
            "Chainlink %s @ %d: binary search round %d price=%.2f",
            asset, target_ts, best_data[0], price,
        )
        return price

    except Exception as exc:
        log.warning("Chainlink binary search failed for %s @ %d: %s", asset, target_ts, exc)
        return None


async def fetch_chainlink_price_at(asset: str, target_ts: int) -> Optional[float]:
    """Async wrapper for the binary-search fallback."""
    return await asyncio.to_thread(_sync_fetch_price_at, asset, target_ts)
