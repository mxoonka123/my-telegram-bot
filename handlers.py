import logging
import httpx
import random
import asyncio
import re
import uuid
from datetime import datetime, timezone
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
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
    SessionLocal,
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# --- Conversation Handler States for Editing Persona ---
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM = range(7)
FIELD_MAP = {
    "name": "имя",
    "description": "описание",
    "system_prompt_template": "системный промпт",
    "should_respond_prompt_template": "промпт 'отвечать?'",
    "spam_prompt_template": "промпт спама",
    "photo_prompt_template": "промпт фото",
    "voice_prompt_template": "промпт голоса"
}

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


def get_persona_and_context(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]]]]:
    chat_instance = get_chat_bot_instance(db, chat_id)
    if not chat_instance or not chat_instance.active:
        return None

    if not chat_instance.bot_instance_ref or not chat_instance.bot_instance_ref.persona_config:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance or PersonaConfig.")
         return None

    persona_config = chat_instance.bot_instance_ref.persona_config
    user = chat_instance.bot_instance_ref.owner # Get the owner user

    persona = Persona(persona_config, chat_instance)
    context_list = get_context_for_chat_bot(db, chat_instance.id)

    return persona, context_list, user


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set. Cannot send request to Langdock.")
        return ""

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7,
        "top_p": 0.95,
        "stream": False
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock URL: {url}")
    try:
        async with httpx.AsyncClient(http2=False) as client:
             resp = await client.post(url, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
        data = resp.json()

        if "content" in data and isinstance(data["content"], list):
            full_response = " ".join([part.get("text", "") for part in data["content"] if part.get("type") == "text"])
            logger.debug(f"Received text from Langdock: {full_response[:200]}...")
            return full_response.strip()

        logger.warning(f"Langdock response format unexpected: {data}. Attempting to get 'response' field.")
        response_text = data.get("response") or ""
        logger.debug(f"Received fallback response from Langdock: {response_text[:200]}...")
        return response_text.strip()

    except httpx.HTTPStatusError as e:
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True)
        raise
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        raise


async def process_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str, persona: Persona, full_bot_response_text: str, db: Session):
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}")

    add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
    logger.debug("AI response added to database context.")

    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    for gif in gif_links:
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()
    logger.debug(f"Extracted {len(gif_links)} gif links. Remaining text: {all_text_content[:200]}...")

    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    for gif in gif_links:
        try:
            await context.bot.send_animation(chat_id=chat_id, animation=gif)
            logger.info(f"Sent gif: {gif}")
            await asyncio.sleep(random.uniform(1.5, 3.0))
        except Exception as e:
            logger.error(f"Error sending gif {gif}: {e}", exc_info=True)
            try:
                 await context.bot.send_photo(chat_id=chat_id, photo=gif)
                 logger.warning(f"Sent gif {gif} as photo after animation failure.")
            except Exception:
                 try:
                      await context.bot.send_document(chat_id=chat_id, document=gif)
                      logger.warning(f"Sent gif {gif} as document after photo failure.")
                 except Exception as e2:
                      logger.error(f"Failed to send gif {gif} as photo or document: {e2}")

    if text_parts_to_send:
        for i, part in enumerate(text_parts_to_send):
            if update.effective_chat and update.effective_chat.type in ["group", "supergroup"]:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.5, 3.0) + len(part) / 50)

            if part.strip():
                 try:
                     await context.bot.send_message(chat_id=chat_id, text=part.strip())
                     logger.info(f"Sent text part: {part.strip()[:100]}...")
                 except Exception as e:
                     logger.error(f"Error sending text part: {e}", exc_info=True)

            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(1.0, 2.5))


