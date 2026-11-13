"""Main bot orchestrator"""

import os
import time
import signal
from pathlib import Path
from typing import Dict, Optional

from .exchanges.polymarket.adapter import PolymarketAdapter
from .api.perpdex_client import PerpdexClient
from .api.auth import AuthManager
from .api.data_api import DataAPIClient
from .risk.risk_manager import RiskManager
from .core.order_coordinator import OrderCoordinator
from .core.paper_fills import PaperFillSimulator
from .core.state_manager import StateManager
from .core.state_reconciler import reconcile_restored_state
from .utils.api_health_check import APIHealthCheck
from .strategies.base_strategy import BaseStrategy
from .strategies.market_making import MarketMakingStrategy
from .strategies.spread_scalping import SpreadScalpingStrategy
from .strategies.tail_end_strategy import TailEndStrategy
from .utils.logger import setup_logger, get_trade_logger, get_error_logger
from .utils.config_loader import ConfigLoader
from .utils.profitability_tracker import ProfitabilityTracker
from .utils.market_cache import MarketCache
from .utils.telegram_notifier import TelegramNotifier


logger = setup_logger(__name__)
trade_logger = get_trade_logger()
error_logger = get_error_logger()

# Written at run() so the dashboard (dashboard/server.py) detects the bot no
# matter how it was launched (manual CLI, script or dashboard start button)
PID_FILE = Path(__file__).resolve().parent.parent / "bot.pid"


