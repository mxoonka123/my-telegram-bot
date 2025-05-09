"""
Комплексное исправление для экранирования Markdown в тексте сообщений бота
"""
from __future__ import annotations
import logging
import re
import handlers as _h
from telegram.constants import ParseMode
from utils import escape_markdown_v2

_logger = logging.getLogger(__name__)

def fix_all_prompt_strings():
    """Исправляет все строки с подсказками для корректного экранирования в Markdown V2"""
    
    # Список всех строк в модуле handlers, которые могут содержать форматирование Markdown
    # и при этом использоваться с ParseMode.MARKDOWN_V2
    prompts_to_fix = [
        'prompt_new_name',
        'prompt_new_prompt_fmt_raw',
        'prompt_confirm_delete_fmt_raw',
        'error_no_session',
        'error_not_found',
        'error_decode_mood',
        'error_unhandled_choice',
        'error_db',
        'error_validation',
        'error_name_exists_fmt_raw',
        'prompt_for_prompt_fmt_raw'
    ]
    
    # Исправляем каждую строку, используя чистое переопределение вместо escape_markdown_v2 внутри кода
    # так как там могут быть уже частично экранированные символы
    _h.prompt_new_name = "введи название нового настроения \\(1\\-30 символов, буквы/цифры/дефис/подчерк\\.\\, без пробелов\\):"
    
    _h.prompt_new_prompt_fmt_raw = "отлично\\! теперь отправь текст промпта для настроения '{name}':"
    
    _h.prompt_confirm_delete_fmt_raw = "⚠️ вы действительно хотите удалить настроение '{name}'?"
    
    _h.error_no_session = "❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\."
    
    _h.error_not_found = "❌ ошибка: личность не найдена\\."
    
    _h.error_decode_mood = "❌ ошибка декодирования названия настроения\\. попробуйте еще раз\\."
    
    _h.error_unhandled_choice = "❌ неизвестный выбор\\. попробуйте еще раз\\."
    
    _h.error_db = "❌ ошибка базы данных при проверке имени\\."
    
    _h.error_validation = "❌ название: 1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\. попробуй еще:"
    
    _h.error_name_exists_fmt_raw = "❌ настроение '{name}' уже существует\\. выбери другое:"
    
    _h.error_general = "❌ непредвиденная ошибка\\."
    
    _h.prompt_for_prompt_fmt_raw = "отлично\\! теперь отправь текст промпта для настроения '{name}':"
    
    _logger.info("markdown_safe_fix: Исправлены строки с экранированием для Markdown V2")

    # Обертка для send_message с проверкой ошибок Markdown
    original_send_prompt = _h._send_prompt
    
    async def _safe_send_prompt(update, context, text, reply_markup=None):
        """Обертка для _send_prompt с дополнительной защитой от ошибок Markdown"""
        try:
            return await original_send_prompt(update, context, text, reply_markup)
        except Exception as e:
            if 'parse entities' in str(e).lower():
                _logger.error(f"Markdown parse error in _send_prompt: {e}. Text: {text[:100]}")
                # Попытка послать сообщение без Markdown форматирования
                chat_id = update.effective_chat.id
                try:
                    new_message = await context.bot.send_message(
                        chat_id, 
                        text.replace('\\', ''), 
                        reply_markup=reply_markup,
                        parse_mode=None
                    )
                    _h.message_tracker[chat_id] = new_message.message_id
                    return new_message
                except Exception as fallback_error:
                    _logger.error(f"Failed to send fallback message: {fallback_error}")
                    return None
            else:
                raise
    
    # Патчим функцию отправки сообщений
    _h._send_prompt = _safe_send_prompt
    
    # Исправление функции edit_moods_menu
    original_edit_moods_menu = _h.edit_moods_menu
    
    async def patched_edit_moods_menu(update, context, persona_config=None):
        """Безопасная версия edit_moods_menu с исправленным форматированием сообщений"""
        try:
            return await original_edit_moods_menu(update, context, persona_config)
        except Exception as e:
            _logger.error(f"Error in edit_moods_menu: {e}", exc_info=True)
            if update.effective_chat:
                try:
                    await update.effective_chat.send_message(
                        "❌ Произошла ошибка при отображении меню настроений. Возвращаемся в главное меню...",
                        parse_mode=None
                    )
                except:
                    pass
                # Возврат в главное меню редактирования волшебника
                if persona_config and hasattr(_h, '_show_edit_wizard_menu'):
                    return await _h._show_edit_wizard_menu(update, context, persona_config)
            return _h.ConversationHandler.END
    
    # Замена оригинальной функции на патч
    _h.edit_moods_menu = patched_edit_moods_menu

# Применяем исправления
fix_all_prompt_strings()
