import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging
import math

logger = logging.getLogger(__name__)

# Константы вынесены для легкой настройки
TELEGRAM_MAX_LEN = 4096
# Мягкий лимит для куска перед применением агрессивного fallback
SOFT_CHUNK_LIMIT_FACTOR = 0.85
MIN_SENSIBLE_LEN = 50 # Минимальная длина части при агрессивном разделении
# Список вводных слов/фраз (в нижнем регистре), указывающих на возможную смену мысли/подтемы
# Добавляем распространенные слова, которые могут начинать новое смысловое направление
TRANSITION_WORDS = [
    "однако", "тем не менее", "зато", "впрочем",
    "кроме того", "более того", "к тому же", "также",
    "во-первых", "во-вторых", "в-третьих", "наконец",
    "итак", "таким образом", "следовательно", "в заключение", "подводя итог",
    "кстати", "между прочим", "к слову",
    "например", "к примеру", "в частности",
    "с другой стороны", "напротив",
    "если говорить о", "что касается",
    "прежде всего", "главное",
    "поэтому", "потому", # Хотя "потому" часто внутри, но иногда начинает ответ
    "далее", "затем",
    "но ", # "но" с пробелом, чтобы не ловить "ноутбук"
    "а ", # "а" с пробелом
    "и ", # "и" с пробелом (менее надежно, но может быть)
    "ведь ", # "ведь" с пробелом
    "еще ", # "еще" с пробелом
]
# Компилируем регэксп один раз
# (?i) - ignore case, \b - word boundary, | - OR
# re.escape для безопасного использования слов в регэкспе
TRANSITION_PATTERN = re.compile(
    r"((?:^|\n|\.\s+|!\s+|\?\s+|…\s+)\s*)(" + # Начало строки/предложения + пробелы
    r"|".join(r"\b" + re.escape(word) for word in TRANSITION_WORDS) +
    r")\b",
    re.IGNORECASE | re.MULTILINE
)


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
    timezones_offsets = {
        "МСК": timedelta(hours=3),       # Moscow Time (fixed UTC+3)
        "Берлин": timedelta(hours=1),   # Central European Time (UTC+1, without DST)
        "Нью-Йорк": timedelta(hours=-5) # Eastern Standard Time (UTC-5, without DST)
    }
    for name, offset in timezones_offsets.items():
        try:
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
             # Only add strings, ignore potential tuples from older patterns if any
             gif_links.update(item for item in found if isinstance(item, str))
         except Exception as e:
              logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")
    # Basic validation and return unique links (preserving order found somewhat)
    valid_links = [link for link in gif_links if link.startswith(('http://', 'https://')) and ' ' not in link]
    # Use dict.fromkeys to get unique links while preserving order
    return list(dict.fromkeys(valid_links))

# --- "Гипер-Гениальный" Сплиттер v5 ---

def _split_aggressively_v5(text: str, max_len: int) -> List[str]:
    """Fallback: Агрессивное разделение по пробелам."""
    logger.debug(f"-> Applying AGGRESSIVE fallback splitting to text (len={len(text)}).")
    parts = []
    remaining_text = text.strip()
    while remaining_text:
        if len(remaining_text) <= max_len:
            parts.append(remaining_text); break
        cut_pos = max_len
        space_pos = remaining_text.rfind(' ', 0, cut_pos)
        if space_pos > MIN_SENSIBLE_LEN: cut_pos = space_pos + 1
        else:
            forward_space_pos = remaining_text.find(' ', MIN_SENSIBLE_LEN)
            if forward_space_pos != -1 and forward_space_pos < max_len: cut_pos = forward_space_pos + 1
            else: cut_pos = max_len # Обрезаем, если нет подходящих пробелов
        part_to_add = remaining_text[:cut_pos].strip()
        if part_to_add: parts.append(part_to_add)
        remaining_text = remaining_text[cut_pos:].strip()
    return [p for p in parts if p]

def _find_potential_split_indices(text: str) -> List[Tuple[int, int]]:
    """Находит индексы и приоритеты всех потенциальных точек разделения."""
    indices = []
    # P1: \n\n (Приоритет 3 - самый высокий)
    for match in re.finditer(r"\n\s*\n", text):
        indices.append((match.start(), 3)) # Используем начало \n\n как точку разрыва

    # P2: Маркеры списка (Приоритет 2)
    # Ищем начало маркера списка
    for match in re.finditer(r"(?:^|\n)\s*(?:[-*•]|\d+\.|\w+\))\s+", text, re.MULTILINE):
         # Добавляем индекс начала маркера списка, если он не в самом начале текста
        if match.start() > 0:
            indices.append((match.start(), 2))

    # P3: Вводные слова/фразы (Приоритет 1)
    for match in TRANSITION_PATTERN.finditer(text):
        # Добавляем индекс начала вводного слова, если оно не в самом начале текста
        # match.start(2) - начало именно слова, без предшествующих пробелов/пунктуации
        if match.start(2) > 0:
            indices.append((match.start(2), 1))

    # P4: Конец предложения (Приоритет 0 - самый низкий из семантических)
    for match in re.finditer(r"[.!?…]\s+", text):
        # Добавляем индекс *после* знака препинания и пробела
        indices.append((match.end(), 0))

    # Сортируем по индексу и убираем дубликаты (оставляя высший приоритет)
    unique_indices = {}
    for index, priority in sorted(indices):
        if index not in unique_indices or priority > unique_indices[index]:
            unique_indices[index] = priority

    return sorted(unique_indices.items()) # Возвращаем отсортированный список пар (индекс, приоритет)

