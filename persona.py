import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta
import logging
import urllib.parse

# Убедимся, что импортируем нужные вещи
from config import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, LANGDOCK_RESPONSE_INSTRUCTIONS
)
# Шаблон DEFAULT_SYSTEM_PROMPT_TEMPLATE теперь берется из DB, но нужен для fallback
from db import PersonaConfig, ChatBotInstance, User, DEFAULT_SYSTEM_PROMPT_TEMPLATE

from utils import get_time_info

logger = logging.getLogger(__name__)

class Persona:
    def __init__(self, persona_config_db_obj: PersonaConfig, chat_bot_instance_db_obj: Optional[ChatBotInstance] = None):
        if persona_config_db_obj is None:
             raise ValueError("persona_config_db_obj cannot be None")
        self.config = persona_config_db_obj
        self.chat_instance = chat_bot_instance_db_obj # Can be None if used outside chat context

        self.id = self.config.id
        self.name = self.config.name or "Без имени"
        self.description = self.config.description or f"личность по имени {self.name}"

        # Load structured settings from DB object
        self.communication_style = self.config.communication_style or "neutral"
        self.verbosity_level = self.config.verbosity_level or "medium"
        self.group_reply_preference = self.config.group_reply_preference or "mentioned_or_contextual"
        self.media_reaction = self.config.media_reaction or "text_only"
        self.max_response_messages = self.config.max_response_messages or 3

        # Load moods safely
        loaded_moods = {}
        if self.config.mood_prompts_json:
            try:
                loaded_moods = json.loads(self.config.mood_prompts_json)
            except json.JSONDecodeError:
                logger.warning(f"Invalid moods JSON for persona {self.id}. Using default.")
                loaded_moods = DEFAULT_MOOD_PROMPTS.copy()
        else:
             logger.warning(f"Moods JSON empty for persona {self.id}. Using default.")
             loaded_moods = DEFAULT_MOOD_PROMPTS.copy()
        self.mood_prompts = loaded_moods or DEFAULT_MOOD_PROMPTS.copy()

        # Determine current mood safely
        self.current_mood = "нейтрально" # Default
        if self.chat_instance and self.chat_instance.current_mood:
            self.current_mood = self.chat_instance.current_mood

        # Validate current mood against loaded moods
        normalized_current_mood = self.current_mood.lower()
        if not any(key.lower() == normalized_current_mood for key in self.mood_prompts):
             neutral_key = next((k for k in self.mood_prompts if k.lower() == "нейтрально"), None)
             if neutral_key:
                  self.current_mood = neutral_key # Set to the actual key 'нейтрально'
                  logger.warning(f"Current mood '{normalized_current_mood}' not found for persona {self.id}, defaulting to '{self.current_mood}'.")
             else: # If even neutral doesn't exist (unlikely with defaults)
                  fallback_mood = next(iter(self.mood_prompts), "нейтрально")
                  logger.warning(f"Current mood '{normalized_current_mood}' and 'нейтрально' not found, defaulting to '{fallback_mood}' for persona {self.id}.")
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

        logger.warning(f"No prompt found for mood '{self.current_mood}' or 'нейтрально' for persona {self.id}.")
        return "" # Return empty string if no prompt found

    def get_all_mood_names(self) -> List[str]:
        """Returns a list of defined mood names."""
        return list(self.mood_prompts.keys())

    def get_persona_description_short(self, max_len: int = 50) -> str:
         """Generates a short description (first sentence or truncated)."""
         desc = self.description.strip()
         if not desc: return self.name[:max_len]

         # Try first sentence
         match = re.match(r"^([^\.!?]+(?:[\.!?]|$))", desc)
         short_desc = match.group(1).strip() if match else desc

         if len(short_desc) <= max_len: return short_desc

         # Truncate if too long
         words = short_desc.split()
         current_short = ""
         for word in words:
             if len(current_short) + len(word) + (1 if current_short else 0) <= max_len - 3: # space for "..."
                 current_short += (" " if current_short else "") + word
             else: break
         return (current_short + "...") if current_short else self.name[:max_len]

    # --- Prompt Generation based on settings ---

    def _generate_base_instructions(self) -> List[str]:
        """Generates common instruction parts based on style/verbosity."""
        instructions = []
        # Style
        style_map = {
            "neutral": "общайся спокойно, нейтрально.",
            "friendly": "общайся дружелюбно, позитивно.",
            "sarcastic": "общайся с сарказмом, немного язвительно.",
            "formal": "общайся формально, вежливо, избегай сленга.",
            "brief": "отвечай кратко и по делу.",
        }
        style_instruction = style_map.get(self.communication_style, style_map["neutral"])
        if style_instruction: instructions.append(style_instruction)

        # Verbosity
        verbosity_map = {
            "concise": "старайся быть лаконичным.",
            "medium": "отвечай со средней подробностью.",
            "talkative": "будь разговорчивым, можешь добавлять детали.",
        }
        verbosity_instruction = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])
        if verbosity_instruction: instructions.append(verbosity_instruction)

        return instructions

    def _get_system_template(self) -> str:
        """Returns the system prompt template (currently from config)."""
        # Could potentially load from self.config.system_prompt_template if needed
        return DEFAULT_SYSTEM_PROMPT_TEMPLATE

    def format_system_prompt(self, user_id: int, username: str, message: str) -> str:
        """Formats the main system prompt using template and dynamic info."""
        template = self._get_system_template() # Получаем актуальный шаблон
        mood_instruction = self.get_mood_prompt_snippet()
        mood_name = self.current_mood

        style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
        verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
        style_text = style_map.get(self.communication_style, style_map["neutral"])
        verbosity_text = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])

        chat_id_info = str(self.chat_instance.chat_id) if self.chat_instance else "unknown_chat"

        # --- Блок try...except для форматирования ---
        try:
            # Словарь с плейсхолдерами для шаблона V9
            placeholders = {
                'persona_name': self.name,
                'persona_description': self.description,
                'communication_style': style_text,
                'verbosity_level': verbosity_text,
                'mood_name': mood_name,
                'mood_prompt': mood_instruction,
                'username': username,
                'user_id': user_id,
                'chat_id': chat_id_info,
                'last_user_message': message
            }
            # Форматируем шаблон, используя словарь
            formatted_prompt = template.format(**placeholders)
            logger.debug(f"Formatting system prompt V9 with keys: {list(placeholders.keys())}")

        except KeyError as e:
            # Этот блок выполняется, если в шаблоне есть ключ, которого нет в placeholders
            logger.error(f"FATAL: Missing key in system prompt template V9: {e}. Template sample: {template[:100]}...", exc_info=True)
            # Fallback на простой формат БЕЗ ШАБЛОНА
            fallback_parts = [
                f"Ты {self.name}. {self.description}.",
                f"Стиль: {style_text}. Разговорчивость: {verbosity_text}.",
                f"Настроение: {mood_name} ({mood_instruction}).",
                f"Ответь на сообщение от {username} (id: {user_id}) в чате {chat_id_info}: {message}"
            ]
            formatted_prompt = " ".join(fallback_parts)
            logger.warning("Using fallback system prompt due to template error.")

        except Exception as format_err:
            # Этот блок выполняется при других ошибках форматирования
            logger.error(f"FATAL: Unexpected error formatting system prompt V9: {format_err}. Template sample: {template[:100]}...", exc_info=True)
            # Fallback на простой формат БЕЗ ШАБЛОНА
            fallback_parts = [
                f"Ты {self.name}. {self.description}.",
                f"Стиль: {style_text}. Разговорчивость: {verbosity_text}.",
                f"Настроение: {mood_name} ({mood_instruction}).",
                f"Ответь на сообщение от {username} (id: {user_id}) в чате {chat_id_info}: {message}"
            ]
            formatted_prompt = " ".join(fallback_parts)
            logger.warning("Using fallback system prompt due to unexpected formatting error.")
        # --- Конец блока try...except ---

        # Если нужно добавить общие инструкции *после* форматирования шаблона
        # formatted_prompt += " " + BASE_PROMPT_SUFFIX # Пример

        return formatted_prompt.strip()

    def format_should_respond_prompt(self, message_text: str, bot_username: str, history: List[Dict[str, str]]) -> Optional[str]:
        """Formats the prompt to decide if the bot should respond in a group based on context."""
        if self.group_reply_preference != "mentioned_or_contextual":
            # Этот метод вызывается только для contextual
            logger.error("format_should_respond_prompt called for non-contextual preference.")
            return None

        # Получаем шаблон из объекта конфига PersonaConfig
        template = self.should_respond_prompt_template
        if not template:
            logger.warning(f"should_respond_prompt_template is empty for persona {self.id}. Cannot generate contextual check prompt. Using default.")
            template = DEFAULT_SHOULD_RESPOND_TEMPLATE # Используем дефолтный из db.py как fallback

        # --- Создание краткого саммари истории ---
        history_limit = 5
        relevant_history = history[-history_limit:]
        context_lines = []
        for msg in relevant_history:
            role = "Ты" if msg.get("role") == "assistant" else "User"
            content_preview = str(msg.get("content", ""))[:80]
            if len(str(msg.get("content", ""))) > 80: content_preview += "..."
            context_lines.append(f"{role}: {content_preview}")
        context_summary = "\n".join(context_lines) if context_lines else "Нет истории."
        # --- Конец саммари ---

        # Подставляем значения в шаблон V5 из db.py
        # Плейсхолдеры: {persona_name}, {bot_username}, {last_user_message}, {context_summary}
        try:
            formatted_prompt = template.format(
                persona_name=self.name,
                bot_username=bot_username,
                last_user_message=message_text,
                context_summary=context_summary
            )
            logger.debug(f"Generated should_respond prompt for persona {self.id}:\n---\n{formatted_prompt}\n---")
            return formatted_prompt
        except KeyError as e:
            logger.error(f"Missing key in should_respond prompt template: {e}. Template: {template[:100]}...")
            return None
        except Exception as e:
             logger.error(f"Error formatting should_respond prompt: {e}", exc_info=True)
             return None

    def _format_media_prompt(self, media_type_text: str) -> Optional[str]:
         """Helper to format prompts for photo/voice reactions."""
         # Check if reaction is enabled for this media type
         react_setting = self.media_reaction
         if react_setting == "none": return None
         if react_setting == "photo_only" and media_type_text != "фото": return None
         if react_setting == "voice_only" and media_type_text != "голосовое сообщение": return None
         # "text_only" and "all" allow reaction

         base_instructions = self._generate_base_instructions()
         mood_instruction = self.get_mood_prompt_snippet()
         chat_id_info = str(self.chat_instance.chat_id) if self.chat_instance else "unknown"

         prompt_parts = [
             f"ты {self.get_persona_description_short()} ({self.name}).",
             f"тебе прислали {media_type_text} в чате {chat_id_info}.",
         ]
         if mood_instruction:
             prompt_parts.append(f"твое текущее настроение: {mood_instruction}.")
         if media_type_text == "фото":
              prompt_parts.append("кратко опиши, что видишь, и добавь комментарий от своего лица.")
         else: # Голосовое
              prompt_parts.append("представь, что прослушал его. кратко прокомментируй от своего лица.")
         prompt_parts.extend(base_instructions) # Add style/verbosity
         prompt_parts.append(get_time_info())

         # Combine and add suffixes
         formatted_prompt = " ".join(prompt_parts)
         formatted_prompt += BASE_PROMPT_SUFFIX
         formatted_prompt += LANGDOCK_RESPONSE_INSTRUCTIONS

         return formatted_prompt

    def format_photo_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to photos."""
        return self._format_media_prompt("фото")

    def format_voice_prompt(self) -> Optional[str]:
        """Formats the prompt for responding to voice messages."""
        return self._format_media_prompt("голосовое сообщение")

    # format_spam_prompt is removed as it wasn't used and placeholders are internal now
