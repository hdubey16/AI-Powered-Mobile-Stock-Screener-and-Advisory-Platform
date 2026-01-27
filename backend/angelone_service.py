import os
import asyncio
from SmartApi import SmartConnect
from dotenv import load_dotenv
import pyotp
from datetime import datetime, timedelta
from instrument_master import (
    search_instruments,
    search_instruments_sync,
    get_instrument_by_symbol,
    get_instrument_by_symbol_sync
)
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
from threading import Lock

load_dotenv()

# Cache TTL
ANGEL_HISTORY_TTL = 60 # 1 minute for intraday data (it changes fast)


# Angel One credentials
API_KEY = os.getenv("ANGELONE_API_KEY")
SECRET_KEY = os.getenv("ANGELONE_SECRET_KEY")
CLIENT_CODE = os.getenv("ANGELONE_CLIENT_CODE")
TOTP_KEY = os.getenv("ANGELONE_TOTP_KEY")
MPIN = os.getenv("ANGELONE_MPIN")

# Historical API Credentials
HIST_API_KEY = os.getenv("ANGELONE_HIST_API_KEY")
HIST_SECRET_KEY = os.getenv("ANGELONE_HIST_SECRET_KEY")

# Market Feeds API Credentials
MARKET_API_KEY = os.getenv("ANGELONE_MARKET_API_KEY")
MARKET_SECRET_KEY = os.getenv("ANGELONE_MARKET_SECRET_KEY")

# Publisher API Credentials
PUBLISHER_API_KEY = os.getenv("ANGELONE_PUBLISHER_API_KEY")
PUBLISHER_SECRET_KEY = os.getenv("ANGELONE_PUBLISHER_SECRET_KEY")

# Initialize SmartAPI
smart_api = None
auth_token = None

# Initialize Historical SmartAPI
smart_api_hist = None
hist_auth_token = None

# Initialize Market SmartAPI
smart_api_market = None
market_auth_token = None

# Initialize Publisher SmartAPI
smart_api_pub = None
pub_auth_token = None

# In-memory generic cache for API responses
_api_cache = {}
_api_cache_ttl = 30  # 30 seconds for quote data (increased from 5)

# Rate Limiter
class RateLimiter:
    def __init__(self, max_calls_per_second=3):
        self.max_calls = max_calls_per_second
        self.params = {
            'last_call_time': 0,
            'lock': Lock()
        }
    
    def wait(self):
        with self.params['lock']:
            current_time = time.time()
            elapsed = current_time - self.params['last_call_time']
            wait_time = (1.0 / self.max_calls) - elapsed
            
            if wait_time > 0:
                time.sleep(wait_time)
            
            self.params['last_call_time'] = time.time()

_rate_limiter = RateLimiter(max_calls_per_second=3)

# Login rate limiting
_last_login_attempt = {}
_login_cooldown_seconds = 60  # Prevent login attempts within 60 seconds

# HTTP Session with connection pooling for faster requests
_http_session = None

def get_http_session():
    """Get or create HTTP session with connection pooling"""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        # Configure retry strategy
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,  # Number of connection pools
            pool_maxsize=50,      # Max connections per pool
            pool_block=False
        )
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
    return _http_session

def get_cached_response(key: str, ttl: int = _api_cache_ttl):
    """Get response from internal cache if valid"""
    if key in _api_cache:
        data, timestamp = _api_cache[key]
        if datetime.now() - timestamp < timedelta(seconds=ttl):
            return data
        else:
            del _api_cache[key]
    return None

def set_cached_response(key: str, data: any):
    """Set response in internal cache"""
    _api_cache[key] = (data, datetime.now())

async def get_stock_quote_angel_async(symbol: str, exchange: str = "NSE"):
    """
    Async wrapper for get_stock_quote_angel to avoid blocking event loop
    """
    return await asyncio.to_thread(get_stock_quote_angel, symbol, exchange)

