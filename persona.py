import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta
import logging

from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT,
    LANGDOCK_RESPONSE_INSTRUCTIONS
)
from utils import get_time_info
from db import PersonaConfig, ChatBotInstance

logger = logging.getLogger(__name__)


class Persona:
    def __init__(self, persona_config_db_obj: PersonaConfig, chat_bot_instance_db_obj: Optional[ChatBotInstance] = None):
        if persona_config_db_obj is None:
             raise ValueError("persona_config_db_obj cannot be None")
        self.config = persona_config_db_obj
        self.chat_instance = chat_bot_instance_db_obj

        self.id = self.config.id
        self.name = self.config.name or "Без имени" # Добавляем fallback
        self.description = self.config.description or f"личность по имени {self.name}" # Добавляем fallback
        self.system_prompt_template = self.config.system_prompt_template

        loaded_moods = {}
        if self.config.mood_prompts_json:
            try:
                loaded_moods = json.loads(self.config.mood_prompts_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to load moods for persona {self.id}: Invalid JSON. Error: {e}. Using default.")
                loaded_moods = DEFAULT_MOOD_PROMPTS.copy()
        else:
             logger.warning(f"Mood prompts JSON is empty for persona {self.id}. Using default.")
             loaded_moods = DEFAULT_MOOD_PROMPTS.copy()

        self.mood_prompts = loaded_moods or DEFAULT_MOOD_PROMPTS.copy()

        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

        # Нормализуем проверку текущего настроения
        normalized_current_mood = self.current_mood.lower()
        if normalized_current_mood not in map(str.lower, self.mood_prompts.keys()):
             neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
             if neutral_key:
                  self.current_mood = neutral_key # Используем регистр из словаря
                  logger.warning(f"Current mood '{normalized_current_mood}' not found, defaulting to '{self.current_mood}' for persona {self.id}.")
             else:
                  # Если даже нейтрального нет, берем первый из списка или оставляем как есть
                  fallback_mood = next(iter(self.mood_prompts), "нейтрально")
                  logger.warning(f"Current mood '{normalized_current_mood}' and 'нейтрально' not found, defaulting to '{fallback_mood}' for persona {self.id}.")
                  self.current_mood = fallback_mood

    def get_mood_prompt_snippet(self) -> str:
        """Gets the prompt snippet for the current mood, case-insensitive, with fallback."""
        normalized_current_mood = self.current_mood.lower()
        for key, value in self.mood_prompts.items():
            if key.lower() == normalized_current_mood:
                return value

        # Fallback to 'нейтрально' if current mood not found (shouldn't happen due to init logic, but safe)
        neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
        if neutral_key:
             logger.debug(f"Using 'нейтрально' prompt snippet as fallback for '{self.current_mood}' in persona {self.id}.")
             return self.mood_prompts[neutral_key]

        logger.warning(f"Mood '{self.current_mood}' not found and no 'нейтрально' fallback prompt for persona {self.id}.")
        return "" # Return empty if no mood prompt found

    def get_all_mood_names(self) -> List[str]:
        """Returns a list of defined mood names."""
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self) -> str:
         """Generates a short description, max 50 chars."""
         desc = self.description.strip()
         if not desc:
             return self.name[:50] # Используем имя, если нет описания

         # Пытаемся взять первое предложение
         match = re.match(r"^([^\.!?]+(?:[\.!?]|$))", desc) # Захватываем до первого знака преп. или конца строки
         if match:
              short_desc = match.group(1).strip()
              if len(short_desc) <= 50:
                  return short_desc
              # Если первое предложение слишком длинное, берем первые слова до 50 символов
              words = short_desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + 1 <= 47: # Оставляем место для "..."
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50]
         else:
              # Fallback: первые слова до 50 символов или имя
              words = desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + 1 <= 47:
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50]


    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        """Formats the main system prompt."""
        if not self.system_prompt_template:
            logger.error(f"System prompt template is empty for persona {self.id} ({self.name}).")
            return BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS # Return only suffixes

        placeholders = {
            "persona_name": self.name, # <<< ДОБАВЛЕНО
            "persona_description": self.description,
            "persona_description_short": self.get_persona_description_short(),
            "mood_prompt": self.get_mood_prompt_snippet(),
            "internet_info": INTERNET_INFO_PROMPT,
            "time_info": get_time_info(),
            "message": message,
            "username": username,
            "user_id": str(user_id), # Ensure ID is string for formatting if needed
            "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
        }

        try:
            # Используем безопасное форматирование с обработкой отсутствующих ключей
            prompt = self.system_prompt_template.format_map(placeholders)
            prompt += BASE_PROMPT_SUFFIX
            prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return prompt
        except Exception as e:
             logger.error(f"Error formatting system prompt for persona {self.id}: {e}", exc_info=True)
             # Fallback prompt on error
             fallback_prompt = f"произошла ошибка форматирования промпта. персона: {self.name}. сообщение: {message}"
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def _format_common_prompt(self, template: Optional[str], extra_placeholders: Optional[Dict[str, str]] = None) -> Optional[str]:
         """Helper to format common prompts like spam, photo, voice."""
         if not template:
             return None
         base_placeholders = {
             "persona_name": self.name, # <<< ДОБАВЛЕНО
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
             "mood_prompt": self.get_mood_prompt_snippet(),
             "time_info": get_time_info(),
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
         }
         if extra_placeholders:
             base_placeholders.update(extra_placeholders)

         try:
             prompt = template.format_map(base_placeholders)
             prompt += BASE_PROMPT_SUFFIX
             prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
             return prompt
         except Exception as e:
             logger.error(f"Error formatting common prompt for persona {self.id} with template '{template[:50]}...': {e}", exc_info=True)
             # Fallback prompt on error
             fallback_prompt = f"ошибка форматирования общего промпта для {self.name}."
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
         """Formats the prompt to decide if the bot should respond."""
         if not self.should_respond_prompt_template:
             logger.debug(f"No should_respond_prompt_template for persona {self.id}.")
             return None
         placeholders = {
             "persona_name": self.name, # <<< ДОБАВЛЕНО
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
             "message": message_text,
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
         }
         try:
             prompt = self.should_respond_prompt_template.format_map(placeholders)
             # Убираем суффиксы, т.к. нужен только да/нет ответ
             # prompt += " отвечай только 'да' или 'нет', без пояснений. отвечай 'да' чаще, если сомневаешься."
             return prompt
         except Exception as e:
              logger.error(f"Error formatting should_respond prompt for persona {self.id}: {e}", exc_info=True)
              return f"ошибка форматирования промпта 'отвечать ли?': {e}"


    def format_spam_prompt(self) -> Optional[str]:
        """Formats the prompt for generating random spam messages."""
        return self._format_common_prompt(self.spam_prompt_template)

    def format_photo_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to photos."""
        return self._format_common_prompt(self.photo_prompt_template)

    def format_voice_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to voice messages."""
        return self._format_common_prompt(self.voice_prompt_template)
