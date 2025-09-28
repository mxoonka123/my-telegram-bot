# -*- coding: utf-8 -*-
"""
Оптимизированные функции для работы с БД
Заменяют тяжелые запросы на более эффективные
"""

import logging
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_, func
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Импортируем модели из основного модуля
from db import (
    User, PersonaConfig, BotInstance, ChatBotInstance, 
    ChatContext, ApiKey
)
from optimization.cache_manager import cached, cached_sync, cache


@cached(ttl=300)
async def get_user_with_minimal_data(db: Session, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Получить только базовые данные пользователя без лишних joins
    """
    user = db.query(User).filter(User.telegram_id == user_id).first()
    if not user:
        return None
    
    return {
        'id': user.id,
        'telegram_id': user.telegram_id,
        'username': user.username,
        'credits': user.credits,
        'is_subscribed': user.is_subscribed,
        'persona_limit': user.persona_limit
    }


@cached(ttl=600)
async def get_user_personas_count(db: Session, user_id: int) -> int:
    """
    Получить только количество персон пользователя
    """
    user = db.query(User).filter(User.telegram_id == user_id).first()
    if not user:
        return 0
    
    count = db.query(func.count(PersonaConfig.id))\
        .filter(PersonaConfig.owner_id == user.id)\
        .scalar()
    
    return count or 0


@cached(ttl=600) 
async def get_personas_list_optimized(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """
    Получить список персон без лишних данных
    """
    user = db.query(User).filter(User.telegram_id == user_id).first()
    if not user:
        return []
    
    # Только нужные поля, без joins
    personas = db.query(
        PersonaConfig.id,
        PersonaConfig.name,
        PersonaConfig.description
    ).filter(
        PersonaConfig.owner_id == user.id
    ).order_by(PersonaConfig.name).all()
    
    result = []
    for p in personas:
        # Отдельный запрос для bot_instance если нужно
        bot_instance = db.query(
            BotInstance.id,
            BotInstance.telegram_username,
            BotInstance.status
        ).filter(
            BotInstance.persona_config_id == p.id
        ).first()
        
        result.append({
            'id': p.id,
            'name': p.name,
            'description': p.description,
            'has_bot': bot_instance is not None,
            'bot_username': bot_instance.telegram_username if bot_instance else None,
            'bot_status': bot_instance.status if bot_instance else None
        })
    
    return result


async def get_active_chat_bot_optimized(
    db: Session, 
    chat_id: str, 
    bot_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Оптимизированное получение активного бота в чате
    """
    # Сначала проверяем кеш
    cache_key = f"active_bot:{chat_id}:{bot_id or 'main'}"
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data
    
    # Базовый запрос без лишних joins
    query = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == str(chat_id),
        ChatBotInstance.active == True
    )
    
    if bot_id:
        query = query.join(BotInstance).filter(
            BotInstance.telegram_bot_id == str(bot_id)
        )
    
    chat_bot = query.first()
    
    if not chat_bot:
        return None
    
    # Минимально необходимые данные
    result = {
        'id': chat_bot.id,
        'chat_id': chat_bot.chat_id,
        'bot_instance_id': chat_bot.bot_instance_id,
        'current_mood': chat_bot.current_mood,
        'is_muted': chat_bot.is_muted
    }
    
    # Кешируем на 5 минут
    cache.set(cache_key, result, ttl=300)
    
    return result


def bulk_get_personas(db: Session, persona_ids: List[int]) -> Dict[int, PersonaConfig]:
    """
    Получить несколько персон одним запросом
    """
    if not persona_ids:
        return {}
    
    personas = db.query(PersonaConfig).filter(
        PersonaConfig.id.in_(persona_ids)
    ).all()
    
    return {p.id: p for p in personas}