def login_to_angel_one():
    """Authenticate with Angel One and get session token"""
    global smart_api, auth_token
    
    # Check for placeholder credentials
    if not API_KEY or API_KEY == "your_api_key_here":
        print("[ANGELONE] Invalid API Key (Placeholder), skipping login")
        return False

    try:
        smart_api = SmartConnect(api_key=API_KEY)
        
        # Generate TOTP
        try:
            totp = pyotp.TOTP(TOTP_KEY).now()
        except Exception:
            print("[ANGELONE] Invalid TOTP Key")
            return False
        
        # Login using MPIN
        data = smart_api.generateSession(CLIENT_CODE, MPIN, totp)
        
        if data and data.get('status'):
            auth_token = data['data']['jwtToken']
            refresh_token = data['data']['refreshToken']
            feed_token = data['data'].get('feedToken', '')
            
            # Set auth tokens for future requests
            smart_api.setAccessToken(auth_token)
            smart_api.setRefreshToken(refresh_token)
            if feed_token:
                smart_api.setFeedToken(feed_token)
            
            print(f"[ANGELONE] Login successful!")
            return True
        else:
            print(f"[ANGELONE ERROR] Login failed: {data}")
            return False
            
    except Exception as e:
        print(f"[ANGELONE ERROR] Login exception: {e}")
        return False

def login_to_hist_angel_one():
    """Authenticate specific for Historical API"""
    global smart_api_hist, hist_auth_token
    
    if not HIST_API_KEY or HIST_API_KEY == "your_api_key_here":
        print("[ANGELONE] No Historical API Key found, checking main key...")
        return False

    try:
        print("[ANGELONE] Logging in for Historical Data...")
        smart_api_hist = SmartConnect(api_key=HIST_API_KEY)
        
        # Generate TOTP
        try:
            totp = pyotp.TOTP(TOTP_KEY).now()
        except Exception:
            return False
        
        # Login using MPIN
        data = smart_api_hist.generateSession(CLIENT_CODE, MPIN, totp)
        
        if data and data.get('status'):
            hist_auth_token = data['data']['jwtToken']
            print(f"[ANGELONE] Historical Login successful!")
            return True
        else:
            print(f"[ANGELONE ERROR] Historical Login failed: {data}")
            return False
            
    except Exception as e:
        print(f"[ANGELONE ERROR] Historical Login exception: {e}")
        return False

def login_to_market_angel_one():
    """Authenticate specific for Market Feeds API with rate limiting"""
    global smart_api_market, market_auth_token, _last_login_attempt
    
    if not MARKET_API_KEY or MARKET_API_KEY == "your_api_key_here":
        return False
    
    # Check if we already have a valid token
    if market_auth_token:
        return True
    
    # Rate limiting - prevent login attempts within cooldown period
    now = datetime.now()
    if 'market' in _last_login_attempt:
        time_since_last = (now - _last_login_attempt['market']).total_seconds()
        if time_since_last < _login_cooldown_seconds:
            return False
    
    try:
        _last_login_attempt['market'] = now
        smart_api_market = SmartConnect(api_key=MARKET_API_KEY)
        
        # Generate TOTP
        try:
            totp = pyotp.TOTP(TOTP_KEY).now()
        except Exception:
            return False
        
        # Login using MPIN
        data = smart_api_market.generateSession(CLIENT_CODE, MPIN, totp)
        
        if data and data.get('status'):
            market_auth_token = data['data']['jwtToken']
            print(f"[ANGELONE] Market Login successful!")
            return True
        else:
            return False
            
    except Exception as e:
        if "rate" in str(e).lower():
            print(f"[ANGELONE] Rate limited - cooling down")
        return False

def login_to_publisher_angel_one():
    """Authenticate specific for Publisher API"""
    global smart_api_pub, pub_auth_token
    
    if not PUBLISHER_API_KEY or PUBLISHER_API_KEY == "your_api_key_here":
        print("[ANGELONE] No Publisher API Key found.")
        return False

    try:
        print("[ANGELONE] Logging in for Publisher Data...")
        smart_api_pub = SmartConnect(api_key=PUBLISHER_API_KEY)
        
        # Generate TOTP
        try:
            totp = pyotp.TOTP(TOTP_KEY).now()
        except Exception:
            return False
        
        # Login using MPIN
        data = smart_api_pub.generateSession(CLIENT_CODE, MPIN, totp)
        
        if data and data.get('status'):
            pub_auth_token = data['data']['jwtToken']
            print(f"[ANGELONE] Publisher Login successful!")
            return True
        else:
            print(f"[ANGELONE ERROR] Publisher Login failed: {data}")
            return False
            
    except Exception as e:
        print(f"[ANGELONE ERROR] Publisher Login exception: {e}")
        return False

# Login Lock
login_lock = Lock()

