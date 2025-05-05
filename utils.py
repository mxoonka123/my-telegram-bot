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

# --- V9: Simple Aggressive Split (Always Split) ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Natural Split (Always Split) v10.
    Splits text into natural chunks while preserving sentence integrity.
    """
    if not response or not isinstance(response, str):
        return []
    
    response = response.strip()
    if not response:
        return []

    logger.debug(f"--- Postprocessing response (Natural Split v10 - Always Split) ---")
    
    # Split by sentences first, preserving punctuation
    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', response) if s.strip()]
    logger.debug(f"Found {len(sentences)} sentences in response.")
    
    # Calculate target length per message based on sentences
    target_len = max(MIN_SENSIBLE_LEN, math.ceil(len(response) / max(2, min(10, max_messages))))
    
    # Build messages, trying to keep sentences together
    messages = []
    current_msg = ""
    current_len = 0
    
    for sentence in sentences:
        if not sentence: continue
        
        # Calculate new length with sentence
        new_len = current_len + len(sentence) + (2 if current_msg else 0)  # +2 for \n\n
        
        # If adding this sentence would exceed length, finalize current message
        if current_msg and new_len > target_len:
            messages.append(current_msg)
            current_msg = sentence
            current_len = len(sentence)
            
            # If we have enough messages, try to keep remaining sentences together
            if len(messages) >= max_messages - 1:
                break
        else:
            # Add sentence to current message with double newline
            current_msg = f"{current_msg}\n\n{sentence}" if current_msg else sentence
            current_len = new_len
    
    # Add remaining text if any
    if current_msg:
        messages.append(current_msg)
    
    # If we have too few messages, try to split larger ones naturally
    if len(messages) < max_messages:
        for i in range(len(messages) - 1, -1, -1):
            if len(messages) >= max_messages:
                break
            
            msg = messages[i]
            if len(msg) > target_len * 1.5:  # Only split if significantly larger
                # Try to split at a natural point (comma, space)
                split_point = msg.rfind(' ', 0, len(msg)//2)
                if split_point == -1:
                    split_point = len(msg)//2
                
                messages[i] = msg[:split_point].strip()
                messages.insert(i + 1, msg[split_point:].strip())
    
    # Final check and trim if needed
    if len(messages) > max_messages:
        messages = messages[:max_messages]
    
    # Log final results
    logger.info(f"Final processed messages... count: {len(messages)}")
    for i, msg in enumerate(messages):
        logger.debug(f"Finalized message {i+1}/{len(messages)} (len={len(msg)}): {msg[:50]}...")
    
    return messages
