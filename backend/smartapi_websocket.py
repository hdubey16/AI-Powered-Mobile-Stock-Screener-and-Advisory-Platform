
import logging
from typing import Dict, Any, Callable, Optional, Set
import threading
import time
import json
import asyncio
try:
    from smartapi import SmartWebSocket
except ImportError:
    try:
        from SmartApi import SmartWebSocket
    except ImportError:
        from SmartApi.smartConnect import SmartWebSocket
from redis_config import redis_manager
from market_cache import market_data_cache
from price_batcher import price_batcher
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smartapi_websocket")

class SmartApiWebSocketManager:
    def __init__(self):
        self.api_key = os.getenv("ANGEL_API_KEY")
        self.client_code = os.getenv("ANGEL_CLIENT_CODE")
        self.password = os.getenv("ANGEL_PASSWORD")  # Not typically needed for WS feed token
        
        # We need feed token from the http login
        self.feed_token = None
        self.sws = None
        self.is_connected = False
        self.subscribed_tokens: Set[str] = set()
        self._stop_event = threading.Event()
        self.ws_thread = None
    
    def initialize(self, feed_token: str):
        """Initialize WebSocket with a valid feed token"""
        self.feed_token = feed_token
        if not self.feed_token:
            logger.error("Cannot initialize WebSocket without feed_token")
            return
            
        try:
            self.sws = SmartWebSocket(self.feed_token, self.client_code)
            self.sws._on_open = self._on_open
            self.sws._on_message = self._on_message
            self.sws._on_error = self._on_error
            self.sws._on_close = self._on_close
            
        except Exception as e:
            logger.error(f"Failed to initialize SmartWebSocket: {e}")

    def connect(self):
        """Start the WebSocket connection in a separate thread"""
        if not self.sws:
            logger.warning("WebSocket not initialized. Call initialize() first.")
            return

        if self.is_connected:
            return

        self._stop_event.clear()
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()
        logger.info("WebSocket thread started")

    def _run_websocket(self):
        try:
            self.sws.connect()
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            self.is_connected = False

    def subscribe(self, token_list: str):
        """Subscribe to a list of tokens. Format: 'nse_cm|2885&nse_cm|1594' """
        if not self.sws or not self.is_connected:
            logger.warning("WebSocket not connected. Cannot subscribe.")
            return
        
        # Track subscribed tokens
        tokens = token_list.split('&')
        for t in tokens:
            self.subscribed_tokens.add(t)
            
        # "mw" for market watch, "sfi" for simple feed
        # Trying "mw" mode
        try:
            self.sws.subscribe("mw", token_list)
            logger.info(f"Subscribed to: {token_list}")
        except Exception as e:
            logger.error(f"Subscription failed: {e}")

    def _on_open(self, ws):
        logger.info("SmartAPI WebSocket Connected ✅")
        self.is_connected = True
        
        # Resubscribe if we have tokens
        if self.subscribed_tokens:
            token_str = "&".join(self.subscribed_tokens)
            self.subscribe(token_str)

    def _on_message(self, ws, message):
        """Handle incoming tick data"""
        # Message is usually a binary list of dicts or predefined structure
        try:
            # Assuming SDK returns parsed list of ticks or single tick
            # Note: The official python SDK behavior depends on version.
            # We'll assume 'message' is the data payload.
            
            # Send to price batcher for aggregation
            # We need to ensure we're inside an event loop if price_batcher is async
            # But we are in a thread. So price_batcher.add_tick should probably be thread-safe or fire-and-forget.
            
            # Looking at typical SmartAPI response:
            # [{'name': 'sf', 'tk': '2885', 'e': 'nse_cm', 'ltp': '123.45', ...}]
            
            if isinstance(message, list):
                for tick in message:
                    self._process_tick(tick)
            else:
                self._process_tick(message)
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def _process_tick(self, tick):
        # Basic mapping
        if 'tk' in tick and 'ltp' in tick:
            symbol_token = tick.get('tk')
            ltp = float(tick.get('ltp', 0))
            # Send to batcher
            # Since this runs in a thread, and batcher expects async, 
            # we might need a bridge. 
            # ideally price_batcher has a sync method or we just update cache directly here.
            
            # Direct Redis update for speed (L1)
            # key = f"start:quote:{symbol_token}" 
            # But we need symbol name usually. 
            # For now, let's just log.
            pass
            
    def _on_error(self, ws, error):
        logger.error(f"WebSocket Error: {error}")

    def _on_close(self, ws):
        logger.warning("WebSocket Connection Closed")
        self.is_connected = False

    async def disconnect(self):
        """Stop the WebSocket connection"""
        self._stop_event.set()
        if self.sws:
            try:
                self.sws.close()
            except Exception:
                pass
        self.is_connected = False
        logger.info("WebSocket disconnected")

# Global instance
smartapi_ws_manager = SmartApiWebSocketManager()
