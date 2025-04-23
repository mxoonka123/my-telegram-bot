import logging
import httpx
import random
import asyncio
import re
import uuid
import json
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, contains_eager
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func
from typing import List, Dict, Any, Optional, Union, Tuple

from yookassa import Configuration, Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem
# Убрали импорт специфичных ошибок Yookassa


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
        logger.warning(f"Langdock response format unexpected: {data}")
        # --- Fallback if content is not a list (adjust based on actual API response) ---
        # Check if content is a dictionary with a 'text' field
        if isinstance(data.get("content"), dict) and "text" in data["content"]:
            logger.debug("Langdock response content is a dict, extracting text.")
            return data["content"]["text"].strip()
        # Check if response field exists directly
        elif "response" in data and isinstance(data["response"], str):
             logger.debug("Langdock response format has 'response' field.")
             return data.get("response", "").strip()
        else: # Default fallback if structure is unknown
             logger.warning(f"Could not extract text from Langdock response: {data}")
             return "" # Return empty string if cannot parse known structures
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
        try: # Wrap DB operation
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            db.flush() # Flush to ensure context is ready for potential immediate reuse
            logger.debug("AI response added to database context.")
        except SQLAlchemyError as e:
            logger.error(f"DB Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            db.rollback() # Rollback on error
        except Exception as e:
            logger.error(f"Unexpected Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            # Don't rollback here unless it's a DB error
    else:
        logger.warning("Cannot add AI response to context, chat_instance is None.")

    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)
    for gif in gif_links:
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()
    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    # Use persona.config.max_response_messages which should be loaded
    max_messages = persona.config.max_response_messages if persona.config else 3 # Default to 3 if config somehow missing
    if len(text_parts_to_send) > max_messages:
        logger.info(f"Limiting response parts from {len(text_parts_to_send)} to {max_messages} for persona {persona.name}")
        text_parts_to_send = text_parts_to_send[:max_messages]
        if text_parts_to_send:
             text_parts_to_send[-1] += "..."

    # --- Send GIFs ---
    for gif in gif_links:
        try:
            await context.bot.send_animation(chat_id=chat_id, animation=gif)
            logger.info(f"Sent gif: {gif}")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logger.error(f"Error sending gif {gif} to chat {chat_id}: {e}", exc_info=True)

    # --- Send Text Parts ---
    if text_parts_to_send:
        chat_type = None
        if update and update.effective_chat:
             chat_type = update.effective_chat.type
        for i, part in enumerate(text_parts_to_send):
            part = part.strip()
            if not part: continue
            # Typing action only in groups
            if chat_type in ["group", "supergroup"]:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    # Slightly longer delay to simulate typing better
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception as e:
                     logger.warning(f"Failed to send typing action to {chat_id}: {e}")
            # Send the message part
            try:
                 await context.bot.send_message(chat_id=chat_id, text=part)
            except Exception as e:
                 logger.error(f"Error sending text part to {chat_id}: {e}", exc_info=True)
                 # Optional: break loop if sending fails? Or just log and continue? Continuing for now.

            # Pause between parts
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
    username = update.effective_user.username or f"id_{user_id}"
    message_text = update.message.text
    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")
    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona for chat {chat_id}. Ignoring.")
                return
            persona, current_context_list, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling message for persona '{persona.name}' owned by {owner_user.id}")
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}). Not responding.")
                # Don't send limit message here, let the owner know via /profile or failed commands
                return
            available_moods = persona.get_all_mood_names()
            if message_text.lower() in map(str.lower, available_moods): # Case-insensitive mood check
                 logger.info(f"Message '{message_text}' matched mood name. Changing mood.")
                 # Need to pass the retrieved persona and db session to mood handler
                 await mood(update, context, db=db, persona=persona)
                 # Mood change handled, exit handle_message
                 return
            should_ai_respond = True
            # Only check 'should_respond' in group chats
            if update.effective_chat.type in ["group", "supergroup"]:
                 if persona.should_respond_prompt_template:
                     should_respond_prompt = persona.format_should_respond_prompt(message_text)
                     if should_respond_prompt:
                         try:
                             logger.debug(f"Checking should_respond for persona {persona.name} in chat {chat_id}...")
                             decision_response = await send_to_langdock(
                                 system_prompt=should_respond_prompt,
                                 messages=[{"role": "user", "content": f"сообщение в чате: {message_text}"}]
                             )
                             answer = decision_response.strip().lower()
                             logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")
                             if answer.startswith("д"): # More robust check for "да"
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
                              should_ai_respond = True
                     else:
                          logger.debug(f"No should_respond_prompt generated for persona {persona.name}. Defaulting to respond in group.")
                          should_ai_respond = True
                 else:
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic for message: {message_text[:50]}...")
                 # Add user message to context even if bot doesn't respond, so it's aware of the conversation flow
                 if persona.chat_instance:
                     try:
                        add_message_to_context(db, persona.chat_instance.id, "user", message_text)
                        db.flush() # Commit later if needed, or rely on outer commit
                        logger.debug("Added user message to context even though bot is not responding.")
                     except SQLAlchemyError as e_ctx:
                        logger.error(f"DB Error adding non-responding user message to context: {e_ctx}", exc_info=True)
                        db.rollback()
                 return # Don't proceed to generate response

            # Add user message to context if responding
            context_for_ai = []
            if persona.chat_instance:
                try:
                    add_message_to_context(db, persona.chat_instance.id, "user", message_text)
                    db.flush() # Ensure message is added before fetching context
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context after adding user msg.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error adding user message/getting context: {e_ctx}", exc_info=True)
                     db.rollback()
                     await update.message.reply_text("ошибка при обработке контекста.")
                     return
            else:
                 logger.error("Cannot add user message to context or get context, chat_instance is None.")
                 # Fallback: use only the current message
                 context_for_ai = [{"role": "user", "content": message_text}]

            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            logger.debug("Formatted main system prompt.")

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            # Process and send response (also adds assistant message to context)
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            # Commit all changes for this message cycle (limit update, context adds)
            db.commit()
            logger.debug(f"Committed DB changes for handle_message cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message: {e}", exc_info=True)
             if db.is_active: db.rollback()
             await update.message.reply_text("ошибка базы данных, попробуйте позже.")
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback() # Rollback on general errors too
            # Avoid sending generic error message here, error_handler will do it

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")
    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple: return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")
            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding.")
                return
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
            context_for_ai = []
            if persona.chat_instance:
                try:
                    add_message_to_context(db, persona.chat_instance.id, "user", context_text)
                    db.flush()
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error adding media placeholder/getting context: {e_ctx}", exc_info=True)
                     db.rollback()
                     if update.effective_message: await update.effective_message.reply_text("ошибка при обработке контекста медиа.")
                     return
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
            # Commit changes (limit update, context adds)
            db.commit()
            logger.debug(f"Committed DB changes for handle_media cycle chat {chat_id}")
        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if db.is_active: db.rollback()
             if update.effective_message: await update.effective_message.reply_text("ошибка базы данных.")
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)
            if db.is_active: db.rollback()
            # error_handler will send a message

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
                # Refresh user data after potential creation/check
                db.refresh(user)
                # Ensure limits are up-to-date before display
                now = datetime.now(timezone.utc)
                if not user.last_message_reset or user.last_message_reset.date() < now.date():
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit() # Commit reset if needed
                    db.refresh(user) # Refresh again after potential commit

                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber and user.subscription_expires_at else ""
                # Eager load personas count
                persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
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
         "/mood [настроение] - сменить настроение активной личности\n"
         "/reset - очистить память (контекст) личности в этом чате" # Уточнил /reset
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
    db_session = db # Use provided session if available
    if db_session is None:
        db_context = get_db()
        db_session = next(db_context)
        close_db_later = True
        # Fetch persona if not provided (likely called via command)
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
        # Persona provided (likely called from handle_message)
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
        available_moods = persona.get_all_mood_names()
        available_moods_lower = {m.lower(): m for m in available_moods} # Map lower to original case

        if not available_moods:
             reply_text = f"у личности '{persona.name}' не настроены настроения."
             try:
                 if is_callback: await update.callback_query.edit_message_text(reply_text)
                 else: await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
             logger.warning(f"Persona {persona.name} has no moods defined.")
             return

        mood_arg_lower = None
        # Case 1: Command with argument (/mood радость)
        if not is_callback and context.args:
             mood_arg_lower = context.args[0].lower()
        # Case 2: Message text matches a mood name (радость) - from handle_message
        elif not is_callback and message.text and message.text.lower() in available_moods_lower:
             mood_arg_lower = message.text.lower()
        # Case 3: Callback query (set_mood_радость_...)
        elif is_callback and update.callback_query.data.startswith("set_mood_"):
             parts = update.callback_query.data.split('_')
             # Expecting set_mood_<moodname>_<personaid>
             if len(parts) >= 3:
                  mood_arg_lower = parts[2].lower() # Get moodname part
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")

        # If a mood argument was found and it's valid
        if mood_arg_lower and mood_arg_lower in available_moods_lower:
             original_mood_name = available_moods_lower[mood_arg_lower] # Get original case
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, original_mood_name) # Use original case
             # Commit only if mood was set via this function directly (not via handle_message)
             if close_db_later:
                 db_session.commit()
             reply_text = f"настроение для '{persona.name}' теперь: **{original_mood_name}**"
             try:
                 if is_callback:
                     await update.callback_query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
                 else:
                     await message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {original_mood_name}.")
        # If argument provided but invalid, or no argument provided
        else:
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m.lower()}_{persona.id}")] for m in available_moods] # Use lower in callback
             reply_markup = InlineKeyboardMarkup(keyboard)
             if mood_arg_lower: # Invalid argument case
                 reply_text = f"не знаю настроения '{mood_arg_lower}' для '{persona.name}'. выбери из списка:"
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id}. Sent mood selection.")
             else: # No argument case
                 reply_text = f"текущее настроение: **{persona.current_mood}**. выбери новое для '{persona.name}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

             try:
                 if is_callback:
                      # Check if the message text is the same to avoid Telegram error
                      if query.message.text != reply_text:
                           await update.callback_query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                      else: # Just update markup if text is same
                           await update.callback_query.edit_message_reply_markup(reply_markup=reply_markup)
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
            persona, _, owner_user = persona_info_tuple # Get owner
            # --- Check if the command issuer is the owner ---
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to reset persona '{persona.name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("только владелец личности может сбросить её память.", reply_markup=ReplyKeyboardRemove())
                return
            # --- Proceed with reset ---
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
    persona_description = " ".join(args[1:]) if len(args) > 1 else f"ai бот по имени {persona_name}."
    if len(persona_name) < 2 or len(persona_name) > 50:
         await update.message.reply_text("имя личности: 2-50 символов.", reply_markup=ReplyKeyboardRemove())
         return
    if len(persona_description) > 1500: # Increased limit slightly
         await update.message.reply_text("описание: до 1500 символов.", reply_markup=ReplyKeyboardRemove())
         return
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            # Correct way to check count before creating
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
            # Check for existing persona *after* limit check
            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                await update.message.reply_text(f"личность с именем '{persona_name}' уже есть. выбери другое.", reply_markup=ReplyKeyboardRemove())
                return
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)
            # No need to commit here, create_persona_config does it
            await update.message.reply_text(
                f"✅ личность '{new_persona.name}' создана!\n"
                f"id: `{new_persona.id}`\n"
                f"описание: {new_persona.description}\n\n"
                f"добавь в чат: /addbot `{new_persona.id}`\n"
                f"настрой детальнее: /editpersona `{new_persona.id}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")
        except IntegrityError as e:
             db.rollback()
             logger.error(f"IntegrityError creating persona for user {user_id} with name '{persona_name}': {e}", exc_info=True)
             await update.message.reply_text(f"ошибка: личность '{persona_name}' уже существует (возможно, гонка запросов). попробуй еще раз.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             db.rollback()
             logger.error(f"Database error during /createpersona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка базы данных при создании личности.")
        except Exception as e:
             # Catch potential rollback error if session is already inactive
             try:
                 if db.is_active: db.rollback()
             except: pass
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
            # Eager load personas using joinedload for efficiency
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()
            if not user_with_personas: # Should not happen if get_or_create works
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
            response_text = f"твои личности ({persona_count}/{persona_limit}):\n\n"
            for persona in personas:
                response_text += f"🔹 **{persona.name}** (ID: `{persona.id}`)\n"
                response_text += f"   `/editpersona {persona.id}` | `/deletepersona {persona.id}`\n" # Added delete shortcut
                response_text += f"   добавить в чат: `/addbot {persona.id}`\n"
                response_text += "---\n"
            await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка при загрузке списка личностей.")
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("произошла ошибка при обработке команды /mypersonas.")

async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(update.effective_chat.id)
    chat_title = update.effective_chat.title or f"Chat {chat_id}" # Use chat_id if title is missing
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

            # Find any existing ACTIVE link in this chat
            existing_active_link = db.query(ChatBotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).options(joinedload(ChatBotInstance.bot_instance_ref)).first() # Load relation

            if existing_active_link:
                old_bot_instance = existing_active_link.bot_instance_ref
                # Check if the currently active bot already uses the desired persona
                if old_bot_instance and old_bot_instance.persona_config_id == persona.id:
                    await update.message.reply_text(f"личность '{persona.name}' уже активна в этом чате.", reply_markup=ReplyKeyboardRemove())
                    return
                else:
                    # Deactivate the old link before activating the new one
                    old_persona_name = old_bot_instance.persona_config.name if old_bot_instance and old_bot_instance.persona_config else "Неизвестная"
                    logger.info(f"Deactivating previous bot instance {existing_active_link.bot_instance_id} (Persona '{old_persona_name}') in chat {chat_id} before activating {persona_id}.")
                    existing_active_link.active = False
                    db.flush() # Ensure deactivation is processed before linking the new one

            # Find or create BotInstance for the chosen PersonaConfig
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == persona.id
            ).first()
            if not bot_instance:
                 # Use create_bot_instance which handles commit
                 bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Inst:{persona.name}")
                 logger.info(f"Created BotInstance {bot_instance.id} for persona {persona.id}")
                 # No need to commit here, create_bot_instance does it

            # Link the (potentially new) BotInstance to the chat
            # link_bot_instance_to_chat handles adding or reactivating
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            if chat_link:
                 # Clear context of the newly linked/activated bot instance
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 # Commit all changes (deactivation, potential instance creation, linking, context clear)
                 db.commit()
                 logger.debug(f"Cleared {deleted_ctx} context messages for chat_bot_instance {chat_link.id} upon linking.")
                 await update.message.reply_text(
                     f"✅ личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате! Память очищена.",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
                 )
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 # This case should ideally not happen if link_bot_instance_to_chat works correctly
                 db.rollback() # Rollback any pending changes (like deactivation)
                 await update.message.reply_text("не удалось активировать личность (ошибка связывания).", reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id} - link_bot_instance_to_chat returned None.")
        except IntegrityError as e:
             db.rollback()
             logger.warning(f"IntegrityError potentially during addbot for persona {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
             db.rollback()
             logger.error(f"Database error during /addbot for persona {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка базы данных при добавлении бота.")
        except Exception as e:
             # Catch potential rollback error if session is already inactive
             try:
                 if db.is_active: db.rollback()
             except: pass
             logger.error(f"Error adding bot instance {persona_id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка при активации личности.")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data: return

    # Always answer the callback query to remove the "loading" state on the button
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query {query.id}: {e}")
        # Continue processing even if answer fails

    chat_id = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    # Route the callback based on its data prefix or value
    if data.startswith("set_mood_"):
        await mood(update, context)
    elif data == "subscribe_info":
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await generate_payment_link(update, context)
    # Add other callback handlers here if needed
    # elif data.startswith("other_prefix_"):
    #    await handle_other_callback(update, context)
    else:
        logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
        # Optionally notify the user that the button action is unknown or outdated
        # try:
        #     await query.edit_message_text("Эта кнопка больше не активна.", reply_markup=None)
        # except Exception as e:
        #     logger.error(f"Failed to edit message for unhandled callback {query.id}: {e}")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            # Ensure limits are updated before display
            now = datetime.now(timezone.utc)
            if not user.last_message_reset or user.last_message_reset.date() < now.date():
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit() # Commit the reset
                db.refresh(user) # Refresh to get the committed values
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
             if db.is_active: db.rollback() # Rollback if commit failed during reset
             await update.message.reply_text("ошибка базы данных при загрузке профиля.")
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text("ошибка при обработке команды /profile.")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")
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
        keyboard = [[InlineKeyboardButton(f"💳 оплатить {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
    message_to_update = update.callback_query.message if from_callback else update.message
    if not message_to_update: return
    try:
        if from_callback:
            # Check if message content needs changing to avoid error
            if message_to_update.text != text or message_to_update.reply_markup != reply_markup:
                 await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            # else: just answered the query
        else:
            await message_to_update.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        # If editing failed on callback, try sending a new message as fallback
        if from_callback:
            try:
                await context.bot.send_message(chat_id=message_to_update.chat.id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")

async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    logger.debug("Step 1: Checking Yookassa credentials...")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY or not YOOKASSA_SHOP_ID.isdigit():
        logger.error("Yookassa credentials not set correctly in config (shop_id must be numeric). Cannot generate payment link.")
        await query.edit_message_text("❌ ошибка: сервис оплаты не настроен правильно.", reply_markup=None)
        return
    logger.debug(f"Credentials OK. Shop ID: {YOOKASSA_SHOP_ID}")

    try:
        logger.debug("Step 2: Configuring Yookassa...")
        Configuration.configure(int(YOOKASSA_SHOP_ID), YOOKASSA_SECRET_KEY)
        logger.info(f"Yookassa configured for payment creation (Shop ID: {YOOKASSA_SHOP_ID}).")
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK: {conf_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка конфигурации платежной системы.", reply_markup=None)
        return
    logger.debug("Configuration successful.")

    logger.debug("Step 3: Preparing payment data...")
    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium подписка {context.bot.username} на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    # Ensure metadata keys are strings and values are simple types (str, int, bool)
    payment_metadata = {'telegram_user_id': str(user_id)} # Ensure user_id is string if needed
    return_url = f"https://t.me/{context.bot.username}?start=payment_success" # Basic return URL
    logger.debug(f"Data prepared. Idempotence key: {idempotence_key}")

    logger.debug("Step 4: Preparing receipt data...")
    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ {context.bot.username} на {SUBSCRIPTION_DURATION_DAYS} дней",
                "quantity": 1.0,
                "amount": {
                    "value": f"{SUBSCRIPTION_PRICE_RUB:.2f}",
                    "currency": SUBSCRIPTION_CURRENCY
                },
                "vat_code": "1", # VAT code often needs to be string '1' (No VAT) or other valid codes
                "payment_mode": "full_prepayment", # Changed to full_prepayment as service is provided after payment
                "payment_subject": "service"
            })
        ]
        receipt_data = Receipt({
            "customer": {"email": f"user_{user_id}@telegram.bot"}, # Ensure email is valid format
            "items": receipt_items,
            "tax_system_code": "1" # Example: '1' for OSN. Specify your tax system code if required. Check Yookassa docs.
        })
        logger.debug("Receipt data prepared successfully.")
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при формировании данных чека.", reply_markup=None)
        return

    payment_response = None
    try:
        logger.debug("Step 5: Building payment request...")
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata) \
            .set_receipt(receipt_data)
        request = builder.build()
        # Log the request payload for debugging (remove sensitive parts if necessary in production)
        logger.debug(f"Payment request built: {request.json()}")

        logger.info("Step 6: Calling Yookassa Payment.create via asyncio.to_thread...")
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)
        logger.info(f"Yookassa API call successful. Response received.")

        logger.debug("Step 7: Processing Yookassa response...")
        # Check response status and confirmation URL
        if not payment_response or payment_response.status == 'canceled' or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid/empty/canceled response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = "❌ не удалось получить ссылку от платежной системы"
             if payment_response and payment_response.status == 'canceled':
                 error_message += f" (статус: {payment_response.status})"
             else:
                 error_message += " (неверный ответ)."
             error_message += "\nПопробуй позже."
             await query.edit_message_text(error_message, reply_markup=None)
             return

        logger.info(f"Payment response seems valid. Payment ID: {payment_response.id}, Status: {payment_response.status}")
        confirmation_url = payment_response.confirmation.confirmation_url
        payment_id = payment_response.id

        logger.info(f"Created Yookassa payment {payment_id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ ссылка для оплаты создана!\n\n"
            "нажми кнопку ниже для перехода к оплате. после успеха подписка активируется (может занять пару минут).",
            reply_markup=reply_markup
        )
        logger.info("Payment link sent to user.")

    except Exception as e:
        # Catch Yookassa specific exceptions if needed, otherwise general Exception
        logger.error(
            f"Error during Yookassa payment creation for user {user_id}. "
            f"Exception Type: {type(e).__name__}. Exception Args: {e.args}. "
            f"Full Exception: {e}",
            exc_info=True
        )
        error_details = ""
        user_message = "❌ не удалось создать ссылку для оплаты. "

        # Check for common Yookassa error attributes
        if hasattr(e, 'http_status'): error_details += f" HTTP Status: {getattr(e, 'http_status', 'N/A')}."
        if hasattr(e, 'code'): error_details += f" Code: {getattr(e, 'code', 'N/A')}."
        if hasattr(e, 'description'): error_details += f" Description: {getattr(e, 'description', 'N/A')}."
        if hasattr(e, 'parameter'): error_details += f" Parameter: {getattr(e, 'parameter', 'N/A')}."
        if hasattr(e, 'response_body'): logger.error(f"Yookassa response body on error: {getattr(e, 'response_body', 'N/A')}")

        if error_details:
            logger.error(f"Yookassa API error details: {error_details}")
            user_message += f"Проблема с API ЮKassa ({type(e).__name__})." # Don't show details to user
        else:
             user_message += "Произошла непредвиденная ошибка."

        user_message += "\nПопробуй еще раз позже или свяжись с поддержкой."
        try:
            # Use query.message.reply_text if edit fails or isn't appropriate
            await query.edit_message_text(user_message, reply_markup=None)
        except Exception as send_e:
            logger.error(f"Failed to send error message to user {user_id} after payment creation failure: {send_e}")
            try: # Fallback: send new message
                await context.bot.send_message(chat_id=query.message.chat.id, text=user_message)
            except Exception as final_e:
                logger.error(f"Failed even to send fallback error message: {final_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # This handler is likely never called if Flask handles the webhook route
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nнайди id в /mypersonas", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    persona_id = int(args[0])
    # Clear previous edit data if any
    context.user_data.pop('edit_persona_id', None)
    context.user_data.pop('persona_config_object', None)
    context.user_data.pop('edit_field', None)
    context.user_data.pop('edit_mood_name', None)
    context.user_data.pop('delete_mood_name', None)

    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id, update.effective_user.username)
            persona_config = get_persona_by_id_and_owner(db, user.id, persona_id)
            if not persona_config:
                await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
                return ConversationHandler.END
            # Store ID for subsequent steps
            context.user_data['edit_persona_id'] = persona_id
            # No need to store the whole object, fetch it when needed to ensure freshness
            # context.user_data['persona_config_object'] = persona_config
            keyboard = await _get_edit_persona_keyboard(persona_config) # Pass object for current values
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"User {user_id} started editing persona {persona_id}. Sending choice keyboard.")
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
    if not query or not query.data: return EDIT_PERSONA_CHOICE # Stay in current state if no data
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, Persona ID from context: {persona_id}, Callback data: {data} ---")

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text("ошибка: сессия редактирования потеряна (нет id). начни снова /editpersona <id>.", reply_markup=None)
         return ConversationHandler.END

    # Fetch fresh persona config and user for checks
    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id) # Get user for subscription check
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} not found or not owned.")
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END
            # Store fresh object in context ONLY if needed by the next step's handler directly (usually not)
            # context.user_data['persona_config_object'] = persona_config
            is_premium_user = is_admin(user_id) or user.is_active_subscriber
            logger.debug(f"User {user_id} is_premium_user: {is_premium_user}")

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.edit_message_text("ошибка базы данных при проверке данных.", reply_markup=None)
         return EDIT_PERSONA_CHOICE # Stay in state, maybe try again
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.edit_message_text("Непредвиденная ошибка.", reply_markup=None)
         return ConversationHandler.END # Exit conversation on unexpected error

    logger.debug(f"Edit persona choice: {data} for persona {persona_id}")

    if data == "cancel_edit":
        logger.info(f"User {user_id} cancelled edit for persona {persona_id}.")
        await query.edit_message_text("редактирование отменено.")
        context.user_data.clear()
        return ConversationHandler.END

    # --- Moods ---
    if data == "edit_moods":
        if not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.edit_message_text("управление настроениями доступно только по подписке. /subscribe", reply_markup=None)
             # Resend the main edit menu
             keyboard = await _get_edit_persona_keyboard(persona_config) # Use fetched config
             await query.message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
             return EDIT_PERSONA_CHOICE
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             # Pass the fetched persona_config to avoid re-fetching in edit_moods_menu
             return await edit_moods_menu(update, context, persona_config=persona_config)

    # --- Field Edits ---
    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        context.user_data['edit_field'] = field # Store field to edit
        field_display_name = FIELD_MAP.get(field, field)
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        # Check premium status for restricted fields
        advanced_fields = ["should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user:
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             await query.edit_message_text(f"поле '{field_display_name}' доступно только по подписке. /subscribe", reply_markup=None)
             # Resend main edit menu
             keyboard = await _get_edit_persona_keyboard(persona_config)
             await query.message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
             return EDIT_PERSONA_CHOICE

        # Proceed to ask for input
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        if field == "max_response_messages":
            logger.debug(f"Asking user {user_id} for new max_response_messages value.")
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'** (число от 1 до 10):", parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return EDIT_MAX_MESSAGES
        else:
            current_value = getattr(persona_config, field, "")
            logger.debug(f"Asking user {user_id} for new value for field '{field}'. Current: '{current_value[:50]}...'")
            # Truncate long current values for display
            current_value_display = current_value if len(current_value) < 300 else current_value[:300] + "..."
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'**.\nтекущее:\n`{current_value_display}`", parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return EDIT_FIELD

    # --- Back Button ---
    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         keyboard = await _get_edit_persona_keyboard(persona_config)
         await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
         # Clear intermediate edit data like 'edit_field' when going back
         context.user_data.pop('edit_field', None)
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         return EDIT_PERSONA_CHOICE

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_PERSONA_CHOICE for persona {persona_id}.")
    await query.message.reply_text("неизвестный выбор. попробуй еще раз.")
    return EDIT_PERSONA_CHOICE


async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_FIELD # Stay in state if no text
    new_value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_field_update: User={user_id}, PersonaID={persona_id}, Field='{field}' ---")

    if not field or not persona_id:
        logger.warning(f"User {user_id} in edit_field_update, but edit_field ('{field}') or edit_persona_id ('{persona_id}') missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна. начни сначала /editpersona <id>.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    field_display_name = FIELD_MAP.get(field, field)
    logger.debug(f"Attempting to update field '{field}' for persona {persona_id} with value: {new_value[:50]}...")

    # --- Validation ---
    validation_error = None
    if field == "name":
        if not (2 <= len(new_value) <= 50):
             validation_error = "имя: 2-50 символов."
    elif field == "description":
         if len(new_value) > 1500:
             validation_error = "описание: до 1500 символов."
    elif field.endswith("_prompt_template"):
         if len(new_value) > 3000:
             validation_error = "промпт: до 3000 символов."
    # Add other field validations if needed

    if validation_error:
        logger.debug(f"Validation failed for field '{field}': {validation_error}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error} попробуй еще раз:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD # Stay in state

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            # Fetch fresh config for update
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned during field update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # Check name uniqueness if changing name
            if field == "name" and new_value.lower() != persona_config.name.lower():
                logger.debug(f"Checking name uniqueness for '{new_value}' (User {user_id})")
                existing = get_persona_by_name_and_owner(db, user_id, new_value)
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    await update.message.reply_text(f"имя '{new_value}' уже занято другой твоей личностью. попробуй другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                    return EDIT_FIELD # Stay in state

            # Update the attribute
            setattr(persona_config, field, new_value)
            logger.debug(f"Set persona_config.{field} for ID {persona_id}. Committing...")
            db.commit()
            db.refresh(persona_config) # Refresh to get committed value
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            # --- Success Feedback & Return to Main Menu ---
            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")
            # Clear the field being edited
            context.user_data.pop('edit_field', None)
            # Show the main edit keyboard again
            keyboard = await _get_edit_persona_keyboard(persona_config) # Use refreshed config
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return EDIT_PERSONA_CHOICE # Go back to choice state

    except SQLAlchemyError as e:
         try:
             if db.is_active: db.rollback()
         except: pass
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         # Go back to the main edit menu on error to avoid getting stuck
         try:
             with next(get_db()) as db_fallback:
                  persona_config_fallback = get_persona_by_id_and_owner(db_fallback, user_id, persona_id)
                  if persona_config_fallback:
                      keyboard_fallback = await _get_edit_persona_keyboard(persona_config_fallback)
                      await update.message.reply_text(f"редактируем **{persona_config_fallback.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard_fallback), parse_mode=ParseMode.MARKDOWN)
                      return EDIT_PERSONA_CHOICE
                  else: # If persona cannot be fetched even on fallback
                      await update.message.reply_text("Не удалось вернуться к меню редактирования.")
                      context.user_data.clear()
                      return ConversationHandler.END
         except Exception as fallback_e:
             logger.error(f"Error generating fallback menu after DB error: {fallback_e}")
             context.user_data.clear()
             return ConversationHandler.END

    except Exception as e:
         logger.error(f"Unexpected error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear() # Exit conversation on unexpected error
         return ConversationHandler.END

async def edit_max_messages_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MAX_MESSAGES
    new_value_str = update.message.text.strip()
    field = "max_response_messages" # Hardcoded field name
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_max_messages_update: User={user_id}, PersonaID={persona_id}, Value='{new_value_str}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_max_messages_update, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна (нет persona_id). начни снова /editpersona <id>.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # --- Validation ---
    try:
        new_value = int(new_value_str)
        if not (1 <= new_value <= 10):
            raise ValueError("Value out of range 1-10")
    except ValueError:
        logger.debug(f"Validation failed for max_response_messages: '{new_value_str}' is not int 1-10.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text("неверное значение. введи число от 1 до 10:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MAX_MESSAGES # Stay in state

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            logger.debug(f"Fetching PersonaConfig with id={persona_id} for owner={user_id} in edit_max_messages_update.")
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)

            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # Update value and commit
            persona_config.max_response_messages = new_value
            logger.debug(f"Set persona_config.max_response_messages to {new_value} for ID {persona_id}. Committing...")
            db.commit()
            db.refresh(persona_config) # Refresh object
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            # --- Success Feedback & Return ---
            await update.message.reply_text(f"✅ макс. сообщений в ответе для **'{persona_config.name}'** установлено: **{new_value}**")
            # Show main edit keyboard again
            keyboard = await _get_edit_persona_keyboard(persona_config) # Use refreshed config
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return EDIT_PERSONA_CHOICE # Go back to choice state

    except SQLAlchemyError as e:
         try:
             if db.is_active: db.rollback()
         except: pass
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         # Go back to the main edit menu on error
         try:
             with next(get_db()) as db_fallback:
                  persona_config_fallback = get_persona_by_id_and_owner(db_fallback, user_id, persona_id)
                  if persona_config_fallback:
                      keyboard_fallback = await _get_edit_persona_keyboard(persona_config_fallback)
                      await update.message.reply_text(f"редактируем **{persona_config_fallback.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard_fallback), parse_mode=ParseMode.MARKDOWN)
                      return EDIT_PERSONA_CHOICE
                  else:
                      await update.message.reply_text("Не удалось вернуться к меню редактирования.")
                      context.user_data.clear()
                      return ConversationHandler.END
         except Exception as fallback_e:
             logger.error(f"Error generating fallback menu after DB error: {fallback_e}")
             context.user_data.clear()
             return ConversationHandler.END

    except Exception as e:
         logger.error(f"Unexpected error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         await update.message.reply_text("❌ непредвиденная ошибка при обновлении.")
         context.user_data.clear() # Exit on unexpected error
         return ConversationHandler.END

# Helper function to generate the main edit keyboard
async def _get_edit_persona_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
    # Ensure persona_config is not None before accessing attributes
    if not persona_config:
        logger.error("_get_edit_persona_keyboard called with None persona_config")
        # Return a minimal keyboard or handle error appropriately
        return [[InlineKeyboardButton("❌ Ошибка: Личность не найдена", callback_data="cancel_edit")]]

    # Safely get max_response_messages with a default
    max_resp_msg = getattr(persona_config, 'max_response_messages', 3) # Default to 3 if missing

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

# --- Mood Editing Handlers (Minor logging/robustness adjustments) ---

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    query = update.callback_query
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.", reply_markup=None)
        return ConversationHandler.END

    # Fetch persona config if not passed directly (e.g., coming from back button)
    if persona_config is None:
        try:
            with next(get_db()) as db:
                persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
                if not persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.edit_message_text("Ошибка базы данных при загрузке настроений.", reply_markup=None)
             return EDIT_PERSONA_CHOICE # Go back to main menu

    # Check premium status again just in case (redundant if check in edit_persona_choice is reliable)
    try:
        with next(get_db()) as db:
             user = get_or_create_user(db, user_id)
             if not is_admin(user_id) and not user.is_active_subscriber:
                 logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} - likely via back button?")
                 await query.edit_message_text("управление настроениями доступно только по подписке. /subscribe", reply_markup=None)
                 keyboard = await _get_edit_persona_keyboard(persona_config)
                 await query.message.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                 return EDIT_PERSONA_CHOICE
    except Exception as e:
        logger.error(f"Error checking premium status in edit_moods_menu: {e}", exc_info=True)
        # Allow proceeding but log the error

    logger.debug(f"Showing moods menu for persona {persona_id}")

    try:
        moods_json = persona_config.mood_prompts_json if persona_config else '{}'
        moods = json.loads(moods_json or '{}')
    except json.JSONDecodeError:
        moods = {}
        logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {persona_id}. Resetting to empty for display.")

    keyboard = []
    if moods:
        sorted_moods = sorted(moods.keys())
        for mood_name in sorted_moods:
             # Shorten callback data if mood name is very long? No, keep it exact.
             keyboard.append([
                 InlineKeyboardButton(f"✏️ {mood_name.capitalize()}", callback_data=f"editmood_select_{mood_name}"),
                 InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{mood_name}")
             ])
    keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")]) # Back to main edit menu

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         # Attempt to send a new message if editing fails
         try:
            await query.message.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
         except Exception as send_e:
            logger.error(f"Failed to send fallback moods menu message: {send_e}")

    return EDIT_MOOD_CHOICE # State for choosing mood action (add/edit/delete/back)


async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE # Stay in state
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    # --- Fetch Persona Config --- Needed for back button and context
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
         return EDIT_MOOD_CHOICE # Stay in state

    # --- Handle Actions ---
    logger.debug(f"Edit mood choice: {data} for persona {persona_id}")

    # Back to main edit menu
    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config)
        await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        # Clear mood-specific context
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE

    # Add Mood: Ask for name
    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None # Clear any previous mood name being edited
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel") # Back to mood list
        await query.edit_message_text("введи **название** нового настроения (одно слово, например, 'радость'):", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME # State to receive mood name

    # Edit Mood: Ask for prompt
    if data.startswith("editmood_select_"):
        mood_name = data.replace("editmood_select_", "")
        context.user_data['edit_mood_name'] = mood_name # Store name to edit
        logger.debug(f"User {user_id} selected mood '{mood_name}' to edit for {persona_id}.")
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            current_prompt = current_moods.get(mood_name, "_нет промпта_")
        except json.JSONDecodeError:
             current_prompt = "_ошибка JSON_"
        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt = "_ошибка чтения промпта_"

        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel") # Back to mood list
        prompt_display = current_prompt if len(current_prompt) < 300 else current_prompt[:300] + "..."
        await query.edit_message_text(
            f"редактирование настроения: **{mood_name}**\n\n"
            f"текущий промпт:\n`{prompt_display}`\n\n"
            f"отправь **новый текст промпта**:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[back_button]])
        )
        return EDIT_MOOD_PROMPT # State to receive mood prompt

    # Delete Mood: Ask for confirmation
    if data.startswith("deletemood_confirm_"):
         mood_name = data.replace("deletemood_confirm_", "")
         context.user_data['delete_mood_name'] = mood_name # Store name to delete
         logger.debug(f"User {user_id} initiated delete for mood '{mood_name}' for {persona_id}. Asking confirmation.")
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{mood_name}'", callback_data=f"deletemood_delete_{mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")] # Back to mood list
         ]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await query.edit_message_text(f"точно удалить настроение **'{mood_name}'**?", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
         return DELETE_MOOD_CONFIRM # State for confirmation

    # Back from subsequent steps (like entering name/prompt) to mood list
    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         # Clear intermediate data
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         # Pass the fetched config to avoid re-fetching
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text("неизвестный выбор настроения.")
    # Pass the fetched config to avoid re-fetching
    return await edit_moods_menu(update, context, persona_config=persona_config)


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME # Stay in state
    mood_name_raw = update.message.text.strip()
    mood_name = mood_name_raw.lower() # Store lowercase internally for consistency? Or keep original? Let's keep original for now.
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # --- Validation ---
    # Allow more flexible names, but sanitize? For now, basic checks.
    # Regex allows letters (Cyrillic/Latin), numbers, underscore, hyphen. Disallows spaces.
    if not mood_name_raw or len(mood_name_raw) > 30 or not re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("название: 1-30 символов, только буквы/цифры/дефис/подчеркивание, без пробелов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME # Stay in state

    # --- Check Uniqueness ---
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text("ошибка: личность не найдена.", reply_markup=ReplyKeyboardRemove())
                 return ConversationHandler.END # Exit if persona gone

            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            # Case-insensitive check for existence
            if any(existing_name.lower() == mood_name_raw.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name_raw}' which already exists for persona {persona_id}.")
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                await update.message.reply_text(f"настроение '{mood_name_raw}' уже существует. выбери другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                return EDIT_MOOD_NAME # Stay in state

            # --- Store Name & Ask for Prompt ---
            context.user_data['edit_mood_name'] = mood_name_raw # Store the original case name
            logger.debug(f"Stored mood name '{mood_name_raw}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel") # Back to mood list
            await update.message.reply_text(f"отлично! теперь отправь **текст промпта** для настроения **'{mood_name_raw}'**:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[back_button]]))
            return EDIT_MOOD_PROMPT # State to receive prompt

    except json.JSONDecodeError:
         logger.error(f"Invalid JSON in mood_prompts_json for persona {persona_id} during name check.")
         await update.message.reply_text("ошибка чтения существующих настроений. попробуй отменить и начать заново.", reply_markup=ReplyKeyboardRemove())
         return EDIT_MOOD_NAME # Stay in state, maybe user cancels
    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка базы данных при проверке имени.", reply_markup=ReplyKeyboardRemove())
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("непредвиденная ошибка.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT # Stay in state
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name') # Get the stored original case name
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
        return EDIT_MOOD_PROMPT # Stay in state

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            # Fetch fresh config
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

            # Add or update the mood with original case name
            current_moods[mood_name] = mood_prompt
            persona_config.mood_prompts_json = json.dumps(current_moods, ensure_ascii=False) # Save JSON with unicode
            flag_modified(persona_config, "mood_prompts_json") # Mark JSON field as modified
            logger.debug(f"Updated moods JSON for persona {persona_id}. Committing...")
            db.commit()
            # No need to refresh here, as we are returning to the menu which will fetch fresh data

            context.user_data.pop('edit_mood_name', None) # Clear mood name being edited
            logger.info(f"User {user_id} updated mood '{mood_name}' for persona {persona_id}.")
            await update.message.reply_text(f"✅ настроение **'{mood_name}'** сохранено!")

            # --- Return to Mood Menu --- Use a query object simulation for edit_moods_menu
            # We need the original callback query message to edit it
            # This is tricky because we are in a MessageHandler. We need to simulate the callback.
            # Best approach: Send a *new* message with the mood menu.
            try:
                # Fetch the config again to pass to the menu function
                 db.refresh(persona_config)
                 keyboard = await _get_edit_moods_keyboard(persona_config) # Helper needed
                 await update.message.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                 return EDIT_MOOD_CHOICE
            except Exception as menu_e:
                 logger.error(f"Failed to resend mood menu after prompt update: {menu_e}")
                 return EDIT_PERSONA_CHOICE # Fallback to main edit menu

    except SQLAlchemyError as e:
        try:
            if db.is_active: db.rollback()
        except: pass
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка базы данных при сохранении настроения.")
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка при сохранении настроения.")
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE # Stay in state
    await query.answer()
    data = query.data
    mood_name = context.user_data.get('delete_mood_name') # Get original case name
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    expected_data_end = f"_{mood_name}"
    if not mood_name or not persona_id or not data.endswith(expected_data_end):
        logger.warning(f"User {user_id}: Mismatch in delete_mood_confirmed. Mood='{mood_name}', Data='{data}'")
        await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name}' for persona {persona_id}.")

    # --- Database Update ---
    try:
        with next(get_db()) as db:
            # Fetch fresh config
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

            # Delete the mood (case-sensitive match with stored name)
            if mood_name in current_moods:
                del current_moods[mood_name]
                persona_config.mood_prompts_json = json.dumps(current_moods, ensure_ascii=False)
                flag_modified(persona_config, "mood_prompts_json") # Mark as modified
                logger.debug(f"Removed mood '{mood_name}'. Committing changes for persona {persona_id}...")
                db.commit()
                # No need to refresh, returning to menu which fetches fresh data

                context.user_data.pop('delete_mood_name', None) # Clear name being deleted
                logger.info(f"Successfully deleted mood '{mood_name}' for persona {persona_id}.")
                await query.edit_message_text(f"🗑️ настроение **'{mood_name}'** удалено.", parse_mode=ParseMode.MARKDOWN)
            else:
                logger.warning(f"Mood '{mood_name}' not found for deletion in persona {persona_id} (maybe already deleted).")
                await query.edit_message_text(f"настроение '{mood_name}' не найдено (уже удалено?).", reply_markup=None)
                context.user_data.pop('delete_mood_name', None) # Clear name anyway

            # --- Return to Mood Menu --- Use the fetched config
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        try:
            if db.is_active: db.rollback()
        except: pass
        logger.error(f"Database error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка базы данных при удалении настроения.", reply_markup=None)
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при удалении настроения.", reply_markup=None)
        # Try to return to mood menu
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


# Helper function to generate mood keyboard (used in edit_mood_prompt_received)
async def _get_edit_moods_keyboard(persona_config: PersonaConfig) -> List[List[InlineKeyboardButton]]:
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

# Helper function to try returning to mood menu after an error in a sub-step
async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
     logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
     try:
         with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_config:
                 keyboard = await _get_edit_moods_keyboard(persona_config)
                 message = update.effective_message
                 if message: # Can be None if original message deleted
                     await message.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                 return EDIT_MOOD_CHOICE
             else:
                 logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                 await update.effective_message.reply_text("Не удалось вернуться к меню настроений (личность не найдена).")
                 context.user_data.clear()
                 return ConversationHandler.END
     except Exception as e:
         logger.error(f"Failed to return to mood menu after error: {e}")
         await update.effective_message.reply_text("Не удалось вернуться к меню настроений.")
         context.user_data.clear()
         return ConversationHandler.END

# --- Cancel Handler ---
async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user_id = update.effective_user.id
    logger.info(f"User {user_id} cancelled persona edit/mood edit.")
    cancel_message = "редактирование отменено."
    try:
        if update.callback_query:
            await update.callback_query.answer()
            # Check if the message text is already the cancel message to avoid error
            if update.callback_query.message and update.callback_query.message.text != cancel_message:
                await update.callback_query.edit_message_text(cancel_message, reply_markup=None)
            # else: message already shows cancellation or cannot be edited
        elif message:
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        # Try sending a new message as fallback
        if message:
            try:
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove())
            except Exception as send_e:
                logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear()
    return ConversationHandler.END

# --- Delete Persona Handlers (Minor logging adjustments) ---

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
    # Clear previous delete data
    context.user_data.pop('delete_persona_id', None)

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
                return ConversationHandler.END
            # Store ID for confirmation step
            context.user_data['delete_persona_id'] = persona_id
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
            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation.")
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
    if not query or not query.data: return ConversationHandler.END # Should not happen
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    expected_data_end = f"_{persona_id}"
    if not persona_id or not data.endswith(expected_data_end):
         logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. ID='{persona_id}', Data='{data}'")
         await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
         context.user_data.clear()
         return ConversationHandler.END

    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id}.")
    deleted_ok = False
    persona_name_deleted = "Неизвестная"
    try:
        with next(get_db()) as db:
             # Fetch name before deleting
             persona_to_delete = get_persona_by_id_and_owner(db, user_id, persona_id)
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 if delete_persona_config(db, persona_id, user_id): # This function handles commit/rollback
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name_deleted}').")
                     deleted_ok = True
                 else:
                     # delete_persona_config already logged the error
                     logger.error(f"delete_persona_config returned False for persona {persona_id}, user {user_id}.")
             else:
                 logger.warning(f"User {user_id} confirmed delete, but persona {persona_id} not found (maybe already deleted).")
                 persona_name_deleted = f"ID {persona_id}" # Use ID if name unknown
                 deleted_ok = True # Consider it "ok" as it's gone

    except SQLAlchemyError as e:
        # DB errors during the fetch phase
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id}: {e}", exc_info=True)

    # Send confirmation message based on outcome
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
