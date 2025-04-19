import logging
import httpx
import random
import asyncio
import re
import uuid
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, contains_eager # Добавили contains_eager
from sqlalchemy.exc import IntegrityError, SQLAlchemyError # Добавили SQLAlchemyError
from typing import List, Dict, Any, Optional, Union, Tuple

from yookassa import Configuration, Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder


from config import (
    LANGDOCK_API_KEY, LANGDOCK_BASE_URL, LANGDOCK_MODEL,
    DEFAULT_MOOD_PROMPTS, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CURRENCY, WEBHOOK_URL_BASE,
    SUBSCRIPTION_DURATION_DAYS, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT
)
from db import (
    get_chat_bot_instance, get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner, check_and_update_user_limits, activate_subscription,
    create_bot_instance, link_bot_instance_to_chat, get_bot_instance_by_id, delete_persona_config,
    SessionLocal, get_db, # Импортируем get_db
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

# --- Yookassa Setup ---
try:
    if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
        Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        logger.info("Yookassa configured.")
    else:
        logger.warning("Yookassa credentials not found in environment variables. Payment features will be disabled.")
except Exception as e:
     logger.error(f"Failed to configure Yookassa: {e}", exc_info=True)


# --- Conversation Handler States for Editing Persona ---
# Добавляем состояние для нового поля
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Добавляем новое поле в карту
FIELD_MAP = {
    "name": "имя",
    "description": "описание",
    "system_prompt_template": "системный промпт",
    "should_respond_prompt_template": "промпт 'отвечать?'",
    "spam_prompt_template": "промпт спама",
    "photo_prompt_template": "промпт фото",
    "voice_prompt_template": "промпт голоса",
    "max_response_messages": "макс. сообщений в ответе" # Новое поле
}

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Уведомим пользователя об ошибке, если это возможно
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("упс... что-то пошло не так. попробуй еще раз позже.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")


def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    """Обновленная функция для получения персоны, контекста и владельца с eager loading."""
    chat_instance = db.query(ChatBotInstance)\
        .options(
            # Загружаем BotInstance и связанные с ним PersonaConfig и User
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.persona_config),
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.owner)
        )\
        .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
        .first()

    if not chat_instance or not chat_instance.active:
        # logger.debug(f"No active chat_bot_instance found for chat_id {chat_id}")
        return None

    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance or not bot_instance.persona_config or not bot_instance.owner:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance, PersonaConfig or Owner.")
         return None

    persona_config = bot_instance.persona_config
    owner_user = bot_instance.owner

    persona = Persona(persona_config, chat_instance)
    context_list = get_context_for_chat_bot(db, chat_instance.id) # Использует новый лимит

    # logger.debug(f"Found active persona '{persona.name}' for chat {chat_id} owned by {owner_user.id} with {len(context_list)} context messages.")
    return persona, context_list, owner_user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set.")
        return "ошибка: ключ api не настроен."

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    # Ограничиваем контекст и здесь на всякий случай
    messages_to_send = messages[-config.MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]
    payload = {
        "model": LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages_to_send,
        "max_tokens": 1024,
        "temperature": 0.75, # Немного повысим для разнообразия
        "top_p": 0.95,
        "stream": False
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    # logger.debug(f"Sending request to Langdock URL: {url} with {len(messages_to_send)} messages in context.")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client: # Уменьшаем таймаут
             # logger.debug(f"Langdock Payload: {payload}") # Раскомментировать для глубокой отладки
             resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if "content" in data and isinstance(data["content"], list):
            full_response = " ".join([part.get("text", "") for part in data["content"] if part.get("type") == "text"])
            # logger.debug(f"Received text from Langdock: {full_response[:200]}...")
            return full_response.strip()

        logger.warning(f"Langdock response format unexpected: {data}")
        return data.get("response", "").strip() # Fallback

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


async def process_and_send_response(update: Optional[Update], # Update может быть None для спама
                                    context: ContextTypes.DEFAULT_TYPE,
                                    chat_id: str,
                                    persona: Persona,
                                    full_bot_response_text: str,
                                    db: Session):

    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}")

    # Сохраняем полный ответ AI в контекст
    # Проверяем наличие chat_instance перед доступом к id
    if persona.chat_instance:
        add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
        logger.debug("AI response added to database context.")
    else:
        logger.warning("Cannot add AI response to context, chat_instance is None.")


    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    # Удаляем ссылки на гифки из текста
    for gif in gif_links:
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()
    # logger.debug(f"Extracted {len(gif_links)} gif links. Remaining text: {all_text_content[:100]}...")

    # Разбиваем оставшийся текст на части
    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    # --- Применяем ограничение на количество сообщений ---
    max_messages = persona.config.max_response_messages
    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        # Можно добавить "..." к последнему сообщению, если обрезали
        if text_parts_to_send:
             text_parts_to_send[-1] += "..."


    # Отправляем гифки первыми
    for gif in gif_links:
        try:
            # logger.debug(f"Attempting to send gif: {gif}")
            await context.bot.send_animation(chat_id=chat_id, animation=gif)
            logger.info(f"Sent gif: {gif}")
            await asyncio.sleep(random.uniform(0.5, 1.5)) # Уменьшаем паузу
        except Exception as e:
            logger.error(f"Error sending gif {gif}: {e}", exc_info=True)
            # Можно добавить запасные варианты отправки (фото, документ), но это замедлит

    # Отправляем текстовые части с УМЕНЬШЕННОЙ задержкой
    if text_parts_to_send:
        chat_type = None
        if update and update.effective_chat:
             chat_type = update.effective_chat.type

        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: # Пропускаем пустые части
                 continue

            if chat_type in ["group", "supergroup"]:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    # Упрощенная и уменьшенная пауза
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                except Exception as e:
                     logger.warning(f"Failed to send typing action to {chat_id}: {e}")

            try:
                 # logger.debug(f"Sending text part {i+1}/{len(text_parts_to_send)}: {part[:50]}...")
                 await context.bot.send_message(chat_id=chat_id, text=part)
                 # logger.info(f"Sent text part: {part[:100]}...")
            except Exception as e:
                 logger.error(f"Error sending text part to {chat_id}: {e}", exc_info=True)

            # Пауза между текстовыми сообщениями
            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(0.4, 0.9)) # Уменьшаем паузу


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
        # Отвечаем на сообщение пользователя, если возможно
        if update.effective_message:
             await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        # Если нет сообщения (например, в callback), отправляем новое
        elif update.effective_chat:
             await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text: return # Игнорируем пустые сообщения
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    message_text = update.message.text

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")

    # Используем context manager для сессии
    with next(get_db()) as db: # Получаем сессию из генератора
        try:
            # Получаем персону, контекст и ВЛАДЕЛЬЦА персоны
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)

            if not persona_context_owner_tuple:
                # logger.debug(f"No active persona for chat {chat_id}. Ignoring.")
                return

            persona, current_context_list, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id}")

            # --- Проверка лимита СООБЩЕНИЙ ВЛАДЕЛЬЦА ПЕРСОНЫ ---
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}). Not responding.")
                # Отправляем уведомление только если сообщение было адресовано боту (упоминание или ответ)
                # или если это личный чат с ботом (хотя в личке персона не должна быть активна?)
                # Для простоты пока не отправляем уведомление здесь, оно отправится при команде
                return
            # --- Лимит пройден ---

            # Проверка на смену настроения текстом
            available_moods = persona.get_all_mood_names()
            if message_text.lower() in available_moods:
                 logger.info(f"Message '{message_text}' matched mood name. Changing mood.")
                 # Передаем текущую сессию db и persona
                 await mood(update, context, db=db, persona=persona)
                 return # Обработка завершена

            # --- Логика решения, отвечать ли в группе ---
            should_ai_respond = True # По умолчанию отвечаем в личке или если нет шаблона
            if update.effective_chat.type in ["group", "supergroup"]:
                 if persona.should_respond_prompt_template:
                     # Опция: проверка подписки владельца для этой фичи
                     # if not owner_user.is_active_subscriber:
                     #      logger.debug("Owner not subscribed, skipping should_respond check.")
                     # else:
                     should_respond_prompt = persona.format_should_respond_prompt(message_text)
                     if should_respond_prompt:
                         try:
                             logger.debug("Checking should_respond...")
                             decision_response = await send_to_langdock(
                                 system_prompt=should_respond_prompt,
                                 messages=[{"role": "user", "content": f"сообщение в чате: {message_text}"}]
                             )
                             answer = decision_response.strip().lower()
                             logger.debug(f"should_respond AI decision: '{answer}'")

                             if answer.startswith("д"):
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond based on AI.")
                                 should_ai_respond = True
                             elif random.random() < 0.05: # Очень малый шанс ответить вопреки AI
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond randomly despite AI='{answer}'.")
                                 should_ai_respond = True
                             else:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding NOT to respond based on AI.")
                                 should_ai_respond = False

                         except Exception as e:
                              logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                              logger.warning("Error in should_respond. Defaulting to respond.")
                              should_ai_respond = True # Отвечаем в случае ошибки
                     else:
                          logger.debug("No should_respond_prompt generated. Defaulting to respond in group.")
                          should_ai_respond = True
                 else:
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            # Если проверка сказала не отвечать, выходим
            if not should_ai_respond:
                 return

            # --- Добавляем сообщение пользователя в контекст ---
            # Проверяем наличие chat_instance перед доступом к id
            if persona.chat_instance:
                add_message_to_context(db, persona.chat_instance.id, "user", message_text)
                # Перечитываем контекст ПОСЛЕ добавления (он уже обрезан функцией get_context)
                context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")
            else:
                 logger.error("Cannot add user message to context or get context, chat_instance is None.")
                 context_for_ai = [{"role": "user", "content": message_text}] # Минимальный контекст


            # --- Генерация и отправка ответа ---
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            logger.debug("Formatted main system prompt.")

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message: {e}", exc_info=True)
             db.rollback() # Откатываем транзакцию БД
             await update.message.reply_text("ошибка базы данных, попробуйте позже.")
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            # Не отправляем сообщение об ошибке здесь, чтобы не спамить, оно отправится из error_handler


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"

    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple:
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            # --- Проверка лимита ВЛАДЕЛЬЦА ---
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding.")
                # await send_limit_exceeded_message(update, context, owner_user) # Можно раскомментировать
                return
            # --- Лимит пройден ---

            prompt_template = None
            context_text = ""
            system_formatter = None
            if media_type == "photo":
                prompt_template = persona.photo_prompt_template
                context_text = "прислали фотографию."
                system_formatter = persona.format_photo_prompt
            elif media_type == "voice":
                prompt_template = persona.voice_prompt_template
                context_text = "прислали голосовое сообщение."
                system_formatter = persona.format_voice_prompt

            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id} has no {media_type} template. Skipping.")
                return

            # --- Добавляем плейсхолдер в контекст ---
            if persona.chat_instance:
                add_message_to_context(db, persona.chat_instance.id, "user", context_text)
                context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")
            else:
                 logger.error("Cannot add media placeholder to context or get context, chat_instance is None.")
                 context_for_ai = [{"role": "user", "content": context_text}]


            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 return

            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             db.rollback()
             if update.effective_message: await update.effective_message.reply_text("ошибка базы данных.")
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await handle_media(update, context, "photo")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await handle_media(update, context, "voice")


