import os
import json
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from ai_search import ai_search_query
# from redis_cache import instruments_cache

# Common company name to symbol mapping for better search
COMPANY_ALIASES = {
    "infosys": "INFY",
    "reliance": "RELIANCE",
    "tcs": "TCS",
    "tata consultancy": "TCS",
    "hdfc": "HDFCBANK",
    "hdfc bank": "HDFCBANK",
    "icici": "ICICIBANK",
    "icici bank": "ICICIBANK",
    "sbi": "SBIN",
    "state bank": "SBIN",
    "wipro": "WIPRO",
    "bharti": "BHARTIARTL",
    "airtel": "BHARTIARTL",
    "itc": "ITC",
    "tata motors": "TATAMOTORS",
    "tata steel": "TATASTEEL",
    "adani": "ADANIENT",
    "asian paints": "ASIANPAINT",
    "bajaj": "BAJFINANCE",
    "maruti": "MARUTI",
    "ultratech": "ULTRACEMCO",
    "zomato": "ZOMATO",
    "l&t finance": "L&TFH",
    "ltfh": "L&TFH",
}

# In-memory cache for instruments (to avoid loading 21713 instruments repeatedly)
_instruments_memory_cache = None
_instruments_cache_time = None
MEMORY_CACHE_TTL_HOURS = 24

# Redis cache key and TTL
CACHE_KEY = "angelone_instruments"
CACHE_EXPIRY_HOURS = 24
CACHE_EXPIRY_SECONDS = CACHE_EXPIRY_HOURS * 3600

# Fallback file cache (in case Redis is unavailable)
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
INSTRUMENT_CACHE_FILE = os.path.join(CACHE_DIR, "angelone_instruments.json")

# Angel One Instrument Master URL
INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

