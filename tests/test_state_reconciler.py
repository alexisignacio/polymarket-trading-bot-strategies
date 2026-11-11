"""Tests for restored-state reconciliation against live market reality"""

import pytest

from src.core.order_coordinator import OrderCoordinator, OrderStatus
from src.core.state_reconciler import (
    REASON_CLOSED_UNRESOLVED,
    REASON_RESOLVED,
    REASON_UNAVAILABLE,
    reconcile_restored_state,
    settlement_exit_price,
    winning_outcome,
)
from src.risk.position_tracker import Position


class Tracker:
    """Minimal position holder matching the reconciler's contract."""

    def __init__(self):
        self.positions = {}


class Recorder:
    """Records close/cancel calls."""

    def __init__(self):
        self.closed = []
        self.cancelled = []

    def close(self, position_id, exit_price, reason):
        self.closed.append((position_id, exit_price, reason))

    def cancel(self, order_id):
        self.cancelled.append(order_id)


def add_position(tracker, position_id, market_id, outcome='YES', side='buy',
                 size=5.0, entry_price=0.93):
    tracker.positions[position_id] = Position(
        position_id=position_id,
        market_id=market_id,
        strategy='tail_end',
        side=side,
        size=size,
        entry_price=entry_price,
        outcome=outcome,
    )


def resolved_market(winner_outcome):
    return {
        'condition_id': '0xm',
        'closed': True,
        'accepting_orders': False,
        'tokens': [
            {'token_id': '1', 'outcome': 'Yes', 'winner': winner_outcome == 'YES'},
            {'token_id': '2', 'outcome': 'No', 'winner': winner_outcome == 'NO'},
        ],
    }


def closed_no_winner_market():
    return {
        'condition_id': '0xm',
        'closed': True,
        'accepting_orders': False,
        'tokens': [
            {'token_id': '1', 'outcome': 'Yes', 'winner': False},
            {'token_id': '2', 'outcome': 'No', 'winner': False},
        ],
    }


def open_market():
    return {
        'condition_id': '0xm',
        'closed': False,
        'accepting_orders': True,
        'tokens': [
            {'token_id': '1', 'outcome': 'Yes', 'winner': False},
            {'token_id': '2', 'outcome': 'No', 'winner': False},
        ],
    }


def test_winning_outcome_and_settlement_price():
    market = resolved_market('NO')
    assert winning_outcome(market) == 'NO'
    assert settlement_exit_price('NO', market) == 1.0
    assert settlement_exit_price('YES', market) == 0.0
    assert settlement_exit_price('yes', market) == 0.0
    assert settlement_exit_price('YES', closed_no_winner_market()) is None


def test_resolved_positions_settle_at_winner_and_loser():
    tracker = Tracker()
    add_position(tracker, 'p_won', 'm_won', outcome='NO', entry_price=0.93)
    add_position(tracker, 'p_lost', 'm_lost', outcome='YES', entry_price=0.40)
    rec = Recorder()

    markets = {'m_won': resolved_market('NO'), 'm_lost': resolved_market('NO')}
    summary = reconcile_restored_state(
        tracker, OrderCoordinator(), lambda mid: markets.get(mid),
        rec.close, rec.cancel,
    )

    assert summary == {
        'kept': 0, 'settled_won': 1, 'settled_lost': 1, 'dropped': 0, 'cancelled': 0,
    }
    assert rec.closed == [
        ('p_won', 1.0, REASON_RESOLVED),
        ('p_lost', 0.0, REASON_RESOLVED),
    ]


def test_closed_without_winner_settles_pessimistic_at_zero():
    tracker = Tracker()
    add_position(tracker, 'p', 'm', outcome='YES')
    rec = Recorder()

    summary = reconcile_restored_state(
        tracker, OrderCoordinator(), lambda mid: closed_no_winner_market(),
        rec.close, rec.cancel,
    )

    assert summary['settled_lost'] == 1
    assert rec.closed == [('p', 0.0, REASON_CLOSED_UNRESOLVED)]


