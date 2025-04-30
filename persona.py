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
        self.name = self.config.name or "Без имени"
        self.description = self.config.description or f"личность по имени {self.name}"

        # Prompts are now pre-formatted with name/description during creation
        self.system_prompt_template = self.config.system_prompt_template
        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

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

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

        normalized_current_mood = self.current_mood.lower()
        if normalized_current_mood not in map(str.lower, self.mood_prompts.keys()):
             neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
             if neutral_key:
                  self.current_mood = neutral_key
                  logger.warning(f"Current mood '{self.current_mood}' not found for persona {self.id}, defaulting to '{neutral_key}'.")
             else:
                  fallback_mood = next(iter(self.mood_prompts), "нейтрально")
                  logger.warning(f"Current mood '{self.current_mood}' and 'нейтрально' not found, defaulting to '{fallback_mood}' for persona {self.id}.")
                  self.current_mood = fallback_mood

    def get_mood_prompt_snippet(self) -> str:
        """Gets the prompt snippet for the current mood, case-insensitive, with fallback."""
        normalized_current_mood = self.current_mood.lower()
        for key, value in self.mood_prompts.items():
            if key.lower() == normalized_current_mood:
                return value

        neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
        if neutral_key:
             logger.debug(f"Using 'нейтрально' prompt snippet as fallback for '{self.current_mood}' in persona {self.id}.")
             return self.mood_prompts[neutral_key]

        logger.warning(f"Mood '{self.current_mood}' not found and no 'нейтрально' fallback prompt for persona {self.id}.")
        return ""

    def get_all_mood_names(self) -> List[str]:
        """Returns a list of defined mood names."""
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self) -> str:
         """Generates a short description, max 50 chars."""
         desc = self.description.strip()
         if not desc:
             return self.name[:50]

         match = re.match(r"^([^\.!?]+(?:[\.!?]|$))", desc)
         if match:
              short_desc = match.group(1).strip()
              if len(short_desc) <= 50:
                  return short_desc
              words = short_desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + (1 if current_short else 0) <= 47: # Leave space for "..."
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50]
         else:
              words = desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + (1 if current_short else 0) <= 47:
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50]


    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        """Formats the main system prompt by adding dynamic info."""
        base_prompt = self.system_prompt_template
        if not base_prompt:
            logger.error(f"System prompt template is empty in DB for persona {self.id} ({self.name}).")
            # Fallback to a very basic prompt structure
            base_prompt = "{persona_description} {mood_prompt} {internet_info} {time_info} у тебя имя {persona_name}. Сообщение от {username} (id: {user_id}) в чате {chat_id}: {message}"

        # Placeholders to be filled *dynamically* each time
        dynamic_placeholders = {
            "persona_name": self.name, # Include static ones too in case template wasn't pre-filled correctly
            "persona_description": self.description,
            "persona_description_short": self.get_persona_description_short(),
            "mood_prompt": self.get_mood_prompt_snippet(),
            "internet_info": INTERNET_INFO_PROMPT, # Global constant
            "time_info": get_time_info(), # Dynamic
            "message": message, # Dynamic
            "username": username, # Dynamic
            "user_id": str(user_id), # Dynamic
            "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown", # Dynamic
        }

        try:
            # Use format_map which ignores extra keys and handles missing keys gracefully
            formatted_prompt = base_prompt.format_map(dynamic_placeholders)
            # Append suffixes *after* formatting
            formatted_prompt += BASE_PROMPT_SUFFIX
            formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return formatted_prompt
        except Exception as e:
             logger.error(f"Error formatting system prompt for persona {self.id}: {e}", exc_info=True)
             # Fallback prompt on error
             fallback_prompt = f"произошла ошибка форматирования промпта. персона: {self.name}. сообщение: {message}"
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def _format_common_prompt(self, template: Optional[str], extra_placeholders: Optional[Dict[str, str]] = None) -> Optional[str]:
         """Helper to format common prompts like spam, photo, voice by adding dynamic info."""
         if not template:
             return None

         base_dynamic_placeholders = {
             "persona_name": self.name, # Static, but include for robustness
             "persona_description": self.description, # Static
             "persona_description_short": self.get_persona_description_short(), # Static
             "mood_prompt": self.get_mood_prompt_snippet(), # Dynamic
             "time_info": get_time_info(), # Dynamic
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown", # Dynamic
         }
         if extra_placeholders:
             base_dynamic_placeholders.update(extra_placeholders)

         try:
             formatted_prompt = template.format_map(base_dynamic_placeholders)
             # Append suffixes *after* formatting
             formatted_prompt += BASE_PROMPT_SUFFIX
             formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
             return formatted_prompt
         except Exception as e:
             logger.error(f"Error formatting common prompt for persona {self.id} with template '{template[:50]}...': {e}", exc_info=True)
             fallback_prompt = f"ошибка форматирования общего промпта для {self.name}."
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS


    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
         """Formats the prompt to decide if the bot should respond."""
         base_prompt = self.should_respond_prompt_template
         if not base_prompt:
             logger.debug(f"No should_respond_prompt_template for persona {self.id}.")
             return None # Explicitly return None if no template

         # Dynamic placeholders needed for this prompt
         dynamic_placeholders = {
             "persona_name": self.name,
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
             "message": message_text, # The specific message being checked
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
         }
         try:
             # Format the pre-filled template with dynamic info
             formatted_prompt = base_prompt.format_map(dynamic_placeholders)
             # Suffixes are NOT added here as per original logic
             return formatted_prompt
         except Exception as e:
              logger.error(f"Error formatting should_respond prompt for persona {self.id}: {e}", exc_info=True)
              # Return a simple fallback on error
              return f"ошибка форматирования промпта 'отвечать ли?': {e}"


    def format_spam_prompt(self) -> Optional[str]:
        """Formats the prompt for generating random spam messages."""
        # Only needs dynamic time_info, handled by _format_common_prompt
        return self._format_common_prompt(self.spam_prompt_template)

    def format_photo_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to photos."""
        # Only needs dynamic time_info, handled by _format_common_prompt
        return self._format_common_prompt(self.photo_prompt_template)

    def format_voice_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to voice messages."""
        # Only needs dynamic time_info, handled by _format_common_prompt
        return self._format_common_prompt(self.voice_prompt_template)
