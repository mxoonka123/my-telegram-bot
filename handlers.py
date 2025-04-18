import logging
import httpx
import random
import asyncio
import re
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
    """Обработчик ошибок."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Можно добавить отправку сообщения администратору или пользователю при критических ошибках
    # if isinstance(update, Update):
    #     try:
    #         await update.effective_chat.send_message("ой, кажется, что-то пошло не так. я уже разбираюсь!")
    #     except Exception as e:
    #         logger.error(f"Failed to send error message to chat: {e}")


def get_persona_and_context(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]]]]:
    """
    Получает активный экземпляр бота для чата, его персону и текущий контекст из БД.
    Возвращает кортеж (Persona, список_сообщений_контекста) или None.
    """
    chat_instance = get_chat_bot_instance(db, chat_id)
    if not chat_instance or not chat_instance.active:
        #logger.debug(f"No active chat_bot_instance found for chat_id {chat_id}")
        return None

    # Убедимся, что связаны BotInstance и PersonaConfig
    if not chat_instance.bot_instance_ref or not chat_instance.bot_instance_ref.persona_config:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance or PersonaConfig.")
         # Возможно, стоит деактивировать такую связку, или уведомить админа
         return None

    persona_config = chat_instance.bot_instance_ref.persona_config

    persona = Persona(persona_config, chat_instance)
    context = get_context_for_chat_bot(db, chat_instance.id)
    # logger.debug(f"Found active persona '{persona.name}' for chat {chat_id} with {len(context)} context messages.")
    return persona, context


async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Отправляет запрос в Langdock API и возвращает текст ответа."""
    if not LANGDOCK_API_KEY:
        logger.error("LANGDOCK_API_KEY is not set. Cannot send request to Langdock.")
        return "" # Возвращаем пустую строку, если ключ не установлен

    headers = {
        "Authorization": f"Bearer {LANGDOCK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LANGDOCK_MODEL,
        "system": system_prompt,
        "messages": messages,
        "max_tokens": 1024, # Увеличено до 1024 для более длинных ответов
        "temperature": 0.7,
        "top_p": 0.95,
        "stream": False # Пока не используем стриминг
    }
    url = f"{LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock URL: {url}")
    # logger.debug(f"Langdock Payload: {payload}") # Осторожно, может содержать чувствительную информацию
    try:
        async with httpx.AsyncClient(http2=False) as client:
             resp = await client.post(url, json=payload, headers=headers, timeout=90) # Увеличил таймаут
        resp.raise_for_status() # Вызовет исключение для плохих статусов (4xx, 5xx)
        data = resp.json()

        # Langdock API возвращает ответ в поле 'content' как список dict'ов
        if "content" in data and isinstance(data["content"], list):
            # Извлекаем только текстовые части
            full_response = " ".join([part.get("text", "") for part in data["content"] if part.get("type") == "text"])
            logger.debug(f"Received text from Langdock: {full_response[:200]}...") # Логируем начало ответа
            return full_response.strip()

        # На случай, если формат ответа изменится или отличается
        logger.warning(f"Langdock response format unexpected: {data}. Attempting to get 'response' field.")
        response_text = data.get("response") or "" # Fallback к старому формату или другому полю
        logger.debug(f"Received fallback response from Langdock: {response_text[:200]}...")
        return response_text.strip()

    except httpx.HTTPStatusError as e:
        logger.error(f"Langdock API HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True)
        raise # Переподнимаем исключение, чтобы его обработал error_handler
    except httpx.RequestError as e:
        logger.error(f"Langdock API request error: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error communicating with Langdock: {e}", exc_info=True)
        raise


async def process_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str, persona: Persona, full_bot_response_text: str, db: Session):
    """
    Обрабатывает полученный от Langdock текст, извлекает гифки, разбивает на части
    и отправляет сообщения в чат.
    """
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"Received empty response from AI for chat {chat_id}, persona {persona.name}. Not sending anything.")
        return

    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}")

    # Сохраняем полный ответ AI в контекст (если он не пустой)
    add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
    logger.debug("AI response added to database context.")

    # Извлекаем гифки ПЕРЕД обработкой текста
    all_text_content = full_bot_response_text.strip()
    gif_links = extract_gif_links(all_text_content)

    # Удаляем ссылки на гифки из текста для дальнейшей отправки как текста
    for gif in gif_links:
        # Используем re.escape для безопасного удаления ссылок, содержащих спецсимволы
        all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()
    logger.debug(f"Extracted {len(gif_links)} gif links. Remaining text: {all_text_content[:200]}...")

    # Разбиваем оставшийся текст на части для отправки
    # Используем postprocess_response из utils
    text_parts_to_send = postprocess_response(all_text_content)
    logger.debug(f"Postprocessed text into {len(text_parts_to_send)} parts.")

    # Отправляем гифки первыми
    for gif in gif_links:
        try:
            await context.bot.send_animation(chat_id=chat_id, animation=gif)
            logger.info(f"Sent gif: {gif}")
            await asyncio.sleep(random.uniform(1.5, 3.0)) # Небольшая пауза между гифками
        except Exception as e:
            logger.error(f"Error sending gif {gif}: {e}", exc_info=True)
            # Попытка отправить как фото или документ, если анимация не работает
            try:
                 await context.bot.send_photo(chat_id=chat_id, photo=gif)
                 logger.warning(f"Sent gif {gif} as photo after animation failure.")
            except Exception:
                 try:
                      await context.bot.send_document(chat_id=chat_id, document=gif)
                      logger.warning(f"Sent gif {gif} as document after photo failure.")
                 except Exception as e2:
                      logger.error(f"Failed to send gif {gif} as photo or document: {e2}")


    # Отправляем текстовые части с задержкой
    if text_parts_to_send:
        for i, part in enumerate(text_parts_to_send):
            if update.effective_chat.type in ["group", "supergroup"]:
                # Опционально: отправляем статус "typing" только перед текстовыми сообщениями
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                # Пауза перед отправкой текста, имитируя набор
                await asyncio.sleep(random.uniform(1.5, 3.0) + len(part) / 50) # Пауза зависит от длины текста

            if part.strip(): # Убедимся, что часть не пустая после strip
                 try:
                     await context.bot.send_message(chat_id=chat_id, text=part.strip())
                     logger.info(f"Sent text part: {part.strip()[:100]}...") # Логируем начало отправленного текста
                 except Exception as e:
                     logger.error(f"Error sending text part: {e}", exc_info=True)

            # Пауза между текстовыми сообщениями, если их несколько
            if i < len(text_parts_to_send) - 1:
                await asyncio.sleep(random.uniform(1.0, 2.5)) # Пауза между частями ответа


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    message_text = update.message.text

    logger.info(f"Received text message from user {user_id} ({username}) in chat {chat_id}: {message_text}")

    # Используем контекстный менеджер для сессии БД
    with SessionLocal() as db:
        persona_context_tuple = get_persona_and_context(chat_id, db)

        # Если нет активной личности в этом чате, игнорируем сообщение
        if not persona_context_tuple:
            #logger.debug(f"No active persona/bot instance for chat {chat_id}. Ignoring text message.")
            return

        persona, current_context_list = persona_context_tuple
        logger.debug(f"Handling message for persona '{persona.name}' in chat {chat_id}.")

        # Проверяем, не является ли сообщение командой смены настроения
        # Это обрабатывается здесь, чтобы пользователь мог использовать просто название настроения
        # (например, "радость"), а не только команду /mood
        if message_text and message_text.lower() in persona.get_all_mood_names():
             logger.info(f"Message '{message_text}' matched a mood name. Attempting to change mood.")
             # Создаем имитацию объекта update.message для функции mood, если она ожидает update
             # Или передаем данные напрямую, как сейчас реализовано в mood, передавая db и persona
             await mood(update, context, db=db, persona=persona) # Передаем сессию и персону
             return # Завершаем обработку сообщения, если это была команда смены настроения

        # Логика решения, отвечать ли в группе
        if update.effective_chat.type in ["group", "supergroup"]:
            if persona.should_respond_prompt_template:
                should_respond_prompt = persona.format_should_respond_prompt(message_text)
                try:
                    # Используем только последнее сообщение для принятия решения
                    decision_response = await send_to_langdock(
                        system_prompt=should_respond_prompt,
                        messages=[{"role": "user", "content": f"Сообщение в чате: {message_text}"}]
                    )
                    answer = decision_response.strip().lower()

                    # Проверяем ответ AI. Если не 'да', с некоторой вероятностью все равно отвечаем
                    if not answer.startswith("д") and random.random() > 0.9: # 10% шанс ответить, даже если AI сказал "нет"
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond anyway (random chance).")
                        # Продолжаем выполнение, чтобы сгенерировать ответ
                    elif answer.startswith("д"):
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding to respond.")
                        # Продолжаем выполнение
                    else:
                        logger.info(f"Chat {chat_id}, Persona {persona.name}: should_respond AI='{answer}', deciding NOT to respond.")
                        return # Прерываем выполнение, не отвечаем

                except Exception as e:
                     logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                     # В случае ошибки в should_respond, на всякий случай решаем ответить, чтобы не молчать
                     logger.warning("Error in should_respond. Defaulting to respond.")
                     pass # Продолжаем выполнение, чтобы сгенерировать ответ
            else:
                 # Если шаблона should_respond нет, всегда пытаемся ответить в группе
                 logger.debug(f"Persona {persona.name} in chat {chat_id} has no should_respond template. Attempting to respond.")
                 pass


        # Добавляем сообщение пользователя в контекст перед отправкой в AI
        add_message_to_context(db, persona.chat_instance.id, "user", message_text)
        # Обновляем локальный список контекста для текущего запроса к AI
        # Ограничиваем контекст для отправки в AI, чтобы не превышать лимиты и улучшить релевантность
        context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id) # Перечитываем контекст после добавления нового сообщения
        logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")


        try:
            # Формируем системный промпт для основного ответа
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            logger.debug("Formatted main system prompt.")

            # Отправляем контекст (добавленное сообщение пользователя включено) в AI
            # AI получит system prompt и список сообщений
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug("Received response from Langdock for main message.")

            # Обрабатываем и отправляем ответ
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
            # Можно добавить уведомление пользователю об ошибке генерации ответа
            # try:
            #     await update.message.reply_text("ой, я не смог сгенерировать ответ :(")
            # except Exception as send_e:
            #      logger.error(f"Failed to send error message to user: {send_e}")


