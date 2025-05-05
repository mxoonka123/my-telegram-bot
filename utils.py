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
    Splits the bot's response using semantic heuristics (v5).
    Prioritizes \n\n, list markers, transition words, sentence ends,
    then falls back to length-based splitting.
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

    if max_messages == 1:
        logger.debug("Max_messages is 1, returning original (truncated).")
        # Обрезаем, если даже одно сообщение слишком длинное
        return [response[:TELEGRAM_MAX_LEN-3]+"..." if len(response) > TELEGRAM_MAX_LEN else response]


    logger.info(f"--- Postprocessing response (Heuristic Split v5) --- Max messages: {max_messages}")

    potential_splits = _find_potential_split_indices(response)
    logger.debug(f"Found {len(potential_splits)} potential split points.")
    # Добавим конец строки как финальную точку для удобства итерации
    potential_splits.append((len(response), -1)) # -1 приоритет

    final_messages: List[str] = []
    current_message = ""
    last_split_pos = 0

    for i, (split_pos, priority) in enumerate(potential_splits):
        chunk = response[last_split_pos:split_pos].strip()
        if not chunk: # Пропускаем пустые куски между разделителями
            last_split_pos = split_pos
            continue

        # Логика объединения и разделения
        potential_joiner = "\n\n" if current_message else ""
        potential_len = len(current_message) + len(potential_joiner) + len(chunk)

        # Нужно ли завершить текущее сообщение ПЕРЕД добавлением этого куска?
        finalize_before_adding = False
        if current_message: # Если текущее сообщение не пустое
            if potential_len > TELEGRAM_MAX_LEN:
                logger.debug(f"Finalizing msg {len(final_messages)+1}: Adding chunk (len={len(chunk)}) would exceed limit ({potential_len} > {TELEGRAM_MAX_LEN}). Chunk starts: '{chunk[:30]}...'")
                finalize_before_adding = True
            elif priority >= 2 and len(current_message) > MIN_SENSIBLE_LEN: # Приоритет 2 (список) или 3 (\n\n)
                 logger.debug(f"Finalizing msg {len(final_messages)+1}: High priority split point (prio={priority}) found. Current msg len={len(current_message)}. Chunk starts: '{chunk[:30]}...'")
                 finalize_before_adding = True
            elif len(final_messages) >= max_messages - 1:
                 logger.debug(f"Finalizing msg {len(final_messages)+1}: Reached message limit ({max_messages}). Chunk starts: '{chunk[:30]}...'")
                 finalize_before_adding = True


        if finalize_before_adding:
             # Перед добавлением в final_messages, проверим длину current_message еще раз
             if len(current_message) > TELEGRAM_MAX_LEN:
                  logger.warning(f"Message to finalize (len={len(current_message)}) exceeds limit BEFORE appending! Truncating.")
                  current_message = current_message[:TELEGRAM_MAX_LEN-3]+"..."
             final_messages.append(current_message)
             current_message = "" # Начинаем новое сообщение

        # Теперь добавляем (или начинаем) кусок chunk

        # Если САМ КУСОК слишком длинный, его нужно разбить агрессивно
        if len(chunk) > TELEGRAM_MAX_LEN:
            logger.warning(f"Chunk itself (len={len(chunk)}) > {TELEGRAM_MAX_LEN} even after semantic split. Applying aggressive splitting.")
            split_sub_chunks = _split_aggressively_v5(chunk, TELEGRAM_MAX_LEN)
            logger.debug(f"Aggressive split yielded {len(split_sub_chunks)} sub-chunks.")

            # Добавляем эти под-куски, учитывая лимит сообщений
            for sub_chunk in split_sub_chunks:
                 # Если есть текущее сообщение, и добавление под-куска не превысит лимит, добавляем к нему
                 sub_joiner = "\n\n" if current_message else ""
                 if current_message and len(current_message) + len(sub_joiner) + len(sub_chunk) <= TELEGRAM_MAX_LEN and len(final_messages) < max_messages -1:
                      current_message += sub_joiner + sub_chunk
                 # Иначе - начинаем новое сообщение (если лимит позволяет)
                 elif len(final_messages) < max_messages:
                      if current_message: # Сначала сохраняем предыдущее
                           final_messages.append(current_message)
                      current_message = sub_chunk # Начинаем новое с под-куска
                 # Если лимит сообщений достигнут, игнорируем остаток агрессивно разделенного куска
                 else:
                      logger.warning(f"Reached message limit ({max_messages}) while adding aggressively split sub-chunks. Skipping remaining sub-chunks.")
                      break # Прерываем цикл добавления под-кусков
            # После добавления (или пропуска) всех под-кусков, обновляем last_split_pos
            last_split_pos = split_pos
            continue # Переходим к следующей точке разделения основного текста

        # Если кусок нормальной длины, добавляем его к текущему сообщению
        current_message += potential_joiner + chunk
        last_split_pos = split_pos # Сдвигаем позицию

        # Защита: если после добавления current_message стал слишком длинным
        if len(current_message) > TELEGRAM_MAX_LEN:
             logger.error(f"Logic Error: current_message exceeded limit ({len(current_message)}) after adding chunk. Applying aggressive split to current_message!")
             # Разделяем current_message агрессивно
             split_current = _split_aggressively_v5(current_message, TELEGRAM_MAX_LEN)
             if len(split_current) > 1: # Если разделилось на несколько
                  # Добавляем все части, кроме последней, в final_messages
                  for part_idx in range(len(split_current) - 1):
                       if len(final_messages) < max_messages:
                           final_messages.append(split_current[part_idx])
                       else:
                           logger.warning("Reached msg limit while splitting oversized current_message.")
                           break # Прерываем, если лимит достигнут
                  # Последняя часть становится новым current_message
                  current_message = split_current[-1] if len(final_messages) < max_messages else ""
             elif split_current: # Если разделилась на одну (просто обрезалась)
                  current_message = split_current[0]
             else: # Если агрессивное разделение дало пустой результат
                  logger.error("Aggressive split of oversized current_message failed!")
                  current_message = "" # Просто сбрасываем

             # Если лимит сообщений достигнут после этого маневра
             if len(final_messages) >= max_messages:
                  logger.debug("Reached msg limit after handling oversized current_message.")
                  break # Выходим из основного цикла

        # Если мы заполнили последнее доступное место, завершаем цикл
        if len(final_messages) >= max_messages:
            logger.debug(f"Reached message limit ({max_messages}) during processing. Breaking loop.")
            break

    # Добавляем остаток, если он есть и лимит не превышен
    if current_message and len(final_messages) < max_messages:
         if len(current_message) > TELEGRAM_MAX_LEN: # Проверка последнего сообщения
              logger.warning(f"Final message exceeds limit ({len(current_message)}). Applying aggressive split.")
              final_split_parts = _split_aggressively_v5(current_message, TELEGRAM_MAX_LEN)
              for part in final_split_parts:
                   if len(final_messages) < max_messages:
                       final_messages.append(part)
                   else:
                       logger.warning("Reached msg limit while adding final aggressively split parts.")
                       break
         else:
             final_messages.append(current_message)

    # Если сообщений все равно получилось больше (маловероятно с этой логикой, но возможно)
    if len(final_messages) > max_messages:
        logger.warning(f"Heuristic grouping still produced {len(final_messages)} messages (limit {max_messages}). Merging excess.")
        # Объединяем все, начиная с последнего разрешенного
        merged_tail = "\n\n".join(final_messages[max_messages-1:])
        if len(merged_tail) > TELEGRAM_MAX_LEN:
             final_messages[max_messages-1] = merged_tail[:TELEGRAM_MAX_LEN-3] + "..."
        else:
             final_messages[max_messages-1] = merged_tail
        final_messages = final_messages[:max_messages]

    logger.info(f"Final processed messages (Heuristic Split v5) count: {len(final_messages)}")
    for idx, msg_part in enumerate(final_messages):
        logger.debug(f"  Part {idx+1}/{len(final_messages)} (len={len(msg_part)}): '{msg_part[:80]}...'")

    # Финальная проверка и удаление пустых строк, если вдруг образовались
    final_messages = [msg for msg in final_messages if msg]

    return final_messages
