# tasks.py

import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes # Добавлен ContextTypes
from telegram.error import TelegramError
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from db import (
    get_all_active_chat_bot_instances, SessionLocal, User, ChatBotInstance, BotInstance,
    check_and_update_user_limits, get_db, PersonaConfig # Добавлен PersonaConfig
)
from sqlalchemy import func # Добавлен func
from persona import Persona
from utils import postprocess_response, extract_gif_links
from handlers import send_to_langdock, process_and_send_response
from config import FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT # Добавлены лимиты

logger = logging.getLogger(__name__)

# spam_task пока закомментирован, как и раньше
# async def spam_task(application: Application):
#     ...

# Добавляем context в аргументы
async def reset_daily_limits_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Daily limit reset task triggered.")
    logger.info("Limit reset task: Resetting daily message counts...")
    updated_count = 0
    db_session = None # Инициализируем переменную сессии
    try:
        with next(get_db()) as db_session: # Используем db_session здесь
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = db_session.query(User).filter(
                User.last_message_reset < today_start
            ).update({
                User.daily_message_count: 0,
                User.last_message_reset: datetime.now(timezone.utc)
            }, synchronize_session=False)
            db_session.commit()
            updated_count = result
            if updated_count > 0:
                 logger.info(f"Limit reset task: Reset counts for {updated_count} users.")
            else:
                 logger.debug("Limit reset task: No users needed a reset.")
    except Exception as e:
        logger.error(f"Error during daily limit reset: {e}", exc_info=True)
        if db_session and db_session.is_active: # Проверяем db_session перед роллбэком
             try:
                 db_session.rollback()
             except Exception as rb_e:
                 logger.error(f"Error during rollback in reset_daily_limits_task: {rb_e}")


# Меняем application на context, получаем application из context.job.data
async def check_subscription_expiry_task(context: ContextTypes.DEFAULT_TYPE):
    application = context.job.data # Получаем application
    logger.info("Subscription expiry check task triggered.") # Убрали "started"

    now = datetime.now(timezone.utc)
    expired_count = 0
    db_session = None # Инициализируем переменную сессии
    try:
        with next(get_db()) as db_session: # Используем db_session здесь
            expired_users = db_session.query(User).filter(
                User.is_subscribed == True,
                User.subscription_expires_at != None,
                User.subscription_expires_at <= now
            ).all()

            if expired_users:
                user_ids_to_update = [user.id for user in expired_users]
                db_session.query(User).filter(User.id.in_(user_ids_to_update)).update(
                    {User.is_subscribed: False},
                    synchronize_session=False
                )
                db_session.commit()
                expired_count = len(user_ids_to_update)
                logger.info(f"Subscription expiry task: Deactivated {expired_count} expired subscriptions.")

                # Обновляем объекты пользователей в сессии после коммита, чтобы получить новые лимиты
                # Или можно передать старые лимиты в сообщение, как было
                # Пока оставим как было - передаем старые лимиты
                for user in expired_users:
                    logger.info(f"Subscription expired for user {user.telegram_id}. Notifying.")
                    try:
                         # Считаем количество персон для сообщения
                         persona_count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
                         text = (
                             f"⏳ ваша премиум подписка истекла.\n"
                             f"текущие лимиты (Free):\n"
                             # Показываем счетчик сообщений на момент проверки (он мог измениться после)
                             # и новый лимит FREE_DAILY_MESSAGE_LIMIT
                             f"сообщения: {user.daily_message_count}/{FREE_DAILY_MESSAGE_LIMIT} | "
                             f"личности: {persona_count}/{FREE_PERSONA_LIMIT}\n\n"
                             "чтобы продолжить пользоваться всеми возможностями, вы можете снова оформить подписку командой /subscribe"
                         )
                         await application.bot.send_message(
                             chat_id=user.telegram_id,
                             text=text,
                             parse_mode=ParseMode.MARKDOWN
                         )
                    except TelegramError as te:
                        logger.warning(f"Failed to send expiry notification to user {user.telegram_id}: {te}")
                    except Exception as e_notify:
                        logger.error(f"Unexpected error sending expiry notification to user {user.telegram_id}: {e_notify}", exc_info=True)
            else:
                logger.debug("Subscription expiry task: No expired subscriptions found.")

    except Exception as e:
        logger.error(f"Error during subscription expiry check: {e}", exc_info=True)
        if db_session and db_session.is_active: # Проверяем db_session
             try:
                 db_session.rollback()
             except Exception as rb_e:
                 logger.error(f"Error during rollback in check_subscription_expiry_task: {rb_e}")
