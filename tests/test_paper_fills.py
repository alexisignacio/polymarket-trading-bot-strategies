"""Tests for the paper fill simulator and order lifecycle"""

import time

import pytest
from src.core.order_coordinator import OrderCoordinator, OrderStatus
from src.core.paper_fills import PaperFillSimulator
from src.risk.risk_manager import RiskManager
from src.utils.profitability_tracker import ProfitabilityTracker


def flow_confirming_all():
    """Trades lookup whose prints confirm maker fills for both sides."""
    ts = time.time()

    def lookup(market_id):
        return [
            {'price': 0.0, 'timestamp': ts, 'outcome': 'YES'},
            {'price': 1.0, 'timestamp': ts, 'outcome': 'YES'},
            {'price': 0.0, 'timestamp': ts, 'outcome': 'NO'},
            {'price': 1.0, 'timestamp': ts, 'outcome': 'NO'},
        ]
    return lookup


def make_system(fee_lookup=None, trades_lookup='default'):
    rm = RiskManager({
        'max_position_size': 1000.0,
        'max_total_exposure': 10000.0,
        'initial_capital': 1000.0,
        'stop_loss_pct': 10.0
    })
    oc = OrderCoordinator()
    pt = ProfitabilityTracker()
    pt.set_initial_capital(1000.0)
    # Fee-free markets unless a test says otherwise, so P&L assertions stay exact
    sim = PaperFillSimulator(
        oc, rm, pt,
        fee_lookup=fee_lookup or (lambda mid: {'fee_rate': 0.0}),
        trades_lookup=flow_confirming_all() if trades_lookup == 'default' else trades_lookup,
    )
    return oc, rm, pt, sim


def test_buy_fill_opens_position():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')

    # ask below limit -> marketable -> fills at limit price
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})
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

    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.50, 'ask': 0.55, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert len(fills) == 0
    assert oc.get_order('o1').status == OrderStatus.PENDING
    assert len(rm.position_tracker.positions) == 0


def test_sell_fill_closes_matching_position_and_records_trade():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})

    oc.create_order('o2', 'm1', 'YES', 'sell', size=100.0, price=0.60, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.61, 'ask': 0.62, 'bid_size': 1000.0, 'ask_size': 1000.0})
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

    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.85, 'ask': 0.86, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert len(fills) == 1
    pos = next(iter(rm.position_tracker.positions.values()))
    assert pos.side == 'sell'


def test_force_close_records_trade():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})
    pos_id = next(iter(rm.position_tracker.positions))

    closed = sim.force_close(pos_id, 0.40, reason='stop-loss')
    assert closed is not None
    assert closed.pnl == pytest.approx(-20.0)
    assert pt.get_overall_stats()['total_trades'] == 1


def test_traded_through_buy_fills_at_limit():
    """Resting bid fills when the market trades down through it (maker)."""
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')

    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.47, 'ask': 0.55, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert len(fills) == 1
    pos = next(iter(rm.position_tracker.positions.values()))
    assert pos.entry_price == 0.50


def test_resting_order_does_not_fill_without_price_move():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.50, 'ask': 0.55, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert len(fills) == 0


def test_traded_through_sell_fills_at_limit():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'sell', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.40, 'ask': 0.53, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert len(fills) == 1


def test_marketable_fill_needs_level_depth_to_cover_the_order():
    """A crossed level thinner than the order must not fill (no partials)."""
    oc, rm, pt, sim = make_system()
    # 100.0 USD notional @ 0.50 = 200 shares; ask level only offers 50
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 50.0})
    assert len(fills) == 0

    # Same order fills once the level covers it
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 300.0})
    assert len(fills) == 1


def test_marketable_sell_needs_bid_depth():
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'sell', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.55, 'ask': 0.60, 'bid_size': 100.0, 'ask_size': 1000.0})
    assert len(fills) == 0  # 200 shares needed, 100 offered at the bid


def test_traded_through_fill_ignores_depth_but_needs_flow():
    """Maker fills skip the depth check (the size that traded through a level
    is not observable) but require real print flow at the order price."""
    oc, rm, pt, sim = make_system()
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.47, 'ask': 0.55, 'bid_size': 1.0, 'ask_size': 1.0})
    assert len(fills) == 1


def test_maker_fill_blocked_without_trades_lookup():
    """Sin fuente de prints reales el fill maker no existe (fail-closed)."""
    oc, rm, pt, sim = make_system(trades_lookup=None)
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.47, 'ask': 0.55, 'bid_size': 1.0, 'ask_size': 1.0})
    assert len(fills) == 0


def test_maker_fill_blocked_without_confirming_print():
    """El movimiento de cotización solo no basta: hace falta un print real."""
    ts = time.time()
    oc, rm, pt, sim = make_system(trades_lookup=lambda mid: [
        # price por encima del límite de compra y de otra opción: no confirma
        {'price': 0.60, 'timestamp': ts, 'outcome': 'YES'},
        {'price': 0.10, 'timestamp': ts, 'outcome': 'NO'},
    ])
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.47, 'ask': 0.55, 'bid_size': 1.0, 'ask_size': 1.0})
    assert len(fills) == 0


