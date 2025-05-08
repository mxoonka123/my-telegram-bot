"""Monkeypatch to ensure message count limits are strictly enforced across the bot.

This module patches utils.postprocess_response to guarantee that the returned
list length never exceeds the requested max_messages. It should be imported
once at application start-up (e.g. from main.py).
"""
from __future__ import annotations

import logging
from typing import List

import handlers as _handlers
import json as _json
import random as _random
import utils as _utils
from functools import wraps

_logger = logging.getLogger(__name__)

_original_postprocess = _utils.postprocess_response  # type: ignore[attr-defined]

def _patched_postprocess_response(response: str, max_messages: int, message_volume: str = "normal") -> List[str]:
    parts = _original_postprocess(response, max_messages, message_volume)
    if len(parts) > max_messages:
        _logger.warning(
            "message_limit_patch: postprocess_response returned %d parts, exceeding requested %d. Truncating.",
            len(parts), max_messages,
        )
        parts = parts[:max_messages]
    return parts

_original_process_and_send = _handlers.process_and_send_response  # type: ignore[attr-defined]

@wraps(_original_process_and_send)
async def _patched_process_and_send_response(update, context, chat_id, persona, full_bot_response_text, db, reply_to_message_id=None, is_first_message=False):  # noqa: D401,E501
    """Wrapper that enforces max_response_messages *before* sending."""
    try:
        max_setting = getattr(persona.config, "max_response_messages", 3) if getattr(persona, "config", None) else 3
        # Compute actual requested max like in handlers.handle_message
        if max_setting == 1:
            allowed = 1
        elif max_setting == 3:
            allowed = 3
        elif max_setting == 6:
            allowed = 6
        else:  # 0 or other -> random 2-6
            allowed = _random.randint(2, 6)

        stripped = full_bot_response_text.lstrip() if isinstance(full_bot_response_text, str) else ""
        if stripped.startswith("["):
            try:
                data = _json.loads(stripped)
                if isinstance(data, list) and all(isinstance(i, str) for i in data):
                    if len(data) > allowed:
                        _logger.info(
                            "message_limit_patch: Truncating JSON array from %d to %d elements for persona %s.",
                            len(data), allowed, getattr(persona, "name", "<unknown>"),
                        )
                        data = data[:allowed]
                        full_bot_response_text = _json.dumps(data, ensure_ascii=False)
            except Exception as e:
                _logger.warning("message_limit_patch: failed JSON pre-trim: %s", e)
    except Exception as outer_e:
        _logger.error("message_limit_patch: unexpected error computing allowed count: %s", outer_e)

    return await _original_process_and_send(update, context, chat_id, persona, full_bot_response_text, db, reply_to_message_id=reply_to_message_id, is_first_message=is_first_message)  # type: ignore[arg-type]

# Apply patches
_utils.postprocess_response = _patched_postprocess_response  # type: ignore[attr-defined]
_handlers.process_and_send_response = _patched_process_and_send_response  # type: ignore[attr-defined]

_logger.info("message_limit_patch: utils.postprocess_response successfully patched.")
_logger.info("message_limit_patch: handlers.process_and_send_response successfully patched.")
