"""
Исправление проблем при навигации по меню (дублирование пунктов при возврате из подменю)
"""
from __future__ import annotations
import logging
import handlers as _h
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode

_logger = logging.getLogger(__name__)

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

# Создаем обертку для _try_return_to_wizard_menu
original_try_return_to_wizard_menu = _h._try_return_to_wizard_menu

async def patched_try_return_to_wizard_menu(update, context, user_id, persona_id):
    """Обертка для _try_return_to_wizard_menu с очисткой состояния для предотвращения проблем с меню"""
    # Очищаем все временные состояния
    context.user_data.pop('edit_mood_name', None)
    context.user_data.pop('delete_mood_name', None)
    context.user_data.pop('temp_submenu_state', None)
    
    return await original_try_return_to_wizard_menu(update, context, user_id, persona_id)

# Применяем патч
_h._try_return_to_wizard_menu = patched_try_return_to_wizard_menu

_logger.info("menu_navigation_fix: Исправлены проблемы с навигацией в меню (избегаем дублирования пунктов)")
