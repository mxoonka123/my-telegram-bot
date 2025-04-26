import logging
import httpx
import random
import asyncio
import re
import uuid
import json
from datetime import datetime, timezone, timedelta
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func
from typing import List, Dict, Any, Optional, Union, Tuple

from yookassa import Configuration as YookassaConfig # <<< ИЗМЕНЕН ИМПОРТ
from yookassa import Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem


from config import (
    LANGDOCK_API_KEY, LANGDOCK_BASE_URL, LANGDOCK_MODEL,
    DEFAULT_MOOD_PROMPTS, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CURRENCY, WEBHOOK_URL_BASE,
    SUBSCRIPTION_DURATION_DAYS, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    ADMIN_USER_ID
)
# <<< ИЗМЕНЕНО: импортируем config напрямую, а не отдельные переменные, чтобы использовать config.VAR >>>
import config

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
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

# Conversation states
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Field mapping for display names
FIELD_MAP = {
    "name": "имя",
    "description": "описание",
    "system_prompt_template": "системный промпт",
    "should_respond_prompt_template": "промпт 'отвечать?'",
    "spam_prompt_template": "промпт спама",
    "photo_prompt_template": "промпт фото",
    "voice_prompt_template": "промпт голоса",
    "max_response_messages": "макс. сообщений в ответе"
}

# Terms of Service Text (remains the same)
TOS_TEXT = """
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
""".format(subscription_duration=config.SUBSCRIPTION_DURATION_DAYS, subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}", subscription_currency=config.SUBSCRIPTION_CURRENCY)


# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Send simplified error message to user if possible
    if isinstance(update, Update) and update.effective_message:
        err_text = "упс... что-то пошло не так. попробуй еще раз позже."
        # Add more details for specific, known errors if needed
        # if isinstance(context.error, SpecificKnownError):
        #    err_text = "Произошла известная ошибка: ..."
        try:
            await update.effective_message.reply_text(err_text)
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")


# --- Helper Functions ---
def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    """
    Fetches the active Persona, its context, and its owner User for a given chat ID.
    Returns None if no active persona or related data is found.
    """
    try:
        chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id)
        if not chat_instance:
            logger.debug(f"No active chatbot instance found for chat {chat_id}")
            return None

        bot_instance = chat_instance.bot_instance_ref
        # Ensure all necessary linked objects are present
        if not bot_instance or not bot_instance.persona_config or not bot_instance.owner:
             logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance, PersonaConfig or Owner.")
             return None

        persona_config = bot_instance.persona_config
        owner_user = bot_instance.owner # Owner is loaded via relations

        # Initialize Persona object
        persona = Persona(persona_config, chat_instance)

        # Get context (this performs a SELECT)
        context_list = get_context_for_chat_bot(db, chat_instance.id) # Handles its own errors internally

        return persona, context_list, owner_user
    except ValueError as e: # Catch Persona initialization errors
         logger.error(f"Failed to initialize Persona for config {persona_config.id if 'persona_config' in locals() else 'N/A'} in chat {chat_id}: {e}", exc_info=True)
         return None
    except SQLAlchemyError as e: # Catch DB errors during the process
        logger.error(f"Database error in get_persona_and_context_with_owner for chat {chat_id}: {e}", exc_info=True)
        # Let the main handler deal with rollback
        return None
    except Exception as e: # Catch unexpected errors
        logger.error(f"Unexpected error in get_persona_and_context_with_owner for chat {chat_id}: {e}", exc_info=True)
        return None


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Sends a request to the Langdock API and returns the text response."""
    if not config.LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return "ошибка: ключ api не настроен."

    headers = {
        "Authorization": f"Bearer {config.LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    # Send only the last N messages as context
    messages_to_send = messages[-config.MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]

    payload = {
        "model": config.LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages_to_send,
        "max_tokens": 1024, # Consider adjusting if needed
        "temperature": 0.75,
        "top_p": 0.95,
        "stream": False # Assuming non-streaming for simplicity now
    }
    url = f"{config.LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"

    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages.")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client: # Increased timeout
             resp = await client.post(url, json=payload, headers=headers)

        logger.debug(f"Langdock response status: {resp.status_code}")
        resp.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
        data = resp.json()
        logger.debug(f"Langdock response data (first 200 chars): {str(data)[:200]}")

        # --- Extract response text (handle various possible structures) ---
        full_response = ""
        content = data.get("content")
        if isinstance(content, list):
            # Standard Claude-3.5 structure (and likely others)
            text_parts = [part.get("text", "") for part in content if part.get("type") == "text"]
            full_response = " ".join(text_parts)
        elif isinstance(content, dict) and "text" in content:
             # Simpler structure if only one text block
             full_response = content["text"]
        elif "choices" in data and isinstance(data["choices"], list) and data["choices"]:
             # OpenAI-like structure
             choice = data["choices"][0]
             message = choice.get("message")
             if isinstance(message, dict) and "content" in message:
                 full_response = message["content"]
             elif "text" in choice: # Older OpenAI compatibility
                 full_response = choice["text"]
        elif isinstance(data.get("response"), str):
             # Some APIs might return response directly in a 'response' field
             full_response = data["response"]

        if not full_response:
             logger.warning(f"Could not extract text from Langdock response structure: {data}")
             return "хм, я получил ответ, но не смог его прочитать..." # More informative error

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return "хм, кажется, я слишком долго думал... попробуй еще раз?"
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {error_body}", exc_info=True)
        user_message = f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."
        # Try to provide more specific user feedback for common errors
        if e.response.status_code == 401:
            user_message = "ошибка: неверный ключ api для ai."
        elif e.response.status_code == 429:
            user_message = "слишком много запросов к ai, попробуй чуть позже."
        elif e.response.status_code >= 500:
             user_message = "упс, на стороне ai произошла ошибка. попробуй позже."
        return user_message
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return "не могу связаться с ai сейчас (проблема с сетью)..."
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from Langdock: {e}. Response text: {resp.text if 'resp' in locals() else 'N/A'}")
        return "получил непонятный ответ от ai..."
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return "произошла внутренняя ошибка при генерации ответа."


async def process_and_send_response(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    persona: Persona,
    full_bot_response_text: str,
    db: Session # Pass the session to add context
):
    """
    Adds AI response to context, processes text, extracts GIFs, and sends messages/GIFs.
    Raises exceptions on DB errors during context add.
    """
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}")
    response_content_to_save = full_bot_response_text.strip()

    # --- Add response to context FIRST ---
    # If this fails, we shouldn't send the message as context is inconsistent
    if persona.chat_instance:
        try:
            # This stages the message, doesn't commit yet
            add_message_to_context(db, persona.chat_instance.id, "assistant", response_content_to_save)
            logger.debug("AI response staged for database context.")
        except SQLAlchemyError as e:
            # Log is done in add_message_to_context, re-raise to trigger rollback
            logger.error(f"Re-raising DB Error after failing to add assistant response to context for chat_instance {persona.chat_instance.id}.")
            raise # Propagate DB error - causes rollback in handler
        except Exception as e:
            logger.error(f"Unexpected Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            raise # Propagate other errors - causes rollback in handler
    else:
        # This is an internal inconsistency, should probably stop
        logger.error(f"Cannot add AI response to context for persona {persona.name}, chat_instance is None.")
        raise ValueError(f"Internal state error: chat_instance is None for persona {persona.name}")


    # --- Process text and GIFs ---
    all_text_content = response_content_to_save # Use the stripped version
    gif_links = extract_gif_links(all_text_content)
    # Remove GIF links from the text to be sent
    for gif in gif_links:
        # Use regex for safer removal, especially if links have special chars
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()

    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    # --- Limit number of messages ---
    max_messages = 3 # Default
    try:
        # Accessing config directly might be safer if persona object lifecycle is complex
        if persona.config and isinstance(persona.config.max_response_messages, int):
            max_messages = persona.config.max_response_messages
            if not (1 <= max_messages <= 10): # Sanity check limits
                logger.warning(f"Persona {persona.name} has invalid max_response_messages ({max_messages}), using default 3.")
                max_messages = 3
        else:
             # Handle case where config or attribute is missing/invalid
             logger.debug(f"Using default max_response_messages (3) for persona {persona.name}.")
             max_messages = 3
    except Exception as e:
        logger.error(f"Error getting max_response_messages for persona {persona.name}: {e}. Using default 3.")
        max_messages = 3


    # Truncate if necessary
    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        # Optionally add ellipsis to the last part
        if text_parts_to_send:
             text_parts_to_send[-1] += "..."

    # --- Send GIFs ---
    # Randomize GIF sending? Or send first N? Send all found for now.
    gif_send_tasks = []
    for gif_url in gif_links:
        try:
            # Create tasks to send GIFs concurrently (slightly faster)
            task = context.bot.send_animation(chat_id=chat_id, animation=gif_url)
            gif_send_tasks.append(task)
            logger.info(f"Scheduled sending gif: {gif_url} to {chat_id}")
            # Add a small delay between scheduling if needed, or just let asyncio handle it
            # await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error scheduling gif {gif_url} send to chat {chat_id}: {e}", exc_info=True)

    # Wait for GIFs to be sent (optional, can proceed without waiting)
    if gif_send_tasks:
        await asyncio.gather(*gif_send_tasks, return_exceptions=True) # Log errors if any GIF fails


    # --- Send Text Parts ---
    if text_parts_to_send:
        is_group_chat = update and update.effective_chat and update.effective_chat.type in ["group", "supergroup"]
        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: continue

            # Simulate typing in group chats
            if is_group_chat:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    # Adjust sleep based on part length?
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception as e:
                     logger.warning(f"Failed to send typing action to {chat_id}: {e}")

            # Send the text part
            try:
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id}: '{part[:50]}...'")
                 # Use default ParseMode (Markdown) set in Application builder
                 await context.bot.send_message(chat_id=chat_id, text=part)
            except Exception as e:
                 logger.error(f"Error sending text part {i+1} to {chat_id}: {e}", exc_info=True)
                 # Should we stop sending further parts if one fails? Maybe.
                 break

            # Small delay between messages for realism
            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(0.4, 0.9))

async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Sends a message informing the user their limit is reached."""
    text = (
        f"упс! 😕 лимит сообщений ({user.daily_message_count}/{user.message_limit}) на сегодня достигнут.\n\n"
        f"✨ **хочешь безлимита?** ✨\n"
        f"подписка за {config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}/мес дает:\n"
        f"✅ **{config.PAID_DAILY_MESSAGE_LIMIT}** сообщений в день\n"
        f"✅ до **{config.PAID_PERSONA_LIMIT}** личностей\n"
        f"✅ полная настройка промптов и настроений\n\n"
        "👇 жми /subscribe или кнопку ниже!"
    )
    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        target_chat_id = update.effective_chat.id if update.effective_chat else None
        if target_chat_id:
             # Use default ParseMode (Markdown)
             await context.bot.send_message(target_chat_id, text, reply_markup=reply_markup)
        else:
             logger.warning(f"Could not send limit exceeded message to user {user.telegram_id}: no effective chat.")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


