import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction, ParseMode # Добавили ParseMode
from telegram.ext import Application
from telegram.error import TelegramError
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from db import ( # Импортируем нужные функции/модели
    get_all_active_chat_bot_instances, SessionLocal, User, ChatBotInstance, BotInstance,
    check_and_update_user_limits, get_db # Добавили get_db
)
from persona import Persona
from utils import postprocess_response, extract_gif_links
# Убираем импорт send_to_langdock и process_and_send_response из handlers, чтобы избежать циклического импорта
# Вместо этого можно либо скопировать их сюда, либо импортировать application и использовать его для отправки
# Пока оставим так, предполагая, что они доступны (хотя это плохая практика)
# Лучше: Перенести send_to_langdock и process_and_send_response в отдельный модуль `llm_interface.py` или подобный.
# Но пока оставим импорт из handlers для простоты демонстрации.
from handlers import send_to_langdock, process_and_send_response
from config import FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT # Импортируем лимиты для уведомления

logger = logging.getLogger(__name__)

async def spam_task(application: Application):
    logger.info("Spam task started.")
    await asyncio.sleep(random.randint(60, 120)) # Initial delay

    while True:
        sleep_duration = random.randint(300, 1200) # 5 mins to 20 mins interval
        # logger.debug(f"Spam task sleeping for {sleep_duration} seconds.")
        await asyncio.sleep(sleep_duration)

        try:
            with next(get_db()) as db: # Используем context manager
                active_chat_bots = get_all_active_chat_bot_instances(db) # Запрос внутри сессии

            if not active_chat_bots:
                # logger.debug("Spam task: No active chat bots found.")
                continue

            bots_to_consider = random.sample(active_chat_bots, k=min(len(active_chat_bots), 3)) # Уменьшим количество для экономии

            for chat_bot_instance_stub in bots_to_consider: # Используем stub, т.к. связи могут быть неактуальны вне сессии
                # Низкая вероятность спама
                if random.random() > 0.10: # 10% chance
                    continue

                # Используем ID для получения актуального объекта в новой сессии
                instance_id = chat_bot_instance_stub.id
                chat_id = chat_bot_instance_stub.chat_id # Получаем chat_id из стаба

                # Открываем новую сессию для каждой попытки спама
                with next(get_db()) as db_inner:
                    try:
                        # Получаем актуальный объект со связями
                        chat_bot_instance = db_inner.query(ChatBotInstance)\
                            .options(
                                joinedload(ChatBotInstance.bot_instance_ref)
                                .joinedload(BotInstance.persona_config),
                                joinedload(ChatBotInstance.bot_instance_ref)
                                .joinedload(BotInstance.owner)
                            )\
                            .get(instance_id)

                        if not chat_bot_instance or not chat_bot_instance.active: continue

                        bot_instance_ref = chat_bot_instance.bot_instance_ref
                        if not bot_instance_ref or not bot_instance_ref.persona_config or not bot_instance_ref.owner:
                             logger.error(f"Spam task: Missing relations for ChatBotInstance {instance_id} in chat {chat_id}.")
                             continue

                        owner_user = bot_instance_ref.owner
                        persona = Persona(bot_instance_ref.persona_config, chat_bot_instance)

                        # Проверка лимита владельца
                        if not check_and_update_user_limits(db_inner, owner_user):
                            logger.info(f"Spam task: Owner {owner_user.telegram_id} limit reached, skipping spam for chat {chat_id}.")
                            continue

                        if not persona.spam_prompt_template:
                             # logger.debug(f"Spam task: No spam template for persona {persona.name} in chat {chat_id}.")
                             continue

                        logger.info(f"Running spam task for chat {chat_id} with persona {persona.name}.")

                        spam_prompt = persona.format_spam_prompt()
                        if not spam_prompt:
                            logger.error(f"Spam task: Failed to format spam prompt for {persona.name}")
                            continue

                        response_text = await send_to_langdock(spam_prompt, [])

                        if not response_text or not response_text.strip() or "ошибка" in response_text.lower():
                             logger.warning(f"Spam task got empty or error response for chat {chat_id}, persona {persona.name}: {response_text}")
                             continue

                        # Используем application для доступа к боту и контексту
                        await process_and_send_response(None, application, chat_id, persona, response_text, db_inner)

                    except TelegramError as e:
                         logger.error(f"Telegram error in spam task for chat {chat_id}: {e}")
                         if "bot was kicked" in str(e) or "chat not found" in str(e) or "blocked" in str(e):
                             logger.warning(f"Deactivating ChatBotInstance {instance_id} for chat {chat_id} due to Telegram error: {e}")
                             instance_to_deactivate = db_inner.query(ChatBotInstance).get(instance_id)
                             if instance_to_deactivate:
                                 instance_to_deactivate.active = False
                                 db_inner.commit() # Коммитим деактивацию
                    except Exception as e:
                         logger.error(f"Inner error in spam task loop for chat {chat_id}, instance {instance_id}: {e}", exc_info=True)
                         if db_inner.is_active: db_inner.rollback() # Откат при внутренней ошибке

                await asyncio.sleep(random.uniform(3, 10)) # Небольшая пауза между ботами

        except Exception as e:
            logger.error(f"Outer error in spam task main loop: {e}", exc_info=True)
            await asyncio.sleep(60) # Пауза при внешней ошибке


