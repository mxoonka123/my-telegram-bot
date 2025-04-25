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

from yookassa import Configuration, Payment
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
from db import (
    get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner, check_and_update_user_limits, activate_subscription,
    create_bot_instance, link_bot_instance_to_chat, delete_persona_config,
    get_db,
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

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

TOS_TEXT = """
**📜 Пользовательское Соглашение**

Дата последнего обновления: [Вставь актуальную дату, например, 25.07.2024]

**1. Общие положения**
1.1. Настоящее Пользовательское Соглашение (далее - "Соглашение") регулирует отношения между владельцем Telegram-бота [Название твоего бота или @username] (далее – "Сервис") и пользователем Сервиса (далее – "Пользователь").
1.2. Используя Сервис, Пользователь подтверждает, что ознакомился, понял и полностью принимает условия настоящего Соглашения. Если Пользователь не согласен с условиями Соглашения, он должен немедленно прекратить использование Сервиса.
1.3. Сервис предоставляет Пользователю возможность создавать и взаимодействовать с виртуальными AI-собеседниками (далее – "Личности") на базе языковых моделей.

**2. Подписка и Оплата**
2.1. Сервис предлагает бесплатный и платный (Premium) уровни доступа. Условия и лимиты каждого уровня описаны в команде `/profile` и `/subscribe`.
2.2. Платная подписка предоставляет расширенные возможности и увеличенные лимиты на определенный срок ({subscription_duration} дней).
2.3. Стоимость подписки составляет {subscription_price} {subscription_currency} за {subscription_duration} дней.
2.4. Оплата осуществляется через платежную систему Yookassa. Сервис не хранит платежные данные Пользователя.
2.5. **Политика возвратов:** Оплачивая подписку, Пользователь подтверждает свое согласие с тем, что **оплаченные средства за предоставленный доступ к расширенным возможностям Сервиса возврату не подлежат**. Активация подписки происходит сразу после успешной оплаты.
2.6. В исключительном случае длительной технической недоступности Сервиса (более 7 календарных дней подряд) по вине владельца Сервиса, Пользователь с активной подпиской может обратиться в поддержку (если контакт указан) для рассмотрения возможности продления срока действия подписки на период недоступности. Решение о продлении принимается владельцем Сервиса в индивидуальном порядке.

**3. Права и обязанности Сторон**
3.1. Пользователь обязуется:
    - Не использовать Сервис в противозаконных целях.
    - Не пытаться нарушить работу Сервиса.
    - Не использовать Сервис для распространения спама, вредоносного ПО или запрещенной информации.
    - Предоставлять достоверную информацию при необходимости (например, при оплате).
3.2. Владелец Сервиса имеет право:
    - Изменять условия настоящего Соглашения в одностороннем порядке, уведомляя Пользователей путем публикации новой версии Соглашения или иным доступным способом.
    - Приостанавливать или прекращать доступ Пользователя к Сервису в случае нарушения настоящего Соглашения.
    - Модифицировать Сервис, включая изменение функционала, лимитов и стоимости подписки.

**4. Ограничение ответственности**
4.1. Сервис предоставляется "как есть". Владелец Сервиса не несет ответственности за возможные ошибки, сбои в работе Сервиса или языковых моделей.
4.2. Ответы, генерируемые Личностями, являются результатом работы AI и могут быть неточными, неполными или не соответствовать ожиданиям Пользователя. Владелец Сервиса не несет ответственности за содержание ответов AI.
4.3. Владелец Сервиса не несет ответственности за любые прямые или косвенные убытки Пользователя, возникшие в результате использования или невозможности использования Сервиса.

**5. Конфиденциальность**
5.1. Сервис собирает и обрабатывает минимально необходимые данные для своей работы (Telegram ID, username, данные о подписке, созданные личности, история сообщений для контекста AI).
5.2. Владелец Сервиса принимает разумные меры для защиты данных Пользователя, однако не может гарантировать абсолютную безопасность в сети Интернет.
5.3. Детальная информация о сборе и обработке данных может быть изложена в отдельной Политике Конфиденциальности (если применимо).

**6. Заключительные положения**
6.1. Настоящее Соглашение вступает в силу с момента начала использования Сервиса Пользователем и действует до момента прекращения использования Сервиса Пользователем или прекращения работы Сервиса.
6.2. Все споры и разногласия решаются путем переговоров.
""".format(subscription_duration=SUBSCRIPTION_DURATION_DAYS, subscription_price=f"{SUBSCRIPTION_PRICE_RUB:.0f}", subscription_currency=SUBSCRIPTION_CURRENCY)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("упс... что-то пошло не так. попробуй еще раз позже.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    chat_instance = db.query(ChatBotInstance)\
        .options(
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.persona_config),
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.owner)
        )\
        .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
        .first()
    if not chat_instance or not chat_instance.active: return None
    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance or not bot_instance.persona_config or not bot_instance.owner:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance, PersonaConfig or Owner.")
         return None
    persona_config = bot_instance.persona_config
    owner_user = bot_instance.owner
    persona = Persona(persona_config, chat_instance)
    context_list = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context_list, owner_user

async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return "ошибка: ключ api не настроен."
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
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
             resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if "content" in data and isinstance(data["content"], list):
            full_response = " ".join([part.get("text", "") for part in data["content"] if part.get("type") == "text"])
            return full_response.strip()
        elif isinstance(data.get("content"), dict) and "text" in data["content"]:
            logger.debug("Langdock response content is a dict, extracting text.")
            return data["content"]["text"].strip()
        elif "response" in data and isinstance(data["response"], str):
             logger.debug("Langdock response format has 'response' field.")
             return data.get("response", "").strip()
        else:
             logger.warning(f"Could not extract text from Langdock response: {data}")
             return ""
    except httpx.ReadTimeout:
         logger.error("Langdock API request timed out.")
         return "хм, кажется, я слишком долго думал... попробуй еще раз?"
    except httpx.HTTPStatusError as e:
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True)
        return f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        return "не могу связаться с ai сейчас..."
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        return "произошла внутренняя ошибка при генерации ответа."

