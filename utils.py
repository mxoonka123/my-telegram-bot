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
    V5: Handles different newline types (\n, \r\n, \r) for splitting.
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

    # --- ОТЛАДКА: Выводим репрезентацию строки ---
    logger.debug(f"DEBUG V5: Input response repr(): {repr(response)}")
    # --- КОНЕЦ ОТЛАДКИ ---

    parts = []
    processed_by_newline = False

    # Пробуем двойные переносы
    try:
        # --- ОТЛАДКА: Выводим результат re.split ДО очистки ---
        raw_split_double = re.split(r'(?:\r?\n){2,}|\r{2,}', response)
        logger.debug(f"DEBUG V5: Raw re.split for double newlines: {raw_split_double}")
        # --- КОНЕЦ ОТЛАДКИ ---
        potential_parts_double = [p.strip() for p in raw_split_double if p.strip()]
        if len(potential_parts_double) > 1:
            logger.info(f"Split by DOUBLE newlines (any type) resulted in {len(potential_parts_double)} parts.")
            parts = potential_parts_double
            processed_by_newline = True
    except Exception as e_re_double:
        logger.error(f"Error during re.split for double newlines: {e_re_double}", exc_info=True)


    # Пробуем одинарные, если двойные не сработали
    if not processed_by_newline:
        try:
            # --- ОТЛАДКА: Выводим результат re.split ДО очистки ---
            raw_split_single = re.split(r'\r?\n|\r', response)
            logger.debug(f"DEBUG V5: Raw re.split for single newlines: {raw_split_single}")
            # --- КОНЕЦ ОТЛАДКИ ---
            potential_parts_single = [p.strip() for p in raw_split_single if p.strip()]
            if len(potential_parts_single) > 1: # Используем > 1, т.к. если split ничего не нашел, вернется список с 1 элементом (вся строка)
                logger.info(f"Split by SINGLE newlines (any type) resulted in {len(potential_parts_single)} parts.")
                parts = potential_parts_single
                processed_by_newline = True
            else:
                # Если single split вернул 1 или 0 частей, значит переносов нет
                logger.info("Split by SINGLE newlines did not find multiple parts.")
                # parts остается пустым, если был пуст до этого
        except Exception as e_re_single:
            logger.error(f"Error during re.split for single newlines: {e_re_single}", exc_info=True)

    # 3. Если переносов не было ИЛИ их было мало, И текст достаточно длинный
    needs_aggressive_split = False
    if not processed_by_newline:
         needs_aggressive_split = True
         logger.info("No newline splits found. Proceeding to aggressive split.")
         text_to_split_aggressively = response # Делим весь ответ
         max_aggressive_parts = max_messages
         remaining_parts = []
    elif len(parts) < max_messages and len(response) > 150 * len(parts): # Мало частей, но текст длинный
         needs_aggressive_split = True
         logger.info(f"Only {len(parts)} newline parts found, but response is long. Using aggressive split for the longest part.")
         parts.sort(key=len, reverse=True)
         text_to_split_aggressively = parts.pop(0)
         remaining_parts = parts
         max_aggressive_parts = max(1, max_messages - len(remaining_parts)) # Нужно хотя бы 1 часть
         parts = [] # Очищаем для результата
    else:
        # Переносов достаточно, агрессивная не нужна
        text_to_split_aggressively = ""
        max_aggressive_parts = 0
        remaining_parts = []

    # 4. Если после всех попыток частей нет или одна
    if not parts:
        logger.warning("Could not split response using any method.")
        if len(response) > telegram_max_len:
            logger.warning(f"Single message required, but response too long ({len(response)}). Truncating.")
            return [response[:telegram_max_len - 3] + "..."]
        else:
             return [response]

    # 5. Объединяем части, если их БОЛЬШЕ чем max_messages
    final_messages = []
    if needs_aggressive_split and text_to_split_aggressively:
        logger.debug(f"Aggressively splitting text block (len={len(text_to_split_aggressively)}). Need {max_aggressive_parts} parts.")
        aggressive_parts = []
        # --- УПРОЩЕННАЯ АГРЕССИВНАЯ РАЗБИВКА ---
        estimated_len = math.ceil(len(text_to_split_aggressively) / max_aggressive_parts)
        start = 0
        for i in range(max_aggressive_parts):
            # Не выходим за пределы строки
            end = min(start + estimated_len, len(text_to_split_aggressively))
            # Если это последняя часть, берем все до конца
            if i == max_aggressive_parts - 1:
                end = len(text_to_split_aggressively)

            # Ищем пробел для разрыва, если это не конец строки
            if end < len(text_to_split_aggressively):
                space_pos = text_to_split_aggressively.rfind(' ', start, end)
                # Если нашли пробел не в самом начале, используем его
                if space_pos > start:
                    end = space_pos + 1

            part = text_to_split_aggressively[start:end].strip()
            if part:
                aggressive_parts.append(part)
            start = end # Передвигаем начало следующей части
            if start >= len(text_to_split_aggressively): # Выходим, если дошли до конца
                break
        # --- КОНЕЦ УПРОЩЕННОЙ РАЗБИВКИ ---
        logger.info(f"Aggressive splitting created {len(aggressive_parts)} parts.")
        parts = aggressive_parts + remaining_parts
        parts = [p for p in parts if p]
        if len(parts) > parts_taken and final_messages:
             last_msg = final_messages[-1].rstrip('.!?… ')
             final_messages[-1] = f"{last_msg}..."


    else:
        # Если частей меньше или равно max_messages, используем их как есть
        final_messages = parts

    # 6. Финальная проверка длины каждой части
    processed_messages = []
    for msg in final_messages:
        # Убираем пустые строки в начале/конце и лишние пробелы
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue # Пропускаем пустые сообщения

        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part still exceeds Telegram limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)
        logger.warning(f"Trimming final parts from {len(parts)} to {max_messages}.")
        final_messages = parts[:max_messages]
        # Добавляем многоточие к последней части, если она не пустая
        if final_messages and final_messages[-1]:
             last_part = final_messages[-1].rstrip('.!?… ')
             final_messages[-1] = f"{last_part}..."
    else:
        final_messages = parts

    # 6. Дополнительная проверка длины каждой части (на всякий случай)
    processed_messages = []
    for msg in final_messages:
        if len(msg) > 4096:
            logger.warning(f"Message part still exceeds Telegram limit ({len(msg)} > 4096). Truncating.")
            processed_messages.append(msg[:4093] + "...")
        else:
            processed_messages.append(msg)

    logger.info(f"Final processed messages V5 count: {len(processed_messages)}")
    return processed_messages
