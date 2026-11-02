"""Normalize Polymarket CLOB API payloads into the internal market schema.

Raw CLOB markets look like:

    {
      "condition_id": "0x...",
      "question": "...",
      "description": "...",
      "end_date_iso": "2027-01-01T00:00:00Z",
      "active": true, "closed": false,
      "accepting_orders": true, "enable_order_book": true,
      "tokens": [
        {"token_id": "71351...", "outcome": "Yes", "price": 0, "winner": false},
        {"token_id": "64468...", "outcome": "No",  "price": 1, "winner": false}
      ],
      ...
    }

There is no "id", "market_id", "volume" or "resolution_source" field. Internal
code uses a single canonical schema produced here so strategies never touch raw
API shapes:

    market_id       -> condition_id (canonical market identifier)
    yes_token_id    -> token_id of the "Yes" outcome
    no_token_id     -> token_id of the "No" outcome
    tokens          -> [{'token_id', 'outcome'}] with outcome uppercased
    question, description, end_date_iso, active, closed, accepting_orders,
    enable_order_book, neg_risk, minimum_tick_size, minimum_order_size
"""

from typing import Dict, Optional, Tuple


def normalize_market(raw: Dict) -> Dict:
    """Map a raw CLOB market dict to the canonical internal schema.

    Unknown/extra raw fields are preserved under their original keys so nothing
    silently disappears; canonical keys always win on conflict.
    """
    market = dict(raw)

    condition_id = raw.get('condition_id') or raw.get('id') or raw.get('market_id')
    token_ids: Dict[str, str] = {}
    for token in raw.get('tokens') or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get('outcome') or '').upper()
        token_id = token.get('token_id') or token.get('tokenId')
        if token_id and outcome in ('YES', 'NO'):
            token_ids[outcome] = str(token_id)

    market['market_id'] = condition_id
    market['condition_id'] = condition_id
    market['question'] = raw.get('question')
    market['description'] = raw.get('description', '')
    market['end_date_iso'] = raw.get('end_date_iso') or raw.get('endDate')
    market['active'] = bool(raw.get('active', True))
    market['closed'] = bool(raw.get('closed', False))
    market['accepting_orders'] = bool(raw.get('accepting_orders', True))
    market['enable_order_book'] = bool(raw.get('enable_order_book', True))
    market['neg_risk'] = bool(raw.get('neg_risk', False))
    market['minimum_tick_size'] = raw.get('minimum_tick_size')
    market['minimum_order_size'] = raw.get('minimum_order_size')
    market['yes_token_id'] = token_ids.get('YES')
    market['no_token_id'] = token_ids.get('NO')
    market['tokens'] = [
        {'token_id': token_ids[o], 'outcome': o} for o in ('YES', 'NO') if o in token_ids
    ]
    return market


def best_bid_ask(orderbook: Dict) -> Tuple[Optional[float], Optional[float]]:
    """Extract best bid/ask from an orderbook, independent of level ordering.

    CLOB returns bids ascending and asks descending (worst price first in both),
    so indexing [0] gives the *worst* prices. Best bid is the highest bid, best
    ask the lowest ask.
    """
    def _price(level) -> float:
        if isinstance(level, dict):
            return float(level.get('price', 0) or 0)
        if isinstance(level, (list, tuple)) and level:
            return float(level[0])
        return 0.0

    bids = orderbook.get('bids') or []
    asks = orderbook.get('asks') or []

    bid_prices = [p for p in map(_price, bids) if 0 < p < 1]
    ask_prices = [p for p in map(_price, asks) if 0 < p < 1]

    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask
