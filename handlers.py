import logging
import httpx
import random
import asyncio
import re
import uuid
import json
import urllib.parse # <<< ДОБАВЛЕНО: для URL-кодирования в _get_edit_moods_keyboard_internal
from datetime import datetime, timezone, timedelta
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
# <<< ИЗМЕНЕНО: Добавлены импорты для проверки подписки >>>
from telegram.constants import ChatAction, ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError # <--- Убедись, что импорты есть

from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func
from typing import List, Dict, Any, Optional, Union, Tuple

from yookassa import Configuration, Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem

# <<< ИЗМЕНЕНО: Перемещен импорт config и связанных констант сюда >>>
import config # <<< ПЕРЕМЕЩЕН СЮДА
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

# +++ ДОБАВЛЕНА ФУНКЦИЯ ПРОВЕРКИ ПОДПИСКИ (С УЛУЧШЕННЫМ ЛОГИРОВАНИЕМ И ИСПРАВЛЕНИЕМ СТАТУСА) +++
async def check_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user is subscribed to the required channel."""
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID not set in config. Skipping subscription check.")
        return True # Skip check if no channel ID is configured

    if not update or not update.effective_user:
        logger.warning("check_channel_subscription called without valid Update or effective_user.")
        return False # Cannot check without user

    user_id = update.effective_user.id
    if is_admin(user_id): # Admins don't need to subscribe
        return True

    logger.debug(f"Checking subscription status for user {user_id} in channel {CHANNEL_ID}")
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        # <<< ИЗМЕНЕНО: Заменяем CREATOR на OWNER >>>
        allowed_statuses = [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]

        logger.debug(f"User {user_id} status in {CHANNEL_ID}: {member.status}")

        if member.status in allowed_statuses:
            logger.debug(f"User {user_id} IS subscribed to {CHANNEL_ID} (status: {member.status})")
            return True
        else:
            logger.info(f"User {user_id} is NOT subscribed to {CHANNEL_ID} (status: {member.status})")
            return False
    except Forbidden as e:
        logger.error(f"Forbidden error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}. Ensure bot is admin in the channel.")
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    escape_markdown_v2("Не удалось проверить подписку на канал. Убедитесь, что бот добавлен в канал как администратор.")
                )
            except Exception as send_err:
                 logger.error(f"Failed to send 'Forbidden' error message: {send_err}")
        return False # Deny access if check fails
    except BadRequest as e:
         # <<< ИЗМЕНЕНО: Более подробное логирование BadRequest >>>
         error_message = str(e).lower()
         logger.error(f"BadRequest checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}") # Логируем полную ошибку
         if "member list is inaccessible" in error_message:
             logger.error(f"-> Specific BadRequest: Member list is inaccessible. Bot might lack permissions or channel privacy settings restrictive?")
             if update.effective_message:
                 try: await update.effective_message.reply_text(escape_markdown_v2("Не удается получить доступ к списку участников канала для проверки подписки. Возможно, настройки канала не позволяют это сделать."))
                 except Exception as send_err: logger.error(f"Failed to send 'Member list inaccessible' error message: {send_err}")
         elif "user not found" in error_message:
             logger.info(f"-> Specific BadRequest: User {user_id} not found in channel {CHANNEL_ID}.")
             # Сообщение пользователю не требуется, функция просто вернет False
         else:
             # Другие BadRequest
             if update.effective_message:
                 try: await update.effective_message.reply_text(escape_markdown_v2("Произошла ошибка при проверке подписки (BadRequest). Попробуйте позже."))
                 except Exception as send_err: logger.error(f"Failed to send generic 'BadRequest' error message: {send_err}")
         return False # В любом случае BadRequest означает неудачную проверку
    except TelegramError as e:
        logger.error(f"Telegram error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}")
        if update.effective_message:
            try: await update.effective_message.reply_text(escape_markdown_v2("Произошла ошибка при проверке подписки. Попробуйте позже."))
            except Exception as send_err: logger.error(f"Failed to send 'TelegramError' message: {send_err}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking subscription for user {user_id} in channel {CHANNEL_ID}: {e}", exc_info=True)
        return False

async def send_subscription_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message asking the user to subscribe."""
    if not update or not update.effective_message:
        logger.warning("Cannot send subscription required message: invalid update or message object.")
        return

    channel_username = CHANNEL_ID.lstrip('@') if isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith('@') else None
    error_msg_raw = "Произошла ошибка при получении ссылки на канал."
    subscribe_text_raw = "Для использования бота необходимо подписаться на наш канал."
    button_text = "Перейти к каналу"
    keyboard = None

    if channel_username:
        subscribe_text_raw = f"Для использования бота необходимо подписаться на канал @{channel_username}."
        keyboard = [[InlineKeyboardButton(button_text, url=f"https://t.me/{channel_username}")]]
    elif isinstance(CHANNEL_ID, int):
         subscribe_text_raw = f"Для использования бота необходимо подписаться на наш основной канал."
         subscribe_text_raw += " Пожалуйста, найдите канал в поиске или через описание бота."
    else:
         logger.error(f"Invalid CHANNEL_ID format: {CHANNEL_ID}. Cannot generate subscription message correctly.")
         subscribe_text_raw = error_msg_raw

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        await update.effective_message.reply_text(escape_markdown_v2(subscribe_text_raw), reply_markup=reply_markup)
    except Exception as e:
         logger.error(f"Failed to send subscription required message: {e}")

# +++ КОНЕЦ ФУНКЦИЙ ПРОВЕРКИ ПОДПИСКИ +++


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

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

# <<< ИЗМЕНЕНО: Форматированный и ЭКРАНИРОВАННЫЙ текст для отправки через бота >>>
formatted_tos_text_for_bot = TOS_TEXT_RAW.format(
    subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
    subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
    subscription_currency=config.SUBSCRIPTION_CURRENCY
)
TOS_TEXT = escape_markdown_v2(formatted_tos_text_for_bot)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    # <<< ИЗМЕНЕНО: Обработка ошибок подписки >>>
    if isinstance(context.error, Forbidden) and CHANNEL_ID in str(context.error):
         logger.warning(f"Error handler caught Forbidden regarding channel {CHANNEL_ID}. Bot likely not admin.")
         # Не отправляем сообщение пользователю об этой ошибке, т.к. check_channel_subscription уже должен был это сделать
         return
    elif isinstance(context.error, BadRequest) and "chat member status is required" in str(context.error).lower():
         logger.warning(f"Error handler caught BadRequest likely related to missing channel membership check: {context.error}")
         # Не отправляем сообщение пользователю, возможно, это следствие отсутствия подписки
         return

    error_message = "упс... что-то пошло не так. попробуй еще раз позже."
    escaped_error_message = escape_markdown_v2(error_message)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(escaped_error_message)
        except BadRequest as e:
            logger.error(f"Failed to send ESCAPED error message (BadRequest): {e} - Original Text: '{error_message}'")
            try:
                 await update.effective_message.reply_text(error_message, parse_mode=None)
            except Exception as final_e:
                 logger.error(f"Failed even sending plain text error message: {final_e}")
        except Exception as e:
            logger.error(f"Failed to send escaped error message to user: {e}")


def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id)
    if not chat_instance:
        logger.debug(f"No active chatbot instance found for chat {chat_id}")
        return None

    bot_instance = chat_instance.bot_instance_ref
    # <<< ИЗМЕНЕНО: Проверка наличия всех связей >>>
    if not bot_instance:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance.")
         return None
    if not bot_instance.persona_config:
         logger.error(f"BotInstance {bot_instance.id} for chat {chat_id} is missing linked PersonaConfig.")
         return None
    if not bot_instance.owner:
         logger.error(f"BotInstance {bot_instance.id} for chat {chat_id} is missing linked Owner.")
         # Пытаемся загрузить owner через persona_config как запасной вариант
         if bot_instance.persona_config.owner:
              owner_user = bot_instance.persona_config.owner
              logger.warning(f"Loaded Owner {owner_user.id} via PersonaConfig for BotInstance {bot_instance.id}.")
         else:
              logger.error(f"Could not load Owner for BotInstance {bot_instance.id} via PersonaConfig either.")
              return None
    else:
        owner_user = bot_instance.owner

    persona_config = bot_instance.persona_config

    try:
        persona = Persona(persona_config, chat_instance)
    except ValueError as e:
         logger.error(f"Failed to initialize Persona for config {persona_config.id} in chat {chat_id}: {e}", exc_info=True)
         return None

    context_list = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context_list, owner_user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return escape_markdown_v2("ошибка: ключ api не настроен.")
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
    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages.")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
             resp = await client.post(url, json=payload, headers=headers)
        logger.debug(f"Langdock response status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        logger.debug(f"Langdock response data (first 200 chars): {str(data)[:200]}")

        full_response = ""
        if "content" in data and isinstance(data["content"], list):
            text_parts = [part.get("text", "") for part in data["content"] if part.get("type") == "text"]
            full_response = " ".join(text_parts)
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

        return full_response.strip()

    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return escape_markdown_v2("хм, кажется, я слишком долго думал... попробуй еще раз?")
    except httpx.HTTPStatusError as e:
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True)
        error_text = f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."
        return escape_markdown_v2(error_text)
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return escape_markdown_v2("не могу связаться с ai сейчас...")
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return escape_markdown_v2("произошла внутренняя ошибка при генерации ответа.")

async def process_and_send_response(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, chat_id: str, persona: Persona, full_bot_response_text: str, db: Session):
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return
    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}")

    # Add response to context first
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            logger.debug("AI response added to database context (pending commit).")
        except SQLAlchemyError as e:
            logger.error(f"DB Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            db.rollback() # Откатываем только добавление в контекст
            raise # Передаем ошибку выше
        except Exception as e:
            logger.error(f"Unexpected Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            raise
    else:
        logger.warning("Cannot add AI response to context, chat_instance is None.")
        # Не прерываем выполнение, просто не сможем добавить ответ в историю БД


    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)
    for gif in gif_links:
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()

    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    max_messages = 3
    if persona.config and hasattr(persona.config, 'max_response_messages'):
         max_messages = persona.config.max_response_messages or 3


    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        if text_parts_to_send:
             text_parts_to_send[-1] += "..." # Не требует экранирования, т.к. добавляется к уже обработанному тексту

    for gif in gif_links:
        try:
            await context.bot.send_animation(chat_id=chat_id, animation=gif)
            logger.info(f"Sent gif: {gif}")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logger.error(f"Error sending gif {gif} to chat {chat_id}: {e}", exc_info=True)

    if text_parts_to_send:
        chat_type = update.effective_chat.type if update and update.effective_chat else None
        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: continue
            if chat_type in ["group", "supergroup"]:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception as e:
                     logger.warning(f"Failed to send typing action to {chat_id}: {e}")
            try:
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id}: '{part[:50]}...'")
                 # Экранируем ответ от LLM перед отправкой
                 escaped_part = escape_markdown_v2(part)
                 await context.bot.send_message(chat_id=chat_id, text=escaped_part)
            except BadRequest as e:
                 logger.error(f"Error sending ESCAPED text part {i+1} to {chat_id} (BadRequest): {e} - Original: '{part[:100]}...' Escaped: '{escaped_part[:100]}...'")
                 # Пытаемся отправить без форматирования как запасной вариант
                 try:
                      await context.bot.send_message(chat_id=chat_id, text=part, parse_mode=None)
                      logger.info(f"Sent part {i+1} as plain text after MarkdownV2 failed.")
                 except Exception as plain_e:
                      logger.error(f"Failed to send part {i+1} even as plain text: {plain_e}")
                 break # Прерываем отправку остальных частей
            except Exception as e:
                 logger.error(f"Error sending text part {i+1} to {chat_id}: {e}", exc_info=True)
                 break # Прерываем отправку остальных частей

            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(0.4, 0.9))


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    # Формируем текст с Markdown V2
    text_to_send = (
        escape_markdown_v2(f"упс! 😕 лимит сообщений ({user.daily_message_count}/{user.message_limit}) на сегодня достигнут.\n\n") +
        "✨ **хочешь безлимита?** ✨\n" +
        escape_markdown_v2(f"подписка за {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}/мес дает:\n✅ ") +
        f"**{PAID_DAILY_MESSAGE_LIMIT}**" + escape_markdown_v2(" сообщений в день\n✅ до ") +
        f"**{PAID_PERSONA_LIMIT}**" + escape_markdown_v2(" личностей\n✅ полная настройка промптов и настроений\n\n") +
        escape_markdown_v2("👇 жми /subscribe или кнопку ниже!")
    )
    raw_text_for_log = "Limit exceeded message" # Placeholder

    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        target_chat_id = update.effective_chat.id if update.effective_chat else None
        if target_chat_id:
             await context.bot.send_message(target_chat_id, text=text_to_send, reply_markup=reply_markup)
        else:
             logger.warning(f"Could not send limit exceeded message to user {user.telegram_id}: no effective chat.")
    except BadRequest as e:
         logger.error(f"Failed sending limit message (BadRequest): {e} - Text Raw: '{raw_text_for_log}' Escaped: '{text_to_send[:100]}...'")
         try:
              if target_chat_id:
                  # Пытаемся отправить без разметки
                  plain_text = re.sub(r'\\(.)', r'\1', text_to_send) # Убираем экранирование
                  plain_text = plain_text.replace("**", "") # Убираем жирный шрифт
                  await context.bot.send_message(target_chat_id, plain_text, reply_markup=reply_markup, parse_mode=None)
         except Exception as final_e:
              logger.error(f"Failed sending limit message even plain: {final_e}")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not (update.message.text or update.message.caption): return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = update.message.text or update.message.caption or ""
    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona for chat {chat_id}. Ignoring.")
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id} ({owner_user.telegram_id})")

            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}). Not responding or saving context.")
                await send_limit_exceeded_message(update, context, owner_user)
                db.commit() # Commit limit update if needed
                return

            # --- Add user message to context ---
            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_added = True
                    logger.debug("Added user message to context.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error adding user message to context: {e_ctx}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения."))
                    return # Не продолжаем без сохранения контекста
                except Exception as e:
                    logger.error(f"Unexpected Error adding user message to context: {e}", exc_info=True)
                    await update.message.reply_text(escape_markdown_v2("ошибка при сохранении вашего сообщения."))
                    return
            else:
                logger.error("Cannot add user message to context, chat_instance is None.")
                await update.message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать сообщение с личностью."))
                return # Не продолжаем без chat_instance

            # --- Check if muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Message saved to context, but ignoring response.")
                db.commit() # Commit context and limit changes
                return

            # --- Handle potential mood change command ---
            available_moods = persona.get_all_mood_names()
            if message_text.lower() in map(str.lower, available_moods):
                 logger.info(f"Message '{message_text}' matched mood name. Changing mood.")
                 # Вызываем обработчик mood, он сам сделает commit если нужно
                 await mood(update, context, db=db, persona=persona)
                 # НЕ ДЕЛАЕМ commit здесь, т.к. mood() мог его уже сделать или откатить
                 return

            # --- Decide whether to respond (especially in groups) ---
            should_ai_respond = True
            if update.effective_chat.type in ["group", "supergroup"]:
                 if persona.should_respond_prompt_template:
                     should_respond_prompt = persona.format_should_respond_prompt(message_text)
                     if should_respond_prompt:
                         try:
                             logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id}...")
                             # Получаем свежий контекст для решения
                             context_for_should_respond = get_context_for_chat_bot(db, persona.chat_instance.id) if persona.chat_instance else []
                             decision_response = await send_to_langdock(
                                 system_prompt=should_respond_prompt,
                                 messages=context_for_should_respond
                             )
                             answer = decision_response.strip().lower() # Ответ да/нет не экранируем
                             logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")
                             if answer.startswith("д"):
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond based on AI='{answer}'.")
                                 should_ai_respond = True
                             elif random.random() < 0.05: # 5% chance to respond anyway
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond randomly despite AI='{answer}'.")
                                 should_ai_respond = True
                             else:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding NOT to respond based on AI='{answer}'.")
                                 should_ai_respond = False
                         except Exception as e:
                              logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                              logger.warning("Error in should_respond. Defaulting to respond.")
                              should_ai_respond = True # Default to responding on error
                     else:
                          logger.debug(f"No should_respond_prompt generated for persona {persona.name}. Defaulting to respond in group.")
                          should_ai_respond = True
                 else:
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic for message: {message_text[:50]}...")
                 db.commit() # Commit context and limit changes even if not responding
                 return

            # --- Get context for AI response generation ---
            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    # Получаем контекст СНОВА, т.к. should_respond мог его использовать и изменить
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error getting context for AI response: {e_ctx}", exc_info=True)
                     await update.message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа."))
                     return # Не продолжаем без контекста
            elif not context_added:
                 # Эта ветка не должна выполняться из-за проверок выше, но на всякий случай
                 logger.warning("Cannot generate AI response without updated context due to prior error.")
                 await update.message.reply_text(escape_markdown_v2("ошибка: не удалось сохранить ваше сообщение перед ответом."))
                 return

            # --- Generate and send AI response ---
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                logger.error(f"System prompt formatting failed for persona {persona.name}. Cannot generate response.")
                await update.message.reply_text(escape_markdown_v2("ошибка при подготовке ответа."))
                db.commit() # Commit changes up to this point
                return

            logger.debug("Formatted main system prompt.")

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            db.commit() # Commit context updates (user msg, should_respond AI msg, bot response) and limit changes
            logger.debug(f"Committed DB changes for handle_message cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message: {e}", exc_info=True)
             await update.message.reply_text(escape_markdown_v2("ошибка базы данных, попробуйте позже."))
             # Rollback handled by context manager
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка."))
            # Rollback handled by context manager


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple: return # Ignore if no active persona
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding or saving context.")
                await send_limit_exceeded_message(update, context, owner_user)
                db.commit()
                return

            prompt_template = None
            context_text_placeholder = "" # Не экранируем здесь, т.к. это для контекста, а не для отправки
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

            # --- Add placeholder to context ---
            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {context_text_placeholder}" # Неэкранированный текст для контекста
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_added = True
                    logger.debug(f"Added media placeholder to context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error adding media placeholder context: {e_ctx}", exc_info=True)
                     if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа."))
                     return
                except Exception as e:
                    logger.error(f"Unexpected Error adding media placeholder context: {e}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при сохранении информации о медиа."))
                    return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("системная ошибка: не удалось связать медиа с личностью."))
                 return

            # --- Check if muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Media saved to context, but ignoring response.")
                db.commit() # Commit context and limits
                return

            # --- Check if template exists ---
            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id} has no {media_type} template. Skipping response generation.")
                db.commit() # Commit context and limits
                return

            # --- Get context for AI ---
            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка при получении контекста для ответа на медиа."))
                    return
            elif not context_added:
                 # Should not happen due to checks above
                 logger.warning("Cannot generate AI media response without updated context.")
                 if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка: не удалось сохранить информацию о медиа перед ответом."))
                 return

            # --- Format prompt and get response ---
            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit() # Commit context and limits
                 return

            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            db.commit() # Commit context, limits, and bot response
            logger.debug(f"Committed DB changes for handle_media cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("ошибка базы данных."))
             # Rollback handled by context manager
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("произошла непредвиденная ошибка."))
            # Rollback handled by context manager


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
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ (делаем до отправки typing) +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    reply_text_raw = "Произошла ошибка инициализации текста." # Default raw text
    escaped_reply_text = escape_markdown_v2(reply_text_raw) # Default escaped text
    reply_markup = ReplyKeyboardRemove() # <<< ИЗМЕНЕНО: Инициализация разметки по умолчанию

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            db.commit() # Commit user creation/update if needed
            db.refresh(user) # Ensure user object has latest data from DB

            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                # Экранируем сообщение
                reply_text_raw = (
                    f"привет! я {persona.name}. я уже активен в этом чате.\n"
                    "используй /help для списка команд."
                )
                escaped_reply_text = escape_markdown_v2(reply_text_raw)
                # Для активной персоны кнопки не нужны
                reply_markup = ReplyKeyboardRemove()
            else:
                # Refresh user state and limits if needed
                now = datetime.now(timezone.utc)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if not user.last_message_reset or user.last_message_reset < today_start:
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit() # Commit reset if needed
                    db.refresh(user)

                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                escaped_status = escape_markdown_v2(status) # Экранируем статус
                expires_at_obj = user.subscription_expires_at
                escaped_expires_date = ""
                if expires_at_obj and isinstance(expires_at_obj, datetime):
                    # Используем формат с экранированными точками
                    expires_date_str = expires_at_obj.strftime('%d.%m.%Y')
                    escaped_expires_date = escape_markdown_v2(expires_date_str)
                expires_text = f" до {escaped_expires_date}" if user.is_active_subscriber and escaped_expires_date else ""

                # Загружаем количество персон явно, если user.persona_configs не загружен (хотя selectin должен)
                if 'persona_configs' not in user.__dict__ or user.persona_configs is None:
                    persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
                    logger.debug(f"Queried persona count ({persona_count}) directly in /start for user {user.id}")
                else:
                    persona_count = len(user.persona_configs)
                    logger.debug(f"Used loaded persona_configs count ({persona_count}) in /start for user {user.id}")


                # Аккуратное экранирование с сохранением Markdown
                escaped_greeting = escape_markdown_v2("привет! 👋 я бот для создания ai-собеседников (@NunuAiBot).\n\n")
                # Экранируем числа и слеши
                escaped_limits_info = escape_markdown_v2(f"личности: {persona_count}/{user.persona_limit} | сообщения: {user.daily_message_count}/{user.message_limit}\n\n")
                escaped_instruction1 = escape_markdown_v2(" - создай ai-личность.\n")
                escaped_instruction2 = escape_markdown_v2(" - посмотри своих личностей и управляй ими.\n")
                escaped_commands_info = escape_markdown_v2(" - детали статуса | ") + escape_markdown_v2(" - узнать о подписке") # Убран /help

                # Собираем финальный текст
                escaped_reply_text = (
                    escaped_greeting +
                    f"твой статус: **{escaped_status}**{expires_text}\n" + # Используем ** для статуса
                    escaped_limits_info +
                    "**начало работы:**\n" +
                    "`/createpersona <имя>`" + escaped_instruction1 + # Используем `code` для команд
                    "`/mypersonas`" + escaped_instruction2 +
                    "`/profile`" + escaped_commands_info
                 )
                reply_text_raw = "текст для старта (неэкранированный, содержит разметку)" # Placeholder

                # <<< ИЗМЕНЕНО: Добавляем кнопку Help >>>
                keyboard = [[InlineKeyboardButton("❓ Помощь (/help)", callback_data="show_help")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(escaped_reply_text, reply_markup=reply_markup) # Используем reply_markup

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg = "ошибка при загрузке данных. попробуй позже."
        await update.message.reply_text(escape_markdown_v2(error_msg))
    except NameError as ne:
        logger.error(f"NameError in /start handler for user {user_id}: {ne}", exc_info=True)
        error_msg = "произошла внутренняя ошибка конфигурации."
        await update.message.reply_text(escape_markdown_v2(error_msg))
    except BadRequest as e:
        logger.error(f"BadRequest sending /start message for user {user_id}: {e}", exc_info=True)
        logger.error(f"Failed text (escaped): '{escaped_reply_text[:200]}...'")
        try:
            fallback_text = escape_markdown_v2("Привет! Произошла ошибка отображения стартового сообщения. Используй /help для списка команд.")
            await update.message.reply_text(fallback_text, reply_markup=ReplyKeyboardRemove())
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback start message: {fallback_e}")
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg = "произошла ошибка при обработке команды /start."
        await update.message.reply_text(escape_markdown_v2(error_msg))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
     # Определяем, откуда пришел запрос (команда или callback)
     is_callback = update.callback_query is not None
     message_or_query = update.callback_query if is_callback else update.message
     if not message_or_query: return

     user_id = update.effective_user.id
     # <<< ИЗМЕНЕНО: Получаем chat_id корректно для callback >>>
     chat_id = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
     logger.info(f"CMD /help or Callback < User {user_id} in Chat {chat_id}")

     # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
     if not is_callback:
         if not await check_channel_subscription(update, context):
             await send_subscription_required_message(update, context)
             return
     # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

     await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
     # <<< ИЗМЕНЕНО: Экранируем < и > вручную >>>
     help_text = r"""
**🤖 основные команды:**
/start \- приветствие и твой статус
/help \- эта справка
/profile \- твой статус подписки и лимиты
/subscribe \- инфо о подписке и оплата

**👤 управление личностями:**
/createpersona \<имя\> \[описание] \- создать новую
/mypersonas \- список твоих личностей и кнопки управления \(редакт\., удалить, добавить в чат\)
/editpersona \<id\> \- редактировать личность по ID \(или через /mypersonas\)
/deletepersona \<id\> \- удалить личность по ID \(или через /mypersonas\)

**💬 управление в чате \(где есть личность\):**
/addbot \<id\> \- добавить личность в текущий чат \(или через /mypersonas\)
/mood \[настроение] \- сменить настроение активной личности
/reset \- очистить память \(контекст\) личности в этом чате
/mutebot \- заставить личность молчать в чате
/unmutebot \- разрешить личности отвечать в чате
     """
     try:
         # <<< ИЗМЕНЕНО: Отправляем или редактируем сообщение >>>
         if is_callback:
             # Пытаемся отредактировать сообщение, из которого пришел callback
             await update.callback_query.edit_message_text(help_text, reply_markup=None)
         else:
             # Отправляем новое сообщение в ответ на команду /help
             await update.message.reply_text(help_text, reply_markup=ReplyKeyboardRemove())
     except BadRequest as e:
         # Если редактирование не удалось (например, текст тот же), просто игнорируем
         if is_callback and "Message is not modified" in str(e):
             logger.debug("Help message not modified, skipping edit.")
         else:
             logger.error(f"Failed sending/editing help message (BadRequest): {e}", exc_info=True)
             logger.error(f"Failed help text: '{help_text}'")
             try:
                 # Генерируем plain text версию
                 plain_help_text = re.sub(r'\\(.)', r'\1', help_text) # Убираем экранирование TG
                 plain_help_text = re.sub(r'\*\*(.*?)\*\*', r'\1', plain_help_text) # Убираем **
                 plain_help_text = re.sub(r'`(.*?)`', r'\1', plain_help_text) # Убираем ``
                 # Отправляем как новое сообщение в любом случае при ошибке
                 await context.bot.send_message(chat_id=chat_id, text=plain_help_text, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
             except Exception as fallback_e:
                 logger.error(f"Failed sending plain help message: {fallback_e}")


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # --- Остальной код функции mood остается без изменений ---
    close_db_later = False
    db_session = db
    chat_bot_instance = None
    local_persona = persona # Use passed persona if available

    # Default error messages (escaped)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности.")
    error_persona_info = escape_markdown_v2("Ошибка: не найдена информация о личности.")
    error_no_moods_fmt = escape_markdown_v2("у личности '{persona_name}' не настроены настроения.") # Placeholder
    error_bot_muted_fmt = escape_markdown_v2("личность '{persona_name}' сейчас заглушена (/unmutebot).") # Placeholder
    error_db = escape_markdown_v2("ошибка базы данных при смене настроения.")
    error_general = escape_markdown_v2("ошибка при обработке команды /mood.")

    try:
        # Get DB session if not provided
        if db_session is None:
            db_context = get_db()
            db_session = next(db_context)
            close_db_later = True

        # Get Persona if not provided
        if local_persona is None:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db_session)
            if not persona_info_tuple:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                try:
                    if is_callback: await update.callback_query.answer("Нет активной личности", show_alert=True)
                    await reply_target.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove())
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                logger.debug(f"No active persona for chat {chat_id}. Cannot set mood.")
                # db commit/rollback/close handled by context manager or finally block
                if close_db_later: db_session.close() # Закрываем сессию, если открыли ее здесь
                return # Exit early
            local_persona, _, _ = persona_info_tuple

        if not local_persona or not local_persona.chat_instance:
             logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id}.")
             if is_callback: await update.callback_query.answer("Ошибка: не найдена информация о личности.", show_alert=True) # Plain text
             else: await message_or_callback_msg.reply_text(error_persona_info)
             if close_db_later: db_session.close()
             return

        chat_bot_instance = local_persona.chat_instance
        persona_name_escaped = escape_markdown_v2(local_persona.name)

        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{local_persona.name}' is muted in chat {chat_id}. Ignoring mood command.")
            reply_text = error_bot_muted_fmt.format(persona_name=persona_name_escaped)
            try:
                 if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             reply_text = error_no_moods_fmt.format(persona_name=persona_name_escaped)
             try:
                 if is_callback: await update.callback_query.answer("Нет настроений", show_alert=True)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
             logger.warning(f"Persona {local_persona.name} has no moods defined.")
             if close_db_later: db_session.close()
             return

        available_moods_lower = {m.lower(): m for m in available_moods}
        mood_arg_lower = None
        target_mood_original_case = None

        # Determine the target mood
        if is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             if len(parts) >= 3:
                  mood_arg_lower = "_".join(parts[2:-1]).lower()
                  if mood_arg_lower in available_moods_lower:
                      target_mood_original_case = available_moods_lower[mood_arg_lower]
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


        # Process the mood change or show keyboard
        if target_mood_original_case:
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case) # This commits inside
             mood_name_escaped = escape_markdown_v2(target_mood_original_case)
             reply_text = f"настроение для '{persona_name_escaped}' теперь: **{mood_name_escaped}**"
             try:
                 if is_callback:
                     # Try editing, fallback to answering if message is identical
                     if update.callback_query.message.text != reply_text or update.callback_query.message.reply_markup:
                         await update.callback_query.edit_message_text(reply_text, reply_markup=None)
                     else:
                         await update.callback_query.answer(f"Настроение: {target_mood_original_case}") # Plain text
                 else:
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {local_persona.name} in chat {chat_id} set to {target_mood_original_case}.")
        else:
             # Show keyboard
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m.lower()}_{local_persona.id}")] for m in available_moods]
             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             current_mood_escaped = escape_markdown_v2(current_mood_text)

             if mood_arg_lower:
                 mood_arg_escaped = escape_markdown_v2(mood_arg_lower)
                 # <<< ИЗМЕНЕНО: Убран лишний слеш перед ' >>>
                 reply_text = escape_markdown_v2(f"не знаю настроения '{mood_arg_escaped}' для '{persona_name_escaped}'. выбери из списка:")
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id}. Sent mood selection.")
             else:
                 reply_text = f"текущее настроение: **{current_mood_escaped}**\\. выбери новое для '{persona_name_escaped}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

             try:
                 if is_callback:
                      query = update.callback_query
                      # Edit only if content or markup differs
                      if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup)
                      else:
                           await query.answer() # Avoid redundant edit
                 else:
                      await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup)
             except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("Ошибка БД", show_alert=True)
             await reply_target.reply_text(error_db, reply_markup=ReplyKeyboardRemove())
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id}: {e}", exc_info=True)
         reply_target = update.callback_query.message if is_callback else message_or_callback_msg
         try:
             if is_callback: await update.callback_query.answer("Ошибка", show_alert=True)
             await reply_target.reply_text(error_general, reply_markup=ReplyKeyboardRemove())
         except Exception as send_err: logger.error(f"Error sending general error msg: {send_err}")
    finally:
        # Close session only if it was opened in this function
        if close_db_later and db_session:
            try: db_session.close()
            except Exception: pass


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset < User {user_id} ({username}) in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    # Default error/info messages (escaped)
    error_no_persona = escape_markdown_v2("в этом чате нет активной личности для сброса.")
    error_not_owner = escape_markdown_v2("только владелец личности может сбросить её память.")
    error_no_instance = escape_markdown_v2("ошибка: не найден экземпляр бота для сброса.")
    error_db = escape_markdown_v2("ошибка базы данных при сбросе контекста.")
    error_general = escape_markdown_v2("ошибка при сбросе контекста.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка и используется .format >>>
    success_reset_fmt = "память личности '{persona_name}' в этом чате очищена\\." # Placeholder

    with next(get_db()) as db:
        try:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_info_tuple:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove())
                return
            persona, _, owner_user = persona_info_tuple
            persona_name_escaped = escape_markdown_v2(persona.name) # Экранируем имя здесь

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove())
                return

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id}")
                 await update.message.reply_text(error_no_instance)
                 return

            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id} by user {user_id}.")
            # <<< ИЗМЕНЕНО: Форматируем и экранируем результат >>>
            final_success_msg = escape_markdown_v2(success_reset_fmt.format(persona_name=persona.name))
            await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_db)
            # Rollback handled by context manager
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general)
            # Rollback handled by context manager


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # <<< ИЗМЕНЕНО: Убрана r"" строка для usage_text >>>
    usage_text = "формат: `/createpersona <имя> \\[описание]`\n_имя обязательно, описание нет\\._"
    error_name_len = escape_markdown_v2("имя личности: 2-50 символов.")
    error_desc_len = escape_markdown_v2("описание: до 1500 символов.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    error_limit_reached_fmt = escape_markdown_v2("упс! достигнут лимит личностей ({current_count}/{limit}) для статуса ") + "**{status_text}**" + escape_markdown_v2("\\. 😟\nчтобы создавать больше, используй /subscribe") # Placeholder
    error_name_exists_fmt = escape_markdown_v2("личность с именем '{persona_name}' уже есть. выбери другое.") # Placeholder
    error_db = escape_markdown_v2("ошибка базы данных при создании личности.")
    error_general = escape_markdown_v2("ошибка при создании личности.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_create_fmt = escape_markdown_v2("✅ личность '{name}' создана!\nid: ") + "`{id}`" + escape_markdown_v2("\nописание: {description}\n\nдобавь в чат или управляй через /mypersonas") # Placeholder

    args = context.args
    if not args:
        await update.message.reply_text(usage_text)
        return
    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None
    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text(error_name_len, reply_markup=ReplyKeyboardRemove())
         return
    if persona_description and len(persona_description) > 1500:
         await update.message.reply_text(error_desc_len, reply_markup=ReplyKeyboardRemove())
         return

    with next(get_db()) as db:
        try:
            # Получаем пользователя и сразу проверяем лимит
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user: # Если пользователя нет, создаем
                 user = get_or_create_user(db, user_id, username)
                 db.commit() # Коммитим нового пользователя
                 db.refresh(user) # Обновляем объект
                 # Перезагружаем с selectinload для корректной проверки лимита
                 user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()

            if not user.can_create_persona:
                 current_count = len(user.persona_configs) # Используем загруженные данные
                 limit = user.persona_limit
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")
                 status_text_escaped = escape_markdown_v2("⭐ Premium" if user.is_active_subscriber else "🆓 Free")
                 # Format string carefully preserving **
                 final_limit_msg = error_limit_reached_fmt.format(current_count=current_count, limit=limit, status_text=status_text_escaped)
                 await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove())
                 return

            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                 persona_name_escaped = escape_markdown_v2(persona_name)
                 final_exists_msg = error_name_exists_fmt.format(persona_name=persona_name_escaped)
                 await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove())
                 return

            # Создаем персону (функция сама коммитит)
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            name_escaped = escape_markdown_v2(new_persona.name)
            desc_display = escape_markdown_v2(new_persona.description) if new_persona.description else escape_markdown_v2("(пусто)")
            # Format string carefully preserving `id`
            final_success_msg = success_create_fmt.format(name=name_escaped, id=new_persona.id, description=desc_display)
            await update.message.reply_text(final_success_msg)
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError:
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             persona_name_escaped = escape_markdown_v2(persona_name)
             error_msg_ie = escape_markdown_v2(f"ошибка: личность '{persona_name_escaped}' уже существует (возможно, гонка запросов). попробуй еще раз.")
             await update.message.reply_text(error_msg_ie, reply_markup=ReplyKeyboardRemove())
             # Rollback handled by context manager
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db)
             # Rollback handled by context manager
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_general)
             # Rollback handled by context manager


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("ошибка при загрузке списка личностей.")
    error_general = escape_markdown_v2("произошла ошибка при обработке команды /mypersonas.")
    error_user_not_found = escape_markdown_v2("Ошибка: не удалось найти пользователя.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    info_no_personas_fmt = "у тебя пока нет личностей \\(лимит: {count}/{limit}\\)\\.\nсоздай: `/createpersona <имя>`" # Placeholder
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    info_list_header_fmt = "твои личности ({count}/{limit}):\n" # Placeholder

    try:
        with next(get_db()) as db:
            # <<< ИЗМЕНЕНО: Используем selectinload для загрузки персон сразу >>>
            user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()

            if not user_with_personas:
                 # Пытаемся создать, если не найден
                 user_with_personas = get_or_create_user(db, user_id, username)
                 db.commit()
                 db.refresh(user_with_personas)
                 # Перезагружаем с selectinload
                 user_with_personas = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()
                 if not user_with_personas: # Если и после создания не найден
                     logger.error(f"User {user_id} not found even after get_or_create in my_personas.")
                     await update.message.reply_text(error_user_not_found)
                     return

            # Теперь используем загруженные данные
            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            if not personas:
                # <<< ИЗМЕНЕНО: Экранируем результат форматирования >>>
                final_no_personas_msg = escape_markdown_v2(info_no_personas_fmt.format(count=persona_count, limit=persona_limit))
                await update.message.reply_text(final_no_personas_msg)
                return

            # <<< ИЗМЕНЕНО: Форматируем и экранируем заголовок >>>
            text = escape_markdown_v2(info_list_header_fmt.format(count=persona_count, limit=persona_limit))

            keyboard = []
            for p in personas:
                 # Имя персоны НЕ экранируем для кнопки, но экранируем для текста, если понадобится
                 # ID не экранируем, он число
                 button_text = f"👤 {p.name} (ID: {p.id})"
                 # Добавляем строчку с именем и ID (кнопка-заглушка)
                 keyboard.append([InlineKeyboardButton(button_text, callback_data=f"dummy_{p.id}")])
                 # Добавляем кнопки действий
                 keyboard.append([
                     InlineKeyboardButton("⚙️ Редакт.", callback_data=f"edit_persona_{p.id}"),
                     InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_persona_{p.id}"),
                     InlineKeyboardButton("➕ В чат", callback_data=f"add_bot_{p.id}")
                 ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            # Отправляем текст (заголовок) и клавиатуру
            await update.message.reply_text(text, reply_markup=reply_markup)
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db)
        # Rollback handled by context manager
    except KeyError as ke: # <<< ИЗМЕНЕНО: Ловим KeyError
        logger.error(f"KeyError during /mypersonas formatting for user {user_id}: {ke}", exc_info=True)
        await update.message.reply_text(escape_markdown_v2("Ошибка форматирования текста."))
        # Rollback handled by context manager
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general)
        # Rollback handled by context manager


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id}"
    local_persona_id = persona_id # Use passed ID if available

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # <<< ИЗМЕНЕНО: Убраны r"" строки >>>
    usage_text = "формат: `/addbot <id персоны>`\nили используй кнопку '➕ В чат' из /mypersonas"
    error_invalid_id_callback = escape_markdown_v2("Ошибка: неверный ID личности.")
    error_invalid_id_cmd = escape_markdown_v2("id личности должен быть числом.")
    error_no_id = escape_markdown_v2("Ошибка: ID личности не определен.")
    error_persona_not_found_fmt = escape_markdown_v2("личность с id `{id}` не найдена или не твоя.") # Placeholder
    error_already_active_fmt = escape_markdown_v2("личность '{name}' уже активна в этом чате.") # Placeholder
    error_link_failed = escape_markdown_v2("не удалось активировать личность (ошибка связывания).")
    error_integrity = escape_markdown_v2("произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
    error_db = escape_markdown_v2("ошибка базы данных при добавлении бота.")
    error_general = escape_markdown_v2("ошибка при активации личности.")
    # <<< ИЗМЕНЕНО: Определяем структуру строки отдельно >>>
    success_added_structure = "✅ личность '{name}' (id: `{id}`) активирована в этом чате! Память очищена." # Placeholder

    if is_callback and local_persona_id is None:
         try:
             local_persona_id = int(update.callback_query.data.split('_')[-1])
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
             await update.callback_query.answer("Ошибка: неверный ID личности.", show_alert=True) # Plain text
             return
    elif not is_callback:
         logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id}) with args: {context.args}")
         args = context.args
         if not args or len(args) != 1 or not args[0].isdigit():
             await message_or_callback_msg.reply_text(usage_text)
             return
         try:
             local_persona_id = int(args[0])
         except ValueError:
             await message_or_callback_msg.reply_text(error_invalid_id_cmd, reply_markup=ReplyKeyboardRemove())
             return

    if local_persona_id is None:
         logger.error("add_bot_to_chat: persona_id is None after processing input.")
         if is_callback: await update.callback_query.answer("Ошибка: ID личности не определен.", show_alert=True) # Plain text
         else: await message_or_callback_msg.reply_text(error_no_id)
         return

    if is_callback:
        await update.callback_query.answer("Добавляем личность...") # Plain text

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 final_not_found_msg = error_persona_not_found_fmt.format(id=local_persona_id)
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_not_found_msg, reply_markup=ReplyKeyboardRemove())
                 return

            # Deactivate any existing active bot in this chat first
            existing_active_link = db.query(ChatBotInstance).options(
                 selectinload(ChatBotInstance.bot_instance_ref) # Загружаем связь для проверки ID
            ).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).first()

            if existing_active_link:
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    persona_name_escaped = escape_markdown_v2(persona.name) # Экранируем имя здесь
                    final_already_active_msg = error_already_active_fmt.format(name=persona_name_escaped)
                    if is_callback: await update.callback_query.answer(f"личность '{persona.name}' уже активна.", show_alert=True) # Plain text answer
                    else: await message_or_callback_msg.reply_text(final_already_active_msg, reply_markup=ReplyKeyboardRemove())
                    # Очищаем контекст при повторном добавлении
                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id} on re-add command.")
                    deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == existing_active_link.id).delete(synchronize_session='fetch')
                    db.commit()
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return
                else:
                    logger.info(f"Deactivating previous bot instance {existing_active_link.bot_instance_id} in chat {chat_id} before activating {local_persona_id}.")
                    existing_active_link.active = False
                    # Не коммитим здесь, коммит будет после успешного добавления новой
                    db.flush() # Применяем деактивацию в сессии

            # Find or create BotInstance
            user = persona.owner # Используем загруженного владельца
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                 logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                 bot_instance = BotInstance(
                     owner_id=user.id,
                     persona_config_id=local_persona_id,
                     name=f"Inst:{persona.name}" # Optional name for the instance
                 )
                 db.add(bot_instance)
                 db.flush() # Получаем ID
                 logger.info(f"Created BotInstance {bot_instance.id} for persona {local_persona_id}")


            # Link the BotInstance to the chat (функция сама коммитит или откатывает)
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            if chat_link:
                 # <<< ИЗМЕНЕНО: Форматируем и экранируем результат >>>
                 final_success_msg = escape_markdown_v2(
                     success_added_structure.format(name=persona.name, id=local_persona_id)
                 )
                 await context.bot.send_message(chat_id=chat_id, text=final_success_msg, reply_markup=ReplyKeyboardRemove())
                 if is_callback:
                      try:
                           # Удаляем сообщение с кнопками "Мои личности"
                           await update.callback_query.delete_message()
                      except Exception as del_err:
                           logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 # Если link_bot_instance_to_chat вернул None, значит произошел rollback внутри
                 reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                 await reply_target.reply_text(error_link_failed, reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id} - link_bot_instance_to_chat returned None.")

        except IntegrityError as e:
             logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text=error_integrity)
             # Rollback handled by context manager
        except SQLAlchemyError as e:
             logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text=error_db)
             # Rollback handled by context manager
        except KeyError as ke: # <<< ИЗМЕНЕНО: Ловим KeyError при форматировании
             logger.error(f"KeyError during add_bot_to_chat formatting: {ke}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text=escape_markdown_v2("Ошибка форматирования ответа."))
        except Exception as e:
             logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text=error_general)
             # Rollback handled by context manager


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data: return

    chat_id = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для некоторых callback) +++
    needs_subscription_check = True
    no_check_callbacks = (
        "cancel_edit", "edit_persona_back", "edit_moods_back_cancel",
        "delete_persona_cancel", "view_tos", "subscribe_info",
        "show_help", "dummy_", "confirm_pay", "subscribe_pay"
    )
    if data.startswith(no_check_callbacks):
        needs_subscription_check = False
    # Для conversation хендлеров проверка подписки будет внутри самих шагов, если нужно

    if needs_subscription_check:
        # Извлекаем update объект для передачи в check_channel_subscription
        effective_update_for_check = update if update.effective_message else query # Используем query как fallback для получения user_id
        if not await check_channel_subscription(effective_update_for_check, context):
            await send_subscription_required_message(effective_update_for_check, context)
            try: await query.answer(text=escape_markdown_v2("Подпишитесь на канал!"), show_alert=True)
            except: pass
            return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    # --- Route callbacks ---
    if data.startswith("set_mood_"):
        await mood(update, context) # mood() handles answer
    elif data == "subscribe_info":
        await query.answer()
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await query.answer("Создаю ссылку на оплату...") # Plain text
        await generate_payment_link(update, context)
    elif data == "view_tos":
        await query.answer()
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer()
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
        await add_bot_to_chat(update, context) # add_bot_to_chat() handles answer
    elif data == "show_help":
        await query.answer()
        await help_command(update, context) # help_command() handles sending/editing
    elif data.startswith("dummy_"):
        await query.answer() # Просто отвечаем на callback-заглушку
    else:
        # Проверяем, не является ли это callback'ом для ConversationHandler
        known_conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "edit_mood", "deletemood", "cancel_edit", "edit_persona_back", "deletemood_confirm_", "deletemood_delete_")
        if any(data.startswith(p) for p in known_conv_prefixes):
             logger.debug(f"Callback '{data}' seems to be for a ConversationHandler, skipping direct handling.")
             # НЕ отвечаем на callback здесь, ConversationHandler должен это сделать
        else:
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            try:
                 await query.answer("Неизвестное действие") # Plain text
            except Exception as e:
                 logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    error_db = escape_markdown_v2("ошибка базы данных при загрузке профиля.")
    error_general = escape_markdown_v2("ошибка при обработке команды /profile.")
    error_user_not_found = escape_markdown_v2("ошибка: пользователь не найден.") # Добавлено

    with next(get_db()) as db:
        try:
            # <<< ИЗМЕНЕНО: Используем selectinload для persona_configs >>>
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user:
                # Пытаемся создать, если не найден
                user = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user)
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()
                if not user:
                    logger.error(f"User {user_id} not found after get_or_create in profile.")
                    await update.message.reply_text(error_user_not_found)
                    return

            # Ensure limits are up-to-date
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not user.last_message_reset or user.last_message_reset < today_start:
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit()
                db.refresh(user) # Обновляем данные пользователя после коммита

            is_active_subscriber = user.is_active_subscriber
            status_text = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            status = escape_markdown_v2(status_text) # Экранируем статус

            # Экранируем дату и время
            expires_text = escape_markdown_v2("нет активной подписки")
            if is_active_subscriber and user.subscription_expires_at:
                 try:
                     expires_text_raw = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC"
                     expires_text = escape_markdown_v2(expires_text_raw)
                 except AttributeError: # На случай, если дата некорректна
                      expires_text = escape_markdown_v2("активна (дата истечения некорректна)")

            # Используем загруженные данные о персонах
            persona_count = len(user.persona_configs) if user.persona_configs is not None else 0
            persona_limit = user.persona_limit
            msg_count = user.daily_message_count
            msg_limit = user.message_limit

            # Собираем текст с ** и экранированными частями
            text = (
                f"👤 **твой профиль**\n\n"
                f"статус: **{status}**\n" # Жирный для статуса
                f"{expires_text}\n\n"
                f"**лимиты:**\n" # Жирный для заголовка
                f"{escape_markdown_v2(f'сообщения сегодня: {msg_count}/{msg_limit}')}\n" # Экранируем числа и текст
                f"{escape_markdown_v2(f'создано личностей: {persona_count}/{persona_limit}')}\n\n" # Экранируем числа и текст
            )
            if not is_active_subscriber:
                text += escape_markdown_v2("🚀 хочешь больше? жми /subscribe !")

            await update.message.reply_text(text)
        except SQLAlchemyError as e:
             logger.error(f"Database error during /profile for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text(error_db)
             # Rollback handled by context manager
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general)
            # Rollback handled by context manager


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
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

    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)")
    text_raw = "Текст для /subscribe" # Placeholder
    text = "" # Initialize text
    reply_markup = None # Initialize markup

    if not yookassa_ready:
        text = error_payment_unavailable
        reply_markup = None
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
    else:
        # <<< ИЗМЕНЕНО: Скорректировано экранирование и удалена фраза >>>
        price_str = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        # Экранируем только части, которые НЕ содержат Markdown **
        header = f"✨ **премиум подписка ({escape_markdown_v2(price_str)} {escape_markdown_v2(SUBSCRIPTION_CURRENCY)}/мес)** ✨\n\n"
        body = (
            escape_markdown_v2("получи максимум возможностей:\n✅ ") +
            f"**{PAID_DAILY_MESSAGE_LIMIT}**" + escape_markdown_v2(f" сообщений в день \\(вместо {FREE_DAILY_MESSAGE_LIMIT}\\)\n✅ ") +
            f"**{PAID_PERSONA_LIMIT}**" + escape_markdown_v2(f" личностей \\(вместо {FREE_PERSONA_LIMIT}\\)\n✅ полная настройка всех промптов\n✅ создание и редакт\\. своих настроений\n✅ приоритетная поддержка\n\nподписка действует {SUBSCRIPTION_DURATION_DAYS} дней\\.") # Убрали лишние \ и фразу "(если будет)"
        )
        text = header + body
        text_raw = "Premium subscription info text" # Placeholder для лога

        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            [InlineKeyboardButton("✅ Принять и оплатить", callback_data="confirm_pay")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if from_callback:
            query = update.callback_query
            if query.message.text != text or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(text, reply_markup=reply_markup)
            else:
                 await query.answer() # Avoid redundant edit
        else:
            await message_to_update_or_reply.reply_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        logger.error(f"Failed sending subscribe message (BadRequest): {e} - Text Raw: '{text_raw}' Escaped: '{text[:100]}...'")
        try:
            if message_to_update_or_reply:
                 plain_text = re.sub(r'\\(.)', r'\1', text) # Remove escapes
                 plain_text = plain_text.replace("**", "") # Remove bold
                 await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=plain_text, reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
             logger.error(f"Failed sending fallback subscribe message: {fallback_e}")
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        # Try sending as a new message if editing failed
        if from_callback and isinstance(e, (BadRequest, TelegramError)):
            try:
                await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=text, reply_markup=reply_markup)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")


