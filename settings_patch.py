"""Patch UI for max message settings.
Removes outdated numeric grid and uses qualitative options.
"""
from __future__ import annotations
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from utils import escape_markdown_v2
from db import PersonaConfig, get_db
import handlers as _h

_logger = logging.getLogger(__name__)

# Re-use existing state constant
EDIT_MAX_MESSAGES = _h.EDIT_MAX_MESSAGES  # type: ignore[attr-defined]

_display_map = {
    "few": "🤏 Поменьше сообщений",
    "normal": "💬 Стандартное количество",
    "many": "📚 Побольше сообщений",
    "random": "🎲 Случайное количество",
}

async def _patched_edit_max_messages_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: D401
    persona_id = context.user_data.get("edit_persona_id")
    current_value = "normal"
    if persona_id:
        with next(get_db()) as db:
            raw_val = db.query(PersonaConfig.max_response_messages).filter(PersonaConfig.id == persona_id).scalar()
            mapping = {1: "few", 3: "normal", 6: "many", 0: "random"}
            current_value = mapping.get(raw_val, "normal")
    prompt_text = escape_markdown_v2(
        f"🗨️ Выберите желаемое количество сообщений (текущее: {_display_map.get(current_value)})"
    )
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_value == 'few' else ''}{_display_map['few']}", callback_data="set_max_msgs_few")],
        [InlineKeyboardButton(f"{'✅ ' if current_value == 'normal' else ''}{_display_map['normal']}", callback_data="set_max_msgs_normal")],
        [InlineKeyboardButton(f"{'✅ ' if current_value == 'many' else ''}{_display_map['many']}", callback_data="set_max_msgs_many")],
        [InlineKeyboardButton(f"{'✅ ' if current_value == 'random' else ''}{_display_map['random']}", callback_data="set_max_msgs_random")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")],
    ]
    await _h._send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))  # type: ignore[attr-defined]
    return EDIT_MAX_MESSAGES

async def _patched_edit_max_messages_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: D401
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get("edit_persona_id")

    if data == "back_to_wizard_menu":
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _h._show_edit_wizard_menu(update, context, persona)  # type: ignore[attr-defined]

    if data.startswith("set_max_msgs_"):
        key = data.replace("set_max_msgs_", "")
        mapping_set = {
            "few": 1,
            "normal": 3,
            "many": 6,
            "random": 0,
        }
        if key not in mapping_set:
            await query.edit_message_text(escape_markdown_v2("❌ Некорректное значение."))
            return EDIT_MAX_MESSAGES
        new_val = mapping_set[key]
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            if persona:
                persona.max_response_messages = new_val
                db.commit()
        await query.edit_message_text(escape_markdown_v2(f"✅ Установлено: {_display_map[key]}"))
        with next(get_db()) as db:
            persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
            return await _h._show_edit_wizard_menu(update, context, persona)  # type: ignore[attr-defined]
    return EDIT_MAX_MESSAGES

# Apply monkey patches
_h.edit_max_messages_prompt = _patched_edit_max_messages_prompt  # type: ignore[attr-defined]
_h.edit_max_messages_received = _patched_edit_max_messages_received  # type: ignore[attr-defined]
_logger.info("settings_patch: patched max message settings UI and handler.")