def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits the bot's response aggressively based on calculated target length
    to achieve 'max_messages' parts (v9 - Always Split if max_messages > 1).
    Respects max_messages and Telegram length limits.
    """
    if not response or not isinstance(response, str): return []
    response = response.strip()
    if not response: return []

    # --- Handle max_messages setting ---
    original_max_setting = max_messages
    if max_messages <= 0: max_messages = random.randint(1, 3); logger.debug(f"Max messages set random to {max_messages}")
    elif max_messages > 10: logger.warning(f"Max messages ({original_max_setting}) > 10. Setting to 10."); max_messages = 10
    else: logger.debug(f"Using max_messages: {max_messages} (Original: {original_max_setting})")
    # --- End max_messages handling ---

    total_len = len(response)

    # --- ИЗМЕНЕННАЯ ЛОГИКА ---
    # Если нужно ТОЛЬКО 1 сообщение (явно задано или текст слишком короткий для деления)
    if max_messages == 1 or total_len < MIN_SENSIBLE_LEN * 1.5: # Не делим, если текст меньше ~1.5 минимальных частей
        logger.debug(f"Returning single message (max_messages=1 or text too short: {total_len} chars)")
        # Обрезаем, если даже одно сообщение слишком длинное
        return [response[:TELEGRAM_MAX_LEN-3]+"..." if total_len > TELEGRAM_MAX_LEN else response]
    # --- КОНЕЦ ИЗМЕНЕННОЙ ЛОГИКИ ---

    logger.info(f"--- Postprocessing response (Simple Aggressive Split v9 - Always Split) --- Target messages: {max_messages}")

    final_messages: List[str] = []
    remaining_text = response
    remaining_len = total_len

    for i in range(max_messages):
        if not remaining_text: break # Выходим, если текст закончился

        # Если это последняя часть, забираем всё
        if i == max_messages - 1:
            logger.debug(f"Part {i+1}/{max_messages}: Taking all remaining text ({len(remaining_text)} chars).")
            part_to_add = remaining_text
            remaining_text = ""
        else:
            # Рассчитываем идеальную длину для этой части
            parts_left_to_create = max_messages - i
            ideal_len_for_part = math.ceil(remaining_len / parts_left_to_create)
            # Ограничиваем максимальную длину, чтобы не превысить лимит Telegram
            target_cut_point = min(TELEGRAM_MAX_LEN, ideal_len_for_part + 50) # Буфер для поиска пробела
            target_cut_point = min(target_cut_point, len(remaining_text)) # Не больше, чем осталось

            logger.debug(f"Part {i+1}/{max_messages}: Rem_len={remaining_len}, Parts_left={parts_left_to_create}, Ideal_len={ideal_len_for_part}, Target_cut={target_cut_point}")

            cut_pos = target_cut_point
            # Ищем пробел НАЗАД
            if target_cut_point < len(remaining_text):
                 space_pos = remaining_text.rfind(' ', MIN_SENSIBLE_LEN, target_cut_point)
                 if space_pos != -1:
                     cut_pos = space_pos + 1
                     logger.debug(f"Found suitable space break backward at {cut_pos}")
                 else: # Ищем ВПЕРЕД
                     forward_space_pos = remaining_text.find(' ', ideal_len_for_part)
                     if forward_space_pos != -1 and forward_space_pos < target_cut_point + 100:
                          cut_pos = forward_space_pos + 1
                          logger.debug(f"Found suitable space break forward at {cut_pos}")
                     else: # Режем по target_cut_point
                          cut_pos = target_cut_point
                          logger.debug(f"No suitable space break found, cutting at calculated position {cut_pos}")
            else:
                logger.debug("Cut position is at the end of remaining text.")

            part_to_add = remaining_text[:cut_pos].strip()
            remaining_text = remaining_text[cut_pos:].strip()

        # Добавляем непустую часть, проверяя лимит
        if part_to_add:
            if len(part_to_add) > TELEGRAM_MAX_LEN:
                 logger.warning(f"Aggressively split part still exceeds limit ({len(part_to_add)}). Truncating.")
                 final_messages.append(part_to_add[:TELEGRAM_MAX_LEN-3] + "...")
            else:
                 final_messages.append(part_to_add)
        else:
             logger.warning("Skipping empty part created during aggressive split.")

        remaining_len = len(remaining_text) # Обновляем

    # --- Финальная проверка и лог ---
    logger.info(f"Final processed messages (Simple Aggressive v9) count: {len(final_messages)}")
    for idx, msg_part in enumerate(final_messages):
        logger.debug(f"  Part {idx+1}/{len(final_messages)} (len={len(msg_part)}): '{msg_part[:80]}...'")

    return final_messages
