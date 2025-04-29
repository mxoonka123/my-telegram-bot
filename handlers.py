import logging
import httpx
import random
import asyncio
import re
import uuid
import json
import urllib.parse # Для URL-кодирования
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple

from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, Chat as TgChat # Import Chat for type hint
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
# <<< ИЗМЕНЕНО: Импорт escape_markdown_v2 из utils >>>
from utils import postprocess_response, extract_gif_links, get_time_info, escape_markdown_v2

logger = logging.getLogger(__name__)

# --- Функции проверки подписки и отправки сообщения о необходимости подписки ---
async def check_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user is subscribed to the required channel."""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set in config. Skipping subscription check.")
        return True # Skip check if no channel ID is configured

    # <<< ИЗМЕНЕНО: Проверяем, есть ли update.effective_user >>>
    if not hasattr(update, 'effective_user') or not update.effective_user:
        logger.warning("check_channel_subscription called without valid effective_user.")
        # Если это callback, пытаемся получить user_id из query.from_user
        if update.callback_query and update.callback_query.from_user:
             user_id = update.callback_query.from_user.id
             logger.debug(f"Using user_id {user_id} from callback_query.")
        else:
             return False # Cannot check without user
    else:
        user_id = update.effective_user.id

    if is_admin(user_id): # Admins don't need to subscribe
        return True

    logger.debug(f"Checking subscription status for user {user_id} in channel {CHANNEL_ID}")
    try:
        # Добавляем таймаут для запроса
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id, read_timeout=10)
        # logger.debug(f"get_chat_member response for user {user_id} in {CHANNEL_ID}: {member}")
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
        if hasattr(update, 'effective_message') and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    escape_markdown_v2("Не удалось проверить подписку на канал (таймаут)\\. Попробуйте еще раз позже\\."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Timeout' error message: {send_err}")
        return False
    except Forbidden as e:
        logger.error(f"Forbidden error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}. Ensure bot is admin in the channel.")
        if hasattr(update, 'effective_message') and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    escape_markdown_v2("Не удалось проверить подписку на канал\\. Убедитесь, что бот добавлен в канал как администратор\\."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False # Deny access if check fails
    except BadRequest as e:
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}") # Логируем полную ошибку
         reply_text = escape_markdown_v2("Произошла ошибка при проверке подписки \\(BadRequest\\)\\. Попробуйте позже\\.")
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             reply_text = escape_markdown_v2("Не удается получить доступ к списку участников канала для проверки подписки\\. Возможно, настройки канала не позволяют это сделать\\.")
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             # Не отправляем сообщение пользователю об этом, просто возвращаем False
             return False
         elif "chat not found" in error_message:
              logger.error(f"-> Specific BadRequest: Chat {CHANNEL_ID} not found. Check CHANNEL_ID config.")
              reply_text = escape_markdown_v2("Ошибка: не удалось найти указанный канал для проверки подписки\\. Проверьте настройки бота\\.")

         if hasattr(update, 'effective_message') and update.effective_message:
             try: await update.effective_message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Failed to send 'BadRequest' error message: {send_err}")
         return False # В любом случае BadRequest означает неудачную проверку
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        if hasattr(update, 'effective_message') and update.effective_message:
            try: await update.effective_message.reply_text(escape_markdown_v2("Произошла ошибка при проверке подписки\\. Попробуйте позже\\."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Failed to send 'TelegramError' message: {send_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}", exc_info=True)
        return False

async def send_subscription_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message asking the user to subscribe."""
    # <<< ИЗМЕНЕНО: Получаем target для ответа (message или callback_query.message) >>>
    target_message = None
    if hasattr(update, 'effective_message') and update.effective_message:
         target_message = update.effective_message
    elif update.callback_query and update.callback_query.message:
         target_message = update.callback_query.message

    if not target_message:
         logger.warning("Cannot send subscription required message: no target message found.")
         return

    channel_username = CHANNEL_ID.lstrip('@') if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith('@') else None
    error_msg_raw = "Произошла ошибка при получении ссылки на канал\\."
    subscribe_text_raw = "Для использования бота необходимо подписаться на наш канал\\."
    button_text = "Перейти к каналу"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"Для использования бота необходимо подписаться на канал @{channel_username}\\."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         subscribe_text_raw = "Для использования бота необходимо подписаться на наш основной канал\\."
         subscribe_text_raw += " Пожалуйста, найдите канал в поиске или через описание бота\\."
         # Можно добавить кнопку с ID, если это публичный канал по ID, но URL обычно лучше
    else:
         logger.error(f"Invalid CHANNEL_ID format: {CHANNEL_ID}. Cannot generate subscription message correctly.")
         subscribe_text_raw = error_msg_raw

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        # <<< ИЗМЕНЕНО: Используем target_message и parse_mode >>>
        # <<< ИЗМЕНЕНО: Используем escape_markdown_v2 для всего текста сообщения >>>
        await target_message.reply_text(escape_markdown_v2(subscribe_text_raw), reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        if update.callback_query:
             try: await update.callback_query.answer() # Отвечаем на колбэк
             except: pass
    except BadRequest as e:
        # Логируем ошибку парсинга
        escaped_text_log = escape_markdown_v2(subscribe_text_raw)
        logger.error(f"Failed sending subscription required message (BadRequest): {e} - Text Raw: '{subscribe_text_raw}' Escaped: '{escaped_text_log[:100]}...'")
        # Пытаемся отправить без форматирования
        try:
            plain_text = re.sub(r'\\(.)', r'\1', escaped_text_log) # Убираем экранирование
            await target_message.reply_text(plain_text, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
            logger.error(f"Failed sending plain subscription required message: {fallback_e}")
    except Exception as e:
         logger.error(f"Failed to send subscription required message: {e}")
# --- Конец функций подписки ---


def is_admin(user_id: int) -> bool:
    """Checks if user_id matches the ADMIN_USER_ID from config."""
    return user_id == ADMIN_USER_ID

# Состояния для ConversationHandler
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Карта полей для отображения пользователю (уже с экранированием)
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

# --- Текст Пользовательского Соглашения ---
# Содержит ** для Telegra.ph (будут удалены перед отправкой туда),
# но НЕ содержит символов для экранирования Markdown V2.
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

# Форматируем и Экранируем текст для отправки ботом через MarkdownV2
formatted_tos_text_for_bot = TOS_TEXT_RAW.format(
    subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
    subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
    subscription_currency=config.SUBSCRIPTION_CURRENCY
)
TOS_TEXT = escape_markdown_v2(formatted_tos_text_for_bot)
# --- Конец текста ToS ---


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # --- Обработка специфичных ошибок ---
    if isinstance(context.error, Forbidden):
         if CHANNEL_ID and CHANNEL_ID in str(context.error):
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
                    await update.effective_message.reply_text("Произошла ошибка форматирования ответа. Пожалуйста, сообщите администратору.", parse_mode=None)
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

    # --- Отправка общего сообщения об ошибке пользователю ---
    error_message_raw = "упс... что-то пошло не так. попробуй еще раз позже."
    escaped_error_message = escape_markdown_v2(error_message_raw)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(escaped_error_message, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")
            try:
                 await update.effective_message.reply_text(error_message_raw, parse_mode=None)
            except Exception as final_e:
                 logger.error(f"Failed even sending plain text error message: {final_e}")


def get_persona_and_context_with_owner(chat_id: Union[str, int], db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    """Fetches Persona, its context, and its owner User object."""
    chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id)
    if not chat_instance:
        return None

    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance.")
         return None
    if not bot_instance.persona_config:
         logger.error(f"BotInstance {bot_instance.id} (linked to chat {chat_id}) is missing linked PersonaConfig.")
         return None
    owner_user = bot_instance.owner or bot_instance.persona_config.owner
    if not owner_user:
         logger.error(f"Could not load Owner for BotInstance {bot_instance.id} (linked to chat {chat_id}).")
         return None

    persona_config = bot_instance.persona_config

    try:
        persona = Persona(persona_config, chat_instance)
    except ValueError as e:
         logger.error(f"Failed to initialize Persona for config {persona_config.id} in chat {chat_id}: {e}", exc_info=True)
         return None

    context_list = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context_list, owner_user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Sends request to Langdock API and returns the text response."""
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
        if "content" in data and isinstance(data["content"], list):
            text_parts = [part.get("text", "") for part in data["content"] if part.get("type") == "text"]
            full_response = " ".join(text_parts)
        elif isinstance(data.get("content"), str):
             full_response = data["content"]
        elif isinstance(data.get("content"), dict) and "text" in data["content"]:
            full_response = data["content"]["text"]
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
    """Processes AI response, adds to context (pending commit), and sends to chat. Returns True if context was prepared."""
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return False # Ничего не отправляем и не сохраняем пустой ответ в контекст
    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}")

    # --- Добавляем ответ ассистента в контекст ПЕРЕД отправкой ---
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

    # --- Обработка и отправка ответа ---
    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    for gif in gif_links:
        all_text_content = re.sub(r'\b' + re.escape(gif) + r'\b', "", all_text_content, flags=re.IGNORECASE).strip()
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
             text_parts_to_send[-1] = text_parts_to_send[-1].rstrip('. ') + escape_markdown_v2("...")

    send_tasks = []

    # --- Отправка гифок ---
    for gif in gif_links:
        try:
            send_tasks.append(context.bot.send_animation(chat_id=chat_id, animation=gif))
            logger.info(f"Scheduled sending gif: {gif}")
        except Exception as e:
            logger.error(f"Error scheduling gif send {gif} to chat {chat_id}: {e}", exc_info=True)

    # --- Отправка текстовых частей ---
    if text_parts_to_send:
        chat_type = None
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_type = update.effective_chat.type

        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: continue

            # Имитация печати
            if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 try:
                     # Отправляем typing асинхронно
                     asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING))
                     # Небольшая пауза для имитации набора
                     await asyncio.sleep(random.uniform(0.6, 1.2))
                 except Exception as e:
                      logger.warning(f"Failed to send typing action to {chat_id}: {e}")

            try:
                 escaped_part = escape_markdown_v2(part)
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id}: '{escaped_part[:50]}...'")
                 send_tasks.append(context.bot.send_message(chat_id=chat_id, text=escaped_part, parse_mode=ParseMode.MARKDOWN_V2))
            except BadRequest as e:
                 logger.error(f"Error scheduling text part {i+1} send (BadRequest): {e} - Original: '{part[:100]}...' Escaped: '{escaped_part[:100]}...'")
                 try:
                      send_tasks.append(context.bot.send_message(chat_id=chat_id, text=part, parse_mode=None))
                      logger.info(f"Scheduled part {i+1} as plain text after MarkdownV2 failed.")
                 except Exception as plain_e:
                      logger.error(f"Failed to schedule part {i+1} even as plain text: {plain_e}")
                 break
            except Exception as e:
                 logger.error(f"Error scheduling text part {i+1} send: {e}", exc_info=True)
                 break

    # --- Ожидаем завершения всех задач отправки ---
    if send_tasks:
         results = await asyncio.gather(*send_tasks, return_exceptions=True)
         for i, result in enumerate(results):
              if isinstance(result, Exception):
                  logger.error(f"Failed to send message/animation part {i}: {result}")

    return context_prepared


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Sends a message about exceeding limits with a subscribe button."""
    # <<< ИЗМЕНЕНО: Используем escape_markdown_v2 аккуратно >>>
    count_str = escape_markdown_v2(f"{user.daily_message_count}/{user.message_limit}")
    price_str = escape_markdown_v2(f"{SUBSCRIPTION_PRICE_RUB:.0f}")
    currency_str = escape_markdown_v2(SUBSCRIPTION_CURRENCY)
    paid_limit_str = escape_markdown_v2(str(PAID_DAILY_MESSAGE_LIMIT))
    paid_persona_str = escape_markdown_v2(str(PAID_PERSONA_LIMIT))

    text_to_send = (
        escape_markdown_v2(f"упс\\! 😕 лимит сообщений \\({count_str}\\) на сегодня достигнут\\.\n\n") +
        f"✨ **хочешь безлимита?** ✨\n" +
        escape_markdown_v2(f"подписка за {price_str} {currency_str}/мес дает:\n✅ ") +
        f"**{paid_limit_str}**" + escape_markdown_v2(" сообщений в день\n✅ до ") +
        f"**{paid_persona_str}**" + escape_markdown_v2(" личностей\n✅ полная настройка промптов и настроений\n\n") +
        escape_markdown_v2("👇 жми /subscribe или кнопку ниже\\!")
    )
    raw_text_for_log = "Limit exceeded message" # Placeholder для лога

    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
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
                  plain_text = plain_text.replace("**", "")
                  plain_text = plain_text.replace("✨", "")
                  await context.bot.send_message(target_chat_id, plain_text, reply_markup=reply_markup, parse_mode=None)
         except Exception as final_e:
              logger.error(f"Failed sending limit message even plain: {final_e}")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


# --- Основные обработчики ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages."""
    if not update.message or not (update.message.text or update.message.caption):
        return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    if not message_text:
        return

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str}: {message_text[:100]}")

    # +++ ПРОВЕРКА ПОДПИСКИ НА КАНАЛ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    with next(get_db()) as db:
        try:
            # 1. Получаем активную персону и владельца
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id} (TG ID: {owner_user.telegram_id}) in chat {chat_id_str}")

            # 2. Проверяем лимиты владельца
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}).")
                await send_limit_exceeded_message(update, context, owner_user)
                db.commit()
                return

            # 3. Добавляем сообщение пользователя в контекст (pending commit)
            context_placeholder_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_placeholder_added = True
                    logger.debug("User message prepared for context (pending commit).")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error preparing user message for context: {e_ctx}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    return
                except Exception as e:
                    logger.error(f"Unexpected Error preparing user message for context: {e}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    return
            else:
                logger.error("Cannot add user message to context, chat_instance is None unexpectedly.")
                await update.message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать сообщение с личностью\\."), parse_mode=ParseMode.MARKDOWN_V2)
                return

            # 4. Проверяем мьют
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Message saved to context, but ignoring response.")
                db.commit()
                return

            # 5. Проверяем смену настроения
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
                 await mood(update, context, db=db, persona=persona)
                 return

            # 6. Решаем, отвечать ли
            should_ai_respond = True
            ai_decision_response = None
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
                     except Exception as e:
                          logger.error(f"Error in should_respond logic: {e}", exc_info=True)
                          should_ai_respond = True # Default to responding on error
                 else:
                     should_ai_respond = True

            # 7. Если не отвечаем
            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic.")
                 if ai_decision_response and persona.chat_instance:
                     try:
                         add_message_to_context(db, persona.chat_instance.id, "assistant", ai_decision_response.strip())
                         logger.debug("Added 'should_respond=no' AI response to context.")
                     except Exception as e_ctx: pass # Ignore errors adding decision context
                 db.commit()
                 return

            # 8. Добавляем решение AI 'да' в контекст (pending commit)
            if ai_decision_response and persona.chat_instance:
                 try:
                      add_message_to_context(db, persona.chat_instance.id, "assistant", ai_decision_response.strip())
                      logger.debug("Added 'should_respond=yes' AI response to context (pending commit).")
                 except Exception as e_ctx: pass

            # 9. Получаем контекст для основного ответа
            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error getting context for AI main response: {e_ctx}", exc_info=True)
                     await update.message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                     return
            else:
                 logger.error("Cannot get context for AI main response, chat_instance is None.")
                 return

            # 10. Генерируем и отправляем основной ответ
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                logger.error(f"System prompt formatting failed for persona {persona.name}.")
                await update.message.reply_text(escape_markdown_v2("ошибка при подготовке ответа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                db.commit()
                return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received main response from Langdock: {response_text[:100]}...")

            # 11. Обрабатываем и отправляем (включая добавление ответа в контекст)
            context_response_prepared = await process_and_send_response(update, context, chat_id_str, persona, response_text, db)

            # 12. Коммитим все изменения
            db.commit()
            logger.debug(f"Committed DB changes for handle_message chat {chat_id_str} (UserMsgAdded: {context_placeholder_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
             try: await update.message.reply_text(escape_markdown_v2("ошибка базы данных, попробуйте позже\\."), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception: pass
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id_str}: {e}", exc_info=True)
            try: await update.message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка\\."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Handles incoming photo or voice messages."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ НА КАНАЛ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    with next(get_db()) as db:
        try:
            # 1. Получаем персону и владельца
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            # 2. Проверяем лимиты
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media.")
                await send_limit_exceeded_message(update, context, owner_user)
                db.commit()
                return

            # 3. Определяем плейсхолдер и форматтер
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
                 return

            # 4. Добавляем плейсхолдер в контекст (pending commit)
            context_placeholder_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {context_text_placeholder}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_placeholder_added = True
                    logger.debug(f"Media placeholder '{context_text_placeholder}' prepared for context (pending commit).")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error preparing media placeholder context: {e_ctx}", exc_info=True)
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                     return
                except Exception as e:
                    logger.error(f"Unexpected Error preparing media placeholder context: {e}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать медиа с личностью\\."), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # 5. Проверяем мьют
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Media saved to context, but ignoring response.")
                db.commit()
                return

            # 6. Проверяем наличие шаблона промпта
            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id_str} has no {media_type} template. Skipping.")
                db.commit()
                return

            # 7. Получаем контекст для AI
            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа на медиа\\."), parse_mode=ParseMode.MARKDOWN_V2)
                    return
            else:
                 logger.error("Cannot get context for AI media response, chat_instance is None.")
                 return

            # 8. Генерируем и отправляем ответ
            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit()
                 return

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            # 9. Обрабатываем и отправляем
            context_response_prepared = await process_and_send_response(update, context, chat_id_str, persona, response_text, db)

            # 10. Коммитим все
            db.commit()
            logger.debug(f"Committed DB changes for handle_media chat {chat_id_str} (PlaceholderAdded: {context_placeholder_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка базы данных\\."), parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка\\."), parse_mode=ParseMode.MARKDOWN_V2)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles photo messages."""
    if not update.message: return
    await handle_media(update, context, "photo")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles voice messages."""
    if not update.message: return
    await handle_media(update, context, "voice")


# --- Команды ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    escaped_reply_text = escape_markdown_v2("Произошла ошибка инициализации текста\\.") # Default escaped text
    reply_markup = ReplyKeyboardRemove() # Default markup

    try:
        with next(get_db()) as db:
            # Получаем или создаем пользователя
            user = get_or_create_user(db, user_id, username)
            db.commit()
            db.refresh(user)

            # Проверяем, есть ли активная персона в этом чате
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
                # Обновляем лимиты, если нужно
                now = datetime.now(timezone.utc)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if not user.last_message_reset or user.last_message_reset < today_start:
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit()
                    db.refresh(user)

                # Формируем текст приветствия
                status_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                escaped_status = escape_markdown_v2(status_raw)

                escaped_expires_date = ""
                if user.is_active_subscriber and user.subscription_expires_at:
                    expires_date_str = user.subscription_expires_at.strftime('%d.%m.%Y')
                    escaped_expires_date = escape_markdown_v2(expires_date_str)
                expires_text = f" до {escaped_expires_date}" if escaped_expires_date else ""

                if 'persona_configs' not in user.__dict__ or user.persona_configs is None:
                    user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()
                persona_count = len(user.persona_configs)

                persona_limit_esc = escape_markdown_v2(f"{persona_count}/{user.persona_limit}")
                message_limit_esc = escape_markdown_v2(f"{user.daily_message_count}/{user.message_limit}")

                escaped_reply_text = (
                    escape_markdown_v2("привет\\! 👋 я бот для создания ai\\-собеседников \\(@NunuAiBot\\)\\.\n\n") +
                    f"твой статус: **{escaped_status}**{expires_text}\n" +
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
    """Handles the /help command and the show_help callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # Используем escape_markdown_v2 для всего текста, сохраняя ** и ``
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


# --- Команды управления персоной в чате ---

async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    """Handles /mood command or mood setting callback."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id_str = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    close_db_later = False
    db_session = db
    chat_bot_instance = None
    local_persona = persona

    error_no_persona = escape_markdown_v2("в этом чате нет активной личности\\.")
    error_persona_info = escape_markdown_v2("Ошибка: не найдена информация о личности\\.")
    error_no_moods_fmt = escape_markdown_v2("у личности '") + "{persona_name}" + escape_markdown_v2("' не настроены настроения\\.")
    error_bot_muted_fmt = escape_markdown_v2("личность '") + "{persona_name}" + escape_markdown_v2("' сейчас заглушена \\(/unmutebot\\)\\.")
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
            reply_text = error_bot_muted_fmt.format(persona_name=persona_name_escaped)
            try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             reply_text = error_no_moods_fmt.format(persona_name=persona_name_escaped)
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

        # --- Обработка аргумента или callback ---
        if is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             # Пытаемся собрать имя настроения, оно может содержать _
             # Ищем ID персоны в конце (он числовой)
             if len(parts) >= 3 and parts[-1].isdigit():
                  mood_arg_lower = "_".join(parts[2:-1]).lower() # Собираем имя между set_mood_ и _ID
                  if mood_arg_lower in available_moods_lower:
                      target_mood_original_case = available_moods_lower[mood_arg_lower]
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
        elif not is_callback:
            mood_text = ""
            if context.args:
                 mood_text = " ".join(context.args)
            elif update.message and update.message.text:
                 # Если команда /mood вызвана текстом, который совпадает с именем настроения
                 possible_mood = update.message.text.strip()
                 if possible_mood.lower() in available_moods_lower:
                      mood_text = possible_mood

            if mood_text:
                mood_arg_lower = mood_text.lower()
                if mood_arg_lower in available_moods_lower:
                    target_mood_original_case = available_moods_lower[mood_arg_lower]

        # --- Установка настроения или показ кнопок ---
        if target_mood_original_case:
             # set_mood_for_chat_bot сам коммитит
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case)
             mood_name_escaped = escape_markdown_v2(target_mood_original_case)
             reply_text = f"настроение для '{persona_name_escaped}' теперь: **{mood_name_escaped}**"
             try:
                 if is_callback:
                     query = update.callback_query
                     if query.message.text != reply_text or query.message.reply_markup:
                         await query.edit_message_text(reply_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                     else:
                         await query.answer(f"Настроение: {target_mood_original_case}") # Краткий ответ
                 else:
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood confirmation (BadRequest): {e} - Text: '{reply_text}'")
                  # Попытка отправить без форматирования
                  try:
                       plain_text = f"Настроение для '{local_persona.name}' теперь: {target_mood_original_case}"
                       if is_callback: await query.edit_message_text(plain_text, reply_markup=None, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(plain_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood confirmation: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {local_persona.name} in chat {chat_id_str} set to {target_mood_original_case}.")
        else:
             # Показываем кнопки для выбора
             keyboard = []
             for mood_name in sorted(available_moods, key=str.lower):
                 # Используем ID персоны в callback для надежности
                 button_callback = f"set_mood_{mood_name.lower()}_{local_persona.id}"
                 # Проверяем длину callback_data
                 if len(button_callback.encode('utf-8')) <= 64:
                      keyboard.append([InlineKeyboardButton(mood_name.capitalize(), callback_data=button_callback)])
                 else:
                      logger.warning(f"Callback data for mood '{mood_name}' too long, skipping button.")

             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             current_mood_escaped = escape_markdown_v2(current_mood_text)

             reply_text = ""
             if mood_arg_lower: # Если был передан неверный аргумент
                 mood_arg_escaped = escape_markdown_v2(mood_arg_lower)
                 reply_text = escape_markdown_v2(f"не знаю настроения '{mood_arg_escaped}' для '{persona_name_escaped}'\\. выбери из списка:")
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
             else: # Если аргумента не было
                 reply_text = f"текущее настроение: **{current_mood_escaped}**\\. выбери новое для '{persona_name_escaped}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id_str}.")

             try:
                 if is_callback:
                      query = update.callback_query
                      # Редактируем, только если текст или кнопки изменились
                      if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer()
                 else:
                      await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood selection (BadRequest): {e} - Text: '{reply_text}'")
                  # Попытка отправить без форматирования
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
    """Handles the /reset command to clear context."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset < User {user_id} ({username}) in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности для сброса\\.")
    error_not_owner = escape_markdown_v2("только владелец личности может сбросить её память\\.")
    error_no_instance = escape_markdown_v2("ошибка: не найден экземпляр бота для сброса\\.")
    error_db = escape_markdown_v2("ошибка базы данных при сбросе контекста\\.")
    error_general = escape_markdown_v2("ошибка при сбросе контекста\\.")
    success_reset_fmt = escape_markdown_v2("память личности '") + "{persona_name}" + escape_markdown_v2("' в этом чате очищена\\.")

    with next(get_db()) as db:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_info_tuple:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return
            persona, _, owner_user = persona_info_tuple
            persona_name_escaped = escape_markdown_v2(persona.name) # Экранируем имя здесь

            # Проверка владения
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id_str}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id_str}")
                 await update.message.reply_text(error_no_instance, parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Используем dynamic relationship для удаления
            deleted_count_result = chat_bot_instance.context.delete(synchronize_session='fetch')
            deleted_count = deleted_count_result if isinstance(deleted_count_result, int) else 0
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id_str} by user {user_id}.")
            final_success_msg = success_reset_fmt.format(persona_name=persona_name_escaped) # Используем уже экранированное имя
            await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


