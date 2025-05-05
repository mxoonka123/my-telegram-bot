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
    V11: Prioritizes LLM newlines, then splits by sentences, ensuring
         sentence completeness within messages.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    # --- Handle max_messages setting ---
    original_max_setting = max_messages # Keep track for logging
    if max_messages <= 0:
        max_messages = random.randint(1, 3)
        logger.info(f"Max messages set to random (1-3) from setting {original_max_setting}. Chosen: {max_messages}")
    elif max_messages > 10: # Limit max messages
        logger.warning(f"Max messages ({original_max_setting}) exceeds limit 10. Setting to 10.")
        max_messages = 10
    else:
        logger.info(f"Using max_messages setting: {max_messages}")
    # --- End max_messages handling ---

    # Handle the simple case of 1 message needed
    if max_messages == 1:
        if len(response) > telegram_max_len:
            logger.warning(f"Single message required, but response too long ({len(response)}). Truncating.")
            return [response[:telegram_max_len - 3] + "..."]
        else:
            logger.info("Single message required and length is acceptable.")
            return [response]

    logger.info(f"--- Postprocessing response V11 --- Max messages allowed: {max_messages}")
    parts_based_on_llm_newlines = []
    processed_by_newline = False

    # 1. Detect newlines (\n\n preferred, then \n using re.split for any type)
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        logger.info(f"Found {len(potential_parts_double)} parts split by DOUBLE newlines.")
        parts_based_on_llm_newlines = potential_parts_double
        processed_by_newline = True
    else:
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            logger.info(f"Found {len(potential_parts_single)} parts split by SINGLE newlines.")
            parts_based_on_llm_newlines = potential_parts_single
            processed_by_newline = True
        else:
            logger.info("Did not find any type of newline splits in the response.")

    final_messages = [] # This will hold the final list of messages to send

    # 2. Process based on newline detection
    if processed_by_newline:
        logger.debug(f"Processing {len(parts_based_on_llm_newlines)} parts found via newlines.")
        # If LLM provided more parts than allowed, just take the first few
        if len(parts_based_on_llm_newlines) > max_messages:
            logger.info(f"Trimming newline-split parts from {len(parts_based_on_llm_newlines)} down to {max_messages}.")
            final_messages = parts_based_on_llm_newlines[:max_messages]
            # Add ellipsis to the last message if trimming occurred
            if final_messages and final_messages[-1]:
                 last_p = final_messages[-1].rstrip('.!?… ')
                 if not last_p.endswith('...'): final_messages[-1] = f"{last_p}..."
        else:
            # Use all parts provided by LLM if count is within limit
            final_messages = parts_based_on_llm_newlines
        logger.info(f"Using {len(final_messages)} messages based on LLM newlines.")

    # 3. If NO newlines were found -> Attempt splitting by sentences
    else: # not processed_by_newline
        logger.warning("No newlines found. Attempting split by sentences.")
        # Regex to split after sentence-ending punctuation, keeping the punctuation
        sentences = re.split(r'(?<=[.!?…])\s+', response)
        sentences = [s.strip() for s in sentences if s.strip()] # Clean empty entries

        if not sentences: # Fallback if splitting failed or response was empty
             logger.error("Could not split response by sentences. Returning original (or truncated).")
             if len(response) > telegram_max_len:
                 return [response[:telegram_max_len - 3] + "..."]
             elif response:
                 return [response]
             else:
                 return [] # Should not happen if initial checks passed

        logger.info(f"Split by sentences resulted in {len(sentences)} potential sentences.")

        # Assemble messages from sentences
        assembled_messages = []
        current_message_content = ""
        sentences_in_current_message = 0
        total_sentences_processed = 0

        for i, sentence in enumerate(sentences):
            sentence_len = len(sentence)
            # Use space as separator if message already has content
            separator = " " if current_message_content else ""
            separator_len = len(separator)

            # Check if adding this sentence would exceed limits
            if len(assembled_messages) < max_messages and \
               (not current_message_content or # Always add the first sentence to an empty message
                current_len + separator_len + sentence_len <= telegram_max_len):

                # Add sentence to current message
                current_message_content += separator + sentence
                current_len = len(current_message_content) # Recalculate length
                sentences_in_current_message += 1
                total_sentences_processed += 1

            else:
                # Cannot add sentence: finalize the previous message (if not empty)
                if current_message_content:
                    assembled_messages.append(current_message_content)

                # Check if we have already reached the message limit
                if len(assembled_messages) >= max_messages:
                    logger.warning(f"Reached max_messages ({max_messages}) during sentence assembly. Discarding remaining {len(sentences) - i} sentences.")
                    current_message_content = "" # Ensure no trailing part is added
                    break # Stop processing more sentences

                # Start a new message with the current sentence
                current_message_content = sentence
                current_len = sentence_len
                sentences_in_current_message = 1
                total_sentences_processed += 1


        # Add the last assembled message if it's not empty
        if current_message_content and len(assembled_messages) < max_messages:
            assembled_messages.append(current_message_content)

        # Add ellipsis if not all sentences were included AND we hit the message limit
        if total_sentences_processed < len(sentences) and len(assembled_messages) == max_messages:
             if assembled_messages and assembled_messages[-1] and not assembled_messages[-1].endswith("..."):
                 last_msg = assembled_messages[-1].rstrip('.!?… ')
                 assembled_messages[-1] = f"{last_msg}..."

        final_messages = assembled_messages
        logger.info(f"Sentence splitting resulted in {len(final_messages)} messages.")

    # 4. Final cleanup and length check (applies to both newline and sentence paths)
    processed_messages = []
    for msg in final_messages:
        # Clean empty lines within the message itself
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned:
            logger.warning("Skipping empty message part after final cleaning.")
            continue

        # Check length against Telegram limit
        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part exceeds limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    # Final safeguard: ensure we don't exceed max_messages
    if len(processed_messages) > max_messages:
        logger.warning(f"Final message count ({len(processed_messages)}) still exceeds max_messages ({max_messages}). Trimming final list.")
        processed_messages = processed_messages[:max_messages]
        # Add ellipsis if trimming happened here
        if processed_messages and processed_messages[-1] and not processed_messages[-1].endswith("..."):
             last_p = processed_messages[-1].rstrip('.!?… ')
             processed_messages[-1] = f"{last_p}..."

    logger.info(f"Final processed messages V11 count: {len(processed_messages)}")
    return processed_messages
