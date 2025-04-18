import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction
from telegram.ext import Application
from typing import List, Dict, Any, Optional, Union, Tuple


from db import get_all_active_chat_bot_instances, SessionLocal
from persona import Persona
from utils import postprocess_response, extract_gif_links
from handlers import send_to_langdock

logger = logging.getLogger(__name__)

async def spam_task(application: Application):
    """Фоновая задача для отправки случайных сообщений от активных личностей."""
    logger.info("Spam task started.")

    while True:
        await asyncio.sleep(random.randint(60, 300))

        with SessionLocal() as db:
            active_chat_bots = get_all_active_chat_bot_instances(db)

        if not active_chat_bots:
            await asyncio.sleep(60)
            continue

        random.shuffle(active_chat_bots)

        for chat_bot_instance in active_chat_bots:
            try:
                persona_config = chat_bot_instance.bot_instance_ref.persona_config
                if not persona_config:
                     logger.error(f"No persona config found for BotInstance {chat_bot_instance.bot_instance_id} linked to chat {chat_bot_instance.chat_id} for spam task.")
                     continue

                persona = Persona(persona_config, chat_bot_instance)

                if not persona.spam_prompt_template:
                     continue

                if random.random() < 0.5:
                    continue

                logger.info(f"Running spam task for chat {chat_bot_instance.chat_id} with persona {persona.name}.")

                spam_prompt = persona.format_spam_prompt()

                response_text = await send_to_langdock(spam_prompt, [])

                if not response_text or not response_text.strip():
                     logger.warning(f"Spam task got empty response for chat {chat_bot_instance.chat_id}, persona {persona.name}.")
                     continue

                full_bot_response_text = response_text
                responses_parts = postprocess_response(full_bot_response_text)
                all_text_content = " ".join(responses_parts)
                gif_links = extract_gif_links(all_text_content)

                for gif in gif_links:
                    try:
                        await application.bot.send_animation(chat_id=chat_bot_instance.chat_id, animation=gif)
                        all_text_content = all_text_content.replace(gif, "").strip()
                    except Exception as e:
                        logger.error(f"Ошибка отправки гифки {gif}: {e}")

                text_parts_to_send = [part.strip() for part in re.split(r'(?<=[.!?…])\s+', all_text_content) if part.strip()]

                if not text_parts_to_send:
                     logger.warning(f"Spam task resulted in no text parts after processing for chat {chat_bot_instance.chat_id}, persona {persona.name}.")
                     continue

                for i, part in enumerate(text_parts_to_send):
                    await application.bot.send_chat_action(chat_id=chat_bot_instance.chat_id, action=ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(0.8, 1.5) + len(part) / 60)
                    if part:
                        await application.bot.send_message(chat_id=chat_bot_instance.chat_id, text=part)
                    if i < len(text_parts_to_send) - 1:
                        await asyncio.sleep(random.uniform(0.5, 1.5))

            except httpx.HTTPStatusError as e:
                logger.error(f"Langdock API HTTP error in spam task for chat {chat_bot_instance.chat_id}, persona {persona.name}: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                logger.error(f"General error in spam task for chat {chat_bot_instance.chat_id}, persona {persona.name}: {e}", exc_info=True)

            await asyncio.sleep(random.uniform(5, 15))

        await asyncio.sleep(random.randint(120, 600))