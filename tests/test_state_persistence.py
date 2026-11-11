"""Tests for cumulative profitability persistence across restarts"""

from datetime import datetime

from src.core.state_manager import StateManager
from src.core.order_coordinator import OrderCoordinator
from src.risk.position_tracker import PositionTracker
from src.utils.profitability_tracker import ProfitabilityTracker, TradeRecord


def make_trade(pnl: float) -> TradeRecord:
    now = datetime.now()
    return TradeRecord(
        trade_id=f"t_{pnl}", strategy="test", market_id="m",
        entry_time=now, exit_time=now,
        entry_price=0.9, exit_price=1.0, size=5.0, side="buy",
        pnl=pnl, pnl_pct=pnl * 10,
    )


def test_overall_stats_include_historical_counters():
    pt = ProfitabilityTracker()
    pt.set_initial_capital(1000.0)
    pt.historical_trades = 7
    pt.historical_wins = 5
    pt.historical_losses = 2
    pt.historical_pnl = 3.24

    pt.record_trade(make_trade(1.0))
    pt.record_trade(make_trade(-0.5))

    stats = pt.get_overall_stats()
    assert stats['total_trades'] == 9
    assert stats['winning_trades'] == 6
    assert stats['losing_trades'] == 3
    assert stats['total_pnl'] == 3.24 + 1.0 - 0.5
    assert stats['win_rate'] == 6 / 9 * 100


def test_state_round_trip_preserves_cumulative_stats(tmp_path):
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file=str(state_file))

    pt1 = ProfitabilityTracker()
    pt1.set_initial_capital(1000.0)
    pt1.record_trade(make_trade(2.0))
    pt1.record_trade(make_trade(-1.0))

    tracker1 = PositionTracker()
    ok = sm.save_state(tracker1, OrderCoordinator(), pt1)
    assert ok

    # "Restart": fresh trackers, restore from disk
    pt2 = ProfitabilityTracker()
    pt2.set_initial_capital(1000.0)
    tracker2 = PositionTracker()
    state = sm.load_state()
    sm.restore_profitability(state, tracker2, pt2)

    stats = pt2.get_overall_stats()
    assert stats['total_trades'] == 2
    assert stats['winning_trades'] == 1
    assert stats['losing_trades'] == 1
    assert stats['total_pnl'] == 1.0

    # New session trades add on top of the restored history
    pt2.record_trade(make_trade(0.5))
    stats = pt2.get_overall_stats()
    assert stats['total_trades'] == 3
    assert stats['total_pnl'] == 1.5


def test_cancelled_orders_are_pruned_from_state(tmp_path):
    from src.core.order_coordinator import OrderStatus

    state_file = tmp_path / "state.json"
    sm = StateManager(state_file=str(state_file))
    oc = OrderCoordinator()
    oc.create_order('o_cancel', 'm1', 'YES', 'buy', 5.0, 0.9)
    oc.cancel_order('o_cancel')
    oc.create_order('o_pending', 'm2', 'YES', 'buy', 5.0, 0.8)

    pt = ProfitabilityTracker()
    pt.set_initial_capital(1000.0)
    assert sm.save_state(PositionTracker(), oc, pt)

    state = sm.load_state()
    saved = {o['order_id']: o['status'] for o in state['orders']['all_orders']}
    assert saved == {'o_pending': OrderStatus.PENDING.value}
    assert state['orders']['pending_order_ids'] == ['o_pending']
