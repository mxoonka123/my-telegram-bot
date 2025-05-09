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

# Patch show_edit_wizard_menu to display qualitative max-messages labels
_orig_show_wizard = _h._show_edit_wizard_menu  # type: ignore[attr-defined]

async def _patched_show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config):  # type: ignore[override]
    # First call original to build menu
    result_state = await _orig_show_wizard(update, context, persona_config)

    try:
        # Determine desired label according to persona setting
        val = persona_config.max_response_messages
        if val == 1:
            label = "Поменьше"
        elif val == 3:
            label = "Стандарт"
        elif val == 6:
            label = "Побольше"
        else:
            label = "Случайно"

        # Fetch last sent wizard menu message
        msg = update.callback_query.message if update.callback_query else update.effective_message
        if msg:
            km = msg.reply_markup
            if km and km.inline_keyboard:
                new_kb = []
                for row in km.inline_keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == "edit_wizard_max_msgs":
                            # Replace text keeping prefix
                            prefix = "🗨️ " if btn.text.startswith("🗨️") else ""
                            # Показываем короткий текст в основном меню
                            new_text = f"{prefix}Макс. сообщ. ({label})"
                            new_row.append(InlineKeyboardButton(new_text, callback_data=btn.callback_data))
                        else:
                            new_row.append(btn)
                    new_kb.append(new_row)
                await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb))
    except Exception as e:
        _logger.warning("settings_patch: failed to patch wizard menu display: %s", e)

    return result_state

# Apply monkey patches
_h.edit_max_messages_prompt = _patched_edit_max_messages_prompt  # type: ignore[attr-defined]
_h.edit_max_messages_received = _patched_edit_max_messages_received  # type: ignore[attr-defined]
_h._show_edit_wizard_menu = _patched_show_edit_wizard_menu  # type: ignore[attr-defined]
_logger.info("settings_patch: patched max message settings UI and handler.")