async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Sends a message when the user hits their limit."""
    text = (
        f"упс! 😕 похоже, ты достиг дневного лимита сообщений ({user.daily_message_count}/{user.message_limit}).\n\n"
        f"✨ **хочешь общаться без ограничений?** ✨\n"
        f"получи подписку всего за {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY} в месяц!\n\n"
        "**что ты получишь:**\n"
        f"✅ намного больше сообщений в день ({PAID_DAILY_MESSAGE_LIMIT})\n"
        f"✅ возможность создать больше личностей ({PAID_PERSONA_LIMIT} вместо {FREE_PERSONA_LIMIT})\n"
        "✅ полная настройка всех промптов и настроений\n"
        "✅ безграничное общение с твоими ai-личностями!\n\n"
        "👇 нажми кнопку ниже, чтобы узнать больше и подписаться!"
    )
    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    message_text = update.message.text

    logger.info(f"Received text message from user {user_id} ({username}) in chat {chat_id}: {message_text}")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        persona_context_user_tuple = get_persona_and_context(chat_id, db)

        if not persona_context_user_tuple:
            return

        persona, current_context_list, owner_user = persona_context_user_tuple
        logger.debug(f"Handling message for persona '{persona.name}' in chat {chat_id}.")

        # --- Check Message Limit ---
        if not check_and_update_user_limits(db, owner_user):
            logger.info(f"User {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}).")
            # Send notification only once per limit hit maybe, or check if the message is directed to the bot specifically
            # For simplicity, we send it now if the limit is hit during this check.
            await send_limit_exceeded_message(update, context, owner_user)
            return
        # --- Limit check passed ---


        if message_text and message_text.lower() in persona.get_all_mood_names():
             logger.info(f"Message '{message_text}' matched a mood name. Attempting to change mood.")
             await mood(update, context, db=db, persona=persona)
             return

        if update.effective_chat.type in ["group", "supergroup"]:
            if persona.should_respond_prompt_template:
                # Check subscription for advanced feature? Example:
                # if not owner_user.is_subscribed:
                #    logger.debug("User not subscribed, skipping should_respond check")
                # else: # Proceed only for subscribed users
                 should_respond_prompt = persona.format_should_respond_prompt(message_text)
                 if should_respond_prompt:
                     try:
                         decision_response = await send_to_langdock(
                             system_prompt=should_respond_prompt,
                             messages=[{"role": "user", "content": f"сообщение в чате: {message_text}"}]
                         )
                         answer = decision_response.strip().lower()

                         if not answer.startswith("д") and random.random() > 0.9:
                             logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond anyway (random chance).")
                         elif answer.startswith("д"):
                             logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond.")
                         else:
                             logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding NOT to respond.")
                             return

                     except Exception as e:
                          logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                          logger.warning("Error in should_respond. Defaulting to respond.")
                 else:
                     logger.debug(f"Persona {persona.name} in chat {chat_id} has no should_respond template. Attempting to respond.")
            else:
                 logger.debug(f"Persona {persona.name} in chat {chat_id} has no should_respond template configured. Attempting to respond.")


        add_message_to_context(db, persona.chat_instance.id, "user", message_text)
        context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
        logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")


        try:
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            logger.debug("Formatted main system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug("Received response from Langdock for main message.")
            await process_and_send_response(update, context, chat_id, persona, response_text, db)
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        persona_context_user_tuple = get_persona_and_context(chat_id, db)

        if not persona_context_user_tuple:
            return
        persona, current_context_list, owner_user = persona_context_user_tuple
        logger.debug(f"Handling {media_type} for persona '{persona.name}' in chat {chat_id}.")

        # --- Check Message Limit ---
        if not check_and_update_user_limits(db, owner_user):
            logger.info(f"User {owner_user.telegram_id} exceeded daily message limit ({owner_user.daily_message_count}/{owner_user.message_limit}) for media.")
            await send_limit_exceeded_message(update, context, owner_user)
            return
        # --- Limit check passed ---


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
            logger.info(f"Persona {persona.name} in chat {chat_id} does not have a {media_type} prompt template. Skipping.")
            return

        add_message_to_context(db, persona.chat_instance.id, "user", context_text)
        context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
        logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")

        try:
            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 return

            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}.")
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_media(update, context, "photo")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_media(update, context, "voice")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /start from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        persona_context_user_tuple = get_persona_and_context(chat_id, db)

        if persona_context_user_tuple:
             persona, _, _ = persona_context_user_tuple
             await update.message.reply_text(
                 f"привет! я {persona.name}. я уже активен в этом чате.\n"
                 "используй /help для списка команд.",
                 reply_markup=ReplyKeyboardRemove()
             )
        else:
             status = "⭐ Premium" if user.is_subscribed and user.subscription_expires_at and user.subscription_expires_at > datetime.now(timezone.utc) else "🆓 Free"
             await update.message.reply_text(
                 f"привет! 👋 я ai бот для создания уникальных собеседников.\n\n"
                 f"твой статус: **{status}**\n"
                 f"лимит личностей: {len(user.persona_configs)}/{user.persona_limit}\n"
                 f"сообщения сегодня: {user.daily_message_count}/{user.message_limit}\n\n"
                 "**начало работы:**\n"
                 "1. `/createpersona <имя> [описание]` - создай свою первую ai-личность.\n"
                 "2. `/mypersonas` - посмотри список своих личностей и их id.\n"
                 "3. `/addbot <id>` - добавь личность в этот или другой чат.\n\n"
                 "**хочешь больше?**\n"
                 "`/profile` - проверь статус и лимиты.\n"
                 "`/subscribe` - узнай о преимуществах подписки!\n\n"
                 "полный список команд: /help",
                 parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
             )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    logger.info(f"Command /help from user {user_id} ({username}) in chat {chat_id}")

    help_text = (
        "**🤖 основные команды:**\n"
        "/start - приветственное сообщение и статус\n"
        "/help - эта справка\n"
        "/profile - твой статус подписки и лимиты\n"
        "/subscribe - информация о подписке и оплата\n\n"
        "**👤 управление личностями (персонами):**\n"
        "/createpersona <имя> [описание] - создать личность\n"
        "/mypersonas - список твоих личностей\n"
        "/editpersona <id> - редактировать личность (имя, описание, промпты, настроения)\n"
        "/deletepersona <id> - удалить личность (необратимо!)\n"
        "/addbot <id> - активировать личность в текущем чате\n\n"
        "**💬 управление активной личностью в чате:**\n"
        "_(работают, если личность добавлена в чат)_\n"
        "/mood - выбрать или сменить настроение\n"
        "/reset - очистить память (контекст) личности в этом чате\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /mood or mood text command from user {user_id} ({username}) in chat {chat_id}")

    close_db_later = False
    if db is None or persona is None:
        db = SessionLocal()
        close_db_later = True
        persona_context_user_tuple = get_persona_and_context(chat_id, db)
        if not persona_context_user_tuple:
            await update.message.reply_text("в этом чате нет активной личности :(", reply_markup=ReplyKeyboardRemove())
            logger.debug(f"No active persona/bot instance for chat {chat_id}. Cannot set mood.")
            db.close()
            return
        persona, _, _ = persona_context_user_tuple
        chat_bot_instance = persona.chat_instance
    else:
         # Called from handle_message
         chat_bot_instance = persona.chat_instance


    try:
        available_moods = persona.get_all_mood_names()
        if not available_moods:
             logger.warning(f"Persona {persona.name} has no moods defined.")
             if update.callback_query:
                 await update.callback_query.edit_message_text(f"у личности '{persona.name}' не настроены настроения :(")
             else:
                 await update.message.reply_text(f"у личности '{persona.name}' не настроены настроения :(")
             return

        mood_arg = None
        if update.message and context.args:
             mood_arg = context.args[0].lower()
        elif update.message and not context.args: # /mood command without args
             pass # Show buttons
        elif update.message and update.message.text and update.message.text.lower() in available_moods: # Text command mood change
             mood_arg = update.message.text.lower()

        if mood_arg:
             if mood_arg in available_moods:
                 set_mood_for_chat_bot(db, chat_bot_instance.id, mood_arg)
                 reply_text = f"настроение для '{persona.name}' теперь: {mood_arg}"
                 if update.callback_query:
                     await update.callback_query.edit_message_text(reply_text)
                 else:
                     await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
                 logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {mood_arg}.")
             else:
                 # Invalid mood argument
                 keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{persona.id}")] for m in available_moods] # Added persona_id
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 reply_text = f"не знаю настроения '{mood_arg}'. выбери из списка для '{persona.name}':"
                 if update.callback_query:
                      await update.callback_query.edit_message_text(reply_text, reply_markup=reply_markup)
                 else:
                      await update.message.reply_text(reply_text, reply_markup=reply_markup)
                 logger.debug(f"Invalid mood argument '{mood_arg}' for chat {chat_id}. Sent mood selection.")
        else:
             # Show mood selection buttons
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{persona.id}")] for m in available_moods] # Added persona_id
             reply_markup = InlineKeyboardMarkup(keyboard)
             reply_text = f"выбери настроение для '{persona.name}':"
             if update.callback_query:
                  await update.callback_query.edit_message_text(reply_text, reply_markup=reply_markup)
             else:
                  await update.message.reply_text(reply_text, reply_markup=reply_markup)
             logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")


    finally:
        if close_db_later:
            db.close()


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /reset from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        persona_context_user_tuple = get_persona_and_context(chat_id, db)
        if not persona_context_user_tuple:
            await update.message.reply_text("в этом чате нет активной личности :(", reply_markup=ReplyKeyboardRemove())
            logger.debug(f"No active persona/bot instance for chat {chat_id}. Cannot reset context.")
            return
        persona, _, _ = persona_context_user_tuple
        chat_bot_instance = persona.chat_instance

        try:
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}.")
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
            db.rollback()
            logger.error(f"Error resetting context for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("не удалось очистить память :(", reply_markup=ReplyKeyboardRemove())


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    logger.info(f"Command /createpersona from user {user_id} ({username}) in chat {chat_id} with args: {context.args}")

    args = context.args
    if not args:
        await update.message.reply_text(
            "формат: `/createpersona <имя> [описание]`\n"
            "_пример: /createpersona Вася Веселый парень_",
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


    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)

        if not user.can_create_persona:
             logger.warning(f"User {user_id} cannot create persona, limit reached ({len(user.persona_configs)}/{user.persona_limit}).")
             text = (
                 f"упс! ты достиг лимита личностей ({user.persona_limit}). 😟\n\n"
                 f"чтобы создавать больше личностей и получить другие преимущества, переходи на подписку!\n"
                 f"используй /subscribe"
             )
             await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
             return


        existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
        if existing_persona:
            await update.message.reply_text(f"личность с именем '{persona_name}' уже есть. выбери другое имя.", reply_markup=ReplyKeyboardRemove())
            return

        try:
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)
            await update.message.reply_text(
                f"личность '{new_persona.name}' создана!\n"
                f"id: `{new_persona.id}`\n"
                f"описание: {new_persona.description}\n\n"
                f"добавь ее в чат: /addbot `{new_persona.id}`\n"
                f"или настрой детальнее: /editpersona `{new_persona.id}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")
        except IntegrityError:
             db.rollback()
             logger.error(f"IntegrityError creating persona for user {user_id} with name '{persona_name}'.", exc_info=True)
             await update.message.reply_text(f"ошибка: личность '{persona_name}' уже существует.", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
             db.rollback()
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("ошибка при создании личности :(", reply_markup=ReplyKeyboardRemove())


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    logger.info(f"Command /mypersonas from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        personas = get_personas_by_owner(db, user.id)

        if not personas:
            await update.message.reply_text(
                "у тебя пока нет личностей.\n"
                "создай: /createpersona <имя> [описание]",
                parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
            )
            return

        response_text = f"твои личности ({len(personas)}/{user.persona_limit}):\n\n"
        for persona in personas:
            response_text += f"🔹 **{persona.name}** (ID: `{persona.id}`)\n"
            # response_text += f"   Описание: {persona.description[:50] + '...' if persona.description and len(persona.description) > 50 else persona.description or 'нет'}\n"
            response_text += f"   /editpersona `{persona.id}`\n"
            response_text += f"   /deletepersona `{persona.id}`\n"
            response_text += "---\n"

        await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
        logger.info(f"User {user_id} requested mypersonas. Sent {len(personas)} personas.")


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    chat_title = update.effective_chat.title or chat_id
    logger.info(f"Command /addbot from user {user_id} ({username}) in chat {chat_id} ('{chat_title}') with args: {context.args}")

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

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        persona = get_persona_by_id_and_owner(db, user.id, persona_id)
        if not persona:
             await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
             return

        bot_instance = db.query(BotInstance).filter(
            BotInstance.owner_id == user.id,
            BotInstance.persona_config_id == persona.id
        ).first()

        if not bot_instance:
             bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Inst:{persona.name}-User:{username}")
             logger.info(f"Created BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")
        else:
             logger.info(f"Found existing BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")

        try:
            # Deactivate any existing bot for *this user* in *this chat* first
            existing_link = db.query(ChatBotInstance).join(BotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 BotInstance.owner_id == user.id, # Only deactivate bots owned by the command user
                 ChatBotInstance.active == True
            ).first()

            if existing_link and existing_link.bot_instance_id != bot_instance.id:
                 existing_link.active = False
                 db.commit()
                 logger.info(f"Deactivated previous bot instance {existing_link.bot_instance_id} for user {user.id} in chat {chat_id}.")
            elif existing_link and existing_link.bot_instance_id == bot_instance.id:
                 await update.message.reply_text(f"личность '{persona.name}' уже активна в этом чате.", reply_markup=ReplyKeyboardRemove())
                 return # Already active, do nothing more


            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            if chat_link:
                 # Clear context on activation
                 db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 db.commit()
                 logger.debug(f"Cleared old context for chat_bot_instance {chat_link.id} upon linking.")

                 await update.message.reply_text(
                     f"личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате!",
                     parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()
                 )
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 # link_bot_instance_to_chat might return None if linking failed (e.g., another user's bot active)
                 await update.message.reply_text("не удалось активировать личность. возможно, в чате уже есть другая активная личность.", reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id}. link_bot_instance_to_chat returned None.")


        except IntegrityError:
             db.rollback()
             logger.warning(f"IntegrityError linking BotInstance {bot_instance.id} to chat {chat_id}. Already linked?", exc_info=True)
             await update.message.reply_text("эта личность уже была связана с чатом. активирую...", reply_markup=ReplyKeyboardRemove())
             # Attempt to reactivate if integrity error occurs (should be handled by link_bot_instance_to_chat now)
             chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)
             if chat_link and chat_link.active:
                 await update.message.reply_text(f"личность '{persona.name}' снова активна!", reply_markup=ReplyKeyboardRemove())
             else:
                 await update.message.reply_text(f"не удалось повторно активировать '{persona.name}'.", reply_markup=ReplyKeyboardRemove())

        except Exception as e:
             db.rollback()
             logger.error(f"Error linking bot instance {bot_instance.id} to chat {chat_id} ('{chat_title}'): {e}", exc_info=True)
             await update.message.reply_text("ошибка при активации личности :(", reply_markup=ReplyKeyboardRemove())


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat.id)
    user_id = query.from_user.id
    username = query.from_user.username or "unknown"
    data = query.data

    logger.info(f"Callback query from user {user_id} ({username}) in chat {chat_id} data: {data}")

    # --- Mood Setting Callback ---
    if data.startswith("set_mood_"):
        parts = data.split('_')
        if len(parts) == 4 and parts[2].isdigit(): # Expect set_mood_<moodname>_<persona_id>
            mood_name = parts[2]
            persona_id = int(parts[3])
            with SessionLocal() as db:
                user = get_or_create_user(db, user_id, username) # Ensure user exists
                persona_config = get_persona_by_id_and_owner(db, user.id, persona_id)
                if not persona_config:
                    await query.edit_message_text("ошибка: личность не найдена.")
                    return

                # Find the active ChatBotInstance for *this* persona in *this* chat
                chat_bot_instance = db.query(ChatBotInstance)\
                                     .join(BotInstance)\
                                     .filter(ChatBotInstance.chat_id == chat_id,
                                             BotInstance.persona_config_id == persona_id,
                                             ChatBotInstance.active == True)\
                                     .first()

                if not chat_bot_instance:
                     await query.edit_message_text(f"личность '{persona_config.name}' не активна в этом чате.")
                     return

                persona = Persona(persona_config, chat_bot_instance)
                available_moods = persona.get_all_mood_names()
                if not available_moods:
                     await query.edit_message_text(f"у личности '{persona.name}' нет настроений.")
                     return

                if mood_name in available_moods:
                     set_mood_for_chat_bot(db, chat_bot_instance.id, mood_name)
                     await query.edit_message_text(f"настроение для '{persona.name}' теперь: {mood_name}")
                     logger.info(f"User {user_id} set mood for persona {persona.name} (ID: {persona_id}) in chat {chat_id} to {mood_name} via callback.")
                else:
                     await query.edit_message_text(f"неверное настроение: {mood_name}")
                     logger.warning(f"User {user_id} attempted unknown mood '{mood_name}' via callback in chat {chat_id}.")
        else:
             logger.error(f"Invalid set_mood callback format: {data}")
             await query.edit_message_text("ошибка формата настроения.")


    # --- Subscription Callbacks ---
    elif data == "subscribe_info":
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        # Generate and send payment link
        await generate_payment_link(update, context)

    # --- Edit Persona Callbacks (redirect to ConversationHandler) ---
    elif data.startswith("editpersona_"):
        # Let the ConversationHandler deal with these
        # Need to ensure the callback query handler for editing is added correctly
        # This might require passing the data to the conversation handler state
        pass # Handled by ConversationHandler's CallbackQueryHandler entry point
    elif data.startswith("editmood_"):
         pass # Handled by ConversationHandler's CallbackQueryHandler entry point
    elif data.startswith("deletemood_"):
         pass # Handled by ConversationHandler's CallbackQueryHandler entry point
    elif data.startswith("delete_persona_confirm_"):
         pass # Handled by ConversationHandler's CallbackQueryHandler entry point


# --- Subscription Commands ---

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /profile from user {user_id} ({username})")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        now = datetime.now(timezone.utc)

        # Reset daily count if needed (e.g., if user only checks profile)
        if not user.last_message_reset or user.last_message_reset.date() < now.date():
            user.daily_message_count = 0
            user.last_message_reset = now
            db.commit()
            db.refresh(user)

        is_active_subscriber = user.is_subscribed and user.subscription_expires_at and user.subscription_expires_at > now
        status = "⭐ Premium" if is_active_subscriber else "🆓 Free"
        expires_text = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active_subscriber else "нет активной подписки"

        text = (
            f"👤 **твой профиль**\n\n"
            f"статус: **{status}**\n"
            f"{expires_text}\n\n"
            f"**лимиты:**\n"
            f"сообщения сегодня: {user.daily_message_count}/{user.message_limit}\n"
            f"создано личностей: {len(user.persona_configs)}/{user.persona_limit}\n\n"
        )
        if not is_active_subscriber:
            text += "🚀 хочешь больше? жми /subscribe !"

        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /subscribe from user {user_id} ({username})")

    text = (
        f"✨ **премиум подписка ({SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}/мес)** ✨\n\n"
        "получи максимум возможностей:\n"
        f"✅ **{PAID_DAILY_MESSAGE_LIMIT}** сообщений в день (вместо {FREE_DAILY_MESSAGE_LIMIT})\n"
        f"✅ **{PAID_PERSONA_LIMIT}** личностей (вместо {FREE_PERSONA_LIMIT})\n"
        f"✅ полная настройка всех промптов\n"
        f"✅ создание и редактирование своих настроений\n"
        f"✅ безлимитное общение без барьеров!\n\n"
        f"подписка действует {SUBSCRIPTION_DURATION_DAYS} дней."
    )
    keyboard = [[InlineKeyboardButton(f"💳 оплатить {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if from_callback:
        query = update.callback_query
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    telegram_id = query.from_user.id

    logger.info(f"Generating payment link for user {user_id}")

    # Ensure Yookassa keys are set
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("Yookassa credentials not set in config.")
        await query.message.reply_text("ошибка: сервис оплаты временно недоступен.")
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Подписка на бота на {SUBSCRIPTION_DURATION_DAYS} дней (User: {telegram_id})"
    # Metadata can be used to link payment back to user in webhook
    payment_metadata = {'telegram_id': telegram_id}

    # IMPORTANT: Replace with your actual return URL (where user goes after payment)
    # return_url = f"{WEBHOOK_URL_BASE}/payment_success?user_id={telegram_id}" # Example success URL
    return_url = "https://t.me/" + context.bot.username # Simple return to bot

    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": str(SUBSCRIPTION_PRICE_RUB), "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata)
            # Optional: Add receipt details if needed
            # .set_receipt({
            #     "customer": {"email": "user@example.com"}, # Get user email if possible/needed
            #     "items": [
            #         {
            #             "description": f"Подписка на {SUBSCRIPTION_DURATION_DAYS} дней",
            #             "quantity": "1.00",
            #             "amount": {"value": str(SUBSCRIPTION_PRICE_RUB), "currency": SUBSCRIPTION_CURRENCY},
            #             "vat_code": "1" # Check correct VAT code
            #         }
            #     ]
            # })

        request = builder.build()
        payment_response = Payment.create(request, idempotence_key)

        confirmation_url = payment_response.confirmation.confirmation_url
        payment_id = payment_response.id
        context.user_data['pending_payment_id'] = payment_id # Store for potential polling

        logger.info(f"Created Yookassa payment {payment_id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("🔗 перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ ссылка для оплаты создана!\n\n"
            "нажми кнопку ниже, чтобы перейти к оплате. после успешной оплаты твоя подписка будет активирована автоматически.",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Yookassa payment creation failed for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ не удалось создать ссылку для оплаты. попробуй позже.")


async def yookassa_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Conceptual Webhook Handler (Requires Flask/FastAPI etc. and public URL)
    This function shows the logic but won't run directly in python-telegram-bot polling.
    You would typically receive a POST request from Yookassa here.
    """
    # 1. Receive POST request data from Yookassa (usually JSON)
    # request_data = await get_request_body() # pseudo-code
    # logger.info(f"Received Yookassa webhook: {request_data}")

    # 2. Validate the notification (check source IP, signature if available)
    # if not is_valid_yookassa_notification(request_data):
    #     logger.warning("Invalid Yookassa notification received.")
    #     return # Respond with error status

    # 3. Parse the notification
    # try:
    #     notification = WebhookNotification(request_data)
    #     payment_info = notification.object
    # except Exception as e:
    #     logger.error(f"Error parsing Yookassa notification: {e}")
    #     return # Respond with error status

    # 4. Check payment status
    # if payment_info.status == 'succeeded':
    #     payment_id = payment_info.id
    #     amount_paid = float(payment_info.amount.value)
    #     currency_paid = payment_info.amount.currency
    #     telegram_id = payment_info.metadata.get('telegram_id')
    #
    #     logger.info(f"Webhook: Payment {payment_id} succeeded for user {telegram_id}, amount {amount_paid} {currency_paid}")
    #
    #     if telegram_id and amount_paid == SUBSCRIPTION_PRICE_RUB and currency_paid == SUBSCRIPTION_CURRENCY:
    #         with SessionLocal() as db:
    #             user = db.query(User).filter(User.telegram_id == telegram_id).first()
    #             if user:
    #                 if activate_subscription(db, user.id):
    #                      logger.info(f"Subscription activated for user {telegram_id} via webhook.")
    #                      # Send confirmation message to user via bot
    #                      try:
    #                           await context.bot.send_message(
    #                               chat_id=telegram_id,
    #                               text="🎉 твоя премиум подписка успешно активирована! наслаждайся всеми возможностями."
    #                           )
    #                      except Exception as send_error:
    #                           logger.error(f"Failed to send subscription confirmation to user {telegram_id}: {send_error}")
    #                 else:
    #                      logger.error(f"Failed to activate subscription in DB for user {telegram_id} (Payment {payment_id}).")
    #             else:
    #                  logger.error(f"User with telegram_id {telegram_id} not found for successful payment {payment_id}.")
    #     else:
    #          logger.warning(f"Payment {payment_id} succeeded but validation failed (telegram_id: {telegram_id}, amount: {amount_paid}, currency: {currency_paid})")
    #
    # elif payment_info.status == 'canceled':
    #     logger.info(f"Webhook: Payment {payment_info.id} canceled for user {payment_info.metadata.get('telegram_id')}")
    #     # Optionally notify user
    # else:
    #     logger.info(f"Webhook: Received payment status '{payment_info.status}' for payment {payment_info.id}")

    # 5. Respond to Yookassa with 200 OK
    # return # Respond 200 OK
    pass # Placeholder


