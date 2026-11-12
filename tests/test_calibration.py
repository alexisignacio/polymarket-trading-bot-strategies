"""Tests for the price-bucket calibration computation"""

from src.utils.calibration import price_calibration


def rec(entry_price, pnl, strategy='tail_end'):
    return {'entry_price': entry_price, 'pnl': pnl, 'strategy': strategy}


def test_calibration_buckets_compute_win_rate_and_edge():
    records = [
        rec(0.94, 0.32),   # win in 0.93-0.96
        rec(0.95, -5.0),   # loss in 0.93-0.96
        rec(0.97, 0.10),   # win in 0.96-0.99
    ]
    rows = {r['bucket']: r for r in price_calibration(records) if r['trades']}

    mid = rows['0.93-0.96']
    assert mid['trades'] == 2
    assert mid['wins'] == 1
    assert mid['win_rate'] == 50.0
    assert mid['avg_price'] == 0.945
    # edge = 50% - 94.5% = -44.5 pp
    assert mid['edge_pp'] == -44.5
    assert mid['breakeven_win_rate'] == 94.5
    assert mid['total_pnl'] == round(0.32 - 5.0, 2)

    high = rows['0.96-0.99']
    assert high['trades'] == 1
    assert high['win_rate'] == 100.0
    assert high['edge_pp'] == round((1.0 - 0.97) * 100, 1)


def test_calibration_filters_by_strategy():
    records = [rec(0.94, 1.0, 'tail_end'), rec(0.94, -1.0, 'spread_scalping')]
    only_te = price_calibration(records, strategy='tail_end')
    assert sum(r['trades'] for r in only_te) == 1


def test_empty_buckets_are_reported_with_zero_trades():
    rows = price_calibration([])
    assert rows
    assert all(r['trades'] == 0 for r in rows)
    assert all(r['win_rate'] is None for r in rows)


def test_calibration_accepts_objects_with_attributes():
    class T:
        entry_price = 0.95
        pnl = 0.5
        strategy = 'tail_end'
    rows = [r for r in price_calibration([T()]) if r['trades']]
    assert rows[0]['trades'] == 1
