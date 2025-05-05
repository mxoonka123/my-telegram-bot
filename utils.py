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
    Splits the bot's response using semantic heuristics (v6 - Forced Fallback).
    Prioritizes \n\n, list markers, transition words, sentence ends.
    If only one message results but more were requested (max_messages > 1),
    applies aggressive splitting as a final step.
    Respects max_messages and Telegram length limits.
    """
    soft_chunk_limit = TELEGRAM_MAX_LEN * SOFT_CHUNK_LIMIT_FACTOR

    if not response or not isinstance(response, str): return []
    response = response.strip()
    if not response: return []

    # --- Handle max_messages setting ---
    original_max_setting = max_messages
    if max_messages <= 0: max_messages = random.randint(1, 3); logger.debug(f"Max messages set random to {max_messages}")
    elif max_messages > 10: logger.warning(f"Max messages ({original_max_setting}) > 10. Setting to 10."); max_messages = 10
    # --- End max_messages handling ---

    # Если ОДНОЗНАЧНО нужно 1 сообщение, возвращаем сразу
    if original_max_setting == 1: # Проверяем именно исходную настройку
        logger.debug("Max_messages was explicitly 1, returning original (truncated).")
        return [response[:TELEGRAM_MAX_LEN-3]+"..." if len(response) > TELEGRAM_MAX_LEN else response]

    # Если текст очень короткий, тоже нет смысла делить
    if len(response) < 100 :
         logger.debug("Text is very short, returning single message.")
         return [response] # Длину уже проверили выше на случай original_max_setting == 1

    logger.info(f"--- Postprocessing response (Heuristic Split v6 - Forced Fallback) --- Max messages: {max_messages}")

    potential_splits = _find_potential_split_indices(response)
    logger.debug(f"Found {len(potential_splits)} potential split points.")
    potential_splits.append((len(response), -1)) # Добавляем конец строки

    generated_messages: List[str] = [] # Переименовал для ясности
    current_message = ""
    last_split_pos = 0

    # --- Основной цикл разделения по семантике (как в v5) ---
    for i, (split_pos, priority) in enumerate(potential_splits):
        chunk = response[last_split_pos:split_pos].strip()
        if not chunk: last_split_pos = split_pos; continue

        potential_joiner = "\n\n" if current_message else ""
        potential_len = len(current_message) + len(potential_joiner) + len(chunk)

        finalize_before_adding = False
        if current_message:
            if potential_len > TELEGRAM_MAX_LEN: finalize_before_adding = True; logger.debug(f"Finalizing msg {len(generated_messages)+1}: Exceeds length.")
            elif priority >= 2 and len(current_message) > MIN_SENSIBLE_LEN: finalize_before_adding = True; logger.debug(f"Finalizing msg {len(generated_messages)+1}: High priority split (prio={priority}).")
            elif len(generated_messages) >= max_messages - 1: finalize_before_adding = True; logger.debug(f"Finalizing msg {len(generated_messages)+1}: Reached message limit ({max_messages}).")

        if finalize_before_adding:
             if len(current_message) > TELEGRAM_MAX_LEN: logger.warning(f"Message to finalize exceeds limit! Truncating."); current_message = current_message[:TELEGRAM_MAX_LEN-3]+"..."
             generated_messages.append(current_message)
             current_message = ""

        # Если сам chunk слишком длинный -> агрессивный сплит
        if len(chunk) > TELEGRAM_MAX_LEN:
            logger.warning(f"Chunk itself (len={len(chunk)}) > {TELEGRAM_MAX_LEN}. Applying aggressive splitting.")
            split_sub_chunks = _split_aggressively_v5(chunk, TELEGRAM_MAX_LEN)
            for sub_chunk in split_sub_chunks:
                 sub_joiner = "\n\n" if current_message else ""
                 if current_message and len(current_message) + len(sub_joiner) + len(sub_chunk) <= TELEGRAM_MAX_LEN and len(generated_messages) < max_messages -1:
                      current_message += sub_joiner + sub_chunk
                 elif len(generated_messages) < max_messages:
                      if current_message: generated_messages.append(current_message)
                      current_message = sub_chunk
                 else: logger.warning("Reached msg limit while adding aggressively split sub-chunks."); break
            last_split_pos = split_pos; continue # Переходим к следующей точке

        # Добавляем нормальный chunk
        current_message += potential_joiner + chunk
        last_split_pos = split_pos

        # Защита от переполнения current_message
        if len(current_message) > TELEGRAM_MAX_LEN:
             logger.error(f"Logic Error: current_message exceeded limit ({len(current_message)}). Applying aggressive split!")
             split_current = _split_aggressively_v5(current_message, TELEGRAM_MAX_LEN)
             if len(split_current) > 1:
                  for part_idx in range(len(split_current) - 1):
                       if len(generated_messages) < max_messages: generated_messages.append(split_current[part_idx])
                       else: logger.warning("Reached msg limit splitting oversized current_message."); break
                  current_message = split_current[-1] if len(generated_messages) < max_messages else ""
             elif split_current: current_message = split_current[0]
             else: logger.error("Aggressive split of oversized current_message failed!"); current_message = ""
             if len(generated_messages) >= max_messages: logger.debug("Reached msg limit after handling oversized current_message."); break

        if len(generated_messages) >= max_messages: logger.debug(f"Reached message limit ({max_messages}). Breaking loop."); break

    # Добавляем остаток
    if current_message and len(generated_messages) < max_messages:
         if len(current_message) > TELEGRAM_MAX_LEN: logger.warning(f"Final message exceeds limit. Aggressively splitting."); final_split_parts = _split_aggressively_v5(current_message, TELEGRAM_MAX_LEN)
         else: final_split_parts = [current_message]
         for part in final_split_parts:
              if len(generated_messages) < max_messages: generated_messages.append(part)
              else: logger.warning("Reached msg limit adding final aggressively split parts."); break

    # --- НОВЫЙ БЛОК: Принудительный Fallback ---
    if len(generated_messages) == 1 and max_messages > 1:
        single_message_text = generated_messages[0]
        logger.warning(f"Heuristic split resulted in 1 message, but max_messages={max_messages}. Applying forced aggressive split.")
        # Применяем агрессивный сплит ко всему тексту, который был в единственном сообщении
        aggressively_split_parts = _split_aggressively_v5(single_message_text, TELEGRAM_MAX_LEN)
        # Теперь generated_messages - это результат агрессивного сплита
        generated_messages = aggressively_split_parts
        logger.info(f"Forced aggressive split produced {len(generated_messages)} parts.")

    # --- Финальная обработка (объединение лишних, если > max_messages) ---
    if len(generated_messages) > max_messages:
        logger.warning(f"Splitting produced {len(generated_messages)} messages (limit {max_messages}). Merging excess.")
        merged_tail = "\n\n".join(generated_messages[max_messages-1:])
        if len(merged_tail) > TELEGRAM_MAX_LEN: final_messages[max_messages-1] = merged_tail[:TELEGRAM_MAX_LEN-3] + "..."
        else: final_messages[max_messages-1] = merged_tail
        final_messages = generated_messages[:max_messages] # Используем новое имя переменной
    else:
         final_messages = generated_messages # Если количество в норме

    logger.info(f"Final processed messages (Heuristic Split v6) count: {len(final_messages)}")
    for idx, msg_part in enumerate(final_messages):
        logger.debug(f"  Part {idx+1}/{len(final_messages)} (len={len(msg_part)}): '{msg_part[:80]}...'")

    final_messages = [msg for msg in final_messages if msg] # Удаляем пустые
    return final_messages