async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    tos_url = context.bot_data.get('tos_url')
    error_tos_link = "Не удалось отобразить ссылку на соглашение." # Plain text for answer
    error_tos_load = escape_markdown_v2("❌ Не удалось загрузить ссылку на Пользовательское Соглашение. Попробуйте позже.")
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
                await query.edit_message_text(text, reply_markup=reply_markup)
            else:
                 await query.answer() # Avoid redundant edit
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
                await query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await query.answer() # Avoid redundant edit
        except Exception as e:
             logger.error(f"Failed to show ToS error message to user {user_id}: {e}")
             await query.answer("Ошибка загрузки соглашения.", show_alert=True) # Plain text


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    error_payment_unavailable = escape_markdown_v2("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)")
    info_confirm = escape_markdown_v2(
         "✅ Отлично!\n\n"
         "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
         "Пользовательским Соглашением."
         "\n\n👇"
    )
    text = "" # Initialize text
    reply_markup = None # Initialize markup

    if not yookassa_ready:
        text = error_payment_unavailable
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]])
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        text = info_confirm
        price_str = f"{SUBSCRIPTION_PRICE_RUB:.0f}"
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {price_str} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]
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
                disable_web_page_preview=True
            )
        else:
            await query.answer() # Avoid redundant edit
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    error_yk_not_ready = escape_markdown_v2("❌ ошибка: сервис оплаты не настроен правильно.")
    error_yk_config = escape_markdown_v2("❌ ошибка конфигурации платежной системы.")
    error_receipt = escape_markdown_v2("❌ ошибка при формировании данных чека.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    error_link_get_fmt = escape_markdown_v2("❌ не удалось получить ссылку от платежной системы") # Часть сообщения
    error_link_create = escape_markdown_v2("❌ не удалось создать ссылку для оплаты. ") # Часть сообщения
    success_link = escape_markdown_v2(
        "✅ ссылка для оплаты создана!\n\n"
        "нажми кнопку ниже для перехода к оплате. после успеха подписка активируется (может занять пару минут)."
        )
    text = "" # Initialize text
    reply_markup = None # Initialize markup

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly for payment generation.")
        text = error_yk_not_ready
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup)
        return

    try:
        # Используем безопасный доступ к атрибутам Configuration
        current_shop_id = int(YOOKASSA_SHOP_ID)
        # Переконфигурируем перед каждым запросом на всякий случай
        Configuration.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
        logger.info(f"Yookassa configured within generate_payment_link (Shop ID: {current_shop_id}).")
    except ValueError:
         logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) invalid integer.")
         text = error_yk_config
         reply_markup = None
         await query.edit_message_text(text, reply_markup=reply_markup)
         return
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK in generate_payment_link: {conf_e}", exc_info=True)
        text = error_yk_config
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup)
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
                "vat_code": "1", # 1 = НДС не облагается (если применимо)
                "payment_mode": "full_prepayment", # Полная предоплата
                "payment_subject": "service" # Тип товара - услуга
            })
        ]
        user_email = f"user_{user_id}@telegram.bot" # Placeholder email
        receipt_data = Receipt({
            "customer": {"email": user_email},
            "items": receipt_items,
        })
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        text = error_receipt
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup)
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

        # Запускаем синхронный вызов в отдельном потоке
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = error_link_get_fmt # Используем _fmt версию
             if payment_response and payment_response.status: error_message += escape_markdown_v2(f" \\(статус: {payment_response.status}\\)")
             error_message += escape_markdown_v2("\\.\nПопробуй позже\\.")
             text = error_message
             reply_markup = None
             await query.edit_message_text(text, reply_markup=reply_markup)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = success_link
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        user_message = error_link_create
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                # Попытка извлечь детали ошибки из ответа YK
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                # Простые проверки на известные ошибки
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
            # Отправляем сообщение об ошибке пользователю
            await query.edit_message_text(user_message, reply_markup=None)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # This handler should not be called if Flask is handling the webhook
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass


