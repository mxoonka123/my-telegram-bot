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


def postprocess_response(response: str, max_messages: int) -> List[str]:
    """Splits the bot's response into suitable message parts."""
    if not response or not isinstance(response, str) or max_messages <= 0:
        return [response] if isinstance(response, str) and response.strip() else []

    response = response.strip()
    if not response: return []

    logger.debug(f"Postprocessing response. Max messages allowed: {max_messages}")

    parts = []
    processed = False

    # 1. Приоритет: Разделение по \n\n (как просили LLM)
    potential_parts = response.split('\n\n')
    potential_parts = [p.strip() for p in potential_parts if p.strip()] # Убираем пустые части
    if len(potential_parts) > 1:
        logger.info(f"Splitting response by '\\n\\n'. Found {len(potential_parts)} potential parts.")
        # Если частей больше чем max_messages, пытаемся их объединить
        if len(potential_parts) > max_messages:
            logger.info(f"Too many parts ({len(potential_parts)} > {max_messages}). Trying to merge smaller parts.")
            merged_parts = []
            current_part = ""
            telegram_max_len = 4096 # Telegram limit
            for i, part in enumerate(potential_parts):
                # Если текущая часть пустая, начинаем с новой
                if not current_part:
                    current_part = part
                # Если добавление следующей части не превышает лимит TG И
                # количество уже собранных частей < max_messages - 1 (чтобы последняя часть могла быть длинной)
                elif len(current_part) + len(part) + 2 <= telegram_max_len and len(merged_parts) < max_messages -1 :
                     current_part += "\n\n" + part
                # Иначе, сохраняем текущую часть и начинаем новую
                else:
                    merged_parts.append(current_part)
                    current_part = part

                # Если это последняя часть, добавляем то что есть
                if i == len(potential_parts) - 1:
                    merged_parts.append(current_part)

            parts = merged_parts
            logger.info(f"Merged parts by '\\n\\n': {len(parts)}")
        else:
            parts = potential_parts # Используем как есть, если частей <= max_messages
        processed = True

    # 2. Если не получилось по \n\n, пробуем по \n (менее приоритетно)
    if not processed:
        potential_parts = response.split('\n')
        potential_parts = [p.strip() for p in potential_parts if p.strip()]
        if len(potential_parts) > 1:
            logger.info(f"Splitting response by '\\n'. Found {len(potential_parts)} potential parts.")
            # Логика объединения аналогична \n\n
            if len(potential_parts) > max_messages:
                logger.info(f"Too many parts ({len(potential_parts)} > {max_messages}). Trying to merge smaller parts.")
                merged_parts = []
                current_part = ""
                telegram_max_len = 4096
                for i, part in enumerate(potential_parts):
                    if not current_part:
                        current_part = part
                    elif len(current_part) + len(part) + 1 <= telegram_max_len and len(merged_parts) < max_messages - 1:
                         current_part += "\n" + part # Используем \n для объединения
                    else:
                        merged_parts.append(current_part)
                        current_part = part
                    if i == len(potential_parts) - 1:
                        merged_parts.append(current_part)
                parts = merged_parts
                logger.info(f"Merged parts by '\\n': {len(parts)}")
            else:
                parts = potential_parts
            processed = True

    # 3. Если и по \n не разбили, пробуем по предложениям (как раньше)
    if not processed:
         logger.info("No newline splits found, splitting by sentences.")
         sentences = re.split(r'(?<=[.!?…])\s+', response)
         potential_parts = [s.strip() for s in sentences if s.strip()]
         if len(potential_parts) > 1:
             # Если предложений много, объединяем их до max_messages
             merged_parts = []
             current_part = ""
             telegram_max_len = 4096
             for i, sentence in enumerate(potential_parts):
                  if not current_part:
                      current_part = sentence
                  # Если добавление следующего предложения не превысит лимит И не достигли макс. сообщений
                  elif len(current_part) + len(sentence) + 1 <= telegram_max_len and len(merged_parts) < max_messages - 1 :
                      current_part += " " + sentence
                  else:
                      merged_parts.append(current_part)
                      current_part = sentence

                  if i == len(potential_parts) - 1:
                       merged_parts.append(current_part)

             parts = merged_parts
             logger.info(f"Merged parts by sentences: {len(parts)}")
             processed = True
         else:
             parts = potential_parts # Если всего одно предложение, оставляем как есть

    # 4. Если вообще не удалось разбить
    if not processed or not parts:
        logger.info("Could not split response, returning as single part.")
        return [response]

    # 5. Ограничение количества сообщений (если после объединения их все еще > max_messages)
    if len(parts) > max_messages:
        logger.warning(f"Trimming final parts from {len(parts)} to {max_messages}.")
        final_messages = parts[:max_messages]
        # Добавляем многоточие к последней части, если она не пустая
        if final_messages and final_messages[-1]:
             last_part = final_messages[-1].rstrip('.!?… ')
             final_messages[-1] = f"{last_part}..."
    else:
        final_messages = parts

    # 6. Дополнительная проверка длины каждой части (на всякий случай)
    telegram_max_len = 4096
    processed_messages = []
    for msg in final_messages:
        if len(msg) > telegram_max_len:
            logger.warning(f"Message part still exceeds Telegram limit ({len(msg)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg)

    logger.info(f"Final processed messages count: {len(processed_messages)}")
    return processed_messages
