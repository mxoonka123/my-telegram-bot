import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes
from telegram.error import TelegramError, BadRequest, Forbidden # Added Forbidden
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError, ProgrammingError # Added ProgrammingError
from sqlalchemy import func, select, update as sql_update

from db import (
    get_all_active_chat_bot_instances, SessionLocal, User, ChatBotInstance, BotInstance,
    check_and_update_user_limits, get_db, PersonaConfig
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, escape_markdown_v2
from config import FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_USER_MONTHLY_MESSAGE_LIMIT # <-- ИСПРАВЛЕННЫЙ ИМПОРТ

logger = logging.getLogger(__name__)

async def check_subscription_expiry_task(context: ContextTypes.DEFAULT_TYPE):
    if not isinstance(context.job.data, Application):
        logger.error("check_subscription_expiry_task: context.job.data is not PTB Application.")
        return
    application: Application = context.job.data
    logger.info("Task started: Checking subscription expiry...")

    now = datetime.now(timezone.utc)
    expired_users_info = []
    try:
        with get_db() as db_session:
            # Select users who are subscribed AND expiry date is in the past
            expired_users_query = (
                select(User.id, User.telegram_id, User.monthly_message_count, User.subscription_expires_at)
                .where(
                    User.is_subscribed == True,
                    User.subscription_expires_at != None,
                    User.subscription_expires_at <= now
                )
            )
            expired_users_result = db_session.execute(expired_users_query).all()

            if expired_users_result:
                user_ids_to_update = [user.id for user in expired_users_result]
                expired_details = [f"TG ID: {u.telegram_id} (DB ID: {u.id}, Expired: {u.subscription_expires_at})" for u in expired_users_result]
                logger.info(f"Subscription expiry task: Found {len(expired_details)} expired subscriptions: {'; '.join(expired_details)}")

                # Update status and maybe adjust limits if needed
                update_stmt = (
                    sql_update(User)
                    .where(User.id.in_(user_ids_to_update))
                    .values(is_subscribed=False) # Only set is_subscribed to False
                    # Optionally: Reset daily count if it exceeds the free limit?
                    # .values(is_subscribed=False, daily_message_count = case(...)) # More complex update
                    .execution_options(synchronize_session="fetch") # Recommended
                )
                result = db_session.execute(update_stmt)
                db_session.commit()
                expired_count = result.rowcount
                logger.info(f"Subscription expiry task: Deactivated {expired_count} subscriptions.")

                # Gather info for notifications AFTER commit
                for user_id, telegram_id, monthly_count, _ in expired_users_result:
                    persona_count = db_session.execute(
                        select(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user_id)
                    ).scalar() or 0
                    expired_users_info.append({
                        "telegram_id": telegram_id,
                        "monthly_message_count": monthly_count,
                        "persona_count": persona_count
                    })
            else:
                logger.debug("Subscription expiry task: No expired subscriptions found.")

    except (SQLAlchemyError, ProgrammingError) as e:
        logger.error(f"DB error during subscription expiry check: {e}", exc_info=True)
        return
    except Exception as e:
        logger.error(f"Unexpected error during subscription expiry check (DB phase): {e}", exc_info=True)
        return

    # Send notifications outside the DB transaction
    if expired_users_info:
        logger.info(f"Sending expiry notifications to {len(expired_users_info)} users.")
        for user_info in expired_users_info:
            telegram_id = user_info["telegram_id"]
            text_to_send = ""
            try:
                 # Prepare notification message
                 persona_limit_str = escape_markdown_v2(f"{user_info['persona_count']}/{FREE_PERSONA_LIMIT}")
                 # --- ИСПРАВЛЕННЫЙ БЛОК ---
                 # Теперь мы используем корректный monthly_message_count, который передали в user_info
                 monthly_limit_str = escape_markdown_v2(f"{user_info['monthly_message_count']}/{FREE_USER_MONTHLY_MESSAGE_LIMIT}")

                 text_to_send = (
                     escape_markdown_v2("⏳ ваша премиум подписка истекла\\.\n\n") +
                     f"*Текущие лимиты \\(Free\\):*\n" +
                     f"Сообщения \\(в мес\\.\\): `{monthly_limit_str}`\n" +
                     f"Личности: `{persona_limit_str}`\n\n" +
                     escape_markdown_v2("Чтобы продолжить пользоваться всеми возможностями, вы можете снова оформить подписку командой `/subscribe`\\.")
                 )
                 # --- КОНЕЦ ИСПРАВЛЕННОГО БЛОКА ---
                 await application.bot.send_message(
                     chat_id=telegram_id,
                     text=text_to_send,
                     parse_mode=ParseMode.MARKDOWN_V2
                 )
                 logger.info(f"Sent expiry notification to user {telegram_id}.")
                 await asyncio.sleep(0.1)
            except BadRequest as te:
                 logger.warning(f"Failed to send expiry notification to user {telegram_id} (BadRequest): {te}")
                 if "parse" in str(te).lower(): logger.error(f"--> Failed expiry text (MD): '{text_to_send[:200]}...'")
            except Forbidden: # User blocked the bot
                 logger.warning(f"Failed to send expiry notification to user {telegram_id}: Bot blocked or kicked.")
            except TelegramError as te:
                 logger.warning(f"Telegram error sending expiry notification to user {telegram_id}: {te}")
            except Exception as e_notify:
                logger.error(f"Unexpected error sending expiry notification to user {telegram_id}: {e_notify}", exc_info=True)