import json
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm.attributes import flag_modified

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
        try:
            self.mood_prompts = json.loads(self.config.mood_prompts_json) if self.config.mood_prompts_json else DEFAULT_MOOD_PROMPTS
        except json.JSONDecodeError:
             self.mood_prompts = DEFAULT_MOOD_PROMPTS


        self.should_respond_prompt_template = self.config.should_respond_prompt_template
        self.spam_prompt_template = self.config.spam_prompt_template
        self.photo_prompt_template = self.config.photo_prompt_template
        self.voice_prompt_template = self.config.voice_prompt_template

        self.current_mood = self.chat_instance.current_mood if self.chat_instance else "нейтрально"

    def get_mood_prompt_snippet(self) -> str:
        return self.mood_prompts.get(self.current_mood.lower(), "")

    def get_all_mood_names(self) -> List[str]:
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self) -> str:
         if self.description and ',' in self.description:
             return self.description.split(',')[0].strip()
         elif self.description:
             return self.description.split('.')[0].strip() # Fallback: first sentence
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

        prompt = self.system_prompt_template.format(**placeholders)
        prompt += BASE_PROMPT_SUFFIX
        prompt += LANGDOCK_RESPONSE_INSTRUCTIONS

        return prompt

    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
         if not self.should_respond_prompt_template:
             return None

         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
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
            "persona_description_short": self.get_persona_description_short(),
            "mood_prompt": self.get_mood_prompt_snippet(),
            "time_info": get_time_info(),
        }
        prompt = self.spam_prompt_template.format(**placeholders)
        prompt += " отвечай коротко. " + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS

        return prompt

    def format_photo_prompt(self) -> Optional[str]:
         if not self.photo_prompt_template:
             return None
         placeholders = {
             "persona_description": self.description,
             "persona_description_short": self.get_persona_description_short(),
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
             "persona_description_short": self.get_persona_description_short(),
             "mood_prompt": self.get_mood_prompt_snippet(),
             "time_info": get_time_info(),
         }
         prompt = self.voice_prompt_template.format(**placeholders)
         prompt += BASE_PROMPT_SUFFIX
         prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
         return prompt

    def update_mood_prompt(self, db_session, mood_name: str, mood_prompt: str):
         mood_name = mood_name.lower()
         self.mood_prompts[mood_name] = mood_prompt
         self.config.mood_prompts_json = json.dumps(self.mood_prompts)
         flag_modified(self.config, "mood_prompts_json")
         db_session.commit()


    def delete_mood_prompt(self, db_session, mood_name: str):
        mood_name = mood_name.lower()
        if mood_name in self.mood_prompts:
             del self.mood_prompts[mood_name]
             self.config.mood_prompts_json = json.dumps(self.mood_prompts)
             flag_modified(self.config, "mood_prompts_json")
             db_session.commit()


    def update_field(self, db_session, field_name: str, value: str):
        if hasattr(self.config, field_name):
            setattr(self.config, field_name, value)
            db_session.commit()
        else:
             raise AttributeError(f"PersonaConfig has no field named '{field_name}'")
