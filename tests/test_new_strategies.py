
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
import sys
import os

# Add src to path
sys.path.insert(0, os.path.abspath("src"))

from src.strategies.tail_end_strategy import TailEndStrategy

class TestNewStrategies(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_risk_manager = MagicMock()
        self.mock_market_cache = MagicMock()
        self.config = {}

    def test_tail_end_strategy(self):
        # Mocks must return list/dict shapes the strategy actually iterates
        self.mock_risk_manager.position_tracker.get_positions_by_strategy.return_value = []
        self.mock_risk_manager.position_tracker.get_positions_by_market.return_value = []
        self.mock_risk_manager.check_trade_allowed.return_value = (True, None)
        self.mock_client._order_coordinator = None
        self.mock_market_cache.get_prices_parallel.side_effect = (
            lambda pairs: {f"{m}_{o}": self.mock_market_cache.get_price(m, o) for m, o in pairs}
        )
        strategy = TailEndStrategy(
            name="tail_end",
            polymarket_client=self.mock_client,
            risk_manager=self.mock_risk_manager,
            config={'min_price': 0.90, 'max_price': 0.99, 'max_days_to_expiry': 7,
                    'min_return_pct': 2.0, 'max_spread_pct': 5.0},
            market_cache=self.mock_market_cache
        )
        
        # Mock Markets
        future_date = (datetime.utcnow() + timedelta(days=3)).isoformat()
        far_date = (datetime.utcnow() + timedelta(days=20)).isoformat()
        
        markets = [
            {'id': 'm1', 'question': 'Will X happen?', 'end_date_iso': future_date, 'tokens': [{'token_id': 't1', 'outcome': 'YES'}]},
            {'id': 'm2', 'question': 'Will Y happen?', 'end_date_iso': far_date, 'tokens': [{'token_id': 't2', 'outcome': 'YES'}]}
        ]
        self.mock_market_cache.get_markets.return_value = markets
        
        # Mock Prices
        # m1: YES @ 0.95 (Match)
        self.mock_market_cache.get_price.side_effect = lambda mid, outcome: (
            {'bid': 0.94, 'ask': 0.95} if mid == 'm1' else {'bid': 0.49, 'ask': 0.50}
        )
        
        opportunities = strategy.scan_opportunities()
        
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]['market_id'], 'm1')
        self.assertEqual(opportunities[0]['price'], 0.95)
        # Fee-aware economics: net return below gross, breakeven above the price
        self.assertLess(opportunities[0]['return_pct'], opportunities[0]['gross_return_pct'])
        self.assertGreater(opportunities[0]['breakeven_win_prob'], 0.95)
        print("✅ Tail End Strategy verified")

    def test_tail_end_rejects_when_fees_eat_the_edge(self):
        """Con fees de taker altas el retorno neto cae bajo el mínimo y no entra"""
        self.mock_risk_manager.position_tracker.get_positions_by_strategy.return_value = []
        self.mock_risk_manager.position_tracker.get_positions_by_market.return_value = []
        self.mock_client._order_coordinator = None
        self.mock_market_cache.get_prices_parallel.side_effect = (
            lambda pairs: {f"{m}_{o}": self.mock_market_cache.get_price(m, o) for m, o in pairs}
        )
        strategy = TailEndStrategy(
            name="tail_end",
            polymarket_client=self.mock_client,
            risk_manager=self.mock_risk_manager,
            config={'min_price': 0.90, 'max_price': 0.99, 'max_days_to_expiry': 7,
                    'min_return_pct': 2.0, 'max_spread_pct': 5.0, 'taker_fee_rate': 5.0},
            market_cache=self.mock_market_cache
        )
        future_date = (datetime.utcnow() + timedelta(days=3)).isoformat()
        self.mock_market_cache.get_markets.return_value = [
            {'id': 'm1', 'question': 'Will X happen?', 'end_date_iso': future_date,
             'tokens': [{'token_id': 't1', 'outcome': 'YES'}]}
        ]
        self.mock_market_cache.get_price.side_effect = lambda mid, outcome: {'bid': 0.94, 'ask': 0.95}
        self.assertEqual(strategy.scan_opportunities(), [])

    def test_spread_scalping_requires_recent_flow(self):
        """Sin flujo real reciente no se entra: nada de cotizar mercados muertos"""
        import time
        from src.strategies.spread_scalping import SpreadScalpingStrategy

        self.mock_risk_manager.position_tracker.get_positions_by_strategy.return_value = []
        self.mock_client._order_coordinator = None
        prices = {'bid': 0.70, 'ask': 0.76}

        strategy = SpreadScalpingStrategy(
            name="spread_scalping",
            polymarket_client=self.mock_client,
            risk_manager=self.mock_risk_manager,
            config={'min_spread_cents': 0.03, 'likely_outcome_threshold': 0.60,
                    'min_recent_prints': 1, 'recent_prints_window': 1800},
            market_cache=self.mock_market_cache,
            flow_lookup=lambda mid: [],
        )
        self.assertFalse(strategy._analyze_opportunity('m1', 'YES', prices, []))

        fresh = [{'price': 0.71, 'timestamp': time.time(), 'outcome': 'YES'}]
        strategy.flow_lookup = lambda mid: fresh
        opportunities = []
        self.assertTrue(strategy._analyze_opportunity('m1', 'YES', prices, opportunities))
        self.assertEqual(len(opportunities), 1)

    def test_spread_scalping_cancels_stale_orders(self):
        """Unfilled resting orders must expire, or they block the scan forever"""
        from src.core.order_coordinator import OrderCoordinator, OrderStatus
        from src.strategies.spread_scalping import SpreadScalpingStrategy

        coordinator = OrderCoordinator()
        self.mock_client._order_coordinator = coordinator
        self.mock_risk_manager.position_tracker.get_positions_by_strategy.return_value = []

        strategy = SpreadScalpingStrategy(
            name="spread_scalping",
            polymarket_client=self.mock_client,
            risk_manager=self.mock_risk_manager,
            config={'order_ttl_seconds': 120.0},
            market_cache=self.mock_market_cache,
        )

        stale = coordinator.create_order('o_old', 'm1', 'NO', 'buy', 10.0, 0.9,
                                         strategy='spread_scalping')
        stale.created_at = datetime.now() - timedelta(seconds=300)
        fresh = coordinator.create_order('o_new', 'm2', 'NO', 'buy', 10.0, 0.8,
                                         strategy='spread_scalping')

        strategy._manage_existing_positions([])

        self.assertEqual(coordinator.get_order('o_old').status, OrderStatus.CANCELLED)
        self.assertEqual(coordinator.get_order('o_new').status, OrderStatus.PENDING)
        self.assertIs(fresh, coordinator.get_order('o_new'))
        self.assertEqual(strategy.active_markets, {'m2'})
        print("✅ Spread Scalping stale-order expiry verified")

if __name__ == '__main__':
    unittest.main()
