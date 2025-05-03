import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging

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


def postprocess_response(response: str, max_messages: int = 3) -> List[str]:
    """Splits the bot's response into suitable message parts, prioritizing newlines."""
    if not response or not isinstance(response, str):
        return []

    # 1. Normalize whitespace and remove leading/trailing markdown artifacts
    response = re.sub(r'\s+', ' ', response).strip().strip('_*`')
    if not response: return []

    # --- НОВАЯ ЛОГИКА: Приоритет \n ---
    # 2. Сначала пробуем разбить по двойному переносу строки (частый маркер сообщений)
    parts = [p.strip() for p in response.split('\n\n') if p.strip()]
    if len(parts) > 1:
        logger.debug(f"Split by '\\n\\n' into {len(parts)} parts.")
    else:
        # 3. Если двойной перенос не сработал, пробуем по одинарному
        parts = [p.strip() for p in response.split('\n') if p.strip()]
        if len(parts) > 1:
            logger.debug(f"Split by '\\n' into {len(parts)} parts.")
        else:
            # 4. Если и одинарный не сработал, используем старую логику по предложениям
            logger.debug("Splitting by sentences as fallback.")
            sentences = re.split(r'(?<=[.!?…])\s+', response)
            parts = [s.strip() for s in sentences if s.strip()]
            if not parts: # Если даже по предложениям не разбилось
                parts = [response] # Возвращаем исходный текст как одну часть
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    # 5. Объединяем/Обрезаем части, если их больше чем max_messages
    merged_messages = []
    current_message = ""
    max_length = 4000 # Telegram limit
    ideal_length = 1000 # Try to keep messages reasonably short

    for part in parts:
        if not part: continue

        # Если текущее сообщение пустое, просто начинаем его
        if not current_message:
            current_message = part
        # Если добавление следующей части не превышает идеальную длину
        elif len(current_message) + len(part) + 1 <= ideal_length:
            current_message += "\n" + part # Используем \n для соединения частей, если объединяем
        # Если добавление превышает идеальную, но не максимальную, и частей мало
        elif len(merged_messages) < max_messages - 1 and len(current_message) + len(part) + 1 <= max_length:
             # Завершаем текущее, начинаем новое
             merged_messages.append(current_message)
             current_message = part
        # Если превышает максимальную или уже набрали достаточно сообщений
        else:
            # Завершаем текущее, начинаем новое
            merged_messages.append(current_message)
            current_message = part

        # Принудительное разделение, если current_message слишком длинное
        while len(current_message) > max_length:
            split_point = current_message.rfind('\n', 0, max_length) # Ищем последний перенос строки
            if split_point == -1:
                split_point = current_message.rfind('.', 0, max_length) # Ищем точку
            if split_point == -1:
                split_point = current_message.rfind(' ', 0, max_length) # Ищем пробел
            if split_point == -1:
                split_point = max_length # Крайний случай

            merged_messages.append(current_message[:split_point].strip())
            current_message = current_message[split_point:].strip()
            if len(merged_messages) >= max_messages: # Прекращаем, если достигли лимита
                 current_message = "" # Обнуляем остаток
                 break

    # Добавляем последнюю часть, если она есть и лимит не достигнут
    if current_message and len(merged_messages) < max_messages:
        merged_messages.append(current_message)

    # 6. Окончательная очистка и возврат
    final_messages = [msg.strip() for msg in merged_messages if msg.strip()]

    # Обрезаем до max_messages, если все еще больше (на всякий случай)
    if len(final_messages) > max_messages:
        logger.warning(f"Postprocess still resulted in {len(final_messages)} parts, trimming to {max_messages}.")
        final_messages = final_messages[:max_messages]
        # Можно добавить "..." к последнему сообщению, если нужно
        if final_messages:
            final_messages[-1] = final_messages[-1].rstrip('. ') + "..."


    return final_messages if final_messages else ([response] if response else []) # Возвращаем исходное, если ничего не получилось
