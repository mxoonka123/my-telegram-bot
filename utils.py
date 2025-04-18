import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Union, Tuple
import random

def get_time_info():
    """Возвращает информацию о текущем времени в разных часовых поясах."""
    now = datetime.now(timezone.utc)
    msk = now.astimezone(timezone(timedelta(hours=3)))
    nsk = now.astimezone(timezone(timedelta(hours=7)))
    indo = now.astimezone(timezone(timedelta(hours=7)))
    return (
        f"сейчас по москве {msk.strftime('%H:%M %d.%m.%Y')}, по новосибирску {nsk.strftime('%H:%M %Y.%m.%d')}, "
        f"по семарангу (индонезия) {indo.strftime('%H:%M %Y.%m.%d')}, по utc {now.strftime('%H:%M %Y.%m.%d')}."
    )

def extract_gif_links(text: str) -> List[str]:
    """Извлекает прямые ссылки на GIF из текста."""
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass

    gif_links = []
    gif_links += re.findall(r'(https?://[^\s<>"\']+\.gif(?:\?[^\s<>"\']*)?)', text, re.IGNORECASE)
    gif_links += re.findall(r'(https?://media\.giphy\.com/media/[^\s<>"\']+/giphy(?:\?[^\s<>"\']*)?)', text, re.IGNORECASE)

    return list(set(gif_links))

def postprocess_response(response: str) -> List[str]:
    """Обрабатывает сырой ответ от AI, разбивает на короткие сообщения."""
    if not response or not isinstance(response, str):
        return []

    response = response.strip().replace('\r', ' ').replace('\n', ' ')

    sentences = re.split(r'([.!?…]+)', response)

    result = []
    buffer = ""
    for i in range(0, len(sentences), 2):
        buffer += sentences[i]
        if i + 1 < len(sentences):
            buffer += sentences[i+1]
        result.append(buffer.strip())
        buffer = ""

    if buffer.strip():
        result.append(buffer.strip())

    sentences = [s for s in result if s]

    merged_messages = []
    current_message = ""
    for sentence in sentences:
        if (len(current_message) + len(sentence) + (1 if current_message else 0) > 40 and current_message.strip()) or (not current_message.strip() and len(sentence) > 40):
            if current_message.strip():
                merged_messages.append(current_message.strip())
            current_message = sentence
        else:
            if current_message:
                current_message += " " + sentence
            else:
                current_message = sentence

    if current_message.strip():
        merged_messages.append(current_message.strip())

    merged_messages = [msg.strip().lower() for msg in merged_messages if msg.strip()]

    return merged_messages