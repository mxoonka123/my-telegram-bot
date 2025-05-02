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
                    escape_markdown_v2("Не удалось проверить подписку на канал (таймаут). Попробуйте еще раз позже."),
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
                    escape_markdown_v2("Не удалось проверить подписку на канал. Убедитесь, что бот добавлен в канал как администратор."),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False
    except BadRequest as e:
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
         reply_text_raw = "Произошла ошибка при проверке подписки (BadRequest). Попробуйте позже."
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             reply_text_raw = "Не удается получить доступ к списку участников канала для проверки подписки. Возможно, настройки канала не позволяют это сделать."
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             # User is effectively not subscribed if not found in the channel
             return False
         elif "chat not found" in error_message:
              logger.error(f"-> Specific BadRequest: Chat {CHANNEL_ID} not found. Check CHANNEL_ID config.")
              reply_text_raw = "Ошибка: не удалось найти указанный канал для проверки подписки. Проверьте настройки бота."

         # Try to inform the user about the BadRequest
         target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
         if target_message:
             try: await target_message.reply_text(escape_markdown_v2(reply_text_raw), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception as send_err: logger.error(f"Failed to send 'BadRequest' error message: {send_err}")
         return False # Deny access on BadRequest
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        # Try to inform the user about the generic Telegram error
        target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)
        if target_message:
            try: await target_message.reply_text(escape_markdown_v2("Произошла ошибка при проверке подписки. Попробуйте позже."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Failed to send 'TelegramError' message: {send_err}")
        return False # Deny access on other Telegram errors
    except Exception as e:
        logger.error(f"Unexpected error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}", exc_info=True)
        return False # Deny access on unexpected errors

async def send_subscription_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message asking the user to subscribe to the channel."""
    target_message = getattr(update, 'effective_message', None) or getattr(getattr(update, 'callback_query', None), 'message', None)

    if not target_message:
         logger.warning("Cannot send subscription required message: no target message found.")
         return

    channel_username = None
    if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith('@'):
        channel_username = CHANNEL_ID.lstrip('@')

    error_msg_raw = "Произошла ошибка при получении ссылки на канал."
    subscribe_text_raw = "Для использования бота необходимо подписаться на наш канал."
    button_text = "Перейти к каналу"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"Для использования бота необходимо подписаться на канал @{channel_username}."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         # If channel ID is private, we can't generate a direct link easily
         subscribe_text_raw = "Для использования бота необходимо подписаться на наш основной канал. Пожалуйста, найдите канал в поиске или через описание бота."
         # No button possible for private channels by ID without an invite link
    else:
         logger.error(f"Invalid CHANNEL_ID format: {CHANNEL_ID}. Cannot generate subscription message correctly.")
         subscribe_text_raw = error_msg_raw # Fallback error message

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    escaped_text = escape_markdown_v2(subscribe_text_raw)
    try:
        await target_message.reply_text(escaped_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        # Answer callback query if applicable
        if update.callback_query:
             try: await update.callback_query.answer()
             except: pass # Ignore if answering fails
    except BadRequest as e:
        logger.error(f"Failed sending subscription required message (BadRequest): {e} - Text Raw: '{subscribe_text_raw}' Escaped: '{escaped_text[:100]}...'")
        # Fallback to sending without Markdown
        try:
            await target_message.reply_text(subscribe_text_raw, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
            logger.error(f"Failed sending plain subscription required message: {fallback_e}")
    except Exception as e:
         logger.error(f"Failed to send subscription required message: {e}")

def is_admin(user_id: int) -> bool:
    """Checks if the user ID belongs to the admin."""
    return user_id == ADMIN_USER_ID

# Conversation states
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Map for field names and their descriptions (plain text for prompts)
# <<< ИЗМЕНЕНО: Добавлены более подробные описания для промптов >>>
FIELD_INFO = {
    "name": {"label": "Имя", "desc": "Как будут звать личность (2-50 симв)."},
    "description": {"label": "Описание", "desc": "Подробное описание характера, предыстории, интересов (до 1500 симв)."},
    "system_prompt_template": {"label": "Системный промпт", "desc": "Главная инструкция для AI, определяющая роль и поведение. Используйте плейсхолдеры (см. /placeholders). (до 3000 симв)."},
    "should_respond_prompt_template": {"label": "Промпт 'Отвечать?'", "desc": "Инструкция для AI, чтобы решать, отвечать ли на сообщение в группе (ожидается 'да'/'нет'). Используйте плейсхолдеры. (до 1000 симв)."},
    "spam_prompt_template": {"label": "Промпт спама", "desc": "Инструкция для генерации случайных сообщений (если включено). Используйте плейсхолдеры. (до 1000 симв)."},
    "photo_prompt_template": {"label": "Промпт фото", "desc": "Инструкция для реакции на полученные фото. Используйте плейсхолдеры. (до 1000 симв)."},
    "voice_prompt_template": {"label": "Промпт голоса", "desc": "Инструкция для реакции на голосовые сообщения. Используйте плейсхолдеры. (до 1000 симв)."},
    "max_response_messages": {"label": "Макс. сообщений", "desc": "Максимальное кол-во сообщений в одном ответе AI (1-10)."}
}

# --- Terms of Service Text ---
# (Keep the raw text structure for potential future use, e.g., Telegraph page)
TOS_TEXT_RAW = """
📜 Пользовательское Соглашение Сервиса @NunuAiBot

Привет! Добро пожаловать в @NunuAiBot! Мы очень рады, что вы с нами. Это Соглашение — документ, который объясняет правила использования нашего Сервиса. Прочитайте его, пожалуйста.

Дата последнего обновления: 01.03.2025

1. О чем это Соглашение?
1.1. Это Пользовательское Соглашение (или просто "Соглашение") — договор между вами (далее – "Пользователь" или "Вы") и нами (владельцем Telegram-бота @NunuAiBot, далее – "Сервис" или "Мы"). Оно описывает условия использования Сервиса.
1.2. Начиная использовать наш Сервис (просто отправляя боту любое сообщение или команду), Вы подтверждаете, что прочитали, поняли и согласны со всеми условиями этого Соглашения. Если Вы не согласны хотя бы с одним пунктом, пожалуйста, прекратите использование Сервиса.
1.3. Наш Сервис предоставляет Вам интересную возможность создавать и общаться с виртуальными собеседниками на базе искусственного интеллекта (далее – "Личности" или "AI-собеседники").

2. Про подписку и оплату
2.1. Мы предлагаем два уровня доступа: бесплатный и Premium (платный). Возможности и лимиты для каждого уровня подробно описаны внутри бота, например, в командах `/profile` и `/subscribe`.
2.2. Платная подписка дает Вам расширенные возможности и увеличенные лимиты на период в {subscription_duration} дней.
2.3. Стоимость подписки составляет {subscription_price} {subscription_currency} за {subscription_duration} дней.
2.4. Оплата проходит через безопасную платежную систему Yookassa. Важно: мы не получаем и не храним Ваши платежные данные (номер карты и т.п.). Все безопасно.
2.5. Политика возвратов: Покупая подписку, Вы получаете доступ к расширенным возможностям Сервиса сразу же после оплаты. Поскольку Вы получаете услугу немедленно, оплаченные средства за этот период доступа, к сожалению, не подлежат возврату.
2.6. В редких случаях, если Сервис окажется недоступен по нашей вине в течение длительного времени (более 7 дней подряд), и у Вас будет активная подписка, Вы можете написать нам в поддержку (контакт указан в биографии бота и в нашем Telegram-канале). Мы рассмотрим возможность продлить Вашу подписку на срок недоступности Сервиса. Решение принимается индивидуально.

3. Ваши и наши права и обязанности
3.1. Что ожидается от Вас (Ваши обязанности):
•   Использовать Сервис только в законных целях и не нарушать никакие законы при его использовании.
•   Не пытаться вмешаться в работу Сервиса или получить несанкционированный доступ.
•   Не использовать Сервис для рассылки спама, вредоносных программ или любой запрещенной информации.
•   Если требуется (например, для оплаты), предоставлять точную и правдивую информацию.
•   Поскольку у Сервиса нет возрастных ограничений, Вы подтверждаете свою способность принять условия настоящего Соглашения.
3.2. Что можем делать мы (Наши права):
•   Мы можем менять условия этого Соглашения. Если это произойдет, мы уведомим Вас, опубликовав новую версию Соглашения в нашем Telegram-канале или иным доступным способом в рамках Сервиса. Ваше дальнейшее использование Сервиса будет означать согласие с изменениями.
•   Мы можем временно приостановить или полностью прекратить Ваш доступ к Сервису, если Вы нарушите условия этого Соглашения.
•   Мы можем изменять сам Сервис: добавлять или убирать функции, менять лимиты или стоимость подписки.

4. Важное предупреждение об ограничении ответственности
4.1. Сервис предоставляется "как есть". Это значит, что мы не можем гарантировать его идеальную работу без сбоев или ошибок. Технологии иногда подводят, и мы не несем ответственности за возможные проблемы, возникшие не по нашей прямой вине.
4.2. Помните, Личности — это искусственный интеллект. Их ответы генерируются автоматически и могут быть неточными, неполными, странными или не соответствующими Вашим ожиданиям или реальности. Мы не несем никакой ответственности за содержание ответов, сгенерированных AI-собеседниками. Не воспринимайте их как истину в последней инстанции или профессиональный совет.
4.3. Мы не несем ответственности за любые прямые или косвенные убытки или ущерб, который Вы могли понести в результате использования (или невозможности использования) Сервиса.

5. Про Ваши данные (Конфиденциальность)
5.1. Для работы Сервиса нам приходится собирать и обрабатывать минимальные данные: Ваш Telegram ID (для идентификации аккаунта), имя пользователя Telegram (username, если есть), информацию о Вашей подписке, информацию о созданных Вами Личностях, а также историю Ваших сообщений с Личностями (это нужно AI для поддержания контекста разговора).
5.2. Мы предпринимаем разумные шаги для защиты Ваших данных, но, пожалуйста, помните, что передача информации через Интернет никогда не может быть абсолютно безопасной.

6. Действие Соглашения
6.1. Настоящее Соглашение начинает действовать с момента, как Вы впервые используете Сервис, и действует до момента, пока Вы не перестанете им пользоваться или пока Сервис не прекратит свою работу.

7. Интеллектуальная Собственность
7.1. Вы сохраняете все права на контент (текст), который Вы создаете и вводите в Сервис в процессе взаимодействия с AI-собеседниками.
7.2. Вы предоставляете нам неисключительную, безвозмездную, действующую по всему миру лицензию на использование Вашего контента исключительно в целях предоставления, поддержания и улучшения работы Сервиса (например, для обработки Ваших запросов, сохранения контекста диалога, анонимного анализа для улучшения моделей, если применимо).
7.3. Все права на сам Сервис (код бота, дизайн, название, графические элементы и т.д.) принадлежат владельцу Сервиса.
7.4. Ответы, сгенерированные AI-собеседниками, являются результатом работы алгоритмов искусственного интеллекта. Вы можете использовать полученные ответы в личных некоммерческих целях, но признаете, что они созданы машиной и не являются Вашей или нашей интеллектуальной собственностью в традиционном понимании.

8. Заключительные положения
8.1. Все споры и разногласия решаются путем переговоров. Если это не поможет, споры будут рассматриваться в соответствии с законодательством Российской Федерации.
8.2. По всем вопросам, касающимся настоящего Соглашения или работы Сервиса, Вы можете обращаться к нам через контакты, указанные в биографии бота и в нашем Telegram-канале.
"""

# Format the ToS text with current config values and escape for MarkdownV2
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

    # Specific error handling examples
    if isinstance(context.error, Forbidden):
         # Check if it's related to the channel subscription check
         if CHANNEL_ID and str(CHANNEL_ID) in str(context.error):
             logger.warning(f"Error handler caught Forbidden regarding channel {CHANNEL_ID}. Bot likely not admin or kicked.")
             # Don't notify user in this case, as it's a config issue
             return
         else:
             # Handle other Forbidden errors (e.g., bot blocked by user)
             logger.warning(f"Caught generic Forbidden error: {context.error}")
             # Maybe notify admin or just log
             return

    elif isinstance(context.error, BadRequest):
        error_text = str(context.error).lower()
        if "message is not modified" in error_text:
            # Ignore common error when editing message with same content/markup
            logger.info("Ignoring 'message is not modified' error.")
            return
        elif "can't parse entities" in error_text:
            # Markdown parsing error
            logger.error(f"MARKDOWN PARSE ERROR: {context.error}. Update: {update}")
            # Try to inform the user about the formatting issue
            if isinstance(update, Update) and update.effective_message:
                try:
                    await update.effective_message.reply_text("Произошла ошибка при форматировании ответа. Пожалуйста, сообщите администратору.", parse_mode=None)
                except Exception as send_err:
                    logger.error(f"Failed to send plain text formatting error message: {send_err}")
            return
        elif "chat member status is required" in error_text:
             # Often related to channel subscription checks failing unexpectedly
             logger.warning(f"Error handler caught BadRequest likely related to missing channel membership check: {context.error}")
             # Might not need to notify user, depends on context
             return
        elif "chat not found" in error_text:
             # Bot trying to interact with a chat that doesn't exist or it was kicked from
             logger.error(f"BadRequest: Chat not found error: {context.error}")
             return
        # <<< ДОБАВЛЕНО: Обработка ошибки "Reply message not found" >>>
        elif "reply message not found" in error_text:
            logger.warning(f"BadRequest: Reply message not found. Original message might have been deleted. Update: {update}")
            # Не отправляем сообщение пользователю, просто логируем
            return
        else:
             # Log other BadRequest errors
             logger.error(f"Unhandled BadRequest error: {context.error}")

    elif isinstance(context.error, TimedOut):
         # Network timeout communicating with Telegram API
         logger.warning(f"Telegram API request timed out: {context.error}")
         # Maybe retry or inform user if interaction was critical
         return

    elif isinstance(context.error, TelegramError):
         # Catch other Telegram API errors
         logger.error(f"Generic Telegram API error: {context.error}")

    # Generic fallback error message to the user for unhandled errors
    error_message_raw = "упс... что-то пошло не так. попробуй еще раз позже."
    escaped_error_message = escape_markdown_v2(error_message_raw)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(escaped_error_message, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_md:
             # If even the basic Markdown fails, send plain text
             if "can't parse entities" in str(e_md).lower():
                 logger.error(f"Failed sending even basic Markdown error msg ({e_md}). Sending plain.")
                 try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                 except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
             else:
                 # Handle other BadRequest errors when sending error message
                 logger.error(f"Failed sending error message (BadRequest, not parse): {e_md}")
                 try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                 except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
        except Exception as e:
            # Handle other errors during error message sending
            logger.error(f"Failed to send error message to user: {e}")
            # Final attempt to send plain text
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
        # No active bot instance in this chat
        return None

    # Navigate through relationships loaded by get_active_chat_bot_instance_with_relations
    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id_str} is missing linked BotInstance.")
         return None
    if not bot_instance.persona_config:
         logger.error(f"BotInstance {bot_instance.id} (linked to chat {chat_id_str}) is missing linked PersonaConfig.")
         return None
    # Get owner, prioritizing the direct link from BotInstance if available
    owner_user = bot_instance.owner or bot_instance.persona_config.owner
    if not owner_user:
         # This should ideally not happen if DB constraints are set up correctly
         logger.error(f"Could not load Owner for BotInstance {bot_instance.id} (linked to chat {chat_id_str}).")
         return None

    persona_config = bot_instance.persona_config

    # Initialize the Persona object
    try:
        persona = Persona(persona_config, chat_instance)
    except ValueError as e:
         logger.error(f"Failed to initialize Persona for config {persona_config.id} in chat {chat_id_str}: {e}", exc_info=True)
         return None

    # Get the message context
    context_list = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context_list, owner_user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Sends the prompt and context to the Langdock API and returns the response."""
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return escape_markdown_v2("ошибка: ключ api не настроен.") # User-facing error

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    # Ensure we don't send excessively long history
    messages_to_send = messages[-MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]

    payload = {
        "model": LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages_to_send,
        "max_tokens": 1024, # Adjust as needed
        "temperature": 0.7, # Adjust creativity/determinism
        "top_p": 0.95,      # Adjust nucleus sampling
        "stream": False     # Set to True if you want streaming responses
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages" # Ensure correct endpoint
    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages. Temp: {payload['temperature']}. System prompt length: {len(system_prompt)}")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client: # Increased timeout
             resp = await client.post(url, json=payload, headers=headers)
        logger.debug(f"Langdock response status: {resp.status_code}")
        resp.raise_for_status() # Raise exception for 4xx/5xx errors
        data = resp.json()

        # --- Extract response text ---
        # Langdock/Anthropic API structure: response is in data['content'][0]['text']
        full_response = ""
        content = data.get("content")
        if isinstance(content, list) and content:
            first_content_block = content[0]
            if isinstance(first_content_block, dict) and first_content_block.get("type") == "text":
                full_response = first_content_block.get("text", "")
        # Add fallbacks for potentially different structures if needed
        elif isinstance(content, dict) and "text" in content: # Less common
            full_response = content["text"]
        elif isinstance(content, str): # Very unlikely for this API
             full_response = content
        # Add other potential keys based on observed Langdock responses if necessary
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
             return escape_markdown_v2("ai вернул пустой ответ.") # User-facing error

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return escape_markdown_v2("хм, кажется, я слишком долго думал... попробуй еще раз?")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {error_body}", exc_info=False) # Log body for debugging
        error_text_raw = f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."
        # Try to parse specific error message from Langdock/Anthropic
        try:
             error_data = json.loads(error_body)
             if isinstance(error_data.get('error'), dict) and 'message' in error_data['error']:
                  api_error_msg = error_data['error']['message']
                  logger.error(f"Langdock API Error Message: {api_error_msg}")
                  # Optionally include part of the API error in user message if safe
             elif isinstance(error_data.get('error'), str):
                   logger.error(f"Langdock API Error Message: {error_data['error']}")
        except Exception: pass # Ignore parsing errors
        return escape_markdown_v2(error_text_raw)
    except httpx.RequestError as e:
        # Network-level errors (DNS, connection refused, etc.)
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return escape_markdown_v2("не могу связаться с ai сейчас (ошибка сети)...")
    except Exception as e:
        # Catch-all for other unexpected errors
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return escape_markdown_v2("произошла внутренняя ошибка при генерации ответа.")


# <<< ИЗМЕНЕНО: Добавлен параметр reply_to_message_id >>>
async def process_and_send_response(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: Union[str, int],
    persona: Persona,
    full_bot_response_text: str,
    db: Session,
    reply_to_message_id: Optional[int] = None # <<< ДОБАВЛЕНО
) -> bool:
    """Processes the AI response, adds it to context, extracts GIFs, splits text, and sends messages."""
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return False # Indicate context was not prepared with a response

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}. ReplyTo: {reply_to_message_id}")

    chat_id_str = str(chat_id)
    context_prepared = False # Flag to track if assistant message was added to DB context

    # 1. Add the full, raw response to the database context first
    if persona.chat_instance:
        try:
            # Use the raw response before splitting/GIF extraction for context
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            logger.debug("AI response prepared for database context (pending commit).")
            context_prepared = True
        except SQLAlchemyError as e:
            logger.error(f"DB Error preparing assistant response for context chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            # Continue processing to send message, but context might be incomplete
        except Exception as e:
            logger.error(f"Unexpected Error preparing assistant response for context chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
    else:
        # This should not happen if called correctly
        logger.error("Cannot add AI response to context, chat_instance is None.")

    # 2. Extract GIFs and remaining text
    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    # Remove GIF links from the text content
    for gif in gif_links:
        # Use regex to remove the link and surrounding whitespace robustly
        all_text_content = re.sub(r'\s*' + re.escape(gif) + r'\s*', " ", all_text_content, flags=re.IGNORECASE).strip()
    # Clean up multiple spaces that might result from removal
    all_text_content = re.sub(r'\s{2,}', ' ', all_text_content).strip()

    # 3. Split the remaining text into sendable parts
    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    # 4. Apply max response message limit from persona config
    max_messages = 3 # Default
    if persona.config and hasattr(persona.config, 'max_response_messages'):
         # Ensure value is at least 1
         max_messages = max(1, persona.config.max_response_messages or 3)

    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        # Add ellipsis to indicate truncation if parts remain
        if text_parts_to_send:
             ellipsis_raw = "..."
             last_part = text_parts_to_send[-1].rstrip('. ') # Remove trailing dots/spaces
             if last_part and isinstance(last_part, str):
                text_parts_to_send[-1] = f"{last_part}{ellipsis_raw}"
             else:
                # If last part became empty after stripping, just add ellipsis
                text_parts_to_send[-1] = ellipsis_raw


    # 5. Schedule sending GIFs and text parts
    send_tasks = []
    first_message_sent = False # Flag to track if the first message (text or GIF) has been scheduled

    # Schedule GIFs first
    for gif in gif_links:
        try:
            # <<< ИЗМЕНЕНО: Добавляем reply_to_message_id только для первого сообщения (GIF или текст) >>>
            current_reply_id = reply_to_message_id if not first_message_sent else None
            # Use send_animation for GIFs
            send_tasks.append(context.bot.send_animation(
                chat_id=chat_id_str,
                animation=gif,
                reply_to_message_id=current_reply_id
            ))
            first_message_sent = True # Mark first message as scheduled
            logger.info(f"Scheduled sending gif: {gif} (ReplyTo: {current_reply_id})")
        except Exception as e:
            logger.error(f"Error scheduling gif send {gif} to chat {chat_id_str}: {e}", exc_info=True)

    # Schedule text parts
    if text_parts_to_send:
        chat_type = None
        # Get chat type to potentially add typing delay in groups
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_type = update.effective_chat.type

        for i, part in enumerate(text_parts_to_send):
            part_raw = part.strip()
            if not part_raw: continue # Skip empty parts

            # Add a small delay with typing action in group chats for realism
            if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 try:
                     # Send typing action before the delay
                     asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING))
                     await asyncio.sleep(random.uniform(0.6, 1.2)) # Short random delay
                 except Exception as e:
                      logger.warning(f"Failed to send typing action to {chat_id_str}: {e}")

            # <<< ИЗМЕНЕНО: Добавляем reply_to_message_id только для первого сообщения (GIF или текст) >>>
            current_reply_id = reply_to_message_id if not first_message_sent else None

            # Try sending with MarkdownV2 first
            try:
                 escaped_part = escape_markdown_v2(part_raw)
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id_str} (MDv2, ReplyTo: {current_reply_id}): '{escaped_part[:50]}...'")
                 send_tasks.append(context.bot.send_message(
                     chat_id=chat_id_str,
                     text=escaped_part,
                     parse_mode=ParseMode.MARKDOWN_V2,
                     reply_to_message_id=current_reply_id
                 ))
                 first_message_sent = True # Mark first message as scheduled
            except BadRequest as e:
                 # If Markdown parsing fails, retry with plain text
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
                           first_message_sent = True # Mark first message as scheduled
                      except Exception as plain_e:
                           logger.error(f"Failed to schedule part {i+1} even as plain text: {plain_e}")
                 # <<< ДОБАВЛЕНО: Обработка ошибки "Reply message not found" >>>
                 elif "reply message not found" in str(e).lower():
                     logger.warning(f"Cannot reply to message {reply_to_message_id} (likely deleted). Sending part {i+1} without reply.")
                     try:
                         # Retry sending without reply_to_message_id
                         escaped_part = escape_markdown_v2(part_raw)
                         send_tasks.append(context.bot.send_message(
                             chat_id=chat_id_str,
                             text=escaped_part,
                             parse_mode=ParseMode.MARKDOWN_V2,
                             reply_to_message_id=None # <<< УБРАН REPLY ID
                         ))
                         first_message_sent = True # Mark first message as scheduled
                     except Exception as retry_e:
                         logger.error(f"Failed to schedule part {i+1} even without reply: {retry_e}")
                 else:
                     # Handle other BadRequest errors
                     logger.error(f"Error scheduling text part {i+1} send (BadRequest, not parse): {e} - Original: '{part_raw[:100]}...' Escaped: '{escaped_part[:100]}...'")
            except Exception as e:
                 # Handle other errors during scheduling
                 logger.error(f"Error scheduling text part {i+1} send: {e}", exc_info=True)
                 break # Stop trying to send further parts on error

    # 6. Execute all scheduled send tasks concurrently
    if send_tasks:
         results = await asyncio.gather(*send_tasks, return_exceptions=True)
         # Log any errors that occurred during sending
         for i, result in enumerate(results):
              if isinstance(result, Exception):
                  # <<< ДОБАВЛЕНО: Более детальное логирование ошибки отправки >>>
                  error_type = type(result).__name__
                  error_msg = str(result)
                  # Не логируем "Reply message not found" как ошибку здесь, т.к. уже обработали выше
                  if "reply message not found" in error_msg.lower():
                      logger.info(f"Sending message part {i} failed because reply target was lost (expected).")
                  else:
                      logger.error(f"Failed to send message/animation part {i}: {error_type} - {error_msg}")

    return context_prepared # Return whether the response was added to DB context


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Sends a message informing the user they've hit their daily limit."""
    # Prepare dynamic parts (raw values)
    count_raw = f"{user.daily_message_count}/{user.message_limit}"
    price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}" # Integer price
    currency_raw = SUBSCRIPTION_CURRENCY
    paid_limit_raw = str(PAID_DAILY_MESSAGE_LIMIT)
    paid_persona_raw = str(PAID_PERSONA_LIMIT)

    # Construct the message text (raw)
    text_raw = (
        f"упс! 😕 лимит сообщений ({count_raw}) на сегодня достигнут.\n\n"
        f"✨ хочешь безлимита? ✨\n"
        f"подписка за {price_raw} {currency_raw}/мес дает:\n✅ "
        f"{paid_limit_raw} сообщений в день\n✅ до "
        f"{paid_persona_raw} личностей\n✅ полная настройка промптов и настроений\n\n"
        f"👇 жми /subscribe или кнопку ниже!"
    )
    # Escape for MarkdownV2
    text_to_send = escape_markdown_v2(text_raw)

    # Create inline button
    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Determine target chat ID
    target_chat_id = None
    try:
        # Prefer the chat where the limit was hit, fallback to user's TG ID if possible
        target_chat_id = update.effective_chat.id if update.effective_chat else user.telegram_id
        if target_chat_id:
             await context.bot.send_message(target_chat_id, text=text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
             # Should not happen if update or user object is valid
             logger.warning(f"Could not send limit exceeded message to user {user.telegram_id}: no effective chat.")
    except BadRequest as e:
         # Handle Markdown parsing errors specifically
         logger.error(f"Failed sending limit message (BadRequest): {e} - Text Raw: '{text_raw[:100]}...' Escaped: '{text_to_send[:100]}...'")
         # Fallback to plain text
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
        # Ignore messages without text content (e.g., user joins/leaves)
        return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}" # Use ID if no username
    message_text = (update.message.text or update.message.caption or "").strip()
    message_id = update.message.message_id # <<< ДОБАВЛЕНО: Получаем ID сообщения для reply
    if not message_text:
        # Ignore messages with only whitespace
        return

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str} (MsgID: {message_id}): {message_text[:100]}") # Log first 100 chars

    # 1. Check channel subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    # Use a database session context
    with next(get_db()) as db:
        try:
            # 2. Get active persona, context, and owner for this chat
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                # No active persona in this chat, ignore message for bot processing
                logger.debug(f"No active persona in chat {chat_id_str}. Ignoring message.")
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id} (TG ID: {owner_user.telegram_id}) in chat {chat_id_str}")

            # 3. Check and update owner's message limits
            limit_ok = check_and_update_user_limits(db, owner_user)
            limit_state_updated = db.is_modified(owner_user) # Check if limits were actually updated/reset

            if not limit_ok:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}).")
                await send_limit_exceeded_message(update, context, owner_user)
                # Commit the limit update/reset even if limit exceeded
                if limit_state_updated:
                    db.commit()
                return

            # 4. Add user message to context (before checking mute/mood)
            context_user_msg_added = False
            if persona.chat_instance:
                try:
                    # Include username in context for multi-user chats
                    user_prefix = username
                    # <<< ИЗМЕНЕНО: Убрано добавление ID сообщения в контекст, т.к. не используется LLM >>>
                    # context_content = f"{user_prefix} (MsgID: {message_id}): {message_text}"
                    context_content = f"{user_prefix}: {message_text}" # <<< ВОЗВРАЩЕНО: Простой формат для LLM
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_user_msg_added = True
                    logger.debug("User message prepared for context (pending commit).")
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"Error preparing user message for context: {e_ctx}", exc_info=True)
                    # Inform user about the context saving error
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback() # Rollback if context saving fails
                    return
            else:
                # This state should ideally not be reachable if persona_context_owner_tuple was found
                logger.error("Cannot add user message to context, chat_instance is None unexpectedly.")
                await update.message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать сообщение с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            # 5. Check if bot is muted in this chat
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Message saved to context, but ignoring response.")
                db.commit() # Commit user message context and limit updates
                return

            # 6. Check if message is a mood command (simple text match)
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
                 # Commit the user message context before calling mood handler
                 db.commit()
                 # Call the mood handler (needs a separate DB session potentially)
                 with next(get_db()) as mood_db_session:
                      # Re-fetch persona within the new session context for mood handler
                      persona_for_mood_tuple = get_persona_and_context_with_owner(chat_id_str, mood_db_session)
                      if persona_for_mood_tuple:
                           await mood(update, context, db=mood_db_session, persona=persona_for_mood_tuple[0])
                      else:
                          # Should not happen if persona existed moments ago
                          logger.error(f"Could not re-fetch persona for mood change in chat {chat_id_str}")
                          await update.message.reply_text(escape_markdown_v2("ошибка при смене настроения."), parse_mode=ParseMode.MARKDOWN_V2)
                 return # Stop processing after handling mood command

            # 7. Decide if the bot should respond (mainly for group chats)
            should_ai_respond = True
            ai_decision_response = None
            context_ai_decision_added = False
            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                 should_respond_prompt = persona.format_should_respond_prompt(message_text)
                 if should_respond_prompt:
                     try:
                         logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id_str}...")
                         # Get current context *before* the user message was added for this check? Or include it?
                         # Let's include it for now, as the decision might depend on the latest message.
                         context_for_should_respond = get_context_for_chat_bot(db, persona.chat_instance.id)
                         ai_decision_response = await send_to_langdock(
                             system_prompt=should_respond_prompt,
                             messages=context_for_should_respond # Send current context
                         )
                         answer = ai_decision_response.strip().lower()
                         logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")

                         # Interpret the AI's decision
                         if answer.startswith("да"):
                             should_ai_respond = True
                         elif answer.startswith("нет"):
                              # Add a small random chance to respond anyway
                              if random.random() < 0.05: # 5% chance to respond even if AI says no
                                  logger.info(f"Responding randomly despite AI='{answer}'.")
                                  should_ai_respond = True
                              else:
                                  should_ai_respond = False
                         else:
                              # If AI gives unclear answer, default to responding
                              logger.warning(f"Unclear should_respond answer '{answer}'. Defaulting to respond.")
                              should_ai_respond = True

                         # Add the AI's decision process to context if needed for debugging/analysis
                         # Note: This adds more tokens to the context history.
                         if ai_decision_response and persona.chat_instance:
                             try:
                                 # Maybe use a different role like 'system' or 'internal'? Using 'assistant' for now.
                                 add_message_to_context(db, persona.chat_instance.id, "assistant", f"[Decision: {answer}]")
                                 context_ai_decision_added = True
                                 logger.debug("Added AI decision response to context (pending commit).")
                             except Exception as e_ctx_dec:
                                 logger.error(f"Failed to add AI decision to context: {e_ctx_dec}")

                     except Exception as e:
                          # If should_respond logic fails, default to responding
                          logger.error(f"Error in should_respond logic: {e}", exc_info=True)
                          should_ai_respond = True
                 else:
                     # If no should_respond prompt is defined, always respond in groups
                     should_ai_respond = True

            # If decided not to respond, commit context and return
            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic.")
                 db.commit() # Commit user message context, limit updates, and potentially AI decision context
                 return

            # 8. Get context again (including user message and potentially AI decision) for main response
            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                     logger.error(f"DB Error getting context for AI main response: {e_ctx}", exc_info=True)
                     await update.message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else: # Should not happen
                 logger.error("Cannot get context for AI main response, chat_instance is None.")
                 db.rollback()
                 return

            # 9. Format the main system prompt
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                # Handle case where prompt formatting fails
                logger.error(f"System prompt formatting failed for persona {persona.name}.")
                await update.message.reply_text(escape_markdown_v2("ошибка при подготовке ответа."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback()
                return

            # 10. Send to Langdock for the main response
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received main response from Langdock: {response_text[:100]}...")

            # 11. Process and send the response(s) back to Telegram
            # <<< ИЗМЕНЕНО: Передаем message_id для ответа >>>
            context_response_prepared = await process_and_send_response(
                update, context, chat_id_str, persona, response_text, db, reply_to_message_id=message_id
            )

            # 12. Commit all changes for this message interaction
            db.commit()
            logger.debug(f"Committed DB changes for handle_message chat {chat_id_str} (LimitUpdated: {limit_state_updated}, UserMsgAdded: {context_user_msg_added}, AIDecisionAdded: {context_ai_decision_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
             try: await update.message.reply_text(escape_markdown_v2("ошибка базы данных, попробуйте позже."), parse_mode=ParseMode.MARKDOWN_V2)
             except Exception: pass
             db.rollback() # Rollback on DB error
        except TelegramError as e:
             # Handle Telegram API errors during the process
             logger.error(f"Telegram API error during handle_message for chat {chat_id_str}: {e}", exc_info=True)
             # No rollback needed usually, but log the error
        except Exception as e:
            # Catch any other unexpected errors
            logger.error(f"General error processing message in chat {chat_id_str}: {e}", exc_info=True)
            try: await update.message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка."), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
            db.rollback() # Rollback on general errors


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Handles incoming photo or voice messages."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_id = update.message.message_id # <<< ДОБАВЛЕНО: Получаем ID сообщения для reply
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id_str} (MsgID: {message_id})")

    # 1. Check channel subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    with next(get_db()) as db:
        try:
            # 2. Get active persona and owner
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona in chat {chat_id_str} for media message.")
                return # No active persona
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            # 3. Check limits
            limit_ok = check_and_update_user_limits(db, owner_user)
            limit_state_updated = db.is_modified(owner_user)

            if not limit_ok:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media.")
                await send_limit_exceeded_message(update, context, owner_user)
                if limit_state_updated:
                    db.commit()
                return

            # 4. Determine prompt template and context placeholder based on media type
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
                 # Should not happen if called correctly
                 logger.error(f"Unsupported media_type '{media_type}' in handle_media")
                 db.rollback()
                 return

            # 5. Add placeholder message to context
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
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа."), parse_mode=ParseMode.MARKDOWN_V2)
                     db.rollback()
                     return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать медиа с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
                 db.rollback()
                 return

            # 6. Check if muted
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id_str}. Media saved to context, but ignoring response.")
                db.commit() # Commit context placeholder and limit update
                return

            # 7. Check if a prompt template exists for this media type
            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id_str} has no {media_type} template. Skipping response.")
                db.commit() # Commit context placeholder and limit update
                return

            # 8. Get context for AI
            context_for_ai = []
            if persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                except (SQLAlchemyError, Exception) as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа на медиа."), parse_mode=ParseMode.MARKDOWN_V2)
                    db.rollback()
                    return
            else: # Should not happen
                 logger.error("Cannot get context for AI media response, chat_instance is None.")
                 db.rollback()
                 return

            # 9. Format the specific media prompt
            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit() # Commit context placeholder and limit update
                 return

            # 10. Send to Langdock
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            # 11. Process and send response
            # <<< ИЗМЕНЕНО: Передаем message_id для ответа >>>
            context_response_prepared = await process_and_send_response(
                update, context, chat_id_str, persona, response_text, db, reply_to_message_id=message_id
            )

            # 12. Commit all changes
            db.commit()
            logger.debug(f"Committed DB changes for handle_media chat {chat_id_str} (LimitUpdated: {limit_state_updated}, PlaceholderAdded: {context_placeholder_added}, BotRespAdded: {context_response_prepared})")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка базы данных."), parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except TelegramError as e:
             logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка."), parse_mode=ParseMode.MARKDOWN_V2)
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

    # <<< ДОБАВЛЕНО: Логирование перед проверкой подписки >>>
    logger.debug(f"/start: Checking channel subscription for user {user_id}...")
    # Check subscription before proceeding
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    logger.debug(f"/start: Channel subscription check passed for user {user_id}.")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    reply_text_final = ""
    reply_markup = ReplyKeyboardRemove()
    # Variables to hold raw data for potential fallback message
    status_raw = ""
    expires_raw = ""
    persona_limit_raw = ""
    message_limit_raw = ""

    try:
        with next(get_db()) as db:
            # Get or create user, commit immediately to ensure user exists
            user = get_or_create_user(db, user_id, username)
            if db.is_modified(user): # Commit only if user was created or modified
                logger.info(f"/start: Committing new/updated user {user_id}.")
                db.commit()
                db.refresh(user) # Refresh to get latest state after commit
            else:
                logger.debug(f"/start: User {user_id} already exists and is up-to-date.")

            # Check if a persona is already active in this specific chat
            logger.debug(f"/start: Checking for active persona in chat {chat_id_str}...")
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if persona_info_tuple:
                # Persona active in this chat
                persona, _, _ = persona_info_tuple
                logger.info(f"/start: Persona '{persona.name}' is active in chat {chat_id_str}.")
                part1_raw = f"привет! я {persona.name}. я уже активен в этом чате.\n"
                part2_raw = "используй /help или /menu для списка команд."
                reply_text_final = escape_markdown_v2(part1_raw + part2_raw)
                reply_markup = ReplyKeyboardRemove() # No keyboard needed if bot active
            else:
                # No persona active, show general welcome and user status
                logger.info(f"/start: No active persona in chat {chat_id_str}. Showing welcome message.")
                # Reload user with persona_configs relationship for accurate count
                # No need to reload if already refreshed after creation/update
                if not db.is_modified(user): # Reload only if not just created/updated
                    user = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user.id).one()

                # Reset daily limit if needed (can be done here or via scheduled task)
                now = datetime.now(timezone.utc)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if not user.last_message_reset or user.last_message_reset < today_start:
                    logger.info(f"/start: Resetting daily limit for user {user_id}.")
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit() # Commit the reset
                    db.refresh(user) # Refresh again after reset

                # Prepare status strings (raw)
                status_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_raw = ""
                if user.is_active_subscriber and user.subscription_expires_at:
                     # Check if expiry is in the far future (admin/permanent)
                     if user.subscription_expires_at > now + timedelta(days=365*10):
                         expires_raw = " (бессрочно)"
                     else:
                         expires_raw = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}"

                persona_count = len(user.persona_configs) if user.persona_configs else 0
                persona_limit_raw = f"{persona_count}/{user.persona_limit}"
                message_limit_raw = f"{user.daily_message_count}/{user.message_limit}"

                # Construct welcome message (raw) - Removed backticks
                start_text_raw = (
                    f"привет! 👋 я бот для создания ai-собеседников (@NunuAiBot).\n\n"
                    f"твой статус: {status_raw}{expires_raw}\n"
                    f"личности: {persona_limit_raw} | сообщения: {message_limit_raw}\n\n"
                    f"начало работы:\n"
                    f"/createpersona <имя> - создай ai-личность.\n"
                    f"/mypersonas - посмотри своих личностей и управляй ими.\n"
                    f"/menu - панель управления командами.\n"
                    f"/profile - детали статуса | /subscribe - узнать о подписке"
                 )
                # Escape for MarkdownV2
                reply_text_final = escape_markdown_v2(start_text_raw)

                # Add menu button
                keyboard = [[InlineKeyboardButton("🚀 Меню Команд (/menu)", callback_data="show_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            # Send the final message
            logger.debug(f"/start: Sending final message to user {user_id}.")
            await update.message.reply_text(reply_text_final, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "ошибка при загрузке данных. попробуй позже."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass
    except TelegramError as e:
        logger.error(f"Telegram error during /start for user {user_id}: {e}", exc_info=True)
        # Handle potential Markdown parsing errors specifically
        if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
            logger.error(f"--> Failed text (escaped): '{reply_text_final[:500]}...'")
            # Fallback to plain text using the raw variables
            try:
                fallback_text_raw = (
                     f"привет! 👋 я бот для создания ai-собеседников (@NunuAiBot).\n\n"
                     f"твой статус: {status_raw}{expires_raw}\n"
                     f"личности: {persona_limit_raw} | сообщения: {message_limit_raw}\n\n"
                     f"начало работы:\n"
                     f"/createpersona <имя> - создай ai-личность.\n"
                     f"/mypersonas - посмотри своих личностей и управляй ими.\n"
                     f"/menu - панель управления командами.\n"
                     f"/profile - детали статуса | /subscribe - узнать о подписке"
                ) if status_raw else "Привет! Произошла ошибка отображения стартового сообщения. Используй /help или /menu."

                await update.message.reply_text(fallback_text_raw, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                 logger.error(f"Failed sending fallback start message: {fallback_e}")
        else:
            # Handle other Telegram errors
            error_msg_raw = "произошла ошибка при обработке команды /start."
            try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "произошла ошибка при обработке команды /start."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command and the show_help callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    # Check subscription only if it's a command, not a callback from menu
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # <<< ИЗМЕНЕНО: Экранируем все зарезервированные символы >>>
    help_text_escaped = escape_markdown_v2("""
*Помощь по командам:*

*Основные:*
/start - Приветствие и ваш статус
/help - Эта справка
/menu - Панель управления командами
/placeholders - Список плейсхолдеров для промптов

*Личности:*
/createpersona <имя> [описание] - Создать
/mypersonas - Список и управление
/editpersona <id> - Редактировать
/deletepersona <id> - Удалить

*Аккаунт:*
/profile - Статус и лимиты
/subscribe - Информация о подписке

*В чате (с активной личностью):*
/addbot <id> - Добавить личность в чат
/mood [настроение] - Сменить настроение
/reset - Очистить память личности
/mutebot - Запретить отвечать
/unmutebot - Разрешить отвечать
""")
    # Use the escaped text
    help_text_to_send = help_text_escaped

    # Add back button only if it's a callback
    keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()

    try:
        if is_callback:
            query = update.callback_query
            # Edit message only if text or markup differs
            if query.message.text != help_text_to_send or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(help_text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer() # Answer silently if message is already correct
        else:
            # Send as a new message if it's a command
            await update.message.reply_text(help_text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Help message not modified, skipping edit.")
            await query.answer()
        else:
            # Handle other BadRequests (like parsing errors)
            logger.error(f"Failed sending/editing help message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed help text (escaped): '{help_text_to_send[:200]}...'") # Log the escaped text
            try:
                # Fallback: send the raw text without Markdown parsing
                # <<< ИЗМЕНЕНО: Используем неэкранированный текст для fallback >>>
                help_text_raw_no_md = """
Помощь по командам:

Основные:
/start - Приветствие и ваш статус
/help - Эта справка
/menu - Панель управления командами
/placeholders - Список плейсхолдеров для промптов

Личности:
/createpersona <имя> [описание] - Создать
/mypersonas - Список и управление
/editpersona <id> - Редактировать
/deletepersona <id> - Удалить

Аккаунт:
/profile - Статус и лимиты
/subscribe - Информация о подписке

В чате (с активной личностью):
/addbot <id> - Добавить личность в чат
/mood [настроение] - Сменить настроение
/reset - Очистить память личности
/mutebot - Запретить отвечать
/unmutebot - Разрешить отвечать
"""
                await context.bot.send_message(chat_id=chat_id_str, text=help_text_raw_no_md, reply_markup=reply_markup, parse_mode=None)
                # If it was a callback, try to delete the old message to avoid confusion
                if is_callback:
                    try: await query.delete_message()
                    except: pass # Ignore deletion errors
            except Exception as fallback_e:
                logger.error(f"Failed sending plain help message: {fallback_e}")
                if is_callback: await query.answer("Ошибка отображения справки", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing help message: {e}", exc_info=True)
         if is_callback: await query.answer("Ошибка отображения справки", show_alert=True)

# <<< ДОБАВЛЕНО: Команда для отображения плейсхолдеров >>>
async def placeholders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays available placeholders for prompts."""
    if not update.message: return
    user_id = update.effective_user.id
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /placeholders < User {user_id} in Chat {chat_id_str}")

    # Check subscription before proceeding
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # <<< ИЗМЕНЕНО: Текст написан с учетом MarkdownV2 и затем экранирован >>>
    text = escape_markdown_v2("""
*Доступные плейсхолдеры для промптов:*

Эти плейсхолдеры можно использовать в полях "Системный промпт", "Промпт 'Отвечать?'", "Промпт спама", "Промпт фото" и "Промпт голоса" при редактировании личности. Бот автоматически заменит их на актуальные значения при генерации ответа.

`{persona_name}` - Имя вашей личности.
`{persona_description}` - Полное описание личности.
`{persona_description_short}` - Краткое описание личности (первое предложение или ~50 символов).
`{mood_prompt}` - Текст промпта для текущего настроения личности.
`{time_info}` - Текущее время в разных часовых поясах (UTC, МСК и др.).
`{internet_info}` - Напоминание AI о доступе к интернету (используется в системном промпте).
`{username}` - Имя пользователя Telegram, отправившего сообщение.
`{user_id}` - ID пользователя Telegram, отправившего сообщение.
`{chat_id}` - ID текущего чата.
`{message}` - Текст сообщения, на которое отвечает бот (только в Системном промпте и Промпте 'Отвечать?').

*Пример использования в Системном промпте:*
`Ты - {persona_name}, {persona_description_short}. Твое настроение: {mood_prompt}. Сейчас {time_info}. Пользователь {username} написал: {message}`
""")

    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed sending placeholders message: {e}", exc_info=True)
        # Fallback to plain text
        plain_text = """
Доступные плейсхолдеры для промптов:

Эти плейсхолдеры можно использовать в полях "Системный промпт", "Промпт 'Отвечать?'", "Промпт спама", "Промпт фото" и "Промпт голоса" при редактировании личности. Бот автоматически заменит их на актуальные значения при генерации ответа.

{persona_name} - Имя вашей личности.
{persona_description} - Полное описание личности.
{persona_description_short} - Краткое описание личности (первое предложение или ~50 символов).
{mood_prompt} - Текст промпта для текущего настроения личности.
{time_info} - Текущее время в разных часовых поясах (UTC, МСК и др.).
{internet_info} - Напоминание AI о доступе к интернету (используется в системном промпте).
{username} - Имя пользователя Telegram, отправившего сообщение.
{user_id} - ID пользователя Telegram, отправившего сообщение.
{chat_id} - ID текущего чата.
{message} - Текст сообщения, на которое отвечает бот (только в Системном промпте и Промпте 'Отвечать?').

Пример использования в Системном промпте:
Ты - {persona_name}, {persona_description_short}. Твое настроение: {mood_prompt}. Сейчас {time_info}. Пользователь {username} написал: {message}
"""
        try:
            await update.message.reply_text(plain_text, parse_mode=None)
        except Exception as fallback_e:
            logger.error(f"Failed sending plain placeholders message: {fallback_e}")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /menu command and the show_menu callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /menu or Callback 'show_menu' < User {user_id} in Chat {chat_id_str}")

    # Check subscription only if it's a command
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # Prepare menu text and keyboard
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
            # Edit only if content differs
            if query.message.text != menu_text_escaped or query.message.reply_markup != reply_markup:
                await query.edit_message_text(menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer() # Answer silently
        else:
            # Send new message for command
            await context.bot.send_message(chat_id=chat_id_str, text=menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Menu message not modified, skipping edit.")
            await query.answer()
        else:
            # Handle other BadRequests
            logger.error(f"Failed sending/editing menu message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed menu text (escaped): '{menu_text_escaped[:200]}...'")
            # Fallback to plain text
            try:
                await context.bot.send_message(chat_id=chat_id_str, text=menu_text_raw, reply_markup=reply_markup, parse_mode=None)
                if is_callback:
                    try: await query.delete_message()
                    except: pass
            except Exception as fallback_e:
                logger.error(f"Failed sending plain menu message: {fallback_e}")
                if is_callback: await query.answer("Ошибка отображения меню", show_alert=True)
    except Exception as e:
         logger.error(f"Error sending/editing menu message: {e}", exc_info=True)
         if is_callback: await query.answer("Ошибка отображения меню", show_alert=True)


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

    # Check subscription only for command, not callback
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # --- Setup DB Session and Persona ---
    close_db_later = False
    db_session = db # Use passed session if available (e.g., from handle_message)
    chat_bot_instance = None
    local_persona = persona # Use passed persona if available

    # Define error messages (escaped)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности.")
    error_persona_info = escape_markdown_v2("Ошибка: не найдена информация о личности.")
    error_no_moods_fmt_raw = "у личности '{persona_name}' не настроены настроения." # Raw for formatting
    error_bot_muted_fmt_raw = "личность '{persona_name}' сейчас заглушена (/unmutebot)." # Raw for formatting
    error_db = escape_markdown_v2("ошибка базы данных при смене настроения.")
    error_general = escape_markdown_v2("ошибка при обработке команды /mood.")

    try:
        # Get DB session if not passed
        if db_session is None:
            db_context = get_db()
            db_session = next(db_context)
            close_db_later = True

        # Get Persona if not passed
        if local_persona is None:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db_session)
            if not persona_info_tuple:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                try:
                    if is_callback: await update.callback_query.answer("Нет активной личности", show_alert=True)
                    await reply_target.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                logger.debug(f"No active persona for chat {chat_id_str}. Cannot set mood.")
                # Close session if opened locally
                if close_db_later: db_session.close()
                return
            local_persona, _, _ = persona_info_tuple

        # Ensure persona and chat_instance are valid
        if not local_persona or not local_persona.chat_instance:
             logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id_str}.")
             reply_target = update.callback_query.message if is_callback else message_or_callback_msg
             if is_callback: await update.callback_query.answer("Ошибка: не найдена информация о личности.", show_alert=True)
             else: await reply_target.reply_text(error_persona_info, parse_mode=ParseMode.MARKDOWN_V2)
             if close_db_later: db_session.close()
             return

        chat_bot_instance = local_persona.chat_instance
        persona_name_raw = local_persona.name # Raw name for formatting messages

        # Check if muted
        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{persona_name_raw}' is muted in chat {chat_id_str}. Ignoring mood command.")
            reply_text = escape_markdown_v2(error_bot_muted_fmt_raw.format(persona_name=persona_name_raw))
            try:
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        # Get available moods
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

        # --- Determine Target Mood ---
        available_moods_lower = {m.lower(): m for m in available_moods} # Map lower case to original case
        mood_arg_lower = None
        target_mood_original_case = None

        # From callback query (e.g., "set_mood_Радость_123")
        if is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             # Ensure format is set_mood_<encoded_name>_<persona_id>
             if len(parts) >= 3 and parts[-1].isdigit():
                  try:
                      # Join parts between "set_mood_" and the final ID
                      encoded_mood_name = "_".join(parts[2:-1])
                      decoded_mood_name = urllib.parse.unquote(encoded_mood_name)
                      mood_arg_lower = decoded_mood_name.lower()
                      # Find the original case from the available moods
                      if mood_arg_lower in available_moods_lower:
                          target_mood_original_case = available_moods_lower[mood_arg_lower]
                  except Exception as decode_err:
                      logger.error(f"Error decoding mood name from callback {update.callback_query.data}: {decode_err}")
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
        # From command arguments or direct text message
        elif not is_callback:
            mood_text = ""
            if context.args: # Check command arguments first
                 mood_text = " ".join(context.args)
            elif update.message and update.message.text: # Check if the whole message matches a mood
                 possible_mood = update.message.text.strip()
                 if possible_mood.lower() in available_moods_lower:
                      mood_text = possible_mood

            if mood_text:
                mood_arg_lower = mood_text.lower()
                if mood_arg_lower in available_moods_lower:
                    target_mood_original_case = available_moods_lower[mood_arg_lower]

        # --- Process Mood Change or Show Selection ---
        if target_mood_original_case:
             # Set the mood (commits inside)
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case)
             # Prepare confirmation message
             reply_text_raw = f"настроение для '{persona_name_raw}' теперь: {target_mood_original_case}"
             reply_text = escape_markdown_v2(reply_text_raw)

             try:
                 if is_callback:
                     query = update.callback_query
                     # Edit message to show confirmation, remove keyboard
                     if query.message.text != reply_text or query.message.reply_markup: # Check if change needed
                         await query.edit_message_text(reply_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                     else:
                         await query.answer(f"Настроение: {target_mood_original_case}") # Silent answer if no change
                 else:
                     # Reply to command
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood confirmation (BadRequest): {e} - Text: '{reply_text}'")
                  # Fallback to plain text
                  try:
                       if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=None, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                  except Exception as fe: logger.error(f"Failed sending plain mood confirmation: {fe}")
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {persona_name_raw} in chat {chat_id_str} set to {target_mood_original_case}.")
        else:
             # Show mood selection keyboard
             keyboard = []
             # Sort moods alphabetically for display
             for mood_name in sorted(available_moods, key=str.lower):
                 try:
                     # URL-encode mood name for callback data
                     encoded_mood_name = urllib.parse.quote(mood_name)
                     # Include persona ID in callback to ensure correct context if user interacts later
                     button_callback = f"set_mood_{encoded_mood_name}_{local_persona.id}"
                     # Check callback data length (max 64 bytes)
                     if len(button_callback.encode('utf-8')) <= 64:
                          keyboard.append([InlineKeyboardButton(mood_name.capitalize(), callback_data=button_callback)])
                     else:
                          logger.warning(f"Callback data for mood '{mood_name}' (encoded: '{encoded_mood_name}') too long, skipping button.")
                 except Exception as encode_err:
                     logger.error(f"Error encoding mood name '{mood_name}' for callback: {encode_err}")

             reply_markup = InlineKeyboardMarkup(keyboard)
             # Get current mood for display
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             persona_name_escaped = escape_markdown_v2(persona_name_raw) # Escape name for message

             reply_text = ""
             reply_text_raw = ""
             # If user provided an invalid mood argument
             if mood_arg_lower:
                 mood_arg_escaped = escape_markdown_v2(mood_arg_lower) # Escape the invalid argument
                 reply_text_raw = f"не знаю настроения '{mood_arg_lower}' для '{persona_name_raw}'. выбери из списка:"
                 reply_text = escape_markdown_v2(reply_text_raw)
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
             else: # If no mood argument was provided (/mood command)
                 reply_text_raw = f"текущее настроение: {current_mood_text}. выбери новое для '{persona_name_raw}':"
                 reply_text = escape_markdown_v2(reply_text_raw)
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id_str}.")

             # Send or edit the message with the keyboard
             try:
                 if is_callback:
                      query = update.callback_query
                      # Edit only if content or markup differs
                      if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer() # Silent answer
                 else:
                      await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             except BadRequest as e:
                  logger.error(f"Failed sending mood selection (BadRequest): {e} - Text: '{reply_text}'")
                  # Fallback to plain text
                  try:
                       if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
                       else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
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
        # Close the session only if it was opened within this function
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

    # Check subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    # Define user messages (escaped)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности для сброса.")
    error_not_owner = escape_markdown_v2("только владелец личности может сбросить её память.")
    error_no_instance = escape_markdown_v2("ошибка: не найден экземпляр бота для сброса.")
    error_db = escape_markdown_v2("ошибка базы данных при сбросе контекста.")
    error_general = escape_markdown_v2("ошибка при сбросе контекста.")
    success_reset_fmt_raw = "память личности '{persona_name}' в этом чате очищена." # Raw for formatting

    with next(get_db()) as db:
        try:
            # Get active persona and owner
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db)
            if not persona_info_tuple:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return
            persona, _, owner_user = persona_info_tuple

            # Check ownership
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id_str}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Get the ChatBotInstance
            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 # This should not happen if persona_info_tuple was found
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id_str}")
                 await update.message.reply_text(error_no_instance, parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Delete context using the dynamic relationship's delete method
            # synchronize_session='fetch' might be safer if other operations happen before commit
            deleted_count_result = chat_bot_instance.context.delete(synchronize_session='fetch')
            # Ensure deleted_count is an integer
            deleted_count = deleted_count_result if isinstance(deleted_count_result, int) else 0
            db.commit() # Commit the deletion
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id_str} by user {user_id}.")
            # Send success message
            final_success_msg = escape_markdown_v2(success_reset_fmt_raw.format(persona_name=persona.name))
            await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback() # Rollback on error
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback() # Rollback on error


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /createpersona command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    # Check subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # Define user messages (escaped and raw for formatting)
    usage_text = escape_markdown_v2("формат: /createpersona <имя> [описание]\n_имя обязательно, описание нет._")
    error_name_len = escape_markdown_v2("имя личности: 2-50 символов.")
    error_desc_len = escape_markdown_v2("описание: до 1500 символов.")
    error_limit_reached_fmt_raw = "упс! достигнут лимит личностей ({current_count}/{limit}) для статуса {status_text}. 😟\nчтобы создавать больше, используй /subscribe"
    error_name_exists_fmt_raw = "личность с именем '{persona_name}' уже есть. выбери другое."
    success_create_fmt_raw = "✅ личность '{name}' создана!\nID: {id}\nописание: {description}\n\nдобавь в чат или управляй через /mypersonas"
    error_db = escape_markdown_v2("ошибка базы данных при создании личности.")
    error_general = escape_markdown_v2("ошибка при создании личности.")

    # Parse arguments
    args = context.args
    if not args:
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return
    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None

    # Validate input length
    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text(error_name_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         return
    if persona_description and len(persona_description) > 1500:
         await update.message.reply_text(error_desc_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
         return

    with next(get_db()) as db:
        try:
            # Get user and check limits
            user = get_or_create_user(db, user_id, username)
            # Commit if user was created, then reload with relationship
            if not user.id:
                db.commit()
                db.refresh(user)
            # Ensure persona_configs relationship is loaded for can_create_persona check
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()

            if not user.can_create_persona:
                 current_count = len(user.persona_configs)
                 limit = user.persona_limit
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")
                 # Format and send limit message
                 status_text_raw = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                 final_limit_msg = escape_markdown_v2(error_limit_reached_fmt_raw.format(
                     current_count=current_count,
                     limit=limit,
                     status_text=status_text_raw
                 ))
                 await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Check if persona name already exists for this user (case-insensitive)
            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                 final_exists_msg = escape_markdown_v2(error_name_exists_fmt_raw.format(persona_name=persona_name))
                 await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # Create the persona (commits inside)
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            # Send success message
            desc_raw = new_persona.description or "(пусто)" # Use raw description for message
            final_success_msg = escape_markdown_v2(success_create_fmt_raw.format(
                name=new_persona.name,
                id=new_persona.id,
                description=desc_raw
                ))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError:
             # Handle the specific case where create_persona_config raises IntegrityError
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             persona_name_escaped = escape_markdown_v2(persona_name)
             error_msg_ie_raw = f"ошибка: личность '{persona_name_escaped}' уже существует (возможно, гонка запросов). попробуй еще раз."
             await update.message.reply_text(escape_markdown_v2(error_msg_ie_raw), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
             # No rollback needed here as create_persona_config handles its own rollback on IntegrityError
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
             # No explicit rollback needed if create_persona_config failed and rolled back
        except BadRequest as e:
             # Handle errors sending messages back to user
             logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
             try: await update.message.reply_text("Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback create_persona error: {fe}")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
             # No explicit rollback needed if create_persona_config failed and rolled back


async def my_personas(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mypersonas command and show_mypersonas callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message = update.message if not is_callback else None

    # Determine user and target message/chat
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

    # Log entry point
    if is_callback:
        logger.info(f"Callback 'show_mypersonas' < User {user_id} ({username}) in Chat {chat_id_str}")
    else:
        logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id_str}")
        # Check subscription only for command
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
        await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # Define user messages
    error_db = escape_markdown_v2("ошибка при загрузке списка личностей.")
    error_general = escape_markdown_v2("произошла ошибка при получении списка личностей.")
    error_user_not_found = escape_markdown_v2("Ошибка: не удалось найти пользователя.")
    info_no_personas_fmt_raw = "у тебя пока нет личностей ({count}/{limit}).\nсоздай: /createpersona <имя>"
    info_list_header_fmt_raw = "твои личности ({count}/{limit}):\n"

    try:
        with next(get_db()) as db:
            # Get user with personas preloaded
            user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()

            # If user not found, try creating (shouldn't usually happen if start is used)
            if not user_with_personas:
                 user_with_personas = get_or_create_user(db, user_id, username)
                 db.commit()
                 db.refresh(user_with_personas)
                 # Reload with relationship after creation
                 user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_with_personas.id).one()
                 if not user_with_personas: # Still not found? Critical error.
                     logger.error(f"User {user_id} not found even after get_or_create/refresh in my_personas.")
                     error_text = error_user_not_found
                     if is_callback: await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
                     else: await message_target.reply_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
                     return

            # Get personas and limits
            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            # --- Build response based on whether personas exist ---
            if not personas:
                # No personas found
                text_to_send = escape_markdown_v2(info_no_personas_fmt_raw.format(count=persona_count, limit=persona_limit))
                # Add back button only for callback
                keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()

                # Send or edit message
                if is_callback:
                    if message_target.text != text_to_send or message_target.reply_markup != reply_markup:
                        await query.edit_message_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                    else: await query.answer() # Silent answer if no change
                else: await message_target.reply_text(text_to_send, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Personas exist, build list with buttons
            text = escape_markdown_v2(info_list_header_fmt_raw.format(count=persona_count, limit=persona_limit))

            keyboard = []
            for p in personas:
                 # Display name and ID
                 button_text = f"👤 {p.name} (ID: {p.id})"
                 # Define callback data for actions
                 edit_cb = f"edit_persona_{p.id}"
                 delete_cb = f"delete_persona_{p.id}"
                 add_cb = f"add_bot_{p.id}"
                 # Check callback data length (max 64 bytes) - important!
                 if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64 or len(add_cb.encode('utf-8')) > 64:
                      logger.warning(f"Callback data for persona {p.id} might be too long, potentially causing issues.")
                 # Add row with persona info (non-clickable) and action buttons
                 keyboard.append([InlineKeyboardButton(button_text, callback_data=f"dummy_{p.id}")]) # Dummy button for display
                 keyboard.append([
                     InlineKeyboardButton("⚙️ Редакт.", callback_data=edit_cb),
                     InlineKeyboardButton("🗑️ Удалить", callback_data=delete_cb),
                     InlineKeyboardButton("➕ В чат", callback_data=add_cb)
                 ])
            # Add back button if it's a callback
            if is_callback:
                keyboard.append([InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send or edit message
            if is_callback:
                 if message_target.text != text or message_target.reply_markup != reply_markup:
                     await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                 else: await query.answer() # Silent answer
            else: await message_target.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during my_personas for user {user_id}: {e}", exc_info=True)
        error_text = error_db
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

    # Get user and chat info
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id_str}"
    local_persona_id = persona_id # Use passed ID if available (e.g., from button)

    # Check subscription only for command
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # Define user messages
    usage_text = escape_markdown_v2("формат: /addbot <id персоны>\nили используй кнопку '➕ В чат' из /mypersonas")
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности.")
    error_invalid_id_cmd = escape_markdown_v2("id личности должен быть числом.")
    error_no_id = escape_markdown_v2("Ошибка: ID личности не определен.")
    error_persona_not_found_fmt_raw = "личность с id {id} не найдена или не твоя."
    error_already_active_fmt_raw = "личность '{name}' уже активна в этом чате."
    success_added_structure_raw = "✅ личность '{name}' (id: {id}) активирована в этом чате! Память очищена."
    error_link_failed = escape_markdown_v2("не удалось активировать личность (ошибка связывания).")
    error_integrity = escape_markdown_v2("произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
    error_db = escape_markdown_v2("ошибка базы данных при добавлении бота.")
    error_general = escape_markdown_v2("ошибка при активации личности.")

    # --- Determine Persona ID ---
    if is_callback and local_persona_id is None:
         # Extract ID from callback data (e.g., "add_bot_123")
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("Ошибка: неверный ID", show_alert=True)
             return
    elif not is_callback:
         # Extract ID from command arguments
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

    # Final check if persona ID was determined
    if local_persona_id is None:
         logger.error("add_bot_to_chat: persona_id is None after processing input.")
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         if is_callback: await update.callback_query.answer("Ошибка: ID не определен.", show_alert=True)
         else: await reply_target.reply_text(error_no_id, parse_mode=ParseMode.MARKDOWN_V2)
         return

    # Answer callback quickly
    if is_callback:
        await update.callback_query.answer("Добавляем личность...")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            # 1. Verify persona exists and belongs to the user
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 final_not_found_msg = escape_markdown_v2(error_persona_not_found_fmt_raw.format(id=local_persona_id))
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 # Use reply_text for command, edit_message_text for callback might fail if original deleted
                 await reply_target.reply_text(final_not_found_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return

            # 2. Check if any bot is currently active in this chat
            existing_active_link = db.query(ChatBotInstance).options(
                 selectinload(ChatBotInstance.bot_instance_ref).selectinload(BotInstance.persona_config) # Load relations to get name
            ).filter(
                 ChatBotInstance.chat_id == chat_id_str,
                 ChatBotInstance.active == True
            ).first()

            # 3. Handle existing active bot
            if existing_active_link:
                # If the *same* persona is already active, inform user and clear context
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    final_already_active_msg = escape_markdown_v2(error_already_active_fmt_raw.format(name=persona.name))
                    reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                    if is_callback: await update.callback_query.answer(f"'{persona.name}' уже активна", show_alert=True)
                    await reply_target.reply_text(final_already_active_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

                    # Clear context on re-add attempt
                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id_str} on re-add.")
                    deleted_ctx_result = existing_active_link.context.delete(synchronize_session='fetch')
                    deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                    db.commit() # Commit context deletion
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return # Stop processing
                else:
                    # If a *different* persona is active, deactivate it first
                    prev_persona_name = "Неизвестная личность"
                    if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config:
                        prev_persona_name = existing_active_link.bot_instance_ref.persona_config.name
                    else: # Fallback if relations didn't load
                        prev_persona_name = f"ID {existing_active_link.bot_instance_id}"

                    logger.info(f"Deactivating previous bot '{prev_persona_name}' in chat {chat_id_str} before activating '{persona.name}'.")
                    existing_active_link.active = False
                    db.flush() # Flush deactivation before proceeding

            # 4. Find or Create BotInstance for the Persona
            user = persona.owner # Get owner from the loaded persona
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                 # Create BotInstance if it doesn't exist for this persona
                 logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                 try:
                      # Pass owner_id and persona_id, name is optional
                      bot_instance = create_bot_instance(db, user.id, local_persona_id, name=f"Inst:{persona.name}")
                 except (IntegrityError, SQLAlchemyError) as create_err:
                      # Handle potential race condition if another request created it
                      logger.error(f"Failed to create BotInstance ({create_err}), possibly due to concurrent request. Retrying fetch.")
                      db.rollback() # Rollback failed creation attempt
                      bot_instance = db.query(BotInstance).filter(BotInstance.persona_config_id == local_persona_id).first()
                      if not bot_instance:
                           logger.error("Failed to fetch BotInstance even after retry.")
                           raise SQLAlchemyError("Failed to create or fetch BotInstance") # Raise error

            # 5. Link the BotInstance to the Chat (creates or reactivates)
            # This function handles committing the link creation/update
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)

            # 6. Send confirmation message
            if chat_link:
                 final_success_msg = escape_markdown_v2(success_added_structure_raw.format(name=persona.name, id=local_persona_id))
                 await context.bot.send_message(chat_id=chat_id_str, text=final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 # If triggered by callback, delete the original message with buttons
                 if is_callback:
                      try:
                           await update.callback_query.delete_message()
                      except Exception as del_err:
                           logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}, '{persona.name}') to chat {chat_id_str}. ChatBotInstance ID: {chat_link.id}")
            else:
                 # Linking failed
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(error_link_failed, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id_str} - link_bot_instance_to_chat returned None.")

        except IntegrityError as e:
             # Catch potential integrity errors during the process (e.g., unique constraints)
             logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=False)
             await context.bot.send_message(chat_id=chat_id_str, text=error_integrity, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except SQLAlchemyError as e:
             logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_db, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()
        except BadRequest as e:
             # Handle errors sending messages
             logger.error(f"BadRequest sending message in add_bot_to_chat: {e}", exc_info=True)
             try: await context.bot.send_message(chat_id=chat_id_str, text="Произошла ошибка при отправке ответа.", parse_mode=None)
             except Exception as fe: logger.error(f"Failed sending fallback add_bot_to_chat error: {fe}")
        except Exception as e:
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id_str, text=error_general, parse_mode=ParseMode.MARKDOWN_V2)
             db.rollback()


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from inline keyboards."""
    query = update.callback_query
    if not query or not query.data: return

    # Extract basic info
    chat_id_str = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

    # --- Subscription Check ---
    # Decide if subscription check is needed based on callback data prefix
    needs_subscription_check = True
    # Callbacks that DON'T require subscription check (menus, payment flow, cancel, etc.)
    no_check_callbacks = (
        "cancel_edit", "edit_persona_back", "edit_moods_back_cancel",
        "delete_persona_cancel", "view_tos", "subscribe_info",
        "show_help", "dummy_", "confirm_pay", "subscribe_pay",
        "show_menu", "show_profile", "show_mypersonas"
    )
    # Prefixes for conversation handlers (handled separately) or specific actions
    # Note: Conversation handler callbacks are routed by PTB itself, not this function directly,
    # but listing them helps avoid unnecessary checks if logic changes.
    conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood", "set_mood_")

    # Skip check if data matches known non-requiring callbacks/prefixes
    if data.startswith(no_check_callbacks) or any(data.startswith(p) for p in conv_prefixes):
        needs_subscription_check = False

    # Perform check if needed
    if needs_subscription_check:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            try: await query.answer(text="Подпишитесь на канал!", show_alert=True)
            except: pass # Ignore answer errors
            return

    # --- Route callbacks to appropriate handlers ---
    # Use elif structure for clarity
    if data.startswith("set_mood_"):
        # Handled by the mood function
        await mood(update, context)
    elif data == "subscribe_info":
        await query.answer() # Answer immediately
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
        # Handled by add_bot_to_chat function
        await add_bot_to_chat(update, context)
    elif data == "show_help":
        await query.answer()
        await help_command(update, context)
    elif data == "show_menu":
        await query.answer()
        await menu_command(update, context)
    elif data == "show_profile":
        await query.answer()
        await profile(query, context) # Pass CallbackQuery directly
    elif data == "show_mypersonas":
        await query.answer()
        await my_personas(query, context) # Pass CallbackQuery directly
    elif data.startswith("dummy_"):
        # Ignore dummy callbacks used for display purposes
        await query.answer()
    else:
        # Check if it's a known conversation callback prefix (should be handled by ConversationHandler)
        known_conv_prefixes_full = (
            "edit_persona_", "delete_persona_", "edit_field_", "editmood_", "deletemood_",
            "cancel_edit", "edit_persona_back", "edit_moods_back_cancel",
            "deletemood_confirm_", "deletemood_delete_"
            )
        if any(data.startswith(p) for p in known_conv_prefixes_full):
             # Log that it's likely handled by a conversation handler
             logger.debug(f"Callback '{data}' appears to be for a ConversationHandler, skipping direct handling in handle_callback_query.")
             # IMPORTANT: Do NOT answer the query here, as the ConversationHandler needs to process it.
             # await query.answer() # <<< DO NOT ADD THIS HERE
        else:
            # Log unhandled callbacks
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            try:
                 await query.answer("Неизвестное действие") # Inform user
            except Exception as e:
                 logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


async def profile(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows user profile info. Can be triggered by command or callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message = update.message if not is_callback else None

    # Determine user and target message/chat
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

    # Check subscription only for command
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Define user messages
    error_db = escape_markdown_v2("ошибка базы данных при загрузке профиля.")
    error_general = escape_markdown_v2("ошибка при обработке команды /profile.")
    error_user_not_found = escape_markdown_v2("ошибка: пользователь не найден.")

    with next(get_db()) as db:
        try:
            # Get user, preloading personas
            user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            # If user doesn't exist, create them
            if not user_db:
                user_db = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user_db)
                # Reload with relationship after creation
                user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_db.id).one()
                if not user_db: # Still not found? Critical error.
                    logger.error(f"User {user_id} not found after get_or_create/refresh in profile.")
                    await context.bot.send_message(chat_id, error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                    return

            # Check and reset daily limit if necessary
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not user_db.last_message_reset or user_db.last_message_reset < today_start:
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user_db.daily_message_count = 0
                user_db.last_message_reset = now
                db.commit() # Commit reset
                db.refresh(user_db) # Refresh state

            # Prepare profile information (raw)
            is_active_subscriber = user_db.is_active_subscriber
            status_text_raw = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            expires_raw = ""
            if is_active_subscriber and user_db.subscription_expires_at:
                 try:
                     # Format expiry date and time
                     if user_db.subscription_expires_at > now + timedelta(days=365*10):
                         expires_raw = "активна (бессрочно)"
                     else:
                         expires_raw = f"активна до: {user_db.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
                 except AttributeError: # Handle potential None or invalid date
                      expires_raw = "активна (дата истечения некорректна)"
            elif is_active_subscriber: # Subscribed but no expiry date (e.g., admin)
                 expires_raw = "активна (бессрочно)"
            else:
                 expires_raw = "нет активной подписки"

            persona_count = len(user_db.persona_configs) if user_db.persona_configs is not None else 0
            persona_limit_raw = f"{persona_count}/{user_db.persona_limit}"
            msg_limit_raw = f"{user_db.daily_message_count}/{user_db.message_limit}"

            # Construct profile text (raw)
            profile_text_raw = (
                f"👤 Твой профиль\n\n"
                f"Статус: {status_text_raw}\n"
                f"{expires_raw}\n\n"
                f"Лимиты:\n"
                f"Сообщения сегодня: {msg_limit_raw}\n"
                f"Создано личностей: {persona_limit_raw}\n\n"
            )
            # Add promo text if user is free
            if not is_active_subscriber:
                profile_text_raw += "🚀 Хочешь больше? Жми /subscribe или кнопку 'Подписка' в /menu !"

            # Escape for MarkdownV2
            profile_text_escaped = escape_markdown_v2(profile_text_raw)

            # Prepare keyboard (only back button for callback)
            keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if is_callback else None
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            # Send or edit message
            if is_callback:
                if message_target.text != profile_text_escaped or message_target.reply_markup != reply_markup:
                    await query.edit_message_text(profile_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await query.answer() # Silent answer
            else:
                await message_target.reply_text(profile_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
             logger.error(f"Database error during profile for user {user_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
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

    # Check subscription only for command
    if not from_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return

    # Check if Yookassa is configured
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    # Define user messages
    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)")
    text = ""
    reply_markup = None

    if not yookassa_ready:
        # Payment system not configured
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")]] if from_callback else None
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
    else:
        # Payment system ready, show subscription info
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}" # Integer price
        currency_raw = SUBSCRIPTION_CURRENCY
        duration_raw = str(SUBSCRIPTION_DURATION_DAYS)
        paid_limit_raw = str(PAID_DAILY_MESSAGE_LIMIT)
        free_limit_raw = str(FREE_DAILY_MESSAGE_LIMIT)
        paid_persona_raw = str(PAID_PERSONA_LIMIT)
        free_persona_raw = str(FREE_PERSONA_LIMIT)

        # Construct info text (raw)
        text_raw = (
            f"✨ Премиум подписка ({price_raw} {currency_raw}/мес) ✨\n\n"
            f"Получи максимум возможностей:\n✅ "
            f"{paid_limit_raw} сообщений в день (вместо {free_limit_raw})\n✅ "
            f"{paid_persona_raw} личностей (вместо {free_persona_raw})\n✅ полная настройка всех промптов\n✅ создание и редакт. своих настроений\n✅ приоритетная поддержка\n\nПодписка действует {duration_raw} дней."
        )
        # Escape for MarkdownV2
        text = escape_markdown_v2(text_raw)

        # Create keyboard with ToS and Pay buttons
        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            [InlineKeyboardButton("✅ Принять и оплатить", callback_data="confirm_pay")]
        ]
        # Add back button only for callback
        if from_callback:
             keyboard.append([InlineKeyboardButton("⬅️ Назад в Меню", callback_data="show_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    # Send or edit the message
    try:
        if from_callback: # Edit if called from callback
            query = update.callback_query
            if query.message.text != text or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer() # Silent answer
        else: # Send new message if called by command
            await message_to_update_or_reply.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Failed sending subscribe message (BadRequest): {e} - Text Escaped: '{text[:100]}...'")
        # Fallback to plain text
        try:
            if message_to_update_or_reply:
                 # Use raw text for fallback
                 await context.bot.send_message(chat_id=chat_id, text=text_raw if yookassa_ready else error_payment_unavailable, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback subscribe message: {fallback_e}")
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        # If editing failed, try sending a new message as fallback
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

    # Get ToS URL from bot_data (set during startup)
    tos_url = context.bot_data.get('tos_url')
    # Define user messages
    error_tos_link = "Не удалось отобразить ссылку на соглашение." # For answer callback
    error_tos_load = escape_markdown_v2("❌ Не удалось загрузить ссылку на Пользовательское Соглашение. Попробуйте позже.")
    info_tos = escape_markdown_v2("Ознакомьтесь с Пользовательским Соглашением, открыв его по ссылке ниже:")

    if tos_url:
        # URL exists, show button
        keyboard = [
            [InlineKeyboardButton("📜 Открыть Соглашение", url=tos_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")] # Back to subscribe info
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = info_tos
        try:
            # Edit message to show the link button
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                 await query.answer() # Silent answer
        except Exception as e:
            logger.error(f"Failed to show ToS link to user {user_id}: {e}")
            await query.answer(error_tos_link, show_alert=True) # Alert user on error
    else:
        # URL not found (e.g., Telegraph setup failed)
        logger.error(f"ToS URL not found in bot_data for user {user_id}.")
        text = error_tos_load
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            # Edit message to show error
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer() # Silent answer
        except Exception as e:
             logger.error(f"Failed to show ToS error message to user {user_id}: {e}")
             await query.answer("Ошибка загрузки соглашения.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the confirm_pay callback after user agrees to ToS."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    # Check prerequisites again
    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    # Define user messages
    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)")
    info_confirm = escape_markdown_v2(
         "✅ Отлично!\n\n"
         "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "Пользовательским Соглашением."
         "\n\n👇"
    )
    text = ""
    reply_markup = None

    if not yookassa_ready:
        # Payment system failed or became unavailable
        text = error_payment_unavailable
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        # Show final confirmation and payment button
        text = info_confirm
        price_raw = f"{SUBSCRIPTION_PRICE_RUB:.0f}" # Integer price
        currency_raw = SUBSCRIPTION_CURRENCY
        button_text = f"💳 Оплатить {price_raw} {currency_raw}"

        keyboard = [
            [InlineKeyboardButton(button_text, callback_data="subscribe_pay")] # Button to trigger payment link generation
        ]
        # Add ToS link again for reference (non-clickable if already read)
        if tos_url:
             keyboard.append([InlineKeyboardButton("📜 Условия использования (прочитано)", url=tos_url)])
        else:
             # Show error button if ToS URL failed to load previously
             keyboard.append([InlineKeyboardButton("📜 Условия (ошибка загрузки)", callback_data="view_tos")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]) # Back button
        reply_markup = InlineKeyboardMarkup(keyboard)

    # Edit the message
    try:
        if query.message.text != text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True, # Disable preview for ToS link if present
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await query.answer() # Silent answer
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates and sends the Yookassa payment link."""
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    # Define user messages
    error_yk_not_ready = escape_markdown_v2("❌ ошибка: сервис оплаты не настроен правильно.")
    error_yk_config = escape_markdown_v2("❌ ошибка конфигурации платежной системы.")
    error_receipt = escape_markdown_v2("❌ ошибка при формировании данных чека.")
    error_link_get_fmt_raw = "❌ не удалось получить ссылку от платежной системы{status_info}.\nПопробуй позже." # Raw for formatting
    error_link_create_raw = "❌ не удалось создать ссылку для оплаты. {error_detail}\nПопробуй еще раз позже или свяжись с поддержкой." # Raw for formatting
    success_link = escape_markdown_v2(
        "✅ ссылка для оплаты создана!\n\n"
        "нажми кнопку ниже для перехода к оплате. "
        "после успеха подписка активируется (может занять пару минут)."
    )

    text = ""
    reply_markup = None

    # --- Yookassa Configuration Check ---
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly for payment generation.")
        text = error_yk_not_ready
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # --- Configure Yookassa SDK ---
    # It's good practice to configure it just before use, in case keys change
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

    # --- Prepare Payment Data ---
    idempotence_key = str(uuid.uuid4()) # Unique key for each payment request
    payment_description = f"Premium подписка @NunuAiBot на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    # Metadata to identify user upon webhook notification
    payment_metadata = {'telegram_user_id': str(user_id)}
    # Return URL (where user is redirected after payment) - usually back to the bot
    bot_username = context.bot_data.get('bot_username', "NunuAiBot") # Get bot username from context
    return_url = f"https://t.me/{bot_username}"

    # --- Prepare Receipt Data (Required by Russian Law) ---
    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ @{bot_username} на {SUBSCRIPTION_DURATION_DAYS} дней", # Clear description
                "quantity": 1.0,
                "amount": {"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1", # VAT code (1 = No VAT) - adjust if applicable
                "payment_mode": "full_prepayment", # Payment mode
                "payment_subject": "service" # Payment subject (service, commodity, etc.)
            })
        ]
        # Use a placeholder email if real email is not collected
        user_email = f"user_{user_id}@telegram.bot" # Placeholder
        receipt_data = Receipt({
            "customer": {"email": user_email}, # Customer email or phone needed
            "items": receipt_items,
            # "tax_system_code": "1" # Optional: Tax system code if applicable
        })
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        text = error_receipt
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # --- Create Payment Request ---
    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata) \
            .set_receipt(receipt_data) # Add receipt data
        request = builder.build()
        logger.debug(f"Payment request built: {request.json()}") # Log request for debugging

        # Create payment using Yookassa SDK (run in thread to avoid blocking asyncio loop)
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        # --- Process Payment Response ---
        # Check if response is valid and contains confirmation URL
        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             status_info = f" (статус: {payment_response.status})" if payment_response and payment_response.status else ""
             error_message = escape_markdown_v2(error_link_get_fmt_raw.format(status_info=status_info))
             text = error_message
             reply_markup = None
             await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
             return

        # Success - get URL and show button
        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = success_link
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        # Handle errors during payment creation
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        error_detail = ""
        # Try to get more specific error info
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
        elif isinstance(e, httpx.RequestError): # Handle network errors
             error_detail = "Проблема с сетевым подключением к ЮKassa."
        else: # Generic error
             error_detail = "Произошла непредвиденная ошибка."

        # Inform user about the failure
        user_message = escape_markdown_v2(error_link_create_raw.format(error_detail=error_detail))
        try:
            await query.edit_message_text(user_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder - webhooks are handled by the Flask app, not PTB."""
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    # This handler should ideally never be reached if webhooks are set up correctly.
    pass

# --- Conversation Handlers ---

# --- Edit Persona Conversation ---

async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the persona editing conversation (common logic for command/callback)."""
    user_id = update.effective_user.id
    effective_target = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if not effective_target: return ConversationHandler.END # Cannot proceed without a target message
    chat_id = effective_target.chat.id
    is_callback = update.callback_query is not None

    # Check subscription only if started by command
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Clear previous conversation data

    # Define user messages
    error_not_found_fmt_raw = "личность с id {id} не найдена или не твоя."
    # <<< ИЗМЕНЕНО: Экранируем скобки и используем Markdown >>>
    prompt_edit_fmt_raw = "Редактируем *{name}* \\(ID: {id}\\)\n\nВыберите, что изменить:"
    error_db = escape_markdown_v2("ошибка базы данных при начале редактирования.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")

    try:
        with next(get_db()) as db:
            # Find persona by ID and ensure ownership via user's Telegram ID
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id) # Filter by owner's telegram_id
            ).first()

            if not persona_config:
                 # Persona not found or doesn't belong to user
                 final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            # Store persona ID in user_data for conversation context
            context.user_data['edit_persona_id'] = persona_id
            # Generate keyboard with edit options
            keyboard = await _get_edit_persona_keyboard(persona_config)
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Format prompt message
            msg_text = prompt_edit_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id) # Escape name

            # Send or edit the message
            reply_target = update.callback_query.message if is_callback else update.effective_message
            if is_callback:
                 query = update.callback_query
                 try:
                      # Edit only if content differs
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer() # Silent answer
                 except BadRequest as edit_err: # <<< ИЗМЕНЕНО: Ловим BadRequest
                      # If editing fails (e.g., message too old or parse error), send a new message
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                 except Exception as edit_err:
                      logger.error(f"Unexpected error editing message for edit start (persona {persona_id}): {edit_err}", exc_info=True)
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2) # Fallback send
            else:
                 # Send new message for command
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
        return EDIT_PERSONA_CHOICE # Transition to choice state
    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END # End conversation on DB error
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END # End conversation on other errors

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")

    # Define user messages
    usage_text = escape_markdown_v2("укажи id личности: /editpersona <id>\nили используй кнопку из /mypersonas")
    error_invalid_id = escape_markdown_v2("ID должен быть числом.")

    # Validate arguments
    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Call the common start logic
    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for edit persona button press."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...") # Quick feedback

    # Define user message
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности в кнопке.")

    # Extract persona ID from callback data (e.g., "edit_persona_123")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        # Call the common start logic
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        try:
            # Try to edit the message to show the error
            await query.edit_message_text(error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with invalid ID error: {e}")
        return ConversationHandler.END # End conversation

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's choice of which field to edit or action to take."""
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE # Stay in this state if no data

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id # <<< ДОБАВЛЕНО: Получаем chat_id для отправки новых сообщений

    logger.info(f"--- edit_persona_choice: User {user_id}, PersonaID={persona_id}, Callback data={data} ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна (нет id). начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("ошибка базы данных при проверке данных.")
    error_general = escape_markdown_v2("Непредвиденная ошибка.")
    info_premium_mood = "⭐ Редактирование настроений доступно по подписке"
    info_premium_field_fmt_raw = "⭐ Поле '{field_name}' доступно по подписке"
    # <<< ИЗМЕНЕНО: Улучшен промпт для шаблонных полей >>>
    prompt_edit_template_fmt_raw = "✏️ Редактируем: *{field_name}*\n\n_{field_desc}_\n\nИспользуй плейсхолдеры \\(см\\. `/placeholders`\\)\\. Отправь `.` чтобы оставить текущее значение без изменений\\."
    # Keep current value for max messages
    prompt_edit_max_msg_fmt_raw = "✏️ Редактируем: *{field_name}*\n\n_{field_desc}_\n\nТекущее: *{current_value}*\n\nОтправь новое значение \\(число от 1 до 10\\):"
    # <<< ИЗМЕНЕНО: Улучшен формат для простых полей с Markdown >>>
    prompt_edit_simple_value_fmt_raw = "✏️ Редактируем: *{field_name}*\n\n_{field_desc}_\n\nТекущее значение:\n```\n{current_value}\n```\nОтправь новое значение:"


    # Check if persona_id exists in context
    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         # <<< ИЗМЕНЕНО: Отправляем новое сообщение вместо редактирования >>>
         await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END

    # --- Fetch Persona and Check Premium Status ---
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
                # <<< ИЗМЕНЕНО: Отправляем новое сообщение вместо редактирования >>>
                await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear() # Clear invalid state
                return ConversationHandler.END

            # Check if owner is premium
            owner = persona_config.owner
            # Added logging for premium check
            logger.info(f"Checking premium status for user {user_id} in edit_persona_choice. is_subscribed={owner.is_subscribed if owner else 'N/A'}, expires_at={owner.subscription_expires_at if owner else 'N/A'}, is_admin={is_admin(user_id)}")
            if owner:
                 is_premium_user = owner.is_active_subscriber or is_admin(user_id)
            else: # Should not happen if loaded correctly
                 logger.warning(f"Owner not loaded for persona {persona_id} in edit_persona_choice check. Assuming non-premium.")
                 is_premium_user = False
            logger.info(f"User {user_id} premium status determined as: {is_premium_user}")


    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         # <<< ИЗМЕНЕНО: Отправляем новое сообщение вместо редактирования >>>
         await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return EDIT_PERSONA_CHOICE # Stay in choice state on DB error
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.answer("Непредвиденная ошибка", show_alert=True)
         # <<< ИЗМЕНЕНО: Отправляем новое сообщение вместо редактирования >>>
         await context.bot.send_message(chat_id, error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         return EDIT_PERSONA_CHOICE # Stay in choice state

    # --- Route based on callback data ---
    if data == "cancel_edit":
        # Call cancel handler
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        # Check premium status for mood editing
        if not is_premium_user:
             logger.info(f"User {user_id} (non-premium: {is_premium_user}) attempted to edit moods for persona {persona_id}.") # Added logging
             await query.answer(info_premium_mood, show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay in choice state
        else:
             # Proceed to mood editing menu
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             await query.answer()
             # Pass the already loaded persona_config to avoid re-fetching
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        # Extract field name from callback data (e.g., "edit_field_name" -> "name")
        field = data.replace("edit_field_", "")
        field_info = FIELD_INFO.get(field)
        if not field_info:
            logger.error(f"Invalid field '{field}' requested for edit by user {user_id}")
            await query.answer("Неизвестное поле", show_alert=True)
            return EDIT_PERSONA_CHOICE # Stay in choice state

        field_display_name_plain = field_info["label"]
        field_desc_plain = field_info["desc"]

        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        # Check premium status for advanced fields
        advanced_fields = ["system_prompt_template", "should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user:
             logger.info(f"User {user_id} (non-premium: {is_premium_user}) attempted to edit premium field '{field}' for persona {persona_id}.") # Added logging
             await query.answer(info_premium_field_fmt_raw.format(field_name=field_display_name_plain), show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay in choice state

        # Store the field being edited in user_data
        context.user_data['edit_field'] = field
        # Prepare back button for the *new* message
        # <<< ИЗМЕНЕНО: Кнопка "Отмена" вместо "Назад" для запроса значения >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit") # <<< ИЗМЕНЕНО: callback_data на cancel_edit
        reply_markup = InlineKeyboardMarkup([[cancel_button]])

        await query.answer() # Answer callback quickly
        next_state = EDIT_FIELD # Default next state

        # Format the prompt message based on field type
        if field == "max_response_messages":
            current_value = getattr(persona_config, field, 3) # Get current value or default
            # Format prompt for integer input
            final_prompt = prompt_edit_max_msg_fmt_raw.format(
                field_name=escape_markdown_v2(field_display_name_plain),
                field_desc=escape_markdown_v2(field_desc_plain),
                current_value=escape_markdown_v2(str(current_value))
                )
            next_state = EDIT_MAX_MESSAGES # Transition to max messages state
        elif field in ["name", "description"]:
            current_value_raw = getattr(persona_config, field, "") or "(пусто)" # Get current value or placeholder
            # Use specific format with current value (already escaped in the format string)
            final_prompt = prompt_edit_simple_value_fmt_raw.format(
                field_name=escape_markdown_v2(field_display_name_plain), # Escape parts separately
                field_desc=escape_markdown_v2(field_desc_plain),
                current_value=escape_markdown_v2(current_value_raw) # Escape the value itself
                )
            next_state = EDIT_FIELD # Transition to field editing state
        # Handle template fields - DO NOT show current value, but explain placeholders
        else:
            final_prompt = prompt_edit_template_fmt_raw.format(
                field_name=escape_markdown_v2(field_display_name_plain),
                field_desc=escape_markdown_v2(field_desc_plain)
                )
            next_state = EDIT_FIELD # Transition to field editing state

        # <<< ИЗМЕНЕНО: Отправляем новое сообщение вместо редактирования >>>
        logger.debug(f"Sending NEW prompt message for field '{field}' to user {user_id}. Next state: {next_state}")
        try:
            # Try deleting the previous menu message first for cleaner look
            try:
                await query.delete_message()
                logger.debug("Deleted previous menu message.")
            except Exception as del_err:
                logger.warning(f"Could not delete previous menu message: {del_err}")

            # Send the new prompt message
            await context.bot.send_message(
                chat_id=chat_id,
                text=final_prompt,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            logger.debug(f"Successfully sent NEW prompt message for field '{field}'.")
        except BadRequest as e:
            logger.error(f"BadRequest sending NEW prompt message for field '{field}': {e}")
            logger.error(f"--> Failed prompt text (raw format used): '{final_prompt[:500]}...'") # Log the potentially problematic text
            # Try sending plain text as fallback
            try:
                plain_text_prompt = f"Отправь новое значение для поля '{field_display_name_plain}'.\n\n{field_desc_plain}"
                if field == "max_response_messages": plain_text_prompt += f"\nТекущее: {getattr(persona_config, field, 3)}"
                elif field in ["name", "description"]: plain_text_prompt += f"\nТекущее:\n{getattr(persona_config, field, '') or '(пусто)'}"
                elif field.endswith("_template"): plain_text_prompt += "\n(Используй плейсхолдеры, см. /placeholders. Отправь '.' чтобы пропустить)."

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=plain_text_prompt,
                    reply_markup=reply_markup,
                    parse_mode=None
                )
            except Exception as plain_e:
                logger.error(f"Failed sending even plain text NEW prompt message for field '{field}': {plain_e}")
                # If sending prompt fails critically, maybe end conversation?
                await context.bot.send_message(chat_id, escape_markdown_v2("Ошибка отображения запроса."), parse_mode=ParseMode.MARKDOWN_V2)
                return await _try_return_to_edit_menu(update, context, user_id, persona_id) # Try to recover
        except Exception as e:
            logger.error(f"Unexpected error sending NEW prompt message for field '{field}': {e}", exc_info=True)
            await context.bot.send_message(chat_id, escape_markdown_v2("Ошибка отображения запроса."), parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_edit_menu(update, context, user_id, persona_id) # Try to recover

        return next_state # Transition to the determined state

    if data == "edit_persona_back":
         # Handle back button press from field/mood input state
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         await query.answer()
         # Regenerate the main edit menu
         keyboard = await _get_edit_persona_keyboard(persona_config)
         prompt_edit_back_raw = "Редактируем *{name}* \\(ID: {id}\\)\n\nВыберите, что изменить:" # Escaped parentheses
         final_back_msg = prompt_edit_back_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
         # <<< ИЗМЕНЕНО: Редактируем сообщение, т.к. это возврат к меню >>>
         await query.edit_message_text(final_back_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
         # Clear temporary state
         context.user_data.pop('edit_field', None)
         return EDIT_PERSONA_CHOICE # Return to choice state

    # Fallback for unhandled data in this state
    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.answer("Неизвестный выбор", show_alert=True)
    return EDIT_PERSONA_CHOICE # Stay in choice state

async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the new value for a text field being edited."""
    # <<< ДОБАВЛЕНО: Логирование входа в функцию >>>
    logger.debug(f"Entering edit_field_update...")
    if not update.message or not update.message.text:
        logger.debug("edit_field_update: Ignoring non-text message.")
        return EDIT_FIELD # Ignore non-text messages

    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id # <<< ДОБАВЛЕНО: Получаем chat_id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}', Value='{new_value[:50]}...' ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна. начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_validation_fmt_raw = "{field_name}: макс. {max_len} символов."
    error_validation_min_fmt_raw = "{field_name}: мин. {min_len} символа."
    error_name_taken_fmt_raw = "имя '{name}' уже занято другой твоей личностью. попробуй другое:"
    success_update_fmt_raw = "✅ поле *{field_name}* для личности *{persona_name}* обновлено!"
    prompt_next_edit_fmt_raw = "Что еще изменить для *{name}* \\(ID: {id}\\)?" # Escaped parentheses
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении. попробуй еще раз.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении.")
    info_no_change = escape_markdown_v2("значение не изменено.") # <<< ДОБАВЛЕНО

    # Check if session data is present
    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    field_display_name_plain = FIELD_INFO.get(field, {}).get("label", field) # Get display name

    # <<< ДОБАВЛЕНО: Проверка на точку для пропуска обновления шаблонных полей >>>
    is_template_field = field.endswith("_template")
    if is_template_field and new_value == ".":
        logger.info(f"User {user_id} chose not to update template field '{field}' for persona {persona_id}.")
        await update.message.reply_text(info_no_change, parse_mode=ParseMode.MARKDOWN_V2)
        # --- Return to Main Edit Menu ---
        context.user_data.pop('edit_field', None) # Clear the field being edited
        try:
            with next(get_db()) as db:
                persona_config = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
                if persona_config:
                    keyboard = await _get_edit_persona_keyboard(persona_config) # Regenerate keyboard
                    final_next_prompt = prompt_next_edit_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
                    # <<< ИЗМЕНЕНО: Отправляем новое сообщение с меню >>>
                    await context.bot.send_message(chat_id, final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                    return EDIT_PERSONA_CHOICE # Transition back to choice state
                else:
                    logger.warning(f"Persona {persona_id} not found when returning to menu after skipping template update.")
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"Error returning to edit menu after skipping template update: {e}", exc_info=True)
             return ConversationHandler.END


    # --- Input Validation ---
    validation_error_msg = None
    max_len_map = { # Max lengths for different fields
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len_map = {"name": 2} # Min lengths

    # Check max length
    if field in max_len_map and len(new_value) > max_len_map[field]:
        max_len = max_len_map[field]
        validation_error_msg = escape_markdown_v2(error_validation_fmt_raw.format(field_name=field_display_name_plain, max_len=max_len))
    # Check min length
    if field in min_len_map and len(new_value) < min_len_map[field]:
        min_len = min_len_map[field]
        validation_error_msg = escape_markdown_v2(error_validation_min_fmt_raw.format(field_name=field_display_name_plain, min_len=min_len))

    # If validation failed, ask user to try again
    if validation_error_msg:
        logger.debug(f"Validation failed for field '{field}': {validation_error_msg}")
        # <<< ИЗМЕНЕНО: Кнопка "Отмена" вместо "Назад" >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")
        await update.message.reply_text(f"{validation_error_msg} {escape_markdown_v2('попробуй еще раз:')}", reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_FIELD # Stay in this state

    # --- Update Database ---
    try:
        with next(get_db()) as db:
            # Fetch the persona again to ensure it exists and lock it
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).with_for_update().first() # Lock the row

            if not persona_config:
                 # Should not happen if check passed before, but handle defensively
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            # Specific check for 'name' field uniqueness (case-insensitive)
            if field == "name" and new_value.lower() != persona_config.name.lower():
                existing = db.query(PersonaConfig.id).filter(
                    PersonaConfig.owner_id == persona_config.owner_id, # Check only for the same owner
                    func.lower(PersonaConfig.name) == new_value.lower()
                ).first()
                if existing:
                    # Name already taken by another persona of the same user
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by their persona {existing.id}.")
                    # <<< ИЗМЕНЕНО: Кнопка "Отмена" вместо "Назад" >>>
                    cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")
                    final_name_taken_msg = escape_markdown_v2(error_name_taken_fmt_raw.format(name=new_value))
                    await update.message.reply_text(final_name_taken_msg, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                    return EDIT_FIELD # Stay in this state

            # Update the field value
            setattr(persona_config, field, new_value)
            db.commit() # Commit the change
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            # Send success confirmation
            final_success_msg = success_update_fmt_raw.format(
                field_name=escape_markdown_v2(field_display_name_plain),
                persona_name=escape_markdown_v2(persona_config.name) # Use potentially updated name
            )
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            # --- Return to Main Edit Menu ---
            context.user_data.pop('edit_field', None) # Clear the field being edited
            db.refresh(persona_config) # Refresh to get latest state
            keyboard = await _get_edit_persona_keyboard(persona_config) # Regenerate keyboard
            final_next_prompt = prompt_next_edit_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
            # <<< ИЗМЕНЕНО: Отправляем новое сообщение с меню >>>
            await context.bot.send_message(chat_id, final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_PERSONA_CHOICE # Transition back to choice state

    except SQLAlchemyError as e:
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
         db.rollback() # Rollback on error
         # Try to return user to the main edit menu gracefully
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear() # Clear state on unexpected error
         return ConversationHandler.END

async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the new value for max_response_messages."""
    logger.debug("Entering edit_max_messages_update...") # <<< ДОБАВЛЕНО
    if not update.message or not update.message.text:
        logger.debug("edit_max_messages_update: Ignoring non-text message.")
        return EDIT_MAX_MESSAGES # Ignore non-text
    new_value_str = update.message.text.strip()
    field = "max_response_messages" # Hardcoded field name
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна (нет persona_id). начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_invalid_value = escape_markdown_v2("неверное значение. введи число от 1 до 10:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении. попробуй еще раз.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении.")
    success_update_fmt_raw = "✅ макс. сообщений в ответе для *{name}* установлено: *{value}*"
    prompt_next_edit_fmt_raw = "Что еще изменить для *{name}* \\(ID: {id}\\)?" # Escaped parentheses

    # Check session
    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # --- Validate Input ---
    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10): # Check range
            raise ValueError("Value out of range 1-10")
    except ValueError:
        # Invalid input (not integer or out of range)
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        # <<< ИЗМЕНЕНО: Кнопка "Отмена" вместо "Назад" >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")
        await update.message.reply_text(error_invalid_value, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MAX_MESSAGES # Stay in this state

    # --- Update Database ---
    try:
        with next(get_db()) as db:
            # Fetch and lock persona
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).with_for_update().first()

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            # Update value and commit
            persona_config.max_response_messages = new_value
            db.commit()
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            # Send success message
            final_success_msg = success_update_fmt_raw.format(name=escape_markdown_v2(persona_config.name), value=new_value)
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            # --- Return to Main Edit Menu ---
            db.refresh(persona_config) # Refresh state
            keyboard = await _get_edit_persona_keyboard(persona_config) # Regenerate keyboard
            final_next_prompt = prompt_next_edit_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
            # <<< ИЗМЕНЕНО: Отправляем новое сообщение с меню >>>
            await context.bot.send_message(chat_id, final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_PERSONA_CHOICE # Transition back

    except SQLAlchemyError as e:
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
         db.rollback()
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

    # Check if owner is premium
    is_premium = False
    owner = persona_config.owner
    if owner:
        is_premium = owner.is_active_subscriber or is_admin(owner.telegram_id)
    else:
        # This might happen if owner wasn't loaded, log warning
        logger.warning(f"Owner not loaded for persona {persona_config.id} in _get_edit_persona_keyboard")

    star = " ⭐" # Indicator for premium features
    # Get current max messages value for display
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)

    # Create keyboard rows using FIELD_INFO for labels
    keyboard = [
        # Row 1: Name, Description
        [InlineKeyboardButton(f"📝 {FIELD_INFO['name']['label']}", callback_data="edit_field_name"),
         InlineKeyboardButton(f"📜 {FIELD_INFO['description']['label']}", callback_data="edit_field_description")],
        # Row 2: System Prompt (Premium)
        [InlineKeyboardButton(f"⚙️ {FIELD_INFO['system_prompt_template']['label']}{star if not is_premium else ''}", callback_data="edit_field_system_prompt_template")],
        # Row 3: Max Messages (Premium)
        [InlineKeyboardButton(f"📊 {FIELD_INFO['max_response_messages']['label']} ({max_resp_msg}){star if not is_premium else ''}", callback_data="edit_field_max_response_messages")],
        # Row 4: Should Respond Prompt (Premium)
        [InlineKeyboardButton(f"🤔 {FIELD_INFO['should_respond_prompt_template']['label']}{star if not is_premium else ''}", callback_data="edit_field_should_respond_prompt_template")],
        # Row 5: Spam Prompt (Premium)
        [InlineKeyboardButton(f"💬 {FIELD_INFO['spam_prompt_template']['label']}{star if not is_premium else ''}", callback_data="edit_field_spam_prompt_template")],
        # Row 6: Photo, Voice Prompts (Premium)
        [InlineKeyboardButton(f"🖼️ {FIELD_INFO['photo_prompt_template']['label']}{star if not is_premium else ''}", callback_data="edit_field_photo_prompt_template"),
         InlineKeyboardButton(f"🎤 {FIELD_INFO['voice_prompt_template']['label']}{star if not is_premium else ''}", callback_data="edit_field_voice_prompt_template")],
        # Row 7: Moods (Premium)
        [InlineKeyboardButton(f"🎭 Настроения{star if not is_premium else ''}", callback_data="edit_moods")],
        # Row 8: Cancel
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
    ]
    return keyboard

async def _get_edit_moods_keyboard_internal(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
     """Generates the keyboard for the mood editing menu."""
     if not persona_config: return []
     try:
         # Load moods from JSON
         moods = json.loads(persona_config.mood_prompts_json or '{}')
     except json.JSONDecodeError:
         logger.warning(f"Invalid JSON in mood_prompts_json for persona {persona_config.id} when building keyboard.")
         moods = {}

     keyboard = []
     if moods:
         # Sort moods alphabetically for display
         sorted_moods = sorted(moods.keys(), key=str.lower)
         for mood_name in sorted_moods:
              try:
                  display_name = mood_name.capitalize()
                  # URL-encode name for callback data safety
                  encoded_mood_name = urllib.parse.quote(mood_name)
                  # Define callback data for edit and delete actions
                  edit_cb = f"editmood_select_{encoded_mood_name}"
                  delete_cb = f"deletemood_confirm_{encoded_mood_name}"

                  # Check callback data length
                  if len(edit_cb.encode('utf-8')) > 64 or len(delete_cb.encode('utf-8')) > 64:
                       logger.warning(f"Encoded mood name '{encoded_mood_name}' too long for callback data, skipping buttons.")
                       continue # Skip this mood if callback data is too long

                  # Add row with edit and delete buttons
                  keyboard.append([
                      InlineKeyboardButton(f"✏️ {display_name}", callback_data=edit_cb),
                      InlineKeyboardButton(f"🗑️", callback_data=delete_cb) # Delete icon
                  ])
              except Exception as encode_err:
                  logger.error(f"Error processing mood '{mood_name}' for keyboard: {encode_err}")

     # Add "Add Mood" and "Back" buttons
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")]) # Back to main edit menu
     return keyboard

async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    """Helper function to attempt returning to the main edit menu after an error."""
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    # <<< ИЗМЕНЕНО: Определяем chat_id более надежно >>>
    chat_id = None
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id
    elif update.message:
        chat_id = update.message.chat.id

    # Define user messages
    error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню редактирования (личность не найдена).")
    error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню редактирования.")
    prompt_edit_raw = "Редактируем *{name}* \\(ID: {id}\\)\n\nВыберите, что изменить:" # Escaped parentheses

    if not chat_id:
        logger.warning("Cannot return to edit menu: could not determine chat_id.")
        context.user_data.clear() # Clear state
        return ConversationHandler.END # End conversation

    try:
        with next(get_db()) as db:
            # Fetch persona again to ensure it exists
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if persona_config:
                # Regenerate main edit keyboard and prompt
                keyboard = await _get_edit_persona_keyboard(persona_config)
                final_prompt = prompt_edit_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
                # Send the menu as a new message
                await context.bot.send_message(chat_id, final_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_PERSONA_CHOICE # Return to choice state
            else:
                # Persona not found anymore
                logger.warning(f"Persona {persona_id} not found when trying to return to main edit menu.")
                await context.bot.send_message(chat_id, error_cannot_return, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        # Handle errors during the recovery attempt
        logger.error(f"Failed to return to main edit menu after error: {e}", exc_info=True)
        await context.bot.send_message(chat_id, error_cannot_return_general, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.clear()
        return ConversationHandler.END

async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     """Helper function to attempt returning to the mood edit menu after an error."""
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     # Determine the message to reply to or edit
     callback_message = update.callback_query.message if update.callback_query else None
     user_message = update.message # Message that likely caused the error
     target_message = callback_message or user_message

     # Define user messages
     error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню настроений (личность не найдена).")
     error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню настроений.")
     prompt_mood_menu_raw = "🎭 Управление настроениями для *{name}*:"

     if not target_message:
         logger.warning("Cannot return to mood menu: no target message found.")
         context.user_data.clear()
         return ConversationHandler.END
     target_chat_id = target_message.chat.id

     try:
         with next(get_db()) as db:
             # Fetch persona
             persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()

             if persona_config:
                 # Regenerate mood menu keyboard and prompt
                 keyboard = await _get_edit_moods_keyboard_internal(persona_config)
                 final_prompt = prompt_mood_menu_raw.format(name=escape_markdown_v2(persona_config.name))

                 # Send the mood menu as a new message
                 await context.bot.send_message(
                     chat_id=target_chat_id,
                     text=final_prompt,
                     reply_markup=InlineKeyboardMarkup(keyboard),
                     parse_mode=ParseMode.MARKDOWN_V2
                 )
                 # Try to delete the previous message if it was from the bot (e.g., a prompt)
                 if callback_message and callback_message.from_user.is_bot:
                     try: await callback_message.delete()
                     except Exception as del_e: logger.warning(f"Could not delete previous bot message: {del_e}")

                 return EDIT_MOOD_CHOICE # Return to mood choice state
             else:
                 # Persona not found
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await context.bot.send_message(target_chat_id, error_cannot_return, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         # Handle errors during recovery
         logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
         await context.bot.send_message(target_chat_id, error_cannot_return_general, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear()
         return ConversationHandler.END

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    """Displays the mood editing menu (list moods, add button)."""
    query = update.callback_query
    if not query: return ConversationHandler.END # Should be called from callback

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна. начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("Ошибка базы данных при загрузке настроений.")
    info_premium = "⭐ Доступно по подписке" # For answer callback
    prompt_mood_menu_fmt_raw = "🎭 Управление настроениями для *{name}*:"

    # Check session
    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # --- Fetch Persona and Check Premium (if not passed) ---
    local_persona_config = persona_config # Use passed config if available
    is_premium = False

    if local_persona_config is None:
        # Fetch if not passed (e.g., returning from sub-state)
        try:
            with next(get_db()) as db:
                local_persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                     PersonaConfig.id == persona_id,
                     PersonaConfig.owner.has(User.telegram_id == user_id)
                 ).first()

                if not local_persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.answer("Личность не найдена", show_alert=True)
                    # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
                    await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    context.user_data.clear()
                    return ConversationHandler.END

                # Check premium status after fetching
                owner = local_persona_config.owner
                if owner:
                     is_premium = owner.is_active_subscriber or is_admin(user_id)
                else:
                     logger.warning(f"Owner not loaded for persona {persona_id} in edit_moods_menu fetch.")
                     is_premium = False # Assume non-premium if owner load fails

        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("Ошибка базы данных", show_alert=True)
             # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
             await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
             # Try to return to main edit menu on DB error
             return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    else:
         # Persona config was passed, check premium status from it
         owner = local_persona_config.owner
         if owner:
             is_premium = owner.is_active_subscriber or is_admin(user_id)
         else:
              # If owner wasn't loaded when passed, try fetching owner separately
              logger.warning(f"Owner not loaded for passed persona {persona_id} in edit_moods_menu. Fetching...")
              with next(get_db()) as db:
                  owner_db = db.query(User).filter(User.id == local_persona_config.owner_id).first()
                  if owner_db: is_premium = owner_db.is_active_subscriber or is_admin(user_id)
                  else: is_premium = False # Assume non-premium if fetch fails

    # Double-check premium status before showing menu
    if not is_premium:
        logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
        await query.answer(info_premium, show_alert=True)
        # Try to return to main edit menu
        return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    # --- Display Mood Menu ---
    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config) # Generate mood list keyboard
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = prompt_mood_menu_fmt_raw.format(name=escape_markdown_v2(local_persona_config.name))

    try:
        # Edit the message to show the mood list
        if query.message.text != msg_text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await query.answer() # Silent answer
    except Exception as e:
         # Handle errors editing the message
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         # Fallback: Send a new message
         try:
            await context.bot.send_message(chat_id=query.message.chat.id, text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            # Try to delete the old message if it was from the bot
            if query.message.from_user.is_bot:
                 try: await query.message.delete()
                 except: pass
         except Exception as send_e: logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE # Stay in mood choice state

async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses within the mood editing menu (edit, delete, add, back)."""
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE # Stay if no data

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна. начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("Ошибка базы данных.")
    error_unhandled_choice = escape_markdown_v2("неизвестный выбор настроения.")
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения.")
    prompt_new_name = escape_markdown_v2("введи название нового настроения (1-30 символов, буквы/цифры/дефис/подчерк., без пробелов):")
    # Updated prompt format: No current value
    prompt_new_prompt_fmt_raw = "✏️ Редактирование настроения: *{name}*\n\nОтправь новый текст промпта (до 1500 симв\\.):"
    prompt_confirm_delete_fmt_raw = "точно удалить настроение '{name}'?"

    # Check session
    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        await context.bot.send_message(chat_id, error_no_session, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # --- Fetch Persona Config ---
    # Needed to get current moods and name
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
                 # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
                 await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
         await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         # Try to return to main edit menu on error
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    await query.answer() # Answer callback quickly

    # --- Route based on callback data ---
    if data == "edit_persona_back":
        # Go back to the main persona edit menu
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        prompt_edit_raw = "Редактируем *{name}* \\(ID: {id}\\)\n\nВыберите, что изменить:" # Escaped parentheses
        final_prompt = prompt_edit_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
        await query.edit_message_text(final_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        # Clear mood-specific state
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE # Transition back

    if data == "editmood_add":
        # Start adding a new mood: ask for name
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None # Indicate we are adding a new mood
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel") # Back to mood list
        try:
            await query.delete_message()
        except Exception: pass
        await context.bot.send_message(chat_id, prompt_new_name, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME # Transition to name input state

    if data.startswith("editmood_select_"):
        # User selected a mood to edit: ask for new prompt
        original_mood_name = None
        try:
             # Decode mood name from callback data
             encoded_mood_name = data.split("editmood_select_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except Exception as decode_err:
             logger.error(f"Error decoding mood name from callback {data}: {decode_err}")
             # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
             await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

        # Store the mood name being edited
        context.user_data['edit_mood_name'] = original_mood_name
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' to edit for {persona_id}.")

        # Prepare prompt message asking for new prompt text (without showing current prompt)
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel") # Back to mood list
        final_prompt = prompt_new_prompt_fmt_raw.format(
            name=escape_markdown_v2(original_mood_name)
            )
        try:
            await query.delete_message()
        except Exception: pass
        await context.bot.send_message(chat_id, final_prompt, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT # Transition to prompt input state

    if data.startswith("deletemood_confirm_"):
         # User pressed delete button: ask for confirmation
         original_mood_name = None
         encoded_mood_name = ""
         try:
             # Decode mood name from callback
             encoded_mood_name = data.split("deletemood_confirm_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
         except Exception as decode_err:
             logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
             # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
             await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
             return await edit_moods_menu(update, context, persona_config=persona_config)

         # Store mood name to be deleted
         context.user_data['delete_mood_name'] = original_mood_name
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' for {persona_id}. Asking confirmation.")

         # Create confirmation keyboard
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")] # Back to mood list
            ]
         # Send confirmation message
         final_confirm_prompt = escape_markdown_v2(prompt_confirm_delete_fmt_raw.format(name=original_mood_name))
         await query.edit_message_text(final_confirm_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
         return DELETE_MOOD_CONFIRM # Transition to confirmation state

    if data == "edit_moods_back_cancel":
         # User pressed back/cancel from a sub-state (name/prompt input, delete confirm)
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         # Clear temporary state
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         # Return to mood menu
         return await edit_moods_menu(update, context, persona_config=persona_config)

    # Fallback for unhandled data
    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text(error_unhandled_choice, parse_mode=ParseMode.MARKDOWN_V2)
    # Return to mood menu
    return await edit_moods_menu(update, context, persona_config=persona_config)

async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the name for a new mood."""
    logger.debug("Entering edit_mood_name_received...") # <<< ДОБАВЛЕНО
    if not update.message or not update.message.text:
        logger.debug("edit_mood_name_received: Ignoring non-text message.")
        return EDIT_MOOD_NAME # Ignore non-text
    mood_name_raw = update.message.text.strip()
    # Validate mood name format (letters, numbers, hyphen, underscore, no spaces)
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна. начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена.")
    error_validation = escape_markdown_v2("название: 1-30 символов, буквы/цифры/дефис/подчерк., без пробелов. попробуй еще:")
    error_name_exists_fmt_raw = "настроение '{name}' уже существует. выбери другое:"
    error_db = escape_markdown_v2("ошибка базы данных при проверке имени.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")
    prompt_for_prompt_fmt_raw = "отлично! теперь отправь текст промпта для настроения '{name}':"

    # Check session
    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Validate name format and length
    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        # <<< ИЗМЕНЕНО: Кнопка "Отмена" >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME # Stay in this state

    mood_name = mood_name_raw # Use validated name

    # --- Check Uniqueness ---
    try:
        with next(get_db()) as db:
            # Fetch persona to check its current moods
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 context.user_data.clear()
                 return ConversationHandler.END

            # Load current moods
            current_moods = {}
            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: pass # Ignore if JSON is invalid, treat as empty

            # Check if name (case-insensitive) already exists
            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists.")
                # <<< ИЗМЕНЕНО: Кнопка "Отмена" >>>
                cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
                final_exists_msg = escape_markdown_v2(error_name_exists_fmt_raw.format(name=mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME # Stay in this state

            # --- Name is valid and unique, proceed to ask for prompt ---
            context.user_data['edit_mood_name'] = mood_name # Store the new name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
            cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
            final_prompt = escape_markdown_v2(prompt_for_prompt_fmt_raw.format(name=mood_name))
            await context.bot.send_message(chat_id, final_prompt, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
            return EDIT_MOOD_PROMPT # Transition to prompt input state

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        # Stay in name input state on DB error during check
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END # End on unexpected error

async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the prompt text for a mood being edited or added."""
    logger.debug("Entering edit_mood_prompt_received...") # <<< ДОБАВЛЕНО
    if not update.message or not update.message.text:
        logger.debug("edit_mood_prompt_received: Ignoring non-text message.")
        return EDIT_MOOD_PROMPT # Ignore non-text
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name') # Name being edited or added
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна (нет имени настроения). начни снова (/mypersonas).")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_validation = escape_markdown_v2("промпт настроения: 1-1500 символов. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения.")
    success_saved_fmt_raw = "✅ настроение *{name}* сохранено!"

    # Check session state
    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Validate prompt length
    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        # <<< ИЗМЕНЕНО: Кнопка "Отмена" >>>
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT # Stay in this state

    # --- Update Database ---
    try:
        with next(get_db()) as db:
            # Fetch and lock persona
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            # Load current moods, add/update the new one
            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {} # Start fresh if JSON invalid

            current_moods[mood_name] = mood_prompt # Add or update the mood
            # Use the dedicated setter method which handles JSON conversion and modification flag
            persona_config.set_moods(db, current_moods)
            db.commit() # Commit the change

            # --- Success - Return to Mood Menu ---
            context.user_data.pop('edit_mood_name', None) # Clear temporary state
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            final_success_msg = success_saved_fmt_raw.format(name=escape_markdown_v2(mood_name))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            db.refresh(persona_config) # Refresh state
            # Go back to the mood menu, passing the updated config
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        # Try to return to mood menu on error
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        # Try to return to mood menu on error
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the final confirmation button press for deleting a mood."""
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM # Stay if no data

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name') # Get name from context
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна. начни снова (/mypersonas).")
    error_not_found_persona = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения.")
    info_not_found_mood_fmt_raw = "настроение '{name}' не найдено (уже удалено?)."
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения для удаления.")
    success_delete_fmt_raw = "🗑️ настроение *{name}* удалено."

    # --- Validate Callback Data and Session State ---
    original_name_from_callback = None
    if data.startswith("deletemood_delete_"):
        try:
            # Decode name from callback data (e.g., "deletemood_delete_...")
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await query.answer("Ошибка данных", show_alert=True)
            # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
            await context.bot.send_message(chat_id, error_decode_mood, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            # Try to return to mood menu
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    # Check if session data matches callback data
    if not mood_name_to_delete or not persona_id or not original_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. Stored='{mood_name_to_delete}', Callback='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("Ошибка сессии", show_alert=True)
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('delete_mood_name', None) # Clear invalid state
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    await query.answer("Удаляем...") # Feedback
    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name_to_delete}' for persona {persona_id}.")

    # --- Delete Mood from Database ---
    try:
        with next(get_db()) as db:
            # Fetch and lock persona
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
                await context.bot.send_message(chat_id, error_not_found_persona, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            # Load moods, delete the target mood, save back
            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {}

            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete] # Remove the mood
                persona_config.set_moods(db, current_moods) # Save updated moods
                db.commit() # Commit deletion

                # --- Success ---
                context.user_data.pop('delete_mood_name', None) # Clear state
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                final_success_msg = success_delete_fmt_raw.format(name=escape_markdown_v2(mood_name_to_delete))
                await query.edit_message_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2) # Edit confirmation message
            else:
                # Mood already deleted or never existed
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id}.")
                final_not_found_msg = escape_markdown_v2(info_not_found_mood_fmt_raw.format(name=mood_name_to_delete))
                await query.edit_message_text(final_not_found_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.pop('delete_mood_name', None) # Clear state

            # --- Return to Mood Menu ---
            db.refresh(persona_config) # Refresh state
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
        await context.bot.send_message(chat_id, error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the entire persona editing conversation."""
    message = update.effective_message
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A') # Get ID for logging
    chat_id = update.effective_chat.id # <<< ДОБАВЛЕНО
    logger.info(f"User {user_id} cancelled persona edit/mood edit for persona {persona_id}.")

    # Define user message
    cancel_message = escape_markdown_v2("редактирование отменено.")

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer() # Answer callback
            # Try to edit the message where the cancel button was pressed
            if query.message and query.message.text != cancel_message:
                try:
                    await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                except BadRequest as e:
                    # Handle cases where message can't be edited (e.g., too old)
                    if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                         logger.warning(f"Could not edit cancel message (not found/too old). Sending new for user {user_id}.")
                         await context.bot.send_message(chat_id=query.message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        raise # Re-raise other BadRequest errors
        elif message:
            # If cancelled via command, send a reply
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        # Handle errors sending/editing the confirmation
        logger.warning(f"Error sending/editing cancellation confirmation for user {user_id}: {e}")
        # Fallback: try sending plain text
        if chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id, text="Редактирование отменено.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
            except Exception as send_e: logger.error(f"Failed to send fallback cancel message: {send_e}")

    # Clear conversation state and end
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

    # Check subscription only for command
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Clear previous state

    # Define user messages
    error_not_found_fmt_raw = "личность с id {id} не найдена или не твоя."
    prompt_delete_fmt_raw = "🚨 ВНИМАНИЕ! 🚨\nудалить личность '{name}' \\(ID: {id}\\)?\n\nэто действие НЕОБРАТИМО!" # Escaped parentheses
    error_db = escape_markdown_v2("ошибка базы данных.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")

    try:
        with next(get_db()) as db:
            # Find persona and check ownership
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 # Persona not found or not owned
                 final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                 return ConversationHandler.END

            # Store ID for confirmation step
            context.user_data['delete_persona_id'] = persona_id
            # Truncate long names for button text
            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            # Create confirmation keyboard
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_name_display}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Format confirmation message
            msg_text = prompt_delete_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id) # Escape name
            # Send or edit message
            if is_callback:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                      else:
                           await query.answer() # Silent answer
                 except BadRequest as edit_err: # Catch potential parse errors or other issues
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                 except Exception as edit_err:
                      logger.error(f"Unexpected error editing message for delete start (persona {persona_id}): {edit_err}", exc_info=True)
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2) # Fallback send
            else:
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
            return DELETE_PERSONA_CONFIRM # Transition to confirmation state
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

    # Define user messages
    usage_text = escape_markdown_v2("укажи id личности: /deletepersona <id>\nили используй кнопку из /mypersonas")
    error_invalid_id = escape_markdown_v2("ID должен быть числом.")

    # Validate arguments
    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Call common start logic
    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for delete persona button press."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем удаление...") # Feedback

    # Define user message
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности в кнопке.")

    # Extract ID from callback data (e.g., "delete_persona_123")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        # Call common start logic
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
    if not query or not query.data: return DELETE_PERSONA_CONFIRM # Stay if no data

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id') # Get ID from session
    chat_id = query.message.chat.id # <<< ДОБАВЛЕНО

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    # Define user messages
    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна. начни снова (/mypersonas).")
    error_delete_failed = escape_markdown_v2("❌ не удалось удалить личность (ошибка базы данных).")
    success_deleted_fmt_raw = "✅ личность '{name}' удалена."

    # --- Validate Callback Data and Session ---
    # Ensure the callback data matches the persona ID stored in the session
    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.answer("Ошибка сессии", show_alert=True)
         # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
         await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
         context.user_data.clear() # Clear invalid state
         return ConversationHandler.END

    await query.answer("Удаляем...") # Feedback
    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id}" # Fallback name for logging/message

    # --- Perform Deletion ---
    try:
        with next(get_db()) as db:
             # Find the user first
             user = db.query(User).filter(User.telegram_id == user_id).first()
             if not user:
                  # Should not happen if user started the conversation
                  logger.error(f"User {user_id} not found in DB during persona deletion.")
                  # <<< ИЗМЕНЕНО: Отправляем новое сообщение >>>
                  await context.bot.send_message(chat_id, escape_markdown_v2("Ошибка: пользователь не найден."), reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                  context.user_data.clear()
                  return ConversationHandler.END

             # Get persona name before deleting (for success message)
             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first() # <<< ЗАВЕРШЕНО: Запрос для получения имени
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Found persona '{persona_name_deleted}' (ID: {persona_id}) for deletion.")
             else:
                 # Persona might have been deleted between confirmation and this step
                 logger.warning(f"Persona {persona_id} not found for user {user.id} just before calling delete function (might be already deleted).")
                 # We'll proceed assuming it's already deleted, setting deleted_ok = True later

             # Call the delete function (handles commit/rollback inside)
             deleted_ok = delete_persona_config(db, persona_id, user.id)

             # If delete_persona_config returned False because it wasn't found, treat as success (already deleted)
             if not deleted_ok and not persona_to_delete:
                 logger.warning(f"Persona {persona_id} was not found by delete_persona_config (likely already deleted). Treating as success.")
                 deleted_ok = True # Consider it successful if already gone

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
        deleted_ok = False # Ensure deletion is marked as failed
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)
        deleted_ok = False # Ensure deletion is marked as failed

    # --- Send Final Message ---
    if deleted_ok:
        final_success_msg = escape_markdown_v2(success_deleted_fmt_raw.format(name=persona_name_deleted))
        try:
            await query.edit_message_text(final_success_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with deletion success: {e}")
    else:
        # Deletion failed
        try:
            await query.edit_message_text(error_delete_failed, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to edit message with deletion failure: {e}")

    # Clean up and end conversation
    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona deletion process."""
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer() # Answer callback
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A') # Get ID for logging
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")

    # Define user message
    cancel_message = escape_markdown_v2("удаление отменено.")

    # Edit the confirmation message to show cancellation
    try:
        await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to edit message with deletion cancellation: {e}")
    # Clean up and end
    context.user_data.clear()
    return ConversationHandler.END

# --- Mute/Unmute Commands ---

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id_str}")

    # Check subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    # Define user messages
    error_no_persona = escape_markdown_v2("В этом чате нет активной личности.")
    error_not_owner = escape_markdown_v2("Только владелец личности может ее заглушить.")
    error_no_instance = escape_markdown_v2("Ошибка: не найден объект связи с чатом.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке заглушить бота.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды.")
    info_already_muted_fmt_raw = "Личность '{name}' уже заглушена в этом чате."
    success_muted_fmt_raw = "✅ Личность '{name}' больше не будет отвечать в этом чате (но будет запоминать сообщения). Используйте /unmutebot, чтобы вернуть."

    with next(get_db()) as db:
        try:
            # Get active persona and owner
            instance_info = get_persona_and_context_with_owner(chat_id_str, db)
            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance

            # Check ownership
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Ensure chat instance exists
            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id_str} during mute.")
                await update.message.reply_text(error_no_instance, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Mute if not already muted
            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                db.commit() # Commit the change
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = escape_markdown_v2(success_muted_fmt_raw.format(name=persona.name))
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # Inform user if already muted
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
    """Handles the /unmutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id_str}")

    # Check subscription
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    # Define user messages
    error_no_persona = escape_markdown_v2("В этом чате нет активной личности, которую можно размьютить.")
    error_not_owner = escape_markdown_v2("Только владелец личности может снять заглушку.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке вернуть бота к общению.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды.")
    info_not_muted_fmt_raw = "Личность '{name}' не была заглушена."
    success_unmuted_fmt_raw = "✅ Личность '{name}' снова может отвечать в этом чате."

    with next(get_db()) as db:
        try:
            # Get active instance with relations to check owner and name
            active_instance = get_active_chat_bot_instance_with_relations(db, chat_id_str)

            # Check if an active instance exists and relations loaded
            if not active_instance or not active_instance.bot_instance_ref or not active_instance.bot_instance_ref.owner or not active_instance.bot_instance_ref.persona_config:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name

            # Check ownership
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Unmute if currently muted
            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit() # Commit the change
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = escape_markdown_v2(success_unmuted_fmt_raw.format(name=persona_name))
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # Inform user if not muted
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
# --- END OF FILE handlers.py ---
