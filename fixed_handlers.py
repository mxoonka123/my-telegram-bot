"""
Исправленные функции для Telegram бота (ФИНАЛЬНАЯ ВЕРСИЯ 3)
- handle_message - окончательно упрощен, передает "сырой" ответ от LLM.
- process_and_send_response - содержит надежный парсер и исправленную логику сохранения контекста.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Optional, Union, List

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from telegram import Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from openai import AsyncOpenAI, OpenAIError

# Убедитесь, что все импорты на месте
import config
from db import get_db, get_persona_and_context_with_owner, add_message_to_context, MAX_CONTEXT_MESSAGES_SENT_TO_LLM
# Эти функции должны быть определены в вашем файле handlers.py или импортированы
from handlers import check_channel_subscription, send_subscription_required_message, send_limit_exceeded_message
from persona import Persona
from utils import escape_markdown_v2, extract_gif_links, postprocess_response

logger = logging.getLogger(__name__)


async def process_and_send_response(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: Union[str, int],
    persona: Persona,
    full_bot_response_text: str,
    db: Session,
    reply_to_message_id: Optional[int] = None,
    is_first_message: bool = False
) -> bool:
    """
    Processes LLM response, robustly handling JSON and fallbacks. (v3 - Context Fix)
    Saves CLEANED response to context. Sends parts sequentially.
    """
    logger.info(f"process_and_send_response [v3]: --- ENTER --- ChatID: {chat_id}, Persona: '{persona.name}'")
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"process_and_send_response [v3]: Received empty response. Not processing.")
        return False

    raw_llm_response = full_bot_response_text.strip()
    
    # 1. Parse the response to get clean text parts
    text_parts_to_send = None

    def _robust_json_parser(text: str) -> Optional[List[str]]:
        """Tries to extract and parse a JSON list of strings from messy LLM output."""
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        
        for _ in range(5):
            try:
                data = json.loads(text)
                
                if isinstance(data, list):
                    unwrapped_parts = [str(item).strip() for item in data if str(item).strip()]
                    if unwrapped_parts:
                        logger.info(f"Robust parser: Successfully parsed list with {len(unwrapped_parts)} items.")
                        return unwrapped_parts
                
                if isinstance(data, str):
                    text = data
                    continue
                
                return [str(data)]

            except (json.JSONDecodeError, TypeError):
                return None
        return None

    text_parts_to_send = _robust_json_parser(raw_llm_response)
    
    # 2. Prepare content for DB and for sending
    content_to_save_in_db = ""
    if text_parts_to_send is not None:
        # Success parsing! Save clean, joined text to DB.
        content_to_save_in_db = "\n".join(text_parts_to_send)
        logger.info(f"Saving CLEAN response to context: '{content_to_save_in_db[:100]}...'")
    else:
        # Parse failed. Save raw response to DB, assuming it's plain text.
        content_to_save_in_db = raw_llm_response
        logger.warning(f"JSON parse failed. Saving RAW response to context: '{content_to_save_in_db[:100]}...'")
        
        # And generate parts for sending from this raw text.
        text_without_gifs = raw_llm_response
        gif_links = extract_gif_links(raw_llm_response)
        if gif_links:
            for gif in gif_links:
                text_without_gifs = re.sub(r'\s*' + re.escape(gif) + r'\s*', ' ', text_without_gifs, flags=re.IGNORECASE)
        text_without_gifs = re.sub(r'\s{2,}', ' ', text_without_gifs).strip()
        
        if text_without_gifs:
            # Используем max_response_messages из настроек персоны, с fallback на 3
            max_messages = persona.config.max_response_messages if persona.config and persona.config.max_response_messages > 0 else 3
            text_parts_to_send = postprocess_response(text_without_gifs, max_messages)
        else:
            text_parts_to_send = []

    # 3. Save the prepared content (clean or raw) to the DB
    context_response_prepared = False
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", content_to_save_in_db)
            context_response_prepared = True
            logger.debug(f"Response added to context for CBI {persona.chat_instance.id}.")
        except Exception as e:
            logger.error(f"DB Error saving assistant response to context: {e}", exc_info=True)
    else:
        logger.error("Cannot add response to context, chat_instance is None.")

    # 4. Sequentially send messages
    gif_links_to_send = extract_gif_links(raw_llm_response)

    if not text_parts_to_send and not gif_links_to_send:
        logger.warning("process_and_send_response [v3]: No text parts or GIFs found after processing. Nothing to send.")
        return context_response_prepared

    first_message_sent = False
    chat_id_str = str(chat_id)
    chat_type = update.effective_chat.type if update and update.effective_chat else None

    # Send GIFs
    for gif_url in gif_links_to_send:
        try:
            current_reply_id = reply_to_message_id if not first_message_sent else None
            await context.bot.send_animation(chat_id=chat_id_str, animation=gif_url, reply_to_message_id=current_reply_id)
            first_message_sent = True
            await asyncio.sleep(random.uniform(0.5, 1.2))
        except Exception as e:
            logger.error(f"Error sending gif {gif_url} to chat {chat_id_str}: {e}", exc_info=True)

    # Send Text
    for i, part in enumerate(text_parts_to_send):
        part_raw = part.strip()
        if not part_raw: continue

        if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            try:
                await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(0.8, 1.5))
            except Exception: pass

        current_reply_id = reply_to_message_id if not first_message_sent else None
        
        try:
            escaped_part = escape_markdown_v2(part_raw)
            await context.bot.send_message(
                chat_id=chat_id_str, text=escaped_part, parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=current_reply_id
            )
        except BadRequest as e_md:
            logger.warning(f"MDv2 parse failed for part {i+1}. Retrying as plain text. Error: {e_md}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id_str, text=part_raw, parse_mode=None,
                    reply_to_message_id=current_reply_id
                )
            except Exception as e_plain:
                logger.error(f"Failed to send part {i+1} even as plain text: {e_plain}", exc_info=True)
                break
        except Exception as e:
            logger.error(f"Unexpected error sending part {i+1}: {e}", exc_info=True)
            break

        first_message_sent = True
        if len(text_parts_to_send) > 1:
            await asyncio.sleep(random.uniform(0.5, 1.0))

    logger.info(f"process_and_send_response [v3]: --- EXIT --- Returning context_prepared_status: {context_response_prepared}")
    return context_response_prepared


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages. (v3 - Final)"""
    logger.info("!!! VERSION CHECK: Running with Context Fix (2024-06-09) !!!")
    if not update.message or not (update.message.text or update.message.caption):
        return

    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    message_id = update.message.message_id

    if len(message_text) > config.MAX_USER_MESSAGE_LENGTH_CHARS:
        await update.message.reply_text("Ваше сообщение слишком длинное. Пожалуйста, попробуйте его сократить.")
        return
    
    if not message_text:
        return

    logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str} (MsgID: {message_id}): '{message_text[:100]}'")
    
    if not await check_channel_subscription(update, context):
        await send_subscription_required_message(update, context)
        return

    db_session = None
    try:
        with get_db() as db:
            db_session = db
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db_session)
            if not persona_context_owner_tuple:
                return
            
            persona, initial_context_from_db, owner_user = persona_context_owner_tuple
            
            # ... (остальная логика: проверка лимитов, мьюта, группового чата) ...
            # Весь код до блока LLM Request остается без изменений
            if persona.config.media_reaction in ["all_media_no_text", "photo_only", "voice_only", "none"]:
                if not persona.chat_instance.is_muted:
                    try:
                        add_message_to_context(db_session, persona.chat_instance.id, "user", f"{username}: {message_text}")
                        db_session.commit()
                    except Exception as e:
                        logger.error(f"Error saving context for ignored text response: {e}", exc_info=True)
                        db_session.rollback()
                return

            now_utc = datetime.now(timezone.utc)
            current_month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if owner_user.message_count_reset_at is None or owner_user.message_count_reset_at < current_month_start:
                owner_user.monthly_message_count = 0
                owner_user.message_count_reset_at = current_month_start
                db_session.add(owner_user)

            if owner_user.monthly_message_count >= owner_user.message_limit:
                await send_limit_exceeded_message(update, context, owner_user)
                db_session.commit()
                return
            
            add_message_to_context(db_session, persona.chat_instance.id, "user", f"{username}: {message_text}")
            
            if persona.chat_instance.is_muted:
                db_session.commit()
                return

            should_ai_respond = True
            if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                reply_pref = persona.group_reply_preference
                bot_username = context.bot_data.get('bot_username', "NunuAiBot")
                is_mentioned = f"@{bot_username}".lower() in message_text.lower()
                is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
                contains_persona_name = bool(re.search(rf'(?i)\b{re.escape(persona.name.lower())}\b', message_text))

                if reply_pref == "never": should_ai_respond = False
                elif reply_pref == "mentioned_only" and not (is_mentioned or is_reply_to_bot or contains_persona_name): should_ai_respond = False
            
            if not should_ai_respond:
                db_session.commit()
                return

            # --- LLM Request ---
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
            
            system_prompt = persona.format_system_prompt(user_id, username, message_text)
            if not system_prompt:
                await update.message.reply_text(escape_markdown_v2("❌ ошибка при подготовке системного сообщения."), parse_mode=ParseMode.MARKDOWN_V2)
                db_session.rollback()
                return

            context_for_ai = initial_context_from_db + [{"role": "user", "content": f"{username}: {message_text}"}]
            
            open_ai_client = AsyncOpenAI(api_key=config.OPENROUTER_API_KEY, base_url=config.OPENROUTER_API_BASE_URL)
            assistant_response_text = None
            
            try:
                formatted_messages_for_llm = [{"role": "system", "content": system_prompt}]
                for msg in context_for_ai[-MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]:
                    role = "assistant" if msg["role"] != "user" else "user"
                    formatted_messages_for_llm.append({"role": role, "content": msg["content"]})
                
                llm_response = await open_ai_client.chat.completions.create(
                    model=config.OPENROUTER_MODEL_NAME,
                    messages=formatted_messages_for_llm,
                    temperature=persona.config.temperature if persona.config.temperature is not None else 0.7,
                    top_p=persona.config.top_p if persona.config.top_p is not None else 1.0,
                    max_tokens=2048
                )
                
                # ---- ГЛАВНОЕ ИЗМЕНЕНИЕ ----
                # Просто берем сырой ответ. Никаких проверок на JSON, никакого оборачивания.
                assistant_response_text = llm_response.choices[0].message.content.strip()
                logger.info(f"LLM Raw Response (CBI {persona.chat_instance.id}): '{assistant_response_text[:300]}...'")

            except OpenAIError as e:
                # ... (обработка ошибок без изменений) ...
                await update.message.reply_text("Произошла ошибка при обращении к нейросети. Попробуйте немного позже.")
                db_session.commit()
                return


            if not assistant_response_text:
                # ... (обработка пустого ответа без изменений) ...
                await update.message.reply_text("Модель не дала содержательного ответа. Попробуйте переформулировать запрос.")
                db_session.commit()
                return

            # --- Process and Send Response ---
            context_response_prepared = await process_and_send_response(
                update, context, chat_id_str, persona, assistant_response_text, db_session,
                reply_to_message_id=message_id, is_first_message=(len(initial_context_from_db) == 0)
            )

            # ... (инкремент счетчика и коммит без изменений) ...
            owner_user.monthly_message_count += 1
            db_session.add(owner_user)
            logger.info(f"Incremented monthly message count for user {owner_user.id} to {owner_user.monthly_message_count}")
            
            db_session.commit()
            logger.info(f"handle_message: Successfully processed message and committed changes for chat {chat_id_str}.")

            
    except SQLAlchemyError as e:
        # ... (обработка ошибок без изменений) ...
        logger.error(f"handle_message: SQLAlchemyError: {e}", exc_info=True)
        if update.effective_message:
            try: await update.effective_message.reply_text("❌ Ошибка базы данных. Попробуйте позже.")
            except Exception: pass
        if db_session: db_session.rollback()

    except Exception as e:
        # ... (обработка ошибок без изменений) ...
        logger.error(f"handle_message: Unexpected Exception: {e}", exc_info=True)
        if update.effective_message:
            try: await update.effective_message.reply_text("❌ Произошла непредвиденная ошибка.")
            except Exception: pass
        if db_session: db_session.rollback()