# --- Edit Persona Conversation ---
async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    is_callback = update.callback_query is not None
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Очищаем user_data в начале новой сессии редактирования

    error_not_found_fmt = escape_markdown_v2("личность с id `{id}` не найдена или не твоя.") # Placeholder
    error_db = escape_markdown_v2("ошибка базы данных при начале редактирования.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    prompt_edit_fmt = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:" # Placeholder

    try:
        with next(get_db()) as db:
            # Используем selectinload для owner, чтобы проверить подписку ниже
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                # Проверяем владельца по telegram_id для надежности
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 final_error_msg = error_not_found_fmt.format(id=persona_id)
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove())
                 return ConversationHandler.END

            # Сохраняем ID в user_data
            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config) # Передаем config для проверки подписки
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = prompt_edit_fmt.format(name=escape_markdown_v2(persona_config.name), id=persona_id)

            reply_target = update.callback_query.message if is_callback else update.effective_message
            if is_callback:
                 query = update.callback_query
                 try:
                      # Редактируем сообщение ТОЛЬКО если текст или кнопки изменились
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup)
                      else:
                           await query.answer() # Отвечаем, чтобы убрать часики
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup)
            else:
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup)

        logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
        return EDIT_PERSONA_CHOICE
    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general)
         return ConversationHandler.END


