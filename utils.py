import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging
import math

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: Optional[str]) -> str:
    """Escapes characters reserved in Telegram MarkdownV2."""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            logger.warning(f"Could not convert non-string value to string for Markdown escaping: {type(text)}")
            return ""
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Use a lambda function to handle escaping
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_time_info() -> str:
    """Gets formatted time string for different timezones."""
    now_utc = datetime.now(timezone.utc)
    time_parts = [f"UTC {now_utc.strftime('%H:%M %d.%m.%Y')}"]

    # Define timezones with their offsets from UTC
    # Note: These are fixed offsets and don't account for DST changes.
    # For production, consider using a library like `pytz` or `zoneinfo` (Python 3.9+)
    # if accurate DST handling is crucial.
    timezones_offsets = {
        "МСК": timedelta(hours=3),       # Moscow Time (fixed UTC+3)
        "Берлин": timedelta(hours=1),   # Central European Time (UTC+1, without DST)
        "Нью-Йорк": timedelta(hours=-5) # Eastern Standard Time (UTC-5, without DST)
    }

    for name, offset in timezones_offsets.items():
        try:
            # Create a fixed offset timezone object
            tz = timezone(offset)
            local_time = now_utc.astimezone(tz)
            time_parts.append(f"{name} {local_time.strftime('%H:%M %d.%m')}")
        except Exception as e:
             logger.warning(f"Could not calculate time for tz '{name}' with offset {offset}: {e}")
             time_parts.append(f"{name} N/A")

    return f"сейчас " + ", ".join(time_parts) + "."

def extract_gif_links(text: str) -> List[str]:
    """Extracts potential GIF links from text."""
    if not isinstance(text, str): return []
    try:
        decoded_text = urllib.parse.unquote(text)
    except Exception:
        decoded_text = text # Fallback

    # Updated patterns
    gif_patterns = [
        r'(https?://media[0-9]?\.giphy\.com/media/[a-zA-Z0-9]+/giphy\.gif)', # Giphy direct media links
        r'(https?://i\.giphy\.com/[a-zA-Z0-9]+\.gif)',                      # Giphy i.giphy links
        r'(https?://c\.tenor\.com/[a-zA-Z0-9]+/[a-zA-Z0-9]+\.gif)',        # Tenor direct c.tenor links
        r'(https?://media\.tenor\.com/[a-zA-Z0-9]+/[a-zA-Z0-9]+/AAA[AC]\.gif)', # Tenor media.tenor links
        r'(https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]+\.gif)',                # Imgur direct links
        r'(https?://[^\s<>"\']+\.gif(?:[?#][^\s<>"\']*)?)'                   # Generic direct .gif (must be last)
    ]

    gif_links = set()
    for pattern in gif_patterns:
         try:
             found = re.findall(pattern, decoded_text, re.IGNORECASE)
             for item in found:
                 # Only add strings, ignore potential tuples from older patterns if any
                 if isinstance(item, str):
                     gif_links.add(item)
         except Exception as e:
              logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")

    # Basic validation and return unique links (preserving order found somewhat)
    valid_links = [link for link in gif_links if link.startswith(('http://', 'https://')) and ' ' not in link]
    # Use dict.fromkeys to get unique links while preserving order
    return list(dict.fromkeys(valid_links))

