"""Strategy 4: Tail-End Trading (High Probability & Near Expiry)"""

from typing import Dict, List, Optional
from datetime import datetime
from dateutil import parser

from ..strategies.base_strategy import BaseStrategy
from ..risk.position_tracker import Position
from ..utils.logger import setup_logger, get_trade_logger, get_error_logger

logger = setup_logger(__name__)
trade_logger = get_trade_logger()
error_logger = get_error_logger()


class TailEndStrategy(BaseStrategy):
    """
    Tail-End Trading Strategy:
    Buy high-probability outcomes (e.g., >$0.93) near resolution (e.g., < 7 days)
    and exit early via take-profit / stop-loss instead of holding blindly.

    Honest economics: a share pays exactly $1 at resolution, so the gross return
    of buying at `ask` is (1 - ask) / ask. We only enter when that return clears
    `min_return_pct` AND the bid/ask spread is small enough that an early exit
    would not eat the whole edge.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Entry parameters
        self.min_price = self.config.get('min_price', 0.93)
        self.max_price = self.config.get('max_price', 0.98)
        self.max_days_to_expiry = self.config.get('max_days_to_expiry', 7)
        self.position_size = self.config.get('position_size', 5.0)
        self.max_positions = self.config.get('max_positions', 10)

        # Entry economics: gross return at resolution must clear this
        self.min_return_pct = self.config.get('min_return_pct', 2.0)
        self.max_spread_pct = self.config.get('max_spread_pct', 3.0)

        # Exit parameters (these actually place sell orders now)
        self.take_profit = self.config.get('take_profit', 0.99)
        self.stop_loss = self.config.get('stop_loss', 0.85)

    # ------------------------------------------------------------------ scan

    def scan_opportunities(self) -> List[Dict]:
        """Scan for high-probability markets near expiry."""
        opportunities = []

        try:
            self._manage_positions()

            open_count = len(self._open_positions())
            if open_count >= self.max_positions:
                logger.debug(f"[{self.name}] Max positions reached ({open_count})")
                return []

            if self.market_cache:
                markets = self.market_cache.get_markets(active=True, limit=100)
            else:
                markets = self.polymarket_client.get_markets(active=True, limit=100)

            if not isinstance(markets, list):
                return []

            # Prefetch prices in batch for all candidate tokens
            pairs = []
            for market in markets:
                if not isinstance(market, dict):
                    continue
                mid = market.get('market_id') or market.get('id')
                if not mid or self._has_position_or_order(mid):
                    continue
                for token in market.get('tokens', []):
                    tid = token.get('token_id')
                    outcome = token.get('outcome', 'UNKNOWN')
                    if tid:
                        pairs.append((mid, outcome))
            prices_map = self.market_cache.get_prices_parallel(pairs) if (self.market_cache and pairs) else {}

            for market in markets:
                market_id = market.get('market_id') or market.get('id')
                if not market_id or self._has_position_or_order(market_id):
                    continue

                end_date_str = market.get('end_date_iso') or market.get('endDate')
                if not self._check_expiry_window(end_date_str):
                    continue

                tokens = market.get('tokens', [])
                if not tokens:
                    continue

                try:
                    for token in tokens:
                        token_id = token.get('token_id')
                        outcome = token.get('outcome', 'UNKNOWN')
                        if not token_id:
                            continue

                        price_info = prices_map.get(f"{market_id}_{outcome}")
                        if price_info is None:
                            if self.market_cache:
                                price_info = self.market_cache.get_price(market_id, outcome=outcome)
                            else:
                                price_info = self.polymarket_client.get_best_price(token_id, outcome=outcome)

                        if not price_info:
                            continue

                        ask = float(price_info.get('ask') or 0)
                        bid = float(price_info.get('bid') or 0)

                        if not (self.min_price <= ask <= self.max_price):
                            continue

                        # Honest EV: gross return if held to resolution
                        if ask <= 0:
                            continue
                        return_pct = ((1.0 - ask) / ask) * 100
                        if return_pct < self.min_return_pct:
                            continue

                        # The spread must not eat the edge on an early exit
                        spread_pct = ((ask - bid) / ask) * 100 if (ask > bid > 0) else 100.0
                        if spread_pct > self.max_spread_pct:
                            continue

                        logger.info(
                            f"[{self.name}] Found opportunity: {market.get('question')} | "
                            f"{outcome} @ {ask} (return {return_pct:.1f}%, spread {spread_pct:.1f}%)"
                        )

                        opportunities.append({
                            'market_id': market_id,
                            'token_id': token_id,
                            'outcome': outcome,
                            'price': ask,
                            'return_pct': return_pct,
                            'spread_pct': spread_pct,
                            'question': market.get('question'),
                            'end_date': end_date_str
                        })

                except Exception as e:
                    logger.debug(f"Error checking market {market_id}: {e}")
                    continue

            opportunities.sort(key=lambda x: x['price'])

        except Exception as e:
            error_logger.error(f"[{self.name}] Error scanning: {e}", exc_info=True)

        return opportunities

    # ----------------------------------------------------------------- exits

    def _manage_positions(self) -> None:
        """Place exit orders for open positions: take-profit / stop-loss / expiry."""
        positions = list(self._open_positions())
        if not positions:
            return
        
        # Batch price lookup: one request for all open positions
        pairs = [(p.market_id, getattr(p, 'outcome', 'YES')) for p in positions]
        if hasattr(self.polymarket_client, 'get_best_prices_batch'):
            prices_map = self.polymarket_client.get_best_prices_batch(pairs)
        else:
            prices_map = {}
        
        for position in positions:
            try:
                outcome = getattr(position, 'outcome', 'YES')
                prices = prices_map.get(f"{position.market_id}_{outcome}")
                if prices is None:
                    prices = self.polymarket_client.get_best_price(position.market_id, outcome=outcome)
                bid = float((prices or {}).get('bid') or 0)

                if bid <= 0:
                    continue

                reason = None
                if bid >= self.take_profit:
                    reason = f"take-profit (bid {bid:.3f} >= {self.take_profit})"
                elif bid <= self.stop_loss:
                    reason = f"stop-loss (bid {bid:.3f} <= {self.stop_loss})"

                if reason is None:
                    continue

                logger.info(f"[{self.name}] Exiting {position.position_id}: {reason}")
                order_coordinator = getattr(self.polymarket_client, '_order_coordinator', None)
                order = self.polymarket_client.place_order(
                    market_id=position.market_id,
                    outcome=outcome,
                    side='sell',
                    size=position.size,
                    price=bid,
                    order_coordinator=order_coordinator,
                    strategy=self.name
                )
                if order and order.get('order_id'):
                    trade_logger.info(
                        f"🔚 [{self.name}] EXIT {position.position_id} @ {bid:.3f} — {reason}"
                    )

            except Exception as e:
                logger.debug(f"Error managing position {position.position_id}: {e}")

    # --------------------------------------------------------------- helpers

    def _open_positions(self) -> List[Position]:
        """Open positions of this strategy (book truth, not a local set)."""
        return self.risk_manager.position_tracker.get_positions_by_strategy(self.name)

    def _has_position_or_order(self, market_id: str) -> bool:
        """True if we already have exposure or a pending order in this market."""
        if self.risk_manager.position_tracker.get_positions_by_market(market_id):
            return True
        coordinator = getattr(self.polymarket_client, '_order_coordinator', None)
        if coordinator:
            for order in coordinator.get_pending_orders(market_id=market_id, strategy=self.name):
                return True
            for order in coordinator.get_orders_by_market(market_id):
                if order.strategy == self.name and order.status.value == 'filled':
                    return True
        return False

    def _check_expiry_window(self, end_date_str: str) -> bool:
        if not end_date_str:
            return False
        try:
            end_date = parser.parse(end_date_str).replace(tzinfo=None)
            now = datetime.utcnow()
            diff = end_date - now
            days_diff = diff.days

            # Must be in future but close
            return 0 <= days_diff <= self.max_days_to_expiry
        except Exception:
            return False

    # --------------------------------------------------------------- execute

    def execute_trade(self, opportunity: Dict) -> Optional[Dict]:
        """Execute buy for high probability outcome"""
        market_id = opportunity['market_id']
        outcome = opportunity['outcome']
        price = opportunity['price']

        allowed, reason = self.risk_manager.check_trade_allowed(
            strategy=self.name,
            market_id=market_id,
            size=self.position_size,
            price=price,
            side='buy'
        )

        if not allowed:
            logger.debug(f"[{self.name}] Trade rejected: {reason}")
            return None

        try:
            logger.info(f"[{self.name}] Executing trade for {market_id} {outcome} @ {price}")

            order_coordinator = getattr(self.polymarket_client, '_order_coordinator', None)

            order = self.polymarket_client.place_order(
                market_id=market_id,
                outcome=outcome,
                side='buy',
                size=self.position_size,
                price=price,
                order_coordinator=order_coordinator,
                strategy=self.name
            )

            if order and order.get('order_id'):
                trade_logger.info("=" * 80)
                trade_logger.info("🎯 TRADE EXECUTED: Tail-End Strategy")
                trade_logger.info(f"   Market: {market_id}")
                trade_logger.info(f"   Outcome: {outcome} @ ${price:.3f}")
                trade_logger.info(f"   Size: ${self.position_size:.2f} | Expected return: {opportunity.get('return_pct', 0):.1f}%")
                trade_logger.info("=" * 80)

                return {
                    'position_id': order.get('order_id'),
                    'market_id': market_id,
                    'price': price,
                    'strategy': self.name
                }

        except Exception as e:
            error_logger.error(f"[{self.name}] Error executing trade: {e}")
            return None