async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    usage_text = "укажи id личности: `/editpersona <id>`\nили используй кнопку из /mypersonas"
    error_invalid_id = escape_markdown_v2("ID должен быть числом.")
    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id)
        return ConversationHandler.END
    return await _start_edit_convo(update, context, persona_id)


async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...") # Plain text
    error_invalid_id = escape_markdown_v2("Ошибка: неверный ID личности в кнопке.")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        await query.edit_message_text(error_invalid_id)
        return ConversationHandler.END


async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, Persona ID from context: {persona_id}, Callback data: {data} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна (нет id). начни снова.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("ошибка базы данных при проверке данных.")
    error_general = escape_markdown_v2("Непредвиденная ошибка.")
    info_premium_mood = "⭐ Редактирование настроений доступно по подписке" # Plain text for answer
    info_premium_field_fmt = "⭐ Поле '{field_name}' доступно по подписке" # Placeholder for plain text answer
    # <<< ИЗМЕНЕНО: Убраны r"" строки >>>
    prompt_edit_value_fmt = "отправь новое значение для **{field_name}**\\.\n_текущее:_\n`{current_value}`" # Placeholder
    prompt_edit_max_msg_fmt = "отправь новое значение для **{field_name}** \\(число от 1 до 10\\):\n_текущее: {current_value}_" # Placeholder

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text(error_no_session, reply_markup=None)
         return ConversationHandler.END

    # Fetch user and persona
    persona_config = None
    is_premium_user = False
    try:
        with next(get_db()) as db:
            # Загружаем персону и ее владельца
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id) # Проверка владельца
            ).first()

            if not persona_config:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} not found or not owned.")
                await query.answer("Личность не найдена", show_alert=True)
                await query.edit_message_text(error_not_found, reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END
            # Используем загруженного владельца
            is_premium_user = persona_config.owner.is_active_subscriber

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text(error_db, reply_markup=None)
         return EDIT_PERSONA_CHOICE # Возвращаемся в то же состояние
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.answer("Непредвиденная ошибка", show_alert=True)
         await query.edit_message_text(error_general, reply_markup=None)
         return ConversationHandler.END # Завершаем при серьезной ошибке

    # Handle callback data
    # await query.answer() # Не отвечаем здесь, ответим ниже или в след. шаге

    if data == "cancel_edit":
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        if not is_premium_user and not is_admin(user_id):
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer(info_premium_mood, show_alert=True)
             return EDIT_PERSONA_CHOICE
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             await query.answer() # Отвечаем здесь перед переходом
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        # Получаем экранированное имя из FIELD_MAP
        field_display_name = FIELD_MAP.get(field, escape_markdown_v2(field))
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        advanced_fields = ["system_prompt_template", "should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user and not is_admin(user_id):
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             # Форматируем plain text ответ (убираем Markdown для ответа)
             field_plain_name = re.sub(r'\\(.)', r'\1', field_display_name)
             await query.answer(info_premium_field_fmt.format(field_name=field_plain_name), show_alert=True)
             return EDIT_PERSONA_CHOICE

        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        await query.answer() # Отвечаем перед редактированием сообщения

        if field == "max_response_messages":
            current_value = getattr(persona_config, field, 3)
            final_prompt = prompt_edit_max_msg_fmt.format(field_name=field_display_name, current_value=current_value)
            await query.edit_message_text(final_prompt, reply_markup=reply_markup)
            return EDIT_MAX_MESSAGES
        else:
            current_value = getattr(persona_config, field, "")
            # Экранируем текущее значение для показа в `code`
            current_value_display = escape_markdown_v2(str(current_value) if len(str(current_value)) < 300 else str(current_value)[:300] + "...")
            final_prompt = prompt_edit_value_fmt.format(field_name=field_display_name, current_value=current_value_display)
            await query.edit_message_text(final_prompt, reply_markup=reply_markup)
            return EDIT_FIELD

    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         await query.answer() # Отвечаем перед редактированием
         keyboard = await _get_edit_persona_keyboard(persona_config)
         # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
         prompt_edit_back = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"
         final_back_msg = prompt_edit_back.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
         await query.edit_message_text(final_back_msg, reply_markup=InlineKeyboardMarkup(keyboard))
         context.user_data.pop('edit_field', None) # Убираем поле из контекста при возврате
         return EDIT_PERSONA_CHOICE

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.answer("Неизвестный выбор", show_alert=True) # Отвечаем на callback
    # Не меняем сообщение, просто остаемся в том же состоянии
    return EDIT_PERSONA_CHOICE


async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_FIELD
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна. начни снова.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_validation_fmt = escape_markdown_v2("{field_name}: макс. {max_len} символов.") # Placeholder
    error_validation_min_fmt = escape_markdown_v2("{field_name}: мин. {min_len} символа.") # Placeholder
    error_name_taken_fmt = escape_markdown_v2("имя '{name}' уже занято другой твоей личностью. попробуй другое:") # Placeholder
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении. попробуй еще раз.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_update_fmt = "✅ поле **{field_name}** для личности **{persona_name}** обновлено!" # Placeholder
    prompt_next_edit_fmt = "что еще изменить для **{name}** \\(id: `{id}`\\)?" # Placeholder

    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    field_display_name = FIELD_MAP.get(field, escape_markdown_v2(field))

    # Validation logic
    validation_error_msg = None
    max_len_map = {
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len_map = {"name": 2}

    if field in max_len_map and len(new_value) > max_len_map[field]:
        validation_error_msg = error_validation_fmt.format(field_name=field_display_name, max_len=max_len_map[field])
    if field in min_len_map and len(new_value) < min_len_map[field]:
        validation_error_msg = error_validation_min_fmt.format(field_name=field_display_name, min_len=min_len_map[field])

    if validation_error_msg:
        logger.debug(f"Validation failed for field '{field}': {validation_error_msg}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error_msg} {escape_markdown_v2('попробуй еще раз:')}", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD

    try:
        with next(get_db()) as db:
            # Загружаем персону и владельца
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # Check name uniqueness
            if field == "name" and new_value.lower() != persona_config.name.lower():
                # Проверяем уникальность имени в пределах ОДНОГО пользователя
                existing = db.query(PersonaConfig.id).filter(
                    PersonaConfig.owner_id == persona_config.owner_id, # Используем ID владельца
                    func.lower(PersonaConfig.name) == new_value.lower()
                ).first()
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by their persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    final_name_taken_msg = error_name_taken_fmt.format(name=escape_markdown_v2(new_value))
                    await update.message.reply_text(final_name_taken_msg, reply_markup=InlineKeyboardMarkup([[back_button]]))
                    return EDIT_FIELD

            # Update field
            setattr(persona_config, field, new_value)
            db.commit()
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            final_success_msg = success_update_fmt.format(field_name=field_display_name, persona_name=escape_markdown_v2(persona_config.name))
            await update.message.reply_text(final_success_msg)

            # Return to main edit menu
            context.user_data.pop('edit_field', None)
            db.refresh(persona_config) # Обновляем объект после коммита
            keyboard = await _get_edit_persona_keyboard(persona_config)
            final_next_prompt = prompt_next_edit_fmt.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
            await update.message.reply_text(final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db)
         # Rollback handled by context manager
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_general)
         context.user_data.clear() # Clear data on unexpected error
         return ConversationHandler.END


async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages"
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна (нет persona_id). начни снова.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_invalid_value = escape_markdown_v2("неверное значение. введи число от 1 до 10:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при обновлении. попробуй еще раз.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при обновлении.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_update_fmt = "✅ макс\\. сообщений в ответе для **{name}** установлено: **{value}**" # Placeholder
    prompt_next_edit_fmt = "что еще изменить для **{name}** \\(id: `{id}`\\)?" # Placeholder

    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10): raise ValueError("Value out of range 1-10")
    except ValueError:
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(error_invalid_value, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MAX_MESSAGES

    try:
        with next(get_db()) as db:
            # Загружаем персону и владельца
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            persona_config.max_response_messages = new_value
            db.commit()
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            final_success_msg = success_update_fmt.format(name=escape_markdown_v2(persona_config.name), value=new_value)
            await update.message.reply_text(final_success_msg)

            # Return to main edit menu
            db.refresh(persona_config) # Обновляем объект
            keyboard = await _get_edit_persona_keyboard(persona_config)
            final_next_prompt = prompt_next_edit_fmt.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
            await update.message.reply_text(final_next_prompt, reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_db)
         # Rollback handled by context manager
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text(error_general)
         context.user_data.clear() # Clear data on unexpected error
         return ConversationHandler.END


async def _get_edit_persona_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]

    # Проверяем подписку владельца для отображения ⭐
    is_premium = False
    if persona_config.owner:
        is_premium = persona_config.owner.is_active_subscriber or is_admin(persona_config.owner.telegram_id)
    else:
        # Пытаемся загрузить владельца, если он не был загружен (хотя selectinload должен был)
        try:
            with next(get_db()) as db:
                owner = db.query(User).filter(User.id == persona_config.owner_id).first()
                if owner:
                    is_premium = owner.is_active_subscriber or is_admin(owner.telegram_id)
        except Exception as e:
            logger.error(f"Failed to fetch owner for premium check in _get_edit_persona_keyboard: {e}")

    star = " ⭐" if is_premium else "" # Добавляем звезду для премиум полей

    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)

    # Обновляем тексты кнопок, добавляя звезду где нужно
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
              # Используем оригинальное имя для отображения
              display_name = mood_name.capitalize()
              # Кодируем имя для callback data
              encoded_mood_name = urllib.parse.quote(mood_name)
              # Ограничиваем длину закодированного имени (на всякий случай)
              if len(f"editmood_select_{encoded_mood_name}") > 60: # Оставляем запас
                  logger.warning(f"Encoded mood name '{encoded_mood_name}' too long for callback data, skipping.")
                  continue
              if len(f"deletemood_confirm_{encoded_mood_name}") > 60:
                  logger.warning(f"Encoded mood name '{encoded_mood_name}' too long for delete callback data, skipping delete button.")
                  keyboard.append([
                      InlineKeyboardButton(f"✏️ {display_name}", callback_data=f"editmood_select_{encoded_mood_name}")
                      # Нет кнопки удаления
                  ])
                  continue

              keyboard.append([
                  InlineKeyboardButton(f"✏️ {display_name}", callback_data=f"editmood_select_{encoded_mood_name}"),
                  InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{encoded_mood_name}")
              ])
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])
     return keyboard


