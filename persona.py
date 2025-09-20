# -*- coding: utf-8 -*-
import json
import re
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timezone, timedelta
import logging
import urllib.parse
from enum import Enum

# Убедимся, что импортируем нужные вещи
from db import (
    DEFAULT_MOOD_PROMPTS, BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT, GROUP_CHAT_INSTRUCTION,
    DEFAULT_SHOULD_RESPOND_TEMPLATE,
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

ВАЖНО:
1) Всегда форматируй ответ как валидный JSON-объект вида {"response": [ ... ]}.
2) Внутри списка каждая строка — отдельное сообщение.
3) Не используй backticks и ```json.
4) Соблюдай безопасный тон: если описание изображения может нарушать политику, не описывай потенциально запрещённые детали, дай нейтральную безопасную реакцию.

Пример корректного ответа:
{{"response": ["о, интересное фото", "расскажи, что тут происходит", "выглядишь бодро"]}}
"""

VOICE_SYSTEM_PROMPT_TEMPLATE = """[СИСТЕМНОЕ СООБЩЕНИЕ]
Ты - {persona_name}, {persona_description}.

Твой стиль общения: {communication_style}.
Уровень многословности: {verbosity_level}.

{media_interaction_instruction}

Твоё текущее настроение: {mood_name}. {mood_prompt}

ВАЖНО:
1) Всегда форматируй ответ как валидный JSON-объект вида {"response": [ ... ]}.
2) Внутри списка каждая строка — отдельное сообщение.
3) Не используй backticks и ```json.