def test_missing_market_drops_position():
    tracker = Tracker()
    add_position(tracker, 'p', 'm_gone')
    rec = Recorder()

    summary = reconcile_restored_state(
        tracker, OrderCoordinator(), lambda mid: None, rec.close, rec.cancel,
    )

    assert summary['dropped'] == 1
    assert rec.closed == [('p', 0.0, REASON_UNAVAILABLE)]


def test_transient_lookup_failure_keeps_position():
    tracker = Tracker()
    add_position(tracker, 'p', 'm')
    rec = Recorder()

    def boom(mid):
        raise RuntimeError('timeout')

    summary = reconcile_restored_state(
        tracker, OrderCoordinator(), boom, rec.close, rec.cancel,
    )

    assert summary['kept'] == 1
    assert rec.closed == []
    assert 'p' in tracker.positions


def test_live_market_position_is_kept():
    tracker = Tracker()
    add_position(tracker, 'p', 'm')
    rec = Recorder()

    summary = reconcile_restored_state(
        tracker, OrderCoordinator(), lambda mid: open_market(), rec.close, rec.cancel,
    )

    assert summary['kept'] == 1
    assert rec.closed == []
    assert 'p' in tracker.positions


def test_pending_orders_in_dead_markets_are_cancelled():
    oc = OrderCoordinator()
    oc.create_order('o_dead', 'm_dead', 'YES', 'buy', 5.0, 0.9, strategy='t')
    oc.create_order('o_alive', 'm_alive', 'YES', 'buy', 5.0, 0.9, strategy='t')

    markets = {'m_dead': resolved_market('YES'), 'm_alive': open_market()}
    summary = reconcile_restored_state(
        Tracker(), oc, lambda mid: markets.get(mid),
        Recorder().close, oc.cancel_order,
    )

    assert summary['cancelled'] == 1
    assert oc.get_order('o_dead').status == OrderStatus.CANCELLED
    assert oc.get_order('o_alive').status == OrderStatus.PENDING


def test_lookup_is_cached_per_market():
    tracker = Tracker()
    add_position(tracker, 'p1', 'm', outcome='YES')
    add_position(tracker, 'p2', 'm', outcome='YES')
    calls = []

    def lookup(mid):
        calls.append(mid)
        return open_market()

    reconcile_restored_state(
        tracker, OrderCoordinator(), lookup, Recorder().close, Recorder().cancel,
    )
    assert calls == ['m']


def test_full_pipeline_settles_through_paper_fills():
    """End-to-end wiring: reconciliation closes real positions and records P&L."""
    from src.core.paper_fills import PaperFillSimulator
    from src.risk.risk_manager import RiskManager
    from src.utils.profitability_tracker import ProfitabilityTracker

    rm = RiskManager({
        'max_position_size': 1000.0,
        'max_total_exposure': 10000.0,
        'initial_capital': 1000.0,
        'stop_loss_pct': 10.0,
    })
    oc = OrderCoordinator()
    pt = ProfitabilityTracker()
    pt.set_initial_capital(1000.0)
    sim = PaperFillSimulator(oc, rm, pt)

    oc.create_order('o1', 'm_resolved', 'NO', 'buy', size=5.0, price=0.93, strategy='tail_end')
    fills = sim.process_fills(lambda mid, outcome: {'bid': 0.92, 'ask': 0.93})
    assert len(fills) == 1
    assert len(rm.position_tracker.positions) == 1

    summary = reconcile_restored_state(
        rm.position_tracker, oc,
        lambda mid: resolved_market('NO'),
        sim.force_close, oc.cancel_order,
    )

    assert summary['settled_won'] == 1
    assert rm.position_tracker.positions == {}
    closed = rm.position_tracker.closed_positions
    assert len(closed) == 1
    # buy 5.0 USD notional @ 0.93, settled at 1.0 -> (1-0.93)/0.93*5
    assert closed[0].pnl == pytest.approx((1.0 - 0.93) / 0.93 * 5.0)
    assert len(pt.trades) == 1
