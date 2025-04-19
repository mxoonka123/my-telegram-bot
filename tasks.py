import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction
from telegram.ext import Application
from telegram.error import TelegramError
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from db import get_all_active_chat_bot_instances, SessionLocal, User
from persona import Persona
from utils import postprocess_response, extract_gif_links
from handlers import send_to_langdock, process_and_send_response

logger = logging.getLogger(__name__)

async def spam_task(application: Application):
    logger.info("Spam task started.")
    await asyncio.sleep(random.randint(30, 90)) # Initial delay

    while True:
        sleep_duration = random.randint(180, 900) # 3 mins to 15 mins interval
        logger.debug(f"Spam task sleeping for {sleep_duration} seconds.")
        await asyncio.sleep(sleep_duration)

        with SessionLocal() as db:
            active_chat_bots = get_all_active_chat_bot_instances(db)

        if not active_chat_bots:
            logger.debug("Spam task: No active chat bots found.")
            continue

        # Select a subset of bots to potentially spam to avoid bursts
        bots_to_consider = random.sample(active_chat_bots, k=min(len(active_chat_bots), 5)) # Consider up to 5 bots per cycle

        for chat_bot_instance in bots_to_consider:
            # Low probability of actually sending a message
            if random.random() > 0.15: # 15% chance to proceed for this bot
                continue

            try:
                # Ensure relations are loaded correctly within the session context
                with SessionLocal() as db_inner: # Use a new session for safety
                    chat_bot_instance = db_inner.query(ChatBotInstance).get(chat_bot_instance.id)
                    if not chat_bot_instance or not chat_bot_instance.active: continue

                    if not chat_bot_instance.bot_instance_ref or not chat_bot_instance.bot_instance_ref.persona_config:
                         logger.error(f"Spam task: No persona config found for BotInstance {chat_bot_instance.bot_instance_id} linked to chat {chat_bot_instance.chat_id}.")
                         continue

                    # Get owner and check limits (spam shouldn't bypass limits)
                    owner_user = chat_bot_instance.bot_instance_ref.owner
                    if not owner_user:
                        logger.error(f"Spam task: Owner not found for BotInstance {chat_bot_instance.bot_instance_id}.")
                        continue

                    # Check and update limit - spam counts towards the limit
                    if not check_and_update_user_limits(db_inner, owner_user):
                        logger.info(f"Spam task: User {owner_user.telegram_id} limit reached, skipping spam for chat {chat_bot_instance.chat_id}.")
                        continue

                    persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)

                    if not persona.spam_prompt_template:
                         logger.debug(f"Spam task: No spam template for persona {persona.name} in chat {chat_bot_instance.chat_id}.")
                         continue

                    logger.info(f"Running spam task for chat {chat_bot_instance.chat_id} with persona {persona.name}.")

                    spam_prompt = persona.format_spam_prompt()
                    if not spam_prompt:
                        logger.error(f"Spam task: Failed to format spam prompt for {persona.name}")
                        continue

                    response_text = await send_to_langdock(spam_prompt, [])

                    if not response_text or not response_text.strip():
                         logger.warning(f"Spam task got empty response for chat {chat_bot_instance.chat_id}, persona {persona.name}.")
                         continue

                    # Use the existing response processing logic, passing None for update
                    # Pass the inner db session
                    await process_and_send_response(None, application, chat_bot_instance.chat_id, persona, response_text, db_inner)


            except TelegramError as e:
                 logger.error(f"Telegram error in spam task for chat {chat_bot_instance.chat_id}: {e}")
                 # Consider deactivating the bot instance if chat is inaccessible (e.g., bot kicked)
                 if "bot was kicked" in str(e) or "chat not found" in str(e):
                     try:
                         with SessionLocal() as db_update:
                             instance_to_deactivate = db_update.query(ChatBotInstance).get(chat_bot_instance.id)
                             if instance_to_deactivate:
                                 instance_to_deactivate.active = False
                                 db_update.commit()
                                 logger.warning(f"Deactivated ChatBotInstance {chat_bot_instance.id} for chat {chat_bot_instance.chat_id} due to Telegram error: {e}")
                     except Exception as db_err:
                         logger.error(f"Failed to deactivate ChatBotInstance {chat_bot_instance.id} after error: {db_err}")

            except httpx.HTTPStatusError as e:
                logger.error(f"Langdock API HTTP error in spam task for chat {chat_bot_instance.chat_id}: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                # Avoid using persona.name here if persona object creation failed
                logger.error(f"General error in spam task for chat {chat_bot_instance.chat_id}, instance {chat_bot_instance.id}: {e}", exc_info=True)

            await asyncio.sleep(random.uniform(2, 8)) # Small delay between potential spam messages


async def reset_daily_limits_task():
    """Resets daily message counts for all users shortly after midnight UTC."""
    logger.info("Daily limit reset task started.")
    while True:
        now = datetime.now(timezone.utc)
        # Calculate time until next midnight UTC + a few seconds
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        wait_seconds = (midnight - now).total_seconds()
        logger.info(f"Limit reset task: Sleeping for {wait_seconds:.0f} seconds until next reset cycle.")
        await asyncio.sleep(wait_seconds)

        logger.info("Limit reset task: Resetting daily message counts...")
        updated_count = 0
        try:
            with SessionLocal() as db:
                # Update users whose last reset was before today
                today_utc = datetime.now(timezone.utc).date()
                users_to_reset = db.query(User).filter(User.last_message_reset < today_utc).all() # Fetch objects
                if users_to_reset:
                     for user in users_to_reset:
                         user.daily_message_count = 0
                         user.last_message_reset = datetime.now(timezone.utc)
                         updated_count += 1
                     db.commit()
                logger.info(f"Limit reset task: Reset counts for {updated_count} users.")
        except Exception as e:
            logger.error(f"Error during daily limit reset: {e}", exc_info=True)
            # Retry after a shorter delay?
            await asyncio.sleep(300) # Wait 5 minutes before trying again on error

async def check_subscription_expiry_task():
    """Checks for expired subscriptions periodically."""
    logger.info("Subscription expiry check task started.")
    while True:
        # Check every hour, for example
        wait_seconds = 3600
        logger.debug(f"Subscription expiry task: Sleeping for {wait_seconds} seconds.")
        await asyncio.sleep(wait_seconds)

        now = datetime.now(timezone.utc)
        logger.debug(f"Subscription expiry task: Checking for subscriptions expired before {now}.")
        expired_count = 0
        try:
            with SessionLocal() as db:
                expired_users = db.query(User).filter(
                    User.is_subscribed == True,
                    User.subscription_expires_at != None,
                    User.subscription_expires_at <= now
                ).all()

                if expired_users:
                    for user in expired_users:
                         user.is_subscribed = False
                         # Optionally reset limits or keep them until daily reset
                         # user.subscription_expires_at = None # Optionally clear the date
                         expired_count += 1
                         logger.info(f"Subscription expired for user {user.telegram_id}. Reverted to free.")
                         # TODO: Optionally notify the user their subscription expired
                    db.commit()
                    logger.info(f"Subscription expiry task: Processed {expired_count} expired subscriptions.")
                else:
                     logger.debug("Subscription expiry task: No expired subscriptions found.")

        except Exception as e:
            logger.error(f"Error during subscription expiry check: {e}", exc_info=True)
