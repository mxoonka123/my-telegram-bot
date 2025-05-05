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
    Natural Split (Always Split) v11.
    Splits text into separate messages, preserving natural conversation flow.
    """
    if not response or not isinstance(response, str):
        return []
    
    response = response.strip()
    if not response:
        return []

    logger.debug(f"--- Postprocessing response (Natural Split v11 - Always Split) ---")
    
    # First try to split by natural conversation points (sentences, questions, exclamations)
    parts = [p.strip() for p in re.split(r'(?<=[.!?…])\s+', response) if p.strip()]
    logger.debug(f"Found {len(parts)} natural parts in response.")
    
    # If we have more parts than max_messages, combine them
    if len(parts) > max_messages:
        combined_parts = []
        current_part = ""
        
        for i, part in enumerate(parts):
            # If adding this part would exceed max_messages, finalize current part
            if len(combined_parts) + 1 >= max_messages:
                if current_part:
                    combined_parts.append(current_part)
                combined_parts.extend(parts[i:])
                break
            
            # If current part would be too long if combined
            if len(current_part) + len(part) + 2 > TELEGRAM_MAX_LEN:
                if current_part:
                    combined_parts.append(current_part)
                current_part = part
            else:
                # Combine with previous part using double newline
                current_part = f"{current_part}\n\n{part}"
        
        if current_part:
            combined_parts.append(current_part)
        
        parts = combined_parts
    
    # If we still have fewer parts than max_messages, split larger ones
    if len(parts) < max_messages and len(parts) > 0:
        avg_len = len(response) // max_messages  # Use integer division
        
        new_parts = []
        for part in parts:
            if len(part) <= avg_len:
                new_parts.append(part)
                continue
            
            # Split large part into smaller chunks
            current_chunk = ""
            words = part.split()
            for word in words:
                # If adding this word would exceed average length or Telegram max
                if (current_chunk and 
                    (len(current_chunk) + len(word) + 1 > avg_len or 
                     len(current_chunk) + len(word) + 1 > TELEGRAM_MAX_LEN)):
                    new_parts.append(current_chunk.strip())
                    current_chunk = word
                else:
                    current_chunk = f"{current_chunk} {word}" if current_chunk else word
            
            if current_chunk:
                new_parts.append(current_chunk.strip())
        
        parts = new_parts[:max_messages]  # Limit to max_messages
    
    # Final check to ensure we don't exceed max_messages
    if len(parts) > max_messages:
        parts = parts[:max_messages]
    
    # Add a newline between parts for better readability
    for i in range(len(parts)):
        if i > 0:
            parts[i] = f"\n\n{parts[i]}"
    
    return parts
    
    # Log final results
    logger.info(f"Final processed messages... count: {len(parts)}")
    for i, part in enumerate(parts):
        logger.debug(f"Finalized message {i+1}/{len(parts)} (len={len(part)}): {part[:50]}...")
    
    return parts
