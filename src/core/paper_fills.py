"""Paper trading fill simulator.

Paper orders are born PENDING and nothing ever filled them, so P&L, positions
and risk limits were fiction. This module gives paper mode an honest lifecycle:

Fill model (book-snapshot proxies, both price levels matter):
  buy  @ P  fills at P when best ask <= P      (marketable: we cross -> taker)
             or when best bid <  P              (market traded down through
             our resting bid -> maker, a seller consumed our level)
  sell @ P  fills at P when best bid >= P      (marketable: we cross -> taker)
             or when best ask >  P              (market traded up through our
             resting ask -> maker)

The traded-through proxy cannot distinguish "our level was consumed" from
"quotes were pulled"; it is the standard snapshot-based approximation.

Fees (Polymarket: taker-only, fee = shares x rate x p x (1-p), rounded to 5
decimals): taker fills pay the market's feeSchedule rate; maker fills pay
nothing. Fee config comes from an optional fee_lookup (Gamma metadata); when
unknown a conservative default rate is applied. Fees are deducted from the
P&L recorded on close (entry fee accrued at fill time).

On fill the simulator:
  1. marks the order FILLED in the OrderCoordinator,
  2. opens a Position in the RiskManager on buys (or closes the matching open
     position on sells) so exposure/drawdown/stop-loss see real state,
  3. records closed trades in the ProfitabilityTracker.
"""

from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .order_coordinator import Order, OrderCoordinator, OrderStatus
from ..risk.risk_manager import RiskManager
from ..risk.position_tracker import Position
from ..utils.logger import setup_logger, get_trade_logger
from ..utils.profitability_tracker import ProfitabilityTracker, TradeRecord


logger = setup_logger(__name__)
trade_logger = get_trade_logger()

# Used when fee_lookup has no config for the market (most categories are
# 0.04-0.07; 0.05 overcharges politics slightly rather than understating)
DEFAULT_TAKER_FEE_RATE = 0.05
DEFAULT_FEE_EXPONENT = 1.0
MIN_FEE_USDC = 0.00001


def taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket taker fee: shares x rate x (p x (1 - p)) ** exponent.

    Rounded to 5 decimals per the fee schedule; anything smaller is zero.
    Makers always pay 0 (callers decide taker vs maker).
    """
    if shares <= 0 or rate <= 0:
        return 0.0
    p = min(max(float(price), 0.0), 1.0)
    fee = shares * rate * (p * (1.0 - p)) ** exponent
    return round(fee, 5)


class PaperFillSimulator:
    """Fills paper orders against real market prices."""

    def __init__(
        self,
        order_coordinator: OrderCoordinator,
        risk_manager: RiskManager,
        profitability_tracker: Optional[ProfitabilityTracker] = None,
        fee_lookup: Optional[Callable[[str], Optional[Dict]]] = None
    ):
        self.order_coordinator = order_coordinator
        self.risk_manager = risk_manager
        self.profitability_tracker = profitability_tracker
        self.fee_lookup = fee_lookup

    def _fee_config(self, market_id: str) -> Tuple[float, float]:
        """(taker_rate, exponent) for a market; conservative default if unknown."""
        if self.fee_lookup:
            try:
                cfg = self.fee_lookup(market_id)
            except Exception:
                cfg = None
            if cfg:
                rate = float(cfg.get('fee_rate') or 0.0)
                exponent = float(cfg.get('fee_exponent') or DEFAULT_FEE_EXPONENT)
                return rate, exponent
        return DEFAULT_TAKER_FEE_RATE, DEFAULT_FEE_EXPONENT

    def process_fills(self, price_lookup: Callable[[str, str], Optional[Dict]]) -> List[Order]:
        """Try to fill every pending order against current market prices.

        Args:
            price_lookup: callable(market_id, outcome) -> {'bid', 'ask', ...}

        Returns:
            Orders filled in this pass.
        """
        fills: List[Order] = []
        for order in self.order_coordinator.get_pending_orders():
            try:
                prices = price_lookup(order.market_id, order.outcome) or {}
            except Exception as e:
                logger.debug(f"Fill check failed for {order.order_id}: {e}")
                continue

            bid = prices.get('bid')
            ask = prices.get('ask')

            # (fill_price, is_taker): see module docstring for the fill model
            fill_price: Optional[float] = None
            is_taker = False
            if order.side == 'buy':
                if ask is not None and ask <= order.price:
                    fill_price, is_taker = order.price, True
                elif bid is not None and bid < order.price:
                    fill_price, is_taker = order.price, False
            elif order.side == 'sell':
                if bid is not None and bid >= order.price:
                    fill_price, is_taker = order.price, True
                elif ask is not None and ask > order.price:
                    fill_price, is_taker = order.price, False

            if fill_price is None:
                continue

            self._fill(order, fill_price, is_taker)
            fills.append(order)

        return fills

    def _fee_for(self, market_id: str, shares: float, price: float, is_taker: bool) -> float:
        """Taker fee for a fill; makers pay nothing."""
        if not is_taker:
            return 0.0
        rate, exponent = self._fee_config(market_id)
        return taker_fee(shares, price, rate, exponent)

    def _apply_fees(self, closed: Position, total_fee: float) -> None:
        """Deduct total (entry+exit) fees from a closed position's P&L."""
        if total_fee <= 0:
            return
        closed.pnl -= total_fee
        closed.pnl_pct = (closed.pnl / closed.size) * 100 if closed.size else 0.0
        closed.metadata['fees'] = total_fee

    def _close_with_fees(self, position_id: str, exit_price: float, is_taker_exit: bool) -> Optional[Position]:
        """Close a position and net out entry (accrued) + exit fees from P&L."""
        position = self.risk_manager.position_tracker.positions.get(position_id)
        exit_fee = 0.0
        if position is not None:
            shares = position.size / position.entry_price if position.entry_price > 0 else 0.0
            exit_fee = self._fee_for(position.market_id, shares, exit_price, is_taker_exit)

        closed = self.risk_manager.close_position(position_id, exit_price)
        if closed:
            total_fee = float(closed.metadata.get('entry_fee') or 0.0) + exit_fee
            self._apply_fees(closed, total_fee)
        return closed

    def _fill(self, order: Order, fill_price: float, is_taker: bool) -> None:
        """Apply a fill: order status, position bookkeeping, trade record."""
        self.order_coordinator.update_order_status(
            order.order_id, OrderStatus.FILLED, filled_size=order.size
        )
        order.metadata['fill_price'] = fill_price

        entry_shares = order.size / fill_price if fill_price > 0 else 0.0
        entry_fee = self._fee_for(order.market_id, entry_shares, fill_price, is_taker)
        order.metadata['fee'] = entry_fee

        if order.side == 'buy':
            position = Position(
                position_id=f"pos_{order.order_id}",
                market_id=order.market_id,
                strategy=order.strategy,
                side='buy',
                size=order.size,
                entry_price=fill_price,
                outcome=order.outcome,
                metadata={'order_id': order.order_id, 'entry_fee': entry_fee}
            )
            self.risk_manager.add_position(position)
            trade_logger.info(
                f"📝 [PAPER FILL] BUY {order.size:.2f} @ {fill_price:.4f} "
                f"({order.outcome}) {order.market_id[:24]}... [{order.strategy}]"
                f"{' taker fee $%.4f' % entry_fee if entry_fee else ' (maker, no fee)'}"
            )
        else:
            matched = self._find_matching_position(order)
            if matched:
                closed = self._close_with_fees(matched.position_id, fill_price, is_taker)
                if closed:
                    self._record_trade(closed, fill_price)
                trade_logger.info(
                    f"📝 [PAPER FILL] SELL {order.size:.2f} @ {fill_price:.4f} "
                    f"({order.outcome}) closed {matched.position_id} "
                    f"P&L ${closed.pnl:.2f}" if closed else
                    f"📝 [PAPER FILL] SELL {order.size:.2f} @ {fill_price:.4f}"
                )
            else:
                # No inventory: track as a short/flip position
                position = Position(
                    position_id=f"pos_{order.order_id}",
                    market_id=order.market_id,
                    strategy=order.strategy,
                    side='sell',
                    size=order.size,
                    entry_price=fill_price,
                    outcome=order.outcome,
                    metadata={'order_id': order.order_id, 'entry_fee': entry_fee}
                )
                self.risk_manager.add_position(position)
                trade_logger.info(
                    f"📝 [PAPER FILL] SELL {order.size:.2f} @ {fill_price:.4f} "
                    f"({order.outcome}) {order.market_id[:24]}... [{order.strategy}] (sin inventario)"
                )

    def force_close(self, position_id: str, exit_price: float, reason: str = "",
                    charge_exit_fee: bool = True) -> Optional[Position]:
        """Close a position at the given price (stop-loss / manual exit).

        In paper mode this IS the closing market order: no real order is sent
        but the bookkeeping (order fill, P&L, trade record) is identical to a
        filled close. A market close is a taker (charge_exit_fee=True); market
        settlement/redemption is not a trade and charges no exit fee.
        """
        closed = self._close_with_fees(position_id, exit_price, is_taker_exit=charge_exit_fee)
        if closed:
            self._record_trade(closed, exit_price)
            fees = float(closed.metadata.get('fees') or 0.0)
            trade_logger.info(
                f"📝 [PAPER CLOSE] {position_id} @ {exit_price:.4f} "
                f"P&L ${closed.pnl:.2f} ({closed.pnl_pct:.2f}%) "
                f"fees ${fees:.4f} {reason}"
            )
        return closed

    def _find_matching_position(self, order: Order) -> Optional[Position]:
        """FIFO match of an open long position for this market/outcome/strategy."""
        candidates = [
            p for p in self.risk_manager.position_tracker.positions.values()
            if p.market_id == order.market_id
            and p.strategy == order.strategy
            and getattr(p, 'outcome', 'YES') == order.outcome
            and p.side == 'buy'
            and p.status == 'open'
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.timestamp)

    def _record_trade(self, closed: Position, exit_price: float) -> None:
        """Record a closed position in the profitability tracker (net of fees)."""
        if not self.profitability_tracker:
            return
        try:
            trade = TradeRecord(
                trade_id=closed.position_id,
                strategy=closed.strategy,
                market_id=closed.market_id,
                entry_time=closed.timestamp,
                exit_time=datetime.now(),
                entry_price=closed.entry_price,
                exit_price=exit_price,
                size=closed.size,
                side=closed.side,
                pnl=closed.pnl,
                pnl_pct=closed.pnl_pct,
                fees=float(closed.metadata.get('fees') or 0.0)
            )
            self.profitability_tracker.record_trade(trade)
        except Exception as e:
            logger.debug(f"Failed to record trade {closed.position_id}: {e}")