# --- Edit Persona Conversation Handler ---

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"Command /editpersona from user {user_id} with args: {args}")

    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nнайди id в /mypersonas", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    persona_id = int(args[0])
    context.user_data['edit_persona_id'] = persona_id

    with SessionLocal() as db:
        user = get_or_create_user(db, update.effective_user.id, update.effective_user.username)
        persona_config = get_persona_by_id_and_owner(db, user.id, persona_id)

        if not persona_config:
            await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

        persona = Persona(persona_config)
        context.user_data['persona_object'] = persona # Store Persona object for easier access

        keyboard = [
            [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
            [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
            # Add buttons only for subscribers?
            [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
            [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
            [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
            [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"что ты хочешь изменить для личности **{persona.name}** (id: `{persona.id}`)?", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return EDIT_PERSONA_CHOICE

async def edit_persona_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel_edit":
        await query.edit_message_text("редактирование отменено.")
        context.user_data.pop('edit_persona_id', None)
        context.user_data.pop('persona_object', None)
        return ConversationHandler.END

    if data == "edit_moods":
        return await edit_moods_menu(update, context) # Transition to mood editing menu

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        context.user_data['edit_field'] = field
        field_display_name = FIELD_MAP.get(field, field)

        # Check subscription for advanced fields if needed
        # user = get_or_create_user(SessionLocal(), query.from_user.id) # Need DB session
        # if field in ["should_respond_prompt_template", "spam_prompt_template"] and not user.is_subscribed:
        #     await query.message.reply_text("эта настройка доступна только по подписке (/subscribe).")
        #     # Reshow menu? Or end? For now, proceed but could block here.

        await query.edit_message_text(f"отправь новое значение для поля **'{field_display_name}'**:", parse_mode=ParseMode.MARKDOWN)
        return EDIT_FIELD

    await query.message.reply_text("неизвестный выбор. попробуй еще раз.")
    return EDIT_PERSONA_CHOICE # Stay in the same state


async def edit_field_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_value = update.message.text
    field = context.user_data.get('edit_field')
    persona_id = context.user_data.get('edit_persona_id')
    persona: Persona = context.user_data.get('persona_object')
    field_display_name = FIELD_MAP.get(field, field)


    if not field or not persona_id or not persona:
        await update.message.reply_text("ошибка: не найдены данные для редактирования. начни сначала /editpersona <id>.")
        return ConversationHandler.END

    # Validation
    if field == "name" and (len(new_value) < 2 or len(new_value) > 50):
        await update.message.reply_text("имя: 2-50 символов. попробуй еще раз:")
        return EDIT_FIELD # Stay in state to retry
    if field == "description" and len(new_value) > 1000:
        await update.message.reply_text("описание: до 1000 символов. попробуй еще раз:")
        return EDIT_FIELD
    if field.endswith("_prompt_template") and len(new_value) > 2000:
        await update.message.reply_text("промпт: до 2000 символов. попробуй еще раз:")
        return EDIT_FIELD


    with SessionLocal() as db:
        try:
            # Fetch the config again within the session to update
            persona_config = db.query(PersonaConfig).get(persona_id)
            if not persona_config or persona_config.owner_id != update.effective_user.id:
                 await update.message.reply_text("ошибка доступа к личности.")
                 return ConversationHandler.END

            # Check for name uniqueness if name is changed
            if field == "name" and new_value != persona_config.name:
                existing = get_persona_by_name_and_owner(db, update.effective_user.id, new_value)
                if existing:
                    await update.message.reply_text(f"имя '{new_value}' уже занято. попробуй другое:")
                    return EDIT_FIELD


            setattr(persona_config, field, new_value)
            db.commit()
            db.refresh(persona_config)
            # Update the persona object in user_data as well
            context.user_data['persona_object'] = Persona(persona_config)
            logger.info(f"User {update.effective_user.id} updated field '{field}' for persona {persona_id}.")

            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")

        except Exception as e:
             db.rollback()
             logger.error(f"Error updating field {field} for persona {persona_id}: {e}", exc_info=True)
             await update.message.reply_text("❌ произошла ошибка при обновлении.")
             return ConversationHandler.END # End on error


    # Go back to main edit menu
    keyboard = [
            [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
            [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
            [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
            [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
            [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
            [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
            [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")] # Changed text
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return EDIT_PERSONA_CHOICE


async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    # await query.answer() # Already answered in parent? If called directly, answer here.
    persona: Persona = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')

    if not persona:
        await query.message.reply_text("ошибка: не найдена личность для редактирования.")
        return ConversationHandler.END

    moods = persona.mood_prompts
    keyboard = []
    if moods:
        for mood_name in moods:
             keyboard.append([
                 InlineKeyboardButton(f"✏️ {mood_name.capitalize()}", callback_data=f"editmood_select_{mood_name}"),
                 InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{mood_name}")
             ])
    keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад к редактированию", callback_data="edit_persona_back")]) # Back button

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(f"управление настроениями для **{persona.name}**:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return EDIT_MOOD_CHOICE


async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona: Persona = context.user_data.get('persona_object')

    if not persona:
        await query.message.reply_text("ошибка: не найдена личность для редактирования.")
        return ConversationHandler.END

    if data == "edit_persona_back":
        # Regenerate main edit menu
        keyboard = [
            [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
            [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
            [InlineKeyboardButton("🤔 Промпт 'Отвечать?'", callback_data="edit_field_should_respond_prompt_template")],
            [InlineKeyboardButton("💬 Промпт спама", callback_data="edit_field_spam_prompt_template")],
            [InlineKeyboardButton("🖼️ Промпт фото", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса", callback_data="edit_field_voice_prompt_template")],
            [InlineKeyboardButton("🎭 Настроения", callback_data="edit_moods")],
            [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"что ты хочешь изменить для личности **{persona.name}**?", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        context.user_data['edit_mood_name'] = None # Indicate adding new
        await query.edit_message_text("введи название нового настроения (например, 'веселье', 'задумчивость'):")
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        mood_name = data.replace("editmood_select_", "")
        context.user_data['edit_mood_name'] = mood_name
        current_prompt = persona.mood_prompts.get(mood_name, "нет промпта")
        await query.edit_message_text(f"редактирование настроения: **{mood_name}**\n\nтекущий промпт:\n`{current_prompt}`\n\nотправь новый текст промпта для этого настроения:", parse_mode=ParseMode.MARKDOWN)
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         mood_name = data.replace("deletemood_confirm_", "")
         context.user_data['delete_mood_name'] = mood_name
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{mood_name}'", callback_data=f"deletemood_delete_{mood_name}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back")] # Go back to mood list
         ]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await query.edit_message_text(f"точно удалить настроение '{mood_name}'?", reply_markup=reply_markup)
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back":
         # Go back to mood list from delete confirm
         return await edit_moods_menu(update, context)


    await query.message.reply_text("неизвестный выбор.")
    return EDIT_MOOD_CHOICE


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    mood_name = update.message.text.strip().lower()
    persona: Persona = context.user_data.get('persona_object')

    if not mood_name or len(mood_name) > 30:
        await update.message.reply_text("название настроения: 1-30 символов. попробуй еще:")
        return EDIT_MOOD_NAME

    if mood_name in persona.mood_prompts:
        await update.message.reply_text(f"настроение '{mood_name}' уже существует. выбери другое:")
        return EDIT_MOOD_NAME

    context.user_data['edit_mood_name'] = mood_name
    await update.message.reply_text(f"отлично! теперь отправь текст промпта для настроения '{mood_name}':\n(например: 'ты очень игривый и веселый')")
    return EDIT_MOOD_PROMPT


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona: Persona = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')

    if not mood_name or not persona or not persona_id:
        await update.message.reply_text("ошибка: потеряны данные. начни сначала /editpersona <id>.")
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 1000:
        await update.message.reply_text("промпт настроения: 1-1000 символов. попробуй еще:")
        return EDIT_MOOD_PROMPT

    with SessionLocal() as db:
        # Fetch config within session
        persona_config = db.query(PersonaConfig).get(persona_id)
        if not persona_config or persona_config.owner_id != update.effective_user.id:
            await update.message.reply_text("ошибка доступа к личности.")
            return ConversationHandler.END

        # Update using Persona method which handles JSON logic
        current_moods = json.loads(persona_config.mood_prompts_json or '{}')
        current_moods[mood_name] = mood_prompt
        persona_config.mood_prompts_json = json.dumps(current_moods)
        flag_modified(persona_config, "mood_prompts_json") # Important for JSON modification detection
        db.commit()
        db.refresh(persona_config)

        # Update context persona object
        context.user_data['persona_object'] = Persona(persona_config)

        logger.info(f"User {update.effective_user.id} updated mood '{mood_name}' for persona {persona_id}.")
        await update.message.reply_text(f"✅ настроение '{mood_name}' обновлено!")

    # Go back to moods menu
    return await edit_moods_menu(update, context)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    mood_name = context.user_data.get('delete_mood_name')
    persona: Persona = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')

    if not mood_name or not persona or not persona_id or not data.endswith(mood_name): # Basic check
        await query.edit_message_text("ошибка: неверные данные для удаления.")
        return await edit_moods_menu(update, context) # Go back to menu

    with SessionLocal() as db:
        # Fetch config within session
        persona_config = db.query(PersonaConfig).get(persona_id)
        if not persona_config or persona_config.owner_id != query.from_user.id:
            await query.edit_message_text("ошибка доступа к личности.")
            return ConversationHandler.END

        current_moods = json.loads(persona_config.mood_prompts_json or '{}')
        if mood_name in current_moods:
            del current_moods[mood_name]
            persona_config.mood_prompts_json = json.dumps(current_moods)
            flag_modified(persona_config, "mood_prompts_json")
            db.commit()
            db.refresh(persona_config)
            # Update context persona object
            context.user_data['persona_object'] = Persona(persona_config)
            logger.info(f"User {query.from_user.id} deleted mood '{mood_name}' for persona {persona_id}.")
            await query.edit_message_text(f"🗑️ настроение '{mood_name}' удалено.")
        else:
            await query.edit_message_text(f"настроение '{mood_name}' не найдено.")


    # Go back to moods menu
    return await edit_moods_menu(update, context)


async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("редактирование отменено.")
    else:
        await update.message.reply_text("редактирование отменено.")
    context.user_data.pop('edit_persona_id', None)
    context.user_data.pop('persona_object', None)
    context.user_data.pop('edit_field', None)
    context.user_data.pop('edit_mood_name', None)
    context.user_data.pop('delete_mood_name', None)
    return ConversationHandler.END

# --- Delete Persona ---
async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"Command /deletepersona from user {user_id} with args: {args}")

    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/deletepersona <id>`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    persona_id = int(args[0])
    context.user_data['delete_persona_id'] = persona_id

    with SessionLocal() as db:
        user = get_or_create_user(db, update.effective_user.id, update.effective_user.username)
        persona_config = get_persona_by_id_and_owner(db, user.id, persona_id)

        if not persona_config:
            await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не твоя.", parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

        keyboard = [
             [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{persona_config.name}' БЕЗВОЗВРАТНО ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
             [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
         ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🚨 **ВНИМАНИЕ!** 🚨\n"
            f"ты уверен, что хочешь удалить личность **'{persona_config.name}'** (id: `{persona_id}`)?\n\n"
            f"это действие **НЕОБРАТИМО**! все связанные с ней данные (контексты, настройки) будут удалены.",
            reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )

    return DELETE_PERSONA_CONFIRM


async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    if not persona_id or not data.endswith(str(persona_id)):
         await query.edit_message_text("ошибка: неверные данные для удаления.")
         return ConversationHandler.END

    with SessionLocal() as db:
         persona_to_delete = get_persona_by_id_and_owner(db, user_id, persona_id)
         if persona_to_delete:
             persona_name = persona_to_delete.name
             if delete_persona_config(db, persona_id, user_id): # Uses the function with owner_id check
                 logger.info(f"User {user_id} deleted persona {persona_id} ('{persona_name}').")
                 await query.edit_message_text(f"✅ личность '{persona_name}' (id: {persona_id}) успешно удалена.")
             else:
                 logger.error(f"Failed to delete persona {persona_id} for user {user_id} despite initial check.")
                 await query.edit_message_text("❌ не удалось удалить личность.")
         else:
             await query.edit_message_text(f"❌ личность с id `{persona_id}` уже удалена или не найдена.", parse_mode=ParseMode.MARKDOWN)


    context.user_data.pop('delete_persona_id', None)
    return ConversationHandler.END


async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("удаление отменено.")
    context.user_data.pop('delete_persona_id', None)
    return ConversationHandler.END