async def process_and_send_response(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, chat_id: str, persona: Persona, full_bot_response_text: str, db: Session):
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return
    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}")

    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            db.flush()
            logger.debug("AI response added to database context.")
        except SQLAlchemyError as e:
            logger.error(f"DB Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
    else:
        logger.warning("Cannot add AI response to context, chat_instance is None.")

    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)
    for gif in gif_links:
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()

    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    max_messages = persona.config.max_response_messages if persona.config else 3
    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        if text_parts_to_send:
             text_parts_to_send[-1] += "..."

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
                 await context.bot.send_message(chat_id=chat_id, text=part)
            except Exception as e:
                 logger.error(f"Error sending text part to {chat_id}: {e}", exc_info=True)
                 break

            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(0.4, 0.9))

async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    text = (
        f"упс! 😕 лимит сообщений ({user.daily_message_count}/{user.message_limit}) на сегодня достигнут.\n\n"
        f"✨ **хочешь безлимита?** ✨\n"
        f"подписка за {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}/мес дает:\n"
        f"✅ **{PAID_DAILY_MESSAGE_LIMIT}** сообщений в день\n"
        f"✅ до **{PAID_PERSONA_LIMIT}** личностей\n"
        f"✅ полная настройка промптов и настроений\n\n"
        "👇 жми /subscribe или кнопку ниже!"
    )
    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if update.effective_message:
             await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        elif update.effective_chat:
             await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("--- handle_message ENTERED ---")
    if not update.message or not update.message.text: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = update.message.text
    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona for chat {chat_id}. Ignoring.")
                return
            persona, current_context_list, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id} ({owner_user.telegram_id})")

            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}). Not responding or saving context.")
                return

            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    db.flush()
                    context_added = True
                    logger.debug("Added user message to context.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error adding user message to context: {e_ctx}", exc_info=True)
                    db.rollback()
                    await update.message.reply_text("ошибка при сохранении вашего сообщения.")
                    return
            else:
                logger.error("Cannot add user message to context, chat_instance is None.")

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Message saved to context, but ignoring response.")
                db.commit()
                return

            available_moods = persona.get_all_mood_names()
            if message_text.lower() in map(str.lower, available_moods):
                 logger.info(f"Message '{message_text}' matched mood name. Changing mood.")
                 await mood(update, context, db=db, persona=persona)
                 db.commit()
                 return

            should_ai_respond = True
            if update.effective_chat.type in ["group", "supergroup"]:
                 if persona.should_respond_prompt_template:
                     should_respond_prompt = persona.format_should_respond_prompt(message_text)
                     if should_respond_prompt:
                         try:
                             logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id}...")
                             context_for_should_respond = get_context_for_chat_bot(db, persona.chat_instance.id) if persona.chat_instance else []
                             decision_response = await send_to_langdock(
                                 system_prompt=should_respond_prompt,
                                 messages=context_for_should_respond
                             )
                             answer = decision_response.strip().lower()
                             logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")
                             if answer.startswith("д"):
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond based on AI='{answer}'.")
                                 should_ai_respond = True
                             elif random.random() < 0.05:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond randomly despite AI='{answer}'.")
                                 should_ai_respond = True
                             else:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding NOT to respond based on AI='{answer}'.")
                                 should_ai_respond = False
                         except Exception as e:
                              logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                              logger.warning("Error in should_respond. Defaulting to respond.")
                              should_ai_respond = True
                     else:
                          logger.debug(f"No should_respond_prompt generated for persona {persona.name}. Defaulting to respond in group.")
                          should_ai_respond = True
                 else:
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic for message: {message_text[:50]}...")
                 db.commit()
                 return

            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error getting context for AI response: {e_ctx}", exc_info=True)
                     db.rollback()
                     await update.message.reply_text("ошибка при получении контекста для ответа.")
                     return
            elif not context_added:
                 logger.warning("Cannot generate AI response without updated context due to prior error.")
                 await update.message.reply_text("ошибка: не удалось сохранить ваше сообщение перед ответом.")
                 return

            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            logger.debug("Formatted main system prompt.")

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            db.commit()
            logger.debug(f"Committed DB changes for handle_message cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message: {e}", exc_info=True)
             if db.is_active: db.rollback()
             await update.message.reply_text("ошибка базы данных, попробуйте позже.")
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple: return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding or saving context.")
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

            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {context_text_placeholder}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    db.flush()
                    context_added = True
                    logger.debug(f"Added media placeholder to context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error adding media placeholder context: {e_ctx}", exc_info=True)
                     db.rollback()
                     if update.effective_message: await update.effective_message.reply_text("ошибка при сохранении информации о медиа.")
                     return
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")

            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Media saved to context, but ignoring response.")
                db.commit()
                return

            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id} has no {media_type} template. Skipping response generation.")
                db.commit()
                return

            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    db.rollback()
                    if update.effective_message: await update.effective_message.reply_text("ошибка при получении контекста для ответа на медиа.")
                    return
            elif not context_added:
                 logger.warning("Cannot generate AI media response without updated context.")
                 if update.effective_message: await update.effective_message.reply_text("ошибка: не удалось сохранить информацию о медиа перед ответом.")
                 return

            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit()
                 return
            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            db.commit()
            logger.debug(f"Committed DB changes for handle_media cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if db.is_active: db.rollback()
             if update.effective_message: await update.effective_message.reply_text("ошибка базы данных.")
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()


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
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                reply_text = (
                    f"привет! я {persona.name}. я уже активен в этом чате.\n"
                    "используй /help для списка команд."
                )
                await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            else:
                db.refresh(user)
                now = datetime.now(timezone.utc)
                if not user.last_message_reset or user.last_message_reset.date() < now.date():
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit()
                    db.refresh(user)

                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber and user.subscription_expires_at else ""
                persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
                reply_text = (
                    f"привет! 👋 я бот для создания ai-собеседников ({context.bot.username}).\n\n"
                    f"твой статус: **{status}**{expires_text}\n"
                    f"личности: {persona_count}/{user.persona_limit} | "
                    f"сообщения: {user.daily_message_count}/{user.message_limit}\n\n"
                    "**начало работы:**\n"
                    "1. `/createpersona <имя>` - создай ai-личность.\n"
                    "2. `/mypersonas` - посмотри своих личностей и управляй ими.\n"
                    # "3. `/addbot <id>` - добавь личность в чат.\n\n" # Убрали, т.к. кнопка в mypersonas
                    "`/profile` - детали статуса | `/subscribe` - узнать о подписке\n"
                    "`/help` - все команды"
                )
                await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка при загрузке данных. попробуй позже.")
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("произошла ошибка при обработке команды /start.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
     if not update.message: return
     user_id = update.effective_user.id
     chat_id = str(update.effective_chat.id)
     logger.info(f"CMD /help < User {user_id} in Chat {chat_id}")
     await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
     help_text = (
         "**🤖 основные команды:**\n"
         "/start - приветствие и твой статус\n"
         "/help - эта справка\n"
         "/profile - твой статус подписки и лимиты\n"
         "/subscribe - инфо о подписке и оплата\n\n"
         "**👤 управление личностями:**\n"
         "/createpersona <имя> [описание] - создать новую\n"
         "/mypersonas - список твоих личностей и кнопки управления (редакт., удалить, добавить в чат)\n"
         # Убрали дублирующие команды, т.к. есть кнопки
         # "/editpersona <id> - изменить личность (имя, промпты, настройки)\n"
         # "/deletepersona <id> - удалить личность (!)\n"
         # "/addbot <id> - активировать личность в чате\n\n"
         "\n**💬 управление в чате (где есть личность):**\n"
         "/mood [настроение] - сменить настроение активной личности\n"
         "/reset - очистить память (контекст) личности в этом чате\n"
         "/mutebot - заставить личность молчать в чате\n"
         "/unmutebot - разрешить личности отвечать в чате"
     )
     await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())

async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    is_callback = update.callback_query is not None
    message = update.message if not is_callback else update.callback_query.message
    if not message: return
    chat_id = str(message.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id}")

    close_db_later = False
    db_session = db
    if db_session is None:
        db_context = get_db()
        db_session = next(db_context)
        close_db_later = True
        persona_info_tuple = get_persona_and_context_with_owner(chat_id, db_session)
        if not persona_info_tuple:
            reply_text = "в этом чате нет активной личности."
            try:
                if is_callback: await update.callback_query.edit_message_text(reply_text)
                else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
            logger.debug(f"No active persona for chat {chat_id}. Cannot set mood.")
            if close_db_later: db_session.close()
            return
        persona, _, _ = persona_info_tuple
        chat_bot_instance = persona.chat_instance
    elif persona is not None:
        chat_bot_instance = persona.chat_instance
        if not chat_bot_instance:
            logger.error("Mood called with persona, but persona.chat_instance is None.")
            if close_db_later: db_session.close()
            return
    else:
         logger.error("Mood called with db but without persona.")
         if close_db_later: db_session.close()
         return

    try:
        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Ignoring mood command.")
            reply_text=f"Личность '{persona.name}' сейчас заглушена (/unmutebot)."
            try:
                 if is_callback: await update.callback_query.edit_message_text(reply_text)
                 else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = persona.get_all_mood_names()
        available_moods_lower = {m.lower(): m for m in available_moods}

        if not available_moods:
             reply_text = f"у личности '{persona.name}' не настроены настроения."
             try:
                 if is_callback: await update.callback_query.edit_message_text(reply_text)
                 else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
             logger.warning(f"Persona {persona.name} has no moods defined.")
             if close_db_later: db_session.close()
             return

        mood_arg_lower = None
        if not is_callback and context.args:
             mood_arg_lower = context.args[0].lower()
        elif not is_callback and message.text and message.text.lower() in available_moods_lower:
             mood_arg_lower = message.text.lower()
        elif is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             if len(parts) >= 3:
                  mood_arg_lower = "_".join(parts[2:-1]).lower()
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")

        if mood_arg_lower and mood_arg_lower in available_moods_lower:
             original_mood_name = available_moods_lower[mood_arg_lower]
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, original_mood_name)
             reply_text = f"настроение для '{persona.name}' теперь: **{original_mood_name}**"
             try:
                 if is_callback:
                     await update.callback_query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
                 else:
                     await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {original_mood_name}.")
        else:
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{persona.id}")] for m in available_moods]
             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = ""
             try:
                 current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
             except Exception as e:
                 logger.error(f"Error getting current mood for {chat_bot_instance.id}: {e}")
                 current_mood_text = "Неизвестно"

             if mood_arg_lower:
                 reply_text = f"не знаю настроения '{mood_arg_lower}' для '{persona.name}'. выбери из списка:"
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id}. Sent mood selection.")
             else:
                 reply_text = f"текущее настроение: **{current_mood_text}**. выбери новое для '{persona.name}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

             try:
                 if is_callback:
                      query = update.callback_query
                      if query.message.text != reply_text:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                      else:
                           await query.edit_message_reply_markup(reply_markup=reply_markup)
                 else:
                      await message.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
             except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id}: {e}", exc_info=True)
         if db_session and db_session.is_active: db_session.rollback()
         reply_text = "ошибка базы данных при смене настроения."
         try:
             if is_callback: await update.callback_query.edit_message_text(reply_text)
             else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id}: {e}", exc_info=True)
         reply_text = "ошибка при обработке команды /mood."
         try:
             if is_callback: await update.callback_query.edit_message_text(reply_text)
             else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
         except Exception as send_err: logger.error(f"Error sending general error msg: {send_err}")
    finally:
        if close_db_later and db_session:
            db_session.close()

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                return
            persona, _, owner_user = persona_info_tuple
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("только владелец личности может сбросить её память.", reply_markup=ReplyKeyboardRemove())
                return
            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 await update.message.reply_text("ошибка: не найден экземпляр бота для сброса.")
                 return
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id} by user {user_id}.")
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()
            await update.message.reply_text("ошибка базы данных при сбросе контекста.")
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("ошибка при сбросе контекста.")

