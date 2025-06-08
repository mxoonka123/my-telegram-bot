"""
Исправленные функции для Telegram бота
- handle_message - обновленная версия с улучшенной обработкой LLM ответов
- process_and_send_response - обновленная версия с надежным разбором JSON
"""

import asyncio
import json
import logging
import random
import re
from typing import Optional, Union, List

from sqlalchemy.orm import Session
from telegram import Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import MAX_USER_MESSAGE_LENGTH_CHARS, OPENROUTER_API_KEY, OPENROUTER_API_BASE_URL, OPENROUTER_MODEL_NAME
from db import get_db, get_persona_and_context_with_owner, add_message_to_context
from handlers_helpers import check_channel_subscription, send_subscription_required_message, send_limit_exceeded_message # Предполагается, что эти хелперы существуют
from openai import AsyncOpenAI, OpenAIError
from persona import Persona
from utils import escape_markdown_v2, extract_gif_links, postprocess_response
from sqlalchemy.exc import SQLAlchemyError


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
    Processes LLM response, robustly handling JSON and fallbacks. (v2)
    Sends parts sequentially. Adds original FULL response to context.
    """
    logger.info(f"process_and_send_response [v2]: --- ENTER --- ChatID: {chat_id}, Persona: '{persona.name}'")
    if not full_bot_response_text or not full_bot_response_text.strip():
        logger.warning(f"process_and_send_response [v2]: Received empty response. Not processing.")
        return False

    raw_llm_response = full_bot_response_text.strip()
    context_response_prepared = False

    # 1. Save RAW response to context
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", raw_llm_response)
            context_response_prepared = True
            logger.debug(f"process_and_send_response [v2]: Raw response added to context for CBI {persona.chat_instance.id}.")
        except Exception as e:
            logger.error(f"DB Error preparing assistant response for context: {e}", exc_info=True)
            context_response_prepared = False # Don't commit if this failed
    else:
        logger.error("Cannot add raw response to context, chat_instance is None.")

    # 2. Parse the response
    text_parts_to_send = None

    def _robust_json_parser(text: str) -> Optional[List[str]]:
        """Tries to extract and parse a JSON list of strings from messy LLM output."""
        # First, try to find a markdown block
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        
        # Iteratively try to load JSON, max 5 levels of stringification
        for _ in range(5):
            try:
                # Try to load what we have
                data = json.loads(text)
                
                if isinstance(data, list):
                    # It's a list. Convert all items to string and filter out empty ones.
                    unwrapped_parts = [str(item).strip() for item in data if str(item).strip()]
                    if unwrapped_parts:
                        logger.info(f"Robust parser: Successfully parsed list with {len(unwrapped_parts)} items.")
                        return unwrapped_parts
                
                if isinstance(data, str):
                    # It's a string within a string, loop again
                    text = data
                    continue
                
                # It's some other valid JSON (dict, int). Convert to string and return.
                return [str(data)]

            except (json.JSONDecodeError, TypeError):
                # This text is not valid JSON. Stop trying.
                return None
        return None # Exceeded max depth

    text_parts_to_send = _robust_json_parser(raw_llm_response)

    # 3. Fallback to sentence splitting if JSON parsing failed
    if text_parts_to_send is None:
        logger.warning("process_and_send_response [v2]: JSON parse failed. Falling back to sentence splitting.")
        # We need to remove potential GIFs from the raw response before splitting
        text_without_gifs = raw_llm_response
        gif_links = extract_gif_links(raw_llm_response)
        if gif_links:
            for gif in gif_links:
                text_without_gifs = re.sub(r'\s*' + re.escape(gif) + r'\s*', ' ', text_without_gifs, flags=re.IGNORECASE)
        
        text_without_gifs = re.sub(r'\s{2,}', ' ', text_without_gifs).strip()
        
        # Use the utility to split the text
        if text_without_gifs:
            max_messages = persona.max_response_messages if persona.max_response_messages > 0 else 3
            text_parts_to_send = postprocess_response(text_without_gifs, max_messages)
        else:
            text_parts_to_send = [] # No text left after removing GIFs

    # 4. Extract GIFs and prepare for sending
    gif_links_to_send = extract_gif_links(raw_llm_response)

    if not text_parts_to_send and not gif_links_to_send:
        logger.warning("process_and_send_response [v2]: No text parts or GIFs found after processing. Nothing to send.")
        return context_response_prepared

    # 5. Send messages sequentially
    first_message_sent = False
    chat_id_str = str(chat_id)
    chat_type = update.effective_chat.type if update and update.effective_chat else None

    # Send GIFs first
    for gif_url in gif_links_to_send:
        try:
            current_reply_id = reply_to_message_id if not first_message_sent else None
            await context.bot.send_animation(chat_id=chat_id_str, animation=gif_url, reply_to_message_id=current_reply_id)
            first_message_sent = True
            await asyncio.sleep(random.uniform(0.5, 1.2))
        except Exception as e:
            logger.error(f"Error sending gif {gif_url} to chat {chat_id_str}: {e}", exc_info=True)

    # Send text parts
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
                break # Stop sending if a part fails
        except Exception as e:
            logger.error(f"Unexpected error sending part {i+1}: {e}", exc_info=True)
            break

        first_message_sent = True
        if len(text_parts_to_send) > 1:
            await asyncio.sleep(random.uniform(0.5, 1.0))

    logger.info(f"process_and_send_response [v2]: --- EXIT --- Returning context_prepared_status: {context_response_prepared}")
    return context_response_prepared


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages. (v2 - Simplified LLM response handling)"""
    logger.info("!!! VERSION CHECK: Running with Simplified JSON response handling (2024-06-08) !!!")
    if not update.message or not (update.message.text or update.message.caption):
        logger.debug("handle_message: Exiting - No message or text/caption.")
        return

    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_text = (update.message.text or update.message.caption or "").strip()
    message_id = update.message.message_id
    
    # ... other handle_message code ...
    
    # --- LLM Request ---
    open_ai_client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_API_BASE_URL)
    assistant_response_text = None
    llm_call_succeeded = False
    
    try:
        # Construct messages for OpenAI compatible API
        # ... your context formation code here ...
        
        # Example:
        # formatted_messages_for_llm = [{"role": "system", "content": system_prompt}]
        # formatted_messages_for_llm.extend(context_for_ai)

        llm_response = await open_ai_client.chat.completions.create(
            model=OPENROUTER_MODEL_NAME,
            messages=formatted_messages_for_llm, # Make sure this is correctly populated
            temperature=persona.config.temperature if persona.config.temperature is not None else 0.7,
            top_p=persona.config.top_p if persona.config.top_p is not None else 1.0,
            max_tokens=2048 # Generous limit
        )
        
        # *** CHANGE HERE ***
        # Simply take the raw response from the LLM. No json.dumps or json.loads.
        assistant_response_text = llm_response.choices[0].message.content.strip()
        
        llm_call_succeeded = True
        logger.info(f"LLM Raw Response (CBI {persona.chat_instance.id}): '{assistant_response_text[:300]}...'")

    except OpenAIError as e:
        logger.error(f"OpenRouter API error (CBI {persona.chat_instance.id}): {e}", exc_info=True)
        error_message_to_user = "Произошла ошибка при обращении к нейросети. Попробуйте немного позже."
        await update.message.reply_text(error_message_to_user, parse_mode=None)
        # db_session.commit() # Commit context and limit changes even on API failure
        return
    
    # ... rest of handle_message code ...

    if not llm_call_succeeded or not assistant_response_text:
        logger.warning(f"handle_message: LLM call failed or returned empty text. Not processing response. CBI: {persona.chat_instance.id}")
        # db_session.commit() # Commit changes even if response is not sent
        return
        
    # Process and send the response using the new robust function
    # ... and so on ...