async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message_target = update.effective_message
    error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню редактирования (личность не найдена).")
    error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню редактирования.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    prompt_edit = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"

    if not message_target:
        logger.warning("Cannot return to edit menu: effective_message is None.")
        context.user_data.clear()
        return ConversationHandler.END
    try:
        with next(get_db()) as db:
            # Загружаем персону и владельца
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if persona_config:
                keyboard = await _get_edit_persona_keyboard(persona_config)
                final_prompt = prompt_edit.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
                await message_target.reply_text(final_prompt, reply_markup=InlineKeyboardMarkup(keyboard))
                return EDIT_PERSONA_CHOICE
            else:
                logger.warning(f"Persona {persona_id} not found when trying to return to main edit menu.")
                await message_target.reply_text(error_cannot_return)
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to return to main edit menu after error: {e}", exc_info=True)
        await message_target.reply_text(error_cannot_return_general)
        context.user_data.clear()
        return ConversationHandler.END


async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     message_target = update.effective_message
     error_cannot_return = escape_markdown_v2("Не удалось вернуться к меню настроений (личность не найдена).")
     error_cannot_return_general = escape_markdown_v2("Не удалось вернуться к меню настроений.")
     # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
     prompt_mood_menu = "управление настроениями для **{name}**:"

     if not message_target:
         logger.warning("Cannot return to mood menu: effective_message is None.")
         context.user_data.clear()
         return ConversationHandler.END
     try:
         with next(get_db()) as db:
             # Загружаем персону (владелец не нужен для клавиатуры настроений)
             persona_config = db.query(PersonaConfig).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()

             if persona_config:
                 keyboard = await _get_edit_moods_keyboard_internal(persona_config)
                 final_prompt = prompt_mood_menu.format(name=escape_markdown_v2(persona_config.name))
                 # Отправляем как новое сообщение, т.к. предыдущее могло быть от пользователя
                 await context.bot.send_message(
                     chat_id=message_target.chat.id,
                     text=final_prompt,
                     reply_markup=InlineKeyboardMarkup(keyboard)
                 )
                 # Удаляем сообщение об ошибке, если это было сообщение бота
                 if update.callback_query and update.callback_query.message.from_user.is_bot:
                     try: await update.callback_query.message.delete()
                     except: pass

                 return EDIT_MOOD_CHOICE
             else:
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await message_target.reply_text(error_cannot_return)
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
         await message_target.reply_text(error_cannot_return_general)
         context.user_data.clear()
         return ConversationHandler.END


