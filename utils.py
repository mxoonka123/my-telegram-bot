import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
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

# --- V4: Further Corrected Response Splitting Function ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits the AI response into a specified number of messages,
    respecting sentence boundaries and Telegram's length limit. V4 Logic.

    Args:
        response: The full text response from the AI.
        max_messages: The desired maximum number of messages to split into.
                      If 0 or less, defaults to a random number between 1 and 3.

    Returns:
        A list of strings, where each string is a message part.
    """
    if not response or not isinstance(response, str):
        return []
    response = response.strip()
    if not response:
        return []

    # Determine the target number of messages
    if max_messages <= 0:
        target_messages = random.randint(1, 3)
        logger.debug(f"max_messages was {max_messages}, setting target to random {target_messages}")
    elif max_messages > 10:
         logger.warning(f"max_messages ({max_messages}) > 10, capping at 10.")
         target_messages = 10
    else:
        target_messages = max_messages
    logger.info(f"Splitting response (len={len(response)}) into target_messages={target_messages}")

    # If only one message is needed or the text fits, return it (or aggressively split if too long)
    if target_messages == 1:
        if len(response) <= TELEGRAM_MAX_LEN:
            return [response]
        else:
            logger.warning(f"Response (len={len(response)}) too long for single message target. Aggressively splitting.")
            return _split_aggressively(response, TELEGRAM_MAX_LEN)

    # 1. Split into sentences
    sentences_with_delimiters = re.split(r'(?<=[.!?…])\s*', response)
    sentences = [s.strip() for s in sentences_with_delimiters if s and s.strip()]

    if not sentences:
        logger.warning("Response splitting resulted in no sentences.")
        if len(response) <= TELEGRAM_MAX_LEN: return [response]
        else: return _split_aggressively(response, TELEGRAM_MAX_LEN)

    # 2. Build messages respecting limits
    final_messages: List[str] = []
    current_message_parts: List[str] = []
    current_length = 0
    sentence_index = 0

    while sentence_index < len(sentences):
        # Stop if we have already generated the target number of messages
        if len(final_messages) >= target_messages:
            logger.warning(f"Reached target messages ({target_messages}) but more sentences remain starting with: '{sentences[sentence_index][:50]}...'")
            break

        sentence = sentences[sentence_index]
        sentence_len = len(sentence)
        separator = "\n\n" if current_message_parts else ""
        separator_len = len(separator)

        # --- Handle sentence longer than the limit ---
        if sentence_len > TELEGRAM_MAX_LEN:
            logger.warning(f"Single sentence (len={sentence_len}) starting with '{sentence[:30]}...' exceeds limit. Aggressively splitting.")
            # Finalize the previous message if any
            if current_message_parts:
                final_messages.append(separator.join(current_message_parts))
                current_message_parts = []
                current_length = 0
                # Check again if we reached the limit after adding the previous part
                if len(final_messages) >= target_messages:
                    logger.warning(f"Reached target messages ({target_messages}) after finalizing bucket before splitting long sentence.")
                    break

            # Split the long sentence and add parts
            split_long_sentence = _split_aggressively(sentence, TELEGRAM_MAX_LEN)
            for part in split_long_sentence:
                if len(final_messages) < target_messages:
                    final_messages.append(part)
                else:
                    logger.warning("Reached max messages while adding parts of aggressively split sentence.")
                    break
            sentence_index += 1 # Move to the next sentence index
            # Check if we reached the limit after adding split parts
            if len(final_messages) >= target_messages:
                 logger.warning(f"Reached target messages ({target_messages}) after adding split parts of long sentence.")
                 break
            continue # Continue to the next sentence in the outer loop

        # --- Check if adding the current sentence exceeds the limit for the current message ---
        if current_length + separator_len + sentence_len <= TELEGRAM_MAX_LEN:
            # Add sentence to current message parts
            current_message_parts.append(sentence)
            current_length += separator_len + sentence_len
            sentence_index += 1 # Move to the next sentence
        else:
            # Current message is full, finalize it (if it has content)
            if current_message_parts:
                final_messages.append(separator.join(current_message_parts))
                logger.debug(f"Finalized message {len(final_messages)}/{target_messages} (len={current_length}). Starting new one with '{sentence[:30]}...'")
                # Reset for the new message (which will start with the current sentence)
                current_message_parts = []
                current_length = 0
            else:
                # This case should ideally not happen if sentence_len > TELEGRAM_MAX_LEN is handled above,
                # but as a safeguard, if the first sentence for a new message is already too long, split it.
                logger.error(f"Logical error: Sentence '{sentence[:30]}...' too long but not caught earlier? Aggressively splitting.")
                split_long_sentence = _split_aggressively(sentence, TELEGRAM_MAX_LEN)
                for part in split_long_sentence:
                    if len(final_messages) < target_messages:
                        final_messages.append(part)
                    else: break
                sentence_index += 1
                # Reset current message parts as we added split parts directly
                current_message_parts = []
                current_length = 0

            # Check if we reached the message limit *after* finalizing the previous one
            if len(final_messages) >= target_messages:
                logger.warning(f"Reached target messages ({target_messages}) after finalizing a bucket. Remaining sentences start with: '{sentences[sentence_index][:50]}...'")
                break # Stop processing further sentences

    # Add the last collected message if it exists and limit not reached
    if current_message_parts and len(final_messages) < target_messages:
        final_messages.append("\n\n".join(current_message_parts))
        logger.debug(f"Finalized last message {len(final_messages)}/{target_messages} (len={current_length}).")

    logger.info(f"Splitting finished. Generated {len(final_messages)} messages (target was {target_messages}).")
    # Ensure we don't return more messages than requested, unless necessary due to aggressive splitting
    return final_messages[:target_messages] if len(final_messages) > target_messages else final_messages
