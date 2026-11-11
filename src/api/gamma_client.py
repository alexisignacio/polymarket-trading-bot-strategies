"""Gamma API client for market metadata (volume, liquidity, creation date).

The CLOB API does not expose volume/liquidity/created_at on market objects.
Gamma (https://gamma-api.polymarket.com) does. Strategies that filter on
liquidity or market age get that metadata from here.

Everything is best-effort: if Gamma is unreachable the client returns an empty
map and callers treat metadata as unknown instead of crashing.
"""

import json
import threading
import time
from typing import Dict

import requests

from ..utils.logger import setup_logger


logger = setup_logger(__name__)


def _fee_schedule_fields(fee_schedule) -> Dict[str, float]:
    """Normalize Gamma's feeSchedule (dict or JSON string) into flat fields."""
    if isinstance(fee_schedule, str):
        try:
            fee_schedule = json.loads(fee_schedule)
        except (ValueError, TypeError):
            fee_schedule = None
    if not isinstance(fee_schedule, dict):
        return {"fee_rate": 0.0, "fee_exponent": 1.0, "taker_only": True}
    return {
        "fee_rate": float(fee_schedule.get("rate") or 0.0),
        "fee_exponent": float(fee_schedule.get("exponent") or 1.0),
        "taker_only": bool(fee_schedule.get("takerOnly", True)),
    }


class GammaClient:
    """Read-only Gamma metadata client with a TTL cache."""

    BASE_URL = "https://gamma-api.polymarket.com"
    PAGE_SIZE = 500

    def __init__(self, cache_ttl: float = 300.0):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Dict] = {}
        self._cache_timestamp: float = 0.0
        self._lock = threading.Lock()

    def get_metadata(self, condition_ids=None) -> Dict[str, Dict]:
        """Return {condition_id: {'volume', 'liquidity', 'created_at',
        'fees_enabled', 'fee_rate', 'fee_exponent', 'taker_only'}}.

        The cache holds *all* active markets; `condition_ids` only narrows the
        returned map. Unknown ids are simply absent from the result.
        """
        with self._lock:
            if time.time() - self._cache_timestamp >= self.cache_ttl:
                self._refresh()

            if condition_ids is None:
                return dict(self._cache)
            return {cid: self._cache[cid] for cid in condition_ids if cid in self._cache}

    def _refresh(self) -> None:
        """Fetch all active markets from Gamma (paginated)."""
        refreshed: Dict[str, Dict] = {}
        offset = 0
        try:
            while True:
                response = requests.get(
                    f"{self.BASE_URL}/markets",
                    params={
                        "closed": "false",
                        "limit": self.PAGE_SIZE,
                        "offset": offset,
                        "order": "volumeNum",
                        "ascending": "false",
                    },
                    timeout=15,
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list) or not page:
                    break

                for raw in page:
                    condition_id = raw.get("conditionId")
                    if not condition_id:
                        continue
                    refreshed[condition_id] = {
                        "volume": float(raw.get("volumeNum") or 0.0),
                        "liquidity": float(raw.get("liquidityNum") or 0.0),
                        "created_at": raw.get("createdAt"),
                        "fees_enabled": bool(raw.get("feesEnabled")),
                        **_fee_schedule_fields(raw.get("feeSchedule")),
                    }

                if len(page) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                if offset >= 10000:  # hard safety cap
                    break

            self._cache = refreshed
            self._cache_timestamp = time.time()
            logger.debug(f"Gamma metadata refreshed: {len(refreshed)} markets")
        except Exception as e:
            # Keep stale cache if we have one; otherwise stay empty
            logger.warning(f"Gamma metadata refresh failed: {e}")
            if not self._cache:
                self._cache_timestamp = time.time()  # back off until next TTL