def get_smart_api(force_fresh=False):
    """
    Get or create authenticated SmartAPI session with Double-Checked Locking
    force_fresh: If True, force a new login
    """
    global smart_api, auth_token, _last_login_attempt
    
    # 1. Fast Path: If we have a valid session and not forcing fresh, return it
    if smart_api and auth_token and not force_fresh:
        return smart_api
    
    # 2. Acquire Lock
    with login_lock:
        # 3. Double Check: Check again in case another thread initialized it while we were waiting
        if smart_api and auth_token and not force_fresh:
            return smart_api
            
        # Check global cooldown to prevent spamming login on persistent failures
        now = datetime.now()
        if 'main' in _last_login_attempt:
            time_since_last = (now - _last_login_attempt['main']).total_seconds()
            if time_since_last < 5 and not force_fresh: # 5s cooldown
                 print("[ANGELONE] Login cooldown active, skipping...")
                 return smart_api # Return what we have, might be None
        
        _last_login_attempt['main'] = now
        
        # 4. Perform Login
        import threading
        print(f"[ANGELONE] Initializing or Refreshing SmartAPI Session... (Thread: {threading.current_thread().name})")
        if login_to_angel_one():
            return smart_api
        
        # If login failed, add a small sleep to slow down all waiting threads
        time.sleep(1)
        
        return None

def search_stocks_angel(query: str):
    """
    Search for stocks on NSE/BSE using Angel One API
    """
    try:
        api = get_smart_api()
        if not api:
            return []
        
        # Angel One uses symboltoken for identification
        # For search, we'll use their instrument master file
        # This is a simplified version - you may need to download and cache the master file
        
        results = []
        # Placeholder: In production, load from instrument master CSV
        # For now, return basic format that matches your app
        
        return results
        
    except Exception as e:
        print(f"[ANGELONE ERROR] Search failed: {e}")
        return []