def ensure_cache_dir():
    """Create cache directory if it doesn't exist (for fallback)"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
        print(f"[INSTRUMENTS] Created cache directory: {CACHE_DIR}")

async def is_cache_valid() -> bool:
    """Check if cached instruments exist and are not expired (Cache first, fallback to file)"""
    # Try Cache first
    try:
        from market_cache import market_data_cache
        # Check raw existence to avoid parsing overhead
        if market_data_cache.redis:
            exists = await market_data_cache.redis.exists(CACHE_KEY)
            if exists:
                return True
    except Exception as e:
        print(f"[INSTRUMENTS] Cache check failed: {e}")
    
    # Fallback to file cache
    if not os.path.exists(INSTRUMENT_CACHE_FILE):
        return False
    
    # Check if file is older than CACHE_EXPIRY_HOURS
    file_time = datetime.fromtimestamp(os.path.getmtime(INSTRUMENT_CACHE_FILE))
    expiry_time = datetime.now() - timedelta(hours=CACHE_EXPIRY_HOURS)
    
    return file_time > expiry_time

async def download_instrument_master() -> bool:
    """Download the instrument master file from Angel One and cache in Redis"""
    try:
        print("[INSTRUMENTS] Downloading instrument master file...")
        
        response = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        response.raise_for_status()
        
        # Parse JSON
        instruments = response.json()
        
        # Filter only NSE and BSE instruments
        filtered = []
        for inst in instruments:
            exch = inst.get("exch_seg", "")
            symbol = inst.get("symbol", "")
            name = inst.get("name", "")
            
            # Keep NSE and BSE stocks only (Equity)
            # Explicitly exclude NFO and CDS segments
            if exch in ["NSE", "BSE"] and symbol and name and exch != "NFO":
                # Exclude indices and special instruments
                if not any(x in symbol for x in ["NIFTY", "SENSEX", "BANKNIFTY"]):
                    filtered.append(inst)
        
        # Save to Cache
        try:
            from market_cache import market_data_cache
            success = await market_data_cache.set(CACHE_KEY, filtered, ttl=CACHE_EXPIRY_SECONDS)
            if success:
                print(f"[INSTRUMENTS] Cached {len(filtered)} instruments in Cache")
        except Exception as e:
            print(f"[INSTRUMENTS] Cache failed, using file fallback: {e}")
        
        # Also save to file as fallback
        ensure_cache_dir()
        with open(INSTRUMENT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)
        
        print(f"[INSTRUMENTS] Downloaded and cached {len(filtered)} instruments")
        return True
        
    except Exception as e:
        print(f"[INSTRUMENTS ERROR] Failed to download: {e}")
        return False

async def load_instruments() -> List[Dict]:
    """Load instruments from cache or download if needed (Redis first, file fallback)"""
    # Check if cache is valid
    if not await is_cache_valid():
        print("[INSTRUMENTS] Cache expired or missing, downloading...")
        if not await download_instrument_master():
            print("[INSTRUMENTS ERROR] Failed to download, returning empty list")
            return []
    
    # Try to load from Cache first
    try:
        from market_cache import market_data_cache
        instruments = await market_data_cache.get(CACHE_KEY)
        if instruments:
            print(f"[INSTRUMENTS] Loaded {len(instruments)} instruments from Cache")
            return instruments
    except Exception as e:
        print(f"[INSTRUMENTS] Cache load failed, trying file fallback: {e}")
    
    # Fallback to file cache
    try:
        with open(INSTRUMENT_CACHE_FILE, 'r', encoding='utf-8') as f:
            instruments = json.load(f)
        print(f"[INSTRUMENTS] Loaded {len(instruments)} instruments from file")
        return instruments
    except Exception as e:
        print(f"[INSTRUMENTS ERROR] Failed to load cache: {e}")
        return []

async def search_instruments(query: str, limit: int = 10, use_ai: bool = True) -> List[Dict]:
    """
    Search for instruments by symbol or company name
    Uses AI to interpret natural language queries (optional)
    Returns list of matching instruments with format compatible with the app
    """
    if not query or len(query) < 2:
        return []
    
    instruments = await load_instruments()
    query_lower = query.lower().strip()
    
    # First, try AI to interpret the query (if enabled)
    if use_ai:
        ai_result = ai_search_query(query)
        if ai_result != query:
            query_lower = ai_result.lower()
            print(f"[INSTRUMENTS] AI converted '{query}' → '{ai_result}'")
    
    # Then check manual aliases as fallback
    if query_lower in COMPANY_ALIASES:
        matched_symbol = COMPANY_ALIASES[query_lower]
        query_lower = matched_symbol.lower()
        print(f"[INSTRUMENTS] Alias matched '{query}' → '{matched_symbol}'")
    
    results = []
    
    for inst in instruments:
        symbol = inst.get("symbol", "")
        name = inst.get("name", "")
        exchange = inst.get("exch_seg", "NSE")
        
        # Double check exclusion in search time
        if exchange == "NFO":
            continue
        
        # Match by symbol OR name (case-insensitive)
        symbol_match = query_lower in symbol.lower()
        name_match = query_lower in name.lower()
        
        if symbol_match or name_match:
            # Convert to app-compatible format
            results.append({
                "symbol": f"{symbol}.X{exchange}",  # e.g., INFY.XNSE
                "name": name,
                "company": name,
                "exchange": exchange,
                "token": inst.get("token"),  # Angel One symbol token
                "lot_size": inst.get("lotsize", 1)
            })
            
            if len(results) >= limit:
                break
    
    print(f"[INSTRUMENTS] Search '{query}' returned {len(results)} results")
    return results

async def get_instrument_by_symbol(symbol: str, exchange: str = "NSE") -> Optional[Dict]:
    """
    Get instrument details by exact symbol match
    """
    instruments = await load_instruments()
    
    # Angel One uses suffixes like -EQ for equity, -BE for BE series, etc.
    # Try multiple patterns to find the instrument
    search_patterns = [
        symbol,           # Exact match
        f"{symbol}-EQ",   # Equity series (most common)
        f"{symbol}-BE",   # BE series
        f"{symbol}-SM",   # SM series
    ]
    
    # Search for matches with various patterns
    for inst in instruments:
        inst_symbol = inst.get("symbol", "")
        inst_exchange = inst.get("exch_seg", "")
        
        if inst_exchange == exchange and inst_symbol in search_patterns:
            return {
                "symbol": f"{symbol}.X{exchange}",
                "name": inst.get("name"),
                "exchange": exchange,
                "token": inst.get("token"),
                "lot_size": inst.get("lotsize", 1),
                "trading_symbol": inst_symbol  # Include actual trading symbol
            }
    
    return None

# Pre-load instruments on module import (async in background)
async def init_instruments():
    """Initialize instrument cache in background"""
    try:
        if not await is_cache_valid():
            print("[INSTRUMENTS] Initializing cache...")
            await download_instrument_master()
    except Exception as e:
        print(f"[INSTRUMENTS] Init error: {e}")


# =============================================================================
# Synchronous Wrapper Functions (for backward compatibility)
# =============================================================================

def load_instruments_sync() -> List[Dict]:
    """
    Synchronous wrapper for load_instruments()
    Uses in-memory cache first, then file cache to avoid loading 21713 instruments repeatedly.
    """
    global _instruments_memory_cache, _instruments_cache_time
    
    # Check in-memory cache first
    if _instruments_memory_cache and _instruments_cache_time:
        cache_age = datetime.now() - _instruments_cache_time
        if cache_age < timedelta(hours=MEMORY_CACHE_TTL_HOURS):
            return _instruments_memory_cache
    
    # Skip Redis entirely in sync mode - use file cache only
    try:
        # Check if file cache exists and is valid
        if os.path.exists(INSTRUMENT_CACHE_FILE):
            file_time = datetime.fromtimestamp(os.path.getmtime(INSTRUMENT_CACHE_FILE))
            expiry_time = datetime.now() - timedelta(hours=CACHE_EXPIRY_HOURS)
            
            if file_time > expiry_time:
                # File cache is valid, load it
                with open(INSTRUMENT_CACHE_FILE, 'r', encoding='utf-8') as f:
                    instruments = json.load(f)
                # Store in memory cache
                _instruments_memory_cache = instruments
                _instruments_cache_time = datetime.now()
                print(f"[INSTRUMENTS] Loaded {len(instruments)} instruments from file (cached in memory)")
                return instruments
        
        # File doesn't exist or is expired - download synchronously
        print("[INSTRUMENTS] Downloading instrument master (sync)...")
        response = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        response.raise_for_status()
        
        instruments = response.json()
        
        # Filter only NSE and BSE instruments
        filtered = []
        for inst in instruments:
            exch = inst.get("exch_seg", "")
            symbol = inst.get("symbol", "")
            name = inst.get("name", "")
            
            if exch in ["NSE", "BSE"] and symbol and name and exch != "NFO":
                if not any(x in symbol for x in ["NIFTY", "SENSEX", "BANKNIFTY"]):
                    filtered.append(inst)
        
        # Save to file
        ensure_cache_dir()
        with open(INSTRUMENT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)
        
        # Store in memory cache
        _instruments_memory_cache = filtered
        _instruments_cache_time = datetime.now()
        
        print(f"[INSTRUMENTS] Downloaded and cached {len(filtered)} instruments")
        return filtered
    except Exception as e:
        print(f"[INSTRUMENTS] Sync load error: {e}")
        # Fallback to file cache
        try:
            with open(INSTRUMENT_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []


def search_instruments_sync(query: str, limit: int = 10, use_ai: bool = True) -> List[Dict]:
    """
    Synchronous wrapper for search_instruments()
    Uses direct file-based search to avoid event loop conflicts.
    """
    try:
        # Load instruments synchronously
        instruments = load_instruments_sync()
        
        if not instruments:
            return []
        
        # Normalize query
        query_lower = query.lower().strip()
        
        # Check for company alias first
        if query_lower in COMPANY_ALIASES:
            query_lower = COMPANY_ALIASES[query_lower].lower()
        
        results = []
        
        # Try AI search if enabled
        if use_ai and not query_lower.isdigit():
            try:
                ai_symbols = ai_search_query(query)
                if ai_symbols:
                    # Find instruments matching AI suggestions
                    for inst in instruments:
                        symbol = inst.get("symbol", "").upper()
                        if any(ai_sym.upper() in symbol for ai_sym in ai_symbols):
                            results.append({
                                "symbol": symbol,
                                "name": inst.get("name", ""),
                                "exchange": inst.get("exch_seg", "NSE")
                            })
                            if len(results) >= limit:
                                break
                    
                    if results:
                        print(f"[INSTRUMENTS] AI search found {len(results)} results")
                        return results
            except Exception as e:
                print(f"[INSTRUMENTS] AI search failed, falling back to fuzzy: {e}")
        
        # Fallback: Fuzzy search
        for inst in instruments:
            symbol = inst.get("symbol", "").lower()
            name = inst.get("name", "").lower()
            
            # Exact symbol match (highest priority)
            if symbol == query_lower:
                results.insert(0, {
                    "symbol": inst.get("symbol", ""),
                    "name": inst.get("name", ""),
                    "exchange": inst.get("exch_seg", "NSE")
                })
            # Symbol starts with query
            elif symbol.startswith(query_lower):
                results.append({
                    "symbol": inst.get("symbol", ""),
                    "name": inst.get("name", ""),
                    "exchange": inst.get("exch_seg", "NSE")
                })
            # Name contains query
            elif query_lower in name:
                results.append({
                    "symbol": inst.get("symbol", ""),
                    "name": inst.get("name", ""),
                    "exchange": inst.get("exch_seg", "NSE")
                })
            
            if len(results) >= limit * 2:  # Get extra for deduplication
                break
        
        # Deduplicate and limit
        seen = set()
        unique_results = []
        for r in results:
            if r["symbol"] not in seen:
                seen.add(r["symbol"])
                unique_results.append(r)
                if len(unique_results) >= limit:
                    break
        
        return unique_results
    except Exception as e:
        print(f"[INSTRUMENTS] Sync search error: {e}")
        return []


def get_instrument_by_symbol_sync(symbol: str, exchange: str = "NSE") -> Optional[Dict]:
    """
    Synchronous wrapper for get_instrument_by_symbol()
    Uses direct file-based lookup to avoid event loop conflicts.
    """
    try:
        # Load instruments synchronously
        instruments = load_instruments_sync()
        
        if not instruments:
            return None
        
        # Angel One uses suffixes like -EQ for equity, -BE for BE series, etc.
        # Try multiple patterns to find the instrument
        search_patterns = [
            symbol,           # Exact match
            f"{symbol}-EQ",   # Equity series (most common)
            f"{symbol}-BE",   # BE series
            f"{symbol}-SM",   # SM series
        ]
        
        # Search for matches with various patterns
        for inst in instruments:
            inst_symbol = inst.get("symbol", "")
            inst_exchange = inst.get("exch_seg", "")
            
            if inst_exchange == exchange and inst_symbol in search_patterns:
                return {
                    "symbol": f"{symbol}.X{exchange}",
                    "name": inst.get("name"),
                    "exchange": exchange,
                    "token": inst.get("token"),
                    "lot_size": inst.get("lotsize", 1),
                    "trading_symbol": inst_symbol  # Include actual trading symbol
                }
        
        return None
    except Exception as e:
        print(f"[INSTRUMENTS] Sync get error: {e}")
        return None


# Backward compatibility aliases (use sync wrappers by default)
load_instruments_old = load_instruments_sync
search_instruments_old = search_instruments_sync
get_instrument_by_symbol_old = get_instrument_by_symbol_sync