async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            "_имя обязательно, описание нет._",
            parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
        )
        return
    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None
    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text("имя личности: 2-50 символов.", reply_markup=ReplyKeyboardRemove())
         return
    if persona_description and len(persona_description) > 1500:
         await update.message.reply_text("описание: до 1500 символов.", reply_markup=ReplyKeyboardRemove())
         return
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
            if not is_admin(user_id) and persona_count >= user.persona_limit:
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({persona_count}/{user.persona_limit}).")
                 status_text = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                 text = (
                     f"упс! достигнут лимит личностей ({persona_count}/{user.persona_limit}) для статуса **{status_text}**. 😟\n"
                     f"чтобы создавать больше, используй /subscribe"
                 )
                 await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
                 return
            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                await update.message.reply_text(f"личность с именем '{persona_name}' уже есть. выбери другое.", reply_markup=ReplyKeyboardRemove())
                return
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)
            await update.message.reply_text(
                f"✅ личность '{new_persona.name}' создана!\n"
                f"id: `{new_persona.id}`\n"
                f"описание: {new_persona.description}\n\n"
                f"добавь в чат или управляй через /mypersonas", # Обновили текст
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")
        except IntegrityError:
             logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
             await update.message.reply_text(f"ошибка: личность '{persona_name}' уже существует (возможно, гонка запросов). попробуй еще раз.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка базы данных при создании личности.")
        except Exception as e:
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка при создании личности.")

async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()
            if not user_with_personas:
                logger.error(f"User {user_id} not found after get_or_create in my_personas.")
                await update.message.reply_text("Ошибка: не удалось найти пользователя.")
                return

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            if not personas:
                await update.message.reply_text(
                    "у тебя пока нет личностей.\n"
                    "создай: /createpersona <имя>",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
                )
                return

            text = f"твои личности ({persona_count}/{persona_limit}):\n"
            keyboard = []
            for p in personas:
                 # Добавляем имя личности и ID в одну строку
                 keyboard.append([InlineKeyboardButton(f"👤 {p.name} (ID: {p.id})", callback_data=f"dummy_{p.id}")])
                 # Добавляем кнопки действий для этой личности в следующую строку
                 keyboard.append([
                     InlineKeyboardButton("⚙️ Редакт.", callback_data=f"edit_persona_{p.id}"),
                     InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_persona_{p.id}"),
                     InlineKeyboardButton("➕ В чат", callback_data=f"add_bot_{p.id}")
                 ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка при загрузке списка личностей.")
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("произошла ошибка при обработке команды /mypersonas.")


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    """Обработчик команды /addbot или колбэка add_bot_<id>."""
    is_callback = update.callback_query is not None
    message = update.message if not is_callback else update.callback_query.message
    if not message: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(message.chat.id)
    chat_title = message.chat.title or f"Chat {chat_id}"

    if is_callback and not persona_id:
         logger.error(f"add_bot_to_chat called from callback without persona_id")
         await update.callback_query.answer("Ошибка: ID личности не найден в колбэке.", show_alert=True)
         return
    elif not is_callback:
         logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id}) with args: {context.args}")
         args = context.args
         if not args or len(args) != 1 or not args[0].isdigit():
             await message.reply_text(
                 "формат: `/addbot <id персоны>`\n"
                 "или используй кнопку '➕ В чат' из /mypersonas",
                 parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
             )
             return
         try:
             persona_id = int(args[0])
         except ValueError:
             await message.reply_text("id личности должен быть числом.", reply_markup=ReplyKeyboardRemove())
             return
    else: # Is callback with persona_id
        logger.info(f"CALLBACK add_bot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id}) for persona_id: {persona_id}")
        await update.callback_query.answer("Добавляем личность...") # Ответ на колбэк

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona:
                 response_text = f"личность с id `{persona_id}` не найдена или не твоя."
                 if is_callback: await update.callback_query.edit_message_text(response_text, parse_mode=ParseMode.MARKDOWN)
                 else: await message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
                 return

            existing_active_link = db.query(ChatBotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).options(
                joinedload(ChatBotInstance.bot_instance_ref)
                .joinedload(BotInstance.persona_config)
            ).first()

            if existing_active_link:
                old_bot_instance = existing_active_link.bot_instance_ref
                if old_bot_instance and old_bot_instance.persona_config_id == persona.id:
                    response_text = f"личность '{persona.name}' уже активна в этом чате."
                    if is_callback: await update.callback_query.answer(response_text, show_alert=True) # Покажем в уведомлении
                    else: await message.reply_text(response_text, reply_markup=ReplyKeyboardRemove())
                    return
                else:
                    old_persona_name = old_bot_instance.persona_config.name if old_bot_instance and old_bot_instance.persona_config else "Неизвестная"
                    logger.info(f"Deactivating previous bot instance {existing_active_link.bot_instance_id} (Persona '{old_persona_name}') in chat {chat_id} before activating {persona_id}.")
                    existing_active_link.active = False
                    db.flush()

            user = get_or_create_user(db, user_id, username)
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == persona.id
            ).first()
            if not bot_instance:
                 bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Inst:{persona.name}")
                 logger.info(f"Created BotInstance {bot_instance.id} for persona {persona.id}")

            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            if chat_link:
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 db.commit()
                 logger.debug(f"Cleared {deleted_ctx} context messages for chat_bot_instance {chat_link.id} upon linking.")
                 response_text = f"✅ личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате! Память очищена."
                 # Отправляем новое сообщение, т.к. колбэк от mypersonas
                 await context.bot.send_message(chat_id=chat_id, text=response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
                 # Если исходное сообщение было от колбэка, можно его удалить или изменить
                 if is_callback:
                      try: await update.callback_query.delete_message()
                      except: pass # Игнорируем ошибки удаления старого сообщения
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 db.rollback()
                 response_text = "не удалось активировать личность (ошибка связывания)."
                 if is_callback: await context.bot.send_message(chat_id=chat_id, text=response_text)
                 else: await message.reply_text(response_text, reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id} - link_bot_instance_to_chat returned None.")
        except IntegrityError as e:
             db.rollback()
             logger.warning(f"IntegrityError potentially during addbot for persona {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
        except SQLAlchemyError as e:
             db.rollback()
             logger.error(f"Database error during /addbot for persona {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="ошибка базы данных при добавлении бота.")
        except Exception as e:
             try:
                 if db.is_active: db.rollback()
             except: pass
             logger.error(f"Error adding bot instance {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await context.bot.send_message(chat_id=chat_id, text="ошибка при активации личности.")

# --- Обработчики колбэков ---

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data: return

    try:
        # Не отвечаем сразу на колбэки, которые меняют сообщение
        pass # await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query {query.id}: {e}")

    chat_id = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    if data.startswith("set_mood_"):
        await query.answer() # Отвечаем здесь
        await mood(update, context)
    elif data == "subscribe_info":
        await query.answer() # Отвечаем здесь
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await query.answer("Создаю ссылку на оплату...") # Отвечаем здесь
        await generate_payment_link(update, context)
    elif data == "view_tos":
        await query.answer() # Отвечаем здесь
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer() # Отвечаем здесь
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
         # persona_id извлекается из data
         try:
             persona_id_to_add = int(data.split('_')[-1])
             await add_bot_to_chat(update, context, persona_id=persona_id_to_add)
         except (IndexError, ValueError):
             logger.error(f"Could not parse persona_id from add_bot callback data: {data}")
             await query.answer("Ошибка: неверный ID личности.", show_alert=True)
    elif data.startswith("dummy_"):
        await query.answer() # Просто отвечаем на нажатие кнопки с именем
    else:
        # Проверяем, не относится ли колбэк к активному диалогу
        known_prefixes = ("edit_field_", "edit_mood", "deletemood", "cancel_edit", "edit_persona_back", "delete_persona", "edit_persona_", "delete_persona_") # Добавили префиксы для кнопок
        if not any(data.startswith(p) for p in known_prefixes):
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            await query.answer() # Отвечаем, чтобы кнопка перестала грузиться

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            now = datetime.now(timezone.utc)
            if not user.last_message_reset or user.last_message_reset.date() < now.date():
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit()
                db.refresh(user)
            is_active_subscriber = user.is_active_subscriber
            status = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            expires_text = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active_subscriber and user.subscription_expires_at else "нет активной подписки"
            persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
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
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except SQLAlchemyError as e:
             logger.error(f"Database error during /profile for user {user_id}: {e}", exc_info=True)
             if db.is_active: db.rollback()
             await update.message.reply_text("ошибка базы данных при загрузке профиля.")
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text("ошибка при обработке команды /profile.")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update = update.callback_query.message if from_callback else update.message
    if not message_to_update: return

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY or not YOOKASSA_SHOP_ID.isdigit():
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        reply_markup = None
        logger.warning("Yookassa credentials not set or shop ID is not numeric.")
    else:
        text = (
            f"✨ **премиум подписка ({SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}/мес)** ✨\n\n"
            "получи максимум возможностей:\n"
            f"✅ **{PAID_DAILY_MESSAGE_LIMIT}** сообщений в день (вместо {FREE_DAILY_MESSAGE_LIMIT})\n"
            f"✅ **{PAID_PERSONA_LIMIT}** личностей (вместо {FREE_PERSONA_LIMIT})\n"
            f"✅ полная настройка всех промптов\n"
            f"✅ создание и редакт. своих настроений\n"
            f"✅ приоритетная поддержка (если будет)\n\n"
            f"подписка действует {SUBSCRIPTION_DURATION_DAYS} дней."
        )
        keyboard = [
            [InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")],
            [InlineKeyboardButton("✅ Принять и оплатить", callback_data="confirm_pay")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if from_callback:
            if message_to_update.text != text or message_to_update.reply_markup != reply_markup:
                 await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            else:
                 await update.callback_query.answer() # Отвечаем, если текст не изменился
        else:
            await message_to_update.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        if from_callback:
            try:
                await context.bot.send_message(chat_id=message_to_update.chat.id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")

async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текст Пользовательского Соглашения."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            TOS_TEXT,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to show ToS to user {user_id}: {e}")
        # Попытка отправить новое сообщение как fallback
        try:
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=TOS_TEXT,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception as send_e:
            logger.error(f"Failed to send fallback ToS message to user {user_id}: {send_e}")


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает подтверждение и финальную кнопку оплаты."""
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY or not YOOKASSA_SHOP_ID.isdigit():
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        reply_markup = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        logger.warning("Yookassa credentials not set or shop ID is not numeric.")
    else:
        text = (
             "✅ Отлично!\n\n"
             "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
             "[Пользовательским Соглашением](https://t.me/TODO_LINK_TO_TOS_OR_HANDLER)." # Замени ссылку или используй #view_tos?
             "\n\n👇"
        ) # TODO: Добавить ссылку на #view_tos если возможно или на сам текст
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")

async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY or not YOOKASSA_SHOP_ID.isdigit():
        logger.error("Yookassa credentials not set correctly.")
        await query.edit_message_text("❌ ошибка: сервис оплаты не настроен правильно.", reply_markup=None)
        return

    try:
        Configuration.configure(int(YOOKASSA_SHOP_ID), YOOKASSA_SECRET_KEY)
        logger.info(f"Yookassa configured for payment creation (Shop ID: {YOOKASSA_SHOP_ID}).")
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK: {conf_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка конфигурации платежной системы.", reply_markup=None)
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium подписка {context.bot.username} на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)}
    # !! Важно: Убедись, что бот публичный, или используй другой return_url !!
    # return_url = f"https://t.me/{context.bot.username}?start=payment_success" # Может не сработать для приватных ботов
    # Более надежный вариант - страница "спасибо" на твоем веб-ресурсе, если он есть
    return_url = f"https://t.me/{context.bot.username}" # Просто возврат в бот

    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ {context.bot.username} на {SUBSCRIPTION_DURATION_DAYS} дней",
                "quantity": 1.0,
                "amount": {"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1",
                "payment_mode": "full_prepayment",
                "payment_subject": "service"
            })
        ]
        # TODO: Узнай у Yookassa, нужен ли email обязательно или можно телефон/другой ID
        receipt_data = Receipt({
            "customer": {"email": f"user_{user_id}@telegram.bot"}, # Placeholder email
            "items": receipt_items,
        })
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при формировании данных чека.", reply_markup=None)
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

        # Запуск синхронного кода Yookassa в отдельном потоке
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or payment_response.status == 'canceled' or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid/empty/canceled response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = "❌ не удалось получить ссылку от платежной системы"
             if payment_response and payment_response.status == 'canceled': error_message += f" (статус: {payment_response.status})"
             else: error_message += " (неверный ответ)."
             error_message += "\nПопробуй позже."
             await query.edit_message_text(error_message, reply_markup=None)
             return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ ссылка для оплаты создана!\n\n"
            "нажми кнопку ниже для перехода к оплате. после успеха подписка активируется (может занять пару минут).",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        user_message = "❌ не удалось создать ссылку для оплаты. "
        if hasattr(e, 'code'): user_message += f"Проблема с API ЮKassa ({type(e).__name__})."
        else: user_message += "Произошла непредвиденная ошибка."
        user_message += "\nПопробуй еще раз позже или свяжись с поддержкой."
        try:
            await query.edit_message_text(user_message, reply_markup=None)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")

async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass

# --- Edit Persona Conversation ---

async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Helper to start edit conversation from command or button."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg, parse_mode=ParseMode.MARKDOWN)
                 else: await update.effective_message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN)
                 return ConversationHandler.END

            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config)
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:"

            if update.callback_query:
                 # Try editing the message from the button press
                 try: await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                 except Exception as edit_err: # If fails (e.g., message too old), send new message
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            else: # From command
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

        logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
        return EDIT_PERSONA_CHOICE
    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, "ошибка базы данных при начале редактирования.")
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, "непредвиденная ошибка.")
         return ConversationHandler.END

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nили используй кнопку из /mypersonas", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    persona_id = int(args[0])
    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for edit_persona_<id> callback button."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем редактирование...")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK edit_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_edit_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        await query.edit_message_text("Ошибка: неверный ID личности в кнопке.")
        return ConversationHandler.END

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, Persona ID from context: {persona_id}, Callback data: {data} ---")

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text("ошибка: сессия редактирования потеряна (нет id). начни снова.", reply_markup=None)
         return ConversationHandler.END

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id)
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} not found or not owned.")
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END
            is_premium_user = is_admin(user_id) or user.is_active_subscriber
    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.edit_message_text("ошибка базы данных при проверке данных.", reply_markup=None)
         return EDIT_PERSONA_CHOICE
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.edit_message_text("Непредвиденная ошибка.", reply_markup=None)
         return ConversationHandler.END

    if data == "cancel_edit":
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        if not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer("Доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        field_display_name = FIELD_MAP.get(field, field)
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        advanced_fields = ["should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             await query.answer(f"Поле '{field_display_name}' доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE

        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        if field == "max_response_messages":
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'** (число от 1 до 10):", parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return EDIT_MAX_MESSAGES
        else:
            current_value = getattr(persona_config, field, "")
            current_value_display = current_value if len(current_value) < 300 else current_value[:300] + "..."
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'**.\nтекущее:\n`{current_value_display}`", parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return EDIT_FIELD

    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         keyboard = await _get_edit_persona_keyboard(persona_config)
         await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
         context.user_data.pop('edit_field', None)
         return EDIT_PERSONA_CHOICE

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.message.reply_text("неизвестный выбор. попробуй еще раз.")
    return EDIT_PERSONA_CHOICE

async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_FIELD
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}' ---")

    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна. начни сначала.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    field_display_name = FIELD_MAP.get(field, field)

    validation_error = None
    if field == "name":
        if not (2 <= len(new_value) <= 50): validation_error = "имя: 2-50 символов."
    elif field == "description":
         if len(new_value) > 1500: validation_error = "описание: до 1500 символов."
    elif field.endswith("_prompt_template"):
         if len(new_value) > 3000: validation_error = "промпт: до 3000 символов."

    if validation_error:
        logger.debug(f"Validation failed for field '{field}': {validation_error}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error} попробуй еще раз:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            user = get_or_create_user(db, user_id)
            if field == "name" and new_value.lower() != persona_config.name.lower():
                existing = get_persona_by_name_and_owner(db, user.id, new_value)
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    await update.message.reply_text(f"имя '{new_value}' уже занято другой твоей личностью. попробуй другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                    return EDIT_FIELD

            setattr(persona_config, field, new_value)
            db.commit()
            db.refresh(persona_config)
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")
            context.user_data.pop('edit_field', None)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         try:
             if db.is_active: db.rollback()
         except: pass
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear()
         return ConversationHandler.END

async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages"
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна (нет persona_id). начни снова.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10): raise ValueError("Value out of range 1-10")
    except ValueError:
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text("неверное значение. введи число от 1 до 10:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MAX_MESSAGES

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            persona_config.max_response_messages = new_value
            db.commit()
            db.refresh(persona_config)
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            await update.message.reply_text(f"✅ макс. сообщений в ответе для **'{persona_config.name}'** установлено: **{new_value}**")
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         try:
             if db.is_active: db.rollback()
         except: pass
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear()
         return ConversationHandler.END

async def _get_edit_persona_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3)
    keyboard = [
        [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
        [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
        [InlineKeyboardButton(f"📊 Макс. ответов ({max_resp_msg})", callback_data="edit_field_max_response_messages")],
        [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
        [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
        [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
        [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
    ]
    return keyboard

async def _get_edit_moods_keyboard_internal(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
     if not persona_config: return []
     try:
         moods = json.loads(persona_config.mood_prompts_json or '{}')
     except json.JSONDecodeError:
         moods = {}
     keyboard = []
     if moods:
         sorted_moods = sorted(moods.keys())
         for mood_name in sorted_moods:
              keyboard.append([
                  InlineKeyboardButton(f"✏️ {mood_name.capitalize()}", callback_data=f"editmood_select_{mood_name}"),
                  InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{mood_name}")
              ])
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])
     return keyboard

async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message = update.effective_message
    if not message:
        logger.warning("Cannot return to edit menu: effective_message is None.")
        context.user_data.clear()
        return ConversationHandler.END
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if persona_config:
                keyboard = await _get_edit_persona_keyboard(persona_config)
                await message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                return EDIT_PERSONA_CHOICE
            else:
                logger.warning(f"Persona {persona_id} not found when trying to return to main edit menu.")
                await message.reply_text("Не удалось вернуться к меню редактирования (личность не найдена).")
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to return to main edit menu after error: {e}", exc_info=True)
        await message.reply_text("Не удалось вернуться к меню редактирования.")
        context.user_data.clear()
        return ConversationHandler.END

async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     message = update.effective_message
     if not message:
         logger.warning("Cannot return to mood menu: effective_message is None.")
         context.user_data.clear()
         return ConversationHandler.END
     try:
         with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_config:
                 keyboard = await _get_edit_moods_keyboard_internal(persona_config)
                 await message.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                 return EDIT_MOOD_CHOICE
             else:
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await message.reply_text("Не удалось вернуться к меню настроений (личность не найдена).")
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
         await message.reply_text("Не удалось вернуться к меню настроений.")
         context.user_data.clear()
         return ConversationHandler.END


async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    query = update.callback_query
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.", reply_markup=None)
        return ConversationHandler.END

    if persona_config is None:
        try:
            with next(get_db()) as db:
                persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
                if not persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                    context.user_data.clear()
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.edit_message_text("Ошибка базы данных при загрузке настроений.", reply_markup=None)
             return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    try:
        with next(get_db()) as db:
             user = get_or_create_user(db, user_id)
             if not is_admin(user_id) and not user.is_active_subscriber:
                 logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
                 await query.answer("Доступно по подписке", show_alert=True)
                 return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error checking premium status in edit_moods_menu: {e}", exc_info=True)

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         try:
            await query.message.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
         except Exception as send_e:
            logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE


async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    try:
        with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_mood_choice.")
                 await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.edit_message_text("Ошибка базы данных.", reply_markup=None)
         return EDIT_MOOD_CHOICE

    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await query.edit_message_text("введи **название** нового настроения (одно слово, например, 'радость'):", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        mood_name = data.split("editmood_select_", 1)[1]
        context.user_data['edit_mood_name'] = mood_name
        logger.debug(f"User {user_id} selected mood '{mood_name}' to edit for {persona_id}.")
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            current_prompt = current_moods.get(mood_name, "_нет промпта_")
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt = "_ошибка чтения промпта_"
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        prompt_display = current_prompt if len(current_prompt) < 300 else current_prompt[:300] + "..."
        await query.edit_message_text(f"редактирование настроения: **{mood_name}**\n\nтекущий промпт:\n`{prompt_display}`\n\nотправь **новый текст промпта**:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         mood_name = data.split("deletemood_confirm_", 1)[1]
         context.user_data['delete_mood_name'] = mood_name
         logger.debug(f"User {user_id} initiated delete for mood '{mood_name}' for {persona_id}. Asking confirmation.")
         keyboard = [[InlineKeyboardButton(f"✅ да, удалить '{mood_name}'", callback_data=f"deletemood_delete_{mood_name}")], [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]]
         await query.edit_message_text(f"точно удалить настроение **'{mood_name}'**?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text("неизвестный выбор настроения.")
    return await edit_moods_menu(update, context, persona_config=persona_config)

async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    mood_name = mood_name_raw
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if not mood_name or len(mood_name) > 30 or not re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name, re.UNICODE):
        logger.debug(f"Validation failed for mood name '{mood_name}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("название: 1-30 символов, только буквы/цифры/дефис/подчеркивание, без пробелов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text("ошибка: личность не найдена.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists for persona {persona_id}.")
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                await update.message.reply_text(f"настроение '{mood_name}' уже существует. выбери другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            await update.message.reply_text(f"отлично! теперь отправь **текст промпта** для настроения **'{mood_name}'**:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button]]))
            return EDIT_MOOD_PROMPT

    except json.JSONDecodeError:
         logger.error(f"Invalid JSON in mood_prompts_json for persona {persona_id} during name check.")
         await update.message.reply_text("ошибка чтения существующих настроений. попробуй отменить и начать заново.", reply_markup=ReplyKeyboardRemove())
         return EDIT_MOOD_NAME
    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка базы данных при проверке имени.", reply_markup=ReplyKeyboardRemove())
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("непредвиденная ошибка.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if not mood_prompt or len(mood_prompt) > 1500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("промпт настроения: 1-1500 символов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_PROMPT

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                context.user_data.clear()
                return ConversationHandler.END

            try:
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} when saving mood prompt, resetting moods.")
                 current_moods = {}

            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods)
            db.commit()

            context.user_data.pop('edit_mood_name', None)
            logger.info(f"User {user_id} updated mood '{mood_name}' for persona {persona_id}.")
            await update.message.reply_text(f"✅ настроение **'{mood_name}'** сохранено!")

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        try:
            if db.is_active: db.rollback()
        except: pass
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка базы данных при сохранении настроения.")
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка при сохранении настроения.")
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE
    await query.answer()
    data = query.data
    mood_name = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    expected_data_suffix = f"delete_delete_{mood_name}"
    if not mood_name or not persona_id or not data.startswith("deletemood_delete_") or not data.endswith(mood_name):
        logger.warning(f"User {user_id}: Mismatch in delete_mood_confirmed. Mood='{mood_name}', Data='{data}'")
        await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name}' for persona {persona_id}.")

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END

            try:
                current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} during mood deletion, assuming empty.")
                 current_moods = {}

            if mood_name in current_moods:
                del current_moods[mood_name]
                persona_config.set_moods(db, current_moods)
                db.commit()

                context.user_data.pop('delete_mood_name', None)
                logger.info(f"Successfully deleted mood '{mood_name}' for persona {persona_id}.")
                await query.edit_message_text(f"🗑️ настроение **'{mood_name}'** удалено.", parse_mode=ParseMode.MARKDOWN)
            else:
                logger.warning(f"Mood '{mood_name}' not found for deletion in persona {persona_id} (maybe already deleted).")
                await query.edit_message_text(f"настроение '{mood_name}' не найдено (уже удалено?).", reply_markup=None)
                context.user_data.pop('delete_mood_name', None)

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        try:
            if db.is_active: db.rollback()
        except: pass
        logger.error(f"Database error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка базы данных при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user_id = update.effective_user.id
    logger.info(f"User {user_id} cancelled persona edit/mood edit.")
    cancel_message = "редактирование отменено."
    try:
        if update.callback_query:
            await update.callback_query.answer()
            if update.callback_query.message and update.callback_query.message.text != cancel_message:
                await update.callback_query.edit_message_text(cancel_message, reply_markup=None)
        elif message:
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        if message:
            try:
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove())
            except Exception as send_e:
                logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END

# --- Delete Persona Conversation ---

async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Helper to start delete conversation from command or button."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear()

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg, parse_mode=ParseMode.MARKDOWN)
                 else: await update.effective_message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN)
                 return ConversationHandler.END

            context.user_data['delete_persona_id'] = persona_id
            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_config.name}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = (
                f"🚨 **ВНИМАНИЕ!** 🚨\n"
                f"удалить личность **'{persona_config.name}'** (id: `{persona_id}`)?\n\n"
                f"это действие **НЕОБРАТИМО**!"
            )

            if update.callback_query:
                 try: await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            else: # From command
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
        return DELETE_PERSONA_CONFIRM
    except SQLAlchemyError as e:
         logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, "ошибка базы данных.")
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting delete persona {persona_id}: {e}", exc_info=True)
         await context.bot.send_message(chat_id, "непредвиденная ошибка.")
         return ConversationHandler.END

async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/deletepersona <id>`\nили используй кнопку из /mypersonas", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    persona_id = int(args[0])
    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for delete_persona_<id> callback button."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer("Начинаем удаление...")
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"CALLBACK delete_persona < User {query.from_user.id} for persona_id: {persona_id}")
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        await query.edit_message_text("Ошибка: неверный ID личности в кнопке.")
        return ConversationHandler.END


async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    expected_pattern = f"delete_persona_confirm_{persona_id}"
    if not persona_id or data != expected_pattern: # Строгая проверка callback_data
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
         context.user_data.clear()
         return ConversationHandler.END

    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id}"
    try:
        with next(get_db()) as db:
             user = get_or_create_user(db, user_id)
             persona_to_delete = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 if delete_persona_config(db, persona_id, user.id):
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name_deleted}').")
                     deleted_ok = True
                 else:
                     logger.error(f"delete_persona_config returned False for persona {persona_id}, user internal ID {user.id}.")
             else:
                 logger.warning(f"User {user_id} confirmed delete, but persona {persona_id} not found (maybe already deleted).")
                 deleted_ok = True

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)

    if deleted_ok:
        await query.edit_message_text(f"✅ личность '{persona_name_deleted}' удалена.", reply_markup=None)
    else:
        await query.edit_message_text("❌ не удалось удалить личность.", reply_markup=None)

    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")
    await query.edit_message_text("удаление отменено.", reply_markup=None)
    context.user_data.clear()
    return ConversationHandler.END

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id}")

    with next(get_db()) as db:
        try:
            instance_info = get_persona_and_context_with_owner(chat_id, db)
            if not instance_info:
                await update.message.reply_text("В этом чате нет активной личности.", reply_markup=ReplyKeyboardRemove())
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("Только владелец личности может ее заглушить.", reply_markup=ReplyKeyboardRemove())
                return

            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id} during mute.")
                await update.message.reply_text("Ошибка: не найден объект связи с чатом.", reply_markup=ReplyKeyboardRemove())
                return

            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                db.commit()
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona.name}' больше не будет отвечать в этом чате (но будет запоминать сообщения). Используйте /unmutebot, чтобы вернуть.", reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(f"Личность '{persona.name}' уже заглушена в этом чате.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()
            await update.message.reply_text("Ошибка базы данных при попытке заглушить бота.")
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("Непредвиденная ошибка при выполнении команды.")

async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id}")

    with next(get_db()) as db:
        try:
            active_instance = db.query(ChatBotInstance)\
                .options(
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.owner),
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.persona_config)
                )\
                .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
                .first()

            if not active_instance:
                await update.message.reply_text("В этом чате нет активной личности, которую можно размьютить.", reply_markup=ReplyKeyboardRemove())
                return

            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name if active_instance.bot_instance_ref and active_instance.bot_instance_ref.persona_config else "Неизвестная"

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("Только владелец личности может снять заглушку.", reply_markup=ReplyKeyboardRemove())
                return

            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona_name}' снова может отвечать в этом чате.", reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(f"Личность '{persona_name}' не была заглушена.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()
            await update.message.reply_text("Ошибка базы данных при попытке вернуть бота к общению.")
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("Непредвиденная ошибка при выполнении команды.")
