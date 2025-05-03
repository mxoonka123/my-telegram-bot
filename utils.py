import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging

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


def postprocess_response(response: str) -> List[str]:
    """Splits the bot's response into suitable message parts."""
    if not response or not isinstance(response, str):
        return []

    # 1. Normalize whitespace and remove leading/trailing markdown artifacts
    response = re.sub(r'\s+', ' ', response).strip().strip('_*`')
    if not response: return []

    # 2. Split by sentence-ending punctuation followed by space or end-of-string.
    #    Keep the delimiters. Use lookbehind. Handle multiple delimiters.
    sentences = re.split(r'(?<=[.!?…])\s*', response) # Split after delimiter and optional space

    # 3. Merge sentences into messages, respecting length limits
    merged_messages = []
    current_message = ""
    max_length = 4000 # Telegram's approximate limit, reduced for safety
    ideal_length = 300 # Try to keep messages shorter for readability

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence: continue

        # Check length BEFORE adding space
        potential_len = len(current_message) + len(sentence) + (1 if current_message else 0)

        if not current_message:
            # Start new message if current is empty
            current_message = sentence
        elif potential_len <= ideal_length:
            # Append if fits within ideal length
            current_message += " " + sentence
        elif len(sentence) < ideal_length // 2 and potential_len <= max_length:
             # Append short sentences if total doesn't exceed max
             current_message += " " + sentence
        else:
            # Sentence is too long or makes current message too long
            # Finish the current message and start a new one
            merged_messages.append(current_message)
            current_message = sentence

        # Force split if current message exceeds max length (should be rare with above logic)
        if len(current_message) > max_length:
             # Find a good split point (e.g., space) near the max length
             split_point = current_message.rfind(' ', 0, max_length)
             if split_point == -1: split_point = max_length # Force split if no space found

             merged_messages.append(current_message[:split_point].strip())
             current_message = current_message[split_point:].strip()

    # Add the last part
    if current_message:
        merged_messages.append(current_message)

    # Final cleanup
    final_messages = [msg.strip() for msg in merged_messages if msg.strip()]

    return final_messages if final_messages else [response] # Fallback to single message
