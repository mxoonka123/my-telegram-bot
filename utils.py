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
    V7: Prioritizes LLM's newlines (\n\n or \n). Aggressive split as last resort.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    if max_messages <= 0:
        max_messages = random.randint(1, 3) # Случайное, если настройка 0 или меньше
        logger.info(f"Max messages set to random (1-3), chosen: {max_messages}")
    elif max_messages > 10: # Ограничим сверху 10 сообщениями
        logger.warning(f"Max messages ({max_messages}) exceeds limit 10. Setting to 10.")
        max_messages = 10

    if max_messages == 1:
        if len(response) > telegram_max_len:
            return [response[:telegram_max_len - 3] + "..."]
        else:
            return [response]

    logger.info(f"--- Postprocessing response V7 --- Max messages: {max_messages}")
    parts = []
    processed_by_newline = False

    # 1. Ищем \n\n
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        logger.info(f"Split by DOUBLE newlines resulted in {len(potential_parts_double)} parts.")
        parts = potential_parts_double
        processed_by_newline = True

    # 2. Ищем \n, если не нашли \n\n
    if not processed_by_newline:
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            logger.info(f"Split by SINGLE newlines resulted in {len(potential_parts_single)} parts.")
            parts = potential_parts_single
            processed_by_newline = True
        else:
            logger.info("Did not find any type of newline splits.")

    # 3. Агрессивная разбивка ТОЛЬКО если переносов не было СОВСЕМ
    if not processed_by_newline:
        logger.warning("No newlines found in LLM response. Using aggressive splitting.")
        aggressive_parts = []
        # Используем упрощенную агрессивную разбивку из V5/V4
        estimated_len = math.ceil(len(response) / max_messages)
        estimated_len = max(estimated_len, 50)
        estimated_len = min(estimated_len, telegram_max_len - 10)
        start = 0
        for i in range(max_messages):
            end = min(start + estimated_len, len(response))
            if i == max_messages - 1: end = len(response)
            if end < len(response):
                space_pos = response.rfind(' ', start, end)
                if space_pos > start: end = space_pos + 1
            part = response[start:end].strip()
            if part: aggressive_parts.append(part)
            start = end
            if start >= len(response): break
        logger.info(f"Aggressive splitting created {len(aggressive_parts)} parts.")
        parts = aggressive_parts # Результат агрессивной разбивки

    # 4. Если после всего частей нет
    if not parts:
        logger.warning("Could not split response using any method V7.")
        if len(response) > telegram_max_len:
             return [response[:telegram_max_len - 3] + "..."]
        else:
             return [response] # Возвращаем как есть, если она не пустая

    # 5. ОБРЕЗАЕМ (Trimming), если частей БОЛЬШЕ чем max_messages
    #    (Не объединяем, чтобы сохранить структуру LLM, если она была)
    final_messages = []
    if len(parts) > max_messages:
        logger.info(f"Trimming parts from {len(parts)} down to {max_messages}.")
        final_messages = parts[:max_messages]
        # Добавляем многоточие к последней части
        if final_messages and final_messages[-1]:
             last_part = final_messages[-1].rstrip('.!?… ')
             # Добавляем многоточие, только если оно там не подразумевается
             if not last_part.endswith('...'):
                 final_messages[-1] = f"{last_part}..."
    else:
        # Если частей меньше или равно, используем все, что есть
        final_messages = parts

    # 6. Финальная проверка длины и очистка
    processed_messages = []
    for msg in final_messages:
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue
        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part still exceeds limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    logger.info(f"Final processed messages V7 count: {len(processed_messages)}")
    return processed_messages