async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("Ошибка базы данных при загрузке настроений.")
    info_premium = "⭐ Доступно по подписке" # Plain text for answer
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    prompt_mood_menu_fmt = "управление настроениями для **{name}**:"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session, reply_markup=None)
        return ConversationHandler.END

    local_persona_config = persona_config
    is_premium = False # Default

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
                    await query.edit_message_text(error_not_found, reply_markup=None)
                    context.user_data.clear()
                    return ConversationHandler.END
                # Проверяем подписку загруженного владельца
                is_premium = local_persona_config.owner.is_active_subscriber or is_admin(user_id)

        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("Ошибка базы данных", show_alert=True)
             await query.edit_message_text(error_db, reply_markup=None)
             return await _try_return_to_edit_menu(update, context, user_id, persona_id) # Пытаемся вернуться в главное меню
    else:
         # Если persona_config передан, проверяем подписку его владельца
         is_premium = local_persona_config.owner.is_active_subscriber or is_admin(user_id)

    # Check premium status
    if not is_premium:
        logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
        await query.answer(info_premium, show_alert=True)
        # Не меняем сообщение, просто остаемся в главном меню редактирования
        return EDIT_PERSONA_CHOICE

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = prompt_mood_menu_fmt.format(name=escape_markdown_v2(local_persona_config.name))

    try:
        if query.message.text != msg_text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(msg_text, reply_markup=reply_markup)
        else:
            await query.answer() # Avoid redundant edit
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         # If editing fails, try sending a new message
         try:
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=msg_text,
                reply_markup=reply_markup
            )
            # Попытка удалить старое сообщение, если оно от бота
            if query.message.from_user.is_bot:
                 try: await query.message.delete()
                 except: pass
         except Exception as send_e:
            logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE


async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("Ошибка базы данных.")
    error_unhandled_choice = escape_markdown_v2("неизвестный выбор настроения.")
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения.")
    # <<< ИЗМЕНЕНО: Убраны r"" строки >>>
    prompt_new_name = "введи **название** нового настроения \\(одно слово, латиница/кириллица, цифры, дефис, подчеркивание, без пробелов\\):"
    prompt_new_prompt_fmt = "редактирование настроения: **{name}**\n\n_текущий промпт:_\n`{prompt}`\n\nотправь **новый текст промпта**:" # Placeholder
    prompt_confirm_delete_fmt = "точно удалить настроение **'{name}'**\\?" # Placeholder

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text(error_no_session)
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
                 await query.edit_message_text(error_not_found, reply_markup=None)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text(error_db, reply_markup=None)
         return EDIT_MOOD_CHOICE

    await query.answer() # Отвечаем на callback перед обработкой

    # --- Handle Mood Menu Actions ---
    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
        prompt_edit = "редактируем **{name}** \\(id: `{id}`\\)\nвыбери, что изменить:"
        final_prompt = prompt_edit.format(name=escape_markdown_v2(persona_config.name), id=persona_id)
        await query.edit_message_text(final_prompt, reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None # Сбрасываем имя редактируемого настроения
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await query.edit_message_text(prompt_new_name, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        try:
             encoded_mood_name = data.split("editmood_select_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except IndexError:
             logger.error(f"Could not parse mood name from callback: {data}")
             await query.edit_message_text(error_unhandled_choice)
             return await edit_moods_menu(update, context, persona_config=persona_config)
        except Exception as decode_err:
             logger.error(f"Error decoding mood name '{encoded_mood_name}' from callback: {decode_err}")
             await query.edit_message_text(error_decode_mood)
             return await edit_moods_menu(update, context, persona_config=persona_config)

        context.user_data['edit_mood_name'] = original_mood_name # Сохраняем оригинальное имя
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' to edit for {persona_id}.")

        current_prompt_raw = "_не найдено_"
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            current_prompt_raw = current_moods.get(original_mood_name, "_нет промпта_")
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt_raw = "_ошибка чтения промпта_"

        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        # Экранируем промпт для отображения в `code`, имя для **
        prompt_display = escape_markdown_v2(current_prompt_raw[:300] + "..." if len(current_prompt_raw) > 300 else current_prompt_raw)
        display_name = escape_markdown_v2(original_mood_name)
        final_prompt = prompt_new_prompt_fmt.format(name=display_name, prompt=prompt_display)
        await query.edit_message_text(final_prompt, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         try:
             encoded_mood_name = data.split("deletemood_confirm_", 1)[1]
             original_mood_name = urllib.parse.unquote(encoded_mood_name)
         except IndexError:
             logger.error(f"Could not parse mood name from delete callback: {data}")
             await query.edit_message_text(error_unhandled_choice)
             return await edit_moods_menu(update, context, persona_config=persona_config)
         except Exception as decode_err:
             logger.error(f"Error decoding mood name '{encoded_mood_name}' from delete callback: {decode_err}")
             await query.edit_message_text(error_decode_mood)
             return await edit_moods_menu(update, context, persona_config=persona_config)


         context.user_data['delete_mood_name'] = original_mood_name # Сохраняем оригинальное имя для удаления
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' for {persona_id}. Asking confirmation.")
         escaped_original_name = escape_markdown_v2(original_mood_name)
         # Текст кнопок не экранируем
         # Передаем ЗАКОДИРОВАННОЕ имя в callback для подтверждения удаления
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
            ]
         final_confirm_prompt = prompt_confirm_delete_fmt.format(name=escaped_original_name)
         await query.edit_message_text(final_confirm_prompt, reply_markup=InlineKeyboardMarkup(keyboard))
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text(error_unhandled_choice)
    return await edit_moods_menu(update, context, persona_config=persona_config)


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    # Валидация имени: буквы/цифры/дефис/подчеркивание, без пробелов
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    error_validation = "название: 1-30 символов, только буквы/цифры/дефис/подчеркивание \\(кириллица/латиница\\), без пробелов\\. попробуй еще:"
    error_name_exists_fmt = escape_markdown_v2("настроение '{name}' уже существует. выбери другое:") # Placeholder
    error_db = escape_markdown_v2("ошибка базы данных при проверке имени.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    prompt_for_prompt_fmt = "отлично! теперь отправь **текст промпта** для настроения **'{name}'**:" # Placeholder

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME

    mood_name = mood_name_raw # Используем валидированное имя

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            current_moods = {}
            try:
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} in mood name check, starting fresh.")

            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists for persona {persona_id}.")
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                final_exists_msg = error_name_exists_fmt.format(name=escape_markdown_v2(mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[back_button]]))
                return EDIT_MOOD_NAME

            # Store name and proceed
            context.user_data['edit_mood_name'] = mood_name # Сохраняем введенное имя
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            final_prompt = prompt_for_prompt_fmt.format(name=escape_markdown_v2(mood_name))
            await update.message.reply_text(final_prompt, reply_markup=InlineKeyboardMarkup([[back_button]]))
            return EDIT_MOOD_PROMPT

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, reply_markup=ReplyKeyboardRemove())
        # Остаемся в том же состоянии для повторного ввода
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END # Завершаем при неизвестной ошибке


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name') # Получаем сохраненное имя
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    error_no_session = escape_markdown_v2("ошибка: сессия редактирования потеряна.")
    error_not_found = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_validation = escape_markdown_v2("промпт настроения: 1-1500 символов. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_saved_fmt = "✅ настроение **{name}** сохранено!" # Placeholder

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_PROMPT

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner.has(User.telegram_id == user_id)
             ).first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove())
                context.user_data.clear()
                return ConversationHandler.END

            try:
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} when saving mood prompt, resetting moods.")
                 current_moods = {}

            # Add or update mood using the name stored in user_data
            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods) # Функция set_moods использует flag_modified
            db.commit()

            context.user_data.pop('edit_mood_name', None) # Очищаем имя из user_data
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            final_success_msg = success_saved_fmt.format(name=escape_markdown_v2(mood_name))
            await update.message.reply_text(final_success_msg)

            # Return to mood menu
            db.refresh(persona_config) # Обновляем объект после коммита
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db)
        # Rollback handled by context manager
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general)
        # Rollback handled by context manager
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name') # Получаем оригинальное имя
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна.")
    error_not_found_persona = escape_markdown_v2("ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения.")
    info_not_found_mood_fmt = escape_markdown_v2("настроение '{name}' не найдено (уже удалено?).") # Placeholder
    error_decode_mood = escape_markdown_v2("ошибка декодирования имени настроения для удаления.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_delete_fmt = "🗑️ настроение **{name}** удалено." # Placeholder

    # Получаем закодированное имя из callback'а для проверки
    encoded_mood_name_from_callback = ""
    original_name_from_callback = ""
    if data.startswith("deletemood_delete_"):
        try:
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except IndexError:
            logger.error(f"Could not parse encoded mood name from delete confirm callback: {data}")
            await query.answer("Ошибка данных", show_alert=True)
            await query.edit_message_text(error_no_session, reply_markup=None)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name '{encoded_mood_name_from_callback}' from delete confirm callback: {decode_err}")
            await query.answer("Ошибка данных", show_alert=True)
            await query.edit_message_text(error_decode_mood, reply_markup=None)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)


    if not mood_name_to_delete or not persona_id or not encoded_mood_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. StoredName='{mood_name_to_delete}', CallbackDecoded='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("Ошибка сессии", show_alert=True)
        await query.edit_message_text(error_no_session, reply_markup=None)
        context.user_data.pop('delete_mood_name', None) # Очищаем некорректное имя
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
                await query.edit_message_text(error_not_found_persona, reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END

            try:
                current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} during mood deletion, assuming empty.")
                 current_moods = {}

            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete]
                persona_config.set_moods(db, current_moods)
                db.commit()

                context.user_data.pop('delete_mood_name', None)
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                final_success_msg = success_delete_fmt.format(name=escape_markdown_v2(mood_name_to_delete))
                await query.edit_message_text(final_success_msg)
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id} (maybe already deleted).")
                final_not_found_msg = info_not_found_mood_fmt.format(name=escape_markdown_v2(mood_name_to_delete))
                await query.edit_message_text(final_not_found_msg, reply_markup=None)
                context.user_data.pop('delete_mood_name', None)

            # Return to mood menu
            db.refresh(persona_config) # Обновляем объект
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text(error_db, reply_markup=None)
        # Rollback handled by context manager
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text(error_general, reply_markup=None)
        # Rollback handled by context manager
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user_id = update.effective_user.id
    persona_id = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled persona edit/mood edit for persona {persona_id}.")
    cancel_message = escape_markdown_v2("редактирование отменено.")
    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            # Редактируем только если текст отличается
            if query.message and query.message.text != cancel_message:
                await query.edit_message_text(cancel_message, reply_markup=None)
        elif message:
            # Отправляем новое сообщение, если это была команда /cancel
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        if message:
            try:
                # Пробуем отправить как новое сообщение в любом случае при ошибке
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove())
            except Exception as send_e:
                logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END