class TradingBot:
    """Main trading bot orchestrator"""
    
    def __init__(self, config_path: str = "config/config.yaml", paper_trading: Optional[bool] = None):
        """
        Initialize trading bot.
        
        Args:
            config_path: Path to configuration file
            paper_trading: Override paper trading mode (None = use config)
        """
        self.config_loader = ConfigLoader(config_path)
        self.config = self.config_loader.config
        
        # Initialize API clients
        if paper_trading is None:
            paper_trading = self.config.get('paper_trading', True)
        self.paper_trading = paper_trading
        
        ws_enabled = self.config.get('websocket', {}).get('enabled', False)
        # Get API credentials if available
        api_key = None
        private_key = None
        try:
            creds = AuthManager.get_polymarket_credentials()
            api_key = creds.get('api_key')
            private_key = creds.get('private_key')
        except Exception:
            # Credentials not required for paper trading
            pass
        
        self.polymarket_client = PolymarketAdapter(
            api_key=api_key,
            private_key=private_key,
            paper_trading=paper_trading,
            use_websocket=ws_enabled
        )
        self.polymarket_client._order_coordinator = None
        
        self.perpdex_client = None
        if AuthManager.validate_credentials('perpdex'):
            try:
                creds = AuthManager.get_perpdex_credentials()
                self.perpdex_client = PerpdexClient(
                    wallet_address=creds.get('wallet_address'),
                    private_key=creds.get('private_key'),
                    paper_trading=paper_trading
                )
                logger.info(f"Hyperliquid client initialized for wallet: {creds.get('wallet_address', 'unknown')[:10]}...")
            except Exception as e:
                logger.warning(f"Failed to initialize Hyperliquid client: {e}")
                self.perpdex_client = None
        
        # Initialize risk manager
        risk_config = self.config_loader.get_risk_config()
        self.risk_manager = RiskManager(risk_config)
        
        # Initialize order coordinator (wired into risk manager as in-flight exposure)
        self.order_coordinator = OrderCoordinator()
        self.risk_manager.order_coordinator = self.order_coordinator
        # Attach to client so strategies can use it
        if hasattr(self.polymarket_client, '_order_coordinator'):
            self.polymarket_client._order_coordinator = self.order_coordinator
        elif hasattr(self.polymarket_client, 'rest_client'):
            # Adapter pattern
            self.polymarket_client._order_coordinator = self.order_coordinator
        
        # Initialize market cache for parallel fetching
        cache_ttl = self.config.get('market_cache_ttl', 5.0)
        self.market_cache = MarketCache(self.polymarket_client, cache_ttl=cache_ttl)
        
        # Real trade prints (flow evidence for fills and candidate filtering)
        self.data_api = DataAPIClient()
        
        # Initialize strategies
        self.strategies: Dict[str, BaseStrategy] = {}
        self._initialize_strategies()
        
        # WebSocket is handled by adapter if using new pattern
        # Old client pattern handled separately above
        
        # Bot state
        self.running = False
        self.iteration_count = 0
        self.total_trades = 0
        
        # Profitability tracking
        self.profitability_tracker = ProfitabilityTracker()
        initial_capital = risk_config.get('initial_capital', 10000.0)
        self.profitability_tracker.set_initial_capital(initial_capital)
        
        # Paper fill simulator (fills pending paper orders against real prices)
        self.paper_fills = PaperFillSimulator(
            self.order_coordinator,
            self.risk_manager,
            self.profitability_tracker,
            fee_lookup=self._fee_config,
            trades_lookup=self.data_api.recent_trades
        ) if paper_trading else None
        
        # State management
        state_file = self.config.get('state_file', 'bot_state.json')
        self.state_manager = StateManager(state_file=state_file)
        
        # Try to restore state
        self._restore_state()
        
        # Run API health check on startup if enabled
        if self.config.get('run_health_check_on_startup', False):
            self._run_startup_health_check()
        
        # Initialize Telegram notifier
        telegram_config = self.config.get('telegram', {})
        telegram_token = (
            os.getenv('TELEGRAM_BOT_TOKEN') or 
            telegram_config.get('bot_token') or 
            self.config.get('telegram_bot_token')
        )
        telegram_chat_id = telegram_config.get('chat_id')
        self.telegram = TelegramNotifier(telegram_token, telegram_chat_id) if telegram_token else None
        
        # Try to detect chat_id if not set
        if self.telegram and not self.telegram.chat_id:
            self.telegram.check_for_updates()
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Initialize start time for uptime calculations
        self.start_time = time.time()
    
    def _initialize_strategies(self) -> None:
        """Initialize all trading strategies"""
        # Market-making strategy (continuous band-based order management)
        market_making_config = self.config_loader.get_strategy_config('market_making')
        if market_making_config.get('enabled', False):
            self.strategies['market_making'] = MarketMakingStrategy(
                name='market_making',
                polymarket_client=self.polymarket_client,
                risk_manager=self.risk_manager,
                config=market_making_config,
                market_cache=self.market_cache,
                flow_lookup=self.data_api.recent_trades
            )
        
        # Spread Scalping Strategy
        spread_scalping_config = self.config_loader.get_strategy_config('spread_scalping')
        if spread_scalping_config.get('enabled', False):
            self.strategies['spread_scalping'] = SpreadScalpingStrategy(
                name='spread_scalping',
                polymarket_client=self.polymarket_client,
                risk_manager=self.risk_manager,
                config=spread_scalping_config,
                market_cache=self.market_cache,
                flow_lookup=self.data_api.recent_trades
            )
        
        # Tail-End Strategy
        tail_end_config = self.config_loader.get_strategy_config('tail_end')
        if tail_end_config.get('enabled', False):
            self.strategies['tail_end'] = TailEndStrategy(
                name='tail_end',
                polymarket_client=self.polymarket_client,
                risk_manager=self.risk_manager,
                config=tail_end_config,
                market_cache=self.market_cache,
                flow_lookup=self.data_api.recent_trades
            )
        
        logger.info(f"Initialized {len(self.strategies)} strategies: {list(self.strategies.keys())}")
        trade_logger.info(f"✅ Initialized {len(self.strategies)} strategies: {', '.join(self.strategies.keys())}")
    
    def _restore_state(self) -> None:
        """Restore bot state from file"""
        try:
            state = self.state_manager.load_state()
            if state:
                positions_restored = self.state_manager.restore_positions(
                    state, self.risk_manager.position_tracker
                )
                orders_restored = self.state_manager.restore_orders(
                    state, self.order_coordinator
                )
                self.state_manager.restore_profitability(
                    state, self.risk_manager.position_tracker, self.profitability_tracker
                )
                
                if positions_restored > 0 or orders_restored > 0:
                    logger.info(
                        f"State restored: {positions_restored} positions, "
                        f"{orders_restored} orders"
                    )
                self._reconcile_restored_state()
        except Exception as e:
            logger.warning(f"Failed to restore state: {e}")
    
    def _lookup_market_raw(self, market_id: str):
        """Raw market dict for reconciliation, None when confirmed missing.

        Transient API errors propagate so the reconciler keeps the position.
        """
        import requests
        try:
            return self.polymarket_client.get_market(market_id)
        except requests.exceptions.HTTPError as e:
            if getattr(e.response, 'status_code', None) == 404:
                return None
            raise
    
    def _fee_config(self, market_id: str):
        """Fee schedule for a market (Gamma metadata), None when unknown."""
        try:
            gamma = getattr(getattr(self.polymarket_client, 'rest_client', None), 'gamma', None)
            if gamma is None:
                return None
            return gamma.get_metadata([market_id]).get(market_id)
        except Exception:
            return None
    
    def _reconcile_close(self, position_id: str, exit_price: float, reason: str) -> None:
        """Close a restored position during reconciliation (paper-aware).

        Market settlement/redemption is not a trade: no exit fee is charged.
        """
        if self.paper_fills:
            self.paper_fills.force_close(position_id, exit_price, reason, charge_exit_fee=False)
        else:
            closed = self.risk_manager.close_position(position_id, exit_price)
            if closed:
                trade_logger.info(
                    f"📝 [CLOSE] {position_id} @ {exit_price:.4f} "
                    f"P&L ${closed.pnl:.2f} ({reason})"
                )
    
    def _reconcile_restored_state(self) -> None:
        """Settle restored positions/orders whose markets resolved or vanished."""
        try:
            summary = reconcile_restored_state(
                position_tracker=self.risk_manager.position_tracker,
                order_coordinator=self.order_coordinator,
                market_lookup=self._lookup_market_raw,
                close_fn=self._reconcile_close,
                cancel_fn=self.order_coordinator.cancel_order,
            )
            if any(summary.values()):
                logger.info(
                    f"State reconciled: settled_won={summary['settled_won']} "
                    f"settled_lost={summary['settled_lost']} dropped={summary['dropped']} "
                    f"cancelled_orders={summary['cancelled']} kept={summary['kept']}"
                )
        except Exception as e:
            logger.warning(f"State reconciliation failed: {e}")
    
    def _run_startup_health_check(self) -> None:
        """Run API health check on startup"""
        try:
            logger.info("Running startup API health check...")
            health_check = APIHealthCheck(self.polymarket_client)
            results = health_check.run_full_check()
            
            if results['overall_status'] != 'healthy':
                logger.warning("API health check failed - bot may not receive market data correctly")
            else:
                logger.info("API health check passed - market data connectivity verified")
        except Exception as e:
            logger.warning(f"Error running health check: {e}")
    
    def _save_state(self) -> None:
        """Save current bot state"""
        try:
            self.state_manager.save_state(
                position_tracker=self.risk_manager.position_tracker,
                order_coordinator=self.order_coordinator,
                profitability_tracker=self.profitability_tracker,
                additional_state={
                    'iteration_count': self.iteration_count,
                    'total_trades': self.total_trades
                }
            )
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info("Received shutdown signal, stopping bot...")
        self._save_state()
        self.stop()
    
    def run_iteration(self) -> int:
        """
        Run one iteration of the trading loop.
        
        Returns:
            Number of paper orders filled this iteration
        """
        import time
        iteration_start = time.time()
        orders_placed = 0
        
        logger.debug(f"=== Iteration {self.iteration_count + 1} ===")
        
        # Run each strategy
        for strategy_name, strategy in self.strategies.items():
            if not strategy.is_enabled():
                logger.debug(f"Strategy {strategy_name} is disabled, skipping")
                continue
            
            try:
                strategy_start = time.time()
                logger.debug(f"Running strategy: {strategy_name}")
                
                placed = strategy.run()
                orders_placed += len(placed)
                
                strategy_time = time.time() - strategy_start
                
                if placed:
                    trade_logger.info("")
                    trade_logger.info("🔥" * 40)
                    trade_logger.info(f"✅ Strategy {strategy_name} PLACED {len(placed)} ORDER(S) in {strategy_time:.2f}s")
                    trade_logger.info("🔥" * 40)
                    trade_logger.info("")
                    
                    # Send Telegram notification for each placed order
                    if self.telegram:
                        for trade in placed:
                            market_id = trade.get('market_id', 'unknown')
                            details = {}
                            
                            # Extract trade details based on strategy
                            if 'expected_profit_pct' in trade:
                                details['profit_pct'] = trade['expected_profit_pct']
                            if 'buy_price' in trade and 'sell_price' in trade:
                                details['buy_price'] = trade['buy_price']
                                details['sell_price'] = trade['sell_price']
                            if 'profit_cents' in trade:
                                details['profit_cents'] = trade['profit_cents']
                            if 'effective_cost' in trade:
                                details['effective_cost'] = trade['effective_cost']
                            if 'potential_profit' in trade:
                                details['potential_profit'] = trade['potential_profit']
                            
                            self.telegram.trade_executed(strategy_name, market_id, details)
                else:
                    logger.debug(f"Strategy {strategy_name} placed no orders (took {strategy_time:.2f}s)")
            
            except Exception as e:
                error_logger.error(f"Error running strategy {strategy_name}: {e}", exc_info=True)
        
        # Fill pending paper orders against real market prices
        fills_executed = self._process_paper_fills() if self.paper_fills else []
        self.total_trades += len(fills_executed)
        
        # Check stop losses
        self._check_stop_losses()

        # Periodic status update to Telegram every 5 iterations
        if self.iteration_count % 5 == 0:
            try:
                self._send_status_update(self.start_time)
            except Exception as e:
                logger.error(f"Failed to send periodic status update: {e}", exc_info=True)

        # Log risk metrics periodically (file only, not terminal)
        if self.iteration_count % 10 == 0:
            self._log_risk_metrics()
        
        iteration_time = time.time() - iteration_start
        if orders_placed > 0 or fills_executed:
            trade_logger.info(
                f"📊 Iteration {self.iteration_count + 1}: "
                f"{len(fills_executed)} fill(s), {orders_placed} order(s) placed "
                f"in {iteration_time:.2f}s"
            )
        
        logger.debug(
            f"Iteration {self.iteration_count + 1} completed: "
            f"{len(fills_executed)} fills, {orders_placed} orders placed"
        )
        
        self.iteration_count += 1
        return len(fills_executed)
    
    def _process_paper_fills(self) -> list:
        """Fill pending paper orders against current real prices"""
        if not self.paper_fills:
            return []
        
        def price_lookup(market_id: str, outcome: str):
            return self.polymarket_client.get_best_price(market_id, outcome=outcome)
        
        try:
            fills = self.paper_fills.process_fills(price_lookup)
            if fills:
                trade_logger.info(f"📝 {len(fills)} paper order(s) filled this iteration")
            return fills
        except Exception as e:
            error_logger.error(f"Error processing paper fills: {e}", exc_info=True)
            return []
    
    def _check_stop_losses(self) -> None:
        """Check all positions for stop loss triggers"""
        positions = list(self.risk_manager.position_tracker.positions.items())
        if not positions:
            return
        
        # Batch price lookup: one request for all positions instead of N
        pairs = [(p.market_id, getattr(p, 'outcome', 'YES')) for _, p in positions]
        if hasattr(self.polymarket_client, 'get_best_prices_batch'):
            prices_map = self.polymarket_client.get_best_prices_batch(pairs)
        else:
            prices_map = {}
        
        for position_id, position in positions:
            try:
                # Get current price for the position's actual outcome
                outcome = getattr(position, 'outcome', 'YES')
                prices = prices_map.get(f"{position.market_id}_{outcome}")
                if prices is None:
                    prices = self.polymarket_client.get_best_price(position.market_id, outcome=outcome)
                current_price = (prices or {}).get('bid') or (prices or {}).get('ask')
                
                if current_price and self.risk_manager.check_stop_loss(position_id, current_price):
                    # Close position for real (fills book + P&L + trade record)
                    if self.paper_fills:
                        closed_position = self.paper_fills.force_close(
                            position_id, current_price, reason="stop-loss"
                        )
                    else:
                        closed_position = self.risk_manager.close_position(position_id, current_price)
                    logger.warning(f"Stop loss triggered for position {position_id}")
                    
                    # Send Telegram notification
                    if self.telegram and closed_position:
                        profit = closed_position.pnl if hasattr(closed_position, 'pnl') else 0.0
                        self.telegram.trade_completed(position.strategy, position.market_id, profit)
            
            except Exception as e:
                logger.debug(f"Error checking stop loss for {position_id}: {e}")
    
    def _send_status_update(self, start_time: float) -> None:
        """Send periodic status update via Telegram"""
        if not self.telegram or not self.telegram.chat_id:
            return
        
        try:
            # Calculate uptime
            uptime_seconds = time.time() - start_time
            uptime_minutes = uptime_seconds / 60

            # Get risk metrics for P&L
            metrics = self.risk_manager.get_risk_metrics()
            
            # Get WebSocket status
            ws_status = "❌ Disconnected"
            if hasattr(self.polymarket_client, 'ws_client') and self.polymarket_client.ws_client:
                if self.polymarket_client.ws_client.is_connected():
                    ws_status = "✅ Connected"
                else:
                    ws_status = "❌ Disconnected"
            
            # Prepare status stats
            stats = {
                'uptime_minutes': uptime_minutes,
                'total_trades': self.total_trades,
                'total_pnl': metrics.get('total_pnl', 0.0),
                'active_strategies': list(self.strategies.keys()),
                'websocket_status': ws_status,
                'iteration_count': self.iteration_count
            }
            
            # Send status update
            self.telegram.send_status_update(stats)
            logger.info(f"Sent status update: {uptime_minutes:.0f} min uptime, {self.total_trades} trades")
        
        except Exception as e:
            logger.error(f"Error sending status update: {e}", exc_info=True)
    
    def _log_risk_metrics(self) -> None:
        """Log current risk metrics and profitability (file only, not terminal)"""
        metrics = self.risk_manager.get_risk_metrics()
        profit_stats = self.profitability_tracker.get_overall_stats()
        
        # Log to file only (not terminal)
        logger.debug(
            f"Risk Metrics | Exposure: ${metrics['total_exposure']:.2f} | "
            f"P&L: ${metrics['total_pnl']:.2f} | "
            f"Capital: ${metrics['current_capital']:.2f} | "
            f"Drawdown: {metrics['drawdown_pct']:.2f}% | "
            f"Open Positions: {metrics['open_positions']}"
        )
        
        if profit_stats['total_trades'] > 0:
            logger.debug(
                f"Profitability | Trades: {profit_stats['total_trades']} | "
                f"Win Rate: {profit_stats['win_rate']:.1f}% | "
                f"Total P&L: ${profit_stats['total_pnl']:.2f} | "
                f"ROI: {profit_stats['roi']:.2f}%"
            )
    
    def run(self) -> None:
        """Run the trading bot main loop"""
        logger.info("Starting trading bot...")
        trade_logger.info("🤖 Bot is running...")
        
        self.running = True
        self._write_pid_file()
        
        # Send Telegram notification (after running is set and chat_id is detected)
        if self.telegram:
            # Try to detect chat_id one more time if not set
            if not self.telegram.chat_id:
                self.telegram.check_for_updates()
            
            if self.telegram.chat_id:
                strategy_names = list(self.strategies.keys())
                self.telegram.bot_started(strategy_names)
            else:
                trade_logger.info("⚠️  Telegram chat ID not detected. Send a message to your bot to enable notifications.")
        
        # Get polling interval from config
        polling_interval = self.config.get('polling_interval', 5.0)  # Default 5 seconds
        
        # Status update interval (15 minutes)
        status_update_interval = 15 * 60  # 15 minutes in seconds
        last_status_update = time.time()
        start_time = time.time()
        
        try:
            while self.running:
                iteration_start = time.time()
                
                # Run trading iteration
                self.run_iteration()
                
                # Check if it's time for status update (every 30 minutes)
                current_time = time.time()
                time_since_last_status = current_time - last_status_update
                
                if time_since_last_status >= status_update_interval:
                    self._send_status_update(start_time)
                    last_status_update = current_time
                
                # Periodic state checkpoint (crash/corte de luz no pierde posiciones)
                if self.iteration_count % 10 == 0:
                    try:
                        self._save_state()
                    except Exception as e:
                        logger.error(f"Periodic state save failed: {e}")
                
                # Periodically check for Telegram chat ID if not set
                if self.telegram and not self.telegram.chat_id and self.iteration_count % 12 == 0: # Every ~1 minute (assuming 5s interval)
                    chat_id = self.telegram.check_for_updates()
                    if chat_id:
                        trade_logger.info(f"✅ Telegram chat ID detected: {chat_id}")
                        strategy_names = list(self.strategies.keys())
                        self.telegram.bot_started(strategy_names)
                
                # Sleep until next iteration
                elapsed = time.time() - iteration_start
                sleep_time = max(0, polling_interval - elapsed)
                
                if sleep_time > 0:
                    logger.debug(f"Sleeping for {sleep_time:.2f}s until next iteration")
                    time.sleep(sleep_time)
                else:
                    error_logger.warning(f"Iteration took {elapsed:.2f}s, longer than polling interval of {polling_interval}s")
        
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        finally:
            self._save_state()
            self.stop()
    
    def stop(self) -> None:
        """Stop the trading bot"""
        if not self.running:
            return
        
        logger.info("Stopping trading bot...")
        self.running = False
        
        # Log final statistics
        metrics = self.risk_manager.get_risk_metrics()
        profit_stats = self.profitability_tracker.get_overall_stats()
        
        logger.info(
            f"Bot stopped | Total iterations: {self.iteration_count} | "
            f"Total fills: {self.total_trades} | "
            f"Final P&L: ${metrics['total_pnl']:.2f}"
        )
        
        # Send Telegram notification
        if self.telegram:
            self.telegram.bot_stopped(self.total_trades, metrics['total_pnl'])
        
        # Print profitability summary
        if profit_stats['total_trades'] > 0:
            logger.info(self.profitability_tracker.get_performance_summary())
        
        # Save final state
        self._save_state()
        self._remove_pid_file()
    
    def _write_pid_file(self) -> None:
        """Publish our PID so the dashboard can detect externally started bots.

        Refuses to start when another live bot already owns the PID file: two
        bots sharing bot_state.json silently corrupt each other's history.
        """
        try:
            if PID_FILE.exists():
                existing = PID_FILE.read_text().strip()
                if existing.isdigit() and int(existing) != os.getpid():
                    try:
                        os.kill(int(existing), 0)
                    except OSError:
                        pass  # stale file, safe to take over
                    else:
                        raise RuntimeError(
                            f"Otro bot ya está corriendo (PID {existing}). "
                            f"Detenlo primero (scripts/service.sh stop) o borra {PID_FILE}."
                        )
            PID_FILE.write_text(str(os.getpid()))
        except RuntimeError:
            raise
        except OSError as e:
            logger.warning(f"Could not write PID file {PID_FILE}: {e}")
    
    def _remove_pid_file(self) -> None:
        """Remove our PID file unless another process owns it now."""
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError as e:
            logger.warning(f"Could not remove PID file {PID_FILE}: {e}")
    
    def get_status(self) -> Dict:
        """
        Get current bot status.
        
        Returns:
            Status dictionary
        """
        metrics = self.risk_manager.get_risk_metrics()
        
        return {
            'running': self.running,
            'iteration_count': self.iteration_count,
            'total_trades': self.total_trades,
            'strategies': {
                name: {'enabled': strategy.is_enabled()} 
                for name, strategy in self.strategies.items()
            },
            'risk_metrics': metrics
        }

