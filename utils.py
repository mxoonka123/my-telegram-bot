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
    Splits the bot's response into suitable message parts, trying harder
    if standard delimiters are missing. V3 - fixed potential \n split issue.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    if max_messages <= 0:
        max_messages = 1 # Если 0 или меньше, считаем как 1

    # Если нужно всего одно сообщение
    if max_messages == 1:
        if len(response) > telegram_max_len:
            logger.warning(f"Single message required, but response too long ({len(response)}). Truncating.")
            return [response[:telegram_max_len - 3] + "..."]
        else:
            return [response]

    logger.info(f"--- Postprocessing response V3 --- Max messages: {max_messages}")
    parts = []
    processed_by_newline = False

    # 1. Разделение по \n\n (приоритет)
    potential_parts_nn = [p.strip() for p in response.split('\n\n') if p.strip()]
    if len(potential_parts_nn) > 1:
        logger.info(f"Split by '\n\n' resulted in {len(potential_parts_nn)} parts.")
        parts = potential_parts_nn
        processed_by_newline = True

    # 2. Если не разделили по \n\n, пробуем по \n
    if not processed_by_newline:
        potential_parts_n = [p.strip() for p in response.split('\n') if p.strip()]
        # --- ИСПРАВЛЕНИЕ: Убедимся, что частей ДЕЙСТВИТЕЛЬНО больше одной ---
        if len(potential_parts_n) > 1:
            logger.info(f"Split by '\n' resulted in {len(potential_parts_n)} parts.")
            parts = potential_parts_n
            processed_by_newline = True
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    # 3. Если переносов не было ИЛИ их было недостаточно (< max_messages),
    # ИЛИ если текст был одной строкой, пробуем агрессивную разбивку
    # (Проверяем !processed_by_newline или что частей мало, но текст длинный)
    needs_aggressive_split = False
    if not processed_by_newline:
        needs_aggressive_split = True
        logger.info("No newline splits found. Proceeding to aggressive split.")
    elif len(parts) < max_messages and len(response) > 200 * len(parts): # Если частей мало, но текст длинный
        needs_aggressive_split = True
        logger.info(f"Only {len(parts)} newline parts found, but response is long ({len(response)} chars). Proceeding to aggressive split of the largest part.")
        # В этом случае будем делить самую длинную часть
        parts.sort(key=len, reverse=True) # Сортируем по длине
        text_to_split_aggressively = parts.pop(0) # Берем самую длинную
        remaining_parts = parts # Остальные части оставляем
        parts = [] # Сбрасываем основной список, будем наполнять заново
        max_aggressive_parts = max_messages - len(remaining_parts) # Сколько частей нужно получить агрессивно
    else:
        # Если переносы были и их достаточно, агрессивная разбивка не нужна
        text_to_split_aggressively = ""
        remaining_parts = []
        max_aggressive_parts = 0

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
    if len(parts) > max_messages:
        logger.info(f"Merging {len(parts)} parts down to {max_messages}.")
        # Простая логика: объединяем по N частей в одно сообщение
        parts_per_message_exact = len(parts) / max_messages
        parts_taken = 0
        for i in range(max_messages):
            start_index = round(parts_taken)
            # Определяем, сколько частей взять для этого сообщения
            end_index_exact = round(parts_taken + parts_per_message_exact)
            # Убедимся, что end_index не выходит за пределы
            end_index = min(end_index_exact, len(parts))

            # Если start_index догнал или перегнал end_index, выходим
            if start_index >= end_index:
                break

            # Используем '\n\n' как разделитель при объединении
            merged_part = "\n\n".join(parts[start_index:end_index])

            # Проверяем длину объединенной части
            if len(merged_part) <= telegram_max_len:
                 final_messages.append(merged_part)
            else:
                 # Если даже после объединения слишком длинно, берем только первую часть из группы
                 # и добавляем многоточие, если это последняя доступная часть
                 first_part_of_group = parts[start_index]
                 if len(first_part_of_group) > telegram_max_len:
                     first_part_of_group = first_part_of_group[:telegram_max_len - 3] + "..."
                 final_messages.append(first_part_of_group)
                 logger.warning(f"Merged part was too long, using only first sub-part: {first_part_of_group[:50]}...")
                 # Прерываем объединение, если достигли лимита сообщений
                 if len(final_messages) >= max_messages:
                     break

            parts_taken += (end_index - start_index) # Обновляем количество взятых частей

            # Прерываем, если достигли лимита сообщений
            if len(final_messages) >= max_messages:
                 break

        # Добавляем многоточие к последнему сообщению, если были обрезаны части
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

    logger.info(f"Final processed messages count: {len(processed_messages)}")
    return processed_messages
