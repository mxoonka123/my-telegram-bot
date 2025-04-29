import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random
import logging

logger = logging.getLogger(__name__)

# +++ Экранирование для MarkdownV2 +++
def escape_markdown_v2(text: str) -> str:
    """Escapes characters reserved in Telegram MarkdownV2."""
    if not isinstance(text, str):
        return ""
    # Список символов, требующих экранирования в MarkdownV2
    # Источник: https://core.telegram.org/bots/api#markdownv2-style
    # Добавлены . и ! т.к. они тоже могут вызывать проблемы в некоторых контекстах
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Экранируем символы, добавляя перед ними \
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
# +++ Конец +++

def get_time_info():
    now_utc = datetime.now(timezone.utc)
    time_parts = [f"utc {now_utc.strftime('%H:%M %d.%m.%Y')}"]

    timezones = {
        "мск": timedelta(hours=3),
        "берлин": timedelta(hours=2), # CET with DST usually
        "нью-йорк": timedelta(hours=-4) # EDT usually
    }

    for name, offset in timezones.items():
        try:
            local_time = now_utc.astimezone(timezone(offset))
            time_parts.append(f"{name} {local_time.strftime('%H:%M %d.%m')}") # Shorter format
        except Exception as e:
             logger.warning(f"Could not calculate time for tz offset {offset}: {e}")
             time_parts.append(f"{name} N/A")

    return f"сейчас " + ", ".join(time_parts) + "."


def extract_gif_links(text: str) -> List[str]:
    if not isinstance(text, str): return []
    try:
        # Minimal decoding, avoid errors
        text = urllib.parse.unquote(text)
    except Exception:
        pass # Ignore decoding errors if input is malformed

    # Common GIF patterns
    gif_patterns = [
        r'(https?://[^\s<>"\']+\.gif(?:[?#][^\s<>"\']*)?)', # Direct .gif
        r'(https?://media\.giphy\.com/media/[a-zA-Z0-9]+(?:/giphy\.gif)?(?:[?#][^\s<>"\']*)?)', # Giphy
        # r'(https?://(?:www\.)?tenor\.com/view/[a-zA-Z0-9-]+(?:/[a-zA-Z0-9-]+)?)', # Tenor (often links to page, not direct gif) - maybe exclude
        r'(https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]+\.gif(?:[?#][^\s<>"\']*)?)' # Imgur direct .gif
    ]

    gif_links = set()
    for pattern in gif_patterns:
         try:
             found = re.findall(pattern, text, re.IGNORECASE)
             gif_links.update(found)
         except Exception as e:
              logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")


    # Basic validation and remove duplicates
    valid_links = [link for link in gif_links if isinstance(link, str) and link.startswith(('http://', 'https://')) and ' ' not in link]

    return list(set(valid_links)) # Return unique list

def postprocess_response(response: str) -> List[str]:
    if not response or not isinstance(response, str):
        return []

    # 1. Normalize whitespace: Replace multiple spaces/newlines with single space
    response = re.sub(r'\s+', ' ', response).strip()

    # 2. Split into potential sentences/clauses
    # Split by common sentence endings followed by space. Keep the punctuation.
    # Also split by double newlines if they somehow survived normalization (less likely now)
    sentences = re.split(r'(?<=[.!?…])\s+', response)

    # 3. Process segments: Merge short ones, ensure no excessively long ones
    merged_messages = []
    current_message = ""
    # Adjust thresholds as needed
    max_length = 350 # Allow slightly longer messages if needed
    ideal_length = 200 # Try to stay around this
    min_length = 10 # Avoid tiny messages unless it's the whole response

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        potential_length = len(current_message) + len(sentence) + (1 if current_message else 0)

        if not current_message:
            # Start new message
            current_message = sentence
        elif potential_length <= ideal_length:
            # Add to current message if it fits nicely
            current_message += " " + sentence
        elif len(current_message) < min_length and potential_length <= max_length:
             # If current message is very short, allow adding if it doesn't exceed max length too much
             current_message += " " + sentence
        else:
            # Current message is long enough, or adding makes it too long. Finalize current.
            merged_messages.append(current_message)
            current_message = sentence # Start new message with current sentence

    # Add the last remaining message
    if current_message:
        merged_messages.append(current_message)

    # 4. Final cleanup: Strip again, filter empty
    # <<< ИЗМЕНЕНО: НЕ делаем lowercase здесь, т.к. это может сломать Markdown >>>
    final_messages = [msg.strip() for msg in merged_messages if msg.strip()]

    # 5. Sanity check: If somehow the result is empty but input wasn't, return original split differently
    if not final_messages and response:
         logger.warning("Postprocessing resulted in empty list, returning basic split.")
         # Fallback to splitting by space if all else fails
         return [part for part in response.split() if part]

    return final_messages
