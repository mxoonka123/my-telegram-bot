"""
Комплексные улучшения меню и интерфейса бота:
- Структура меню настроек персоны
- Функциональность настроений
- Исправление навигации по меню
"""
from __future__ import annotations
import logging
import urllib.parse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from db import PersonaConfig, get_db
import handlers as _h

_logger = logging.getLogger(__name__)

#
# --- УЛУЧШЕНИЕ СТРУКТУРЫ МЕНЮ ---
#

def apply_menu_structure_fixes():
    """Улучшение структуры меню настроек персоны"""
    
    # Оригинальные функции меню
    _original_edit_max_messages_prompt = _h.edit_max_messages_prompt
    _original_show_edit_wizard_menu = _h._show_edit_wizard_menu

    # Исправленная функция отображения основного меню настройки персоны
    async def fixed_show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: PersonaConfig) -> int:
        """Отображает главное меню настройки без кнопок выбора количества сообщений."""
        # Получаем исходные данные
        query = update.callback_query
        message = update.effective_message if not query else query.message
        chat_id = message.chat.id
        persona_id = persona_config.id
        user_id = update.effective_user.id

        # Определяем подпись для кнопки количества сообщений
        val = persona_config.max_response_messages
        if val == 1:
            label = "Поменьше"
        elif val == 2:
            label = "Стандартное"
        elif val == 3:
            label = "Побольше"
        elif val == 0:
            label = "Случайное"
        else:
            label = f"{val}"

        # Определяем значения для других настроек
        vol_value = "Короткие" if persona_config.message_volume == "short" else "Длинные" if persona_config.message_volume == "long" else "Стандарт"
        style_value = persona_config.communication_style or "Нейтральный"
        verbosity_value = "Немногословный" if persona_config.verbosity == "succinct" else "Многословный" if persona_config.verbosity == "verbose" else "Стандарт"
        group_value = "Всегда" if persona_config.group_chat_mode else "Только на @" if persona_config.group_chat_mode_at_mention else "Никогда"
        media_value = "Только текст" if persona_config.media_only_text else "Текст+картинка" if persona_config.media_reaction else "Стандарт"

        # Создаем кнопки меню
        keyboard = [
            [
                InlineKeyboardButton("✏️ Имя", callback_data="edit_wizard_name"),
                InlineKeyboardButton("📜 Описание", callback_data="edit_wizard_description")
            ],
            [InlineKeyboardButton(f"💬 Стиль ({style_value})", callback_data="edit_wizard_comm_style")],
            [InlineKeyboardButton(f"🗣️ Многословный ({verbosity_value})", callback_data="edit_wizard_verbosity")],
            [InlineKeyboardButton(f"👥 Ответы в группе ({group_value})", callback_data="edit_wizard_group_mode")],
            [InlineKeyboardButton(f"📷 Реакция на медиа ({media_value})", callback_data="edit_wizard_media_reaction")],
            [InlineKeyboardButton(f"🔢 Макс. сообщ. ({label})", callback_data="edit_max_messages")],
            [InlineKeyboardButton(f"📏 Объем сообщений ({vol_value})", callback_data="edit_message_volume")],
            [InlineKeyboardButton("🎭 Настроения", callback_data="edit_wizard_moods")],
            [InlineKeyboardButton("✅ Завершить", callback_data="edit_wizard_done")]
        ]

        # Отправляем или редактируем сообщение с меню
        markup = InlineKeyboardMarkup(keyboard)
        
        # Заголовок сообщения с информацией о персоне
        text = f"⚙️ Настройка личности: {persona_config.name} (ID: {persona_id})\n\nВыберите, что изменить:"
        
        try:
            # Пытаемся отредактировать существующее сообщение
            if query:
                await query.edit_message_text(text, reply_markup=markup)
            else:
                # Если нет query, отправляем новое сообщение
                await context.bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:
            _logger.warning(f"Не удалось отредактировать сообщение: {e}. Отправляем новое сообщение.")
            await context.bot.send_message(chat_id, text, reply_markup=markup)
        
        return _h.EDIT_WIZARD_CHOICE
    
    # Применяем исправление для меню
    _h._show_edit_wizard_menu = fixed_show_edit_wizard_menu
    
    _logger.info("menu_fix: Исправлена структура меню настроек персоны.")

