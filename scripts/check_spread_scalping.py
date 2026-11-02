#!/usr/bin/env python3
"""
Test script for Spread Scalping Strategy
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import TradingBot
from src.strategies.spread_scalping import SpreadScalpingStrategy
from src.utils.logger import setup_logger, get_trade_logger

logger = setup_logger(__name__)
trade_logger = get_trade_logger()

def test_spread_scalping():
    print("=" * 80)
    print("Testing Spread Scalping Strategy")
    print("=" * 80)
    print()
    
    # Check config
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        print("❌ Error: config/config.yaml not found!")
        return False
        
    try:
        # Initialize bot (to get clients and managers)
        print("📦 Initializing bot...")
        bot = TradingBot(config_path=str(config_path))
        
        # Manually initialize our new strategy
        strategy_config = {
            'enabled': True,
            'min_spread_cents': 0.005, # Very low spread
            'min_liquidity': 100.0, # Very low liquidity
            'min_days_to_expiry': 0, # Any expiry
            'likely_outcome_threshold': 0.1, # Any probability
            'order_size_usdc': 10.0
        }
        
        strategy = SpreadScalpingStrategy(
            name='spread_scalping',
            polymarket_client=bot.polymarket_client,
            risk_manager=bot.risk_manager,
            config=strategy_config,
            market_cache=bot.market_cache
        )
        
        print(f"✅ Strategy initialized")
        print(f"   Min Spread: {strategy.min_spread_cents}")
        print(f"   Min Liquidity: {strategy.min_liquidity}")
        
        # Mock PolymarketClient methods to return a perfect candidate
        original_get_markets = bot.polymarket_client.get_markets
        original_get_best_price = bot.polymarket_client.get_best_price
        
        def mock_get_markets(active=True, limit=100):
            return [{
                'id': '0x1234567890abcdef',
                'market_id': '0x1234567890abcdef',
                'question': 'Will Bitcoin hit $100k by 2025?',
                'outcomes': '["YES", "NO"]',
                'volume': '50000',
                'endDate': '2025-12-31T23:59:59Z',
                'closed': False,
                'accepting_orders': True
            }]
            
        def mock_get_best_price(market_id, outcome="YES"):
            print(f"DEBUG: Mock get_best_price called for {outcome}")
            if outcome == "YES":
                return {'bid': 0.75, 'ask': 0.80, 'spread': 0.05}
            else:
                return {'bid': 0.20, 'ask': 0.25, 'spread': 0.05}
                
        bot.polymarket_client.get_markets = mock_get_markets
        bot.polymarket_client.get_best_price = mock_get_best_price
        
        # Run a few iterations
        print("\n🔄 Running iterations with MOCKED data...")
        
        for i in range(1): # Just 1 iteration needed
            print(f"\n--- Iteration {i+1} ---")
            trades = strategy.run()
            
            if trades:
                print(f"   ✅ Executed {len(trades)} trade(s)")
                for trade in trades:
                    print(f"      {trade}")
            else:
                print("   ℹ️  No trades executed (scanning...)")
            
            time.sleep(1)
            
        # Restore mocks (good practice)
        bot.polymarket_client.get_markets = original_get_markets
        bot.polymarket_client.get_best_price = original_get_best_price
            
        print("\n✅ Test completed!")
        return True
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_spread_scalping()
