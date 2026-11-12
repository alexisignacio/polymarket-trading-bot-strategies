"""Polymarket Data API v2 read-only client (real trade prints).

Used by the paper fill simulator as flow evidence: a resting limit order only
fills when the market actually printed at its price (quote moves alone do not
count). TTL cache keeps the check cheap; failures return no prints, which
blocks maker fills for the cache window (conservative).
"""

import time
from typing import Dict, List

import requests

from ..utils.logger import setup_logger


logger = setup_logger(__name__)


class DataAPIClient:
    """Fetches real trade prints for a market (condition id)."""

    BASE_URL = "https://data-api.polymarket.com/v2"

    def __init__(self, cache_ttl: float = 60.0):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}

    def recent_trades(self, condition_id: str, limit: int = 200) -> List[Dict]:
        """Most recent prints for a market: [{'price','timestamp','outcome','side'}].

        Empty list when the API fails or the market has no prints; the result
        is cached for cache_ttl seconds.
        """
        cached = self._cache.get(condition_id)
        if cached is not None and time.time() - cached[0] < self.cache_ttl:
            return cached[1]

        prints: List[Dict] = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/trades",
                params={"condition": condition_id, "limit": limit},
                headers={"User-Agent": "polyhft/1.0"},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("data") if isinstance(payload, dict) else payload
            for t in raw or []:
                prints.append({
                    "price": float(t.get("price") or 0),
                    "timestamp": float(t.get("timestamp") or 0),
                    "outcome": str(t.get("outcome") or ""),
                    "side": str(t.get("side") or ""),
                })
        except Exception as e:
            logger.debug(f"Data API trades fetch failed for {str(condition_id)[:16]}...: {e}")

        self._cache[condition_id] = (time.time(), prints)
        return prints
