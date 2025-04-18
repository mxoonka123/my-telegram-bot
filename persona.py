import json
from typing import Dict, Any, List, Optional, Union, Tuple # <-- Убедись, что Tuple импортирован
from datetime import datetime, timezone, timedelta

from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT,
    LANGDOCK_RESPONSE_INSTRUCTIONS
)
from utils import get_time_info

class Persona:
    """Класс для удобной работы с конфигурацией персоны и чата."""
    def __init__(self, persona_config_db_obj, chat_bot_instance_db_obj):
        self.config = persona_config_db_obj
        self.chat_instance = chat_bot_instance_db_obj

        self.id = self.config.id
        self.name = self.config.name
        self.description = self.config.description
        self.system_prompt_template = self.config.system_prompt_template
        self.mood_prompts = json.loads(self.config.mood_prompts_json) if self.config.mood_prompts_json else DEFAULT_MOOD_PROMPTS

        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

    def get_mood_prompt_snippet(self) -> str:
        """Возвращает текстовый сниппет для текущего настроения."""
        return self.mood_prompts.get(self.current_mood.lower(), "")

    def get_all_mood_names(self) -> List[str]:
        """Возвращает список доступных названий настроений."""
        return list(self.mood_prompts.keys())

    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        """Форматирует полный системный промпт для Langdock API."""
        placeholders = {
            "persona_description": self.description,
            "persona_description_short": self.description.split(',')[0].strip() if self.description else self.name,
            "mood_prompt": self.get_mood_prompt_snippet(),
            "internet_info": INTERNET_INFO_PROMPT,
            "time_info": get_time_info(),
            "message": message,
            "username": username,
            "user_id": user_id,
            "chat_id": self.chat_instance.chat_id if self.chat_instance else "unknown",
        }

        prompt = self.system_prompt_template.format(**placeholders)
        prompt += BASE_PROMPT_SUFFIX
        prompt += LANGDOCK_RESPONSE_INSTRUCTIONS


        return prompt

    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
         if not self.should_respond_prompt_template:
             return None

         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.description.split(',')[0].strip() if self.description else self.name,
             "message": message_text,
         }
         prompt = self.should_respond_prompt_template.format(**placeholders)
         prompt += " отвечай только 'да' или 'нет', без пояснений. отвечай 'да' чаще, если сомневаешься."
         return prompt

    def format_spam_prompt(self) -> Optional[str]:
        if not self.spam_prompt_template:
            return None

        placeholders = {
            "persona_description": self.description,
            "persona_description_short": self.description.split(',')[0].strip() if self.description else self.name,
            "mood_prompt": self.get_mood_prompt_snippet(),
            "time_info": get_time_info(),
        }
        prompt = self.spam_prompt_template.format(**placeholders)
        prompt += " отвечай коротко. " + LANGDOCK_RESPONSE_INSTRUCTIONS

        return prompt

    def format_photo_prompt(self) -> Optional[str]:
         if not self.photo_prompt_template:
             return None
         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.description.split(',')[0].strip() if self.description else self.name,
             "mood_prompt": self.get_mood_prompt_snippet(),
             "time_info": get_time_info(),
         }
         prompt = self.photo_prompt_template.format(**placeholders)
         prompt += BASE_PROMPT_SUFFIX
         prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
         return prompt

    def format_voice_prompt(self) -> Optional[str]:
         if not self.voice_prompt_template:
             return None
         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.description.split(',')[0].strip() if self.description else self.name,
             "mood_prompt": self.get_mood_prompt_snippet(),
             "time_info": get_time_info(),
         }
         prompt = self.voice_prompt_template.format(**placeholders)
         prompt += BASE_PROMPT_SUFFIX
         prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
         return prompt