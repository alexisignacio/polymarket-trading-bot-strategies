"""State persistence and recovery"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
from ..utils.logger import setup_logger
from ..risk.position_tracker import PositionTracker, Position
from ..core.order_coordinator import OrderCoordinator, Order, OrderStatus


logger = setup_logger(__name__)


def _trade_to_dict(trade: Any) -> Dict:
    """Serialize a TradeRecord to a JSON-safe dict."""
    return {
        'trade_id': trade.trade_id,
        'strategy': trade.strategy,
        'market_id': trade.market_id,
        'entry_time': trade.entry_time.isoformat() if trade.entry_time else None,
        'exit_time': trade.exit_time.isoformat() if trade.exit_time else None,
        'entry_price': trade.entry_price,
        'exit_price': trade.exit_price,
        'size': trade.size,
        'side': trade.side,
        'pnl': trade.pnl,
        'pnl_pct': trade.pnl_pct,
        'fees': trade.fees,
    }


def _trade_from_dict(data: Dict) -> Optional[Any]:
    """Parse a serialized trade back into a TradeRecord (None on garbage)."""
    try:
        from ..utils.profitability_tracker import TradeRecord
        return TradeRecord(
            trade_id=data['trade_id'],
            strategy=data['strategy'],
            market_id=data['market_id'],
            entry_time=datetime.fromisoformat(data['entry_time']),
            exit_time=datetime.fromisoformat(data['exit_time']),
            entry_price=float(data['entry_price']),
            exit_price=float(data['exit_price']),
            size=float(data['size']),
            side=data['side'],
            pnl=float(data['pnl']),
            pnl_pct=float(data.get('pnl_pct') or 0.0),
            fees=float(data.get('fees') or 0.0),
        )
    except Exception:
        return None


class StateManager:
    """Manages bot state persistence and recovery"""
    
    def __init__(self, state_file: str = "bot_state.json"):
        """
        Initialize state manager.
        
        Args:
            state_file: Path to state file
        """
        self.state_file = Path(state_file)
        self.state_dir = self.state_file.parent
        self.state_dir.mkdir(parents=True, exist_ok=True)
    
    def save_state(
        self,
        position_tracker: PositionTracker,
        order_coordinator: OrderCoordinator,
        profitability_tracker: Any,
        additional_state: Optional[Dict] = None
    ) -> bool:
        """
        Save bot state to file.
        
        Args:
            position_tracker: Position tracker instance
            order_coordinator: Order coordinator instance
            profitability_tracker: Profitability tracker instance
            additional_state: Additional state to save
            
        Returns:
            True if saved successfully
        """
        try:
            state = {
                'timestamp': datetime.now().isoformat(),
                'positions': self._serialize_positions(position_tracker),
                'orders': self._serialize_orders(order_coordinator),
                'profitability': self._serialize_profitability(profitability_tracker),
                'additional': additional_state or {}
            }
            
            # Write to temporary file first, then rename (atomic operation)
            temp_file = self.state_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            
            # Atomic rename
            temp_file.replace(self.state_file)
            
            logger.info(f"State saved to {self.state_file}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False
    
    def _serialize_positions(self, tracker: PositionTracker) -> Dict:
        """Serialize positions to dict"""
        positions = []
        for position in tracker.positions.values():
            positions.append({
                'position_id': position.position_id,
                'market_id': position.market_id,
                'strategy': position.strategy,
                'side': position.side,
                'size': position.size,
                'entry_price': position.entry_price,
                'outcome': getattr(position, 'outcome', 'YES'),
                'current_price': position.current_price,
                'timestamp': position.timestamp.isoformat(),
                'status': position.status,
                'pnl': position.pnl,
                'pnl_pct': position.pnl_pct,
                'metadata': position.metadata
            })
        
        return {
            'open_positions': positions,
            'closed_positions_count': len(tracker.closed_positions),
            'closed_pnl': tracker.historical_pnl + sum(p.pnl for p in tracker.closed_positions)
        }
    
    def _serialize_orders(self, coordinator: OrderCoordinator) -> Dict:
        """Serialize orders to dict.

        Cancelled/failed orders are dead and not persisted; filled ones are
        capped so the state file cannot grow without bound under high re-quote
        rates. Only pending orders are restored on startup.
        """
        MAX_FILLED_SERIALIZED = 200
        active: List[Dict] = []
        filled: List[Dict] = []
        for order in coordinator.orders.values():
            record = {
                'order_id': order.order_id,
                'market_id': order.market_id,
                'outcome': order.outcome,
                'side': order.side,
                'size': order.size,
                'price': order.price,
                'filled_size': order.filled_size,
                'status': order.status.value,
                'created_at': order.created_at.isoformat(),
                'updated_at': order.updated_at.isoformat(),
                'strategy': order.strategy,
                'metadata': order.metadata
            }
            if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
                continue
            if order.status == OrderStatus.FILLED:
                filled.append(record)
            else:
                active.append(record)
        
        filled.sort(key=lambda r: r['updated_at'])
        active.extend(filled[-MAX_FILLED_SERIALIZED:])
        
        return {
            'all_orders': active,
            'pending_order_ids': list(coordinator.pending_orders)
        }
    
    def _serialize_profitability(self, tracker: Any) -> Dict:
        """Serialize profitability tracker to dict (cumulative across sessions)"""
        try:
            stats = tracker.get_overall_stats()
            records = getattr(tracker, 'get_trade_records', lambda: [])()
            return {
                'initial_capital': tracker.initial_capital,
                'current_capital': tracker.current_capital,
                'total_trades': stats.get('total_trades', 0),
                'winning_trades': stats.get('winning_trades', 0),
                'losing_trades': stats.get('losing_trades', 0),
                'total_pnl': stats.get('total_pnl', 0.0),
                'win_rate': stats.get('win_rate', 0.0),
                'roi': stats.get('roi', 0.0),
                'strategy_stats': {
                    name: dict(values)
                    for name, values in getattr(tracker, 'strategy_stats', {}).items()
                },
                'trade_history': [_trade_to_dict(t) for t in records[-500:]]
            }
        except Exception as e:
            logger.debug(f"Error serializing profitability: {e}")
            return {}
    
    def load_state(self) -> Optional[Dict]:
        """
        Load bot state from file.
        
        Returns:
            State dictionary or None if not found/invalid
        """
        if not self.state_file.exists():
            logger.debug(f"State file not found: {self.state_file}")
            return None
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            
            logger.info(f"State loaded from {self.state_file}")
            return state
        
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in state file: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return None
    
    def restore_positions(
        self,
        state: Dict,
        position_tracker: PositionTracker
    ) -> int:
        """
        Restore positions from state.
        
        Args:
            state: State dictionary
            position_tracker: Position tracker instance
            
        Returns:
            Number of positions restored
        """
        try:
            positions_data = state.get('positions', {}).get('open_positions', [])
            restored = 0
            
            for pos_data in positions_data:
                try:
                    position = Position(
                        position_id=pos_data['position_id'],
                        market_id=pos_data['market_id'],
                        strategy=pos_data['strategy'],
                        side=pos_data['side'],
                        size=pos_data['size'],
                        entry_price=pos_data['entry_price'],
                        outcome=pos_data.get('outcome', 'YES'),
                        current_price=pos_data.get('current_price'),
                        timestamp=datetime.fromisoformat(pos_data['timestamp']),
                        status=pos_data['status'],
                        pnl=pos_data.get('pnl', 0.0),
                        pnl_pct=pos_data.get('pnl_pct', 0.0),
                        metadata=pos_data.get('metadata', {})
                    )
                    
                    position_tracker.add_position(position)
                    restored += 1
                
                except Exception as e:
                    logger.warning(f"Failed to restore position {pos_data.get('position_id')}: {e}")
            
            if restored > 0:
                logger.info(f"Restored {restored} positions from state")
            
            return restored
        
        except Exception as e:
            logger.error(f"Error restoring positions: {e}")
            return 0
    
    def restore_profitability(
        self,
        state: Dict,
        position_tracker: PositionTracker,
        profitability_tracker: Any = None
    ) -> None:
        """Restore realized P&L so drawdown/capital survive restarts."""
        try:
            closed_pnl = float(state.get('positions', {}).get('closed_pnl', 0.0))
            position_tracker.historical_pnl = closed_pnl
            
            prof = state.get('profitability', {}) or {}
            if profitability_tracker is not None and prof:
                profitability_tracker.initial_capital = float(
                    prof.get('initial_capital', profitability_tracker.initial_capital)
                )
                profitability_tracker.current_capital = float(
                    prof.get('current_capital', profitability_tracker.current_capital)
                )
                # Cumulative stats so restarts don't reset dashboard counters
                profitability_tracker.historical_trades = int(prof.get('total_trades', 0))
                profitability_tracker.historical_wins = int(prof.get('winning_trades', 0))
                profitability_tracker.historical_losses = int(prof.get('losing_trades', 0))
                profitability_tracker.historical_pnl = float(prof.get('total_pnl', 0.0))

                # Per-strategy stats and trade records (reporting only; the
                # cumulative counters above already include them)
                for name, values in (prof.get('strategy_stats') or {}).items():
                    if isinstance(values, dict):
                        profitability_tracker.strategy_stats[name].update(values)
                profitability_tracker.trade_history = [
                    t for t in (_trade_from_dict(h) for h in (prof.get('trade_history') or []))
                    if t is not None
                ][-500:]
            
            if closed_pnl:
                logger.info(f"Restored historical P&L: ${closed_pnl:.2f}")
        except Exception as e:
            logger.warning(f"Failed to restore profitability: {e}")
    
    def restore_orders(
        self,
        state: Dict,
        order_coordinator: OrderCoordinator
    ) -> int:
        """
        Restore orders from state.
        
        Args:
            state: State dictionary
            order_coordinator: Order coordinator instance
            
        Returns:
            Number of orders restored
        """
        try:
            orders_data = state.get('orders', {}).get('all_orders', [])
            restored = 0
            
            for order_data in orders_data:
                try:
                    # Only restore pending orders (filled/cancelled are historical)
                    status = OrderStatus(order_data['status'])
                    if status not in [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED]:
                        continue
                    
                    order = Order(
                        order_id=order_data['order_id'],
                        market_id=order_data['market_id'],
                        outcome=order_data['outcome'],
                        side=order_data['side'],
                        size=order_data['size'],
                        price=order_data['price'],
                        filled_size=order_data.get('filled_size', 0.0),
                        status=status,
                        created_at=datetime.fromisoformat(order_data['created_at']),
                        updated_at=datetime.fromisoformat(order_data['updated_at']),
                        strategy=order_data.get('strategy', ''),
                        metadata=order_data.get('metadata', {})
                    )
                    
                    order_coordinator.orders[order.order_id] = order
                    if status == OrderStatus.PENDING:
                        order_coordinator.pending_orders.add(order.order_id)
                    
                    restored += 1
                
                except Exception as e:
                    logger.warning(f"Failed to restore order {order_data.get('order_id')}: {e}")
            
            if restored > 0:
                logger.info(f"Restored {restored} orders from state")
            
            return restored
        
        except Exception as e:
            logger.error(f"Error restoring orders: {e}")
            return 0
    
    def clear_state(self) -> bool:
        """
        Clear saved state.
        
        Returns:
            True if cleared successfully
        """
        try:
            if self.state_file.exists():
                self.state_file.unlink()
                logger.info("State file cleared")
            return True
        except Exception as e:
            logger.error(f"Failed to clear state: {e}")
            return False

