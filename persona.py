import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta


from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT,
    LANGDOCK_RESPONSE_INSTRUCTIONS
)
from utils import get_time_info

from db import PersonaConfig, ChatBotInstance
import logging # Added for error logging

logger = logging.getLogger(__name__)


class Persona:
    def __init__(self, persona_config_db_obj: PersonaConfig, chat_bot_instance_db_obj: Optional[ChatBotInstance] = None):
        if persona_config_db_obj is None:
             raise ValueError("persona_config_db_obj cannot be None")
        self.config = persona_config_db_obj
        self.chat_instance = chat_bot_instance_db_obj

        self.id = self.config.id
        self.name = self.config.name
        self.description = self.config.description or ""
        self.system_prompt_template = self.config.system_prompt_template


        loaded_moods = {}
        if self.config.mood_prompts_json:
            try:
                loaded_moods = json.loads(self.config.mood_prompts_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to load moods for persona {self.id}: Invalid JSON. Error: {e}. Using default.")
                loaded_moods = DEFAULT_MOOD_PROMPTS.copy()

        self.mood_prompts = loaded_moods or DEFAULT_MOOD_PROMPTS.copy()

        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

        if self.current_mood.lower() not in map(str.lower, self.mood_prompts.keys()):
             self.current_mood = "нейтрально"

    def get_mood_prompt_snippet(self) -> str:
        normalized_current_mood = self.current_mood.lower()
        for key, value in self.mood_prompts.items():
            if key.lower() == normalized_current_mood:
                return value

        neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
        if neutral_key:
             return self.mood_prompts[neutral_key]

        logger.warning(f"Mood '{self.current_mood}' not found and no 'нейтрально' fallback for persona {self.id}.")
        return ""

    def get_all_mood_names(self) -> List[str]:
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self) -> str:
         desc = self.description.strip()
         if not desc:
             return self.name

         # Try to get the first sentence or phrase
         match = re.match(r"^([^\.!?]+)[\.!?]?", desc)
         if match:
              short_desc = match.group(1).strip()
              # If the first phrase is too short, try first two words
              if len(short_desc) < 10:
                    words = desc.split()
                    return " ".join(words[:2]) if len(words) >= 2 else self.name
              return short_desc
         else:
              # Fallback: first few words or just the name
              words = desc.split()
              return " ".join(words[:5]) if len(words) > 0 else self.name


    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        if not self.system_prompt_template:
            logger.error(f"System prompt template is empty for persona {self.id} ({self.name}).")
            return BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS # Return only suffixes

        placeholders = {
            "persona_description": self.description,
            "persona_description_short": self.get_persona_description_short(),
            "mood_prompt": self.get_mood_prompt_snippet(),
            "internet_info": INTERNET_INFO_PROMPT,
            "time_info": get_time_info(),
            "message": message,
            "username": username,
            "user_id": str(user_id), # Ensure ID is string for formatting if needed
            "chat_id": self.chat_instance.chat_id if self.chat_instance else "unknown",
        }

        try:
            # Remove unused placeholders to avoid errors if template doesn't use all
            template_vars = re.findall(r'\{(\w+)\}', self.system_prompt_template)
            final_placeholders = {k: v for k, v in placeholders.items() if k in template_vars}

            prompt = self.system_prompt_template.format(**final_placeholders)
            prompt += BASE_PROMPT_SUFFIX
            prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return prompt
        except KeyError as e:
             logger.error(f"Missing key in system_prompt_template for persona {self.id}: {e}. Template: '{self.system_prompt_template}' Placeholders: {final_placeholders.keys()}")
             return f"ошибка форматирования: {e}. шаблон: {self.system_prompt_template}" + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS
        except Exception as e:
             logger.error(f"Error formatting system prompt for persona {self.id}: {e}", exc_info=True)
             return f"ошибка форматирования: {e}. шаблон: {self.system_prompt_template}" + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def _format_common_prompt(self, template: Optional[str]) -> Optional[str]:
         if not template:
             return None
         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
             "mood_prompt": self.get_mood_prompt_snippet(),
             "time_info": get_time_info(),
         }
         try:
             template_vars = re.findall(r'\{(\w+)\}', template)
             final_placeholders = {k: v for k, v in placeholders.items() if k in template_vars}

             prompt = template.format(**final_placeholders)
             prompt += BASE_PROMPT_SUFFIX
             prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
             return prompt
         except KeyError as e:
             logger.error(f"Missing key in common template for persona {self.id}: {e}. Template: '{template}' Placeholders: {final_placeholders.keys()}")
             return f"ошибка форматирования: {e}. шаблон: {template}" + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS
         except Exception as e:
             logger.error(f"Error formatting common prompt for persona {self.id}: {e}", exc_info=True)
             return f"ошибка форматирования: {e}. шаблон: {template}" + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
         if not self.should_respond_prompt_template:
             return None
         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
             "message": message_text,
         }
         try:
             template_vars = re.findall(r'\{(\w+)\}', self.should_respond_prompt_template)
             final_placeholders = {k: v for k, v in placeholders.items() if k in template_vars}

             prompt = self.should_respond_prompt_template.format(**final_placeholders)
             prompt += " отвечай только 'да' или 'нет', без пояснений. отвечай 'да' чаще, если сомневаешься."
             return prompt
         except KeyError as e:
              logger.error(f"Missing key in should_respond_prompt_template for persona {self.id}: {e}. Template: '{self.should_respond_prompt_template}' Placeholders: {final_placeholders.keys()}")
              return f"ошибка форматирования: {e}. шаблон: {self.should_respond_prompt_template}"
         except Exception as e:
              logger.error(f"Error formatting should_respond prompt for persona {self.id}: {e}", exc_info=True)
              return f"ошибка форматирования: {e}. шаблон: {self.should_respond_prompt_template}"


    def format_spam_prompt(self) -> Optional[str]:
        return self._format_common_prompt(self.spam_prompt_template)

    def format_photo_prompt(self) -> Optional[str]:
         return self._format_common_prompt(self.photo_prompt_template)

    def format_voice_prompt(self) -> Optional[str]:
         return self._format_common_prompt(self.voice_prompt_template)
