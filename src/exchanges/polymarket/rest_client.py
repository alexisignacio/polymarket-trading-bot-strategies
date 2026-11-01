"""REST API client for Polymarket"""

import time
import requests
from typing import Dict, List, Optional
from ...api.auth import AuthManager
from ...api.gamma_client import GammaClient
from ...api.rate_limiter import RateLimiter
from ...utils.logger import setup_logger
from ...utils.market_data_validator import MarketDataValidator
from .normalizer import normalize_market, best_bid_ask


logger = setup_logger(__name__)


class PolymarketRESTClient:
    """REST API client for Polymarket"""
    
    BASE_URL = "https://clob.polymarket.com"
    
    def __init__(self, api_key: Optional[str] = None, private_key: Optional[str] = None, paper_trading: bool = False):
        """
        Initialize REST client.
        
        Args:
            api_key: API key (optional)
            private_key: Private key (optional)
            paper_trading: Paper trading mode
        """
        if api_key and private_key:
            self.api_key = api_key
            self.private_key = private_key
        else:
            try:
                creds = AuthManager.get_polymarket_credentials()
                self.api_key = creds['api_key']
                self.private_key = creds['private_key']
            except ValueError:
                if not paper_trading:
                    raise
                self.api_key = ""
                self.private_key = ""
        
        self.paper_trading = paper_trading
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        })
        
        # Rate limiter
        self.rate_limiter = RateLimiter()
        
        # Validator
        self.validator = MarketDataValidator()
        self.verbose_validation = False
        
        # Token resolution index: market_id -> {'YES': token_id, 'NO': token_id}
        self._token_index: Dict[str, Dict[str, str]] = {}
        
        # Metadata (volume/liquidity/created_at) from Gamma - CLOB lacks these
        self.gamma = GammaClient()
    
    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make API request with rate limiting"""
        url = f"{self.BASE_URL}{endpoint}"
        
        # Log API call
        logger.debug(f"API Call: {method} {endpoint}")
        
        # Wait if rate limit is active
        self.rate_limiter.wait_if_needed(endpoint)
        
        try:
            kwargs.setdefault('timeout', 15)
            response = self.session.request(method, url, **kwargs)
            logger.debug(f"API Response: {method} {endpoint} -> {response.status_code}")
            
            # Handle rate limit errors (429)
            if response.status_code == 429:
                self.rate_limiter.handle_rate_limit_error(endpoint)
                self.rate_limiter.wait_if_needed(endpoint)
                kwargs.setdefault('timeout', 15)
            response = self.session.request(method, url, **kwargs)
            
            response.raise_for_status()
            self.rate_limiter.record_call(endpoint)
            
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                self.rate_limiter.handle_rate_limit_error(endpoint)
                logger.warning(f"Rate limit error for {endpoint}, will retry after backoff")
            logger.error(f"API request failed: {method} {endpoint} - {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {method} {endpoint} - {e}")
            raise
    
    def get_markets(self, active: bool = True, limit: int = 100, next_cursor: str = "") -> Dict:
        """Get list of markets with pagination support.
        
        Uses /sampling-markets for active scans: /markets ignores its filters and
        mostly returns historical/closed markets, while /sampling-markets returns
        live markets with enabled order books (what strategies can actually trade).
        
        Args:
            active: Filter for active markets
            limit: Number of markets per page (max 1000)
            next_cursor: Cursor for pagination (empty string for first page)
            
        Returns:
            Dict with 'markets' list (canonical schema) and 'next_cursor' for pagination
        """
        endpoint = "/sampling-markets" if active else "/markets"
        params = {'limit': limit}
        if next_cursor:
            params['next_cursor'] = next_cursor
        
        try:
            response = self._request('GET', endpoint, params=params)
            
            if self.verbose_validation:
                self.validator.log_response_sample(response, endpoint)
            
            is_valid, error_msg, markets = self.validator.validate_markets_response(response)
            
            if not is_valid:
                logger.error(f"Invalid markets response: {error_msg}")
                return {'markets': [], 'next_cursor': None}
            
            # Extract next_cursor from response (API returns it in response)
            next_cursor_value = response.get('next_cursor', None)
            
            normalized = []
            for raw in markets or []:
                market = normalize_market(raw)
                if market.get('market_id'):
                    self._token_index[market['market_id']] = {
                        t['outcome']: t['token_id'] for t in market.get('tokens', [])
                    }
                    normalized.append(market)
            
            # /sampling-markets ignores 'limit' and returns pages of 1000
            if limit and len(normalized) > limit:
                normalized = normalized[:limit]
            
            # Enrich with Gamma metadata (volume/liquidity/created_at) when available
            if normalized:
                try:
                    meta = self.gamma.get_metadata(
                        [m['market_id'] for m in normalized]
                    )
                    for market in normalized:
                        info = meta.get(market['market_id'])
                        if info:
                            market['volume'] = info['volume']
                            market['liquidity'] = info['liquidity']
                            market['created_at'] = info['created_at']
                except Exception as e:
                    logger.debug(f"Gamma enrichment skipped: {e}")
            
            return {
                'markets': normalized,
                'next_cursor': next_cursor_value if next_cursor_value != 'LTE=' else None
            }
        except Exception as e:
            logger.error(f"Error fetching markets: {e}", exc_info=True)
            return {'markets': [], 'next_cursor': None}
    
    def get_all_markets(self, active: bool = True, max_markets: int = None) -> List[Dict]:
        """Get all markets using pagination
        
        Args:
            active: Filter for active markets
            max_markets: Maximum number of markets to fetch (None = all)
            
        Returns:
            List of all markets
        """
        all_markets = []
        next_cursor = ""
        page = 1
        
        while True:
            logger.info(f"Fetching markets page {page}...")
            result = self.get_markets(active=active, limit=1000, next_cursor=next_cursor)
            
            markets = result['markets']
            all_markets.extend(markets)
            
            logger.info(f"Page {page}: Got {len(markets)} markets (total: {len(all_markets)})")
            
            # Check if we've reached max_markets
            if max_markets and len(all_markets) >= max_markets:
                all_markets = all_markets[:max_markets]
                logger.info(f"Reached max_markets limit: {max_markets}")
                break
            
            # Check if there are more pages
            next_cursor = result['next_cursor']
            if not next_cursor:
                logger.info(f"No more pages. Total markets: {len(all_markets)}")
                break
            
            page += 1
        
        return all_markets
    
    def resolve_token_id(self, market_or_token_id: str, outcome: str = "YES") -> Optional[str]:
        """Resolve a market/condition id + outcome to a token_id.
        
        Accepts either an already-resolved token_id (long numeric string) or a
        market/condition id (0x... or arbitrary), in which case the token is
        looked up in the index built by get_markets, falling back to a direct
        /markets/{condition_id} fetch.
        """
        if not market_or_token_id:
            return None
        
        # Already a token_id (CLOB token ids are long numeric strings)
        if market_or_token_id.isdigit() and len(market_or_token_id) > 20:
            return market_or_token_id
        
        outcome_key = (outcome or "YES").upper()
        if outcome_key not in ("YES", "NO"):
            outcome_key = "YES"
        
        indexed = self._token_index.get(market_or_token_id)
        if not indexed:
            try:
                raw = self._request('GET', f"/markets/{market_or_token_id}")
                market = normalize_market(raw if isinstance(raw, dict) else {})
                indexed = {t['outcome']: t['token_id'] for t in market.get('tokens', [])}
                if indexed:
                    self._token_index[market_or_token_id] = indexed
            except Exception as e:
                logger.debug(f"Token resolution failed for {market_or_token_id}: {e}")
                return None
        
        return indexed.get(outcome_key)
    
    def get_orderbook(self, token_id: str, outcome: str = "YES") -> Dict:
        """Get orderbook for a token or market
        
        Args:
            token_id: Token ID, or market/condition id (resolved via outcome)
            outcome: Outcome name used to resolve the token when needed
        """
        resolved = self.resolve_token_id(token_id, outcome)
        if not resolved:
            logger.debug(f"Could not resolve token for {token_id} {outcome}")
            return {'bids': [], 'asks': []}
        endpoint = "/book"
        params = {'token_id': resolved}
        
        try:
            response = self._request('GET', endpoint, params=params)
            
            if self.verbose_validation:
                self.validator.log_response_sample(response, f"{endpoint}?token_id={token_id}")
            
            is_valid, error_msg, orderbook = self.validator.validate_orderbook_response(response)
            
            if not is_valid:
                logger.error(f"Invalid orderbook response: {error_msg}")
                return {'bids': [], 'asks': []}
            
            return orderbook or {'bids': [], 'asks': []}
        except Exception as e:
            logger.error(f"Error getting orderbook: {e}", exc_info=True)
            return {'bids': [], 'asks': []}
    
    def get_orderbooks(self, token_ids: List[str]) -> List[Dict]:
        """Batch-fetch orderbooks for many tokens in one request (POST /books).
        
        Returns the raw book list (each with 'asset_id', 'bids', 'asks'); empty
        list on failure so callers can fall back to per-token fetches.
        """
        if not token_ids:
            return []
        payload = [{'token_id': tid} for tid in token_ids]
        try:
            response = self._request('POST', '/books', json=payload)
            return response if isinstance(response, list) else []
        except Exception as e:
            logger.debug(f"Batch orderbook fetch failed: {e}")
            return []
    
    def place_order(self, market_id: str, outcome: str, side: str, size: float, price: float) -> Dict:
        """Place order.
        
        Args:
            size: Order size in USD notional (converted to shares for the wire)
            price: Limit price
        """
        shares = size / price if price > 0 else 0.0
        if self.paper_trading:
            order_id = f'paper_{market_id}_{side}_{int(time.time() * 1000)}'
            return {
                'order_id': order_id,
                'status': 'pending',
                'size': size,
                'shares': shares,
                'price': price,
                'paper_trading': True
            }
        
        endpoint = "/orders"
        data = {
            'market': market_id,
            'outcome': outcome,
            'side': side,
            'size': str(shares),
            'price': str(price)
        }
        
        return self._request('POST', endpoint, json=data)
    
    def cancel_order(self, order_id: str) -> Dict:
        """Cancel order"""
        if self.paper_trading:
            return {'status': 'cancelled', 'paper_trading': True}
        
        endpoint = f"/orders/{order_id}"
        return self._request('DELETE', endpoint)
    
    def get_orders(self, market_id: Optional[str] = None, status: str = "open") -> List[Dict]:
        """Get orders"""
        if self.paper_trading:
            return []
        
        endpoint = "/orders"
        params = {'status': status}
        if market_id:
            params['market'] = market_id
        
        return self._request('GET', endpoint, params=params)
    
    def get_positions(self, market_id: Optional[str] = None) -> List[Dict]:
        """Get positions"""
        if self.paper_trading:
            return []
        
        endpoint = "/positions"
        params = {}
        if market_id:
            params['market'] = market_id
        
        return self._request('GET', endpoint, params=params)
    
    def get_balance(self) -> Dict:
        """Get balance"""
        endpoint = "/balance"
        try:
            return self._request('GET', endpoint)
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return {'usdc': 0.0, 'error': str(e)}

