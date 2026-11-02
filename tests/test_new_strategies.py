
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
        print("✅ Tail End Strategy verified")

if __name__ == '__main__':
    unittest.main()
