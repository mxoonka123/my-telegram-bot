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
    # Use a lambda function to avoid issues with backslashes in replacement
    return re.sub(f'([{re.escape(escape_chars)}])', lambda m: '\\' + m.group(1), text)

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
        # Decode URL-encoded characters first (e.g., %2F becomes /)
        decoded_text = urllib.parse.unquote(text)
    except Exception:
        decoded_text = text # Use original if decoding fails

    # Patterns for common GIF hosting sites and direct .gif links
    gif_patterns = [
        r'(https?://media\.giphy\.com/media/[a-zA-Z0-9]+/giphy\.gif)', # Giphy direct links
        r'(https?://media[0-9]*\.giphy\.com/media/[a-zA-Z0-9]+/giphy\.gif)', # Giphy media links (media0, media1, etc.)
        r'(https?://giphy\.com/gifs/(?:[a-zA-Z0-9]+-)*([a-zA-Z0-9]+))', # Giphy page links (extract ID, need conversion later maybe?)
        r'(https?://tenor\.com/view/[a-zA-Z0-9%-]+)', # Tenor page links (need conversion)
        r'(https?://tenor\.com/.*\.(?:gif))', # Tenor direct gif links
        r'(https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]+\.gif)', # Imgur direct links
        r'(https?://[^\s<>"\']+\.gif(?:[?#][^\s<>"\']*)?)' # Generic direct .gif links (should be last)
    ]

    gif_links = set()
    for pattern in gif_patterns:
         try:
             # Find all matches in the decoded text
             found = re.findall(pattern, decoded_text, re.IGNORECASE)
             # Handle tuples returned by patterns with capture groups (like giphy page links)
             for item in found:
                 if isinstance(item, tuple):
                     # If it's a tuple, take the last non-empty element (usually the ID or relevant part)
                     link_part = next((part for part in reversed(item) if part), None)
                     if link_part:
                         # Reconstruct potential full link if needed (depends on pattern)
                         # For Giphy page links, we might just store the ID or the full page URL
                         if 'giphy.com/gifs/' in pattern:
                             full_link = f"https://media.giphy.com/media/{link_part}/giphy.gif" # Attempt direct link reconstruction
                             gif_links.add(full_link)
                         elif 'tenor.com/view/' in pattern:
                              # Tenor links are tricky, often need API or scraping. Store page link for now.
                              gif_links.add(item[0] if isinstance(item, tuple) else item) # Store the full match (URL)
                         else:
                              gif_links.add(link_part)
                 elif isinstance(item, str):
                     gif_links.add(item)

         except Exception as e:
              logger.error(f"Regex error in extract_gif_links for pattern '{pattern}': {e}")

    # Filter for valid HTTP/HTTPS URLs without spaces
    valid_links = [link for link in gif_links if isinstance(link, str) and link.startswith(('http://', 'https://')) and ' ' not in link]

    # Prioritize direct .gif links if multiple types found for the same source
    # (This part is complex and might need refinement based on observed AI outputs)
    final_links = []
    processed_sources = set() # To avoid adding multiple links for the same GIF source page

    for link in valid_links:
        is_direct_gif = link.lower().endswith('.gif')
        source_page = None

        # Try to identify source page for non-direct links
        if 'giphy.com/gifs/' in link:
            match = re.search(r'giphy\.com/gifs/(?:[a-zA-Z0-9]+-)*([a-zA-Z0-9]+)', link)
            if match: source_page = f"giphy_{match.group(1)}"
        elif 'tenor.com/view/' in link:
             source_page = link # Use the full tenor page URL as the source identifier

        # Add direct links immediately
        if is_direct_gif:
            final_links.append(link)
            if source_page: processed_sources.add(source_page)
        # Add source page links only if no direct link for that source was added
        elif source_page and source_page not in processed_sources:
             # We might not be able to send tenor page links directly, but store them for now
             # Or potentially try to resolve them later if needed
             if 'tenor.com' in link:
                 logger.debug(f"Found Tenor page link, cannot directly send: {link}")
                 # Optionally skip adding tenor page links if they can't be used
                 # continue
             final_links.append(link)
             processed_sources.add(source_page)
        elif not source_page: # Add other valid links that aren't direct gifs or known pages
             final_links.append(link)


    # Return unique, valid links, prioritizing direct .gif if possible
    # Using list(dict.fromkeys(final_links)) preserves order while ensuring uniqueness
    return list(dict.fromkeys(final_links))


def postprocess_response(response: str) -> List[str]:
    if not response or not isinstance(response, str):
        return []

    # 1. Normalize whitespace more aggressively
    response = re.sub(r'\s+', ' ', response).strip()
    # Remove potential leading/trailing markdown-like characters leftover from generation
    response = response.strip('_*`')

    # 2. Split into potential sentences/clauses, preserving delimiters
    # Split by '.', '!', '?', '…' followed by space or end of string
    # Use lookbehind `(?<=[.!?…])` to keep the delimiter with the preceding sentence
    sentences = re.split(r'(?<=[.!?…])(?=\s|$)', response)

    # 3. Process segments
    merged_messages = []
    current_message = ""
    max_length = 450 # Allow longer segments before force splitting
    ideal_length = 250 # Try to keep segments around this length
    min_length = 10

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        potential_length = len(current_message) + len(sentence) + (1 if current_message else 0)

        if not current_message:
            # Start a new message
            current_message = sentence
        elif potential_length <= ideal_length:
            # Append if within ideal length
            current_message += " " + sentence
        elif len(current_message) < min_length and potential_length <= max_length:
             # Append if current message is very short and total is within max
             current_message += " " + sentence
        else:
            # Current message is long enough, start a new one
            merged_messages.append(current_message)
            current_message = sentence

    # Add the last accumulated message
    if current_message:
        merged_messages.append(current_message)

    # 4. Final cleanup: strip again and remove empty messages
    final_messages = [msg.strip() for msg in merged_messages if msg.strip()]

    # 5. Sanity check: If splitting resulted in nothing, return the original response as one part
    if not final_messages and response:
         logger.warning("Postprocessing resulted in empty list, returning original response as one part.")
         return [response] # Return original response in a list

    return final_messages