# --- Delete Persona Conversation ---
async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    # +++ ПРОВЕРКА ПОДПИСКИ (пропускаем для callback) +++
    is_callback = update.callback_query is not None
    if not is_callback:
        if not await check_channel_subscription(update, context):
            await send_subscription_required_message(update, context)
            return ConversationHandler.END
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Очищаем перед началом

    error_not_found_fmt = escape_markdown_v2("личность с id `{id}` не найдена или не твоя.") # Placeholder
    error_db = escape_markdown_v2("ошибка базы данных.")
    error_general = escape_markdown_v2("непредвиденная ошибка.")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    prompt_delete_fmt = """
🚨 **ВНИМАНИЕ\\!** 🚨
удалить личность **'{name}'** \\(id: `{id}`\\)\\?

это действие **НЕОБРАТИМО**\\!
    """ # Placeholder

    try:
        with next(get_db()) as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                 final_error_msg = error_not_found_fmt.format(id=persona_id)
                 reply_target = update.callback_query.message if is_callback else update.effective_message
                 if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                 await reply_target.reply_text(final_error_msg, reply_markup=ReplyKeyboardRemove())
                 return ConversationHandler.END

            context.user_data['delete_persona_id'] = persona_id
            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            # Текст кнопок не экранируем
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_name_display}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = prompt_delete_fmt.format(name=escape_markdown_v2(persona_config.name), id=persona_id)

            reply_target = update.callback_query.message if is_callback else update.effective_message
            if is_callback:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup)
                      else:
                           await query.answer()
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup)
            else:
                 await reply_target.reply_text(msg_text, reply_markup=reply_markup)

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
            return DELETE_PERSONA_CONFIRM
    except SQLAlchemyError as e:
         logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_db)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, error_general)
         return ConversationHandler.END


async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    usage_text = "укажи id личности: `/deletepersona <id>`\nили используй кнопку из /mypersonas"
    error_invalid_id = escape_markdown_v2("ID должен быть числом.")
    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id)
        return ConversationHandler.END
    return await _start_delete_convo(update, context, persona_id)


async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем удаление...") # Plain text
    error_invalid_id = escape_markdown_v2("Ошибка: неверный ID личности в кнопке.")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        await query.edit_message_text(error_invalid_id)
        return ConversationHandler.END


async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("ошибка: неверные данные для удаления или сессия потеряна.")
    error_delete_failed = escape_markdown_v2("❌ не удалось удалить личность (ошибка базы данных).")
    success_deleted_fmt = escape_markdown_v2("✅ личность '{name}' удалена.") # Placeholder

    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern:
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.answer("Ошибка сессии", show_alert=True) # Plain text
         await query.edit_message_text(error_no_session, reply_markup=None)
         context.user_data.clear()
         return ConversationHandler.END

    await query.answer("Удаляем...") # Plain text

    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id}"
    try:
        with next(get_db()) as db:
             # Находим владельца по telegram_id
             user = db.query(User).filter(User.telegram_id == user_id).first()
             if not user:
                  logger.error(f"User {user_id} not found in DB during persona deletion confirmation.")
                  await query.edit_message_text(escape_markdown_v2("Ошибка: пользователь не найден."), reply_markup=None)
                  context.user_data.clear()
                  return ConversationHandler.END

             # Находим персону по ID и ID владельца
             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 # Используем функцию delete_persona_config, которая сама коммитит или откатывает
                 if delete_persona_config(db, persona_id, user.id):
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name_deleted}').")
                     deleted_ok = True
                 else:
                     logger.error(f"delete_persona_config returned False for persona {persona_id}, user internal ID {user.id}.")
                     # delete_persona_config уже сделала rollback при ошибке
             else:
                 logger.warning(f"User {user_id} confirmed delete, but persona {persona_id} not found (maybe already deleted). Assuming OK.")
                 deleted_ok = True # Считаем успешным, если уже удалено

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager or delete_persona_config
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)

    if deleted_ok:
        final_success_msg = success_deleted_fmt.format(name=escape_markdown_v2(persona_name_deleted))
        await query.edit_message_text(final_success_msg, reply_markup=None)
    else:
        await query.edit_message_text(error_delete_failed, reply_markup=None)

    context.user_data.clear()
    return ConversationHandler.END


async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer() # Plain text
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")
    cancel_message = escape_markdown_v2("удаление отменено.")
    await query.edit_message_text(cancel_message, reply_markup=None)
    context.user_data.clear()
    return ConversationHandler.END


async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    error_no_persona = escape_markdown_v2("В этом чате нет активной личности.")
    error_not_owner = escape_markdown_v2("Только владелец личности может ее заглушить.")
    error_no_instance = escape_markdown_v2("Ошибка: не найден объект связи с чатом.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке заглушить бота.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды.")
    info_already_muted_fmt = escape_markdown_v2("Личность '{name}' уже заглушена в этом чате.") # Placeholder
    # <<< ИЗМЕНЕНО: Убрана r"" строка >>>
    success_muted_fmt = "✅ Личность '{name}' больше не будет отвечать в этом чате \\(но будет запоминать сообщения\\)\\. Используйте /unmutebot, чтобы вернуть\\." # Placeholder

    with next(get_db()) as db:
        try:
            instance_info = get_persona_and_context_with_owner(chat_id, db)
            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove())
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance
            persona_name_escaped = escape_markdown_v2(persona.name) # Экранируем имя здесь

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove())
                return

            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id} during mute.")
                await update.message.reply_text(error_no_instance, reply_markup=ReplyKeyboardRemove())
                return

            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                db.commit()
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id} by user {user_id}.")
                # <<< ИЗМЕНЕНО: Форматируем и экранируем результат >>>
                final_success_msg = escape_markdown_v2(success_muted_fmt.format(name=persona.name))
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove())
            else:
                final_already_muted_msg = info_already_muted_fmt.format(name=persona_name_escaped)
                await update.message.reply_text(final_already_muted_msg, reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_db)
            # Rollback handled by context manager
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general)
            # Rollback handled by context manager


async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id}")

    # +++ ПРОВЕРКА ПОДПИСКИ +++
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return
    # +++ КОНЕЦ ПРОВЕРКИ ПОДПИСКИ +++

    error_no_persona = escape_markdown_v2("В этом чате нет активной личности, которую можно размьютить.")
    error_not_owner = escape_markdown_v2("Только владелец личности может снять заглушку.")
    error_db = escape_markdown_v2("Ошибка базы данных при попытке вернуть бота к общению.")
    error_general = escape_markdown_v2("Непредвиденная ошибка при выполнении команды.")
    info_not_muted_fmt = escape_markdown_v2("Личность '{name}' не была заглушена.") # Placeholder
    success_unmuted_fmt = escape_markdown_v2("✅ Личность '{name}' снова может отвечать в этом чате.") # Placeholder

    with next(get_db()) as db:
        try:
            # Fetch the active instance directly with relations needed for checks
            active_instance = db.query(ChatBotInstance)\
                .options(
                    selectinload(ChatBotInstance.bot_instance_ref)
                    .selectinload(BotInstance.owner),
                    selectinload(ChatBotInstance.bot_instance_ref)
                    .selectinload(BotInstance.persona_config)
                )\
                .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
                .first()

            if not active_instance or not active_instance.bot_instance_ref or not active_instance.bot_instance_ref.owner or not active_instance.bot_instance_ref.persona_config:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove())
                return

            # Check ownership
            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name
            escaped_persona_name = escape_markdown_v2(persona_name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove())
                return

            # Perform unmute
            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id} by user {user_id}.")
                final_success_msg = success_unmuted_fmt.format(name=escaped_persona_name)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove())
            else:
                final_not_muted_msg = info_not_muted_fmt.format(name=escaped_persona_name)
                await update.message.reply_text(final_not_muted_msg, reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_db)
            # Rollback handled by context manager
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general)
            # Rollback handled by context manager