# --- Команды управления личностями ---

async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /createpersona command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # <<< ИЗМЕНЕНО: Экранирование текстов ошибок и форматов >>>
    usage_text = escape_markdown_v2("формат: `/createpersona <имя> \\[описание]`\n_имя обязательно, описание нет\\._")
    error_name_len = escape_markdown_v2("имя личности: 2\\-50 символов\\.")
    error_desc_len = escape_markdown_v2("описание: до 1500 символов\\.")
    error_limit_reached_fmt = (
        escape_markdown_v2("упс\\! достигнут лимит личностей \\(") +
        "{current_count}/{limit}" + # Числа экранируются ниже
        escape_markdown_v2("\\) для статуса ") +
        "**{status_text}**" + # Статус экранируется ниже
        escape_markdown_v2("\\. 😟\nчтобы создавать больше, используй /subscribe")
    )
    error_name_exists_fmt = escape_markdown_v2("личность с именем '") + "{persona_name}" + escape_markdown_v2("' уже есть\\. выбери другое\\.")
    error_db = escape_markdown_v2("ошибка базы данных при создании личности\\.")
    error_general = escape_markdown_v2("ошибка при создании личности\\.")
    success_create_fmt = (
        escape_markdown_v2("✅ личность '") + "{name}" + escape_markdown_v2("' создана\\!\nid: ") +
        "`{id}`" + # ID внутри code не экранируем
        escape_markdown_v2("\nописание: ") + "{description}" + # Описание экранируется ниже
        escape_markdown_v2("\n\nдобавь в чат или управляй через /mypersonas")
    )

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
            # Получаем пользователя и сразу проверяем лимит
            user = get_or_create_user(db, user_id, username) # Эта функция не коммитит
            if not user.id: # Если пользователь только что создан, нужен commit
                db.commit()
                db.refresh(user)
                # Перезагружаем с persona_configs для проверки лимита
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()

            # Проверка лимита с использованием user.can_create_persona
            if not user.can_create_persona:
                 # Используем загруженное количество
                 current_count = len(user.persona_configs) if user.persona_configs is not None else 0
                 limit = user.persona_limit
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")

                 status_text_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                 status_text_escaped = escape_markdown_v2(status_text_raw)
                 current_count_esc = escape_markdown_v2(str(current_count))
                 limit_esc = escape_markdown_v2(str(limit))

                 final_limit_msg = error_limit_reached_fmt.format(
                     current_count=current_count_esc,
                     limit=limit_esc,
                     status_text=f"**{status_text_escaped}**" # Вставляем жирный статус
                 )
                 await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Проверяем уникальность имени для этого пользователя
            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                 persona_name_escaped = escape_markdown_v2(persona_name)
                 final_exists_msg = error_name_exists_fmt.format(persona_name=persona_name_escaped)
                 await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Создаем персону (функция сама коммитит)
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            name_escaped = escape_markdown_v2(new_persona.name)
            desc_display = escape_markdown_v2(new_persona.description) if new_persona.description else escape_markdown_v2("\\(пусто\\)")
            final_success_msg = success_create_fmt.format(name=name_escaped, id=new_persona.id, description=desc_display)
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
        except BadRequest as e: # Ловим BadRequest здесь
             logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
             # Попытка отправить простое сообщение
             try: await update.message.reply_text("Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback create_persona error: {fe}")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mypersonas command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_db = escape_markdown_v2("ошибка при загрузке списка личностей\\.")
    error_general = escape_markdown_v2("произошла ошибка при обработке команды /mypersonas\\.")
    error_user_not_found = escape_markdown_v2("Ошибка: не удалось найти пользователя\\.")
    info_no_personas_fmt = escape_markdown_v2("у тебя пока нет личностей \\({count}/{limit}\\)\\.\nсоздай: `/createpersona <имя>`")
    info_list_header_fmt = escape_markdown_v2("твои личности \\({count}/{limit}\\):\n")

    try:
        with next(get_db()) as db:
            # Загружаем пользователя с его персонами
            user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()

            if not user_with_personas:
                 user_with_personas = get_or_create_user(db, user_id, username)
                 db.commit()
                 db.refresh(user_with_personas)
                 user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()
                 if not user_with_personas:
                     logger.error(f"User {user_id} not found even after get_or_create in my_personas.")
                     await update.message.reply_text(error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                     return

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            if not personas:
                count_esc = escape_markdown_v2(str(persona_count))
                limit_esc = escape_markdown_v2(str(persona_limit))
                text_to_send = info_no_personas_fmt.format(count=count_esc, limit=limit_esc)
                await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN_V2)
                return

            count_esc = escape_markdown_v2(str(persona_count))
            limit_esc = escape_markdown_v2(str(persona_limit))
            text = info_list_header_fmt.format(count=count_esc, limit=limit_esc)

            keyboard = []
            for p in personas:
                 # Текст кнопок НЕ экранируем
                 button_text = f"👤 {p.name} (ID: {p.id})"
                 # Проверяем длину callback_data
                 edit_cb = f"edit_persona_{p.id}"
                 delete_cb = f"delete_persona_{p.id}"
                 add_cb = f"add_bot_{p.id}"
                 if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64 or len(add_cb.encode('utf-8')) > 64:
                      logger.warning(f"Callback data for persona {p.id} might be too long, potentially causing issues.")
                 keyboard.append([InlineKeyboardButton(button_text, callback_data=f"dummy_{p.id}")]) # Заглушка
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
    """Handles /addbot command or callback to add a persona to the current chat."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(message_or_callback_msg.chat.id)
    chat_title = escape_markdown_v2(message_or_callback_msg.chat.title or f"Chat {chat_id_str}") # Экранируем название чата
    local_persona_id = persona_id

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    usage_text = escape_markdown_v2("формат: `/addbot <id персоны>`\nили используй кнопку '➕ В чат' из /mypersonas")
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности\\.")
    error_invalid_id_cmd = escape_markdown_v2("id личности должен быть числом\\.")
    error_no_id = escape_markdown_v2("Ошибка: ID личности не определен\\.")
    error_persona_not_found_fmt = escape_markdown_v2("личность с id `") + "{id}" + escape_markdown_v2("` не найдена или не твоя\\.")
    error_already_active_fmt = escape_markdown_v2("личность '") + "{name}" + escape_markdown_v2("' уже активна в этом чате\\.")
    error_link_failed = escape_markdown_v2("не удалось активировать личность \\(ошибка связывания\\)\\.")
    error_integrity = escape_markdown_v2("произошла ошибка целостности данных \\(возможно, конфликт активации\\), попробуйте еще раз\\.")
    error_db = escape_markdown_v2("ошибка базы данных при добавлении бота\\.")
    error_general = escape_markdown_v2("ошибка при активации личности\\.")
    success_added_structure = escape_markdown_v2("✅ личность '{name}' \\(id: `{id}`\\) активирована в этом чате\\! Память очищена\\.")

    # --- Определение ID персоны ---
    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("Ошибка: неверный ID", show_alert=True)
             # await update.callback_query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2) # Не редактируем, т.к. могли удалить
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
            # 1. Проверяем, существует ли персона и принадлежит ли она пользователю
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 id_esc = escape_markdown_v2(str(local_persona_id))
                 final_not_found_msg = error_persona_not_found_fmt.format(id=id_esc)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_not_found_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # 2. Деактивируем любую другую активную персону в этом чате
            existing_active_link = db.query(ChatBotInstance).options(
                 selectinload(ChatBotInstance.bot_instance_ref).selectinload(BotInstance.persona_config) # Загружаем связи для проверки ID и имени
            ).filter(
                 ChatBotInstance.chat_id == int(chat_id_str), # Сравниваем с int
                 ChatBotInstance.active == True
            ).first()

            if existing_active_link:
                # Проверяем, не пытаемся ли мы добавить ту же самую персону
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    persona_name_escaped = escape_markdown_v2(persona.name)
                    final_already_active_msg = error_already_active_fmt.format(name=persona_name_escaped)
                    reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                    if is_callback: await update.callback_query.answer(f"'{persona.name}' уже активна", show_alert=True)
                    await reply_target.reply_text(final_already_active_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

                    # Очищаем контекст при повторном добавлении
                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id_str} on re-add.")
                    deleted_ctx_result = existing_active_link.context.delete(synchronize_session='fetch')
                    deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                    db.commit() # Коммитим удаление контекста
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return
                else:
                    # Деактивируем предыдущую
                    prev_persona_name = existing_active_link.bot_instance_ref.persona_config.name if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config else f"ID {existing_active_link.bot_instance_id}"
                    logger.info(f"Deactivating previous bot '{prev_persona_name}' in chat {chat_id_str} before activating '{persona.name}'.")
                    existing_active_link.active = False
                    # Не коммитим здесь, коммит будет после успешного добавления новой
                    db.flush() # Применяем деактивацию в сессии

            # 3. Находим или создаем BotInstance
            user = persona.owner # Используем загруженного владельца
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                 logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                 # create_bot_instance сама коммитит
                 try:
                      bot_instance = create_bot_instance(db, user.id, local_persona_id, name=f"Inst:{persona.name}")
                 except (IntegrityError, SQLAlchemyError):
                      logger.error("Failed to create BotInstance, possibly due to concurrent request. Retrying fetch.")
                      db.rollback() # Откатываем неудавшуюся попытку создания
                      bot_instance = db.query(BotInstance).filter(BotInstance.persona_config_id == local_persona_id).first()
                      if not bot_instance:
                           logger.error("Failed to fetch BotInstance even after retry.")
                           raise SQLAlchemyError("Failed to create or fetch BotInstance")


            # 4. Связываем BotInstance с чатом (функция сама коммитит или откатывает)
            # Передаем chat_id_str, функция link_bot_instance_to_chat сама конвертирует в int
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)

            if chat_link:
                 # Форматируем сообщение об успехе
                 name_esc = escape_markdown_v2(persona.name)
                 id_esc = escape_markdown_v2(str(local_persona_id))
                 final_success_msg = success_added_structure.format(name=name_esc, id=id_esc)
                 # Используем send_message вместо reply_text, чтобы точно отправить в нужный чат
                 await context.bot.send_message(chat_id=chat_id_str, text=final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 if is_callback:
                      try:
                           await update.callback_query.delete_message() # Удаляем сообщение с кнопками
                      except Exception as del_err:
                           logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}, '{persona.name}') to chat {chat_id_str}. ChatBotInstance ID: {chat_link.id}")
            else:
                 # Если link_bot_instance_to_chat вернул None, значит произошла ошибка/откат внутри
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(error_link_failed, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id_str} - link_bot_instance_to_chat returned None.")

        except IntegrityError as e:
             logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=False)
             await context.bot.send_message(chat_id=chat_id_str, text=error_integrity, parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
             logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e: # Ловим BadRequest при отправке сообщения
             logger.error(f"BadRequest sending message in add_bot_to_chat: {e}", exc_info=True)
             try: await context.bot.send_message(chat_id=chat_id_str, text="Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback add_bot_to_chat error: {fe}")
        except Exception as e:
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_general, parse_mode=ParseMode.MARKDOWN_V2)


# --- Обработчик Callback Query ---

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from inline keyboards."""
    query = update.callback_query
    if not query or not query.data: return

    chat_id_str = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для некоторых callback) +++
    needs_subscription_check = True
    no_check_callbacks = (
        "cancel_edit", "edit_persona_back", "edit_moods_back_cancel",
        "delete_persona_cancel", "view_tos", "subscribe_info",
        "show_help", "dummy_", "confirm_pay", "subscribe_pay"
    )
    # Проверяем также префиксы для conversation хендлеров
    conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood")

    if data.startswith(no_check_callbacks) or any(data.startswith(p) for p in conv_prefixes):
        needs_subscription_check = False

    if needs_subscription_check:
        # Передаем update для получения user_id
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context) # Эта функция сама обработает callback
            try: await query.answer(text="Подпишитесь на канал!", show_alert=True)
            except: pass
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # --- Маршрутизация колбэков ---
    if data.startswith("set_mood_"):
        await mood(update, context) # mood() handles answer
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
        # add_bot_to_chat() handles answer
        await add_bot_to_chat(update, context)
    elif data == "show_help":
        await query.answer()
        await help_command(update, context) # help_command() handles sending/editing
    elif data.startswith("dummy_"):
        await query.answer() # Просто отвечаем на callback-заглушку
    else:
        # Проверяем, не является ли это callback'ом для ConversationHandler
        # Эти префиксы уже проверялись выше, но на всякий случай
        known_conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood_", "cancel_edit", "edit_persona_back", "edit_moods_back_cancel", "deletemood_confirm_", "deletemood_delete_")
        if any(data.startswith(p) for p in known_conv_prefixes):
             logger.debug(f"Callback '{data}' appears to be for a ConversationHandler, skipping direct handling.")
             # НЕ отвечаем на callback здесь, ConversationHandler должен это сделать
        else:
            # Если это не известный нам колбэк, логируем и отвечаем
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            try:
                 # Отвечаем на колбэк, чтобы кнопка перестала "грузиться"
                 await query.answer("Неизвестное действие")
            except Exception as e:
                 logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