# --- Main Message Handler ---
# <<< ИЗМЕНЕНО: Структура с одним commit/rollback в конце >>>
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not (update.message.text or update.message.caption): return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    if not message_text: return # Ignore empty messages

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")

    # The 'with' block manages the session and transaction lifecycle
    # It ensures rollback on exception and closes the session
    with next(get_db()) as db:
        try:
            # --- 1. Get Persona, Context, and Owner ---
            # This function now loads relations efficiently
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona for chat {chat_id}. Ignoring.")
                # No DB changes made, transaction closes cleanly without commit/rollback needed here
                return
            persona, _, owner_user = persona_context_owner_tuple # Context list isn't needed immediately
            logger.debug(f"Handling message for persona '{persona.name}' owned by User ID {owner_user.id} (TG: {owner_user.telegram_id})")

            # --- 2. Check Limits (Modifies owner_user object in session, NO COMMIT inside) ---
            # This function now only modifies the user object in the session
            can_send_message = check_and_update_user_limits(db, owner_user)
            if not can_send_message:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}). Not responding or saving context.")
                await send_limit_exceeded_message(update, context, owner_user)
                # No DB changes should be committed (limit check modified user, but won't be committed)
                # The session will be rolled back or closed cleanly by the 'with' block
                return # Stop processing

            # --- 3. Add User Message to Context (Adds ChatContext object, NO FLUSH/COMMIT inside) ---
            context_added = False
            if persona.chat_instance:
                # add_message_to_context now raises error on failure, which triggers rollback
                user_prefix = username
                context_content = f"{user_prefix}: {message_text}"
                add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                context_added = True
                logger.debug("User message staged for context addition.")
            else:
                # This is an internal state error
                logger.error(f"Cannot add user message to context for persona {persona.name}, chat_instance is None.")
                await update.message.reply_text("системная ошибка: не удалось связать сообщение с личностью.")
                # No commit needed, error state reached
                return

            # --- 4. Check Muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Message saved to context, but ignoring response.")
                # Commit the successfully staged changes (user limit update, user message context)
                db.commit()
                logger.info(f"Committed staged changes (limit, user context) for muted persona {persona.name}.")
                return

            # --- 5. Handle potential mood change command ---
            available_moods = persona.get_all_mood_names()
            # Check for exact match (case-insensitive) first
            matched_mood = next((m for m in available_moods if m.lower() == message_text.lower()), None)
            if matched_mood:
                 logger.info(f"Message '{message_text}' matched mood name '{matched_mood}'. Changing mood.")
                 # mood() handles its own DB session and commit/rollback currently.
                 # Pass the existing session for potential use? Or let it manage its own.
                 # For now, let mood() manage its own as it's a distinct user action.
                 await mood(update, context) # Pass update/context, it will find persona/db again
                 # Stop processing here after mood change attempt
                 return

            # --- 6. Decide whether to respond (especially in groups) ---
            should_ai_respond = True # Default to respond in private chats or if checks fail
            is_group_chat = update.effective_chat.type in ["group", "supergroup"]

            if is_group_chat:
                 if persona.should_respond_prompt_template:
                     should_respond_prompt = persona.format_should_respond_prompt(message_text)
                     if should_respond_prompt:
                         try:
                             logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id}...")
                             # Get context *within the current transaction* to include the user's message
                             context_for_should_respond = get_context_for_chat_bot(db, persona.chat_instance.id)
                             decision_response = await send_to_langdock(
                                 system_prompt=should_respond_prompt,
                                 messages=context_for_should_respond
                             )
                             answer = decision_response.strip().lower()
                             logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")
                             if answer.startswith("д"): # If response starts with 'д' (yes)
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond based on AI='{answer}'.")
                                 should_ai_respond = True
                             # Add randomness? Maybe respond sometimes even if AI says no?
                             # elif random.random() < 0.05: # Small chance (5%) to respond anyway
                             #     logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond randomly despite AI='{answer}'.")
                             #     should_ai_respond = True
                             else:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding NOT to respond based on AI='{answer}'.")
                                 should_ai_respond = False
                         except Exception as e:
                              logger.error(f"Error in should_respond LLM logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                              logger.warning("Error in should_respond. Defaulting to respond.")
                              should_ai_respond = True # Fail safe: respond if check fails
                     else:
                          logger.debug(f"No should_respond_prompt generated for persona {persona.name}. Defaulting to respond in group.")
                          should_ai_respond = True # Default to respond if prompt formatting fails
                 else:
                     # If no template is set, default to responding in groups (can be changed)
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic for message: {message_text[:50]}...")
                 # Commit the staged user message and limit update even if not responding
                 db.commit()
                 logger.info(f"Committed staged changes (limit, user context) after deciding not to respond.")
                 return

            # --- 7. Get Context for AI Response Generation ---
            # Context already includes the user's message staged earlier
            # Re-fetch to ensure we have the latest state from the current transaction buffer
            context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
            if context_for_ai is None: # get_context_for_chat_bot returns [] on DB error now, not None
                # This case should ideally not be hit if add_message_to_context succeeded,
                # but handle defensively. Error would have been logged in get_context_for_chat_bot.
                logger.error(f"Failed to retrieve context for AI response generation for persona {persona.name}, chat {chat_id}.")
                await update.message.reply_text("ошибка при получении контекста для ответа.")
                # Let the 'with' block handle rollback
                return
            logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")


            # --- 8. Generate AI Response (External Call) ---
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt or "ошибка форматирования" in system_prompt: # Check if formatting failed
                logger.error(f"System prompt formatting failed for persona {persona.name}. Prompt: '{system_prompt}'")
                await update.message.reply_text("ошибка при подготовке ответа для ai.")
                # Commit user message + limit update even if prompt fails
                db.commit()
                logger.info(f"Committed staged changes (limit, user context) after prompt formatting failure.")
                return

            logger.debug(f"Formatted main system prompt for persona {persona.name}.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            # --- 9. Process & Send Response (Stages assistant message via add_message_to_context) ---
            # This function now also raises errors if adding context fails, triggering rollback
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            # --- 10. FINAL COMMIT ---
            # If we reach here without exceptions, commit *all* staged changes:
            # - User limit update (from check_and_update_user_limits)
            # - User message context INSERT/DELETE (from first add_message_to_context call)
            # - Assistant message context INSERT/DELETE (from add_message_to_context inside process_and_send_response)
            db.commit()
            logger.info(f"Successfully processed message and committed DB changes for handle_message cycle chat {chat_id}, persona {persona.name}.")

        # --- Exception Handling for the 'with' block ---
        except SQLAlchemyError as e:
             # Logging and rollback are handled by get_db context manager
             logger.error(f"Database error during handle_message transaction: {e}", exc_info=True)
             try: # Try to notify user
                 await update.message.reply_text("произошла ошибка базы данных, попробуйте позже.")
             except Exception as send_e:
                 logger.error(f"Failed to send DB error message to user: {send_e}")
        except Exception as e:
            # Catch any other unexpected error during the process
            # Logging and rollback are handled by get_db context manager
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            try: # Try to notify user
                await update.message.reply_text("произошла непредвиденная ошибка при обработке вашего сообщения.")
            except Exception as send_e:
                 logger.error(f"Failed to send general error message to user: {send_e}")


# --- Media Handlers ---
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Handles photo and voice messages using a similar transactional pattern."""
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with next(get_db()) as db:
        try:
            # --- 1. Get Persona and Owner ---
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple: return # No active persona
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by User ID {owner_user.id}")

            # --- 2. Check Limits ---
            can_send_message = check_and_update_user_limits(db, owner_user)
            if not can_send_message:
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding or saving context.")
                await send_limit_exceeded_message(update, context, owner_user)
                return # No commit needed, session rolls back or closes

            # --- 3. Add Media Placeholder to Context ---
            prompt_template = None
            context_text_placeholder = ""
            system_formatter = None
            if media_type == "photo":
                prompt_template = persona.photo_prompt_template
                context_text_placeholder = f"{username}: прислал(а) фотографию."
                system_formatter = persona.format_photo_prompt
            elif media_type == "voice":
                prompt_template = persona.voice_prompt_template
                context_text_placeholder = f"{username}: прислал(а) голосовое сообщение."
                system_formatter = persona.format_voice_prompt
            else:
                 logger.error(f"Unsupported media_type '{media_type}' in handle_media")
                 return # No commit needed

            context_added = False
            if persona.chat_instance:
                add_message_to_context(db, persona.chat_instance.id, "user", context_text_placeholder)
                context_added = True
                logger.debug(f"Added media placeholder to context for {media_type}.")
            else:
                 logger.error(f"Cannot add media placeholder to context for persona {persona.name}, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text("системная ошибка: не удалось связать медиа с личностью.")
                 return # No commit needed

            # --- 4. Check Muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Media saved to context, but ignoring response.")
                db.commit() # Commit context placeholder and limit update
                logger.info(f"Committed staged changes (limit, media context) for muted persona {persona.name}.")
                return

            # --- 5. Check Template Existence ---
            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id} has no {media_type} template. Skipping response generation.")
                db.commit() # Commit context placeholder and limit update
                logger.info(f"Committed staged changes (limit, media context) as no template exists.")
                return

            # --- 6. Get Context for AI ---
            context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
            if context_for_ai is None:
                 logger.error(f"Failed to retrieve context for AI media response for persona {persona.name}, chat {chat_id}.")
                 if update.effective_message: await update.effective_message.reply_text("ошибка при получении контекста для ответа на медиа.")
                 return # Let rollback handle

            # --- 7. Format Prompt & Get Response ---
            system_prompt = system_formatter()
            if not system_prompt or "ошибка форматирования" in system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}. Prompt: '{system_prompt}'")
                 if update.effective_message: await update.effective_message.reply_text(f"ошибка при подготовке ответа на {media_type}.")
                 db.commit() # Commit context placeholder and limit update
                 logger.info(f"Committed staged changes (limit, media context) after {media_type} prompt failure.")
                 return

            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            # --- 8. Process & Send Response (stages assistant message) ---
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            # --- 9. FINAL COMMIT ---
            db.commit()
            logger.info(f"Successfully processed {media_type} and committed DB changes for chat {chat_id}, persona {persona.name}.")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text("ошибка базы данных при обработке медиа.")
             # Rollback handled by context manager
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text("произошла непредвиденная ошибка при обработке медиа.")
            # Rollback handled by context manager


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for photo messages."""
    if not update.message or not update.message.photo: return
    await handle_media(update, context, "photo")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for voice messages."""
    if not update.message or not update.message.voice: return
    await handle_media(update, context, "voice")


# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            # Get or create user - this stages the user object if new/modified
            user = get_or_create_user(db, user_id, username)

            # Check if a persona is active in this specific chat
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)

            reply_text = ""
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                reply_text = (
                    f"привет! я {persona.name}. я уже активен в этом чате.\n"
                    "используй /help для списка команд."
                )
            else:
                # If no active persona in this chat, show general info
                # Ensure user limits are correct before display (reset if needed)
                # Pass the user object we already have
                check_and_update_user_limits(db, user) # Check/reset limits, modifies user in session

                # Fetch persona count separately
                persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0

                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber and user.subscription_expires_at else ""
                reply_text = (
                    f"привет! 👋 я бот для создания ai-собеседников (@NunuAiBot).\n\n"
                    f"твой статус: **{status}**{expires_text}\n"
                    f"личности: {persona_count}/{user.persona_limit} | "
                    f"сообщения сегодня: {user.daily_message_count}/{user.message_limit}\n\n" # Show current count/limit
                    "**начало работы:**\n"
                    "1. `/createpersona <имя>` - создай ai-личность.\n"
                    "2. `/mypersonas` - посмотри своих личностей и управляй ими.\n"
                    "`/profile` - детали статуса | `/subscribe` - узнать о подписке\n"
                    "`/help` - все команды"
                )

            # Commit any changes made (user creation/update, limit reset)
            db.commit()
            logger.debug(f"Committed changes for /start command for user {user_id}.")

            # Send the reply
            await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove()) # Uses default Markdown

        except SQLAlchemyError as e:
            logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("ошибка при загрузке данных. попробуй позже.")
        except Exception as e:
            logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("произошла ошибка при обработке команды /start.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
     """Displays the help message."""
     if not update.message: return
     user_id = update.effective_user.id
     chat_id = str(update.effective_chat.id)
     logger.info(f"CMD /help < User {user_id} in Chat {chat_id}")
     await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
     help_text = (
         "**🤖 основные команды:**\n"
         "`/start` - приветствие и твой статус\n"
         "`/help` - эта справка\n"
         "`/profile` - твой статус подписки и лимиты\n"
         "`/subscribe` - инфо о подписке и оплата\n\n"
         "**👤 управление личностями:**\n"
         "`/createpersona <имя> [описание]` - создать новую\n"
         "`/mypersonas` - список твоих личностей и кнопки управления (редакт., удалить, добавить в чат)\n"
         "`/editpersona <id>` - редактировать личность по ID (через /mypersonas удобнее)\n"
         "`/deletepersona <id>` - удалить личность по ID (через /mypersonas удобнее)\n\n"
         "**💬 управление в чате (где есть личность):**\n"
         "`/addbot <id>` - добавить личность в текущий чат (через /mypersonas удобнее)\n"
         "`/mood [настроение]` - сменить настроение активной личности (или просто `/mood` для выбора)\n"
         "`/reset` - очистить память (контекст) личности в этом чате\n"
         "`/mutebot` - заставить личность молчать в чате (сохраняя контекст)\n"
         "`/unmutebot` - разрешить личности отвечать в чате"
     )
     # Use default ParseMode (Markdown)
     await update.message.reply_text(help_text, reply_markup=ReplyKeyboardRemove())


# Note: mood() function handles its own commit/rollback for the mood change itself.
# This is acceptable as changing mood is a distinct, immediate action.
async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id}")

    target_chat_instance = None
    local_persona = None
    available_moods = []
    current_mood_name = "нейтрально" # Default

    # Fetch instance and persona info within a single transaction
    with next(get_db()) as db_session:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db_session)
            if not persona_info_tuple:
                reply_text = "в этом чате нет активной личности."
                logger.debug(f"No active persona for chat {chat_id}. Cannot set mood.")
                # No commit needed
                try: # Send reply outside transaction
                    if is_callback: await update.callback_query.edit_message_text(reply_text)
                    else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                return

            local_persona, _, owner_user = persona_info_tuple
            target_chat_instance = local_persona.chat_instance # Get the ChatBotInstance

            if not target_chat_instance:
                logger.error(f"Mood command: ChatBotInstance not found for persona {local_persona.name} in chat {chat_id} (internal error).")
                await message_or_callback_msg.reply_text("Ошибка: не найден экземпляр бота для смены настроения.")
                return # No commit needed

            # --- Authorization Check ---
            is_owner_or_admin = (owner_user.telegram_id == user_id) or is_admin(user_id)
            # For mood changes, maybe allow anyone in the chat? Or only owner/admin?
            # Let's allow anyone for now, but log if not owner/admin.
            if not is_owner_or_admin:
                 logger.info(f"User {user_id} (not owner/admin) is changing mood for persona '{local_persona.name}' in chat {chat_id}.")
                 # No permission error, just log

            # --- Check Muted ---
            if target_chat_instance.is_muted:
                logger.debug(f"Persona '{local_persona.name}' is muted in chat {chat_id}. Ignoring mood command.")
                reply_text=f"Личность '{local_persona.name}' сейчас заглушена (/unmutebot)."
                try: # Send reply outside transaction
                     if is_callback: await update.callback_query.edit_message_text(reply_text)
                     else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
                except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
                return # No commit needed

            available_moods = local_persona.get_all_mood_names()
            current_mood_name = target_chat_instance.current_mood or "нейтрально" # Use current mood from DB

            if not available_moods:
                 reply_text = f"у личности '{local_persona.name}' не настроены настроения."
                 logger.warning(f"Persona {local_persona.name} has no moods defined.")
                 try: # Send reply outside transaction
                     if is_callback: await update.callback_query.edit_message_text(reply_text)
                     else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
                 except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
                 return # No commit needed

        except SQLAlchemyError as e:
             logger.error(f"Database error fetching persona for /mood in chat {chat_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await message_or_callback_msg.reply_text("ошибка базы данных при загрузке данных личности.")
             return
        except Exception as e:
             logger.error(f"Unexpected error fetching persona for /mood in chat {chat_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await message_or_callback_msg.reply_text("непредвиденная ошибка при загрузке данных личности.")
             return

    # --- Determine Target Mood (outside DB transaction) ---
    available_moods_lower = {m.lower(): m for m in available_moods}
    mood_arg_lower = None
    target_mood_original_case = None
    persona_id_from_callback = None # For constructing callback data

    if is_callback and update.callback_query.data.startswith("set_mood_"):
         parts = update.callback_query.data.split('_')
         # Example: set_mood_радость_123 -> parts = ['set', 'mood', 'радость', '123']
         if len(parts) >= 4: # Need at least set_mood_name_id
              persona_id_from_callback = parts[-1]
              mood_arg_lower = "_".join(parts[2:-1]).lower() # Join parts between 'set_mood_' and ID
              if mood_arg_lower in available_moods_lower:
                  target_mood_original_case = available_moods_lower[mood_arg_lower]
         else:
              logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
    elif not is_callback:
        mood_text = ""
        if context.args:
             mood_text = " ".join(context.args) # Allow multi-word mood names from args
        elif update.message and update.message.text:
             # Allow changing mood just by sending the mood name as text
             possible_mood = update.message.text.strip()
             if possible_mood.lower() in available_moods_lower:
                  mood_text = possible_mood

        if mood_text:
            mood_arg_lower = mood_text.lower()
            if mood_arg_lower in available_moods_lower:
                target_mood_original_case = available_moods_lower[mood_arg_lower]

    # --- Perform Action (Set Mood or Show Keyboard) ---
    if target_mood_original_case and target_chat_instance:
         # Set the mood - this function handles its own commit/rollback
         set_mood_for_chat_bot(SessionLocal(), target_chat_instance.id, target_mood_original_case) # Use a new session
         reply_text = f"настроение для '{local_persona.name}' теперь: **{target_mood_original_case}**"
         try:
             if is_callback:
                 # Avoid edit if message is identical
                 if update.callback_query.message.text != reply_text:
                     await update.callback_query.edit_message_text(reply_text) # Default Markdown
                 else:
                     await update.callback_query.answer(f"Настроение: {target_mood_original_case}") # Confirm with answer
             else:
                 await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove()) # Default Markdown
         except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
         logger.info(f"Mood for persona {local_persona.name} in chat {chat_id} set to {target_mood_original_case}.")
    else:
         # Show keyboard
         # Use the persona ID obtained earlier
         p_id = local_persona.id if local_persona else persona_id_from_callback or "unknown"
         if p_id == "unknown": logger.warning("Could not determine persona ID for mood callback generation.")

         keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m.lower()}_{p_id}")] for m in available_moods]
         reply_markup = InlineKeyboardMarkup(keyboard)

         if mood_arg_lower: # If user provided an invalid mood
             reply_text = f"не знаю настроения '{mood_arg_lower}' для '{local_persona.name}'. выбери из списка:"
             logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id}. Sent mood selection.")
         else: # Just /mood command or callback without target
             reply_text = f"текущее настроение: **{current_mood_name}**. выбери новое для '{local_persona.name}':"
             logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

         try:
             if is_callback:
                  query = update.callback_query
                  # Avoid editing if text and markup are identical
                  if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                       await query.edit_message_text(reply_text, reply_markup=reply_markup) # Default Markdown
                  else:
                       await query.answer() # Answer callback without editing
             else:
                  await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup) # Default Markdown
         except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the context history for the active persona in the chat."""
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset < User {user_id} ({username}) in Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_info_tuple:
                await update.message.reply_text("в этом чате нет активной личности для сброса.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            persona, _, owner_user = persona_info_tuple

            # --- Authorization Check ---
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("только владелец личности может сбросить её память.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id}")
                 await update.message.reply_text("ошибка: не найден экземпляр бота для сброса.")
                 return # No commit needed

            # --- Perform Deletion ---
            # This DELETE operation happens within the transaction
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')

            # --- Commit ---
            db.commit() # Commit the deletion
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id} by user {user_id}.")
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("ошибка базы данных при сбросе контекста.")
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("ошибка при сбросе контекста.")


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /createpersona command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    args = context.args
    if not args:
        await update.message.reply_text(
            "формат: `/createpersona <имя> [описание]`\n"
            "_имя обязательно (2-50 симв.), описание нет (до 1500 симв.)._"
            # Default ParseMode is Markdown
        )
        return

    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None # Default handled by create_persona_config

    # Basic validation
    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text("имя личности: 2-50 символов.", reply_markup=ReplyKeyboardRemove())
         return
    if persona_description and len(persona_description) > 1500:
         await update.message.reply_text("слишком длинное описание (макс. 1500 символов).", reply_markup=ReplyKeyboardRemove())
         return

    with next(get_db()) as db:
        try:
            # Get user and check limits - use eager loading for can_create_persona
            user = get_or_create_user(db, user_id, username)
            # Explicitly load the relationship needed by the property for the check
            # It's often better to query the count directly if that's all you need
            current_persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()

            if current_persona_count >= user.persona_limit:
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_persona_count}/{user.persona_limit}).")
                 status_text = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                 text = (
                     f"упс! достигнут лимит личностей ({current_persona_count}/{user.persona_limit}) для статуса **{status_text}**. 😟\n"
                     f"чтобы создавать больше, используй /subscribe"
                 )
                 await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove()) # Default Markdown
                 # No commit needed, nothing changed yet
                 return

            # Check for existing persona name (case-insensitive)
            # get_persona_by_name_and_owner uses func.lower for check
            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                await update.message.reply_text(f"личность с именем '{persona_name}' уже есть. выбери другое.", reply_markup=ReplyKeyboardRemove())
                # No commit needed
                return

            # Create persona - function handles commit/rollback internally
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            await update.message.reply_text(
                f"✅ личность '{new_persona.name}' создана!\n"
                f"id: `{new_persona.id}`\n"
                # f"описание: {new_persona.description}\n\n" # Keep it concise maybe
                f"добавь в чат или управляй через /mypersonas"
                # Default ParseMode is Markdown
            )
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError: # Catch potential race condition if name check fails
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             # Rollback handled by create_persona_config or context manager
             await update.message.reply_text(f"ошибка: личность '{persona_name}' уже существует (возможно, гонка запросов). попробуй еще раз.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await update.message.reply_text("ошибка базы данных при создании личности.")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await update.message.reply_text("ошибка при создании личности.")


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists the user's personas with action buttons."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            # Eager load persona_configs when fetching the user for display
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()

            if not user_with_personas:
                logger.error(f"User {user_id} not found after get_or_create in my_personas.")
                await update.message.reply_text("Ошибка: не удалось найти пользователя.")
                # No commit needed
                return

            # Use the loaded personas
            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name.lower()) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            # Commit user creation if it happened
            db.commit()

            if not personas:
                await update.message.reply_text(
                    f"у тебя пока нет личностей (лимит: {persona_count}/{persona_limit}).\n"
                    "создай: `/createpersona <имя>`"
                    # Default ParseMode is Markdown
                )
                return

            text = f"твои личности ({persona_count}/{persona_limit}):\n"
            keyboard = []
            for p in personas:
                 # Use a non-functional callback for the name display row (or maybe edit?)
                 # Let's use edit as the action for clicking the name
                 keyboard.append([InlineKeyboardButton(f"👤 {p.name} (ID: {p.id})", callback_data=f"edit_persona_{p.id}")])
                 # Action buttons below
                 keyboard.append([
                     # InlineKeyboardButton("⚙️ Редакт.", callback_data=f"edit_persona_{p.id}"), # Covered by name click now
                     InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_persona_{p.id}"),
                     InlineKeyboardButton("➕ В этот чат", callback_data=f"add_bot_{p.id}") # Clarify action
                 ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            # Use default ParseMode (Markdown)
            await update.message.reply_text(text, reply_markup=reply_markup)
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("ошибка при загрузке списка личностей.")
        except Exception as e:
            logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("произошла ошибка при обработке команды /mypersonas.")


# Note: add_bot_to_chat handles commits internally because it involves multiple steps
# (deactivation, instance creation, linking, context clearing) that should ideally succeed together.
async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    """Adds a specified persona (BotInstance) to the current chat, deactivating any other active bot."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    chat_id = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id}"
    local_persona_id = persona_id # Use passed ID if available (e.g., from future direct calls)

    # --- Determine Persona ID ---
    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("Ошибка: неверный ID личности.", show_alert=True)
             return
    elif not is_callback:
         logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id}) with args: {context.args}")
         args = context.args
         if not args or len(args) != 1 or not args[0].isdigit():
             await message_or_callback_msg.reply_text(
                 "формат: `/addbot <id персоны>`\n"
                 "или используй кнопку '➕ В этот чат' из /mypersonas"
                 # Default ParseMode is Markdown
             )
             return
         try:
             local_persona_id = int(args[0])
         except ValueError:
             await message_or_callback_msg.reply_text("id личности должен быть числом.", reply_markup=ReplyKeyboardRemove())
             return

    if local_persona_id is None:
         logger.error("add_bot_to_chat: persona_id is None after processing input.")
         if is_callback: await update.callback_query.answer("Ошибка: ID личности не определен.", show_alert=True)
         else: await message_or_callback_msg.reply_text("Ошибка: ID личности не определен.")
         return

    # --- Acknowledge and Start Process ---
    if is_callback:
        await update.callback_query.answer("Добавляем личность...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # --- Database Operations ---
    # Use a single transaction for the entire linking process
    with next(get_db()) as db:
        try:
            # --- 1. Verify Persona Ownership ---
            # Use the function that checks ownership via telegram_id
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 response_text = f"личность с id `{local_persona_id}` не найдена или не твоя."
                 # No commit needed, just inform user
                 if is_callback: await update.callback_query.edit_message_text(response_text) # Default Markdown
                 else: await message_or_callback_msg.reply_text(response_text, reply_markup=ReplyKeyboardRemove()) # Default Markdown
                 return

            # --- 2. Deactivate Existing Bot in Chat ---
            # Find any currently active ChatBotInstance in this chat
            existing_active_link = db.query(ChatBotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).options(
                joinedload(ChatBotInstance.bot_instance_ref) # Need bot_instance to check its persona_config_id
            ).first()

            if existing_active_link:
                # Check if the existing active link is for the *same* persona
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    response_text = f"личность '{persona.name}' уже активна в этом чате."
                    # No commit needed, already in desired state
                    if is_callback: await update.callback_query.answer(response_text, show_alert=True)
                    else: await message_or_callback_msg.reply_text(response_text, reply_markup=ReplyKeyboardRemove())
                    return
                else:
                    # Deactivate the *different* currently active persona
                    logger.info(f"Deactivating previous bot instance {existing_active_link.bot_instance_id} in chat {chat_id} before activating {local_persona_id}.")
                    existing_active_link.active = False
                    # Stage this change, don't commit yet
                    # flag_modified(existing_active_link, "active") # SQLAlchemy tracks this

            # --- 3. Find or Create BotInstance ---
            # We need the owner's internal ID for creating BotInstance if needed
            owner_internal_id = persona.owner_id # Already loaded via get_persona_by_id_and_owner

            # Find if a BotInstance already exists for this specific PersonaConfig
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                 # Create BotInstance if it doesn't exist
                 # create_bot_instance handles its own commit, which we want to avoid here.
                 # Let's create it manually within this transaction.
                 logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                 bot_instance = BotInstance(
                     owner_id=owner_internal_id,
                     persona_config_id=local_persona_id,
                     name=f"Inst:{persona.name}"[:50] # Example name, limit length
                 )
                 db.add(bot_instance)
                 db.flush() # Flush to get the bot_instance ID if needed immediately (though link_bot does query)
                 db.refresh(bot_instance) # Refresh to get ID
                 logger.info(f"Staged creation of BotInstance {bot_instance.id} for persona {local_persona_id}")


            # --- 4. Link BotInstance to Chat (Create or Reactivate) ---
            # link_bot_instance_to_chat handles finding/creating/reactivating the ChatBotInstance link
            # AND clearing context upon activation/creation. Avoid its internal commit.

            # --- 4a. Find or Create/Reactivate ChatLink Manually ---
            chat_link = db.query(ChatBotInstance).filter(
                ChatBotInstance.chat_id == chat_id,
                ChatBotInstance.bot_instance_id == bot_instance.id
            ).first()

            needs_context_clear = False
            if chat_link:
                if not chat_link.active:
                    logger.info(f"Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance.id} in chat {chat_id}")
                    chat_link.active = True
                    chat_link.current_mood = "нейтрально"
                    chat_link.is_muted = False
                    needs_context_clear = True
                else:
                     logger.debug(f"ChatBotInstance link for bot {bot_instance.id} in chat {chat_id} is already active.")
                     # Should we clear context even if already active? Maybe not by default.
                     # needs_context_clear = True # Uncomment if context should always be cleared on /addbot
            else:
                logger.info(f"Creating new ChatBotInstance link for bot {bot_instance.id} in chat {chat_id}")
                chat_link = ChatBotInstance(
                    chat_id=chat_id,
                    bot_instance_id=bot_instance.id,
                    active=True,
                    current_mood="нейтрально",
                    is_muted=False
                )
                db.add(chat_link)
                needs_context_clear = True # Clear context for new links

            # --- 4b. Clear Context if Needed ---
            if needs_context_clear and chat_link:
                 # Flush to ensure chat_link has an ID if it was just created
                 db.flush()
                 db.refresh(chat_link)
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 logger.debug(f"Cleared {deleted_ctx} context messages for chat_bot_instance {chat_link.id} upon linking/reactivation.")


            # --- 5. FINAL COMMIT for add_bot_to_chat ---
            db.commit() # Commit all changes: deactivation, instance creation, linking, context clearing
            logger.info(f"Committed changes for add_bot_to_chat: Linked BotInstance {bot_instance.id} (Persona {local_persona_id}) to chat {chat_id}. ChatBotInstance ID: {chat_link.id if chat_link else 'N/A'}")

            # --- 6. Notify User ---
            response_text = f"✅ личность '{persona.name}' (id: `{local_persona_id}`) активирована в этом чате!"
            if needs_context_clear: response_text += " Память очищена."
            await context.bot.send_message(chat_id=chat_id, text=response_text, reply_markup=ReplyKeyboardRemove()) # Default Markdown

            if is_callback:
                 try: # Attempt to delete the original message with the buttons
                      await update.callback_query.delete_message()
                 except Exception as del_err:
                      logger.warning(f"Could not delete callback message after adding bot: {del_err}")


        except IntegrityError as e:
             # Rollback handled by context manager
             logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
        except SQLAlchemyError as e:
             # Rollback handled by context manager
             logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="ошибка базы данных при добавлении бота.")
        except Exception as e:
             # Rollback handled by context manager
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="ошибка при активации личности.")


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses (inline keyboard callbacks)."""
    query = update.callback_query
    if not query or not query.data: return

    # --- Basic Info ---
    chat_id_obj = query.message.chat if query.message else None
    chat_id = str(chat_id_obj.id) if chat_id_obj else "Unknown Chat"
    user = query.from_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    # --- Route callbacks ---
    # Note: Answer callbacks early if the subsequent handler will edit the message.
    # If the handler only performs an action without editing, answer there.

    # Handlers that typically EDIT the message:
    if data == "subscribe_info":
        await query.answer() # Answer early
        await subscribe(update, context, from_callback=True)
    elif data == "view_tos":
        await query.answer() # Answer early
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer() # Answer early
        await confirm_pay(update, context)
    elif data == "subscribe_pay":
        await query.answer("Создаю ссылку на оплату...") # Specific answer before potentially long operation
        await generate_payment_link(update, context) # This function edits the message

    # Handlers that perform an action and might send NEW messages or just answer:
    elif data.startswith("set_mood_"):
        await query.answer() # Answer early, mood() will edit or send confirmation
        await mood(update, context)
    elif data.startswith("add_bot_"):
        # add_bot_to_chat handles its own answers/edits/deletes
        await add_bot_to_chat(update, context) # No persona_id needed, extracted inside
    elif data.startswith("dummy_"): # Example for non-functional buttons
        await query.answer() # Just acknowledge the press

    # --- Conversation Handler Callbacks ---
    # These patterns should match the ones defined in ConversationHandler setup in main.py
    elif data.startswith(("edit_persona_", "delete_persona_", "edit_field_", "edit_mood", "deletemood", "cancel_edit", "edit_persona_back", "delete_persona_confirm_", "delete_persona_cancel")):
        # Don't answer here. The ConversationHandler is responsible for managing state
        # and should ideally answer the callback within its own logic (e.g., after editing a message).
        # If the ConversationHandler *doesn't* answer, the callback might time out visually for the user.
        # We rely on the ConversationHandler steps (like edit_persona_choice, delete_persona_confirmed)
        # calling query.answer() or editing the message.
        logger.debug(f"Callback '{data}' routed to ConversationHandler.")
        pass # Let ConversationHandler take over

    # --- Fallback for Unhandled Callbacks ---
    else:
        logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
        try:
             # Provide feedback that the button press was received but not understood
             await query.answer("Неизвестное действие", show_alert=False)
        except Exception as e:
             # Log if answering fails (e.g., query expired)
             logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


# --- Subscription / Profile Handlers ---

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays user profile with subscription status and limits."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            # Ensure limits are up-to-date before display
            check_and_update_user_limits(db, user) # Modifies user object in session

            # Get persona count
            persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0

            # Commit potential user creation/update and limit reset
            db.commit()
            logger.debug(f"Committed changes for /profile fetch for user {user_id}.")

            # Now display info using the potentially updated user object
            # Re-fetch might be safer if commit modified significantly, but usually okay
            # db.refresh(user) # Optional: refresh user state after commit

            is_active_subscriber = user.is_active_subscriber # Use property
            status = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            expires_text = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active_subscriber and user.subscription_expires_at else "нет активной подписки"

            text = (
                f"👤 **твой профиль**\n\n"
                f"статус: **{status}**\n"
                f"{expires_text}\n\n"
                f"**лимиты:**\n"
                f"сообщения сегодня: {user.daily_message_count}/{user.message_limit}\n"
                f"создано личностей: {persona_count}/{user.persona_limit}\n\n"
            )
            if not is_active_subscriber:
                text += "🚀 хочешь больше? жми /subscribe !"

            # Use default ParseMode (Markdown)
            await update.message.reply_text(text)

        except SQLAlchemyError as e:
             logger.error(f"Database error during /profile for user {user_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await update.message.reply_text("ошибка базы данных при загрузке профиля.")
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("ошибка при обработке команды /profile.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    """Handles /subscribe command or callback to show subscription info."""
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update_or_reply = update.callback_query.message if from_callback else update.message
    if not message_to_update_or_reply: return

    # Check Yookassa config readiness
    yookassa_ready = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit())

    text = ""
    reply_markup = None
    if not yookassa_ready:
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        # No buttons if payment is unavailable
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
    else:
        text = (
            f"✨ **премиум подписка ({config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}/мес)** ✨\n\n"
            "получи максимум возможностей:\n"
            f"✅ **{config.PAID_DAILY_MESSAGE_LIMIT}** сообщений в день (вместо {config.FREE_DAILY_MESSAGE_LIMIT})\n"
            f"✅ **{config.PAID_PERSONA_LIMIT}** личностей (вместо {config.FREE_PERSONA_LIMIT})\n"
            f"✅ полная настройка всех промптов\n"
            f"✅ создание и редакт. своих настроений\n"
            # f"✅ приоритетная поддержка (если будет)\n\n" # Add future benefits here
            f"подписка действует {config.SUBSCRIPTION_DURATION_DAYS} дней."
        )
        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            # Changed flow: Go to confirmation screen first
            [InlineKeyboardButton("✅ Принять Условия и перейти к оплате", callback_data="confirm_pay")]
            # [InlineKeyboardButton("✅ Принять и оплатить", callback_data="subscribe_pay")] # Old direct pay button
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if from_callback:
            query = update.callback_query
            # Edit only if content or markup differs
            if query.message.text != text or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(text, reply_markup=reply_markup) # Default Markdown
            # else: await query.answer() # Answered in handle_callback_query
        else:
            # Default ParseMode is Markdown
            await message_to_update_or_reply.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        if from_callback:
            # Fallback for failed edit
            try:
                # Use default Markdown
                await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=text, reply_markup=reply_markup)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")


async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the callback to view Terms of Service."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    # ToS URL should be stored in bot_data during initialization (post_init)
    tos_url = context.bot_data.get('tos_url')

    text = ""
    keyboard = []
    if tos_url:
        keyboard = [
            [InlineKeyboardButton("📜 Открыть Соглашение (Telegra.ph)", url=tos_url)],
            [InlineKeyboardButton("⬅️ Назад к Подписке", callback_data="subscribe_info")]
        ]
        text = "Пожалуйста, ознакомьтесь с Пользовательским Соглашением, открыв его по ссылке ниже:"
    else:
        logger.error(f"ToS URL not found in bot_data for user {user_id}.")
        text = "❌ Не удалось загрузить ссылку на Пользовательское Соглашение. Попробуйте позже или запросите у администратора."
        keyboard = [[InlineKeyboardButton("⬅️ Назад к Подписке", callback_data="subscribe_info")]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        # Edit only if needed
        if query.message.text != text or query.message.reply_markup != reply_markup:
            # Use default Markdown, disable preview for cleaner look
            await query.edit_message_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        # else: await query.answer() # Answered in handle_callback_query
    except Exception as e:
        logger.error(f"Failed to show ToS link/error to user {user_id}: {e}")
        # Provide feedback via answer if edit fails
        await query.answer("Не удалось отобразить информацию о соглашении.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the callback after user agrees to ToS, shows final pay button."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit())

    text = ""
    reply_markup = None

    if not yookassa_ready:
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]])
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        text = (
             "✅ Отлично!\n\n"
             "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
             "[Пользовательским Соглашением]" # Link it if possible
             f"({tos_url if tos_url else '#'})." # Add link if URL exists
             "\n\n👇"
        )
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]
        ]
        # Link to ToS again for reference
        if tos_url:
             keyboard.append([InlineKeyboardButton("📜 Условия использования (прочитано)", url=tos_url)])
        # Add back button
        keyboard.append([InlineKeyboardButton("⬅️ Назад к описанию", callback_data="subscribe_info")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Edit only if needed
        if query.message.text != text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                # Use default Markdown
                disable_web_page_preview=not bool(tos_url) # Disable preview if URL is missing
            )
        # else: await query.answer() # Answered in handle_callback_query
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")


# <<< ИЗМЕНЕНО: Добавлено больше логов и проверок конфигурации >>>
async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates and sends the Yookassa payment link."""
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    # --- Verify Yookassa Readiness ---
    yookassa_ready = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly (Shop ID or Secret Key missing/invalid) for payment generation.")
        await query.edit_message_text("❌ ошибка: сервис оплаты не настроен правильно (отсутствуют данные).", reply_markup=None)
        return

    current_shop_id_str = config.YOOKASSA_SHOP_ID
    current_secret_key = config.YOOKASSA_SECRET_KEY
    current_shop_id_int = 0

    # --- Configure Yookassa SDK ---
    try:
        current_shop_id_int = int(current_shop_id_str)
        # Check if SDK needs configuration/re-configuration
        # It might be configured globally, but checking here ensures it's set for this request
        if not YookassaConfig.secret_key or YookassaConfig.account_id != current_shop_id_int:
             logger.info(f"Configuring Yookassa SDK within generate_payment_link. Shop ID: {current_shop_id_str}, Secret Key Set: {bool(current_secret_key)}")
             YookassaConfig.configure(current_shop_id_int, current_secret_key)
             # Verify after configuring
             if YookassaConfig.account_id == current_shop_id_int and YookassaConfig.secret_key == current_secret_key:
                 logger.info("Yookassa SDK configured successfully.")
             else:
                 logger.error("Yookassa SDK configuration check FAILED after attempt.")
                 raise RuntimeError("Failed to verify Yookassa SDK configuration.")
        else:
             logger.info("Yookassa SDK already configured correctly.")

    except ValueError:
        logger.error(f"Yookassa Shop ID '{current_shop_id_str}' is not a valid integer.")
        await query.edit_message_text("❌ ошибка: неверный формат ID магазина платежной системы.", reply_markup=None)
        return
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK in generate_payment_link: {conf_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка конфигурации платежной системы.", reply_markup=None)
        return

    # --- Prepare Payment Details ---
    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium @NunuAiBot {config.SUBSCRIPTION_DURATION_DAYS} дней (User: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)} # Pass TG ID for webhook identification
    bot_username = "YourBotUsername" # Fallback username
    try:
        me = await context.bot.get_me()
        bot_username = me.username or bot_username
    except Exception as e:
        logger.warning(f"Could not get bot username dynamically: {e}")
    # Return URL after payment (can be bot link or specific page)
    return_url = f"https://t.me/{bot_username}"

    # --- Prepare Receipt ---
    try:
        # Ensure price is formatted correctly (e.g., "699.00")
        price_str = f"{config.SUBSCRIPTION_PRICE_RUB:.2f}"
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум @{bot_username} {config.SUBSCRIPTION_DURATION_DAYS} дн.", # Keep description short
                "quantity": 1.0,
                "amount": {"value": price_str, "currency": config.SUBSCRIPTION_CURRENCY},
                "vat_code": "1", # 1 = VAT exempt. Check Yookassa docs for correct code based on your tax status.
                "payment_mode": "full_prepayment", # Or "full_payment" etc.
                "payment_subject": "service" # Or "commodity", "intellectual_activity" etc.
            })
        ]
        # Use a placeholder email if real one isn't available/required
        customer_email = f"user_{user_id}@telegram.bot"
        receipt_data = Receipt({
            "customer": {"email": customer_email},
            "items": receipt_items,
            # "tax_system_code": "1" # Optional: Specify your tax system if needed (e.g., 1 for OSN)
        })
        logger.debug("Receipt data prepared.")
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при формировании данных чека.", reply_markup=None)
        return

    # --- Create Payment Request ---
    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": price_str, "currency": config.SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata) \
            .set_receipt(receipt_data)
        # builder.set_payment_method_data({"type": "bank_card"}) # Optional: Limit payment methods

        request = builder.build()
        logger.info(f"Attempting Yookassa Payment.create. Shop ID: {YookassaConfig.account_id}, Idempotence: {idempotence_key}")
        logger.debug(f"Payment request built: {request.json()}")

        # --- Execute Blocking Call in Thread ---
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        # --- Process Response ---
        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid/incomplete response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = "❌ не удалось получить ссылку от платежной системы"
             if payment_response and payment_response.status: error_message += f" (статус: {payment_response.status})"
             error_message += ".\nПопробуй позже."
             await query.edit_message_text(error_message, reply_markup=None)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        # Add back button?
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ ссылка для оплаты создана!\n\n"
            "нажми кнопку ниже для перехода к оплате. после успеха подписка активируется (может занять пару минут).",
            reply_markup=reply_markup
        )
    except Exception as e:
        # Catch potential errors from Payment.create or builder
        logger.error(f"Error during Yookassa payment creation or request building for user {user_id}: {e}", exc_info=True)
        user_message = "❌ не удалось создать ссылку для оплаты. "
        # Try to parse Yookassa specific API errors
        if hasattr(e, 'response') and hasattr(e.response, 'json'):
            try:
                err_data = e.response.json()
                err_type = err_data.get('type')
                err_code = err_data.get('code')
                err_desc = err_data.get('description') or err_data.get('message')
                if err_type == 'error':
                    logger.error(f"Yookassa API Error details: Code={err_code}, Desc={err_desc}, Data={err_data}")
                    user_message += f"({err_desc or err_code or 'детали в логах'})"
            except Exception: pass # Ignore if parsing response fails
        elif isinstance(e, httpx.RequestError):
             user_message += "Проблема с сетевым подключением к ЮKassa."
        else:
             user_message += "Произошла непредвиденная ошибка."
        user_message += "\nПопробуй еще раз позже или свяжись с поддержкой."

        try: # Send error message back to user
            await query.edit_message_text(user_message, reply_markup=None)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")