# Helper function to handle media (photo, voice) processing
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Обрабатывает фото или голосовые сообщения."""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with SessionLocal() as db:
        persona_context_tuple = get_persona_and_context(chat_id, db)
        if not persona_context_tuple:
            #logger.debug(f"No active persona/bot instance for chat {chat_id}. Ignoring {media_type} message.")
            return
        persona, current_context_list = persona_context_tuple
        logger.debug(f"Handling {media_type} for persona '{persona.name}' in chat {chat_id}.")

        # Выбираем шаблон промпта в зависимости от типа медиа
        prompt_template = None
        context_text = ""
        if media_type == "photo":
            prompt_template = persona.photo_prompt_template
            context_text = "прислали фотографию."
        elif media_type == "voice":
            prompt_template = persona.voice_prompt_template
            context_text = "прислали голосовое сообщение."
        # Можно добавить другие типы медиа здесь (video, document etc.)

        if not prompt_template:
            logger.info(f"Persona {persona.name} in chat {chat_id} does not have a {media_type} prompt template. Skipping.")
            return

        # Добавляем информацию о полученном медиа в контекст
        add_message_to_context(db, persona.chat_instance.id, "user", context_text)
        # Обновляем локальный список контекста для текущего запроса к AI
        context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
        logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")

        try:
            # Формируем системный промпт для ответа на медиа
            system_prompt = ""
            if media_type == "photo":
                system_prompt = persona.format_photo_prompt()
            elif media_type == "voice":
                system_prompt = persona.format_voice_prompt()
            # Добавить форматирование для других типов медиа

            logger.debug(f"Formatted {media_type} system prompt.")

            # Отправляем контекст в AI
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}.")

            # Обрабатываем и отправляем ответ
            await process_and_send_response(update, context, chat_id, persona, response_text, db)

        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
            # Уведомление об ошибке
            # try:
            #     await update.message.reply_text(f"ой, я не смог обработать {media_type}. :(")
            # except Exception as send_e:
            #      logger.error(f"Failed to send error message to user: {send_e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик фото сообщений."""
    await handle_media(update, context, "photo")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик голосовых сообщений."""
    await handle_media(update, context, "voice")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
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
                 f"привет! я {persona.name}. я уже настроен в этом чате.\n"
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
    """Обработчик команды /help."""
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
    """Обработчик команды /mood или смены настроения по тексту."""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /mood or mood text command from user {user_id} ({username}) in chat {chat_id}")

    # Если функция вызвана из handle_message с уже открытой сессией и персоной
    if db is not None and persona is not None:
        chat_bot_instance = persona.chat_instance # Используем переданную персону
        # В этом случае message_text уже проверен как название настроения в handle_message
        mood_arg = update.message.text.lower()
        available_moods = persona.get_all_mood_names()

        if mood_arg in available_moods:
             set_mood_for_chat_bot(db, chat_bot_instance.id, mood_arg)
             await update.message.reply_text(f"настроение для '{persona.name}' теперь: {mood_arg}", reply_markup=ReplyKeyboardRemove())
             logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {mood_arg} via text command.")
        # Если mood_arg не найден в available_moods, это не должно произойти при вызове из handle_message
        # после проверки, но на всякий случай можно добавить лог или игнорировать.
        return

    # Если функция вызвана напрямую как команда /mood
    db = SessionLocal() # Открываем новую сессию
    try:
        chat_bot_instance = get_chat_bot_instance(db, chat_id)
        if not chat_bot_instance or not chat_bot_instance.active:
            await update.message.reply_text("в этом чате нет активной личности, для которой можно менять настроение :(", reply_markup=ReplyKeyboardRemove())
            logger.debug(f"No active persona/bot instance for chat {chat_id}. Cannot set mood.")
            return

        persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)

        # Получаем доступные настроения из конфига персоны или используем дефолтные
        available_moods = persona.get_all_mood_names()
        if not available_moods:
             logger.warning(f"Persona {persona.name} has no custom moods defined. Using default moods.")
             available_moods = list(DEFAULT_MOOD_PROMPTS.keys())
             if not available_moods:
                  await update.message.reply_text(f"у личности '{persona.name}' не настроены настроения :(")
                  logger.error(f"Persona {persona.name} and DEFAULT_MOOD_PROMPTS are empty.")
                  return

        # Если команда /mood без аргументов - показываем кнопки
        if not context.args:
            keyboard = [[InlineKeyboardButton(mood_name.capitalize(), callback_data=f"set_mood_{mood_name}")] for mood_name in available_moods]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"выберите настроение для '{persona.name}':",
                reply_markup=reply_markup
            )
            logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")
            return

        # Если команда /mood с аргументом - пытаемся установить настроение
        mood_arg = context.args[0].lower()
        if mood_arg in available_moods:
             set_mood_for_chat_bot(db, chat_bot_instance.id, mood_arg)
             await update.message.reply_text(f"настроение для '{persona.name}' теперь: {mood_arg}", reply_markup=ReplyKeyboardRemove())
             logger.info(f"Mood for persona {persona.name} in chat {chat_id} set to {mood_arg} via command argument.")
        else:
             # Если аргумент не совпал с названием настроения - предлагаем выбрать из списка
             keyboard = [[InlineKeyboardButton(mood_name.capitalize(), callback_data=f"set_mood_{mood_name}")] for mood_name in available_moods]
             reply_markup = InlineKeyboardMarkup(keyboard)
             await update.message.reply_text(
                 f"не знаю такого настроения '{mood_arg}' для личности '{persona.name}'. выберите из списка:",
                 reply_markup=reply_markup
             )
             logger.debug(f"Invalid mood argument '{mood_arg}' for chat {chat_id}. Sent mood selection keyboard.")


    finally:
        # Закрываем сессию только если открыли ее в этой функции
        if 'db' in locals() and db is not None and not (db is not None and persona is not None): # Условие для закрытия, если не передано извне
             db.close()


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /reset."""
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    logger.info(f"Command /reset from user {user_id} ({username}) in chat {chat_id}")


    with SessionLocal() as db:
        chat_bot_instance = get_chat_bot_instance(db, chat_id)
        if not chat_bot_instance or not chat_bot_instance.active:
            await update.message.reply_text("в этом чате нет активной личности, для которой можно очистить память :(", reply_markup=ReplyKeyboardRemove())
            logger.debug(f"No active persona/bot instance for chat {chat_id}. Cannot reset context.")
            return

        try:
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='auto')
            db.commit()
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}.")
            # Получаем персону для сообщения пользователю
            persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
            db.rollback()
            logger.error(f"Error resetting context for chat_bot_instance {chat_bot_instance.id} in chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("не удалось очистить память :(", reply_markup=ReplyKeyboardRemove())


async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /createpersona."""
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
    persona_description = " ".join(args[1:]) if len(args) > 1 else f"ты ai бот по имени {persona_name}." # Дефолтное описание, если не указано

    if len(persona_name) < 3 or len(persona_name) > 50:
         await update.message.reply_text("имя личности должно быть от 3 до 50 символов.", reply_markup=ReplyKeyboardRemove())
         logger.warning(f"User {user_id} provided invalid persona name length: '{persona_name}'")
         return
    if len(persona_description) > 500: # Ограничение на длину описания, чтобы не перегружать промпт
         await update.message.reply_text("описание личности не должно превышать 500 символов.", reply_markup=ReplyKeyboardRemove())
         logger.warning(f"User {user_id} provided persona description exceeding 500 characters.")
         return


    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)

        # Проверяем, существует ли уже персона с таким именем у этого пользователя
        existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
        if existing_persona:
            await update.message.reply_text(f"у вас уже есть личность с именем '{persona_name}'. выберите другое имя.", reply_markup=ReplyKeyboardRemove())
            logger.warning(f"User {user_id} attempted to create persona with existing name: '{persona_name}'")
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
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")
        except IntegrityError:
             db.rollback()
             # Этот IntegrityError маловероятен после проверки выше, но лучше оставить
             logger.error(f"IntegrityError creating persona for user {user_id} with name '{persona_name}': already exists (DB constraint).", exc_info=True)
             await update.message.reply_text(f"ошибка: личность с именем '{persona_name}' уже существует. выберите другое имя.", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
             db.rollback()
             logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
             await update.message.reply_text("произошла ошибка при создании личности :(", reply_markup=ReplyKeyboardRemove())


async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /mypersonas."""
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
            logger.debug(f"User {user_id} has no personas.")
            return

        response_text = "ваши личности:\n\n"
        for persona in personas:
            response_text += f"**имя:** {persona.name}\n"
            response_text += f"**id:** `{persona.id}`\n"
            response_text += f"**описание:** {persona.description if persona.description else 'нет описания'}\n"
            response_text += "---\n"

        await update.message.reply_text(response_text, parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
        logger.info(f"User {user_id} requested mypersonas. Sent {len(personas)} personas.")


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /addbot."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"
    chat_id = str(update.effective_chat.id)
    chat_title = update.effective_chat.title or chat_id # Получаем название чата, если это группа
    logger.info(f"Command /addbot from user {user_id} ({username}) in chat {chat_id} ('{chat_title}') with args: {context.args}")

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
        logger.warning(f"User {user_id} provided invalid persona ID '{args[0]}' for /addbot in chat {chat_id}.")
        await update.message.reply_text("неверный id личности. id должен быть числом.", reply_markup=ReplyKeyboardRemove())
        return

    with SessionLocal() as db:
        user = get_or_create_user(db, user_id, username)

        # Проверяем, существует ли личность с таким ID и принадлежит ли она этому пользователю
        persona = get_persona_by_id_and_owner(db, user.id, persona_id)
        if not persona:
             logger.warning(f"User {user_id} attempted to add persona with ID {persona_id} which was not found or not owned in chat {chat_id}.")
             await update.message.reply_text(f"личность с id `{persona_id}` не найдена или не принадлежит вам. проверьте /mypersonas.", parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
             return

        # Ищем существующий экземпляр бота для этой персоны и этого пользователя
        # Если не найдено, создаем новый
        bot_instance = db.query(BotInstance).filter(
            BotInstance.owner_id == user.id,
            BotInstance.persona_config_id == persona.id
        ).first()

        if not bot_instance:
             bot_instance = create_bot_instance(db, user.id, persona.id, name=f"Экземпляр {persona.name} для пользователя {username}")
             logger.info(f"Created BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")
        else:
             logger.info(f"Found existing BotInstance {bot_instance.id} for user {user.id}, persona {persona.id}")

        # Связываем экземпляр бота с текущим чатом
        try:
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)

            # Очищаем старый контекст при первой активации в этом чате или повторной активации
            # Это гарантирует, что бот начинает новый диалог с этой личностью в этом чате
            db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='auto')
            db.commit()
            logger.debug(f"Cleared old context for chat_bot_instance {chat_link.id} upon linking.")


            await update.message.reply_text(
                f"личность '{persona.name}' (id: `{persona.id}`) активирована в этом чате!",
                parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"Linked BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")

        except IntegrityError:
             db.rollback()
             # Эта ошибка возникает, если ChatBotInstance с таким chat_id и bot_instance_id уже существует (даже если active=False)
             logger.warning(f"Attempted to link BotInstance {bot_instance.id} (Persona {persona.id}) to chat {chat_id} ('{chat_title}'), but link already exists (IntegrityError).")
             # В этом случае, link_bot_instance_to_chat уже обновила active=True, но можно уведомить пользователя
             await update.message.reply_text("эта личность уже активна в этом чате.", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
             db.rollback()
             logger.error(f"Error linking bot instance {bot_instance.id} to chat {chat_id} ('{chat_title}'): {e}", exc_info=True)
             await update.message.reply_text("произошла ошибка при активации личности в чате :(", reply_markup=ReplyKeyboardRemove())


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на Inline кнопки (например, для смены настроения)."""
    query = update.callback_query
    # Always answer the callback query to remove the loading indicator
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
                # Пробуем отредактировать сообщение, если оно еще доступно
                try:
                    await query.edit_message_text("в этом чате нет активной личности :(")
                except Exception:
                    # Если не получилось отредактировать, отправляем новое
                    await context.bot.send_message(chat_id=chat_id, text="в этом чате нет активной личности :(")

                logger.debug(f"Callback query received for inactive chat_bot_instance in chat {chat_id}.")
                return

            persona = Persona(chat_bot_instance.bot_instance_ref.persona_config, chat_bot_instance)

            # Получаем доступные настроения (из конфига или дефолтные)
            available_moods = persona.get_all_mood_names()
            if not available_moods:
                 available_moods = list(DEFAULT_MOOD_PROMPTS.keys())
                 if not available_moods:
                      await query.edit_message_text(f"у личности '{persona.name}' не настроены настроения :(")
                      logger.error(f"Persona {persona.name} and DEFAULT_MOOD_PROMPTS are empty for mood callback.")
                      return

            if mood_name in available_moods:
                 set_mood_for_chat_bot(db, chat_bot_instance.id, mood_name)
                 await query.edit_message_text(f"настроение для '{persona.name}' теперь: {mood_name}")
                 logger.info(f"User {user_id} set mood for persona {persona.name} in chat {chat_id} to {mood_name} via callback.")
            else:
                 await query.edit_message_text(f"неверное настроение: {mood_name}")
                 logger.warning(f"User {user_id} attempted to set unknown mood '{mood_name}' via callback in chat {chat_id}.")
