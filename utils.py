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

# --- V13: Improved Splitting ---
def postprocess_response(response: str, max_messages: int) -> List[str]:
    """
    Splits text aiming for approx. max_messages (v13).
    Prioritizes natural breaks, then splits longer parts further.
    """
    if not response or not isinstance(response, str):
        return []

    response = response.strip()
    if not response:
        return []

    logger.debug(f"--- Postprocessing response (Natural Split v13) ---")
    logger.debug(f"Input text length: {len(response)}, Target max_messages: {max_messages}")

    # --- Sanitize max_messages ---
    if not isinstance(max_messages, int) or max_messages <= 0:
        max_messages = 3 # Default fallback
        logger.warning(f"Invalid max_messages value, defaulting to {max_messages}")
    hard_max_messages = 10
    if max_messages > hard_max_messages:
        logger.warning(f"User requested max_messages={max_messages}, capping at {hard_max_messages} for stability.")
        max_messages = hard_max_messages
    # --- End Sanitize ---

    # 1. Initial Natural Split (Paragraphs then Sentences)
    parts = [p.strip() for p in re.split(r'\n\s*\n', response) if p.strip()]
    logger.debug(f"Split by paragraphs resulted in {len(parts)} parts.")

    refined_parts = []
    for part in parts:
        if len(part) <= TELEGRAM_MAX_LEN:
            refined_parts.append(part)
        else:
            logger.debug(f"Part exceeds max length ({len(part)} > {TELEGRAM_MAX_LEN}), splitting by sentence.")
            sentences = re.split(r'(?<=[.!?…])\s+', part)
            refined_parts.extend([s.strip() for s in sentences if s.strip()])
    parts = refined_parts
    logger.debug(f"After sentence splitting long parts, total parts: {len(parts)}")

    # 2. Iteratively split longest parts if count < max_messages
    min_len_for_further_split = 150 # Не делим слишком короткие куски

    while len(parts) < max_messages:
        # Find the longest part eligible for splitting
        longest_part_idx = -1
        max_len_found = min_len_for_further_split
        for i, part in enumerate(parts):
            if len(part) > max_len_found:
                max_len_found = len(part)
                longest_part_idx = i

        if longest_part_idx == -1:
            # No more parts long enough to split further
            logger.debug(f"No more parts longer than {min_len_for_further_split} found to split towards max_messages ({len(parts)}/{max_messages}).")
            break

        part_to_split = parts[longest_part_idx]
        logger.debug(f"Attempting to split longest part (idx {longest_part_idx}, len {len(part_to_split)}) to increase part count ({len(parts)} < {max_messages}).")

        # Attempt to split the part naturally (e.g., near the middle)
        split_point = -1
        target_split_len = len(part_to_split) // 2 # Aim for middle

        # Try splitting by sentence near the middle
        best_sentence_split = -1
        min_sentence_diff = float('inf')
        for match in re.finditer(r'(?<=[.!?…])\s+', part_to_split):
            pos = match.start()
            diff = abs(pos - target_split_len)
            if diff < min_sentence_diff and pos > MIN_SENSIBLE_LEN and len(part_to_split) - pos > MIN_SENSIBLE_LEN:
                 min_sentence_diff = diff
                 best_sentence_split = match.end() # Split *after* the space

        if best_sentence_split != -1:
            split_point = best_sentence_split
            logger.debug(f"Found sentence split point at {split_point}.")
        else:
            # Try splitting by transition word near the middle
            best_transition_split = -1
            min_transition_diff = float('inf')
            for match in TRANSITION_PATTERN.finditer(part_to_split):
                 # Split before the transition word
                 pos = match.start(2) # Start of the transition word itself
                 diff = abs(pos - target_split_len)
                 # Ensure split doesn't create tiny parts
                 if diff < min_transition_diff and pos > MIN_SENSIBLE_LEN and len(part_to_split) - pos > MIN_SENSIBLE_LEN:
                      # Find the start of the line/sentence containing the match for clean split
                      start_of_context = part_to_split.rfind('\n', 0, pos) + 1
                      if start_of_context > 0 or pos < 10: # Avoid splitting right at the beginning if no newline
                          min_transition_diff = diff
                          best_transition_split = pos
            
            if best_transition_split != -1:
                split_point = best_transition_split
                logger.debug(f"Found transition word split point at {split_point}.")
            else:
                # Fallback: Split by last space before the midpoint (or slightly after if needed)
                split_area_end = min(target_split_len + 50, len(part_to_split) - MIN_SENSIBLE_LEN) # Look slightly past midpoint
                fallback_split = part_to_split.rfind(' ', MIN_SENSIBLE_LEN, split_area_end)
                if fallback_split != -1:
                     split_point = fallback_split + 1 # Split after the space
                     logger.debug(f"Using fallback space split point at {split_point}.")

        if split_point != -1:
            part1 = part_to_split[:split_point].strip()
            part2 = part_to_split[split_point:].strip()
            if part1 and part2: # Ensure both parts are non-empty
                parts = parts[:longest_part_idx] + [part1, part2] + parts[longest_part_idx+1:]
                logger.debug(f"Split successful. New part count: {len(parts)}.")
            else:
                 logger.warning(f"Splitting part resulted in empty part(s). Aborting split for this part.")
                 # Mark this part as unsplittable for this iteration by making it short temp.
                 parts[longest_part_idx] = " " * (MIN_SENSIBLE_LEN -1) 
        else:
            logger.warning(f"Could not find a suitable split point for part idx {longest_part_idx}.")
            # Mark as unsplittable for this iteration
            parts[longest_part_idx] = " " * (MIN_SENSIBLE_LEN -1) 

    # 3. Combine parts ONLY if OVER max_messages limit (less likely now)
    if len(parts) > max_messages:
        logger.warning(f"Splitting resulted in {len(parts)} parts, exceeding limit {max_messages}. Combining first parts.")
        while len(parts) > max_messages:
            # Combine part 0 and 1
            combined = parts[0] + "\n\n" + parts[1] # Use double newline when combining
            if len(combined) <= TELEGRAM_MAX_LEN:
                parts[0] = combined
                del parts[1]
            else:
                # If combining makes it too long, we can't combine. Just stop.
                logger.error(f"Cannot combine parts 0 and 1 as they exceed max length. Stopping combination.")
                break # Stop combining if it creates oversized messages

    # 4. Final length check and aggressive split if necessary
    final_parts = []
    for part in parts:
        if len(part) > TELEGRAM_MAX_LEN:
            logger.error(f"Part still exceeds max length ({len(part)}) after all processing! Applying final aggressive split.")
            final_parts.extend(_split_aggressively(part, TELEGRAM_MAX_LEN))
        elif part.strip(): # Add non-empty parts
            final_parts.append(part.strip()) # Ensure parts are stripped

    # 5. Final check against max_messages (aggressive split might increase count)
    if len(final_parts) > max_messages:
        logger.warning(f"Aggressive splitting during final check increased parts ({len(final_parts)}) beyond limit ({max_messages}). Truncating.")
        final_parts = final_parts[:max_messages]

    # Log final results
    logger.info(f"Final processed messages count: {len(final_parts)} (Target: {max_messages})")
    for i, part in enumerate(final_parts):
        logger.debug(f"Finalized message {i+1}/{len(final_parts)} (len={len(part)}): {part[:80]}...")

    return final_parts
