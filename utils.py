import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random

# Константы для Telegram
TELEGRAM_MAX_LEN = 4096  # Максимальная длина сообщения в Telegram
MIN_SENSIBLE_LEN = 50   # Минимальная длина для разумного сообщения
import logging
import config
import math
import tiktoken # Added for OpenRouter token counting

logger = logging.getLogger(__name__)

def count_openai_compatible_tokens(text_content: str, model_identifier: str = config.OPENROUTER_MODEL_NAME) -> int:
    """
    Counts the number of tokens in the text_content using tiktoken,
    compatible with OpenAI models and OpenRouter's Gemini via OpenAI-compatible API.

    Args:
        text_content: The text to count tokens for.
        model_identifier: The model identifier (e.g., "google/gemini-2.0-flash-001" or "gpt-4").
                          This helps select the correct tiktoken encoding.

    Returns:
        The number of tokens.
    """
    if not text_content:
        return 0

    try:
        try:
            encoding = tiktoken.encoding_for_model(model_identifier)
        except KeyError:
            # Меняем уровень на INFO, так как это ожидаемое поведение для некоторых моделей
            logger.info(
                f"Model '{model_identifier}' not found by tiktoken's predefined list. "
                f"This is expected for some OpenRouter models. Using 'cl100k_base' as a reliable fallback."
            )
            encoding = tiktoken.get_encoding("cl100k_base")

        num_tokens = len(encoding.encode(text_content))
        return num_tokens
    except Exception as e:
        logger.error(f"Error counting tokens with tiktoken for model {model_identifier}: {e}", exc_info=True)
        # Fallback: очень грубая оценка, если tiktoken не сработает
        return len(text_content) // 3



# Constants are defined above
# Transition words (lowercase) indicating potential topic shifts
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
    "потому что", "потому",
    "далее", "затем",
    "но ", "а ", "и ", "ведь ", "еще ",
]
# Compile regex once for efficiency
TRANSITION_PATTERN = re.compile(
    r"((?:^|\n|\.\s+|!\s+|\?\s+|…\s+)\s*)(" +
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

async def send_safe_message(reply_target, text: str, reply_markup=None, disable_web_page_preview: bool = None):
    """Безопасная отправка сообщения.
    Сначала пытается отправить MarkdownV2 (с экранированием), при ошибке — обычным текстом.

    Args:
        reply_target: объект с методом reply_text (Update.message, CallbackQuery.message и т.п.)
        text: исходный текст (сырой)
        reply_markup: опциональный markup
        disable_web_page_preview: опционально, отключение предпросмотра ссылок
    """
    if not hasattr(reply_target, 'reply_text'):
        logger.error("send_safe_message: reply_target has no reply_text()")
        return
    try:
        escaped = escape_markdown_v2(text)
        await reply_target.reply_text(
            escaped,
            parse_mode='MarkdownV2',
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview
        )
    except Exception as e:
        logger.warning(f"send_safe_message: MarkdownV2 failed, fallback to plain. Err: {e}")
        try:
            await reply_target.reply_text(
                text,
                parse_mode=None,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview
            )
        except Exception as fe:
            logger.error(f"send_safe_message: plain text failed too: {fe}")

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

# --- Aggressive Splitting Fallback ---
def _split_aggressively(text: str, max_len: int) -> List[str]:
    """Fallback: Aggressively splits text by words if necessary."""
    logger.debug(f"-> Applying AGGRESSIVE fallback splitting to text (len={len(text)}).")
    # Новый алгоритм: разбиваем по словам без разрыва слова
    words = text.strip().split()
    parts: List[str] = []
    current: List[str] = []

    for w in words:
        # Если текущее слово само длиннее max_len, вынужденно делим его по символам
        if len(w) > max_len:
            if current:
                parts.append(' '.join(current))
                current = []
            # Делим длинное слово кусками
            for i in range(0, len(w), max_len):
                parts.append(w[i : i + max_len])
            continue

        tentative_len = len(' '.join(current + [w])) if current else len(w)
        if tentative_len <= max_len:
            current.append(w)
        else:
            # Сбрасываем текущий буфер
            parts.append(' '.join(current))
            current = [w]

    if current:
        parts.append(' '.join(current))

    return parts

# Expose _split_aggressively globally so that other modules can call it without explicit import
import builtins as _builtins_module  # Local alias
_builtins_module._split_aggressively = _split_aggressively

# --- V13: Improved Splitting ---
def postprocess_response(response: str, max_messages: int, message_volume: str = "normal") -> List[str]:
    # Оригинальная обработка ответа
    parts = _split_response_to_messages(response, max_messages, message_volume)
    
    # Дополнительная проверка ограничения сообщений
    if len(parts) > max_messages:
        logger.warning(
            f"Postprocess returned {len(parts)} parts, exceeding requested {max_messages}. Truncating."
        )
        parts = parts[:max_messages]
    
    return parts

def _split_response_to_messages(response: str, max_messages: int, message_volume: str = "normal") -> List[str]:
    """Обработка ответа с учетом ограничений на количество сообщений и их объем. V2 - Refactored."""
    if max_messages <= 0:
        return []

    if message_volume == 'random':
        message_volume = random.choice(['short', 'normal', 'long'])
    
    if message_volume == 'short':
        max_len = TELEGRAM_MAX_LEN // 2
    elif message_volume == 'long':
        max_len = TELEGRAM_MAX_LEN
    else:  # normal
        max_len = TELEGRAM_MAX_LEN * 3 // 4

    logger.info(f"Processing response for max_messages={max_messages}, volume={message_volume}, max_len={max_len}")
    
    response = response.strip()
    if not response:
        return []

    # 1. Первичная обработка: всегда делим по переносам строк, если они есть.
    initial_parts = [line.strip() for line in response.splitlines() if line.strip()]
    if not initial_parts:
        # Если после разделения по строкам ничего не осталось (например, ответ был " \n "),
        # считаем, что весь ответ - одна часть, которую нужно обработать.
        initial_parts = [response]
    
    logger.info(f"Initial split created {len(initial_parts)} part(s).")

    # 2. Обработка каждой части: если часть слишком длинная, делим ее агрессивно.
    final_parts = []
    for part in initial_parts:
        if len(part) > max_len:
            logger.warning(f"Part (len={len(part)}) exceeds max_len ({max_len}). Applying aggressive split.")
            final_parts.extend(_split_aggressively(part, max_len))
        elif part: # Добавляем только непустые части
            final_parts.append(part)

    # 3. Финальная корректировка количества сообщений.
    # Если частей больше, чем разрешено, обрезаем.
    if len(final_parts) > max_messages:
        logger.warning(f"Final parts count ({len(final_parts)}) exceeds max_messages ({max_messages}). Truncating.")
        final_parts = final_parts[:max_messages]
    
    # Если частей меньше, чем нужно (и это не единственный способ деления), можно попробовать разделить еще.
    # Этот блок опционален и может быть добавлен позже, если потребуется "добивать" до max_messages.
    # Пока что простота важнее.

    logger.info(f"Final processed messages count: {len(final_parts)} (Target: {max_messages})")
    for i, part in enumerate(final_parts):
        logger.debug(f"Finalized message {i+1}/{len(final_parts)} (len={len(part)}): {part[:80]}...")

    return final_parts
