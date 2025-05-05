import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging
import math

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
MIN_PART_LEN = 100 # Минимальная длина части, чтобы не было совсем коротких

def escape_markdown_v2(text: Optional[str]) -> str:
    if text is None: return ""
    if not isinstance(text, str):
        try: text = str(text)
        except Exception: logger.warning(f"Could not convert non-string value to string for Markdown escaping: {type(text)}"); return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'; return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_time_info() -> str:
    now_utc = datetime.now(timezone.utc); time_parts = [f"UTC {now_utc.strftime('%H:%M %d.%m.%Y')}"]
    timezones_offsets = {"МСК": timedelta(hours=3), "Берлин": timedelta(hours=1), "Нью-Йорк": timedelta(hours=-5)}
    for name, offset in timezones_offsets.items():
        try: tz = timezone(offset); local_time = now_utc.astimezone(tz); time_parts.append(f"{name} {local_time.strftime('%H:%M %d.%m')}")
        except Exception as e: logger.warning(f"Could not calculate time for tz '{name}' with offset {offset}: {e}"); time_parts.append(f"{name} N/A")
    return f"сейчас " + ", ".join(time_parts) + "."

def extract_gif_links(text: str) -> List[str]:
    if not isinstance(text, str): return []
    try: decoded_text = urllib.parse.unquote(text)
    except Exception: decoded_text = text
    gif_patterns = [ r'(https?://media[0-9]?.giphy.com/media/[a-zA-Z0-9]+/giphy.gif)', r'(https?://i.giphy.com/[a-zA-Z0-9]+.gif)', r'(https?://c.tenor.com/[a-zA-Z0-9]+/[a-zA-Z0-9]+.gif)', r'(https?://media.tenor.com/[a-zA-Z0-9]+/[a-zA-Z0-9]+/AAA[AC].gif)', r'(https?://(?:i.)?imgur.com/[a-zA-Z0-9]+.gif)', r'(https?://[^\s<>"]+\.gif(?:[?#][^\s<>"]*)?)' ]
    gif_links = set()
    for pattern in gif_patterns:
        try: found = re.findall(pattern, decoded_text, re.IGNORECASE); gif_links.update(item for item in found if isinstance(item, str))
        except Exception as e: logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")
    valid_links = [link for link in gif_links if link.startswith(('http://', 'https://')) and ' ' not in link]
    return list(dict.fromkeys(valid_links))

def postprocess_response(response: str, max_messages: int) -> List[str]:
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    original_max_setting = max_messages
    if max_messages <= 0:
        max_messages = random.randint(1, 3)
        logger.info(f"Max messages set to random (1-3) from setting {original_max_setting}. Chosen: {max_messages}")
    elif max_messages > 10:
        logger.warning(f"Max messages ({original_max_setting}) exceeds limit 10. Setting to 10.")
        max_messages = 10
    else:
        logger.info(f"Using max_messages setting: {max_messages}")

    if max_messages == 1 or len(response) < 150 : 
        logger.info(f"Returning single message (max_messages=1 or text too short: {len(response)} chars)")
        if len(response) > TELEGRAM_MAX_LEN:
            return [response[:TELEGRAM_MAX_LEN - 3] + "..."]
        else:
            return [response]

    logger.info(f"--- Postprocessing response V12 (Aggressive Split) --- Max messages allowed: {max_messages}")
    final_messages = []
    remaining_text = response

    for i in range(max_messages):
        if not remaining_text:
            break

        if i == max_messages - 1:
            logger.debug(f"Taking remaining text for the last part ({len(final_messages) + 1}/{max_messages}).")
            part = remaining_text
            remaining_text = "" 
        else:
            parts_left_to_create = max_messages - i
            ideal_len = math.ceil(len(remaining_text) / parts_left_to_create)
            target_len = max(min_part_len, min(ideal_len, TELEGRAM_MAX_LEN - 10))
            # Определяем точку среза
            cut_pos = min(target_len, len(remaining_text))

            logger.debug(f"Part {i+1}: remaining={len(remaining_text)}, parts_left={parts_left_to_create}, ideal_len={ideal_len}, target_len={target_len}, potential_cut={cut_pos}")


            # Ищем лучший разрыв (пробел) НАЗАД от точки среза
            # Не ищем, если точка среза уже в конце текста
            if cut_pos < len(remaining_text):
                # Ищем последний пробел в диапазоне [~половина длины до точки среза]
                search_start = max(0, cut_pos - target_len // 2)
                space_pos = remaining_text.rfind(' ', search_start, cut_pos)
                # Если нашли пробел и он не в самом начале, используем его
                if space_pos > 10: # Дальше чем 10 символов от начала
                    cut_pos = space_pos + 1 # Режем после пробела
                    logger.debug(f"Found space break at {cut_pos}")
                else:
                    logger.debug(f"No suitable space found before {cut_pos}, cutting at target.")
            else:
                 logger.debug("Cut position is at the end of remaining text.")

            part = remaining_text[:cut_pos]
            remaining_text = remaining_text[cut_pos:]

        # Добавляем непустую часть
        part_cleaned = part.strip()
        if part_cleaned:
            final_messages.append(part_cleaned)
        else:
            logger.warning("Skipping empty part created during aggressive split.")

    # Финальная проверка длины (хотя она не должна превышаться)
    processed_messages = []
    for msg in final_messages:
        if len(msg) > TELEGRAM_MAX_LEN:
             logger.warning(f"Aggressively split part still exceeds limit ({len(msg)}). Truncating.")
             processed_messages.append(msg[:TELEGRAM_MAX_LEN-3] + "...")
        else:
             processed_messages.append(msg)

    logger.info(f"Final processed messages V12 count: {len(processed_messages)}")
    return processed_messages
