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
    Simple Aggressive Split (Always Split) v9.
    Always splits text into messages, even if it fits in one.
    """
    if not response or not isinstance(response, str):
        return []
    
    response = response.strip()
    if not response:
        return []

    logger.debug(f"--- Postprocessing response (Simple Aggressive Split v9 - Always Split) ---")
    
    # Always split into at least 2 messages
    target_messages = max(2, min(10, max_messages)) if max_messages > 0 else random.randint(2, 3)
    
    # Calculate target length per message
    target_len = max(MIN_SENSIBLE_LEN, math.ceil(len(response) / target_messages))
    
    # Split by sentences first
    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', response) if s.strip()]
    logger.debug(f"Found {len(sentences)} sentences in response.")
    
    # Build messages
    messages = []
    current_msg = ""
    
    for sentence in sentences:
        if not sentence: continue
        
        # If adding this sentence would exceed length, finalize current message
        if current_msg and len(current_msg) + len(sentence) + 1 > target_len:
            messages.append(current_msg)
            current_msg = ""
            if len(messages) >= target_messages:
                break
        
        # Add sentence to current message
        current_msg = f"{current_msg}\n{sentence}".strip() if current_msg else sentence
        
        # Always finalize message after each sentence if we're still below target
        if len(messages) < target_messages and current_msg:
            messages.append(current_msg)
            current_msg = ""
    
    # Add remaining text
    if current_msg and len(messages) < target_messages:
        messages.append(current_msg)
    
    # If we have too few messages, split the last one
    while len(messages) < target_messages and messages:
        last_msg = messages.pop()
        split_point = len(last_msg) // 2
        messages.extend([last_msg[:split_point], last_msg[split_point:]])
    
    # Final aggressive split if any message is still too long
    final_messages = []
    for i, msg in enumerate(messages):
        if len(msg) > TELEGRAM_MAX_LEN:
            logger.debug(f"Message {i+1}/{len(messages)} is too long ({len(msg)} chars), splitting...")
            final_messages.extend(_split_aggressively(msg, TELEGRAM_MAX_LEN))
        else:
            final_messages.append(msg)
        
        # Log message lengths
        logger.debug(f"Final message {i+1}/{len(final_messages)} length: {len(final_messages[i])} chars")
    
    # Ensure we don't exceed target number of messages
    final_messages = final_messages[:target_messages]
    
    # Log final results
    logger.info(f"Final processed messages... count: {len(final_messages)}")
    for i, msg in enumerate(final_messages):
        logger.debug(f"Finalized message {i+1}/{len(final_messages)} (len={len(msg)}): {msg[:50]}...")
    
    return final_messages
