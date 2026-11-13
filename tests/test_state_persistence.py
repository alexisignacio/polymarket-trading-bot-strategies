"""Tests for cumulative profitability persistence across restarts"""

from datetime import datetime

import pytest

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


def test_strategy_stats_and_trade_history_survive_restart(tmp_path):
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file=str(state_file))

    pt1 = ProfitabilityTracker()
    pt1.set_initial_capital(1000.0)
    pt1.record_trade(make_trade(2.0))
    pt1.record_trade(make_trade(-1.0))

    assert sm.save_state(PositionTracker(), OrderCoordinator(), pt1)

    pt2 = ProfitabilityTracker()
    pt2.set_initial_capital(1000.0)
    state = sm.load_state()
    sm.restore_profitability(state, PositionTracker(), pt2)

    # Per-strategy stats restored
    stats = pt2.strategy_stats['test']
    assert stats['total_trades'] == 2
    assert stats['winning_trades'] == 1
    assert stats['losing_trades'] == 1
    assert stats['total_pnl'] == 1.0

    # Trade records restored (reporting) and new trades append
    records = pt2.get_trade_records()
    assert len(records) == 2
    assert {r.trade_id for r in records} == {'t_2.0', 't_-1.0'}

    pt2.record_trade(make_trade(0.5))
    records = pt2.get_trade_records()
    assert len(records) == 3
    # Cumulative counters must NOT double count the restored records
    assert pt2.get_overall_stats()['total_trades'] == 3


def test_trade_history_round_trip_preserves_fields(tmp_path):
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file=str(state_file))

    pt1 = ProfitabilityTracker()
    pt1.set_initial_capital(1000.0)
    pt1.record_trade(make_trade(1.234))
    sm.save_state(PositionTracker(), OrderCoordinator(), pt1)

    pt2 = ProfitabilityTracker()
    pt2.set_initial_capital(1000.0)
    sm.restore_profitability(sm.load_state(), PositionTracker(), pt2)

    t = pt2.trade_history[0]
    assert t.pnl == pytest.approx(1.234)
    assert t.strategy == 'test'
    assert t.side == 'buy'
    assert t.fees == 0.0


def test_pid_file_refuses_second_bot(tmp_path, monkeypatch):
    """Dos bots compartiendo bot_state.json se corrompen: el pid file lo impide."""
    import os
    import src.bot as bot_module

    pid_file = tmp_path / "bot.pid"
    monkeypatch.setattr(bot_module, "PID_FILE", pid_file)
    dummy = object.__new__(bot_module.TradingBot)

    # Un PID vivo ajeno bloquea el arranque (el del padre, que sí está vivo)
    pid_file.write_text(str(os.getppid()))
    with pytest.raises(RuntimeError, match="Otro bot ya"):
        dummy._write_pid_file()

    # Un pid file muerto se toma sin problema
    pid_file.write_text("999999999")
    dummy._write_pid_file()
    assert pid_file.read_text().strip() == str(os.getpid())


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