# --- Conversation Handlers (Edit/Delete Persona/Moods) ---
# These handlers manage their own state and commits/rollbacks for their specific multi-step processes.
# They are generally okay as separate units of work, but ensure errors are handled gracefully within them.

# --- Edit Persona ---
async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the edit persona conversation, fetching persona and showing main menu."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Start fresh for this conversation

    # Fetch persona within a transaction, but don't commit here
    with next(get_db()) as db:
        try:
            # Use the function that checks ownership via telegram_id and loads owner
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg) # Default Markdown
                 else: await update.effective_message.reply_text(error_msg) # Default Markdown
                 return ConversationHandler.END

            # Store ID in user_data for conversation state
            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config) # Build keyboard based on fetched data
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:"

            # Send or edit the message
            if update.callback_query:
                 query = update.callback_query
                 try: # Edit existing message from button press
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup) # Default Markdown
                      else:
                           await query.answer() # Message already correct
                 except Exception as edit_err: # Handle potential edit errors (e.g., message too old)
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup) # Default Markdown
            else: # Send new message if started via command
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup) # Default Markdown

            logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
            return EDIT_PERSONA_CHOICE # Proceed to choice state

        except SQLAlchemyError as e:
             logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await context.bot.send_message(chat_id, "ошибка базы данных при начале редактирования.")
             return ConversationHandler.END
        except Exception as e:
             logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await context.bot.send_message(chat_id, "непредвиденная ошибка.")
             return ConversationHandler.END

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nили используй кнопку из /mypersonas") # Default Markdown
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
         await update.message.reply_text("ID должен быть числом.")
         return ConversationHandler.END
    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for 'Edit' button press from /mypersonas."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...")
    try:
        # Extract persona ID from callback data (e.g., "edit_persona_123")
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        await query.edit_message_text("Ошибка: неверный ID личности в кнопке.")
        return ConversationHandler.END

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles user's choice from the main edit menu (buttons)."""
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE # Stay in choice state if no data

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, Persona ID from context: {persona_id}, Callback data: {data} ---")

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text("ошибка: сессия редактирования потеряна (нет id). начни снова.", reply_markup=None)
         return ConversationHandler.END

    # Fetch persona and check premium status within a transaction
    persona_config = None
    is_premium_user = False
    owner_obj = None # To check premium status
    try:
        with next(get_db()) as db:
            # get_persona_by_id_and_owner already loads the owner via selectinload
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)

            if not persona_config or not persona_config.owner:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} or its owner not found/not owned.")
                await query.answer("Личность или владелец не найдены", show_alert=True)
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END

            owner_obj = persona_config.owner
            is_premium_user = owner_obj.is_active_subscriber # Use property

            # No commit needed, just fetching data

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text("ошибка базы данных при проверке данных.", reply_markup=None)
         return EDIT_PERSONA_CHOICE # Stay in the same state, maybe user retries
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         # Rollback handled by context manager
         await query.answer("Непредвиденная ошибка", show_alert=True)
         await query.edit_message_text("Непредвиденная ошибка.", reply_markup=None)
         return ConversationHandler.END # End convo on unexpected error

    # --- Handle Callback Data ---
    await query.answer() # Answer most callbacks here unless handled specifically below

    if data == "cancel_edit":
        return await edit_persona_cancel(update, context) # This handles cleanup and ends convo

    if data == "edit_moods":
        if not is_premium_user and not is_admin(user_id): # Allow admin to edit moods
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer("⭐ Редактирование настроений доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay on the main edit menu
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             # Pass fetched persona_config to avoid re-fetching in the next step
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        field_display_name = FIELD_MAP.get(field, field)
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        # Check premium fields - allow admin override
        advanced_fields = ["should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        is_advanced_field = field in advanced_fields
        if is_advanced_field and not is_premium_user and not is_admin(user_id):
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             await query.answer(f"⭐ Поле '{field_display_name}' доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay on main edit menu

        # Store field to edit in user_data
        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        # Get current value for display
        # Use a default if the attribute doesn't exist for some reason
        default_vals = {"max_response_messages": 3}
        current_value = getattr(persona_config, field, default_vals.get(field, ""))
        current_value_display = str(current_value) # Convert to string for display

        # Handle specific input types
        if field == "max_response_messages":
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'** (число от 1 до 10):\n_текущее: {current_value_display}_", reply_markup=reply_markup) # Default Markdown
            return EDIT_MAX_MESSAGES # Go to specific state for number input
        else:
            # Truncate long values for display
            if len(current_value_display) > 300:
                current_value_display = current_value_display[:300] + "..."
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'**.\n_текущее:_\n`{current_value_display}`", reply_markup=reply_markup) # Default Markdown
            return EDIT_FIELD # Go to general text input state

    if data == "edit_persona_back":
         # User clicked back from a field input prompt or mood menu
         logger.info(f"User {user_id} pressed back button, returning to main edit menu for persona {persona_id}.")
         # We already have persona_config fetched
         keyboard = await _get_edit_persona_keyboard(persona_config)
         await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
         # Clear any field/mood state
         context.user_data.pop('edit_field', None)
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return EDIT_PERSONA_CHOICE # Return to main choice state

    # --- Fallback ---
    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    # Don't end conversation, just prompt again
    await query.message.reply_text("неизвестный выбор. попробуй еще раз.")
    return EDIT_PERSONA_CHOICE # Stay in choice state

async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text input when user is editing a standard text field."""
    if not update.message or not update.message.text: return EDIT_FIELD # Stay if no text
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}', Value='{new_value[:50]}...' ---")

    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна. начни сначала.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    field_display_name = FIELD_MAP.get(field, field)

    # --- Validation ---
    validation_error = None
    max_len = {
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len = {"name": 2} # Only name has min length currently

    if field in max_len and len(new_value) > max_len[field]:
        validation_error = f"слишком длинное значение для '{field_display_name}' (макс. {max_len[field]} символов)."
    if field in min_len and len(new_value) < min_len[field]:
        validation_error = f"слишком короткое значение для '{field_display_name}' (мин. {min_len[field]} символа)."
    # Add more specific validation if needed (e.g., check for placeholders in templates)

    if validation_error:
        logger.debug(f"Validation failed for field '{field}' update by user {user_id}: {validation_error}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error} попробуй еще раз:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD # Stay in this state for re-entry

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            # Re-fetch config within transaction to perform update
            # Use the function that checks ownership
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # --- Uniqueness Check (for 'name' field) ---
            if field == "name" and new_value.lower() != persona_config.name.lower():
                # Check if the new name (case-insensitive) is taken by another persona of the same user
                existing_persona_with_name = get_persona_by_name_and_owner(db, persona_config.owner_id, new_value)
                # Ensure the found persona is not the one we are currently editing
                if existing_persona_with_name and existing_persona_with_name.id != persona_id:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by persona {existing_persona_with_name.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    await update.message.reply_text(f"имя '{new_value}' уже занято другой твоей личностью. попробуй другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                    return EDIT_FIELD # Stay in edit field state

            # --- Update the Field ---
            setattr(persona_config, field, new_value)
            logger.debug(f"Field '{field}' updated in session for persona {persona_id}.")

            # --- Commit ---
            db.commit() # Commit the change
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")

            # --- Return to Main Edit Menu ---
            context.user_data.pop('edit_field', None) # Clear state
            # We need persona_config for the keyboard, refresh might be needed if name changed
            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
            return EDIT_PERSONA_CHOICE

    except IntegrityError as e: # Catch potential unique constraint errors if name check fails somehow
        logger.error(f"IntegrityError during update of field {field} for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("❌ ошибка: такое имя уже используется. Попробуй другое.")
        # Try to return to the input state
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"попробуй ввести другое значение для **'{field_display_name}'**:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD
    except SQLAlchemyError as e:
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         # Attempt to return to main edit menu gracefully
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear()
         return ConversationHandler.END

async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles input for the 'max_response_messages' field."""
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages" # Field name
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна (нет persona_id). начни снова.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # --- Validation ---
    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10): # Define valid range
            raise ValueError("Value out of range 1-10")
    except ValueError:
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text("неверное значение. введи **число от 1 до 10**:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MAX_MESSAGES # Stay in this state

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # --- Update Field ---
            persona_config.max_response_messages = new_value
            logger.debug(f"Field '{field}' updated in session for persona {persona_id}.")

            # --- Commit ---
            db.commit() # Commit the change
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            await update.message.reply_text(f"✅ макс. сообщений в ответе для **'{persona_config.name}'** установлено: **{new_value}**")

            # --- Return to Main Edit Menu ---
            db.refresh(persona_config) # Refresh needed for keyboard helper
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear()
         return ConversationHandler.END


async def _get_edit_persona_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
    """Helper function to build the main edit menu keyboard."""
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]

    # Get current value for max messages, default to 3
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)

    # Check premium status of the owner (should be loaded)
    is_premium = persona_config.owner.is_active_subscriber if persona_config.owner else False
    is_admin_user = is_admin(persona_config.owner.telegram_id) if persona_config.owner else False
    can_edit_advanced = is_premium or is_admin_user # Allow admin to edit all

    premium_marker = " ⭐" if not can_edit_advanced else "" # Show star only if feature is locked

    keyboard = [
        # Row 1: Basic Info
        [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"),
         InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
        # Row 2: System Prompt
        [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
        # Row 3: Advanced Prompts / Settings
        [InlineKeyboardButton(f"📊 Макс. ответов ({max_resp_msg}){premium_marker}", callback_data="edit_field_max_response_messages")],
        [InlineKeyboardButton(f"🤔 Промпт 'Отвечать?'{premium_marker}", callback_data="edit_field_should_respond_prompt_template")],
        [InlineKeyboardButton(f"💬 Промпт спама{premium_marker}", callback_data="edit_field_spam_prompt_template")],
        # Row 4: Media Prompts
        [InlineKeyboardButton(f"🖼️ Промпт фото{premium_marker}", callback_data="edit_field_photo_prompt_template"),
         InlineKeyboardButton(f"🎤 Промпт голоса{premium_marker}", callback_data="edit_field_voice_prompt_template")],
        # Row 5: Moods
        [InlineKeyboardButton(f"🎭 Настроения{premium_marker}", callback_data="edit_moods")],
        # Row 6: Cancel
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
    ]
    return keyboard

async def _get_edit_moods_keyboard_internal(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
     """Helper to build the mood editing keyboard."""
     if not persona_config: return []
     try:
         # Use the getter method which handles JSON errors
         mood_names = persona_config.get_mood_names()
         if not isinstance(mood_names, list): # Ensure it's a list
             mood_names = []
     except Exception as e:
         logger.error(f"Error getting mood names for persona {persona_config.id}: {e}", exc_info=True)
         mood_names = []

     keyboard = []
     if mood_names:
         # Sort moods alphabetically (case-insensitive) for consistent display
         sorted_moods = sorted(mood_names, key=str.lower)
         for mood_name in sorted_moods:
              # Sanitize mood name for callback data (remove problematic chars)
              # Allow letters (cyr/lat), numbers, hyphen, underscore
              safe_mood_name = re.sub(r'[^\wа-яА-ЯёЁ-]+', '', mood_name, flags=re.UNICODE)
              if not safe_mood_name:
                  logger.warning(f"Mood name '{mood_name}' became empty after sanitization, skipping.")
                  continue # Skip if name becomes empty

              # Button text: Capitalize first letter for display
              display_name = mood_name.capitalize()
              keyboard.append([
                  InlineKeyboardButton(f"✏️ {display_name}", callback_data=f"editmood_select_{safe_mood_name}"),
                  # Use safe name in delete callback, original name stored in user_data later
                  InlineKeyboardButton("🗑️", callback_data=f"deletemood_confirm_{safe_mood_name}")
              ])
     # Add "Add" and "Back" buttons
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад к ред. личности", callback_data="edit_persona_back")])
     return keyboard

# --- Helper Functions for Graceful Return ---
async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    """Attempts to return the user to the main edit menu after an error."""
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message_target = update.effective_message # Message where the error occurred or button was pressed
    if not message_target:
        logger.warning("Cannot return to edit menu: effective_message is None.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if persona_config:
                keyboard = await _get_edit_persona_keyboard(persona_config)
                await message_target.reply_text(f"возврат в меню редактирования **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
                # Clear potentially broken state
                context.user_data.pop('edit_field', None)
                context.user_data.pop('edit_mood_name', None)
                context.user_data.pop('delete_mood_name', None)
                return EDIT_PERSONA_CHOICE # Return to the main choice state
            else:
                logger.warning(f"Persona {persona_id} not found when trying to return to main edit menu.")
                await message_target.reply_text("Не удалось вернуться к меню редактирования (личность не найдена).")
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to return to main edit menu after error: {e}", exc_info=True)
        await message_target.reply_text("Не удалось вернуться к меню редактирования.")
        context.user_data.clear()
        return ConversationHandler.END

async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     """Attempts to return the user to the mood editing menu after an error."""
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     message_target = update.effective_message
     if not message_target:
         logger.warning("Cannot return to mood menu: effective_message is None.")
         context.user_data.clear()
         return ConversationHandler.END

     try:
         with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_config:
                 keyboard = await _get_edit_moods_keyboard_internal(persona_config)
                 await message_target.reply_text(f"возврат к управлению настроениями для **{persona_config.name}**:", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
                 # Clear potentially broken state
                 context.user_data.pop('edit_mood_name', None)
                 context.user_data.pop('delete_mood_name', None)
                 return EDIT_MOOD_CHOICE # Return to mood choice state
             else:
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await message_target.reply_text("Не удалось вернуться к меню настроений (личность не найдена).")
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
         await message_target.reply_text("Не удалось вернуться к меню настроений.")
         context.user_data.clear()
         return ConversationHandler.END

# --- Mood Editing Steps ---

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    """Displays the mood editing menu (list moods, add, back)."""
    query = update.callback_query
    # This state should only be reached via callback
    if not query or not query.message: return ConversationHandler.END

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.", reply_markup=None)
        return ConversationHandler.END

    # Use passed persona_config if available to avoid re-fetch
    local_persona_config = persona_config
    owner_obj = None
    if local_persona_config is None or not hasattr(local_persona_config, 'owner') or not local_persona_config.owner:
        logger.debug(f"Fetching persona config {persona_id} inside edit_moods_menu.")
        try:
            with next(get_db()) as db:
                # Ensure owner is loaded for premium check
                local_persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
                if not local_persona_config or not local_persona_config.owner:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} or owner not found/owned in edit_moods_menu fetch.")
                    await query.answer("Личность или владелец не найдены", show_alert=True)
                    await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                    context.user_data.clear()
                    return ConversationHandler.END
                owner_obj = local_persona_config.owner
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             # Rollback handled by context manager
             await query.answer("Ошибка базы данных", show_alert=True)
             await query.edit_message_text("Ошибка базы данных при загрузке настроений.", reply_markup=None)
             return await _try_return_to_edit_menu(update, context, user_id, persona_id) # Back to main menu
    else:
        owner_obj = local_persona_config.owner # Owner was already loaded

    # --- Premium Check ---
    if not owner_obj: # Should not happen if fetch worked
        logger.error(f"Owner object missing for persona {persona_id} in edit_moods_menu check.")
        await query.edit_message_text("Ошибка проверки владельца.", reply_markup=None)
        return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    is_premium_user = owner_obj.is_active_subscriber
    if not is_premium_user and not is_admin(user_id):
         logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
         await query.answer("⭐ Доступно по подписке", show_alert=True)
         # Return to main edit menu, not end convo
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    # --- Display Menu ---
    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = f"управление настроениями для **{local_persona_config.name}**:"

    try:
        # Edit only if needed
        if query.message.text != msg_text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(msg_text, reply_markup=reply_markup) # Default Markdown
        # else: await query.answer() # Already answered if needed
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         # Fallback: send new message if edit fails
         try: await query.message.reply_text(msg_text, reply_markup=reply_markup) # Default Markdown
         except Exception as send_e: logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE # Go to mood choice state


async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses within the mood editing menu."""
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE # Stay

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    # Fetch persona config for context within a transaction
    persona_config = None
    try:
        with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if not persona_config:
                 # Handle case where persona was deleted mid-conversation
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_mood_choice.")
                 await query.answer("Личность не найдена", show_alert=True)
                 await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                 context.user_data.clear()
                 return ConversationHandler.END
            # No commit needed, just fetching
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         # Rollback handled by context manager
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text("Ошибка базы данных.", reply_markup=None)
         return EDIT_MOOD_CHOICE # Stay in mood menu

    # --- Handle Mood Menu Actions ---
    await query.answer() # Answer most mood callbacks here

    if data == "edit_persona_back":
        # This action takes user back to the main persona edit menu
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        # Use fetched persona_config
        keyboard = await _get_edit_persona_keyboard(persona_config)
        await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
        # Clear mood-specific state
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE # Go back to the main choice state

    if data == "editmood_add":
        # Start process to add a new mood
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None # Indicate adding new, not editing existing
        context.user_data.pop('delete_mood_name', None) # Clear delete state
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel") # Back to mood list
        await query.edit_message_text("введи **название** нового настроения (1 слово, латиница/кириллица, цифры, дефис, подчеркивание):", reply_markup=InlineKeyboardMarkup([[back_button]])) # Default Markdown
        return EDIT_MOOD_NAME # Go to state expecting mood name input

    if data.startswith("editmood_select_"):
        # User selected an existing mood to edit
        safe_mood_name_from_callback = data.split("editmood_select_", 1)[1]

        # Try to find the original mood name using the safe name
        original_mood_name = safe_mood_name_from_callback # Default if not found
        current_prompt = "_не найдено_"
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            # Iterate through keys to find one that matches the safe name when sanitized
            for key, value in current_moods.items():
                sanitized_key = re.sub(r'[^\wа-яА-ЯёЁ-]+', '', key, flags=re.UNICODE)
                if sanitized_key == safe_mood_name_from_callback:
                    original_mood_name = key # Found the original name
                    current_prompt = value
                    break # Stop searching
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt = "_ошибка чтения промпта_"

        # Store the *original* name being edited (important for update)
        context.user_data['edit_mood_name'] = original_mood_name
        context.user_data.pop('delete_mood_name', None) # Clear delete state
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' (safe: {safe_mood_name_from_callback}) to edit for {persona_id}.")

        # Prepare message asking for the new prompt
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel") # Back to mood list
        prompt_display = current_prompt if len(current_prompt) < 300 else current_prompt[:300] + "..."
        display_name = original_mood_name.capitalize() # Use original name for display
        await query.edit_message_text(f"редактирование настроения: **{display_name}**\n\n_текущий промпт:_\n`{prompt_display}`\n\nотправь **новый текст промпта** (до 1500 симв.):", reply_markup=InlineKeyboardMarkup([[back_button]])) # Default Markdown
        return EDIT_MOOD_PROMPT # Go to state expecting prompt input

    if data.startswith("deletemood_confirm_"):
         # User pressed the trash icon next to a mood
         safe_mood_name_from_callback = data.split("deletemood_confirm_", 1)[1]

         # Try to find the original mood name for confirmation message
         original_mood_name = safe_mood_name_from_callback # Default
         try:
             current_moods = json.loads(persona_config.mood_prompts_json or '{}')
             for key in current_moods.keys():
                 sanitized_key = re.sub(r'[^\wа-яА-ЯёЁ-]+', '', key, flags=re.UNICODE)
                 if sanitized_key == safe_mood_name_from_callback:
                     original_mood_name = key
                     break
         except Exception: pass

         # Store the *original* name to be deleted
         context.user_data['delete_mood_name'] = original_mood_name
         context.user_data.pop('edit_mood_name', None) # Clear edit state
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' (safe: {safe_mood_name_from_callback}) for {persona_id}. Asking confirmation.")

         # Use safe name in the confirmation callback data
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name.capitalize()}'", callback_data=f"deletemood_delete_{safe_mood_name_from_callback}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")] # Back to mood list
            ]
         await query.edit_message_text(f"точно удалить настроение **'{original_mood_name.capitalize()}'**?", reply_markup=InlineKeyboardMarkup(keyboard)) # Default Markdown
         return DELETE_MOOD_CONFIRM # Go to confirmation state

    if data == "edit_moods_back_cancel":
         # User pressed back/cancel from name/prompt input or delete confirmation
         logger.debug(f"User {user_id} pressed back/cancel button, returning to mood list for {persona_id}.")
         # Clear potentially partial mood edit/delete state
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         # Pass fetched config to avoid re-fetch
         return await edit_moods_menu(update, context, persona_config=persona_config)

    # --- Fallback ---
    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text("неизвестный выбор настроения.")
    # Return to mood menu gracefully
    return await edit_moods_menu(update, context, persona_config=persona_config)


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text input when user is providing a name for a new mood."""
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()

    # Validation: 1-30 chars, letters (cyr/lat), numbers, hyphen, underscore. No spaces.
    mood_name_match = re.fullmatch(r'[\wа-яА-ЯёЁ-]{1,30}', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if not mood_name_match:
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("неверный формат имени. 1-30 символов: буквы (рус/лат), цифры, дефис, подчерк. без пробелов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME # Stay in this state

    mood_name = mood_name_raw # Use the validated name

    # --- Check Uniqueness (Case-Insensitive) ---
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text("ошибка: личность не найдена.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            current_moods = {}
            try: # Load existing moods safely
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} in mood name check, assuming empty.")

            # Check if name already exists (case-insensitive)
            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists (case-insensitive) for persona {persona_id}.")
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                await update.message.reply_text(f"настроение '{mood_name}' уже существует (регистр не важен). выбери другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                return EDIT_MOOD_NAME # Stay in name state

            # --- Store Name and Proceed to Prompt ---
            context.user_data['edit_mood_name'] = mood_name # Store the validated, original case name
            logger.debug(f"Stored new mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            await update.message.reply_text(f"отлично! теперь отправь **текст промпта** для настроения **'{mood_name}'** (до 1500 симв.):", reply_markup=InlineKeyboardMarkup([[back_button]])) # Default Markdown
            return EDIT_MOOD_PROMPT # Go to prompt input state

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("ошибка базы данных при проверке имени.", reply_markup=ReplyKeyboardRemove())
        return EDIT_MOOD_NAME # Stay in name state on DB error
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("непредвиденная ошибка.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text input for the mood prompt (new or existing mood)."""
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    # Get the mood name stored in the previous step (could be new or existing)
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}', Prompt='{mood_prompt[:50]}...' ---")

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # --- Validation ---
    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("промпт настроения: 1-1500 символов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_PROMPT # Stay in prompt state

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                context.user_data.clear()
                return ConversationHandler.END

            # Use the set_moods helper which handles JSON and flagging
            try:
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} when saving mood prompt, resetting moods.")
                 current_moods = {}

            # Add or update the mood using the stored name
            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods) # This flags the json field as modified
            logger.debug(f"Moods dictionary updated in session for persona {persona_id}.")

            # --- Commit ---
            db.commit() # Commit the mood update
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")

            # --- Clean up and Return ---
            context.user_data.pop('edit_mood_name', None) # Clear mood name from context
            await update.message.reply_text(f"✅ настроение **'{mood_name.capitalize()}'** сохранено!")

            # Return to mood menu
            db.refresh(persona_config) # Refresh to show updated list in menu
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("❌ ошибка базы данных при сохранении настроения.")
        # Attempt to return gracefully
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("❌ ошибка при сохранении настроения.")
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Yes, delete' confirmation button for a mood."""
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM # Stay if no data

    data = query.data
    # Get the original mood name (with correct case) stored earlier
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    # Verify state and callback data consistency
    expected_prefix = "deletemood_delete_"
    if not data.startswith(expected_prefix):
        logger.warning(f"User {user_id}: Unexpected data '{data}' in delete_mood_confirmed.")
        await query.answer("Неверная команда", show_alert=True)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id) # Back to mood menu

    if not mood_name_to_delete or not persona_id:
        logger.warning(f"User {user_id}: Missing state in delete_mood_confirmed. Mood='{mood_name_to_delete}', PersonaID='{persona_id}'")
        await query.answer("Ошибка сессии", show_alert=True)
        await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    # --- Database Update ---
    await query.answer("Удаляем...") # Acknowledge button press
    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name_to_delete}' for persona {persona_id}.")

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END

            try: # Load current moods
                current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} during mood deletion, assuming empty.")
                 current_moods = {}

            # --- Delete Mood ---
            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete]
                persona_config.set_moods(db, current_moods) # Update JSON and flag modification
                logger.debug(f"Mood '{mood_name_to_delete}' removed from dictionary in session for {persona_id}.")

                # --- Commit ---
                db.commit() # Commit deletion
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                await query.edit_message_text(f"🗑️ настроение **'{mood_name_to_delete.capitalize()}'** удалено.") # Default Markdown
            else:
                # Mood might have been deleted in another session/request
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id} (maybe already deleted).")
                await query.edit_message_text(f"настроение '{mood_name_to_delete.capitalize()}' не найдено (уже удалено?).", reply_markup=None)
                # No commit needed if not found

            # --- Clean Up and Return ---
            context.user_data.pop('delete_mood_name', None) # Clear state
            db.refresh(persona_config) # Refresh for menu
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await query.edit_message_text("❌ ошибка базы данных при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await query.edit_message_text("❌ ошибка при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the entire edit persona conversation."""
    message = update.effective_message # Original message or message from callback
    user_id = update.effective_user.id
    logger.info(f"User {user_id} cancelled persona edit conversation.")
    cancel_message = "редактирование отменено."

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer() # Acknowledge callback
            # Try to edit the message where the button was pressed
            if query.message and query.message.text != cancel_message:
                await query.edit_message_text(cancel_message, reply_markup=None)
        elif message:
            # If cancelled via command, reply to that command message
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        # Fallback: send a new message if edit/reply fails, especially if original message is old
        if message and message.chat:
            try:
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove())
            except Exception as send_e:
                logger.error(f"Failed to send fallback cancel message: {send_e}")

    # --- Clean up ---
    context.user_data.clear() # Clear all conversation state
    return ConversationHandler.END


# --- Delete Persona Conversation ---

async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the delete persona conversation, asking for confirmation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Start fresh

    # Fetch persona within a transaction
    with next(get_db()) as db:
        try:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg) # Default Markdown
                 else: await update.effective_message.reply_text(error_msg) # Default Markdown
                 return ConversationHandler.END

            # Store ID for confirmation step
            context.user_data['delete_persona_id'] = persona_id
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_config.name}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = (
                f"🚨 **ВНИМАНИЕ!** 🚨\n"
                f"ты уверен, что хочешь удалить личность **'{persona_config.name}'** (id: `{persona_id}`)?\n\n"
                f"это действие **НЕОБРАТИМО**! все настройки и экземпляры бота будут удалены."
            )

            # Send or edit message
            if update.callback_query:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup) # Default Markdown
                      else: await query.answer()
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup) # Default Markdown
            else:
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup) # Default Markdown

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
            return DELETE_PERSONA_CONFIRM # Go to confirmation state

        except SQLAlchemyError as e:
             logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await context.bot.send_message(chat_id, "ошибка базы данных при запросе на удаление.")
             return ConversationHandler.END
        except Exception as e:
             logger.error(f"Unexpected error starting delete persona {persona_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await context.bot.send_message(chat_id, "непредвиденная ошибка.")
             return ConversationHandler.END

async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /deletepersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/deletepersona <id>`\nили используй кнопку из /mypersonas") # Default Markdown
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return ConversationHandler.END
    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for 'Delete' button press from /mypersonas."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Запрос на удаление...")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        await query.edit_message_text("Ошибка: неверный ID личности в кнопке.")
        return ConversationHandler.END


async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Yes, delete' confirmation button."""
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    # Verify consistency
    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.answer("Ошибка сессии", show_alert=True)
         await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
         context.user_data.clear()
         return ConversationHandler.END

    await query.answer("Удаляем...") # Acknowledge confirmation
    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")

    deleted_ok = False
    persona_name_deleted = f"ID {persona_id}" # Fallback name

    # Perform deletion in a transaction
    try:
        with next(get_db()) as db:
             # We need the user's internal ID for the delete function
             user = db.query(User).filter(User.telegram_id == user_id).first()
             if not user:
                 # Should not happen if user initiated the convo, but check defensively
                 logger.error(f"User {user_id} not found during persona deletion confirmation.")
                 await query.edit_message_text("ошибка: пользователь не найден.", reply_markup=None)
                 context.user_data.clear()
                 return ConversationHandler.END

             # Get persona name for confirmation message *before* deleting
             persona_to_delete = db.query(PersonaConfig.name).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).scalar()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 # Call the delete function which handles the actual delete and commit
                 deleted_ok = delete_persona_config(db, persona_id, user.id)
                 if not deleted_ok:
                     # Error should have been logged in delete_persona_config
                     logger.error(f"delete_persona_config returned False for persona {persona_id}, user internal ID {user.id}.")
             else:
                 # Persona might have been deleted between confirmation and this step
                 logger.warning(f"User {user_id} confirmed delete, but persona {persona_id} not found (maybe already deleted). Assuming OK.")
                 deleted_ok = True # Treat as OK if already gone

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager

    # --- Notify User ---
    if deleted_ok:
        await query.edit_message_text(f"✅ личность '{persona_name_deleted}' удалена.", reply_markup=None)
    else:
        await query.edit_message_text("❌ не удалось удалить личность (ошибка базы данных или она уже удалена).", reply_markup=None)

    # --- Clean Up ---
    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'No, keep' cancellation button."""
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer() # Acknowledge
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")
    await query.edit_message_text("удаление отменено.", reply_markup=None)
    context.user_data.clear() # Clear state
    return ConversationHandler.END


# --- Mute/Unmute Handlers ---
# These perform single actions and commit immediately, which is acceptable.

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mutes the active bot in the chat."""
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id}")

    with next(get_db()) as db:
        try:
            # Fetch the active instance and owner info
            instance_info = get_persona_and_context_with_owner(chat_id, db)
            if not instance_info:
                await update.message.reply_text("В этом чате нет активной личности.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance

            # --- Authorization ---
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("Только владелец личности может ее заглушить.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id} during mute.")
                await update.message.reply_text("Ошибка: не найден объект связи с чатом.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            # --- Perform Mute ---
            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                # Commit this specific change immediately
                db.commit()
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona.name}' больше не будет отвечать в этом чате (но будет запоминать сообщения). Используйте /unmutebot, чтобы вернуть.", reply_markup=ReplyKeyboardRemove())
            else:
                # Already muted, inform user
                await update.message.reply_text(f"Личность '{persona.name}' уже заглушена в этом чате.", reply_markup=ReplyKeyboardRemove())
                # No commit needed

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("Ошибка базы данных при попытке заглушить бота.")
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("Непредвиденная ошибка при выполнении команды.")


async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unmutes the active bot in the chat."""
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id}")

    with next(get_db()) as db:
        try:
            # Fetch the active instance directly with owner/persona info
            active_instance = db.query(ChatBotInstance)\
                .options(
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.owner), # Load owner for auth check
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.persona_config) # Load persona for name
                )\
                .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
                .first()

            if not active_instance:
                await update.message.reply_text("В этом чате нет активной личности, которую можно размьютить.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            # Extract info for checks and messages
            owner_user = active_instance.bot_instance_ref.owner if active_instance.bot_instance_ref else None
            persona_name = active_instance.bot_instance_ref.persona_config.name if active_instance.bot_instance_ref and active_instance.bot_instance_ref.persona_config else "Неизвестная"

            if not owner_user:
                 logger.error(f"Could not find owner for active ChatBotInstance {active_instance.id} during unmute.")
                 await update.message.reply_text("Ошибка: не найден владелец активной личности.")
                 return # No commit needed

            # --- Authorization ---
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("Только владелец личности может снять заглушку.", reply_markup=ReplyKeyboardRemove())
                return # No commit needed

            # --- Perform Unmute ---
            if active_instance.is_muted:
                active_instance.is_muted = False
                # Commit this specific change immediately
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona_name}' снова может отвечать в этом чате.", reply_markup=ReplyKeyboardRemove())
            else:
                # Already unmuted
                await update.message.reply_text(f"Личность '{persona_name}' не была заглушена.", reply_markup=ReplyKeyboardRemove())
                # No commit needed

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("Ошибка базы данных при попытке вернуть бота к общению.")
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("Непредвиденная ошибка при выполнении команды.")
