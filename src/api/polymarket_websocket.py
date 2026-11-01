"""WebSocket client for real-time Polymarket data"""

import json
import time
import threading
import websocket
from typing import Dict, List, Optional, Callable
from ..utils.logger import setup_logger


logger = setup_logger(__name__)


class PolymarketWebSocketClient:
    """WebSocket client for real-time Polymarket orderbook updates"""
    
    # Polymarket WebSocket endpoint from official documentation
    # https://docs.polymarket.com/developers/CLOB/endpoints
    # Error message indicates channels are '/ws/user' and '/ws/market'
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    def __init__(self, api_key: Optional[str] = None, private_key: Optional[str] = None):
        """
        Initialize WebSocket client.
        
        Args:
            api_key: Polymarket API key (optional, required for USER channel)
            private_key: Polymarket private key (optional, required for USER channel)
        """
        self.api_key = api_key
        self.private_key = private_key
        self.ws = None
        self.connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.reconnect_delay = 5.0
        
        # Subscriptions - track asset_ids for MARKET channel
        self.subscriptions: Dict[str, set] = {}  # market_id -> set of outcomes
        self.asset_id_map: Dict[str, tuple] = {}  # asset_id -> (market_id, outcome)
        self.orderbook_cache: Dict[str, Dict] = {}  # (market_id, outcome) -> orderbook
        self.pending_subscriptions: List[tuple] = []  # List of (market_id, outcome) to subscribe
        self.rest_client = None  # Will be set by adapter to fetch market data
        
        # Callbacks
        self.on_orderbook_update: Optional[Callable] = None
        self.on_error: Optional[Callable] = None
        self.on_connect: Optional[Callable] = None
        
        # Threading
        self.lock = threading.Lock()
        self.ws_thread = None
        self.running = False
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages from Polymarket MARKET channel
        
        According to Polymarket docs: https://docs.polymarket.com/developers/CLOB/websocket/market-channel
        Messages have event_type: "book", "price_change", "tick_size_change", "last_trade_price"
        """
        try:
            if isinstance(message, bytes):
                message = message.decode('utf-8', errors='ignore')
            if not message or not str(message).strip():
                return  # keepalive / empty frame
            
            data = json.loads(message)
            
            # MARKET channel delivers batches as JSON arrays of events
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        self._dispatch_event(item)
                return
            
            if isinstance(data, dict):
                self._dispatch_event(data)
            
        except json.JSONDecodeError as e:
            logger.debug(f"Non-JSON WebSocket frame ignored: {e}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")
    
    def _dispatch_event(self, data: Dict) -> None:
        """Route a single MARKET-channel event"""
        if not isinstance(data, dict):
            return
        
        # Handle different event types from MARKET channel
        event_type = data.get('event_type', '').lower()
        
        if event_type == 'book':
            # Book message - full orderbook snapshot
            self._handle_book_message(data)
        elif event_type == 'price_change':
            # Price change message - incremental updates
            self._handle_price_change_message(data)
        elif event_type == 'tick_size_change':
            # Tick size change - market tick size updated
            logger.debug(f"Tick size change: {data.get('market')}")
        elif event_type == 'last_trade_price':
            # Last trade - trade executed
            logger.debug(f"Last trade: {data.get('market')} @ {data.get('price')}")
        else:
            # Unknown message type - log for debugging
            logger.debug(f"Unknown WebSocket message type: {event_type}")
    
    def _handle_book_message(self, data: Dict) -> None:
        """
        Handle 'book' event_type message from MARKET channel.
        
        Structure according to docs:
        - event_type: "book"
        - asset_id: token ID
        - market: condition ID
        - bids: OrderSummary[] with {price, size}
        - asks: OrderSummary[] with {price, size}
        """
        try:
            asset_id = data.get('asset_id')
            market_id = data.get('market')
            
            if not asset_id or not market_id:
                logger.debug(f"Book message missing asset_id or market: {data}")
                return
            
            # Convert Polymarket format to our format
            # Polymarket uses "bids"/"asks" with {price, size} objects
            bids_raw = data.get('bids', [])
            asks_raw = data.get('asks', [])
            
            # Convert to format expected by our code
            # Each bid/ask is {price: string, size: string}
            bids = [{'price': float(b.get('price', 0)), 'size': float(b.get('size', 0))} for b in bids_raw]
            asks = [{'price': float(a.get('price', 0)), 'size': float(a.get('size', 0))} for a in asks_raw]
            
            # Resolve outcome from the asset map or the REST token index.
            # Never assume YES: a NO book cached as YES corrupts prices.
            outcome = None
            if asset_id in self.asset_id_map:
                market_id, outcome = self.asset_id_map[asset_id]
            else:
                outcome = self._resolve_outcome(market_id, asset_id)
                if not outcome:
                    logger.debug(f"Unknown asset_id {str(asset_id)[:20]}... - skipping book (no outcome)")
                    return
                self.asset_id_map[asset_id] = (market_id, outcome)
            
            key = (market_id, outcome)
            
            with self.lock:
                self.orderbook_cache[key] = {
                    'bids': bids,
                    'asks': asks,
                    'timestamp': time.time()
                }
            
            # Call callback if set
            if self.on_orderbook_update:
                try:
                    self.on_orderbook_update(market_id, outcome, bids, asks)
                except Exception as e:
                    logger.error(f"Error in orderbook update callback: {e}")
        
        except Exception as e:
            logger.error(f"Error handling book message: {e}")
    
    def _handle_price_change_message(self, data: Dict) -> None:
        """
        Handle 'price_change' event_type message from MARKET channel.
        
        Real payload:
        {
          "event_type": "price_change",
          "market": "<condition_id>",
          "price_changes": [
            {"asset_id": "...", "price": "0.22", "size": "384", "side": "SELL",
             "best_bid": "0.019", "best_ask": "0.02", ...}, ...
          ]
        }
        
        Each change updates one price level of one asset's book. We apply the
        level update to the cached orderbook and refresh best bid/ask.
        """
        try:
            market_id = data.get('market')
            price_changes = data.get('price_changes', [])
            
            if not market_id or not price_changes:
                return
            
            for change in price_changes:
                if not isinstance(change, dict):
                    continue
                
                asset_id = change.get('asset_id')
                if not asset_id:
                    continue
                
                if asset_id in self.asset_id_map:
                    _, outcome = self.asset_id_map[asset_id]
                else:
                    outcome = self._resolve_outcome(market_id, asset_id)
                    if not outcome:
                        continue
                    self.asset_id_map[asset_id] = (market_id, outcome)
                
                key = (market_id, outcome)
                price = float(change.get('price', 0) or 0)
                size = float(change.get('size', 0) or 0)
                side = str(change.get('side', '')).upper()
                
                if price <= 0:
                    continue
                
                with self.lock:
                    book = self.orderbook_cache.get(key)
                    if book is None:
                        continue  # no snapshot yet; wait for 'book' event
                    
                    levels = book.get('bids') if side == 'BUY' else book.get('asks') if side == 'SELL' else None
                    if levels is None:
                        continue
                    
                    # Update or remove the level at this price (size 0 = remove)
                    updated = False
                    for i, level in enumerate(levels):
                        if abs(float(level.get('price', 0)) - price) < 1e-9:
                            if size > 0:
                                levels[i]['size'] = size
                            else:
                                levels.pop(i)
                            updated = True
                            break
                    if not updated and size > 0:
                        levels.append({'price': price, 'size': size})
                    
                    book['timestamp'] = time.time()
        
        except Exception as e:
            logger.error(f"Error handling price change message: {e}")
    
    def _resolve_outcome(self, market_id: str, asset_id: str) -> Optional[str]:
        """Map an asset_id (token) to YES/NO using the REST client's token index"""
        index = getattr(self.rest_client, '_token_index', None) if self.rest_client else None
        if not index:
            return None
        for outcome, token_id in (index.get(market_id) or {}).items():
            if str(token_id) == str(asset_id):
                return outcome
        # Fall back to a direct market fetch via the REST client
        if self.rest_client:
            try:
                resolved = self.rest_client.resolve_token_id(market_id, 'YES')
                if resolved == str(asset_id):
                    return 'YES'
                resolved = self.rest_client.resolve_token_id(market_id, 'NO')
                if resolved == str(asset_id):
                    return 'NO'
            except Exception:
                pass
        return None
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors"""
        logger.error(f"WebSocket error: {error}")
        self.connected = False
        
        if self.on_error:
            try:
                self.on_error(error)
            except Exception as e:
                logger.error(f"Error in error callback: {e}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close"""
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
        self.connected = False
        
        # Attempt reconnection if running
        if self.running:
            self._reconnect()
    
    def _on_open(self, ws):
        """
        Handle WebSocket open - send initial subscription for MARKET channel.
        
        According to Polymarket docs and poly-websockets implementation:
        - Connect to /ws/market endpoint
        - Subscribe with {"type": "MARKET", "asset_ids": [...]}
        - Only subscribe when we have actual asset_ids (not empty array)
        """
        logger.info("WebSocket connected")
        self.connected = True
        self.reconnect_attempts = 0
        
        # Don't subscribe with empty array - wait until we have asset_ids
        # This prevents the server from closing the connection
        # We'll subscribe when we have actual asset_ids to track
        
        # Process pending subscriptions that now have asset_ids resolved
        if self.rest_client:
            self._process_pending_subscriptions()
            # Also resubscribe to existing subscriptions
            self._resubscribe_all(self.rest_client)
        
        if self.on_connect:
            try:
                self.on_connect()
            except Exception as e:
                logger.error(f"Error in connect callback: {e}")
    
    def _reconnect(self) -> None:
        """Attempt to reconnect WebSocket"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")
            self.running = False
            return
        
        self.reconnect_attempts += 1
        wait_time = self.reconnect_delay * self.reconnect_attempts
        
        logger.info(f"Reconnecting in {wait_time:.1f}s (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})")
        time.sleep(wait_time)
        
        try:
            self.connect()
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            if self.running:
                self._reconnect()
    
    def _process_pending_subscriptions(self) -> None:
        """Process pending subscriptions that need asset_id resolution"""
        if not self.rest_client:
            return
        
        pending = []
        with self.lock:
            pending = list(self.pending_subscriptions)
            self.pending_subscriptions.clear()
        
        for market_id, outcome in pending:
            self._subscribe(market_id, outcome, self.rest_client)
    
    def _resubscribe_all(self, rest_client=None) -> None:
        """Re-subscribe to all previous subscriptions"""
        with self.lock:
            subscriptions_copy = dict(self.subscriptions)
        
        for market_id, outcomes in subscriptions_copy.items():
            for outcome in outcomes:
                self._subscribe(market_id, outcome, rest_client)
    
    def _get_asset_id_from_market(self, market_id: str, outcome: str, rest_client=None) -> Optional[str]:
        """
        Get asset_id (token ID) from market data.
        
        Args:
            market_id: Market identifier
            outcome: Outcome (YES, NO, or token ID)
            rest_client: REST client to fetch market data (optional)
            
        Returns:
            Asset ID (token ID) or None if not found
        """
        # If outcome is already a token ID (long numeric string), use it
        if outcome and len(outcome) > 20 and outcome.replace('.', '').isdigit():
            return outcome
        
        # Resolve via REST client's token index (markets carry tokens[] with token_id)
        if rest_client is not None:
            try:
                if hasattr(rest_client, 'resolve_token_id'):
                    return rest_client.resolve_token_id(market_id, outcome)
                if hasattr(rest_client, 'rest_client') and hasattr(rest_client.rest_client, 'resolve_token_id'):
                    return rest_client.rest_client.resolve_token_id(market_id, outcome)
            except Exception as e:
                logger.debug(f"Error resolving token for {market_id}: {e}")
        
        return None
    
    def _update_subscription(self, asset_ids: List[str]) -> None:
        """
        Update WebSocket subscription with new asset_ids.
        Based on poly-websockets approach - send updated subscription message.
        
        Args:
            asset_ids: List of asset IDs to subscribe to
        """
        if not self.connected or not self.ws:
            return
        
        try:
            subscribe_msg = {
                "type": "MARKET",
                "assets_ids": asset_ids
            }
            
            self.ws.send(json.dumps(subscribe_msg))
            logger.debug(f"Updated MARKET channel subscription with {len(asset_ids)} assets_ids")
        except Exception as e:
            logger.error(f"Failed to update subscription: {e}")
    
    def _subscribe(self, market_id: str, outcome: str = "YES", rest_client=None) -> None:
        """
        Subscribe to orderbook updates for a market.
        Based on poly-websockets approach - requires actual asset_ids.
        
        Args:
            market_id: Market identifier
            outcome: Outcome type (YES, NO, etc.)
            rest_client: REST client to fetch market data (optional)
        """
        if not self.connected or not self.ws:
            # Queue for later
            self.pending_subscriptions.append((market_id, outcome))
            return
        
        try:
            # Get asset_id from market data
            asset_id = self._get_asset_id_from_market(market_id, outcome, rest_client)
            
            if not asset_id:
                # Can't subscribe without asset_id - queue for later
                logger.debug(f"Cannot subscribe to {market_id} {outcome} - asset_id not found, using REST fallback")
                self.pending_subscriptions.append((market_id, outcome))
                return
            
            # Store mapping
            with self.lock:
                self.asset_id_map[asset_id] = (market_id, outcome)
                if market_id not in self.subscriptions:
                    self.subscriptions[market_id] = set()
                self.subscriptions[market_id].add(outcome)
            
            # Get all current asset_ids we're tracking
            current_asset_ids = list(self.asset_id_map.keys())
            
            # Update subscription with all asset_ids
            self._update_subscription(current_asset_ids)
            
            logger.debug(f"Subscribed to {market_id} {outcome} (asset_id: {asset_id[:20]}...)")
        
        except Exception as e:
            logger.error(f"Failed to subscribe to {market_id} {outcome}: {e}")
    
    def connect(self) -> bool:
        """
        Connect to WebSocket.
        
        Returns:
            True if connected successfully
        """
        if self.connected:
            return True
        
        try:
            # Create WebSocket connection
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            
            self.ws = websocket.WebSocketApp(
                self.WS_URL,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
                header=headers
            )
            
            # Start WebSocket in a separate thread
            self.running = True
            self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
            self.ws_thread.start()
            
            # Wait for connection
            timeout = 10.0
            start_time = time.time()
            while not self.connected and (time.time() - start_time) < timeout:
                time.sleep(0.1)
            
            return self.connected
        
        except Exception as e:
            logger.error(f"Failed to connect WebSocket: {e}")
            self.connected = False
            return False
    
    def subscribe_orderbook(self, market_id: str, outcome: str = "YES", rest_client=None) -> None:
        """
        Subscribe to orderbook updates for a market.
        
        Args:
            market_id: Market identifier
            outcome: Outcome type (YES, NO, etc.)
            rest_client: REST client to fetch market data (optional, needed to get asset_id)
        """
        key = (market_id, outcome)
        
        with self.lock:
            if market_id not in self.subscriptions:
                self.subscriptions[market_id] = set()
            self.subscriptions[market_id].add(outcome)
        
        if self.connected:
            self._subscribe(market_id, outcome, rest_client)
        else:
            # Will subscribe on connect
            self.pending_subscriptions.append((market_id, outcome))
            logger.debug(f"Queued subscription for {market_id} {outcome}")
    
    def unsubscribe_orderbook(self, market_id: str, outcome: str = "YES") -> None:
        """
        Unsubscribe from orderbook updates.
        
        Args:
            market_id: Market identifier
            outcome: Outcome type
        """
        with self.lock:
            if market_id in self.subscriptions:
                self.subscriptions[market_id].discard(outcome)
                if not self.subscriptions[market_id]:
                    del self.subscriptions[market_id]
            
            key = (market_id, outcome)
            if key in self.orderbook_cache:
                del self.orderbook_cache[key]
        
        if self.connected and self.ws:
            try:
                # Protocol has no dedicated unsubscribe; resubscribe without the asset
                self._update_subscription(list(self.asset_id_map.keys()))
            except Exception as e:
                logger.error(f"Failed to unsubscribe: {e}")
    
    def get_orderbook(self, market_id: str, outcome: str = "YES") -> Optional[Dict]:
        """
        Get cached orderbook data.
        
        Args:
            market_id: Market identifier
            outcome: Outcome type
            
        Returns:
            Orderbook dict or None if not available
        """
        key = (market_id, outcome)
        
        with self.lock:
            return self.orderbook_cache.get(key)
    
    def disconnect(self) -> None:
        """Disconnect WebSocket"""
        self.running = False
        
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        
        self.connected = False
        logger.info("WebSocket disconnected")
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self.connected

