"""Strategy 4: Single-market arbitrage (YES+NO < $1.00)"""

from datetime import datetime
from typing import Dict, List, Optional
from ..strategies.base_strategy import BaseStrategy
from ..utils.logger import setup_logger, get_trade_logger, get_error_logger
from ..utils.market_analyzer import MarketAnalyzer


logger = setup_logger(__name__)
trade_logger = get_trade_logger()
error_logger = get_error_logger()


class SingleArbitrageStrategy(BaseStrategy):
    """Single-market arbitrage: buy all outcomes when total < $1.00"""
    
    def __init__(self, *args, **kwargs):
        """Initialize single arbitrage strategy"""
        super().__init__(*args, **kwargs)
        
        # Strategy parameters
        self.max_total_price = self.config.get('max_total_price', 0.99)  # Max 99¢ total
        self.min_profit_pct = self.config.get('min_profit_pct', 1.0)  # Minimum 1% profit
        self.position_size = self.config.get('position_size', 100.0)
        self.require_clear_resolution = self.config.get('require_clear_resolution', True)
        
        self.analyzer = MarketAnalyzer()
        self.arbitrage_positions: Dict[str, Dict] = {}
    
    def scan_opportunities(self) -> List[Dict]:
        """
        Scan for markets where YES+NO < $1.00.
        
        Returns:
            List of arbitrage opportunities
        """
        opportunities = []
        
        try:
            # Neutralize incomplete legs before looking for new entries
            self._manage_legs()
            
            # Use market cache if available
            if self.market_cache:
                markets = self.market_cache.get_markets(active=True, limit=200)
            else:
                markets = self.polymarket_client.get_markets(active=True, limit=200)
            
            # Ensure markets is a list
            if not isinstance(markets, list):
                logger.warning(f"  [{self.name}] get_markets returned non-list: {type(markets)}")
                return []
            
            # Prefetch YES/NO prices for all markets in batch (1 request per ~25 tokens)
            pairs = []
            for market in markets:
                if not isinstance(market, dict):
                    continue
                mid = market.get('market_id') or market.get('id')
                if mid:
                    pairs.append((mid, 'YES'))
                    pairs.append((mid, 'NO'))
            if self.market_cache and pairs:
                prices_map = self.market_cache.get_prices_parallel(pairs)
            else:
                prices_map = {}
            
            for market in markets:
                # Ensure market is a dict
                if not isinstance(market, dict):
                    continue
                    
                market_id = market.get('id') or market.get('market_id')
                if not market_id:
                    continue
                
                # Check resolution clarity if required (real CLOB fields:
                # resolution_source does not exist; end date + description do)
                if self.require_clear_resolution:
                    if not market.get('end_date_iso') or not market.get('description', '').strip():
                        continue
                
                try:
                    # Get prices for YES and NO (batched above; direct fallback)
                    yes_prices = prices_map.get(f"{market_id}_YES")
                    no_prices = prices_map.get(f"{market_id}_NO")
                    if yes_prices is None:
                        yes_prices = self.polymarket_client.get_best_price(market_id, outcome="YES") or {}
                    if no_prices is None:
                        no_prices = self.polymarket_client.get_best_price(market_id, outcome="NO") or {}
                    
                    yes_ask = yes_prices.get('ask')
                    no_ask = no_prices.get('ask')
                    
                    if not yes_ask or not no_ask:
                        continue
                    
                    # Check for arbitrage opportunity
                    arb_opp = self.analyzer.find_arbitrage_opportunity(yes_ask, no_ask)
                    
                    if arb_opp and arb_opp['total_price'] <= self.max_total_price:
                        if arb_opp['profit_pct'] >= self.min_profit_pct:
                            opportunity = {
                                'market_id': market_id,
                                'yes_price': yes_ask,
                                'no_price': no_ask,
                                'total_price': arb_opp['total_price'],
                                'profit': arb_opp['profit'],
                                'profit_pct': arb_opp['profit_pct'],
                                'market_question': market.get('question'),
                                'end_date': market.get('end_date_iso')
                            }
                            
                            opportunities.append(opportunity)
                
                except Exception as e:
                    logger.debug(f"Error scanning market {market_id}: {e}")
                    continue
            
        except Exception as e:
            logger.error(f"  [{self.name}] Error scanning for arbitrage opportunities: {e}", exc_info=True)
        
        # Sort by profit percentage (highest first)
        opportunities.sort(key=lambda x: x['profit_pct'], reverse=True)
        
        if opportunities:
            logger.debug(f"  [{self.name}] Top opportunity: Market {opportunities[0].get('market_id', 'unknown')[:20]} | Profit: {opportunities[0].get('profit_pct', 0):.2f}% | Total cost: ${opportunities[0].get('total_price', 0):.4f}")
        
        return opportunities
    
    LEG_TIMEOUT_SEC = 60.0
    
    def _manage_legs(self) -> None:
        """Leg risk management: an arbitrage pair must stay balanced.
        
        If one leg filled and the other is still pending after a timeout,
        cancel the pending leg and exit the filled one (market sell at bid)
        so we never hold directional exposure by accident.
        """
        coordinator = getattr(self.polymarket_client, '_order_coordinator', None)
        if not coordinator:
            return
        
        for position_id, pos in list(self.arbitrage_positions.items()):
            yes_order = coordinator.get_order(pos.get('yes_order_id') or '')
            no_order = coordinator.get_order(pos.get('no_order_id') or '')
            if not yes_order or not no_order:
                self.arbitrage_positions.pop(position_id, None)
                continue
            
            yes_filled = yes_order.status.value == 'filled'
            no_filled = no_order.status.value == 'filled'
            if yes_filled and no_filled:
                # Pair complete: leave to resolution / normal lifecycle
                self.arbitrage_positions.pop(position_id, None)
                continue
            
            pending_order = no_order if (yes_filled and not no_filled) else \
                yes_order if (no_filled and not yes_filled) else None
            if pending_order is None:
                continue
            
            age = (datetime.now() - pending_order.created_at).total_seconds()
            if age < self.LEG_TIMEOUT_SEC:
                continue
            
            filled_order = yes_order if yes_filled else no_order
            logger.warning(
                f"[{self.name}] Unbalanced legs on {pos['market_id']}: "
                f"canceling {pending_order.order_id} and exiting {filled_order.order_id}"
            )
            
            # Cancel the unfilled leg
            try:
                self.polymarket_client.cancel_order(pending_order.order_id)
                coordinator.cancel_order(pending_order.order_id)
            except Exception as e:
                logger.debug(f"Cancel failed for {pending_order.order_id}: {e}")
            
            # Exit the filled leg at current bid
            try:
                prices = self.polymarket_client.get_best_price(
                    pos['market_id'], outcome=filled_order.outcome
                )
                bid = prices.get('bid')
                if bid:
                    order_coordinator = coordinator
                    self.polymarket_client.place_order(
                        market_id=pos['market_id'],
                        outcome=filled_order.outcome,
                        side='sell',
                        size=pos.get('size', self.position_size),
                        price=bid,
                        order_coordinator=order_coordinator,
                        strategy=self.name
                    )
                    trade_logger.info(
                        f"⚖️ [{self.name}] Leg exit: sold {filled_order.outcome} @ {bid:.4f} "
                        f"on {pos['market_id'][:24]}... (unbalanced pair)"
                    )
            except Exception as e:
                logger.debug(f"Leg exit failed: {e}")
            
            self.arbitrage_positions.pop(position_id, None)
    
    def execute_trade(self, opportunity: Dict) -> Optional[Dict]:
        """
        Execute arbitrage: buy all outcomes simultaneously.
        
        Args:
            opportunity: Arbitrage opportunity
            
        Returns:
            Trade result dictionary
        """
        return self._execute_binary_arbitrage(opportunity)
    
    def _execute_binary_arbitrage(self, opportunity: Dict) -> Optional[Dict]:
        """Execute binary market arbitrage (YES/NO)"""
        market_id = opportunity['market_id']
        yes_price = opportunity['yes_price']
        no_price = opportunity['no_price']
        
        # Check risk limits for both sides
        allowed_yes, reason_yes = self.risk_manager.check_trade_allowed(
            strategy=self.name,
            market_id=market_id,
            size=self.position_size,
            price=yes_price,
            side='buy'
        )
        
        allowed_no, reason_no = self.risk_manager.check_trade_allowed(
            strategy=self.name,
            market_id=market_id,
            size=self.position_size,
            price=no_price,
            side='buy'
        )
        
        if not allowed_yes or not allowed_no:
            logger.debug(f"Arbitrage trade not allowed: yes={reason_yes}, no={reason_no}")
            return None
        
        try:
            # Get order coordinator from client if available
            order_coordinator = getattr(self.polymarket_client, '_order_coordinator', None)
            
            # Place YES order
            yes_order = self.polymarket_client.place_order(
                market_id=market_id,
                outcome="YES",
                side='buy',
                size=self.position_size,
                price=yes_price,
                order_coordinator=order_coordinator,
                strategy=self.name
            )
            
            if not yes_order:
                return None
            
            # Place NO order
            no_order = self.polymarket_client.place_order(
                market_id=market_id,
                outcome="NO",
                side='buy',
                size=self.position_size,
                price=no_price,
                order_coordinator=order_coordinator,
                strategy=self.name
            )
            
            if not no_order:
                # Cancel YES order if NO fails
                if 'order_id' in yes_order:
                    self.polymarket_client.cancel_order(yes_order['order_id'])
                return None
            
            # Track position
            position_id = self._generate_position_id()
            
            self.arbitrage_positions[position_id] = {
                'market_id': market_id,
                'yes_order_id': yes_order.get('order_id'),
                'no_order_id': no_order.get('order_id'),
                'yes_price': yes_price,
                'no_price': no_price,
                'total_price': opportunity['total_price'],
                'expected_profit': opportunity['profit'],
                'size': self.position_size
            }
            
            trade_logger.info("=" * 80)
            trade_logger.info(f"🎯 TRADE EXECUTED: Single-market Arbitrage | {position_id}")
            trade_logger.info(f"   Market: {market_id[:30]}...")
            trade_logger.info(f"   YES @ ${yes_price:.4f} | NO @ ${no_price:.4f}")
            trade_logger.info(f"   Total Cost: ${opportunity['total_price']:.4f}")
            trade_logger.info(f"   Expected Profit: ${opportunity['profit']:.4f} ({opportunity['profit_pct']:.2f}%)")
            trade_logger.info(f"   Size: ${self.position_size:.2f} per outcome")
            trade_logger.info("=" * 80)
            
            return {
                'position_id': position_id,
                'strategy': self.name,
                'market_id': market_id,
                'yes_order': yes_order,
                'no_order': no_order,
                'expected_profit': opportunity['profit'],
                'expected_profit_pct': opportunity['profit_pct']
            }
        
        except Exception as e:
            error_logger.error(f"Error executing binary arbitrage: {e}", exc_info=True)
            return None
