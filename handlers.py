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
                    escape_markdown_v2("⏳ не удалось проверить подписку на канал (таймаут). попробуйте еще раз позже."),
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
                    escape_markdown_v2("❌ не удалось проверить подписку на канал. убедитесь, что бот добавлен в канал как администратор."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False
    except BadRequest as e:
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
         reply_text_raw = "❌ произошла ошибка при проверке подписки (badrequest). попробуйте позже."
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             reply_text_raw = "❌ не удается получить доступ к списку участников канала для проверки подписки. возможно, настройки канала не позволяют это сделать."
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             return False
         elif "chat not found" in error_message:
              logger.error(f"-> Specific BadRequest: Chat {CHANNEL_ID} not found. Check CHANNEL_ID config.")
              reply_text_raw = "❌ ошибка: не удалось найти указанный канал для проверки подписки. проверьте настройки бота."

         target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
         if target_message:
             try: await target_message.reply_text(escape_markdown_v2(reply_text_raw), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Failed to send 'BadRequest' error message: {send_err}")
         return False
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
        if target_message:
            try: await target_message.reply_text(escape_markdown_v2("❌ произошла ошибка telegram при проверке подписки. попробуйте позже."), parse_mode=ParseMode.MARKDOWN_V2)
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

    error_msg_raw = "❌ произошла ошибка при получении ссылки на канал."
    subscribe_text_raw = "❗ для использования бота необходимо подписаться на наш канал."
    button_text = "➡️ перейти к каналу"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"❗ для использования бота необходимо подписаться на канал @{channel_username}."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         subscribe_text_raw = "❗ для использования бота необходимо подписаться на наш основной канал. пожалуйста, найдите канал в поиске или через описание бота."
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
 DELETE_PERSONA_CONFIRM
 ) = range(13) # Total 13 states

# --- Terms of Service Text ---
# (Assuming TOS_TEXT_RAW and TOS_TEXT are defined as before)
TOS_TEXT_RAW = """
📜 пользовательское соглашение сервиса @NunuAiBot

привет! добро пожаловать в @NunuAiBot! мы рады, что ты с нами. это соглашение — документ, который объясняет правила использования нашего сервиса. прочитай его, пожалуйста.

дата последнего обновления: 01.03.2025

1. о чем это соглашение?
1.1. это пользовательское соглашение (или просто "соглашение") — договор между тобой (далее – "пользователь" или "ты") и нами (владельцем telegram-бота @NunuAiBot, далее – "сервис" или "мы"). оно описывает условия использования сервиса.
1.2. начиная использовать наш сервис (просто отправляя боту любое сообщение или команду), ты подтверждаешь, что прочитал, понял и согласен со всеми условиями этого соглашения. если ты не согласен хотя бы с одним пунктом, пожалуйста, прекрати использование сервиса.
1.3. наш сервис предоставляет тебе интересную возможность создавать и общаться с виртуальными собеседниками на базе искусственного интеллекта (далее – "личности" или "ai-собеседники").

2. про подписку и оплату
2.1. мы предлагаем два уровня доступа: бесплатный и premium (платный). возможности и лимиты для каждого уровня подробно описаны внутри бота, например, в командах `/profile` и `/subscribe`.
2.2. платная подписка дает тебе расширенные возможности и увеличенные лимиты на период в {subscription_duration} дней.
2.3. стоимость подписки составляет {subscription_price} {subscription_currency} за {subscription_duration} дней.
2.4. оплата проходит через безопасную платежную систему yookassa. важно: мы не получаем и не храним твои платежные данные (номер карты и т.п.). все безопасно.
2.5. политика возвратов: покупая подписку, ты получаешь доступ к расширенным возможностям сервиса сразу же после оплаты. поскольку ты получаешь услугу немедленно, оплаченные средства за этот период доступа, к сожалению, не подлежат возврату.
2.6. в редких случаях, если сервис окажется недоступен по нашей вине в течение длительного времени (более 7 дней подряд), и у тебя будет активная подписка, ты можешь написать нам в поддержку (контакт указан в биографии бота и в нашем telegram-канале). мы рассмотрим возможность продлить твою подписку на срок недоступности сервиса. решение принимается индивидуально.

3. твои и наши права и обязанности
3.1. что ожидается от тебя (твои обязанности):
•   использовать сервис только в законных целях и не нарушать никакие законы при его использовании.
•   не пытаться вмешаться в работу сервиса или получить несанкционированный доступ.
•   не использовать сервис для рассылки спама, вредоносных программ или любой запрещенной информации.
•   если требуется (например, для оплаты), предоставлять точную и правдивую информацию.
•   поскольку у сервиса нет возрастных ограничений, ты подтверждаешь свою способность принять условия настоящего соглашения.
3.2. что можем делать мы (наши права):
•   мы можем менять условия этого соглашения. если это произойдет, мы уведомим тебя, опубликовав новую версию соглашения в нашем telegram-канале или иным доступным способом в рамках сервиса. твое дальнейшее использование сервиса будет означать согласие с изменениями.
•   мы можем временно приостановить или полностью прекратить твой доступ к сервису, если ты нарушишь условия этого соглашения.
•   мы можем изменять сам сервис: добавлять или убирать функции, менять лимиты или стоимость подписки.

4. важное предупреждение об ограничении ответственности
4.1. сервис предоставляется "как есть". это значит, что мы не можем гарантировать его идеальную работу без сбоев или ошибок. технологии иногда подводят, и мы не несем ответственности за возможные проблемы, возникшие не по нашей прямой вине.
4.2. помни, личности — это искусственный интеллект. их ответы генерируются автоматически и могут быть неточными, неполными, странными или не соответствующими твоим ожиданиям или реальности. мы не несем никакой ответственности за содержание ответов, сгенерированных ai-собеседниками. не воспринимай их как истину в последней инстанции или профессиональный совет.
4.3. мы не несем ответственности за любые прямые или косвенные убытки или ущерб, который ты мог понести в результате использования (или невозможности использования) сервиса.

5. про твои данные (конфиденциальность)
5.1. для работы сервиса нам приходится собирать и обрабатывать минимальные данные: твой telegram id (для идентификации аккаунта), имя пользователя telegram (username, если есть), информацию о твоей подписке, информацию о созданных тобой личностях, а также историю твоих сообщений с личностями (это нужно ai для поддержания контекста разговора).
5.2. мы предпринимаем разумные шаги для защиты твоих данных, но, пожалуйста, помни, что передача информации через интернет никогда не может быть абсолютно безопасной.

6. действие соглашения
6.1. настоящее соглашение начинает действовать с момента, как ты впервые используешь сервис, и действует до момента, пока ты не перестанешь им пользоваться или пока сервис не прекратит свою работу.

7. интеллектуальная собственность
7.1. ты сохраняешь все права на контент (текст), который ты создаешь и вводишь в сервис в процессе взаимодействия с ai-собеседниками.
7.2. ты предоставляешь нам неисключительную, безвозмездную, действующую по всему миру лицензию на использование твоего контента исключительно в целях предоставления, поддержания и улучшения работы сервиса (например, для обработки твоих запросов, сохранения контекста диалога, анонимного анализа для улучшения моделей, если применимо).
7.3. все права на сам сервис (код бота, дизайн, название, графические элементы и т.д.) принадлежат владельцу сервиса.
7.4. ответы, сгенерированные ai-собеседниками, являются результатом работы алгоритмов искусственного интеллекта. ты можешь использовать полученные ответы в личных некоммерческих целях, но признаешь, что они созданы машиной и не являются твоей или нашей интеллектуальной собственностью в традиционном понимании.

8. заключительные положения
8.1. все споры и разногласия решаются путем переговоров. если это не поможет, споры будут рассматриваться в соответствии с законодательством российской федерации.
8.2. по всем вопросам, касающимся настоящего соглашения или работы сервиса, ты можешь обращаться к нам через контакты, указанные в биографии бота и в нашем telegram-канале.
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
                    await update.effective_message.reply_text("❌ произошла ошибка при форматировании ответа. пожалуйста, сообщите администратору.", parse_mode=None)
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

    error_message_raw = "упс... 😕 что-то пошло не так. попробуй еще раз позже."
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


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Sends the prompt and context to the Langdock API and returns the response."""
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return escape_markdown_v2("❌ ошибка: ключ api не настроен.")

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    messages_to_send = messages[-MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]

    payload = {
        "model": LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages_to_send,
        "max_tokens": 1024,
        "temperature": 0.7,
        "top_p": 0.95,
        "stream": False
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages. Temp: {payload['temperature']}. System prompt length: {len(system_prompt)}")

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
             logger.warning(f"Could not extract text from Langdock response structure: {data}")
             return escape_markdown_v2("ai вернул пустой ответ 🤷")

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return escape_markdown_v2("⏳ хм, кажется, я слишком долго думал... попробуй еще раз?")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {error_body}", exc_info=False)
        error_text_raw = f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."
        try:
             error_data = json.loads(error_body)
             if isinstance(error_data.get('error'), dict) and 'message' in error_data['error']:
                  api_error_msg = error_data['error']['message']
                  logger.error(f"Langdock API Error Message: {api_error_msg}")
             elif isinstance(error_data.get('error'), str):
                   logger.error(f"Langdock API Error Message: {error_data['error']}")
        except Exception: pass
        return escape_markdown_v2(error_text_raw)
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return escape_markdown_v2("❌ не могу связаться с ai сейчас (ошибка сети)...")
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return escape_markdown_v2("❌ произошла внутренняя ошибка при генерации ответа.")


async def process_and_send_response(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: Union[str, int],
    persona: Persona,
    full_bot_response_text: str,
    db: Session,
    reply_to_message_id: Optional[int] = None
) -> bool:
    """Processes the AI response, adds it to context, extracts GIFs, splits text, and sends messages."""
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return False

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}. ReplyTo: {reply_to_message_id}")

    chat_id_str = str(chat_id)
    context_prepared = False

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

    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    max_messages = 3
    if persona.config and hasattr(persona.config, 'max_response_messages'):
         max_messages = max(1, persona.config.max_response_messages or 3)

    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        if text_parts_to_send:
             ellipsis_raw = "..."
             last_part = text_parts_to_send[-1].rstrip('. ')
             if last_part and isinstance(last_part, str):
                text_parts_to_send[-1] = f"{last_part}{ellipsis_raw}"
             else:
                text_parts_to_send[-1] = ellipsis_raw

    send_tasks = []
    first_message_sent = False

    for gif in gif_links:
        try:
            current_reply_id = reply_to_message_id if not first_message_sent else None
            send_tasks.append(context.bot.send_animation(
                chat_id=chat_id_str,
                animation=gif,
                reply_to_message_id=current_reply_id
            ))
            first_message_sent = True
            logger.info(f"Scheduled sending gif: {gif} (ReplyTo: {current_reply_id})")
        except Exception as e:
            logger.error(f"Error scheduling gif send {gif} to chat {chat_id_str}: {e}", exc_info=True)

    if text_parts_to_send:
        chat_type = None
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_type = update.effective_chat.type

        for i, part in enumerate(text_parts_to_send):
            part_raw = part.strip()
            if not part_raw: continue

            if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 try:
                     asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING))
                     await asyncio.sleep(random.uniform(0.6, 1.2))
                 except Exception as e:
                      logger.warning(f"Failed to send typing action to {chat_id_str}: {e}")

            current_reply_id = reply_to_message_id if not first_message_sent else None

            try:
                 escaped_part = escape_markdown_v2(part_raw)
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id_str} (MDv2, ReplyTo: {current_reply_id}): '{escaped_part[:50]}...'")
                 send_tasks.append(context.bot.send_message(
                     chat_id=chat_id_str,
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
                               chat_id=chat_id_str,
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
                             chat_id=chat_id_str,
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
        f"упс! 😕 лимит сообщений ({count_raw}) на сегодня достигнут.\n\n"
        f"✨ хочешь большего? ✨\n"
        f"подписка за {price_raw} {currency_raw}/мес дает:\n"
        f"✅ до {paid_limit_raw} сообщений в день\n"
        f"✅ до {paid_persona_raw} личностей\n"
        f"✅ полная настройка поведения и настроений\n\n" # Обновлен текст
        f"👇 жми /subscribe или кнопку ниже!"
    )
    text_to_send = escape_markdown_v2(text_raw)

    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
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
                logger.debug(f"No active persona in chat {chat_id_str}. Ignoring message.")
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id} (TG ID: {owner_user.telegram_id}) in chat {chat_id_str}")

            limit_ok = check_and_update_user_limits(db, owner_user)
            limit_state_updated = db.is_modified(owner_user)

            if not limit_ok:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}).")
                await send_limit_exceeded_message(update, context, owner_user)
                if limit_state_updated:
                    db.commit()
                return

            context_user_msg_added = False
            if persona.chat_instance:
                try:
                    context_content = f"{username}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_user_msg_added = True
                    logger.debug("User message prepared for context (pending commit).")
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"Error preparing user message for context: {e_ctx}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("❌ ошибка при сохранении вашего сообщения."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else:
                logger.error("Cannot add user message to context, chat_instance is None unexpectedly.")
                await update.message.reply_text(escape_markdown_v2("❌ системная ошибка: не удалось связать сообщение с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Message saved to context, but ignoring response.")
                db.commit()
                return

            available_moods = persona.get_all_mood_names()
            matched_mood = None
            if message_text:
                 mood_lower = message_text.lower()
                 for m in available_moods:
                     if m.lower() == mood_lower:
                         matched_mood = m
                         break
            if matched_mood:
                 logger.info(f"Message '{message_text}' matched mood name '{matched_mood}'. Changing mood.")
                 db.commit()
                 with next(get_db()) as mood_db_session:
                      persona_for_mood_tuple = get_persona_and_context_with_owner(chat_id_str, mood_db_session)
                      if persona_for_mood_tuple:
                           await mood(update, context, db=mood_db_session, persona=persona_for_mood_tuple[0])
                      else:
                          logger.error(f"Could not re-fetch persona for mood change in chat {chat_id_str}")
                          await update.message.reply_text(escape_markdown_v2("❌ ошибка при смене настроения."), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            should_ai_respond = True
            ai_decision_response = None
            context_ai_decision_added = False
            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                reply_pref = persona.group_reply_preference
                logger.debug(f"Group chat detected. Persona '{persona.name}' reply preference: {reply_pref}")
                if reply_pref == "never":
                    should_ai_respond = False
                    logger.debug("Group reply preference is 'never'. Skipping response.")
                elif reply_pref == "always":
                    should_ai_respond = True
                    logger.debug("Group reply preference is 'always'. Will respond.")
                else: # mentioned_only or mentioned_or_contextual
                    # Use the Persona method to get the appropriate prompt
                    should_respond_prompt = persona.format_should_respond_prompt(message_text)
                    if should_respond_prompt:
                        try:
                            logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id_str} (pref: {reply_pref})...")
                            context_for_should_respond = get_context_for_chat_bot(db, persona.chat_instance.id)
                            ai_decision_response = await send_to_langdock(
                                system_prompt=should_respond_prompt,
                                messages=context_for_should_respond
                            )
                            answer = ai_decision_response.strip().lower()
                            logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")

                            if answer.startswith("да"):
                                should_ai_respond = True
                            elif answer.startswith("нет"):
                                should_ai_respond = False
                            else:
                                logger.warning(f"Unclear should_respond answer '{answer}'. Defaulting to respond for safety.")
                                should_ai_respond = True

                            if ai_decision_response and persona.chat_instance:
                                try:
                                    add_message_to_context(db, persona.chat_instance.id, "assistant", f"[Decision: {answer}]")
                                    context_ai_decision_added = True
                                    logger.debug("Added AI decision response to context (pending commit).")
                                except Exception as e_ctx_dec:
                                    logger.error(f"Failed to add AI decision to context: {e_ctx_dec}")

                        except Exception as e:
                            logger.error(f"Error in should_respond logic: {e}", exc_info=True)
                            should_ai_respond = True
                    else:
                        logger.warning(f"Could not generate should_respond prompt for pref '{reply_pref}'. Defaulting to respond.")
                        should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic or preference.")
                 db.commit()
                 return

            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                     logger.error(f"DB Error getting context for AI main response: {e_ctx}", exc_info=True)
                     await update.message.reply_text(escape_markdown_v2("❌ ошибка при получении контекста для ответа."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot get context for AI main response, chat_instance is None.")
                 db.rollback()
                 return

            # Use the Persona method to format the main system prompt
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                logger.error(f"System prompt formatting failed for persona {persona.name}.")
                await update.message.reply_text(escape_markdown_v2("❌ ошибка при подготовке ответа."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received main response from Langdock: {response_text[:100]}...")

            context_response_prepared = await process_and_send_response(
                update, context, chat_id_str, persona, response_text, db, reply_to_message_id=message_id
            )

            db.commit()
            logger.debug(f"Committed DB changes for handle_message chat {chat_id_str} (LimitUpdated: {limit_state_updated}, UserMsgAdded: {context_user_msg_added}, AIDecisionAdded: {context_ai_decision_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
             try: await update.message.reply_text(escape_markdown_v2("❌ ошибка базы данных, попробуйте позже."), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception: pass
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id_str}: {e}", exc_info=True)
            try: await update.message.reply_text(escape_markdown_v2("❌ произошла непредвиденная ошибка."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
            db.rollback()


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
                context_text_placeholder = "[получено фото]"
                prompt_generator = persona.format_photo_prompt
            elif media_type == "voice":
                context_text_placeholder = "[получено голосовое сообщение]"
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
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ ошибка при сохранении информации о медиа."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ системная ошибка: не удалось связать медиа с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
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
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ ошибка при получении контекста для ответа на медиа."), parse_mode=ParseMode.MARKDOWN_V2)
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
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ ошибка базы данных."), parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ произошла непредвиденная ошибка."), parse_mode=ParseMode.MARKDOWN_V2)
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
    fallback_text_raw = "Привет! Произошла ошибка отображения стартового сообщения. Используй /help или /menu."

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
                part1_raw = f"привет! я {persona.name}. я уже активен в этом чате.\n"
                part2_raw = "используй /menu для списка команд."
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

                status_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_raw = ""
                if user.is_active_subscriber and user.subscription_expires_at:
                     if user.subscription_expires_at > now + timedelta(days=365*10):
                         expires_raw = "(бессрочно)"
                     else:
                         expires_raw = f"до {user.subscription_expires_at.strftime('%d.%m.%Y')}"

                persona_count = len(user.persona_configs) if user.persona_configs else 0
                persona_limit_raw = f"{persona_count}/{user.persona_limit}"
                message_limit_raw = f"{user.daily_message_count}/{user.message_limit}"

                start_text_md = (
                    f"привет\\! 👋 я бот для создания ai\\-собеседников \\(`@{escape_markdown_v2(context.bot.username)}`\\)\\.\n\n"
                    f"*твой статус:* {escape_markdown_v2(status_raw)} {escape_markdown_v2(expires_raw)}\n"
                    f"*личности:* `{escape_markdown_v2(persona_limit_raw)}` \\| *сообщения:* `{escape_markdown_v2(message_limit_raw)}`\n\n"
                    f"*начало работы:*\n"
                    f"`/createpersona <имя>` \\- создай ai\\-личность\n"
                    f"`/mypersonas` \\- список твоих личностей\n"
                    f"`/menu` \\- панель управления\n"
                    f"`/profile` \\- детали статуса\n"
                    f"`/subscribe` \\- узнать о подписке"
                 )
                reply_text_final = start_text_md

                fallback_text_raw = (
                     f"привет! 👋 я бот для создания ai-собеседников (@{context.bot.username}).\n\n"
                     f"твой статус: {status_raw} {expires_raw}\n"
                     f"личности: {persona_limit_raw} | сообщения: {message_limit_raw}\n\n"
                     f"начало работы:\n"
                     f"/createpersona <имя> - создай ai-личность\n"
                     f"/mypersonas - список твоих личностей\n"
                     f"/menu - панель управления\n"
                     f"/profile - детали статуса\n"
                     f"/subscribe - узнать о подписке"
                )

                keyboard = [[InlineKeyboardButton("🚀 Меню Команд", callback_data="show_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            logger.debug(f"/start: Sending final message to user {user_id}.")
            await update.message.reply_text(reply_text_final, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "❌ ошибка при загрузке данных. попробуй позже."
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
            error_msg_raw = "❌ произошла ошибка при обработке команды /start."
            try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "❌ произошла ошибка при обработке команды /start."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command and the show_help callback."""
    # Определяем, был ли вызов через команду или кнопку
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user = update.effective_user
    user_id = user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    # Проверка подписки только для команды, не для коллбэка
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # Текст справки (оставляем тот же)
    help_text_md = (
        "🤖 *команды бота:*\n\n"
        "/start \\- начало работы с ботом\n"
        "/help \\- эта справка\n"
        "/menu \\- главное меню\n"
        "/profile \\- информация о вашем профиле\n"
        "/reset \\- сбросить диалог\n"
        "/mute \\- отключить бота в чате\n"
        "/unmute \\- включить бота в чате\n"
        "/subscribe \\- информация о премиум подписке\n\n"
        "🤖 *настройка личности:*\n\n"
        "/mood \\- изменить настроение бота\n"
        "/create \\- создать новую личность\n"
        "/my \\- список ваших личностей\n"
        "/edit \\- редактировать личность\n"
        "/delete \\- удалить личность\n\n"
        "🤖 *дополнительно:*\n\n"
        "бот может отвечать на фото и голосовые сообщения\\.\n"
        "в групповых чатах бот отвечает только на упоминания\\.\n"
        "для добавления бота в группу используйте кнопку в меню\\."
    )

    # Кнопка "Назад" только для коллбэка
    keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()

    # Отправка или редактирование сообщения
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
            # Простой текст для запасного варианта
            help_text_raw_no_md = help_text_md.replace('\\', '') # Убираем двойные слэши для простого текста
            try:
                # Отправляем простой текст без форматирования
                await context.bot.send_message(chat_id=chat_id_str, text=help_text_raw_no_md, reply_markup=reply_markup, parse_mode=None)
                if is_callback:
                    try: await query.delete_message()
                    except: pass
            except Exception as fallback_e:
                logger.error(f"Failed sending plain help message: {fallback_e}")
                if is_callback: await query.answer("❌ Ошибка отображения справки", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing help message: {e}", exc_info=True)
         if is_callback: await query.answer("❌ Ошибка отображения справки", show_alert=True)


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

    menu_text_raw = "🚀 Панель Управления\n\nВыберите действие:"
    menu_text_escaped = escape_markdown_v2(menu_text_raw)

    keyboard = [
        [
            InlineKeyboardButton("👤 Профиль", callback_data="show_profile"),
            InlineKeyboardButton("🎭 Мои Личности", callback_data="show_mypersonas")
        ],
        [
            InlineKeyboardButton("⭐ Подписка", callback_data="subscribe_info"),
            InlineKeyboardButton("❓ Помощь", callback_data="show_help")
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
                if is_callback: await query.answer("❌ Ошибка отображения меню", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing menu message: {e}", exc_info=True)
         if is_callback: await query.answer("❌ Ошибка отображения меню", show_alert=True)


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

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности.")
    error_persona_info = escape_markdown_v2("❌ ошибка: не найдена информация о личности.")
    error_no_moods_fmt_raw = "у личности '{persona_name}' не настроены настроения."
    error_bot_muted_fmt_raw = "🔇 личность '{persona_name}' сейчас заглушена \\(используй `/unmutebot`\\)."
    error_db = escape_markdown_v2("❌ ошибка базы данных при смене настроения.")
    error_general = escape_markdown_v2("❌ ошибка при обработке команды /mood.")
    success_mood_set_fmt_raw = "✅ настроение для '{persona_name}' теперь: *{mood_name}*"
    prompt_select_mood_fmt_raw = "текущее настроение: *{current_mood}*\\. выбери новое для '{persona_name}':"
    prompt_invalid_mood_fmt_raw = "не знаю настроения '{mood_arg}' для '{persona_name}'. выбери из списка:"

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
                    if is_callback: await update.callback_query.answer("Нет активной личности", show_alert=True)
                    await reply_target.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                logger.debug(f"No active persona for chat {chat_id_str}. Cannot set mood.")
                if close_db_later: db_session.close()
                return
            local_persona, _, _ = persona_info_tuple

        if not local_persona or not local_persona.chat_instance:
             logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id_str}.")
             reply_target = update.callback_query.message if is_callback else message_or_callback_msg
             if is_callback: await update.callback_query.answer("❌ Ошибка: не найдена информация о личности.", show_alert=True)
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
                 if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             reply_text = escape_markdown_v2(error_no_moods_fmt_raw.format(persona_name=persona_name_raw))
             try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Нет настроений", show_alert=True)
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
                         await query.answer(f"Настроение: {target_mood_original_case}")
                 else:
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood confirmation (BadRequest): {e} - Text: '{reply_text}'")
                  try:
                       reply_text_raw = f"✅ настроение для '{persona_name_raw}' теперь: {target_mood_original_case}"
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
                          mood_emoji_map = {"радость": "😊", "грусть": "😢", "злость": "😠", "милота": "🥰", "нейтрально": "😐"}
                          emoji = mood_emoji_map.get(mood_name.lower(), "🎭")
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
                 reply_text_raw = f"не знаю настроения '{mood_arg_lower}' для '{persona_name_raw}'. выбери из списка:"
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
             else:
                 reply_text = prompt_select_mood_fmt_raw.format(
                     current_mood=escape_markdown_v2(current_mood_text),
                     persona_name=persona_name_escaped
                     )
                 reply_text_raw = f"текущее настроение: {current_mood_text}. выбери новое для '{persona_name_raw}':"
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
             if is_callback: await update.callback_query.answer("❌ Ошибка БД", show_alert=True)
             await reply_target.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id_str}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("❌ Ошибка", show_alert=True)
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
    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности для сброса.")
    error_not_owner = escape_markdown_v2("❌ только владелец личности может сбросить её память.")
    error_no_instance = escape_markdown_v2("❌ ошибка: не найден экземпляр бота для сброса.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сбросе контекста.")
    error_general = escape_markdown_v2("❌ ошибка при сбросе контекста.")
    success_reset_fmt_raw = "✅ память личности '{persona_name}' в этом чате очищена."

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

    usage_text = escape_markdown_v2("формат: `/createpersona <имя> [описание]`\n_имя обязательно, описание нет\\._")
    error_name_len = escape_markdown_v2("❌ имя личности: 2\\-50 символов.")
    error_desc_len = escape_markdown_v2("❌ описание: до 1500 символов.")
    error_limit_reached_fmt_raw = "упс! 😕 достигнут лимит личностей ({current_count}/{limit}) для статуса {status_text}\\. чтобы создавать больше, используй /subscribe"
    error_name_exists_fmt_raw = "❌ личность с именем '{persona_name}' уже есть. выбери другое."
    success_create_fmt_raw = "✅ личность '{name}' создана!\nID: `{id}`\nописание: {description}\n\nтеперь можно настроить поведение через `/editpersona {id}` или сразу добавить в чат через `/mypersonas`" # Updated success message
    error_db = escape_markdown_v2("❌ ошибка базы данных при создании личности.")
    error_general = escape_markdown_v2("❌ ошибка при создании личности.")

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
                 status_text_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
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

            desc_raw = new_persona.description or "(пусто)"
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
             error_msg_ie_raw = f"❌ ошибка: личность '{persona_name_escaped}' уже существует \\(возможно, гонка запросов\\)\\. попробуй еще раз."
             await update.message.reply_text(escape_markdown_v2(error_msg_ie_raw), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
             logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
             try: await update.message.reply_text("❌ Произошла ошибка при отправке ответа.", parse_mode=None)
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

    error_db = escape_markdown_v2("❌ ошибка при загрузке списка личностей.")
    error_general = escape_markdown_v2("❌ произошла ошибка при получении списка личностей.")
    error_user_not_found = escape_markdown_v2("❌ ошибка: не удалось найти пользователя.")
    info_no_personas_fmt_raw = "у тебя пока нет личностей ({count}/{limit})\\. создай первую: `/createpersona <имя>`"
    info_list_header_fmt_raw = "🎭 *твои личности* \\\\({count}/{limit}\\\\):"
    fallback_text_plain = "Ошибка загрузки списка личностей."

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
                fallback_text_plain = f"у тебя пока нет личностей ({persona_count}/{persona_limit}). создай первую: /createpersona <имя>"
                keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
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
            fallback_lines = [f"Твои личности ({persona_count}/{persona_limit}):"]

            for p in personas:
                 message_lines.append(f"\n👤 *{escape_markdown_v2(p.name)}* \\(ID: `{p.id}`\\)")
                 fallback_lines.append(f"\n- {p.name} (ID: {p.id})")

                 edit_cb = f"edit_persona_{p.id}"
                 delete_cb = f"delete_persona_{p.id}"
                 add_cb = f"add_bot_{p.id}"
                 if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64 or len(add_cb.encode('utf-8')) > 64:
                      logger.warning(f"Callback data for persona {p.id} might be too long.")

                 keyboard.append([
                     InlineKeyboardButton("⚙️ Настроить", callback_data=edit_cb), # Changed text
                     InlineKeyboardButton("🗑️ Удалить", callback_data=delete_cb),
                     InlineKeyboardButton("➕ В чат", callback_data=add_cb)
                 ])

            text_to_send = "\n".join(message_lines)
            fallback_text_plain = "\n".join(fallback_lines)

            if is_callback:
                keyboard.append([InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            if is_callback:
                 if message_target.text != text_to_send or message_target.reply_markup != reply_markup:
                     await query.edit_message_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                 else: await query.answer()
            else: await message_target.reply_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

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

    usage_text = escape_markdown_v2("формат: `/addbot <id персоны>`\nили используй кнопку '➕ В чат' из `/mypersonas`")
    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности.")
    error_invalid_id_cmd = escape_markdown_v2("❌ id личности должен быть числом.")
    error_no_id = escape_markdown_v2("❌ ошибка: ID личности не определен.")
    error_persona_not_found_fmt_raw = "❌ личность с id `{id}` не найдена или не твоя."
    error_already_active_fmt_raw = "✅ личность '{name}' уже активна в этом чате."
    success_added_structure_raw = "✅ личность '{name}' \\(id: `{id}`\\) активирована в этом чате\\! память очищена."
    error_link_failed = escape_markdown_v2("❌ не удалось активировать личность (ошибка связывания).")
    error_integrity = escape_markdown_v2("❌ произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при добавлении бота.")
    error_general = escape_markdown_v2("❌ ошибка при активации личности.")

    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("❌ Ошибка: неверный ID", show_alert=True)
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
         if is_callback: await update.callback_query.answer("❌ Ошибка: ID не определен.", show_alert=True)
         else: await reply_target.reply_text(error_no_id, parse_mode=ParseMode.MARKDOWN_V2)
         return

    if is_callback:
        await update.callback_query.answer("Добавляем личность...")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 final_not_found_msg = error_persona_not_found_fmt_raw.format(id=local_persona_id)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
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
                    final_already_active_msg = error_already_active_fmt_raw.format(name=escape_markdown_v2(persona.name))
                    reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                    if is_callback: await update.callback_query.answer(f"'{persona.name}' уже активна", show_alert=True)
                    await reply_target.reply_text(final_already_active_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id_str} on re-add.")
                    deleted_ctx_result = existing_active_link.context.delete(synchronize_session='fetch')
                    deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                    db.commit()
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return
                else:
                    prev_persona_name = "Неизвестная личность"
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
                 await context.bot.send_message(chat_id=chat_id_str, text=final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
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
             try: await context.bot.send_message(chat_id=chat_id_str, text="❌ Произошла ошибка при отправке ответа.", parse_mode=None)
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
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

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
            try: await query.answer(text="❗ Подпишитесь на канал!", show_alert=True)
            except: pass
            return

    # --- Route non-conversation callbacks ---
    if data.startswith("set_mood_"):
        await mood(update, context)
    elif data == "subscribe_info":
        await query.answer()
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await query.answer("Создаю ссылку на оплату...")
        await generate_payment_link(update, context)
    elif data == "view_tos":
        await query.answer()
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer()
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
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
             await query.answer("Неизвестное действие")
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

    error_db = escape_markdown_v2("❌ ошибка базы данных при загрузке профиля.")
    error_general = escape_markdown_v2("❌ ошибка при обработке команды /profile.")
    error_user_not_found = escape_markdown_v2("❌ ошибка: пользователь не найден.")
    profile_text_plain = "Ошибка загрузки профиля."

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
            status_text_escaped = escape_markdown_v2("⭐ Premium" if is_active_subscriber else "🆓 Free")
            expires_text_md = ""
            expires_text_plain = ""

            if is_active_subscriber and user_db.subscription_expires_at:
                 try:
                     if user_db.subscription_expires_at > now + timedelta(days=365*10):
                         expires_text_md = escape_markdown_v2("активна (бессрочно)")
                         expires_text_plain = "активна (бессрочно)"
                     else:
                         date_str = user_db.subscription_expires_at.strftime('%d.%m.%Y %H:%M')
                         expires_text_md = f"активна до: *{escape_markdown_v2(date_str)}* UTC"
                         expires_text_plain = f"активна до: {date_str} UTC"
                 except AttributeError:
                      expires_text_md = escape_markdown_v2("активна (дата истечения некорректна)")
                      expires_text_plain = "активна (дата истечения некорректна)"
            elif is_active_subscriber:
                 expires_text_md = escape_markdown_v2("активна (бессрочно)")
                 expires_text_plain = "активна (бессрочно)"
            else:
                 expires_text_md = escape_markdown_v2("нет активной подписки")
                 expires_text_plain = "нет активной подписки"

            persona_count = len(user_db.persona_configs) if user_db.persona_configs is not None else 0
            persona_limit_raw = f"{persona_count}/{user_db.persona_limit}"
            msg_limit_raw = f"{user_db.daily_message_count}/{user_db.message_limit}"
            persona_limit_escaped = escape_markdown_v2(persona_limit_raw)
            msg_limit_escaped = escape_markdown_v2(msg_limit_raw)

            profile_text_md = (
                f"👤 *Твой профиль*\n\n"
                f"*Статус:* {status_text_escaped}\n"
                f"{expires_text_md}\n\n"
                f"*Лимиты:*\n"
                f"сообщения сегодня: `{msg_limit_escaped}`\n"
                f"создано личностей: `{persona_limit_escaped}`\n\n"
            )
            promo_text_md = "🚀 хочешь больше\\? жми `/subscribe` или кнопку 'Подписка' в `/menu`\\!"
            promo_text_plain = "🚀 Хочешь больше? Жми /subscribe или кнопку 'Подписка' в /menu !"
            if not is_active_subscriber:
                profile_text_md += promo_text_md

            profile_text_plain = (
                f"👤 Твой профиль\n\n"
                f"Статус: {'Premium' if is_active_subscriber else 'Free'}\n"
                f"{expires_text_plain}\n\n"
                f"Лимиты:\n"
                f"Сообщения сегодня: {msg_limit_raw}\n"
                f"Создано личностей: {persona_limit_raw}\n\n"
            )
            if not is_active_subscriber:
                profile_text_plain += promo_text_plain

            final_text_to_send = profile_text_md

            keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
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

    error_payment_unavailable = escape_markdown_v2("❌ к сожалению, функция оплаты сейчас недоступна \\(проблема с настройками\\)\\. 😥")
    # Убираем ручное экранирование !
    info_confirm = escape_markdown_v2(
         "✅ отлично!\n\n"  # <--- Просто ! без бэкслешей
         "нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "пользовательским соглашением\\."
         "\n\n👇"
    )
    text = ""
    reply_markup = None
    text_raw = ""

    if not yookassa_ready:
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if from_callback else None
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
        text_raw = "❌ к сожалению, функция оплаты сейчас недоступна (проблема с настройками). 😥"
    else:
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        currency_raw = SUBSCRIPTION_CURRENCY
        duration_raw = str(SUBSCRIPTION_DURATION_DAYS)
        paid_limit_raw = str(PAID_DAILY_MESSAGE_LIMIT)
        free_limit_raw = str(FREE_DAILY_MESSAGE_LIMIT)
        paid_persona_raw = str(PAID_PERSONA_LIMIT)
        free_persona_raw = str(FREE_PERSONA_LIMIT)

        text_md = (
            f"✨ *Премиум подписка* \\({escape_markdown_v2(price_raw)} {escape_markdown_v2(currency_raw)}/мес\\) ✨\n\n"
            f"*Получите максимум возможностей:*\n"
            f"✅ до `{escape_markdown_v2(paid_limit_raw)}` сообщений в день \\(вместо `{escape_markdown_v2(free_limit_raw)}`\\)\n"
            f"✅ до `{escape_markdown_v2(paid_persona_raw)}` личностей \\(вместо `{escape_markdown_v2(free_persona_raw)}`\\)\n"
            f"✅ полная настройка поведения\n"
            f"✅ создание и редактирование своих настроений\n"
            f"✅ приоритетная поддержка\n\n"
            f"*Срок действия:* {escape_markdown_v2(duration_raw)} дней\\."
        )
        text = text_md

        text_raw = (
            f"✨ Премиум подписка ({price_raw} {currency_raw}/мес) ✨\n\n"
            f"Получите максимум возможностей:\n"
            f"✅ {paid_limit_raw} сообщений в день (вместо {free_limit_raw})\n"
            f"✅ {paid_persona_raw} личностей (вместо {free_persona_raw})\n"
            f"✅ полная настройка поведения\n"
            f"✅ создание и редактирование своих настроений\n"
            f"✅ приоритетная поддержка\n\n"
            f"Срок действия: {duration_raw} дней."
        )

        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            [InlineKeyboardButton("✅ Принять и оплатить", callback_data="confirm_pay")]
        ]
        if from_callback:
             keyboard.append([InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")])
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
    error_tos_link = "❌ Не удалось отобразить ссылку на соглашение."
    error_tos_load = escape_markdown_v2("❌ не удалось загрузить ссылку на пользовательское соглашение. попробуйте позже.")
    info_tos = escape_markdown_v2("ознакомьтесь с пользовательским соглашением, открыв его по ссылке ниже:")

    if tos_url:
        keyboard = [
            [InlineKeyboardButton("📜 Открыть Соглашение", url=tos_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]
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
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer()
        except Exception as e:
             logger.error(f"Failed to show ToS error message to user {user_id}: {e}")
             await query.answer("❌ Ошибка загрузки соглашения.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the confirm_pay callback after user agrees to ToS."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("❌ к сожалению, функция оплаты сейчас недоступна \\(проблема с настройками\\)\\. 😥")
    info_confirm = escape_markdown_v2(
         "✅ отлично\\!\n\n"
         "нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "пользовательским соглашением\\."
         "\n\n👇"
    )
    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        # Формируем текст СРАЗУ с экранированием и реальными переносами
        info_confirm_md = (
             "✅ отлично\!\n\n"  # Экранируем ! и используем реальный перенос
             "нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
             "пользовательским соглашением\.\n\n" # Экранируем . и используем реальный перенос
             "👇"
        )
        text = info_confirm_md # Передаем уже экранированный текст
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        currency_raw = SUBSCRIPTION_CURRENCY
        # Экранируем символы в тексте кнопки, если они там могут быть (на всякий случай)
        button_text_raw = f"💳 Оплатить {price_raw} {currency_raw}"
        button_text = button_text_raw # Кнопки не требуют Markdown экранирования

        keyboard = [
            [InlineKeyboardButton(button_text, callback_data="subscribe_pay")]
        ]
        # URL в кнопке не требует экранирования
        if tos_url:
             keyboard.append([InlineKeyboardButton("📜 Условия использования (прочитано)", url=tos_url)])
        else:
             # Текст кнопки не требует спец. экранирования, т.к. не MD
             keyboard.append([InlineKeyboardButton("📜 Условия (ошибка загрузки)", callback_data="view_tos")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if query.message.text != text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN_V2
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

    error_yk_not_ready = escape_markdown_v2("❌ ошибка: сервис оплаты не настроен правильно.")
    error_yk_config = escape_markdown_v2("❌ ошибка конфигурации платежной системы.")
    error_receipt = escape_markdown_v2("❌ ошибка при формировании данных чека.")
    error_link_get_fmt_raw = "❌ не удалось получить ссылку от платежной системы{status_info}\\. попробуй позже."
    error_link_create_raw = "❌ не удалось создать ссылку для оплаты\\. {error_detail}\\. попробуй еще раз позже или свяжись с поддержкой."
    # Ручное экранирование для success_link
    success_link_md = (
        "✨ *Ссылка для оплаты создана\!*\n\n"
        "Нажмите кнопку ниже для перехода к оплате\.\n"
        "После успешной оплаты подписка активируется автоматически \(может занять до 5 минут\)\.\n\n"
        "Если возникнут проблемы, обратитесь в поддержку\."
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
    payment_description = f"Premium подписка @NunuAiBot на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)}
    bot_username = context.bot_data.get('bot_username', "NunuAiBot")
    return_url = f"https://t.me/{bot_username}"

    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ @{bot_username} на {SUBSCRIPTION_DURATION_DAYS} дней",
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
             status_info = f" \\(статус: {escape_markdown_v2(payment_response.status)}\\)" if payment_response and payment_response.status else ""
             error_message = error_link_get_fmt_raw.format(status_info=status_info)
             text = error_message
             reply_markup = None
             await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = success_link_md # Используем вручную экранированную строку
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        error_detail = ""
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                if "Invalid credentials" in err_text:
                    error_detail = "ошибка аутентификации с юkassa"
                elif "receipt" in err_text.lower():
                     error_detail = "ошибка данных чека \\(детали в логах\\)"
                else:
                    error_detail = "ошибка от юkassa \\(детали в логах\\)"
            except Exception as parse_e:
                logger.error(f"Could not parse YK error response: {parse_e}")
                error_detail = "ошибка от юkassa \\(не удалось разобрать ответ\\)"
        elif isinstance(e, httpx.RequestError):
             error_detail = "проблема с сетевым подключением к юkassa"
        else:
             error_detail = "произошла непредвиденная ошибка"

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

    error_not_found_fmt_raw = "❌ личность с id `{id}` не найдена или не твоя."
    error_db = escape_markdown_v2("❌ ошибка базы данных при начале редактирования.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 final_error_msg = error_not_found_fmt_raw.format(id=persona_id)
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
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

    usage_text = escape_markdown_v2("укажи id личности: `/editpersona <id>`\nили используй кнопку из `/mypersonas`")
    error_invalid_id = escape_markdown_v2("❌ id должен быть числом.")

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
    await query.answer("Начинаем редактирование...")

    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности в кнопке.")

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
    star = " ⭐"

    # Get current values for display
    style = persona_config.communication_style or "neutral"
    verbosity = persona_config.verbosity_level or "medium"
    group_reply = persona_config.group_reply_preference or "mentioned_or_contextual"
    media_react = persona_config.media_reaction or "text_only"

    # Map internal keys to user-friendly text (ПОЛНЫЕ СЛОВА)
    style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
    verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
    group_reply_map = {"always": "Всегда", "mentioned_only": "По @", "mentioned_or_contextual": "По @ / Контексту", "never": "Никогда"}
    media_react_map = {"all": "Текст+GIF", "text_only": "Только текст", "none": "Никак", "photo_only": "Только фото", "voice_only": "Только голос"}

    # Build keyboard with full text
    keyboard = [
        [
            InlineKeyboardButton("✏️ Имя", callback_data="edit_wizard_name"),
            InlineKeyboardButton("📜 Описание", callback_data="edit_wizard_description")
        ],
        [InlineKeyboardButton(f"💬 Стиль ({style_map.get(style, '?')})", callback_data="edit_wizard_comm_style")],
        [InlineKeyboardButton(f"🗣️ Разговорчивость ({verbosity_map.get(verbosity, '?')})", callback_data="edit_wizard_verbosity")],
        [InlineKeyboardButton(f"👥 Отв. в группе ({group_reply_map.get(group_reply, '?')})", callback_data="edit_wizard_group_reply")],
        [InlineKeyboardButton(f"🖼️ Реакт. на медиа ({media_react_map.get(media_react, '?')})", callback_data="edit_wizard_media_reaction")],
        [InlineKeyboardButton(f"🎭 Настроения{star if not is_premium else ''}", callback_data="edit_wizard_moods")],
        [InlineKeyboardButton("✅ Завершить", callback_data="finish_edit")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Объединяю f-строку
    msg_text = f"⚙️ *Настройка личности: {escape_markdown_v2(persona_config.name)}* \\(ID: `{persona_id}`\)\\\\n\nВыберите, что изменить:"

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
        await context.bot.send_message(chat_id, escape_markdown_v2("❌ Ошибка отображения меню."), parse_mode=ParseMode.MARKDOWN_V2)
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
    elif data == "edit_wizard_moods":
        with next(get_db()) as db:
            owner = db.query(User).join(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            if owner and (owner.is_active_subscriber or is_admin(user_id)):
                return await edit_moods_entry(update, context)
            else:
                await query.answer("⭐ Доступно по подписке", show_alert=True)
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
    prompt_text = escape_markdown_v2(f"✏️ Введите новое имя (текущее: '{current_name}', 2-50 симв.):")
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_NAME

async def edit_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_NAME
    new_name = update.message.text.strip()
    persona_id = context.user_data.get('edit_persona_id')

    if not (2 <= len(new_name) <= 50):
        await update.message.reply_text(escape_markdown_v2("❌ Имя: 2-50 симв. Попробуйте еще:"))
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
                await update.message.reply_text(escape_markdown_v2(f"❌ Имя '{new_name}' уже занято. Введите другое:"))
                return EDIT_NAME

            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
            if persona:
                persona.name = new_name
                db.commit()
                await update.message.reply_text(escape_markdown_v2(f"✅ Имя обновлено на '{new_name}'."))
                # Delete the prompt message before showing menu
                prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
                if prompt_msg_id:
                    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                    except Exception: pass
                return await _show_edit_wizard_menu(update, context, persona)
            else:
                await update.message.reply_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error updating persona name for {persona_id}: {e}")
        await update.message.reply_text(escape_markdown_v2("❌ Ошибка при сохранении имени."))
        return await _try_return_to_wizard_menu(update, context, update.effective_user.id, persona_id)

# --- Edit Description ---
async def edit_description_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_desc = db.query(PersonaConfig.description).filter(PersonaConfig.id == persona_id).scalar() or "(пусто)"
    current_desc_preview = escape_markdown_v2((current_desc[:100] + '...') if len(current_desc) > 100 else current_desc)
    prompt_text = escape_markdown_v2(f"✏️ Введите новое описание (макс. 1500 симв.).\nТекущее (начало): \n```\n{current_desc_preview}\n```")
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_DESCRIPTION

async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_DESCRIPTION
    new_desc = update.message.text.strip()
    persona_id = context.user_data.get('edit_persona_id')

    if len(new_desc) > 1500:
        await update.message.reply_text(escape_markdown_v2("❌ Описание: макс. 1500 симв. Попробуйте еще:"))
        return EDIT_DESCRIPTION

    try:
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).with_for_update().first()
            if persona:
                persona.description = new_desc
                db.commit()
                await update.message.reply_text(escape_markdown_v2("✅ Описание обновлено."))
                prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
                if prompt_msg_id:
                    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                    except Exception: pass
                return await _show_edit_wizard_menu(update, context, persona)
            else:
                await update.message.reply_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error updating persona description for {persona_id}: {e}")
        await update.message.reply_text(escape_markdown_v2("❌ Ошибка при сохранении описания."))
        return await _try_return_to_wizard_menu(update, context, update.effective_user.id, persona_id)

# --- Edit Communication Style ---
async def edit_comm_style_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current_style = db.query(PersonaConfig.communication_style).filter(PersonaConfig.id == persona_id).scalar() or "neutral"
    prompt_text = escape_markdown_v2(f"💬 Выберите стиль общения (текущий: {current_style}):")
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_style == 'neutral' else ''}😐 Нейтральный", callback_data="set_comm_style_neutral")],
        [InlineKeyboardButton(f"{'✅ ' if current_style == 'friendly' else ''}😊 Дружелюбный", callback_data="set_comm_style_friendly")],
        [InlineKeyboardButton(f"{'✅ ' if current_style == 'sarcastic' else ''}😏 Саркастичный", callback_data="set_comm_style_sarcastic")],
        [InlineKeyboardButton(f"{'✅ ' if current_style == 'formal' else ''}✍️ Формальный", callback_data="set_comm_style_formal")],
        [InlineKeyboardButton(f"{'✅ ' if current_style == 'brief' else ''}🗣️ Краткий", callback_data="set_comm_style_brief")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]
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
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting communication_style for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("❌ Ошибка при сохранении стиля общения."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_comm_style_received: {data}")
        return EDIT_COMM_STYLE

# --- Edit Verbosity ---
async def edit_verbosity_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.verbosity_level).filter(PersonaConfig.id == persona_id).scalar() or "medium"
    prompt_text = escape_markdown_v2(f"🗣️ Выберите разговорчивость (текущая: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current == 'concise' else ''}🤏 Лаконичный", callback_data="set_verbosity_concise")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'medium' else ''}💬 Средний", callback_data="set_verbosity_medium")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'talkative' else ''}📚 Болтливый", callback_data="set_verbosity_talkative")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]
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
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting verbosity_level for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("❌ Ошибка при сохранении разговорчивости."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_verbosity_received: {data}")
        return EDIT_VERBOSITY

# --- Edit Group Reply Preference ---
async def edit_group_reply_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.group_reply_preference).filter(PersonaConfig.id == persona_id).scalar() or "mentioned_or_contextual"
    prompt_text = escape_markdown_v2(f"👥 Как отвечать в группах (текущее: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current == 'always' else ''}📢 Всегда", callback_data="set_group_reply_always")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'mentioned_only' else ''}🎯 Только по упоминанию (@)", callback_data="set_group_reply_mentioned_only")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'mentioned_or_contextual' else ''}🤔 По @ или контексту", callback_data="set_group_reply_mentioned_or_contextual")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'never' else ''}🚫 Никогда", callback_data="set_group_reply_never")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]
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
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting group_reply_preference for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("❌ Ошибка при сохранении настройки ответа в группе."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_group_reply_received: {data}")
        return EDIT_GROUP_REPLY

# --- Edit Media Reaction ---
async def edit_media_reaction_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_id = context.user_data.get('edit_persona_id')
    with next(get_db()) as db:
        current = db.query(PersonaConfig.media_reaction).filter(PersonaConfig.id == persona_id).scalar() or "text_only"
    prompt_text = escape_markdown_v2(f"🖼️ Как реагировать на фото/голос (текущее: {current}):")
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current == 'all' else ''}✍️ Текст + GIF", callback_data="set_media_react_all")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'text_only' else ''}💬 Только текст", callback_data="set_media_react_text_only")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'photo_only' else ''}🖼️ Только на фото", callback_data="set_media_react_photo_only")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'voice_only' else ''}🎤 Только на голос", callback_data="set_media_react_voice_only")],
        [InlineKeyboardButton(f"{'✅ ' if current == 'none' else ''}🚫 Никак", callback_data="set_media_react_none")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]
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
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: личность не найдена."))
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error setting media_reaction for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("❌ Ошибка при сохранении настройки реакции на медиа."))
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
            await query.answer("⭐ Доступно по подписке", show_alert=True)
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

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при загрузке настроений.")
    prompt_mood_menu_fmt_raw = "🎭 управление настроениями для *{name}*:"

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
                    await query.answer("Личность не найдена", show_alert=True)
                    await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    context.user_data.clear()
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("❌ Ошибка базы данных", show_alert=True)
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

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных.")
    error_unhandled_choice = escape_markdown_v2("❌ неизвестный выбор настроения.")
    error_decode_mood = escape_markdown_v2("❌ ошибка декодирования имени настроения.")
    prompt_new_name = escape_markdown_v2("введи название нового настроения \\(1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\):")
    prompt_new_prompt_fmt_raw = "✏️ редактирование настроения: *{name}*\n\nотправь новый текст промпта \\(до 1500 симв\\.\\):"
    prompt_confirm_delete_fmt_raw = "точно удалить настроение '{name}'?"

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
                 await query.answer("Личность не найдена", show_alert=True)
                 await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("❌ Ошибка базы данных", show_alert=True)
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
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
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
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
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
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
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
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена.")
    error_validation = escape_markdown_v2("❌ название: 1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\. попробуй еще:")
    error_name_exists_fmt_raw = "❌ настроение '{name}' уже существует\\. выбери другое:"
    error_db = escape_markdown_v2("❌ ошибка базы данных при проверке имени.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка.")
    prompt_for_prompt_fmt_raw = "отлично\\! теперь отправь текст промпта для настроения '{name}':"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
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
                cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
                final_exists_msg = error_name_exists_fmt_raw.format(name=escape_markdown_v2(mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
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

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна \\(нет имени настроения\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_validation = escape_markdown_v2("❌ промпт настроения: 1\\-1500 символов\\. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения.")
    success_saved_fmt_raw = "✅ настроение *{name}* сохранено\\!"

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
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

    error_no_session = escape_markdown_v2("❌ ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found_persona = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения.")
    info_not_found_mood_fmt_raw = "настроение '{name}' не найдено \\(уже удалено\\?\\)\\."
    error_decode_mood = escape_markdown_v2("❌ ошибка декодирования имени настроения для удаления.")
    success_delete_fmt_raw = "🗑️ настроение *{name}* удалено\\."

    original_name_from_callback = None
    if data.startswith("deletemood_delete_"):
        try:
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await query.answer("❌ Ошибка данных", show_alert=True)
            await context.bot.send_message(chat_id, error_decode_mood, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    if not mood_name_to_delete or not persona_id or not original_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. Stored='{mood_name_to_delete}', Callback='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("❌ Ошибка сессии", show_alert=True)
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('delete_mood_name', None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    await query.answer("Удаляем...")
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

     error_cannot_return = escape_markdown_v2("❌ не удалось вернуться к меню настроений \\(личность не найдена\\)\\.")
     error_cannot_return_general = escape_markdown_v2("❌ не удалось вернуться к меню настроений.")
     prompt_mood_menu_raw = "🎭 управление настроениями для *{name}*:"

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
    """Handles finishing the persona editing conversation via the 'Завершить' button."""
    query = update.callback_query
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} finished editing persona {persona_id}.")

    finish_message = escape_markdown_v2("✅ редактирование завершено.")

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
                await context.bot.send_message(chat_id=chat_id, text="Редактирование завершено.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
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

    cancel_message = escape_markdown_v2("редактирование отменено.")

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
                await context.bot.send_message(chat_id=chat_id, text="Редактирование отменено.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
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

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()

    error_not_found_fmt_raw = "❌ личность с id `{id}` не найдена или не твоя."
    prompt_delete_fmt_raw = "🚨 *ВНИМАНИЕ\\!* 🚨\nудалить личность '{name}' \\(ID: `{id}`\\)?\n\nэто действие *НЕОБРАТИМО\\!*"
    error_db = escape_markdown_v2("❌ ошибка базы данных.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 final_error_msg = error_not_found_fmt_raw.format(id=persona_id)
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            context.user_data['delete_persona_id'] = persona_id
            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_name_display}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = prompt_delete_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
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

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
            return DELETE_PERSONA_CONFIRM
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

    usage_text = escape_markdown_v2("укажи id личности: `/deletepersona <id>`\nили используй кнопку из `/mypersonas`")
    error_invalid_id = escape_markdown_v2("❌ id должен быть числом.")

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
    await query.answer("Начинаем удаление...")

    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности в кнопке.")

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
    """Handles the final confirmation button press for deletion."""
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')
    chat_id = query.message.chat.id

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("❌ ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_delete_failed = escape_markdown_v2("❌ не удалось удалить личность \\(ошибка базы данных\\)\\.")
    success_deleted_fmt_raw = "✅ личность '{name}' удалена."

    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.answer("❌ Ошибка сессии", show_alert=True)
         await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear()
         return ConversationHandler.END

    await query.answer("Удаляем...")
    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id}"

    try:
        with next(get_db()) as db:
             user = db.query(User).filter(User.telegram_id == user_id).first()
             if not user:
                  logger.error(f"User {user_id} not found in DB during persona deletion.")
                  await context.bot.send_message(chat_id, escape_markdown_v2("❌ Ошибка: пользователь не найден."), reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                  context.user_data.clear()
                  return ConversationHandler.END

             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Found persona '{persona_name_deleted}' (ID: {persona_id}) for deletion.")
             else:
                 logger.warning(f"Persona {persona_id} not found for user {user.id} just before calling delete function (might be already deleted).")

             deleted_ok = delete_persona_config(db, persona_id, user.id)

             if not deleted_ok and not persona_to_delete:
                 logger.warning(f"Persona {persona_id} was not found by delete_persona_config (likely already deleted). Treating as success.")
                 deleted_ok = True

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
        deleted_ok = False
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)
        deleted_ok = False

    if deleted_ok:
        final_success_msg = success_deleted_fmt_raw.format(name=escape_markdown_v2(persona_name_deleted))
        try:
            await query.edit_message_text(final_success_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with deletion success: {e}")
    else:
        try:
            await query.edit_message_text(error_delete_failed, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with deletion failure: {e}")

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

    cancel_message = escape_markdown_v2("удаление отменено.")

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

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности.")
    error_not_owner = escape_markdown_v2("❌ только владелец личности может ее заглушить.")
    error_no_instance = escape_markdown_v2("❌ ошибка: не найден объект связи с чатом.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при попытке заглушить бота.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при выполнении команды.")
    info_already_muted_fmt_raw = "🔇 личность '{name}' уже заглушена в этом чате."
    success_muted_fmt_raw = "🔇 личность '{name}' больше не будет отвечать в этом чате \\(но будет запоминать сообщения\\)\\. используйте `/unmutebot`, чтобы вернуть."

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

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности, которую можно размьютить.")
    error_not_owner = escape_markdown_v2("❌ только владелец личности может снять заглушку.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при попытке вернуть бота к общению.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при выполнении команды.")
    info_not_muted_fmt_raw = "🔊 личность '{name}' не была заглушена."
    success_unmuted_fmt_raw = "🔊 личность '{name}' снова может отвечать в этом чате."

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
