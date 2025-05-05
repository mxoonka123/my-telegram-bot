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
    V8: Prioritizes LLM's newlines (\n\n or \n). Aggressive split as last resort.
    Optimized for short, complete blocks (1-3 sentences each).
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

    logger.info(f"--- Postprocessing response V8 --- Max messages: {max_messages}")
    parts = []
    processed_by_newline = False

    # 1. Ищем \n\n - приоритет на короткие, законченные блоки
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        # Фильтруем слишком короткие и слишком длинные блоки
        filtered_parts = []
        for part in potential_parts_double:
            if len(part) < 50: continue  # Пропускаем слишком короткие
            if len(part) > telegram_max_len - 100: continue  # Пропускаем слишком длинные
            filtered_parts.append(part)
        
        if filtered_parts:
            logger.info(f"Split by DOUBLE newlines resulted in {len(filtered_parts)} valid parts.")
            parts = filtered_parts
            processed_by_newline = True

    # 2. Ищем \n, если не нашли \n\n
    if not processed_by_newline:
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            # Группируем по предложениям (1-3 предложения в блоке)
            blocks = []
            current_block = []
            for part in potential_parts_single:
                current_block.append(part)
                if len(current_block) >= 3:  # Максимум 3 предложения в блоке
                    blocks.append(" ".join(current_block))
                    current_block = []
            if current_block:  # Добавляем последний блок
                blocks.append(" ".join(current_block))
            
            if blocks:
                logger.info(f"Split by SINGLE newlines resulted in {len(blocks)} blocks.")
                parts = blocks
                processed_by_newline = True
            else:
                logger.info("Could not create valid blocks from single newlines.")
        else:
            logger.info("Did not find any type of newline splits.")

    # 3. Агрессивная разбивка ТОЛЬКО если переносов не было СОВСЕМ
    if not processed_by_newline:
        logger.warning("No newlines found in LLM response. Using aggressive splitting.")
        aggressive_parts = []
        
        # Используем упрощенную агрессивную разбивку с учетом предложений
        estimated_len = math.ceil(len(response) / max_messages)
        estimated_len = max(estimated_len, 50)  # Минимум 50 символов
        estimated_len = min(estimated_len, telegram_max_len - 100)  # Максимум telegram_max_len - 100
        
        start = 0
        while start < len(response) and len(aggressive_parts) < max_messages:
            end = min(start + estimated_len, len(response))
            
            # Ищем последнее предложение в блоке
            last_period = response.rfind('.', start, end)
            last_exclamation = response.rfind('!', start, end)
            last_question = response.rfind('?', start, end)
            
            # Берем последнее из найденных знаков препинания
            last_punctuation = max(last_period, last_exclamation, last_question)
            if last_punctuation > start:
                end = min(last_punctuation + 1, end)
            
            part = response[start:end].strip()
            if part and len(part) >= 50:  # Пропускаем слишком короткие блоки
                aggressive_parts.append(part)
            
            start = end
            if start >= len(response): break
        
        logger.info(f"Aggressive splitting created {len(aggressive_parts)} parts.")
        parts = aggressive_parts

    # 4. Если после всего частей нет
    if not parts:
        logger.warning("Could not split response using any method V8.")
        if len(response) > telegram_max_len:
             return [response[:telegram_max_len - 3] + "..."]
        else:
             return [response] # Возвращаем как есть, если она не пустая

    # 5. ОБРЕЗАЕМ (Trimming), если частей БОЛЬШЕ чем max_messages
    final_messages = []
    if len(parts) > max_messages:
        logger.info(f"Trimming parts from {len(parts)} down to {max_messages}.")
        final_messages = parts[:max_messages]
        # Добавляем многоточие к последней части
        if final_messages and final_messages[-1]:
             last_part = final_messages[-1].rstrip('.!?… ')
             if not last_part.endswith('...'):
                 final_messages[-1] = f"{last_part}..."
    else:
        final_messages = parts

    # 6. Финальная проверка длины и очистка
    processed_messages = []
    for msg in final_messages:
        # Добавляем двойные переносы между блоками
        msg_cleaned = "\n\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned: continue
        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part still exceeds limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    logger.info(f"Final processed messages V8 count: {len(processed_messages)}")
    return processed_messages
