# -*- coding: utf-8 -*-
"""
Менеджер кеширования для оптимизации производительности бота
"""

import json
import logging
import asyncio
from typing import Any, Optional, Dict, List
from datetime import datetime, timedelta
from functools import wraps
import hashlib

logger = logging.getLogger(__name__)

class InMemoryCache:
    """Простой in-memory кеш для быстрого доступа к данным"""
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cleanup_task = None
        
    async def start(self):
        """Запуск фонового процесса очистки устаревших записей"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
    async def stop(self):
        """Остановка фонового процесса"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def _cleanup_loop(self):
        """Периодическая очистка устаревших записей"""
        while True:
            try:
                await asyncio.sleep(60)  # Проверка каждую минуту
                now = datetime.now()
                expired_keys = []
                
                for key, data in self._cache.items():
                    if data.get('expires_at') and data['expires_at'] < now:
                        expired_keys.append(key)
                
                for key in expired_keys:
                    del self._cache[key]
                    logger.debug(f"Cleaned up expired cache key: {key}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache cleanup: {e}")
    
    def get(self, key: str) -> Optional[Any]:
        """Получить значение из кеша"""
        if key not in self._cache:
            return None
            
        data = self._cache[key]
        
        # Проверка срока действия
        if data.get('expires_at') and data['expires_at'] < datetime.now():
            del self._cache[key]
            return None
            
        return data.get('value')
    
    def set(self, key: str, value: Any, ttl_seconds: int = 300):
        """Сохранить значение в кеш"""
        self._cache[key] = {
            'value': value,
            'expires_at': datetime.now() + timedelta(seconds=ttl_seconds),
            'created_at': datetime.now()
        }
    
    def delete(self, key: str) -> bool:
        """Удалить значение из кеша"""
        if key in self._cache:
            del self._cache[key]
            return True
        return False
    
    def clear_pattern(self, pattern: str):
        """Удалить все ключи, соответствующие паттерну"""
        keys_to_delete = [k for k in self._cache.keys() if pattern in k]
        for key in keys_to_delete:
            del self._cache[key]
        return len(keys_to_delete)
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику кеша"""
        return {
            'total_keys': len(self._cache),
            'memory_usage_estimate': sum(
                len(str(k)) + len(str(v)) 
                for k, v in self._cache.items()
            )
        }


# Глобальный экземпляр кеша
cache = InMemoryCache()


def cache_key(*args, **kwargs) -> str:
    """Генерация уникального ключа кеша на основе аргументов"""
    key_data = {
        'args': args,
        'kwargs': kwargs
    }
    key_str = json.dumps(key_data, sort_keys=True, default=str)
    return hashlib.md5(key_str.encode()).hexdigest()


def cached(ttl: int = 300, key_prefix: str = None):
    """
    Декоратор для кеширования результатов асинхронных функций
    
    Args:
        ttl: Время жизни кеша в секундах
        key_prefix: Префикс для ключа кеша
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Генерация ключа
            prefix = key_prefix or func.__name__
            cache_key_str = f"{prefix}:{cache_key(*args, **kwargs)}"
            
            # Попытка получить из кеша
            cached_value = cache.get(cache_key_str)
            if cached_value is not None:
                logger.debug(f"Cache hit for {cache_key_str}")
                return cached_value
            
            # Вызов оригинальной функции
            result = await func(*args, **kwargs)
            
            # Сохранение в кеш
            cache.set(cache_key_str, result, ttl)
            logger.debug(f"Cache set for {cache_key_str}, TTL: {ttl}s")
            
            return result
        return wrapper
    return decorator


def cached_sync(ttl: int = 300, key_prefix: str = None):
    """
    Декоратор для кеширования результатов синхронных функций
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Генерация ключа
            prefix = key_prefix or func.__name__
            cache_key_str = f"{prefix}:{cache_key(*args, **kwargs)}"
            
            # Попытка получить из кеша
            cached_value = cache.get(cache_key_str)
            if cached_value is not None:
                logger.debug(f"Cache hit for {cache_key_str}")
                return cached_value
            
            # Вызов оригинальной функции
            result = func(*args, **kwargs)
            
            # Сохранение в кеш
            cache.set(cache_key_str, result, ttl)
            logger.debug(f"Cache set for {cache_key_str}, TTL: {ttl}s")
            
            return result
        return wrapper
    return decorator


# Специализированные функции кеширования

def cache_user_data(user_id: int, data: Dict[str, Any], ttl: int = 300):
    """Кешировать данные пользователя"""
    cache.set(f"user:{user_id}", data, ttl)


def get_cached_user_data(user_id: int) -> Optional[Dict[str, Any]]:
    """Получить кешированные данные пользователя"""
    return cache.get(f"user:{user_id}")


def cache_persona_data(persona_id: int, data: Dict[str, Any], ttl: int = 600):
    """Кешировать данные персоны"""
    cache.set(f"persona:{persona_id}", data, ttl)


def get_cached_persona_data(persona_id: int) -> Optional[Dict[str, Any]]:
    """Получить кешированные данные персоны"""
    return cache.get(f"persona:{persona_id}")


def cache_menu_keyboard(user_id: int, menu_type: str, keyboard_data: Any, ttl: int = 1800):
    """Кешировать клавиатуру меню"""
    cache.set(f"menu:{user_id}:{menu_type}", keyboard_data, ttl)


def get_cached_menu_keyboard(user_id: int, menu_type: str) -> Optional[Any]:
    """Получить кешированную клавиатуру меню"""
    return cache.get(f"menu:{user_id}:{menu_type}")


def invalidate_user_cache(user_id: int):
    """Инвалидировать весь кеш пользователя"""
    patterns = [
        f"user:{user_id}",
        f"menu:{user_id}",
        f"personas:{user_id}"
    ]
    for pattern in patterns:
        cache.clear_pattern(pattern)
    logger.info(f"Invalidated all cache for user {user_id}")


def invalidate_persona_cache(persona_id: int):
    """Инвалидировать кеш персоны"""
    cache.delete(f"persona:{persona_id}")
    logger.info(f"Invalidated cache for persona {persona_id}")


# Кеширование промптов и шаблонов

class PromptCache:
    """Специальный кеш для системных промптов"""
    
    def __init__(self):
        self._prompts: Dict[str, str] = {}
        
    def get_system_prompt(self, persona_id: int, template: str, **kwargs) -> str:
        """Получить или сгенерировать системный промпт"""
        cache_key = f"prompt:{persona_id}:{hash(frozenset(kwargs.items()))}"
        
        if cache_key in self._prompts:
            return self._prompts[cache_key]
        
        # Генерация промпта (предполагается, что template.format существует)
        prompt = template.format(**kwargs)
        self._prompts[cache_key] = prompt
        
        # Ограничение размера кеша
        if len(self._prompts) > 100:
            # Удаляем старейший элемент
            oldest = next(iter(self._prompts))
            del self._prompts[oldest]
        
        return prompt


prompt_cache = PromptCache()


# Статистика и мониторинг

async def log_cache_stats():
    """Логировать статистику кеша"""
    stats = cache.get_stats()
    logger.info(f"Cache stats: {stats}")
    

# Инициализация при импорте модуля
async def init_cache():
    """Инициализация кеш-менеджера"""
    await cache.start()
    logger.info("Cache manager initialized")


async def shutdown_cache():
    """Корректное завершение работы кеша"""
    await cache.stop()
    logger.info("Cache manager shut down")
