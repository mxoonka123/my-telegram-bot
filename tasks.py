import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes
from telegram.error import TelegramError
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, select, update as sql_update

from db import (
    get_all_active_chat_bot_instances, SessionLocal, User, ChatBotInstance, BotInstance,
    check_and_update_user_limits, get_db, PersonaConfig
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, escape_markdown_v2
from handlers import send_to_langdock, process_and_send_response
from config import FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT

logger = logging.getLogger(__name__)


async def reset_daily_limits_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Task started: Resetting daily message counts...")
    updated_count = 0
    try:
        with next(get_db()) as db_session:
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            users_to_reset_stmt = (
                select(User.id)
                .where(User.last_message_reset < today_start)
            )
            users_to_reset = db_session.execute(users_to_reset_stmt).scalars().all()

            if users_to_reset:
                update_stmt = (
                    sql_update(User)
                    .where(User.id.in_(users_to_reset))
                    .values(
                        daily_message_count=0,
                        last_message_reset=now
                    )
                )
                result = db_session.execute(update_stmt)
                db_session.commit()
                updated_count = result.rowcount
                logger.info(f"Limit reset task: Reset counts for {updated_count} users.")
            else:
                 logger.debug("Limit reset task: No users needed a reset.")
    except SQLAlchemyError as e:
        logger.error(f"Error during daily limit reset: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during daily limit reset: {e}", exc_info=True)


async def check_subscription_expiry_task(context: ContextTypes.DEFAULT_TYPE):
    if not isinstance(context.job.data, Application):
        logger.error("check_subscription_expiry_task: context.job.data is not a PTB Application instance.")
        return
    application: Application = context.job.data
    logger.info("Task started: Checking subscription expiry...")

    now = datetime.now(timezone.utc)
    expired_users_info = []
    try:
        with next(get_db()) as db_session:
            expired_users_query = (
                select(User.id, User.telegram_id, User.daily_message_count)
                .where(
                    User.is_subscribed == True,
                    User.subscription_expires_at != None,
                    User.subscription_expires_at <= now
                )
            )
            expired_users_result = db_session.execute(expired_users_query).all()

            if expired_users_result:
                user_ids_to_update = [user.id for user in expired_users_result]

                update_stmt = (
                    sql_update(User)
                    .where(User.id.in_(user_ids_to_update))
                    .values(is_subscribed=False)
                )
                result = db_session.execute(update_stmt)
                db_session.commit()
                expired_count = result.rowcount
                logger.info(f"Subscription expiry task: Deactivated {expired_count} expired subscriptions.")

                for user_id, telegram_id, daily_count in expired_users_result:
                    persona_count = db_session.execute(
                        select(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user_id)
                    ).scalar() or 0
                    expired_users_info.append({
                        "telegram_id": telegram_id,
                        "daily_count": daily_count,
                        "persona_count": persona_count
                    })
            else:
                logger.debug("Subscription expiry task: No expired subscriptions found.")

    except SQLAlchemyError as e:
        logger.error(f"Error during subscription expiry check (DB phase): {e}", exc_info=True)
        return
    except Exception as e:
        logger.error(f"Unexpected error during subscription expiry check (DB phase): {e}", exc_info=True)
        return

    if expired_users_info:
        logger.info(f"Sending expiry notifications to {len(expired_users_info)} users.")
        for user_info in expired_users_info:
            telegram_id = user_info["telegram_id"]
            try:
                 persona_limit_str = escape_markdown_v2(f"{user_info['persona_count']}/{FREE_PERSONA_LIMIT}")
                 daily_limit_str = escape_markdown_v2(f"{user_info['daily_count']}/{FREE_DAILY_MESSAGE_LIMIT}")
                 text = (
                     escape_markdown_v2(f"⏳ ваша премиум подписка истекла\\.\n\n") +
                     f"*текущие лимиты \\(Free\\):*\n" +
                     f"сообщения: {daily_limit_str}\n" +
                     f"личности: {persona_limit_str}\n\n" +
                     escape_markdown_v2("чтобы продолжить пользоваться всеми возможностями, вы можете снова оформить подписку командой /subscribe")
                 )
                 await application.bot.send_message(
                     chat_id=telegram_id,
                     text=text,
                     parse_mode=ParseMode.MARKDOWN_V2
                 )
                 logger.info(f"Sent expiry notification to user {telegram_id}.")
                 await asyncio.sleep(0.1)
            except TelegramError as te:
                 logger.warning(f"Failed to send expiry notification to user {telegram_id}: {te}")
                 if isinstance(te, TelegramError) and hasattr(te, 'message') and "parse" in te.message.lower():
                     logger.error(f"--> Failed text (escaped): '{text[:200]}...'")
            except Exception as e_notify:
                logger.error(f"Unexpected error sending expiry notification to user {telegram_id}: {e_notify}", exc_info=True)