# --- Команды управления профилем и подпиской ---

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows user profile info."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_db = escape_markdown_v2("ошибка базы данных при загрузке профиля\\.")
    error_general = escape_markdown_v2("ошибка при обработке команды /profile\\.")
    error_user_not_found = escape_markdown_v2("ошибка: пользователь не найден\\.")

    with next(get_db()) as db:
        try:
            # Загружаем пользователя с его персонами
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user:
                user = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user)
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()
                if not user:
                    logger.error(f"User {user_id} not found after get_or_create in profile.")
                    await update.message.reply_text(error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                    return

            # Обновляем лимиты
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
                     expires_text_raw = user.subscription_expires_at.strftime('%d.%m.%Y %H:%M') + " UTC"
                     expires_text = escape_markdown_v2(f"активна до: {expires_text_raw}")
                 except AttributeError:
                      expires_text = escape_markdown_v2("активна \\(дата истечения некорректна\\)")

            persona_count = len(user.persona_configs) if user.persona_configs is not None else 0
            persona_limit = user.persona_limit
            msg_count = user.daily_message_count
            msg_limit = user.message_limit

            # Экранируем числа
            msg_count_esc = escape_markdown_v2(str(msg_count))
            msg_limit_esc = escape_markdown_v2(str(msg_limit))
            persona_count_esc = escape_markdown_v2(str(persona_count))
            persona_limit_esc = escape_markdown_v2(str(persona_limit))

            # Собираем текст
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
    """Handles /subscribe command or 'subscribe_info' callback."""
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update_or_reply = update.callback_query.message if from_callback else update.message
    if not message_to_update_or_reply: return

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not from_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
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
    """Handles the 'view_tos' callback to show the Terms of Service."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    tos_url = context.bot_data.get('tos_url')
    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_tos_link = "Не удалось отобразить ссылку на соглашение." # Plain text for answer
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
    """Handles the 'confirm_pay' callback after viewing ToS."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна\\. 😥 \\(проблема с настройками\\)")
    info_confirm = escape_markdown_v2(
         "✅ Отлично\\!\n\n"
         "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "Пользовательским Соглашением\\." # Добавлена точка
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
        price_str = escape_markdown_v2(f"{SUBSCRIPTION_PRICE_RUB:.0f}")
        currency_str = escape_markdown_v2(SUBSCRIPTION_CURRENCY)
        # Текст кнопки НЕ экранируем
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {price_str} {currency_str}", callback_data="subscribe_pay")]
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
    """Generates Yookassa payment link."""
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_yk_not_ready = escape_markdown_v2("❌ ошибка: сервис оплаты не настроен правильно\\.")
    error_yk_config = escape_markdown_v2("❌ ошибка конфигурации платежной системы\\.")
    error_receipt = escape_markdown_v2("❌ ошибка при формировании данных чека\\.")
    error_link_get_fmt = escape_markdown_v2("❌ не удалось получить ссылку от платежной системы") # Часть сообщения
    error_link_create = escape_markdown_v2("❌ не удалось создать ссылку для оплаты\\. ") # Часть сообщения
    success_link = escape_markdown_v2(
        "✅ ссылка для оплаты создана\\!\n\n"
        "нажми кнопку ниже для перехода к оплате\\. " # Добавлена точка
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
                "vat_code": "1", # НДС не облагается (или ваш код НДС)
                "payment_mode": "full_prepayment",
                "payment_subject": "service" # Предмет расчета: услуга
            })
        ]
        user_email = f"user_{user_id}@telegram.bot" # Placeholder email, YK требует email или телефон
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

        # Выполняем синхронный вызов Yookassa в отдельном потоке
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = error_link_get_fmt
             if payment_response and payment_response.status:
                 error_message += escape_markdown_v2(f" \\(статус: {payment_response.status}\\)")
             error_message += escape_markdown_v2("\\.\nПопробуй позже\\.")
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
        user_message = error_link_create
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                if "Invalid credentials" in err_text:
                    user_message += escape_markdown_v2(" Ошибка аутентификации с ЮKassa\\.")
                elif "receipt" in err_text.lower():
                     user_message += escape_markdown_v2(" Ошибка данных чека \\(детали в логах\\)\\.")
                else:
                    user_message += escape_markdown_v2(" Ошибка от ЮKassa \\(детали в логах\\)\\.")
            except Exception as parse_e:
                logger.error(f"Could not parse YK error response: {parse_e}")
                user_message += escape_markdown_v2(" Ошибка от ЮKassa \\(не удалось разобрать ответ\\)\\.")
        elif isinstance(e, httpx.RequestError):
             user_message += escape_markdown_v2(" Проблема с сетевым подключением к ЮKassa\\.")
        else:
             user_message += escape_markdown_v2(" Произошла непредвиденная ошибка\\.")
        user_message += escape_markdown_v2("\nПопробуй еще раз позже или свяжись с поддержкой\\.")
        try:
            await query.edit_message_text(user_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


# --- Placeholder для вебхука Yookassa (не должен вызываться) ---
async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass # Ничего не делаем


# --- Conversation Handlers ---
# (Код для Edit Persona и Delete Persona скопирован из предыдущего ответа,
#  т.к. он уже содержал исправления для MarkdownV2 и другие улучшения)

# --- Edit Persona Conversation ---
async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Internal helper to start the edit conversation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id
    is_callback = update.callback_query is not None

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Clear previous convo state

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_not_found_fmt = escape_markdown_v2("личность с id `") + "{id}" + escape_markdown_v2("` не найдена или не твоя\\.")
    error_db = escape_markdown_v2("ошибка базы данных при начале редактирования\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")
    prompt_edit_fmt = escape_markdown_v2("редактируем ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)\nвыбери, что изменить:")

    try:
        with next(get_db()) as db:
            # Загружаем персону и ее владельца для проверки
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id) # Убеждаемся, что владелец - текущий пользователь
            ).first()

            if not persona_config:
                 id_esc = escape_markdown_v2(str(persona_id))
                 final_error_msg = error_not_found_fmt.format(id=id_esc)
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config) # Клавиатура генерируется с учетом премиума
            reply_markup = InlineKeyboardMarkup(keyboard)
            persona_name_escaped = escape_markdown_v2(persona_config.name)
            id_esc = escape_markdown_v2(str(persona_id))
            msg_text = prompt_edit_fmt.format(name=f"**{persona_name_escaped}**", id=id_esc) # Используем жирный для имени

            reply_target = update.callback_query.message if is_callback else update.effective_message
            if is_callback:
                 query = update.callback_query
                 try:
                      # Редактируем только если текст или кнопки отличаются
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer() # Просто отвечаем, если ничего не поменялось
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
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
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
    """Entry point for edit button from /mypersonas."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности в кнопке\\.")

    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        try: # Пытаемся отредактировать сообщение с ошибкой
            await query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: # Если не удалось, просто логируем
            logger.error(f"Failed to edit message with invalid ID error: {e}")
        return ConversationHandler.END

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's choice of field to edit or action."""
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, PersonaID={persona_id}, Callback data={data} ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет id\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("ошибка базы данных при проверке данных\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка\\.")
    info_premium_mood = "⭐ Редактирование настроений доступно по подписке"
    info_premium_field_fmt = "⭐ Поле '{field_name}' доступно по подписке"
    prompt_edit_value_fmt = escape_markdown_v2("отправь новое значение для ") + "**{field_name}**" + escape_markdown_v2("\\.\n_текущее:_") + "\n`{current_value}`"
    prompt_edit_max_msg_fmt = escape_markdown_v2("отправь новое значение для ") + "**{field_name}**" + escape_markdown_v2(" \\(число от 1 до 10\\):\n_текущее: {current_value}_")

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

    # Fetch user and persona
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
            is_premium_user = persona_config.owner.is_active_subscriber

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return EDIT_PERSONA_CHOICE
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.answer("Непредвиденная ошибка", show_alert=True)
         await query.edit_message_text(error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

    # --- Обработка выбора пользователя ---
    if data == "cancel_edit":
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        if not is_premium_user and not is_admin(user_id):
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer(info_premium_mood, show_alert=True)
             return EDIT_PERSONA_CHOICE
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             await query.answer()
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        field_display_name = FIELD_MAP.get(field, escape_markdown_v2(field)) # Уже экранировано
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        advanced_fields = ["system_prompt_template", "should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user and not is_admin(user_id):
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             field_plain_name = re.sub(r'\\(.)', r'\1', field_display_name) # Убираем экранирование для ответа
             await query.answer(info_premium_field_fmt.format(field_name=field_plain_name), show_alert=True)
             return EDIT_PERSONA_CHOICE

        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        await query.answer()

        if field == "max_response_messages":
            current_value = getattr(persona_config, field, 3)
            current_value_esc = escape_markdown_v2(str(current_value)) # Экранируем текущее значение
            final_prompt = prompt_edit_max_msg_fmt.format(field_name=field_display_name, current_value=current_value_esc)
            await query.edit_message_text(final_prompt, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_MAX_MESSAGES
        else:
            current_value_raw = getattr(persona_config, field, "")
            current_value_display = escape_markdown_v2(str(current_value_raw) if len(str(current_value_raw)) < 300 else str(current_value_raw)[:300] + "...")
            final_prompt = prompt_edit_value_fmt.format(field_name=field_display_name, current_value=current_value_display) # current_value_display уже экранирован
            await query.edit_message_text(final_prompt, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_FIELD

    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         await query.answer()
         keyboard = await _get_edit_persona_keyboard(persona_config)
         prompt_edit_back = escape_markdown_v2("редактируем ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)\nвыбери, что изменить:")
         name_esc = escape_markdown_v2(persona_config.name)
         id_esc = escape_markdown_v2(str(persona_id))
         final_back_msg = prompt_edit_back.format(name=f"**{name_esc}**", id=id_esc)
         await query.edit_message_text(final_back_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.pop('edit_field', None)
         return EDIT_PERSONA_CHOICE

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.answer("Неизвестный выбор", show_alert=True)
    return EDIT_PERSONA_CHOICE

async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the new value for a field."""
    if not update.message or not update.message.text: return EDIT_FIELD
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}' ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_validation_fmt = "{field_name}" + escape_markdown_v2(": макс\\. ") + "{max_len}" + escape_markdown_v2(" символов\\.")
    error_validation_min_fmt = "{field_name}" + escape_markdown_v2(": мин\\. ") + "{min_len}" + escape_markdown_v2(" символа\\.")
    error_name_taken_fmt = escape_markdown_v2("имя '") + "{name}" + escape_markdown_v2("' уже занято другой твоей личностью\\. попробуй другое:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении\\. попробуй еще раз\\.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении\\.")
    success_update_fmt = escape_markdown_v2("✅ поле ") + "**{field_name}**" + escape_markdown_v2(" для личности ") + "**{persona_name}**" + escape_markdown_v2(" обновлено\\!")
    prompt_next_edit_fmt = escape_markdown_v2("что еще изменить для ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)?")

    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    field_display_name = FIELD_MAP.get(field, escape_markdown_v2(field)) # Уже экранировано

    # Validation logic
    validation_error_msg = None
    max_len_map = {
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len_map = {"name": 2}

    if field in max_len_map and len(new_value) > max_len_map[field]:
        max_len_esc = escape_markdown_v2(str(max_len_map[field]))
        validation_error_msg = error_validation_fmt.format(field_name=field_display_name, max_len=max_len_esc)
    if field in min_len_map and len(new_value) < min_len_map[field]:
        min_len_esc = escape_markdown_v2(str(min_len_map[field]))
        validation_error_msg = error_validation_min_fmt.format(field_name=field_display_name, min_len=min_len_esc)

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

            # Check name uniqueness
            if field == "name" and new_value.lower() != persona_config.name.lower():
                existing = db.query(PersonaConfig.id).filter(
                    PersonaConfig.owner_id == persona_config.owner_id,
                    func.lower(PersonaConfig.name) == new_value.lower()
                ).first()
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by their persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    name_esc = escape_markdown_v2(new_value)
                    final_name_taken_msg = error_name_taken_fmt.format(name=name_esc)
                    await update.message.reply_text(final_name_taken_msg, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                    return EDIT_FIELD

            # Update field
            setattr(persona_config, field, new_value)
            db.commit()
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            name_esc = escape_markdown_v2(persona_config.name)
            final_success_msg = success_update_fmt.format(field_name=field_display_name, persona_name=f"**{name_esc}**")
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            # Return to main edit menu
            context.user_data.pop('edit_field', None)
            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            id_esc = escape_markdown_v2(str(persona_id))
            final_next_prompt = prompt_next_edit_fmt.format(name=f"**{name_esc}**", id=id_esc)
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
    """Handles receiving the new value for max_response_messages."""
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages" # Используем имя поля напрямую
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет persona_id\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_invalid_value = escape_markdown_v2("неверное значение\\. введи число от 1 до 10:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении\\. попробуй еще раз\\.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении\\.")
    success_update_fmt = escape_markdown_v2("✅ макс\\. сообщений в ответе для ") + "**{name}**" + escape_markdown_v2(" установлено: ") + "**{value}**"
    prompt_next_edit_fmt = escape_markdown_v2("что еще изменить для ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)?")

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
            final_success_msg = success_update_fmt.format(name=f"**{name_esc}**", value=f"**{value_esc}**")
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            # Return to main edit menu
            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            id_esc = escape_markdown_v2(str(persona_id))
            final_next_prompt = prompt_next_edit_fmt.format(name=f"**{name_esc}**", id=id_esc)
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
    """Generates the keyboard for the main persona edit menu."""
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]

    is_premium = False
    owner = persona_config.owner # Используем уже загруженного владельца
    if owner:
        is_premium = owner.is_active_subscriber or is_admin(owner.telegram_id)
    else:
        logger.warning(f"Owner not loaded for persona {persona_config.id} in _get_edit_persona_keyboard")

    star = " ⭐" if is_premium else ""
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)

    # Текст кнопок НЕ экранируем
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
     """Generates the keyboard for the mood editing menu."""
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
              # Используем оригинальное имя для отображения
              display_name = mood_name.capitalize()
              # Кодируем имя для callback data
              encoded_mood_name = urllib.parse.quote(mood_name)
              edit_cb = f"editmood_select_{encoded_mood_name}"
              delete_cb = f"deletemood_confirm_{encoded_mood_name}"

              # Проверяем длину callback_data
              if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64:
                   logger.warning(f"Encoded mood name '{encoded_mood_name}' too long for callback data, skipping buttons.")
                   continue

              keyboard.append([
                  InlineKeyboardButton(f"✏️ {display_name}", callback_data=edit_cb),
                  InlineKeyboardButton(f"🗑️", callback_data=delete_cb)
              ])
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])
     return keyboard

async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    """Attempts to return to the main edit menu after an error."""
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message_target = update.effective_message

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню редактирования \\(личность не найдена\\)\\.")
    error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню редактирования\\.")
    prompt_edit = escape_markdown_v2("редактируем ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)\nвыбери, что изменить:")

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
                name_esc = escape_markdown_v2(persona_config.name)
                id_esc = escape_markdown_v2(str(persona_id))
                final_prompt = prompt_edit.format(name=f"**{name_esc}**", id=id_esc)
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
     """Attempts to return to the mood edit menu after an error."""
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     callback_message = update.callback_query.message if update.callback_query else None
     user_message = update.message # Сообщение пользователя, вызвавшее ошибку (если было)

     # <<< ИЗМЕНЕНО: Экранирование текстов >>>
     error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню настроений \\(личность не найдена\\)\\.")
     error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню настроений\\.")
     prompt_mood_menu = escape_markdown_v2("управление настроениями для ") + "**{name}**" + escape_markdown_v2(":")

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
                 name_esc = escape_markdown_v2(persona_config.name)
                 final_prompt = prompt_mood_menu.format(name=f"**{name_esc}**")

                 # Отправляем новое сообщение с меню
                 await context.bot.send_message(
                     chat_id=target_chat_id,
                     text=final_prompt,
                     reply_markup=InlineKeyboardMarkup(keyboard),
                     parse_mode=ParseMode.MARKDOWN_V2
                 )
                 # Пытаемся удалить сообщение, вызвавшее ошибку (если это было сообщение бота)
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
    """Displays the mood editing menu."""
    query = update.callback_query
    if not query: return ConversationHandler.END # Should not happen if called from callback

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при загрузке настроений\\.")
    info_premium = "⭐ Доступно по подписке"
    prompt_mood_menu_fmt = escape_markdown_v2("управление настроениями для ") + "**{name}**" + escape_markdown_v2(":")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    local_persona_config = persona_config
    is_premium = False

    # Загружаем персону, если она не передана
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
                is_premium = local_persona_config.owner.is_active_subscriber or is_admin(user_id)

        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("Ошибка базы данных", show_alert=True)
             await query.edit_message_text(error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
             return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    else:
         is_premium = local_persona_config.owner.is_active_subscriber or is_admin(user_id)

    # Проверка премиума
    if not is_premium:
        logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
        await query.answer(info_premium, show_alert=True)
        return EDIT_PERSONA_CHOICE

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    name_esc = escape_markdown_v2(local_persona_config.name)
    msg_text = prompt_mood_menu_fmt.format(name=f"**{name_esc}**")

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
    """Handles selecting a mood to edit/delete or adding a new one."""
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("Ошибка базы данных\\.")
    error_unhandled_choice = escape_markdown_v2("неизвестный выбор настроения\\.")
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения\\.")
    prompt_new_name = escape_markdown_v2("введи **название** нового настроения \\(одно слово, латиница/кириллица, цифры, дефис, подчеркивание, без пробелов\\):")
    prompt_new_prompt_fmt = escape_markdown_v2("редактирование настроения: ") + "**{name}**" + escape_markdown_v2("\n\n_текущий промпт:_") + "\n`{prompt}`" + escape_markdown_v2("\n\nотправь **новый текст промпта**:")
    prompt_confirm_delete_fmt = escape_markdown_v2("точно удалить настроение ") + "**'{name}'**" + escape_markdown_v2("\\?")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Fetch persona config
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
         return EDIT_MOOD_CHOICE

    await query.answer()

    # --- Обработка выбора ---
    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        prompt_edit = escape_markdown_v2("редактируем ") + "**{name}**" + escape_markdown_v2(" \\(id: `{id}`\\)\nвыбери, что изменить:")
        name_esc = escape_markdown_v2(persona_config.name)
        id_esc = escape_markdown_v2(str(persona_id))
        final_prompt = prompt_edit.format(name=f"**{name_esc}**", id=id_esc)
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

        current_prompt_raw = escape_markdown_v2("_не найдено_")
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            prompt_raw = current_moods.get(original_mood_name, "_нет промпта_")
            # Экранируем промпт для отображения
            current_prompt_raw = escape_markdown_v2(prompt_raw[:300] + "..." if len(prompt_raw) > 300 else prompt_raw)
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt_raw = escape_markdown_v2("_ошибка чтения промпта_")

        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        display_name = escape_markdown_v2(original_mood_name)
        final_prompt = prompt_new_prompt_fmt.format(name=f"**{display_name}**", prompt=current_prompt_raw)
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
         escaped_original_name = escape_markdown_v2(original_mood_name)

         # Текст кнопок НЕ экранируем
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
            ]
         final_confirm_prompt = prompt_confirm_delete_fmt.format(name=f"**{escaped_original_name}**")
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
    """Handles receiving the new mood name."""
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена\\.")
    error_validation = escape_markdown_v2("название: 1\\-30 символов, только буквы/цифры/дефис/подчеркивание \\(кириллица/латиница\\), без пробелов\\. попробуй еще:")
    error_name_exists_fmt = escape_markdown_v2("настроение '") + "{name}" + escape_markdown_v2("' уже существует\\. выбери другое:")
    error_db = escape_markdown_v2("ошибка базы данных при проверке имени\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")
    prompt_for_prompt_fmt = escape_markdown_v2("отлично\\! теперь отправь **текст промпта** для настроения ") + "**'{name}'**" + escape_markdown_v2(":")

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
                name_esc = escape_markdown_v2(mood_name)
                final_exists_msg = error_name_exists_fmt.format(name=name_esc)
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[back_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            name_esc = escape_markdown_v2(mood_name)
            final_prompt = prompt_for_prompt_fmt.format(name=f"**{name_esc}**")
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
    """Handles receiving the new mood prompt."""
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна \\(нет имени настроения\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_validation = escape_markdown_v2("промпт настроения: 1\\-1500 символов\\. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения\\.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения\\.")
    success_saved_fmt = escape_markdown_v2("✅ настроение ") + "**{name}**" + escape_markdown_v2(" сохранено\\!")

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
            persona_config.set_moods(db, current_moods) # set_moods использует flag_modified
            db.commit()

            context.user_data.pop('edit_mood_name', None)
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            name_esc = escape_markdown_v2(mood_name)
            final_success_msg = success_saved_fmt.format(name=f"**{name_esc}**")
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
    """Handles confirmation of mood deletion."""
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found_persona = escape_markdown_v2("ошибка: личность не найдена или нет доступа\\.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения\\.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения\\.")
    info_not_found_mood_fmt = escape_markdown_v2("настроение '") + "{name}" + escape_markdown_v2("' не найдено \\(уже удалено?\\)\\.")
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения для удаления\\.")
    success_delete_fmt = escape_markdown_v2("🗑️ настроение ") + "**{name}**" + escape_markdown_v2(" удалено\\.")

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
                name_esc = escape_markdown_v2(mood_name_to_delete)
                final_success_msg = success_delete_fmt.format(name=f"**{name_esc}**")
                await query.edit_message_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id}.")
                name_esc = escape_markdown_v2(mood_name_to_delete)
                final_not_found_msg = info_not_found_mood_fmt.format(name=name_esc)
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
    """Cancels the persona edit conversation."""
    message = update.effective_message
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled persona edit/mood edit for persona {persona_id}.")

    # <<< ИЗМЕНЕНО: Экранирование текста >>>
    cancel_message = escape_markdown_v2("редактирование отменено\\.")

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            # Редактируем, только если текст отличается
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

# --- Delete Persona Conversation ---
async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Internal helper to start the delete conversation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id
    is_callback = update.callback_query is not None
    reply_target = update.callback_query.message if is_callback else update.effective_message

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_not_found_fmt = escape_markdown_v2("личность с id `") + "{id}" + escape_markdown_v2("` не найдена или не твоя\\.")
    error_db = escape_markdown_v2("ошибка базы данных\\.")
    error_general = escape_markdown_v2("непредвиденная ошибка\\.")
    prompt_delete_fmt = (
        escape_markdown_v2("🚨 **ВНИМАНИЕ\\!** 🚨\nудалить личность ") +
        "**'{name}'**" + escape_markdown_v2(" \\(id: `{id}`\\)\\?\n\n") +
        escape_markdown_v2("это действие **НЕОБРАТИМО**\\!")
    )

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 id_esc = escape_markdown_v2(str(persona_id))
                 final_error_msg = error_not_found_fmt.format(id=id_esc)
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            context.user_data['delete_persona_id'] = persona_id
            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            # Текст кнопок НЕ экранируем
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_name_display}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            name_esc = escape_markdown_v2(persona_config.name)
            id_esc = escape_markdown_v2(str(persona_id))
            msg_text = prompt_delete_fmt.format(name=f"**{name_esc}**", id=id_esc)

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
    """Entry point for /deletepersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
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
    """Entry point for delete button from /mypersonas."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем удаление...")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
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
    """Handles confirmation of persona deletion."""
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_delete_failed = escape_markdown_v2("❌ не удалось удалить личность \\(ошибка базы данных\\)\\.")
    success_deleted_fmt = escape_markdown_v2("✅ личность '") + "{name}" + escape_markdown_v2("' удалена\\.")

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

             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 if delete_persona_config(db, persona_id, user.id): # Функция коммитит или откатывает
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name_deleted}').")
                     deleted_ok = True
                 else:
                     logger.error(f"delete_persona_config returned False for persona {persona_id}, user internal ID {user.id}.")
             else:
                 logger.warning(f"User {user_id} confirmed delete, but persona {persona_id} not found (maybe already deleted). Assuming OK.")
                 deleted_ok = True

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)

    if deleted_ok:
        name_esc = escape_markdown_v2(persona_name_deleted)
        final_success_msg = success_deleted_fmt.format(name=name_esc)
        await query.edit_message_text(final_success_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await query.edit_message_text(error_delete_failed, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)

    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona deletion conversation."""
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")

    # <<< ИЗМЕНЕНО: Экранирование текста >>>
    cancel_message = escape_markdown_v2("удаление отменено\\.")

    await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

# --- Mute/Unmute Commands ---

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /mutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_persona = escape_markdown_v2("В этом чате нет активной личности\\.")
    error_not_owner = escape_markdown_v2("Только владелец личности может ее заглушить\\.")
    error_no_instance = escape_markdown_v2("Ошибка: не найден объект связи с чатом\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке заглушить бота\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды\\.")
    info_already_muted_fmt = escape_markdown_v2("Личность '") + "{name}" + escape_markdown_v2("' уже заглушена в этом чате\\.")
    success_muted_fmt = escape_markdown_v2("✅ Личность '") + "{name}" + escape_markdown_v2("' больше не будет отвечать в этом чате \\(но будет запоминать сообщения\\)\\. Используйте /unmutebot, чтобы вернуть\\.")

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
                final_success_msg = success_muted_fmt.format(name=persona_name_escaped)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_already_muted_msg = info_already_muted_fmt.format(name=persona_name_escaped)
                await update.message.reply_text(final_already_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)

async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /unmutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id_str}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # <<< ИЗМЕНЕНО: Экранирование текстов >>>
    error_no_persona = escape_markdown_v2("В этом чате нет активной личности, которую можно размьютить\\.")
    error_not_owner = escape_markdown_v2("Только владелец личности может снять заглушку\\.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке вернуть бота к общению\\.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды\\.")
    info_not_muted_fmt = escape_markdown_v2("Личность '") + "{name}" + escape_markdown_v2("' не была заглушена\\.")
    success_unmuted_fmt = escape_markdown_v2("✅ Личность '") + "{name}" + escape_markdown_v2("' снова может отвечать в этом чате\\.")

    with next(get_db()) as db:
        try:
            active_instance = db.query(ChatBotInstance)\
                .options(
                    selectinload(ChatBotInstance.bot_instance_ref)
                    .selectinload(BotInstance.owner),
                    selectinload(ChatBotInstance.bot_instance_ref)
                    .selectinload(BotInstance.persona_config)
                )\
                .filter(ChatBotInstance.chat_id == int(chat_id_str), ChatBotInstance.active == True)\
                .first()

            if not active_instance or not active_instance.bot_instance_ref or not active_instance.bot_instance_ref.owner or not active_instance.bot_instance_ref.persona_config:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name
            escaped_persona_name = escape_markdown_v2(persona_name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = success_unmuted_fmt.format(name=escaped_persona_name)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_not_muted_msg = info_not_muted_fmt.format(name=escaped_persona_name)
                await update.message.reply_text(final_not_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