def get_stock_quote_angel(symbol: str, exchange: str = "NSE"):
    """
    Get real-time quote for a stock using direct API call
    symbol: Stock symbol (e.g., "RELIANCE")
    exchange: NSE or BSE
    """
    # Check internal cache
    cache_key = f"quote:{symbol}:{exchange}"
    cached = get_cached_response(cache_key)
    if cached:
        return cached

    try:
        # 1. Try Market Session First (with rate limiting)
        if MARKET_API_KEY and market_auth_token:
            # Reuse existing token
            use_token = market_auth_token
            use_key = MARKET_API_KEY
        elif MARKET_API_KEY and not market_auth_token:
            # Try to login only if we don't have a token
            if login_to_market_angel_one():
                use_token = market_auth_token
                use_key = MARKET_API_KEY
            else:
                use_token = None
        else:
            use_token = None
        
        # Skip the old login attempt code
        if False:  # Placeholder to maintain line structure
            pass
            
            use_token = market_auth_token
            use_key = MARKET_API_KEY
            print("[ANGELONE] Using Market API session for Quote")
        else:
            use_token = None

        # 2. Fallback to Trading Session (Standard)
        if not use_token:
            api = get_smart_api()
            
            # Auto-retry login once if token is missing
            if not auth_token:
                 api = get_smart_api(force_fresh=True)
            
            if api and auth_token:
                use_token = auth_token
                use_key = API_KEY
                print(f"[ANGELONE] Using Trading API session for Quote {symbol}")
        
        if not use_token:
            print("[ANGELONE] No authenticated session for quote")
            return None
        
        # Convert exchange format
        exchange_map = {
            "NSE": "NSE",
            "BSE": "BSE",
            "XNSE": "NSE",
            "XBSE": "BSE"
        }
        
        angel_exchange = exchange_map.get(exchange, "NSE")
        
        # Get instrument details to get the token (use sync version)
        instrument = get_instrument_by_symbol_sync(symbol, angel_exchange)
        if not instrument:
            print(f"[ANGELONE] Instrument not found: {symbol}.{exchange}")
            return None
        
        token = instrument.get("token")
        if not token:
            print(f"[ANGELONE] No token for {symbol}")
            return None
        
        # Use the actual trading symbol from instrument master (e.g., RELIANCE-EQ)
        trading_symbol = instrument.get("trading_symbol", symbol)
        
        # Make direct API call with proper auth header
        url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getLtpData"
        headers = {
            "Authorization": use_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "e4:e7:49:35:41:f9",
            "X-PrivateKey": use_key
        }
        
        payload = {
            "exchange": angel_exchange,
            "tradingsymbol": trading_symbol,  # Use actual trading symbol with suffix
            "symboltoken": str(token)
        }
        
        # Apply Rate Limit before request
        _rate_limiter.wait()
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        # Handle empty responses
        if not response.text or response.text.strip() == '':
            print(f"[ANGELONE] Empty response for {symbol}")
            return None
        
        try:
            data = response.json()
        except ValueError as e:
            print(f"[ANGELONE] JSON parse error for {symbol}: {e}")
            # Fallback to Yahoo Finance
            print(f"[ANGELONE] Trying Yahoo Finance fallback for {symbol}")
            try:
                from yahoo_service import get_yahoo_quote
                import asyncio
                yahoo_quote = asyncio.run(get_yahoo_quote(symbol, exchange))
                if yahoo_quote:
                    print(f"[YAHOO] Successfully fetched {symbol} as fallback")
                    return yahoo_quote
            except Exception as yahoo_error:
                print(f"[YAHOO] Fallback failed for {symbol}: {yahoo_error}")
            return None
        
        if data.get("data"):
            quote = data["data"]
            
            # Calculate accurate price and percentage change
            ltp = float(quote.get("ltp", 0))
            
            # Validate price - if 0, treat as error and fallback to Yahoo
            if ltp <= 0:
                print(f"[ANGELONE] Zero price returned for {symbol}. Triggering fallback.")
                # Raise exception to trigger the Yahoo fallback in the except block below?
                # Or handle it directly here.
                # Let's direct to Yahoo fallback directly for cleanliness
                print(f"[ANGELONE] Trying Yahoo Finance fallback for {symbol} (Zero Price)")
                try:
                    from yahoo_service import get_yahoo_quote
                    import asyncio
                    yahoo_quote = asyncio.run(get_yahoo_quote(symbol, exchange))
                    if yahoo_quote:
                        print(f"[YAHOO] Successfully fetched {symbol} as fallback")
                        # Cache this result too!
                        set_cached_response(cache_key, yahoo_quote)
                        return yahoo_quote
                except Exception as yahoo_error:
                    print(f"[YAHOO] Fallback failed for {symbol}: {yahoo_error}")
                return None
            open_price = float(quote.get("open", 0))
            close_price = float(quote.get("close", 0))
            
            # Use previous close for change calculation (close from yesterday)
            # If close is 0, use open as fallback
            previous_close = close_price if close_price > 0 else open_price
            
            if previous_close > 0 and ltp > 0:
                change = ltp - previous_close
                change_percent = (change / previous_close) * 100
            else:
                change = 0
                change_percent = 0
            
            print(f"[DEBUG] REST Quote for {symbol}: LTP={ltp}, PrevClose={previous_close}, Change={change:.2f} ({change_percent:.2f}%)")
            
            result = {
                "symbol": f"{symbol}.{exchange}",
                "name": instrument.get("name", symbol),
                "exchange": angel_exchange,
                "ltp": ltp,
                "close": ltp,  # Current trading price
                "previous_close": previous_close,
                "open": open_price,
                "high": float(quote.get("high", 0)),
                "low": float(quote.get("low", 0)),
                "volume": int(quote.get("volume", 0)),
                "change": round(change, 2),
                "changePercent": round(change_percent, 2),
                "date": datetime.now().isoformat()
            }
            # Cache the result
            set_cached_response(cache_key, result)
            return result
        else:
            print(f"[ANGELONE] Quote API error: {data}")
        
        return None
        
    except Exception as e:
        print(f"[ANGELONE ERROR] Quote failed for {symbol}: {e}")
        return None

async def get_stock_history_angel(symbol: str, exchange: str = "NSE", days: int = 30, interval: str = "ONE_DAY"):
    """
    Get historical candlestick data using Cache + Sync API in thread
    """
    # Import locally to avoid circular dependency
    from market_cache import market_data_cache
    
    cache_key = f"angel_history:{symbol}:{exchange}:{interval}:{days}"
    cached_data = await market_data_cache.get(cache_key)
    if cached_data:
        return cached_data

    # Run sync function in thread
    data = await asyncio.to_thread(_get_stock_history_angel_sync, symbol, exchange, days, interval)
    
    if data:
        await market_data_cache.set(cache_key, data, ttl=ANGEL_HISTORY_TTL)
        
    return data

