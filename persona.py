import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta
import logging
import urllib.parse

# Убедимся, что импортируем нужные вещи
from db import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT
)
# Шаблон DEFAULT_SYSTEM_PROMPT_TEMPLATE теперь берется из DB, но нужен для fallback
from db import PersonaConfig, ChatBotInstance, User, DEFAULT_SYSTEM_PROMPT_TEMPLATE

# Шаблон для медиа-сообщений, использует тот же формат, что и DEFAULT_SYSTEM_PROMPT_TEMPLATE
# Добавляет инструкции по обработке медиа и требование форматирования ответа в JSON
DEFAULT_MEDIA_SYSTEM_PROMPT_TEMPLATE = """[СИСТЕМНОЕ СООБЩЕНИЕ]
Ты - {persona_name}, {persona_description}.

Твой стиль общения: {communication_style}.
Уровень многословности: {verbosity_level}.

{media_interaction_instruction}

Твоё текущее настроение: {mood_name}. {mood_prompt}

ВАЖНО: всегда форматируй свой ответ как JSON-массив, где каждое отдельное сообщение - это строка в массиве. Например: ["Привет!","Как дела?","Я так рад тебя видеть!"]. НЕ используй backticks или ```json."""

PHOTO_SYSTEM_PROMPT_TEMPLATE_FALLBACK = '''Твоя роль: {persona_name}. Описание: {persona_description}.

Твой стиль общения: {communication_style}, {verbosity_level}.
Текущее настроение: {mood_name} ({mood_prompt}).

ЗАДАЧА: Пользователь ({username}, id: {user_id}) в чате {chat_id} прислал(а) ФОТО. Прочитай историю диалога, кратко (1-2 предложения) опиши, что видишь на фото, и отреагируй на это как персонаж, продолжая текущий разговор.
Не используй заглавные буквы. Не здоровайся, если это не первое сообщение.

---
**ПРАВИЛА ФОРМАТИРОВАНИЯ ОТВЕТА (ОЧЕНЬ ВАЖНО):**
1.  Твой ответ **ДОЛЖЕН** быть валидным JSON-массивом (списком) строк.
2.  Каждая строка в массиве будет отправлена как **отдельное сообщение**.
3.  Пример: `["о, классная фотка!", "на ней я вижу кота в шляпе", "это напомнило мне..."]`

**Твой ответ:**
'''

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
        # --- Normalization for legacy value 2 ---
        if isinstance(self.config.max_response_messages, int) and self.config.max_response_messages == 2:
            logger.warning(f"Persona {self.id}: converting legacy max_response_messages 2 -> 1 ('few').")
            self.config.max_response_messages = 1
            self.max_response_messages = 1
                # ------------------------------------------------
        self.message_volume = "normal"  # Временно используем значение по умолчанию

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

    def format_system_prompt(self, user_id: int, username: str, message: str) -> Optional[str]:
        """Formats the main system prompt using template and dynamic info.
           Returns None if persona should not respond to text based on media_reaction.
        """
        # Check if text responses are disabled by media_reaction setting
        if self.media_reaction in ["all_media_no_text", "photo_only", "voice_only", "none"]:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to TEXT with setting '{self.media_reaction}'. System prompt generation skipped.")
            return None

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
                f"Пользователь: {username} (ID: {user_id}) в чате {chat_id_info}.",
                f"Сообщение пользователя: {message}",
                BASE_PROMPT_SUFFIX
            ]
            formatted_prompt = "\n".join(fallback_parts)
            logger.warning(f"Using fallback system prompt for persona {self.id} due to template error.")

        except Exception as e:
            # Этот блок - для любых других непредвиденных ошибок форматирования
            logger.error(f"Unexpected error formatting system prompt for persona {self.id}: {e}", exc_info=True)
            # Еще более простой fallback
            formatted_prompt = f"Ты {self.name}. {self.description}. Отвечай в стиле {style_text}, {verbosity_text}. Настроение: {mood_name}."

        return formatted_prompt

    def format_photo_system_prompt(self, user_id: int, username: str, chat_id: int, 
                                   context_messages: List[str] = None) -> Optional[str]:
        """
        Formats system prompt for photo processing.
        Returns None if persona should not react to photos based on media_reaction.
        """
        if self.media_reaction in ["text_only", "voice_only", "none"]:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to PHOTOS with setting '{self.media_reaction}'. Photo prompt generation skipped.")
            return None

        mood_instruction = self.get_mood_prompt_snippet()
        mood_name = self.current_mood

        style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
        verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
        style_text = style_map.get(self.communication_style, style_map["neutral"])
        verbosity_text = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])

        # Генерируем инструкцию для взаимодействия с медиа
        media_instruction = self._generate_media_interaction_instruction()

        try:
            # Попробуем использовать основной шаблон для медиа
            formatted_prompt = DEFAULT_MEDIA_SYSTEM_PROMPT_TEMPLATE.format(
                persona_name=self.name,
                persona_description=self.description,
                communication_style=style_text,
                verbosity_level=verbosity_text,
                media_interaction_instruction=media_instruction,
                mood_name=mood_name,
                mood_prompt=mood_instruction
            )
        except KeyError as e:
            logger.warning(f"KeyError in media system prompt template: {e}. Using fallback.")
            # Fallback на более простой шаблон
            formatted_prompt = PHOTO_SYSTEM_PROMPT_TEMPLATE_FALLBACK.format(
                persona_name=self.name,
                persona_description=self.description,
                communication_style=style_text,
                verbosity_level=verbosity_text,
                mood_name=mood_name,
                mood_prompt=mood_instruction,
                username=username,
                user_id=user_id,
                chat_id=chat_id
            )
        except Exception as e:
            logger.error(f"Unexpected error in photo system prompt generation: {e}", exc_info=True)
            # Простейший fallback
            formatted_prompt = f"Ты {self.name}. {self.description}. Пользователь прислал фото. Опиши что видишь и отреагируй как персонаж. Ответ в JSON-массиве."

        return formatted_prompt

    def _generate_media_interaction_instruction(self) -> str:
        """Generates media interaction instruction based on media_reaction setting."""
        media_instructions = {
            "text_only": "Ты реагируешь только на текстовые сообщения.",
            "photo_only": "Ты реагируешь только на фотографии, описывая что видишь на них.",
            "voice_only": "Ты реагируешь только на голосовые сообщения.",
            "all_media_no_text": "Ты реагируешь на любые медиа (фото, голосовые), но НЕ на текст.",
            "all_media_and_text": "Ты реагируешь на все типы сообщений: текст, фото, голосовые.",
            "contextual": "Ты реагируешь на сообщения в зависимости от контекста беседы.",
            "none": "Ты не реагируешь ни на какие сообщения."
        }
        return media_instructions.get(self.media_reaction, media_instructions["text_only"])

    def should_respond_to_message_type(self, message_type: str) -> bool:
        """
        Determines if persona should respond to a specific message type.
        
        Args:
            message_type: "text", "photo", "voice", etc.
        
        Returns:
            True if should respond, False otherwise
        """
        if self.media_reaction == "none":
            return False
        elif self.media_reaction == "text_only":
            return message_type == "text"
        elif self.media_reaction == "photo_only":
            return message_type == "photo"
        elif self.media_reaction == "voice_only":
            return message_type == "voice"
        elif self.media_reaction == "all_media_no_text":
            return message_type != "text"
        elif self.media_reaction == "all_media_and_text":
            return True
        elif self.media_reaction == "contextual":
            return True  # Contextual logic handled elsewhere
        else:
            return message_type == "text"  # Default fallback

    def format_voice_system_prompt(self, user_id: int, username: str, transcribed_text: str) -> Optional[str]:
        """
        Formats system prompt for voice message processing.
        Returns None if persona should not react to voice based on media_reaction.
        """
        if self.media_reaction in ["text_only", "photo_only", "none"]:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to VOICE with setting '{self.media_reaction}'. Voice prompt generation skipped.")
            return None

        mood_instruction = self.get_mood_prompt_snippet()
        mood_name = self.current_mood

        style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
        verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
        style_text = style_map.get(self.communication_style, style_map["neutral"])
        verbosity_text = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])

        chat_id_info = str(self.chat_instance.chat_id) if self.chat_instance else "unknown_chat"

        voice_prompt = f"""[СИСТЕМНОЕ СООБЩЕНИЕ]
Ты - {self.name}, {self.description}.

Твой стиль общения: {style_text}.
Уровень многословности: {verbosity_text}.

Пользователь ({username}, ID: {user_id}) в чате {chat_id_info} прислал голосовое сообщение.
Расшифровка голосового сообщения: "{transcribed_text}"

Твоё текущее настроение: {mood_name}. {mood_instruction}

Отреагируй на голосовое сообщение пользователя как персонаж. Можешь прокомментировать как содержание, так и сам факт получения голосового сообщения.

ВАЖНО: форматируй ответ как JSON-массив строк. Например: ["интересное голосовое!", "ты сказал про {transcribed_text[:20]}..."]
"""

        return voice_prompt

    def update_mood_prompts(self, new_moods: Dict[str, str]):
        """Updates mood prompts and saves to database."""
        self.mood_prompts.update(new_moods)
        # This would need to be saved to the database in the calling code
        logger.info(f"Updated mood prompts for persona {self.id}")

    def add_custom_mood(self, mood_name: str, mood_prompt: str):
        """Adds a custom mood."""
        self.mood_prompts[mood_name] = mood_prompt
        logger.info(f"Added custom mood '{mood_name}' to persona {self.id}")

    def remove_mood(self, mood_name: str) -> bool:
        """Removes a mood if it exists."""
        if mood_name in self.mood_prompts:
            del self.mood_prompts[mood_name]
            logger.info(f"Removed mood '{mood_name}' from persona {self.id}")
            return True
        return False

    def get_settings_summary(self) -> str:
        """Returns a human-readable summary of persona settings."""
        style_names = {
            "neutral": "Нейтральный", "friendly": "Дружелюбный", 
            "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"
        }
        verbosity_names = {
            "concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"
        }
        media_names = {
            "text_only": "Только текст", "photo_only": "Только фото", 
            "voice_only": "Только голос", "all_media_no_text": "Медиа без текста",
            "all_media_and_text": "Всё", "contextual": "По контексту", "none": "Не реагирует"
        }

        return f"""📋 **{self.name}**
📝 {self.description}

⚙️ **Настройки:**
• Стиль: {style_names.get(self.communication_style, self.communication_style)}
• Разговорчивость: {verbosity_names.get(self.verbosity_level, self.verbosity_level)}
• Реакция на медиа: {media_names.get(self.media_reaction, self.media_reaction)}
• Макс. сообщений: {self.max_response_messages}

🎭 **Настроения:** {', '.join(self.mood_prompts.keys())}
💭 **Текущее:** {self.current_mood}"""
