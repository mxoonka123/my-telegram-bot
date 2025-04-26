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
    get_db, get_active_chat_bot_instance_with_relations, # <<<--- ДОБАВЛЕН ИМПОРТ
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
""".format(subscription_duration=SUBSCRIPTION_DURATION_DAYS, subscription_price=f"{SUBSCRIPTION_PRICE_RUB:.0f}", subscription_currency=SUBSCRIPTION_CURRENCY)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("упс... что-то пошло не так. попробуй еще раз позже.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id) # Function is now imported
    if not chat_instance:
        logger.debug(f"No active chatbot instance found for chat {chat_id}")
        return None

    bot_instance = chat_instance.bot_instance_ref
    if not bot_instance or not bot_instance.persona_config or not bot_instance.owner:
         logger.error(f"ChatBotInstance {chat_instance.id} for chat {chat_id} is missing linked BotInstance, PersonaConfig or Owner.")
         return None

    persona_config = bot_instance.persona_config
    owner_user = bot_instance.owner

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
    logger.debug(f"Processing AI response for chat {chat_id}, persona {persona.name}. Raw length: {len(full_bot_response_text)}")

    # Add response to context first
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", full_bot_response_text.strip())
            # No commit here, should be handled by the calling function after all operations succeed
            logger.debug("AI response added to database context (pending commit).")
        except SQLAlchemyError as e:
            logger.error(f"DB Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            db.rollback() # Rollback immediately on context add failure
            # Optionally notify user? Probably better to let the main handler fail.
            raise # Propagate DB error
        except Exception as e:
            logger.error(f"Unexpected Error adding assistant response to context for chat_instance {persona.chat_instance.id}: {e}", exc_info=True)
            raise # Propagate other errors
    else:
        logger.warning("Cannot add AI response to context, chat_instance is None.")
        # Decide if this is fatal. If context is essential, maybe raise an error.
        # For now, continue processing but context won't include this response.


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
                 logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} to chat {chat_id}: '{part[:50]}...'")
                 await context.bot.send_message(chat_id=chat_id, text=part)
            except Exception as e:
                 logger.error(f"Error sending text part {i+1} to {chat_id}: {e}", exc_info=True)
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
        target_chat_id = update.effective_chat.id if update.effective_chat else None
        if target_chat_id:
             # ParseMode set by default in Application builder
             await context.bot.send_message(target_chat_id, text, reply_markup=reply_markup)
        else:
             logger.warning(f"Could not send limit exceeded message to user {user.telegram_id}: no effective chat.")
    except Exception as e:
        logger.error(f"Failed to send limit exceeded message to user {user.telegram_id}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not (update.message.text or update.message.caption): return # Handle captions too maybe? Text only for now.
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = update.message.text or update.message.caption or "" # Prioritize text
    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id}: {message_text[:100]}")

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
                return # Stop processing

            # --- Add user message to context ---
            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {message_text}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    # db.flush() # Flush is handled within add_message_to_context or before commit
                    context_added = True
                    logger.debug("Added user message to context.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error adding user message to context: {e_ctx}", exc_info=True)
                    # db.rollback() # Rolled back by get_db context manager
                    await update.message.reply_text("ошибка при сохранении вашего сообщения.")
                    return # Stop if context saving fails
            else:
                logger.error("Cannot add user message to context, chat_instance is None.")
                await update.message.reply_text("системная ошибка: не удалось связать сообщение с личностью.")
                return # Stop if internal state is inconsistent

            # --- Check if muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Message saved to context, but ignoring response.")
                db.commit() # Commit the added context message even if muted
                return

            # --- Handle potential mood change command ---
            available_moods = persona.get_all_mood_names()
            if message_text.lower() in map(str.lower, available_moods):
                 logger.info(f"Message '{message_text}' matched mood name. Changing mood.")
                 await mood(update, context, db=db, persona=persona)
                 # mood handler manages its own commits/rollbacks
                 return # Stop processing here after mood change

            # --- Decide whether to respond (especially in groups) ---
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
                                 messages=context_for_should_respond # Send current context
                             )
                             answer = decision_response.strip().lower()
                             logger.debug(f"should_respond AI decision for '{message_text[:50]}...': '{answer}'")
                             if answer.startswith("д"):
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond based on AI='{answer}'.")
                                 should_ai_respond = True
                             elif random.random() < 0.05: # Small chance to respond anyway
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding to respond randomly despite AI='{answer}'.")
                                 should_ai_respond = True
                             else:
                                 logger.info(f"Chat {chat_id}, Persona {persona.name}: Deciding NOT to respond based on AI='{answer}'.")
                                 should_ai_respond = False
                         except Exception as e:
                              logger.error(f"Error in should_respond logic for chat {chat_id}, persona {persona.name}: {e}", exc_info=True)
                              logger.warning("Error in should_respond. Defaulting to respond.")
                              should_ai_respond = True # Fail safe: respond if check fails
                     else:
                          logger.debug(f"No should_respond_prompt generated for persona {persona.name}. Defaulting to respond in group.")
                          should_ai_respond = True
                 else:
                     logger.debug(f"Persona {persona.name} has no should_respond template. Defaulting to respond in group.")
                     should_ai_respond = True

            if not should_ai_respond:
                 logger.debug(f"Decided not to respond based on should_respond logic for message: {message_text[:50]}...")
                 db.commit() # Commit the user message context even if not responding
                 return

            # --- Get context for AI response generation ---
            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    # Re-fetch context to include the user's latest message added earlier
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error getting context for AI response: {e_ctx}", exc_info=True)
                     # db.rollback() # Handled by context manager
                     await update.message.reply_text("ошибка при получении контекста для ответа.")
                     return
            elif not context_added:
                 logger.warning("Cannot generate AI response without updated context due to prior error.")
                 await update.message.reply_text("ошибка: не удалось сохранить ваше сообщение перед ответом.")
                 return

            # --- Generate and send AI response ---
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt: # Check if formatting failed
                logger.error(f"System prompt formatting failed for persona {persona.name}. Cannot generate response.")
                await update.message.reply_text("ошибка при подготовке ответа.")
                # Commit context so far
                db.commit()
                return

            logger.debug("Formatted main system prompt.")

            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for main message: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            # Commit everything (user message, AI response context, limit update)
            db.commit()
            logger.debug(f"Committed DB changes for handle_message cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_message: {e}", exc_info=True)
             # Rollback handled by context manager
             await update.message.reply_text("ошибка базы данных, попробуйте позже.")
        except Exception as e:
            logger.error(f"General error processing message in chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("произошла непредвиденная ошибка.")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    if not update.message: return
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id}")

    with next(get_db()) as db:
        try:
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id, db)
            if not persona_context_owner_tuple: return # No active persona
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            if not check_and_update_user_limits(db, owner_user):
                logger.info(f"Owner {owner_user.telegram_id} exceeded daily message limit for media. Not responding or saving context.")
                await send_limit_exceeded_message(update, context, owner_user)
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
            else:
                 logger.error(f"Unsupported media_type '{media_type}' in handle_media")
                 return

            # --- Add placeholder to context ---
            context_added = False
            if persona.chat_instance:
                try:
                    user_prefix = username
                    context_content = f"{user_prefix}: {context_text_placeholder}"
                    add_message_to_context(db, persona.chat_instance.id, "user", context_content)
                    context_added = True
                    logger.debug(f"Added media placeholder to context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                     logger.error(f"DB Error adding media placeholder context: {e_ctx}", exc_info=True)
                     if update.effective_message: await update.effective_message.reply_text("ошибка при сохранении информации о медиа.")
                     return # Stop if context fails
            else:
                 logger.error("Cannot add media placeholder to context, chat_instance is None.")
                 if update.effective_message: await update.effective_message.reply_text("системная ошибка: не удалось связать медиа с личностью.")
                 return # Stop if inconsistent

            # --- Check if muted ---
            if persona.chat_instance and persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted in chat {chat_id}. Media saved to context, but ignoring response.")
                db.commit() # Commit context placeholder
                return

            # --- Check if template exists ---
            if not prompt_template or not system_formatter:
                logger.info(f"Persona {persona.name} in chat {chat_id} has no {media_type} template. Skipping response generation.")
                db.commit() # Commit context placeholder
                return

            # --- Get context for AI ---
            context_for_ai = []
            if context_added and persona.chat_instance:
                try:
                    context_for_ai = get_context_for_chat_bot(db, persona.chat_instance.id)
                    logger.debug(f"Prepared {len(context_for_ai)} messages for AI context for {media_type}.")
                except SQLAlchemyError as e_ctx:
                    logger.error(f"DB Error getting context for AI media response: {e_ctx}", exc_info=True)
                    if update.effective_message: await update.effective_message.reply_text("ошибка при получении контекста для ответа на медиа.")
                    return # Stop if context retrieval fails
            elif not context_added:
                 logger.warning("Cannot generate AI media response without updated context.")
                 if update.effective_message: await update.effective_message.reply_text("ошибка: не удалось сохранить информацию о медиа перед ответом.")
                 return

            # --- Format prompt and get response ---
            system_prompt = system_formatter()
            if not system_prompt:
                 logger.error(f"Failed to format {media_type} prompt for persona {persona.name}")
                 db.commit() # Commit context so far
                 return

            logger.debug(f"Formatted {media_type} system prompt.")
            response_text = await send_to_langdock(system_prompt, context_for_ai)
            logger.debug(f"Received response from Langdock for {media_type}: {response_text[:100]}...")

            await process_and_send_response(update, context, chat_id, persona, response_text, db)

            db.commit() # Commit everything
            logger.debug(f"Committed DB changes for handle_media cycle chat {chat_id}")

        except SQLAlchemyError as e:
             logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
             if update.effective_message: await update.effective_message.reply_text("ошибка базы данных.")
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text("произошла непредвиденная ошибка.")


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
            user = get_or_create_user(db, user_id, username) # Also handles commit
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db) # Now uses imported func
            if persona_info_tuple:
                persona, _, _ = persona_info_tuple
                reply_text = (
                    f"привет! я {persona.name}. я уже активен в этом чате.\n"
                    "используй /help для списка команд."
                )
                await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            else:
                # Refresh user state after potential creation/update and check limits
                db.refresh(user)
                now = datetime.now(timezone.utc)
                if not user.last_message_reset or user.last_message_reset.date() < now.date():
                    user.daily_message_count = 0
                    user.last_message_reset = now
                    db.commit() # Commit reset if needed
                    db.refresh(user)

                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"
                expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber and user.subscription_expires_at else ""
                persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
                reply_text = (
                    f"привет! 👋 я бот для создания ai-собеседников (@NunuAiBot).\n\n"
                    f"твой статус: **{status}**{expires_text}\n"
                    f"личности: {persona_count}/{user.persona_limit} | "
                    f"сообщения: {user.daily_message_count}/{user.message_limit}\n\n"
                    "**начало работы:**\n"
                    "1. `/createpersona <имя>` - создай ai-личность.\n"
                    "2. `/mypersonas` - посмотри своих личностей и управляй ими.\n"
                    "`/profile` - детали статуса | `/subscribe` - узнать о подписке\n"
                    "`/help` - все команды"
                )
                # Default ParseMode is Markdown
                await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка при загрузке данных. попробуй позже.")
    except NameError as ne: # Catch the specific NameError if import was missed
        logger.error(f"NameError in /start handler for user {user_id}: {ne}", exc_info=True)
        await update.message.reply_text("произошла внутренняя ошибка конфигурации.")
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
         "/editpersona <id> - редактировать личность по ID\n"
         "/deletepersona <id> - удалить личность по ID\n\n"
         "**💬 управление в чате (где есть личность):**\n"
         "/addbot <id> - добавить личность в текущий чат\n"
         "/mood [настроение] - сменить настроение активной личности\n"
         "/reset - очистить память (контекст) личности в этом чате\n"
         "/mutebot - заставить личность молчать в чате\n"
         "/unmutebot - разрешить личности отвечать в чате"
     )
     # Default ParseMode is Markdown
     await update.message.reply_text(help_text, reply_markup=ReplyKeyboardRemove())


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id}")

    close_db_later = False
    db_session = db
    chat_bot_instance = None
    local_persona = persona # Use passed persona if available

    try:
        if db_session is None:
            db_context = get_db()
            db_session = next(db_context)
            close_db_later = True

        if local_persona is None:
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db_session)
            if not persona_info_tuple:
                reply_text = "в этом чате нет активной личности."
                try:
                    if is_callback: await update.callback_query.edit_message_text(reply_text)
                    else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
                except Exception as send_err: logger.error(f"Error sending 'no active persona' msg: {send_err}")
                logger.debug(f"No active persona for chat {chat_id}. Cannot set mood.")
                # Ensure db is closed if opened locally
                if close_db_later: db_session.close()
                return # Exit early
            local_persona, _, _ = persona_info_tuple

        if not local_persona or not local_persona.chat_instance:
             logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id}.")
             if is_callback: await update.callback_query.answer("Ошибка: не найдена информация о личности.", show_alert=True)
             else: await message_or_callback_msg.reply_text("Ошибка: не найдена информация о личности.")
             if close_db_later: db_session.close()
             return

        chat_bot_instance = local_persona.chat_instance

        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{local_persona.name}' is muted in chat {chat_id}. Ignoring mood command.")
            reply_text=f"Личность '{local_persona.name}' сейчас заглушена (/unmutebot)."
            try:
                 if is_callback: await update.callback_query.edit_message_text(reply_text)
                 else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
             reply_text = f"у личности '{local_persona.name}' не настроены настроения."
             try:
                 if is_callback: await update.callback_query.edit_message_text(reply_text)
                 else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
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
             # Example: set_mood_радость_123 -> parts = ['set', 'mood', 'радость', '123']
             if len(parts) >= 3:
                  # Join parts between 'set_mood_' and the last part (persona_id)
                  mood_arg_lower = "_".join(parts[2:-1]).lower()
                  if mood_arg_lower in available_moods_lower:
                      target_mood_original_case = available_moods_lower[mood_arg_lower]
             else:
                  logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
        elif not is_callback:
            mood_text = ""
            if context.args:
                 mood_text = " ".join(context.args) # Allow multi-word mood names from args
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
             set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case) # Function handles commit/rollback
             reply_text = f"настроение для '{local_persona.name}' теперь: **{target_mood_original_case}**"
             try:
                 if is_callback:
                     # Avoid edit if message is identical
                     if update.callback_query.message.text != reply_text:
                         await update.callback_query.edit_message_text(reply_text)
                     else:
                         await update.callback_query.answer(f"Настроение: {target_mood_original_case}") # Confirm with answer
                 else:
                     await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
             except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
             logger.info(f"Mood for persona {local_persona.name} in chat {chat_id} set to {target_mood_original_case}.")
        else:
             # Show keyboard if no valid mood provided or derived
             keyboard = [[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m}_{local_persona.id}")] for m in available_moods]
             reply_markup = InlineKeyboardMarkup(keyboard)
             current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)

             if mood_arg_lower: # If user provided an invalid mood
                 reply_text = f"не знаю настроения '{mood_arg_lower}' для '{local_persona.name}'. выбери из списка:"
                 logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id}. Sent mood selection.")
             else: # Just /mood command
                 reply_text = f"текущее настроение: **{current_mood_text}**. выбери новое для '{local_persona.name}':"
                 logger.debug(f"Sent mood selection keyboard for chat {chat_id}.")

             try:
                 if is_callback:
                      query = update.callback_query
                      # Avoid editing if text and markup are identical
                      if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(reply_text, reply_markup=reply_markup)
                      else:
                           await query.answer() # Answer callback without editing
                 else:
                      await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup)
             except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
         logger.error(f"Database error during /mood for chat {chat_id}: {e}", exc_info=True)
         # Rollback handled by context manager if close_db_later is True
         reply_text = "ошибка базы данных при смене настроения."
         try:
             if is_callback: await update.callback_query.edit_message_text(reply_text)
             else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
         except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
         logger.error(f"Error in /mood handler for chat {chat_id}: {e}", exc_info=True)
         reply_text = "ошибка при обработке команды /mood."
         try:
             if is_callback: await update.callback_query.edit_message_text(reply_text)
             else: await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
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
                 logger.error(f"Reset command: ChatBotInstance not found for persona {persona.name} in chat {chat_id}")
                 await update.message.reply_text("ошибка: не найден экземпляр бота для сброса.")
                 return

            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance.id).delete(synchronize_session='fetch')
            db.commit() # Commit deletion
            logger.info(f"Deleted {deleted_count} context messages for chat_bot_instance {chat_bot_instance.id} (Persona '{persona.name}') in chat {chat_id} by user {user_id}.")
            await update.message.reply_text(f"память личности '{persona.name}' в этом чате очищена.", reply_markup=ReplyKeyboardRemove())
        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
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
            "_имя обязательно, описание нет._"
            # ParseMode set globally
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
            user = get_or_create_user(db, user_id, username) # Ensure user exists

            # Use the property directly after getting the user
            # Need to eager load persona_configs or count them for the property check
            # Re-fetch user with loaded relation for accurate check
            user_for_check = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).one()

            if not user_for_check.can_create_persona:
                 current_count = len(user_for_check.persona_configs)
                 limit = user_for_check.persona_limit
                 logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")
                 status_text = "⭐ Premium" if user_for_check.is_active_subscriber else "🆓 Free"
                 text = (
                     f"упс! достигнут лимит личностей ({current_count}/{limit}) для статуса **{status_text}**. 😟\n"
                     f"чтобы создавать больше, используй /subscribe"
                 )
                 await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
                 return

            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                await update.message.reply_text(f"личность с именем '{persona_name}' уже есть. выбери другое.", reply_markup=ReplyKeyboardRemove())
                return

            new_persona = create_persona_config(db, user.id, persona_name, persona_description) # Handles commit internally
            await update.message.reply_text(
                f"✅ личность '{new_persona.name}' создана!\n"
                f"id: `{new_persona.id}`\n"
                f"описание: {new_persona.description}\n\n"
                f"добавь в чат или управляй через /mypersonas"
                # ParseMode set globally
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
            # Eager load persona_configs when fetching the user for display
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()

            if not user_with_personas:
                logger.error(f"User {user_id} not found after get_or_create in my_personas.")
                await update.message.reply_text("Ошибка: не удалось найти пользователя.")
                return

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit # Use property
            persona_count = len(personas)

            if not personas:
                await update.message.reply_text(
                    f"у тебя пока нет личностей (лимит: {persona_count}/{persona_limit}).\n"
                    "создай: /createpersona <имя>"
                    # ParseMode set globally
                )
                return

            text = f"твои личности ({persona_count}/{persona_limit}):\n"
            keyboard = []
            for p in personas:
                 # Use a non-functional callback for the name display row
                 keyboard.append([InlineKeyboardButton(f"👤 {p.name} (ID: {p.id})", callback_data=f"dummy_{p.id}")])
                 keyboard.append([
                     InlineKeyboardButton("⚙️ Редакт.", callback_data=f"edit_persona_{p.id}"),
                     InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_persona_{p.id}"),
                     InlineKeyboardButton("➕ В чат", callback_data=f"add_bot_{p.id}")
                 ])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup) # ParseMode set globally
            logger.info(f"User {user_id} requested mypersonas. Sent {persona_count} personas with action buttons.")
    except SQLAlchemyError as e:
        logger.error(f"Database error during /mypersonas for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("ошибка при загрузке списка личностей.")
    except Exception as e:
        logger.error(f"Error in /mypersonas handler for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("произошла ошибка при обработке команды /mypersonas.")


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id}"
    local_persona_id = persona_id # Use passed ID if available

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
                 "или используй кнопку '➕ В чат' из /mypersonas"
                 # ParseMode set globally
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

    if is_callback:
        await update.callback_query.answer("Добавляем личность...")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    with next(get_db()) as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                 response_text = f"личность с id `{local_persona_id}` не найдена или не твоя."
                 if is_callback: await update.callback_query.edit_message_text(response_text) # ParseMode set globally
                 else: await message_or_callback_msg.reply_text(response_text, reply_markup=ReplyKeyboardRemove()) # ParseMode set globally
                 return

            # Deactivate any existing active bot in this chat first
            existing_active_link = db.query(ChatBotInstance).filter(
                 ChatBotInstance.chat_id == chat_id,
                 ChatBotInstance.active == True
            ).options(
                joinedload(ChatBotInstance.bot_instance_ref) # Need bot_instance to check ID
            ).first()

            if existing_active_link:
                # Check if the existing active link is for the *same* persona we're trying to add
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    response_text = f"личность '{persona.name}' уже активна в этом чате."
                    if is_callback: await update.callback_query.answer(response_text, show_alert=True)
                    else: await message_or_callback_msg.reply_text(response_text, reply_markup=ReplyKeyboardRemove())
                    # No db.commit needed here, as nothing changed
                    return
                else:
                    # Deactivate the different persona
                    logger.info(f"Deactivating previous bot instance {existing_active_link.bot_instance_id} in chat {chat_id} before activating {local_persona_id}.")
                    existing_active_link.active = False
                    db.flush() # Ensure deactivation is processed before linking the new one

            # Find or create BotInstance for the persona
            user = get_or_create_user(db, user_id, username) # Need user.id for potential creation
            bot_instance = db.query(BotInstance).filter(
                BotInstance.persona_config_id == local_persona_id
                # Optional: Add owner_id check if BotInstances shouldn't be shared?
                # BotInstance.owner_id == user.id
            ).first()

            if not bot_instance:
                 # Create BotInstance if it doesn't exist for this persona
                 bot_instance = create_bot_instance(db, user.id, local_persona_id, name=f"Inst:{persona.name}")
                 logger.info(f"Created BotInstance {bot_instance.id} for persona {local_persona_id}")
                 # create_bot_instance handles its own commit/rollback

            # Link the (potentially new) BotInstance to the chat
            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id)
            # link_bot_instance_to_chat handles its own commit/rollback

            if chat_link:
                 # Clear context for the newly linked/activated instance
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 db.commit() # Commit the context deletion
                 logger.debug(f"Cleared {deleted_ctx} context messages for chat_bot_instance {chat_link.id} upon linking.")
                 response_text = f"✅ личность '{persona.name}' (id: `{local_persona_id}`) активирована в этом чате! Память очищена."
                 await context.bot.send_message(chat_id=chat_id, text=response_text, reply_markup=ReplyKeyboardRemove()) # ParseMode global
                 if is_callback:
                      try:
                           # Attempt to delete the original message with the buttons
                           await update.callback_query.delete_message()
                      except Exception as del_err:
                           logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                 logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}) to chat {chat_id} ('{chat_title}'). ChatBotInstance ID: {chat_link.id}")
            else:
                 # link_bot_instance_to_chat handles rollback on failure
                 response_text = "не удалось активировать личность (ошибка связывания)."
                 if is_callback:
                      # If called from button, send a new message as edit might fail
                      await context.bot.send_message(chat_id=chat_id, text=response_text)
                 else:
                      await message_or_callback_msg.reply_text(response_text, reply_markup=ReplyKeyboardRemove())
                 logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id} - link_bot_instance_to_chat returned None.")

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
    query = update.callback_query
    if not query or not query.data: return

    chat_id = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data
    logger.info(f"CALLBACK < User {user_id} ({username}) in Chat {chat_id} data: {data}")

    # --- Route callbacks ---
    if data.startswith("set_mood_"):
        await query.answer() # Answer here, mood() will edit the message
        await mood(update, context)
    elif data == "subscribe_info":
        await query.answer() # Answer here, subscribe() will edit
        await subscribe(update, context, from_callback=True)
    elif data == "subscribe_pay":
        await query.answer("Создаю ссылку на оплату...") # Specific answer
        await generate_payment_link(update, context)
    elif data == "view_tos":
        await query.answer() # Answer here, view_tos() will edit
        await view_tos(update, context)
    elif data == "confirm_pay":
        await query.answer() # Answer here, confirm_pay() will edit
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
        # add_bot_to_chat handles its own answers/edits
        await add_bot_to_chat(update, context) # No persona_id needed, extracted inside
    elif data.startswith("dummy_"):
        await query.answer() # Just acknowledge the press
    else:
        # Check if it's handled by ConversationHandlers
        known_conv_prefixes = ("edit_persona_", "delete_persona_", "edit_field_", "edit_mood", "deletemood", "cancel_edit", "edit_persona_back")
        if any(data.startswith(p) for p in known_conv_prefixes):
            # Let the ConversationHandler process it, don't answer here
             logger.debug(f"Callback '{data}' seems to be for a ConversationHandler, skipping direct handling.")
             # ConversationHandler should call query.answer() itself
        else:
            logger.warning(f"Unhandled callback query data: {data} from user {user_id}")
            try:
                 await query.answer("Неизвестное действие") # Provide some feedback
            except Exception as e:
                 logger.warning(f"Failed to answer unhandled callback {query.id}: {e}")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /profile < User {user_id} ({username})")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username) # Handles commit

            # Ensure limits are up-to-date
            now = datetime.now(timezone.utc)
            if not user.last_message_reset or user.last_message_reset.date() < now.date():
                logger.info(f"Resetting daily limit for user {user_id} during /profile check.")
                user.daily_message_count = 0
                user.last_message_reset = now
                db.commit() # Commit reset
                db.refresh(user)

            is_active_subscriber = user.is_active_subscriber
            status = "⭐ Premium" if is_active_subscriber else "🆓 Free"
            expires_text = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active_subscriber and user.subscription_expires_at else "нет активной подписки"

            # Get persona count
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
            # ParseMode set globally
            await update.message.reply_text(text)
        except SQLAlchemyError as e:
             logger.error(f"Database error during /profile for user {user_id}: {e}", exc_info=True)
             # Rollback handled by context manager
             await update.message.reply_text("ошибка базы данных при загрузке профиля.")
        except Exception as e:
            logger.error(f"Error in /profile handler for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text("ошибка при обработке команды /profile.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or f"id_{user_id}"
    logger.info(f"CMD /subscribe or Info Callback < User {user_id} ({username})")

    message_to_update_or_reply = update.callback_query.message if from_callback else update.message
    if not message_to_update_or_reply: return

    # Check Yookassa config readiness
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    if not yookassa_ready:
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        reply_markup = None
        logger.warning("Yookassa credentials not set or shop ID is not numeric in subscribe handler.")
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
            # Edit only if content or markup differs to avoid "Message is not modified" error
            query = update.callback_query
            if query.message.text != text or query.message.reply_markup != reply_markup:
                 await query.edit_message_text(text, reply_markup=reply_markup) # ParseMode global
            # else:
                 # If message is identical, maybe just answer the callback silently?
                 # await query.answer() # Already answered in handle_callback_query
        else:
            # ParseMode global
            await message_to_update_or_reply.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Failed to send/edit subscribe message for user {user_id}: {e}")
        if from_callback:
            # If editing failed, try sending a new message as a fallback
            try:
                # ParseMode global
                await context.bot.send_message(chat_id=message_to_update_or_reply.chat.id, text=text, reply_markup=reply_markup)
            except Exception as send_e:
                 logger.error(f"Failed to send fallback subscribe message for user {user_id}: {send_e}")


async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} requested to view ToS.")

    tos_url = context.bot_data.get('tos_url')

    if tos_url:
        keyboard = [
            [InlineKeyboardButton("📜 Открыть Соглашение", url=tos_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = "Ознакомьтесь с Пользовательским Соглашением, открыв его по ссылке ниже:"
        try:
            # Edit only if needed
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup) # ParseMode global
        except Exception as e:
            logger.error(f"Failed to show ToS link to user {user_id}: {e}")
            await query.answer("Не удалось отобразить ссылку на соглашение.", show_alert=True)
    else:
        logger.error(f"ToS URL not found in bot_data for user {user_id}.")
        text = "❌ Не удалось загрузить ссылку на Пользовательское Соглашение. Попробуйте позже."
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            if query.message.text != text or query.message.reply_markup != reply_markup:
                await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception as e:
             logger.error(f"Failed to show ToS error message to user {user_id}: {e}")
             await query.answer("Ошибка загрузки соглашения.", show_alert=True)


async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message: return
    user_id = query.from_user.id
    logger.info(f"User {user_id} confirmed ToS agreement, proceeding to payment button.")

    tos_url = context.bot_data.get('tos_url')
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())

    if not yookassa_ready:
        text = "К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)"
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]])
        logger.warning("Yookassa credentials not set or shop ID is not numeric in confirm_pay handler.")
    else:
        text = (
             "✅ Отлично!\n\n"
             "Нажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с "
             "Пользовательским Соглашением."
             "\n\n👇"
        )
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {SUBSCRIPTION_PRICE_RUB:.0f} {SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]
        ]
        if tos_url:
             keyboard.append([InlineKeyboardButton("📜 Условия использования (прочитано)", url=tos_url)])
        else:
             # Indicate error but still allow proceeding if user confirmed
             keyboard.append([InlineKeyboardButton("📜 Условия (ошибка загрузки)", callback_data="view_tos")])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")])
        reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Edit only if needed
        if query.message.text != text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                # ParseMode global
                disable_web_page_preview=True
            )
    except Exception as e:
        logger.error(f"Failed to show final payment confirmation to user {user_id}: {e}")


async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message: return

    user_id = query.from_user.id
    logger.info(f"--- generate_payment_link ENTERED for user {user_id} ---")

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly for payment generation.")
        await query.edit_message_text("❌ ошибка: сервис оплаты не настроен правильно.", reply_markup=None)
        return

    try:
        # Ensure YK SDK is configured *before* creating payment
        # It might be configured globally in main.py, but doesn't hurt to ensure here
        if not YookassaConfig.secret_key or YookassaConfig.account_id != int(YOOKASSA_SHOP_ID):
             Configuration.configure(int(YOOKASSA_SHOP_ID), YOOKASSA_SECRET_KEY)
             logger.info(f"Yookassa re-configured within generate_payment_link (Shop ID: {YOOKASSA_SHOP_ID}).")
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK in generate_payment_link: {conf_e}", exc_info=True)
        await query.edit_message_text("❌ ошибка конфигурации платежной системы.", reply_markup=None)
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium подписка @NunuAiBot на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)}
    bot_username = "NunuAiBot" # Fallback username
    try:
        me = await context.bot.get_me()
        bot_username = me.username or bot_username
    except Exception as e:
        logger.warning(f"Could not get bot username dynamically: {e}")
    return_url = f"https://t.me/{bot_username}"

    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ @{bot_username} на {SUBSCRIPTION_DURATION_DAYS} дней",
                "quantity": 1.0,
                "amount": {"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1", # 1 = НДС не облагается (или 2=0%, 3=10%, 4=20%, 5=10/110, 6=20/120) - УТОЧНИТЬ!
                "payment_mode": "full_prepayment",
                "payment_subject": "service"
            })
        ]
        receipt_data = Receipt({
            # Using placeholder email as Yookassa often requires it
            "customer": {"email": f"user_{user_id}@telegram.bot"},
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

        # Run blocking Yookassa call in a separate thread
        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or not payment_response.confirmation or not payment_response.confirmation.confirmation_url:
             logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
             error_message = "❌ не удалось получить ссылку от платежной системы"
             if payment_response and payment_response.status: error_message += f" (статус: {payment_response.status})"
             error_message += ".\nПопробуй позже."
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
        # Check for specific Yookassa API errors if possible, otherwise generic message
        if hasattr(e, 'response') and hasattr(e.response, 'json'):
            try:
                err_data = e.response.json()
                err_type = err_data.get('type')
                err_desc = err_data.get('description')
                if err_type == 'error':
                    logger.error(f"Yookassa API Error details: {err_data}")
                    user_message += f"({err_desc or 'детали в логах'})"
            except Exception:
                pass # Ignore if parsing fails
        elif isinstance(e, httpx.RequestError):
             user_message += "Проблема с сетевым подключением к ЮKassa."
        else:
             user_message += "Произошла непредвиденная ошибка."
        user_message += "\nПопробуй еще раз позже или свяжись с поддержкой."
        try:
            # Use edit_message_text as query should still be valid
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

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Start fresh for this conversation

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg) # Global parse mode
                 else: await update.effective_message.reply_text(error_msg) # Global parse mode
                 return ConversationHandler.END

            context.user_data['edit_persona_id'] = persona_id
            keyboard = await _get_edit_persona_keyboard(persona_config)
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:"

            if update.callback_query:
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup) # Global parse mode
                      else:
                           await query.answer() # Message already correct, just answer callback
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for edit start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup) # Global parse mode
            else:
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup) # Global parse mode

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
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")
    if not args or not args[0].isdigit():
        await update.message.reply_text("укажи id личности: `/editpersona <id>`\nили используй кнопку из /mypersonas") # Global parse mode
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
         await update.message.reply_text("ID должен быть числом.")
         return ConversationHandler.END
    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_persona_choice: User {user_id}, Persona ID from context: {persona_id}, Callback data: {data} ---")

    if not persona_id:
         logger.warning(f"User {user_id} in edit_persona_choice, but edit_persona_id not found in user_data.")
         await query.edit_message_text("ошибка: сессия редактирования потеряна (нет id). начни снова.", reply_markup=None)
         return ConversationHandler.END

    # Fetch user and persona to check premium status and existence
    persona_config = None
    is_premium_user = False
    try:
        with next(get_db()) as db:
            user = get_or_create_user(db, user_id) # Ensure user exists
            # Need to load owner relationship for premium check if not admin
            persona_config = db.query(PersonaConfig).options(joinedload(PersonaConfig.owner)).filter(
                 PersonaConfig.id == persona_id,
                 PersonaConfig.owner_id == user.id # Ensure ownership
            ).first()

            if not persona_config:
                logger.warning(f"User {user_id} in edit_persona_choice: PersonaConfig {persona_id} not found or not owned.")
                await query.answer("Личность не найдена", show_alert=True)
                await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                context.user_data.clear()
                return ConversationHandler.END
            is_premium_user = persona_config.owner.is_active_subscriber # Use property from owner relation

    except SQLAlchemyError as e:
         logger.error(f"DB error fetching user/persona in edit_persona_choice for persona {persona_id}: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text("ошибка базы данных при проверке данных.", reply_markup=None)
         return EDIT_PERSONA_CHOICE # Stay in the same state
    except Exception as e:
         logger.error(f"Unexpected error fetching user/persona in edit_persona_choice: {e}", exc_info=True)
         await query.answer("Непредвиденная ошибка", show_alert=True)
         await query.edit_message_text("Непредвиденная ошибка.", reply_markup=None)
         return ConversationHandler.END # End convo on unexpected error

    # Now handle the callback data
    await query.answer() # Answer most callbacks here unless handled specifically below

    if data == "cancel_edit":
        return await edit_persona_cancel(update, context)

    if data == "edit_moods":
        if not is_premium_user and not is_admin(user_id): # Allow admin to edit
             logger.info(f"User {user_id} (non-premium) attempted to edit moods for persona {persona_id}.")
             await query.answer("⭐ Редактирование настроений доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay on the main edit menu
        else:
             logger.info(f"User {user_id} proceeding to edit moods for persona {persona_id}.")
             # Pass fetched persona_config to avoid re-fetching
             return await edit_moods_menu(update, context, persona_config=persona_config)

    if data.startswith("edit_field_"):
        field = data.replace("edit_field_", "")
        field_display_name = FIELD_MAP.get(field, field)
        logger.info(f"User {user_id} selected field '{field}' for persona {persona_id}.")

        # Check premium fields
        advanced_fields = ["should_respond_prompt_template", "spam_prompt_template",
                           "photo_prompt_template", "voice_prompt_template", "max_response_messages"]
        if field in advanced_fields and not is_premium_user and not is_admin(user_id): # Allow admin
             logger.info(f"User {user_id} (non-premium) attempted to edit premium field '{field}' for persona {persona_id}.")
             await query.answer(f"⭐ Поле '{field_display_name}' доступно по подписке", show_alert=True)
             return EDIT_PERSONA_CHOICE # Stay on main edit menu

        context.user_data['edit_field'] = field
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        reply_markup = InlineKeyboardMarkup([[back_button]])

        if field == "max_response_messages":
            current_value = getattr(persona_config, field, 3)
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'** (число от 1 до 10):\n_текущее: {current_value}_", reply_markup=reply_markup) # Global parse mode
            return EDIT_MAX_MESSAGES
        else:
            current_value = getattr(persona_config, field, "")
            current_value_display = current_value if len(str(current_value)) < 300 else str(current_value)[:300] + "..."
            await query.edit_message_text(f"отправь новое значение для **'{field_display_name}'**.\n_текущее:_\n`{current_value_display}`", reply_markup=reply_markup) # Global parse mode
            return EDIT_FIELD

    if data == "edit_persona_back":
         logger.info(f"User {user_id} pressed back button in edit_persona_choice for persona {persona_id}.")
         keyboard = await _get_edit_persona_keyboard(persona_config) # Use already fetched config
         await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
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

    # Validation logic
    validation_error = None
    max_len = {
        "name": 50, "description": 1500, "system_prompt_template": 3000,
        "should_respond_prompt_template": 1000, "spam_prompt_template": 1000,
        "photo_prompt_template": 1000, "voice_prompt_template": 1000
    }
    min_len = {"name": 2}

    if field in max_len and len(new_value) > max_len[field]:
        validation_error = f"{field_display_name}: макс. {max_len[field]} символов."
    if field in min_len and len(new_value) < min_len[field]:
        validation_error = f"{field_display_name}: мин. {min_len[field]} символа."

    if validation_error:
        logger.debug(f"Validation failed for field '{field}': {validation_error}")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
        await update.message.reply_text(f"{validation_error} попробуй еще раз:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_FIELD

    try:
        with next(get_db()) as db:
            # Re-fetch config to perform update
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned during field update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            # Check name uniqueness if changing name
            if field == "name" and new_value.lower() != persona_config.name.lower():
                user = get_or_create_user(db, user_id) # Need user.id
                existing = get_persona_by_name_and_owner(db, user.id, new_value)
                if existing:
                    logger.info(f"User {user_id} tried to set name to '{new_value}', but it's already taken by persona {existing.id}.")
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")
                    await update.message.reply_text(f"имя '{new_value}' уже занято другой твоей личностью. попробуй другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                    return EDIT_FIELD # Stay in edit field state

            # Update the field
            setattr(persona_config, field, new_value)
            db.commit() # Commit the change
            logger.info(f"User {user_id} successfully updated field '{field}' for persona {persona_id}.")

            await update.message.reply_text(f"✅ поле **'{field_display_name}'** для личности **'{persona_config.name}'** обновлено!")

            # Return to main edit menu
            context.user_data.pop('edit_field', None)
            db.refresh(persona_config) # Refresh to get latest state for keyboard
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating field {field} for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
         await update.message.reply_text("❌ ошибка базы данных при обновлении. попробуй еще раз.")
         # Attempt to return to main edit menu on error
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
        return EDIT_MAX_MESSAGES # Stay in this state

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found or not owned in edit_max_messages_update.")
                 await update.message.reply_text("ошибка: личность не найдена или нет доступа.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            persona_config.max_response_messages = new_value
            db.commit() # Commit change
            logger.info(f"User {user_id} updated max_response_messages to {new_value} for persona {persona_id}.")

            await update.message.reply_text(f"✅ макс. сообщений в ответе для **'{persona_config.name}'** установлено: **{new_value}**")

            # Return to main edit menu
            db.refresh(persona_config)
            keyboard = await _get_edit_persona_keyboard(persona_config)
            await update.message.reply_text(f"что еще изменить для **{persona_config.name}** (id: `{persona_id}`)?", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
            return EDIT_PERSONA_CHOICE

    except SQLAlchemyError as e:
         logger.error(f"Database error updating max_response_messages for persona {persona_id}: {e}", exc_info=True)
         # Rollback handled by context manager
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

    max_resp_msg = getattr(persona_config, 'max_response_messages', 3) # Default to 3 if not set

    keyboard = [
        [InlineKeyboardButton("📝 Имя", callback_data="edit_field_name"), InlineKeyboardButton("📜 Описание", callback_data="edit_field_description")],
        [InlineKeyboardButton("⚙️ Системный промпт", callback_data="edit_field_system_prompt_template")],
        [InlineKeyboardButton(f"📊 Макс. ответов ({max_resp_msg}) ⭐", callback_data="edit_field_max_response_messages")],
        [InlineKeyboardButton("🤔 Промпт 'Отвечать?' ⭐", callback_data="edit_field_should_respond_prompt_template")],
        [InlineKeyboardButton("💬 Промпт спама ⭐", callback_data="edit_field_spam_prompt_template")],
        [InlineKeyboardButton("🖼️ Промпт фото ⭐", callback_data="edit_field_photo_prompt_template"), InlineKeyboardButton("🎤 Промпт голоса ⭐", callback_data="edit_field_voice_prompt_template")],
        [InlineKeyboardButton("🎭 Настроения ⭐", callback_data="edit_moods")],
        [InlineKeyboardButton("❌ Завершить", callback_data="cancel_edit")]
    ]
    # Add premium markers (⭐) maybe based on user status later if needed
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
         # Sort moods alphabetically for consistent display
         sorted_moods = sorted(moods.keys(), key=str.lower)
         for mood_name in sorted_moods:
              # Ensure mood_name is safe for callback data (basic check)
              safe_mood_name = re.sub(r'[^\w-]', '', mood_name) # Remove potentially problematic chars
              if not safe_mood_name: continue # Skip if name becomes empty

              keyboard.append([
                  InlineKeyboardButton(f"✏️ {mood_name.capitalize()}", callback_data=f"editmood_select_{safe_mood_name}"),
                  # Use original name for delete confirmation display logic if needed, but safe name in callback
                  InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{safe_mood_name}")
              ])
     keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
     keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_persona_back")])
     return keyboard

async def _try_return_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    logger.debug(f"Attempting to return to main edit menu for user {user_id}, persona {persona_id} after error.")
    message_target = update.effective_message
    if not message_target:
        logger.warning("Cannot return to edit menu: effective_message is None.")
        context.user_data.clear()
        return ConversationHandler.END
    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if persona_config:
                keyboard = await _get_edit_persona_keyboard(persona_config)
                await message_target.reply_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
                return EDIT_PERSONA_CHOICE # Return to the choice state
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
                 await message_target.reply_text(f"управление настроениями для **{persona_config.name}**:", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
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


async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[PersonaConfig] = None) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END # Should be called from callback

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- edit_moods_menu: User={user_id}, PersonaID={persona_id} ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.", reply_markup=None)
        return ConversationHandler.END

    # Use passed persona_config if available, otherwise fetch
    local_persona_config = persona_config
    if local_persona_config is None:
        try:
            with next(get_db()) as db:
                local_persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
                if not local_persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.answer("Личность не найдена", show_alert=True)
                    await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                    context.user_data.clear()
                    return ConversationHandler.END
        except Exception as e:
             logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
             await query.answer("Ошибка базы данных", show_alert=True)
             await query.edit_message_text("Ошибка базы данных при загрузке настроений.", reply_markup=None)
             # Try returning to main edit menu might be better here
             return await _try_return_to_edit_menu(update, context, user_id, persona_id)

    # Check premium status again just in case
    try:
        with next(get_db()) as db:
             # Fetch owner status if config didn't have it loaded
             if not hasattr(local_persona_config, 'owner') or not local_persona_config.owner:
                  owner = db.query(User).filter(User.id == local_persona_config.owner_id).first()
                  is_prem = owner.is_active_subscriber if owner else False
             else:
                  is_prem = local_persona_config.owner.is_active_subscriber

             if not is_prem and not is_admin(user_id):
                 logger.warning(f"User {user_id} (non-premium) reached mood editor for {persona_id} unexpectedly.")
                 await query.answer("⭐ Доступно по подписке", show_alert=True)
                 # Return to main edit menu, not end convo
                 return await _try_return_to_edit_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error checking premium status in edit_moods_menu: {e}", exc_info=True)
        # Continue anyway, maybe let it fail later if needed

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = f"управление настроениями для **{local_persona_config.name}**:"

    try:
        if query.message.text != msg_text or query.message.reply_markup != reply_markup:
            await query.edit_message_text(msg_text, reply_markup=reply_markup) # Global parse mode
        # else: await query.answer() # Already answered
    except Exception as e:
         logger.error(f"Error editing moods menu message for persona {persona_id}: {e}")
         try:
            # Fallback: send new message if edit fails
            await query.message.reply_text(msg_text, reply_markup=reply_markup) # Global parse mode
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

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await query.edit_message_text("ошибка: сессия редактирования потеряна.")
        return ConversationHandler.END

    # Fetch persona config for context
    persona_config = None
    try:
        with next(get_db()) as db:
             persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
             if not persona_config:
                 logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_mood_choice.")
                 await query.answer("Личность не найдена", show_alert=True)
                 await query.edit_message_text("ошибка: личность не найдена или нет доступа.", reply_markup=None)
                 context.user_data.clear()
                 return ConversationHandler.END
    except Exception as e:
         logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
         await query.answer("Ошибка базы данных", show_alert=True)
         await query.edit_message_text("Ошибка базы данных.", reply_markup=None)
         return EDIT_MOOD_CHOICE # Stay in mood menu

    await query.answer() # Answer most mood callbacks here

    # --- Handle Mood Menu Actions ---
    if data == "edit_persona_back":
        logger.debug(f"User {user_id} going back from mood menu to main edit menu for {persona_id}.")
        keyboard = await _get_edit_persona_keyboard(persona_config) # Use fetched config
        await query.edit_message_text(f"редактируем **{persona_config.name}** (id: `{persona_id}`)\nвыбери, что изменить:", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return EDIT_PERSONA_CHOICE

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None # Clear any previous edit name
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await query.edit_message_text("введи **название** нового настроения (одно слово, латиница/кириллица, цифры, дефис, подчеркивание, без пробелов):", reply_markup=InlineKeyboardMarkup([[back_button]])) # Global parse mode
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        # Mood name was sanitized for callback, need original if possible, but maybe not critical
        mood_name_safe = data.split("editmood_select_", 1)[1]
        context.user_data['edit_mood_name'] = mood_name_safe # Store the name being edited
        logger.debug(f"User {user_id} selected mood '{mood_name_safe}' to edit for {persona_id}.")

        current_prompt = "_не найдено_"
        original_mood_name = mood_name_safe # Default to safe name
        try:
            current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            # Find original case key matching the safe name
            original_mood_name = next((k for k in current_moods if re.sub(r'[^\w-]', '', k) == mood_name_safe), mood_name_safe)
            current_prompt = current_moods.get(original_mood_name, "_нет промпта_")
            context.user_data['edit_mood_name'] = original_mood_name # Store original name now we found it

        except Exception as e:
            logger.error(f"Error reading moods JSON for persona {persona_id} in editmood_select: {e}")
            current_prompt = "_ошибка чтения промпта_"

        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        prompt_display = current_prompt if len(current_prompt) < 300 else current_prompt[:300] + "..."
        display_name = context.user_data.get('edit_mood_name', mood_name_safe) # Show original if found
        await query.edit_message_text(f"редактирование настроения: **{display_name}**\n\n_текущий промпт:_\n`{prompt_display}`\n\nотправь **новый текст промпта**:", reply_markup=InlineKeyboardMarkup([[back_button]])) # Global parse mode
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
         mood_name_safe = data.split("deletemood_confirm_", 1)[1]
         # Try to find original name for confirmation message
         original_mood_name = mood_name_safe
         try:
             current_moods = json.loads(persona_config.mood_prompts_json or '{}')
             original_mood_name = next((k for k in current_moods if re.sub(r'[^\w-]', '', k) == mood_name_safe), mood_name_safe)
         except Exception: pass

         context.user_data['delete_mood_name'] = original_mood_name # Store original name for deletion
         logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' (safe: {mood_name_safe}) for {persona_id}. Asking confirmation.")
         # Use safe name in confirm callback data
         keyboard = [
             [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{mood_name_safe}")],
             [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
            ]
         await query.edit_message_text(f"точно удалить настроение **'{original_mood_name}'**?", reply_markup=InlineKeyboardMarkup(keyboard)) # Global parse mode
         return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
         logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
         context.user_data.pop('edit_mood_name', None)
         context.user_data.pop('delete_mood_name', None)
         # Pass fetched config to avoid re-fetch
         return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text("неизвестный выбор настроения.")
    return await edit_moods_menu(update, context, persona_config=persona_config) # Return to mood menu


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    # Basic validation - allow letters, numbers, hyphen, underscore, cyrillic. No spaces.
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text("ошибка: сессия редактирования потеряна.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if not mood_name_match or len(mood_name_raw) > 30:
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
        await update.message.reply_text("название: 1-30 символов, только буквы/цифры/дефис/подчеркивание (кириллица/латиница), без пробелов. попробуй еще:", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return EDIT_MOOD_NAME

    mood_name = mood_name_raw # Use the validated name

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                 await update.message.reply_text("ошибка: личность не найдена.", reply_markup=ReplyKeyboardRemove())
                 context.user_data.clear()
                 return ConversationHandler.END

            current_moods = {}
            try:
                 current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} in mood name check, starting fresh.")

            # Case-insensitive check for existing mood
            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists for persona {persona_id}.")
                back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
                await update.message.reply_text(f"настроение '{mood_name}' уже существует. выбери другое:", reply_markup=InlineKeyboardMarkup([[back_button]]))
                return EDIT_MOOD_NAME # Stay in name state

            # Store the new mood name and proceed to prompt input
            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            back_button = InlineKeyboardButton("⬅️ Назад", callback_data="edit_moods_back_cancel")
            await update.message.reply_text(f"отлично! теперь отправь **текст промпта** для настроения **'{mood_name}'**:", reply_markup=InlineKeyboardMarkup([[back_button]])) # Global parse mode
            return EDIT_MOOD_PROMPT

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("ошибка базы данных при проверке имени.", reply_markup=ReplyKeyboardRemove())
        return EDIT_MOOD_NAME # Stay in name state on DB error
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("непредвиденная ошибка.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name') # Should be original case name if edited, or new name
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
        return EDIT_MOOD_PROMPT # Stay in prompt state

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

            # Add or update the mood
            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods) # Handles JSON conversion and flag_modified
            db.commit() # Commit the mood update

            context.user_data.pop('edit_mood_name', None) # Clear mood name from context
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            await update.message.reply_text(f"✅ настроение **'{mood_name}'** сохранено!")

            # Return to mood menu
            db.refresh(persona_config) # Refresh to show updated list
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await update.message.reply_text("❌ ошибка базы данных при сохранении настроения.")
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка при сохранении настроения.")
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM # Stay if no data

    data = query.data
    # Original mood name (with correct case) should be stored here
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    # Extract safe name from callback data for comparison
    safe_mood_name_from_callback = ""
    if data.startswith("deletemood_delete_"):
        safe_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]

    # Verify state consistency
    if not mood_name_to_delete or not persona_id or not safe_mood_name_from_callback:
        logger.warning(f"User {user_id}: Missing state in delete_mood_confirmed. Mood='{mood_name_to_delete}', SafeCB='{safe_mood_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("Ошибка сессии", show_alert=True)
        await query.edit_message_text("ошибка: неверные данные для удаления или сессия потеряна.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

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

            try:
                current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError:
                 logger.warning(f"Invalid JSON for persona {persona_id} during mood deletion, assuming empty.")
                 current_moods = {}

            # Check if the mood (original name) exists before deleting
            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete]
                persona_config.set_moods(db, current_moods) # Update JSON
                db.commit() # Commit deletion

                context.user_data.pop('delete_mood_name', None) # Clear state
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                await query.edit_message_text(f"🗑️ настроение **'{mood_name_to_delete}'** удалено.") # Global parse mode
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id} (maybe already deleted).")
                await query.edit_message_text(f"настроение '{mood_name_to_delete}' не найдено (уже удалено?).", reply_markup=None)
                context.user_data.pop('delete_mood_name', None) # Clear state anyway

            # Return to mood menu
            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        # Rollback handled by context manager
        await query.edit_message_text("❌ ошибка базы данных при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при удалении настроения.", reply_markup=None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message # Could be message from callback or command
    user_id = update.effective_user.id
    logger.info(f"User {user_id} cancelled persona edit/mood edit.")
    cancel_message = "редактирование отменено."
    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer() # Acknowledge callback
            if query.message and query.message.text != cancel_message:
                # Edit the message the button was attached to
                await query.edit_message_text(cancel_message, reply_markup=None)
        elif message:
            # If cancelled via command, reply to that command message
            await message.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.warning(f"Error sending cancellation confirmation for user {user_id}: {e}")
        # Fallback: send a new message if edit/reply fails
        if message: # Check if we have a chat context
            try:
                await context.bot.send_message(chat_id=message.chat.id, text=cancel_message, reply_markup=ReplyKeyboardRemove())
            except Exception as send_e:
                logger.error(f"Failed to send fallback cancel message: {send_e}")

    context.user_data.clear() # Clear conversation state
    return ConversationHandler.END


# --- Delete Persona Conversation ---
async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    context.user_data.clear() # Start fresh

    try:
        with next(get_db()) as db:
            persona_config = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona_config:
                 error_msg = f"личность с id `{persona_id}` не найдена или не твоя."
                 if update.callback_query: await update.callback_query.edit_message_text(error_msg) # Global parse mode
                 else: await update.effective_message.reply_text(error_msg) # Global parse mode
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
                 query = update.callback_query
                 try:
                      if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                           await query.edit_message_text(msg_text, reply_markup=reply_markup) # Global parse mode
                      else:
                           await query.answer()
                 except Exception as edit_err:
                      logger.warning(f"Could not edit message for delete start (persona {persona_id}): {edit_err}. Sending new message.")
                      await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup) # Global parse mode
            else:
                 await update.effective_message.reply_text(msg_text, reply_markup=reply_markup) # Global parse mode

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
        await update.message.reply_text("укажи id личности: `/deletepersona <id>`\nили используй кнопку из /mypersonas") # Global parse mode
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return ConversationHandler.END
    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if not query or not query.data: return DELETE_PERSONA_CONFIRM # Stay if no data

    data = query.data
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id')

    logger.info(f"--- delete_persona_confirmed: User={user_id}, PersonaID={persona_id}, Data={data} ---")

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
    persona_name_deleted = f"ID {persona_id}"
    try:
        with next(get_db()) as db:
             # Need user.id for delete_persona_config
             user = get_or_create_user(db, user_id)
             # Fetch name before deleting
             persona_to_delete = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == user.id).first()
             if persona_to_delete:
                 persona_name_deleted = persona_to_delete.name
                 logger.info(f"Attempting database deletion for persona {persona_id} ('{persona_name_deleted}')...")
                 if delete_persona_config(db, persona_id, user.id): # Function handles commit
                     logger.info(f"User {user_id} successfully deleted persona {persona_id} ('{persona_name_deleted}').")
                     deleted_ok = True
                 else:
                     # delete_persona_config already logged error if commit failed
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

    if deleted_ok:
        await query.edit_message_text(f"✅ личность '{persona_name_deleted}' удалена.", reply_markup=None)
    else:
        await query.edit_message_text("❌ не удалось удалить личность (ошибка базы данных).", reply_markup=None)

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
                db.commit() # Commit the mute status
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona.name}' больше не будет отвечать в этом чате (но будет запоминать сообщения). Используйте /unmutebot, чтобы вернуть.", reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(f"Личность '{persona.name}' уже заглушена в этом чате.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
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
            # Fetch the active instance directly
            active_instance = db.query(ChatBotInstance)\
                .options(
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.owner),
                    joinedload(ChatBotInstance.bot_instance_ref)
                    .joinedload(BotInstance.persona_config) # Load persona for name
                )\
                .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
                .first()

            if not active_instance:
                await update.message.reply_text("В этом чате нет активной личности, которую можно размьютить.", reply_markup=ReplyKeyboardRemove())
                return

            # Check ownership
            owner_user = active_instance.bot_instance_ref.owner
            persona_name = active_instance.bot_instance_ref.persona_config.name if active_instance.bot_instance_ref and active_instance.bot_instance_ref.persona_config else "Неизвестная"

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id} in chat {chat_id}.")
                await update.message.reply_text("Только владелец личности может снять заглушку.", reply_markup=ReplyKeyboardRemove())
                return

            # Perform unmute
            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit() # Commit unmute status
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id} by user {user_id}.")
                await update.message.reply_text(f"✅ Личность '{persona_name}' снова может отвечать в этом чате.", reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(f"Личность '{persona_name}' не была заглушена.", reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            # Rollback handled by context manager
            await update.message.reply_text("Ошибка базы данных при попытке вернуть бота к общению.")
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("Непредвиденная ошибка при выполнении команды.")
