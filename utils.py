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

# --- Improved Response Splitting Function ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits the AI response into a specified number of messages,
    respecting sentence boundaries and Telegram's length limit.

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
    # Use lookbehind `(?<=...)` to keep delimiters, then filter empty strings
    sentences_with_delimiters = re.split(r'(?<=[.!?…])\s*', response)
    sentences = [s.strip() for s in sentences_with_delimiters if s and s.strip()]

    if not sentences:
        logger.warning("Response splitting resulted in no sentences.")
        # Fallback: If splitting fails but there's text, return as one chunk or split aggressively
        if len(response) <= TELEGRAM_MAX_LEN:
            return [response]
        else:
            return _split_aggressively(response, TELEGRAM_MAX_LEN)

    num_sentences = len(sentences)
    final_messages = []
    current_message_parts = []
    current_length = 0
    sentences_processed = 0

    # 2. Distribute sentences into target message buckets
    for i in range(target_messages):
        # Calculate sentences for this bucket (distribute remaining sentences)
        remaining_messages = target_messages - i
        remaining_sentences = num_sentences - sentences_processed
        if remaining_messages <= 0 or remaining_sentences <= 0:
            break # Should not happen if logic is correct, but safety first

        # Calculate how many sentences this message *should* ideally take
        num_sentences_for_this_message = math.ceil(remaining_sentences / remaining_messages)
        logger.debug(f"Bucket {i+1}/{target_messages}: Aiming for {num_sentences_for_this_message} sentences (remaining: {remaining_sentences}/{remaining_messages})")

        current_message_parts = []
        current_length = 0
        sentences_added_to_this_message = 0

        # Add sentences to the current message bucket
        while sentences_added_to_this_message < num_sentences_for_this_message and sentences_processed < num_sentences:
            sentence = sentences[sentences_processed]
            sentence_len = len(sentence)
            separator_len = len("\n\n") if current_message_parts else 0

            # Check if the *single* sentence is too long
            if sentence_len > TELEGRAM_MAX_LEN:
                logger.warning(f"Single sentence (len={sentence_len}) exceeds TELEGRAM_MAX_LEN. Aggressively splitting it.")
                # If there's content in the current bucket, finalize it first
                if current_message_parts:
                    final_messages.append("\n\n".join(current_message_parts))
                    if len(final_messages) >= target_messages: break # Stop if max messages reached
                    current_message_parts = [] # Reset for next potential message
                    current_length = 0

                # Aggressively split the long sentence
                split_long_sentence = _split_aggressively(sentence, TELEGRAM_MAX_LEN)
                for part in split_long_sentence:
                     if len(final_messages) < target_messages:
                         final_messages.append(part)
                     else:
                         logger.warning("Reached max messages while adding parts of aggressively split sentence.")
                         break # Stop adding parts if max messages reached
                sentences_processed += 1 # Mark the original long sentence as processed
                sentences_added_to_this_message += 1 # Count it towards this message's quota
                # Reset current bucket as we added the split parts directly
                current_message_parts = []
                current_length = 0
                if len(final_messages) >= target_messages: break # Stop if max messages reached
                continue # Move to the next sentence for the *next* bucket potentially

            # Check if adding the *next* sentence exceeds the length limit for the *current* bucket
            if current_length + separator_len + sentence_len <= TELEGRAM_MAX_LEN:
                current_message_parts.append(sentence)
                current_length += separator_len + sentence_len
                sentences_processed += 1
                sentences_added_to_this_message += 1
            else:
                # Cannot add this sentence to the current bucket, move to the next bucket
                logger.debug(f"Bucket {i+1}: Sentence '{sentence[:30]}...' (len={sentence_len}) would exceed limit ({current_length}+{separator_len}+{sentence_len} > {TELEGRAM_MAX_LEN}). Finalizing bucket.")
                break

        # Finalize the current message bucket if it has content
        if current_message_parts:
            final_messages.append("\n\n".join(current_message_parts))
            logger.debug(f"Bucket {i+1} finalized with {len(current_message_parts)} sentences, total length {current_length}.")

        # Stop if we've already reached the target number of messages
        if len(final_messages) >= target_messages:
            if sentences_processed < num_sentences:
                 logger.warning(f"Reached target messages ({target_messages}) but {num_sentences - sentences_processed} sentences remain unprocessed.")
            break

    # If after distributing, some sentences remain (e.g., due to length limits),
    # try adding them as new messages if we haven't hit the target count.
    while sentences_processed < num_sentences and len(final_messages) < target_messages:
        logger.warning(f"Processing remaining sentences. Current messages: {len(final_messages)}/{target_messages}, Sentences left: {num_sentences - sentences_processed}")
        sentence = sentences[sentences_processed]
        if len(sentence) <= TELEGRAM_MAX_LEN:
            final_messages.append(sentence)
            sentences_processed += 1
        else:
            # Aggressively split the remaining long sentence
            split_long_sentence = _split_aggressively(sentence, TELEGRAM_MAX_LEN)
            for part in split_long_sentence:
                if len(final_messages) < target_messages:
                    final_messages.append(part)
                else:
                    break
            sentences_processed += 1 # Mark original sentence processed
        if len(final_messages) >= target_messages: break


    logger.info(f"Splitting finished. Generated {len(final_messages)} messages (target was {target_messages}).")
    return final_messages