async def reset_daily_limits_task():
    logger.info("Daily limit reset task started.")
    while True:
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=10, microsecond=0) # Сдвинем на 10 сек
        wait_seconds = (midnight - now).total_seconds()
        logger.info(f"Limit reset task: Sleeping for {wait_seconds:.0f} seconds until next reset.")
        await asyncio.sleep(wait_seconds)

        logger.info("Limit reset task: Resetting daily message counts...")
        updated_count = 0
        try:
            with next(get_db()) as db:
                # Используем update для эффективности
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                result = db.query(User).filter(
                    User.last_message_reset < today_start
                ).update({
                    User.daily_message_count: 0,
                    User.last_message_reset: datetime.now(timezone.utc)
                }, synchronize_session=False) # Не синхронизируем сессию для производительности
                db.commit()
                updated_count = result # .update() возвращает количество обновленных строк
                logger.info(f"Limit reset task: Reset counts for {updated_count} users.")
        except Exception as e:
            logger.error(f"Error during daily limit reset: {e}", exc_info=True)
            if 'db' in locals() and db.is_active: db.rollback()
            await asyncio.sleep(300)


async def check_subscription_expiry_task(application: Application): # Принимаем application
    logger.info("Subscription expiry check task started.")
    while True:
        wait_seconds = 3600 # Check every hour
        # logger.debug(f"Subscription expiry task: Sleeping for {wait_seconds} seconds.")
        await asyncio.sleep(wait_seconds)

        now = datetime.now(timezone.utc)
        # logger.debug(f"Subscription expiry task: Checking for subscriptions expired before {now}.")
        expired_count = 0
        try:
            with next(get_db()) as db:
                expired_users = db.query(User).filter(
                    User.is_subscribed == True,
                    User.subscription_expires_at != None,
                    User.subscription_expires_at <= now
                ).all() # Получаем объекты для уведомления

                if expired_users:
                    user_ids_to_update = [user.id for user in expired_users]
                    # Массовое обновление статуса
                    db.query(User).filter(User.id.in_(user_ids_to_update)).update(
                        {User.is_subscribed: False},
                        synchronize_session=False
                    )
                    db.commit()
                    expired_count = len(user_ids_to_update)
                    logger.info(f"Subscription expiry task: Deactivated {expired_count} expired subscriptions.")

                    # Отправляем уведомления пользователям
                    for user in expired_users:
                        logger.info(f"Subscription expired for user {user.telegram_id}. Notifying.")
                        try:
                             # Считаем новые лимиты для сообщения
                             persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()
                             text = (
                                 f"⏳ ваша премиум подписка истекла.\n"
                                 f"текущие лимиты (Free):\n"
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
                        except Exception as e:
                            logger.error(f"Unexpected error sending expiry notification to user {user.telegram_id}: {e}", exc_info=True)

                # else:
                     # logger.debug("Subscription expiry task: No expired subscriptions found.")

        except Exception as e:
            logger.error(f"Error during subscription expiry check: {e}", exc_info=True)
            if 'db' in locals() and db.is_active: db.rollback()