def get_context_messages_optimized(
    db: Session, 
    chat_bot_instance_id: int, 
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Оптимизированное получение контекста чата
    """
    messages = db.query(
        ChatContext.message_order,
        ChatContext.role,
        ChatContext.content,
        ChatContext.timestamp
    ).filter(
        ChatContext.chat_bot_instance_id == chat_bot_instance_id
    ).order_by(
        ChatContext.message_order.desc()
    ).limit(limit).all()
    
    # Возвращаем в правильном порядке
    return [
        {
            'role': m.role,
            'content': m.content,
            'timestamp': m.timestamp,
            'order': m.message_order
        }
        for m in reversed(messages)
    ]


async def check_user_access_optimized(
    db: Session,
    user_id: int,
    bot_instance_id: int
) -> bool:
    """
    Быстрая проверка доступа пользователя к боту
    """
    # Проверяем кеш
    cache_key = f"access:{user_id}:{bot_instance_id}"
    cached_access = cache.get(cache_key)
    if cached_access is not None:
        return cached_access
    
    bot_instance = db.query(
        BotInstance.access_level,
        BotInstance.whitelisted_users_json,
        User.telegram_id
    ).join(
        User, BotInstance.owner_id == User.id
    ).filter(
        BotInstance.id == bot_instance_id
    ).first()
    
    if not bot_instance:
        return False
    
    access_level = bot_instance.access_level
    owner_tg_id = bot_instance.telegram_id
    
    # Проверка доступа
    allowed = False
    
    if access_level == 'public':
        allowed = True
    elif user_id == owner_tg_id:
        allowed = True
    elif access_level == 'whitelist':
        import json
        try:
            whitelist = json.loads(bot_instance.whitelisted_users_json or '[]')
            allowed = str(user_id) in whitelist or user_id in whitelist
        except:
            allowed = False
    
    # Кешируем результат на 10 минут
    cache.set(cache_key, allowed, ttl=600)
    
    return allowed


class OptimizedDBOperations:
    """
    Класс с оптимизированными операциями БД
    """
    
    @staticmethod
    def batch_create_context_messages(
        db: Session,
        chat_bot_instance_id: int,
        messages: List[Tuple[str, str]]  # [(role, content), ...]
    ):
        """
        Пакетное создание сообщений контекста
        """
        # Получаем текущий максимальный order
        max_order = db.query(func.max(ChatContext.message_order))\
            .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
            .scalar() or 0
        
        # Создаем объекты
        new_messages = []
        for i, (role, content) in enumerate(messages, 1):
            new_messages.append(ChatContext(
                chat_bot_instance_id=chat_bot_instance_id,
                message_order=max_order + i,
                role=role,
                content=content,
                timestamp=datetime.now(timezone.utc)
            ))
        
        # Пакетная вставка
        db.bulk_save_objects(new_messages)
        db.commit()
        
        return len(new_messages)
    
    @staticmethod
    def cleanup_old_context(
        db: Session,
        chat_bot_instance_id: int,
        keep_last: int = 100
    ) -> int:
        """
        Удаление старых сообщений контекста
        """
        # Получаем ID сообщений для удаления
        subquery = db.query(ChatContext.id)\
            .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
            .order_by(ChatContext.message_order.desc())\
            .limit(keep_last)\
            .subquery()
        
        # Удаляем все, кроме последних keep_last
        deleted = db.query(ChatContext)\
            .filter(
                ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                ~ChatContext.id.in_(select(subquery))
            ).delete(synchronize_session=False)
        
        db.commit()
        
        return deleted
    
    @staticmethod
    async def get_or_create_user_optimized(
        db: Session,
        telegram_id: int,
        username: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Оптимизированное получение или создание пользователя
        """
        # Проверяем кеш
        cached_user = cache.get(f"user:{telegram_id}")
        if cached_user:
            return cached_user
        
        # Ищем в БД
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        
        if not user:
            # Создаем нового
            user = User(
                telegram_id=telegram_id,
                username=username or f"user_{telegram_id}",
                created_at=datetime.now(timezone.utc)
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        elif username and user.username != username:
            # Обновляем username
            user.username = username
            db.commit()
        
        # Формируем результат
        result = {
            'id': user.id,
            'telegram_id': user.telegram_id,
            'username': user.username,
            'credits': user.credits,
            'is_new': user.created_at.replace(tzinfo=timezone.utc) > 
                     datetime.now(timezone.utc) - timedelta(seconds=5)
        }
        
        # Кешируем на 5 минут
        cache.set(f"user:{telegram_id}", result, ttl=300)
        
        return result


# Функции для предзагрузки данных в кеш

async def preload_user_data(db: Session, user_id: int):
    """
    Предзагрузка всех данных пользователя в кеш
    """
    # Базовые данные пользователя
    user_data = await get_user_with_minimal_data(db, user_id)
    if not user_data:
        return
    
    # Список персон
    personas = await get_personas_list_optimized(db, user_id)
    
    # Кешируем все
    cache.set(f"user:{user_id}", user_data, ttl=300)
    cache.set(f"personas:{user_id}", personas, ttl=600)
    
    logger.info(f"Preloaded data for user {user_id}: {len(personas)} personas")


async def warm_up_cache(db: Session):
    """
    Прогрев кеша для активных пользователей
    """
    # Получаем ID недавно активных пользователей
    recent_users = db.query(User.telegram_id).filter(
        User.updated_at > datetime.now(timezone.utc) - timedelta(hours=1)
    ).limit(100).all()
    
    for user_id, in recent_users:
        try:
            await preload_user_data(db, user_id)
        except Exception as e:
            logger.error(f"Failed to preload data for user {user_id}: {e}")
    
    logger.info(f"Cache warmed up for {len(recent_users)} recent users")
