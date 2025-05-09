"""
Исправление функциональности настроек настроений в меню персоны
"""
from __future__ import annotations
import logging
import urllib.parse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import handlers as _h
from db import PersonaConfig

_logger = logging.getLogger(__name__)

# Добавляем недостающую функцию для генерации клавиатуры настроений
async def _get_edit_moods_keyboard_internal(persona_config: PersonaConfig):
    """Создает клавиатуру для меню редактирования настроений."""
    keyboard = []
    
    # Получаем настроения для этой персоны
    moods = persona_config.moods if persona_config and hasattr(persona_config, 'moods') else {}
    
    # Добавляем кнопки для каждого настроения (для редактирования и удаления)
    for mood_name, mood_prompt in sorted(moods.items()):
        # URL-кодируем имя настроения для корректной передачи в callback_data
        encoded_name = urllib.parse.quote(mood_name)
        # Добавляем строку с кнопками для каждого настроения
        keyboard.append([
            InlineKeyboardButton(f"✏️ {mood_name}", callback_data=f"editmood_select_{encoded_name}"),
            InlineKeyboardButton(f"🗑️", callback_data=f"deletemood_confirm_{encoded_name}")
        ])
    
    # Кнопка для добавления нового настроения
    keyboard.append([InlineKeyboardButton("➕ Добавить настроение", callback_data="editmood_add")])
    
    # Кнопка возврата в основное меню настроек
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")])
    
    return keyboard

# Патчим функцию edit_moods_menu в handlers.py
original_edit_moods_menu = _h.edit_moods_menu

async def patched_edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config=None):
    """Модифицированная функция edit_moods_menu с использованием новой функции _get_edit_moods_keyboard_internal."""
    
    # Устанавливаем глобальную функцию
    _h._get_edit_moods_keyboard_internal = _get_edit_moods_keyboard_internal
    
    # Вызываем оригинальную функцию
    return await original_edit_moods_menu(update, context, persona_config)

# Применяем патч
_h.edit_moods_menu = patched_edit_moods_menu

_logger.info("moods_fix: Исправлена функциональность редактирования настроений.")
