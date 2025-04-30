import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging

logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """Escapes characters reserved in Telegram MarkdownV2."""
    if not isinstance(text, str):
        return ""
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_time_info():
    now_utc = datetime.now(timezone.utc)
    time_parts = [f"utc {now_utc.strftime('%H:%M %d.%m.%Y')}"]

    timezones = {
        "мск": timedelta(hours=3),
        "берлин": timedelta(hours=2),
        "нью-йорк": timedelta(hours=-4)
    }

    for name, offset in timezones.items():
        try:
            local_time = now_utc.astimezone(timezone(offset))
            time_parts.append(f"{name} {local_time.strftime('%H:%M %d.%m')}")
        except Exception as e:
             logger.warning(f"Could not calculate time for tz offset {offset}: {e}")
             time_parts.append(f"{name} N/A")

    return f"сейчас " + ", ".join(time_parts) + "."


def extract_gif_links(text: str) -> List[str]:
    if not isinstance(text, str): return []
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass

    gif_patterns = [
        r'(https?://[^\s<>"\']+\.gif(?:[?#][^\s<>"\']*)?)',
        r'(https?://media\.giphy\.com/media/[a-zA-Z0-9]+(?:/giphy\.gif)?(?:[?#][^\s<>"\']*)?)',
        r'(https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]+\.gif(?:[?#][^\s<>"\']*)?)'
    ]

    gif_links = set()
    for pattern in gif_patterns:
         try:
             found = re.findall(pattern, text, re.IGNORECASE)
             gif_links.update(found)
         except Exception as e:
              logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")

    valid_links = [link for link in gif_links if isinstance(link, str) and link.startswith(('http://', 'https://')) and ' ' not in link]

    return list(set(valid_links))

def postprocess_response(response: str) -> List[str]:
    if not response or not isinstance(response, str):
        return []

    # 1. Normalize whitespace more aggressively
    response = re.sub(r'\s+', ' ', response).strip()
    # Remove potential leading/trailing markdown-like characters leftover from generation
    response = response.strip('_*`')

    # 2. Split into potential sentences/clauses
    sentences = re.split(r'(?<=[.!?…])\s+', response)

    # 3. Process segments
    merged_messages = []
    current_message = ""
    # <<< ИЗМЕНЕНО: Увеличена максимальная длина сегмента >>>
    max_length = 450 # Allow longer segments before force splitting
    ideal_length = 250 # Try to keep segments around this length
    min_length = 10

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        potential_length = len(current_message) + len(sentence) + (1 if current_message else 0)

        if not current_message:
            current_message = sentence
        elif potential_length <= ideal_length:
            current_message += " " + sentence
        elif len(current_message) < min_length and potential_length <= max_length:
             current_message += " " + sentence
        else:
            merged_messages.append(current_message)
            current_message = sentence

    if current_message:
        merged_messages.append(current_message)

    # 4. Final cleanup
    final_messages = [msg.strip() for msg in merged_messages if msg.strip()]

    # 5. Sanity check
    if not final_messages and response:
         logger.warning("Postprocessing resulted in empty list, returning basic split.")
         return [part for part in response.split() if part]

    return final_messages
