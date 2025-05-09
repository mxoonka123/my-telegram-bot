"""
Исправление экранирования символов Markdown в подсказках меню настроений
"""
from __future__ import annotations
import logging
import handlers as _h
from utils import escape_markdown_v2

_logger = logging.getLogger(__name__)

# Исправляем текст подсказки для создания нового настроения,
# обеспечивая корректное экранирование символов для Markdown V2
def apply_fixes():
    # Поправляем экранирование в prompt_new_name
    # Убираем escape_markdown_v2, так как он не корректно обрабатывает уже экранированные символы
    _h.prompt_new_name = "введи название нового настроения \\(1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\):"
    
    # Правильно применяем escape_markdown_v2 к тексту без предварительного экранирования
    _h.prompt_new_name = escape_markdown_v2("введи название нового настроения (1-30 символов, буквы/цифры/дефис/подчерк., без пробелов):")
    
    _logger.info("moods_escape_fix: Исправлены проблемы с экранированием символов Markdown в подсказках настроений")

# Применяем исправления
apply_fixes()
