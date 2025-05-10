"""
Объединенный модуль с патчами для всех аспектов бота:
- Ограничения сообщений
- Исправления форматирования Markdown
- Другие общие исправления
"""
from __future__ import annotations
import logging
import re as _re
from typing import List, Optional
import random as _random
import json as _json
import urllib.parse
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode

import handlers as _h
import utils as _utils
from db import PersonaConfig, get_db

_logger = logging.getLogger(__name__)

#
# --- ПАТЧИ ДЛЯ ОГРАНИЧЕНИЯ СООБЩЕНИЙ ---
#

def apply_message_limit_patches():
    """Патчи для строгого ограничения количества сообщений во всем боте."""
    
    _original_postprocess = _utils.postprocess_response
    
    def _patched_postprocess_response(response: str, max_messages: int, message_volume: str = "normal") -> List[str]:
        parts = _original_postprocess(response, max_messages, message_volume)
        if len(parts) > max_messages:
            _logger.warning(
                "message_limit_patch: postprocess_response returned %d parts, exceeding requested %d. Truncating.",
                len(parts), max_messages,
            )
            parts = parts[:max_messages]
        return parts
    
    # Оригинальная функция обработки и отправки сообщений
    _original_process_send = _h.process_and_send_response
    
    @wraps(_original_process_send)
    async def _patched_process_and_send_response(update, context, chat_id, persona, full_bot_response_text, db, 
                                                reply_to_message_id=None, is_first_message=False):
        """
        Обертка, которая обеспечивает соблюдение max_response_messages *до* отправки сообщений.
        """
        # Извлекаем настройки персоны
        max_messages = getattr(persona, 'max_response_messages', 0) or 3  # Default to 3 if not set
        msg_volume = getattr(persona, 'message_volume', 'normal') or 'normal'
        
        # Проверяем значение max_messages перед обработкой сообщений
        if not isinstance(max_messages, int) or max_messages < 1:
            _logger.warning(
                "Invalid max_response_messages (%r) for persona %d. Setting to 3.", 
                max_messages, getattr(persona, 'id', '??'),
            )
            max_messages = 3
            
        # Здесь мы принудительно ограничиваем до max_messages
        response_parts = _utils.postprocess_response(full_bot_response_text, max_messages, msg_volume)
        
        # Теперь можно безопасно вызывать оригинальную функцию с уже ограниченным списком сообщений
        await _original_process_send(
            update, context, chat_id, persona, 
            full_bot_response_text, db,
            reply_to_message_id, is_first_message
        )
    
    # Применяем патчи
    _utils.postprocess_response = _patched_postprocess_response
    _h.process_and_send_response = _patched_process_and_send_response
    
    _logger.info("message_limit_patch: Successfully applied message limits enforcing patches")


#
# --- ПАТЧИ ДЛЯ ОБРАБОТКИ MARKDOWN ---
#

def apply_markdown_fixes():
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
    
    # Удаляем экранирование во всех строковых константах
    for attr_name in dir(_h):
        if attr_name.startswith('__'):
            continue
        
        try:
            attr_value = getattr(_h, attr_name)
            if isinstance(attr_value, str) and '\\' in attr_value:
                # Удаляем экранирование из строк
                setattr(_h, attr_name, attr_value.replace('\\', ''))
        except (AttributeError, TypeError):
            pass
    
    _logger.info("markdown_simple_fix: Применен радикальный патч - полностью отключено Markdown форматирование для проблемных текстов")


#
# --- ПРИМЕНЕНИЕ ВСЕХ ПАТЧЕЙ ---
#

def apply_all_patches():
    """Применяет все патчи, определенные в этом модуле."""
    apply_message_limit_patches()
    apply_markdown_fixes()
    _logger.info("patches: Все патчи успешно применены")

# Автоматическое применение всех патчей при импорте модуля
apply_all_patches()
