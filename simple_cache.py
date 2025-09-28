# -*- coding: utf-8 -*-
"""
Простая система кеширования для ускорения отклика бота
"""
import time
import logging
from typing import Any, Dict, Tuple
from functools import wraps

logger = logging.getLogger(__name__)

class SimpleCache:
    """Простой кеш с TTL для хранения результатов запросов"""
    
    def __init__(self, ttl_seconds: int = 60):
        self.cache: Dict[Any, Tuple[Any, float]] = {}
        self.ttl = ttl_seconds
        
    def get(self, key: Any) -> Any:
        """Получить значение из кеша"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                logger.debug(f"Cache hit for key: {key}")
                return value
            else:
                # Удаляем устаревшее значение
                del self.cache[key]
                logger.debug(f"Cache expired for key: {key}")
        return None
    
    def set(self, key: Any, value: Any):
        """Сохранить значение в кеш"""
        self.cache[key] = (value, time.time())
        logger.debug(f"Cached value for key: {key}")
        
    def clear(self):
        """Очистить весь кеш"""
        self.cache.clear()
        logger.debug("Cache cleared")

# Глобальные экземпляры кеша для разных типов данных
user_cache = SimpleCache(ttl_seconds=300)  # 5 минут для данных пользователя
persona_cache = SimpleCache(ttl_seconds=600)  # 10 минут для персон

def cache_user_data(func):
    """Декоратор для кеширования данных пользователя"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Извлекаем user_id из аргументов
        update = args[0] if args else None
        if hasattr(update, 'effective_user'):
            user_id = update.effective_user.id if update.effective_user else None
        elif hasattr(update, 'from_user'):
            user_id = update.from_user.id if update.from_user else None
        else:
            user_id = None
            
        if user_id:
            cache_key = f"{func.__name__}:{user_id}"
            cached = user_cache.get(cache_key)
            if cached is not None:
                return cached
                
        result = await func(*args, **kwargs)
        
        if user_id and result is not None:
            user_cache.set(cache_key, result)
            
        return result
    return wrapper

def invalidate_user_cache(user_id: int):
    """Инвалидация кеша для конкретного пользователя"""
    keys_to_remove = []
    for key in user_cache.cache.keys():
        if str(user_id) in str(key):
            keys_to_remove.append(key)
    
    for key in keys_to_remove:
        del user_cache.cache[key]
        logger.debug(f"Invalidated cache for key: {key}")