def _get_stock_history_angel_sync(symbol: str, exchange: str = "NSE", days: int = 30, interval: str = "ONE_DAY"):
    """
    Internal Sync function for historical data
    """
    try:
        # Normalize symbol - remove exchange suffix but keep -EQ
        # RELIANCE-EQ.XNSE -> RELIANCE-EQ
        normalized_symbol = symbol
        if ".XNSE" in normalized_symbol:
            normalized_symbol = normalized_symbol.replace(".XNSE", "")
        if ".XBSE" in normalized_symbol:
            normalized_symbol = normalized_symbol.replace(".XBSE", "")
        
        # Ensure we're logged in with Historical Key if available
        if HIST_API_KEY:
            if not hist_auth_token:
               login_to_hist_angel_one()
            
            use_token = hist_auth_token
            use_key = HIST_API_KEY
        else:
            # Fallback to main key
            api = get_smart_api()
            if not api or not auth_token:
                print("[ANGELONE] Not authenticated for history")
                return []
            use_token = auth_token
            use_key = API_KEY

        if not use_token:
             print("[ANGELONE] No auth token available for history")
             return []
        
        # Convert exchange format
        exchange_map = {
            "NSE": "NSE",
            "BSE": "BSE",
            "XNSE": "NSE",
            "XBSE": "BSE"
        }
        angel_exchange = exchange_map.get(exchange, "NSE")
        
        # Get instrument token using normalized symbol (Using SYNC lookup)
        instrument = get_instrument_by_symbol_sync(normalized_symbol, angel_exchange)
        if not instrument:
            print(f"[ANGELONE] Instrument not found for history: {normalized_symbol}.{angel_exchange}")
            return []
        
        token = instrument.get("token")
        if not token:
            print(f"[ANGELONE] No token for history: {normalized_symbol}")
            return []
        
        # Calculate date range
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)
        
        # Format dates for Angel One API (YYYY-MM-DD HH:MM)
        from_date_str = from_date.strftime("%Y-%m-%d 09:15")
        to_date_str = to_date.strftime("%Y-%m-%d 15:30")
        
        print(f"[ANGELONE DEBUG] Requesting history for {normalized_symbol} | Days: {days}| Interval: {interval}")
        print(f"[ANGELONE DEBUG] From: {from_date_str} To: {to_date_str}")
        
        # Make direct API call with proper auth header
        url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
        headers = {
            "Authorization": use_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "e4:e7:49:35:41:f9",
            "X-PrivateKey": use_key
        }
        
        payload = {
            "exchange": angel_exchange,
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": from_date_str,
            "todate": to_date_str
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        hist_data = response.json()
        
        if hist_data.get("data"):
            candles = []
            for candle in hist_data["data"]:
                # Angel One returns: [timestamp, open, high, low, close, volume]
                candles.append({
                    "date": candle[0],
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5])
                })
            print(f"[ANGELONE] Got {len(candles)} candles for {symbol}")
            return candles
        else:
            print(f"[ANGELONE] History API error: {hist_data}")
        
        return []
        
    except Exception as e:
        print(f"[ANGELONE ERROR] History failed for {symbol}: {e}")
        return []

# Main functions for the app
async def search_stocks(query: str, use_ai: bool = True):
    """
    Search for stocks using Angel One instrument master
    """
    return await search_instruments(query, limit=20, use_ai=use_ai)

def get_stock_eod(symbol: str):
    """
    Get End of Day data using Angel One
    """
    # Parse symbol (e.g., "RELIANCE.XNSE" -> "RELIANCE", "NSE")
    parts = symbol.split(".")
    stock_symbol = parts[0]
    exchange = parts[1] if len(parts) > 1 else "NSE"
    
    return get_stock_quote_angel(stock_symbol, exchange)

async def get_stock_history(symbol: str, days: int = 30, interval: str = "ONE_DAY"):
    """
    Get historical data using Angel One
    """
    parts = symbol.split(".")
    stock_symbol = parts[0]
    exchange = parts[1] if len(parts) > 1 else "NSE"
    
    return await get_stock_history_angel(stock_symbol, exchange, days, interval)


# ============================================================================
# DERIVATIVES MARKET DATA APIS
# ============================================================================

