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
    V5: Handles different newline types (\n, \r\n, \r) for splitting
    and uses aggressive splitting if needed.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    if max_messages <= 0:
        max_messages = 1

    if max_messages == 1:
        if len(response) > telegram_max_len:
            return [response[:telegram_max_len - 3] + "..."]
        else:
            return [response]

    logger.info(f"--- Postprocessing response V5 --- Max messages: {max_messages}")
    parts = []
    processed_by_newline = False

    # 1. Пробуем двойные переносы
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        logger.info(f"Split by DOUBLE newlines (any type) resulted in {len(potential_parts_double)} parts.")
        parts = potential_parts_double
        processed_by_newline = True

    # 2. Пробуем одинарные, если двойные не сработали
    if not processed_by_newline:
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            logger.info(f"Split by SINGLE newlines (any type) resulted in {len(potential_parts_single)} parts.")
            parts = potential_parts_single
            processed_by_newline = True
        else:
             logger.info("Split by SINGLE newlines did not find multiple parts.")

    # 3. Если переносов не было ИЛИ их мало, И текст достаточно длинный -> Агрессивная разбивка
    needs_aggressive_split = False
    text_to_split_aggressively = ""
    max_aggressive_parts = 0
    remaining_parts = []

    if not processed_by_newline:
         needs_aggressive_split = True
         logger.info("No valid newline splits found. Proceeding to aggressive split.")
         text_to_split_aggressively = response
         max_aggressive_parts = max_messages
    elif len(parts) < max_messages and len(response) > 150 * len(parts):
         needs_aggressive_split = True
         logger.info(f"Only {len(parts)} newline parts found ({len(response)} chars). Using aggressive split for the longest part.")
         parts.sort(key=len, reverse=True)
         text_to_split_aggressively = parts.pop(0)
         remaining_parts = parts
         max_aggressive_parts = max(1, max_messages - len(remaining_parts))
         parts = [] # Очищаем, будем собирать заново

    if needs_aggressive_split and text_to_split_aggressively:
        logger.debug(f"Aggressively splitting text block (len={len(text_to_split_aggressively)}). Need {max_aggressive_parts} parts.")
        aggressive_parts = []
        estimated_len = math.ceil(len(text_to_split_aggressively) / max_aggressive_parts)
        estimated_len = max(estimated_len, 50) # Мин. длина
        estimated_len = min(estimated_len, telegram_max_len - 10) # Макс. длина

        start = 0
        for i in range(max_aggressive_parts):
            end = min(start + estimated_len, len(text_to_split_aggressively))
            if i == max_aggressive_parts - 1: end = len(text_to_split_aggressively)

            # Ищем пробел назад для разрыва (упрощенный вариант V4/V5)
            if end < len(text_to_split_aggressively):
                space_pos = text_to_split_aggressively.rfind(' ', start, end)
                if space_pos > start: end = space_pos + 1

            part = text_to_split_aggressively[start:end].strip()
            if part: aggressive_parts.append(part)
            start = end
            if start >= len(text_to_split_aggressively): break

        logger.info(f"Aggressive splitting created {len(aggressive_parts)} parts.")
        parts = aggressive_parts + remaining_parts
        parts = [p for p in parts if p]

    # 4. Если после всех попыток частей нет
    if not parts:
        logger.warning("Could not split response using any method V5.")
        if len(response) > telegram_max_len:
             return [response[:telegram_max_len - 3] + "..."]
        else:
             return [response]

    # 5. Объединяем части, если их БОЛЬШЕ чем max_messages
    final_messages = []
    if len(parts) > max_messages:
        logger.info(f"Merging {len(parts)} parts down to {max_messages}.")
        parts_per_message_exact = len(parts) / max_messages
        parts_taken = 0
        for i in range(max_messages):
            start_index = round(parts_taken)
            end_index_exact = round(parts_taken + parts_per_message_exact)
            end_index = min(end_index_exact, len(parts))
            if start_index >= end_index: break
            merged_part = "\n\n".join(parts[start_index:end_index])
            if len(merged_part) <= telegram_max_len:
                 final_messages.append(merged_part)
            else:
                 first_part_of_group = parts[start_index]
                 if len(first_part_of_group) > telegram_max_len:
                     first_part_of_group = first_part_of_group[:telegram_max_len - 3] + "..."
                 final_messages.append(first_part_of_group)
                 if len(final_messages) >= max_messages: break
            parts_taken += (end_index - start_index)
            if len(final_messages) >= max_messages: break
        if len(parts) > parts_taken and final_messages:
             last_msg = final_messages[-1].rstrip('.!?… ')
             final_messages[-1] = f"{last_msg}..."
    else:
        final_messages = parts

    # 6. Финальная проверка длины и очистка
    processed_messages = []
    for msg in final_messages:
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue
        if len(msg_cleaned) > telegram_max_len:
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    logger.info(f"Final processed messages V5 count: {len(processed_messages)}")
    return processed_messages