def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits the bot's response into suitable message parts.
    V11: Prioritizes LLM newlines, then splits by sentences, ensuring
         sentence completeness within messages.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    # --- Handle max_messages setting ---
    original_max_setting = max_messages
    if max_messages <= 0:
        max_messages = random.randint(1, 3)
        logger.info(f"Max messages set to random (1-3) from setting {original_max_setting}. Chosen: {max_messages}")
    elif max_messages > 10:
        logger.warning(f"Max messages ({original_max_setting}) exceeds limit 10. Setting to 10.")
        max_messages = 10
    else:
        logger.info(f"Using max_messages setting: {max_messages}")
    # --- End max_messages handling ---

    if max_messages == 1:
        if len(response) > telegram_max_len: return [response[:telegram_max_len - 3] + "..."]
        else: return [response]

    logger.info(f"--- Postprocessing response V8 --- Max messages allowed: {max_messages}")
    initial_parts = []
    processed_by_newline = False

    # 1. Detect newlines (\n\n preferred, then \n using re.split for any type)
    # Try double newlines first
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        logger.info(f"Split by DOUBLE newlines resulted in {len(potential_parts_double)} parts.")
        initial_parts = potential_parts_double
        processed_by_newline = True
    else:
        # Try single newlines if double didn't work
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            logger.info(f"Split by SINGLE newlines resulted in {len(potential_parts_single)} parts.")
            initial_parts = potential_parts_single
            processed_by_newline = True
        else:
            logger.info("Did not find any type of newline splits in the response.")

    final_messages = []

    # 2. Process based on newline detection
    if processed_by_newline:
        # --- MERGE LOGIC if LLM provided too many parts ---
        if len(initial_parts) > max_messages:
            logger.info(f"LLM provided {len(initial_parts)} parts (more than max {max_messages}). Attempting to merge parts...")
            merged_parts = []
            current_part_text = ""
            # Iterate through parts provided by LLM's newlines
            for i, part in enumerate(initial_parts):
                separator = "\n\n" if current_part_text else "" # Use double newline for merging visual separation

                # Check if adding the next part fits within Telegram limit AND we haven't filled up allowed message slots (leaving one slot for the rest if possible)
                if len(current_part_text) + len(separator) + len(part) <= telegram_max_len and len(merged_parts) < max_messages - 1:
                    current_part_text += separator + part
                else:
                    # If current merged part is not empty, save it
                    if current_part_text:
                        merged_parts.append(current_part_text)
                    # Start the new part with the current 'part'
                    current_part_text = part
                    # Check if we have already reached the maximum number of messages allowed
                    if len(merged_parts) >= max_messages:
                        logger.warning(f"Reached max_messages ({max_messages}) during merge. Discarding part: '{part[:50]}...' and any further parts.")
                        current_part_text = "" # Discard this part too

        # Условие для доразбивки: частей меньше чем можно, И самая длинная часть достаточно велика
        if len(initial_parts) < max_messages and len(longest_part) > min_len_to_subsplit:
            logger.info(f"Newline parts ({len(initial_parts)}) < max_messages ({max_messages}) and longest part is long ({len(longest_part)} chars). Attempting sub-split.")
            # Сколько еще частей нам не хватает до лимита
            needed_more_parts = max_messages - len(remaining_short_parts)
            if needed_more_parts <= 1: # Если нужна всего одна или меньше, нет смысла бить
                 logger.info("Only 1 more part needed, keeping longest part as is.")
                 final_parts = initial_parts # Используем исходные части
            else:
                # Пытаемся разбить самую длинную часть на недостающее количество
                logger.debug(f"Aggressively splitting the longest part to get up to {needed_more_parts} sub-parts.")
                sub_parts = []
                estimated_len = math.ceil(len(longest_part) / needed_more_parts)
                estimated_len = max(estimated_len, 50)
                estimated_len = min(estimated_len, telegram_max_len - 10)
                start = 0
                for i in range(needed_more_parts):
                    end = min(start + estimated_len, len(longest_part))
                    if i == needed_more_parts - 1: end = len(longest_part)
                    if end < len(longest_part):
                        space_pos = longest_part.rfind(' ', start, end)
                        if space_pos > start: end = space_pos + 1
                    part_text = longest_part[start:end].strip()
                    if part_text: sub_parts.append(part_text)
                    start = end
                    if start >= len(longest_part): break
                logger.info(f"Sub-splitting created {len(sub_parts)} parts from the longest one.")
                # Собираем итоговый список: разбитые части + остальные короткие
                final_parts = sub_parts + remaining_short_parts
        else:
             # Частей достаточно или самая длинная часть короткая, используем как есть
             logger.info("Newline parts count is sufficient or longest part is short. Using newline parts.")
             final_parts = initial_parts # Используем исходные части

    # 3. Если переносы НЕ найдены -> Разбивка по предложениям
    else: # not processed_by_newline
        logger.warning("No newlines found. Attempting split by sentences.")
        # Используем lookbehind для сохранения знака препинания
        sentences = re.split(r'(?<=[.!?…])\s+', response)
        sentences = [s.strip() for s in sentences if s.strip()] # Очищаем пустые

        if not sentences: # Если регулярка ничего не нашла
             logger.error("Could not split response by sentences. Returning original.")
             # Возвращаем как есть (или обрезанное)
             if len(response) > telegram_max_len:
                 return [response[:telegram_max_len - 3] + "..."]
             else:
                 return [response]

        logger.info(f"Split by sentences resulted in {len(sentences)} potential sentences.")

        current_message_parts = []
        current_len = 0
        # Целевое количество предложений на сообщение (примерно)
        # Не менее 1, не более 5 (можно настроить)
        sentences_per_msg_target = max(1, min(5, math.ceil(len(sentences) / max_messages)))

        for i, sentence in enumerate(sentences):
            sentence_len = len(sentence)
            # Проверяем, поместится ли СЛЕДУЮЩЕЕ предложение в ТЕКУЩЕЕ сообщение
            # И не превышено ли целевое кол-во предложений (с небольшой гибкостью)
            # И не превышен ли лимит сообщений
            separator_len = 1 if current_message_parts else 0 # Пробел между предложениями

            # Условие для добавления предложения в текущее сообщение:
            # 1. Это первое предложение для этого сообщения.
            # ИЛИ
            # 2. Оно влезает по длине.
            # И
            # 3. Количество сообщений еще не достигло максимума.
            # И
            # 4. Количество предложений в текущем сообщении еще не слишком велико
            #    (позволяем чуть больше целевого, если влезает по длине)

            can_add = False
            if not current_message_parts: # Первое предложение всегда добавляем
                can_add = True
            elif current_len + separator_len + sentence_len <= telegram_max_len and \
                 len(final_messages) < max_messages: #and \
                 #len(current_message_parts) < sentences_per_msg_target + 1: # Не слишком много предложений
                 can_add = True

            if can_add:
                current_message_parts.append(sentence)
                current_len += separator_len + sentence_len
            else:
                # Если добавить нельзя, "закрываем" предыдущее сообщение
                if current_message_parts:
                    final_messages.append(" ".join(current_message_parts))
                # Начинаем новое сообщение с текущего предложения
                # Но только если мы еще не достигли лимита сообщений
                if len(final_messages) < max_messages:
                    current_message_parts = [sentence]
                    current_len = sentence_len
                else:
                    # Лимит сообщений достигнут, прекращаем обработку предложений
                    logger.warning(f"Reached max_messages ({max_messages}) during sentence assembly. Remaining sentences discarded.")
                    current_message_parts = [] # Очищаем, чтобы не добавилось в конце
                    break

        # Добавляем последнее собранное сообщение, если оно не пустое
        if current_message_parts and len(final_messages) < max_messages:
            final_messages.append(" ".join(current_message_parts))

        # Добавляем многоточие, если не все предложения влезли
        if len(final_messages) < len(sentences) and final_messages:
             last_msg = final_messages[-1].rstrip('.!?… ')
             if not last_msg.endswith('...'): final_messages[-1] = f"{last_msg}..."

        logger.info(f"Sentence splitting resulted in {len(final_messages)} messages.")

    # 4. Финальная проверка длины и очистка (ОБЯЗАТЕЛЬНО)
    processed_messages = []
    for msg in final_messages:
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue
        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part exceeds limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)
    processed_messages = []
    for msg in trimmed_messages:
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue
        if len(msg_cleaned) > telegram_max_len:
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    logger.info(f"Final processed messages V10 count: {len(processed_messages)}")
    if len(processed_messages) > max_messages:
        logger.warning(f"Final message count ({len(processed_messages)}) still exceeds max_messages ({max_messages}). Trimming final list.")
        processed_messages = processed_messages[:max_messages]
        # Add ellipsis if trimming happened here
        if processed_messages and processed_messages[-1] and not processed_messages[-1].endswith("..."):
             last_p = processed_messages[-1].rstrip('.!?… ')
             processed_messages[-1] = f"{last_p}..."


    logger.info(f"Final processed messages V8 count: {len(processed_messages)}")
    return processed_messages
