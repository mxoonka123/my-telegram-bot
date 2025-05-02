# --- START OF FILE persona.py ---
import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta
import logging

from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT,
    LANGDOCK_RESPONSE_INSTRUCTIONS, DEFAULT_SYSTEM_PROMPT_TEMPLATE
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

        # Загружаем новые структурированные настройки
        self.communication_style = self.config.communication_style or "neutral"
        self.verbosity_level = self.config.verbosity_level or "medium"
        self.group_reply_preference = self.config.group_reply_preference or "mentioned_or_contextual"
        self.media_reaction = self.config.media_reaction or "text_only"

        # Настройки, которые остаются
        self.max_response_messages = self.config.max_response_messages or 3

        # Загрузка настроений
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

        # Текущее настроение
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

         # Try to get the first sentence
         match = re.match(r"^([^\.!?]+(?:[\.!?]|$))", desc)
         if match:
              short_desc = match.group(1).strip()
              # If first sentence is short enough, use it
              if len(short_desc) <= 50:
                  return short_desc
              # Otherwise, truncate the first sentence by words
              words = short_desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + (1 if current_short else 0) <= 47: # Leave space for "..."
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50] # Fallback to name if even first word is too long
         else:
              # If no sentence structure found, truncate the whole description by words
              words = desc.split()
              current_short = ""
              for word in words:
                   if len(current_short) + len(word) + (1 if current_short else 0) <= 47:
                       current_short += (" " if current_short else "") + word
                   else:
                       break
              return current_short + "..." if current_short else self.name[:50] # Fallback to name

    # --- Новые методы для генерации промптов ---

    def _generate_base_instructions(self) -> List[str]:
        """Generates common instruction parts based on settings."""
        instructions = []
        # Communication Style
        if self.communication_style == "friendly":
            instructions.append("общайся дружелюбно, позитивно, можешь использовать смайлики.")
        elif self.communication_style == "sarcastic":
            instructions.append("общайся с сарказмом, немного язвительно, но не переходи на прямые оскорбления.")
        elif self.communication_style == "brief":
            instructions.append("отвечай кратко и по делу.")
        elif self.communication_style == "formal":
            instructions.append("общайся формально, вежливо, избегай сленга.")
        # else: neutral - no specific instruction needed

        # Verbosity Level
        if self.verbosity_level == "concise":
            instructions.append("старайся быть лаконичным.")
        elif self.verbosity_level == "talkative":
            instructions.append("будь разговорчивым, можешь добавлять детали и рассуждения.")
        # else: medium - no specific instruction needed

        return instructions

    def _generate_system_prompt(self) -> str:
        """Generates the main system prompt based on structured settings."""
        base_instructions = self._generate_base_instructions()
        mood_instruction = self.get_mood_prompt_snippet()

        # Собираем основную часть промпта
        prompt_parts = [
            f"описание твоей личности: {self.description}.",
            f"твое имя: {self.name}.",
        ]
        if mood_instruction:
            prompt_parts.append(f"твое текущее настроение: {mood_instruction}.")
        if base_instructions:
            prompt_parts.extend(base_instructions)

        # Используем базовый шаблон из config для добавления динамической части
        # (message, username, user_id, chat_id, time_info, internet_info)
        # Эти плейсхолдеры останутся в шаблоне и будут заполнены в format_system_prompt
        system_core = " ".join(prompt_parts)
        final_template = f"{system_core} {{internet_info}} {{time_info}} сообщение от {{username}} (id: {{user_id}}) в чате {{chat_id}}: {{message}}"

        return final_template

    def _generate_should_respond_prompt(self) -> str:
        """Generates the 'should respond' prompt based on group reply preference."""
        # Базовая инструкция
        base = f"ты — {self.get_persona_description_short()} ({self.name}). ты обычный участник чата {{chat_id}}. отвечай только 'да' или 'нет'."

        # Добавляем условия в зависимости от настройки
        conditions = []
        if self.group_reply_preference == "always":
            # Если всегда отвечать, промпт не нужен, но для консистентности вернем "да"
            # Хотя лучше эту логику обработать в handlers.py и не вызывать LLM
            return "ответь 'да'." # Этот промпт заставит AI почти всегда отвечать да
        elif self.group_reply_preference == "mentioned_only":
            conditions.append("сообщение адресовано тебе лично (упомянуто твое имя)")
        elif self.group_reply_preference == "mentioned_or_contextual":
            conditions.append("сообщение адресовано тебе лично (упомянуто твое имя)")
            conditions.append("сообщение связано с тобой или твоей ролью")
            conditions.append("ты можешь добавить что-то важное или интересное в разговор")
        elif self.group_reply_preference == "never":
             # Если никогда не отвечать, промпт не нужен, но вернем "нет"
             # Лучше обработать в handlers.py
             return "ответь 'нет'."

        if conditions:
            condition_str = " или ".join(conditions)
            prompt = f"{base} если {condition_str} — ответь 'да'. иначе — ответь 'нет'. если сомневаешься, лучше ответь 'да'. сообщение: {{message}}"
        else:
            # Fallback, если настройка некорректна - отвечаем по контексту
            prompt = f"{base} если сообщение адресовано тебе, касается твоей роли ({self.description}), твоих интересов, или ты считаешь важным ответить — напиши 'да'. если сообщение тебя не касается или не требует ответа — напиши 'нет'. если сомневаешься, лучше ответь 'да'. сообщение: {{message}}"

        return prompt

    def _generate_photo_prompt(self) -> Optional[str]:
        """Generates the photo reaction prompt."""
        if self.media_reaction in ["none", "voice_only"]:
            return None # Не реагируем на фото

        base_instructions = self._generate_base_instructions()
        prompt_parts = [
            f"ты {self.get_persona_description_short()} ({self.name}).",
            f"тебе прислали фото в чате {{chat_id}}.",
            "кратко опиши, что видишь на фото, и добавь комментарий от своего лица, согласно твоей роли и стилю общения.",
        ]
        prompt_parts.extend(base_instructions)
        prompt_parts.append("сейчас {time_info}.")
        return " ".join(prompt_parts)

    def _generate_voice_prompt(self) -> Optional[str]:
        """Generates the voice reaction prompt."""
        if self.media_reaction in ["none", "photo_only"]:
            return None # Не реагируем на голос

        base_instructions = self._generate_base_instructions()
        prompt_parts = [
            f"ты {self.get_persona_description_short()} ({self.name}).",
            f"тебе прислали голосовое сообщение в чате {{chat_id}}.",
            "представь, что прослушал его. кратко прокомментируй от своего лица, согласно твоей роли и стилю общения.",
        ]
        prompt_parts.extend(base_instructions)
        prompt_parts.append("сейчас {time_info}.")
        return " ".join(prompt_parts)

    # --- Обновленные методы форматирования ---

    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        """Formats the main system prompt using generated template and dynamic info."""
        # Генерируем шаблон на основе настроек
        generated_template = self._generate_system_prompt()

        # Динамические плейсхолдеры для заполнения шаблона
        dynamic_placeholders = {
            "internet_info": INTERNET_INFO_PROMPT,
            "time_info": get_time_info(),
            "message": message,
            "username": username,
            "user_id": str(user_id),
            "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
        }

        try:
            # Заполняем динамические части сгенерированного шаблона
            formatted_prompt = generated_template.format_map(dynamic_placeholders)
            # Добавляем общие суффиксы
            formatted_prompt += BASE_PROMPT_SUFFIX
            formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return formatted_prompt
        except Exception as e:
             logger.error(f"Error formatting generated system prompt for persona {self.id}: {e}", exc_info=True)
             fallback_prompt = f"произошла ошибка форматирования промпта. персона: {self.name}. сообщение: {message}"
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS

    def format_should_respond_prompt(self, message_text: str) -> Optional[str]:
        """Formats the prompt to decide if the bot should respond."""
        # Генерируем шаблон на основе настроек
        generated_template = self._generate_should_respond_prompt()
        if not generated_template: # Если генерация вернула None (например, для always/never)
            return None

        # Динамические плейсхолдеры
        dynamic_placeholders = {
            "message": message_text,
            "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
        }
        try:
            # Заполняем динамические части
            formatted_prompt = generated_template.format_map(dynamic_placeholders)
            # Суффиксы здесь НЕ добавляются
            return formatted_prompt
        except Exception as e:
              logger.error(f"Error formatting generated should_respond prompt for persona {self.id}: {e}", exc_info=True)
              return f"ошибка форматирования промпта 'отвечать ли?': {e}"

    def _format_common_generated_prompt(self, generator_method) -> Optional[str]:
         """Helper to format common generated prompts (photo, voice) by adding dynamic info."""
         generated_template = generator_method()
         if not generated_template:
             return None

         dynamic_placeholders = {
             "time_info": get_time_info(),
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
         }

         try:
             formatted_prompt = generated_template.format_map(dynamic_placeholders)
             # Добавляем суффиксы
             formatted_prompt += BASE_PROMPT_SUFFIX
             formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
             return formatted_prompt
         except Exception as e:
             logger.error(f"Error formatting generated common prompt for persona {self.id}: {e}", exc_info=True)
             fallback_prompt = f"ошибка форматирования общего промпта для {self.name}."
             return fallback_prompt + BASE_PROMPT_SUFFIX + LANGDOCK_RESPONSE_INSTRUCTIONS

    def format_photo_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to photos."""
        return self._format_common_generated_prompt(self._generate_photo_prompt)

    def format_voice_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to voice messages."""
        return self._format_common_generated_prompt(self._generate_voice_prompt)

    # Метод format_spam_prompt можно убрать или переделать, если спам не нужен или генерируется иначе
    def format_spam_prompt(self) -> Optional[str]:
        """Formats the prompt for generating random spam messages (placeholder)."""
        # TODO: Implement spam generation based on new settings or remove
        logger.warning("Spam prompt generation is not fully implemented with new settings.")
        # Пока вернем простой вариант
        base_instructions = self._generate_base_instructions()
        prompt_parts = [
            f"ты {self.get_persona_description_short()} ({self.name}).",
            "напиши короткую случайную фразу от своего лица, не обращаясь ни к кому.",
        ]
        prompt_parts.extend(base_instructions)
        prompt_parts.append("сейчас {time_info}.")
        generated_template = " ".join(prompt_parts)

        dynamic_placeholders = {
             "time_info": get_time_info(),
             "chat_id": str(self.chat_instance.chat_id) if self.chat_instance else "unknown",
         }
        try:
            formatted_prompt = generated_template.format_map(dynamic_placeholders)
            formatted_prompt += BASE_PROMPT_SUFFIX
            formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS
            return formatted_prompt
        except Exception as e:
            logger.error(f"Error formatting generated spam prompt: {e}")
            return None

# --- END OF FILE persona.py ---
