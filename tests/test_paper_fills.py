"""Tests for the paper fill simulator and order lifecycle"""

import pytest
from src.core.order_coordinator import OrderCoordinator, OrderStatus
from src.core.paper_fills import PaperFillSimulator
from src.risk.risk_manager import RiskManager
from src.utils.profitability_tracker import ProfitabilityTracker


def make_system():
    rm = RiskManager({
        'max_position_size': 1000.0,
        'max_total_exposure': 10000.0,
        'initial_capital': 1000.0,
        'stop_loss_pct': 10.0
    })
    oc = OrderCoordinator()
    pt = ProfitabilityTracker()
    pt.set_initial_capital(1000.0)
    sim = PaperFillSimulator(oc, rm, pt)
    return oc, rm, pt, sim


def test_buy_fill_opens_position():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')

    # ask below limit -> marketable -> fills at limit price
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49})
    assert len(fills) == 1
    order = oc.get_order('o1')
    assert order.status == OrderStatus.FILLED

    positions = rm.position_tracker.positions
    assert len(positions) == 1
    pos = next(iter(positions.values()))
    assert pos.side == 'buy'
    assert pos.entry_price == 0.50
    assert pos.size == 100.0
    assert pos.outcome == 'YES'


def test_buy_not_filled_when_not_marketable():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')

    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.50, 'ask': 0.55})
    assert len(fills) == 0
    assert oc.get_order('o1').status == OrderStatus.PENDING
    assert len(rm.position_tracker.positions) == 0


def test_sell_fill_closes_matching_position_and_records_trade():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49})

    oc.create_order('o2', 'm1', 'YES', 'sell', size=100.0, price=0.60, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.61, 'ask': 0.62})
    assert len(fills) == 1

    # position closed
    assert len(rm.position_tracker.positions) == 0
    # pnl = (0.60 - 0.50) / 0.50 * 100 = 20.0
    assert rm.position_tracker.get_total_pnl() == pytest.approx(20.0)

    # trade recorded
    stats = pt.get_overall_stats()
    assert stats['total_trades'] == 1
    assert stats['total_pnl'] == pytest.approx(20.0)


def test_sell_without_inventory_opens_short_position():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'NO', 'sell', size=50.0, price=0.80, strategy='test')

    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.85, 'ask': 0.86})
    assert len(fills) == 1
    pos = next(iter(rm.position_tracker.positions.values()))
    assert pos.side == 'sell'


def test_force_close_records_trade():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49})
    pos_id = next(iter(rm.position_tracker.positions))

    closed = sim.force_close(pos_id, 0.40, reason='stop-loss')
    assert closed is not None
    assert closed.pnl == pytest.approx(-20.0)
    assert pt.get_overall_stats()['total_trades'] == 1


def test_duplicate_order_rejected_only_within_window():
    oc = OrderCoordinator()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    with pytest.raises(ValueError):
        oc.create_order('o2', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')

    # Backdate the first order past the dedup window: re-quote must be allowed
    order = oc.get_order('o1')
    from datetime import datetime, timedelta
    order.created_at = datetime.now() - timedelta(seconds=OrderCoordinator.DEDUP_WINDOW_SEC + 5)
    o2 = oc.create_order('o2', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    assert o2.order_id == 'o2'


def test_state_roundtrip_restores_historical_pnl(tmp_path):
    from src.core.state_manager import StateManager
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49})
    pos_id = next(iter(rm.position_tracker.positions))
    sim.force_close(pos_id, 0.55)

    sm = StateManager(state_file=str(tmp_path / 'state.json'))
    sm.save_state(rm.position_tracker, oc, pt)

    rm2 = RiskManager({'initial_capital': 1000.0})
    oc2 = OrderCoordinator()
    pt2 = ProfitabilityTracker()
    pt2.set_initial_capital(1000.0)
    state = sm.load_state()
    sm.restore_profitability(state, rm2.position_tracker, pt2)

    assert rm2.position_tracker.get_total_pnl() == pytest.approx(rm.position_tracker.get_total_pnl())