def test_maker_fill_blocked_when_prints_are_stale():
    old_ts = time.time() - 3600
    oc, rm, pt, sim = make_system(trades_lookup=lambda mid: [
        {'price': 0.10, 'timestamp': old_ts, 'outcome': 'YES'},
    ])
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    fills = sim.process_fills(lambda mid, oc_: {'bid': 0.47, 'ask': 0.55, 'bid_size': 1.0, 'ask_size': 1.0})
    assert len(fills) == 0


def test_taker_fee_charged_and_maker_fill_free():
    rates = lambda mid: {'fee_rate': 0.04, 'fee_exponent': 1.0}
    oc, rm, pt, sim = make_system(fee_lookup=rates)

    # marketable buy (ask <= limit): taker -> fee
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})
    # shares = 100/0.50 = 200; fee = 200 * 0.04 * 0.5*0.5 = 2.0
    assert rm.position_tracker.positions['pos_o1'].metadata['entry_fee'] == pytest.approx(2.0)

    # traded-through buy (maker): no fee
    oc.create_order('o2', 'm2', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.45, 'ask': 0.55, 'bid_size': 1000.0, 'ask_size': 1000.0})
    assert rm.position_tracker.positions['pos_o2'].metadata['entry_fee'] == 0.0


def test_fees_reduce_recorded_pnl():
    rates = lambda mid: {'fee_rate': 0.04, 'fee_exponent': 1.0}
    oc, rm, pt, sim = make_system(fee_lookup=rates)
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})   # taker entry
    oc.create_order('o2', 'm1', 'YES', 'sell', size=100.0, price=0.60, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.61, 'ask': 0.62, 'bid_size': 1000.0, 'ask_size': 1000.0})   # taker exit

    # gross 20.0; entry fee 2.0; exit fee = 200 * 0.04 * 0.6*0.4 = 1.92
    expected = 20.0 - 2.0 - 1.92
    closed = rm.position_tracker.closed_positions[0]
    assert closed.pnl == pytest.approx(expected)
    assert closed.metadata['fees'] == pytest.approx(3.92)
    assert pt.get_overall_stats()['total_pnl'] == pytest.approx(expected)


def test_settlement_close_charges_no_exit_fee():
    rates = lambda mid: {'fee_rate': 0.04, 'fee_exponent': 1.0}
    oc, rm, pt, sim = make_system(fee_lookup=rates)
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})  # entry fee 2.0
    pos_id = next(iter(rm.position_tracker.positions))

    closed = sim.force_close(pos_id, 1.0, reason='market_resolved', charge_exit_fee=False)
    assert closed.pnl == pytest.approx(100.0 - 2.0)  # gross 100, only entry fee
    assert closed.metadata['fees'] == pytest.approx(2.0)


def test_unknown_fee_config_uses_conservative_default():
    oc, rm, pt, sim = make_system(fee_lookup=lambda mid: None)
    oc.create_order('o1', 'm1', 'YES', 'buy', size=100.0, price=0.50, strategy='test')
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})
    pos = next(iter(rm.position_tracker.positions.values()))
    # default rate 0.05: 200 * 0.05 * 0.25 = 2.5
    assert pos.metadata['entry_fee'] == pytest.approx(2.5)


def test_taker_fee_formula_matches_polymarket_tables():
    from src.core.paper_fills import taker_fee
    assert taker_fee(100, 0.50, 0.07) == pytest.approx(1.75)  # crypto @ 50c
    assert taker_fee(100, 0.30, 0.05) == pytest.approx(1.05)  # sports @ 30c
    assert taker_fee(100, 0.99, 0.04) == pytest.approx(0.04, abs=0.001)  # politics @ 99c
    assert taker_fee(100, 0.50, 0.0) == 0.0
    assert taker_fee(0, 0.50, 0.05) == 0.0


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
    sim.process_fills(lambda mid, oc_: {'bid': 0.48, 'ask': 0.49, 'bid_size': 1000.0, 'ask_size': 1000.0})
    pos_id = next(iter(rm.position_tracker.positions))
    sim.force_close(pos_id, 0.55)

    sm = StateManager(state_file=str(tmp_path / 'state.json'))
    sm.save_state(rm.position_tracker, oc, pt)

    rm2 = RiskManager({'initial_capital': 1000.0})
    pt2 = ProfitabilityTracker()
    pt2.set_initial_capital(1000.0)
    state = sm.load_state()
    sm.restore_profitability(state, rm2.position_tracker, pt2)

    assert rm2.position_tracker.get_total_pnl() == pytest.approx(rm.position_tracker.get_total_pnl())
