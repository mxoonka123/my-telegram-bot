import logging
import httpx
import random
import asyncio
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List, Dict, Any, Optional, Union, Tuple

from config import (
    LANGDOCK_API_KEY, LANGDOCK_BASE_URL, LANGDOCK_MODEL,
    DEFAULT_MOOD_PROMPTS
)
from db import (
    get_chat_bot_instance, get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner,
    create_bot_instance, link_bot_instance_to_chat, get_bot_instance_by_id,
    SessionLocal,
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


def get_persona_and_context(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]]]]:
    chat_instance = get_chat_bot_instance(db, chat_id)
    if not chat_instance or not chat_instance.active:
        return None

    persona_config = chat_instance.bot_instance_ref.persona_config

    if not persona_config:
         logger.error(f"No persona config found for bot instance {chat_instance.bot_instance_id} linked to chat {chat_id}")
         return None

    persona = Persona(persona_config, chat_instance)
    context = get_context_for_chat_bot(db, chat_instance.id)
    return persona, context


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
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
    try:
        async with httpx.AsyncClient(http2=False) as client: # <-- ДОБАВЛЕНО http2=False
             resp = await client.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        if "content" in data and isinstance(data["content"], list):
            full_response = " ".join([part.get("text", "") for part in data["content"] if part.get("type") == "text"])
            return full_response
        return data.get("response") or ""
    except httpx.HTTPStatusError as e:
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True)
        raise
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        raise


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    message_text = update.message.text

    logger.info(f"Received text message from user {user_id} ({username}) in chat {chat_id}: {message_text}")

    with SessionLocal() as db:
        persona_context_tuple = get_persona_and_context(chat_id, db)

        if not persona_context_tuple:
            return

        persona, current_context_list = persona_context_tuple

        if message_text.lower() in persona.get_all_mood_names():
             await mood(update, context, db=db, persona=persona)
             return

        if update.effective_chat.type in ["group", "supergroup"]:
            if not persona.should_respond_prompt_template:
                 pass
            else:
                should_respond_prompt = persona.format_should_respond_prompt(message_text)
                try:
                    decision_response = await send_to_langdock(
                        system_prompt=should_respond_prompt,
                        messages=[{"role": "user", "content": f"Сообщение в чате: {message_text}"}]
                    )
                    answer = decision_response.strip().lower()

                    if not answer.startswith("д") and random.random() > 0.9:
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond anyway.")
                        pass
                    elif answer.startswith("д"):
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond.")
                        pass
                    else:
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding NOT to respond.")
                        return
                except Exception as e:
                     logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                     pass


        add_message_to_context(db, persona.chat_instance.id, "user", message_text)
        current_context_list.append({"role": "user", "content": message_text})

        try:
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            response_text = await send_to_langdock(system_prompt, current_context_list[-200:])

            if not response_text:
                logger.warning(f"Langdock returned empty response for chat {chat_id}, persona {persona.name}")
                return

            full_bot_response_text = response_text
            responses_parts = postprocess_response(full_bot_response_text)
            all_text_content = " ".join(responses_parts)
            gif_links = extract_gif_links(all_text_content)

            for gif in gif_links:
                try:
                    await context.bot.send_animation(chat_id=chat_id, animation=gif)
                    all_text_content = all_text_content.replace(gif, "").strip()
                except Exception as e:
                    logger.error(f"Ошибка отправки гифки {gif}: {e}", exc_info=True)

            text_parts_to_send = [part.strip() for part in re.split(r'(?<=[.!?…])\s+', all_text_content) if part.strip()]

            if full_bot_response_text.strip():
                 add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())

            for i, part in enumerate(text_parts_to_send):
                if update.effective_chat.type in ["group", "supergroup"]:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.2, 2.5) + len(part) / 40)
                if part:
                    await update.message.reply_text(part)
                if i < len(text_parts_to_send) - 1:
                    await asyncio.sleep(random.uniform(0.7, 2.2))

        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Received photo message from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        persona_context_tuple = get_persona_and_context(chat_id, db)
        if not persona_context_tuple:
            return
        persona, current_context_list = persona_context_tuple

        if not persona.photo_prompt_template:
            logger.info(f"Persona {persona.name} in chat {chat_id} does not have a photo prompt template. Skipping.")
            return

        try:
            context_text = "прислали фотографию."
            logger.info(f"Received photo message in chat {chat_id}.")

        except Exception as e:
            logger.error(f"Error getting photo file info in chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("не получилось обработать фото :(")
            return

        add_message_to_context(db, persona.chat_instance.id, "user", context_text)
        current_context_list.append({"role": "user", "content": context_text})

        try:
            system_prompt = persona.format_photo_prompt()
            response_text = await send_to_langdock(system_prompt, current_context_list[-200:])

            if not response_text:
                 logger.warning(f"Langdock returned empty response for photo in chat {chat_id}, persona {persona.name}")
                 return

            full_bot_response_text = response_text
            responses_parts = postprocess_response(full_bot_response_text)
            all_text_content = " ".join(responses_parts)
            gif_links = extract_gif_links(all_text_content)

            for gif in gif_links:
                try:
                    await context.bot.send_animation(chat_id=chat_id, animation=gif)
                    all_text_content = all_text_content.replace(gif, "").strip()
                except Exception as e:
                    logger.error(f"Ошибка отправки гифки {gif}: {e}", exc_info=True)

            text_parts_to_send = [part.strip() for part in re.split(r'(?<=[.!?…])\s+', all_text_content) if part.strip()]

            if full_bot_response_text.strip():
                 add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())

            for i, part in enumerate(text_parts_to_send):
                if update.effective_chat.type in ["group", "supergroup"]:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.2, 2.5) + len(part) / 40)
                if part:
                    await update.message.reply_text(part)
                if i < len(text_parts_to_send) - 1:
                    await asyncio.sleep(random.uniform(0.7, 2.2))

        except Exception as e:
            logger.error(f"General error processing photo in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Received voice message from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        persona_context_tuple = get_persona_and_context(chat_id, db)
        if not persona_context_tuple:
            return
        persona, current_context_list = persona_context_tuple

        if not persona.voice_prompt_template:
            logger.info(f"Persona {persona.name} in chat {chat_id} does not have a voice prompt template. Skipping.")
            return

        try:
            context_text = "прислали голосовое сообщение."
            logger.info(f"Received voice message in chat {chat_id}.")

        except Exception as e:
            logger.error(f"Error getting voice file info in chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("не получилось обработать голосовое :(")
            return

        add_message_to_context(db, persona.chat_instance.id, "user", context_text)
        current_context_list.append({"role": "user", "content": context_text})

        try:
            system_prompt = persona.format_voice_prompt()
            response_text = await send_to_langdock(system_prompt, current_context_list[-200:])

            if not response_text:
                 logger.warning(f"Langdock returned empty response for voice in chat {chat_id}, persona {persona.name}")
                 return

            full_bot_response_text = response_text
            responses_parts = postprocess_response(full_bot_response_text)
            all_text_content = " ".join(responses_parts)
            gif_links = extract_gif_links(all_text_content)

            for gif in gif_links:
                try:
                    await context.bot.send_animation(chat_id=chat_id, animation=gif)
                    all_text_content = all_text_content.replace(gif, "").strip()
                except Exception as e:
                    logger.error(f"Ошибка отправки гифки {gif}: {e}", exc_info=True)

            text_parts_to_send = [part.strip() for part in re.split(r'(?<=[.!?…])\s+', all_text_content) if part.strip()]

            if full_bot_response_text.strip():
                 add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())

            for i, part in enumerate(text_parts_to_send):
                if update.effective_chat.type in ["group", "supergroup"]:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.2, 2.5) + len(part) / 40)
                if part:
                    await update.message.reply_text(part)
                if i < len(text_parts_to_send) - 1:
                    await asyncio.sleep(random.uniform(0.7, 2.2))

        except Exception as e:
            logger.error(f"General error processing voice in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Command /start from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)
        persona_context_tuple = get_persona_and_context(chat_id, db)

        if persona_context_tuple:
             persona, _ = persona_context_tuple
             await update.message.reply_text(
                 f"привет! я {persona.name}. Я уже настроен в этом чате.\n"
                 "чтобы узнать, что я умею, используй команду /help.",
                 parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
             )
        else:
             await update.message.reply_text(
                 "привет! 👋 я ai бот, который может стать твоим уникальным собеседником в чате.\n\n"
                 "**как меня настроить:**\n"
                 "1. **создай личность (персону):** `/createpersona <имя> [описание]`\n"
                 "   _например: /createpersona Саша Я подросток из новосибирска_\n"
                 "   ты получишь уникальный ID для своей личности.\n\n"
                 "2. **посмотри свои личности:** `/mypersonas`\n"
                 "   увидишь список созданных тобой личностей и их ID.\n\n"
                 "3. **активируй личность в чате:** перейди в нужный чат (этот или другой групповой) и напиши `/addbot <id твоей личности>`\n"
                 "   _замени <id твоей личности> на id из команды /mypersonas._\n\n"
                 "после активации бот с выбранной тобой личностью начнет общаться в этом чате!\n\n"
                 "для справки по всем командам используй /help.",
                 parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
             )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    logger.info(f"Command /help from user {user_id} ({username}) in chat {chat_id}")

    help_text = (
        "**🤖 команды бота:**\n"
        "/start — начать взаимодействие и узнать как настроить бота\n"
        "/help — показать эту справку\n\n"
        "**👤 команды управления личностями (персонами):**\n"
        "/createpersona <имя> [описание] — создать новую личность. имя должно быть уникальным.\n"
        "   _описание поможет боту лучше понять, кем он должен быть._\n"
        "/mypersonas — показать список ваших личностей и их id.\n"
        "   _id нужен для активации личности в чате._\n"
        "/addbot <id персоны> — активировать бота с выбранной личностью в текущем чате.\n"
        "   _выполни в чате, где хочешь, чтобы бот общался._\n\n"
        "**💬 команды для активной личности (в чате):**\n"
        "_(работают только если в чате активирована личность)_\n"
        "/mood — выбрать настроение для текущей личности.\n"
        "/reset — очистить память (контекст диалога) для текущей личности в этом чате.\n\n"
        "боты с личностями отвечают на сообщения, комментируют фото и голосовые сообщения, и иногда пишут сами!"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /mood from user {user_id} ({username}) in chat {chat_id}")

    if db is None or persona is None:
        db = SessionLocal()
        try:
            chat_bot_instance = get_chat_bot_instance(db, chat_id)
            if not chat_bot_instance or not chat_bot_instance.active:
                await update.message.reply_text("в этом чате нет активной личности, для которой можно менять настроение :(", reply_markup=ReplyKeyboardRemove())
                return

            persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)

            available_moods = persona.get_all_mood_names()
            if not available_moods:
                 logger.warning(f"Persona {persona.name} has no custom moods. Using default moods.")
                 available_moods = list(DEFAULT_MOOD_PROMPTS.keys())
                 if not available_moods:
                      await update.message.reply_text(f"у личности '{persona.name}' не настроены настроения :(")
                      return


            if not context.args:
                keyboard = [[InlineKeyboardButton(mood_name.capitalize(), callback_data=f"set_mood_{mood_name}")] for mood_name in available_moods]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    f"выберите настроение для '{persona.name}':",
                    reply_markup=reply_markup
                )
                return

            mood_arg = context.args[0].lower()
            if mood_arg in available_moods:
                 set_mood_for_chat_bot(db, chat_bot_instance.id, mood_arg)
                 await update.message.reply_text(f"настроение для '{persona.name}' теперь: {mood_arg}", reply_markup=ReplyKeyboardRemove())
            else:
                 keyboard = [[InlineKeyboardButton(mood_name.capitalize(), callback_data=f"set_mood_{mood_name}")] for mood_name in available_moods]
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 await update.message.reply_text(
                     f"не знаю такого настроения '{mood_arg}' для личности '{persona.name}'. выберите из списка:",
                     reply_markup=reply_markup
                 )


        finally:
            if 'db' in locals() and db is not None and not (context.args is None and persona is not None):
                 db.close()

    else:
        mood_arg = update.message.text.lower()
        available_moods = persona.get_all_mood_names()
        if mood_arg in available_moods:
             set_mood_for_chat_bot(db, persona.chat_instance.id, mood_arg)
             await update.message.reply_text(f"настроение для '{persona.name}' теперь: {mood_arg}", reply_markup=ReplyKeyboardRemove())


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /reset from user {user_id} ({username}) in chat {chat_id}")


    with SessionLocal() as db:
        chat_bot_instance = get_chat_bot_instance(db, chat_id)
        if not chat_bot_instance or not chat_bot_instance.active:
            await update.message.reply_text("в этом чате нет активной личности, для которой можно очистить память :(", reply_markup=ReplyKeyboardRemove())
            return

        try:
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='auto')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}.")
            persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)
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
            "используйте команду в формате: `/createpersona <имя> [описание]`\n"
            "имя должно быть уникальным.\n"
            "_например: /createpersona Саша Я подросток из новосибирска_",
            parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
        )
        return

    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else f"ты ai бот по имени {persona_name}."

    if len(persona_name) < 3 or len(persona_name) > 50:
         await update.message.reply_text("имя личности должно быть от 3 до 50 символов.", reply_markup=ReplyKeyboardRemove())
         return

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)

        existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
        if existing_persona:
            await update.message.reply_text(f"у вас уже есть личность с именем '{persona_name}'. выберите другое имя.", reply_markup=ReplyKeyboardRemove())
            return

        try:
            new_persona = create_persona_config(db, user.id, persona_name, persona_description)
            await update.message.reply_text(
                f"личнось '{new_persona.name}' создана!\n"
                f"описание: {new_persona.description}\n"
                f"ее id: `{new_persona.id}` (используйте его для активации)\n"
                f"теперь вы можете добавить ее в чат командой /addbot `{new_persona.id}`",
                parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"User {user_id} created persona: {new_persona.name} (ID: {new_persona.id})")
        except IntegrityError:
             db.rollback()
             logger.error(f"IntegrityError creating persona for user {user_id} with name '{persona_name}': already exists.", exc_info=True)
             await update.message.reply_text(f"ошибка: личность с именем '{persona_name}' уже существует. выберите другое имя.", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
             db.rollback()
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("произошла ошибка при создании личности :(", reply_markup=ReplyKeyboardRemove())


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
                "у вас пока нет созданных личностей.\n"
                "создайте первую с помощью команды /createpersona <имя> [описание]",
                parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
            )
            return

        response_text = "ваши личности:\n\n"
        for persona in personas:
            response_text += f"**имя:** {persona.name}\n"
            response_text += f"**id:** `{persona.id}`\n"
            response_text += f"**описание:** {persona.description}\n"
            response_text += "---\n"

        await update.message.reply_text(response_text, parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    logger.info(f"Command /addbot from user {user_id} ({username}) in chat {chat_id} with args: {context.args}")

    args = context.args

    if not args or len(args) != 1:
        await update.message.reply_text(
            "используйте команду в формате: `/addbot <id персоны>`\n"
            "идентификатор личности (id) можно найти в списке ваших личностей (/mypersonas).\n"
            "выполните эту команду в чате, куда хотите добавить бота.",
            parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
        )
        return

    try:
        persona_id = int(args[0])
    except ValueError:
        logger.warning(f"User {user_id} provided invalid persona ID '{args[0]}' for /addbot.")
        await update.message.reply_text("неверный id личности. id должен быть числом.", reply_markup=ReplyKeyboardRemove())
        return

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)

        persona = get_persona_by_id_and_owner(db, user.id, persona_id)
        if not persona:
             logger.warning(f"User {user_id} attempted to add persona with ID {persona_id} which was not found or not owned.")
             await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не принадлежит вам. проверьте /mypersonas.", parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
             return

        bot_instance = db.query(BotInstance).filter(
            BotInstance.owner_id == user.id,
            BotInstance.persona_config_id == persona.id
        ).first()

        if not bot_instance:
             bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Экземпляр {persona.name}")
             logger.info(f"Created BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")
        else:
             logger.info(f"Found existing BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")

        try:
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            await update.message.reply_text(
                f"личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате!",
                parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id}. ChatBotInstance ID: {chat_link.id}")

        except IntegrityError:
             db.rollback()
             logger.warning(f"Attempted to link BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id}, but link already exists.")
             await update.message.reply_text("эта личность уже активна в этом чате.", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
             db.rollback()
             logger.error(f"Error linking bot instance {bot_instance.id} to chat {chat_id}: {e}", exc_info=True)
             await update.message.reply_text("произошла ошибка при активации личности в чате :(", reply_markup=ReplyKeyboardRemove())


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat.id)
    user_id = query.from_user.id
    username = query.from_user.username or "unknown"
    data = query.data

    logger.info(f"Received callback query from user {user_id} ({username}) in chat {chat_id} with data: {data}")

    if data.startswith("set_mood_"):
        mood_name = data.replace("set_mood_", "")
        with SessionLocal() as db:
            chat_bot_instance = get_chat_bot_instance(db, chat_id)
            if not chat_bot_instance or not chat_bot_instance.active:
                await query.edit_message_text("в этом чате нет активной личности :(")
                return

            persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)
            available_moods = persona.get_all_mood_names()
            if not available_moods:
                 available_moods = list(DEFAULT_MOOD_PROMPTS.keys())
                 if not available_moods:
                      await query.edit_message_text(f"у личности '{persona.name}' не настроены настроения :(")
                      return

            if mood_name in available_moods:
                 set_mood_for_chat_bot(db, chat_bot_instance.id, mood_name)
                 await query.edit_message_text(f"настроение для '{persona.name}' теперь: {mood_name}")
                 logger.info(f"User {user_id} set mood for persona {persona.name} in chat {chat_id} to {mood_name}")
            else:
                 await query.edit_message_text(f"неверное настроение: {mood_name}")