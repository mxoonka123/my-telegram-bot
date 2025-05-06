import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random

# Константы для Telegram
TELEGRAM_MAX_LEN = 4096  # Максимальная длина сообщения в Telegram
MIN_SENSIBLE_LEN = 100   # Минимальная длина для разумного сообщения
import logging
import math

logger = logging.getLogger(__name__)

# Constants for easier configuration
TELEGRAM_MAX_LEN = 4096
# Minimum sensible length for a chunk when doing aggressive fallback splitting
MIN_SENSIBLE_LEN = 50
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
    parts = []
    remaining_text = text.strip()
    while remaining_text:
        if len(remaining_text) <= max_len:
            parts.append(remaining_text)
            break
        # Find the last space within the limit
        cut_pos = remaining_text.rfind(' ', 0, max_len)
        # If no space found or space is too early, force cut at max_len
        if cut_pos <= MIN_SENSIBLE_LEN:
            cut_pos = max_len
        part_to_add = remaining_text[:cut_pos].strip()
        if part_to_add:
            parts.append(part_to_add)
        remaining_text = remaining_text[cut_pos:].strip()
    return [p for p in parts if p]

# --- V9: Simple Aggressive Split (Always Split) ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits text into separate messages aiming for natural breaks (v12).
    Prioritizes paragraphs, then sentences, then uses fallback.
    Avoids splitting further just to reach max_messages.
    """
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response:
        return []

    logger.debug(f"--- Postprocessing response (Natural Split v12) ---")
    logger.debug(f"Input text length: {len(response)}, max_messages: {max_messages}")

    # Ensure max_messages is reasonable
    if not isinstance(max_messages, int) or max_messages <= 0:
        max_messages = 3 # Default fallback if invalid
        logger.warning(f"Invalid max_messages value, defaulting to {max_messages}")
    # Telegram has a limit of messages per second, avoid too many splits
    # Let's cap it reasonably, e.g., 10, even if user sets more
    hard_max_messages = 10
    if max_messages > hard_max_messages:
        logger.warning(f"User requested max_messages={max_messages}, capping at {hard_max_messages} for stability.")
        max_messages = hard_max_messages

    processed_parts = []
    current_chunk = response

    # 1. Split by Double Newlines (Paragraphs) first
    initial_split = re.split(r'\n\s*\n', current_chunk)
    parts = [p.strip() for p in initial_split if p.strip()]
    logger.debug(f"Split by paragraphs resulted in {len(parts)} parts.")

    # 2. Refine: Split parts longer than TELEGRAM_MAX_LEN by sentence endings
    refined_parts = []
    for part in parts:
        if len(part) <= TELEGRAM_MAX_LEN:
            refined_parts.append(part)
        else:
            logger.debug(f"Part exceeds max length ({len(part)} > {TELEGRAM_MAX_LEN}), splitting by sentence.")
            # Split by sentence-ending punctuation followed by space/newline
            # Keep the punctuation with the sentence.
            sentences = re.split(r'(?<=[.!?…])\s+', part)
            refined_parts.extend([s.strip() for s in sentences if s.strip()])

    parts = refined_parts
    logger.debug(f"After sentence splitting long parts, total parts: {len(parts)}")

    # 3. Combine parts if needed, but prioritize keeping natural breaks
    final_parts = []
    current_combined = ""
    for i, part in enumerate(parts):
        # Check length *before* adding potential separator
        if not current_combined:
            # If the first part itself is too long, split it aggressively
            if len(part) > TELEGRAM_MAX_LEN:
                logger.warning(f"Single part after natural splits still too long ({len(part)} chars). Applying aggressive split.")
                final_parts.extend(_split_aggressively(part, TELEGRAM_MAX_LEN))
                current_combined = "" # Reset combination
            else:
                current_combined = part
        # Check if adding the *next* part (plus separator) exceeds limit
        elif len(current_combined) + len(part) + 2 <= TELEGRAM_MAX_LEN:
            # Combine with double newline
            current_combined += "\n\n" + part
        else:
            # Current combined part is full or adding next part makes it too long
            # Add the completed combined part to final list
            final_parts.append(current_combined)
            # Start the new combined part with the current part
            # Aggressively split the new part if it's too long on its own
            if len(part) > TELEGRAM_MAX_LEN:
                 logger.warning(f"Part starting new combination is too long ({len(part)} chars). Applying aggressive split.")
                 final_parts.extend(_split_aggressively(part, TELEGRAM_MAX_LEN))
                 current_combined = "" # Reset combination
            else:
                current_combined = part

    # Add the last combined part if it exists
    if current_combined:
        final_parts.append(current_combined)

    # 4. Enforce max_messages limit AFTER natural splitting/combining
    if len(final_parts) > max_messages:
        logger.warning(f"Natural splitting resulted in {len(final_parts)} parts, exceeding limit of {max_messages}. Combining further.")
        # Combine the earliest parts first until the limit is met
        while len(final_parts) > max_messages:
            # Combine part 0 and 1
            combined = final_parts[0] + "\n\n" + final_parts[1]
            # Aggressively split if the combination is too long
            if len(combined) > TELEGRAM_MAX_LEN:
                 logger.warning(f"Forced combination exceeds length ({len(combined)} chars). Splitting aggressively.")
                 split_combined = _split_aggressively(combined, TELEGRAM_MAX_LEN)
                 # Replace the first two parts with the aggressively split parts
                 final_parts = split_combined + final_parts[2:]
                 # Re-check length immediately in next iteration
            else:
                # Replace first two parts with the combined one
                final_parts[0] = combined
                del final_parts[1]

        logger.debug(f"After enforcing max_messages, final parts count: {len(final_parts)}")

    # 5. Final length check (aggressive split as last resort)
    result_parts = []
    for part in final_parts:
        if len(part) > TELEGRAM_MAX_LEN:
            logger.error(f"CRITICAL: Part still exceeds max length ({len(part)}) after all processing! Applying final aggressive split.")
            result_parts.extend(_split_aggressively(part, TELEGRAM_MAX_LEN))
        elif part: # Add non-empty parts
            result_parts.append(part)

    # Ensure we don't exceed max_messages due to aggressive splitting in the last step
    if len(result_parts) > max_messages:
         logger.warning(f"Aggressive splitting during final check increased parts beyond limit ({len(result_parts)} > {max_messages}). Truncating.")
         result_parts = result_parts[:max_messages]

    # Log final results
    logger.info(f"Final processed messages count: {len(result_parts)}")
    for i, part in enumerate(result_parts):
        logger.debug(f"Finalized message {i+1}/{len(result_parts)} (len={len(part)}): {part[:80]}...")

    return result_parts
    return parts
