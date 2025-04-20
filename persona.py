# persona.py

import json
import re # <--- ДОБАВЬ ЭТУ СТРОКУ
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta


from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT,
    LANGDOCK_RESPONSE_INSTRUCTIONS
)
from utils import get_time_info

from db import PersonaConfig, ChatBotInstance


class Persona:
    def __init__(self, persona_config_db_obj: PersonaConfig, chat_bot_instance_db_obj: Optional[ChatBotInstance] = None):
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
            except json.JSONDecodeError:

                pass
        self.mood_prompts = loaded_moods or DEFAULT_MOOD_PROMPTS.copy()

        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

        if self.current_mood.lower() not in map(str.lower, self.mood_prompts.keys()):
             self.current_mood = "нейтрально"

    def get_mood_prompt_snippet(self) -> str:

        for key, value in self.mood_prompts.items():
            if key.lower() == self.current_mood.lower():
                return value
        return self.mood_prompts.get("нейтрально", "")

    def get_all_mood_names(self) -> List[str]:
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self) -> str:
         desc = self.description.strip()
         if not desc:
             return self.name


         match = re.match(r"^([^.,!?]+)", desc)
         if match:
              short_desc = match.group(1).strip()

              return short_desc if len(short_desc) > 10 else desc.split()[0] + " " + desc.split()[1] if len(desc.split())>1 else self.name
         else:
              return self.name


    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        placeholders = {
            "persona_description": self.description,
            "persona_description_short": self.get_persona_description_short(),
            "mood_prompt": self.get_mood_prompt_snippet(),
            "internet_info": INTERNET_INFO_PROMPT,
            "time_info": get_time_info(),
            "message": message,
            "username": username,
            "user_id": user_id,
            "chat_id": self.chat_instance.chat_id if self.chat_instance else "unknown",
        }

        try:
            prompt = self.system_prompt_template.format(**placeholders)
            prompt += BASE_PROMPT_SUFFIX
            prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return prompt
        except KeyError as e:

             print(f"Warning: Missing key in system_prompt_template: {e}")

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
             prompt = template.format(**placeholders)
             prompt += BASE_PROMPT_SUFFIX
             prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
             return prompt
         except KeyError as e:
             print(f"Warning: Missing key in template: {e}")
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
             prompt = self.should_respond_prompt_template.format(**placeholders)

             prompt += " отвечай только 'да' или 'нет', без пояснений. отвечай 'да' чаще, если сомневаешься."
             return prompt
         except KeyError as e:
              print(f"Warning: Missing key in should_respond_prompt_template: {e}")
              return f"ошибка форматирования: {e}. шаблон: {self.should_respond_prompt_template}"


    def format_spam_prompt(self) -> Optional[str]:
        prompt = self._format_common_prompt(self.spam_prompt_template)


        return prompt

    def format_photo_prompt(self) -> Optional[str]:
         return self._format_common_prompt(self.photo_prompt_template)

    def format_voice_prompt(self) -> Optional[str]:
         return self._format_common_prompt(self.voice_prompt_template)
