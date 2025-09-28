# -*- coding: utf-8 -*-
"""
Оптимизированные утилиты для парсинга и обработки ответов
"""
import json
import re
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

def parse_llm_response_optimized(text: str) -> List[str]:
    """
    ОПТИМИЗИРОВАННЫЙ парсинг ответа от LLM.
    Быстрый и эффективный, без множественных fallback.
    """
    if not text:
        return []
    
    # Быстрая очистка от markdown обертки
    text = text.strip()
    if text.startswith("```"):
        # Убираем markdown блок
        lines = text.split('\n')
        if lines[0].startswith("```"):
            lines = lines[1:]  # Убираем первую строку
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # Убираем последнюю строку
        text = '\n'.join(lines)
        
        # Убираем "json" если есть в начале
        if text.startswith("json"):
            text = text[4:].lstrip()
    
    # Пробуем распарсить как JSON
    try:
        parsed = json.loads(text)
        
        # Обработка словаря
        if isinstance(parsed, dict):
            # Приоритет: ключ "response"
            if "response" in parsed:
                resp = parsed["response"]
                if isinstance(resp, list):
                    return [str(x) for x in resp if x]
                if resp:
                    return [str(resp)]
            
            # Альтернативные ключи
            for key in ['answer', 'text', 'messages', 'parts']:
                if key in parsed:
                    val = parsed[key]
                    if isinstance(val, list):
                        return [str(x) for x in val if x]
                    if val:
                        return [str(val)]
            
            # Если ничего не нашли, возвращаем весь текст
            return [text]
        
        # Обработка списка
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
            
    except json.JSONDecodeError:
        # JSON не распарсился - простой fallback
        pass
    
    # Финальный fallback: разбиваем по строкам
    lines = text.strip().split('\n')
    return [line.strip() for line in lines if line.strip()]


def extract_json_from_markdown_optimized(text: str) -> str:
    """Быстрое извлечение JSON из markdown блока"""
    if not text:
        return text
        
    # Паттерн для поиска ```json ... ```
    pattern = r'```(?:json)?\s*\n?(.*?)```'
    match = re.search(pattern, text, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    
    return text
