"""
Упрощенное исправление для устранения проблем с Markdown
"""
from __future__ import annotations
import logging
import handlers as _h
from telegram.constants import ParseMode

_logger = logging.getLogger(__name__)

def apply_fixes():
    """Применяет радикальное исправление для проблем с Markdown - полностью отключает его"""
    
    # Оригинальная функция отправки текста
    original_send_prompt = _h._send_prompt
    
    async def _safe_send_prompt_without_markdown(update, context, text, reply_markup=None):
        """
        Безопасная версия функции отправки сообщений, которая отключает Markdown форматирование
        и просто отправляет текст как есть, избегая всех проблем с парсингом
        """
        try:
            _logger.info(f"Отправка сообщения без Markdown форматирования: {text[:50]}...")
            chat_id = update.effective_chat.id
            
            # Отправка сообщения БЕЗ Markdown форматирования
            new_message = await context.bot.send_message(
                chat_id, 
                # Удаляем все экранирующие символы
                text.replace('\\', ''),
                reply_markup=reply_markup,
                parse_mode=None  # Отключаем форматирование полностью
            )
            return new_message
            
        except Exception as e:
            _logger.error(f"Ошибка при отправке сообщения без Markdown: {e}")
            try:
                # Крайняя мера - отправить сообщение с минимальным текстом
                chat_id = update.effective_chat.id
                await context.bot.send_message(
                    chat_id,
                    "❌ Ошибка отображения сообщения. Попробуйте вернуться в главное меню (/menu).",
                    reply_markup=reply_markup,
                    parse_mode=None
                )
            except Exception as fallback_err:
                _logger.error(f"Критическая ошибка отправки сообщения: {fallback_err}")
            return None
    
    # Заменяем оригинальную функцию
    _h._send_prompt = _safe_send_prompt_without_markdown
    
    # Патч для edit_mood_name_received
    original_edit_mood_name_received = _h.edit_mood_name_received
    
    async def patched_edit_mood_name_received(update, context):
        """Исправленная версия функции edit_mood_name_received с отключенным Markdown"""
        try:
            # Явно исправляем сообщения об ошибках, которые используются в этой функции
            # Удаляем все экранирующие символы и отключаем Markdown
            _h.error_validation = "❌ название: 1-30 символов, буквы/цифры/дефис/подчерк., без пробелов. попробуй еще:"
            _h.error_name_exists_fmt_raw = "❌ настроение '{name}' уже существует. выбери другое:"
            
            return await original_edit_mood_name_received(update, context)
            
        except Exception as e:
            _logger.error(f"Ошибка в patched_edit_mood_name_received: {e}", exc_info=True)
            # Сброс к меню настроений
            if update.effective_chat and hasattr(_h, 'edit_moods_menu'):
                try:
                    persona_id = context.user_data.get('edit_persona_id')
                    if persona_id:
                        with next(_h.get_db()) as db:
                            persona_config = db.query(_h.PersonaConfig).filter(
                                _h.PersonaConfig.id == persona_id
                            ).first()
                            if persona_config:
                                return await _h.edit_moods_menu(update, context, persona_config=persona_config)
                except Exception as db_err:
                    _logger.error(f"Ошибка доступа к БД в резервном обработчике: {db_err}")
            
            # В случае проблем отправляем сообщение об ошибке
            try:
                await update.effective_chat.send_message(
                    "❌ Произошла ошибка. Попробуйте вернуться в главное меню (/menu).",
                    parse_mode=None
                )
            except:
                pass
                
            return _h.ConversationHandler.END
    
    # Применяем патч
    _h.edit_mood_name_received = patched_edit_mood_name_received
    
    # Общий патч для всех функций reply_text с Markdown
    original_update_message_reply_text = _h.Update.Message.reply_text
    
    async def safe_reply_text(self, text, *args, **kwargs):
        """Безопасная версия reply_text, которая отключает Markdown если он используется"""
        # Если указан ParseMode.MARKDOWN_V2, заменяем его на None и очищаем экранирование
        if kwargs.get('parse_mode') == ParseMode.MARKDOWN_V2:
            kwargs['parse_mode'] = None
            text = text.replace('\\', '')
        
        return await original_update_message_reply_text(self, text, *args, **kwargs)
    
    # Применяем патч к методу reply_text
    _h.Update.Message.reply_text = safe_reply_text
    
    _logger.info("markdown_simple_fix: Применен радикальный патч - полностью отключено Markdown форматирование для проблемных текстов")

# Применяем исправления
apply_fixes()
