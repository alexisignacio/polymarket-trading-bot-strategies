"""Reconcile restored state against live market reality.

A restart can resurrect positions and orders whose markets resolved, closed or
vanished while the bot was down. Left alone they (a) block risk limits
("open positions 10 exceeds limit 10"), (b) trigger endless 404 orderbook
fetches, and (c) fake exposure/P&L. This module settles them honestly:

  - market resolved  -> close at the settlement price (won: 1.0, lost: 0.0)
  - market closed with no winner evidence -> close at 0.0 (pessimistic, logged)
  - market gone (404) -> close at 0.0 (logged)
  - lookup transient failure -> keep the position untouched

Restored pending orders in dead markets are cancelled.

The math lives in PositionTracker.close_position; recording/logging is the
caller's close_fn (PaperFillSimulator.force_close in paper mode).
"""

from typing import Any, Callable, Dict, Optional

from ..utils.logger import setup_logger

logger = setup_logger(__name__)

WON_EXIT_PRICE = 1.0
LOST_EXIT_PRICE = 0.0

REASON_RESOLVED = "market_resolved"
REASON_CLOSED_UNRESOLVED = "market_closed_unresolved"
REASON_UNAVAILABLE = "market_unavailable"


def winning_outcome(raw_market: Dict) -> Optional[str]:
    """Return the uppercase outcome name of the winning token, if any."""
    for token in raw_market.get("tokens") or []:
        if isinstance(token, dict) and token.get("winner"):
            return str(token.get("outcome") or "").upper() or None
    return None


def market_is_dead(raw_market: Dict) -> bool:
    """True when the market can never trade again (resolved/closed)."""
    return bool(raw_market.get("closed"))


def settlement_exit_price(position_outcome: str, raw_market: Dict) -> Optional[float]:
    """Settlement price for a position's outcome token, or None if undetermined.

    A resolved market settles each token at 1.0 (won) or 0.0 (lost). A closed
    market without winner evidence has no honest settlement; callers use the
    pessimistic 0.0 with REASON_CLOSED_UNRESOLVED.
    """
    winner = winning_outcome(raw_market)
    if not winner:
        return None
    return WON_EXIT_PRICE if str(position_outcome).upper() == winner else LOST_EXIT_PRICE


def reconcile_restored_state(
    position_tracker: Any,
    order_coordinator: Any,
    market_lookup: Callable[[str], Optional[Dict]],
    close_fn: Callable[[str, float, str], Any],
    cancel_fn: Callable[[str], Any],
) -> Dict[str, int]:
    """Settle restored positions/orders that no longer have a live market.

    Args:
        position_tracker: PositionTracker with restored open positions
        order_coordinator: OrderCoordinator with restored pending orders
        market_lookup: market_id -> raw market dict, None if confirmed missing.
            Transient failures must RAISE so those positions are kept.
        close_fn: (position_id, exit_price, reason) -> anything
        cancel_fn: (order_id) -> anything

    Returns:
        Summary counts: kept, settled_won, settled_lost, dropped, cancelled.
    """
    summary = {"kept": 0, "settled_won": 0, "settled_lost": 0, "dropped": 0, "cancelled": 0}

    # Cache lookups: many positions/orders can share one market
    cache: Dict[str, Optional[Dict]] = {}

    def lookup(market_id: str) -> Optional[Dict]:
        if market_id not in cache:
            cache[market_id] = market_lookup(market_id)
        return cache[market_id]

    for position in list(position_tracker.positions.values()):
        try:
            raw = lookup(position.market_id)
        except Exception as e:
            logger.warning(
                f"Reconcile: market lookup failed for {position.market_id} "
                f"({e}); keeping position {position.position_id}"
            )
            summary["kept"] += 1
            continue

        if raw is None:
            close_fn(position.position_id, LOST_EXIT_PRICE, REASON_UNAVAILABLE)
            logger.warning(
                f"Reconcile: market {position.market_id} no longer exists; "
                f"closed {position.position_id} at {LOST_EXIT_PRICE} ({REASON_UNAVAILABLE})"
            )
            summary["dropped"] += 1
            continue

        if not market_is_dead(raw):
            summary["kept"] += 1
            continue

        exit_price = settlement_exit_price(position.outcome, raw)
        if exit_price is None:
            exit_price = LOST_EXIT_PRICE
            reason = REASON_CLOSED_UNRESOLVED
        else:
            reason = REASON_RESOLVED

        close_fn(position.position_id, exit_price, reason)
        won = exit_price == WON_EXIT_PRICE
        summary["settled_won" if won else "settled_lost"] += 1
        logger.info(
            f"Reconcile: settled {position.position_id} ({position.outcome}) "
            f"at {exit_price} [{reason}]"
        )

    for order in list(order_coordinator.get_pending_orders()):
        try:
            raw = lookup(order.market_id)
        except Exception as e:
            logger.warning(
                f"Reconcile: market lookup failed for order {order.order_id} ({e}); keeping it"
            )
            continue
        if raw is None or market_is_dead(raw):
            cancel_fn(order.order_id)
            summary["cancelled"] += 1
            logger.info(
                f"Reconcile: cancelled restored order {order.order_id} "
                f"({order.market_id} is dead)"
            )

    return summary
