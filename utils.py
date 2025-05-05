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
    V8: Prioritizes LLM newlines, merges intelligently if too many parts,
        uses aggressive split as fallback. Handles random max_messages.
    """
    telegram_max_len = 4096
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response: return []

    # --- Handle max_messages setting ---
    original_max_setting = max_messages # Store original setting for logging if needed
    if max_messages <= 0:
        # If setting is 0 or negative, choose randomly between 1, 2, or 3
        max_messages = random.randint(1, 3)
        logger.info(f"Original max_messages setting was {original_max_setting}. Choosing randomly: {max_messages}")
    elif max_messages > 10:
        # Limit the maximum number of messages to 10
        logger.warning(f"Max messages ({original_max_setting}) exceeds limit 10. Setting to 10.")
        max_messages = 10
    else:
        # Use the valid setting (1-10)
        logger.info(f"Using max_messages setting: {max_messages}")
    # --- End max_messages handling ---

    # If only one message is needed after handling random/limits
    if max_messages == 1:
        if len(response) > telegram_max_len:
            logger.warning(f"Single message required, but response too long ({len(response)}). Truncating.")
            return [response[:telegram_max_len - 3] + "..."]
        else:
            logger.info("Single message required and length is acceptable.")
            return [response]

    logger.info(f"--- Postprocessing response V8 --- Max messages allowed: {max_messages}")
    initial_parts = []
    processed_by_newline = False

    # 1. Detect newlines (\n\n preferred, then \n using re.split for any type)
    # Try double newlines first
    potential_parts_double = [p.strip() for p in re.split(r'(?:\r?\n){2,}|\r{2,}', response) if p.strip()]
    if len(potential_parts_double) > 1:
        logger.info(f"Split by DOUBLE newlines resulted in {len(potential_parts_double)} parts.")
        initial_parts = potential_parts_double
        processed_by_newline = True
    else:
        # Try single newlines if double didn't work
        potential_parts_single = [p.strip() for p in re.split(r'\r?\n|\r', response) if p.strip()]
        if len(potential_parts_single) > 1:
            logger.info(f"Split by SINGLE newlines resulted in {len(potential_parts_single)} parts.")
            initial_parts = potential_parts_single
            processed_by_newline = True
        else:
            logger.info("Did not find any type of newline splits in the response.")

    final_messages = []

    # 2. Process based on newline detection
    if processed_by_newline:
        # --- MERGE LOGIC if LLM provided too many parts ---
        if len(initial_parts) > max_messages:
            logger.info(f"LLM provided {len(initial_parts)} parts (more than max {max_messages}). Attempting to merge parts...")
            merged_parts = []
            current_part_text = ""
            # Iterate through parts provided by LLM's newlines
            for i, part in enumerate(initial_parts):
                separator = "\n\n" if current_part_text else "" # Use double newline for merging visual separation

                # Check if adding the next part fits within Telegram limit AND we haven't filled up allowed message slots (leaving one slot for the rest if possible)
                if len(current_part_text) + len(separator) + len(part) <= telegram_max_len and len(merged_parts) < max_messages - 1:
                    current_part_text += separator + part
                else:
                    # If current merged part is not empty, save it
                    if current_part_text:
                        merged_parts.append(current_part_text)
                    # Start the new part with the current 'part'
                    current_part_text = part
                    # Check if we have already reached the maximum number of messages allowed
                    if len(merged_parts) >= max_messages:
                        logger.warning(f"Reached max_messages ({max_messages}) during merge. Discarding part: '{part[:50]}...' and any further parts.")
                        current_part_text = "" # Discard this part too
                        break # Stop processing more parts

            # Add the last accumulated part if it exists and there's still room
            if current_part_text and len(merged_parts) < max_messages:
                merged_parts.append(current_part_text)

            # Final check - if merging somehow still resulted in too many parts (unlikely with the logic above but safe)
            if len(merged_parts) > max_messages:
                 logger.warning(f"Merging still resulted in {len(merged_parts)} parts, trimming to {max_messages}")
                 final_messages = merged_parts[:max_messages]
                 # Add ellipsis to the very last part if trimming occurred
                 if final_messages and final_messages[-1]:
                      last_p = final_messages[-1].rstrip('.!?… ')
                      if not last_p.endswith('...'): final_messages[-1] = f"{last_p}..."
            else:
                 final_messages = merged_parts
            logger.info(f"After merging LLM parts based on newlines: {len(final_messages)} messages.")

        else:
            # Use parts as is if LLM provided <= max_messages parts
            final_messages = initial_parts
            logger.info(f"Using LLM's {len(final_messages)} newline-separated parts directly (count <= max_messages).")

    # 3. Aggressive split ONLY if NO newlines were found by LLM
    else: # not processed_by_newline
        logger.warning("No newlines found in LLM response. Using aggressive splitting by length/space.")
        aggressive_parts = []
        # Calculate estimated length per part
        # Add 1 to prevent division by zero if response is tiny
        estimated_len = math.ceil(len(response) / max_messages)
        estimated_len = max(estimated_len, 50) # Ensure parts are not too tiny
        estimated_len = min(estimated_len, telegram_max_len - 10) # Ensure parts don't exceed limit easily

        start = 0
        # Create up to max_messages parts
        for i in range(max_messages):
            # Calculate potential end position
            end = min(start + estimated_len, len(response))
            # If this is the last allowed part, take everything remaining
            if i == max_messages - 1:
                end = len(response)

            # Try to find a better break point (space) before the potential end, if not already at the end of the response
            if end < len(response):
                # Look for the last space within the calculated chunk
                space_pos = response.rfind(' ', start, end)
                # If a space is found and it's not at the very beginning of the chunk, use it as the end point
                if space_pos > start:
                    end = space_pos + 1 # Include the space in the previous part or cut after it

            # Extract the part
            part = response[start:end].strip()
            if part: # Add only non-empty parts
                aggressive_parts.append(part)

            # Update the start position for the next iteration
            start = end
            # If we've processed the entire response, stop
            if start >= len(response):
                break

        final_messages = aggressive_parts
        logger.info(f"Aggressive splitting created {len(final_messages)} parts.")


    # 4. Final length check and cleanup for ALL resulting messages
    processed_messages = []
    for msg in final_messages:
        # Clean empty lines that might result from merging/splitting
        msg_cleaned = "\n".join(line.strip() for line in msg.strip().splitlines() if line.strip())
        if not msg_cleaned:
            logger.warning("Skipping empty message part after final cleaning.")
            continue # Skip empty messages

        # Check length against Telegram limit
        if len(msg_cleaned) > telegram_max_len:
            logger.warning(f"Final message part exceeds limit ({len(msg_cleaned)} > {telegram_max_len}). Truncating.")
            processed_messages.append(msg_cleaned[:telegram_max_len - 3] + "...")
        else:
            processed_messages.append(msg_cleaned)

    # Ensure we don't return more messages than requested, even after cleanup (e.g., if aggressive split created an extra tiny part)
    if len(processed_messages) > max_messages:
        logger.warning(f"Final message count ({len(processed_messages)}) still exceeds max_messages ({max_messages}). Trimming final list.")
        processed_messages = processed_messages[:max_messages]
        # Add ellipsis if trimming happened here
        if processed_messages and processed_messages[-1] and not processed_messages[-1].endswith("..."):
             last_p = processed_messages[-1].rstrip('.!?… ')
             processed_messages[-1] = f"{last_p}..."


    logger.info(f"Final processed messages V8 count: {len(processed_messages)}")
    return processed_messages
