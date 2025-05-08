import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random

# Константы для Telegram
TELEGRAM_MAX_LEN = 4096  # Максимальная длина сообщения в Telegram
MIN_SENSIBLE_LEN = 50   # Минимальная длина для разумного сообщения
import logging
import math

logger = logging.getLogger(__name__)

# Constants for easier configuration
# TELEGRAM_MAX_LEN = 4096
# MIN_SENSIBLE_LEN = 50
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
def postprocess_response(response: str, max_messages: int, message_volume: str = 'normal') -> List[str]:
    """
    Splits text aiming for approx. max_messages (v13).
    Prioritizes natural breaks, then splits longer parts further.
    Adjusts split based on message_volume: 'short', 'normal', 'long', or 'random'.
    """
    if not response:
        return []

    # Adjust max_messages based on volume if random is selected
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
    if len(response) <= max_len:
        logger.info(f"Response fits in one message (len={len(response)}).")
        return [response]

    # 1. Initial split on natural boundaries (paragraphs, transitions)
    parts = []
    current_part = ""
    for line in response.splitlines():
        line = line.strip()
        if not line:  # Empty line = paragraph break
            if current_part:
                parts.append(current_part.strip())
                current_part = ""
            continue
        current_part += line + " "
    if current_part:
        parts.append(current_part.strip())

    # If still one big part, try splitting by transition words or sentence ends
    if len(parts) == 1 and len(parts[0]) > max_len:
        text = parts[0]
        parts = []
        current_part = ""
        last_break = 0
        matches = list(re.finditer(TRANSITION_PATTERN, text))
        if not matches and ". " in text:
            matches = list(re.finditer(r"\.\s+", text))
        if not matches and "! " in text:
            matches = list(re.finditer(r"!\s+", text))
        if not matches and "? " in text:
            matches = list(re.finditer(r"\?\s+", text))
        if not matches and "… " in text:
            matches = list(re.finditer(r"…\s+", text))
        for m in matches:
            pos = m.start()
            if pos - last_break >= MIN_SENSIBLE_LEN and len(current_part) <= max_len:
                if current_part:
                    parts.append(current_part.strip())
                current_part = text[last_break:pos].strip()
                last_break = pos
        if current_part or last_break < len(text):
            current_part += " " + text[last_break:]
            if len(current_part) > max_len:
                subparts = _split_aggressively(current_part, max_len)
                parts.extend(subparts)
            elif current_part.strip():
                parts.append(current_part.strip())

    # 2. Split long parts further if under max_messages
    while len(parts) < max_messages:
        longest_part_idx = -1
        max_part_len = 0
        for i, part in enumerate(parts):
            if len(part) > max_len and len(part) > max_part_len:
                longest_part_idx = i
                max_part_len = len(part)
        if longest_part_idx == -1:
            break  # No more long parts to split
        long_part = parts[longest_part_idx]
        split_point = len(long_part) // 2
        space_pos = long_part.rfind(" ", 0, split_point + 50)
        if space_pos == -1 or space_pos < MIN_SENSIBLE_LEN or len(long_part) - space_pos < MIN_SENSIBLE_LEN:
            space_pos = long_part.find(" ", split_point - 50)
        if space_pos != -1 and space_pos >= MIN_SENSIBLE_LEN and len(long_part) - space_pos >= MIN_SENSIBLE_LEN:
            part1 = long_part[:space_pos].strip()
            part2 = long_part[space_pos:].strip()
            parts[longest_part_idx] = part1
            parts.insert(longest_part_idx + 1, part2)
        else:
            logger.warning(f"Could not find a suitable split point for part idx {longest_part_idx}.")
            # Mark this part as unsplittable for this iteration by making it short temp.
            parts[longest_part_idx] = " " * (MIN_SENSIBLE_LEN -1) 
    else:
        logger.warning(f"Splitting resulted in {len(parts)} parts, exceeding limit {max_messages}. Combining first parts.")
        while len(parts) > max_messages:
            # Combine part 0 and 1
            combined = parts[0] + "\n\n" + parts[1] # Use double newline when combining
            if len(combined) <= max_len:
                parts[0] = combined
                del parts[1]
            else:
                # If combining makes it too long, we can't combine. Just stop.
                logger.error(f"Cannot combine parts 0 and 1 as they exceed max length. Stopping combination.")
                break # Stop combining if it creates oversized messages

    # 4. Final length check and aggressive split if necessary
    final_parts = []
    for part in parts:
        if len(part) > max_len:
            logger.error(f"Part still exceeds max length ({len(part)}) after all processing! Applying final aggressive split.")
            final_parts.extend(_split_aggressively(part, max_len))
        elif part.strip(): # Add non-empty parts
            final_parts.append(part.strip()) # Ensure parts are stripped

    # 5. Final check against max_messages (aggressive split might increase count)
    if len(final_parts) > max_messages:
        logger.warning(f"Aggressive splitting during final check increased parts ({len(final_parts)}) beyond limit ({max_messages}). Truncating.")
        final_parts = final_parts[:max_messages]
    elif len(final_parts) < max_messages:
        logger.info(f"Final parts count ({len(final_parts)}) is less than requested ({max_messages}). Attempting additional splits.")
        max_attempts = 20  # Prevent potential infinite loops
        attempts = 0
        while len(final_parts) < max_messages and attempts < max_attempts:
            # Pick the longest current part
            idx_longest = max(range(len(final_parts)), key=lambda i: len(final_parts[i]))
            longest_part = final_parts[idx_longest]

            # Stop if the part is too short to split sensibly
            if len(longest_part) <= MIN_SENSIBLE_LEN * 2:
                logger.debug("Longest remaining part too short to split further. Stopping split loop.")
                break

            # Try to split roughly in half using aggressive splitter
            candidate_splits = _split_aggressively(longest_part, max(len(longest_part) // 2, MIN_SENSIBLE_LEN))
            if len(candidate_splits) < 2:
                logger.debug("Aggressive split produced fewer than 2 parts. Stopping split loop.")
                break

            new_part1 = candidate_splits[0].strip()
            new_part2 = " ".join(candidate_splits[1:]).strip()

            if not new_part1 or not new_part2:
                logger.debug("Split produced empty segment(s). Stopping split loop.")
                break

            # Replace the longest part with its two new segments
            final_parts = final_parts[:idx_longest] + [new_part1, new_part2] + final_parts[idx_longest + 1:]
            attempts += 1

        if len(final_parts) < max_messages:
            logger.info(f"Unable to reach requested max_messages after extra splits. Final count: {len(final_parts)}.")

    # Log final results
    logger.info(f"Final processed messages count: {len(final_parts)} (Target: {max_messages})")
    for i, part in enumerate(final_parts):
        logger.debug(f"Finalized message {i+1}/{len(final_parts)} (len={len(part)}): {part[:80]}...")

    return final_parts
