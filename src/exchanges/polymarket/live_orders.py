"""Live order execution for the Polymarket CLOB.

This is the ONLY module allowed to submit real orders. Two gates must both be
open before anything leaves the process:

  1. The bot runs with paper_trading disabled (rest_client wiring), and
  2. The environment variable POLYMARKET_LIVE_TRADING is exactly
     "I_UNDERSTAND" (explicit opt-in per launch, never persisted in config).

Order signing (EIP-712) and CLOB authentication (L1/L2 headers, API creds)
are delegated to the official polymarket-apis SDK; no hand-rolled crypto.

Fail-closed: any doubt (missing key, missing opt-in, SDK error) raises and
nothing is submitted.
"""

import os
from typing import Any, Dict, List, Optional

from ...utils.logger import setup_logger, get_trade_logger

logger = setup_logger(__name__)
trade_logger = get_trade_logger()

LIVE_OPT_IN_ENV = "POLYMARKET_LIVE_TRADING"
LIVE_OPT_IN_VALUE = "I_UNDERSTAND"


def live_trading_allowed() -> bool:
    """True only with the explicit per-launch opt-in env var."""
    return os.environ.get(LIVE_OPT_IN_ENV) == LIVE_OPT_IN_VALUE


class LiveTradingNotPermitted(RuntimeError):
    """Raised when live order submission is attempted without the opt-in."""


class LiveOrderExecutor:
    """Signs and submits real orders via the official Polymarket SDK."""

    def __init__(
        self,
        private_key: str,
        funder_address: Optional[str] = None,
        api_creds: Optional[Dict[str, str]] = None,
        chain_id: int = 137,
        signature_type: int = 0,
    ):
        """
        Args:
            private_key: Polygon wallet private key used to sign orders
            funder_address: wallet holding the funds (defaults to the key's address)
            api_creds: optional {'key','secret','passphrase'} to skip derivation
            chain_id: 137 (Polygon mainnet) or 80002 (Amoy testnet)
            signature_type: 0 = EOA wallet (default), 1/2 = proxy/relayer wallets
        """
        if not private_key:
            raise ValueError("private_key is required for live trading")
        self._private_key = private_key
        self._funder_address = funder_address
        self._api_creds = api_creds
        self._chain_id = chain_id
        self._signature_type = signature_type
        self._client: Optional[Any] = None

    def _assert_allowed(self) -> None:
        if not live_trading_allowed():
            raise LiveTradingNotPermitted(
                f"Live trading requires {LIVE_OPT_IN_ENV}={LIVE_OPT_IN_VALUE!r} "
                f"in the environment (got {os.environ.get(LIVE_OPT_IN_ENV)!r})"
            )

    def _ensure_client(self):
        self._assert_allowed()
        if self._client is None:
            from polymarket_apis import PolymarketClobClient
            from polymarket_apis.types.clob_types import ApiCreds

            address = self._funder_address
            if not address:
                from eth_account import Account
                address = Account.from_key(self._private_key).address

            creds = ApiCreds(**self._api_creds) if self._api_creds else None
            self._client = PolymarketClobClient(
                private_key=self._private_key,
                address=address,
                creds=creds,
                chain_id=self._chain_id,
                signature_type=self._signature_type,
            )
            logger.info(f"Live CLOB client initialized for {str(address)[:10]}...")
        return self._client

    def place_limit_order(self, token_id: str, side: str, price: float, size_shares: float) -> Dict:
        """Sign and submit a GTC limit order. Returns {'order_id','status',...}."""
        self._assert_allowed()
        from polymarket_apis import OrderArgs

        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            raise ValueError(f"side must be buy/sell, got {side!r}")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price must be in (0, 1), got {price!r}")
        if size_shares <= 0:
            raise ValueError(f"size_shares must be > 0, got {size_shares!r}")

        client = self._ensure_client()
        args = OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(size_shares),
            side=side_l,
        )
        response = client.create_and_post_order(args)

        result = {
            "order_id": getattr(response, "order_id", "") or "",
            "status": getattr(response, "status", "") or "",
            "success": bool(getattr(response, "success", False)),
            "error": getattr(response, "error_msg", "") or "",
            "live_trading": True,
        }
        if not result["success"]:
            logger.error(f"Live order rejected: {result['error']} (token={str(token_id)[:24]}...)")
        else:
            trade_logger.info(
                f"🔴 [LIVE ORDER] {side_l.upper()} {size_shares:.2f} @ {price:.4f} "
                f"token={str(token_id)[:24]}... -> {result['status']} ({result['order_id'][:16]})"
            )
        return result

    def cancel_order(self, order_id: str) -> Dict:
        """Cancel a live order. Returns {'canceled': [...], 'not_canceled': {...}}."""
        self._assert_allowed()
        client = self._ensure_client()
        response = client.cancel_order(order_id)
        return {
            "canceled": list(getattr(response, "canceled", None) or []),
            "not_canceled": dict(getattr(response, "not_canceled", None) or {}),
            "live_trading": True,
        }

    def get_open_orders(self) -> List[Dict]:
        """Open live orders (read-only)."""
        self._assert_allowed()
        client = self._ensure_client()
        response = client.get_orders()
        orders = []
        for raw in response or []:
            if hasattr(raw, "model_dump"):
                orders.append(raw.model_dump())
            elif isinstance(raw, dict):
                orders.append(raw)
        return orders