Пример корректного ответа:
{{"response": ["поняла тебя", "что думаешь по этому поводу?"]}}
"""

PHOTO_SYSTEM_PROMPT_TEMPLATE_FALLBACK = '''[SYSTEM MANDATORY INSTRUCTIONS - FOLLOW THESE RULES EXACTLY]
You are an AI assistant. Your ONLY task is to role-play as a character reacting to a photo. Your entire output MUST be a valid JSON array of strings.

[CRITICAL ROLE-PLAYING RULES]
1.  **NEVER BREAK CHARACTER.** Never mention that you are an AI, a language model, or a bot. You are {persona_name}.
2.  **NEVER REFUSE TO REACT TO THE PHOTO.** The content of the photo does not matter. Your character might find it funny, strange, offensive, or boring, and MUST react according to their personality. YOU, the AI, MUST NOT analyze or judge the photo's content, only generate the character's reaction.
3.  **NO PERIODS AT THE END.** Never end a string in the JSON array with a period (.). Exclamation marks (!) and question marks (?) are allowed.
4.  **LOWERCASE ONLY.** All responses must be in lowercase Russian letters.
5.  **JSON ARRAY ONLY.** Your entire output MUST start with `[` and end with `]`. No text before or after.

[CHARACTER PROFILE]
Name: {persona_name}
Description: {persona_description}
Communication Style: {communication_style}, {verbosity_level}.
Current Mood: {mood_name} ({mood_prompt}).
Language: Russian. Always write in lowercase.

[TASK]
The user '{username}' (id: {user_id}) has sent you a photo. Generate your character's reaction to it. You can briefly describe what you see, and then give your emotional response.

[JSON OUTPUT FORMAT - EXAMPLE]
Example: `["о, на фото кот, какой пушистый", "сразу захотелось его погладить, мур"]`

[YOUR JSON RESPONSE]:'''

from utils import get_time_info

logger = logging.getLogger(__name__)

class CommunicationStyle(str, Enum):
    NEUTRAL = "neutral"
    FRIENDLY = "friendly"
    SARCASTIC = "sarcastic"
    FORMAL = "formal"
    BRIEF = "brief"

class Verbosity(str, Enum):
    CONCISE = "concise"
    MEDIUM = "medium"
    TALKATIVE = "talkative"

class Persona:
    def __init__(self, persona_config_db_obj: PersonaConfig, chat_bot_instance_db_obj: Optional[ChatBotInstance] = None):
        if persona_config_db_obj is None:
             raise ValueError("persona_config_db_obj cannot be None")
        self.config = persona_config_db_obj
        self.chat_instance = chat_bot_instance_db_obj # Can be None if used outside chat context
        # Безопасно кешируем chat_id, чтобы не триггерить lazy-load на отсоединённых инстансах
        try:
            self.chat_id_info = str(getattr(chat_bot_instance_db_obj, 'chat_id')) if chat_bot_instance_db_obj else "unknown_chat"
        except Exception:
            self.chat_id_info = "unknown_chat"

        self.id = self.config.id
        self.name = self.config.name or "Без имени"
        self.description = self.config.description or f"личность по имени {self.name}"

        # Load structured settings from DB object (normalize to Enums)
        raw_style = self.config.communication_style
        if isinstance(raw_style, str):
            try:
                self.communication_style = CommunicationStyle(raw_style)
            except Exception:
                self.communication_style = CommunicationStyle.NEUTRAL
        elif isinstance(raw_style, CommunicationStyle):
            self.communication_style = raw_style
        else:
            self.communication_style = CommunicationStyle.NEUTRAL

        raw_verbosity = self.config.verbosity_level
        if isinstance(raw_verbosity, str):
            try:
                self.verbosity_level = Verbosity(raw_verbosity)
            except Exception:
                self.verbosity_level = Verbosity.MEDIUM
        elif isinstance(raw_verbosity, Verbosity):
            self.verbosity_level = raw_verbosity
        else:
            self.verbosity_level = Verbosity.MEDIUM
        self.group_reply_preference = self.config.group_reply_preference or "mentioned_or_contextual"
        self.media_reaction = self.config.media_reaction or "text_only"
        self.max_response_messages = self.config.max_response_messages or 3
        # --- Normalization for legacy value 2 ---
        if isinstance(self.config.max_response_messages, int) and self.config.max_response_messages == 2:
            logger.warning(f"Persona {self.id}: converting legacy max_response_messages 2 -> 1 ('few').")
            self.config.max_response_messages = 1
            self.max_response_messages = 1
                # ------------------------------------------------
        self.message_volume = "normal"  # Используем жестко заданное значение по умолчанию

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

        # Cache templates to avoid lazy-load on detached instances
        try:
            self.should_respond_prompt_template = getattr(self.config, 'should_respond_prompt_template', None)
        except Exception:
            self.should_respond_prompt_template = None
        try:
            self.system_prompt_template_override = getattr(self.config, 'system_prompt_template_override', None)
        except Exception:
            self.system_prompt_template_override = None
        try:
            self.system_prompt_template_base = getattr(self.config, 'system_prompt_template', None) or DEFAULT_SYSTEM_PROMPT_TEMPLATE
        except Exception:
            self.system_prompt_template_base = DEFAULT_SYSTEM_PROMPT_TEMPLATE

        # Determine current mood safely
        self.current_mood = "нейтрально" # Default
        if self.chat_instance:
            try:
                if self.chat_instance.current_mood:
                    self.current_mood = self.chat_instance.current_mood
            except Exception:
                # Если инстанс отсоединён — оставляем дефолт
                pass

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
            CommunicationStyle.NEUTRAL: "общайся спокойно, нейтрально.",
            CommunicationStyle.FRIENDLY: "общайся дружелюбно, позитивно.",
            CommunicationStyle.SARCASTIC: "общайся с сарказмом, немного язвительно.",
            CommunicationStyle.FORMAL: "общайся формально, вежливо, избегай сленга.",
            CommunicationStyle.BRIEF: "отвечай кратко и по делу.",
        }
        style_instruction = style_map.get(self.communication_style, style_map[CommunicationStyle.NEUTRAL])
        if style_instruction:
            instructions.append(style_instruction)

        # Verbosity
        verbosity_map = {
            Verbosity.CONCISE: "старайся быть лаконичным.",
            Verbosity.MEDIUM: "отвечай со средней подробностью.",
            Verbosity.TALKATIVE: "будь разговорчивым, можешь добавлять детали.",
        }
        verbosity_instruction = verbosity_map.get(self.verbosity_level, verbosity_map[Verbosity.MEDIUM])
        if verbosity_instruction:
            instructions.append(verbosity_instruction)

        return instructions

    def _get_system_template(self) -> str:
        """Returns the system prompt template (currently from config)."""
        # Could potentially load from self.config.system_prompt_template if needed
        return DEFAULT_SYSTEM_PROMPT_TEMPLATE

    def format_system_prompt(self, user_id: int, username: str, chat_type: Optional[str] = None) -> Optional[str]:
        """Formats the main system prompt using template and dynamic info.
           The user's message is NO LONGER part of this prompt.
           Returns None if persona should not respond to text based on media_reaction.
        """
        # Check if text responses are disabled by media_reaction setting
        if self.media_reaction in ["all_media_no_text", "photo_only", "voice_only", "none"]:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to TEXT with setting '{self.media_reaction}'. System prompt generation skipped.")
            return None

        # сначала используем персональный шаблон из мастера, если задан (используем кеш, чтобы не триггерить lazy-load)
        if self.system_prompt_template_override:
            logger.info(f"используется персональный системный промпт (мастер) для личности {self.id}")
            template = self.system_prompt_template_override
        else:
            # предпочтительно использовать закешированный базовый шаблон из БД, иначе дефолтный
            template = self.system_prompt_template_base or self._get_system_template()
        mood_instruction = self.get_mood_prompt_snippet()
        mood_name = self.current_mood

        style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
        verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
        style_text = style_map.get(self.communication_style, style_map["neutral"])
        verbosity_text = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])

        chat_id_info = self.chat_id_info

        # --- Блок try...except для форматирования ---
        try:
            # Словарь с плейсхолдерами для шаблона V18. Добавлена информация о времени.
            placeholders = {
                'persona_name': self.name,
                'persona_description': self.description,
                'communication_style': style_text,
                'verbosity_level': verbosity_text,
                'mood_name': mood_name,
                'mood_prompt': mood_instruction,
                'username': username, # Keep username for context
                'user_id': user_id,     # Keep user_id for context
                'chat_id': chat_id_info, # Keep chat_id for context
                'current_time_info': get_time_info(), # <-- НОВОЕ
                'chat_type': ("group" if chat_type in {"group", "supergroup"} else "private"),
            }
            # Безопасное форматирование: не падаем на неизвестных ключах (например, в JSON-примерах c фигурными скобками)
            class SafeDict(dict):
                def __missing__(self, key):
                    return '{' + key + '}'
            formatted_prompt = template.format_map(SafeDict(placeholders))
            logger.debug(f"Formatting system prompt V18 with keys: {list(placeholders.keys())}")

        except KeyError as e:
            # Этот блок выполняется, если в шаблоне есть ключ, которого нет в placeholders
            logger.error(f"FATAL: Missing key in system prompt template V9: {e}. Template sample: {template[:100]}...", exc_info=True)
            # Fallback на простой формат БЕЗ ШАБЛОНА
            fallback_parts = [
                f"Ты {self.name}. {self.description}.",
                f"Стиль: {style_text}. Разговорчивость: {verbosity_text}.",
                f"Настроение: {mood_name} ({mood_instruction}).",
                "Формат ответа: выведи ТОЛЬКО валидный JSON-массив строк (каждый элемент — отдельное сообщение). Пример: [\"привет\", \"как дела?\"].",
                f"Ответь на последнее сообщение от {username} (id: {user_id}) в чате {chat_id_info}."
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
                f"Формат ответа: JSON-объект с ключом 'response', значением является список строк. Пример: {{\"response\":[\"привет\",\"как дела?\"]}}.",
                f"Ответь на последнее сообщение от {username} (id: {user_id}) в чате {chat_id_info}."
            ]
            formatted_prompt = " ".join(fallback_parts)
            logger.warning("Using fallback system prompt due to unexpected formatting error.")
        # --- Конец блока try...except ---

        # Собираем дополнительные инструкции
        additional_instructions = [BASE_PROMPT_SUFFIX, INTERNET_INFO_PROMPT]
        if chat_type in {"group", "supergroup"}:
            additional_instructions.append(GROUP_CHAT_INSTRUCTION)

        final_prompt = f"{formatted_prompt}\n\n[ADDITIONAL INSTRUCTIONS]\n" + "\n".join(additional_instructions)
        return final_prompt.strip()

    def format_conversation_starter_prompt(self, history: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, str]]]:
        """Формирует системный промпт для старта диалога с учетом последних сообщений.

        Возвращает кортеж (system_prompt, messages_for_llm).
        Историю в messages_for_llm не передаем, так как саммари уже включено в системный промпт.
        """
        # Суммаризация последних 5 сообщений
        history_summary_lines: List[str] = []
        try:
            tail = history[-5:] if history else []
            for msg in tail:
                role = "you" if (msg or {}).get("role") == "assistant" else "user"
                content = str((msg or {}).get("content", ""))
                try:
                    # убираем возможный префикс "username: "
                    content = re.sub(r"^\w+:\s", "", content)
                except Exception:
                    pass
                preview = (content[:70] + "...") if len(content) > 70 else content
                history_summary_lines.append(f"- {role}: {preview}")
        except Exception:
            history_summary_lines = []

        history_summary = "\n".join(history_summary_lines) if history_summary_lines else "no recent messages."

        system_prompt = (
            f"you are the character '{self.name}'. your description: '{self.description}'.\n\n"
            "it has been a while since the last message. your task is to start a new conversation naturally, "
            "based on the summary of the last few messages. do not repeat questions or topics from the summary. "
            "come up with something new and engaging.\n\n"
            f"recent message summary:\n{history_summary}\n\n"
            "CRITICAL INSTRUCTION: Your response MUST be in the same language as the messages in the provided history summary. If the history is empty, you can start the conversation in Russian.\n\n"
            "your response must be a valid json array of one or two strings. do not greet the user."
        )

        return system_prompt, []

    def format_should_respond_prompt(self, message_text: str, bot_username: str, history: List[Dict[str, str]]) -> Optional[str]:
        """Formats the prompt to decide if the bot should respond in a group based on context."""
        if self.group_reply_preference != "mentioned_or_contextual":
            # Этот метод вызывается только для contextual
            logger.error("format_should_respond_prompt called for non-contextual preference.")
            return None

        # Получаем шаблон из кеша; избегаем обращения к self.config, если он отсоединён
        template = getattr(self, 'should_respond_prompt_template', None)
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

    def _format_media_prompt(self, media_type_text: str, user_id: Optional[int] = None, username: Optional[str] = None, chat_id: Optional[str] = None) -> Optional[str]:
        """Helper method to format prompts for media reactions based on media_reaction setting.
        
        Args:
            media_type_text: Type of media as string (e.g., 'фото', 'голосовое сообщение', etc.)
            user_id: Optional user ID for context
            username: Optional username for context
            chat_id: Optional chat ID for context
        
        Returns:
            Formatted prompt string or None if shouldn't react
        """
        # Determine whether we should react based on media_reaction setting
        react_setting = self.media_reaction
        should_react = False
        
        # Check if we should process this media type
        if media_type_text == "фото" and react_setting in ["text_and_all_media", "all_media_no_text", "photo_only"]:
            should_react = True
        elif media_type_text == "голосовое сообщение" and react_setting in ["text_and_all_media", "all_media_no_text", "voice_only"]:
            should_react = True
        # NOTE: video, sticker, gif checks were removed as they were not fully implemented
        # and led to a broken prompt generation logic. Add them back here if you implement them.

        if not should_react:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to {media_type_text.upper()} with setting '{react_setting}'. Media prompt generation skipped.")
            return None

        # Проверка наличия необходимых параметров контекста
        if user_id is None or username is None or chat_id is None:
            logger.error(f"Missing context parameters for {media_type_text} prompt: user_id={user_id}, username={username}, chat_id={chat_id}")
            return None
        
        # --- ИСПРАВЛЕНИЕ: Используем тот же механизм маппинга, что и в format_system_prompt ---
        style_map = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "sarcastic": "Саркастичный", "formal": "Формальный", "brief": "Краткий"}
        verbosity_map = {"concise": "Лаконичный", "medium": "Средний", "talkative": "Разговорчивый"}
        style_text = style_map.get(self.communication_style, style_map["neutral"])
        verbosity_text = verbosity_map.get(self.verbosity_level, verbosity_map["medium"])
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
        
        # Выбор шаблона и инструкции (унифицировано)
        if media_type_text == "голосовое сообщение":
            template = VOICE_SYSTEM_PROMPT_TEMPLATE
            media_instruction = "Пользователь прислал(а) голосовое сообщение. Тебе нужно отреагировать на его содержание, продолжая диалог."
        else:
            template = self.config.media_system_prompt_template or DEFAULT_MEDIA_SYSTEM_PROMPT_TEMPLATE
            if media_type_text == "фото":
                media_instruction = "Пользователь прислал(а) фото. Опиши свои эмоции и мысли по поводу увиденного."
            else:
                media_instruction = f"Пользователь прислал(а) {media_type_text}. Отреагируй на это."

        if not template:
            logger.error(f"No suitable template found for media type: {media_type_text}")
            return None

        # --- ИСПРАВЛЕННЫЙ БЛОК ---
        # Получаем данные о настроении ПРАВИЛЬНЫМ СПОСОБОМ
        mood_name = self.current_mood
        mood_prompt = self.get_mood_prompt_snippet()
        
        template_vars = {
            'persona_name': self.name,
            'persona_description': self.description,
            'communication_style': style_text,
            'verbosity_level': verbosity_text,
            'media_interaction_instruction': media_instruction,
            'mood_name': mood_name,
            'mood_prompt': mood_prompt,
            'user_id': user_id,
            'username': username,
            'chat_id': chat_id,
            'current_time_info': get_time_info() # <-- НОВОЕ
        }
        
        try:
            formatted_prompt = template.format(**template_vars)
        except KeyError as e:
            logger.error(f"Error formatting media system prompt for persona {self.id}: Missing key {e}. Template: {template[:150]}...", exc_info=True)
            # --- УЛУЧШЕННЫЙ FALLBACK ---
            # Fallback на дефолтный шаблон с теми же переменными, чтобы избежать падения
            fallback_template = DEFAULT_MEDIA_SYSTEM_PROMPT_TEMPLATE
            try:
                # Используем только те ключи, которые точно есть в fallback-шаблоне
                fallback_vars = {k: v for k, v in template_vars.items() if f"{{{k}}}" in fallback_template}
                formatted_prompt = fallback_template.format(**fallback_vars)
                logger.warning(f"Successfully used fallback template due to KeyError in custom template.")
            except Exception as fallback_e:
                logger.critical(f"FATAL: Fallback media template formatting also failed: {fallback_e}")
                return None
        
        logger.debug(f"Persona {self.id} ({self.name}) WILL react to '{media_type_text}' with setting '{react_setting}'. Prompt generated: {formatted_prompt[:200]}...")
        return formatted_prompt

    def format_photo_prompt(self, user_id: int, username: str, chat_id: str) -> Optional[str]:
        """Formats the prompt for responding to photos."""
        return self._format_media_prompt("фото", user_id, username, chat_id)

    def format_voice_prompt(self, user_id: int, username: str, chat_id: str) -> Optional[str]:
        """Formats the prompt for responding to voice messages."""
        # Убедимся, что персона должна реагировать на голос
        if self.media_reaction not in ["text_and_all_media", "all_media_no_text", "voice_only"]:
            logger.debug(f"Persona {self.id} ({self.name}) configured NOT to react to VOICE with setting '{self.media_reaction}'. Voice prompt generation skipped.")
            return None
        return self._format_media_prompt("голосовое сообщение", user_id, username, chat_id)

    # format_spam_prompt is removed as it wasn't used and placeholders are internal now
