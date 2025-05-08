"""Monkeypatch to ensure message count limits are strictly enforced across the bot.

This module patches utils.postprocess_response to guarantee that the returned
list length never exceeds the requested max_messages. It should be imported
once at application start-up (e.g. from main.py).
"""
from __future__ import annotations

import logging
from typing import List

import utils as _utils

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

# Apply monkey-patch
_utils.postprocess_response = _patched_postprocess_response  # type: ignore[attr-defined]

_logger.info("message_limit_patch: utils.postprocess_response successfully patched.")
