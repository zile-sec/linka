"""
Redis client for caching wallet balances and provider data.
Provides fast reads during slow external API responses.
"""

import json
import logging
import os
from typing import Any, Optional
from redis import Redis, ConnectionError as RedisConnectionError

logger = logging.getLogger(__name__)


class RedisClient:
    """Singleton Redis client with wallet-specific caching helpers."""
    
    _instance: Optional["RedisClient"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        try:
            self.client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self.client.ping()
            logger.info("redis connected: %s", redis_url)
        except (RedisConnectionError, Exception) as e:
            logger.error("redis connection failed: %s", e)
            self.client = None
        
        self._initialized = True
    
    def is_available(self) -> bool:
        """Check if Redis is reachable."""
        if not self.client:
            return False
        try:
            self.client.ping()
            return True
        except Exception:
            return False
    
    # ===================================================================
    #  WALLET BALANCE CACHING
    # ===================================================================
    
    def cache_balance(self, user_id: str, balance: float, ttl: int = 60) -> bool:
        """Cache a user's wallet balance for *ttl* seconds."""
        if not self.client:
            return False
        try:
            key = f"wallet:balance:{user_id}"
            self.client.setex(key, ttl, str(balance))
            return True
        except Exception as e:
            logger.warning("cache_balance failed: %s", e)
            return False
    
    def get_cached_balance(self, user_id: str) -> Optional[float]:
        """Retrieve cached balance or None if not found/expired."""
        if not self.client:
            return None
        try:
            key = f"wallet:balance:{user_id}"
            val = self.client.get(key)
            return float(val) if val else None
        except Exception as e:
            logger.warning("get_cached_balance failed: %s", e)
            return None
    
    def invalidate_balance(self, user_id: str) -> bool:
        """Delete cached balance (call after transaction)."""
        if not self.client:
            return False
        try:
            key = f"wallet:balance:{user_id}"
            self.client.delete(key)
            return True
        except Exception:
            return False
    
    # ===================================================================
    #  EXTERNAL PROVIDER DATA CACHING
    # ===================================================================
    
    def cache_external_balance(
        self, user_id: str, provider: str, account_id: str, balance: float, ttl: int = 300
    ) -> bool:
        """Cache external provider balance (MTN, Airtel, etc.)."""
        if not self.client:
            return False
        try:
            key = f"ext:balance:{user_id}:{provider}:{account_id}"
            self.client.setex(key, ttl, str(balance))
            return True
        except Exception as e:
            logger.warning("cache_external_balance failed: %s", e)
            return False
    
    def get_cached_external_balance(
        self, user_id: str, provider: str, account_id: str
    ) -> Optional[float]:
        """Retrieve cached external balance."""
        if not self.client:
            return None
        try:
            key = f"ext:balance:{user_id}:{provider}:{account_id}"
            val = self.client.get(key)
            return float(val) if val else None
        except Exception:
            return None
    
    # ===================================================================
    #  TRANSACTION STATUS CACHING (for polling)
    # ===================================================================
    
    def cache_tx_status(self, tx_ref: str, status: str, data: dict, ttl: int = 600) -> bool:
        """Cache transaction status from external provider."""
        if not self.client:
            return False
        try:
            key = f"tx:status:{tx_ref}"
            payload = {"status": status, "data": data}
            self.client.setex(key, ttl, json.dumps(payload))
            return True
        except Exception as e:
            logger.warning("cache_tx_status failed: %s", e)
            return False
    
    def get_cached_tx_status(self, tx_ref: str) -> Optional[dict]:
        """Retrieve cached transaction status."""
        if not self.client:
            return None
        try:
            key = f"tx:status:{tx_ref}"
            val = self.client.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None
    
    # ===================================================================
    #  RATE LIMITING HELPERS
    # ===================================================================
    
    def check_rate_limit(
        self, user_id: str, action: str, limit: int, window: int
    ) -> tuple[bool, int]:
        """
        Check if user has exceeded rate limit for an action.
        Returns (allowed, remaining_requests).
        """
        if not self.client:
            return (True, limit)  # Fail open if Redis down
        try:
            key = f"rate:{action}:{user_id}"
            current = self.client.get(key)
            if current is None:
                self.client.setex(key, window, "1")
                return (True, limit - 1)
            
            count = int(current)
            if count >= limit:
                return (False, 0)
            
            self.client.incr(key)
            return (True, limit - count - 1)
        except Exception:
            return (True, limit)  # Fail open
    
    # ===================================================================
    #  UNIFIED BALANCE CACHE
    # ===================================================================
    
    def cache_unified_balance(self, user_id: str, data: dict, ttl: int = 120) -> bool:
        """Cache the aggregated (internal + external) balance."""
        if not self.client:
            return False
        try:
            key = f"wallet:unified:{user_id}"
            self.client.setex(key, ttl, json.dumps(data))
            return True
        except Exception as e:
            logger.warning("cache_unified_balance failed: %s", e)
            return False
    
    def get_cached_unified_balance(self, user_id: str) -> Optional[dict]:
        """Retrieve cached unified balance."""
        if not self.client:
            return None
        try:
            key = f"wallet:unified:{user_id}"
            val = self.client.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None


# Singleton getter
_redis_client_instance: Optional[RedisClient] = None

def get_redis_client() -> RedisClient:
    """Get or create the singleton Redis client."""
    global _redis_client_instance
    if _redis_client_instance is None:
        _redis_client_instance = RedisClient()
    return _redis_client_instance
