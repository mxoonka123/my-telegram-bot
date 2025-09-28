# -*- coding: utf-8 -*-
"""
Оптимизированные обработчики для Telegram бота
Внедрено кеширование и оптимизация запросов к БД
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction, ParseMode

from optimization.cache_manager import cache, cached, cache_user_data, get_cached_user_data
from optimization.db_optimized import (
    get_user_with_minimal_data,
    get_personas_list_optimized,
    get_active_chat_bot_optimized,
    check_user_access_optimized
)
import config
from db import get_db
from utils import escape_markdown_v2

logger = logging.getLogger(__name__)

# Кеш для клавиатур
KEYBOARD_CACHE = {}

def get_cached_keyboard(key: str, ttl: int = None) -> Optional[InlineKeyboardMarkup]:
    """Получить клавиатуру из кеша"""
    return cache.get(f"keyboard:{key}")


def set_cached_keyboard(key: str, keyboard: InlineKeyboardMarkup, ttl: int = None):
    """Сохранить клавиатуру в кеш"""
    ttl = ttl or config.CACHE_TTL_MENU
    cache.set(f"keyboard:{key}", keyboard, ttl)


async def get_user_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """
    Оптимизированное получение главного меню
    Кешируется на 30 минут
    """
    cache_key = f"menu_main_{user_id}"
    
    # Проверяем кеш
    cached = get_cached_keyboard(cache_key)
    if cached:
        return cached
    
    keyboard = [
        [
            InlineKeyboardButton("профиль", callback_data="show_profile"),
            InlineKeyboardButton("мои личности", callback_data="show_mypersonas")
        ],
        [
            InlineKeyboardButton("пополнить", callback_data="buycredits_open"),
            InlineKeyboardButton("помощь", callback_data="show_help")
        ]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    set_cached_keyboard(cache_key, markup, config.CACHE_TTL_MENU)
    
    return markup


async def get_personas_keyboard_optimized(user_id: int) -> InlineKeyboardMarkup:
    """
    Оптимизированное получение списка персон
    Использует кеш и минимальные запросы к БД
    """
    cache_key = f"personas_kb_{user_id}"
    
    # Проверяем кеш
    cached = get_cached_keyboard(cache_key)
    if cached:
        return cached
    
    with get_db() as db:
        personas = await get_personas_list_optimized(db, user_id)
    
    if not personas:
        keyboard = [[InlineKeyboardButton("создать личность", callback_data="create_persona")]]
    else:
        keyboard = []
        for p in personas:
            status = "🟢" if p.get('bot_status') == 'active' else "⚪"
            btn_text = f"{status} {p['name']}"
            keyboard.append([
                InlineKeyboardButton(btn_text, callback_data=f"select_persona_{p['id']}")
            ])
        keyboard.append([
            InlineKeyboardButton("➕ создать новую", callback_data="create_persona")
        ])
    
    keyboard.append([
        InlineKeyboardButton("⬅️ назад", callback_data="show_menu")
    ])
    
    markup = InlineKeyboardMarkup(keyboard)
    set_cached_keyboard(cache_key, markup, config.CACHE_TTL_PERSONA)
    
    return markup


async def handle_callback_optimized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Оптимизированный обработчик callback-запросов
    Использует кеш и минимальные запросы к БД
    """
    query = update.callback_query
    if not query:
        return
    
    user_id = query.from_user.id
    data = query.data
    
    # Используем кешированные данные пользователя
    user_data = get_cached_user_data(user_id)
    if not user_data:
        with get_db() as db:
            user_data = await get_user_with_minimal_data(db, user_id)
            if user_data:
                cache_user_data(user_id, user_data)
    
    try:
        # Обработка разных callback'ов
        if data == "show_menu":
            keyboard = await get_user_menu_keyboard(user_id)
            text = "панель управления"
            
            if query.message.text != text or query.message.reply_markup != keyboard:
                await query.edit_message_text(
                    text,
                    reply_markup=keyboard,
                    parse_mode=None
                )
            else:
                await query.answer()
                
        elif data == "show_mypersonas":
            keyboard = await get_personas_keyboard_optimized(user_id)
            
            # Получаем количество персон из кеша
            with get_db() as db:
                from optimization.db_optimized import get_user_personas_count
                count = await get_user_personas_count(db, user_id)
            
            text = f"твои личности ({count}/{config.PAID_PERSONA_LIMIT})"
            
            await query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode=None
            )
            
        elif data == "show_profile":
            # Профиль из кеша
            if user_data:
                credits = user_data.get('credits', 0)
                username = user_data.get('username', f'user_{user_id}')
                
                text = f"👤 профиль @{username}\n\n💳 баланс: {credits:.2f} кредитов"
                
                keyboard = [
                    [InlineKeyboardButton("пополнить кредиты", callback_data="buycredits_open")],
                    [InlineKeyboardButton("⬅️ назад", callback_data="show_menu")]
                ]
                
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=None
                )
            else:
                await query.answer("ошибка загрузки профиля", show_alert=True)
                
        elif data.startswith("select_persona_"):
            try:
                persona_id = int(data.replace("select_persona_", ""))
                # Здесь можно добавить логику выбора персоны
                await query.answer(f"выбрана личность #{persona_id}")
            except ValueError:
                await query.answer("ошибка", show_alert=True)
                
        else:
            # Для остальных callback'ов используем стандартный обработчик
            await query.answer()
            
    except Exception as e:
        logger.error(f"Error in optimized callback handler: {e}", exc_info=True)
        await query.answer("произошла ошибка", show_alert=True)


