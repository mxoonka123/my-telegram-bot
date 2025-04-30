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

from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, Chat as TgChat
from telegram.constants import ChatAction, ParseMode, ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut

from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func

from yookassa import Configuration, Payment
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

async def check_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set in config. Skipping subscription check.")
        return True

    user_id = None
    if hasattr(update, 'effective_user') and update.effective_user:
        user_id = update.effective_user.id
    elif update.callback_query and update.callback_query.from_user:
        user_id = update.callback_query.from_user.id
        logger.debug(f"Using user_id {user_id} from callback_query.")
    else:
        logger.warning("check_channel_subscription called without valid user information.")
        return False

    if is_admin(user_id):
        return True

    logger.debug(f"Checking subscription status for user {user_id} in channel {CHANNEL_ID}")
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id, read_timeout=10)
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
        target_message = update.effective_message or (update.callback_query.message if update.callback_query else None)
        if target_message:
            try:
                await target_message.reply_text(
                    escape_markdown_v2("Не удалось проверить подписку на канал (таймаут)\\. Попробуйте еще раз позже\\."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Timeout' error message: {send_err}")
        return False
    except Forbidden as e:
        logger.error(f"Forbidden error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}. Ensure bot is admin in the channel.")
        target_message = update.effective_message or (update.callback_query.message if update.callback_query else None)
        if target_message:
            try:
                await target_message.reply_text(
                    escape_markdown_v2("Не удалось проверить подписку на канал\\. Убедитесь, что бот добавлен в канал как администратор\\."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False
    except BadRequest as e:
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
         reply_text = escape_markdown_v2("Произошла ошибка при проверке подписки \\(BadRequest\\)\\. Попробуйте позже\\.")
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             reply_text = escape_markdown_v2("Не удается получить доступ к списку участников канала для проверки подписки\\. Возможно, настройки канала не позволяют это сделать\\.")
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             return False
         elif "chat not found" in error_message:
              logger.error(f"-> Specific BadRequest: Chat {CHANNEL_ID} not found. Check CHANNEL_ID config.")
              reply_text = escape_markdown_v2("Ошибка: не удалось найти указанный канал для проверки подписки\\. Проверьте настройки бота\\.")

         target_message = update.effective_message or (update.callback_query.message if update.callback_query else None)
         if target_message:
             try: await target_message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Failed to send 'BadRequest' error message: {send_err}")
         return False
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        target_message = update.effective_message or (update.callback_query.message if update.callback_query else None)
        if target_message:
            try: await target_message.reply_text(escape_markdown_v2("Произошла ошибка при проверке подписки\\. Попробуйте позже\\."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Failed to send 'TelegramError' message: {send_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}", exc_info=True)
        return False

async def send_subscription_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = None
    if hasattr(update, 'effective_message') and update.effective_message:
         target_message = update.effective_message
    elif update.callback_query and update.callback_query.message:
         target_message = update.callback_query.message

    if not target_message:
         logger.warning("Cannot send subscription required message: no target message found.")
         return

    channel_username = None
    if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith('@'):
        channel_username = CHANNEL_ID.lstrip('@')

    error_msg_raw = "Произошла ошибка при получении ссылки на канал." # Plain text for logic
    subscribe_text_raw = "Для использования бота необходимо подписаться на наш канал."
    button_text = "Перейти к каналу"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"Для использования бота необходимо подписаться на канал @{channel_username}."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         subscribe_text_raw = "Для использования бота необходимо подписаться на наш основной канал. Пожалуйста, найдите канал в поиске или через описание бота."
    else:
         logger.error(f"Invalid CHANNEL_ID format: {CHANNEL_ID}. Cannot generate subscription message correctly.")
         subscribe_text_raw = error_msg_raw # Use plain error text

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
            plain_text = re.sub(r'\\(.)', r'\1', escaped_text) # Use the previously escaped text and unescape
            await target_message.reply_text(plain_text, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
            logger.error(f"Failed sending plain subscription required message: {fallback_e}")
    except Exception as e:
         logger.error(f"Failed to send subscription required message: {e}")

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Keep FIELD_MAP escaped as it's used directly for display names often
FIELD_MAP = {
    "name": escape_markdown_v2("имя"),
    "description": escape_markdown_v2("описание"),
    "system_prompt_template": escape_markdown_v2("системный промпт"),
    "should_respond_prompt_template": escape_markdown_v2("промпт 'отвечать?'"),
    "spam_prompt_template": escape_markdown_v2("промпт спама"),
    "photo_prompt_template": escape_markdown_v2("промпт фото"),
    "voice_prompt_template": escape_markdown_v2("промпт голоса"),
    "max_response_messages": escape_markdown_v2("макс. сообщений в ответе")
}

TOS_TEXT_RAW = """
**📜 Пользовательское Соглашение Сервиса @NunuAiBot**

Привет! Добро пожаловать в @NunuAiBot! Мы очень рады, что вы с нами. Это Соглашение — документ, который объясняет правила использования нашего Сервиса. Прочитайте его, пожалуйста.

Дата последнего обновления: 01.03.2025

**1. О чем это Соглашение?**
1.1. Это Пользовательское Соглашение (или просто "Соглашение") — договор между вами (далее – "Пользователь" или "Вы") и нами (владельцем Telegram-бота @NunuAiBot, далее – "Сервис" или "Мы"). Оно описывает условия использования Сервиса.
1.2. Начиная использовать наш Сервис (просто отправляя боту любое сообщение или команду), Вы подтверждаете, что прочитали, поняли и согласны со всеми условиями этого Соглашения. Если Вы не согласны хотя бы с одним пунктом, пожалуйста, прекратите использование Сервиса.
1.3. Наш Сервис предоставляет Вам интересную возможность создавать и общаться с виртуальными собеседниками на базе искусственного интеллекта (далее – "Личности" или "AI-собеседники").

**2. Про подписку и оплату**
2.1. Мы предлагаем два уровня доступа: бесплатный и Premium (платный). Возможности и лимиты для каждого уровня подробно описаны внутри бота, например, в командах `/profile` и `/subscribe`.
2.2. Платная подписка дает Вам расширенные возможности и увеличенные лимиты на период в {subscription_duration} дней.
2.3. Стоимость подписки составляет {subscription_price} {subscription_currency} за {subscription_duration} дней.
2.4. Оплата проходит через безопасную платежную систему Yookassa. Важно: мы не получаем и не храним Ваши платежные данные (номер карты и т.п.). Все безопасно.
2.5. **Политика возвратов:** Покупая подписку, Вы получаете доступ к расширенным возможностям Сервиса сразу же после оплаты. Поскольку Вы получаете услугу немедленно, оплаченные средства за этот период доступа, к сожалению, **не подлежат возврату**.
2.6. В редких случаях, если Сервис окажется недоступен по нашей вине в течение длительного времени (более 7 дней подряд), и у Вас будет активная подписка, Вы можете написать нам в поддержку (контакт указан в биографии бота и в нашем Telegram-канале). Мы рассмотрим возможность продлить Вашу подписку на срок недоступности Сервиса. Решение принимается индивидуально.

**3. Ваши и наши права и обязанности**
3.1. Что ожидается от Вас (Ваши обязанности):
*   Использовать Сервис только в законных целях и не нарушать никакие законы при его использовании.
*   Не пытаться вмешаться в работу Сервиса или получить несанкционированный доступ.
*   Не использовать Сервис для рассылки спама, вредоносных программ или любой запрещенной информации.
*   Если требуется (например, для оплаты), предоставлять точную и правдивую информацию.
*   Поскольку у Сервиса нет возрастных ограничений, Вы подтверждаете свою способность принять условия настоящего Соглашения.
3.2. Что можем делать мы (Наши права):
*   Мы можем менять условия этого Соглашения. Если это произойдет, мы уведомим Вас, опубликовав новую версию Соглашения в нашем Telegram-канале или иным доступным способом в рамках Сервиса. Ваше дальнейшее использование Сервиса будет означать согласие с изменениями.
*   Мы можем временно приостановить или полностью прекратить Ваш доступ к Сервису, если Вы нарушите условия этого Соглашения.
*   Мы можем изменять сам Сервис: добавлять или убирать функции, менять лимиты или стоимость подписки.

**4. Важное предупреждение об ограничении ответственности**
4.1. Сервис предоставляется "как есть". Это значит, что мы не можем гарантировать его идеальную работу без сбоев или ошибок. Технологии иногда подводят, и мы не несем ответственности за возможные проблемы, возникшие не по нашей прямой вине.
4.2. Помните, Личности — это искусственный интеллект. Их ответы генерируются автоматически и могут быть неточными, неполными, странными или не соответствующими Вашим ожиданиям или реальности. Мы не несем никакой ответственности за содержание ответов, сгенерированных AI-собеседниками. Не воспринимайте их как истину в последней инстанции или профессиональный совет.
4.3. Мы не несем ответственности за любые прямые или косвенные убытки или ущерб, который Вы могли понести в результате использования (или невозможности использования) Сервиса.

**5. Про Ваши данные (Конфиденциальность)**
5.1. Для работы Сервиса нам приходится собирать и обрабатывать минимальные данные: Ваш Telegram ID (для идентификации аккаунта), имя пользователя Telegram (username, если есть), информацию о Вашей подписке, информацию о созданных Вами Личностях, а также историю Ваших сообщений с Личностями (это нужно AI для поддержания контекста разговора).
5.2. Мы предпринимаем разумные шаги для защиты Ваших данных, но, пожалуйста, помните, что передача информации через Интернет никогда не может быть абсолютно безопасной.

**6. Действие Соглашения**
6.1. Настоящее Соглашение начинает действовать с момента, как Вы впервые используете Сервис, и действует до момента, пока Вы не перестанете им пользоваться или пока Сервис не прекратит свою работу.

**7. Интеллектуальная Собственность**
7.1. Вы сохраняете все права на контент (текст), который Вы создаете и вводите в Сервис в процессе взаимодействия с AI-собеседниками.
7.2. Вы предоставляете нам неисключительную, безвозмездную, действующую по всему миру лицензию на использование Вашего контента исключительно в целях предоставления, поддержания и улучшения работы Сервиса (например, для обработки Ваших запросов, сохранения контекста диалога, анонимного анализа для улучшения моделей, если применимо).
7.3. Все права на сам Сервис (код бота, дизайн, название, графические элементы и т.д.) принадлежат владельцу Сервиса.
7.4. Ответы, сгенерированные AI-собеседниками, являются результатом работы алгоритмов искусственного интеллекта. Вы можете использовать полученные ответы в личных некоммерческих целях, но признаете, что они созданы машиной и не являются Вашей или нашей интеллектуальной собственностью в традиционном понимании.

**8. Заключительные положения**
8.1. Все споры и разногласия решаются путем переговоров. Если это не поможет, споры будут рассматриваться в соответствии с законодательством Российской Федерации.
8.2. По всем вопросам, касающимся настоящего Соглашения или работы Сервиса, Вы можете обращаться к нам через контакты, указанные в биографии бота и в нашем Telegram-канале.
"""

formatted_tos_text_for_bot = TOS_TEXT_RAW.format(
    subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
    subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
    subscription_currency=config.SUBSCRIPTION_CURRENCY
)
TOS_TEXT = escape_markdown_v2(formatted_tos_text_for_bot)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(context.error, Forbidden):
         if CHANNEL_ID and str(CHANNEL_ID) in str(context.error): # Convert CHANNEL_ID to str for comparison
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
                    await update.effective_message.reply_text("Произошла ошибка при форматировании ответа. Пожалуйста, сообщите администратору.", parse_mode=None)
                except Exception as send_err:
                    logger.error(f"Failed to send plain text formatting error message: {send_err}")
            return
        elif "chat member status is required" in error_text:
             logger.warning(f"Error handler caught BadRequest likely related to missing channel membership check: {context.error}")
             return
        elif "chat not found" in error_text:
             logger.error(f"BadRequest: Chat not found error: {context.error}")
             return
        else:
             logger.error(f"Unhandled BadRequest error: {context.error}")

    elif isinstance(context.error, TimedOut):
         logger.warning(f"Telegram API request timed out: {context.error}")
         return

    elif isinstance(context.error, TelegramError):
         logger.error(f"Generic Telegram API error: {context.error}")

    error_message_raw = "упс... что-то пошло не так. попробуй еще раз позже."
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


def get_persona_and_context_with_owner(chat_id: Union[str, int], db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
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
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return escape_markdown_v2("ошибка: ключ api не настроен\\.")
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
        "temperature": 0.75,
        "top_p": 0.95,
        "stream": False
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages. System prompt length: {len(system_prompt)}")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
             resp = await client.post(url, json=payload, headers=headers)
        logger.debug(f"Langdock response status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

        full_response = ""
        content = data.get("content")
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if part.get("type") == "text"]
            full_response = " ".join(text_parts)
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
             return escape_markdown_v2("ai вернул пустой ответ\\.")

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return escape_markdown_v2("хм, кажется, я слишком долго думал\\.\\.\\. попробуй еще раз?")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {error_body}", exc_info=False)
        error_text = f"ой, произошла ошибка при связи с ai \\({e.response.status_code}\\)\\.\\.\\."
        try:
             error_data = json.loads(error_body)
             if isinstance(error_data.get('error'), dict) and 'message' in error_data['error']:
                  api_error_msg = error_data['error']['message']
                  logger.error(f"Langdock API Error Message: {api_error_msg}")
             elif isinstance(error_data.get('error'), str):
                   logger.error(f"Langdock API Error Message: {error_data['error']}")
        except Exception: pass
        return escape_markdown_v2(error_text)
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return escape_markdown_v2("не могу связаться с ai сейчас \\(ошибка сети\\)\\.\\.\\.")
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return escape_markdown_v2("произошла внутренняя ошибка при генерации ответа\\.")


async def process_and_send_response(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, chat_id: Union[str, int], persona: Persona, full_bot_response_text: str, db: Session) -> bool:
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return False
    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}")

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
             escaped_ellipsis = escape_markdown_v2("...")
             last_part = text_parts_to_send[-1].rstrip('. ')
             if last_part and isinstance(last_part, str):
                text_parts_to_send[-1] = f"{last_part}{escaped_ellipsis}"
             else:
                text_parts_to_send[-1] = escaped_ellipsis


    send_tasks = []

    for gif in gif_links:
        try:
            send_tasks.append(context.bot.send_animation(chat_id=chat_id_str, animation=gif))
            logger.info(f"Scheduled sending gif: {gif}")
        except Exception as e:
            logger.error(f"Error scheduling gif send {gif} to chat {chat_id_str}: {e}", exc_info=True)

    if text_parts_to_send:
        chat_type = None
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_type = update.effective_chat.type

        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: continue

            if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 try:
                     asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING))
                     await asyncio.sleep(random.uniform(0.6, 1.2))
                 except Exception as e:
                      logger.warning(f"Failed to send typing action to {chat_id_str}: {e}")

            try:
                 escaped_part = escape_markdown_v2(part)
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id_str} (MDv2): '{escaped_part[:50]}...'")
                 send_tasks.append(context.bot.send_message(chat_id=chat_id_str, text=escaped_part, parse_mode=ParseMode.MARKDOWN_V2))
            except BadRequest as e:
                 logger.error(f"Error scheduling text part {i+1} send (BadRequest): {e} - Original: '{part[:100]}...' Escaped: '{escaped_part[:100]}...'")
                 try:
                      logger.info(f"Retrying part {i+1} as plain text after MarkdownV2 failed.")
                      send_tasks.append(context.bot.send_message(chat_id=chat_id_str, text=part, parse_mode=None))
                 except Exception as plain_e:
                      logger.error(f"Failed to schedule part {i+1} even as plain text: {plain_e}")
            except Exception as e:
                 logger.error(f"Error scheduling text part {i+1} send: {e}", exc_info=True)
                 break

    if send_tasks:
         results = await asyncio.gather(*send_tasks, return_exceptions=True)
         for i, result in enumerate(results):
              if isinstance(result, Exception):
                  logger.error(f"Failed to send message/animation part {i}: {result}")

    return context_prepared


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    count_str = escape_markdown_v2(f"{user.daily_message_count}/{user.message_limit}")
    price_str = escape_markdown_v2(f"{SUBSCRIPTION_PRICE_RUB:.0f}")
    currency_str = escape_markdown_v2(SUBSCRIPTION_CURRENCY)
    paid_limit_str = escape_markdown_v2(str(PAID_DAILY_MESSAGE_LIMIT))
    paid_persona_str = escape_markdown_v2(str(PAID_PERSONA_LIMIT))

    text_to_send = (
        escape_markdown_v2(f"упс\\! 😕 лимит сообщений \\({count_str}\\) на сегодня достигнут\\.\n\n") +
        f"✨ **{escape_markdown_v2('хочешь безлимита?')}** ✨\n" +
        escape_markdown_v2(f"подписка за {price_str} {currency_str}/мес дает:\n✅ ") +
        f"**{paid_limit_str}**" + escape_markdown_v2(" сообщений в день\n✅ до ") +
        f"**{paid_persona_str}**" + escape_markdown_v2(" личностей\n✅ полная настройка промптов и настроений\n\n") +
        escape_markdown_v2("👇 жми /subscribe или кнопку ниже\\!")
    )
    raw_text_for_log = f"Limit exceeded: {user.daily_message_count}/{user.message_limit}"

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
         logger.error(f"Failed sending limit message (BadRequest): {e} - Text Raw: '{raw_text_for_log}' Escaped: '{text_to_send[:100]}...'")
         try:
              if target_chat_id:
                  plain_text = re.sub(r'\\(.)', r'\1', text_to_send)
                  plain_text = plain_text.replace("**", "").replace("✨", "")
                  await context.bot.send_message(target_chat_id, plain_text, reply_markup=reply_markup, parse_mode=None)
         except Exception as final_e:
              logger.error(f"Failed sending limit message even plain: {final_e}")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not (update.message.text or update.message.caption):
        return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    if not message_text:
        return

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str}: {message_text[:100]}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                # Maybe send a generic reply if no persona is active in this chat?
                # logger.debug(f"No active persona found for chat {chat_id_str}. Ignoring message.")
                # await update.message.reply_text("В этом чате нет активной личности. Используйте /addbot <id>, чтобы добавить.", parse_mode=None)
                return # Or just return silently
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
                    user_prefix = username
                    context_content = f"{user_prefix}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_user_msg_added = True
                    logger.debug("User message prepared for context (pending commit).")
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"Error preparing user message for context: {e_ctx}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else:
                logger.error("Cannot add user message to context, chat_instance is None unexpectedly.")
                await update.message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать сообщение с личностью\\."), parse_mode=ParseMode.MARKDOWN_V2)
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
                          await update.message.reply_text(escape_markdown_v2("ошибка при смене настроения\\."), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            should_ai_respond = True
            ai_decision_response = None
            context_ai_decision_added = False
            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 should_respond_prompt = persona.format_should_respond_prompt(message_text)
                 if should_respond_prompt:
                     try:
                         logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id_str}...")
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
                              if random.random() < 0.05:
                                  logger.info(f"Responding randomly despite AI='{answer}'.")
                                  should_ai_respond = True
                              else:
                                  should_ai_respond = False
                         else:
                              logger.warning(f"Unclear should_respond answer '{answer}'. Defaulting to respond.")
                              should_ai_respond = True

                         if ai_decision_response and persona.chat_instance:
                             try:
                                 add_message_to_context(db, persona.chat_instance.id, "assistant", ai_decision_response.strip())
                                 context_ai_decision_added = True
                                 logger.debug("Added AI decision response to context (pending commit).")
                             except Exception as e_ctx_dec:
                                 logger.error(f"Failed to add AI decision to context: {e_ctx_dec}")

                     except Exception as e:
                          logger.error(f"Error in should_respond logic: {e}", exc_info=True)
                          should_ai_respond = True
                 else:
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic.")
                 db.commit()
                 return

            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                     logger.error(f"DB Error getting context for AI main response: {e_ctx}", exc_info=True)
                     await update.message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot get context for AI main response, chat_instance is None.")
                 db.rollback()
                 return

            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                logger.error(f"System prompt formatting failed for persona {persona.name}.")
                await update.message.reply_text(escape_markdown_v2("ошибка при подготовке ответа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received main response from Langdock: {response_text[:100]}...")

            context_response_prepared = await process_and_send_response(update, context, chat_id_str, persona, response_text, db)

            db.commit()
            logger.debug(f"Committed DB changes for handle_message chat {chat_id_str} (LimitUpdated: {limit_state_updated}, UserMsgAdded: {context_user_msg_added}, AIDecisionAdded: {context_ai_decision_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
             try: await update.message.reply_text(escape_markdown_v2("ошибка базы данных, попробуйте позже\\."), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception: pass
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id_str}: {e}", exc_info=True)
            try: await update.message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка\\."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
            db.rollback()


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
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

            prompt_template = None
            context_text_placeholder = ""
            system_formatter = None
            if media_type == "photo":
                prompt_template = persona.photo_prompt_template
                context_text_placeholder = "прислали фотографию."
                system_formatter = persona.format_photo_prompt
            elif media_type == "voice":
                prompt_template = persona.voice_prompt_template
                context_text_placeholder = "прислали голосовое сообщение."
                system_formatter = persona.format_voice_prompt
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
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать медиа с личностью\\."), parse_mode=ParseMode.MARKDOWN_V2)
                 db.rollback()
                 return

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Media saved to context, but ignoring response.")
                db.commit()
                return

            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id_str} has no {media_type} template. Skipping.")
                db.commit()
                return

            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа на медиа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else:
                 logger.error("Cannot get context for AI media response, chat_instance is None.")
                 db.rollback()
                 return

            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit()
                 return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            context_response_prepared = await process_and_send_response(update, context, chat_id_str, persona, response_text, db)

            db.commit()
            logger.debug(f"Committed DB changes for handle_media chat {chat_id_str} (LimitUpdated: {limit_state_updated}, PlaceholderAdded: {context_placeholder_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка базы данных\\."), parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка\\."), parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await handle_media(update, context, "photo")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await handle_media(update, context, "voice")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    escaped_reply_text = escape_markdown_v2("Произошла ошибка инициализации текста\\.")
    reply_markup = ReplyKeyboardRemove()

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            db.commit()
            db.refresh(user)

            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                persona_name_escaped = escape_markdown_v2(persona.name)
                escaped_reply_text = (
                    escape_markdown_v2(f"привет\\! я {persona_name_escaped}\\. я уже активен в этом чате\\.\n") +
                    escape_markdown_v2("используй /help для списка команд\\.")
                )
                reply_markup = ReplyKeyboardRemove()
            else:
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user.id).one()

                now = datetime.now(timezone.utc)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if not user.last_message_reset or user.last_message_reset < today_start:
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit()
                    db.refresh(user)

                status_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                escaped_status = escape_markdown_v2(status_raw)

                escaped_expires_date = ""
                if user.is_active_subscriber and user.subscription_expires_at:
                    expires_date_str = user.subscription_expires_at.strftime('%d.%m.%Y')
                    escaped_expires_date = escape_markdown_v2(f" до {expires_date_str}")

                persona_count = len(user.persona_configs)
                persona_limit_esc = escape_markdown_v2(f"{persona_count}/{user.persona_limit}")
                message_limit_esc = escape_markdown_v2(f"{user.daily_message_count}/{user.message_limit}")

                escaped_reply_text = (
                    escape_markdown_v2("привет\\! 👋 я бот для создания ai\\-собеседников \\(@NunuAiBot\\)\\.\n\n") +
                    f"твой статус: **{escaped_status}**{escaped_expires_date}\n" +
                    escape_markdown_v2(f"личности: {persona_limit_esc} | сообщения: {message_limit_esc}\n\n") +
                    f"**{escape_markdown_v2('начало работы:')}**\n" +
                    f"`/createpersona <имя>`{escape_markdown_v2(' - создай ai-личность.')}\n" +
                    f"`/mypersonas`{escape_markdown_v2(' - посмотри своих личностей и управляй ими.')}\n" +
                    f"`/profile`{escape_markdown_v2(' - детали статуса | ')}`/subscribe`{escape_markdown_v2(' - узнать о подписке')}"
                 )

                keyboard = [[InlineKeyboardButton("❓ Помощь (/help)", callback_data="show_help")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(escaped_reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg = "ошибка при загрузке данных\\. попробуй позже\\."
        await update.message.reply_text(escape_markdown_v2(error_msg), parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"BadRequest sending /start message for user {user_id}: {e}", exc_info=True)
        logger.error(f"Failed text (escaped): '{escaped_reply_text[:200]}...'")
        try:
            fallback_text = "Привет! Произошла ошибка отображения стартового сообщения. Используй /help для списка команд."
            await update.message.reply_text(fallback_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback start message: {fallback_e}")
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg = "произошла ошибка при обработке команды /start\\."
        await update.message.reply_text(escape_markdown_v2(error_msg), parse_mode=ParseMode.MARKDOWN_V2)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    help_text = (
        f"**{escape_markdown_v2('🤖 основные команды:')}**\n"
        f"`/start`{escape_markdown_v2(' - приветствие и твой статус')}\n"
        f"`/help`{escape_markdown_v2(' - эта справка')}\n"
        f"`/profile`{escape_markdown_v2(' - твой статус подписки и лимиты')}\n"
        f"`/subscribe`{escape_markdown_v2(' - инфо о подписке и оплата')}\n\n"
        f"**{escape_markdown_v2('👤 управление личностями:')}**\n"
        f"`/createpersona <имя> \\[описание\\]`{escape_markdown_v2(' - создать новую')}\n"
        f"`/mypersonas`{escape_markdown_v2(' - список твоих личностей и кнопки управления (редакт., удалить, добавить в чат)')}\n"
        f"`/editpersona <id>`{escape_markdown_v2(' - редактировать личность по ID (или через /mypersonas)')}\n"
        f"`/deletepersona <id>`{escape_markdown_v2(' - удалить личность по ID (или через /mypersonas)')}\n\n"
        f"**{escape_markdown_v2('💬 управление в чате (где есть личность):')}**\n"
        f"`/addbot <id>`{escape_markdown_v2(' - добавить личность в текущий чат (или через /mypersonas)')}\n"
        f"`/mood \\[настроение\\]`{escape_markdown_v2(' - сменить настроение активной личности')}\n"
        f"`/reset`{escape_markdown_v2(' - очистить память (контекст) личности в этом чате')}\n"
        f"`/mutebot`{escape_markdown_v2(' - заставить личность молчать в чате')}\n"
        f"`/unmutebot`{escape_markdown_v2(' - разрешить личности отвечать в чате')}"
    )

    try:
        if is_callback:
            query = update.callback_query
            if query.message.text != help_text or query.message.reply_markup:
                 await query.edit_message_text(help_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer()
        else:
            await update.message.reply_text(help_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Help message not modified, skipping edit.")
            await query.answer()
        else:
            logger.error(f"Failed sending/editing help message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed help text (escaped): '{help_text[:200]}...'")
            try:
                plain_help_text = re.sub(r'\\(.)', r'\1', help_text)
                plain_help_text = re.sub(r'\*\*(.*?)\*\*', r'\1', plain_help_text)
                plain_help_text = re.sub(r'`(.*?)`', r'\1', plain_help_text)
                if is_callback:
                    await query.edit_message_text(plain_help_text, reply_markup=None, parse_mode=None)
                else:
                    await update.message.reply_text(plain_help_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
            except Exception as fallback_e:
                logger.error(f"Failed sending plain help message: {fallback_e}")
                if is_callback: await query.answer("Ошибка отображения справки", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing help message: {e}", exc_info=True)
         if is_callback: await query.answer("Ошибка отображения справки", show_alert=True)


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
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

    error_no_persona = escape_markdown_v2("в этом чате нет активной личности\\.")
    error_persona_info = escape_markdown_v2("Ошибка: не найдена информация о личности\\.")
    # Use raw format strings
    error_no_moods_fmt_raw = "у личности '{persona_name}' не настроены настроения\\."
    error_bot_muted_fmt_raw = "личность '{persona_name}' сейчас заглушена \\(/unmutebot\\)\\."
    error_db = escape_markdown_v2("ошибка базы данных при смене настроения\\.")
    error_general = escape_markdown_v2("ошибка при обработке команды /mood\\.")

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
             if is_callback: await update.callback_query.answer("Ошибка: не найдена информация о личности.", show_alert=True)
             else: await reply_target.reply_text(error_persona_info, parse_mode=ParseMode.MARKDOWN_V2)
             if close_db_later: db_session.close()
             return

        chat_bot_instance = local_persona.chat_instance
        persona_name_escaped = escape_markdown_v2(local_persona.name)

        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{local_persona.name}' is muted in chat {chat_id_str}. Ignoring mood command.")
            # Format correctly
            reply_text = escape_markdown_v2(error_bot_muted_fmt_raw.format(persona_name=local_persona.name))
            try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             # Format correctly
             reply_text = escape_markdown_v2(error_no_moods_fmt_raw.format(persona_name=local_persona.name))
             try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Нет настроений", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
             logger.warning(f"Persona {local_persona.name} has no moods defined.")
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
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case) # Commits inside
             mood_name_escaped = escape_markdown_v2(target_mood_original_case)
             reply_text = f"настроение для '{persona_name_escaped}' теперь: **{mood_name_escaped}**"
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
                       plain_text = f"Настроение для '{local_persona.name}' теперь: {target_mood_original_case}"
                       if is_callback: await query.edit_message_text(plain_text, reply_markup=None, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(plain_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood confirmation: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {local_persona.name} in chat {chat_id_str} set to {target_mood_original_case}.")
        else:
             keyboard = []
             for mood_name in sorted(available_moods, key=str.lower):
                 try:
                     encoded_mood_name = urllib.parse.quote(mood_name)
                     button_callback = f"set_mood_{encoded_mood_name}_{local_persona.id}"
                     if len(button_callback.encode('utf-8')) <= 64:
                          keyboard.append([InlineKeyboardButton(mood_name.capitalize(), callback_data=button_callback)])
                     else:
                          logger.warning(f"Callback data for mood '{mood_name}' (encoded: '{encoded_mood_name}') too long, skipping button.")
                 except Exception as encode_err:
                     logger.error(f"Error encoding mood name '{mood_name}' for callback: {encode_err}")

             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             current_mood_escaped = escape_markdown_v2(current_mood_text)

             reply_text = ""
             if mood_arg_lower:
                 mood_arg_escaped = escape_markdown_v2(mood_arg_lower)
                 reply_text = escape_markdown_v2(f"не знаю настроения '{mood_arg_escaped}' для '{persona_name_escaped}'\\. выбери из списка:")
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
             else:
                 reply_text = f"текущее настроение: **{current_mood_escaped}**\\. выбери новое для '{persona_name_escaped}':"
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
                       plain_text = re.sub(r'\\(.)', r'\1', reply_text).replace('**','')
                       if is_callback: await query.edit_message_text(plain_text, reply_markup=reply_markup, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(plain_text, reply_markup=reply_markup, parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood selection: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id_str}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("Ошибка БД", show_alert=True)
             await reply_target.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id_str}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("Ошибка", show_alert=True)
             await reply_target.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         except Exception as send_err: logger.error(f"Error sending general error msg: {send_err}")
    finally:
        if close_db_later and db_session:
            try: db_session.close()
            except Exception: pass


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset < User {user_id} ({username}) in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности для сброса\\.")
    error_not_owner = escape_markdown_v2("только владелец личности может сбросить её память\\.")
    error_no_instance = escape_markdown_v2("ошибка: не найден экземпляр бота для сброса\\.")
    error_db = escape_markdown_v2("ошибка базы данных при сбросе контекста\\.")
    error_general = escape_markdown_v2("ошибка при сбросе контекста\\.")
    success_reset_fmt_raw = "память личности '{persona_name}' в этом чате очищена\\."

    with next(get_db()) as db:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_info_tuple:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return
            persona, _, owner_user = persona_info_tuple
            persona_name_escaped = escape_markdown_v2(persona.name) # Escape name for use in formatted string

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
            # Format correctly
            final_success_msg = escape_markdown_v2(success_reset_fmt_raw.format(persona_name=persona.name)) # Use unescaped name here
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
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    usage_text = escape_markdown_v2("формат: `/createpersona <имя> \\[описание]`\n_имя обязательно, описание нет\\._")
    error_name_len = escape_markdown_v2("имя личности: 2\\-50 символов\\.")
    error_desc_len = escape_markdown_v2("описание: до 1500 символов\\.")
    # Raw format strings
    error_limit_reached_fmt_raw = "упс\\! достигнут лимит личностей \\({current_count}/{limit}\\) для статуса **{status_text}**\\. 😟\nчтобы создавать больше, используй /subscribe"
    error_name_exists_fmt_raw = "личность с именем '{persona_name}' уже есть\\. выбери другое\\."
    success_create_fmt_raw = "✅ личность '{name}' создана\\!\nid: `{id}`\nописание: {description}\n\nдобавь в чат или управляй через /mypersonas"
    error_db = escape_markdown_v2("ошибка базы данных при создании личности\\.")
    error_general = escape_markdown_v2("ошибка при создании личности\\.")

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
                 # Format correctly
                 final_limit_msg = escape_markdown_v2(error_limit_reached_fmt_raw.format(
                     current_count=current_count, # No need to escape numbers/status here
                     limit=limit,
                     status_text=status_text_raw
                 ))
                 await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                 # Format correctly
                 final_exists_msg = escape_markdown_v2(error_name_exists_fmt_raw.format(persona_name=persona_name))
                 await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            new_persona = create_persona_config(db, user.id, persona_name, persona_description) # Commits inside

            name_escaped = escape_markdown_v2(new_persona.name)
            desc_display_escaped = escape_markdown_v2(new_persona.description) if new_persona.description else escape_markdown_v2("\\(пусто\\)")
            # Format correctly
            final_success_msg = escape_markdown_v2(success_create_fmt_raw.format(
                name=new_persona.name, # Use unescaped name inside quotes
                id=new_persona.id, # ID in backticks doesn't need escaping
                description=new_persona.description or "(пусто)" # Use unescaped description
                ))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError:
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             persona_name_escaped = escape_markdown_v2(persona_name)
             error_msg_ie = escape_markdown_v2(f"ошибка: личность '{persona_name_escaped}' уже существует \\(возможно, гонка запросов\\)\\. попробуй еще раз\\.")
             await update.message.reply_text(error_msg_ie, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
             logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
             try: await update.message.reply_text("Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback create_persona error: {fe}")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("ошибка при загрузке списка личностей\\.")
    error_general = escape_markdown_v2("произошла ошибка при обработке команды /mypersonas\\.")
    error_user_not_found = escape_markdown_v2("Ошибка: не удалось найти пользователя\\.")
    info_no_personas_fmt_raw = "у тебя пока нет личностей \\({count}/{limit}\\)\\.\nсоздай: `/createpersona <имя>`"
    info_list_header_fmt_raw = "твои личности \\({count}/{limit}\\):\n"

    try:
        with next(get_db()) as db:
            user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()

            if not user_with_personas:
                 user_with_personas = get_or_create_user(db, user_id, username)
                 db.commit()
                 db.refresh(user_with_personas)
                 user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_with_personas.id).one() # Use internal ID
                 if not user_with_personas:
                     logger.error(f"User {user_id} not found even after get_or_create/refresh in my_personas.")
                     await update.message.reply_text(error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                     return

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            count_esc = escape_markdown_v2(str(persona_count))
            limit_esc = escape_markdown_v2(str(persona_limit))

            if not personas:
                # Format correctly
                text_to_send = escape_markdown_v2(info_no_personas_fmt_raw.format(count=persona_count, limit=persona_limit))
                await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Format correctly
            text = escape_markdown_v2(info_list_header_fmt_raw.format(count=persona_count, limit=persona_limit))

            keyboard = []
            for p in personas:
                 button_text = f"👤 {p.name} (ID: {p.id})"
                 edit_cb = f"edit_persona_{p.id}"
                 delete_cb = f"delete_persona_{p.id}"
                 add_cb = f"add_bot_{p.id}"
                 if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64 or len(add_cb.encode('utf-8')) > 64:
                      logger.warning(f"Callback data for persona {p.id} might be too long, potentially causing issues.")
                 keyboard.append([InlineKeyboardButton(button_text, callback_data=f"dummy_{p.id}")])
                 keyboard.append([
                     InlineKeyboardButton("⚙️ Редакт.", callback_data=edit_cb),
                     InlineKeyboardButton("🗑️ Удалить", callback_data=delete_cb),
                     InlineKeyboardButton("➕ В чат", callback_data=add_cb)
                 ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(message_or_callback_msg.chat.id)
    chat_title = escape_markdown_v2(message_or_callback_msg.chat.title or f"Chat {chat_id_str}")
    local_persona_id = persona_id

    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    usage_text = escape_markdown_v2("формат: `/addbot <id персоны>`\nили используй кнопку '➕ В чат' из /mypersonas")
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности\\.")
    error_invalid_id_cmd = escape_markdown_v2("id личности должен быть числом\\.")
    error_no_id = escape_markdown_v2("Ошибка: ID личности не определен\\.")
    # Raw format strings
    error_persona_not_found_fmt_raw = "личность с id `{id}` не найдена или не твоя\\."
    error_already_active_fmt_raw = "личность '{name}' уже активна в этом чате\\."
    success_added_structure_raw = "✅ личность '{name}' \\(id: `{id}`\\) активирована в этом чате\\! Память очищена\\."
    error_link_failed = escape_markdown_v2("не удалось активировать личность \\(ошибка связывания\\)\\.")
    error_integrity = escape_markdown_v2("произошла ошибка целостности данных \\(возможно, конфликт активации\\), попробуйте еще раз\\.")
    error_db = escape_markdown_v2("ошибка базы данных при добавлении бота\\.")
    error_general = escape_markdown_v2("ошибка при активации личности\\.")

    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("Ошибка: неверный ID", show_alert=True)
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
         if is_callback: await update.callback_query.answer("Ошибка: ID не определен.", show_alert=True)
         else: await reply_target.reply_text(error_no_id, parse_mode=ParseMode.MARKDOWN_V2)
         return

    if is_callback:
        await update.callback_query.answer("Добавляем личность...")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 # Format correctly
                 final_not_found_msg = escape_markdown_v2(error_persona_not_found_fmt_raw.format(id=local_persona_id))
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
                    # Format correctly
                    final_already_active_msg = escape_markdown_v2(error_already_active_fmt_raw.format(name=persona.name))
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
                    prev_persona_name = existing_active_link.bot_instance_ref.persona_config.name if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config else f"ID {existing_active_link.bot_instance_id}"
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
                 except (IntegrityError, SQLAlchemyError):
                      logger.error("Failed to create BotInstance, possibly due to concurrent request. Retrying fetch.")
                      db.rollback()
                      bot_instance = db.query(BotInstance).filter(BotInstance.persona_config_id == local_persona_id).first()
                      if not bot_instance:
                           logger.error("Failed to fetch BotInstance even after retry.")
                           raise SQLAlchemyError("Failed to create or fetch BotInstance")

            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)

            if chat_link:
                 # Format correctly
                 final_success_msg = escape_markdown_v2(success_added_structure_raw.format(name=persona.name, id=local_persona_id))
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
             try: await context.bot.send_message(chat_id=chat_id_str, text="Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback add_bot_to_chat error: {fe}")
        except Exception as e:
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_general, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data: return

    chat_id_str = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

    needs_subscription_check = True
    no_check_callbacks = (
        "cancel_edit", "edit_persona_back", "edit_moods_back_cancel",
        "delete_persona_cancel", "view_tos", "subscribe_info",
        "show_help", "dummy_", "confirm_pay", "subscribe_pay"
    )
    conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood", "set_mood_")

    if data.startswith(no_check_callbacks) or any(data.startswith(p) for p in conv_prefixes):
        needs_subscription_check = False

    if needs_subscription_check:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            try: await query.answer(text="Подпишитесь на канал!", show_alert=True)
            except: pass
            return

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
    elif data.startswith("dummy_"):
        await query.answer()
    else:
        known_conv_prefixes_full = ("edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood_", "cancel_edit", "edit_persona_back", "edit_moods_back_cancel", "deletemood_confirm_", "deletemood_delete_")
        if any(data.startswith(p) for p in known_conv_prefixes_full):
             logger.debug(f"Callback '{data}' appears to be for a ConversationHandler, skipping direct handling.")
        else:
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            try:
                 await query.answer("Неизвестное действие")
            except Exception as e:
                 logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("ошибка базы данных при загрузке профиля\\.")
    error_general = escape_markdown_v2("ошибка при обработке команды /profile\\.")
    error_user_not_found = escape_markdown_v2("ошибка: пользователь не найден\\.")

    with next(get_db()) as db:
        try:
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user:
                user = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user)
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user.id).one()
                if not user:
                    logger.error(f"User {user_id} not found after get_or_create/refresh in profile.")
                    await update.message.reply_text(error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                    return

            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not user.last_message_reset or user.last_message_reset < today_start:
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit()
                db.refresh(user)

            is_active_subscriber = user.is_active_subscriber
            status_text = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            status = escape_markdown_v2(status_text)

            expires_text = escape_markdown_v2("нет активной подписки")
            if is_active_subscriber and user.subscription_expires_at:
                 try:
                     expires_date_str = user.subscription_expires_at.strftime('%d.%m.%Y %H:%M') + " UTC"
                     expires_text = escape_markdown_v2(f"активна до: {expires_date_str}")
                 except AttributeError:
                      expires_text = escape_markdown_v2("активна \\(дата истечения некорректна\\)")

            persona_count = len(user.persona_configs) if user.persona_configs is not None else 0
            persona_limit = user.persona_limit
            msg_count = user.daily_message_count
            msg_limit = user.message_limit

            msg_count_esc = escape_markdown_v2(str(msg_count))
            msg_limit_esc = escape_markdown_v2(str(msg_limit))
            persona_count_esc = escape_markdown_v2(str(persona_count))
            persona_limit_esc = escape_markdown_v2(str(persona_limit))

            text = (
                f"👤 **{escape_markdown_v2('твой профиль')}**\n\n"
                f"{escape_markdown_v2('статус:')} **{status}**\n"
                f"{expires_text}\n\n"
                f"**{escape_markdown_v2('лимиты:')}**\n"
                f"{escape_markdown_v2('сообщения сегодня:')} {msg_count_esc}/{msg_limit_esc}\n"
                f"{escape_markdown_v2('создано личностей:')} {persona_count_esc}/{persona_limit_esc}\n\n"
            )
            if not is_active_subscriber:
                text += escape_markdown_v2("🚀 хочешь больше? жми /subscribe \\!")

            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
             logger.error(f"Database error during /profile for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update_or_reply = update.callback_query.message if from_callback else update.message
    if not message_to_update_or_reply: return

    if not from_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна\\. 😥 \\(проблема с настройками\\)")
    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = error_payment_unavailable
        reply_markup = None
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
    else:
        price_str = escape_markdown_v2(f"{SUBSCRIPTION_PRICE_RUB:.0f}")
        currency_str = escape_markdown_v2(SUBSCRIPTION_CURRENCY)
        duration_str = escape_markdown_v2(str(SUBSCRIPTION_DURATION_DAYS))
        paid_limit_esc = escape_markdown_v2(str(PAID_DAILY_MESSAGE_LIMIT))
        free_limit_esc = escape_markdown_v2(str(FREE_DAILY_MESSAGE_LIMIT))
        paid_persona_esc = escape_markdown_v2(str(PAID_PERSONA_LIMIT))
        free_persona_esc = escape_markdown_v2(str(FREE_PERSONA_LIMIT))

        header = f"✨ **{escape_markdown_v2(f'премиум подписка ({price_str} {currency_str}/мес)')}** ✨\n\n"
        body = (
            escape_markdown_v2("получи максимум возможностей:\n✅ ") +
            f"**{paid_limit_esc}**" + escape_markdown_v2(f" сообщений в день \\(вместо {free_limit_esc}\\)\n✅ ") +
            f"**{paid_persona_esc}**" + escape_markdown_v2(f" личностей \\(вместо {free_persona_esc}\\)\n✅ полная настройка всех промптов\n✅ создание и редакт\\. своих настроений\n✅ приоритетная поддержка\n\nподписка действует {duration_str} дней\\.")
        )
        text = header + body

        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            [InlineKeyboardButton("✅ Принять и оплатить", callback_data="confirm_pay")]
        ]
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
        logger.error(f"Failed sending subscribe message (BadRequest): {e} - Text Escaped: '{text[:100]}...'")
        try:
            if message_to_update_or_reply:
                 plain_text = re.sub(r'\\(.)', r'\1', text)
                 plain_text = plain_text.replace("**", "").replace("✨","")
                 await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=plain_text, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback subscribe message: {fallback_e}")
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        if from_callback and isinstance(e, (BadRequest, TelegramError)):
            try:
                await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")


async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    tos_url = context.bot_data.get('tos_url')
    error_tos_link = "Не удалось отобразить ссылку на соглашение."
    error_tos_load = escape_markdown_v2("❌ Не удалось загрузить ссылку на Пользовательское Соглашение\\. Попробуйте позже\\.")
    info_tos = escape_markdown_v2("Ознакомьтесь с Пользовательским Соглашением, открыв его по ссылке ниже:")

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
             await query.answer("Ошибка загрузки соглашения.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна\\. 😥 \\(проблема с настройками\\)")
    info_confirm = escape_markdown_v2(
         "✅ Отлично\\!\n\n"
         "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "Пользовательским Соглашением\\."
         "\n\n👇"
    )
    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = error_payment_unavailable
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]])
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        text = info_confirm
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        currency_raw = SUBSCRIPTION_CURRENCY
        button_text = f"💳 Оплатить {price_raw} {currency_raw}"

        keyboard = [
            [InlineKeyboardButton(button_text, callback_data="subscribe_pay")]
        ]
        if tos_url:
             keyboard.append([InlineKeyboardButton("📜 Условия использования (прочитано)", url=tos_url)])
        else:
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
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    error_yk_not_ready = escape_markdown_v2("❌ ошибка: сервис оплаты не настроен правильно\\.")
    error_yk_config = escape_markdown_v2("❌ ошибка конфигурации платежной системы\\.")
    error_receipt = escape_markdown_v2("❌ ошибка при формировании данных чека\\.")
    error_link_get_fmt_raw = "❌ не удалось получить ссылку от платежной системы{status_info}\\.\nПопробуй позже\\."
    error_link_create_raw = "❌ не удалось создать ссылку для оплаты\\. {error_detail}\nПопробуй еще раз позже или свяжись с поддержкой\\."
    success_link = escape_markdown_v2(
        "✅ ссылка для оплаты создана\\!\n\n"
        "нажми кнопку ниже для перехода к оплате\\. "
        "после успеха подписка активируется \\(может занять пару минут\\)\\."
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
        Configuration.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
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
             status_info = f" \\(статус: {payment_response.status}\\)" if payment_response and payment_response.status else ""
             # Format correctly
             error_message = escape_markdown_v2(error_link_get_fmt_raw.format(status_info=status_info))
             text = error_message
             reply_markup = None
             await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = success_link
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        error_detail = ""
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                if "Invalid credentials" in err_text:
                    error_detail = "Ошибка аутентификации с ЮKassa."
                elif "receipt" in err_text.lower():
                     error_detail = "Ошибка данных чека (детали в логах)."
                else:
                    error_detail = "Ошибка от ЮKassa (детали в логах)."
            except Exception as parse_e:
                logger.error(f"Could not parse YK error response: {parse_e}")
                error_detail = "Ошибка от ЮKassa (не удалось разобрать ответ)."
        elif isinstance(e, httpx.RequestError):
             error_detail = "Проблема с сетевым подключением к ЮKassa."
        else:
             error_detail = "Произошла непредвиденная ошибка."

        # Format correctly
        user_message = escape_markdown_v2(error_link_create_raw.format(error_detail=error_detail))
        try:
            await query.edit_message_text(user_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass

async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
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

    # Raw format strings
    error_not_found_fmt_raw = "личность с id `{id}` не найдена или не твоя\\."
    prompt_edit_fmt_raw = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"
    error_db = escape_markdown_v2("ошибка базы данных при начале редактирования\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 # Format correctly
                 final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config)
            reply_markup = InlineKeyboardMarkup(keyboard)
            persona_name_escaped = escape_markdown_v2(persona_config.name)
            # Format correctly
            msg_text = escape_markdown_v2(prompt_edit_fmt_raw.format(name=persona_config.name, id=persona_id))

            reply_target = update.callback_query.message if is_callback else update.effective_message
            if is_callback:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer()
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
        return EDIT_PERSONA_CHOICE
    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")

    usage_text = escape_markdown_v2("укажи id личности: `/editpersona <id>`\nили используй кнопку из /mypersonas")
    error_invalid_id = escape_markdown_v2("ID должен быть числом\\.")

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
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...")

    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности в кнопке\\.")

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

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, PersonaID={persona_id}, Callback data={data} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет id\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("ошибка базы данных при проверке данных\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка\\.")
    info_premium_mood = "⭐ Редактирование настроений доступно по подписке"
    info_premium_field_fmt_raw = "⭐ Поле '{field_name}' доступно по подписке"
    # Raw format strings
    prompt_edit_value_fmt_raw = "отправь новое значение для **{field_name}**\\.\n_текущее:_\n`{current_value}`"
    prompt_edit_max_msg_fmt_raw = "отправь новое значение для **{field_name}** \\(число от 1 до 10\\):\n_текущее: {current_value}_"

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

    persona_config = None
    is_premium_user = False
    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} not found or not owned.")
                await query.answer("Личность не найдена", show_alert=True)
                await query.edit_message_text(error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END
            owner = persona_config.owner
            if owner:
                 is_premium_user = owner.is_active_subscriber or is_admin(user_id)
            else:
                 logger.warning(f"Owner not loaded for persona {persona_id} in edit_persona_choice check. Assuming non-premium.")
                 is_premium_user = False

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return EDIT_PERSONA_CHOICE
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.answer("Непредвиденная ошибка", show_alert=True)
         await query.edit_message_text(error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return EDIT_PERSONA_CHOICE

    if data == "cancel_edit":
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        if not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer(info_premium_mood, show_alert=True)
             return EDIT_PERSONA_CHOICE
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             await query.answer()
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        field_display_name_escaped = FIELD_MAP.get(field, escape_markdown_v2(field))
        field_display_name_plain = re.sub(r'\\(.)', r'\1', field_display_name_escaped)

        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        advanced_fields = ["system_prompt_template", "should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             # Format correctly
             await query.answer(info_premium_field_fmt_raw.format(field_name=field_display_name_plain), show_alert=True)
             return EDIT_PERSONA_CHOICE

        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        await query.answer()

        if field == "max_response_messages":
            current_value = getattr(persona_config, field, 3)
            # Format correctly
            final_prompt = escape_markdown_v2(prompt_edit_max_msg_fmt_raw.format(
                field_name=field_display_name_plain, # Use plain name inside raw string
                current_value=current_value
                ))
            await query.edit_message_text(final_prompt, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_MAX_MESSAGES
        else:
            current_value_raw = getattr(persona_config, field, "")
            current_value_display = escape_markdown_v2(str(current_value_raw) if len(str(current_value_raw)) < 300 else str(current_value_raw)[:300] + "...")
            # Format correctly
            final_prompt = escape_markdown_v2(prompt_edit_value_fmt_raw.format(
                field_name=field_display_name_plain, # Use plain name inside raw string
                current_value=current_value_raw[:300] + "..." if len(current_value_raw) > 300 else current_value_raw # Use raw value for code block
                ))
            await query.edit_message_text(final_prompt, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_FIELD

    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         await query.answer()
         keyboard = await _get_edit_persona_keyboard(persona_config)
         # Raw format string
         prompt_edit_back_raw = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"
         name_esc = escape_markdown_v2(persona_config.name)
         # Format correctly
         final_back_msg = escape_markdown_v2(prompt_edit_back_raw.format(name=persona_config.name, id=persona_id))
         await query.edit_message_text(final_back_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.pop('edit_field', None)
         return EDIT_PERSONA_CHOICE

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.answer("Неизвестный выбор", show_alert=True)
    return EDIT_PERSONA_CHOICE

async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_FIELD
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    # Raw format strings
    error_validation_fmt_raw = "{field_name}: макс\\. {max_len} символов\\."
    error_validation_min_fmt_raw = "{field_name}: мин\\. {min_len} символа\\."
    error_name_taken_fmt_raw = "имя '{name}' уже занято другой твоей личностью\\. попробуй другое:"
    success_update_fmt_raw = "✅ поле **{field_name}** для личности **{persona_name}** обновлено\\!"
    prompt_next_edit_fmt_raw = "что еще изменить для **{name}** \\(id: `{id}`\\)?"
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении\\. попробуй еще раз\\.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении\\.")


    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    field_display_name_escaped = FIELD_MAP.get(field, escape_markdown_v2(field))
    field_display_name_plain = re.sub(r'\\(.)', r'\1', field_display_name_escaped) # For format strings

    validation_error_msg = None
    max_len_map = {
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len_map = {"name": 2}

    if field in max_len_map and len(new_value) > max_len_map[field]:
        max_len = max_len_map[field]
        # Format correctly
        validation_error_msg = escape_markdown_v2(error_validation_fmt_raw.format(field_name=field_display_name_plain, max_len=max_len))
    if field in min_len_map and len(new_value) < min_len_map[field]:
        min_len = min_len_map[field]
        # Format correctly
        validation_error_msg = escape_markdown_v2(error_validation_min_fmt_raw.format(field_name=field_display_name_plain, min_len=min_len))

    if validation_error_msg:
        logger.debug(f"Validation failed for field '{field}': {validation_error_msg}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error_msg} {escape_markdown_v2('попробуй еще раз:')}", reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_FIELD

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            if field == "name" and new_value.lower() != persona_config.name.lower():
                existing = db.query(PersonaConfig.id).filter(
                    PersonaConfig.owner_id == persona_config.owner_id,
                    func.lower(PersonaConfig.name) == new_value.lower()
                ).first()
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by their persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    # Format correctly
                    final_name_taken_msg = escape_markdown_v2(error_name_taken_fmt_raw.format(name=new_value))
                    await update.message.reply_text(final_name_taken_msg, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                    return EDIT_FIELD

            setattr(persona_config, field, new_value)
            db.commit()
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            name_esc = escape_markdown_v2(persona_config.name)
            # Format correctly
            final_success_msg = escape_markdown_v2(success_update_fmt_raw.format(
                field_name=field_display_name_plain, # Use plain name inside raw string
                persona_name=persona_config.name # Use plain name inside raw string
            ))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            context.user_data.pop('edit_field', None)
            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            # Format correctly
            final_next_prompt = escape_markdown_v2(prompt_next_edit_fmt_raw.format(name=persona_config.name, id=persona_id))
            await update.message.reply_text(final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear()
         return ConversationHandler.END

async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages"
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет persona_id\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_invalid_value = escape_markdown_v2("неверное значение\\. введи число от 1 до 10:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении\\. попробуй еще раз\\.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении\\.")
    # Raw format strings
    success_update_fmt_raw = "✅ макс\\. сообщений в ответе для **{name}** установлено: **{value}**"
    prompt_next_edit_fmt_raw = "что еще изменить для **{name}** \\(id: `{id}`\\)?"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10): raise ValueError("Value out of range 1-10")
    except ValueError:
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(error_invalid_value, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MAX_MESSAGES

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            persona_config.max_response_messages = new_value
            db.commit()
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            name_esc = escape_markdown_v2(persona_config.name)
            value_esc = escape_markdown_v2(str(new_value))
            # Format correctly
            final_success_msg = escape_markdown_v2(success_update_fmt_raw.format(name=persona_config.name, value=new_value))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            # Format correctly
            final_next_prompt = escape_markdown_v2(prompt_next_edit_fmt_raw.format(name=persona_config.name, id=persona_id))
            await update.message.reply_text(final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear()
         return ConversationHandler.END

async def _get_edit_persona_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]

    is_premium = False
    owner = persona_config.owner
    if owner:
        is_premium = owner.is_active_subscriber or is_admin(owner.telegram_id)
    else:
        logger.warning(f"Owner not loaded for persona {persona_config.id} in _get_edit_persona_keyboard")

    star = " ⭐" if is_premium else ""
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)

    keyboard = [
        [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
        [InlineKeyboardButton(f"⚙️ Системный промпт{star}", callback_data="edit_field_system_prompt_template")],
        [InlineKeyboardButton(f"📊 Макс. ответов ({max_resp_msg}){star}", callback_data="edit_field_max_response_messages")],
        [InlineKeyboardButton(f"🤔 Промпт 'Отвечать?'{star}", callback_data="edit_field_should_respond_prompt_template")],
        [InlineKeyboardButton(f"💬 Промпт спама{star}", callback_data="edit_field_spam_prompt_template")],
        [InlineKeyboardButton(f"🖼️ Промпт фото{star}", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton(f"🎤 Промпт голоса{star}", callback_data="edit_field_voice_prompt_template")],
        [InlineKeyboardButton(f"🎭 Настроения{star}", callback_data="edit_moods")],
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
    ]
    return keyboard

async def _get_edit_moods_keyboard_internal(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
     if not persona_config: return []
     try:
         moods = json.loads(persona_config.mood_prompts_json or '{}')
     except json.JSONDecodeError:
         logger.warning(f"Invalid JSON in mood_prompts_json for persona {persona_config.id} when building keyboard.")
         moods = {}

     keyboard = []
     if moods:
         sorted_moods = sorted(moods.keys(), key=str.lower)
         for mood_name in sorted_moods:
              try:
                  display_name = mood_name.capitalize()
                  encoded_mood_name = urllib.parse.quote(mood_name)
                  edit_cb = f"editmood_select_{encoded_mood_name}"
                  delete_cb = f"deletemood_confirm_{encoded_mood_name}"

                  if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64:
                       logger.warning(f"Encoded mood name '{encoded_mood_name}' too long for callback data, skipping buttons.")
                       continue

                  keyboard.append([
                      InlineKeyboardButton(f"✏️ {display_name}", callback_data=edit_cb),
                      InlineKeyboardButton(f"🗑️", callback_data=delete_cb)
                  ])
              except Exception as encode_err:
                  logger.error(f"Error processing mood '{mood_name}' for keyboard: {encode_err}")

     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])
     return keyboard

async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message_target = update.effective_message

    error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню редактирования \\(личность не найдена\\)\\.")
    error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню редактирования\\.")
    prompt_edit_raw = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"

    if not message_target:
        logger.warning("Cannot return to edit menu: effective_message is None.")
        context.user_data.clear()
        return ConversationHandler.END
    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if persona_config:
                keyboard = await _get_edit_persona_keyboard(persona_config)
                # Format correctly
                final_prompt = escape_markdown_v2(prompt_edit_raw.format(name=persona_config.name, id=persona_id))
                await message_target.reply_text(final_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_PERSONA_CHOICE
            else:
                logger.warning(f"Persona {persona_id} not found when trying to return to main edit menu.")
                await message_target.reply_text(error_cannot_return, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to return to main edit menu after error: {e}", exc_info=True)
        await message_target.reply_text(error_cannot_return_general, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.clear()
        return ConversationHandler.END

async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     callback_message = update.callback_query.message if update.callback_query else None
     user_message = update.message

     error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню настроений \\(личность не найдена\\)\\.")
     error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню настроений\\.")
     prompt_mood_menu_raw = "управление настроениями для **{name}**:"

     target_chat_id = None
     if callback_message:
         target_chat_id = callback_message.chat.id
     elif user_message:
         target_chat_id = user_message.chat.id

     if not target_chat_id:
         logger.warning("Cannot return to mood menu: no target chat_id found.")
         context.user_data.clear()
         return ConversationHandler.END

     try:
         with next(get_db()) as db:
             persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()

             if persona_config:
                 keyboard = await _get_edit_moods_keyboard_internal(persona_config)
                 # Format correctly
                 final_prompt = escape_markdown_v2(prompt_mood_menu_raw.format(name=persona_config.name))

                 await context.bot.send_message(
                     chat_id=target_chat_id,
                     text=final_prompt,
                     reply_markup=InlineKeyboardMarkup(keyboard),
                     parse_mode=ParseMode.MARKDOWN_V2
                 )
                 if callback_message and callback_message.from_user.is_bot:
                     try: await callback_message.delete()
                     except Exception as del_e: logger.warning(f"Could not delete previous bot message: {del_e}")

                 return EDIT_MOOD_CHOICE
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

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при загрузке настроений\\.")
    info_premium = "⭐ Доступно по подписке"
    prompt_mood_menu_fmt_raw = "управление настроениями для **{name}**:"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    local_persona_config = persona_config
    is_premium = False

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
                    await query.edit_message_text(error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    context.user_data.clear()
                    return ConversationHandler.END
                owner = local_persona_config.owner
                if owner:
                     is_premium = owner.is_active_subscriber or is_admin(user_id)
                else:
                     logger.warning(f"Owner not loaded for persona {persona_id} in edit_moods_menu fetch.")
                     is_premium = False

        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("Ошибка базы данных", show_alert=True)
             await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
             return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    else:
         owner = local_persona_config.owner
         if owner:
             is_premium = owner.is_active_subscriber or is_admin(user_id)
         else:
              logger.warning(f"Owner not loaded for persona {persona_id} in edit_moods_menu. Fetching...")
              with next(get_db()) as db:
                  owner_db = db.query(User).filter(User.id == local_persona_config.owner_id).first()
                  if owner_db: is_premium = owner_db.is_active_subscriber or is_admin(user_id)
                  else: is_premium = False

    if not is_premium:
        logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
        await query.answer(info_premium, show_alert=True)
        return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Format correctly
    msg_text = escape_markdown_v2(prompt_mood_menu_fmt_raw.format(name=local_persona_config.name))

    try:
        if query.message.text != msg_text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await query.answer()
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         try:
            await context.bot.send_message(chat_id=query.message.chat.id, text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            if query.message.from_user.is_bot:
                 try: await query.message.delete()
                 except: pass
         except Exception as send_e: logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE

async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("Ошибка базы данных\\.")
    error_unhandled_choice = escape_markdown_v2("неизвестный выбор настроения\\.")
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения\\.")
    prompt_new_name = escape_markdown_v2("введи **название** нового настроения \\(1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\):")
    # Raw format strings
    prompt_new_prompt_fmt_raw = "редактирование настроения: **{name}**\n\n_текущий промпт:_\n`{prompt}`\n\nотправь **новый текст промпта**:"
    prompt_confirm_delete_fmt_raw = "точно удалить настроение **'{name}'**\\?"


    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session, parse_mode=ParseMode.MARKDOWN_V2)
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
                 await query.edit_message_text(error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    await query.answer()

    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        prompt_edit_raw = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"
        # Format correctly
        final_prompt = escape_markdown_v2(prompt_edit_raw.format(name=persona_config.name, id=persona_id))
        await query.edit_message_text(final_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await query.edit_message_text(prompt_new_name, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        original_mood_name = None
        try:
             encoded_mood_name = data.split("editmood_select_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except Exception as decode_err:
             logger.error(f"Error decoding mood name from callback {data}: {decode_err}")
             await query.edit_message_text(error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

        context.user_data['edit_mood_name'] = original_mood_name
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' to edit for {persona_id}.")

        current_prompt_raw_text = "_не найдено_"
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            current_prompt_raw_text = current_moods.get(original_mood_name, "_нет промпта_")
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt_raw_text = "_ошибка чтения промпта_"

        # current_prompt_escaped = escape_markdown_v2(current_prompt_raw_text[:300] + "..." if len(current_prompt_raw_text) > 300 else current_prompt_raw_text)
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        display_name_escaped = escape_markdown_v2(original_mood_name)
        # Format correctly, use raw prompt for code block
        final_prompt = escape_markdown_v2(prompt_new_prompt_fmt_raw.format(
            name=original_mood_name, # Use plain name for bold
            prompt=current_prompt_raw_text[:300] + "..." if len(current_prompt_raw_text) > 300 else current_prompt_raw_text # Use raw for code block
            ))
        await query.edit_message_text(final_prompt, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         original_mood_name = None
         encoded_mood_name = ""
         try:
             encoded_mood_name = data.split("deletemood_confirm_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
         except Exception as decode_err:
             logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
             await query.edit_message_text(error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

         context.user_data['delete_mood_name'] = original_mood_name
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' for {persona_id}. Asking confirmation.")

         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
            ]
         # Format correctly
         final_confirm_prompt = escape_markdown_v2(prompt_confirm_delete_fmt_raw.format(name=original_mood_name))
         await query.edit_message_text(final_confirm_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text(error_unhandled_choice, parse_mode=ParseMode.MARKDOWN_V2)
    return await edit_moods_menu(update, context, persona_config=persona_config)

async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена\\.")
    error_validation = escape_markdown_v2("название: 1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\. попробуй еще:")
    error_name_exists_fmt_raw = "настроение '{name}' уже существует\\. выбери другое:"
    error_db = escape_markdown_v2("ошибка базы данных при проверке имени\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")
    prompt_for_prompt_fmt_raw = "отлично\\! теперь отправь **текст промпта** для настроения **'{name}'**:"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
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
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                # Format correctly
                final_exists_msg = escape_markdown_v2(error_name_exists_fmt_raw.format(name=mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            # Format correctly
            final_prompt = escape_markdown_v2(prompt_for_prompt_fmt_raw.format(name=mood_name))
            await update.message.reply_text(final_prompt, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
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
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет имени настроения\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_validation = escape_markdown_v2("промпт настроения: 1\\-1500 символов\\. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения\\.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения\\.")
    success_saved_fmt_raw = "✅ настроение **{name}** сохранено\\!"

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
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
            # Format correctly
            final_success_msg = escape_markdown_v2(success_saved_fmt_raw.format(name=mood_name))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found_persona = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения\\.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения\\.")
    info_not_found_mood_fmt_raw = "настроение '{name}' не найдено \\(уже удалено?\\)\\."
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения для удаления\\.")
    success_delete_fmt_raw = "🗑️ настроение **{name}** удалено\\."

    original_name_from_callback = None
    if data.startswith("deletemood_delete_"):
        try:
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await query.answer("Ошибка данных", show_alert=True)
            await query.edit_message_text(error_decode_mood, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    if not mood_name_to_delete or not persona_id or not original_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. Stored='{mood_name_to_delete}', Callback='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("Ошибка сессии", show_alert=True)
        await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('delete_mood_name', None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    await query.answer("Удаляем...")
    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name_to_delete}' for persona {persona_id}.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                await query.edit_message_text(error_not_found_persona, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
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
                # Format correctly
                final_success_msg = escape_markdown_v2(success_delete_fmt_raw.format(name=mood_name_to_delete))
                await query.edit_message_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id}.")
                # Format correctly
                final_not_found_msg = escape_markdown_v2(info_not_found_mood_fmt_raw.format(name=mood_name_to_delete))
                await query.edit_message_text(final_not_found_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.pop('delete_mood_name', None)

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text(error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled persona edit/mood edit for persona {persona_id}.")

    cancel_message = escape_markdown_v2("редактирование отменено\\.")

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            if query.message and query.message.text != cancel_message:
                await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        elif message:
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        if message:
            try:
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_e: logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END

async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
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

    # Raw format strings
    error_not_found_fmt_raw = "личность с id `{id}` не найдена или не твоя\\."
    prompt_delete_fmt_raw = "🚨 **ВНИМАНИЕ\\!** 🚨\nудалить личность **'{name}'** \\(id: `{id}`\\)\\?\n\nэто действие **НЕОБРАТИМО**\\!"
    error_db = escape_markdown_v2("ошибка базы данных\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 # Format correctly
                 final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
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
            # Format correctly
            msg_text = escape_markdown_v2(prompt_delete_fmt_raw.format(name=persona_config.name, id=persona_id))

            if is_callback:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer()
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
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
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")

    usage_text = escape_markdown_v2("укажи id личности: `/deletepersona <id>`\nили используй кнопку из /mypersonas")
    error_invalid_id = escape_markdown_v2("ID должен быть числом\\.")

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
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем удаление...")

    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности в кнопке\\.")

    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        try: await query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Failed to edit message with invalid ID error: {e}")
        return ConversationHandler.END

async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_delete_failed = escape_markdown_v2("❌ не удалось удалить личность \\(ошибка базы данных\\)\\.")
    success_deleted_fmt_raw = "✅ личность '{name}' удалена\\."

    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.answer("Ошибка сессии", show_alert=True)
         await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
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
                  await query.edit_message_text(escape_markdown_v2("Ошибка: пользователь не найден\\."), reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                  context.user_data.clear()
                  return ConversationHandler.END

             # Retrieve the name before attempting deletion
             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name # Store name before deletion

             deleted_ok = delete_persona_config(db, persona_id, user.id) # Commits or rolls back inside

             if not deleted_ok and not persona_to_delete:
                 # If deletion failed AND persona wasn't found initially, it was likely already gone
                 logger.warning(f"Persona {persona_id} not found for user {user.id} upon delete confirmation (likely already deleted).")
                 deleted_ok = True # Treat as success from user's perspective

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)

    if deleted_ok:
        # Format correctly
        final_success_msg = escape_markdown_v2(success_deleted_fmt_raw.format(name=persona_name_deleted))
        await query.edit_message_text(final_success_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await query.edit_message_text(error_delete_failed, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)

    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")

    cancel_message = escape_markdown_v2("удаление отменено\\.")

    await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    error_no_persona = escape_markdown_v2("В этом чате нет активной личности\\.")
    error_not_owner = escape_markdown_v2("Только владелец личности может ее заглушить\\.")
    error_no_instance = escape_markdown_v2("Ошибка: не найден объект связи с чатом\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке заглушить бота\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды\\.")
    info_already_muted_fmt_raw = "Личность '{name}' уже заглушена в этом чате\\."
    success_muted_fmt_raw = "✅ Личность '{name}' больше не будет отвечать в этом чате \\(но будет запоминать сообщения\\)\\. Используйте /unmutebot, чтобы вернуть\\."

    with next(get_db()) as db:
        try:
            instance_info = get_persona_and_context_with_owner(chat_id_str, db)
            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance

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
                # Format correctly
                final_success_msg = escape_markdown_v2(success_muted_fmt_raw.format(name=persona.name))
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # Format correctly
                final_already_muted_msg = escape_markdown_v2(info_already_muted_fmt_raw.format(name=persona.name))
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
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id_str}")

    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    error_no_persona = escape_markdown_v2("В этом чате нет активной личности, которую можно размьютить\\.")
    error_not_owner = escape_markdown_v2("Только владелец личности может снять заглушку\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке вернуть бота к общению\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды\\.")
    info_not_muted_fmt_raw = "Личность '{name}' не была заглушена\\."
    success_unmuted_fmt_raw = "✅ Личность '{name}' снова может отвечать в этом чате\\."

    with next(get_db()) as db:
        try:
            active_instance = get_active_chat_bot_instance_with_relations(db, chat_id_str)

            if not active_instance or not active_instance.bot_instance_ref or not active_instance.bot_instance_ref.owner or not active_instance.bot_instance_ref.persona_config:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id_str} by user {user_id}.")
                # Format correctly
                final_success_msg = escape_markdown_v2(success_unmuted_fmt_raw.format(name=persona_name))
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # Format correctly
                final_not_muted_msg = escape_markdown_v2(info_not_muted_fmt_raw.format(name=persona_name))
                await update.message.reply_text(final_not_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