# --- Команды ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start с улучшенным логированием и обработкой ошибок."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id}")

    # Отправляем Typing Action сразу для индикации работы
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            # Проверяем активную персону в текущем чате
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)

            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                reply_text = (
                    f"привет! я {persona.name}. я уже активен в этом чате.\n"
                    "используй /help для списка команд."
                )
                await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            else:
                # Если персоны нет, показываем приветствие и статус пользователя
                # Перезагружаем пользователя на случай изменений в БД
                db.refresh(user)
                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber else ""

                # Считаем персон явно через запрос к БД для точности
                persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()

                reply_text = (
                    f"привет! 👋 я бот для создания ai-собеседников ({context.bot.username}).\n\n"
                    f"твой статус: **{status}**{expires_text}\n"
                    f"личности: {persona_count}/{user.persona_limit} | "
                    f"сообщения: {user.daily_message_count}/{user.message_limit}\n\n"
                    "**начало работы:**\n"
                    "1. `/createpersona <имя>` - создай ai-личность.\n"
                    "2. `/mypersonas` - посмотри своих личностей.\n"
                    "3. `/addbot <id>` - добавь личность в чат.\n\n"
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
        "/createpersona <имя> [описание] - создать\n"
        "/mypersonas - список твоих личностей и их ID\n"
        "/editpersona <id> - изменить личность (имя, промпты, настройки)\n"
        "/deletepersona <id> - удалить личность (!)\n"
        "/addbot <id> - активировать личность в чате\n\n"
        "**💬 управление в чате (где есть личность):**\n"
        "/mood - сменить настроение активной личности\n"
        "/reset - очистить память (контекст) личности"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    # Логика команды /mood остается прежней, но убедимся, что db передается правильно
    # и используется get_persona_and_context_with_owner если db не передан
    # ... (код mood из предыдущей версии с небольшими правками для передачи db) ...
    # Важно: Если db передается, НЕ закрывать его в конце. Если создается новый, то закрывать.

    is_callback = update.callback_query is not None
    message = update.message if not is_callback else update.callback_query.message
    if not message: return

    chat_id = str(message.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id}")

    # Определяем, нужно ли закрывать сессию в конце
    close_db_later = False
    if db is None: # Если сессия не передана (вызов через /mood)
        db_context = get_db() # Получаем контекстный менеджер
        db = next(db_context) # Получаем сессию
        close_db_later = True
        persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
        if not persona_context_owner_tuple:
            reply_text = "в этом чате нет активной личности."
            if is_callback: await update.callback_query.edit_message_text(reply_text)
            else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            logger.debug(f"No active persona for chat {chat_id}. Cannot set mood.")
            if close_db_later: db.close()
            return
        persona, _, _ = persona_context_owner_tuple
        chat_bot_instance = persona.chat_instance
    elif persona is not None: # Если сессия и персона переданы (из handle_message)
        chat_bot_instance = persona.chat_instance
        if not chat_bot_instance: # Доп. проверка
            logger.error("Mood called from handle_message, but persona.chat_instance is None.")
            if close_db_later: db.close()
            return
    else: # Неожиданная ситуация
         logger.error("Mood called with db but without persona.")
         if close_db_later: db.close()
         return

    try:
        available_moods = persona.get_all_mood_names()
        if not available_moods:
             reply_text = f"у личности '{persona.name}' не настроены настроения."
             if is_callback: await update.callback_query.edit_message_text(reply_text)
             else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             logger.warning(f"Persona {persona.name} has no moods defined.")
             return

        mood_arg = None
        # Определение аргумента настроения
        if not is_callback and context.args: # /mood <arg>
             mood_arg = context.args[0].lower()
        elif not is_callback and message.text and message.text.lower() in available_moods: # text command
             mood_arg = message.text.lower()
        elif is_callback and update.callback_query.data.startswith("set_mood_"): # Callback button
             parts = update.callback_query.data.split('_')
             # Ожидаем set_mood_<moodname>_<persona_id>
             if len(parts) >= 3:
                  mood_arg = parts[2].lower()
                  # Дополнительно можно проверить persona_id, если нужно
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")


        # Обработка аргумента или показ кнопок
        if mood_arg:
             if mood_arg in available_moods:
                 set_mood_for_chat_bot(db, chat_bot_instance.id, mood_arg)
                 reply_text = f"настроение для '{persona.name}' теперь: **{mood_arg}**"
                 if is_callback:
                     await update.callback_query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
                 else:
                     await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
                 logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {mood_arg}.")
             else:
                 # Неверный аргумент - показываем кнопки
                 keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{persona.id}")] for m in available_moods]
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 reply_text = f"не знаю настроения '{mood_arg}' для '{persona.name}'. выбери из списка:"
                 if is_callback:
                      await update.callback_query.edit_message_text(reply_text, reply_markup=reply_markup)
                 else:
                      await message.reply_text(reply_text, reply_markup=reply_markup)
                 logger.debug(f"Invalid mood argument '{mood_arg}' for chat {chat_id}. Sent mood selection.")
        else:
             # Нет аргумента - показываем кнопки
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{persona.id}")] for m in available_moods]
             reply_markup = InlineKeyboardMarkup(keyboard)
             reply_text = f"выбери настроение для '{persona.name}':"
             if is_callback:
                  await update.callback_query.edit_message_text(reply_text, reply_markup=reply_markup)
             else:
                  await message.reply_text(reply_text, reply_markup=reply_markup)
             logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id}: {e}", exc_info=True)
         if db and not db.is_active: db.rollback()
         reply_text = "ошибка базы данных при смене настроения."
         if is_callback: await update.callback_query.edit_message_text(reply_text)
         else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id}: {e}", exc_info=True)
         reply_text = "ошибка при обработке команды /mood."
         if is_callback: await update.callback_query.edit_message_text(reply_text)
         else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
    finally:
        # Закрываем сессию, только если она была создана в этой функции
        if close_db_later and db:
            db.close()


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
            persona, _, _ = persona_info_tuple
            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                 await update.message.reply_text("ошибка: не найден экземпляр бота для сброса.")
                 return

            # Удаляем контекст
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}.")
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id}: {e}", exc_info=True)
            db.rollback()
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
    persona_description = " ".join(args[1:]) if len(args) > 1 else f"ai бот по имени {persona_name}."

    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text("имя личности: 2-50 символов.", reply_markup=ReplyKeyboardRemove())
         return
    if len(persona_description) > 1000:
         await update.message.reply_text("описание: до 1000 символов.", reply_markup=ReplyKeyboardRemove())
         return

    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            # Перезагружаем user, чтобы точно получить актуальное состояние подписки и лимитов
            db.refresh(user, ['persona_configs'])

            if not user.can_create_persona:
                 persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({persona_count}/{user.persona_limit}).")
                 text = (
                     f"упс! достигнут лимит личностей ({persona_count}/{user.persona_limit}) для статуса **{'Premium' if user.is_active_subscriber else 'Free'}**. 😟\n"
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
                f"добавь в чат: /addbot `{new_persona.id}`\n"
                f"настрой детальнее: /editpersona `{new_persona.id}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError: # Обработка возможной гонки потоков или ошибки логики
             db.rollback()
             logger.error(f"IntegrityError creating persona for user {user_id} with name '{persona_name}'.", exc_info=True)
             await update.message.reply_text(f"ошибка: личность '{persona_name}' уже существует (возможно, создана только что).", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             db.rollback()
             logger.error(f"Database error during /createpersona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка базы данных при создании личности.")
        except Exception as e:
             db.rollback()
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка при создании личности.")


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /mypersonas с улучшенным логированием и обработкой ошибок."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, username)
            # Явно загружаем персоны, чтобы избежать ленивой загрузки вне сессии
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()
            personas = user_with_personas.persona_configs if user_with_personas else []
            persona_limit = user_with_personas.persona_limit if user_with_personas else FREE_PERSONA_LIMIT

            if not personas:
                await update.message.reply_text(
                    "у тебя пока нет личностей.\n"
                    "создай: /createpersona <имя>",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
                )
                return

            response_text = f"твои личности ({len(personas)}/{persona_limit}):\n\n"
            for persona in personas:
                response_text += f"🔹 **{persona.name}** (ID: `{persona.id}`)\n"
                # Описание убрано для краткости
                response_text += f"   /editpersona `{persona.id}` | /addbot `{persona.id}`\n" # | /deletepersona `{persona.id}` - можно добавить
                response_text += "---\n"

            await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
            logger.info(f"User {user_id} requested mypersonas. Sent {len(personas)} personas.")

    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        # db.rollback() # Не нужно, так как был только SELECT
        await update.message.reply_text("ошибка при загрузке списка личностей.")
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("произошла ошибка при обработке команды /mypersonas.")


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    chat_title = update.effective_chat.title or chat_id
    logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id}) with args: {context.args}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text(
            "формат: `/addbot <id персоны>`\n"
            "id можно найти в /mypersonas",
            parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
        )
        return

    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text("id личности должен быть числом.", reply_markup=ReplyKeyboardRemove())
        return

    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            persona = get_persona_by_id_and_owner(db, user.id, persona_id)
            if not persona:
                 await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
                 return

            # --- Логика деактивации старого бота ---
            # Ищем ЛЮБОГО активного бота в ЭТОМ чате (неважно, чей он)
            existing_active_link = db.query(ChatBotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).options(joinedload(ChatBotInstance.bot_instance_ref)).first() # Загружаем связь для логгирования

            if existing_active_link:
                old_bot_instance_id = existing_active_link.bot_instance_id
                # Если этот активный бот НЕ тот, которого мы пытаемся добавить
                if old_bot_instance_id != persona.bot_instances[0].id if persona.bot_instances else -1: # Проверка, если у персоны уже есть инстанс
                    existing_active_link.active = False
                    # db.commit() # Коммит будет ниже общим
                    logger.info(f"Deactivated previous bot instance {old_bot_instance_id} in chat {chat_id} before activating {persona_id}.")
                else:
                    # Если активен тот же самый бот, которого добавляем
                    await update.message.reply_text(f"личность '{persona.name}' уже активна в этом чате.", reply_markup=ReplyKeyboardRemove())
                    return

            # --- Создание/поиск инстанса бота ---
            # Ищем или создаем BotInstance для этой ПЕРСОНЫ (независимо от чата)
            bot_instance = db.query(BotInstance).filter(
                # BotInstance.owner_id == user.id, # Инстанс привязан к персоне, не к пользователю напрямую в этой логике
                BotInstance.persona_config_id == persona.id
            ).first()

            if not bot_instance:
                 bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Inst:{persona.name}")
                 logger.info(f"Created BotInstance {bot_instance.id} for persona {persona.id}")
                 # db.commit() # Коммит будет ниже общим

            # --- Связывание с чатом ---
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            if chat_link:
                 # Очищаем контекст при активации
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 db.commit() # Коммитим все изменения (деактивацию, создание инстанса, линк, удаление контекста)
                 logger.debug(f"Cleared {deleted_ctx} context messages for chat_bot_instance {chat_link.id} upon linking.")

                 await update.message.reply_text(
                     f"✅ личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате!",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
                 )
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 # link_bot_instance_to_chat может вернуть None, если что-то пошло не так
                 db.rollback() # Откатываем все, если связь не удалась
                 await update.message.reply_text("не удалось активировать личность.", reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id}.")


        except IntegrityError:
             db.rollback()
             logger.warning(f"IntegrityError potentially during addbot for persona {persona_id} to chat {chat_id}.", exc_info=True)
             await update.message.reply_text("произошла ошибка целостности данных, попробуйте еще раз.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             db.rollback()
             logger.error(f"Database error during /addbot for persona {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка базы данных при добавлении бота.")
        except Exception as e:
             db.rollback()
             logger.error(f"Error adding bot instance {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка при активации личности.")


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Этот обработчик теперь ТОЛЬКО для кнопок, не связанных с ConversationHandler
    # (т.е. кнопки подписки и кнопки смены настроения из /mood)
    query = update.callback_query
    if not query or not query.data: return
    await query.answer() # Отвечаем на коллбэк сразу

    chat_id = str(query.message.chat.id)
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data

    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    # --- Mood Setting Callback ---
    if data.startswith("set_mood_"):
        await mood(update, context) # Передаем управление функции mood

    # --- Subscription Callbacks ---
    elif data == "subscribe_info":
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await generate_payment_link(update, context)

    # --- Другие возможные коллбэки (если появятся) ---
    # elif data == "some_other_action":
    #     pass

    # НЕ обрабатываем здесь коллбэки от ConversationHandler (editpersona_, editmood_, delete_persona_)
    # Они должны обрабатываться самим ConversationHandler.


# --- Subscription Commands ---

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

            # Сбрасываем счетчик, если нужно
            if not user.last_message_reset or user.last_message_reset.date() < now.date():
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit()
                db.refresh(user) # Обновляем user после коммита

            # Статус и срок действия
            is_active_subscriber = user.is_active_subscriber
            status = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            expires_text = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active_subscriber else "нет активной подписки"

            # Считаем персоны
            persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()

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
             await update.message.reply_text("ошибка базы данных при загрузке профиля.")
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text("ошибка при обработке команды /profile.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    # Проверка, настроена ли Юкасса
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        text = "К сожалению, функция оплаты сейчас недоступна. 😥"
        reply_markup = None
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
        keyboard = [[InlineKeyboardButton(f"💳 оплатить {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    message_to_edit = update.callback_query.message if from_callback else update.message
    if not message_to_edit: return # Не должно происходить

    try:
        if from_callback:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        else:
            await message_to_edit.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"Generating payment link for user {user_id}")

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("Yookassa credentials not set in config.")
        await query.edit_message_text("ошибка: сервис оплаты временно недоступен.", reply_markup=None)
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium подписка {context.bot.username} на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': user_id} # Используем telegram_user_id для ясности

    # Возврат просто в бота после оплаты
    return_url = f"https://t.me/{context.bot.username}?start=payment_success"

    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata)
        # Добавить чек, если нужно соответствовать законам
        # builder.set_receipt(...)

        request = builder.build()
        # Запускаем синхронный запрос к API Юкассы в потоке, чтобы не блокировать asyncio
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)
        # payment_response = Payment.create(request, idempotence_key) # Синхронный вариант

        confirmation_url = payment_response.confirmation.confirmation_url
        payment_id = payment_response.id
        context.user_data['pending_payment_id'] = payment_id

        logger.info(f"Created Yookassa payment {payment_id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ ссылка для оплаты создана!\n\n"
            "нажми кнопку ниже для перехода к оплате. после успеха подписка активируется (может занять пару минут).",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Yookassa payment creation failed for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ не удалось создать ссылку для оплаты. попробуй позже или свяжись с поддержкой.", reply_markup=None)


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ЭТО ЗАГЛУШКА! Реальный вебхук должен быть отдельным веб-приложением (Flask, FastAPI)
    # Логика внутри (парсинг, проверка статуса, активация подписки) должна быть там.
    logger.warning("Placeholder Yookassa webhook endpoint called. This should be handled by a separate web application.")
    # Пример логики, которая ДОЛЖНА БЫТЬ В ВЕБХУКЕ:
    # data = request.json()
    # notification = WebhookNotification(data)
    # payment = notification.object
    # if payment.status == 'succeeded':
    #     telegram_id = payment.metadata.get('telegram_user_id')
    #     if telegram_id:
    #         with next(get_db()) as db:
    #             user = db.query(User).filter(User.telegram_id == telegram_id).first()
    #             if user and activate_subscription(db, user.id):
    #                 logger.info(f"Subscription activated for {telegram_id} via webhook.")
    #                 try:
    #                     # Уведомляем пользователя асинхронно через application.bot
    #                     await context.application.bot.send_message(
    #                         chat_id=telegram_id,
    #                         text="🎉 твоя премиум подписка активна! спасибо!"
    #                     )
    #                 except Exception as send_e:
    #                     logger.error(f"Failed to send webhook success message to {telegram_id}: {send_e}")
    pass

# --- Edit Persona Conversation Handler ---

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # --- Начало: Логирование и проверка аргументов ---
    if not update.message: return ConversationHandler.END # Не должно вызываться без сообщения
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nнайди id в /mypersonas", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    persona_id = int(args[0])
    context.user_data['edit_persona_id'] = persona_id
    # --- Конец: Логирование и проверка аргументов ---

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, update.effective_user.username)
            # Загружаем персону с владельцем
            persona_config = get_persona_by_id_and_owner(db, user.id, persona_id)

            if not persona_config:
                await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
                context.user_data.pop('edit_persona_id', None)
                return ConversationHandler.END

            # Сохраняем объект Persona для удобства
            context.user_data['persona_object'] = Persona(persona_config)

            # --- Формирование клавиатуры ---
            keyboard = [
                [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
                [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
                # Добавляем новую кнопку
                [InlineKeyboardButton("📊 Макс. ответов", callback_data="edit_field_max_response_messages")],
                [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
                [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
                [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
                [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # --- Конец: Формирование клавиатуры ---

            await update.message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

        return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error starting edit persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("ошибка базы данных при начале редактирования.")
         context.user_data.pop('edit_persona_id', None)
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting edit persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("непредвиденная ошибка.")
         context.user_data.pop('edit_persona_id', None)
         return ConversationHandler.END

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_PERSONA_CHOICE # Остаемся в том же состоянии
    await query.answer()
    data = query.data
    persona: Optional[Persona] = context.user_data.get('persona_object')
    user_id = query.from_user.id

    if not persona:
         await query.edit_message_text("ошибка: сессия редактирования потеряна. начни снова /editpersona <id>.")
         return ConversationHandler.END

    logger.debug(f"Edit persona choice: {data} for persona {persona.id}")

    if data == "cancel_edit":
        await query.edit_message_text("редактирование отменено.")
        context.user_data.clear() # Очищаем все данные сессии
        return ConversationHandler.END

    if data == "edit_moods":
        # Проверка подписки для редактирования настроений
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id)
            if not user.is_active_subscriber:
                 await query.edit_message_text("управление настроениями доступно только по подписке. /subscribe", reply_markup=None)
                 # Возвращаемся в главное меню редактирования
                 keyboard = await _get_edit_persona_keyboard(persona) # Нужна функция для генерации кнопок
                 await query.message.reply_text(f"редактируем **{persona.name}**\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                 return EDIT_PERSONA_CHOICE
        # Если подписчик, переходим к меню настроений
        return await edit_moods_menu(update, context)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        context.user_data['edit_field'] = field
        field_display_name = FIELD_MAP.get(field, field)

        # Проверка подписки для "продвинутых" полей
        advanced_fields = ["should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields:
             with next(get_db()) as db:
                 user = get_or_create_user(db, user_id)
                 if not user.is_active_subscriber:
                     await query.edit_message_text(f"поле '{field_display_name}' доступно только по подписке. /subscribe", reply_markup=None)
                     keyboard = await _get_edit_persona_keyboard(persona)
                     await query.message.reply_text(f"редактируем **{persona.name}**\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                     return EDIT_PERSONA_CHOICE

        # Запрашиваем новое значение
        if field == "max_response_messages":
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'** (число от 1 до 10):", parse_mode=ParseMode.MARKDOWN)
            return EDIT_MAX_MESSAGES # Переходим в новое состояние
        else:
            # Получаем текущее значение для отображения
            current_value = getattr(persona.config, field, "")
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'**.\nтекущее:\n`{current_value}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")]])) # Добавили кнопку Назад
            return EDIT_FIELD

    # Возврат из подменю
    if data == "edit_persona_back":
         keyboard = await _get_edit_persona_keyboard(persona)
         await query.edit_message_text(f"редактируем **{persona.name}**\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
         return EDIT_PERSONA_CHOICE


    await query.message.reply_text("неизвестный выбор. попробуй еще раз.")
    return EDIT_PERSONA_CHOICE


async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # --- Получение данных из контекста ---
    if not update.message or not update.message.text: return EDIT_FIELD # Ждем текст
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    if not field or not persona_id:
        await update.message.reply_text("ошибка: сессия редактирования потеряна. начни сначала /editpersona <id>.")
        return ConversationHandler.END
    # --- Конец: Получение данных ---

    field_display_name = FIELD_MAP.get(field, field)
    logger.debug(f"Attempting to update field '{field}' for persona {persona_id} with value: {new_value[:50]}...")

    # --- Валидация ---
    if field == "name":
        if not (2 <= len(new_value) <= 50):
             await update.message.reply_text("имя: 2-50 символов. попробуй еще раз:")
             return EDIT_FIELD
    elif field == "description":
         if len(new_value) > 1500: # Увеличим лимит
             await update.message.reply_text("описание: до 1500 символов. попробуй еще раз:")
             return EDIT_FIELD
    elif field.endswith("_prompt_template"):
         if len(new_value) > 3000: # Увеличим лимит
             await update.message.reply_text("промпт: до 3000 символов. попробуй еще раз:")
             return EDIT_FIELD
    # Валидация для max_response_messages происходит в отдельном хендлере
    # --- Конец: Валидация ---

    try:
        with next(get_db()) as db:
            # Получаем конфиг персоны для обновления
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.")
                 context.user_data.clear()
                 return ConversationHandler.END

            # Проверка уникальности имени, если меняем имя
            if field == "name" and new_value.lower() != persona_config.name.lower():
                existing = get_persona_by_name_and_owner(db, user_id, new_value)
                if existing:
                    await update.message.reply_text(f"имя '{new_value}' уже занято другой твоей личностью. попробуй другое:")
                    return EDIT_FIELD

            # Обновляем поле
            setattr(persona_config, field, new_value)
            db.commit()
            db.refresh(persona_config)

            # Обновляем объект Persona в контексте
            context.user_data['persona_object'] = Persona(persona_config)
            logger.info(f"User {user_id} updated field '{field}' for persona {persona_id}.")

            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")

    except SQLAlchemyError as e:
         db.rollback()
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении.")
         # Не завершаем диалог, даем попробовать еще раз или отменить
    except Exception as e:
         db.rollback()
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         # Не завершаем диалог

    # --- Возвращаемся в главное меню редактирования ---
    persona: Optional[Persona] = context.user_data.get('persona_object')
    if persona:
        keyboard = await _get_edit_persona_keyboard(persona)
        await update.message.reply_text(f"что еще изменить для **{persona.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else: # Если вдруг персона пропала из контекста
         await update.message.reply_text("возврат в меню редактирования...") # Общее сообщение
    return EDIT_PERSONA_CHOICE
    # --- Конец: Возвращаемся в главное меню ---


async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # --- Получение данных ---
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages" # Поле известно
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    if not persona_id:
        await update.message.reply_text("ошибка: сессия редактирования потеряна. начни снова /editpersona <id>.")
        return ConversationHandler.END
    # --- Конец: Получение данных ---

    logger.debug(f"Attempting to update max_response_messages for persona {persona_id} with value: {new_value_str}")

    # --- Валидация ---
    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10):
            raise ValueError("Value out of range")
    except ValueError:
        await update.message.reply_text("неверное значение. введи число от 1 до 10:")
        return EDIT_MAX_MESSAGES # Остаемся в этом состоянии для повторного ввода
    # --- Конец: Валидация ---

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.")
                 context.user_data.clear()
                 return ConversationHandler.END

            # Обновляем поле
            persona_config.max_response_messages = new_value
            db.commit()
            db.refresh(persona_config)

            # Обновляем объект Persona в контексте
            context.user_data['persona_object'] = Persona(persona_config)
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            await update.message.reply_text(f"✅ макс. сообщений в ответе для **'{persona_config.name}'** установлено: **{new_value}**")

    except SQLAlchemyError as e:
         db.rollback()
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении.")
    except Exception as e:
         db.rollback()
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")

    # --- Возвращаемся в главное меню редактирования ---
    persona: Optional[Persona] = context.user_data.get('persona_object')
    if persona:
        keyboard = await _get_edit_persona_keyboard(persona)
        await update.message.reply_text(f"что еще изменить для **{persona.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
         await update.message.reply_text("возврат в меню редактирования...")
    return EDIT_PERSONA_CHOICE
    # --- Конец: Возвращаемся в главное меню ---


async def _get_edit_persona_keyboard(persona: Persona) -> List[List[InlineKeyboardButton]]:
    """Вспомогательная функция для генерации клавиатуры редактирования."""
    # Можно добавить логику, чтобы скрывать кнопки продвинутых настроек для не-подписчиков
    keyboard = [
        [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
        [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
        [InlineKeyboardButton(f"📊 Макс. ответов ({persona.config.max_response_messages})", callback_data="edit_field_max_response_messages")], # Показываем текущее значение
        [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
        [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
        [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
        [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")] # Изменили текст кнопки Отмена
    ]
    return keyboard

# --- Функции редактирования настроений (edit_moods_menu, edit_mood_choice, etc.) ---
# Остаются похожими на предыдущую версию, но с улучшенной обработкой ошибок и возвратом в меню

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    # if query: await query.answer() # Не нужно, отвечаем в вызывающей функции
    persona: Optional[Persona] = context.user_data.get('persona_object')

    if not persona:
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    logger.debug(f"Showing moods menu for persona {persona.id}")

    # Обновляем объект персоны из БД на всякий случай
    with next(get_db()) as db:
        persona_config = db.query(PersonaConfig).get(persona.id)
        if persona_config:
            persona = Persona(persona_config)
            context.user_data['persona_object'] = persona # Обновляем в контексте
        else:
            await query.edit_message_text("ошибка: личность не найдена.")
            return ConversationHandler.END

    moods = persona.mood_prompts
    keyboard = []
    if moods:
        # Сортируем настроения для единообразия
        sorted_moods = sorted(moods.keys())
        for mood_name in sorted_moods:
             # Отображаем только часть промпта для краткости
             prompt_preview = moods[mood_name][:30] + "..." if len(moods[mood_name]) > 30 else moods[mood_name]
             keyboard.append([
                 InlineKeyboardButton(f"✏️ {mood_name.capitalize()}", callback_data=f"editmood_select_{mood_name}"),
                 # InlineKeyboardButton(f"`{prompt_preview}`", callback_data=f"editmood_select_{mood_name}"), # Вариант с предпросмотром
                 InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{mood_name}")
             ])
    keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(f"управление настроениями для **{persona.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
         logger.error(f"Error editing moods menu message: {e}")
         # Если сообщение не изменилось, это не критично

    return EDIT_MOOD_CHOICE

async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE
    await query.answer()
    data = query.data
    persona: Optional[Persona] = context.user_data.get('persona_object')

    if not persona:
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    logger.debug(f"Edit mood choice: {data} for persona {persona.id}")

    if data == "edit_persona_back":
        keyboard = await _get_edit_persona_keyboard(persona)
        await query.edit_message_text(f"редактируем **{persona.name}**\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        context.user_data['edit_mood_name'] = None # Флаг добавления
        await query.edit_message_text("введи **название** нового настроения (одно слово, например, 'радость'):", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")]]))
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        mood_name = data.replace("editmood_select_", "")
        context.user_data['edit_mood_name'] = mood_name
        current_prompt = persona.mood_prompts.get(mood_name, "_нет промпта_")
        await query.edit_message_text(
            f"редактирование настроения: **{mood_name}**\n\n"
            f"текущий промпт:\n`{current_prompt}`\n\n"
            f"отправь **новый текст промпта**:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")]])
        )
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         mood_name = data.replace("deletemood_confirm_", "")
         context.user_data['delete_mood_name'] = mood_name
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{mood_name}'", callback_data=f"deletemood_delete_{mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
         ]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await query.edit_message_text(f"точно удалить настроение **'{mood_name}'**?", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
         return DELETE_MOOD_CONFIRM

    # Возврат из других состояний (например, после удаления или ошибки)
    if data == "edit_moods_back_cancel":
         return await edit_moods_menu(update, context)


    await query.message.reply_text("неизвестный выбор настроения.")
    return await edit_moods_menu(update, context) # Возврат в меню настроений


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name = update.message.text.strip().lower()
    persona: Optional[Persona] = context.user_data.get('persona_object')

    if not persona:
        await update.message.reply_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    # Валидация имени
    if not mood_name or len(mood_name) > 30 or not re.match(r'^[a-zа-яё0-9_-]+$', mood_name):
        await update.message.reply_text("название: 1-30 символов, только буквы/цифры/дефис/подчеркивание. попробуй еще:")
        return EDIT_MOOD_NAME
    if mood_name in persona.get_all_mood_names():
        await update.message.reply_text(f"настроение '{mood_name}' уже существует. выбери другое:")
        return EDIT_MOOD_NAME

    context.user_data['edit_mood_name'] = mood_name
    await update.message.reply_text(f"отлично! теперь отправь **текст промпта** для настроения **'{mood_name}'**:", parse_mode=ParseMode.MARKDOWN)
    return EDIT_MOOD_PROMPT


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona: Optional[Persona] = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    if not mood_name or not persona or not persona_id:
        await update.message.reply_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END
    if not mood_prompt or len(mood_prompt) > 1500: # Увеличим лимит
        await update.message.reply_text("промпт настроения: 1-1500 символов. попробуй еще:")
        return EDIT_MOOD_PROMPT

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                await update.message.reply_text("ошибка: личность не найдена или нет доступа.")
                return ConversationHandler.END

            # Обновляем JSON поле
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            current_moods[mood_name] = mood_prompt
            persona_config.mood_prompts_json = json.dumps(current_moods)
            flag_modified(persona_config, "mood_prompts_json") # Помечаем поле как измененное
            db.commit()
            db.refresh(persona_config)

            context.user_data['persona_object'] = Persona(persona_config) # Обновляем в контексте
            logger.info(f"User {user_id} updated mood '{mood_name}' for persona {persona_id}.")
            await update.message.reply_text(f"✅ настроение **'{mood_name}'** сохранено!")

    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка базы данных при сохранении настроения.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка при сохранении настроения.")

    return await edit_moods_menu(update, context) # Возврат в меню настроений


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE
    await query.answer()
    data = query.data
    mood_name = context.user_data.get('delete_mood_name')
    persona: Optional[Persona] = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    # Проверка данных из контекста
    if not mood_name or not persona or not persona_id or not data.endswith(mood_name):
        await query.edit_message_text("ошибка: неверные данные для удаления.")
        return await edit_moods_menu(update, context)

    logger.info(f"User {user_id} confirmed deletion of mood '{mood_name}' for persona {persona_id}.")

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.")
                return ConversationHandler.END

            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            if mood_name in current_moods:
                del current_moods[mood_name]
                persona_config.mood_prompts_json = json.dumps(current_moods)
                flag_modified(persona_config, "mood_prompts_json")
                db.commit()
                db.refresh(persona_config)

                context.user_data['persona_object'] = Persona(persona_config) # Обновляем в контексте
                logger.info(f"Successfully deleted mood '{mood_name}' for persona {persona_id}.")
                await query.edit_message_text(f"🗑️ настроение **'{mood_name}'** удалено.", parse_mode=ParseMode.MARKDOWN)
            else:
                logger.warning(f"Mood '{mood_name}' not found for deletion in persona {persona_id}.")
                await query.edit_message_text(f"настроение '{mood_name}' не найдено (уже удалено?).")

    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Database error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка базы данных при удалении настроения.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при удалении настроения.")


    # Возвращаемся в меню настроений после действия
    return await edit_moods_menu(update, context)


async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Отмена всей операции редактирования
    message = update.effective_message
    if not message: return ConversationHandler.END

    logger.info(f"User {update.effective_user.id} cancelled persona edit.")
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("редактирование отменено.")
    else:
        await message.reply_text("редактирование отменено.")
    context.user_data.clear() # Очищаем все данные редактирования
    return ConversationHandler.END

# --- Delete Persona ---
# Логика удаления остается прежней, добавим try-except

async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/deletepersona <id>`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    persona_id = int(args[0])
    context.user_data['delete_persona_id'] = persona_id

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)

            if not persona_config:
                await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
                return ConversationHandler.END

            keyboard = [
                 [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_config.name}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                 [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
             ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"🚨 **ВНИМАНИЕ!** 🚨\n"
                f"удалить личность **'{persona_config.name}'** (id: `{persona_id}`)?\n\n"
                f"это действие **НЕОБРАТИМО**!",
                reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
            )
        return DELETE_PERSONA_CONFIRM

    except SQLAlchemyError as e:
         logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("ошибка базы данных.")
         return ConversationHandler.END
    except Exception as e:
         logger.error(f"Unexpected error starting delete persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("непредвиденная ошибка.")
         return ConversationHandler.END


async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    if not persona_id or not data.endswith(str(persona_id)):
         await query.edit_message_text("ошибка: неверные данные для удаления.")
         context.user_data.clear()
         return ConversationHandler.END

    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")

    try:
        with next(get_db()) as db:
             # Получаем имя перед удалением для сообщения
             persona_to_delete = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_to_delete:
                 persona_name = persona_to_delete.name
                 if delete_persona_config(db, persona_id, user_id): # Используем функцию с проверкой владельца
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name}').")
                     await query.edit_message_text(f"✅ личность '{persona_name}' (id: {persona_id}) удалена.")
                 else:
                     # Этого не должно произойти, если get_persona_by_id_and_owner сработал, но на всякий случай
                     logger.error(f"Failed to delete persona {persona_id} for user {user_id} despite checks.")
                     await query.edit_message_text("❌ не удалось удалить личность.")
             else:
                 await query.edit_message_text(f"❌ личность с id `{persona_id}` уже удалена или не найдена.", parse_mode=ParseMode.MARKDOWN)

    except SQLAlchemyError as e:
        # Не откатываем, так как delete_persona_config уже сделала commit или была ошибка до него
        logger.error(f"Database error confirming delete persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка базы данных при удалении.")
    except Exception as e:
        logger.error(f"Unexpected error confirming delete persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ непредвиденная ошибка при удалении.")


    context.user_data.clear()
    return ConversationHandler.END


async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("удаление отменено.")
    context.user_data.clear()
    return ConversationHandler.END
