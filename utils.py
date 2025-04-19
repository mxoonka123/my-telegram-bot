import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random

def get_time_info():
    now = datetime.now(timezone.utc)
    # Using common timezones for example
    try:
        msk = now.astimezone(timezone(timedelta(hours=3)))
        msk_str = msk.strftime('%H:%M %d.%m.%Y')
    except Exception:
        msk_str = "N/A"
    try:
        cet = now.astimezone(timezone(timedelta(hours=1))) # Central European Time (Winter)
        cet_str = cet.strftime('%H:%M %d.%m.%Y')
    except Exception:
        cet_str = "N/A"
    try:
        est = now.astimezone(timezone(timedelta(hours=-5))) # US Eastern Standard Time
        est_str = est.strftime('%H:%M %d.%m.%Y')
    except Exception:
        est_str = "N/A"

    return (
        f"сейчас utc {now.strftime('%H:%M %d.%m.%Y')}, мск {msk_str}, берлин {cet_str}, нью-йорк {est_str}."
    )

def extract_gif_links(text: str) -> List[str]:
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass # Ignore decoding errors

    # More robust regex for URLs ending in .gif, potentially with query params
    # Also matches common GIF hosting patterns like Giphy
    gif_patterns = [
        r'(https?://[^\s<>"\']+\.gif(?:[?#][^\s<>"\']*)?)', # .gif links
        r'(https?://media\.giphy\.com/media/[a-zA-Z0-9]+(?:/giphy\.gif)?(?:[?#][^\s<>"\']*)?)', # Giphy media links
        r'(https?://(?:www\.)?tenor\.com/view/[a-zA-Z0-9-]+/[a-zA-Z0-9-]+)', # Tenor links (might not be direct gif, but usually render)
        r'(https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]+\.gif(?:[?#][^\s<>"\']*)?)' # Imgur gif links
    ]

    gif_links = set()
    for pattern in gif_patterns:
         found = re.findall(pattern, text, re.IGNORECASE)
         gif_links.update(found)


    # Basic filtering: ensure it looks like a URL
    valid_links = [link for link in gif_links if link.startswith(('http://', 'https://')) and ' ' not in link]

    return list(valid_links)

def postprocess_response(response: str) -> List[str]:
    if not response or not isinstance(response, str):
        return []

    # Normalize whitespace and remove excessive newlines/carriage returns
    response = re.sub(r'\s+', ' ', response).strip()

    # Split primarily by sentence-ending punctuation followed by space
    # Keep the punctuation with the sentence. Use lookbehind/lookahead.
    sentences = re.split(r'(?<=[.!?…])\s+', response)

    # Further split long sentences without punctuation (heuristic)
    processed_sentences = []
    for sentence in sentences:
         if len(sentence) > 250: # If a segment is very long without punctuation
              # Try splitting by commas or just chunking
              sub_sentences = re.split(r'(?<=,)\s+', sentence)
              temp_buffer = ""
              for sub in sub_sentences:
                   if len(temp_buffer) + len(sub) < 200: # Combine short sub-sentences
                        temp_buffer += (sub + " ")
                   else:
                        if temp_buffer:
                            processed_sentences.append(temp_buffer.strip())
                        temp_buffer = sub + " "
              if temp_buffer:
                  processed_sentences.append(temp_buffer.strip())
         else:
             processed_sentences.append(sentence)


    # Merge short consecutive sentences intelligently
    merged_messages = []
    current_message = ""
    max_length = 150 # Target max length for a message part
    min_length = 20 # Try not to have super short messages unless necessary

    for sentence in processed_sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        potential_length = len(current_message) + len(sentence) + (1 if current_message else 0)

        if not current_message:
             current_message = sentence
        elif potential_length <= max_length:
             current_message += " " + sentence
        else: # potential_length > max_length
             # If current message is too short, try adding anyway if it doesn't exceed max drastically
             if len(current_message) < min_length and potential_length < max_length + 50:
                 current_message += " " + sentence
             else:
                 # Finalize the current message and start a new one
                 merged_messages.append(current_message)
                 current_message = sentence

    # Add the last message
    if current_message:
        merged_messages.append(current_message)

    # Final cleanup: ensure lowercase and strip again
    final_messages = [msg.strip().lower() for msg in merged_messages if msg.strip()]

    return final_messages
