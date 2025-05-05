import logging
import httpx
import random
import asyncio
import re
import uuid
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple

from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, Chat as TgChat, CallbackQuery
from telegram.constants import ChatAction, ParseMode, ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut

from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func
from sqlalchemy import delete

from yookassa import Configuration as YookassaConfig, Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem


import config
from config import (
    LANGDOCK_API_KEY, LANGDOCK_BASE_URL, LANGDOCK_MODEL,
    DEFAULT_MOOD_PROMPTS, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CURRENCY, WEBHOOK_URL_BASE,
    SUBSCRIPTION_DURATION_DAYS, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    ADMIN_USER_ID, CHANNEL_ID
)
from db import (
    get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner, check_and_update_user_limits, activate_subscription,
    create_bot_instance, link_bot_instance_to_chat, delete_persona_config,
    get_db, get_active_chat_bot_instance_with_relations,
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info, escape_markdown_v2

logger = logging.getLogger(__name__)

# --- Helper Functions ---

async def check_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user is subscribed to the required channel."""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set in config. Skipping subscription check.")
        return True # Skip check if no channel is configured

    user_id = None
    # Determine user ID from update or callback query
    eff_user = getattr(update, 'effective_user', None)
    cb_user = getattr(getattr(update, 'callback_query', None), 'from_user', None)

    if eff_user:
        user_id = eff_user.id
    elif cb_user:
        user_id = cb_user.id
        logger.debug(f"Using user_id {user_id} from callback_query.")
    else:
        logger.warning("check_channel_subscription called without valid user information.")
        return False # Cannot check without user ID

    # Admin always passes
    if is_admin(user_id):
        return True

    logger.debug(f"Checking subscription status for user {user_id} in channel {CHANNEL_ID}")
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id, read_timeout=10)
        # Check if user status is one of the allowed ones
        allowed_statuses = [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
        logger.debug(f"User {user_id} status in {CHANNEL_ID}: {member.status}")
        if member.status in allowed_statuses:
            logger.debug(f"User {user_id} IS subscribed to {CHANNEL_ID} (status: {member.status})")
            return True
        else:
            logger.info(f"User {user_id} is NOT subscribed to {CHANNEL_ID} (status: {member.status})")
            return False
    except TimedOut:
        logger.warning(f"Timeout checking subscription for user {user_id} in channel {CHANNEL_ID}. Denying access.")
        # Try to inform the user about the timeout
        target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
        if target_message:
            try:
                await target_message.reply_text(
                    escape_markdown_v2("вЏі РЅРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕРІРµСЂРёС‚СЊ РїРѕРґРїРёСЃРєСѓ РЅР° РєР°РЅР°Р» (С‚Р°Р№РјР°СѓС‚). РїРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ СЂР°Р· РїРѕР·Р¶Рµ."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Timeout' error message: {send_err}")
        return False
    except Forbidden as e:
        logger.error(f"Forbidden error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}. Ensure bot is admin in the channel.")
        # Try to inform the user about the permission issue
        target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
        if target_message:
            try:
                await target_message.reply_text(
                    escape_markdown_v2("вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕРІРµСЂРёС‚СЊ РїРѕРґРїРёСЃРєСѓ РЅР° РєР°РЅР°Р». СѓР±РµРґРёС‚РµСЃСЊ, С‡С‚Рѕ Р±РѕС‚ РґРѕР±Р°РІР»РµРЅ РІ РєР°РЅР°Р» РєР°Рє Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False
    except BadRequest as e:
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
         reply_text_raw = "вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РїСЂРѕРІРµСЂРєРµ РїРѕРґРїРёСЃРєРё (badrequest). РїРѕРїСЂРѕР±СѓР№С‚Рµ РїРѕР·Р¶Рµ."
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             reply_text_raw = "вќЊ РЅРµ СѓРґР°РµС‚СЃСЏ РїРѕР»СѓС‡РёС‚СЊ РґРѕСЃС‚СѓРї Рє СЃРїРёСЃРєСѓ СѓС‡Р°СЃС‚РЅРёРєРѕРІ РєР°РЅР°Р»Р° РґР»СЏ РїСЂРѕРІРµСЂРєРё РїРѕРґРїРёСЃРєРё. РІРѕР·РјРѕР¶РЅРѕ, РЅР°СЃС‚СЂРѕР№РєРё РєР°РЅР°Р»Р° РЅРµ РїРѕР·РІРѕР»СЏСЋС‚ СЌС‚Рѕ СЃРґРµР»Р°С‚СЊ."
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             return False
         elif "chat not found" in error_message:
              logger.error(f"-> Specific BadRequest: Chat {CHANNEL_ID} not found. Check CHANNEL_ID config.")
              reply_text_raw = "вќЊ РѕС€РёР±РєР°: РЅРµ СѓРґР°Р»РѕСЃСЊ РЅР°Р№С‚Рё СѓРєР°Р·Р°РЅРЅС‹Р№ РєР°РЅР°Р» РґР»СЏ РїСЂРѕРІРµСЂРєРё РїРѕРґРїРёСЃРєРё. РїСЂРѕРІРµСЂСЊС‚Рµ РЅР°СЃС‚СЂРѕР№РєРё Р±РѕС‚Р°."

         target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
         if target_message:
             try: await target_message.reply_text(escape_markdown_v2(reply_text_raw), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Failed to send 'BadRequest' error message: {send_err}")
         return False
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
        if target_message:
            try: await target_message.reply_text(escape_markdown_v2("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° telegram РїСЂРё РїСЂРѕРІРµСЂРєРµ РїРѕРґРїРёСЃРєРё. РїРѕРїСЂРѕР±СѓР№С‚Рµ РїРѕР·Р¶Рµ."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Failed to send 'TelegramError' message: {send_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}", exc_info=True)
        return False

async def send_subscription_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message asking the user to subscribe to the channel."""
    target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)

    if not target_message:
         logger.warning("Cannot send subscription required message: no target message found.")
         return

    channel_username = None
    if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith('@'):
        channel_username = CHANNEL_ID.lstrip('@')

    error_msg_raw = "вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё СЃСЃС‹Р»РєРё РЅР° РєР°РЅР°Р»."
    subscribe_text_raw = "вќ— РґР»СЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ Р±РѕС‚Р° РЅРµРѕР±С…РѕРґРёРјРѕ РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РЅР° РЅР°С€ РєР°РЅР°Р»."
    button_text = "вћЎпёЏ РїРµСЂРµР№С‚Рё Рє РєР°РЅР°Р»Сѓ"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"вќ— РґР»СЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ Р±РѕС‚Р° РЅРµРѕР±С…РѕРґРёРјРѕ РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РЅР° РєР°РЅР°Р» @{channel_username}."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         subscribe_text_raw = "вќ— РґР»СЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ Р±РѕС‚Р° РЅРµРѕР±С…РѕРґРёРјРѕ РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РЅР° РЅР°С€ РѕСЃРЅРѕРІРЅРѕР№ РєР°РЅР°Р». РїРѕР¶Р°Р»СѓР№СЃС‚Р°, РЅР°Р№РґРёС‚Рµ РєР°РЅР°Р» РІ РїРѕРёСЃРєРµ РёР»Рё С‡РµСЂРµР· РѕРїРёСЃР°РЅРёРµ Р±РѕС‚Р°."
    else:
         logger.error(f"Invalid CHANNEL_ID format: {CHANNEL_ID}. Cannot generate subscription message correctly.")
         subscribe_text_raw = error_msg_raw

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    escaped_text = escape_markdown_v2(subscribe_text_raw)
    try:
        await target_message.reply_text(escaped_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        if update.callback_query:
             try: await update.callback_query.answer()
             except: pass
    except BadRequest as e:
        logger.error(f"Failed sending subscription required message (BadRequest): {e} - Text Raw: '{subscribe_text_raw}' Escaped: '{escaped_text[:100]}...'")
        try:
            await target_message.reply_text(subscribe_text_raw, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
            logger.error(f"Failed sending plain subscription required message: {fallback_e}")
    except Exception as e:
         logger.error(f"Failed to send subscription required message: {e}")

def is_admin(user_id: int) -> bool:
    """Checks if the user ID belongs to the admin."""
    return user_id == ADMIN_USER_ID

# --- Conversation States ---
# Edit Persona Wizard States
(EDIT_WIZARD_MENU, # Main wizard menu
 EDIT_NAME, EDIT_DESCRIPTION, EDIT_COMM_STYLE, EDIT_VERBOSITY,
 EDIT_GROUP_REPLY, EDIT_MEDIA_REACTION,
 EDIT_MOODS_ENTRY, # Entry point for mood sub-conversation
 # Mood Editing Sub-Conversation States
 EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM,
 # Delete Persona Conversation State
 DELETE_PERSONA_CONFIRM,
 EDIT_MAX_MESSAGES # <-- РќРѕРІС‹Р№ СЃС‚РµР№С‚
 ) = range(14) # Total 14 states

# --- Terms of Service Text ---
# (Assuming TOS_TEXT_RAW and TOS_TEXT are defined as before)
TOS_TEXT_RAW = """
рџ“њ РїРѕР»СЊР·РѕРІР°С‚РµР»СЃРєРѕРµ СЃРѕРіР»Р°С€РµРЅРёРµ СЃРµСЂРІРёСЃР° @NunuAiBot

РїСЂРёРІРµС‚! РґРѕР±СЂРѕ РїРѕР¶Р°Р»РѕРІР°С‚СЊ РІ @NunuAiBot! РјС‹ СЂР°РґС‹, С‡С‚Рѕ С‚С‹ СЃ РЅР°РјРё. СЌС‚Рѕ СЃРѕРіР»Р°С€РµРЅРёРµ вЂ” РґРѕРєСѓРјРµРЅС‚, РєРѕС‚РѕСЂС‹Р№ РѕР±СЉСЏСЃРЅСЏРµС‚ РїСЂР°РІРёР»Р° РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ РЅР°С€РµРіРѕ СЃРµСЂРІРёСЃР°. РїСЂРѕС‡РёС‚Р°Р№ РµРіРѕ, РїРѕР¶Р°Р»СѓР№СЃС‚Р°.

РґР°С‚Р° РїРѕСЃР»РµРґРЅРµРіРѕ РѕР±РЅРѕРІР»РµРЅРёСЏ: 01.03.2025

1. Рѕ С‡РµРј СЌС‚Рѕ СЃРѕРіР»Р°С€РµРЅРёРµ?
1.1. СЌС‚Рѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЃРєРѕРµ СЃРѕРіР»Р°С€РµРЅРёРµ (РёР»Рё РїСЂРѕСЃС‚Рѕ "СЃРѕРіР»Р°С€РµРЅРёРµ") вЂ” РґРѕРіРѕРІРѕСЂ РјРµР¶РґСѓ С‚РѕР±РѕР№ (РґР°Р»РµРµ вЂ“ "РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ" РёР»Рё "С‚С‹") Рё РЅР°РјРё (РІР»Р°РґРµР»СЊС†РµРј telegram-Р±РѕС‚Р° @NunuAiBot, РґР°Р»РµРµ вЂ“ "СЃРµСЂРІРёСЃ" РёР»Рё "РјС‹"). РѕРЅРѕ РѕРїРёСЃС‹РІР°РµС‚ СѓСЃР»РѕРІРёСЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ СЃРµСЂРІРёСЃР°.
1.2. РЅР°С‡РёРЅР°СЏ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РЅР°С€ СЃРµСЂРІРёСЃ (РїСЂРѕСЃС‚Рѕ РѕС‚РїСЂР°РІР»СЏСЏ Р±РѕС‚Сѓ Р»СЋР±РѕРµ СЃРѕРѕР±С‰РµРЅРёРµ РёР»Рё РєРѕРјР°РЅРґСѓ), С‚С‹ РїРѕРґС‚РІРµСЂР¶РґР°РµС€СЊ, С‡С‚Рѕ РїСЂРѕС‡РёС‚Р°Р», РїРѕРЅСЏР» Рё СЃРѕРіР»Р°СЃРµРЅ СЃРѕ РІСЃРµРјРё СѓСЃР»РѕРІРёСЏРјРё СЌС‚РѕРіРѕ СЃРѕРіР»Р°С€РµРЅРёСЏ. РµСЃР»Рё С‚С‹ РЅРµ СЃРѕРіР»Р°СЃРµРЅ С…РѕС‚СЏ Р±С‹ СЃ РѕРґРЅРёРј РїСѓРЅРєС‚РѕРј, РїРѕР¶Р°Р»СѓР№СЃС‚Р°, РїСЂРµРєСЂР°С‚Рё РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ СЃРµСЂРІРёСЃР°.
1.3. РЅР°С€ СЃРµСЂРІРёСЃ РїСЂРµРґРѕСЃС‚Р°РІР»СЏРµС‚ С‚РµР±Рµ РёРЅС‚РµСЂРµСЃРЅСѓСЋ РІРѕР·РјРѕР¶РЅРѕСЃС‚СЊ СЃРѕР·РґР°РІР°С‚СЊ Рё РѕР±С‰Р°С‚СЊСЃСЏ СЃ РІРёСЂС‚СѓР°Р»СЊРЅС‹РјРё СЃРѕР±РµСЃРµРґРЅРёРєР°РјРё РЅР° Р±Р°Р·Рµ РёСЃРєСѓСЃСЃС‚РІРµРЅРЅРѕРіРѕ РёРЅС‚РµР»Р»РєС‚Р° (РґР°Р»РµРµ вЂ“ "Р»РёС‡РЅРѕСЃС‚Рё" РёР»Рё "ai-СЃРѕР±РµСЃРµРґРЅРёРєРё").

2. РїСЂРѕ РїРѕРґРїРёСЃРєСѓ Рё РѕРїР»Р°С‚Сѓ
2.1. РјС‹ РїСЂРµРґР»Р°РіР°РµРј РґРІР° СѓСЂРѕРІРЅСЏ РґРѕСЃС‚СѓРїР°: Р±РµСЃРїР»Р°С‚РЅС‹Р№ Рё premium (РїР»Р°С‚РЅС‹Р№). РІРѕР·РјРѕР¶РЅРѕСЃС‚Рё Рё Р»РёРјРёС‚С‹ РґР»СЏ РєР°Р¶РґРѕРіРѕ СѓСЂРѕРІРЅСЏ РїРѕРґСЂРѕР±РЅРѕ РѕРїРёСЃР°РЅС‹ РІРЅСѓС‚СЂРё Р±РѕС‚Р°, РЅР°РїСЂРёРјРµСЂ, РІ РєРѕРјР°РЅРґР°С… `/profile` Рё `/subscribe`.
2.2. РїР»Р°С‚РЅР°СЏ РїРѕРґРїРёСЃРєР° РґР°РµС‚ С‚РµР±Рµ СЂР°СЃС€РёСЂРµРЅРЅС‹Рµ РІРѕР·РјРѕР¶РЅРѕСЃС‚Рё Рё СѓРІРµР»РёС‡РµРЅРЅС‹Рµ Р»РёРјРёС‚С‹ РЅР° РїРµСЂРёРѕРґ РІ {subscription_duration} РґРЅРµР№.
2.3. СЃС‚РѕРёРјРѕСЃС‚СЊ РїРѕРґРїРёСЃРєРё СЃРѕСЃС‚Р°РІР»СЏРµС‚ {subscription_price} {subscription_currency} Р·Р° {subscription_duration} РґРЅРµР№.
2.4. РѕРїР»Р°С‚Р° РїСЂРѕС…РѕРґРёС‚ С‡РµСЂРµР· Р±РµР·РѕРїР°СЃРЅСѓСЋ РїР»Р°С‚РµР¶РЅСѓСЋ СЃРёСЃС‚РµРјСѓ yookassa. РІР°Р¶РЅРѕ: РјС‹ РЅРµ РїРѕР»СѓС‡Р°РµРј Рё РЅРµ С…СЂР°РЅРёРј С‚РІРѕРё РїР»Р°С‚РµР¶РЅС‹Рµ РґР°РЅРЅС‹Рµ (РЅРѕРјРµСЂ РєР°СЂС‚С‹ Рё С‚.Рї.). РІСЃРµ Р±РµР·РѕРїР°СЃРЅРѕ.
2.5. РїРѕР»РёС‚РёРєР° РІРѕР·РІСЂР°С‚РѕРІ: РїРѕРєСѓРїР°СЏ РїРѕРґРїРёСЃРєСѓ, С‚С‹ РїРѕР»СѓС‡Р°РµС€СЊ РґРѕСЃС‚СѓРї Рє СЂР°СЃС€РёСЂРµРЅРЅС‹Рј РІРѕР·РјРѕР¶РЅРѕСЃС‚СЏРј СЃРµСЂРІРёСЃР° СЃСЂР°Р·Сѓ Р¶Рµ РїРѕСЃР»Рµ РѕРїР»Р°С‚С‹. РїРѕСЃРєРѕР»СЊРєСѓ С‚С‹ РїРѕР»СѓС‡Р°РµС€СЊ СѓСЃР»СѓРіСѓ РЅРµРјРµРґР»РµРЅРЅРѕ, РѕРїР»Р°С‡РµРЅРЅС‹Рµ СЃСЂРµРґСЃС‚РІР° Р·Р° СЌС‚РѕС‚ РїРµСЂРёРѕРґ РґРѕСЃС‚СѓРїР°, Рє СЃРѕР¶Р°Р»РµРЅРёСЋ, РЅРµ РїРѕРґР»РµР¶Р°С‚ РІРѕР·РІСЂР°С‚Сѓ.
2.6. РІ СЂРµРґРєРёС… СЃР»СѓС‡Р°СЏС…, РµСЃР»Рё СЃРµСЂРІРёСЃ РѕРєР°Р¶РµС‚СЃСЏ РЅРµРґРѕСЃС‚СѓРїРµРЅ РїРѕ РЅР°С€РµР№ РІРёРЅРµ РІ С‚РµС‡РµРЅРёРµ РґР»РёС‚РµР»СЊРЅРѕРіРѕ РІСЂРµРјРµРЅРё (Р±РѕР»РµРµ 7 РґРЅРµР№ РїРѕРґСЂСЏРґ), Рё Сѓ С‚РµР±СЏ Р±СѓРґРµС‚ Р°РєС‚РёРІРЅР°СЏ РїРѕРґРїРёСЃРєР°, С‚С‹ РјРѕР¶РµС€СЊ РЅР°РїРёСЃР°С‚СЊ РЅР°Рј РІ РїРѕРґРґРµСЂР¶РєСѓ (РєРѕРЅС‚Р°РєС‚ СѓРєР°Р·Р°РЅ РІ Р±РёРѕРіСЂР°С„РёРё Р±РѕС‚Р° Рё РІ РЅР°С€РµРј telegram-РєР°РЅР°Р»Рµ). РјС‹ СЂР°СЃСЃРјРѕС‚СЂРёРј РІРѕР·РјРѕР¶РЅРѕСЃС‚СЊ РїСЂРѕРґР»РёС‚СЊ С‚РІРѕСЋ РїРѕРґРїРёСЃРєСѓ РЅР° СЃСЂРѕРє РЅРµРґРѕСЃС‚СѓРїРЅРѕСЃС‚Рё СЃРµСЂРІРёСЃР°. СЂРµС€РµРЅРёРµ РїСЂРёРЅРёРјР°РµС‚СЃСЏ РёРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕ.

3. С‚РІРѕРё Рё РЅР°С€Рё РїСЂР°РІР° Рё РѕР±СЏР·Р°РЅРЅРѕСЃС‚Рё
3.1. С‡С‚Рѕ РѕР¶РёРґР°РµС‚СЃСЏ РѕС‚ С‚РµР±СЏ (С‚РІРѕРё РѕР±СЏР·Р°РЅРЅРѕСЃС‚Рё):
вЂў   РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ СЃРµСЂРІРёСЃ С‚РѕР»СЊРєРѕ РІ Р·Р°РєРѕРЅРЅС‹С… С†РµР»СЏС… Рё РЅРµ РЅР°СЂСѓС€Р°С‚СЊ РЅРёРєР°РєРёРµ Р·Р°РєРѕРЅС‹ РїСЂРё РµРіРѕ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРё.
вЂў   РЅРµ РїС‹С‚Р°С‚СЊСЃСЏ РІРјРµС€Р°С‚СЊСЃСЏ РІ СЂР°Р±РѕС‚Сѓ СЃРµСЂРІРёСЃР° РёР»Рё РїРѕР»СѓС‡РёС‚СЊ РЅРµСЃР°РЅРєС†РёРѕРЅРёСЂРѕРІР°РЅРЅС‹Р№ РґРѕСЃС‚СѓРї.
вЂў   РЅРµ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ СЃРµСЂРІРёСЃ РґР»СЏ СЂР°СЃСЃС‹Р»РєРё СЃРїР°РјР°, РІСЂРµРґРѕРЅРѕСЃРЅС‹С… РїСЂРѕРіСЂР°РјРј РёР»Рё Р»СЋР±РѕР№ Р·Р°РїСЂРµС‰РµРЅРЅРѕР№ РёРЅС„РѕСЂРјР°С†РёРё.
вЂў   РµСЃР»Рё С‚СЂРµР±СѓРµС‚СЃСЏ (РЅР°РїСЂРёРјРµСЂ, РґР»СЏ РѕРїР»Р°С‚С‹), РїСЂРµРґРѕСЃС‚Р°РІР»СЏС‚СЊ С‚РѕС‡РЅСѓСЋ Рё РїСЂР°РІРґРёРІСѓСЋ РёРЅС„РѕСЂРјР°С†РёСЋ.
вЂў   РїРѕСЃРєРѕР»СЊРєСѓ Сѓ СЃРµСЂРІРёСЃР° РЅРµС‚ РІРѕР·СЂР°СЃС‚РЅС‹С… РѕРіСЂР°РЅРёС‡РµРЅРёР№, С‚С‹ РїРѕРґС‚РІРµСЂР¶РґР°РµС€СЊ СЃРІРѕСЋ СЃРїРѕСЃРѕР±РЅРѕСЃС‚СЊ РїСЂРёРЅСЏС‚СЊ СѓСЃР»РѕРІРёСЏ РЅР°СЃС‚РѕСЏС‰РµРіРѕ СЃРѕРіР»Р°С€РµРЅРёСЏ.
3.2. С‡С‚Рѕ РјРѕР¶РµРј РґРµР»Р°С‚СЊ РјС‹ (РЅР°С€Рё РїСЂР°РІР°):
вЂў   РјС‹ РјРѕР¶РµРј РјРµРЅСЏС‚СЊ СѓСЃР»РѕРІРёСЏ СЌС‚РѕРіРѕ СЃРѕРіР»Р°С€РµРЅРёСЏ. РµСЃР»Рё СЌС‚Рѕ РїСЂРѕРёР·РѕР№РґРµС‚, РјС‹ СѓРІРµРґРѕРјРёРј С‚РµР±СЏ, РѕРїСѓР±Р»РёРєРѕРІР°РІ РЅРѕРІСѓСЋ РІРµСЂСЃРёСЋ СЃРѕРіР»Р°С€РµРЅРёСЏ РІ РЅР°С€РµРј telegram-РєР°РЅР°Р»Рµ РёР»Рё РёРЅС‹Рј РґРѕСЃС‚СѓРїРЅС‹Рј СЃРїРѕСЃРѕР±РѕРј РІ СЂР°РјРєР°С… СЃРµСЂРІРёСЃР°. С‚РІРѕРµ РґР°Р»СЊРЅРµР№С€РµРµ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ СЃРµСЂРІРёСЃР° Р±СѓРґРµС‚ РѕР·РЅР°С‡Р°С‚СЊ СЃРѕРіР»Р°СЃРёРµ СЃ РёР·РјРµРЅРµРЅРёСЏРјРё.
вЂў   РјС‹ РјРѕР¶РµРј РІСЂРµРјРµРЅРЅРѕ РїСЂРёРѕСЃС‚Р°РЅРѕРІРёС‚СЊ РёР»Рё РїРѕР»РЅРѕСЃС‚СЊСЋ РїСЂРµРєСЂР°С‚РёС‚СЊ С‚РІРѕР№ РґРѕСЃС‚СѓРї Рє СЃРµСЂРІРёСЃСѓ, РµСЃР»Рё С‚С‹ РЅР°СЂСѓС€РёС€СЊ СѓСЃР»РѕРІРёСЏ СЌС‚РѕРіРѕ СЃРѕРіР»Р°С€РµРЅРёСЏ.
вЂў   РјС‹ РјРѕР¶РµРј РёР·РјРµРЅСЏС‚СЊ СЃР°Рј СЃРµСЂРІРёСЃ: РґРѕР±Р°РІР»СЏС‚СЊ РёР»Рё СѓР±РёСЂР°С‚СЊ С„СѓРЅРєС†РёРё, РјРµРЅСЏС‚СЊ Р»РёРјРёС‚С‹ РёР»Рё СЃС‚РѕРёРјРѕСЃС‚СЊ РїРѕРґРїРёСЃРєРё.

4. РІР°Р¶РЅРѕРµ РїСЂРµРґСѓРїСЂРµР¶РґРµРЅРёРµ РѕР± РѕРіСЂР°РЅРёС‡РµРЅРёРё РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕСЃС‚Рё
4.1. СЃРµСЂРІРёСЃ РїСЂРµРґРѕСЃС‚Р°РІР»СЏРµС‚СЃСЏ "РєР°Рє РµСЃС‚СЊ". СЌС‚Рѕ Р·РЅР°С‡РёС‚, С‡С‚Рѕ РјС‹ РЅРµ РјРѕР¶РµРј РіР°СЂР°РЅС‚РёСЂРѕРІР°С‚СЊ РµРіРѕ РёРґРµР°Р»СЊРЅСѓСЋ СЂР°Р±РѕС‚Сѓ Р±РµР· СЃР±РѕРµРІ РёР»Рё РѕС€РёР±РѕРє. С‚РµС…РЅРѕР»РѕРіРёРё РёРЅРѕРіРґР° РїРѕРґРІРѕРґСЏС‚, Рё РјС‹ РЅРµ РЅРµСЃРµРј РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕСЃС‚Рё Р·Р° РІРѕР·РјРѕР¶РЅС‹Рµ РїСЂРѕР±Р»РµРјС‹, РІРѕР·РЅРёРєС€РёРµ РЅРµ РїРѕ РЅР°С€РµР№ РїСЂСЏРјРѕР№ РІРёРЅРµ.
4.2. РїРѕРјРЅРё, Р»РёС‡РЅРѕСЃС‚Рё вЂ” СЌС‚Рѕ РёСЃРєСѓСЃСЃС‚РІРµРЅРЅС‹Р№ РёРЅС‚РµР»Р»РєС‚. РёС… РѕС‚РІРµС‚С‹ РіРµРЅРµСЂРёСЂСѓСЋС‚СЃСЏ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё Рё РјРѕРіСѓС‚ Р±С‹С‚СЊ РЅРµС‚РѕС‡РЅС‹РјРё, РЅРµРїРѕР»РЅС‹РјРё, СЃС‚СЂР°РЅРЅС‹РјРё РёР»Рё РЅРµ СЃРѕРѕС‚РІРµС‚СЃС‚РІСѓСЋС‰РёРјРё С‚РІРѕРёРј РѕР¶РёРґР°РЅРёСЏРј РёР»Рё СЂРµР°Р»СЊРЅРѕСЃС‚Рё. РјС‹ РЅРµ РЅРµСЃРµРј РЅРёРєР°РєРѕР№ РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕСЃС‚Рё Р·Р° СЃРѕРґРµСЂР¶Р°РЅРёРµ РѕС‚РІРµС‚РѕРІ, СЃРіРµРЅРµСЂРёСЂРѕРІР°РЅРЅС‹С… ai-СЃРѕР±РµСЃРµРґРЅРёРєР°РјРё. РЅРµ РІРѕСЃРїСЂРёРЅРёРјР°Р№ РёС… РєР°Рє РёСЃС‚РёРЅСѓ РІ РїРѕСЃР»РµРґРЅРµР№ РёРЅСЃС‚Р°РЅС†РёРё РёР»Рё РїСЂРѕС„РµСЃСЃРёРѕРЅР°Р»СЊРЅС‹Р№ СЃРѕРІРµС‚.
4.3. РјС‹ РЅРµ РЅРµСЃРµРј РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕСЃС‚Рё Р·Р° Р»СЋР±С‹Рµ РїСЂСЏРјС‹Рµ РёР»Рё РєРѕСЃРІРµРЅРЅС‹Рµ СѓР±С‹С‚РєРё РёР»Рё СѓС‰РµСЂР±, РєРѕС‚РѕСЂС‹Р№ С‚С‹ РјРѕРі РїРѕРЅРµСЃС‚Рё РІ СЂРµР·СѓР»СЊС‚Р°С‚Рµ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ (РёР»Рё РЅРµРІРѕР·РјРѕР¶РЅРѕСЃС‚Рё РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ) СЃРµСЂРІРёСЃР°.

5. РїСЂРѕ С‚РІРѕРё РґР°РЅРЅС‹Рµ (РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚СЊ)
5.1. РґР»СЏ СЂР°Р±РѕС‚С‹ СЃРµСЂРІРёСЃР° РЅР°Рј РїСЂРёС…РѕРґРёС‚СЃСЏ СЃРѕР±РёСЂР°С‚СЊ Рё РѕР±СЂР°Р±Р°С‚С‹РІР°С‚СЊ РјРёРЅРёРјР°Р»СЊРЅС‹Рµ РґР°РЅРЅС‹Рµ: С‚РІРѕР№ telegram id (РґР»СЏ РёРґРµРЅС‚РёС„РёРєР°С†РёРё Р°РєРєР°СѓРЅС‚Р°), РёРјСЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ telegram (username, РµСЃР»Рё РµСЃС‚СЊ), РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ С‚РІРѕРµР№ РїРѕРґРїРёСЃРєРµ, РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ СЃРѕР·РґР°РЅРЅС‹С… С‚РѕР±РѕР№ Р»РёС‡РЅРѕСЃС‚СЏС…, Р° С‚Р°РєР¶Рµ РёСЃС‚РѕСЂРёСЋ С‚РІРѕРёС… СЃРѕРѕР±С‰РµРЅРёР№ СЃ Р»РёС‡РЅРѕСЃС‚СЏРјРё (СЌС‚Рѕ РЅСѓР¶РЅРѕ ai РґР»СЏ РїРѕРґРґРµСЂР¶Р°РЅРёСЏ РєРѕРЅС‚РµРєСЃС‚Р° СЂР°Р·РіРѕРІРѕСЂР°).
5.2. РјС‹ РїСЂРµРґРїСЂРёРЅРёРјР°РµРј СЂР°Р·СѓРјРЅС‹Рµ С€Р°РіРё РґР»СЏ Р·Р°С‰РёС‚С‹ С‚РІРѕРёС… РґР°РЅРЅС‹С…, РЅРѕ, РїРѕР¶Р°Р»СѓР№СЃС‚Р°, РїРѕРјРЅРё, С‡С‚Рѕ РїРµСЂРµРґР°С‡Р° РёРЅС„РѕСЂРјР°С†РёРё С‡РµСЂРµР· РёРЅС‚РµСЂРЅРµС‚ РЅРёРєРѕРіРґР° РЅРµ РјРѕР¶РµС‚ Р±С‹С‚СЊ Р°Р±СЃРѕР»СЋС‚РЅРѕ Р±РµР·РѕРїР°СЃРЅРѕР№.

6. РґРµР№СЃС‚РІРёРµ СЃРѕРіР»Р°С€РµРЅРёСЏ
6.1. РЅР°СЃС‚РѕСЏС‰РµРµ СЃРѕРіР»Р°С€РµРЅРёРµ РЅР°С‡РёРЅР°РµС‚ РґРµР№СЃС‚РІРѕРІР°С‚СЊ СЃ РјРѕРјРµРЅС‚Р°, РєР°Рє С‚С‹ РІРїРµСЂРІС‹Рµ РёСЃРїРѕР»СЊР·СѓРµС€СЊ СЃРµСЂРІРёСЃ, Рё РґРµР№СЃС‚РІСѓРµС‚ РґРѕ РјРѕРјРµРЅС‚Р°, РїРѕРєР° С‚С‹ РЅРµ РїРµСЂРµСЃС‚Р°РЅРµС€СЊ РёРј РїРѕР»СЊР·РѕРІР°С‚СЊСЃСЏ РёР»Рё РїРѕРєР° СЃРµСЂРІРёСЃ РЅРµ РїСЂРµРєСЂР°С‚РёС‚ СЃРІРѕСЋ СЂР°Р±РѕС‚Сѓ.

7. РёРЅС‚РµР»Р»РєС‚СѓР°Р»СЊРЅР°СЏ СЃРѕР±СЃС‚РІРµРЅРЅРѕСЃС‚СЊ
7.1. С‚С‹ СЃРѕС…СЂР°РЅСЏРµС€СЊ РІСЃРµ РїСЂР°РІР° РЅР° РєРѕРЅС‚РµРЅС‚ (С‚РµРєСЃС‚), РєРѕС‚РѕСЂС‹Р№ С‚С‹ СЃРѕР·РґР°РµС€СЊ Рё РІРІРѕРґРёС€СЊ РІ СЃРµСЂРІРёСЃ РІ РїСЂРѕС†РµСЃСЃРµ РІР·Р°РёРјРѕРґРµР№СЃС‚РІРёСЏ СЃ ai-СЃРѕР±РµСЃРµРґРЅРёРєР°РјРё.
7.2. С‚С‹ РїСЂРµРґРѕСЃС‚Р°РІР»СЏРµС€СЊ РЅР°Рј РЅРµРёСЃРєР»СЋС‡РёС‚РµР»СЊРЅСѓСЋ, Р±РµР·РІРѕР·РјРµР·РґРЅСѓСЋ, РґРµР№СЃС‚РІСѓСЋС‰СѓСЋ РїРѕ РІСЃРµРјСѓ РјРёСЂСѓ Р»РёС†РµРЅР·РёСЋ РЅР° РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ С‚РІРѕРµРіРѕ РєРѕРЅС‚РµРЅС‚Р° РёСЃРєР»СЋС‡РёС‚РµР»СЊРЅРѕ РІ С†РµР»СЏС… РїСЂРµРґРѕСЃС‚Р°РІР»РµРЅРёСЏ, РїРѕРґРґРµСЂР¶Р°РЅРёСЏ Рё СѓР»СѓС‡С€РµРЅРёСЏ СЂР°Р±РѕС‚С‹ СЃРµСЂРІРёСЃР° (РЅР°РїСЂРёРјРµСЂ, РґР»СЏ РѕР±СЂР°Р±РѕС‚РєРё С‚РІРѕРёС… Р·Р°РїСЂРѕСЃРѕРІ, СЃРѕС…СЂР°РЅРµРЅРёСЏ РєРѕРЅС‚РµРєСЃС‚Р° РґРёР°Р»РѕРіР°, Р°РЅРѕРЅРёРјРЅРѕРіРѕ Р°РЅР°Р»РёР·Р° РґР»СЏ СѓР»СѓС‡С€РµРЅРёСЏ РјРѕРґРµР»РµР№, РµСЃР»Рё РїСЂРёРјРµРЅРёРјРѕ).
7.3. РІСЃРµ РїСЂР°РІР° РЅР° СЃР°Рј СЃРµСЂРІРёСЃ (РєРѕРґ Р±РѕС‚Р°, РґРёР·Р°Р№РЅ, РЅР°Р·РІР°РЅРёРµ, РіСЂР°С„РёС‡РµСЃРєРёРµ СЌР»РµРјРµРЅС‚С‹ Рё С‚.Рґ.) РїСЂРёРЅР°РґР»РµР¶Р°С‚ РІР»Р°РґРµР»СЊС†Сѓ СЃРµСЂРІРёСЃР°.
7.4. РѕС‚РІРµС‚С‹, СЃРіРµРЅРµСЂРёСЂРѕРІР°РЅРЅС‹Рµ ai-СЃРѕР±РµСЃРµРґРЅРёРєР°РјРё, СЏРІР»СЏСЋС‚СЃСЏ СЂРµР·СѓР»СЊС‚Р°С‚РѕРј СЂР°Р±РѕС‚С‹ Р°Р»РіРѕСЂРёС‚РјРѕРІ РёСЃРєСѓСЃСЃС‚РІРµРЅРЅРѕРіРѕ РёРЅС‚РµР»Р»РєС‚Р°. С‚С‹ РјРѕР¶РµС€СЊ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РїРѕР»СѓС‡РµРЅРЅС‹Рµ РѕС‚РІРµС‚С‹ РІ Р»РёС‡РЅС‹С… РЅРµРєРѕРјРјРµСЂС‡РµСЃРєРёС… С†РµР»СЏС…, РЅРѕ РїСЂРёР·РЅР°РµС€СЊ, С‡С‚Рѕ РѕРЅРё СЃРѕР·РґР°РЅС‹ РјР°С€РёРЅРѕР№ Рё РЅРµ СЏРІР»СЏСЋС‚СЃСЏ С‚РІРѕРµР№ РёР»Рё РЅР°С€РµР№ РёРЅС‚РµР»Р»РєС‚СѓР°Р»СЊРЅРѕР№ СЃРѕР±СЃС‚РІРµРЅРЅРѕСЃС‚СЊСЋ РІ С‚СЂР°РґРёС†РёРѕРЅРЅРѕРј РїРѕРЅРёРјР°РЅРёРё.

8. Р·Р°РєР»СЋС‡РёС‚РµР»СЊРЅС‹Рµ РїРѕР»РѕР¶РµРЅРёСЏ
8.1. РІСЃРµ СЃРїРѕСЂС‹ Рё СЂР°Р·РЅРѕРіР»Р°СЃРёСЏ СЂРµС€Р°СЋС‚СЃСЏ РїСѓС‚РµРј РїРµСЂРµРіРѕРІРѕСЂРѕРІ. РµСЃР»Рё СЌС‚Рѕ РЅРµ РїРѕРјРѕР¶РµС‚, СЃРїРѕСЂС‹ Р±СѓРґСѓС‚ СЂР°СЃСЃРјР°С‚СЂРёРІР°С‚СЊСЃСЏ РІ СЃРѕРѕС‚РІРµС‚СЃС‚РІРёРё СЃ Р·Р°РєРѕРЅРѕРґР°С‚РµР»СЃС‚РІРѕРј СЂРѕСЃСЃРёР№СЃРєРѕР№ С„РµРґРµСЂР°С†РёРё.
8.2. РїРѕ РІСЃРµРј РІРѕРїСЂРѕСЃР°Рј, РєР°СЃР°СЋС‰РёРјСЃСЏ РЅР°СЃС‚РѕСЏС‰РµРіРѕ СЃРѕРіР»Р°С€РµРЅРёСЏ РёР»Рё СЂР°Р±РѕС‚С‹ СЃРµСЂРІРёСЃР°, С‚С‹ РјРѕР¶РµС€СЊ РѕР±СЂР°С‰Р°С‚СЊСЃСЏ Рє РЅР°Рј С‡РµСЂРµР· РєРѕРЅС‚Р°РєС‚С‹, СѓРєР°Р·Р°РЅРЅС‹Рµ РІ Р±РёРѕРіСЂР°С„РёРё Р±РѕС‚Р° Рё РІ РЅР°С€РµРј telegram-РєР°РЅР°Р»Рµ.
"""
formatted_tos_text_for_bot = TOS_TEXT_RAW.format(
    subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
    subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}", # Format as integer
    subscription_currency=config.SUBSCRIPTION_CURRENCY
)
TOS_TEXT = escape_markdown_v2(formatted_tos_text_for_bot)

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(context.error, Forbidden):
         if CHANNEL_ID and str(CHANNEL_ID) in str(context.error):
             logger.warning(f"Error handler caught Forbidden regarding channel {CHANNEL_ID}. Bot likely not admin or kicked.")
             return
         else:
             logger.warning(f"Caught generic Forbidden error: {context.error}")
             return

    elif isinstance(context.error, BadRequest):
        error_text = str(context.error).lower()
        if "message is not modified" in error_text:
            logger.info("Ignoring 'message is not modified' error.")
            return
        elif "can't parse entities" in error_text:
            logger.error(f"MARKDOWN PARSE ERROR: {context.error}. Update: {update}")
            if isinstance(update, Update) and update.effective_message:
                try:
                    await update.effective_message.reply_text("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё С„РѕСЂРјР°С‚РёСЂРѕРІР°РЅРёРё РѕС‚РІРµС‚Р°. РїРѕР¶Р°Р»СѓР№СЃС‚Р°, СЃРѕРѕР±С‰РёС‚Рµ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂСѓ.", parse_mode=None)
                except Exception as send_err:
                    logger.error(f"Failed to send plain text formatting error message: {send_err}")
            return
        elif "chat member status is required" in error_text:
             logger.warning(f"Error handler caught BadRequest likely related to missing channel membership check: {context.error}")
             return
        elif "chat not found" in error_text:
             logger.error(f"BadRequest: Chat not found error: {context.error}")
             return
        elif "reply message not found" in error_text:
            logger.warning(f"BadRequest: Reply message not found. Original message might have been deleted. Update: {update}")
            return
        else:
             logger.error(f"Unhandled BadRequest error: {context.error}")

    elif isinstance(context.error, TimedOut):
         logger.warning(f"Telegram API request timed out: {context.error}")
         return

    elif isinstance(context.error, TelegramError):
         logger.error(f"Generic Telegram API error: {context.error}")

    error_message_raw = "СѓРїСЃ... рџ• С‡С‚Рѕ-С‚Рѕ РїРѕС€Р»Рѕ РЅРµ С‚Р°Рє. РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ СЂР°Р· РїРѕР·Р¶Рµ."
    escaped_error_message = escape_markdown_v2(error_message_raw)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(escaped_error_message, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_md:
             if "can't parse entities" in str(e_md).lower():
                 logger.error(f"Failed sending even basic Markdown error msg ({e_md}). Sending plain.")
                 try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                 except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
             else:
                 logger.error(f"Failed sending error message (BadRequest, not parse): {e_md}")
                 try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                 except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")
            try:
                 await update.effective_message.reply_text(error_message_raw, parse_mode=None)
            except Exception as final_e:
                 logger.error(f"Failed even sending plain text error message: {final_e}")


# --- Core Logic Helpers ---

def get_persona_and_context_with_owner(chat_id: Union[str, int], db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    """Fetches the active Persona, its context, and its owner User for a given chat."""
    chat_id_str = str(chat_id)
    chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id_str)
    if not chat_instance:
        return None

    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id_str} is missing linked BotInstance.")
         return None
    if not bot_instance.persona_config:
         logger.error(f"BotInstance {bot_instance.id} (linked to chat {chat_id_str}) is missing linked PersonaConfig.")
         return None
    owner_user = bot_instance.owner or bot_instance.persona_config.owner
    if not owner_user:
         logger.error(f"Could not load Owner for BotInstance {bot_instance.id} (linked to chat {chat_id_str}).")
         return None

    persona_config = bot_instance.persona_config

    try:
        persona = Persona(persona_config, chat_instance)
    except ValueError as e:
         logger.error(f"Failed to initialize Persona for config {persona_config.id} in chat {chat_id_str}: {e}", exc_info=True)
         return None

    context_list = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context_list, owner_user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]], is_decision_prompt: bool = False) -> str:
    """Sends the prompt and context to the Langdock API and returns the response."""
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        # Р”Р»СЏ decision prompt РІРѕР·РІСЂР°С‰Р°РµРј РїСѓСЃС‚СѓСЋ СЃС‚СЂРѕРєСѓ, С‡С‚РѕР±С‹ РїРѕРєР°Р·Р°С‚СЊ РѕС€РёР±РєСѓ РєРѕРЅС„РёРіСѓСЂР°С†РёРё
        return "" if is_decision_prompt else escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РєР»СЋС‡ api РЅРµ РЅР°СЃС‚СЂРѕРµРЅ.")

    # Р•СЃР»Рё СЌС‚Рѕ РїСЂРѕРјРїС‚ РґР»СЏ СЂРµС€РµРЅРёСЏ, messages РјРѕР¶РµС‚ Р±С‹С‚СЊ РїСѓСЃС‚С‹Рј
    if not messages and not is_decision_prompt:
        logger.error("send_to_langdock called with an empty messages list for non-decision prompt!")
        return "РѕС€РёР±РєР°: РЅРµС‚ СЃРѕРѕР±С‰РµРЅРёР№ РґР»СЏ РѕС‚РїСЂР°РІРєРё РІ ai."

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    messages_to_send = messages[-MAX_CONTEXT_MESSAGES_SENT_TO_LLM:] if messages else []

    # РЈСЃС‚Р°РЅР°РІР»РёРІР°РµРј РїР°СЂР°РјРµС‚СЂС‹ РґР»СЏ СЂР°Р·РЅС‹С… С‚РёРїРѕРІ РїСЂРѕРјРїС‚РѕРІ
    if is_decision_prompt:
        # Р”Р»СЏ РїСЂРѕСЃС‚С‹С… Р”Р°/РќРµС‚ РїСЂРѕРјРїС‚РѕРІ РјРѕР¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ Р±РѕР»РµРµ РґРµС€РµРІСѓСЋ РјРѕРґРµР»СЊ Рё РјРµРЅСЊС€Рµ С‚РѕРєРµРРЅРѕРІ
        model = "claude-3-haiku-20240307" # РР»Рё РґСЂСѓРіР°СЏ Р±С‹СЃС‚СЂР°СЏ/РґРµС€РµРІР°СЏ РјРѕРґРµР»СЊ
        max_tokens = 10 # РќСѓР¶РЅРѕ С‚РѕР»СЊРєРѕ "Р”Р°" РёР»Рё "РќРµС‚"
        temperature = 0.1
        top_p = 0.5
        messages_payload = [] # Р”Р»СЏ decision prompt РёСЃС‚РѕСЂРёСЏ РЅРµ РЅСѓР¶РЅР°
    else:
        # РџР°СЂР°РјРµС‚СЂС‹ РґР»СЏ РіРµРЅРµСЂР°С†РёРё РѕСЃРЅРѕРІРЅРѕРіРѕ РѕС‚РІРµС‚Р°
        model = LANGDOCK_MODEL
        max_tokens = 1024
        temperature = 0.65
        top_p = 0.95
        messages_payload = messages_to_send

    payload = {
        "model": model,
        "system": system_prompt,
        "messages": messages_payload,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": False
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock: {url} (Model: {model}, DecisionPrompt: {is_decision_prompt}, SysPromptLen: {len(system_prompt)}, Msgs: {len(messages_payload)})")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
             resp = await client.post(url, json=payload, headers=headers)
        logger.debug(f"Langdock response status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

        full_response = ""
        content = data.get("content")

        if isinstance(content, list) and content:
            first_content_block = content[0]
            if isinstance(first_content_block, dict) and first_content_block.get("type") == "text":
                full_response = first_content_block.get("text", "")
        elif isinstance(content, dict) and "text" in content:
            full_response = content["text"]
        elif isinstance(content, str):
             full_response = content
        elif "response" in data and isinstance(data["response"], str):
             full_response = data.get("response", "")
        elif "choices" in data and isinstance(data["choices"], list) and data["choices"]:
             choice = data["choices"][0]
             if "message" in choice and isinstance(choice["message"], dict) and "content" in choice["message"]:
                 full_response = choice["message"]["content"]
             elif "text" in choice:
                 full_response = choice["text"]

        if not full_response:
             stop_reason = data.get('stop_reason')
             logger.warning(f"Could not extract text from Langdock response structure (Content: {content}, StopReason: {stop_reason}). Response data: {data}")
             # Р”Р»СЏ decision prompt РІРѕР·РІСЂР°С‰Р°РµРј РїСѓСЃС‚СѓСЋ СЃС‚СЂРѕРєСѓ
             return "" if is_decision_prompt else escape_markdown_v2("ai РІРµСЂРЅСѓР» РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚ рџ¤·")

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return "" if is_decision_prompt else escape_markdown_v2("вЏі С…Рј, РєР°Р¶РµС‚СЃСЏ, СЏ СЃР»РёС€РєРѕРј РґРѕР»РіРѕ РґСѓРјР°Р»... РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ СЂР°Р·?")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {error_body}", exc_info=False)
        error_text_raw = f"РѕР№, РѕС€РёР±РєР° СЃРІСЏР·Рё СЃ ai ({e.response.status_code})"
        try:
             error_data = json.loads(error_body)
             if isinstance(error_data.get('error'), dict) and 'message' in error_data['error']:
                  api_error_msg = error_data['error']['message']
                  logger.error(f"Langdock API Error Message: {api_error_msg}")
             elif isinstance(error_data.get('error'), str):
                   logger.error(f"Langdock API Error Message: {error_data['error']}")
        except Exception: pass
        return error_text_raw
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return escape_markdown_v2("вќЊ РЅРµ РјРѕРіСѓ СЃРІСЏР·Р°С‚СЊСЃСЏ СЃ ai СЃРµР№С‡Р°СЃ (РѕС€РёР±РєР° СЃРµС‚Рё)...")
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return escape_markdown_v2("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РІРЅСѓС‚СЂРµРЅРЅСЏСЏ РѕС€РёР±РєР° РїСЂРё РіРµРЅРµСЂР°С†РёРё РѕС‚РІРµС‚Р°.")


async def process_and_send_response(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: Union[str, int],
    persona: Persona,
    full_bot_response_text: str,
    db: Session,
    reply_to_message_id: Optional[int] = None,
    is_first_message: bool = False
) -> bool:
    """Processes the AI response, adds it to context, extracts GIFs, splits text, and sends messages."""
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return False

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}. ReplyTo: {reply_to_message_id}. IsFirstMsg: {is_first_message}")

    chat_id_str = str(chat_id)
    context_prepared = False

    # РЎРѕС…СЂР°РЅРµРЅРёРµ РѕС‚РІРµС‚Р° РІ РєРѕРЅС‚РµРєСЃС‚
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            logger.debug("AI response prepared for database context (pending commit).")
            context_prepared = True
        except SQLAlchemyError as e:
            logger.error(f"DB Error preparing assistant response for context chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected Error preparing assistant response for context chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
    else:
        logger.error("Cannot add AI response to context, chat_instance is None.")

    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    for gif in gif_links:
        all_text_content = re.sub(r'\s*' + re.escape(gif) + r'\s*', " ", all_text_content, flags=re.IGNORECASE).strip()
    all_text_content = re.sub(r'\s{2,}', ' ', all_text_content).strip()

    # --- РР—РњР•РќР•РќРР•: РџРѕР»СѓС‡Р°РµРј РЅР°СЃС‚СЂРѕР№РєСѓ РёР· persona ---
    max_messages_setting = persona.max_response_messages # Р‘РµСЂРµРј РїСЂСЏРјРѕ РёР· РѕР±СЉРµРєС‚Р° Persona
    max_messages = 3 # Default
    if max_messages_setting <= 0:
        # РЎР»СѓС‡Р°Р№РЅРѕРµ 1-3, РµСЃР»Рё РЅР°СЃС‚СЂРѕР№РєР° 0 РёР»Рё РјРµРЅСЊС€Рµ (РёСЃРїРѕР»СЊР·СѓРµРј 0 РєР°Рє С„Р»Р°Рі "СЃР»СѓС‡Р°Р№РЅРѕ")
        max_messages = random.randint(1, 3)
        logger.debug(f"Using random max_messages (1-3) because setting is {max_messages_setting}. Chosen: {max_messages}")
    elif 1 <= max_messages_setting <= 10: # РџСЂРёРЅРёРјР°РµРј Р·РЅР°С‡РµРЅРёСЏ РѕС‚ 1 РґРѕ 10
        max_messages = max_messages_setting
        logger.debug(f"Using max_messages={max_messages} from persona config.")
    else:
        logger.warning(f"Invalid max_response_messages value ({max_messages_setting}) for persona {persona.id}. Using default: {max_messages}")
    # --- РљРћРќР•Р¦ РР—РњР•РќР•РќРРЇ ---

    text_parts_to_send = postprocess_response(all_text_content, max_messages) # РџРµСЂРµРґР°РµРј СЂР°СЃСЃС‡РёС‚Р°РЅРЅРѕРµ Р·РЅР°С‡РµРЅРёРµ
    logger.debug(f"postprocess_response (with max_messages={max_messages}) resulted in {len(text_parts_to_send)} parts.")

    # --- РћСЃС‚Р°РІР»СЏРµРј WORKAROUND РґР»СЏ РїСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕР№ СЂР°Р·Р±РёРІРєРё --- ## РЈСЃР»РѕРІРёРµ РЅР° РґР»РёРЅСѓ > 150 РґРѕР±Р°РІР»РµРЅРѕ
    if len(text_parts_to_send) == 1 and max_messages > 1 and len(text_parts_to_send[0]) > 150:
        logger.info(f"Only 1 long part returned, but max_messages={max_messages}. Attempting sentence split.")
        sentences = re.split(r'(?<=[.!?вЂ¦])\s+', text_parts_to_send[0])
        potential_parts = [s.strip() for s in sentences if s.strip()]
        if len(potential_parts) > 1:
            logger.info(f"Splitting into {len(potential_parts)} sentences.")
            text_parts_to_send = potential_parts
            # РћР±СЂРµР·Р°РµРј РґРѕ max_messages, РµСЃР»Рё РїСЂРµРґР»РѕР¶РµРЅРёР№ Р±РѕР»СЊС€Рµ
            if len(text_parts_to_send) > max_messages:
                logger.info(f"Trimming sentences from {len(text_parts_to_send)} to {max_messages}.")
                text_parts_to_send = text_parts_to_send[:max_messages]
                if text_parts_to_send and text_parts_to_send[-1]:
                    last_part = text_parts_to_send[-1].rstrip('.!?вЂ¦ ')
                    text_parts_to_send[-1] = f"{last_part}..."

    # --- РљРћРќР•Р¦ WORKAROUND ---

    send_tasks = []
    first_message_sent = False

    # --- РћС‚РїСЂР°РІРєР° GIF (Р±РµР· РёР·РјРµРЅРµРЅРёР№) ---
    for gif in gif_links:
        try:
            current_reply_id = reply_to_message_id if not first_message_sent else None
            send_tasks.append(context.bot.send_animation(
                chat_id=chat_id, # РСЃРїРѕР»СЊР·СѓРµРј chat_id РЅР°РїСЂСЏРјСѓСЋ
                animation=gif,
                reply_to_message_id=current_reply_id
            ))
            first_message_sent = True
            logger.info(f"Scheduled sending gif: {gif} (ReplyTo: {current_reply_id})")
        except Exception as e:
            logger.error(f"Error scheduling gif send {gif} to chat {chat_id}: {e}", exc_info=True)

    # --- РћС‚РїСЂР°РІРєР° С‚РµРєСЃС‚Р° ---
    if text_parts_to_send:
        chat_type = None
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_type = update.effective_chat.type

        # --- WORKAROUND: РЈРґР°Р»РµРЅРёРµ РїСЂРёРІРµС‚СЃС‚РІРёСЏ РёР· РїРµСЂРІРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ --- ## РћСЃС‚Р°РІР»СЏРµРј Р±РµР· РёР·РјРµРЅРµРЅРёР№
        if text_parts_to_send and not is_first_message:
            first_part = text_parts_to_send[0]
            greetings_pattern = r"^\s*(?:РїСЂРёРІРµС‚|Р·РґСЂР°РІСЃС‚РІСѓР№|РґРѕР±СЂ(?:С‹Р№|РѕРµ|РѕРіРѕ)\s+(?:РґРµРЅСЊ|СѓС‚СЂРѕ|РІРµС‡РµСЂ)|С…Р°Р№|РєСѓ|Р·РґРѕСЂРѕРІРѕ|СЃР°Р»СЋС‚|Рѕ[Р№Рё])(?:[,.!?\s]|\b)"
            match = re.match(greetings_pattern, first_part, re.IGNORECASE)
            if match:
                end_of_greeting = match.end()
                cleaned_part = first_part[end_of_greeting:].strip()
                if cleaned_part:
                    logger.warning(f"Removed greeting from first message part. Original: '{first_part[:50]}...' New: '{cleaned_part[:50]}...'")
                    text_parts_to_send[0] = cleaned_part
                else:
                    logger.warning(f"Greeting removal resulted in empty first part. Original: '{first_part[:50]}...' Removing part.")
                    text_parts_to_send.pop(0)
        # --- РљРћРќР•Р¦ WORKAROUND ---

        if not text_parts_to_send:
             logger.warning("No text parts left to send after removing greeting.")
        else:
            for i, part in enumerate(text_parts_to_send):
                part_raw = part.strip()
                if not part_raw: continue

                if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                     try:
                         asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING))
                         await asyncio.sleep(random.uniform(0.6, 1.2))
                     except Exception as e:
                          logger.warning(f"Failed to send typing action to {chat_id}: {e}")

                current_reply_id = reply_to_message_id if not first_message_sent else None

                try:
                     escaped_part = escape_markdown_v2(part_raw)
                     logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id} (MDv2, ReplyTo: {current_reply_id}): '{escaped_part[:50]}...'")
                     send_tasks.append(context.bot.send_message(
                         chat_id=chat_id, # РСЃРїРѕР»СЊР·СѓРµРј chat_id РЅР°РїСЂСЏРјСѓСЋ
                         text=escaped_part,
                         parse_mode=ParseMode.MARKDOWN_V2,
                         reply_to_message_id=current_reply_id
                     ))
                     first_message_sent = True
                except BadRequest as e:
                     if "can't parse entities" in str(e).lower():
                          logger.error(f"Error sending part {i+1} (MarkdownV2 parse failed): {e} - Original: '{part_raw[:100]}...' Escaped: '{escaped_part[:100]}...'")
                          try:
                               logger.info(f"Retrying part {i+1} as plain text.")
                               send_tasks.append(context.bot.send_message(
                                   chat_id=chat_id, # РСЃРїРѕР»СЊР·СѓРµРј chat_id РЅР°РїСЂСЏРјСѓСЋ
                                   text=part_raw,
                                   parse_mode=None,
                                   reply_to_message_id=current_reply_id
                               ))
                               first_message_sent = True
                          except Exception as plain_e:
                               logger.error(f"Failed to schedule part {i+1} even as plain text: {plain_e}")
                     elif "reply message not found" in str(e).lower():
                         logger.warning(f"Cannot reply to message {reply_to_message_id} (likely deleted). Sending part {i+1} without reply.")
                         try:
                             escaped_part = escape_markdown_v2(part_raw)
                             send_tasks.append(context.bot.send_message(
                                 chat_id=chat_id, # РСЃРїРѕР»СЊР·СѓРµРј chat_id РЅР°РїСЂСЏРјСѓСЋ
                                 text=escaped_part,
                                 parse_mode=ParseMode.MARKDOWN_V2,
                                 reply_to_message_id=None
                             ))
                             first_message_sent = True
                         except Exception as retry_e:
                             logger.error(f"Failed to schedule part {i+1} even without reply: {retry_e}")
                     else:
                         logger.error(f"Error scheduling text part {i+1} send (BadRequest, not parse): {e} - Original: '{part_raw[:100]}...' Escaped: '{escaped_part[:100]}...'")
                except Exception as e:
                     logger.error(f"Error scheduling text part {i+1} send: {e}", exc_info=True)
                     break

    if send_tasks:
         results = await asyncio.gather(*send_tasks, return_exceptions=True)
         for i, result in enumerate(results):
              if isinstance(result, Exception):
                  error_type = type(result).__name__
                  error_msg = str(result)
                  if "reply message not found" in error_msg.lower():
                      logger.info(f"Sending message part {i} failed because reply target was lost (expected).")
                  else:
                      logger.error(f"Failed to send message/animation part {i}: {error_type} - {error_msg}")

    return context_prepared


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Sends a message informing the user they've hit their daily limit."""
    count_raw = f"{user.daily_message_count}/{user.message_limit}"
    price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
    currency_raw = SUBSCRIPTION_CURRENCY
    paid_limit_raw = str(PAID_DAILY_MESSAGE_LIMIT)
    paid_persona_raw = str(PAID_PERSONA_LIMIT)

    text_raw = (
        f"СѓРїСЃ! рџ• Р»РёРјРёС‚ СЃРѕРѕР±С‰РµРЅРёР№ ({count_raw}) РЅР° СЃРµРіРѕРґРЅСЏ РґРѕСЃС‚РёРіРЅСѓС‚.\n\n"
        f"вњЁ С…РѕС‡РµС€СЊ Р±РѕР»СЊС€РµРіРѕ? вњЁ\n"
        f"РїРѕРґРїРёСЃРєР° Р·Р° {price_raw} {currency_raw}/РјРµСЃ РґР°РµС‚:\n"
        f"вњ… РґРѕ {paid_limit_raw} СЃРѕРѕР±С‰РµРЅРёР№ РІ РґРµРЅСЊ\n"
        f"вњ… РґРѕ {paid_persona_raw} Р»РёС‡РЅРѕСЃС‚РµР№\n"
        f"вњ… РїРѕР»РЅР°СЏ РЅР°СЃС‚СЂРѕР№РєР° РїРѕРІРµРґРµРЅРёСЏ Рё РЅР°СЃС‚СЂРѕРµРЅРёР№\n\n" # РћР±РЅРѕРІР»РµРЅ С‚РµРєСЃС‚
        f"рџ‘‡ Р¶РјРё /subscribe РёР»Рё РєРЅРѕРїРєСѓ РЅРёР¶Рµ!"
    )
    text_to_send = escape_markdown_v2(text_raw)

    keyboard = [[InlineKeyboardButton("рџљЂ РїРѕР»СѓС‡РёС‚СЊ РїРѕРґРїРёСЃРєСѓ!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = None
    try:
        target_chat_id = update.effective_chat.id if update.effective_chat else user.telegram_id
        if target_chat_id:
             await context.bot.send_message(target_chat_id, text=text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
             logger.warning(f"Could not send limit exceeded message to user {user.telegram_id}: no effective chat.")
    except BadRequest as e:
         logger.error(f"Failed sending limit message (BadRequest): {e} - Text Raw: '{text_raw[:100]}...' Escaped: '{text_to_send[:100]}...'")
         try:
              if target_chat_id:
                  await context.bot.send_message(target_chat_id, text_raw, reply_markup=reply_markup, parse_mode=None)
         except Exception as final_e:
              logger.error(f"Failed sending limit message even plain: {final_e}")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


# --- Message Handlers ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages."""
    if not update.message or not (update.message.text or update.message.caption):
        return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    message_id = update.message.message_id
    if not message_text:
        return

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str} (MsgID: {message_id}): {message_text[:100]}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)

            if not persona_context_owner_tuple:
                logger.warning(f"handle_message: get_persona_and_context_with_owner returned None for chat {chat_id_str}. No active persona found?")
                return
            persona, initial_context_from_db, owner_user = persona_context_owner_tuple
            logger.info(f"handle_message: Found active persona '{persona.name}' for chat {chat_id_str}.")

            # --- Проверка лимитов (как раньше) ---
            limit_ok = check_and_update_user_limits(db, owner_user)
            limit_state_updated = db.is_modified(owner_user)
            if not limit_ok:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}).")
                await send_limit_exceeded_message(update, context, owner_user)
                if limit_state_updated: db.commit()
                return
            # --- Конец проверки лимитов ---

            # --- Добавление сообщения пользователя в контекст (как раньше) ---
            current_user_message_content = f"{username}: {message_text}"
            current_user_message_dict = {"role": "user", "content": current_user_message_content}
            context_user_msg_added = False
            if persona.chat_instance:
                try:
                    add_message_to_context(db, persona.chat_instance.id, "user", current_user_message_content)
                    context_user_msg_added = True
                    logger.debug("User message prepared for context DB (pending commit).")
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"Error preparing user message for context DB: {e_ctx}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("❌ ошибка при сохранении вашего сообщения."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else:
                logger.error("Cannot add user message to context DB, chat_instance is None unexpectedly.")
                await update.message.reply_text(escape_markdown_v2("❌ системная ошибка: не удалось связать сообщение с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return
            # --- Конец добавления ---

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted. Saving context and exiting.")
                if limit_state_updated or context_user_msg_added: db.commit()
                return

            # --- ЛОГИКА ОТВЕТА В ГРУППЕ (ОБНОВЛЕННАЯ) ---
            should_ai_respond = True # По умолчанию отвечаем (для ЛС и префа 'always')
            # llm_decision_made = False # Флаг больше не нужен

            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                reply_pref = persona.group_reply_preference
                bot_username = context.bot_data.get('bot_username', "YOUR_BOT_USERNAME")
                persona_name_lower = persona.name.lower()

                # 1. Проверка явных триггеров
                is_mentioned = f"@{bot_username}".lower() in message_text.lower()
                is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
                contains_persona_name = bool(re.search(rf'\b{re.escape(persona_name_lower)}\b', message_text.lower()))
                has_direct_trigger = is_mentioned or is_reply_to_bot or contains_persona_name

                logger.debug(f"Group chat. Pref: {reply_pref}, Mentioned: {is_mentioned}, ReplyToBot: {is_reply_to_bot}, ContainsName: {contains_persona_name}, DirectTrigger: {has_direct_trigger}")

                if reply_pref == "never":
                    should_ai_respond = False
                elif reply_pref == "always":
                    should_ai_respond = True
                elif reply_pref == "mentioned_only":
                    should_ai_respond = has_direct_trigger
                elif reply_pref == "mentioned_or_contextual":
                    if has_direct_trigger:
                        should_ai_respond = True # Отвечаем на прямой триггер
                    else:
                        # --- НАЧАЛО LLM ПРОВЕРКИ КОНТЕКСТА ---
                        logger.info(f"No direct trigger for '{persona.name}'. Checking context via LLM...")
                        should_respond_prompt = persona.format_should_respond_prompt(
                            message_text=message_text,
                            bot_username=bot_username,
                            history=initial_context_from_db # История ДО текущего сообщения
                        )

                        if should_respond_prompt:
                            # Отправляем промпт для решения (без истории)
                            # Используем await, так как send_to_langdock теперь async
                            decision_response = await send_to_langdock(should_respond_prompt, messages=[], is_decision_prompt=True)
                            # llm_decision_made флаг убираем

                            logger.info(f"LLM decision response for 'should respond': '{decision_response}'")
                            # Анализируем ответ LLM (ожидаем "Да" или "Нет", регистронезависимо)
                            if decision_response and "да" in decision_response.strip().lower():
                                should_ai_respond = True
                                logger.info(f"LLM decided YES, bot should respond.")
                            else:
                                should_ai_respond = False
                                logger.info(f"LLM decided NO (or invalid response: '{decision_response}'), bot should not respond.")
                        else:
                            # Если не удалось создать промпт, не отвечаем по контексту
                            should_ai_respond = False
                            logger.warning("Failed to generate 'should respond' prompt. Defaulting to NO.")
                        # --- КОНЕЦ LLM ПРОВЕРКИ КОНТЕКСТА ---
                # else: # Обработка неизвестного преференса
                #     should_ai_respond = has_direct_trigger # По умолчанию как mentioned_only

                if not should_ai_respond:
                    logger.debug(f"Final decision: Not responding in group chat.")
                    # Коммитим изменения лимитов и контекст пользователя
                    if limit_state_updated or context_user_msg_added:
                        try:
                            db.commit()
                            logger.debug("Committed user context and limits before exiting group logic.")
                        except Exception as commit_err:
                             logger.error(f"Commit failed when exiting group logic: {commit_err}")
                             db.rollback()
                    return # Выходим, если решено не отвечать
            # --- КОНЕЦ ЛОГИКИ ОТВЕТА В ГРУППЕ ---

            # --- Отправка основного запроса к LLM (остается только если should_ai_respond == True) ---
            context_for_ai = initial_context_from_db + [current_user_message_dict] # Контекст С текущим сообщением

            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                # ... (обработка ошибки) ...
                logger.error(f"System prompt formatting failed for persona {persona.name}.")
                await update.message.reply_text(escape_markdown_v2("❌ ошибка при подготовке ответа."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            logger.debug(f"--- Sending main request to Langdock --- Chat: {chat_id_str}, Persona: {persona.name}")

            response_text = await send_to_langdock(system_prompt, context_for_ai, is_decision_prompt=False)

            # ... (обработка ответа LLM, вызов process_and_send_response, финальный коммит - без изменений) ...
            logger.debug(f"Raw Response from Langdock:\n---\n{response_text}\n---")

            if response_text.startswith("ai вернул пустой ответ") or \
               response_text.startswith("ошибка:") or \
               response_text.startswith("ой, ошибка связи с ai"):
                logger.error(f"Langdock returned an error/empty message: {response_text}")
                await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN_V2 if response_text.startswith("ai вернул") else None)
                if limit_state_updated or context_user_msg_added:
                    db.commit() # Save context even if AI failed
                return

            # Продолжаем обработку если получили нормальный ответ
            success = await process_and_send_response(
                update, context, chat_id_str, persona, response_text, db,
                update.message.message_id, is_first_message=False
            )

            if success and (limit_state_updated or context_user_msg_added):
                db.commit()
                logger.debug(f"Committed context & limits after successful response in chat {chat_id_str}")
            
        except SQLAlchemyError as e:
            logger.error(f"Database error in handle_message for chat {chat_id_str}: {e}", exc_info=True)
            try: db.rollback()
            except Exception as rb_err: logger.error(f"Error during rollback: {rb_err}")
            await update.message.reply_text(escape_markdown_v2("❌ произошла ошибка базы данных. попробуйте позже."),
                                         parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as e:
            logger.error(f"Telegram API error in handle_message for chat {chat_id_str}: {e}")
            try: db.rollback()
            except Exception as rb_err: logger.error(f"Error during rollback after TelegramError: {rb_err}")
            await update.message.reply_text(escape_markdown_v2("❌ произошла ошибка telegram api. попробуйте позже."),
                                         parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Unexpected error in handle_message for chat {chat_id_str}: {e}", exc_info=True)
            try: db.rollback()
            except Exception as rb_err: logger.error(f"Error during rollback after unexpected error: {rb_err}")
            await update.message.reply_text(escape_markdown_v2("❌ произошла внутренняя ошибка. попробуйте позже."),
                                         parse_mode=ParseMode.MARKDOWN_V2)

import logging
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Handles incoming photo or voice messages."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_id = update.message.message_id
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id_str} (MsgID: {message_id})")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona in chat {chat_id_str} for media message.")
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            limit_ok = check_and_update_user_limits(db, owner_user)
            limit_state_updated = db.is_modified(owner_user)

            if not limit_ok:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media.")
                await send_limit_exceeded_message(update, context, owner_user)
                if limit_state_updated:
                    db.commit()
                return

            context_text_placeholder = ""
            prompt_generator = None
            if media_type == "photo":
                context_text_placeholder = "[РїРѕР»СѓС‡РµРЅРѕ С„РѕС‚Рѕ]"
                prompt_generator = persona.format_photo_prompt
            elif media_type == "voice":
                context_text_placeholder = "[РїРѕР»СѓС‡РµРЅРѕ РіРѕР»РѕСЃРѕРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ]"
                prompt_generator = persona.format_voice_prompt
            else:
                 logger.error(f"Unsupported media_type '{media_type}' in handle_media")
                 db.rollback()
                 return

            context_placeholder_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {context_text_placeholder}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_placeholder_added = True
                    logger.debug(f"Media placeholder '{context_text_placeholder}' prepared for context (pending commit).")
                except (SQLAlchemyError, Exception) as e_ctx:
                     logger.error(f"DB Error preparing media placeholder context: {e_ctx}", exc_info=True)
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РёРЅС„РѕСЂРјР°С†РёРё Рѕ РјРµРґРёР°."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("вќЊ СЃРёСЃС‚РµРјРЅР°СЏ РѕС€РёР±РєР°: РЅРµ СѓРґР°Р»РѕСЃСЊ СЃРІСЏР·Р°С‚СЊ РјРµРґРёР° СЃ Р»РёС‡РЅРѕСЃС‚СЊСЋ."), parse_mode=ParseMode.MARKDOWN_V2)
                 db.rollback()
                 return

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Media saved to context, but ignoring response.")
                db.commit()
                return

            # Use the Persona method to generate the prompt based on settings
            system_prompt = prompt_generator()

            if not system_prompt:
                logger.info(f"Persona {persona.name} in chat {chat_id_str} is configured not to react to {media_type} (media_reaction: {persona.media_reaction}). Skipping response.")
                db.commit()
                return

            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё РєРѕРЅС‚РµРєСЃС‚Р° РґР»СЏ РѕС‚РІРµС‚Р° РЅР° РјРµРґРёР°."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else:
                 logger.error("Cannot get context for AI media response, chat_instance is None.")
                 db.rollback()
                 return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            context_response_prepared = await process_and_send_response(
                update, context, chat_id_str, persona, response_text, db, reply_to_message_id=message_id
            )

            db.commit()
            logger.debug(f"Committed DB changes for handle_media chat {chat_id_str} (LimitUpdated: {limit_state_updated}, PlaceholderAdded: {context_placeholder_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…."), parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР°."), parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles photo messages by calling the generic media handler."""
    if not update.message: return
    await handle_media(update, context, "photo")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles voice messages by calling the generic media handler."""
    if not update.message: return
    await handle_media(update, context, "voice")


# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id_str}")

    logger.debug(f"/start: Checking channel subscription for user {user_id}...")
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    logger.debug(f"/start: Channel subscription check passed for user {user_id}.")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    reply_text_final = ""
    reply_markup = ReplyKeyboardRemove()
    status_raw = ""
    expires_raw = ""
    persona_limit_raw = ""
    message_limit_raw = ""
    fallback_text_raw = "РџСЂРёРІРµС‚! РџСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ СЃС‚Р°СЂС‚РѕРІРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ. РСЃРїРѕР»СЊР·СѓР№ /help РёР»Рё /menu."

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            if db.is_modified(user):
                logger.info(f"/start: Committing new/updated user {user_id}.")
                db.commit()
                db.refresh(user)
            else:
                logger.debug(f"/start: User {user_id} already exists and is up-to-date.")

            logger.debug(f"/start: Checking for active persona in chat {chat_id_str}...")
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                logger.info(f"/start: Persona '{persona.name}' is active in chat {chat_id_str}.")
                part1_raw = f"РїСЂРёРІРµС‚! СЏ {persona.name}. СЏ СѓР¶Рµ Р°РєС‚РёРІРµРЅ РІ СЌС‚РѕРј С‡Р°С‚Рµ.\n"
                part2_raw = "РёСЃРїРѕР»СЊР·СѓР№ /menu РґР»СЏ СЃРїРёСЃРєР° РєРѕРјР°РЅРґ."
                reply_text_final = escape_markdown_v2(part1_raw + part2_raw)
                fallback_text_raw = part1_raw + part2_raw
                reply_markup = ReplyKeyboardRemove()
            else:
                logger.info(f"/start: No active persona in chat {chat_id_str}. Showing welcome message.")
                if not db.is_modified(user):
                    user = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user.id).one()

                now = datetime.now(timezone.utc)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if not user.last_message_reset or user.last_message_reset < today_start:
                    logger.info(f"/start: Resetting daily limit for user {user_id}.")
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit()
                    db.refresh(user)

                status_raw = "в­ђ Premium" if user.is_active_subscriber else "рџ†“ Free"
                expires_raw = ""
                if user.is_active_subscriber and user.subscription_expires_at:
                     if user.subscription_expires_at > now + timedelta(days=365*10):
                         expires_raw = "(Р±РµСЃСЃСЂРѕС‡РЅРѕ)"
                     else:
                         expires_raw = f"РґРѕ {user.subscription_expires_at.strftime('%d.%m.%Y')}"

                persona_count = len(user.persona_configs) if user.persona_configs else 0
                persona_limit_raw = f"{persona_count}/{user.persona_limit}"
                message_limit_raw = f"{user.daily_message_count}/{user.message_limit}"

                start_text_md = (
                    f"РїСЂРёРІРµС‚\\! рџ‘‹ СЏ Р±РѕС‚ РґР»СЏ СЃРѕР·РґР°РЅРёСЏ ai\\-СЃРѕР±РµСЃРµРґРЅРёРєРѕРІ \\(`@{escape_markdown_v2(context.bot.username)}`\\)\\.\n\n"
                    f"*С‚РІРѕР№ СЃС‚Р°С‚СѓСЃ:* {escape_markdown_v2(status_raw)} {escape_markdown_v2(expires_raw)}\n"
                    f"*Р»РёС‡РЅРѕСЃС‚Рё:* `{escape_markdown_v2(persona_limit_raw)}` \\| *СЃРѕРѕР±С‰РµРЅРёСЏ:* `{escape_markdown_v2(message_limit_raw)}`\n\n"
                    f"*РЅР°С‡Р°Р»Рѕ СЂР°Р±РѕС‚С‹:*\n"
                    f"`/createpersona <РёРјСЏ>` \\- СЃРѕР·РґР°Р№ ai\\-Р»РёС‡РЅРѕСЃС‚СЊ\n"
                    f"`/mypersonas` \\- СЃРїРёСЃРѕРє С‚РІРѕРёС… Р»РёС‡РЅРѕСЃС‚РµР№\n"
                    f"`/menu` \\- РїР°РЅРµР»СЊ СѓРїСЂР°РІР»РµРЅРёСЏ\n"
                    f"`/profile` \\- РґРµС‚Р°Р»Рё СЃС‚Р°С‚СѓСЃР°\n"
                    f"`/subscribe` \\- СѓР·РЅР°С‚СЊ Рѕ РїРѕРґРїРёСЃРєРµ"
                 )
                reply_text_final = start_text_md

                fallback_text_raw = (
                     f"РїСЂРёРІРµС‚! рџ‘‹ СЏ Р±РѕС‚ РґР»СЏ СЃРѕР·РґР°РЅРёСЏ ai-СЃРѕР±РµСЃРµРґРЅРёРєРѕРІ (@{context.bot.username}).\n\n"
                     f"С‚РІРѕР№ СЃС‚Р°С‚СѓСЃ: {status_raw} {expires_raw}\n"
                     f"Р»РёС‡РЅРѕСЃС‚Рё: {persona_limit_raw} | СЃРѕРѕР±С‰РµРЅРёСЏ: {message_limit_raw}\n\n"
                     f"РЅР°С‡Р°Р»Рѕ СЂР°Р±РѕС‚С‹:\n"
                     f"/createpersona <РёРјСЏ> - СЃРѕР·РґР°Р№ ai-Р»РёС‡РЅРѕСЃС‚СЊ\n"
                     f"/mypersonas - СЃРїРёСЃРѕРє С‚РІРѕРёС… Р»РёС‡РЅРѕСЃС‚РµР№\n"
                     f"/menu - РїР°РЅРµР»СЊ СѓРїСЂР°РІР»РµРЅРёСЏ\n"
                     f"/profile - РґРµС‚Р°Р»Рё СЃС‚Р°С‚СѓСЃР°\n"
                     f"/subscribe - СѓР·РЅР°С‚СЊ Рѕ РїРѕРґРїРёСЃРєРµ"
                )

                keyboard = [[InlineKeyboardButton("рџљЂ РњРµРЅСЋ РљРѕРјР°РЅРґ", callback_data="show_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            logger.debug(f"/start: Sending final message to user {user_id}.")
            await update.message.reply_text(reply_text_final, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "вќЊ РѕС€РёР±РєР° РїСЂРё Р·Р°РіСЂСѓР·РєРµ РґР°РЅРЅС‹С…. РїРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass
    except TelegramError as e:
        logger.error(f"Telegram error during /start for user {user_id}: {e}", exc_info=True)
        if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
            logger.error(f"--> Failed text (MD): '{reply_text_final[:500]}...'")
            try:
                await update.message.reply_text(fallback_text_raw, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                 logger.error(f"Failed sending fallback start message: {fallback_e}")
        else:
            error_msg_raw = "вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РѕР±СЂР°Р±РѕС‚РєРµ РєРѕРјР°РЅРґС‹ /start."
            try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РѕР±СЂР°Р±РѕС‚РєРµ РєРѕРјР°РЅРґС‹ /start."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command and the show_help callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user = update.effective_user
    user_id = user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # --- РРЎРџР РђР’Р›Р•РќРќР«Р™ РўР•РљРЎРў РЎРџР РђР’РљР v3 ---
    # РСЃРїРѕР»СЊР·СѓРµРј f-СЃС‚СЂРѕРєСѓ Рё escape_markdown_v2 РґР»СЏ РѕРїРёСЃР°РЅРёР№
    # РљРѕРјР°РЅРґС‹ РІ ``, РїР°СЂР°РјРµС‚СЂС‹ < > [] РІРЅСѓС‚СЂРё РЅРёС… РќР• СЌРєСЂР°РЅРёСЂСѓРµРј
    help_text_md = f"""
*_РћСЃРЅРѕРІРЅС‹Рµ РєРѕРјР°РЅРґС‹:_*
`/start`        - {escape_markdown_v2("РќР°С‡Р°Р»Рѕ СЂР°Р±РѕС‚С‹")}
`/help`         - {escape_markdown_v2("Р­С‚Р° СЃРїСЂР°РІРєР°")}
`/menu`         - {escape_markdown_v2("Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ")}
`/profile`      - {escape_markdown_v2("Р’Р°С€ РїСЂРѕС„РёР»СЊ Рё Р»РёРјРёС‚С‹")}
`/subscribe`    - {escape_markdown_v2("РРЅС„РѕСЂРјР°С†РёСЏ Рѕ РїРѕРґРїРёСЃРєРµ")}

*_РЈРїСЂР°РІР»РµРЅРёРµ Р»РёС‡РЅРѕСЃС‚СЊСЋ РІ С‡Р°С‚Рµ:_*
`/mood`         - {escape_markdown_v2("РЎРјРµРЅРёС‚СЊ РЅР°СЃС‚СЂРѕРµРЅРёРµ")}
`/clear`        - {escape_markdown_v2("РћС‡РёСЃС‚РёС‚СЊ РїР°РјСЏС‚СЊ (РєРѕРЅС‚РµРєСЃС‚)")}
`/reset`        - {escape_markdown_v2("РЎР±СЂРѕСЃРёС‚СЊ РґРёР°Р»РѕРі (С‚Рѕ Р¶Рµ, С‡С‚Рѕ /clear)")}
`/mutebot`      - {escape_markdown_v2("Р—Р°РїСЂРµС‚РёС‚СЊ РѕС‚РІРµС‡Р°С‚СЊ РІ С‡Р°С‚Рµ")}
`/unmutebot`    - {escape_markdown_v2("Р Р°Р·СЂРµС€РёС‚СЊ РѕС‚РІРµС‡Р°С‚СЊ РІ С‡Р°С‚Рµ")}

*_РЎРѕР·РґР°РЅРёРµ Рё РЅР°СЃС‚СЂРѕР№РєР° Р»РёС‡РЅРѕСЃС‚РµР№:_*
`/createpersona <РёРјСЏ> [РѕРїРёСЃР°РЅРёРµ]` - {escape_markdown_v2("РЎРѕР·РґР°С‚СЊ РЅРѕРІСѓСЋ")}
`/mypersonas`    - {escape_markdown_v2("РЎРїРёСЃРѕРє РІР°С€РёС… Р»РёС‡РЅРѕСЃС‚РµР№")}
`/editpersona <id>`   - {escape_markdown_v2("Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ (РёРјСЏ, РѕРїРёСЃР°РЅРёРµ, СЃС‚РёР»СЊ, РЅР°СЃС‚СЂРѕРµРЅРёСЏ Рё РґСЂ.)")}
`/deletepersona <id>` - {escape_markdown_v2("РЈРґР°Р»РёС‚СЊ Р»РёС‡РЅРѕСЃС‚СЊ")}

*_Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ:_*
вЂў {escape_markdown_v2("Р‘РѕС‚ РјРѕР¶РµС‚ СЂРµР°РіРёСЂРѕРІР°С‚СЊ РЅР° С„РѕС‚Рѕ Рё РіРѕР»РѕСЃРѕРІС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ (РЅР°СЃС‚СЂР°РёРІР°РµС‚СЃСЏ РІ /editpersona <id>).")}
вЂў {escape_markdown_v2("Р’ РіСЂСѓРїРїР°С… Р±РѕС‚ РѕС‚РІРµС‡Р°РµС‚ СЃРѕРіР»Р°СЃРЅРѕ РЅР°СЃС‚СЂРѕР№РєРµ (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ - РЅР° СѓРїРѕРјРёРЅР°РЅРёСЏ РёР»Рё РїРѕ РєРѕРЅС‚РµРєСЃС‚Сѓ).")}
вЂў {escape_markdown_v2("Р§С‚РѕР±С‹ РґРѕР±Р°РІРёС‚СЊ СЃРѕР·РґР°РЅРЅСѓСЋ Р»РёС‡РЅРѕСЃС‚СЊ РІ С‡Р°С‚, РёСЃРїРѕР»СЊР·СѓР№С‚Рµ РєРЅРѕРїРєСѓ 'вћ• Р’ С‡Р°С‚' РІ /mypersonas.")}
"""
    # РЈР±РёСЂР°РµРј Р»РёС€РЅРёРµ РїСЂРѕР±РµР»С‹/РїРµСЂРµРЅРѕСЃС‹ РїРѕ РєСЂР°СЏРј f-СЃС‚СЂРѕРєРё
    help_text_md = help_text_md.strip()
    # --- РљРћРќР•Р¦ РРЎРџР РђР’Р›Р•РќРќРћР“Рћ РўР•РљРЎРўРђ ---

    # РџСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚ РґР»СЏ Р·Р°РїР°СЃРЅРѕРіРѕ РІР°СЂРёР°РЅС‚Р°
    # РЈР»СѓС‡С€Р°РµРј СѓРґР°Р»РµРЅРёРµ СЃРёРјРІРѕР»РѕРІ Markdown
    help_text_raw_no_md = re.sub(r'[`*_~\\[\\]()|{}+#-.!=]', '', help_text_md)

    keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ РІ РњРµРЅСЋ", callback_data="show_menu")]] if is_callback else None
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()

    try:
        if is_callback:
            query = update.callback_query
            if query.message.text != help_text_md or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(help_text_md, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer()
        else:
            await message_or_query.reply_text(help_text_md, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Help message not modified, skipping edit.")
            await query.answer()
        else:
            logger.error(f"Failed sending/editing help message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed help text (MD): '{help_text_md[:200]}...'")
            try:
                if is_callback:
                    await query.edit_message_text(help_text_raw_no_md, reply_markup=reply_markup, parse_mode=None)
                else:
                    await message_or_query.reply_text(help_text_raw_no_md, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                logger.error(f"Failed sending plain help message: {fallback_e}")
                if is_callback: await query.answer("вќЊ РћС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ СЃРїСЂР°РІРєРё", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing help message: {e}", exc_info=True)
         if is_callback: await query.answer("вќЊ РћС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ СЃРїСЂР°РІРєРё", show_alert=True)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /menu command and the show_menu callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /menu or Callback 'show_menu' < User {user_id} in Chat {chat_id_str}")

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    menu_text_raw = "рџљЂ РџР°РЅРµР»СЊ РЈРїСЂР°РІР»РµРЅРёСЏ\n\nР’С‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ:"
    menu_text_escaped = escape_markdown_v2(menu_text_raw)

    keyboard = [
        [
            InlineKeyboardButton("рџ‘¤ РџСЂРѕС„РёР»СЊ", callback_data="show_profile"),
            InlineKeyboardButton("рџЋ­ РњРѕРё Р›РёС‡РЅРѕСЃС‚Рё", callback_data="show_mypersonas")
        ],
        [
            InlineKeyboardButton("в­ђ РџРѕРґРїРёСЃРєР°", callback_data="subscribe_info"),
            InlineKeyboardButton("вќ“ РџРѕРјРѕС‰СЊ", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if is_callback:
            query = update.callback_query
            if query.message.text != menu_text_escaped or query.message.reply_markup != reply_markup:
                await query.edit_message_text(menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer()
        else:
            await context.bot.send_message(chat_id=chat_id_str, text=menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Menu message not modified, skipping edit.")
            await query.answer()
        else:
            logger.error(f"Failed sending/editing menu message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed menu text (escaped): '{menu_text_escaped[:200]}...'")
            try:
                await context.bot.send_message(chat_id=chat_id_str, text=menu_text_raw, reply_markup=reply_markup, parse_mode=None)
                if is_callback:
                    try: await query.delete_message()
                    except: pass
            except Exception as fallback_e:
                logger.error(f"Failed sending plain menu message: {fallback_e}")
                if is_callback: await query.answer("вќЊ РћС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ РјРµРЅСЋ", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing menu message: {e}", exc_info=True)
         if is_callback: await query.answer("вќЊ РћС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ РјРµРЅСЋ", show_alert=True)


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    """Handles the /mood command and mood selection callbacks."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id_str = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id_str}")

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    close_db_later = False
    db_session = db
    chat_bot_instance = None
    local_persona = persona

    error_no_persona = escape_markdown_v2("рџЋ­ РІ СЌС‚РѕРј С‡Р°С‚Рµ РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё.")
    error_persona_info = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµ РЅР°Р№РґРµРЅР° РёРЅС„РѕСЂРјР°С†РёСЏ Рѕ Р»РёС‡РЅРѕСЃС‚Рё.")
    error_no_moods_fmt_raw = "Сѓ Р»РёС‡РЅРѕСЃС‚Рё '{persona_name}' РЅРµ РЅР°СЃС‚СЂРѕРµРЅС‹ РЅР°СЃС‚СЂРѕРµРЅРёСЏ."
    error_bot_muted_fmt_raw = "рџ”‡ Р»РёС‡РЅРѕСЃС‚СЊ '{persona_name}' СЃРµР№С‡Р°СЃ Р·Р°РіР»СѓС€РµРЅР° \\(РёСЃРїРѕР»СЊР·СѓР№ `/unmutebot`\\)."
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё СЃРјРµРЅРµ РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё РѕР±СЂР°Р±РѕС‚РєРµ РєРѕРјР°РЅРґС‹ /mood.")
    success_mood_set_fmt_raw = "вњ… РЅР°СЃС‚СЂРѕРµРЅРёРµ РґР»СЏ '{persona_name}' С‚РµРїРµСЂСЊ: *{mood_name}*"
    prompt_select_mood_fmt_raw = "С‚РµРєСѓС‰РµРµ РЅР°СЃС‚СЂРѕРµРЅРёРµ: *{current_mood}*\\. РІС‹Р±РµСЂРё РЅРѕРІРѕРµ РґР»СЏ '{persona_name}':"
    prompt_invalid_mood_fmt_raw = "РЅРµ Р·РЅР°СЋ РЅР°СЃС‚СЂРѕРµРЅРёСЏ '{mood_arg}' РґР»СЏ '{persona_name}'. РІС‹Р±РµСЂРё РёР· СЃРїРёСЃРєР°:"

    try:
        if db_session is None:
            db_context = get_db()
            db_session = next(db_context)
            close_db_later = True

        if local_persona is None:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db_session)
            if not persona_info_tuple:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                try:
                    if is_callback: await update.callback_query.answer("РќРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё", show_alert=True)
                    await reply_target.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                logger.debug(f"No active persona for chat {chat_id_str}. Cannot set mood.")
                if close_db_later: db_session.close()
                return
            local_persona, _, _ = persona_info_tuple

        if not local_persona or not local_persona.chat_instance:
             logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id_str}.")
             reply_target = update.callback_query.message if is_callback else message_or_callback_msg
             if is_callback: await update.callback_query.answer("вќЊ РћС€РёР±РєР°: РЅРµ РЅР°Р№РґРµРЅР° РёРЅС„РѕСЂРјР°С†РёСЏ Рѕ Р»РёС‡РЅРѕСЃС‚Рё.", show_alert=True)
             else: await reply_target.reply_text(error_persona_info, parse_mode=ParseMode.MARKDOWN_V2)
             if close_db_later: db_session.close()
             return

        chat_bot_instance = local_persona.chat_instance
        persona_name_raw = local_persona.name
        persona_name_escaped = escape_markdown_v2(persona_name_raw)

        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{persona_name_raw}' is muted in chat {chat_id_str}. Ignoring mood command.")
            reply_text = error_bot_muted_fmt_raw.format(persona_name=persona_name_escaped)
            try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Р‘РѕС‚ Р·Р°РіР»СѓС€РµРЅ", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             reply_text = escape_markdown_v2(error_no_moods_fmt_raw.format(persona_name=persona_name_raw))
             try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("РќРµС‚ РЅР°СЃС‚СЂРѕРµРЅРёР№", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
             logger.warning(f"Persona {persona_name_raw} has no moods defined.")
             if close_db_later: db_session.close()
             return

        available_moods_lower = {m.lower(): m for m in available_moods}
        mood_arg_lower = None
        target_mood_original_case = None

        if is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             if len(parts) >= 3 and parts[-1].isdigit():
                  try:
                      encoded_mood_name = "_".join(parts[2:-1])
                      decoded_mood_name = urllib.parse.unquote(encoded_mood_name)
                      mood_arg_lower = decoded_mood_name.lower()
                      if mood_arg_lower in available_moods_lower:
                          target_mood_original_case = available_moods_lower[mood_arg_lower]
                  except Exception as decode_err:
                      logger.error(f"Error decoding mood name from callback {update.callback_query.data}: {decode_err}")
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
        elif not is_callback:
            mood_text = ""
            if context.args:
                 mood_text = " ".join(context.args)
            elif update.message and update.message.text:
                 possible_mood = update.message.text.strip()
                 if possible_mood.lower() in available_moods_lower:
                      mood_text = possible_mood

            if mood_text:
                mood_arg_lower = mood_text.lower()
                if mood_arg_lower in available_moods_lower:
                    target_mood_original_case = available_moods_lower[mood_arg_lower]

        if target_mood_original_case:
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case)
             reply_text = success_mood_set_fmt_raw.format(
                 persona_name=persona_name_escaped,
                 mood_name=escape_markdown_v2(target_mood_original_case)
                 )

             try:
                 if is_callback:
                     query = update.callback_query
                     if query.message.text != reply_text or query.message.reply_markup:
                         await query.edit_message_text(reply_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                     else:
                         await query.answer(f"РќР°СЃС‚СЂРѕРµРЅРёРµ: {target_mood_original_case}")
                 else:
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood confirmation (BadRequest): {e} - Text: '{reply_text}'")
                  try:
                       reply_text_raw = f"вњ… РЅР°СЃС‚СЂРѕРµРЅРёРµ РґР»СЏ '{persona_name_raw}' С‚РµРїРµСЂСЊ: {target_mood_original_case}"
                       if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=None, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood confirmation: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {persona_name_raw} in chat {chat_id_str} set to {target_mood_original_case}.")
        else:
             keyboard = []
             for mood_name in sorted(available_moods, key=str.lower):
                 try:
                     encoded_mood_name = urllib.parse.quote(mood_name)
                     button_callback = f"set_mood_{encoded_mood_name}_{local_persona.id}"
                     if len(button_callback.encode('utf-8')) <= 64:
                          mood_emoji_map = {"СЂР°РґРѕСЃС‚СЊ": "рџЉ", "РіСЂСѓСЃС‚СЊ": "рџў", "Р·Р»РѕСЃС‚СЊ": "рџ ", "РјРёР»РѕС‚Р°": "рџҐ°", "РЅРµР№С‚СЂР°Р»СЊРЅРѕ": "рџђ"}
                          emoji = mood_emoji_map.get(mood_name.lower(), "рџЋ­")
                          keyboard.append([InlineKeyboardButton(f"{emoji} {mood_name.capitalize()}", callback_data=button_callback)])
                     else:
                          logger.warning(f"Callback data for mood '{mood_name}' (encoded: '{encoded_mood_name}') too long, skipping button.")
                 except Exception as encode_err:
                     logger.error(f"Error encoding mood name '{mood_name}' for callback: {encode_err}")

             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             reply_text = ""
             reply_text_raw = ""

             if mood_arg_lower:
                 mood_arg_escaped = escape_markdown_v2(mood_arg_lower)
                 reply_text = prompt_invalid_mood_fmt_raw.format(
                     mood_arg=mood_arg_escaped,
                     persona_name=persona_name_escaped
                     )
                 reply_text_raw = f"РЅРµ Р·РЅР°СЋ РЅР°СЃС‚СЂРѕРµРЅРёСЏ '{mood_arg_lower}' РґР»СЏ '{persona_name_raw}'. РІС‹Р±РµСЂРё РёР· СЃРїРёСЃРєР°:"
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
             else:
                 reply_text = prompt_select_mood_fmt_raw.format(
                     current_mood=escape_markdown_v2(current_mood_text),
                     persona_name=persona_name_escaped
                     )
                 reply_text_raw = f"С‚РµРєСѓС‰РµРµ РЅР°СЃС‚СЂРѕРµРЅРёРµ: {current_mood_text}. РІС‹Р±РµСЂРё РЅРѕРІРѕРµ РґР»СЏ '{persona_name_raw}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id_str}.")

             try:
                 if is_callback:
                      query = update.callback_query
                      if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer()
                 else:
                      await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood selection (BadRequest): {e} - Text: '{reply_text}'")
                  try:
                       if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood selection: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id_str}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("вќЊ РћС€РёР±РєР° Р‘Р”", show_alert=True)
             await reply_target.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id_str}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("вќЊ РћС€РёР±РєР°", show_alert=True)
             await reply_target.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err: logger.error(f"Error sending general error msg: {send_err}")
    finally:
        if close_db_later and db_session:
            try: db_session.close()
            except Exception: pass


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /reset command to clear persona context in the current chat."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset < User {user_id} ({username}) in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    error_no_persona = escape_markdown_v2("рџЋ­ РІ СЌС‚РѕРј С‡Р°С‚Рµ РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё РґР»СЏ СЃР±СЂРѕСЃР°.")
    error_not_owner = escape_markdown_v2("вќЊ С‚РѕР»СЊРєРѕ РІР»Р°РґРµР»РµС† Р»РёС‡РЅРѕСЃС‚Рё РјРѕР¶РµС‚ СЃР±СЂРѕСЃРёС‚СЊ РµС‘ РїР°РјСЏС‚СЊ.")
    error_no_instance = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµ РЅР°Р№РґРµРЅ СЌРєР·РµРјРїР»СЏСЂ Р±РѕС‚Р° РґР»СЏ СЃР±СЂРѕСЃР°.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё СЃР±СЂРѕСЃРµ РєРѕРЅС‚РµРєСЃС‚Р°.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё СЃР±СЂРѕСЃРµ РєРѕРЅС‚РµРєСЃС‚Р°.")
    success_reset_fmt_raw = "вњ… РїР°РјСЏС‚СЊ Р»РёС‡РЅРѕСЃС‚Рё '{persona_name}' РІ СЌС‚РѕРј С‡Р°С‚Рµ РѕС‡РёС‰РµРЅР°."

    with next(get_db()) as db:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_info_tuple:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return
            persona, _, owner_user = persona_info_tuple

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id_str}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id_str}")
                 await update.message.reply_text(error_no_instance, parse_mode=ParseMode.MARKDOWN_V2)
                 return

            deleted_count_result = chat_bot_instance.context.delete(synchronize_session='fetch')
            deleted_count = deleted_count_result if isinstance(deleted_count_result, int) else 0
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id_str} by user {user_id}.")
            final_success_msg = success_reset_fmt_raw.format(persona_name=escape_markdown_v2(persona.name))
            await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /createpersona command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # РЈР±РёСЂР°РµРј СЂСѓС‡РЅРѕРµ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ \.
    usage_text = escape_markdown_v2("С„РѕСЂРјР°С‚: `/createpersona <РёРјСЏ> [РѕРїРёСЃР°РЅРёРµ]`\n_РёРјСЏ РѕР±СЏР·Р°С‚РµР»СЊРЅРѕ, РѕРїРёСЃР°РЅРёРµ РЅРµС‚._")
    error_name_len = escape_markdown_v2("вќЊ РёРјСЏ Р»РёС‡РЅРѕСЃС‚Рё: 2\\-50 СЃРёРјРІРѕР»РѕРІ.")
    error_desc_len = escape_markdown_v2("вќЊ РѕРїРёСЃР°РЅРёРµ: РґРѕ 1500 СЃРёРјРІРѕР»РѕРІ.")
    error_limit_reached_fmt_raw = "СѓРїСЃ! рџ• РґРѕСЃС‚РёРіРЅСѓС‚ Р»РёРјРёС‚ Р»РёС‡РЅРѕСЃС‚РµР№ ({current_count}/{limit}) РґР»СЏ СЃС‚Р°С‚СѓСЃР° {status_text}\\. С‡С‚РѕР±С‹ СЃРѕР·РґР°РІР°С‚СЊ Р±РѕР»СЊС€Рµ, РёСЃРїРѕР»СЊР·СѓР№ /subscribe"
    error_name_exists_fmt_raw = "вќЊ Р»РёС‡РЅРѕСЃС‚СЊ СЃ РёРјРµРЅРµРј '{persona_name}' СѓР¶Рµ РµСЃС‚СЊ\\. РІС‹Р±РµСЂРё РґСЂСѓРіРѕРµ\\."
    # Р”РѕР±Р°РІР»СЏРµРј СЂСѓС‡РЅРѕРµ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ РІРѕСЃРєР»РёС†Р°С‚РµР»СЊРЅРѕРіРѕ Р·РЅР°РєР°
    success_create_fmt_raw = "вњ… Р»РёС‡РЅРѕСЃС‚СЊ '{name}' СЃРѕР·РґР°РЅР°\\!\nID: `{id}`\nРѕРїРёСЃР°РЅРёРµ: {description}\n\nС‚РµРїРµСЂСЊ РјРѕР¶РЅРѕ РЅР°СЃС‚СЂРѕРёС‚СЊ РїРѕРІРµРґРµРЅРёРµ С‡РµСЂРµР· `/editpersona {id}` РёР»Рё СЃСЂР°Р·Сѓ РґРѕР±Р°РІРёС‚СЊ РІ С‡Р°С‚ С‡РµСЂРµР· `/mypersonas`"
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё СЃРѕР·РґР°РЅРёРё Р»РёС‡РЅРѕСЃС‚Рё.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё СЃРѕР·РґР°РЅРёРё Р»РёС‡РЅРѕСЃС‚Рё.")

    args = context.args
    if not args:
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return
    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None

    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text(error_name_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         return
    if persona_description and len(persona_description) > 1500:
         await update.message.reply_text(error_desc_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         return

    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            if not user.id:
                db.commit()
                db.refresh(user)
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()

            if not user.can_create_persona:
                 current_count = len(user.persona_configs)
                 limit = user.persona_limit
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")
                 status_text_raw = "в­ђ Premium" if user.is_active_subscriber else "рџ†“ Free"
                 final_limit_msg = error_limit_reached_fmt_raw.format(
                     current_count=escape_markdown_v2(str(current_count)),
                     limit=escape_markdown_v2(str(limit)),
                     status_text=escape_markdown_v2(status_text_raw)
                 )
                 await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                 final_exists_msg = error_name_exists_fmt_raw.format(persona_name=escape_markdown_v2(persona_name))
                 await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Use the updated create_persona_config from db.py
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            desc_raw = new_persona.description or "(РїСѓСЃС‚Рѕ)"
            final_success_msg = success_create_fmt_raw.format(
                name=escape_markdown_v2(new_persona.name),
                id=new_persona.id,
                description=escape_markdown_v2(desc_raw)
                )
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError:
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             persona_name_escaped = escape_markdown_v2(persona_name)
             error_msg_ie_raw = f"вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ '{persona_name_escaped}' СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚ \\(РІРѕР·РјРѕР¶РЅРѕ, РіРѕРЅРєР° Р·Р°РїСЂРѕСЃРѕРІ\\)\\. РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ СЂР°Р·."
             await update.message.reply_text(escape_markdown_v2(error_msg_ie_raw), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
             logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
             try: await update.message.reply_text("вќЊ РџСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ РѕС‚РІРµС‚Р°.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback create_persona error: {fe}")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def my_personas(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mypersonas command and show_mypersonas callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message = update.message if not is_callback else None

    if is_callback:
        user = query.from_user
        message_target = query.message
    elif message:
        user = message.from_user
        message_target = message
    else:
        logger.error("my_personas handler called with invalid update type.")
        return

    if not user or not message_target:
        logger.error("my_personas handler could not determine user or message target.")
        return

    user_id = user.id
    username = user.username or f"id_{user_id}"
    chat_id = message_target.chat.id
    chat_id_str = str(chat_id)

    if is_callback:
        logger.info(f"Callback 'show_mypersonas' < User {user_id} ({username}) in Chat {chat_id_str}")
    else:
        logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id_str}")
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
        await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё Р·Р°РіСЂСѓР·РєРµ СЃРїРёСЃРєР° Р»РёС‡РЅРѕСЃС‚РµР№.")
    error_general = escape_markdown_v2("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РїРѕР»СѓС‡РµРЅРёРё СЃРїРёСЃРєР° Р»РёС‡РЅРѕСЃС‚РµР№.")
    error_user_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµ СѓРґР°Р»РѕСЃСЊ РЅР°Р№С‚Рё РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ.")
    info_no_personas_fmt_raw = "Сѓ С‚РµР±СЏ РїРѕРєР° РЅРµС‚ Р»РёС‡РЅРѕСЃС‚РµР№ ({count}/{limit})\\. СЃРѕР·РґР°Р№ РїРµСЂРІСѓСЋ: `/createpersona <РёРјСЏ>`"
    info_list_header_fmt_raw = "рџЋ­ *С‚РІРѕРё Р»РёС‡РЅРѕСЃС‚Рё* \\\\({count}/{limit}\\\\):"
    fallback_text_plain = "РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё СЃРїРёСЃРєР° Р»РёС‡РЅРѕСЃС‚РµР№."

    try:
        with next(get_db()) as db:
            user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()

            if not user_with_personas:
                 user_with_personas = get_or_create_user(db, user_id, username)
                 db.commit()
                 db.refresh(user_with_personas)
                 user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_with_personas.id).one()
                 if not user_with_personas:
                     logger.error(f"User {user_id} not found even after get_or_create/refresh in my_personas.")
                     error_text = error_user_not_found
                     if is_callback: await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
                     else: await message_target.reply_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
                     return

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            if not personas:
                text_to_send = info_no_personas_fmt_raw.format(
                    count=escape_markdown_v2(str(persona_count)),
                    limit=escape_markdown_v2(str(persona_limit))
                    )
                fallback_text_plain = f"Сѓ С‚РµР±СЏ РїРѕРєР° РЅРµС‚ Р»РёС‡РЅРѕСЃС‚РµР№ ({persona_count}/{persona_limit}). СЃРѕР·РґР°Р№ РїРµСЂРІСѓСЋ: /createpersona <РёРјСЏ>"
                keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ РІ РњРµРЅСЋ", callback_data="show_menu")]] if is_callback else None
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()

                if is_callback:
                    if message_target.text != text_to_send or message_target.reply_markup != reply_markup:
                        await query.edit_message_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                    else: await query.answer()
                else: await message_target.reply_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                return

            message_lines = [
                info_list_header_fmt_raw.format(
                    count=escape_markdown_v2(str(persona_count)),
                    limit=escape_markdown_v2(str(persona_limit))
                )
            ]
            keyboard = []
            fallback_lines = [f"РўРІРѕРё Р»РёС‡РЅРѕСЃС‚Рё ({persona_count}/{persona_limit}):"]

            for p in personas:
                 # РСЃРїРѕР»СЊР·СѓРµРј РґРІРѕР№РЅС‹Рµ Р±СЌРєСЃР»РµС€Рё РґР»СЏ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёСЏ СЃРєРѕР±РѕРє РІ f-СЃС‚СЂРѕРєРµ
                 message_lines.append(f"\nрџ‘¤ *{escape_markdown_v2(p.name)}* \\\\(ID: `{p.id}`\\\\)")
                 fallback_lines.append(f"\n- {p.name} (ID: {p.id})")

                 edit_cb = f"edit_persona_{p.id}"
                 delete_cb = f"delete_persona_{p.id}"
                 add_cb = f"add_bot_{p.id}"
                 if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64 or len(add_cb.encode('utf-8')) > 64:
                      logger.warning(f"Callback data for persona {p.id} might be too long.")

                 keyboard.append([
                     InlineKeyboardButton("вљ™пёЏ РќР°СЃС‚СЂРѕРёС‚СЊ", callback_data=edit_cb), # Changed text
                     InlineKeyboardButton("рџ—‘пёЏ РЈРґР°Р»РёС‚СЊ", callback_data=delete_cb),
                     InlineKeyboardButton("вћ• Р’ С‡Р°С‚", callback_data=add_cb)
                 ])

            text_to_send = "\n".join(message_lines)
            fallback_text_plain = "\n".join(fallback_lines)

            if is_callback:
                keyboard.append([InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ РІ РњРµРЅСЋ", callback_data="show_menu")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            if is_callback:
                 if message_target.text != text_to_send or message_target.reply_markup != reply_markup:
                     # РћС‚РїСЂР°РІР»СЏРµРј РєР°Рє РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
                     await query.edit_message_text(fallback_text_plain, reply_markup=reply_markup, parse_mode=None)
                 else: await query.answer()
            # РћС‚РїСЂР°РІР»СЏРµРј РєР°Рє РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
            else: await message_target.reply_text(fallback_text_plain, reply_markup=reply_markup, parse_mode=None)

            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")

    except SQLAlchemyError as e:
        logger.error(f"Database error during my_personas for user {user_id}: {e}", exc_info=True)
        error_text = error_db
        if is_callback: await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
        else: await message_target.reply_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e:
        logger.error(f"Telegram error during my_personas for user {user_id}: {e}", exc_info=True)
        if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
            logger.error(f"--> Failed text (MD): '{text_to_send[:500]}...'")
            try:
                if is_callback:
                    await query.edit_message_text(fallback_text_plain, reply_markup=reply_markup, parse_mode=None)
                else:
                    await message_target.reply_text(fallback_text_plain, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                 logger.error(f"Failed sending fallback mypersonas message: {fallback_e}")
        else:
            error_text = error_general
            if is_callback: await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
            else: await message_target.reply_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error in my_personas handler for user {user_id}: {e}", exc_info=True)
        error_text = error_general
        if is_callback: await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
        else: await message_target.reply_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    """Handles adding a persona (BotInstance) to the current chat."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id_str}"
    local_persona_id = persona_id

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    usage_text = escape_markdown_v2("С„РѕСЂРјР°С‚: `/addbot <id РїРµСЂСЃРѕРЅС‹>`\nРёР»Рё РёСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєСѓ 'вћ• Р’ С‡Р°С‚' РёР· `/mypersonas`")
    error_invalid_id_callback = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Р№ ID Р»РёС‡РЅРѕСЃС‚Рё.")
    error_invalid_id_cmd = escape_markdown_v2("вќЊ id Р»РёС‡РЅРѕСЃС‚Рё РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј.")
    error_no_id = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: ID Р»РёС‡РЅРѕСЃС‚Рё РЅРµ РѕРїСЂРµРґРµР»РµРЅ.")
    error_persona_not_found_fmt_raw = "вќЊ Р»РёС‡РЅРѕСЃС‚СЊ СЃ id `{id}` РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµ С‚РІРѕСЏ."
    error_already_active_fmt_raw = "вњ… Р»РёС‡РЅРѕСЃС‚СЊ '{name}' СѓР¶Рµ Р°РєС‚РёРІРЅР° РІ СЌС‚РѕРј С‡Р°С‚Рµ."
    success_added_structure_raw = "вњ… Р»РёС‡РЅРѕСЃС‚СЊ '{name}' \\(id: `{id}`\\) Р°РєС‚РёРІРёСЂРѕРІР°РЅР° РІ СЌС‚РѕРј С‡Р°С‚Рµ\\! РїР°РјСЏС‚СЊ РѕС‡РёС‰РµРЅР°."
    error_link_failed = escape_markdown_v2("вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ Р°РєС‚РёРІРёСЂРѕРІР°С‚СЊ Р»РёС‡РЅРѕСЃС‚СЊ (РѕС€РёР±РєР° СЃРІСЏР·С‹РІР°РЅРёСЏ).")
    error_integrity = escape_markdown_v2("вќЊ РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° С†РµР»РѕСЃС‚РЅРѕСЃС‚Рё РґР°РЅРЅС‹С… (РІРѕР·РјРѕР¶РЅРѕ, РєРѕРЅС„Р»РёРєС‚ Р°РєС‚РёРІР°С†РёРё), РїРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ СЂР°Р·.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РґРѕР±Р°РІР»РµРЅРёРё Р±РѕС‚Р°.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё Р°РєС‚РёРІР°С†РёРё Р»РёС‡РЅРѕСЃС‚Рё.")

    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("вќЊ РћС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Р№ ID", show_alert=True)
             return
    elif not is_callback:
         logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id_str}) with args: {context.args}")
         args = context.args
         if not args or len(args) != 1 or not args[0].isdigit():
             await message_or_callback_msg.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
             return
         try:
             local_persona_id = int(args[0])
         except ValueError:
             await message_or_callback_msg.reply_text(error_invalid_id_cmd, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             return

    if local_persona_id is None:
         logger.error("add_bot_to_chat: persona_id is None after processing input.")
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         if is_callback: await update.callback_query.answer("вќЊ РћС€РёР±РєР°: ID РЅРµ РѕРїСЂРµРґРµР»РµРЅ.", show_alert=True)
         else: await reply_target.reply_text(error_no_id, parse_mode=ParseMode.MARKDOWN_V2)
         return

    if is_callback:
        await update.callback_query.answer("Р”РѕР±Р°РІР»СЏРµРј Р»РёС‡РЅРѕСЃС‚СЊ...")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 final_not_found_msg = error_persona_not_found_fmt_raw.format(id=local_persona_id)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Р›РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
                 await reply_target.reply_text(final_not_found_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            existing_active_link = db.query(ChatBotInstance).options(
                 selectinload(ChatBotInstance.bot_instance_ref).selectinload(BotInstance.persona_config)
            ).filter(
                 ChatBotInstance.chat_id == chat_id_str,
                 ChatBotInstance.active == True
            ).first()

            if existing_active_link:
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    # Р“РѕС‚РѕРІРёРј РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚ РґР»СЏ СЃРѕРѕР±С‰РµРЅРёСЏ
                    already_active_msg_plain = f"вњ… Р»РёС‡РЅРѕСЃС‚СЊ '{persona.name}' СѓР¶Рµ Р°РєС‚РёРІРЅР° РІ СЌС‚РѕРј С‡Р°С‚Рµ."
                    reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                    if is_callback: await update.callback_query.answer(f"'{persona.name}' СѓР¶Рµ Р°РєС‚РёРІРЅР°", show_alert=True)
                    # РћС‚РїСЂР°РІР»СЏРµРј РєР°Рє РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
                    await reply_target.reply_text(already_active_msg_plain, reply_markup=ReplyKeyboardRemove(), parse_mode=None)

                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id_str} on re-add.")
                    # РџСЂР°РІРёР»СЊРЅРѕРµ СѓРґР°Р»РµРЅРёРµ РєРѕРЅС‚РµРєСЃС‚Р° РґР»СЏ dynamic relationship
                    stmt = delete(ChatContext).where(ChatContext.chat_bot_instance_id == existing_active_link.id)
                    delete_result = db.execute(stmt)
                    deleted_ctx = delete_result.rowcount # РџРѕР»СѓС‡Р°РµРј РєРѕР»РёС‡РµСЃС‚РІРѕ СѓРґР°Р»РµРЅРЅС‹С… СЃС‚СЂРѕРє
                    db.commit()
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return
                else:
                    prev_persona_name = "РќРµРёР·РІРµСЃС‚РЅР°СЏ Р»РёС‡РЅРѕСЃС‚СЊ"
                    if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config:
                        prev_persona_name = existing_active_link.bot_instance_ref.persona_config.name
                    else:
                        prev_persona_name = f"ID {existing_active_link.bot_instance_id}"

                    logger.info(f"Deactivating previous bot '{prev_persona_name}' in chat {chat_id_str} before activating '{persona.name}'.")
                    existing_active_link.active = False
                    db.flush()

            user = persona.owner
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                 logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                 try:
                      bot_instance = create_bot_instance(db, user.id, local_persona_id, name=f"Inst:{persona.name}")
                 except (IntegrityError, SQLAlchemyError) as create_err:
                      logger.error(f"Failed to create BotInstance ({create_err}), possibly due to concurrent request. Retrying fetch.")
                      db.rollback()
                      bot_instance = db.query(BotInstance).filter(BotInstance.persona_config_id == local_persona_id).first()
                      if not bot_instance:
                           logger.error("Failed to fetch BotInstance even after retry.")
                           raise SQLAlchemyError("Failed to create or fetch BotInstance")

            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)

            if chat_link:
                 final_success_msg = success_added_structure_raw.format(
                     name=escape_markdown_v2(persona.name),
                     id=local_persona_id
                     )
                 # Р“РѕС‚РѕРІРёРј РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
                 final_success_msg_plain = f"вњ… Р»РёС‡РЅРѕСЃС‚СЊ '{persona.name}' (id: {local_persona_id}) Р°РєС‚РёРІРёСЂРѕРІР°РЅР° РІ СЌС‚РѕРј С‡Р°С‚Рµ! РїР°РјСЏС‚СЊ РѕС‡РёС‰РµРЅР°."
                 # РћС‚РїСЂР°РІР»СЏРµРј РєР°Рє РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
                 await context.bot.send_message(chat_id=chat_id_str, text=final_success_msg_plain, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                 if is_callback:
                      try:
                           await update.callback_query.delete_message()
                      except Exception as del_err:
                           logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}, '{persona.name}') to chat {chat_id_str}. ChatBotInstance ID: {chat_link.id}")
            else:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(error_link_failed, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id_str} - link_bot_instance_to_chat returned None.")

        except IntegrityError as e:
             logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=False)
             await context.bot.send_message(chat_id=chat_id_str, text=error_integrity, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except SQLAlchemyError as e:
             logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_db, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except BadRequest as e:
             logger.error(f"BadRequest sending message in add_bot_to_chat: {e}", exc_info=True)
             try: await context.bot.send_message(chat_id=chat_id_str, text="вќЊ РџСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РїСЂРё РѕС‚РїСЂР°РІРєРµ РѕС‚РІРµС‚Р°.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback add_bot_to_chat error: {fe}")
        except Exception as e:
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_general, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from inline keyboards NOT part of a ConversationHandler."""
    query = update.callback_query
    if not query or not query.data: return

    chat_id_str = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data

    # --- Check if data matches known conversation entry patterns --- 
    # If it matches, let the ConversationHandler deal with it.
    # Add patterns for ALL conversation entry points here.
    convo_entry_patterns = [
        r'^edit_persona_',    # Edit persona entry
        r'^delete_persona_',  # Delete persona entry
        # Add other ConversationHandler entry point patterns if they exist
    ]
    for pattern in convo_entry_patterns:
        if re.match(pattern, data):
            logger.debug(f"Callback {data} matches convo entry pattern '{pattern}', letting ConversationHandler handle it.")
            # Don't answer here, let the convo handler answer.
            # We don't explicitly pass it on, PTB should handle it if we don't.
            return # <--- Let PTB handle routing

    # Log only callbacks handled by this general handler
    logger.info(f"GENERAL CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

    # --- Subscription Check --- 
    needs_subscription_check = True
    # Callbacks that DON'T require subscription check
    no_check_callbacks = (
        "view_tos", "subscribe_info", "dummy_", "confirm_pay", "subscribe_pay",
        "show_help", "show_menu", "show_profile", "show_mypersonas"
        # Note: Conversation handler callbacks are handled by their respective handlers
    )
    if data.startswith(no_check_callbacks):
        needs_subscription_check = False

    if needs_subscription_check:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            try: await query.answer(text="вќ— РџРѕРґРїРёС€РёС‚РµСЃСЊ РЅР° РєР°РЅР°Р»!", show_alert=True)
            except: pass
            return

    # --- Route non-conversation callbacks ---
    if data.startswith("set_mood_"):
        await mood(update, context)
    elif data == "subscribe_info":
        await query.answer()
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await query.answer("РЎРѕР·РґР°СЋ СЃСЃС‹Р»РєСѓ РЅР° РѕРїР»Р°С‚Сѓ...")
        await generate_payment_link(update, context)
    elif data == "view_tos":
        await query.answer()
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer()
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
        # No need to answer here, add_bot_to_chat does it
        await add_bot_to_chat(update, context)
    elif data == "show_help":
        await query.answer()
        await help_command(update, context)
    elif data == "show_menu":
        await query.answer()
        await menu_command(update, context)
    elif data == "show_profile":
        await query.answer()
        await profile(query, context)
    elif data == "show_mypersonas":
        await query.answer()
        await my_personas(query, context)
    elif data.startswith("dummy_"):
        await query.answer()
    else:
        # Log unhandled non-conversation callbacks
        logger.warning(f"Unhandled non-conversation callback query data: {data} from user {user_id}")
        try:
             await query.answer("РќРµРёР·РІРµСЃС‚РЅРѕРµ РґРµР№СЃС‚РІРёРµ")
        except Exception as e:
             logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


async def profile(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows user profile info. Can be triggered by command or callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message = update.message if not is_callback else None

    if is_callback:
        user = query.from_user
        message_target = query.message
    elif message:
        user = message.from_user
        message_target = message
    else:
        logger.error("Profile handler called with invalid update type.")
        return

    if not user or not message_target:
        logger.error("Profile handler could not determine user or message target.")
        return

    user_id = user.id
    username = user.username or f"id_{user_id}"
    chat_id = message_target.chat.id

    logger.info(f"CMD /profile or Callback 'show_profile' < User {user_id} ({username})")

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё Р·Р°РіСЂСѓР·РєРµ РїСЂРѕС„РёР»СЏ.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё РѕР±СЂР°Р±РѕС‚РєРµ РєРѕРјР°РЅРґС‹ /profile.")
    error_user_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ.")
    profile_text_plain = "РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё РїСЂРѕС„РёР»СЏ."

    with next(get_db()) as db:
        try:
            user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user_db:
                user_db = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user_db)
                user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_db.id).one()
                if not user_db:
                    logger.error(f"User {user_id} not found after get_or_create/refresh in profile.")
                    await context.bot.send_message(chat_id, error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                    return

            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not user_db.last_message_reset or user_db.last_message_reset < today_start:
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user_db.daily_message_count = 0
                user_db.last_message_reset = now
                db.commit()
                db.refresh(user_db)

            is_active_subscriber = user_db.is_active_subscriber
            status_text_escaped = escape_markdown_v2("в­ђ Premium" if is_active_subscriber else "рџ†“ Free")
            expires_text_md = ""
            expires_text_plain = ""

            if is_active_subscriber and user_db.subscription_expires_at:
                 try:
                     if user_db.subscription_expires_at > now + timedelta(days=365*10):
                         expires_text_md = escape_markdown_v2("Р°РєС‚РёРІРЅР° (Р±РµСЃСЃСЂРѕС‡РЅРѕ)")
                         expires_text_plain = "Р°РєС‚РёРІРЅР° (Р±РµСЃСЃСЂРѕС‡РЅРѕ)"
                     else:
                         date_str = user_db.subscription_expires_at.strftime('%d.%m.%Y %H:%M')
                         expires_text_md = f"Р°РєС‚РёРІРЅР° РґРѕ: *{escape_markdown_v2(date_str)}* UTC"
                         expires_text_plain = f"Р°РєС‚РёРІРЅР° РґРѕ: {date_str} UTC"
                 except AttributeError:
                      expires_text_md = escape_markdown_v2("Р°РєС‚РёРІРЅР° (РґР°С‚Р° РёСЃС‚РµС‡РµРЅРёСЏ РЅРµРєРѕСЂСЂРµРєС‚РЅР°)")
                      expires_text_plain = "Р°РєС‚РёРІРЅР° (РґР°С‚Р° РёСЃС‚РµС‡РµРЅРёСЏ РЅРµРєРѕСЂСЂРµРєС‚РЅР°)"
            elif is_active_subscriber:
                 expires_text_md = escape_markdown_v2("Р°РєС‚РёРІРЅР° (Р±РµСЃСЃСЂРѕС‡РЅРѕ)")
                 expires_text_plain = "Р°РєС‚РёРІРЅР° (Р±РµСЃСЃСЂРѕС‡РЅРѕ)"
            else:
                 expires_text_md = escape_markdown_v2("РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ РїРѕРґРїРёСЃРєРё")
                 expires_text_plain = "РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ РїРѕРґРїРёСЃРєРё"

            persona_count = len(user_db.persona_configs) if user_db.persona_configs is not None else 0
            persona_limit_raw = f"{persona_count}/{user_db.persona_limit}"
            msg_limit_raw = f"{user_db.daily_message_count}/{user_db.message_limit}"
            persona_limit_escaped = escape_markdown_v2(persona_limit_raw)
            msg_limit_escaped = escape_markdown_v2(msg_limit_raw)

            profile_text_md = (
                f"рџ‘¤ *РўРІРѕР№ РїСЂРѕС„РёР»СЊ*\n\n"
                f"*РЎС‚Р°С‚СѓСЃ:* {status_text_escaped}\n"
                f"{expires_text_md}\n\n"
                f"*Р›РёРјРёС‚С‹:*\n"
                f"СЃРѕРѕР±С‰РµРЅРёСЏ СЃРµРіРѕРґРЅСЏ: `{msg_limit_escaped}`\n"
                f"СЃРѕР·РґР°РЅРѕ Р»РёС‡РЅРѕСЃС‚РµР№: `{persona_limit_escaped}`\n\n"
            )
            promo_text_md = "рџљЂ С…РѕС‡РµС€СЊ Р±РѕР»СЊС€Рµ\\? Р¶РјРё `/subscribe` РёР»Рё РєРЅРѕРїРєСѓ 'РџРѕРґРїРёСЃРєР°' РІ `/menu`\\!"
            promo_text_plain = "рџљЂ РҐРѕС‡РµС€СЊ Р±РѕР»СЊС€Рµ? Р–РјРё /subscribe РёР»Рё РєРЅРѕРїРєСѓ 'РџРѕРґРїРёСЃРєР°' РІ /menu !"
            if not is_active_subscriber:
                profile_text_md += promo_text_md

            profile_text_plain = (
                f"рџ‘¤ РўРІРѕР№ РїСЂРѕС„РёР»СЊ\n\n"
                f"РЎС‚Р°С‚СѓСЃ: {'Premium' if is_active_subscriber else 'Free'}\n"
                f"{expires_text_plain}\n\n"
                f"Р›РёРјРёС‚С‹:\n"
                f"РЎРѕРѕР±С‰РµРЅРёСЏ СЃРµРіРѕРґРЅСЏ: {msg_limit_raw}\n"
                f"РЎРѕР·РґР°РЅРѕ Р»РёС‡РЅРѕСЃС‚РµР№: {persona_limit_raw}\n\n"
            )
            if not is_active_subscriber:
                profile_text_plain += promo_text_plain

            final_text_to_send = profile_text_md

            keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ РІ РњРµРЅСЋ", callback_data="show_menu")]] if is_callback else None
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            if is_callback:
                if message_target.text != final_text_to_send or message_target.reply_markup != reply_markup:
                    await query.edit_message_text(final_text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await query.answer()
            else:
                await message_target.reply_text(final_text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
             logger.error(f"Database error during profile for user {user_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as e:
            logger.error(f"Telegram error during profile for user {user_id}: {e}", exc_info=True)
            if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
                logger.error(f"--> Failed text (MD): '{final_text_to_send[:500]}...'")
                try:
                    if is_callback:
                        await query.edit_message_text(profile_text_plain, reply_markup=reply_markup, parse_mode=None)
                    else:
                        await message_target.reply_text(profile_text_plain, reply_markup=reply_markup, parse_mode=None)
                except Exception as fallback_e:
                     logger.error(f"Failed sending fallback profile message: {fallback_e}")
            else:
                await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error in profile handler for user {user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    """Handles the /subscribe command and the subscribe_info callback."""
    is_callback = update.callback_query is not None
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update_or_reply = update.callback_query.message if is_callback else update.message
    if not message_to_update_or_reply: return
    chat_id = message_to_update_or_reply.chat.id

    if not from_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("вќЊ Рє СЃРѕР¶Р°Р»РµРЅРёСЋ, С„СѓРЅРєС†РёСЏ РѕРїР»Р°С‚С‹ СЃРµР№С‡Р°СЃ РЅРµРґРѕСЃС‚СѓРїРЅР° \\(РїСЂРѕР±Р»РµРјР° СЃ РЅР°СЃС‚СЂРѕР№РєР°РјРё\\)\\. рџҐ")
    # Р’РѕР·РІСЂР°С‰Р°РµРјСЃСЏ Рє escape_markdown_v2, РЅРѕ СЃ С‡РёСЃС‚РѕР№ СЃС‚СЂРѕРєРѕР№
    info_confirm_raw = (
         "вњ… РѕС‚Р»РёС‡РЅРѕ!\n\n"  # <--- РћР±С‹С‡РЅС‹Р№ РІРѕСЃРєР»РёС†Р°С‚РµР»СЊРЅС‹Р№ Р·РЅР°Рє
         "РЅР°Р¶РёРјР°СЏ РєРЅРѕРїРєСѓ 'РћРїР»Р°С‚РёС‚СЊ' РЅРёР¶Рµ, РІС‹ РїРѕРґС‚РІРµСЂР¶РґР°РµС‚Рµ, С‡С‚Рѕ РѕР·РЅР°РєРѕРјРёР»РёСЃСЊ Рё РїРѕР»РЅРѕСЃС‚СЊСЋ СЃРѕРіР»Р°СЃРЅС‹ СЃ "
         "РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРёРј СЃРѕРіР»Р°С€РµРЅРёРµРј." # <--- РћР±С‹С‡РЅР°СЏ С‚РѕС‡РєР°
         "\n\nрџ‘‡"
    )
    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        currency_raw = SUBSCRIPTION_CURRENCY
        duration_raw = str(SUBSCRIPTION_DURATION_DAYS)
        paid_limit_raw = str(PAID_DAILY_MESSAGE_LIMIT)
        free_limit_raw = str(FREE_DAILY_MESSAGE_LIMIT)
        paid_persona_raw = str(PAID_PERSONA_LIMIT)
        free_persona_raw = str(FREE_PERSONA_LIMIT)

        text_md = (
            f"вњЁ *РџСЂРµРјРёСѓРј РїРѕРґРїРёСЃРєР°* \\({escape_markdown_v2(price_raw)} {escape_markdown_v2(currency_raw)}/РјРµСЃ\\) вњЁ\n\n"
            f"*РџРѕР»СѓС‡РёС‚Рµ РјР°РєСЃРёРјСѓРј РІРѕР·РјРѕР¶РЅРѕСЃС‚РµР№:*\n"
            f"вњ… РґРѕ `{escape_markdown_v2(paid_limit_raw)}` СЃРѕРѕР±С‰РµРЅРёР№ РІ РґРµРЅСЊ \\(РІРјРµСЃС‚Рѕ `{escape_markdown_v2(free_limit_raw)}`\\)\n"
            f"вњ… РґРѕ `{escape_markdown_v2(paid_persona_raw)}` Р»РёС‡РЅРѕСЃС‚РµР№ \\(РІРјРµСЃС‚Рѕ `{escape_markdown_v2(free_persona_raw)}`\\)\n"
            f"вњ… РїРѕР»РЅР°СЏ РЅР°СЃС‚СЂРѕР№РєР° РїРѕРІРµРґРµРЅРёСЏ\n"
            f"вњ… СЃРѕР·РґР°РЅРёРµ Рё СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ СЃРІРѕРёС… РЅР°СЃС‚СЂРѕРµРЅРёР№\n"
            f"вњ… РїСЂРёРѕСЂРёС‚РµС‚РЅР°СЏ РїРѕРґРґРµСЂР¶РєР°\n\n"
            f"*РЎСЂРѕРє РґРµР№СЃС‚РІРёСЏ:* {escape_markdown_v2(duration_raw)} РґРЅРµР№\\."
        )
        text = text_md

        text_raw = (
            f"вњЁ РџСЂРµРјРёСѓРј РїРѕРґРїРёСЃРєР° ({price_raw} {currency_raw}/РјРµСЃ) вњЁ\n\n"
            f"РџРѕР»СѓС‡РёС‚Рµ РјР°РєСЃРёРјСѓРј РІРѕР·РјРѕР¶РЅРѕСЃС‚РµР№:\n"
            f"вњ… {paid_limit_raw} СЃРѕРѕР±С‰РµРЅРёР№ РІ РґРµРЅСЊ (РІРјРµСЃС‚Рѕ {free_limit_raw})\n"
            f"вњ… {paid_persona_raw} Р»РёС‡РЅРѕСЃС‚РµР№ (РІРјРµСЃС‚Рѕ {free_persona_raw})\n"
            f"вњ… РїРѕР»РЅР°СЏ РЅР°СЃС‚СЂРѕР№РєР° РїРѕРІРµРґРµРЅРёСЏ\n"
            f"вњ… СЃРѕР·РґР°РЅРёРµ Рё СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ СЃРІРѕРёС… РЅР°СЃС‚СЂРѕРµРЅРёР№\n"
            f"вњ… РїСЂРёРѕСЂРёС‚РµС‚РЅР°СЏ РїРѕРґРґРµСЂР¶РєР°\n\n"
            f"РЎСЂРѕРє РґРµР№СЃС‚РІРёСЏ: {duration_raw} РґРЅРµР№."
        )

        keyboard = [
            [InlineKeyboardButton("рџ“њ РЈСЃР»РѕРІРёСЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ", callback_data="view_tos")],
            [InlineKeyboardButton("вњ… РџСЂРёРЅСЏС‚СЊ Рё РѕРїР»Р°С‚РёС‚СЊ", callback_data="confirm_pay")]
        ]
        if from_callback:
             keyboard.append([InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ РІ РњРµРЅСЋ", callback_data="show_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if from_callback:
            query = update.callback_query
            if query.message.text != text or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer()
        else:
            await message_to_update_or_reply.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Failed sending subscribe message (BadRequest): {e} - Text MD: '{text[:100]}...'")
        try:
            if message_to_update_or_reply:
                 await context.bot.send_message(chat_id=chat_id, text=text_raw, reply_markup=reply_markup, parse_mode=None)
                 if from_callback:
                     try: await query.delete_message()
                     except Exception: pass
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback subscribe message: {fallback_e}")
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        if from_callback and isinstance(e, (BadRequest, TelegramError)):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")


async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the view_tos callback to show Terms of Service."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    tos_url = context.bot_data.get('tos_url')
    error_tos_link = "вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РѕР±СЂР°Р·РёС‚СЊ СЃСЃС‹Р»РєСѓ РЅР° СЃРѕРіР»Р°С€РµРЅРёРµ."
    error_tos_load = escape_markdown_v2("вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЃСЃС‹Р»РєСѓ РЅР° РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРѕРµ СЃРѕРіР»Р°С€РµРЅРёРµ. РїРѕРїСЂРѕР±СѓР№С‚Рµ РїРѕР·Р¶Рµ.")
    info_tos = escape_markdown_v2("РѕР·РЅР°РєРѕРјСЊС‚РµСЃСЊ СЃ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРёРј СЃРѕРіР»Р°С€РµРЅРёРµРј, РѕС‚РєСЂС‹РІ РµРіРѕ РїРѕ СЃСЃС‹Р»РєРµ РЅРёР¶Рµ:")

    if tos_url:
        keyboard = [
            [InlineKeyboardButton("рџ“њ РћС‚РєСЂС‹С‚СЊ РЎРѕРіР»Р°С€РµРЅРёРµ", url=tos_url)],
            [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="subscribe_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = info_tos
        try:
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer()
        except Exception as e:
            logger.error(f"Failed to show ToS link to user {user_id}: {e}")
            await query.answer(error_tos_link, show_alert=True)
    else:
        logger.error(f"ToS URL not found in bot_data for user {user_id}.")
        text = error_tos_load
        keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer()
        except Exception as e:
             logger.error(f"Failed to show ToS error message to user {user_id}: {e}")
             await query.answer("вќЊ РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё СЃРѕРіР»Р°С€РµРЅРёСЏ.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the confirm_pay callback after user agrees to ToS."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("вќЊ Рє СЃРѕР¶Р°Р»РµРЅРёСЋ, С„СѓРЅРєС†РёСЏ РѕРїР»Р°С‚С‹ СЃРµР№С‡Р°СЃ РЅРµРґРѕСЃС‚СѓРїРЅР° \\(РїСЂРѕР±Р»РµРјР° СЃ РЅР°СЃС‚СЂРѕР№РєР°РјРё\\)\\. рџҐ")
    info_confirm = escape_markdown_v2(
         "вњ… РѕС‚Р»РёС‡РЅРѕ\\!\n\n"
         "РЅР°Р¶РёРјР°СЏ РєРЅРѕРїРєСѓ 'РћРїР»Р°С‚РёС‚СЊ' РЅРёР¶Рµ, РІС‹ РїРѕРґС‚РІРµСЂР¶РґР°РµС‚Рµ, С‡С‚Рѕ РѕР·РЅР°РєРѕРјРёР»РёСЃСЊ Рё РїРѕР»РЅРѕСЃС‚СЊСЋ СЃРѕРіР»Р°СЃРЅС‹ СЃ "
         "РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРёРј СЃРѕРіР»Р°С€РµРЅРёРµРј\\."
         "\n\nрџ‘‡"
    )
    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        # Р¤РѕСЂРјРёСЂСѓРµРј С‚РµРєСЃС‚ РЎР РђР—РЈ СЃ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµРј Рё СЂРµР°Р»СЊРЅС‹РјРё РїРµСЂРµРЅРѕСЃР°РјРё
        info_confirm_md = (
             "вњ… РѕС‚Р»РёС‡РЅРѕ\\\\!\\n\\n"  # Р­РєСЂР°РЅРёСЂСѓРµРј ! -> \\!
             "РЅР°Р¶РёРјР°СЏ РєРЅРѕРїРєСѓ 'РћРїР»Р°С‚РёС‚СЊ' РЅРёР¶Рµ, РІС‹ РїРѕРґС‚РІРµСЂР¶РґР°РµС‚Рµ, С‡С‚Рѕ РѕР·РЅР°РєРѕРјРёР»РёСЃСЊ Рё РїРѕР»РЅРѕСЃС‚СЊСЋ СЃРѕРіР»Р°СЃРЅС‹ СЃ "
             "РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРёРј СЃРѕРіР»Р°С€РµРЅРёРµРј\\\\.\\n\\n" # Р­РєСЂР°РЅРёСЂСѓРµРј . -> \\.
             "рџ‘‡"
        )
        text = info_confirm_md # РџРµСЂРµРґР°РµРј СѓР¶Рµ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРЅС‹Р№ С‚РµРєСЃС‚
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        currency_raw = SUBSCRIPTION_CURRENCY
        # Р­РєСЂР°РЅРёСЂСѓРµРј СЃРёРјРІРѕР»С‹ РІ С‚РµРєСЃС‚Рµ РєРЅРѕРїРєРё, РµСЃР»Рё РѕРЅРё С‚Р°Рј РјРѕРіСѓС‚ Р±С‹С‚СЊ (РЅР° РІСЃСЏРєРёР№ СЃР»СѓС‡Р°Р№)
        button_text_raw = f"рџ’і РћРїР»Р°С‚РёС‚СЊ {price_raw} {currency_raw}"
        button_text = button_text_raw # РљРЅРѕРїРєРё РЅРµ С‚СЂРµР±СѓСЋС‚ Markdown СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёСЏ

        keyboard = [
            [InlineKeyboardButton(button_text, callback_data="subscribe_pay")]
        ]
        # URL РІ РєРЅРѕРїРєРµ РЅРµ С‚СЂРµР±СѓРµС‚ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёСЏ
        if tos_url:
             keyboard.append([InlineKeyboardButton("рџ“њ РЈСЃР»РѕРІРёСЏ РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ (РїСЂРѕС‡РёС‚Р°РЅРѕ)", url=tos_url)])
        else:
             # РўРµРєСЃС‚ РєРЅРѕРїРєРё РЅРµ С‚СЂРµР±СѓРµС‚ СЃРїРµС†. СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёСЏ, С‚.Рє. РЅРµ MD
             keyboard.append([InlineKeyboardButton("рџ“њ РЈСЃР»РѕРІРёСЏ (РѕС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё)", callback_data="view_tos")])

        keyboard.append([InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="subscribe_info")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Р”РёР°РіРЅРѕСЃС‚РёРєР°: РСЃРїРѕР»СЊР·СѓРµРј РјР°РєСЃРёРјР°Р»СЊРЅРѕ РїСЂРѕСЃС‚РѕР№ С‚РµРєСЃС‚
        # current_text_to_send = info_confirm_raw
        current_text_to_send = "РќР°Р¶РјРёС‚Рµ РєРЅРѕРїРєСѓ РћРїР»Р°С‚РёС‚СЊ РЅРёР¶Рµ:"
        logger.debug(f"Attempting to edit message for confirm_pay. Text: '{current_text_to_send}', ParseMode: None")
        if query.message.text != current_text_to_send or query.message.reply_markup != reply_markup:
            await query.edit_message_text(
                current_text_to_send, # РћС‚РїСЂР°РІР»СЏРµРј РџР РћРЎРўРћР™ С‚РµРєСЃС‚
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                parse_mode=None # <--- РЈР±РёСЂР°РµРј Markdown
            )
        else:
            await query.answer()
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates and sends the Yookassa payment link."""
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    error_yk_not_ready = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: СЃРµСЂРІРёСЃ РѕРїР»Р°С‚С‹ РЅРµ РЅР°СЃС‚СЂРѕРµРЅ РїСЂР°РІРёР»СЊРЅРѕ.")
    error_yk_config = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РєРѕРЅС„РёРіСѓСЂР°С†РёРё РїР»Р°С‚РµР¶РЅРѕР№ СЃРёСЃС‚РµРјС‹.")
    error_receipt = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё С„РѕСЂРјРёСЂРѕРІР°РЅРёРё РґР°РЅРЅС‹С… С‡РµРєР°.")
    error_link_get_fmt_raw = "вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ СЃСЃС‹Р»РєСѓ РѕС‚ РїР»Р°С‚РµР¶РЅРѕР№ СЃРёСЃС‚РµРјС‹{status_info}\\\\. РїРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ."
    error_link_create_raw = "вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР·РґР°С‚СЊ СЃСЃС‹Р»РєСѓ РґР»СЏ РѕРїР»Р°С‚С‹\\\\. {error_detail}\\\\. РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ СЂР°Р· РїРѕР·Р¶Рµ РёР»Рё СЃРІСЏР¶РёСЃСЊ СЃ РїРѕРґРґРµСЂР¶РєРѕР№."
    # РЈР±РёСЂР°РµРј СЂСѓС‡РЅРѕРµ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ
    success_link_raw = (
        "вњЁ РЎСЃС‹Р»РєР° РґР»СЏ РѕРїР»Р°С‚С‹ СЃРѕР·РґР°РЅР°!\n\n"
        "РќР°Р¶РјРёС‚Рµ РєРЅРѕРїРєСѓ РЅРёР¶Рµ РґР»СЏ РїРµСЂРµС…РѕРґР° Рє РѕРїР»Р°С‚Рµ.\n"
        "РџРѕСЃР»Рµ СѓСЃРїРµС€РЅРѕР№ РѕРїР»Р°С‚С‹ РїРѕРґРїРёСЃРєР° Р°РєС‚РёРІРёСЂСѓРµС‚СЃСЏ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё (РјРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ РґРѕ 5 РјРёРЅСѓС‚).\n\n"
        "Р•СЃР»Рё РІРѕР·РЅРёРєРЅСѓС‚ РїСЂРѕР±Р»РµРјС‹, РѕР±СЂР°С‚РёС‚РµСЃСЊ РІ РїРѕРґРґРµСЂР¶РєСѓ."
    )

    text = ""
    reply_markup = None

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly for payment generation.")
        text = error_yk_not_ready
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        current_shop_id = int(YOOKASSA_SHOP_ID)
        YookassaConfig.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
        logger.info(f"Yookassa configured within generate_payment_link (Shop ID: {current_shop_id}).")
    except ValueError:
         logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) invalid integer.")
         text = error_yk_config
         reply_markup = None
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
         return
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK in generate_payment_link: {conf_e}", exc_info=True)
        text = error_yk_config
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium РїРѕРґРїРёСЃРєР° @NunuAiBot РЅР° {SUBSCRIPTION_DURATION_DAYS} РґРЅРµР№ (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)}
    bot_username = context.bot_data.get('bot_username', "NunuAiBot")
    return_url = f"https://t.me/{bot_username}"

    try:
        receipt_items = [
            ReceiptItem({
                "description": f"РџСЂРµРјРёСѓРј РґРѕСЃС‚СѓРї @{bot_username} РЅР° {SUBSCRIPTION_DURATION_DAYS} РґРЅРµР№",
                "quantity": 1.0,
                "amount": {"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1",
                "payment_mode": "full_prepayment",
                "payment_subject": "service"
            })
        ]
        user_email = f"user_{user_id}@telegram.bot"
        receipt_data = Receipt({
            "customer": {"email": user_email},
            "items": receipt_items,
        })
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        text = error_receipt
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata) \
            .set_receipt(receipt_data)
        request = builder.build()
        logger.debug(f"Payment request built: {request.json()}")

        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             status_info = f" \\(СЃС‚Р°С‚СѓСЃ: {escape_markdown_v2(payment_response.status)}\\)" if payment_response and payment_response.status else ""
             error_message = error_link_get_fmt_raw.format(status_info=status_info)
             text = error_message
             reply_markup = None
             await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("рџ”— РїРµСЂРµР№С‚Рё Рє РѕРїР»Р°С‚Рµ", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # РСЃРїРѕР»СЊР·СѓРµРј РќР•СЌРєСЂР°РЅРёСЂРѕРІР°РЅРЅСѓСЋ СЃС‚СЂРѕРєСѓ Рё parse_mode=None
        text_to_send = success_link_raw
        await query.edit_message_text(text_to_send, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        error_detail = ""
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                if "Invalid credentials" in err_text:
                    error_detail = "РѕС€РёР±РєР° Р°СѓС‚РµРЅС‚РёС„РёРєР°С†РёРё СЃ СЋkassa"
                elif "receipt" in err_text.lower():
                     error_detail = "РѕС€РёР±РєР° РґР°РЅРЅС‹С… С‡РµРєР° \\(РґРµС‚Р°Р»Рё РІ Р»РѕРіР°С…\\)"
                else:
                    error_detail = "РѕС€РёР±РєР° РѕС‚ СЋkassa \\(РґРµС‚Р°Р»Рё РІ Р»РѕРіР°С…\\)"
            except Exception as parse_e:
                logger.error(f"Could not parse YK error response: {parse_e}")
                error_detail = "РѕС€РёР±РєР° РѕС‚ СЋkassa \\(РЅРµ СѓРґР°Р»РѕСЃСЊ СЂР°Р·РѕР±СЂР°С‚СЊ РѕС‚РІРµС‚\\)"
        elif isinstance(e, httpx.RequestError):
             error_detail = "РїСЂРѕР±Р»РµРјР° СЃ СЃРµС‚РµРІС‹Рј РїРѕРґРєР»СЋС‡РµРЅРёРµРј Рє СЋkassa"
        else:
             error_detail = "РїСЂРѕРёР·РѕС€Р»Р° РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР°"

        user_message = error_link_create_raw.format(error_detail=escape_markdown_v2(error_detail))
        try:
            await query.edit_message_text(user_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder - webhooks are handled by the Flask app, not PTB."""
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass

# --- Conversation Handlers ---

# --- Edit Persona Wizard ---

async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the persona editing wizard."""
    user_id = update.effective_user.id
    effective_target = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if not effective_target: return ConversationHandler.END
    chat_id = effective_target.chat.id
    is_callback = update.callback_query is not None

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()
    context.user_data['edit_persona_id'] = persona_id # Store ID

    error_not_found_fmt_raw = "вќЊ Р»РёС‡РЅРѕСЃС‚СЊ СЃ id `{id}` РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµ С‚РІРѕСЏ."
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РЅР°С‡Р°Р»Рµ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ.")
    error_general = escape_markdown_v2("вќЊ РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР°.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 final_error_msg = error_not_found_fmt_raw.format(id=persona_id)
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Р›РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            # Show the main wizard menu
            return await _show_edit_wizard_menu(update, context, persona_config)

    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")

    usage_text = escape_markdown_v2("СѓРєР°Р¶Рё id Р»РёС‡РЅРѕСЃС‚Рё: `/editpersona <id>`\nРёР»Рё РёСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєСѓ РёР· `/mypersonas`")
    error_invalid_id = escape_markdown_v2("вќЊ id РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј.")

    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for edit persona button press."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("РќР°С‡РёРЅР°РµРј СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ...")

    error_invalid_id_callback = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Р№ ID Р»РёС‡РЅРѕСЃС‚Рё РІ РєРЅРѕРїРєРµ.")

    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        try:
            await query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with invalid ID error: {e}")
        return ConversationHandler.END

async def _show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: PersonaConfig) -> int:
    """Displays the main wizard menu."""
    query = update.callback_query
    message = update.effective_message if not query else query.message
    chat_id = message.chat.id
    persona_id = persona_config.id
    user_id = update.effective_user.id

    # Check premium status
    owner = persona_config.owner
    is_premium = owner.is_active_subscriber or is_admin(user_id) if owner else False
    star = " в­ђ"

    # Get current values for display
    style = persona_config.communication_style or "neutral"
    verbosity = persona_config.verbosity_level or "medium"
    group_reply = persona_config.group_reply_preference or "mentioned_or_contextual"
    media_react = persona_config.media_reaction or "text_only"

    # Map internal keys to user-friendly text (РџРћР›РќР«Р• РЎР›РћР’Рђ)
    style_map = {"neutral": "РќРµР№С‚СЂР°Р»СЊРЅС‹Р№", "friendly": "Р”СЂСѓР¶РµР»СЋР±РЅС‹Р№", "sarcastic": "РЎР°СЂРєР°СЃС‚РёС‡РЅС‹Р№", "formal": "Р¤РѕСЂРјР°Р»СЊРЅС‹Р№", "brief": "РљСЂР°С‚РєРёР№"}
    verbosity_map = {"concise": "Р›Р°РєРѕРЅРёС‡РЅС‹Р№", "medium": "РЎСЂРµРґРЅРёР№", "talkative": "Р Р°Р·РіРѕРІРѕСЂС‡РёРІС‹Р№"}
    group_reply_map = {"always": "Р’СЃРµРіРґР°", "mentioned_only": "РџРѕ @", "mentioned_or_contextual": "РџРѕ @ / РљРѕРЅС‚РµРєСЃС‚Сѓ", "never": "РќРёРєРѕРіРґР°"}
    media_react_map = {"all": "РўРµРєСЃС‚+GIF", "text_only": "РўРѕР»СЊРєРѕ С‚РµРєСЃС‚", "none": "РќРёРєР°Рє", "photo_only": "РўРѕР»СЊРєРѕ С„РѕС‚Рѕ", "voice_only": "РўРѕР»СЊРєРѕ РіРѕР»РѕСЃ"}

    # РџРѕР»СѓС‡Р°РµРј С‚РµРєСѓС‰РµРµ Р·РЅР°С‡РµРЅРёРµ РјР°РєСЃ. СЃРѕРѕР±С‰РµРЅРёР№
    max_msgs_setting = persona_config.max_response_messages
    max_msgs_display = str(max_msgs_setting) if max_msgs_setting > 0 else "РЎР»СѓС‡Р°Р№РЅРѕ (1-3)"
    if max_msgs_setting == 0: # РСЃРїРѕР»СЊР·СѓРµРј 0 РґР»СЏ "РЎР»СѓС‡Р°Р№РЅРѕ"
        max_msgs_display = "РЎР»СѓС‡Р°Р№РЅРѕ (1-3)"
    elif max_msgs_setting < 0: # Р•СЃР»Рё РІРґСЂСѓРі СЃС‚Р°СЂРѕРµ Р·РЅР°С‡РµРЅРёРµ, С‚РѕР¶Рµ СЃС‡РёС‚Р°РµРј СЃР»СѓС‡Р°Р№РЅС‹Рј
        max_msgs_display = "РЎР»СѓС‡Р°Р№РЅРѕ (1-3)"
    else:
        max_msgs_display = str(max_msgs_setting)

    # Build keyboard with full text
    keyboard = [
        [
            InlineKeyboardButton("вњЏпёЏ РРјСЏ", callback_data="edit_wizard_name"),
            InlineKeyboardButton("рџ“њ РћРїРёСЃР°РЅРёРµ", callback_data="edit_wizard_description")
        ],
        [InlineKeyboardButton(f"рџ’¬ РЎС‚РёР»СЊ ({style_map.get(style, '?')})", callback_data="edit_wizard_comm_style")],
        [InlineKeyboardButton(f"рџ—ЈпёЏ Р Р°Р·РіРѕРІРѕСЂС‡РёРІРѕСЃС‚СЊ ({verbosity_map.get(verbosity, '?')})", callback_data="edit_wizard_verbosity")],
        # Use full words below
        [InlineKeyboardButton(f"рџ‘Ґ РћС‚РІРµС‚С‹ РІ РіСЂСѓРїРїРµ ({group_reply_map.get(group_reply, '?')})", callback_data="edit_wizard_group_reply")],
        [InlineKeyboardButton(f"рџ–јпёЏ Р РµР°РєС†РёСЏ РЅР° РјРµРґРёР° ({media_react_map.get(media_react, '?')})", callback_data="edit_wizard_media_reaction")],
        # End of full words change
        [InlineKeyboardButton(f"рџ—ЁпёЏ РњР°РєСЃ. СЃРѕРѕР±С‰. ({max_msgs_display})", callback_data="edit_wizard_max_msgs")], # <-- РќРѕРІР°СЏ РєРЅРѕРїРєР°
        [InlineKeyboardButton(f"рџЋ­ РќР°СЃС‚СЂРѕРµРЅРёСЏ{star if not is_premium else ''}", callback_data="edit_wizard_moods")],
        [InlineKeyboardButton("вњ… Р—Р°РІРµСЂС€РёС‚СЊ", callback_data="finish_edit")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # РСЃРїСЂР°РІР»СЏСЋ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ СЃРєРѕР±РѕРє (СЃРЅРѕРІР°)
    msg_text = f"вљ™пёЏ *РќР°СЃС‚СЂРѕР№РєР° Р»РёС‡РЅРѕСЃС‚Рё: {escape_markdown_v2(persona_config.name)}* \\(ID: `{persona_id}`\\)\n\nР’С‹Р±РµСЂРёС‚Рµ, С‡С‚Рѕ РёР·РјРµРЅРёС‚СЊ:"

    try:
        if query:
            if message.text != msg_text or message.reply_markup != reply_markup:
                await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer()
        else:
            # Delete previous message if it was a prompt (e.g., "Enter name:")
            if context.user_data.get('last_prompt_message_id'):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data['last_prompt_message_id'])
                except Exception as del_err:
                    logger.warning(f"Could not delete previous prompt message: {del_err}")
                context.user_data.pop('last_prompt_message_id', None)
            # Send new menu message
            sent_message = await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['wizard_menu_message_id'] = sent_message.message_id # Store menu message ID if needed
    except Exception as e:
        logger.error(f"Error showing wizard menu for persona {persona_id}: {e}")
        await context.bot.send_message(chat_id, escape_markdown_v2("вќЊ РћС€РёР±РєР° РѕС‚РѕР±СЂР°Р¶РµРЅРёСЏ РјРµРЅСЋ."), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    return EDIT_WIZARD_MENU

# --- Wizard Menu Handler ---
async def edit_wizard_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses in the main wizard menu."""
    query = update.callback_query
    if not query or not query.data: return EDIT_WIZARD_MENU
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    if not persona_id:
        logger.warning(f"edit_wizard_menu_handler: persona_id missing for user {user_id}")
        return ConversationHandler.END

    # Route to the appropriate prompt function or action
    if data == "edit_wizard_name":
        return await edit_name_prompt(update, context)
    elif data == "edit_wizard_description":
        return await edit_description_prompt(update, context)
    elif data == "edit_wizard_comm_style":
        return await edit_comm_style_prompt(update, context)
    elif data == "edit_wizard_verbosity":
        return await edit_verbosity_prompt(update, context)
    elif data == "edit_wizard_group_reply":
        return await edit_group_reply_prompt(update, context)
    elif data == "edit_wizard_media_reaction":
        return await edit_media_reaction_prompt(update, context)
    elif data == "edit_wizard_max_msgs":
        return await edit_max_messages_prompt(update, context)
    elif data == "edit_wizard_moods":
        with next(get_db()) as db:
            owner = db.query(User).join(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            if owner and (owner.is_active_subscriber or is_admin(user_id)):
                return await edit_moods_entry(update, context)
            else:
                await query.answer("в­ђ Р”РѕСЃС‚СѓРїРЅРѕ РїРѕ РїРѕРґРїРёСЃРєРµ", show_alert=True)
                return EDIT_WIZARD_MENU
    elif data == "finish_edit":
        return await edit_persona_finish(update, context)
    else:
        logger.warning(f"Unhandled wizard menu callback: {data}")
        return EDIT_WIZARD_MENU

# --- Helper to send prompt and store message ID ---
async def _send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """Edits the current message or sends a new one, storing the new message ID."""
    query = update.callback_query
    chat_id = query.message.chat.id if query and query.message else update.effective_chat.id
    new_message = None
    try:
        if query and query.message:
            # Try editing first
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            new_message = query.message # Keep the same message object
        else:
            # Send new message if no query or editing failed
            new_message = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.debug("Prompt message not modified.")
            new_message = query.message # Keep same message
        else:
            logger.warning(f"Failed to edit prompt message, sending new: {e}")
            new_message = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            # Try deleting the old menu message if possible
            old_menu_id = context.user_data.get('wizard_menu_message_id')
            if old_menu_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=old_menu_id)
                except Exception as del_err:
                    logger.warning(f"Could not delete old menu message {old_menu_id}: {del_err}")
    except Exception as e:
        logger.error(f"Error sending/editing prompt: {e}", exc_info=True)
        # Fallback send plain text
        try:
            new_message = await context.bot.send_message(chat_id, text.replace('\\', ''), reply_markup=reply_markup, parse_mode=None) # Basic unescaping for plain text
        except Exception as fallback_e:
            logger.error(f"Failed to send fallback plain text prompt: {fallback_e}")

    # Store the ID of the message that contains the prompt
    if new_message:
        context.user_data['last_prompt_message_id'] = new_message.message_id

# --- Edit Name ---
async def edit_name_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_name = db.query(PersonaConfig.name).filter(PersonaConfig.id == persona_id).scalar() or "N/A"
    prompt_text = escape_markdown_v2(f"вњЏпёЏ Р’РІРµРґРёС‚Рµ РЅРѕРІРѕРµ РёРјСЏ (С‚РµРєСѓС‰РµРµ: '{current_name}', 2-50 СЃРёРјРІ.):")
    keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_NAME

async def edit_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_NAME
    new_name = update.message.text.strip()
    persona_id = context.user_data.get('edit_persona_id')

    if not (2 <= len(new_name) <= 50):
        await update.message.reply_text(escape_markdown_v2("вќЊ РРјСЏ: 2-50 СЃРёРјРІ. РџРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ:"))
        return EDIT_NAME

    try:
        with next(get_db()) as db:
            owner_id = db.query(PersonaConfig.owner_id).filter(PersonaConfig.id == persona_id).scalar()
            existing = db.query(PersonaConfig.id).filter(
                PersonaConfig.owner_id == owner_id,
                func.lower(PersonaConfig.name) == new_name.lower(),
                PersonaConfig.id != persona_id
            ).first()
            if existing:
                await update.message.reply_text(escape_markdown_v2(f"вќЊ РРјСЏ '{new_name}' СѓР¶Рµ Р·Р°РЅСЏС‚Рѕ. Р’РІРµРґРёС‚Рµ РґСЂСѓРіРѕРµ:"))
                return EDIT_NAME

            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
            if persona:
                persona.name = new_name
                db.commit()
                await update.message.reply_text(escape_markdown_v2(f"вњ… РРјСЏ РѕР±РЅРѕРІР»РµРЅРѕ РЅР° '{new_name}'."))
                # Delete the prompt message before showing menu
                prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
                if prompt_msg_id:
                    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                    except Exception: pass
                return await _show_edit_wizard_menu(update, context, persona)
            else:
                await update.message.reply_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error updating persona name for {persona_id}: {e}")
        await update.message.reply_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РёРјРµРЅРё."))
        return await _try_return_to_wizard_menu(update, context, update.effective_user.id, persona_id)

# --- Edit Description ---
async def edit_description_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_desc = db.query(PersonaConfig.description).filter(PersonaConfig.id == persona_id).scalar() or "(РїСѓСЃС‚Рѕ)"
    current_desc_preview = escape_markdown_v2((current_desc[:100] + '...') if len(current_desc) > 100 else current_desc)
    prompt_text = escape_markdown_v2(f"вњЏпёЏ Р’РІРµРґРёС‚Рµ РЅРѕРІРѕРµ РѕРїРёСЃР°РЅРёРµ (РјР°РєСЃ. 1500 СЃРёРјРІ.).\nРўРµРєСѓС‰РµРµ (РЅР°С‡Р°Р»Рѕ): \n```\n{current_desc_preview}\n```")
    keyboard = [[InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_DESCRIPTION

async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_DESCRIPTION
    new_desc = update.message.text.strip()
    persona_id = context.user_data.get('edit_persona_id')

    if len(new_desc) > 1500:
        await update.message.reply_text(escape_markdown_v2("вќЊ РћРїРёСЃР°РЅРёРµ: РјР°РєСЃ. 1500 СЃРёРјРІ. РџРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ:"))
        return EDIT_DESCRIPTION

    try:
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
            if persona:
                persona.description = new_desc
                db.commit()
                await update.message.reply_text(escape_markdown_v2("вњ… РћРїРёСЃР°РЅРёРµ РѕР±РЅРѕРІР»РµРЅРѕ."))
                prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
                if prompt_msg_id:
                    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                    except Exception: pass
                return await _show_edit_wizard_menu(update, context, persona)
            else:
                await update.message.reply_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error updating persona description for {persona_id}: {e}")
        await update.message.reply_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РѕРїРёСЃР°РЅРёСЏ."))
        return await _try_return_to_wizard_menu(update, context, update.effective_user.id, persona_id)

# --- Edit Communication Style ---
async def edit_comm_style_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_style = db.query(PersonaConfig.communication_style).filter(PersonaConfig.id == persona_id).scalar() or "neutral"
    prompt_text = escape_markdown_v2(f"рџ’¬ Р’С‹Р±РµСЂРёС‚Рµ СЃС‚РёР»СЊ РѕР±С‰РµРЅРёСЏ (С‚РµРєСѓС‰РёР№: {current_style}):")
    keyboard = [
        [InlineKeyboardButton(f"{'вњ… ' if current_style == 'neutral' else ''}рџђ РќРµР№С‚СЂР°Р»СЊРЅС‹Р№", callback_data="set_comm_style_neutral")],
        [InlineKeyboardButton(f"{'вњ… ' if current_style == 'friendly' else ''}рџЉ Р”СЂСѓР¶РµР»СЋР±РЅС‹Р№", callback_data="set_comm_style_friendly")],
        [InlineKeyboardButton(f"{'вњ… ' if current_style == 'sarcastic' else ''}рџЏ РЎР°СЂРєР°СЃС‚РёС‡РЅС‹Р№", callback_data="set_comm_style_sarcastic")],
        [InlineKeyboardButton(f"{'вњ… ' if current_style == 'formal' else ''}вњЌпёЏ Р¤РѕСЂРјР°Р»СЊРЅС‹Р№", callback_data="set_comm_style_formal")],
        [InlineKeyboardButton(f"{'вњ… ' if current_style == 'brief' else ''}рџ—ЈпёЏ РљСЂР°С‚РєРёР№", callback_data="set_comm_style_brief")],
        [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_COMM_STYLE

async def edit_comm_style_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_comm_style_"):
        new_style = data.replace("set_comm_style_", "")
        try:
            with next(get_db()) as db:
                persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.communication_style = new_style
                    db.commit()
                    logger.info(f"Set communication_style to {new_style} for persona {persona_id}")
                    return await _show_edit_wizard_menu(update, context, persona)
                else:
                    await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting communication_style for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё СЃС‚РёР»СЏ РѕР±С‰РµРЅРёСЏ."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_comm_style_received: {data}")
        return EDIT_COMM_STYLE

# --- Edit Verbosity ---
async def edit_verbosity_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.verbosity_level).filter(PersonaConfig.id == persona_id).scalar() or "medium"
    prompt_text = escape_markdown_v2(f"рџ—ЈпёЏ Р’С‹Р±РµСЂРёС‚Рµ СЂР°Р·РіРѕРІРѕСЂС‡РёРІРѕСЃС‚СЊ (С‚РµРєСѓС‰Р°СЏ: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'вњ… ' if current == 'concise' else ''}рџ¤Џ Р›Р°РєРѕРЅРёС‡РЅС‹Р№", callback_data="set_verbosity_concise")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'medium' else ''}рџ’¬ РЎСЂРµРґРЅРёР№", callback_data="set_verbosity_medium")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'talkative' else ''}рџ“љ Р‘РѕР»С‚Р»РёРІС‹Р№", callback_data="set_verbosity_talkative")],
        [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_VERBOSITY

async def edit_verbosity_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_verbosity_"):
        new_value = data.replace("set_verbosity_", "")
        try:
            with next(get_db()) as db:
                persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.verbosity_level = new_value
                    db.commit()
                    logger.info(f"Set verbosity_level to {new_value} for persona {persona_id}")
                    return await _show_edit_wizard_menu(update, context, persona)
                else:
                    await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting verbosity_level for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё СЂР°Р·РіРѕРІРѕСЂС‡РёРІРѕСЃС‚Рё."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_verbosity_received: {data}")
        return EDIT_VERBOSITY

# --- Edit Group Reply Preference ---
async def edit_group_reply_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.group_reply_preference).filter(PersonaConfig.id == persona_id).scalar() or "mentioned_or_contextual"
    prompt_text = escape_markdown_v2(f"рџ‘Ґ РљР°Рє РѕС‚РІРµС‡Р°С‚СЊ РІ РіСЂСѓРїРїР°С… (С‚РµРєСѓС‰РµРµ: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'вњ… ' if current == 'always' else ''}рџ“ў Р’СЃРµРіРґР°", callback_data="set_group_reply_always")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'mentioned_only' else ''}рџЋЇ РўРѕР»СЊРєРѕ РїРѕ СѓРїРѕРјРёРЅР°РЅРёСЋ (@)", callback_data="set_group_reply_mentioned_only")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'mentioned_or_contextual' else ''}рџ¤” РџРѕ @ РёР»Рё РєРѕРЅС‚РµРєСЃС‚Сѓ", callback_data="set_group_reply_mentioned_or_contextual")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'never' else ''}рџљ« РќРёРєРѕРіРґР°", callback_data="set_group_reply_never")],
        [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_GROUP_REPLY

async def edit_group_reply_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_group_reply_"):
        new_value = data.replace("set_group_reply_", "")
        try:
            with next(get_db()) as db:
                persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.group_reply_preference = new_value
                    db.commit()
                    logger.info(f"Set group_reply_preference to {new_value} for persona {persona_id}")
                    return await _show_edit_wizard_menu(update, context, persona)
                else:
                    await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting group_reply_preference for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РЅР°СЃС‚СЂРѕР№РєРё РѕС‚РІРµС‚Р° РІ РіСЂСѓРїРїРµ."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_group_reply_received: {data}")
        return EDIT_GROUP_REPLY

# --- Edit Media Reaction ---
async def edit_media_reaction_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.media_reaction).filter(PersonaConfig.id == persona_id).scalar() or "text_only"
    prompt_text = escape_markdown_v2(f"рџ–јпёЏ РљР°Рє СЂРµР°РіРёСЂРѕРІР°С‚СЊ РЅР° С„РѕС‚Рѕ/РіРѕР»РѕСЃ (С‚РµРєСѓС‰РµРµ: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'вњ… ' if current == 'all' else ''}вњЌпёЏ РўРµРєСЃС‚ + GIF", callback_data="set_media_react_all")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'text_only' else ''}рџ’¬ РўРѕР»СЊРєРѕ С‚РµРєСЃС‚", callback_data="set_media_react_text_only")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'photo_only' else ''}рџ–јпёЏ РўРѕР»СЊРєРѕ РЅР° С„РѕС‚Рѕ", callback_data="set_media_react_photo_only")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'voice_only' else ''}рџЋ¤ РўРѕР»СЊРєРѕ РЅР° РіРѕР»РѕСЃ", callback_data="set_media_react_voice_only")],
        [InlineKeyboardButton(f"{'вњ… ' if current == 'none' else ''}рџљ« РќРёРєР°Рє", callback_data="set_media_react_none")],
        [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_MEDIA_REACTION

async def edit_media_reaction_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_media_react_"):
        new_value = data.replace("set_media_react_", "")
        try:
            with next(get_db()) as db:
                persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.media_reaction = new_value
                    db.commit()
                    logger.info(f"Set media_reaction to {new_value} for persona {persona_id}")
                    return await _show_edit_wizard_menu(update, context, persona)
                else:
                    await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting media_reaction for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РЅР°СЃС‚СЂРѕР№РєРё СЂРµР°РєС†РёРё РЅР° РјРµРґРёР°."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_media_reaction_received: {data}")
        return EDIT_MEDIA_REACTION

# --- Mood Editing Sub-Conversation ---
async def edit_moods_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for mood editing sub-conversation."""
    query = update.callback_query
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    with next(get_db()) as db:
        owner = db.query(User).join(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
        if not owner or not (owner.is_active_subscriber or is_admin(user_id)):
            await query.answer("в­ђ Р”РѕСЃС‚СѓРїРЅРѕ РїРѕ РїРѕРґРїРёСЃРєРµ", show_alert=True)
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    logger.info(f"User {user_id} entering mood editing for persona {persona_id}.")
    # Pass control to the mood menu function
    with next(get_db()) as db:
        persona_config = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
        if persona_config:
            return await edit_moods_menu(update, context, persona_config=persona_config)
        else: # Should not happen if check passed
            logger.error(f"Persona {persona_id} not found after premium check in edit_moods_entry.")
            return await _try_return_to_wizard_menu(update, context, user_id, persona_id)

# --- Mood Editing Functions (Adapted for Wizard Flow) ---

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    """Displays the mood editing menu (list moods, add button)."""
    query = update.callback_query
    if not query: return ConversationHandler.END

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- edit_moods_menu (within wizard): User={user_id}, PersonaID={persona_id} ---")

    error_no_session = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: СЃРµСЃСЃРёСЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ РїРѕС‚РµСЂСЏРЅР°\\. РЅР°С‡РЅРё СЃРЅРѕРІР° \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµС‚ РґРѕСЃС‚СѓРїР°.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё Р·Р°РіСЂСѓР·РєРµ РЅР°СЃС‚СЂРѕРµРЅРёР№.")
    prompt_mood_menu_fmt_raw = "рџЋ­ СѓРїСЂР°РІР»РµРЅРёРµ РЅР°СЃС‚СЂРѕРµРЅРёСЏРјРё РґР»СЏ *{name}*:"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    local_persona_config = persona_config
    if local_persona_config is None:
        try:
            with next(get_db()) as db:
                local_persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                     PersonaConfig.id == persona_id,
                     PersonaConfig.owner.has(User.telegram_id == user_id)
                 ).first()
                if not local_persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.answer("Р›РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
                    await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    context.user_data.clear()
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("вќЊ РћС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…", show_alert=True)
             await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
             return await _try_return_to_wizard_menu(update, context, user_id, persona_id)

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = prompt_mood_menu_fmt_raw.format(name=escape_markdown_v2(local_persona_config.name))

    try:
        # Use _send_prompt to handle editing/sending and store message ID
        await _send_prompt(update, context, msg_text, reply_markup)
    except Exception as e:
         logger.error(f"Error displaying moods menu message for persona {persona_id}: {e}")

    return EDIT_MOOD_CHOICE

async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses within the mood editing menu (edit, delete, add, back)."""
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: СЃРµСЃСЃРёСЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ РїРѕС‚РµСЂСЏРЅР°\\. РЅР°С‡РЅРё СЃРЅРѕРІР° \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµС‚ РґРѕСЃС‚СѓРїР°.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С….")
    error_unhandled_choice = escape_markdown_v2("вќЊ РЅРµРёР·РІРµСЃС‚РЅС‹Р№ РІС‹Р±РѕСЂ РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    error_decode_mood = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РґРµРєРѕРґРёСЂРѕРІР°РЅРёСЏ РёРјРµРЅРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    prompt_new_name = escape_markdown_v2("РІРІРµРґРё РЅР°Р·РІР°РЅРёРµ РЅРѕРІРѕРіРѕ РЅР°СЃС‚СЂРѕРµРЅРёСЏ \\(1\\-30 СЃРёРјРІРѕР»РѕРІ, Р±СѓРєРІС‹/С†РёС„СЂС‹/РґРµС„РёСЃ/РїРѕРґС‡РµСЂРє\\., Р±РµР· РїСЂРѕР±РµР»РѕРІ\\):")
    prompt_new_prompt_fmt_raw = "вњЏпёЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ РЅР°СЃС‚СЂРѕРµРЅРёСЏ: *{name}*\n\nРѕС‚РїСЂР°РІСЊ РЅРѕРІС‹Р№ С‚РµРєСЃС‚ РїСЂРѕРјРїС‚Р° \\(РґРѕ 1500 СЃРёРјРІ\\.\\):"
    prompt_confirm_delete_fmt_raw = "С‚РѕС‡РЅРѕ СѓРґР°Р»РёС‚СЊ РЅР°СЃС‚СЂРѕРµРЅРёРµ '{name}'?"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await context.bot.send_message(chat_id, error_no_session, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    persona_config = None
    try:
        with next(get_db()) as db:
             persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
             if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_mood_choice.")
                 await query.answer("Р›РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
                 await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("вќЊ РћС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…", show_alert=True)
         await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return await _try_return_to_wizard_menu(update, context, user_id, persona_id)

    await query.answer()

    # --- Route based on callback data ---
    if data == "back_to_wizard_menu": # Changed from edit_persona_back
        logger.debug(f"User {user_id} going back from mood menu to main wizard menu for {persona_id}.")
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return await _show_edit_wizard_menu(update, context, persona_config) # Back to main wizard

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None
        cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
        await _send_prompt(update, context, prompt_new_name, InlineKeyboardMarkup([[cancel_button]]))
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        original_mood_name = None
        try:
             encoded_mood_name = data.split("editmood_select_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except Exception as decode_err:
             logger.error(f"Error decoding mood name from callback {data}: {decode_err}")
             await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

        context.user_data['edit_mood_name'] = original_mood_name
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' to edit for {persona_id}.")
        cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
        final_prompt = prompt_new_prompt_fmt_raw.format(name=escape_markdown_v2(original_mood_name))
        await _send_prompt(update, context, final_prompt, InlineKeyboardMarkup([[cancel_button]]))
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         original_mood_name = None
         encoded_mood_name = ""
         try:
             encoded_mood_name = data.split("deletemood_confirm_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
         except Exception as decode_err:
             logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
             await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

         context.user_data['delete_mood_name'] = original_mood_name
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' for {persona_id}. Asking confirmation.")
         keyboard = [
             [InlineKeyboardButton(f"вњ… РґР°, СѓРґР°Р»РёС‚СЊ '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("вќЊ РЅРµС‚, РѕС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")]
            ]
         final_confirm_prompt = escape_markdown_v2(prompt_confirm_delete_fmt_raw.format(name=original_mood_name))
         await _send_prompt(update, context, final_confirm_prompt, InlineKeyboardMarkup(keyboard))
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text(error_unhandled_choice, parse_mode=ParseMode.MARKDOWN_V2)
    return await edit_moods_menu(update, context, persona_config=persona_config)


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the name for a new mood."""
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    mood_name_match = re.match(r'^[\wР°-СЏРђ-РЇС‘РЃ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    error_no_session = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: СЃРµСЃСЃРёСЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ РїРѕС‚РµСЂСЏРЅР°\\. РЅР°С‡РЅРё СЃРЅРѕРІР° \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°.")
    error_validation = escape_markdown_v2("вќЊ РЅР°Р·РІР°РЅРёРµ: 1\\-30 СЃРёРјРІРѕР»РѕРІ, Р±СѓРєРІС‹/С†РёС„СЂС‹/РґРµС„РёСЃ/РїРѕРґС‡РµСЂРє\\., Р±РµР· РїСЂРѕР±РµР»РѕРІ\\. РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ:")
    error_name_exists_fmt_raw = "вќЊ РЅР°СЃС‚СЂРѕРµРЅРёРµ '{name}' СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚\\. РІС‹Р±РµСЂРё РґСЂСѓРіРѕРµ:"
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РїСЂРѕРІРµСЂРєРµ РёРјРµРЅРё.")
    error_general = escape_markdown_v2("вќЊ РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР°.")
    prompt_for_prompt_fmt_raw = "РѕС‚Р»РёС‡РЅРѕ\\! С‚РµРїРµСЂСЊ РѕС‚РїСЂР°РІСЊ С‚РµРєСЃС‚ РїСЂРѕРјРїС‚Р° РґР»СЏ РЅР°СЃС‚СЂРѕРµРЅРёСЏ '{name}':"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME

    mood_name = mood_name_raw

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            current_moods = {}
            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: pass

            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists.")
                cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
                final_exists_msg = error_name_exists_fmt_raw.format(name=escape_markdown_v2(mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
            final_prompt = prompt_for_prompt_fmt_raw.format(name=escape_markdown_v2(mood_name))
            # Delete the previous prompt message before sending new one
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                except Exception: pass
            # Send new prompt
            sent_message = await context.bot.send_message(chat_id, final_prompt, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['last_prompt_message_id'] = sent_message.message_id # Store new prompt ID
            return EDIT_MOOD_PROMPT

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the prompt text for a mood being edited or added."""
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    error_no_session = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: СЃРµСЃСЃРёСЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ РїРѕС‚РµСЂСЏРЅР° \\(РЅРµС‚ РёРјРµРЅРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ\\)\\. РЅР°С‡РЅРё СЃРЅРѕРІР° \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµС‚ РґРѕСЃС‚СѓРїР°.")
    error_validation = escape_markdown_v2("вќЊ РїСЂРѕРјРїС‚ РЅР°СЃС‚СЂРѕРµРЅРёСЏ: 1\\-1500 СЃРёРјРІРѕР»РѕРІ\\. РїРѕРїСЂРѕР±СѓР№ РµС‰Рµ:")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    success_saved_fmt_raw = "вњ… РЅР°СЃС‚СЂРѕРµРЅРёРµ *{name}* СЃРѕС…СЂР°РЅРµРЅРѕ\\!"

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        cancel_button = InlineKeyboardButton("вќЊ РћС‚РјРµРЅР°", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {}

            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods)
            db.commit()

            context.user_data.pop('edit_mood_name', None)
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            final_success_msg = success_saved_fmt_raw.format(name=escape_markdown_v2(mood_name))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            db.refresh(persona_config)
             # Delete the prompt message before showing menu
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                except Exception: pass
            # Return to mood menu
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the final confirmation button press for deleting a mood."""
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    error_no_session = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Рµ РґР°РЅРЅС‹Рµ РґР»СЏ СѓРґР°Р»РµРЅРёСЏ РёР»Рё СЃРµСЃСЃРёСЏ РїРѕС‚РµСЂСЏРЅР°\\. РЅР°С‡РЅРё СЃРЅРѕРІР° \\(/mypersonas\\)\\.")
    error_not_found_persona = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµС‚ РґРѕСЃС‚СѓРїР°.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё СѓРґР°Р»РµРЅРёРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    error_general = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РїСЂРё СѓРґР°Р»РµРЅРёРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ.")
    info_not_found_mood_fmt_raw = "РЅР°СЃС‚СЂРѕРµРЅРёРµ '{name}' РЅРµ РЅР°Р№РґРµРЅРѕ \\(СѓР¶Рµ СѓРґР°Р»РµРЅРѕ\\?\\)\\."
    error_decode_mood = escape_markdown_v2("вќЊ РѕС€РёР±РєР° РґРµРєРѕРґРёСЂРѕРІР°РЅРёСЏ РёРјРµРЅРё РЅР°СЃС‚СЂРѕРµРЅРёСЏ РґР»СЏ СѓРґР°Р»РµРЅРёСЏ.")
    success_delete_fmt_raw = "рџ—‘пёЏ РЅР°СЃС‚СЂРѕРµРЅРёРµ *{name}* СѓРґР°Р»РµРЅРѕ\\."

    original_name_from_callback = None
    if data.startswith("deletemood_delete_"):
        try:
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await query.answer("вќЊ РћС€РёР±РєР° РґР°РЅРЅС‹С…", show_alert=True)
            await context.bot.send_message(chat_id, error_decode_mood, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    if not mood_name_to_delete or not persona_id or not original_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. Stored='{mood_name_to_delete}', Callback='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("вќЊ РћС€РёР±РєР° СЃРµСЃСЃРёРё", show_alert=True)
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('delete_mood_name', None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    await query.answer("РЈРґР°Р»СЏРµРј...")
    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name_to_delete}' for persona {persona_id}.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                await context.bot.send_message(chat_id, error_not_found_persona, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {}

            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete]
                persona_config.set_moods(db, current_moods)
                db.commit()

                context.user_data.pop('delete_mood_name', None)
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                final_success_msg = success_delete_fmt_raw.format(name=escape_markdown_v2(mood_name_to_delete))
                # Edit message first, then return to menu
                await query.edit_message_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
                await asyncio.sleep(0.5) # Short pause before showing menu again
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id}.")
                final_not_found_msg = info_not_found_mood_fmt_raw.format(name=escape_markdown_v2(mood_name_to_delete))
                await query.edit_message_text(final_not_found_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.pop('delete_mood_name', None)
                await asyncio.sleep(0.5) # Short pause

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     """Helper function to attempt returning to the mood edit menu after an error."""
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     callback_message = update.callback_query.message if update.callback_query else None
     user_message = update.message
     target_message = callback_message or user_message

     error_cannot_return = escape_markdown_v2("вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊСЃСЏ Рє РјРµРЅСЋ РЅР°СЃС‚СЂРѕРµРЅРёР№ \\(Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°\\)\\.")
     error_cannot_return_general = escape_markdown_v2("вќЊ РЅРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊСЃСЏ Рє РјРµРЅСЋ РЅР°СЃС‚СЂРѕРµРЅРёР№.")
     prompt_mood_menu_raw = "рџЋ­ СѓРїСЂР°РІР»РµРЅРёРµ РЅР°СЃС‚СЂРѕРµРЅРёСЏРјРё РґР»СЏ *{name}*:"

     if not target_message:
         logger.warning("Cannot return to mood menu: no target message found.")
         context.user_data.clear()
         return ConversationHandler.END
     target_chat_id = target_message.chat.id

     try:
         with next(get_db()) as db:
             persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()

             if persona_config:
                 # Use the existing mood menu function
                 return await edit_moods_menu(update, context, persona_config=persona_config)
             else:
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await context.bot.send_message(target_chat_id, error_cannot_return, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
         await context.bot.send_message(target_chat_id, error_cannot_return_general, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear()
         return ConversationHandler.END


# --- Wizard Finish/Cancel ---
async def edit_persona_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles finishing the persona editing conversation via the 'Р—Р°РІРµСЂС€РёС‚СЊ' button."""
    query = update.callback_query
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} finished editing persona {persona_id}.")

    finish_message = escape_markdown_v2("вњ… СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ Р·Р°РІРµСЂС€РµРЅРѕ.")

    try:
        if query:
            await query.answer()
            if query.message and query.message.text != finish_message:
                 try:
                     await query.edit_message_text(finish_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                 except BadRequest as e:
                      if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                           logger.warning(f"Could not edit finish message (not found/too old). Sending new for user {user_id}.")
                           await context.bot.send_message(chat_id=query.message.chat.id, text=finish_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                      else: raise
        elif update.effective_message:
             await update.effective_message.reply_text(finish_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        logger.warning(f"Error sending/editing finish confirmation for user {user_id}: {e}")
        chat_id = query.message.chat.id if query and query.message else update.effective_chat.id
        if chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id, text="Р РµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ Р·Р°РІРµСЂС€РµРЅРѕ.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
            except Exception as send_e: logger.error(f"Failed to send fallback finish message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END

async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona editing wizard."""
    message = update.effective_message
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} cancelled persona edit wizard for persona {persona_id}.")

    cancel_message = escape_markdown_v2("СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ РѕС‚РјРµРЅРµРЅРѕ.")

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            if query.message and query.message.text != cancel_message:
                try:
                    await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                except BadRequest as e:
                    if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                         logger.warning(f"Could not edit cancel message (not found/too old). Sending new for user {user_id}.")
                         await context.bot.send_message(chat_id=query.message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                    else: raise
        elif message:
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.warning(f"Error sending/editing cancellation confirmation for user {user_id}: {e}")
        if chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id, text="Р РµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ РѕС‚РјРµРЅРµРЅРѕ.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
            except Exception as send_e: logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END

# --- Delete Persona Conversation ---

async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the persona deletion conversation (common logic)."""
    user_id = update.effective_user.id
    effective_target = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if not effective_target: return ConversationHandler.END
    chat_id = effective_target.chat.id
    is_callback = update.callback_query is not None
    reply_target = update.callback_query.message if is_callback else update.effective_message
    logger.info(f"--- _start_delete_convo: User={user_id}, PersonaID={persona_id}, IsCallback={is_callback} ---") # <--- Р›РћР“

    # ... (РїСЂРѕРІРµСЂРєР° РїРѕРґРїРёСЃРєРё, chat action) ...
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()

    # --- РРЎРџР РђР’Р›Р•РќРР•: Р­РєСЂР°РЅРёСЂСѓРµРј СЃРѕРѕР±С‰РµРЅРёСЏ ---
    error_not_found_fmt_raw = "вќЊ Р›РёС‡РЅРѕСЃС‚СЊ СЃ ID `{id}` РЅРµ РЅР°Р№РґРµРЅР° РёР»Рё РЅРµ С‚РІРѕСЏ." # РЈР±РёСЂР°РµРј СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ С‚СѓС‚
    prompt_delete_fmt_raw = "рџљЁ *Р’РќРРњРђРќРР•\\!* рџљЁ\nРЈРґР°Р»РёС‚СЊ Р»РёС‡РЅРѕСЃС‚СЊ '{name}' \\(ID: `{id}`\\)?\n\nР­С‚Рѕ РґРµР№СЃС‚РІРёРµ *РќР•РћР‘Р РђРўРРњРћ\\!*" # РћСЃС‚Р°РІР»СЏРµРј СЌРєСЂР°РЅРёСЂРѕРІР°РЅРёРµ РґР»СЏ ! Рё ( )
    error_db_raw = "вќЊ РћС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…."
    error_general_raw = "вќЊ РќРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР°."
    error_db = escape_markdown_v2(error_db_raw)
    error_general = escape_markdown_v2(error_general_raw)
    # --- РљРћРќР•Р¦ РРЎРџР РђР’Р›Р•РќРРЇ ---

    try:
        with next(get_db()) as db:
            # ... (РїРѕРёСЃРє persona_config) ...
            logger.debug(f"Fetching PersonaConfig {persona_id} for owner {user_id}...") # <--- Р›РћР“
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 # --- РРЎРџР РђР’Р›Р•РќРР•: Р­РєСЂР°РЅРёСЂСѓРµРј РїРµСЂРµРґ РѕС‚РїСЂР°РІРєРѕР№ ---
                 final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                 # --- РљРћРќР•Р¦ РРЎРџР РђР’Р›Р•РќРРЇ ---
                 logger.warning(f"Persona {persona_id} not found or not owned by user {user_id}.") # <--- Р›РћР“
                 if is_callback: await update.callback_query.answer("Р›РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            # ... (СЃРѕР·РґР°РЅРёРµ РєР»Р°РІРёР°С‚СѓСЂС‹) ...
            logger.debug(f"Persona found: {persona_config.name}. Storing ID in user_data.") # <--- Р›РћР“
            context.user_data['delete_persona_id'] = persona_id
            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            keyboard = [
                 [InlineKeyboardButton(f"вЂјпёЏ Р”Рђ, РЈР”РђР›РРўР¬ '{persona_name_display}' вЂјпёЏ", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("вќЊ РќР•Рў, РћРЎРўРђР’РРўР¬", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # РСЃРїРѕР»СЊР·СѓРµРј СѓР¶Рµ С‡Р°СЃС‚РёС‡РЅРѕ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРЅС‹Р№ prompt_delete_fmt_raw
            msg_text = prompt_delete_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)

            # ... (РѕС‚РїСЂР°РІРєР°/СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ СЃРѕРѕР±С‰РµРЅРёСЏ) ...
            logger.debug(f"Sending confirmation message for persona {persona_id}.") # <--- Р›РћР“
            if is_callback:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer()
                 except BadRequest as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                 except Exception as edit_err:
                      logger.error(f"Unexpected error editing message for delete start (persona {persona_id}): {edit_err}", exc_info=True)
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation. Returning state DELETE_PERSONA_CONFIRM.") # <--- Р›РћР“
            return DELETE_PERSONA_CONFIRM
    # ... (РѕР±СЂР°Р±РѕС‚РєР° РѕС€РёР±РѕРє) ...
    except SQLAlchemyError as e:
         logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /deletepersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")

    usage_text = escape_markdown_v2("СѓРєР°Р¶Рё id Р»РёС‡РЅРѕСЃС‚Рё: `/deletepersona <id>`\nРёР»Рё РёСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєСѓ РёР· `/mypersonas`")
    error_invalid_id = escape_markdown_v2("вќЊ id РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ С‡РёСЃР»РѕРј.")

    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for delete persona button press."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("РќР°С‡РёРЅР°РµРј СѓРґР°Р»РµРЅРёРµ...")

    error_invalid_id_callback = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Р№ ID Р»РёС‡РЅРѕСЃС‚Рё РІ РєРЅРѕРїРєРµ.")

    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        try:
            await query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with invalid ID error: {e}")
        return ConversationHandler.END

async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id_from_data = None
    try:
        persona_id_from_data = int(data.split('_')[-1])
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete confirmation callback data: {data}")
        await query.answer("вќЊ РћС€РёР±РєР° РґР°РЅРЅС‹С…", show_alert=True)
        return ConversationHandler.END # Р—Р°РІРµСЂС€Р°РµРј, РµСЃР»Рё РґР°РЅРЅС‹Рµ РЅРµРєРѕСЂСЂРµРєС‚РЅС‹

    persona_id_from_state = context.user_data.get('delete_persona_id')
    chat_id = query.message.chat.id

    logger.info(f"--- delete_persona_confirmed: User={user_id}, Data={data}, ID_from_data={persona_id_from_data}, ID_from_state={persona_id_from_state} ---")

    error_no_session = escape_markdown_v2("вќЊ РћС€РёР±РєР°: РЅРµРІРµСЂРЅС‹Рµ РґР°РЅРЅС‹Рµ РґР»СЏ СѓРґР°Р»РµРЅРёСЏ РёР»Рё СЃРµСЃСЃРёСЏ РїРѕС‚РµСЂСЏРЅР°. РќР°С‡РЅРё СЃРЅРѕРІР° (/mypersonas).")
    error_delete_failed = escape_markdown_v2("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р»РёС‡РЅРѕСЃС‚СЊ (РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…).")
    success_deleted_fmt_raw = "вњ… Р›РёС‡РЅРѕСЃС‚СЊ '{name}' СѓРґР°Р»РµРЅР°."

    if not persona_id_from_state or persona_id_from_data != persona_id_from_state:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. State='{persona_id_from_state}', Callback='{persona_id_from_data}'")
         await query.answer("вќЊ РћС€РёР±РєР° СЃРµСЃСЃРёРё", show_alert=True)
         # РћС‚РїСЂР°РІР»СЏРµРј РЅРѕРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ, С‚.Рє. СЂРµРґР°РєС‚РёСЂРѕРІР°С‚СЊ РјРѕР¶РµС‚ Р±С‹С‚СЊ РЅРµР»СЊР·СЏ
         try:
             await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err:
             logger.error(f"Failed to send session error message: {send_err}")
         context.user_data.clear()
         return ConversationHandler.END

    await query.answer("РЈРґР°Р»СЏРµРј...")
    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id_from_state}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id_from_state}" # РСЃРїРѕР»СЊР·СѓРµРј ID РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ

    try:
        with next(get_db()) as db:
             user = db.query(User).filter(User.telegram_id == user_id).first()
             if not user:
                  logger.error(f"User {user_id} not found in DB during persona deletion.")
                  try:
                      await context.bot.send_message(chat_id, escape_markdown_v2("вќЊ РћС€РёР±РєР°: РїРѕР»СЊР·РѕРІР°С‚РµР»СЊ РЅРµ РЅР°Р№РґРµРЅ."), reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                  except Exception as send_err:
                      logger.error(f"Failed to send user not found error message: {send_err}")
                  context.user_data.clear()
                  return ConversationHandler.END

             # РџРѕРїСЂРѕР±СѓРµРј РїРѕР»СѓС‡РёС‚СЊ РёРјСЏ РїРµСЂРµРґ СѓРґР°Р»РµРЅРёРµРј РґР»СЏ СЃРѕРѕР±С‰РµРЅРёСЏ
             persona_before_delete = db.query(PersonaConfig.name).filter(PersonaConfig.id == persona_id_from_state, PersonaConfig.owner_id == user.id).scalar()
             if persona_before_delete:
                 persona_name_deleted = persona_before_delete # РћР±РЅРѕРІР»СЏРµРј РёРјСЏ РґР»СЏ СЃРѕРѕР±С‰РµРЅРёСЏ

             logger.info(f"Calling db.delete_persona_config with persona_id={persona_id_from_state}, owner_id={user.id}")
             deleted_ok = delete_persona_config(db, persona_id_from_state, user.id)
             logger.info(f"db.delete_persona_config returned: {deleted_ok}")

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id_from_state}: {e}", exc_info=True)
        deleted_ok = False
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id_from_state}: {e}", exc_info=True)
        deleted_ok = False

    # --- РР—РњР•РќР•РќРР•: РћС‚РїСЂР°РІРєР° РЅРѕРІРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ РІРјРµСЃС‚Рѕ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ ---
    message_to_send = ""
    if deleted_ok:
        message_to_send = escape_markdown_v2(success_deleted_fmt_raw.format(name=persona_name_deleted))
        logger.info(f"Preparing success message for deletion of persona {persona_id_from_state}")
    else:
        message_to_send = error_delete_failed # РЈР¶Рµ СЌРєСЂР°РЅРёСЂРѕРІР°РЅРѕ
        logger.warning(f"Preparing failure message for deletion of persona {persona_id_from_state}")

    try:
        # РћС‚РїСЂР°РІР»СЏРµРј РЅРѕРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ
        await context.bot.send_message(chat_id, message_to_send, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        logger.info(f"Sent final deletion status message to chat {chat_id}.")
        # РџС‹С‚Р°РµРјСЃСЏ СѓРґР°Р»РёС‚СЊ СЃС‚Р°СЂРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ СЃ РєРЅРѕРїРєР°РјРё РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ
        try:
            await query.message.delete()
            logger.debug(f"Deleted original confirmation message {query.message.message_id}.")
        except Exception as del_err:
            logger.warning(f"Could not delete original confirmation message: {del_err}")
    except Exception as send_err:
        logger.error(f"Failed to send final deletion status message: {send_err}")
        # РџРѕРїС‹С‚РєР° РѕС‚РїСЂР°РІРёС‚СЊ РїСЂРѕСЃС‚Рѕ С‚РµРєСЃС‚РѕРј
        try:
            plain_text = success_deleted_fmt_raw.format(name=persona_name_deleted) if deleted_ok else "вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р»РёС‡РЅРѕСЃС‚СЊ (РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С…)."
            await context.bot.send_message(chat_id, plain_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
        except Exception as final_send_err:
             logger.error(f"Failed to send fallback plain text deletion status: {final_send_err}")
    # --- РљРћРќР•Р¦ РР—РњР•РќР•РќРРЇ ---

    logger.debug("Clearing user_data and ending delete conversation.")
    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona deletion process."""
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")

    cancel_message = escape_markdown_v2("СѓРґР°Р»РµРЅРёРµ РѕС‚РјРµРЅРµРЅРѕ.")

    try:
        await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to edit message with deletion cancellation: {e}")
    context.user_data.clear()
    return ConversationHandler.END


# --- Mute/Unmute Commands ---

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    error_no_persona = escape_markdown_v2("рџЋ­ РІ СЌС‚РѕРј С‡Р°С‚Рµ РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё.")
    error_not_owner = escape_markdown_v2("вќЊ С‚РѕР»СЊРєРѕ РІР»Р°РґРµР»РµС† Р»РёС‡РЅРѕСЃС‚Рё РјРѕР¶РµС‚ РµРµ Р·Р°РіР»СѓС€РёС‚СЊ.")
    error_no_instance = escape_markdown_v2("вќЊ РѕС€РёР±РєР°: РЅРµ РЅР°Р№РґРµРЅ РѕР±СЉРµРєС‚ СЃРІСЏР·Рё СЃ С‡Р°С‚РѕРј.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РїРѕРїС‹С‚РєРµ Р·Р°РіР»СѓС€РёС‚СЊ Р±РѕС‚Р°.")
    error_general = escape_markdown_v2("вќЊ РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР° РїСЂРё РІС‹РїРѕР»РЅРµРЅРёРё РєРѕРјР°РЅРґС‹.")
    info_already_muted_fmt_raw = "рџ”‡ Р»РёС‡РЅРѕСЃС‚СЊ '{name}' СѓР¶Рµ Р·Р°РіР»СѓС€РµРЅР° РІ СЌС‚РѕРј С‡Р°С‚Рµ."
    success_muted_fmt_raw = "рџ”‡ Р»РёС‡РЅРѕСЃС‚СЊ '{name}' Р±РѕР»СЊС€Рµ РЅРµ Р±СѓРґРµС‚ РѕС‚РІРµС‡Р°С‚СЊ РІ СЌС‚РѕРј С‡Р°С‚Рµ \\(РЅРѕ Р±СѓРґРµС‚ Р·Р°РїРѕРјРёРЅР°С‚СЊ СЃРѕРѕР±С‰РµРЅРёСЏ\\)\\. РёСЃРїРѕР»СЊР·СѓР№С‚Рµ `/unmutebot`, С‡С‚РѕР±С‹ РІРµСЂРЅСѓС‚СЊ."

    with next(get_db()) as db:
        try:
            instance_info = get_persona_and_context_with_owner(chat_id_str, db)
            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance
            persona_name_escaped = escape_markdown_v2(persona.name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id_str} during mute.")
                await update.message.reply_text(error_no_instance, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                db.commit()
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = success_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_already_muted_msg = info_already_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_already_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()

async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /unmutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    error_no_persona = escape_markdown_v2("рџЋ­ РІ СЌС‚РѕРј С‡Р°С‚Рµ РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё, РєРѕС‚РѕСЂСѓСЋ РјРѕР¶РЅРѕ СЂР°Р·РјСЊСЋС‚РёС‚СЊ.")
    error_not_owner = escape_markdown_v2("вќЊ С‚РѕР»СЊРєРѕ РІР»Р°РґРµР»РµС† Р»РёС‡РЅРѕСЃС‚Рё РјРѕР¶РµС‚ СЃРЅСЏС‚СЊ Р·Р°РіР»СѓС€РєСѓ.")
    error_db = escape_markdown_v2("вќЊ РѕС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РїРѕРїС‹С‚РєРµ РІРµСЂРЅСѓС‚СЊ Р±РѕС‚Р° Рє РѕР±С‰РµРЅРёСЋ.")
    error_general = escape_markdown_v2("вќЊ РЅРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР° РїСЂРё РІС‹РїРѕР»РЅРµРЅРёРё РєРѕРјР°РЅРґС‹.")
    info_not_muted_fmt_raw = "рџ”Љ Р»РёС‡РЅРѕСЃС‚СЊ '{name}' РЅРµ Р±С‹Р»Р° Р·Р°РіР»СѓС€РµРЅР°."
    success_unmuted_fmt_raw = "рџ”Љ Р»РёС‡РЅРѕСЃС‚СЊ '{name}' СЃРЅРѕРІР° РјРѕР¶РµС‚ РѕС‚РІРµС‡Р°С‚СЊ РІ СЌС‚РѕРј С‡Р°С‚Рµ."

    with next(get_db()) as db:
        try:
            active_instance = get_active_chat_bot_instance_with_relations(db, chat_id_str)

            if not active_instance or not active_instance.bot_instance_ref or not active_instance.bot_instance_ref.owner or not active_instance.bot_instance_ref.persona_config:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name
            persona_name_escaped = escape_markdown_v2(persona_name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = success_unmuted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_not_muted_msg = info_not_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_not_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()

# --- РќРѕРІС‹Рµ С„СѓРЅРєС†РёРё РґР»СЏ РЅР°СЃС‚СЂРѕР№РєРё РјР°РєСЃ. СЃРѕРѕР±С‰РµРЅРёР№ ---

async def edit_max_messages_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends prompt to choose max messages."""
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_value = db.query(PersonaConfig.max_response_messages).filter(PersonaConfig.id == persona_id).scalar() or 0

    prompt_text = escape_markdown_v2(f"рџ—ЁпёЏ Р’С‹Р±РµСЂРёС‚Рµ РјР°РєСЃ. РєРѕР»-РІРѕ СЃРѕРѕР±С‰РµРЅРёР№ РІ РѕРґРЅРѕРј РѕС‚РІРµС‚Рµ Р±РѕС‚Р° (С‚РµРєСѓС‰РµРµ: {'РЎР»СѓС‡Р°Р№РЅРѕ (1-3)' if current_value <= 0 else current_value}):")

    keyboard = [
        # Р СЏРґ 1: 1-5
        [InlineKeyboardButton(f"{'вњ… ' if current_value == 1 else ''}1", callback_data="set_max_msgs_1"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 2 else ''}2", callback_data="set_max_msgs_2"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 3 else ''}3", callback_data="set_max_msgs_3"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 4 else ''}4", callback_data="set_max_msgs_4"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 5 else ''}5", callback_data="set_max_msgs_5")],
        # Р СЏРґ 2: 6-10
        [InlineKeyboardButton(f"{'вњ… ' if current_value == 6 else ''}6", callback_data="set_max_msgs_6"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 7 else ''}7", callback_data="set_max_msgs_7"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 8 else ''}8", callback_data="set_max_msgs_8"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 9 else ''}9", callback_data="set_max_msgs_9"),
         InlineKeyboardButton(f"{'вњ… ' if current_value == 10 else ''}10", callback_data="set_max_msgs_10")],
        # Р СЏРґ 3: РЎР»СѓС‡Р°Р№РЅРѕ Рё РќР°Р·Р°Рґ
        [InlineKeyboardButton(f"{'вњ… ' if current_value <= 0 else ''}рџЋІ РЎР»СѓС‡Р°Р№РЅРѕ (1-3)", callback_data="set_max_msgs_0")],
        [InlineKeyboardButton("в¬…пёЏ РќР°Р·Р°Рґ", callback_data="back_to_wizard_menu")]
    ]

    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_MAX_MESSAGES

async def edit_max_messages_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the choice for max messages."""
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_max_msgs_"):
        try:
            new_value = int(data.replace("set_max_msgs_", "")) # 0 РґР»СЏ РЎР»СѓС‡Р°Р№РЅРѕ
            if not (0 <= new_value <= 10):
                raise ValueError("Value out of range")

            with next(get_db()) as db:
                persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.max_response_messages = new_value
                    db.commit()
                    display_val = 'РЎР»СѓС‡Р°Р№РЅРѕ (1-3)' if new_value <= 0 else str(new_value)
                    logger.info(f"Set max_response_messages to {display_val} ({new_value}) for persona {persona_id}")
                    return await _show_edit_wizard_menu(update, context, persona)
                else:
                    await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР°: Р»РёС‡РЅРѕСЃС‚СЊ РЅРµ РЅР°Р№РґРµРЅР°."))
                    return ConversationHandler.END
        except (ValueError, Exception) as e:
            logger.error(f"Error setting max_response_messages for {persona_id} from data '{data}': {e}")
            await query.edit_message_text(escape_markdown_v2("вќЊ РћС€РёР±РєР° РїСЂРё СЃРѕС…СЂР°РЅРµРЅРёРё РЅР°СЃС‚СЂРѕР№РєРё."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_max_messages_received: {data}")
        return EDIT_MAX_MESSAGES

async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /clear command to clear persona context in the current chat."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /clear < User {user_id} ({username}) in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # РЎРѕРѕР±С‰РµРЅРёСЏ РґР»СЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
    msg_no_persona = escape_markdown_v2("рџЋ­ Р’ СЌС‚РѕРј С‡Р°С‚Рµ РЅРµС‚ Р°РєС‚РёРІРЅРѕР№ Р»РёС‡РЅРѕСЃС‚Рё, РїР°РјСЏС‚СЊ РєРѕС‚РѕСЂРѕР№ РјРѕР¶РЅРѕ Р±С‹Р»Рѕ Р±С‹ РѕС‡РёСЃС‚РёС‚СЊ.")
    msg_not_owner = escape_markdown_v2("вќЊ РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС† Р»РёС‡РЅРѕСЃС‚Рё РёР»Рё Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ Р±РѕС‚Р° РјРѕРіСѓС‚ РѕС‡РёСЃС‚РёС‚СЊ РµС‘ РїР°РјСЏС‚СЊ.")
    msg_no_instance = escape_markdown_v2("вќЊ РћС€РёР±РєР°: РЅРµ РЅР°Р№РґРµРЅ СЌРєР·РµРјРїР»СЏСЂ СЃРІСЏР·Рё Р±РѕС‚Р° СЃ СЌС‚РёРј С‡Р°С‚РѕРј.")
    msg_db_error = escape_markdown_v2("вќЊ РћС€РёР±РєР° Р±Р°Р·С‹ РґР°РЅРЅС‹С… РїСЂРё РѕС‡РёСЃС‚РєРµ РїР°РјСЏС‚Рё.")
    msg_general_error = escape_markdown_v2("вќЊ РќРµРїСЂРµРґРІРёРґРµРЅРЅР°СЏ РѕС€РёР±РєР° РїСЂРё РѕС‡РёСЃС‚РєРµ РїР°РјСЏС‚Рё.")
    msg_success_fmt = "вњ… РџР°РјСЏС‚СЊ Р»РёС‡РЅРѕСЃС‚Рё '{name}' РІ СЌС‚РѕРј С‡Р°С‚Рµ РѕС‡РёС‰РµРЅР° ({count} СЃРѕРѕР±С‰РµРЅРёР№ СѓРґР°Р»РµРЅРѕ)." # РСЃРїРѕР»СЊР·СѓРµРј format РїРѕР·Р¶Рµ

    with next(get_db()) as db:
        try:
            # РќР°С…РѕРґРёРј Р°РєС‚РёРІРЅСѓСЋ Р»РёС‡РЅРѕСЃС‚СЊ Рё РµРµ РІР»Р°РґРµР»СЊС†Р°
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_info_tuple:
                await update.message.reply_text(msg_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, _, owner_user = persona_info_tuple
            persona_name_raw = persona.name
            persona_name_escaped = escape_markdown_v2(persona_name_raw)

            # РџСЂРѕРІРµСЂСЏРµРј РїСЂР°РІР° РґРѕСЃС‚СѓРїР°
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to clear memory for persona '{persona_name_raw}' owned by {owner_user.telegram_id} in chat {chat_id_str}.")
                await update.message.reply_text(msg_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 logger.error(f"Clear command: ChatBotInstance not found for persona {persona_name_raw} in chat {chat_id_str}")
                 await update.message.reply_text(msg_no_instance, parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # РЈРґР°Р»СЏРµРј РєРѕРЅС‚РµРєСЃС‚
            chat_bot_instance_id = chat_bot_instance.id
            logger.warning(f"User {user_id} clearing context for ChatBotInstance {chat_bot_instance_id} (Persona '{persona_name_raw}') in chat {chat_id_str}.")

            # РЎРѕР·РґР°РµРј SQL Р·Р°РїСЂРѕСЃ РЅР° СѓРґР°Р»РµРЅРёРµ
            stmt = delete(ChatContext).where(ChatContext.chat_bot_instance_id == chat_bot_instance_id)
            result = db.execute(stmt)
            deleted_count = result.rowcount # РџРѕР»СѓС‡Р°РµРј РєРѕР»РёС‡РµСЃС‚РІРѕ СѓРґР°Р»РµРЅРЅС‹С… СЃС‚СЂРѕРє
            db.commit()

            logger.info(f"Deleted {deleted_count} context messages for instance {chat_bot_instance_id}.")
            # Р¤РѕСЂРјР°С‚РёСЂСѓРµРј СЃРѕРѕР±С‰РµРЅРёРµ РѕР± СѓСЃРїРµС…Рµ СЃ СЂРµР°Р»СЊРЅС‹Рј РєРѕР»РёС‡РµСЃС‚РІРѕРј
            final_success_msg_raw = msg_success_fmt.format(name=persona_name_raw, count=deleted_count)
            final_success_msg_escaped = escape_markdown_v2(final_success_msg_raw)

            await update.message.reply_text(final_success_msg_escaped, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /clear for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(msg_db_error, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Error in /clear handler for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(msg_general_error, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()


# ... (РѕСЃС‚Р°Р»СЊРЅС‹Рµ С„СѓРЅРєС†РёРё) ...
