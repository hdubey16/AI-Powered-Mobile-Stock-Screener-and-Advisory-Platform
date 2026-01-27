import os
import redis.asyncio as redis
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class RedisManager:
    """
    Async Redis connection manager
    """
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis: redis.Redis = None
        self.is_connected = False

    async def connect(self):
        """Initialize Redis connection"""
        try:
            # Use environment variables for Render compatibility
            host = os.getenv("REDIS_HOST", "localhost")
            port = os.getenv("REDIS_PORT", "6379")
            password = os.getenv("REDIS_PASSWORD", "")
            
            # Construct URL if not provided directly
            if not self.redis_url or "localhost" in self.redis_url:
                if password:
                    self.redis_url = f"redis://:{password}@{host}:{port}/0"
                else:
                    self.redis_url = f"redis://{host}:{port}/0"

            print(f"Connecting to Redis at {host}:{port}...")
            
            self.redis = redis.from_url(
                self.redis_url, 
                encoding="utf-8", 
                decode_responses=True,
                socket_timeout=5.0
            )
            # Test connection
            await self.redis.ping()
            self.is_connected = True
            print("✅ Redis connection established")
            return True
        except Exception as e:
            print(f"⚠️  Redis connection failed. Falling back to in-memory cache.")
            # logger.error(f"Redis error details: {e}")
            self.is_connected = False
            return False

    async def close(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
            self.is_connected = False
            print("Redis connection closed")

    async def get(self, key: str):
        if not self.is_connected or not self.redis:
            return None
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.error(f"Redis get error for {key}: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int = 300):
        if not self.is_connected or not self.redis:
            return False
        try:
            return await self.redis.set(key, value, ex=ttl)
        except Exception as e:
            logger.error(f"Redis set error for {key}: {e}")
            return False
            
    async def delete(self, key: str):
        if not self.is_connected or not self.redis:
            return False
        try:
            return await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Redis delete error for {key}: {e}")
            return False

    async def exists(self, key: str):
        if not self.is_connected or not self.redis:
            return False
        try:
            return await self.redis.exists(key)
        except Exception as e:
            logger.error(f"Redis exists error for {key}: {e}")
            return False

    async def get_keys(self, pattern: str):
        if not self.is_connected or not self.redis:
            return []
        try:
            return await self.redis.keys(pattern)
        except Exception as e:
            logger.error(f"Redis keys error: {e}")
            return []

# Global instance
redis_manager = RedisManager()
