"""Paper trading fill simulator.

Paper orders are born PENDING and nothing ever filled them, so P&L, positions
and risk limits were fiction. This module gives paper mode an honest lifecycle:
a limit order fills when the real market becomes marketable against it

    buy  @ P  fills when best ask <= P
    sell @ P  fills when best bid >= P

On fill the simulator:
  1. marks the order FILLED in the OrderCoordinator,
  2. opens a Position in the RiskManager on buys (or closes the matching open
     position on sells) so exposure/drawdown/stop-loss see real state,
  3. records closed trades in the ProfitabilityTracker.
"""

from datetime import datetime
from typing import Callable, Dict, List, Optional

from .order_coordinator import Order, OrderCoordinator, OrderStatus
from ..risk.risk_manager import RiskManager
from ..risk.position_tracker import Position
from ..utils.logger import setup_logger, get_trade_logger
from ..utils.profitability_tracker import ProfitabilityTracker, TradeRecord


logger = setup_logger(__name__)
trade_logger = get_trade_logger()


class PaperFillSimulator:
    """Fills paper orders against real market prices."""

    def __init__(
        self,
        order_coordinator: OrderCoordinator,
        risk_manager: RiskManager,
        profitability_tracker: Optional[ProfitabilityTracker] = None
    ):
        self.order_coordinator = order_coordinator
        self.risk_manager = risk_manager
        self.profitability_tracker = profitability_tracker

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

            fill_price: Optional[float] = None
            if order.side == 'buy' and ask is not None and ask <= order.price:
                fill_price = order.price
            elif order.side == 'sell' and bid is not None and bid >= order.price:
                fill_price = order.price

            if fill_price is None:
                continue

            self._fill(order, fill_price)
            fills.append(order)

        return fills

    def _fill(self, order: Order, fill_price: float) -> None:
        """Apply a fill: order status, position bookkeeping, trade record."""
        self.order_coordinator.update_order_status(
            order.order_id, OrderStatus.FILLED, filled_size=order.size
        )
        order.metadata['fill_price'] = fill_price

        if order.side == 'buy':
            position = Position(
                position_id=f"pos_{order.order_id}",
                market_id=order.market_id,
                strategy=order.strategy,
                side='buy',
                size=order.size,
                entry_price=fill_price,
                outcome=order.outcome,
                metadata={'order_id': order.order_id}
            )
            self.risk_manager.add_position(position)
            trade_logger.info(
                f"📝 [PAPER FILL] BUY {order.size:.2f} @ {fill_price:.4f} "
                f"({order.outcome}) {order.market_id[:24]}... [{order.strategy}]"
            )
        else:
            matched = self._find_matching_position(order)
            if matched:
                closed = self.risk_manager.close_position(matched.position_id, fill_price)
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
                    metadata={'order_id': order.order_id}
                )
                self.risk_manager.add_position(position)
                trade_logger.info(
                    f"📝 [PAPER FILL] SELL {order.size:.2f} @ {fill_price:.4f} "
                    f"({order.outcome}) {order.market_id[:24]}... [{order.strategy}] (sin inventario)"
                )

    def force_close(self, position_id: str, exit_price: float, reason: str = "") -> Optional[Position]:
        """Close a position at the given price (stop-loss / manual exit).

        In paper mode this IS the closing market order: no real order is sent
        but the bookkeeping (order fill, P&L, trade record) is identical to a
        filled close.
        """
        closed = self.risk_manager.close_position(position_id, exit_price)
        if closed:
            self._record_trade(closed, exit_price)
            trade_logger.info(
                f"📝 [PAPER CLOSE] {position_id} @ {exit_price:.4f} "
                f"P&L ${closed.pnl:.2f} ({closed.pnl_pct:.2f}%) {reason}"
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
        """Record a closed position in the profitability tracker."""
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
                pnl_pct=closed.pnl_pct
            )
            self.profitability_tracker.record_trade(trade)
        except Exception as e:
            logger.debug(f"Failed to record trade {closed.position_id}: {e}")
