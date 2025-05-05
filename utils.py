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

# --- СУПЕР ПРОСТАЯ ВЕРСИЯ V6 ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    # --- ДОБАВЛЯЕМ ЭТОТ ЛОГ ---
    logger.error("!!!!!!!!! UTILS: postprocess_response V6 CALLED !!!!!!!!!!")
    # --- КОНЕЦ ДОБАВЛЕНИЯ ---

    telegram_max_len = 4096
    if not response or not isinstance(response, str): return []
    response = response.strip()
    if not response: return []
    if max_messages <= 0: max_messages = 1
    if max_messages == 1: return [response[:telegram_max_len]]

    logger.info(f"--- Postprocessing response V6 (SUPER SIMPLE TEST) --- Max messages: {max_messages}")
    logger.debug(f"V6 DEBUG: Input response repr(): {repr(response)}")

    # Пытаемся разделить ЛЮБЫМИ переносами (\n или \r или \r\n)
    # Используем стандартный splitlines() - он должен работать надежнее всего
    potential_parts = response.splitlines() # Разделяет по \n, \r, \r\n
    logger.debug(f"V6 DEBUG: response.splitlines() result: {potential_parts}")

    # Убираем пустые строки ПОСЛЕ разделения
    parts = [p.strip() for p in potential_parts if p.strip()]
    logger.debug(f"V6 DEBUG: Parts after stripping empty: {parts}")

    if len(parts) > 1:
        logger.info(f"V6 SUCCESS: Split by newlines (using splitlines()) resulted in {len(parts)} parts.")
        # Если частей больше чем нужно, просто берем первые max_messages
        if len(parts) > max_messages:
            logger.info(f"V6 Trimming parts from {len(parts)} to {max_messages}")
            final_messages = parts[:max_messages]
            # Добавляем многоточие к последней
            if final_messages and final_messages[-1]:
                 last_part = final_messages[-1].rstrip('.!?… ')
                 final_messages[-1] = f"{last_part}..."
        else:
            final_messages = parts
    else:
        # Если splitlines() не разделил (значит, переносов ТОЧНО нет)
        logger.warning("V6 WARNING: splitlines() did not find multiple parts. Response seems to be a single line.")
        # Возвращаем как есть (или обрезаем, если слишком длинное)
        if len(response) > telegram_max_len:
            final_messages = [response[:telegram_max_len-3] + "..."]
        else:
            final_messages = [response]

    # Финальная проверка длины (на всякий случай)
    processed_messages = []
    for msg in final_messages:
        if len(msg) > telegram_max_len:
             processed_messages.append(msg[:telegram_max_len-3] + "...")
        else:
             processed_messages.append(msg)

    logger.info(f"Final processed messages V6 count: {len(processed_messages)}")
    return processed_messages
# --- КОНЕЦ ВЕРСИИ V6 ---