def get_derivatives_gainers_losers(data_type: str = "PercOIGainers", expiry_type: str = "NEAR"):
    """
    Get Top Gainers/Losers in derivatives segment
    
    Args:
        data_type: PercOIGainers, PercOILosers, PercPriceGainers, PercPriceLosers
        expiry_type: NEAR (current month), NEXT (next month), FAR (month after next)
    
    Returns:
        List of derivatives with gain/loss data
    """
    global smart_api, auth_token
    
    if not smart_api or not auth_token:
        if not login_to_angel_one():
            return []
    
    try:
        url = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/gainersLosers"
        
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "192.168.1.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": API_KEY
        }
        
        payload = {
            "datatype": data_type,
            "expirytype": expiry_type
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") and data.get("data"):
                print(f"[ANGELONE] Got {len(data['data'])} {data_type} for {expiry_type}")
                return data
        else:
            print(f"[ANGELONE ERROR] Derivatives API error: {response.status_code} - {response.text}")
        
        return {"data": []}
        
    except Exception as e:
        print(f"[ANGELONE ERROR] Derivatives gainers/losers failed: {e}")
        return {"data": []}


def get_equity_gainers_losers(segment: str = "nse", sort_by: str = "percent"):
    """
    Get Top Gainers/Losers in equity segment by fetching quotes for active stocks
    
    Args:
        segment: nse, bse, or nse_sme
        sort_by: percent (default) or value
    
    Returns:
        Dict with gainers and losers list
    """
    from instrument_master import load_instruments_sync
    
    try:
        # Check cache first
        cache_key = f"equity_gainers_losers:{segment}"
        cached = get_cached_response(cache_key, ttl=300) # 5 minutes TTL
        if cached:
            print(f"[ANGELONE] Returning cached equity gainers/losers for {segment}")
            return cached

        # Use a comprehensive list of actively traded NSE stocks
        # These are stocks from various sectors that typically have good liquidity
        active_stocks = [
            # Nifty 50 & Major Liquid Stocks
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL",
            "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI", "TITAN", "ULTRACEMCO",
            "SUNPHARMA", "NESTLEIND", "WIPRO", "TATAMOTORS", "TATASTEEL", "POWERGRID", "NTPC", "ONGC",
            "JSWSTEEL", "GRASIM", "TECHM", "ADANIENT", "ADANIPORTS", "INDUSINDBK", "HCLTECH", "COALINDIA",
            "BAJAJFINSV", "HINDALCO", "APOLLOHOSP", "DIVISLAB", "EICHERMOT", "DRREDDY", "CIPLA", "HEROMOTOCO",
            "BPCL", "BRITANNIA", "TATACONSUM", "SBILIFE", "HDFCLIFE", "BAJAJ-AUTO", "M&M",
            
            # Banking & Finance
            "PNB", "BANKBARODA", "CANBK", "UNIONBANK", "IDFCFIRSTB", "AUBANK", "BANDHANBNK", "FEDERALBNK",
            "PFC", "RECLTD", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN", "BAJAJHLDNG", "ABCAPITAL", "L&TFH",
            
            # Auto & Ancillary
            "TVSMOTOR", "ASHOKLEY", "BHARATFORG", "MOTHERSON", "BALKRISIND", "MRF", "BOSCHLTD", "EXIDEIND",
            
            # IT & Services
            "LTIM", "LTTS", "PERSISTENT", "COFORGE", "MPHASIS", "TATACOMM", "KPITTECH", "CYIENT",
            "NAUKRI", "ZOMATO", "PAYTM", "NYKAA", "POLICYBZR", "DELHIVERY",
            
            # Pharma & Healthcare
            "LUPIN", "AUROPHARMA", "ALKEM", "TORNTPHARM", "BIOCON", "GLAND", "LAURUSLABS", "SYNGENE",
            "MAXHEALTH", "NH", "FORTIS", "METROPOLIS", "LALPATHLAB",
            
            # Energy, Power & Infra
            "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "ADANIENSOL", "ATGL", "NHPC", "SJVN", "SUZLON",
            "BEL", "HAL", "MAZDOCK", "COCHINSHIP", "BHEL", "RVNL", "IRFC", "IRCTC", "CONCOR",
            "GMRINFRA", "DLF", "GODREJPROP", "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE",
            
            # Consumer & Others
            "DMART", "VBL", "TRENT", "PAGEIND", "HAVELLS", "VOLTAS", "WHIRLPOOL", "DIXON", "POLYCAB",
            "PIDILITIND", "BERGEPAINT", "ASIANPAINT", "GODREJCP", "DABUR", "MARICO", "COLPAL",
            "SRF", "PIIND", "UPL", "AARTIIND", "COROMANDEL", "CHAMBLFERT", "GNFC"
        ]
        
        print(f"[ANGELONE] Fetching quotes for {len(active_stocks)} active stocks...")
        
        stock_data = []

        # Helper function for parallel processing
        def fetch_stock_process(symbol):
            try:
                quote = get_stock_quote_angel(symbol, "NSE")
                if quote and quote.get("changePercent") is not None:
                    change_pct = quote.get("changePercent", 0)
                    return {
                        "symbol": symbol,
                        "name": quote.get("name", symbol),
                        "price": round(quote.get("ltp", 0), 2),
                        "change": round(quote.get("change", 0), 2),
                        "changePercent": round(change_pct, 2),
                        "volume": quote.get("volume", 0),
                        "exchange": "NSE"
                    }
            except Exception:
                pass
            return None

        # Execute in parallel threads
        # Reduce concurrency to prevent 429 Rate Limit errors
        # Was 20, reducing to 3 for stability
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_stock = {executor.submit(fetch_stock_process, symbol): symbol for symbol in active_stocks}
            
            completed_count = 0
            for future in as_completed(future_to_stock):
                data = future.result()
                if data:
                    stock_data.append(data)
                
                completed_count += 1
                if completed_count % 10 == 0:
                    print(f"[ANGELONE] Progress: {completed_count}/{len(active_stocks)}")

        print(f"[ANGELONE] Successfully fetched {len(stock_data)} stock quotes")
        
        # Sort by change percentage
        stock_data.sort(key=lambda x: x["changePercent"], reverse=True)
        
        # Get top 5 gainers and losers
        gainers = [s for s in stock_data if s["changePercent"] > 0][:5]
        losers = [s for s in stock_data if s["changePercent"] < 0][-5:]
        losers.reverse()  # Show worst performers first
        
        result = {
            "gainers": gainers,
            "losers": losers,
            "total_fetched": len(stock_data)
        }
        
        # Cache the result
        set_cached_response(cache_key, result)
        return result
        
    except Exception as e:
        print(f"[ANGELONE ERROR] Equity gainers/losers failed: {e}")
        import traceback
        traceback.print_exc()
        return {"gainers": [], "losers": []}


def get_pcr_volume():
    """
    Get Put-Call Ratio (PCR) for options contracts
    
    Returns:
        List of PCR data for different underlying stocks
    """
    global smart_api, auth_token
    
    if not smart_api or not auth_token:
        if not login_to_angel_one():
            return []
    
    try:
        url = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/putCallRatio"
        
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "192.168.1.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": API_KEY
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") and data.get("data"):
                print(f"[ANGELONE] Got PCR data for {len(data['data'])} instruments")
                return data["data"]
        else:
            print(f"[ANGELONE ERROR] PCR API error: {response.status_code} - {response.text}")
        
        return []
        
    except Exception as e:
        print(f"[ANGELONE ERROR] PCR Volume failed: {e}")
        return []


def get_oi_buildup(data_type: str = "Long Built Up", expiry_type: str = "NEAR"):
    """
    Get OI BuildUp data for derivatives
    
    Args:
        data_type: "Long Built Up", "Short Built Up", "Short Covering", "Long Unwinding"
        expiry_type: NEAR (current month), NEXT (next month), FAR (month after next)
    
    Returns:
        List of OI buildup data
    """
    global smart_api, auth_token
    
    if not smart_api or not auth_token:
        if not login_to_angel_one():
            return []
    
    try:
        url = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/OIBuildup"
        
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "192.168.1.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": API_KEY
        }
        
        payload = {
            "datatype": data_type,
            "expirytype": expiry_type
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") and data.get("data"):
                print(f"[ANGELONE] Got {len(data['data'])} {data_type} for {expiry_type}")
                return data["data"]
        else:
            print(f"[ANGELONE ERROR] OI BuildUp API error: {response.status_code} - {response.text}")
        
        return []
        
    except Exception as e:
        print(f"[ANGELONE ERROR] OI BuildUp failed: {e}")
        return []