def invalidate_user_keyboards(user_id: int):
    """Инвалидировать все клавиатуры пользователя"""
    patterns = [
        f"keyboard:menu_main_{user_id}",
        f"keyboard:personas_kb_{user_id}",
        f"keyboard:profile_{user_id}"
    ]
    for pattern in patterns:
        cache.delete(pattern)


# Оптимизированная функция для парсинга ответов LLM
def parse_llm_response_optimized(text: str) -> List[str]:
    """
    Упрощенный и быстрый парсинг ответов от LLM
    Без множественных regex fallback
    """
    import json
    
    # Быстрая очистка markdown
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json\n"):
            text = text[5:]
    
    # Попытка парсинга JSON
    try:
        parsed = json.loads(text)
        
        # Основной формат
        if isinstance(parsed, dict) and "response" in parsed:
            resp = parsed["response"]
            if isinstance(resp, list):
                return [str(x).strip() for x in resp if x]
            if isinstance(resp, str):
                return [resp.strip()] if resp.strip() else []
        
        # Альтернативный формат - просто список
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if x]
            
    except json.JSONDecodeError:
        pass
    
    # Простой fallback - разбить по строкам
    lines = text.strip().split('\n')
    return [line.strip() for line in lines if line.strip()]


# HTTP клиент с connection pooling
import httpx

# Глобальный клиент для переиспользования соединений
http_client = httpx.AsyncClient(
    limits=httpx.Limits(
        max_keepalive_connections=20,
        max_connections=50,
        keepalive_expiry=30
    ),
    timeout=httpx.Timeout(config.HTTP_CLIENT_TIMEOUT)
)


async def send_to_api_optimized(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Оптимизированная отправка запросов к API
    Использует connection pooling
    """
    try:
        response = await http_client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"API error: {e.response.status_code}")
        raise
    except Exception as e:
        logger.error(f"Request failed: {e}")
        raise


# Функция для прогрева кеша при старте
async def warm_up_cache():
    """Прогрев кеша для активных пользователей"""
    logger.info("Starting cache warm-up...")
    
    with get_db() as db:
        # Получаем недавно активных пользователей
        from datetime import datetime, timezone, timedelta
        from db import User
        
        recent_users = db.query(User.telegram_id).filter(
            User.updated_at > datetime.now(timezone.utc) - timedelta(hours=2)
        ).limit(50).all()
        
        for user_id, in recent_users:
            try:
                # Предзагружаем меню
                await get_user_menu_keyboard(user_id)
                
                # Предзагружаем данные пользователя  
                user_data = await get_user_with_minimal_data(db, user_id)
                if user_data:
                    cache_user_data(user_id, user_data)
                    
            except Exception as e:
                logger.error(f"Failed to warm cache for user {user_id}: {e}")
    
    logger.info(f"Cache warmed up for {len(recent_users)} users")


# Мониторинг производительности
class PerformanceMonitor:
    """Простой мониторинг производительности"""
    
    def __init__(self):
        self.timings = {}
        
    async def measure(self, name: str, func, *args, **kwargs):
        """Измерить время выполнения функции"""
        import time
        start = time.perf_counter()
        
        try:
            result = await func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            
            if name not in self.timings:
                self.timings[name] = []
            self.timings[name].append(elapsed)
            
            # Логируем медленные операции
            if elapsed > 1.0:
                logger.warning(f"Slow operation {name}: {elapsed:.2f}s")
                
            return result
        except Exception as e:
            elapsed = time.perf_counter() - start
            logger.error(f"Operation {name} failed after {elapsed:.2f}s: {e}")
            raise
    
    def get_stats(self) -> Dict[str, Dict[str, float]]:
        """Получить статистику"""
        stats = {}
        for name, times in self.timings.items():
            if times:
                stats[name] = {
                    'count': len(times),
                    'avg': sum(times) / len(times),
                    'min': min(times),
                    'max': max(times)
                }
        return stats


# Глобальный монитор производительности
perf_monitor = PerformanceMonitor()


# Функция для логирования статистики
async def log_performance_stats():
    """Логировать статистику производительности"""
    stats = perf_monitor.get_stats()
    if stats:
        logger.info("Performance stats:")
        for name, data in stats.items():
            logger.info(
                f"  {name}: count={data['count']}, "
                f"avg={data['avg']:.3f}s, "
                f"min={data['min']:.3f}s, "
                f"max={data['max']:.3f}s"
            )
    
    # Статистика кеша
    cache_stats = cache.get_stats()
    logger.info(f"Cache stats: {cache_stats}")
