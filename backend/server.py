"""
Stock Screener Backend (Firebase + Redis)
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import asyncio
import logging
from dotenv import load_dotenv
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s - %(message)s'
)

# Suppress noisy loggers
logging.getLogger("websocket").setLevel(logging.ERROR)
logging.getLogger("smartapi_websocket").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)

# Import Routers
from routers import auth, chat, stocks, market, screener, candles, portfolio, notifications

# Import Lifecycle services
from instrument_master import init_instruments
from smartapi_websocket import smartapi_ws_manager
from price_batcher import price_batcher

# Import Storage/Cache services
from redis_config import redis_manager
from firebase_config import initialize_firebase, get_firestore
from market_cache import market_data_cache
from market_service import market_service
from user_service import user_service
from finnhub_service import finnhub_service
# Notification service might need update if it used timescale
from notification_service import notification_service

# Load environment variables
env_path = Path(__file__).resolve().parent / '.env'
load_dotenv(env_path)

# WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.client_subscriptions: dict[WebSocket, set] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.client_subscriptions[websocket] = set()
        print(f"[WS] Client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.client_subscriptions:
            del self.client_subscriptions[websocket]
        print(f"[WS] Client disconnected. Total: {len(self.active_connections)}")

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)
    
    async def broadcast_price_updates(self, updates: dict):
        if not updates:
            return
        
        message = {
            "type": "delta",
            "updates": [
                {
                    "symbol": data.get("symbol"),
                    "ltp": data.get("ltp"),
                    "prev_ltp": data.get("prev_ltp"),
                    "timestamp": data.get("timestamp")
                }
                for token, data in updates.items()
            ]
        }
        await self.broadcast(message)

manager = ConnectionManager()

# WebSocket Callbacks
async def on_batch_ready(batch: dict):
    await manager.broadcast_price_updates(batch)

price_batcher.on_batch_ready = on_batch_ready

async def on_price_update(token: str, price_data: dict):
    await price_batcher.add_update(token, price_data)

smartapi_ws_manager.on_price_update = on_price_update

# Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    print("\n🚀 Starting Stock Screener Backend (Firebase + Redis)...")
    
    # 0. Connect to Redis (Real-time Cache)
    await redis_manager.connect()
    
    # 1. Connect to Firebase (Persistent Storage)
    if initialize_firebase():
        print("✅ Firebase initialized")
    else:
        print("❌ Firebase initialization failed")
    
    # 2. Initialize Instruments (Background)
    asyncio.create_task(init_instruments())

    # 3. Start WebSocket Services
    await price_batcher.start()
    try:
        smartapi_ws_manager.connect()
    except Exception as e:
        pass
    
    # 4. Schedule Daily Market Snapshots
    try:
        from snapshot_scheduler import start_scheduler
        start_scheduler()
        print("✅ Market snapshot scheduler started (09:00 AM & 03:45 PM)")
    except Exception as e:
        print(f"⚠️  Market snapshot scheduler failed: {e}")
    
    # 5. Start Cache Warming
    try:
        market_data_cache.start_cache_warming()
        print("✅ Cache warming started")
    except Exception as e:
        print(f"⚠️  Cache warming failed: {e}")
    
    # 6. Background task: News Monitor
    from notification_service import initialize_notification_service
    initialize_notification_service(user_service)

    async def news_monitor():
        print("📰 News monitor started")
        while True:
            await asyncio.sleep(300) 
            try:
                new_items = await asyncio.to_thread(finnhub_service.check_for_new_news)
                if new_items:
                    for item in new_items:
                        # 1. Update in-app via WebSocket
                        await manager.broadcast({
                            "type": "news_alert",
                            "data": item
                        })
                        # 2. Send Push Notification
                        await notification_service.broadcast_news_to_all_users(item)
            except Exception as e:
                print(f"⚠️ News monitor error: {e}")

    asyncio.create_task(news_monitor())
    
    print("✅ Server startup complete!\n")

    yield  # Application runs

    # --- SHUTDOWN ---
    print("\n🛑 Shutting down...")
    await price_batcher.stop()
    await smartapi_ws_manager.disconnect()
    await redis_manager.close()
    print("✅ Shutdown complete\n")

# Initialize FastAPI App
app = FastAPI(
    title="AI Stock Screener API",
    version="2.0.0",
    description="Firebase & Redis powered stock screening platform",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(stocks.router)
app.include_router(market.router)
app.include_router(screener.router)
app.include_router(candles.router)
app.include_router(portfolio.router)
app.include_router(notifications.router)

# Health Check
@app.get("/")
async def root():
    return {
        "message": "AI Stock Screener API v2.0",
        "status": "running",
        "database": "Firebase + Redis",
        "features": {
            "auth": "✅ JWT Authentication",
            "market_data": "✅ Real-time from Angel One",
            "database": "✅ Firebase Firestore",
            "caching": "✅ Redis",
            "timescaledb": "❌ Removed",
            "mongodb": "❌ Removed"
        }
    }

@app.get("/health")
async def health():
    health_status = {
        "status": "healthy",
        "firebase": "unknown",
        "redis": "disconnected",
        "instruments": "not_loaded"
    }
    
    try:
        if get_firestore():
            health_status["firebase"] = "connected"
    except:
        health_status["firebase"] = "disconnected"

    if redis_manager.is_connected:
        health_status["redis"] = "connected"
    
    return health_status

# WebSocket Connection Handler
async def handle_websocket_connection(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action") or data.get("type")
            
            if action == "subscribe":
                symbols = data.get("symbols", [])
                if websocket in manager.client_subscriptions:
                    manager.client_subscriptions[websocket].update(symbols)
                await websocket.send_json({
                    "type": "subscription_confirmed",
                    "symbols": list(manager.client_subscriptions[websocket])
                })
            
            elif action == "unsubscribe":
                symbols = data.get("symbols", [])
                if websocket in manager.client_subscriptions:
                    manager.client_subscriptions[websocket].difference_update(symbols)
                await websocket.send_json({
                    "type": "unsubscription_confirmed",
                    "symbols": list(manager.client_subscriptions[websocket])
                })
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        manager.disconnect(websocket)

# WebSocket Endpoints
@app.websocket("/ws")
async def websocket_endpoint_root(websocket: WebSocket):
    await handle_websocket_connection(websocket)

@app.websocket("/ws/market-data")
async def websocket_endpoint_market(websocket: WebSocket):
    await handle_websocket_connection(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=True,
        log_level="info"
    )
