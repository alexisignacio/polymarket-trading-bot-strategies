"""Price-bucket calibration: does the price paid predict the win rate?

For near-resolution strategies (tail_end) the whole premise is that a 0.93-0.95
price understates the true win probability. This module measures exactly that:
group closed trades by entry price and compare the average price paid against
the observed win rate. A positive edge_pp (win_rate - avg_price) means the
entries outperform their own implied probability; a negative number means the
strategy pays more than it wins.

Works on serialized trade records (dicts with 'entry_price' and 'pnl'), i.e.
the persisted trade_history from bot_state.json.
"""

from typing import Any, Dict, List, Sequence

# Half-open buckets [lo, hi); the last one is closed on the right
DEFAULT_BUCKETS = (
    (0.0, 0.5),
    (0.5, 0.75),
    (0.75, 0.9),
    (0.9, 0.93),
    (0.93, 0.96),
    (0.96, 0.99),
    (0.99, 1.0000001),
)


def _get(record: Any, key: str, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def price_calibration(
    records: Sequence[Any],
    buckets=DEFAULT_BUCKETS,
    strategy: str = None,
) -> List[Dict[str, Any]]:
    """Return one row per price bucket with trades, win rate and edge.

    Args:
        records: closed trade records (dicts or TradeRecord-like objects)
        buckets: sequence of (lo, hi) entry-price ranges
        strategy: optional filter on record strategy

    Each row: {'bucket', 'trades', 'wins', 'win_rate', 'avg_price',
               'edge_pp', 'avg_pnl', 'total_pnl', 'breakeven_win_rate'}
    """
    rows: List[Dict[str, Any]] = []
    selected = [
        r for r in records
        if strategy is None or str(_get(r, 'strategy') or '') == strategy
    ]

    for lo, hi in buckets:
        group = [
            r for r in selected
            if _get(r, 'entry_price') is not None and lo <= float(_get(r, 'entry_price')) < hi
        ]
        n = len(group)
        if n == 0:
            rows.append({
                'bucket': f'{lo:.2f}-{hi:.2f}'.rstrip('0').rstrip('.') if hi <= 1.0 else f'{lo:.2f}+',
                'trades': 0, 'wins': 0, 'win_rate': None, 'avg_price': None,
                'edge_pp': None, 'avg_pnl': None, 'total_pnl': 0.0,
                'breakeven_win_rate': None,
            })
            continue

        wins = sum(1 for r in group if float(_get(r, 'pnl') or 0.0) > 0)
        avg_price = sum(float(_get(r, 'entry_price')) for r in group) / n
        win_rate = wins / n
        total_pnl = sum(float(_get(r, 'pnl') or 0.0) for r in group)
        rows.append({
            'bucket': f'{lo:.2f}-{hi:.2f}' if hi <= 1.0 else f'{lo:.2f}+',
            'trades': n,
            'wins': wins,
            'win_rate': round(win_rate * 100, 1),
            'avg_price': round(avg_price, 4),
            # Percentage points of edge vs the implied probability of the price
            'edge_pp': round((win_rate - avg_price) * 100, 1),
            'avg_pnl': round(total_pnl / n, 4),
            'total_pnl': round(total_pnl, 2),
            'breakeven_win_rate': round(avg_price * 100, 1),
        })
    return rows