#
# --- ИСПРАВЛЕНИЕ ФУНКЦИОНАЛЬНОСТИ НАСТРОЕНИЙ ---
#

def apply_moods_fixes():
    """Исправление функциональности настроек настроений в меню персоны"""
    
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

    # Добавляем функцию в handlers
    _h._get_edit_moods_keyboard_internal = _get_edit_moods_keyboard_internal
    
    # Патчим функцию edit_moods_menu в handlers.py
    original_edit_moods_menu = _h.edit_moods_menu
    
    async def patched_edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config=None):
        """Модифицированная функция edit_moods_menu с использованием новой функции _get_edit_moods_keyboard_internal."""
        
        # Вызываем оригинальную функцию
        return await original_edit_moods_menu(update, context, persona_config)
    
    # Применяем патч
    _h.edit_moods_menu = patched_edit_moods_menu
    
    _logger.info("moods_fix: Исправлена функциональность редактирования настроений.")

#
# --- ИСПРАВЛЕНИЕ НАВИГАЦИИ ПО МЕНЮ ---
#

def apply_menu_navigation_fixes():
    """Исправление проблем с навигацией по меню (дублирование элементов)"""
    
    # Заменяем функцию _show_edit_wizard_menu для сброса состояния при возврате из подменю
    original_show_edit_wizard_menu = _h._show_edit_wizard_menu
    
    async def patched_show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config=None):
        """
        Модифицированная версия функции показа меню редактирования персоны с очисткой контекста
        для предотвращения дублирования элементов при возврате из подменю
        """
        # Очищаем все временные состояния, связанные с подменю
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        context.user_data.pop('temp_submenu_state', None)
        
        # Вызываем оригинальную функцию отображения меню
        try:
            return await original_show_edit_wizard_menu(update, context, persona_config)
        except Exception as e:
            _logger.error(f"Error in patched_show_edit_wizard_menu: {e}", exc_info=True)
            # Если происходит ошибка, пробуем отправить сообщение о проблеме
            if update.effective_chat:
                try:
                    await update.effective_chat.send_message(
                        "❌ Произошла ошибка при отображении меню. Попробуйте вернуться в главное меню (/menu).",
                        parse_mode=None
                    )
                except Exception as msg_error:
                    _logger.error(f"Failed to send error message: {msg_error}")
            
            # Очищаем полностью данные пользователя и завершаем разговор
            context.user_data.clear()
            return ConversationHandler.END
    
    # Применяем патч
    _h._show_edit_wizard_menu = patched_show_edit_wizard_menu
    
    # Создаем обертку для _try_return_to_mood_menu
    original_try_return_to_mood_menu = _h._try_return_to_mood_menu
    
    async def patched_try_return_to_mood_menu(update, context, user_id, persona_id):
        """Обертка для _try_return_to_mood_menu с очисткой состояния для предотвращения проблем с меню"""
        # Очищаем все временные состояния
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        context.user_data.pop('temp_submenu_state', None)
        
        return await original_try_return_to_mood_menu(update, context, user_id, persona_id)
    
    # Применяем патч
    _h._try_return_to_mood_menu = patched_try_return_to_mood_menu
    
    _logger.info("menu_navigation_fix: Исправлены проблемы с навигацией в меню (избегаем дублирования пунктов)")

#
# --- ПРИМЕНЕНИЕ ВСЕХ УЛУЧШЕНИЙ МЕНЮ ---
#

def apply_all_menu_improvements():
    """Применяет все улучшения интерфейса и меню"""
    apply_menu_structure_fixes()
    apply_moods_fixes()
    apply_menu_navigation_fixes()
    _logger.info("menu_improvements: Все улучшения меню успешно применены")

# Автоматическое применение всех улучшений при импорте модуля
apply_all_menu_improvements()
