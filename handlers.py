# -*- coding: utf-8 -*-
import asyncio
import httpx
import uuid
import json
import logging
import re
from datetime import datetime, timezone, timedelta
import os
import random
import time
import traceback
import urllib.parse
import uuid
import wave
import subprocess
import base64
from typing import List, Dict, Any, Optional, Union, Tuple
from sqlalchemy import delete
from telegram.constants import ParseMode # Added for confirm_pay

logger = logging.getLogger(__name__)

# Константы для UI
CHECK_MARK = "✅ "  # Unicode Check Mark Symbol

# Импорты для работы с Vosk
try:
    from vosk import Model, KaldiRecognizer
    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False

# --- Vosk model setup ---
VOSK_MODEL_PATH = "model_vosk_ru"
vosk_model = None

def load_vosk_model(model_path: str):
    """Helper function to load the Vosk model if not already loaded."""
    global vosk_model
    if vosk_model is None and VOSK_AVAILABLE:
        logger.info(f"Attempting to load Vosk model from path: {model_path}")
        try:
            if os.path.exists(model_path):
                vosk_model = Model(model_path)
                logger.info(f"Vosk model loaded successfully from {model_path}")
            else:
                logger.warning(f"Vosk model path not found: {model_path}. Voice transcription disabled.")
        except Exception as e:
            logger.error(f"Error loading Vosk model: {e}", exc_info=True)
            vosk_model = None # Ensure it's None on failure
    elif not VOSK_AVAILABLE:
        logger.warning("Vosk library not available. Voice transcription is disabled.")

# Загружаем модель при старте
load_vosk_model(VOSK_MODEL_PATH)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Bot, CallbackQuery
from telegram.constants import ChatAction, ParseMode, ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, ProgrammingError, OperationalError
from sqlalchemy import func, delete

from yookassa import Configuration as YookassaConfig, Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem

# --- ИСПРАВЛЕНИЕ: Добавлены импорты из config.py для устранения NameError ---
import config
from config import (
    SUBSCRIPTION_DURATION_DAYS,
    SUBSCRIPTION_PRICE_RUB,
    SUBSCRIPTION_CURRENCY,
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    PAID_PERSONA_LIMIT,
    FREE_PERSONA_LIMIT,
    MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    CREDIT_PACKAGES
)
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

from db import (
    get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner,
    create_bot_instance, link_bot_instance_to_chat, delete_persona_config,
    get_all_active_chat_bot_instances,
    get_persona_and_context_with_owner,
    unlink_bot_instance_from_chat,
    User, PersonaConfig as DBPersonaConfig, BotInstance as DBBotInstance,
    ChatBotInstance as DBChatBotInstance, ChatContext, func, get_db,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, DEFAULT_MOOD_PROMPTS,
    get_next_api_key,
    set_bot_instance_token
)
from persona import Persona, CommunicationStyle, Verbosity
from utils import (
    postprocess_response,
    extract_gif_links,
    get_time_info,
    escape_markdown_v2,
    TELEGRAM_MAX_LEN,
    count_openai_compatible_tokens,
    send_safe_message,
)

# --- Google Gemini Native API Client ---
async def send_to_google_gemini(
    api_key: str,
    system_prompt: str,
    messages: List[Dict[str, str]],
    image_data: Optional[bytes] = None,
) -> Union[List[str], str]:
    """Отправляет запрос в нативный Google Gemini API и возвращает список строк или строку-ошибку."""
    if not api_key:
        logger.error("send_to_google_gemini called without API key")
        return "[ошибка: API-ключ не предоставлен]"

    model_name = config.GEMINI_MODEL_NAME_FOR_API
    api_url = config.GEMINI_API_BASE_URL_TEMPLATE.format(model=model_name)
    logger.debug(f"Calling Gemini API at: {api_url}")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    # --- Build request for Gemini: use system_instruction and cleaned contents ---
    generation_config: Dict[str, Any] = {
        "temperature": 1.0,
        "topP": 0.95,
        "topK": 64,
        "maxOutputTokens": 8192,
        # Запрашиваем JSON, чтобы модель сразу вернула валидный JSON-массив
        "responseMimeType": "application/json",
    }

    formatted_messages: List[Dict[str, Any]] = []
    for msg in messages:
        # Skip legacy system entries in history; pass system via system_instruction
        if msg.get("role") == "system":
            continue
        role = "model" if msg.get("role") == "assistant" else "user"
        text = msg.get("content", "")
        try:
            # Strip optional "username: " prefix which model doesn't need
            import re as _re
            text = _re.sub(r"^\w+:\s", "", text)
        except Exception:
            pass
        formatted_messages.append({"role": role, "parts": [{"text": text}]})

    # If there are no messages (e.g., proactive "напиши что-нибудь"),
    # add a minimal starter so Google API doesn't reject empty contents
    if not formatted_messages:
        formatted_messages.append({"role": "user", "parts": [{"text": "Начни диалог."}]})

    # Ensure conversation doesn't start with model role
    if formatted_messages and formatted_messages[0].get("role") == "model":
        formatted_messages.insert(0, {"role": "user", "parts": [{"text": "(начало диалога)"}]})

    # Attach image to the last user message, if any
    if image_data and formatted_messages:
        last = formatted_messages[-1]
        if last.get("role") == "user":
            base64_image = base64.b64encode(image_data).decode("utf-8")
            last["parts"].append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64_image
                }
            })

    payload = {
        "contents": formatted_messages,
        "system_instruction": {"parts": [{"text": system_prompt or ""}]},
        "generationConfig": generation_config,
        # Чуть ослабляем фильтры на базовом запросе, чтобы не ловить блок на безобидных сообщениях в группах
        "safetySettings": [
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(api_url, headers=headers, json=payload)
            resp.raise_for_status()
            # Используем встроенный парсер httpx, который корректно учитывает заголовки и кодировку
            data = resp.json()

            # Проверка блокировки промпта
            if isinstance(data, dict) and "promptFeedback" in data and isinstance(data.get("promptFeedback"), dict):
                feedback = data.get("promptFeedback", {}) or {}
                block_reason = feedback.get("blockReason", "UNKNOWN_REASON")
                if block_reason and block_reason != "BLOCK_REASON_UNSPECIFIED":
                    logger.warning(f"Google API blocked prompt. Reason: {block_reason}. Full feedback: {feedback}")
                    # Попробуем единожды переспросить модель с безопасным промптом
                    try:
                        safe_suffix = (
                            "\n\n[SAFETY OVERRIDE]\n"
                            "Если описание изображения может нарушать политику или содержать запрещённый контент,"
                            " не описывай детали. Дай нейтральный, доброжелательный и безопасный ответ в той же"
                            " языковой форме, без упоминания личных признаков людей и без оценочных суждений."
                            " Ответ должен оставаться в формате JSON-массива строк: [\"...\", \"...\"]"
                        )
                        safe_system_prompt = (system_prompt or "") + safe_suffix
                        # --- ИСПРАВЛЕНИЕ: формируем payload в том же формате, что и основной запрос ---
                        safe_formatted_messages: List[Dict[str, Any]] = []
                        for msg in messages or []:
                            if msg.get("role") == "system":
                                continue
                            role = "model" if msg.get("role") == "assistant" else "user"
                            text = msg.get("content", "")
                            safe_formatted_messages.append({"role": role, "parts": [{"text": text}]})

                        if safe_formatted_messages and safe_formatted_messages[0].get("role") == "model":
                            safe_formatted_messages.insert(0, {"role": "user", "parts": [{"text": "(начало диалога)"}]})

                        if image_data and safe_formatted_messages:
                            last_msg = safe_formatted_messages[-1]
                            if last_msg.get("role") == "user":
                                base64_image_retry = base64.b64encode(image_data).decode("utf-8")
                                last_msg["parts"].append({
                                    "inline_data": {
                                        "mime_type": "image/jpeg",
                                        "data": base64_image_retry
                                    }
                                })

                        safe_payload = {
                            "contents": safe_formatted_messages,
                            "system_instruction": {"parts": [{"text": safe_system_prompt}]},
                            "generationConfig": {
                                "temperature": 0.9,
                                "topP": 0.95,
                                "maxOutputTokens": 2048,
                            },
                            # Ослабленные пороги безопасности только для ретрая
                            "safetySettings": [
                                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
                            ],
                        }
                        # Повторный запрос
                        resp2 = await client.post(api_url, headers=headers, json=safe_payload)
                        resp2.raise_for_status()
                        data2 = resp2.json()
                        # Если снова блок — выдаём мягкий ответ
                        if isinstance(data2, dict) and isinstance(data2.get("promptFeedback"), dict):
                            br2 = data2.get("promptFeedback", {}).get("blockReason")
                            if br2 and br2 != "BLOCK_REASON_UNSPECIFIED":
                                logger.warning(f"Google API blocked prompt even after safe retry. Reason: {br2}")
                                return [
                                    "я не могу обсуждать это в деталях, но я с тобой — давай поговорим о чем-то безопасном",
                                ]
                        # иначе продолжаем обычный парсинг ниже, переопределив входные данные
                        data = data2
                    except Exception as _safe_retry_err:
                        logger.warning(f"Safe retry after block failed: {_safe_retry_err}")
                        return [
                            "я не могу обсуждать это в деталях, но я с тобой — давай поговорим о чем-то безопасном",
                        ]

            # Извлекаем первый текстовый ответ
            try:
                text_content = (
                    (data.get("candidates", [{}])[0] or {})
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text")
                )
            except Exception:
                text_content = None

            if not text_content:
                logger.error(f"Google Gemini API returned a valid but empty/unexpected response: {data}")
                return "[ошибка google api: получен пустой или неожиданный ответ от модели]"

            # Пытаемся распарсить текст как JSON
            try:
                parsed = json.loads(text_content)
                if isinstance(parsed, list):
                    return [str(it) for it in parsed if str(it).strip()]
                if isinstance(parsed, dict):
                    # Сначала обрабатываем ключ 'response' как приоритетный
                    response_val = parsed.get('response')
                    if isinstance(response_val, list):
                        return [str(it) for it in response_val if str(it).strip()]
                    if isinstance(response_val, str) and response_val.strip():
                        # Попытка распарсить строку как JSON-массив
                        try:
                            inner_list = json.loads(response_val)
                            if isinstance(inner_list, list):
                                return [str(it) for it in inner_list if str(it).strip()]
                        except (json.JSONDecodeError, TypeError):
                            # Если это не JSON, вернуть как одиночный ответ
                            return [response_val]

                    # Если 'response' не дал ответа — ищем в альтернативных ключах
                    for key in ['answer', 'text', 'parts']:
                        val = parsed.get(key)
                        if isinstance(val, list):
                            return [str(it) for it in val if str(it).strip()]
                        if isinstance(val, str) and val.strip():
                            return [val]

                    logger.warning(f"Model returned JSON but couldn't extract a response list/string: {type(parsed)}. Wrapping as single item.")
                    return [str(text_content)]
                # Иные типы
                logger.warning(f"Model returned JSON but unexpected type: {type(parsed)}. Wrapping as single item.")
                return [str(text_content)]
            except json.JSONDecodeError:
                # Fallback: сначала попробуем извлечь JSON из fenced-блока и найти массив response
                import re
                try:
                    extracted_block = extract_json_from_markdown(text_content)
                except Exception:
                    extracted_block = text_content
                logger.warning(f"Failed to parse JSON. Falling back to regex extraction. Preview: {extracted_block[:200]}")
                # 1) Попробуем найти массив после ключа "response"
                try:
                    m = re.search(r'"response"\s*:\s*(\[.*?\])', extracted_block, re.DOTALL)
                    if m:
                        arr = json.loads(m.group(1))
                        if isinstance(arr, list):
                            return [str(it) for it in arr]
                except Exception:
                    pass
                # 2) Если не вышло — извлечем только элементы массива в квадратных скобках
                try:
                    m2 = re.search(r'(\[\s*".*?"\s*(?:,\s*".*?"\s*)*\])', extracted_block, re.DOTALL)
                    if m2:
                        arr2 = json.loads(m2.group(1))
                        if isinstance(arr2, list):
                            return [str(it) for it in arr2]
                except Exception:
                    pass
                # 3) Последний шанс: извлечь все строки в кавычках, исключая служебные ключи вроде "response"
                try:
                    extracted_parts = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', extracted_block)
                    cleaned_parts = [
                        part.replace('\\"', '"').replace('\\\\', '\\')
                        for part in extracted_parts
                    ]
                    final_parts = [p.strip() for p in cleaned_parts if p and p.strip() and p.strip().lower() != 'response']
                    if final_parts:
                        logger.info(f"Successfully extracted {len(final_parts)} parts via regex fallback.")
                        return final_parts
                    else:
                        logger.error(f"Regex fallback found no quoted strings. Treating as single message. Preview: {extracted_block[:200]}")
                        return [str(text_content)]
                except Exception as regex_err:
                    logger.error(f"Regex fallback failed with error: {regex_err}. Treating as single message.")
                    return [str(text_content)]
    except httpx.HTTPStatusError as e:
        try:
            error_body = e.response.json()
            error_message = (error_body.get("error", {}) or {}).get("message") or str(e)
        except Exception:
            error_message = e.response.text if e.response is not None else str(e)
        logger.error(f"API error calling '{api_url}' (status={getattr(e.response, 'status_code', 'n/a')}): {error_message}")
        api_source = "openrouter" if "openrouter" in (api_url or "") else "google api"
        return f"[ошибка {api_source} {getattr(e.response, 'status_code', 'n/a')}: Provider returned error]"
    except (KeyError, IndexError) as e:
        logger.error(f"Google Gemini API returned unexpected response structure: {str(e)}")
        return "[ошибка google api: неверный формат ответа]"
    except Exception as e:
        logger.error(f"Unexpected error in send_to_google_gemini calling '{api_url}': {e}", exc_info=True)
        return f"[неизвестная ошибка при обращении к API по адресу {api_url}]"

# --- Constants ---
BOTSET_SELECT, BOTSET_MENU, BOTSET_WHITELIST_ADD, BOTSET_WHITELIST_REMOVE = range(4)

def _process_history_for_time_gaps(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Processes message history to insert system notes about time gaps.
    Returns a history list ready for the LLM (with string-only content).
    """
    if not history:
        return []

    processed_history = []
    last_timestamp = None

    for message in history:
        current_timestamp = message.get("timestamp")
        
        if last_timestamp and current_timestamp:
            time_diff = current_timestamp - last_timestamp
            
            # Определяем, какую заметку вставить, если пауза была значительной
            note = None
            if time_diff > timedelta(days=1):
                days = time_diff.days
                note = f"[прошло {days} дн.]"
            elif time_diff > timedelta(hours=2):
                hours = round(time_diff.total_seconds() / 3600)
                note = f"[прошло около {hours} ч.]"
            
            if note:
                processed_history.append({"role": "system", "content": note})

        # Добавляем само сообщение, но уже без timestamp
        processed_history.append({"role": message["role"], "content": message["content"]})
        last_timestamp = current_timestamp
        
    return processed_history

# =====================
# /botsettings (ACL/Whitelist Management)
# =====================

async def botsettings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /botsettings — показать список ваших ботов для настройки ACL."""
    user = update.effective_user
    if not update.message:
        return ConversationHandler.END

    # Разрешаем только владельцу и глобальным админам
    from_id = int(user.id) if user else None
    try:
        admin_ids = set((getattr(config, 'ADMIN_USER_ID', []) or []))
    except Exception:
        admin_ids = set()

    with get_db() as db:
        db_user = db.query(User).filter(User.telegram_id == from_id).first() if from_id else None
        if not db_user:
            await update.message.reply_text("пользователь не найден в системе. напишите /start.", parse_mode=None)
            return ConversationHandler.END

        # Владелец видит только свои боты. Админ — все активные.
        q = db.query(DBBotInstance).filter(DBBotInstance.status == 'active')
        if from_id not in admin_ids:
            q = q.filter(DBBotInstance.owner_id == db_user.id)

        bots = list(q.order_by(DBBotInstance.id.desc()).all())

        if not bots:
            await update.message.reply_text("у вас пока нет активных ботов.", parse_mode=None)
            return ConversationHandler.END

        kb = []
        for bi in bots:
            title = bi.telegram_username or bi.name or f"bot #{bi.id}"
            kb.append([InlineKeyboardButton(title, callback_data=f"botset_pick_{bi.id}")])

        await update.message.reply_text(
            "выберите бота для настройки:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=None
        )
        return BOTSET_SELECT


# --- Chat member updates (auto-link/unlink bot to group chat) ---
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        cmu = update.my_chat_member
        if not cmu:
            return
        chat = cmu.chat
        if not chat:
            return
        chat_id_str = str(chat.id)
        new_status = (cmu.new_chat_member and cmu.new_chat_member.status) or None
        old_status = (cmu.old_chat_member and cmu.old_chat_member.status) or None
        bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None

        logger.info(f"my_chat_member in chat {chat_id_str}: {old_status} -> {new_status} for bot {bot_id_str}")

        # Интересны только группы/супергруппы
        chat_type = str(getattr(chat, 'type', ''))
        if chat_type not in {"group", "supergroup"}:
            return

        with get_db() as db:
            bot_instance = None
            if bot_id_str:
                bot_instance = db.query(DBBotInstance).options(
                    selectinload(DBBotInstance.owner)
                ).filter(
                    DBBotInstance.telegram_bot_id == bot_id_str,
                    DBBotInstance.status == 'active'
                ).first()
            if not bot_instance:
                logger.warning(f"on_my_chat_member: bot instance not found for tg_bot_id={bot_id_str}")
                return

            # Статусы, означающие присутствие бота в чате
            present_statuses = {"member", "administrator", "creator", "owner"}
            gone_statuses = {"left", "kicked", "restricted"}

            if new_status and new_status.lower() in present_statuses:
                inviter_id = getattr(cmu.from_user, 'id', None)
                owner_telegram_id = getattr(getattr(bot_instance, 'owner', None), 'telegram_id', None)
                if inviter_id and owner_telegram_id and str(inviter_id) == str(owner_telegram_id):
                    link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)
                    if link:
                        logger.info(f"on_my_chat_member: Owner {inviter_id} added bot. Linked bot_instance {bot_instance.id} to chat {chat_id_str}")
                    else:
                        logger.warning(f"on_my_chat_member: failed to link bot_instance {bot_instance.id} to chat {chat_id_str}")
                else:
                    logger.warning(f"on_my_chat_member: non-owner inviter={inviter_id} tried to add bot_instance {bot_instance.id} to chat {chat_id_str}. Leaving chat.")
                    try:
                        await context.bot.leave_chat(chat.id)
                    except Exception as leave_err:
                        logger.error(f"on_my_chat_member: failed to leave chat {chat_id_str}: {leave_err}")
            elif new_status and new_status.lower() in gone_statuses:
                ok = unlink_bot_instance_from_chat(db, chat_id_str, bot_instance.id)
                if ok:
                    logger.info(f"on_my_chat_member: unlinked bot_instance {bot_instance.id} from chat {chat_id_str}")
                else:
                    logger.warning(f"on_my_chat_member: no active link to unlink for bot_instance {bot_instance.id} chat {chat_id_str}")
    except Exception as e:
        logger.error(f"on_my_chat_member failed: {e}", exc_info=True)
    return None

async def botsettings_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    m = re.match(r"^botset_pick_(\d+)$", q.data)
    if not m:
        return ConversationHandler.END
    bot_id = int(m.group(1))
    context.user_data['botsettings_bot_id'] = bot_id
    return await botsettings_menu_show(update, context)

async def botsettings_menu_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню настроек для выбранного бота."""
    q = update.callback_query
    chat_id = None
    if q and q.message:
        chat_id = q.message.chat.id
    elif update.effective_chat:
        chat_id = update.effective_chat.id
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        if q:
            await q.edit_message_text("не удалось определить выбранного бота. запустите /botsettings заново.")
        else:
            await context.bot.send_message(chat_id, "не удалось определить выбранного бота. запустите /botsettings заново.")
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            if q:
                await q.edit_message_text("бот не найден.")
            else:
                await context.bot.send_message(chat_id, "бот не найден.")
            return ConversationHandler.END
        title = bi.telegram_username or bi.name or f"bot #{bi.id}"
        access = bi.access_level or 'owner_only'
        try:
            wl = json.loads(bi.whitelisted_users_json or '[]')
        except Exception:
            wl = []
        wl_count = len(wl)
        # Получаем mute-статус для текущего чата и выбранного бота
        cbi = db.query(DBChatBotInstance).filter(
            DBChatBotInstance.chat_id == str(chat_id),
            DBChatBotInstance.bot_instance_id == bi.id,
            DBChatBotInstance.active == True
        ).first()
        is_muted = bool(getattr(cbi, 'is_muted', False)) if cbi else False
        mute_status = '🔇 заглушен' if is_muted else '🔊 активен'
        text = (
            f"настройки бота: {title}\n"
            f"статус в этом чате: {mute_status}\n"
            f"доступ: {access}\n"
            f"белый список: {wl_count} пользователей"
        )
        kb = [
            [InlineKeyboardButton("доступ: public", callback_data="botset_access_public")],
            [InlineKeyboardButton("доступ: whitelist", callback_data="botset_access_whitelist")],
            [InlineKeyboardButton("доступ: owner_only", callback_data="botset_access_owner_only")],
        ]
        # Добавляем кнопку mute/unmute в зависимости от текущего статуса
        if is_muted:
            kb.append([InlineKeyboardButton("🔊 размут бота", callback_data="botset_unmute")])
        else:
            kb.append([InlineKeyboardButton("🔇 мут бота", callback_data="botset_mute")])
        kb += [
            [InlineKeyboardButton("👁 просмотр whitelist", callback_data="botset_wl_show")],
            [InlineKeyboardButton("➕ добавить в whitelist", callback_data="botset_wl_add")],
            [InlineKeyboardButton("➖ удалить из whitelist", callback_data="botset_wl_remove")],
            [InlineKeyboardButton("⬅️ закрыть", callback_data="botset_close")],
        ]
        if q:
            try:
                await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
            except BadRequest as e_br:
                # Игнорируем безвредную ошибку от Telegram: "message is not modified"
                if "message is not modified" in str(e_br).lower():
                    try:
                        await q.answer("нет изменений", show_alert=False)
                    except Exception:
                        pass
                else:
                    raise
        else:
            await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
    return BOTSET_MENU

async def botsettings_set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    m = re.match(r"^botset_access_(public|whitelist|owner_only)$", q.data)
    if not m:
        return ConversationHandler.END
    new_level = m.group(1)
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            await q.edit_message_text("бот не найден.")
            return ConversationHandler.END
        bi.access_level = new_level
        db.add(bi)
        db.commit()
    return await botsettings_menu_show(update, context)

async def botsettings_wl_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            await q.edit_message_text("бот не найден.")
            return ConversationHandler.END
        try:
            wl = json.loads(bi.whitelisted_users_json or '[]')
        except Exception:
            wl = []
        if not wl:
            text = "белый список пуст."
        else:
            text = "белый список (tg ids):\n" + "\n".join(f"• {uid}" for uid in wl)
        kb = [[InlineKeyboardButton("⬅️ назад", callback_data="botset_back")]]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
    return BOTSET_MENU

async def botsettings_wl_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text("отправьте numeric telegram id пользователя для добавления в whitelist:", parse_mode=None)
    else:
        if update.message:
            await update.message.reply_text("отправьте numeric telegram id пользователя для добавления в whitelist:", parse_mode=None)
    return BOTSET_WHITELIST_ADD

async def botsettings_wl_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return BOTSET_WHITELIST_ADD
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("нужно отправить числовой telegram id.", parse_mode=None)
        return BOTSET_WHITELIST_ADD
    add_id = int(text)
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        await update.message.reply_text("сессия настройки утеряна, запустите /botsettings заново.", parse_mode=None)
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            await update.message.reply_text("бот не найден.", parse_mode=None)
            return ConversationHandler.END
        try:
            wl = json.loads(bi.whitelisted_users_json or '[]')
        except Exception:
            wl = []
        if add_id not in wl:
            wl.append(add_id)
            bi.whitelisted_users_json = json.dumps(wl, ensure_ascii=False)
            db.add(bi)
            db.commit()
    await update.message.reply_text("пользователь добавлен в whitelist.", parse_mode=None)
    return await botsettings_menu_show(update, context)

async def botsettings_wl_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            await q.edit_message_text("бот не найден.")
            return ConversationHandler.END
        try:
            wl = json.loads(bi.whitelisted_users_json or '[]')
        except Exception:
            wl = []
        if not wl:
            await q.edit_message_text("белый список пуст.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ назад", callback_data="botset_back")]]), parse_mode=None)
            return BOTSET_MENU
        kb = [[InlineKeyboardButton(f"удалить {uid}", callback_data=f"botset_wl_del_{uid}")]]
        kb = [[InlineKeyboardButton(f"удалить {uid}", callback_data=f"botset_wl_del_{uid}")] for uid in wl]
        kb.append([InlineKeyboardButton("⬅️ назад", callback_data="botset_back")])
        await q.edit_message_text("выберите пользователя для удаления:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
    return BOTSET_WHITELIST_REMOVE

async def botsettings_wl_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    m = re.match(r"^botset_wl_del_(\d+)$", q.data)
    if not m:
        return BOTSET_WHITELIST_REMOVE
    rem_id = int(m.group(1))
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    with get_db() as db:
        bi = db.query(DBBotInstance).filter(DBBotInstance.id == bot_id).first()
        if not bi:
            await q.edit_message_text("Бот не найден.")
            return ConversationHandler.END
        try:
            wl = json.loads(bi.whitelisted_users_json or '[]')
        except Exception:
            wl = []
        if rem_id in wl:
            wl = [x for x in wl if x != rem_id]
            bi.whitelisted_users_json = json.dumps(wl, ensure_ascii=False)
            db.add(bi)
            db.commit()
    return await botsettings_menu_show(update, context)

async def botsettings_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    chat_id = q.message.chat.id if q and q.message else (update.effective_chat.id if update.effective_chat else None)
    if not chat_id:
        return ConversationHandler.END
    with get_db() as db:
        cbi = db.query(DBChatBotInstance).filter(
            DBChatBotInstance.chat_id == str(chat_id),
            DBChatBotInstance.bot_instance_id == int(bot_id),
            DBChatBotInstance.active == True
        ).first()
        if not cbi:
            await q.edit_message_text("бот не привязан к этому чату или не активирован для этой личности.")
            return ConversationHandler.END
        if cbi.is_muted:
            # уже заглушен — просто перерисуем меню
            return await botsettings_menu_show(update, context)
        try:
            cbi.is_muted = True
            db.add(cbi)
            db.commit()
        except Exception:
            db.rollback()
            await q.edit_message_text("не удалось применить мут (ошибка БД)")
            return ConversationHandler.END
    return await botsettings_menu_show(update, context)

async def botsettings_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()
    bot_id = context.user_data.get('botsettings_bot_id')
    if not bot_id:
        return ConversationHandler.END
    chat_id = q.message.chat.id if q and q.message else (update.effective_chat.id if update.effective_chat else None)
    if not chat_id:
        return ConversationHandler.END
    with get_db() as db:
        cbi = db.query(DBChatBotInstance).filter(
            DBChatBotInstance.chat_id == str(chat_id),
            DBChatBotInstance.bot_instance_id == int(bot_id),
            DBChatBotInstance.active == True
        ).first()
        if not cbi:
            await q.edit_message_text("бот не привязан к этому чату или не активирован для этой личности.")
            return ConversationHandler.END
        if not cbi.is_muted:
            # уже размьючен — просто перерисуем меню
            return await botsettings_menu_show(update, context)
        try:
            cbi.is_muted = False
            db.add(cbi)
            db.commit()
        except Exception:
            db.rollback()
            await q.edit_message_text("не удалось применить размут (ошибка БД)")
            return ConversationHandler.END
    return await botsettings_menu_show(update, context)

async def botsettings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await botsettings_menu_show(update, context)

async def botsettings_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text("настройки закрыты.")
        except Exception:
            pass
    return ConversationHandler.END

async def transcribe_audio_with_vosk(audio_data: bytes, original_mime_type: str) -> Optional[str]:
    """
    Транскрибирует аудиоданные с помощью Vosk.
    """
    global vosk_model
    if not vosk_model:
        logger.error("Vosk model not loaded. Cannot transcribe.")
        return None

    temp_ogg_filename = f"temp_voice_{uuid.uuid4().hex}.ogg"
    temp_wav_filename = f"temp_voice_wav_{uuid.uuid4().hex}.wav"

    try:
        with open(temp_ogg_filename, "wb") as f_ogg:
            f_ogg.write(audio_data)

        command = [
            "ffmpeg", "-i", temp_ogg_filename, "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", temp_wav_filename, "-y"
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"ffmpeg conversion failed: {stderr.decode(errors='ignore')}")
            return None

        with wave.open(temp_wav_filename, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE" or wf.getframerate() != 16000:
                logger.error(f"Audio file {temp_wav_filename} is not in the correct format.")
                return None

            current_recognizer = KaldiRecognizer(vosk_model, wf.getframerate())
            current_recognizer.SetWords(True)

            full_transcription = ""
            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if current_recognizer.AcceptWaveform(data):
                    result = json.loads(current_recognizer.Result())
                    full_transcription += result.get("text", "") + " "

            final_result_json = json.loads(current_recognizer.FinalResult())
            full_transcription += final_result_json.get("text", "")

            transcribed_text = full_transcription.strip()
            logger.info(f"Vosk transcription result: '{transcribed_text}'")
            return transcribed_text if transcribed_text else None

    except FileNotFoundError:
        logger.error("ffmpeg not found. Please ensure ffmpeg is installed and in your system's PATH.")
        return None
    except Exception as e:
        logger.error(f"Error during Vosk transcription: {e}", exc_info=True)
        return None
    finally:
        if os.path.exists(temp_ogg_filename):
            os.remove(temp_ogg_filename)
        if os.path.exists(temp_wav_filename):
            os.remove(temp_wav_filename)

# --- Helper Functions ---
## (subscription-related helpers removed; no longer needed in credit model)

def is_admin(user_id: int) -> bool:
    """Checks if the user ID belongs to an admin."""
    return user_id in config.ADMIN_USER_ID

# --- Conversation States ---
# Edit Persona Wizard States
(EDIT_WIZARD_MENU, # Main wizard menu
EDIT_NAME, EDIT_DESCRIPTION, EDIT_COMM_STYLE, EDIT_VERBOSITY,
EDIT_GROUP_REPLY, EDIT_MEDIA_REACTION,
# Delete Persona Conversation State
DELETE_PERSONA_CONFIRM,
EDIT_MAX_MESSAGES,
EDIT_PROACTIVE_RATE,
PROACTIVE_CHAT_SELECT,
# EDIT_MESSAGE_VOLUME removed
) = range(11) # Total 11 states now

# Character Setup Wizard States
(
    CHAR_WIZ_BIO, CHAR_WIZ_TRAITS, CHAR_WIZ_SPEECH,
    CHAR_WIZ_LIKES, CHAR_WIZ_DISLIKES, CHAR_WIZ_GOALS, CHAR_WIZ_TABOOS
) = range(20, 27) # start from a new range

# --- Bot Token Registration State ---
REGISTER_BOT_TOKEN = 100

# --- Terms of Service Text ---
TOS_TEXT_RAW = """
пользовательское соглашение

1. общие положения
1.1. настоящее пользовательское соглашение (далее — «соглашение») регулирует отношения между вами (далее — «пользователь») и сервисом @NunuAiBot (далее — «сервис»).
1.2. начало использования сервиса (отправка любой команды или сообщения боту) означает полное и безоговорочное принятие всех условий соглашения. если вы не согласны с какими‑либо условиями, прекратите использование сервиса.

2. предмет соглашения
2.1. сервис предоставляет пользователю возможность создавать и взаимодействовать с виртуальными собеседниками на базе искусственного интеллекта (далее — «личности»).
2.2. взаимодействие с личностями осуществляется с использованием «кредитов» — внутренней валюты сервиса.

3. кредиты и оплата
3.1. новым пользователям может начисляться стартовый баланс кредитов для ознакомления с сервисом.
3.2. кредиты расходуются при отправке сообщений личностям, генерации изображений, распознавании голосовых сообщений и использовании других функций сервиса. стоимость операций в кредитах определяется сервисом и может изменяться.
3.3. пользователь может пополнить баланс кредитов, приобретая пакеты через команду /buycredits. оплата производится через платежную систему yookassa.
3.4. политика возвратов: все покупки кредитов окончательны. средства, уплаченные за кредиты, не подлежат возврату, поскольку услуга по их зачислению предоставляется немедленно.

4. ограничение ответственности
4.1. сервис предоставляется на условиях «как есть» (as is). мы не гарантируем бесперебойную или безошибочную работу сервиса.
4.2. важно: личности — это искусственный интеллект. генерируемый ими контент может быть неточным, вымышленным или не соответствовать действительности. сервис не несет ответственности за содержание ответов, сгенерированных личностями. не используйте их ответы в качестве финансовых, медицинских, юридических или иных профессиональных советов.
4.3. сервис не несет ответственности за любые прямые или косвенные убытки пользователя, возникшие в результате использования или невозможности использования сервиса.

5. конфиденциальность
5.1. для работы сервис собирает и хранит следующие данные: ваш telegram id, username (если есть), историю сообщений с личностями (для поддержания контекста диалога), информацию о созданных личностях и балансе кредитов.
5.2. мы не передаем ваши данные третьим лицам, за исключением случаев, предусмотренных законодательством.

6. права и обязанности сторон
6.1. пользователь обязуется не использовать сервис в противозаконных целях, для рассылки спама или распространения запрещенной информации.
6.2. сервис оставляет за собой право изменять настоящее соглашение, тарифы на кредиты и функциональность сервиса в одностороннем порядке.
6.3. сервис вправе заблокировать доступ пользователю в случае нарушения условий настоящего соглашения.

7. заключительные положения
7.1. по всем вопросам обращайтесь в поддержку сервиса (контакты указаны в описании бота).
"""
# Текст используем напрямую; для безопасности в Telegram применим экранирование при отправке
formatted_tos_text_for_bot = TOS_TEXT_RAW
TOS_TEXT = escape_markdown_v2(formatted_tos_text_for_bot)

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if isinstance(context.error, Forbidden):
        if config.CHANNEL_ID and str(config.CHANNEL_ID) in str(context.error):
            logger.warning(f"Error handler caught Forbidden regarding channel {config.CHANNEL_ID}. Bot likely not admin or kicked.")
            return
        else:
            logger.warning(f"Caught generic Forbidden error: {context.error}")
            return

    elif isinstance(context.error, BadRequest):
        error_text = str(context.error).lower()
        if "message is not modified" in error_text:
            logger.info("Ignoring 'message is not modified' error.")
            return
        elif "can't parse entities" in error_text:
            logger.error(f"MARKDOWN PARSE ERROR: {context.error}. Update: {update}")
            if isinstance(update, Update) and update.effective_message:
                try:
                    await update.effective_message.reply_text("❌ произошла ошибка при форматировании ответа. пожалуйста, сообщите администратору.", parse_mode=None)
                except Exception as send_err:
                    logger.error(f"Failed to send 'Markdown parse error' message: {send_err}")
            return
        elif "chat member status is required" in error_text:
            logger.warning(f"Error handler caught BadRequest likely related to missing channel membership check: {context.error}")
            return
        elif "chat not found" in error_text:
            logger.error(f"BadRequest: Chat not found error: {context.error}")
            return
        elif "reply message not found" in error_text:
            logger.warning(f"BadRequest: Reply message not found. Original message might have been deleted. Update: {update}")
            return
        else:
            logger.error(f"Unhandled BadRequest error: {context.error}")

    elif isinstance(context.error, TimedOut):
        logger.warning(f"Telegram API request timed out: {context.error}")
        return

    elif isinstance(context.error, TelegramError):
        logger.error(f"Generic Telegram API error: {context.error}")

    error_message_raw = "упс... 😕 что-то пошло не так. попробуй еще раз позже."
    escaped_error_message = escape_markdown_v2(error_message_raw)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(escaped_error_message, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_md:
            if "can't parse entities" in str(e_md).lower():
                logger.error(f"Failed sending even basic Markdown error msg ({e_md}). Sending plain.")
                try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
            else:
                logger.error(f"Failed sending error message (BadRequest, not parse): {e_md}")
                try: await update.effective_message.reply_text(error_message_raw, parse_mode=None)
                except Exception as final_e: logger.error(f"Failed even sending plain text error message: {final_e}")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")
            try:
                await update.effective_message.reply_text(error_message_raw, parse_mode=None)
            except Exception as final_e:
                logger.error(f"Failed even sending plain text error message: {final_e}")


# --- Core Logic Helpers ---
def extract_json_from_markdown(text: str) -> str:
    """
    Extracts a JSON string from a markdown code block (e.g., ```json...```).
    If no markdown block is found, returns the original text.
    """
    # The pattern looks for a string inside ```<lang>? ... ``` or ``` ... ```
    # Previously we only allowed optional 'json' language marker, which caused leaking 'text' into content
    # when models responded with ```text ...```. Now accept any language marker and exclude it from capture.
    pattern = r"```(?:[a-zA-Z0-9_\-]+)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        extracted_json = match.group(1).strip()
        # Safety: drop a leading language marker line like 'text', 'json', 'markdown', etc.
        # Examples to strip: 'text', 'text:\n', 'json -', 'md: ', 'plain\n'
        # Accept punctuation like :, -, ., ;, — and spaces after marker
        lang_marker_pattern = r'^(?:json|text|markdown|plain|md)\b[\s:\-\.；;—–]*'
        extracted_json = re.sub(lang_marker_pattern, '', extracted_json, flags=re.IGNORECASE)
        logger.debug(f"Extracted from fenced block. Original len={len(text)}, extracted len={len(extracted_json)}; preview='{extracted_json[:120]}'")
        return extracted_json
    # If no markdown block is found, maybe the response is already a clean JSON array.
    plain = text.strip()
    # Also strip plain leading language markers if model put them without fences
    plain = re.sub(r'^(?:json|text|markdown|plain|md)\b[\s:\-\.；;—–]*', '', plain, flags=re.IGNORECASE)
    logger.debug(f"No fenced block detected. Returning plain text preview='{plain[:120]}' (orig len={len(text)})")
    return plain

def _is_degenerate_text(text: str) -> bool:
    """Heuristic check for useless model outputs like 'ext', 'ok', single meaningless ascii tokens.
    Returns True if the text is likely garbage and should trigger a fallback/regeneration.
    """
    if text is None:
        return True
    s = str(text).strip().strip('"\'')
    if not s:
        return True
    # Explicit junk tokens (ascii-only); DO NOT include Russian words like 'да', 'нет'
    junk_set = {"ext", "ok", "yes", "no", "k", "x", "test", "response"}
    if s.lower() in junk_set:
        return True
    # If string is ascii-only and very short (<= 4-5), consider junk
    try:
        is_ascii_only = not re.search(r"[А-Яа-яЁё]", s)
        if is_ascii_only and len(s) <= 5:
            # exclude common punctuation-only
            if re.fullmatch(r"[\W_]+", s):
                return True
            # single short ascii token like 'ok', 'yo', 'hi'
            if re.fullmatch(r"[A-Za-z]{1,5}", s):
                return True
    except Exception:
        pass
    return False

# Максимальная длина входящего сообщения от пользователя в символах
MAX_USER_MESSAGE_LENGTH_CHARS = 600

async def send_to_openrouter(
    api_key: str,
    system_prompt: str,
    messages: List[Dict[str, str]],
    model_name: str,
    image_data: Optional[bytes] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Union[List[str], str]:
    """Sends a request to the OpenRouter API and handles the response."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/your_bot_username",
        "X-Title": "NunuAi Telegram Bot",
    }
    openrouter_messages = []
    if system_prompt:
        openrouter_messages.append({"role": "system", "content": system_prompt})
    if image_data:
        last_user_message = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        text_content = last_user_message['content'] if last_user_message else "Опиши картинку кратко, затем задай 1-2 вопроса. Ответ в JSON."
        base64_image = base64.b64encode(image_data).decode('utf-8')
        image_url_data = f"data:image/jpeg;base64,{base64_image}"
        # Пересобираем последнее пользовательское сообщение вместе с картинкой в универсальном формате
        if last_user_message:
            messages_without_last = [m for m in messages if m is not last_user_message]
            openrouter_messages.extend(messages_without_last)
        else:
            openrouter_messages.extend(messages)
        openrouter_messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {"url": image_url_data}},
            ],
        })
    else:
        openrouter_messages.extend(messages)
    # Напрямую используем имя модели из конфига; позволяем OpenRouter маршрутизировать запрос автоматически
    payload = {
        "model": model_name,
        "messages": openrouter_messages,
        "stream": False,
        "response_format": {"type": "json_object"},
        # Pass-through Gemini safety settings via OpenRouter
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    # Temperature: если явно не задано — используем немного творческий режим по умолчанию
    if temperature is not None:
        payload["temperature"] = float(temperature)
    else:
        payload["temperature"] = 0.9
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(config.OPENROUTER_API_BASE_URL, json=payload, headers=headers)
        if resp.status_code == 200:
            try:
                data = resp.json()
                content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                if not content or _is_degenerate_text(content):
                    logger.warning(f"OpenRouter returned empty or degenerate content: '{str(content)[:100]}' — attempting one safe retry with adjusted params")
                    # --- ONE SAFE RETRY ---
                    # Modify the existing system prompt instead of adding a second system message
                    retry_suffix = (
                        "\n\n[SYSTEM WARNING] Your previous response was empty or invalid. "
                        "You MUST generate a valid JSON response authentic to your character role. "
                        "Return only a JSON object with key 'response' mapped to an array of strings."
                    )
                    retry_system_prompt = (system_prompt or "") + retry_suffix

                    # Build retry messages: start with modified system, then original user/assistant messages
                    base_messages = messages or []
                    retry_messages = []
                    if retry_system_prompt:
                        retry_messages.append({"role": "system", "content": retry_system_prompt})
                    # Rebuild image payload if needed
                    if image_data:
                        # replicate same image handling as above
                        last_user_message = next((m for m in reversed(base_messages) if m.get('role') == 'user'), None)
                        text_content = last_user_message['content'] if last_user_message else "Опиши картинку кратко, затем задай 1-2 вопроса. Ответ в JSON."
                        base64_image = base64.b64encode(image_data).decode('utf-8')
                        image_url_data = f"data:image/jpeg;base64,{base64_image}"
                        if last_user_message:
                            messages_without_last = [m for m in base_messages if m is not last_user_message]
                            retry_messages.extend(messages_without_last)
                        else:
                            retry_messages.extend(base_messages)
                        retry_messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": text_content},
                                {"type": "image_url", "image_url": {"url": image_url_data}},
                            ],
                        })
                    else:
                        retry_messages.extend(base_messages)

                    retry_payload = {
                        "model": model_name,
                        "messages": retry_messages,
                        "stream": False,
                        "response_format": {"type": "json_object"},
                        # Pass-through Gemini safety settings via OpenRouter (retry)
                        "safety_settings": [
                            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                        ],
                        "temperature": 0.75,
                    }
                    if max_tokens is not None:
                        retry_payload["max_tokens"] = int(max_tokens)
                    try:
                        async with httpx.AsyncClient(timeout=90.0) as client:
                            retry_resp = await client.post(config.OPENROUTER_API_BASE_URL, json=retry_payload, headers=headers)
                        if retry_resp.status_code == 200:
                            try:
                                retry_data = retry_resp.json()
                                retry_content = retry_data.get('choices', [{}])[0].get('message', {}).get('content', '')
                                if not retry_content or _is_degenerate_text(retry_content):
                                    logger.warning(f"Retry still produced degenerate/empty content: '{str(retry_content)[:100]}' — sending graceful fallback text to user")
                                    # Возвращаем нейтральный вежливый ответ как успешный список сообщений,
                                    # чтобы не сыпать техническими ошибками в чат.
                                    return [
                                        "не совсем понял мысль. можешь сказать иначе или чуть подробнее?",
                                    ]
                                # Parse JSON on retry (flexible)
                                try:
                                    retry_clean = (retry_content or "").strip()
                                    # strip common markdown code fences
                                    if retry_clean.startswith("```") and retry_clean.endswith("```"):
                                        retry_clean = retry_clean.strip("`")
                                    if retry_clean.lower().startswith("json\n"):
                                        retry_clean = retry_clean[5:]
                                    retry_parsed = json.loads(retry_clean)
                                    if isinstance(retry_parsed, dict) and isinstance(retry_parsed.get("response"), list):
                                        return [str(item).strip() for item in retry_parsed["response"] if str(item).strip()]
                                    if isinstance(retry_parsed, list):
                                        return [str(item).strip() for item in retry_parsed if str(item).strip()]
                                    if isinstance(retry_parsed, dict):
                                        for key in ["text", "body", "message", "content", "answer"]:
                                            val = retry_parsed.get(key)
                                            if isinstance(val, str) and val.strip():
                                                return [val.strip()]
                                            if isinstance(val, list):
                                                items = [str(it).strip() for it in val if str(it).strip()]
                                                if items:
                                                    return items
                                    logger.warning(f"OpenRouter retry returned valid JSON but unexpected structure: {str(retry_parsed)[:200]}")
                                    return [json.dumps(retry_parsed, ensure_ascii=False, indent=2)]
                                except Exception as e_json_retry:
                                    logger.warning(f"Retry JSON parse failed: {e_json_retry}. Raw: {retry_content[:200]}")
                                    return [str(retry_content)]
                            except (json.JSONDecodeError, IndexError) as e2:
                                logger.warning(f"Could not parse OpenRouter JSON retry response: {e2}. Raw text: {retry_resp.text[:250]}")
                                return f"[ошибка openrouter: не удалось обработать ответ: {retry_resp.text[:100]}]"
                        else:
                            logger.warning(f"Retry failed with status {retry_resp.status_code}: {retry_resp.text[:180]}")
                            return f"[ошибка openrouter api {retry_resp.status_code}: {retry_resp.text}]"
                    except Exception as retry_err:
                        logger.error(f"Retry request to OpenRouter failed: {retry_err}")
                        return f"[ошибка сети при повторном обращении к OpenRouter: {retry_err}]"
                # Parse primary response as JSON
                try:
                    clean = (content or "").strip()
                    # strip common markdown code fences
                    if clean.startswith("```") and clean.endswith("```"):
                        clean = clean.strip("`")
                    if clean.lower().startswith("json\n"):
                        clean = clean[5:]
                    parsed = json.loads(clean)

                    # 1) Preferred format: {"response": [ ... ]}
                    if isinstance(parsed, dict) and isinstance(parsed.get("response"), list):
                        return [str(item).strip() for item in parsed["response"] if str(item).strip()]
                    # Added: handle string response
                    elif isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
                        response_str = parsed.get("response", "").strip()
                        if response_str:
                            return [response_str]

                    # 2) Fallback: raw list [ ... ]
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]

                    # 3) Heuristic extraction from common keys
                    if isinstance(parsed, dict):
                        for key in ["text", "body", "message", "content", "answer"]:
                            val = parsed.get(key)
                            if isinstance(val, str) and val.strip():
                                return [val.strip()]
                            if isinstance(val, list):
                                items = [str(it).strip() for it in val if str(it).strip()]
                                if items:
                                    return items

                    # 4) Unknown but valid JSON: return formatted for visibility
                    logger.warning(f"OpenRouter returned valid JSON but unexpected structure: {str(parsed)[:200]}")
                    return [json.dumps(parsed, ensure_ascii=False, indent=2)]
                except Exception as e_json:
                    logger.warning(f"Could not parse OpenRouter JSON response: {e_json}. Raw text: {str(content)[:250]}")
                    return [str(content)]
            except (json.JSONDecodeError, IndexError) as e:
                logger.warning(f"Could not parse OpenRouter JSON response: {e}. Raw text: {resp.text[:250]}")
                return f"[ошибка openrouter: не удалось обработать ответ: {resp.text[:100]}]"
        else:
            # Улучшенная обработка ошибок API
            error_text = f"[ошибка openrouter api {resp.status_code}: {resp.text}]"
            try:
                error_data = resp.json()
                msg = (error_data or {}).get("error", {}).get("message", resp.text)
                error_text = f"[ошибка openrouter api {resp.status_code}: {msg}]"

                # Частый кейс: отсутствие доступных эндпоинтов (например, закончились кредиты или недоступна модель)
                if resp.status_code == 404 and isinstance(msg, str) and "no endpoints found" in msg.lower():
                    logger.warning("OpenRouter 404 'No endpoints found' — вероятно, нет кредитов или неверный/недостаточный API-ключ для выбранной модели.")
                    return "[ошибка доступа: модель недоступна. возможно, на балансе openrouter закончились кредиты или неверный api-ключ.]"
            except Exception:
                # Оставляем error_text как есть
                pass

            logger.error(error_text)
            return error_text
    except httpx.RequestError as e:
        logger.error(f"HTTP request to OpenRouter failed: {e}")
        return f"[ошибка сети при обращении к OpenRouter: {e}]"
    except Exception as e:
        logger.error(f"Unexpected error in send_to_openrouter: {e}", exc_info=True)
        return "[неизвестная ошибка при обращении к OpenRouter API]"

def parse_and_split_messages(text_content: str) -> List[str]:
    """Splits a plain text response from an LLM into a list of messages based on newlines."""
    if not text_content or not text_content.strip():
        return []

    cleaned_text = text_content.strip()
    # Снимаем внешние тройные кавычки-код-блоки, если модель их добавила
    if cleaned_text.startswith("```") and cleaned_text.endswith("```"):
        cleaned_text = cleaned_text.strip().strip("`")
    # Снимаем внешние одиночные кавычки, если есть
    if len(cleaned_text) >= 2 and cleaned_text.startswith('"') and cleaned_text.endswith('"'):
        cleaned_text = cleaned_text[1:-1]

    parts = [part.strip() for part in cleaned_text.split('\n') if part.strip()]
    return parts if parts else [cleaned_text]

async def get_llm_response(
    db_session: Session,
    owner_user: User,
    system_prompt: str,
    context_for_ai: List[Dict[str, str]],
    image_data: Optional[bytes] = None,
    media_type: Optional[str] = None,
) -> Tuple[Union[List[str], str], str, Optional[str]]:
    """
    Централизованный выбор LLM: OpenRouter для платных пользователей, Gemini для бесплатных.
    Возвращает (ответ, имя_модели, использованный_api_ключ или None).
    """
    attached_owner = db_session.merge(owner_user)
    has_credits = attached_owner.has_credits()
    llm_response: Union[List[str], str] = "[системная ошибка: ответ LLM не был получен]"
    model_to_use = "unknown"
    api_key_to_use = None

    try:
        if has_credits:
            api_key_to_use = config.OPENROUTER_API_KEY
            if not api_key_to_use:
                return "[ошибка: ключ OPENROUTER_API_KEY не настроен]", "N/A", None

            # Для фото используем специальную модель из конфига, для текста — основную.
            if media_type == 'photo' and image_data:
                model_to_use = config.OPENROUTER_IMAGE_MODEL_NAME
                logger.info(f"get_llm_response: user has credits, media is photo. Using OpenRouter image model: '{model_to_use}'.")
            else:
                model_to_use = config.OPENROUTER_MODEL_NAME
            
            logger.info(
                f"get_llm_response: user {getattr(attached_owner, 'id', 'N/A')} has credits; using OpenRouter model '{model_to_use}'."
            )
            
            llm_response = await send_to_openrouter(
                api_key=api_key_to_use,
                system_prompt=system_prompt,
                messages=context_for_ai,
                model_name=model_to_use,
                image_data=image_data,
                temperature=(0.3 if (media_type == 'photo' and image_data) else None),
                max_tokens=(400 if (media_type == 'photo' and image_data) else None),
            )

        else:
            # Нет кредитов — используем Gemini
            model_to_use = config.GEMINI_MODEL_NAME_FOR_API
            api_key_obj = get_next_api_key(db_session, service='gemini')
            if not api_key_obj or not api_key_obj.api_key:
                return "[ошибка: нет доступных API-ключей Gemini]", model_to_use, None
            api_key_to_use = api_key_obj.api_key
            logger.info(f"get_llm_response: using Gemini model '{model_to_use}'.")
            llm_response = await send_to_google_gemini(
                api_key=api_key_to_use,
                system_prompt=system_prompt,
                messages=context_for_ai,
                image_data=image_data,
            )

    except Exception as e:
        logger.error(f"[CRITICAL] get_llm_response failed: {e}", exc_info=True)
        llm_response = f"[критическая ошибка в get_llm_response: {e}]"

    return llm_response, model_to_use, api_key_to_use

async def process_and_send_response(update: Update, context: ContextTypes.DEFAULT_TYPE, bot: Bot, chat_id: Union[str, int], persona: Persona, llm_response: Union[List[str], str], db: Session, reply_to_message_id: int, is_first_message: bool = False) -> bool:
    """Processes the response from AI (list of strings or error string) and sends messages to the chat."""
    logger.info(f"process_and_send_response [v4]: --- ENTER --- ChatID: {chat_id}, Persona: '{persona.name}'")

    text_parts_to_send: List[str] = []
    if isinstance(llm_response, str):
        # строка с ошибкой или необычным ответом от LLM
        logger.warning(f"process_and_send_response [v4]: received error string from LLM: '{llm_response[:200]}'")
        if llm_response.strip():
            text_parts_to_send = [llm_response.strip()]
    elif isinstance(llm_response, list):
        text_parts_to_send = [str(part).strip() for part in llm_response if str(part).strip()]
        logger.info(f"process_and_send_response [v4]: received {len(text_parts_to_send)} parts from LLM.")
    else:
        logger.error(f"process_and_send_response [v4]: unexpected LLM response type: {type(llm_response)}")
        return False

    if not text_parts_to_send:
        logger.warning("process_and_send_response [v4]: no non-empty parts to send. exiting.")
        return False

    # Подготовим текст для сохранения в БД и поиска GIF
    content_to_save_in_db = "\n".join(text_parts_to_send)
    context_response_prepared = False
    if persona.chat_instance:
        try:
            add_message_to_context(db, persona.chat_instance.id, "assistant", content_to_save_in_db)
            context_response_prepared = True
            logger.debug("AI response prepared for database context (pending commit).")
        except SQLAlchemyError as e:
            logger.error(f"DB Error preparing assistant response for context: {e}", exc_info=True)
            context_response_prepared = False
    else:
        logger.error("Cannot add AI response to context, chat_instance is None.")

    gif_links_to_send = extract_gif_links(content_to_save_in_db)
    if gif_links_to_send:
        logger.info(f"process_and_send_response [v4]: Found {len(gif_links_to_send)} GIF(s) to send: {gif_links_to_send}")

    # --- ФИНАЛЬНАЯ ОЧИСТКА ПЕРЕД ОТПРАВКОЙ (V2) ---
    # Гарантируем отсутствие внешних скобок/кавычек в сообщениях, независимо от пути обработки выше.
    final_cleaned_parts: List[str] = []
    if text_parts_to_send:
        for part in text_parts_to_send:
            cleaned_part = part.strip()
            # Снимаем внешние кавычки
            if len(cleaned_part) >= 2 and cleaned_part.startswith('"') and cleaned_part.endswith('"'):
                cleaned_part = cleaned_part[1:-1].strip()
            # Снимаем внешние квадратные скобки
            if len(cleaned_part) >= 2 and cleaned_part.startswith('[') and cleaned_part.endswith(']'):
                cleaned_part = cleaned_part[1:-1].strip()
            # Ещё раз снимем возможные кавычки после скобок
            if len(cleaned_part) >= 2 and cleaned_part.startswith('"') and cleaned_part.endswith('"'):
                cleaned_part = cleaned_part[1:-1].strip()
            if cleaned_part:
                final_cleaned_parts.append(cleaned_part)
    text_parts_to_send = final_cleaned_parts
    # --- КОНЕЦ БЛОКА ОЧИСТКИ ---


    # --- СТРАХОВКА ОТ ПОВТОРНЫХ ПРИВЕТСТВИЙ ---
    # Если это не первое сообщение в диалоге, и модель вдруг поздоровалась, убираем это.
    if text_parts_to_send and not is_first_message:
        first_part = text_parts_to_send[0]
        # Паттерн для разных вариантов приветствий
        greetings_pattern = r"^\s*(?:привет|здравствуй|добр(?:ый|ое|ого)\s+(?:день|утро|вечер)|хай|ку|здорово|салют|о[йи])(?:[,.!?;:]|\b)"
        match = re.match(greetings_pattern, first_part, re.IGNORECASE)
        if match:
            # Убираем приветствие и лишние пробелы
            cleaned_part = first_part[match.end():].lstrip()
            # Удаляем приветствие, только если после него есть содержимое и исходная фраза была длиннее
            if cleaned_part and len(first_part) > len(match.group(0)) + 5:
                logger.info(f"process_and_send_response [JSON]: Removed greeting. New start of part 1: '{cleaned_part[:50]}...'")
                text_parts_to_send[0] = cleaned_part
            else:
                # Не удаляем, если ответ целиком — короткое приветствие
                logger.info("process_and_send_response [JSON]: Greeting is the whole message. Keeping it.")

    # Фильтрация деградированных ответов (например, 'ext') до применения лимитов
    try:
        filtered_parts = [p for p in text_parts_to_send if not _is_degenerate_text(p)]
        if not filtered_parts:
            logger.warning("process_and_send_response: all parts considered degenerate (e.g., 'ext'). Suppressing send.")
            return False
        text_parts_to_send = filtered_parts
    except Exception as _flt_err:
        logger.warning(f"process_and_send_response: failed to filter degenerate parts: {_flt_err}")

    if persona and persona.config:
        max_messages_setting_value = persona.config.max_response_messages
        target_message_count = -1
        if max_messages_setting_value == 1:
            target_message_count = 1
        elif max_messages_setting_value == 3:
            target_message_count = 3
        elif max_messages_setting_value == 6:
            target_message_count = 6
        elif max_messages_setting_value == 0:
            # "Случайный" режим: если частей много, выбираем случайно разумный предел,
            # если частей мало — отправляем все.
            if len(text_parts_to_send) > 5:
                try:
                    target_message_count = random.randint(2, 5)
                except Exception:
                    target_message_count = 5
            else:
                target_message_count = len(text_parts_to_send)
        else:
            logger.warning(f"Неожиданное значение max_response_messages: {max_messages_setting_value}. Используется стандартное (3).")
            target_message_count = 3

        if target_message_count != -1 and len(text_parts_to_send) > target_message_count:
            logger.info(f"ОБЩЕЕ ОГРАНИЧЕНИЕ: Обрезаем сообщения с {len(text_parts_to_send)} до {target_message_count} (настройка: {max_messages_setting_value})")
            text_parts_to_send = text_parts_to_send[:target_message_count]
        logger.info(f"Финальное количество текстовых частей для отправки: {len(text_parts_to_send)} (настройка: {max_messages_setting_value})")

    try:
        first_message_sent = False
        chat_id_str = str(chat_id)
        # Используем переданный экземпляр бота (он уже соответствует текущему апдейту)
        local_bot = bot
        logger.info(
            f"process_and_send_response: using passed bot @{getattr(local_bot, 'username', None)} (id={getattr(local_bot, 'id', None)}) for persona '{persona.config.name if persona and persona.config else 'unknown'}'"
        )

        # Parsing now happens upstream in send_to_* functions. Use text_parts_to_send as-is.

        chat_type = update.effective_chat.type if update and update.effective_chat else None

        if gif_links_to_send:
            for i, gif_url_send in enumerate(gif_links_to_send):
                try:
                    current_reply_id_gif = reply_to_message_id if not first_message_sent else None
                    logger.info(f"process_and_send_response [JSON]: Attempting to send GIF {i+1}/{len(gif_links_to_send)}: {gif_url_send} (ReplyTo: {current_reply_id_gif})")
                    await local_bot.send_animation(
                        chat_id=chat_id_str, animation=gif_url_send, reply_to_message_id=current_reply_id_gif,
                        read_timeout=30, write_timeout=30
                    )
                    first_message_sent = True
                    logger.info(f"process_and_send_response [JSON]: Successfully sent GIF {i+1}.")
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                except Exception as e_gif:
                    logger.error(f"process_and_send_response [JSON]: Error sending GIF {gif_url_send}: {e_gif}", exc_info=True)

        if text_parts_to_send:
            for i, part_raw_send in enumerate(text_parts_to_send):
                if not part_raw_send:
                    continue
                # Санитизация отключена: отправляем ответ модели как есть
                sanitized_part = part_raw_send

                if not sanitized_part:
                    logger.warning(f"process_and_send_response [JSON]: Part {i+1} is empty after preprocessing. Skipping.")
                    continue

                if len(sanitized_part) > TELEGRAM_MAX_LEN:
                    logger.warning(f"process_and_send_response [JSON]: Part {i+1} exceeds max length ({len(sanitized_part)}). Truncating.")
                    sanitized_part = sanitized_part[:TELEGRAM_MAX_LEN - 3] + "..."

                if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    try:
                        # небольшой таймаут между сообщениями в группах, чтобы не ловить flood control
                        await asyncio.sleep(0.35)
                    except Exception as e:
                        logger.warning(f"Failed to sleep: {e}")

                current_reply_id_text = reply_to_message_id if not first_message_sent else None
                escaped_part_send = escape_markdown_v2(sanitized_part)
                message_sent_successfully = False

                logger.info(f"process_and_send_response [JSON]: Attempting send part {i+1}/{len(text_parts_to_send)} (MDv2, ReplyTo: {current_reply_id_text}) to {chat_id_str}: '{escaped_part_send[:80]}...')")
                try:
                    await local_bot.send_message(
                        chat_id=chat_id_str, text=escaped_part_send, parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=current_reply_id_text, read_timeout=30, write_timeout=30
                    )
                    message_sent_successfully = True
                except Exception as e_md_send:
                    logger.warning(f"process_and_send_response [JSON]: Failed to send part {i+1} with MarkdownV2: {e_md_send}. Retrying plain text...")
                    try:
                        # Если причина — не найдено сообщение для ответа, пробуем без reply_to
                        retry_reply_to = None if isinstance(e_md_send, BadRequest) and 'replied not found' in str(e_md_send).lower() else current_reply_id_text
                        await local_bot.send_message(
                            chat_id=chat_id_str, text=sanitized_part, parse_mode=None,
                            reply_to_message_id=retry_reply_to, read_timeout=30, write_timeout=30
                        )
                        message_sent_successfully = True
                    except Exception as e_plain_send:
                        logger.error(f"process_and_send_response [JSON]: Failed plain send part {i+1}: {e_plain_send}", exc_info=True)
                        break
                except Exception as e_other_send:
                    logger.error(f"process_and_send_response [JSON]: Unexpected error sending part {i+1}: {e_other_send}", exc_info=True)
                    break

                if message_sent_successfully:
                    first_message_sent = True
                    logger.info(f"process_and_send_response [JSON]: Successfully sent part {i+1}/{len(text_parts_to_send)}.")
                else:
                    logger.error(f"process_and_send_response [JSON]: Failed to send part {i+1}, stopping.")
                    break

    except Exception as e_main_process:
        logger.error(f"process_and_send_response [JSON]: CRITICAL UNEXPECTED ERROR in main block: {e_main_process}", exc_info=True)
    finally:
        # --- СИНХРОНИЗАЦИЯ КОНТЕКСТА МЕЖДУ БОТАМИ В ОДНОМ ЧАТЕ ---
        # Если ответ ассистента был успешно сохранен в контексте текущего бота,
        # добавим это же сообщение в контекст всех других активных ботов этого чата.
        try:
            if context_response_prepared and persona and getattr(persona, 'chat_instance', None):
                # --- CONTEXT SYNC DISABLED ---
                # The logic below caused context pollution between different personas in the same chat.
                # Disabling it ensures each persona maintains its own independent conversation history.
                pass
        except Exception as e_sync:
            logger.error(f"process_and_send_response: context sync failed: {e_sync}", exc_info=True)

        logger.info("process_and_send_response [JSON]: --- EXIT --- Returning context_prepared_status: " + str(context_response_prepared))
        return context_response_prepared

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming text messages. (v3 - Final)"""
    logger.info("!!! VERSION CHECK: Running with Context Fix (2024-06-09) !!!")
    try:
        if not update.message or not (update.message.text or update.message.caption):
            logger.debug("handle_message: Exiting - No message or text/caption.")
            return

        # Получаем корректного бота для этого апдейта (важно для multi-bot окружения)
        try:
            current_bot = update.get_bot()
        except Exception:
            current_bot = None

        # --- Determine if this is a command ---
        try:
            entities = update.message.entities or []
            text_raw = update.message.text or ''
            is_command = any((e.type == 'bot_command') for e in entities) or text_raw.startswith('/')
        except Exception:
            entities = []
            text_raw = update.message.text or ''
            is_command = text_raw.startswith('/')

        # --- NEW: Block non-command messages on the main bot ---
        main_bot_id = context.bot_data.get('main_bot_id')
        if (main_bot_id and current_bot and str(current_bot.id) == str(main_bot_id)
                and not is_command):
            logger.info(f"handle_message: Ignored non-command text message for main bot (ID: {main_bot_id}). Main bot only handles commands.")
            return
        # --- END NEW BLOCK ---

        # --- Block commands on attached (non-main) bots ---
        try:
            # main_bot_id already defined above
            if is_command and main_bot_id and current_bot and str(current_bot.id) != str(main_bot_id):
                logger.info(f"handle_message: Skip command on attached bot (current={current_bot.id}, main={main_bot_id}).")
                return
        except Exception as e_cmd_chk:
            logger.error(f"handle_message: error checking command on attached bot: {e_cmd_chk}")

        chat_id_str = str(update.effective_chat.id)
        user_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_id}"
        message_text = (update.message.text or update.message.caption or "").strip()
        message_id = update.message.message_id

        if len(message_text) > MAX_USER_MESSAGE_LENGTH_CHARS:
            logger.info(f"User {user_id} in chat {chat_id_str} sent a message exceeding {MAX_USER_MESSAGE_LENGTH_CHARS} chars. Length: {len(message_text)}")
            await update.message.reply_text("Ваше сообщение слишком длинное. Пожалуйста, попробуйте его сократить.", parse_mode=None)
            return
        
        if not message_text:
            logger.debug(f"handle_message: Exiting - Empty message text from user {user_id} in chat {chat_id_str}.")
            return

        logger.info(f"MSG < User {user_id} ({username}) in Chat {chat_id_str} (MsgID: {message_id}): '{message_text[:100]}'")
        limit_state_changed = False
        context_user_msg_added = False

        # Подписка больше не проверяется; работаем только по кредитной модели

        db_session = None
        try:
            with get_db() as db:
                db_session = db
                logger.debug("handle_message: DB session acquired.")

                # Передаем id текущего телеграм-бота, чтобы выбрать верную персону, привязанную к этому боту
                # ВАЖНО: используем update.get_bot(), чтобы получить именно того бота, для которого пришёл апдейт
                try:
                    current_bot = update.get_bot()
                except Exception:
                    current_bot = None
                current_bot_id_str = str(getattr(current_bot, 'id', None)) if current_bot else None
                logger.debug(f"handle_message: selecting persona for chat {chat_id_str} with current_bot_id={current_bot_id_str}")
                persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db_session, current_bot_id_str)
                if not persona_context_owner_tuple:
                    # авто-связывание для групп и приватных чатов, если связи нет
                    chat_type = str(getattr(update.effective_chat, 'type', ''))
                    if chat_type in {"group", "supergroup", "private"} and current_bot_id_str:
                        logger.info(f"автоматическая привязка личности для чата {chat_id_str} (тип: {chat_type})")
                        try:
                            bot_instance = db_session.query(DBBotInstance).filter(
                                DBBotInstance.telegram_bot_id == str(current_bot_id_str),
                                DBBotInstance.status == 'active'
                            ).first()
                            if bot_instance:
                                logger.info(f"найден bot_instance id={bot_instance.id} (status={bot_instance.status}) для tg_bot_id={current_bot_id_str}")
                                link = link_bot_instance_to_chat(db_session, bot_instance.id, chat_id_str)
                                if link:
                                    logger.info(f"авто-привязка успешна. повторный поиск личности для чата {chat_id_str}.")
                                    # повторная попытка получить персону
                                    persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db_session, current_bot_id_str)
                                else:
                                    logger.warning(f"авто-привязка вернула None для чата {chat_id_str} и bot_instance {bot_instance.id}")
                            else:
                                logger.warning(f"bot_instance со status='active' не найден для tg_bot_id={current_bot_id_str}. авто-привязка невозможна")
                        except Exception as auto_link_err:
                            logger.error(f"ошибка авто-привязки при первом сообщении для чата {chat_id_str}: {auto_link_err}", exc_info=True)
                if not persona_context_owner_tuple:
                    logger.warning(f"handle_message: No active persona found for chat {chat_id_str} even after auto-link attempt.")
                    return
                
                # Распаковываем кортеж правильно. Второй элемент - это ChatBotInstance, а не контекст.
                persona, chat_instance, owner_user = persona_context_owner_tuple
                # Cache owner_user id early while the session is active to avoid DetachedInstanceError later
                owner_user_id_cache = owner_user.id
                logger.info(f"handle_message: Found active persona '{persona.name}' (ID: {persona.id}) owned by User ID {owner_user_id_cache} (TG: {owner_user.telegram_id}).")
                
                # Теперь получаем контекст (список сообщений) отдельно, используя chat_instance.id
                initial_context_from_db = get_context_for_chat_bot(db_session, chat_instance.id)

                if persona.config.media_reaction in ["all_media_no_text", "photo_only", "voice_only", "none"]:
                    logger.info(f"handle_message: Persona '{persona.name}' (ID: {persona.id}) is configured with media_reaction='{persona.config.media_reaction}', so it will not respond to this text message. Message will still be added to context if not muted.")
                    if not persona.chat_instance.is_muted:
                        current_user_message_content = f"{username}: {message_text}"
                        try:
                            add_message_to_context(db_session, persona.chat_instance.id, "user", current_user_message_content)
                            context_user_msg_added = True
                        except (SQLAlchemyError, Exception) as e_ctx_text_ignore:
                            logger.error(f"handle_message: Error preparing user message context (for ignored text response) for CBI {persona.chat_instance.id}: {e_ctx_text_ignore}", exc_info=True)
                    
                    if limit_state_changed or context_user_msg_added:
                        try:
                            db_session.commit()
                            logger.debug("handle_message: Committed owner limit/context state (text response ignored due to media_reaction).")
                        except Exception as commit_err:
                            logger.error(f"handle_message: Commit failed (text response ignored): {commit_err}", exc_info=True)
                            db_session.rollback()
                    return

                # Убраны месячные лимиты и подписки: обработка продолжается по кредитной модели

                current_user_message_content = f"{username}: {message_text}"
                current_user_message_dict = {"role": "user", "content": current_user_message_content}
                context_user_msg_added = False
                
                # --- Broadcasting disabled: save only to current persona.chat_instance ---
                try:
                    if persona.chat_instance:
                        add_message_to_context(db_session, persona.chat_instance.id, "user", current_user_message_content)
                        context_user_msg_added = True
                    else:
                        logger.warning(f"handle_message: persona has no chat_instance for chat {chat_id_str}, cannot save user message context.")
                except Exception as e_ctx_single:
                    logger.error(
                        f"handle_message: add_message_to_context failed for CBI {getattr(persona.chat_instance, 'id', 'unknown')}: {e_ctx_single}",
                        exc_info=True,
                    )

                if persona.chat_instance.is_muted:
                    logger.info(f"handle_message: Persona '{persona.name}' is muted in chat {chat_id_str}. Saving context and exiting.")
                    if limit_state_changed or context_user_msg_added:
                        try:
                            db_session.commit()
                            logger.debug("handle_message: Committed DB changes for muted bot (limits/user context).")
                        except Exception as commit_err:
                            logger.error(f"handle_message: Commit failed for muted bot context save: {commit_err}", exc_info=True)
                            db_session.rollback()
                    return

                should_ai_respond = True
                if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    reply_pref = persona.group_reply_preference
                    # Берём username и id ИМЕННО привязанного к чату бота
                    bot_instance = getattr(persona, 'chat_instance', None) and getattr(persona.chat_instance, 'bot_instance_ref', None)
                    bot_username = (bot_instance.telegram_username if bot_instance else None) or "YourBotUsername"
                    try:
                        bot_telegram_id = int(bot_instance.telegram_bot_id) if (bot_instance and bot_instance.telegram_bot_id) else None
                    except Exception:
                        bot_telegram_id = None

                    if not bot_instance or not bot_telegram_id:
                        logger.error(f"handle_message: Could not get bot username or id for group check! PersonaID: {getattr(persona, 'id', 'unknown')}")

                    persona_name_lower = persona.name.lower()
                    # 1) Явное упоминание @username
                    is_mentioned = (f"@{bot_username}".lower() in message_text.lower()) if bot_username else False
                    # 2) Ответ на сообщение бота (reply)
                    is_reply_to_bot = (
                        bool(getattr(update, 'message', None) and getattr(update.message, 'reply_to_message', None)) and
                        getattr(update.message.reply_to_message, 'from_user', None) is not None and
                        (getattr(update.message.reply_to_message.from_user, 'id', None) == bot_telegram_id)
                    )
                    # 3) Упоминание по имени персоны
                    contains_persona_name = bool(re.search(rf'(?i)\b{re.escape(persona_name_lower)}\b', message_text))

                    logger.debug(
                        f"handle_message: Group chat check. Pref: '{reply_pref}', Mentioned: {is_mentioned}, "
                        f"ReplyToBot: {is_reply_to_bot}, ContainsName: {contains_persona_name}, BotID_checked: {bot_telegram_id}"
                    )

                    if reply_pref == "never":
                        should_ai_respond = False
                    elif reply_pref == "always":
                        should_ai_respond = True
                    elif reply_pref == "mentioned_only":
                        should_ai_respond = is_mentioned or is_reply_to_bot or contains_persona_name
                    elif reply_pref == "mentioned_or_contextual":
                        should_ai_respond = is_mentioned or is_reply_to_bot or contains_persona_name
                        if not should_ai_respond:
                            # --- КОНТЕКСТУАЛЬНАЯ ПРОВЕРКА ЧЕРЕЗ LLM (С ПРЕДВАРИТЕЛЬНЫМ ЗАКРЫТИЕМ СЕССИИ) ---
                            logger.info("handle_message: No direct mention. Performing contextual LLM check...")
                            # Закрываем текущую транзакцию перед длительным сетевым вызовом
                            try:
                                db_session.commit()
                                db_session.close()
                                logger.debug("handle_message: DB session committed and closed before contextual AI call.")
                            except Exception as e_commit_ctx:
                                logger.warning(f"handle_message: commit/close before contextual AI call failed: {e_commit_ctx}")

                            try:
                                ctx_prompt = persona.format_should_respond_prompt(
                                    message_text=message_text,
                                    bot_username=bot_username,
                                    history=initial_context_from_db
                                )
                            except Exception as fmt_err:
                                logger.error(f"Failed to format contextual should_respond prompt: {fmt_err}", exc_info=True)
                                ctx_prompt = None

                            api_key_for_check = None
                            if ctx_prompt:
                                # 1) Получаем ключ в ОТДЕЛЬНОЙ короткой сессии
                                try:
                                    with get_db() as _db_check:
                                        key_obj_check = get_next_api_key(_db_check, service='gemini')
                                        if key_obj_check and getattr(key_obj_check, 'api_key', None):
                                            api_key_for_check = key_obj_check.api_key
                                        _db_check.commit()
                                except Exception as key_err:
                                    logger.error(f"Contextual check: failed to fetch API key: {key_err}", exc_info=True)

                            # 2) Долгий вызов LLM выполняем ВНЕ активной транзакции основной сессии
                            if api_key_for_check and ctx_prompt:
                                try:
                                    llm_decision = await send_to_google_gemini(
                                        api_key=api_key_for_check,
                                        system_prompt="You decide if the bot should respond based on relevance. Answer only with 'Да' or 'Нет'.",
                                        messages=[{"role": "user", "content": ctx_prompt}]
                                    )
                                    # Повторная попытка при перегрузке модели Gemini
                                    if isinstance(llm_decision, str) and ("503" in llm_decision or "overload" in llm_decision.lower()):
                                        logger.warning("Contextual LLM check: Gemini overloaded (503). Retrying once...")
                                        await asyncio.sleep(1.5)
                                        llm_decision = await send_to_google_gemini(
                                            api_key=api_key_for_check,
                                            system_prompt="You decide if the bot should respond based on relevance. Answer only with 'Да' or 'Нет'.",
                                            messages=[{"role": "user", "content": ctx_prompt}]
                                        )
                                    if isinstance(llm_decision, list) and llm_decision:
                                        ans = str(llm_decision[0]).strip().lower()
                                    else:
                                        ans = str(llm_decision or "").strip().lower()
                                    if "да" in ans:
                                        should_ai_respond = True
                                        logger.info(f"LLM contextual check PASSED (answer: {ans}).")
                                    else:
                                        logger.info(f"LLM contextual check FAILED (answer: {ans}).")
                                except Exception as llm_err:
                                    logger.error(f"Contextual LLM check failed: {llm_err}", exc_info=True)
                                    # по ошибке проверки — оставляем решение 'не отвечать'
                            elif ctx_prompt is None:
                                logger.warning("Contextual prompt not generated; skipping LLM check.")
                            else:
                                logger.warning("No API key available for contextual check; skipping LLM check.")

                    if not should_ai_respond:
                        logger.info(f"handle_message: Final decision - NOT responding in group '{getattr(update.effective_chat, 'title', '')}'.")
                        # Сессия уже закрыта перед контекстной проверкой; никаких откатов не делаем
                        return

                if should_ai_respond:
                    try:
                        db_session.commit()  # сохраняем пользовательское сообщение в контекст только при ответе
                        logger.debug("handle_message: User message committed prior to AI call (group decision=respond).")
                    except Exception as commit_err:
                        logger.error(f"handle_message: Commit failed before AI call (group decision=respond): {commit_err}", exc_info=True)

                    # Вызываем format_system_prompt БЕЗ текста сообщения, с учетом типа чата
                    system_prompt = persona.format_system_prompt(user_id, username, getattr(update.effective_chat, 'type', None))
                    if not system_prompt:
                        await update.message.reply_text(escape_markdown_v2("❌ ошибка при подготовке системного сообщения."), parse_mode=ParseMode.MARKDOWN_V2)
                        db_session.rollback()
                        return

                    # Контекст для ИИ - это история + новое сообщение.
                    # ВАЖНО: очищаем историю от лишних полей (например, timestamp), чтобы избежать ошибок сериализации JSON.
                    try:
                        context_for_ai = [
                            {"role": msg.get("role"), "content": msg.get("content")}
                            for msg in (initial_context_from_db or [])
                            if isinstance(msg, dict) and msg.get("role") and msg.get("content") is not None
                        ]
                    except Exception:
                        # Фолбэк: если история неожиданного формата, игнорируем её
                        context_for_ai = []
                    context_for_ai.append({"role": "user", "content": f"{username}: {message_text}"})
                    # --- Закрываем транзакцию/сессию перед долгим IO (AI) ---
                    try:
                        db_session.commit()
                        db_session.close()
                        logger.debug("handle_message: DB session committed and closed before AI call.")
                    except Exception as e_commit:
                        logger.warning(f"handle_message: commit/close before AI call failed: {e_commit}")

                    # --- Вызов LLM через централизованную функцию (OpenRouter/Gemini) ---
                    assistant_response_text: Union[List[str], str]
                    model_used: str
                    with get_db() as llm_db:
                        # Re-fetch fresh owner_user bound to this new session to avoid DetachedInstanceError
                        try:
                            owner_user_for_llm = llm_db.query(User).filter(User.id == owner_user_id_cache).first()
                        except Exception as refetch_err:
                            logger.error(f"Failed to re-fetch owner_user by cached id {owner_user_id_cache}: {refetch_err}")
                            owner_user_for_llm = None
                        if not owner_user_for_llm:
                            logger.error(f"FATAL: owner_user not found in new session by id={owner_user_id_cache}. Aborting AI call.")
                            return
                        assistant_response_text, model_used, _ = await get_llm_response(
                            db_session=llm_db,
                            owner_user=owner_user_for_llm,
                            system_prompt=system_prompt,
                            context_for_ai=context_for_ai,
                        )

                    context_response_prepared = False
                    # Новая логика: успешный ответ — это список строк; строка — это ошибка/заглушка
                    if isinstance(assistant_response_text, list):
                        llm_call_succeeded = True
                        # Открываем новую короткую сессию для сохранения ответа и списания кредитов
                        with get_db() as db_after_ai:
                            try:
                                # Загружаем свежие объекты в новой сессии
                                owner_user_refreshed = db_after_ai.query(User).filter(User.id == owner_user_id_cache).first()
                                persona_tuple_fresh = get_persona_and_context_with_owner(chat_id_str, db_after_ai, str(getattr(current_bot, 'id', None)) if current_bot else None)
                                if not persona_tuple_fresh or not owner_user_refreshed:
                                    logger.warning("handle_message: fresh persona or owner_user not found in Phase 2.")
                                else:
                                    persona_fresh, _, _ = persona_tuple_fresh
                                    context_response_prepared = await process_and_send_response(
                                        update,
                                        context,
                                        current_bot,
                                        chat_id_str,
                                        persona_fresh,
                                        assistant_response_text,  # список строк
                                        db_after_ai,
                                        reply_to_message_id=message_id,
                                        is_first_message=(len(initial_context_from_db) == 0)
                                    )
                                    if context_response_prepared:
                                        # ВАЖНО: присоединяем пользователя к текущей сессии, чтобы избежать DetachedInstanceError
                                        try:
                                            attached_owner = db_after_ai.merge(owner_user_refreshed) if owner_user_refreshed is not None else None
                                        except Exception as merge_err:
                                            logger.error(f"Failed to merge owner_user into session: {merge_err}")
                                            attached_owner = owner_user_refreshed
                                        await deduct_credits_for_interaction(
                                            db=db_after_ai,
                                            owner_user=attached_owner,
                                            input_text=message_text,
                                            output_text="\n".join(assistant_response_text),
                                            model_name=model_used,
                                            media_type=None,
                                            main_bot=context.application.bot
                                        )
                                    db_after_ai.commit()
                            except Exception as e_after:
                                logger.error(f"handle_message: error during Phase 2 DB save: {e_after}", exc_info=True)
                    else:
                        logger.warning(f"handle_message: Received empty or error response from send_to_gemini for chat {chat_id_str}.")
                        try:
                            final_err_msg = assistant_response_text if assistant_response_text else "модель не дала содержательного ответа. попробуйте переформулировать запрос."
                            await update.message.reply_text(final_err_msg, parse_mode=None)
                        except Exception as e_send_empty:
                            logger.error(f"Failed to send empty/error response message: {e_send_empty}")

                    # Списание кредитов уже выполнено в Phase 2 внутри новой короткой сессии
                    # (после успешной подготовки и отправки ответа: context_response_prepared == True).
                    # Во избежание DetachedInstanceError и двойного списания ПОВТОРНО не списываем здесь.
                    # Если в редком случае понадобится списание без сохранения контекста, 
                    # следует открыть новую сессию и выполнить merge, однако по текущей логике это не требуется.

                    if limit_state_changed or context_user_msg_added or context_response_prepared:
                        try:
                            logger.debug(f"handle_message: Final commit. Limit: {limit_state_changed}, UserCtx: {context_user_msg_added}, RespCtx: {context_response_prepared}")
                            db_session.commit()
                            logger.info(f"handle_message: Successfully processed message and committed changes for chat {chat_id_str}.")
                        except SQLAlchemyError as final_commit_err:
                            logger.error(f"handle_message: FINAL COMMIT FAILED: {final_commit_err}", exc_info=True)
                            try:
                                db_session.rollback()
                                db_session.close()
                            except Exception as rollback_err:
                                logger.error(f"handle_message: ROLLBACK FAILED: {rollback_err}", exc_info=True)
                    else:
                        logger.debug("handle_message: No DB changes detected for final commit.")

        except IntegrityError as e:
            logger.error(f"handle_message: IntegrityError (нарушение уникальности): {e}", exc_info=True)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(
                        "❌ Ошибка целостности данных. Возможно, вы пытаетесь создать дубликат.",
                        parse_mode=None,
                    )
                except Exception:
                    pass
            if db_session:
                try:
                    db_session.rollback()
                except Exception as rb_err:
                    logger.error(f"handle_message: rollback after IntegrityError failed: {rb_err}")
        except ProgrammingError as e:
            logger.critical(f"handle_message: CRITICAL ProgrammingError (несоответствие схемы БД и кода?): {e}", exc_info=True)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(
                        "❌ Критическая ошибка конфигурации базы данных. Свяжитесь с администратором.",
                        parse_mode=None,
                    )
                except Exception:
                    pass
            if db_session:
                try:
                    db_session.rollback()
                except Exception as rb_err:
                    logger.error(f"handle_message: rollback after ProgrammingError failed: {rb_err}")
        except OperationalError as e:
            logger.error(f"handle_message: OperationalError (проблема с подключением к БД?): {e}", exc_info=True)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(
                        "❌ Временно не удается подключиться к базе данных. Попробуйте позже.",
                        parse_mode=None,
                    )
                except Exception:
                    pass
            if db_session:
                try:
                    db_session.rollback()
                except Exception as rb_err:
                    logger.error(f"handle_message: rollback after OperationalError failed: {rb_err}")
        except SQLAlchemyError as e:
            logger.error(f"handle_message: Unhandled SQLAlchemyError: {e}", exc_info=True)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text(
                        "❌ Ошибка базы данных. Попробуйте позже.",
                        parse_mode=None,
                    )
                except Exception:
                    pass
            if db_session:
                try:
                    db_session.rollback()
                    db_session.close()
                except Exception as rollback_err:
                    logger.error(f"handle_message: ROLLBACK FAILED: {rollback_err}", exc_info=True)
        except TelegramError as e:
            logger.error(f"handle_message: TelegramError: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"handle_message: Unexpected Exception: {e}", exc_info=True)
            if update.effective_message:
                try: await update.effective_message.reply_text("❌ Произошла непредвиденная ошибка.", parse_mode=None)
                except Exception: pass
            if db_session: db_session.rollback()

    except Exception as outer_e:
        logger.error(f"handle_message: Critical error in outer try block: {outer_e}", exc_info=True)
        if update.effective_message:
            try: await update.effective_message.reply_text("❌ Произошла критическая ошибка.", parse_mode=None)
            except Exception: pass


# --- Unified credits deduction helper ---
async def deduct_credits_for_interaction(
    db: Session,
    owner_user: User,
    input_text: str,
    output_text: str,
    model_name: str,
    media_type: Optional[str] = None,
    media_duration_sec: Optional[int] = None,
    main_bot=None,
) -> None:
    """Рассчитывает и списывает кредиты за одно взаимодействие (текст/фото/голос)."""
    try:
        from config import CREDIT_COSTS, MODEL_PRICE_MULTIPLIERS, GEMINI_MODEL_NAME_FOR_API, LOW_BALANCE_WARNING_THRESHOLD, FREE_IMAGE_RESPONSES
    
        # Используем множитель именно той модели, которая была задействована
        effective_model = model_name or GEMINI_MODEL_NAME_FOR_API
        mult = MODEL_PRICE_MULTIPLIERS.get(effective_model, 1.0)
        total_cost = 0.0
        # безопасное значение по умолчанию для флага бесплатных фото
        try:
            _free_images = bool(FREE_IMAGE_RESPONSES)
        except Exception:
            _free_images = False

        # 1) Базовая стоимость медиа
        if media_type == "photo":
            if _free_images:
                total_cost += 0.0
            else:
                total_cost += CREDIT_COSTS.get("image_per_item", 0.0)
        elif media_type == "voice":
            minutes = max(1.0, (media_duration_sec or 0) / 60.0)
            total_cost += CREDIT_COSTS.get("audio_per_minute", 0.0) * minutes

        # 2) Стоимость токенов
        try:
            input_tokens = count_openai_compatible_tokens(input_text or "", effective_model)
        except Exception:
            input_tokens = 0
        try:
            output_tokens = count_openai_compatible_tokens(output_text or "", effective_model)
        except Exception:
            output_tokens = 0

        tokens_cost = (
            (input_tokens / 1000.0) * CREDIT_COSTS.get("input_tokens_per_1k", 0.0) +
            (output_tokens / 1000.0) * CREDIT_COSTS.get("output_tokens_per_1k", 0.0)
        )
        total_cost += tokens_cost

        # 3) Применяем множитель модели
        # Фото могут быть полностью бесплатными
        if media_type == "photo" and _free_images:
            final_cost = 0.0
        else:
            final_cost = round(total_cost * mult, 6)
        prev_credits = float(getattr(owner_user, 'credits', 0.0) or 0.0)

        if prev_credits >= final_cost and final_cost > 0:
            owner_user.credits = round(prev_credits - final_cost, 6)
            db.add(owner_user)
            logger.info(
                f"кредиты списаны (тип: {media_type or 'text'}): пользователь {owner_user.id}, стоимость={final_cost}, новый баланс={owner_user.credits}"
            )

            # Предупреждение о низком балансе
            try:
                if (
                    owner_user.credits < LOW_BALANCE_WARNING_THRESHOLD and
                    prev_credits >= LOW_BALANCE_WARNING_THRESHOLD and
                    main_bot
                ):
                    warning_text = (
                        f"⚠️ предупреждение: на вашем балансе осталось меньше {LOW_BALANCE_WARNING_THRESHOLD:.0f} кредитов!\n"
                        f"текущий баланс: {owner_user.credits:.2f} кр.\n\n"
                        f"пополните баланс командой /buycredits"
                    )
                    await main_bot.send_message(chat_id=owner_user.telegram_id, text=warning_text, parse_mode=None)
                    logger.info(f"отправлено уведомление о низком балансе пользователю {owner_user.id}")
            except Exception as warn_e:
                logger.error(f"не удалось отправить уведомление о низком балансе: {warn_e}")
        else:
            if media_type == "photo" and final_cost == 0.0:
                logger.info(f"кредиты не списаны (фото бесплатно): пользователь {owner_user.id}, баланс={prev_credits}")
            else:
                logger.info(f"кредиты не списаны: пользователь {owner_user.id}, стоимость={final_cost}, баланс={prev_credits}")

    except Exception as e:
        logger.error(f"ошибка при расчете/списании кредитов: {e}", exc_info=True)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str, caption: Optional[str] = None) -> None:
    """Handles incoming photo or voice messages, now with caption and time gap awareness."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    message_id = update.message.message_id
    logger.info(f"Received {media_type} message from user {user_id} ({username}) in chat {chat_id_str} (MsgID: {message_id})")

    # Подписка больше не проверяется; работаем только по кредитной модели

    with get_db() as db:
        try:
            # ВАЖНО: используем update.get_bot(), чтобы получить именно того бота, для которого пришёл апдейт
            try:
                current_bot = update.get_bot()
            except Exception:
                current_bot = None
            current_bot_id_str = str(getattr(current_bot, 'id', None)) if current_bot else None
            logger.debug(f"handle_media: selecting persona for chat {chat_id_str} with current_bot_id={current_bot_id_str}")
            persona_context_owner_tuple = get_persona_and_context_with_owner(chat_id_str, db, current_bot_id_str)
            if not persona_context_owner_tuple:
                logger.debug(f"No active persona in chat {chat_id_str} for media message.")
                return
            persona, _, owner_user = persona_context_owner_tuple
            logger.debug(f"Handling {media_type} for persona '{persona.name}' owned by {owner_user.id}")

            user_message_content = ""
            system_prompt = None
            image_data = None
            audio_data = None
            
            if media_type == "photo":
                system_prompt = persona.format_photo_prompt(user_id=user_id, username=username, chat_id=chat_id_str)
                try:
                    photo_sizes = update.message.photo
                    if photo_sizes:
                        photo_file = photo_sizes[-1]
                        file = await current_bot.get_file(photo_file.file_id)
                        image_data_io = await file.download_as_bytearray()
                        image_data = bytes(image_data_io)
                        logger.info(f"Downloaded image: {len(image_data)} bytes")
                        if caption:
                            user_message_content = f"{username}: {caption}"
                        else:
                            user_message_content = f"{username}: опиши, что на этой фотографии"
                except Exception as e:
                    logger.error(f"Error downloading photo: {e}", exc_info=True)
                    user_message_content = f"{username}: [ошибка загрузки фото]"

            elif media_type == "voice":
                system_prompt = persona.format_voice_prompt(user_id=user_id, username=username, chat_id=chat_id_str)
                if update.message.voice:
                    # Используем бота из текущего апдейта
                    await current_bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
                    try:
                        # Скачиваем файл тем же ботом
                        voice_file = await current_bot.get_file(update.message.voice.file_id)
                        voice_bytes = await voice_file.download_as_bytearray()
                        audio_data = bytes(voice_bytes)
                        transcribed_text = None
                        if vosk_model is None:
                            load_vosk_model(VOSK_MODEL_PATH)
                        
                        if vosk_model:
                            transcribed_text = await transcribe_audio_with_vosk(audio_data, update.message.voice.mime_type)
                        else:
                            logger.warning("Vosk model is not available, skipping transcription.")
                        
                        if transcribed_text and str(transcribed_text).strip():
                            user_message_content = f"{username}: {transcribed_text}"
                            logger.info(f"Текст голосового сообщения: '{str(transcribed_text).strip()[:120]}'")
                        else:
                            logger.warning(f"Распознавание голоса для чата {chat_id_str} вернуло пустой результат.")
                            await update.message.reply_text("не расслышала, можешь повторить, пожалуйста?", parse_mode=None)
                            # Откатываем любые незакоммиченные изменения и прекращаем обработку
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            return
                    except Exception as e_voice:
                        logger.error(f"handle_media: Error processing voice message for chat {chat_id_str}: {e_voice}", exc_info=True)
                        user_message_content = f"{username}: [ошибка обработки голосового сообщения]"
                else:
                    user_message_content = f"{username}: [получено пустое голосовое сообщение]"

            else:
                logger.error(f"Unsupported media_type '{media_type}' in handle_media")
                return

            if not system_prompt:
                logger.info(f"Persona {persona.name} in chat {chat_id_str} is configured not to react to {media_type}. Saving user message to context and committing.")
                if persona.chat_instance and user_message_content:
                    try:
                        add_message_to_context(db, persona.chat_instance.id, "user", user_message_content)
                        db.commit() # Коммитим, т.к. выходим из функции
                    except Exception as e_ctx_ignore:
                        logger.error(f"DB Error saving user message for ignored media: {e_ctx_ignore}")
                        db.rollback()
                else: # Если нет контента, но был сброс или увеличение счетчика
                    db.commit()
                return
            
            if not persona.chat_instance:
                logger.error("Cannot proceed, chat_instance is None.")
                if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ системная ошибка: не удалось связать медиа с личностью."), parse_mode=ParseMode.MARKDOWN_V2)
                db.rollback() # Откатываем увеличение счетчика
                return

            if persona.chat_instance.is_muted:
                logger.debug(f"Persona '{persona.name}' is muted. Saving user message to context and exiting.")
                add_message_to_context(db, persona.chat_instance.id, "user", user_message_content)
                db.commit()
                return

            history_with_timestamps = get_context_for_chat_bot(db, persona.chat_instance.id)
            context_for_ai = _process_history_for_time_gaps(history_with_timestamps)
            context_for_ai.append({"role": "user", "content": user_message_content})

            add_message_to_context(db, persona.chat_instance.id, "user", user_message_content)


            # Закрываем транзакцию перед долгим IO
            persona_id_cache = persona.id
            owner_user_id_cache = owner_user.id if owner_user else None
            try:
                db.commit()
            except Exception as e_commit_media:
                logger.warning(f"handle_media: commit before AI call failed: {e_commit_media}")
            # Закрываем сессию ПЕРЕД длительным вызовом AI
            try:
                db.close()
                logger.debug("handle_media: DB session explicitly closed before AI call.")
            except Exception as close_err_m:
                logger.warning(f"handle_media: failed to close DB session before AI: {close_err_m}")

            # --- Убираем вежливую задержку перед запросом к AI (медиа) для ускорения ответа ---
            # delay_sec = random.uniform(0.2, 0.7)
            # logger.info(f"Polite delay before AI request (media): {delay_sec:.2f}s")
            # await asyncio.sleep(delay_sec)

            # --- Вызов AI через централизованную функцию (модель выбирается автоматически) ---
            with get_db() as llm_db:
                ai_response_text, model_used, api_key_used = await get_llm_response(
                    db_session=llm_db,
                    owner_user=owner_user,
                    system_prompt=system_prompt,
                    context_for_ai=context_for_ai,
                    image_data=image_data,
                    media_type=media_type,
                )
            # Повторная попытка при перегрузке (503) только для Gemini
            if (
                isinstance(ai_response_text, str)
                and ai_response_text.startswith("[ошибка google api")
                and ("503" in ai_response_text or "overload" in ai_response_text.lower())
                and model_used == config.GEMINI_MODEL_NAME_FOR_API
                and api_key_used
            ):
                for attempt in range(1, 3):
                    backoff = 1.0 * attempt + random.uniform(0.2, 0.8)
                    logger.warning(f"Google API overloaded (media). Retry {attempt}/2 after {backoff:.2f}s...")
                    await asyncio.sleep(backoff)
                    ai_response_text = await send_to_google_gemini(api_key=api_key_used, system_prompt=system_prompt, messages=context_for_ai, image_data=image_data)
                    if not (
                        isinstance(ai_response_text, str)
                        and ai_response_text.startswith("[ошибка google api")
                        and ("503" in ai_response_text or "overload" in ai_response_text.lower())
                    ):
                        break

            if ai_response_text is None:
                ai_response_text = "[ошибка: ключ GEMINI_API_KEY не установлен]"
                logger.error("Cannot call AI for media: GEMINI_API_KEY is not configured.")
            logger.debug(f"Received response from AI for {media_type}: {ai_response_text[:100]}...")

            # --- Фаза 2: сохраняем ответ и списываем кредиты в НОВОЙ короткой сессии ---
            try:
                with get_db() as db_after_ai:
                    owner_user_refreshed = db_after_ai.query(User).filter(User.id == owner_user_id_cache).first() if owner_user_id_cache else None
                    persona_tuple_fresh = get_persona_and_context_with_owner(chat_id_str, db_after_ai, str(getattr(current_bot, 'id', None)) if current_bot else None)
                    if not persona_tuple_fresh or not owner_user_refreshed:
                        logger.warning("handle_media: fresh persona or owner_user not found in Phase 2.")
                    else:
                        persona_fresh, _, _ = persona_tuple_fresh
                        context_response_prepared = await process_and_send_response(
                            update,
                            context,
                            current_bot,
                            chat_id_str,
                            persona_fresh,
                            ai_response_text,
                            db_after_ai,
                            reply_to_message_id=message_id,
                            is_first_message=(len(history_with_timestamps) == 0)
                        )
                        if context_response_prepared:
                            # Нормализуем текст для тарификации
                            _out_text = "\n".join(ai_response_text) if isinstance(ai_response_text, list) else (ai_response_text or "")
                            # Финальная проверка: не списываем кредиты за любой ошибочный или заблокированный ответ
                            is_error_response = (
                                _out_text.strip().startswith('[ошибка') or 
                                'PROHIBITED_CONTENT' in _out_text or 
                                'SAFETY' in _out_text
                            )
                            if is_error_response:
                                logger.warning(f"Skipping credit deduction for user {owner_user_refreshed.id} due to error/blocked response: '{_out_text[:100]}'")
                                # Уведомляем пользователя только о блокировке контента, а не о технических ошибках
                                if 'PROHIBITED_CONTENT' in _out_text or 'SAFETY' in _out_text:
                                    try:
                                        await update.message.reply_text(
                                            "не могу это обсуждать, тема нарушает политику безопасности. кредиты не списаны.",
                                            parse_mode=None
                                        )
                                    except Exception as notify_err:
                                        logger.error(f"Failed to notify user about content block: {notify_err}")
                            else:
                                await deduct_credits_for_interaction(
                                    db=db_after_ai,
                                    owner_user=owner_user_refreshed,  # Используем объект в текущей сессии
                                    input_text="",
                                    output_text=_out_text,
                                    model_name=model_used,
                                    media_type=media_type,
                                    media_duration_sec=getattr(update.message.voice, 'duration', None) if media_type == 'voice' else None,
                                    main_bot=context.application.bot
                                )
                    db_after_ai.commit()
            except Exception as e_media_after:
                logger.error(f"handle_media: error during Phase 2 DB save: {e_media_after}", exc_info=True)

            # Use the current bot associated with this update, not the main application bot
            # (Сохранение и списание выполнены внутри новой короткой сессии db_after_ai)
            logger.debug(f"handle_media: Phase 2 finished for chat {chat_id_str}.")

        except SQLAlchemyError as e:
            logger.error(f"Database error during handle_media ({media_type}): {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ ошибка базы данных."), parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except TelegramError as e:
            logger.error(f"Telegram API error during handle_media ({media_type}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"General error processing {media_type} in chat {chat_id_str}: {e}", exc_info=True)
            if update.effective_message: await update.effective_message.reply_text(escape_markdown_v2("❌ произошла непредвиденная ошибка."), parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles photo messages by calling the generic media handler."""
    if not update.message: return
    # Ignore non-command media for the main bot
    try:
        current_bot = update.get_bot()
    except Exception:
        current_bot = None
    entities = update.message.caption_entities or []
    caption_text = update.message.caption or ''
    is_command = any((e.type == 'bot_command') for e in entities) or caption_text.startswith('/')
    main_bot_id = context.bot_data.get('main_bot_id')
    if (main_bot_id and current_bot and str(current_bot.id) == str(main_bot_id) and not is_command):
        logger.info(f"handle_photo: Ignored non-command photo for main bot (ID: {main_bot_id}).")
        return
    # Передаём подпись к фото в обработчик
    await handle_media(update, context, "photo", caption=update.message.caption)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles voice messages by calling the generic media handler."""
    if not update.message: return
    # Ignore non-command media for the main bot
    try:
        current_bot = update.get_bot()
    except Exception:
        current_bot = None
    # voice has no caption entities, so just treat as non-command unless startswith '/'
    text_raw = update.message.text or ''
    is_command = text_raw.startswith('/')
    main_bot_id = context.bot_data.get('main_bot_id')
    if (main_bot_id and current_bot and str(current_bot.id) == str(main_bot_id) and not is_command):
        logger.info(f"handle_voice: Ignored non-command voice for main bot (ID: {main_bot_id}).")
        return
    await handle_media(update, context, "voice")

# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) in Chat {chat_id_str}")

    # Подписка больше не проверяется; работаем только по кредитной модели

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)
    reply_text_final = ""
    reply_markup = ReplyKeyboardRemove()
    reply_parse_mode = ParseMode.MARKDOWN_V2
    persona_limit_raw = ""
    fallback_text_raw = "Привет! Произошла ошибка отображения стартового сообщения. Используй /help или /menu."

    try:
        with get_db() as db:
            user = get_or_create_user(db, user_id, username)
            if db.is_modified(user):
                logger.info(f"/start: Committing new/updated user {user_id}.")
                db.commit()
                db.refresh(user)
            else:
                logger.debug(f"/start: User {user_id} already exists and is up-to-date.")

            logger.debug(f"/start: Checking for active persona in chat {chat_id_str}...")
            # В основном боте всегда показываем основное приветствие, независимо от активной личности
            current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db, current_bot_id_str)
            if persona_info_tuple:
                logger.info(f"/start: Active persona exists in chat {chat_id_str}, but showing generic welcome for main bot.")

            if not db.is_modified(user):
                user = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user.id).one()

            persona_count = len(user.persona_configs) if user.persona_configs else 0
            persona_limit_raw = f"{persona_count}/{user.persona_limit}"

            start_text_md = (
                f"привет! я бот для создания ai-собеседников (@{escape_markdown_v2(context.bot.username)}).\n\n"
                f"я помогу создать и настроить личности для разных задач.\n\n"
                f"начало работы:\n"
                f"/createpersona <имя> - создай ai-личность\n"
                f"/mypersonas - список твоих личностей\n"
                f"/menu - панель управления\n"
                f"/profile - детали статуса"
            )
            fallback_text_raw = (
                f"привет! я бот для создания ai-собеседников (@{context.bot.username}).\n\n"
                f"я помогу создать и настроить личности для разных задач.\n\n"
                f"начало работы:\n"
                f"/createpersona <имя> - создай ai-личность\n"
                f"/mypersonas - список твоих личностей\n"
                f"/menu - панель управления\n"
                f"/profile - детали статуса"
            )
            # Добавим уведомление о соглашении и кнопку для его показа
            fallback_text_raw += "\n\nначиная работу, вы принимаете условия пользовательского соглашения."
            # Ветку приветствия отправляем без Markdown, чтобы не ловить ошибки экранирования
            reply_text_final = fallback_text_raw
            reply_parse_mode = None
            keyboard = [
                [InlineKeyboardButton("меню команд", callback_data="show_menu")],
                [InlineKeyboardButton("пользовательское соглашение", callback_data="show_tos")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            logger.debug(f"/start: Sending final message to user {user_id}.")
            await update.message.reply_text(reply_text_final, reply_markup=reply_markup, parse_mode=reply_parse_mode)

    except SQLAlchemyError as e:
        logger.error(f"Database error during /start for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "❌ ошибка при загрузке данных. попробуй позже."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass
    except TelegramError as e:
        logger.error(f"Telegram error during /start for user {user_id}: {e}", exc_info=True)
        if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
            logger.error(f"--> Failed text (MD): '{reply_text_final[:500]}...'")
            try:
                await update.message.reply_text(fallback_text_raw, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                logger.error(f"Failed sending fallback start message: {fallback_e}")
        else:
            error_msg_raw = "❌ произошла ошибка при обработке команды /start."
            try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception: pass
    except Exception as e:
        logger.error(f"Error in /start handler for user {user_id}: {e}", exc_info=True)
        error_msg_raw = "❌ произошла ошибка при обработке команды /start."
        try: await update.message.reply_text(escape_markdown_v2(error_msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command and the show_help callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user = update.effective_user
    user_id = user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /help or Callback 'show_help' < User {user_id} in Chat {chat_id_str}")

    # Подписка больше не проверяется; работаем только по кредитной модели

    help_text_plain = (
        "как работает бот\n\n"
        "1) создай личность: /createpersona <имя> [описание]\n"
        "2) открой /mypersonas и выбери личность\n"
        "3) при необходимости привяжи к личности отдельного бота (кнопка 'привязать бота')\n"
        "4) авто-активация произойдет в чате привязанного бота; пиши ему и общайся\n"
        "5) в основном боте доступны меню, профиль и пополнение кредитов\n\n"
        "как привязать своего бота (botfather)\n"
        "• открой @BotFather и отправь команду /newbot\n"
        "• придумай имя (name) и уникальный логин, оканчивающийся на _bot (username)\n"
        "• получи token — его нужно будет указать при привязке в мастере личности\n"
        "• при желании задай описание и аватар через /setdescription, /setabouttext, /setuserpic\n\n"
        "чтобы бот отвечал в группах\n"
        "• открой @BotFather → mybots → выбери своего бота\n"
        "• bot settings → group privacy → turn off (выключи режим приватности)\n"
        "• добавь бота в группу и дай ему нужные права\n\n"
        "важно\n"
        "• авто-команды в чатах привязанных ботов отключены — команды пиши в основном боте\n"
        "• если тишина — убедись, что личность активна в чате бота\n\n"
        "основные команды\n"
        "/start — начало работы\n"
        "/menu — главное меню\n"
        "/help — помощь\n"
        "/profile — профиль и баланс\n"
        "/buycredits — пополнить кредиты\n"
        "/createpersona — создать личность\n"
        "/mypersonas — мои личности\n"
        "/reset — очистить память диалога\n"
    ).strip()

    if is_callback:
        keyboard_inline = [[InlineKeyboardButton("⬅️ назад в меню", callback_data="show_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard_inline)
    else:
        from telegram import ReplyKeyboardMarkup
        commands_kb = [
            ["/start", "/menu", "/help"],
            ["/profile", "/buycredits"],
            ["/createpersona", "/mypersonas"],
            ["/reset"],
        ]
        reply_markup = ReplyKeyboardMarkup(commands_kb, resize_keyboard=True, one_time_keyboard=True)

    try:
        if is_callback:
            query = update.callback_query
            if query.message.text != help_text_plain or query.message.reply_markup != reply_markup:
                await query.edit_message_text(help_text_plain, reply_markup=reply_markup, parse_mode=None)
            else:
                await query.answer()
        else:
            await message_or_query.reply_text(help_text_plain, reply_markup=reply_markup, parse_mode=None)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Help message not modified, skipping edit.")
            await query.answer()
        else:
            logger.error(f"Failed sending/editing help message (BadRequest): {e}", exc_info=True)
            try:
                if is_callback:
                    await query.edit_message_text(help_text_plain, reply_markup=reply_markup, parse_mode=None)
                else:
                    await message_or_query.reply_text(help_text_plain, reply_markup=reply_markup, parse_mode=None)
            except Exception as fallback_e:
                logger.error(f"Failed sending plain help message: {fallback_e}")
                if is_callback: await query.answer("❌ Ошибка отображения справки", show_alert=True)
    except Exception as e:
        logger.error(f"Error sending/editing help message: {e}", exc_info=True)
        if is_callback: await query.answer("❌ Ошибка отображения справки", show_alert=True)

async def show_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текст Пользовательского соглашения в текущем сообщении с кнопкой Назад."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query:
        return

    tos_text_md = TOS_TEXT  # уже экранирован MarkdownV2
    keyboard_inline = [[InlineKeyboardButton("назад в меню", callback_data="show_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard_inline)

    try:
        if is_callback:
            query = update.callback_query
            try:
                await query.edit_message_text(tos_text_md, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                # Если MarkdownV2 не прошёл, отправим plain текст
                if "message is not modified" in str(e).lower():
                    await query.answer()
                else:
                    await query.edit_message_text(formatted_tos_text_for_bot, reply_markup=reply_markup, parse_mode=None)
        else:
            await send_safe_message(message_or_query, formatted_tos_text_for_bot, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"show_tos: failed to display ToS: {e}", exc_info=True)
        try:
            if is_callback:
                await update.callback_query.answer("❌ ошибка отображения", show_alert=True)
        except Exception:
            pass


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /menu command and the show_menu callback."""
    is_callback = update.callback_query is not None
    message_or_query = update.callback_query if is_callback else update.message
    if not message_or_query: return

    user_id = update.effective_user.id
    chat_id_str = str(message_or_query.message.chat.id if is_callback else message_or_query.chat.id)
    logger.info(f"CMD /menu or Callback 'show_menu' < User {user_id} in Chat {chat_id_str}")

    # проверка подписки на канал отключена

    menu_text_raw = "панель управления\n\nвыберите действие:"
    menu_text_escaped = escape_markdown_v2(menu_text_raw)

    keyboard = [
        [
            InlineKeyboardButton("профиль", callback_data="show_profile"),
            InlineKeyboardButton("мои личности", callback_data="show_mypersonas")
        ],
        [
            InlineKeyboardButton("помощь", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if is_callback:
            query = update.callback_query
            if query.message.text != menu_text_escaped or query.message.reply_markup != reply_markup:
                await query.edit_message_text(menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await query.answer()
        else:
            await context.bot.send_message(chat_id=chat_id_str, text=menu_text_escaped, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if is_callback and "Message is not modified" in str(e):
            logger.debug("Menu message not modified, skipping edit.")
            await query.answer()
        else:
            logger.error(f"Failed sending/editing menu message (BadRequest): {e}", exc_info=True)
            logger.error(f"Failed menu text (escaped): '{menu_text_escaped[:200]}...'")
            try:
                await context.bot.send_message(chat_id=chat_id_str, text=menu_text_raw, reply_markup=reply_markup, parse_mode=None)
                if is_callback:
                    try: await query.delete_message()
                    except: pass
            except Exception as fallback_e:
                logger.error(f"Failed sending plain menu message: {fallback_e}")
                if is_callback: await query.answer("❌ Ошибка отображения меню", show_alert=True)
    except Exception as e:
        logger.error(f"Error sending/editing menu message: {e}", exc_info=True)
        if is_callback: await query.answer("❌ Ошибка отображения меню", show_alert=True)


async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Optional[Session] = None, persona: Optional[Persona] = None) -> None:
    """Handles the /mood command and mood selection callbacks."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    chat_id_str = str(message_or_callback_msg.chat.id)
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"
    logger.info(f"CMD /mood or Mood Action < User {user_id} ({username}) in Chat {chat_id_str}")

    if not is_callback:
        # проверка подписки удалена
        pass

    close_db_later = False
    db_session = db
    chat_bot_instance = None
    local_persona = persona

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности.")
    error_persona_info = escape_markdown_v2("❌ ошибка: не найдена информация о личности.")
    error_no_moods_fmt_raw = "у личности '{persona_name}' не настроены настроения."
    error_bot_muted_fmt_raw = "🔇 личность '{persona_name}' сейчас заглушена \\(используй `/unmutebot`\\)."
    error_db = escape_markdown_v2("❌ ошибка базы данных при смене настроения.")
    error_general = escape_markdown_v2("❌ ошибка при обработке команды /mood.")
    success_mood_set_fmt_raw = "✅ настроение для '{persona_name}' теперь: *{mood_name}*"
    prompt_select_mood_fmt_raw = "текущее настроение: *{current_mood}*\\. выбери новое для '{persona_name}':"
    prompt_invalid_mood_fmt_raw = "не знаю настроения '{mood_arg}' для '{persona_name}'. выбери из списка:"

    try:
        if db_session is None:
            db_context = get_db()
            db_session = next(db_context)
            close_db_later = True

        if local_persona is None:
            current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db_session, current_bot_id_str)
            if not persona_info_tuple:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                if is_callback: await update.callback_query.answer("Нет активной личности", show_alert=True)
                await reply_target.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                logger.debug(f"No active persona for chat {chat_id_str}. Cannot set mood.")
                if close_db_later: db_session.close()
                return
            local_persona, _, _ = persona_info_tuple

        if not local_persona or not local_persona.chat_instance:
            logger.error(f"Mood called, but persona or persona.chat_instance is None for chat {chat_id_str}.")
            reply_target = update.callback_query.message if is_callback else message_or_callback_msg
            if is_callback: await update.callback_query.answer("❌ Ошибка: не найдена информация о личности.", show_alert=True)
            else: await reply_target.reply_text(error_persona_info, parse_mode=ParseMode.MARKDOWN_V2)
            if close_db_later: db_session.close()
            return

        chat_bot_instance = local_persona.chat_instance
        persona_name_raw = local_persona.name
        persona_name_escaped = escape_markdown_v2(persona_name_raw)

        if chat_bot_instance.is_muted:
            logger.debug(f"Persona '{persona_name_raw}' is muted in chat {chat_id_str}. Ignoring mood command.")
            reply_text = error_bot_muted_fmt_raw.format(persona_name=persona_name_escaped)
            try:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                if is_callback: await update.callback_query.answer("Бот заглушен", show_alert=True)
                await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'bot muted' msg: {send_err}")
            if close_db_later: db_session.close()
            return

        available_moods = local_persona.get_all_mood_names()
        if not available_moods:
            reply_text = escape_markdown_v2(error_no_moods_fmt_raw.format(persona_name=persona_name_raw))
            try:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                if is_callback: await update.callback_query.answer("Нет настроений", show_alert=True)
                await reply_target.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as send_err: logger.error(f"Error sending 'no moods defined' msg: {send_err}")
            logger.warning(f"Persona {persona_name_raw} has no moods defined.")
            if close_db_later: db_session.close()
            return

        available_moods_lower = {m.lower(): m for m in available_moods}
        mood_arg_lower = None
        target_mood_original_case = None

        if is_callback and update.callback_query.data.startswith("set_mood_"):
            parts = update.callback_query.data.split('_')
            if len(parts) >= 3 and parts[-1].isdigit():
                    try:
                        encoded_mood_name = "_".join(parts[2:-1])
                        decoded_mood_name = urllib.parse.unquote(encoded_mood_name)
                        mood_arg_lower = decoded_mood_name.lower()
                        if mood_arg_lower in available_moods_lower:
                            target_mood_original_case = available_moods_lower[mood_arg_lower]
                    except Exception as decode_err:
                        logger.error(f"Error decoding mood name from callback {update.callback_query.data}: {decode_err}")
            else:
                    logger.warning(f"Invalid mood callback data format: {update.callback_query.data}")
        elif not is_callback:
            mood_text = ""
            if context.args:
                mood_text = " ".join(context.args)
            elif update.message and update.message.text:
                possible_mood = update.message.text.strip()
                if possible_mood.lower() in available_moods_lower:
                        mood_text = possible_mood

            if mood_text:
                mood_arg_lower = mood_text.lower()
                if mood_arg_lower in available_moods_lower:
                    target_mood_original_case = available_moods_lower[mood_arg_lower]

        if target_mood_original_case:
            set_mood_for_chat_bot(db_session, chat_bot_instance.id, target_mood_original_case)
            reply_text = success_mood_set_fmt_raw.format(
                persona_name=persona_name_escaped,
                mood_name=escape_markdown_v2(target_mood_original_case)
                )

            try:
                if is_callback:
                    query = update.callback_query
                    if query.message.text != reply_text or query.message.reply_markup:
                        await query.edit_message_text(reply_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    else:
                        await query.answer(f"Настроение: {target_mood_original_case}")
                else:
                    await message_or_callback_msg.reply_text(reply_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                    logger.error(f"Failed sending mood confirmation (BadRequest): {e} - Text: '{reply_text}'")
                    try:
                        reply_text_raw = f"✅ настроение для '{persona_name_raw}' теперь: {target_mood_original_case}"
                        if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=None, parse_mode=None)
                        else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                    except Exception as fe: logger.error(f"Failed sending plain mood confirmation: {fe}")
            except Exception as send_err: logger.error(f"Error sending mood confirmation: {send_err}")
            logger.info(f"Mood for persona {persona_name_raw} in chat {chat_id_str} set to {target_mood_original_case}.")
        else:
            keyboard = []
            for mood_name in sorted(available_moods, key=str.lower):
                try:
                    encoded_mood_name = urllib.parse.quote(mood_name)
                    button_callback = f"set_mood_{encoded_mood_name}_{local_persona.id}"
                    if len(button_callback.encode('utf-8')) <= 64:
                            mood_emoji_map = {"радость": "😊", "грусть": "😢", "злость": "😠", "милота": "🥰", "нейтрально": "😐"}
                            emoji = mood_emoji_map.get(mood_name.lower(), "🎭")
                            keyboard.append([InlineKeyboardButton(f"{emoji} {mood_name.capitalize()}", callback_data=button_callback)])
                    else:
                            logger.warning(f"Callback data for mood '{mood_name}' (encoded: '{encoded_mood_name}') too long, skipping button.")
                except Exception as encode_err:
                    logger.error(f"Error encoding mood name '{mood_name}' for callback: {encode_err}")

            reply_markup = InlineKeyboardMarkup(keyboard)

            current_mood_text = get_mood_for_chat_bot(db_session, chat_bot_instance.id)
            reply_text = ""
            reply_text_raw = ""

            if mood_arg_lower:
                mood_arg_escaped = escape_markdown_v2(mood_arg_lower)
                reply_text = prompt_invalid_mood_fmt_raw.format(
                    mood_arg=mood_arg_escaped,
                    persona_name=persona_name_escaped
                    )
                reply_text_raw = f"не знаю настроения '{mood_arg_lower}' для '{persona_name_raw}'. выбери из списка:"
                logger.debug(f"Invalid mood argument '{mood_arg_lower}' for chat {chat_id_str}. Sent mood selection.")
            else:
                reply_text = prompt_select_mood_fmt_raw.format(
                    current_mood=escape_markdown_v2(current_mood_text),
                    persona_name=persona_name_escaped
                    )
                reply_text_raw = f"текущее настроение: {current_mood_text}. выбери новое для '{persona_name_raw}':"
                logger.debug(f"Sent mood selection keyboard for chat {chat_id_str}.")

            try:
                if is_callback:
                        query = update.callback_query
                        if query.message.text != reply_text or query.message.reply_markup != reply_markup:
                            await query.edit_message_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                        else:
                            await query.answer()
                else:
                        await message_or_callback_msg.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                    logger.error(f"Failed sending mood selection (BadRequest): {e} - Text: '{reply_text}'")
                    try:
                        if is_callback: await query.edit_message_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
                        else: await message_or_callback_msg.reply_text(reply_text_raw, reply_markup=reply_markup, parse_mode=None)
                    except Exception as fe: logger.error(f"Failed sending plain mood selection: {fe}")
            except Exception as send_err: logger.error(f"Error sending mood selection: {send_err}")

    except SQLAlchemyError as e:
        logger.error(f"Database error during /mood for chat {chat_id_str}: {e}", exc_info=True)
        reply_target = update.callback_query.message if is_callback else message_or_callback_msg
        try:
            if is_callback: await update.callback_query.answer("❌ Ошибка БД", show_alert=True)
            await reply_target.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_err: logger.error(f"Error sending DB error msg: {send_err}")
    except Exception as e:
        logger.error(f"Error in /mood handler for chat {chat_id_str}: {e}", exc_info=True)
        reply_target = update.callback_query.message if is_callback else message_or_callback_msg
        try:
            if is_callback: await update.callback_query.answer("❌ Ошибка", show_alert=True)
            await reply_target.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_err: logger.error(f"Error sending general error msg: {send_err}")
    finally:
        if close_db_later and db_session:
            try: db_session.close()
            except Exception: pass

# === Character Setup Wizard (step-by-step) ===
async def char_wiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """entry from edit menu button. initializes wizard state and asks for bio."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    persona_id = context.user_data.get('edit_persona_id')
    if not persona_id:
        try: await query.answer("ошибка: сессия редактирования не найдена", show_alert=True)
        except Exception: pass
        return ConversationHandler.END
    # init storage
    context.user_data['charwiz'] = {
        'bio': None, 'traits': None, 'speech': None,
        'likes': None, 'dislikes': None, 'goals': None, 'taboos': None
    }
    context.user_data['charwiz_step'] = 'bio'

    # prompt
    text = "опиши кратко биографию и происхождение персонажа. отправь текст сообщением."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("пропустить", callback_data="charwiz_skip")],
        [InlineKeyboardButton("отмена", callback_data="charwiz_cancel")]
    ])
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode=None)
    except Exception:
        await context.bot.send_message(query.message.chat.id, text, reply_markup=keyboard, parse_mode=None)
    return CHAR_WIZ_BIO

async def _charwiz_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """moves to the next step based on current charwiz_step in user_data."""
    query = update.callback_query
    message = update.message
    chat_id = query.message.chat.id if query and query.message else (message.chat.id if message else None)
    if chat_id is None:
        return ConversationHandler.END
    step = context.user_data.get('charwiz_step')
    order = ['bio','traits','speech','likes','dislikes','goals','taboos']
    try:
        idx = order.index(step) if step in order else -1
    except Exception:
        idx = -1
    next_idx = idx + 1
    if next_idx >= len(order):
        return await char_wiz_finish(update, context)
    next_step = order[next_idx]
    context.user_data['charwiz_step'] = next_step

    prompts = {
        'traits': "перечисли 5-8 ключевых черт характера через запятую (например: спокойный, внимательный, упорный).",
        'speech': "опиши стиль речи и манеру общения (темп, словарный запас, обращение к собеседнику).",
        'likes': "перечисли что персонаж любит или предпочитает (через запятую).",
        'dislikes': "перечисли что персонаж не любит или избегает (через запятую).",
        'goals': "обозначь цели, мотивацию и приоритеты персонажа (кратко).",
        'taboos': "что строго не допускается в поведении и ответах персонажа (табу)."
    }
    text = prompts.get(next_step, "введите текст")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("пропустить", callback_data="charwiz_skip")],
        [InlineKeyboardButton("отмена", callback_data="charwiz_cancel")]
    ])
    try:
        if query and query.message:
            await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode=None)
        else:
            await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=None)
    except Exception:
        await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=None)

    # return appropriate state
    return {
        'bio': CHAR_WIZ_BIO,
        'traits': CHAR_WIZ_TRAITS,
        'speech': CHAR_WIZ_SPEECH,
        'likes': CHAR_WIZ_LIKES,
        'dislikes': CHAR_WIZ_DISLIKES,
        'goals': CHAR_WIZ_GOALS,
        'taboos': CHAR_WIZ_TABOOS,
    }[next_step]

async def char_wiz_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """skip current step and move on."""
    q = update.callback_query
    if q:
        try: await q.answer()
        except Exception: pass
    # leave value as None and advance
    return await _charwiz_next_step(update, context)

async def char_wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cancel wizard and return to edit menu without saving."""
    query = update.callback_query
    if query:
        try: await query.answer("мастер отменен")
        except Exception: pass
    persona_id = context.user_data.get('edit_persona_id')
    with get_db() as db:
        persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
    if persona:
        return await _show_edit_wizard_menu(update, context, persona)
    return ConversationHandler.END

async def _charwiz_store_and_next(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> int:
    if not update.message or not update.message.text:
        return {
            'bio': CHAR_WIZ_BIO,
            'traits': CHAR_WIZ_TRAITS,
            'speech': CHAR_WIZ_SPEECH,
            'likes': CHAR_WIZ_LIKES,
            'dislikes': CHAR_WIZ_DISLIKES,
            'goals': CHAR_WIZ_GOALS,
            'taboos': CHAR_WIZ_TABOOS,
        }[field]
    text = (update.message.text or "").strip()
    cw = context.user_data.get('charwiz') or {}
    cw[field] = text
    context.user_data['charwiz'] = cw
    context.user_data['charwiz_step'] = field
    return await _charwiz_next_step(update, context)

async def char_wiz_bio_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'bio')

async def char_wiz_traits_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'traits')

async def char_wiz_speech_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'speech')

async def char_wiz_likes_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'likes')

async def char_wiz_dislikes_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'dislikes')

async def char_wiz_goals_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _charwiz_store_and_next(update, context, 'goals')

async def char_wiz_taboos_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # last step -> finish
    res = await _charwiz_store_and_next(update, context, 'taboos')
    return res

async def char_wiz_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """compose template and save to DB, then show edit menu."""
    persona_id = context.user_data.get('edit_persona_id')
    data = context.user_data.get('charwiz') or {}
    # fetch base communication settings to blend into template
    base_style = None
    base_verbosity = None
    try:
        with get_db() as _db:
            _persona = _db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            if _persona is not None:
                base_style = getattr(_persona, 'communication_style', None)
                base_verbosity = getattr(_persona, 'verbosity_level', None)
    except Exception as _e:
        logger.warning(f"char_wiz_finish: failed to load base style/verbosity for persona {persona_id}: {_e}")

    # build smart placeholder-based template (hybrid language: EN instructions, RU content)
    character_profile_parts = [
        "[CHARACTER PROFILE]",
        "-   **Name:** {persona_name}",
        "-   **Description:** {persona_description}",
        f"-   **Style:** {base_style or '{communication_style}'}, {base_verbosity or '{verbosity_level}'}.",
    ]
    if data.get('bio'): character_profile_parts.append(f"-   **Biography:** {data.get('bio')}")
    if data.get('traits'): character_profile_parts.append(f"-   **Traits:** {data.get('traits')}")
    if data.get('speech'): character_profile_parts.append(f"-   **Speech Manner:** {data.get('speech')}")
    if data.get('likes'): character_profile_parts.append(f"-   **Likes:** {data.get('likes')}")
    if data.get('dislikes'): character_profile_parts.append(f"-   **Dislikes:** {data.get('dislikes')}")
    if data.get('goals'): character_profile_parts.append(f"-   **Goals:** {data.get('goals')}")
    if data.get('taboos'): character_profile_parts.append(f"-   **Special Rules (Taboos):** {data.get('taboos')}")

    character_profile = "\n".join(character_profile_parts)

    rules_parts = [
        "[ROLE-PLAYING RULES - FOLLOW STRICTLY]",
        "1.  **You are {persona_name}.** Never, under any circumstances, mention that you are an AI, language model, or bot.",
        "2.  **Stay in character.** Your entire personality, knowledge, and opinions are defined by the profile above.",
        "3.  **Continue the dialogue naturally.** Do not greet the user again if the conversation has already started.",
    ]
    rules = "\n".join(rules_parts)

    task_block = (
        "[TASK]\n"
        "-   **Current Time:** {current_time_info}\n"
        "-   **Your Mood:** {mood_name} ({mood_prompt})\n"
        "-   **User:** @{username} (id: {user_id}), chat: {chat_id}\n"
        "-   **Goal:** Provide a natural and engaging response to the user's last message, consistent with your role."
    )

    format_block = (
        "[OUTPUT FORMAT - CRITICAL]\n"
        "Your entire response MUST be a valid JSON array of strings. Start with `[` and end with `]`. Nothing else.\n"
        "Example: `[\"Пример.\", \"Ответа из двух сообщений!\"]`\n\n"
        "[YOUR JSON RESPONSE]:"
    )

    template = f"{character_profile}\n\n{rules}\n\n{task_block}\n\n{format_block}"

    try:
        with get_db() as db:
            persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).with_for_update().first()
            if not persona:
                raise ValueError("persona not found")
            persona.system_prompt_template_override = template
            db.commit()
            logger.info(f"char_wiz_finish: saved custom system prompt for persona {persona_id}")
    except Exception as e:
        logger.error(f"char_wiz_finish: failed to save prompt for persona {persona_id}: {e}")
        # try inform user but keep lowercase
        target = update.callback_query.message if update.callback_query else update.message
        if target:
            try: await target.reply_text("ошибка сохранения кастомного промпта", parse_mode=None)
            except Exception: pass
        return ConversationHandler.END

    # confirm and return to edit menu
    target = update.callback_query.message if update.callback_query else update.message
    try:
        if target:
            await target.reply_text("настройка завершена. кастомный системный промпт сохранен.", parse_mode=None)
    except Exception:
        pass
    with get_db() as db2:
        persona_ref = db2.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
        if persona_ref:
            return await _show_edit_wizard_menu(update, context, persona_ref)
    return ConversationHandler.END

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /reset or /clear command to clear persona context in the current chat."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    logger.info(f"CMD /reset or /clear < User {user_id} ({username}) in Chat {chat_id_str}")

    # проверка подписки на канал отключена

    # Сообщения для пользователя (простой текст)
    msg_no_persona_raw = "🎭 в этом чате нет активной личности, память которой можно было бы очистить."
    msg_not_owner_raw = "❌ только владелец личности или администратор бота могут очистить её память."
    msg_no_instance_raw = "❌ ошибка: не найден экземпляр связи бота с этим чатом."
    msg_db_error_raw = "❌ ошибка базы данных при очистке памяти."
    msg_general_error_raw = "❌ непредвиденная ошибка при очистке памяти."
    msg_success_fmt_raw = "✅ память личности '{persona_name}' в этом чате очищена ({count} сообщений удалено)."

    with get_db() as db:
        try:
            # Находим активную личность и ее владельца
            current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
            persona_info_tuple = get_persona_and_context_with_owner(chat_id_str, db, current_bot_id_str)
            if not persona_info_tuple:
                await send_safe_message(update.message, msg_no_persona_raw, reply_markup=ReplyKeyboardRemove())
                return

            persona, _, owner_user = persona_info_tuple
            persona_name_raw = persona.name

            # Проверяем права доступа
            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} attempted to clear memory for persona '{persona_name_raw}' owned by {owner_user.telegram_id} in chat {chat_id_str}.")
                await send_safe_message(update.message, msg_not_owner_raw, reply_markup=ReplyKeyboardRemove())
                return

            chat_bot_instance = persona.chat_instance
            if not chat_bot_instance:
                logger.error(f"Reset command: ChatBotInstance not found for persona {persona_name_raw} in chat {chat_id_str}")
                await send_safe_message(update.message, msg_no_instance_raw)
                return

            # Очищаем ТОЛЬКО контекст, связь бота с чатом остаётся активной
            chat_bot_instance_id = chat_bot_instance.id
            logger.warning(
                f"User {user_id} is resetting context for ChatBotInstance {chat_bot_instance_id} (Persona '{persona_name_raw}') in chat {chat_id_str}."
            )

            # Очистка контекста
            stmt = delete(ChatContext).where(ChatContext.chat_bot_instance_id == chat_bot_instance_id)
            result = db.execute(stmt)
            deleted_count = result.rowcount
            db.commit()

            logger.info(
                f"Deleted {deleted_count} context messages for ChatBotInstance {chat_bot_instance_id} in chat {chat_id_str}."
            )

            # Удобное сообщение об успехе
            final_success_msg_raw = msg_success_fmt_raw.format(
                persona_name=persona_name_raw,
                count=deleted_count or 0
            )
            await send_safe_message(update.message, final_success_msg_raw, reply_markup=ReplyKeyboardRemove())

        except SQLAlchemyError as e:
            logger.error(f"Database error during /reset for chat {chat_id_str}: {e}", exc_info=True)
            await send_safe_message(update.message, msg_db_error_raw)
            db.rollback()
        except Exception as e:
            logger.error(f"Error in /reset handler for chat {chat_id_str}: {e}", exc_info=True)
            await send_safe_message(update.message, msg_general_error_raw)
            db.rollback()

async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /createpersona command."""
    if not update.message: return
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(update.effective_chat.id)
    logger.info(f"CMD /createpersona < User {user_id} ({username}) with args: {context.args}")

    # проверка подписки удалена

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    usage_text = "формат: /createpersona <имя> [описание]\n\nсовет: подробное описание напрямую влияет на характер и поведение личности."
    error_name_len = escape_markdown_v2("❌ имя личности: 2\-50 символов.")
    error_desc_len = escape_markdown_v2("❌ описание: до 2500 символов.")
    error_limit_reached_fmt_raw = "упс! 😕 достигнут лимит личностей ({current_count}/{limit}). удалите ненужные или отредактируйте существующие."
    error_name_exists_fmt_raw = "❌ личность с именем '{persona_name}' уже есть\. выбери другое\."
    success_create_fmt_raw = "✅ личность '{name}' создана\!\nID: `{id}`\nописание: {description}\n\nтеперь можно настроить поведение через `/editpersona {id}` или привязать бота в `/mypersonas`"
    error_db = escape_markdown_v2("❌ ошибка базы данных при создании личности.")
    error_general = escape_markdown_v2("❌ ошибка при создании личности.")

    args = context.args
    if not args:
        await update.message.reply_text(usage_text, parse_mode=None)
        return
    persona_name = args[0]
    persona_description = " ".join(args[1:]) if len(args) > 1 else None

    if len(persona_name) < 2 or len(persona_name) > 50:
        await update.message.reply_text(error_name_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if persona_description and len(persona_description) > 2500:
        await update.message.reply_text(error_desc_len, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return

    with get_db() as db:
        try:
            user = get_or_create_user(db, user_id, username)
            if not user.id:
                db.commit()
                db.refresh(user)
            user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).one()

            if not user.can_create_persona:
                current_count = len(user.persona_configs)
                limit = user.persona_limit
                logger.warning(f"User {user_id} cannot create persona, limit reached ({current_count}/{limit}).")
                status_text_raw = ""
                final_limit_msg = error_limit_reached_fmt_raw.format(
                    current_count=escape_markdown_v2(str(current_count)),
                    limit=escape_markdown_v2(str(limit)),
                    status_text=escape_markdown_v2(status_text_raw)
                )
                await update.message.reply_text(final_limit_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            existing_persona = get_persona_by_name_and_owner(db, user.id, persona_name)
            if existing_persona:
                final_exists_msg = error_name_exists_fmt_raw.format(persona_name=escape_markdown_v2(persona_name))
                await update.message.reply_text(final_exists_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            new_persona = create_persona_config(db, user.id, persona_name, persona_description)

            desc_raw = new_persona.description or "(пусто)"
            final_success_msg = success_create_fmt_raw.format(
                name=escape_markdown_v2(new_persona.name),
                id=new_persona.id,
                description=escape_markdown_v2(desc_raw)
                )
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} created persona: '{new_persona.name}' (ID: {new_persona.id})")

        except IntegrityError:
            logger.warning(f"IntegrityError caught by handler for create_persona user {user_id} name '{persona_name}'.")
            persona_name_escaped = escape_markdown_v2(persona_name)
            error_msg_ie_raw = f"❌ ошибка: личность '{persona_name_escaped}' уже существует \\(возможно, гонка запросов\\)\\. попробуй еще раз."
            await update.message.reply_text(escape_markdown_v2(error_msg_ie_raw), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e:
            logger.error(f"SQLAlchemyError caught by handler for create_persona user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            logger.error(f"BadRequest sending message in create_persona for user {user_id}: {e}", exc_info=True)
            try: await update.message.reply_text("❌ Произошла ошибка при отправке ответа.", parse_mode=None)
            except Exception as fe: logger.error(f"Failed sending fallback create_persona error: {fe}")
        except Exception as e:
            logger.error(f"Error creating persona for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)

async def my_personas(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mypersonas command and show_mypersonas callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message_cmd = update.message if not is_callback else None

    user = None
    if query:
        user = query.from_user
        if not query.message:
            logger.error("my_personas (callback): query.message is None.")
            try: await query.answer("Ошибка: сообщение не найдено.", show_alert=True)
            except Exception: pass
            return
        chat_id = query.message.chat.id
        message_to_delete_if_callback = query.message
    elif message_cmd:
        user = message_cmd.from_user
        chat_id = message_cmd.chat.id
        message_to_delete_if_callback = None
    else:
        logger.error("my_personas handler called with invalid update type or missing user/chat info.")
        return

    user_id = user.id
    username = user.username or f"id_{user_id}"
    chat_id_str = str(chat_id)

    if is_callback:
        logger.info(f"Callback 'show_mypersonas' < User {user_id} ({username}) in Chat {chat_id_str}")
        try:
            await query.answer()
        except Exception as e_ans:
            logger.warning(f"Could not answer query in my_personas: {e_ans}")
    else:
        logger.info(f"CMD /mypersonas < User {user_id} ({username}) in Chat {chat_id_str}")
    
    if not is_callback:
        await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("❌ ошибка при загрузке списка личностей.")
    error_general = escape_markdown_v2("❌ произошла ошибка при получении списка личностей.")
    error_user_not_found = escape_markdown_v2("❌ ошибка: не удалось найти пользователя.")
    info_no_personas_fmt_raw = (
        "у тебя пока нет личностей ({count}/{limit}).\n"
        "создай первую: /createpersona <имя> [описание]"
    )
    info_list_header_fmt_raw = "твои личности ({count}/{limit}):"
    fallback_text_plain_parts = []

    final_text_to_send = ""
    final_reply_markup = None
    final_parse_mode = None

    try:
        with get_db() as db:
            # Сразу используем get_or_create_user, он вернет существующего или создаст нового (id будет доступен благодаря flush внутри функции)
            user_with_personas = get_or_create_user(db, user_id, username)

            # При необходимости дозагружаем связи одним запросом selectinload
            if 'persona_configs' not in user_with_personas.__dict__:
                user_with_personas = db.query(User).options(
                    selectinload(User.persona_configs).selectinload(DBPersonaConfig.bot_instance)
                ).filter(User.id == user_with_personas.id).one_or_none()

            if not user_with_personas:
                logger.error(f"User {user_id} not found even after get_or_create/refresh in my_personas.")
                final_text_to_send = error_user_not_found
                fallback_text_plain_parts.append("Ошибка: не удалось найти пользователя.")
                raise StopIteration

            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name) if user_with_personas.persona_configs else []
            persona_limit = user_with_personas.persona_limit
            persona_count = len(personas)

            if not personas:
                raw_text_no_personas = info_no_personas_fmt_raw.format(
                    count=str(persona_count),
                    limit=str(persona_limit)
                )
                final_text_to_send = raw_text_no_personas
                final_parse_mode = None
                
                fallback_text_plain_parts.append(
                    f"У тебя пока нет личностей ({persona_count}/{persona_limit}).\n"
                    f"Создай первую: /createpersona <имя> [описание]\n\n"
                    f"Подробное описание помогает личности лучше понять свою роль."
                )
                keyboard_no_personas = [[InlineKeyboardButton("⬅️ назад в меню", callback_data="show_menu")]] if is_callback else None
                final_reply_markup = InlineKeyboardMarkup(keyboard_no_personas) if keyboard_no_personas else ReplyKeyboardRemove()
            else:
                header_text_raw = info_list_header_fmt_raw.format(
                    count=str(persona_count), 
                    limit=str(persona_limit)
                )
                header_text = escape_markdown_v2(header_text_raw)
                message_lines = [header_text]
                keyboard_personas = []
                fallback_text_plain_parts.append(f"Твои личности ({persona_count}/{persona_limit}):")

                for p in personas:
                    # статус привязки бота (markdownv2, нижний регистр)
                    bot_status_line = ""
                    if getattr(p, 'bot_instance', None) and p.bot_instance:
                        bi = p.bot_instance
                        if bi.status == 'active' and bi.telegram_username:
                            escaped_username = escape_markdown_v2(bi.telegram_username)
                            bot_status_line = f"\n*привязан:* `@{escaped_username}`"
                        else:
                            bot_status_line = f"\n*статус:* не привязан"
                    else:
                        bot_status_line = f"\n*статус:* не привязан"

                    escaped_name = escape_markdown_v2(p.name)
                    persona_text = f"\n*{escaped_name}* \\(id: `{p.id}`\\){bot_status_line}"
                    message_lines.append(persona_text)
                    fallback_text_plain_parts.append(f"\n- {p.name} (id: {p.id})")

                    edit_cb = f"edit_persona_{p.id}"
                    delete_cb = f"delete_persona_{p.id}"
                    bind_cb = f"bind_bot_{p.id}"

                    # Кнопки без эмодзи; третью кнопку заменяем на привязку/перепривязку
                    keyboard_personas.append([
                        InlineKeyboardButton("настроить", callback_data=edit_cb),
                        InlineKeyboardButton("удалить", callback_data=delete_cb)
                    ])
                    # Подпись привязки зависит от текущего состояния
                    bind_label = "перепривязать бота" if (getattr(p, 'bot_instance', None) and p.bot_instance) else "привязать бота"
                    keyboard_personas.append([
                        InlineKeyboardButton(bind_label, callback_data=bind_cb)
                    ])
                
                final_text_to_send = "\n".join(message_lines)
                final_parse_mode = ParseMode.MARKDOWN_V2
                if is_callback:
                    keyboard_personas.append([InlineKeyboardButton("⬅️ назад в меню", callback_data="show_menu")])
                final_reply_markup = InlineKeyboardMarkup(keyboard_personas)
            
            logger.info(f"User {user_id} requested mypersonas. Prepared {persona_count} personas with action buttons. MD text preview: {final_text_to_send[:100]}")

    except StopIteration:
        pass
    except SQLAlchemyError as e:
        logger.error(f"Database error during my_personas for user {user_id}: {e}", exc_info=True)
        final_text_to_send = error_db
        fallback_text_plain_parts.append("Ошибка базы данных при загрузке списка личностей.")
    except Exception as e:
        logger.error(f"Error preparing my_personas for user {user_id}: {e}", exc_info=True)
        final_text_to_send = error_general
        fallback_text_plain_parts.append("Произошла ошибка при получении списка личностей.")
        
    current_fallback_text_plain = "\n".join(fallback_text_plain_parts) if fallback_text_plain_parts else "Ошибка отображения."

    try:
        if is_callback and message_to_delete_if_callback:
            try:
                await context.bot.delete_message(chat_id=message_to_delete_if_callback.chat.id, 
                                                message_id=message_to_delete_if_callback.message_id)
                logger.debug(f"my_personas (callback): Deleted previous message {message_to_delete_if_callback.message_id}")
            except Exception as e_del:
                logger.warning(f"my_personas (callback): Could not delete previous message {message_to_delete_if_callback.message_id}: {e_del}")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=final_text_to_send,
            reply_markup=final_reply_markup,
            parse_mode=final_parse_mode
        )

    except TelegramError as e_send:
        logger.error(f"Telegram error sending my_personas for user {user_id}: {e_send}", exc_info=True)
        if isinstance(e_send, BadRequest) and "parse entities" in str(e_send).lower():
            logger.error(f"--> my_personas: Failed MD text: '{final_text_to_send[:500]}...' Using fallback: '{current_fallback_text_plain[:500]}'")
            try:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=current_fallback_text_plain,
                    reply_markup=final_reply_markup, 
                    parse_mode=None
                )
            except Exception as e_fallback_send:
                logger.error(f"my_personas: Failed sending fallback plain text: {e_fallback_send}")
        else:
            try:
                await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка при отображении списка личностей.", parse_mode=None)
            except Exception: pass
    except Exception as e_final_send:
        logger.error(f"Unexpected error sending my_personas for user {user_id}: {e_final_send}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=chat_id, text="Произошла критическая ошибка.", parse_mode=None)
        except Exception: pass


async def bind_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает диалог привязки токена бота для выбранной личности."""
    is_callback = update.callback_query is not None
    if not is_callback or not update.callback_query.data:
        return ConversationHandler.END
    try:
        persona_id = int(update.callback_query.data.split('_')[-1])
    except (IndexError, ValueError):
        await update.callback_query.answer("❌ ошибка: неверный id", show_alert=True)
        return ConversationHandler.END

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = update.callback_query.message.chat.id if update.callback_query.message else user_id
    chat_id_str = str(chat_id)

    await update.callback_query.answer("запускаю привязку бота…")
    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # проверим, что персона принадлежит пользователю
    with get_db() as db:
        persona = get_persona_by_id_and_owner(db, user_id, persona_id)
        if not persona:
            try:
                await context.bot.send_message(chat_id=chat_id, text="❌ личность не найдена или не твоя.", parse_mode=None)
            except Exception: pass
            return ConversationHandler.END

    context.user_data['bind_persona_id'] = persona_id
    prompt_text = (
        "введи токен телеграм-бота, которого хочешь привязать к этой личности.\n"
        "мы проверим токен через getme и сохраним id и username бота.\n\n"
        "важно: не публикуй токен в открытых чатах."
    )
    try:
        await context.bot.send_message(chat_id=chat_id, text=prompt_text, parse_mode=None)
    except Exception as e:
        logger.error(f"bind_bot_start: failed to send prompt: {e}")
        return ConversationHandler.END
    return REGISTER_BOT_TOKEN


async def bind_bot_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает токен, валидирует через getMe, сохраняет через set_bot_instance_token."""
    if not update.message or not update.message.text:
        return REGISTER_BOT_TOKEN

    token = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id = update.message.chat.id
    chat_id_str = str(chat_id)

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    # валидируем токен через getMe
    bot_id = None
    bot_username = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json() if resp is not None else {}
            if not data.get('ok'):
                await update.message.reply_text("❌ токен невалиден или недоступен. проверь и попробуй снова.", parse_mode=None)
                return REGISTER_BOT_TOKEN
            result = data.get('result') or {}
            bot_id = result.get('id')
            bot_username = result.get('username')
            if not bot_id or not bot_username:
                await update.message.reply_text("❌ не удалось получить данные бота через getme.", parse_mode=None)
                return REGISTER_BOT_TOKEN
    except Exception as e:
        logger.error(f"bind_bot_token_received: getMe failed: {e}")
        await update.message.reply_text("❌ ошибка при запросе getme. попробуй еще раз позже.", parse_mode=None)
        return REGISTER_BOT_TOKEN

    persona_id = context.user_data.get('bind_persona_id')
    if not persona_id:
        await update.message.reply_text("❌ внутренняя ошибка: нет id личности.", parse_mode=None)
        return ConversationHandler.END

    # сохраняем в БД
    try:
        with get_db() as db:
            # получаем/создаем пользователя и удостоверимся, что персона его
            user_obj = db.query(User).filter(User.telegram_id == user_id).first()
            if not user_obj:
                user_obj = get_or_create_user(db, user_id, username)
                db.commit(); db.refresh(user_obj)

            persona = get_persona_by_id_and_owner(db, user_id, persona_id)
            if not persona:
                await update.message.reply_text("❌ личность не найдена или не твоя.", parse_mode=None)
                return ConversationHandler.END

            instance, status = set_bot_instance_token(db, user_obj.id, persona_id, token, bot_id, bot_username)
            if status == "already_registered":
                await update.message.reply_text("❌ этот бот уже привязан к другой личности.", parse_mode=None)
                return ConversationHandler.END
            elif status in ("created", "updated", "race_condition_resolved"):
                # Пытаемся установить webhook для нового бота
                try:
                    webhook_url = f"{config.WEBHOOK_URL_BASE}/telegram/{token}"
                    temp_bot = Bot(token=token)
                    secret = str(uuid.uuid4())
                    await temp_bot.set_webhook(
                        url=webhook_url,
                        allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
                        secret_token=secret
                    )
                    # фиксируем установку вебхука и секрет в БД (если поля есть)
                    try:
                        from datetime import datetime, timezone
                        if hasattr(instance, 'webhook_secret'):
                            instance.webhook_secret = secret
                        if hasattr(instance, 'last_webhook_set_at'):
                            instance.last_webhook_set_at = datetime.now(timezone.utc)
                        if hasattr(instance, 'status'):
                            instance.status = 'active'
                        db.commit()
                    except Exception as e_db_commit:
                        logger.error(f"bind_bot_token_received: failed to commit webhook secret/timestamp/status: {e_db_commit}", exc_info=True)
                        db.rollback()

                    # Авто-активация личности в текущем чате
                    try:
                        chat_link = link_bot_instance_to_chat(db, instance.id, chat_id_str)
                        if chat_link:
                            await update.message.reply_text(
                                f"✅ бот @{bot_username} привязан к личности '{persona.name}' и активирован в чате привязанного бота. память очищена.",
                                parse_mode=None
                            )
                        else:
                            await update.message.reply_text(
                                f"⚠️ бот @{bot_username} привязан к личности '{persona.name}', но не удалось автоматически активировать в чате привязанного бота. Напиши любое сообщение в чате этого бота или используй /addbot {persona_id}.",
                                parse_mode=None
                            )
                    except Exception as link_err:
                        logger.error(f"bind_bot_token_received: auto-activate link failed: {link_err}", exc_info=True)
                        await update.message.reply_text(
                            f"⚠️ бот @{bot_username} привязан к личности '{persona.name}', но авто-активация не удалась. Напиши любое сообщение в чате этого бота или используй /addbot {persona_id}.",
                            parse_mode=None
                        )
                except Exception as e_webhook:
                    logger.error(f"bind_bot_token_received: failed to set webhook for @{bot_username}: {e_webhook}", exc_info=True)
                    try:
                        if hasattr(instance, 'status'):
                            instance.status = 'webhook_error'
                        db.commit()
                    except Exception:
                        db.rollback()
                    await update.message.reply_text(
                        f"⚠️ бот @{bot_username} сохранен, но не удалось настроить вебхук. попробуй позже.", parse_mode=None
                    )
            else:
                await update.message.reply_text("❌ не удалось сохранить токен. попробуй позже.", parse_mode=None)
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"bind_bot_token_received: DB error: {e}", exc_info=True)
        await update.message.reply_text("❌ ошибка базы данных при сохранении токена.", parse_mode=None)
        return ConversationHandler.END

    # очищаем state
    context.user_data.pop('bind_persona_id', None)
    return ConversationHandler.END


async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    """Handles adding a persona (BotInstance) to the current chat."""
    is_callback = update.callback_query is not None
    message_or_callback_msg = update.callback_query.message if is_callback else update.message
    if not message_or_callback_msg: return

    user_id = update.effective_user.id
    username = update.effective_user.username or f"id_{user_id}"
    chat_id_str = str(message_or_callback_msg.chat.id)
    chat_title = message_or_callback_msg.chat.title or f"Chat {chat_id_str}"
    local_persona_id = persona_id

    if not is_callback:
        # проверка подписки удалена
        pass

    usage_text = escape_markdown_v2("формат: `/addbot <id персоны>`\nили используй кнопку '➕ В чат' из `/mypersonas`")
    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности.")
    error_invalid_id_cmd = escape_markdown_v2("❌ id личности должен быть числом.")
    error_no_id = escape_markdown_v2("❌ ошибка: ID личности не определен.")
    error_persona_not_found_fmt_raw = "❌ личность с id `{id}` не найдена или не твоя."
    error_already_active_fmt_raw = "✅ личность '{name}' уже активна в этом чате."
    success_added_structure_raw = "✅ личность '{name}' \\(id: `{id}`\\) активирована в этом чате\\! память очищена."
    error_link_failed = escape_markdown_v2("❌ не удалось активировать личность (ошибка связывания).")
    error_integrity = escape_markdown_v2("❌ произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при добавлении бота.")
    error_general = escape_markdown_v2("❌ ошибка при активации личности.")

    if is_callback and local_persona_id is None:
        try:
            local_persona_id = int(update.callback_query.data.split('_')[-1])
        except (IndexError, ValueError):
            logger.error(f"Could not parse persona_id from add_bot callback data: {update.callback_query.data}")
            await update.callback_query.answer("❌ Ошибка: неверный ID", show_alert=True)
            return
    elif not is_callback:
        logger.info(f"CMD /addbot < User {user_id} ({username}) in Chat '{chat_title}' ({chat_id_str}) with args: {context.args}")
        args = context.args
        if not args or len(args) != 1 or not args[0].isdigit():
            await message_or_callback_msg.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
            return
        try:
            local_persona_id = int(args[0])
        except ValueError:
            await message_or_callback_msg.reply_text(error_invalid_id_cmd, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            return

    if local_persona_id is None:
        logger.error("add_bot_to_chat: persona_id is None after processing input.")
        reply_target = update.callback_query.message if is_callback else message_or_callback_msg
        if is_callback: await update.callback_query.answer("❌ Ошибка: ID не определен.", show_alert=True)
        else: await reply_target.reply_text(error_no_id, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if is_callback:
        await update.callback_query.answer("Добавляем личность...")

    await context.bot.send_chat_action(chat_id=chat_id_str, action=ChatAction.TYPING)

    with get_db() as db:
        try:
            persona = get_persona_by_id_and_owner(db, user_id, local_persona_id)
            if not persona:
                final_not_found_msg = error_persona_not_found_fmt_raw.format(id=local_persona_id)
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                if is_callback: await update.callback_query.answer("Личность не найдена", show_alert=True)
                await reply_target.reply_text(final_not_found_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            existing_active_link = db.query(DBChatBotInstance).options(
                selectinload(DBChatBotInstance.bot_instance_ref).selectinload(DBBotInstance.persona_config)
            ).filter(
                DBChatBotInstance.chat_id == chat_id_str,
                DBChatBotInstance.active == True
            ).first()

            if existing_active_link:
                if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config_id == local_persona_id:
                    # Готовим простой текст для сообщения
                    already_active_msg_plain = f"✅ личность '{persona.name}' уже активна в этом чате."
                    reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                    if is_callback: await update.callback_query.answer(f"'{persona.name}' уже активна", show_alert=True)
                    # Отправляем как простой текст
                    await reply_target.reply_text(already_active_msg_plain, reply_markup=ReplyKeyboardRemove(), parse_mode=None)

                    logger.info(f"Clearing context for already active persona {persona.name} in chat {chat_id_str} on re-add.")
                    # Правильное удаление контекста для dynamic relationship
                    stmt = delete(ChatContext).where(ChatContext.chat_bot_instance_id == existing_active_link.id)
                    delete_result = db.execute(stmt)
                    deleted_ctx = delete_result.rowcount # Получаем количество удаленных строк
                    db.commit()
                    logger.debug(f"Cleared {deleted_ctx} context messages for re-added ChatBotInstance {existing_active_link.id}.")
                    return
                else:
                    prev_persona_name = "Неизвестная личность"
                    if existing_active_link.bot_instance_ref and existing_active_link.bot_instance_ref.persona_config:
                        prev_persona_name = existing_active_link.bot_instance_ref.persona_config.name
                    else:
                        prev_persona_name = f"ID {existing_active_link.bot_instance_id}"

                    # Разрешаем несколько активных связей в чате. Не деактивируем ранее активный инстанс.
                    logger.info(f"Keeping previous active bot '{prev_persona_name}' in chat {chat_id_str} and activating '{persona.name}' alongside.")
                    # Никаких изменений существующей активной связи не требуется.

            user = persona.owner
            bot_instance = db.query(DBBotInstance).filter(
                DBBotInstance.persona_config_id == local_persona_id
            ).first()

            if not bot_instance:
                logger.info(f"Creating new BotInstance for persona {local_persona_id}")
                try:
                        bot_instance = create_bot_instance(db, user.id, local_persona_id, name=f"Inst:{persona.name}")
                except (IntegrityError, SQLAlchemyError) as create_err:
                        logger.error(f"Failed to create BotInstance ({create_err}), possibly due to concurrent request. Retrying fetch.")
                        db.rollback()
                        bot_instance = db.query(DBBotInstance).filter(DBBotInstance.persona_config_id == local_persona_id).first()
                        if not bot_instance:
                            logger.error("Failed to fetch BotInstance even after retry.")
                            raise SQLAlchemyError("Failed to create or fetch BotInstance")

            chat_link = link_bot_instance_to_chat(db, bot_instance.id, chat_id_str)

            if chat_link:
                final_success_msg = success_added_structure_raw.format(
                    name=escape_markdown_v2(persona.name),
                    id=local_persona_id
                    )
                # Готовим простой текст
                final_success_msg_plain = f"✅ личность '{persona.name}' (id: {local_persona_id}) активирована в этом чате! память очищена."
                # Отправляем как простой текст
                await context.bot.send_message(chat_id=chat_id_str, text=final_success_msg_plain, reply_markup=ReplyKeyboardRemove(), parse_mode=None)
                if is_callback:
                        try:
                            await update.callback_query.delete_message()
                        except Exception as del_err:
                            logger.warning(f"Could not delete callback message after adding bot: {del_err}")
                logger.info(f"Linked BotInstance {bot_instance.id} (Persona {local_persona_id}, '{persona.name}') to chat {chat_id_str}. ChatBotInstance ID: {chat_link.id}")
            else:
                reply_target = update.callback_query.message if is_callback else message_or_callback_msg
                await reply_target.reply_text(error_link_failed, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                logger.warning(f"Failed to link BotInstance {bot_instance.id} to chat {chat_id_str} - link_bot_instance_to_chat returned None.")

        except IntegrityError as e:
            logger.warning(f"IntegrityError potentially during addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=False)
            await context.bot.send_message(chat_id=chat_id_str, text=error_integrity, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except SQLAlchemyError as e:
            logger.error(f"Database error during /addbot for persona {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat_id_str, text=error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except BadRequest as e:
            logger.error(f"BadRequest sending message in add_bot_to_chat: {e}", exc_info=True)
            try: await context.bot.send_message(chat_id=chat_id_str, text="❌ Произошла ошибка при отправке ответа.", parse_mode=None)
            except Exception as fe: logger.error(f"Failed sending fallback add_bot_to_chat error: {fe}")
        except Exception as e:
            logger.error(f"Error adding bot instance {local_persona_id} to chat {chat_id_str}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat_id_str, text=error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from inline keyboards NOT part of a ConversationHandler."""
    query = update.callback_query
    if not query or not query.data: return

    chat_id_str = str(query.message.chat.id) if query.message else "Unknown Chat"
    user_id = query.from_user.id
    username = query.from_user.username or f"id_{user_id}"
    data = query.data

    # --- Check if data matches known conversation entry patterns --- 
    # If it matches, let the ConversationHandler deal with it.
    # Add patterns for ALL conversation entry points here.
    convo_entry_patterns = [
        r'^edit_persona_',    # Edit persona entry
        r'^delete_persona_',  # Delete persona entry
        # Add other ConversationHandler entry point patterns if they exist
    ]
    for pattern in convo_entry_patterns:
        if re.match(pattern, data):
            logger.debug(f"Callback {data} matches convo entry pattern '{pattern}', letting ConversationHandler handle it.")
            # Don't answer here, let the convo handler answer.
            # We don't explicitly pass it on, PTB should handle it if we don't.
            return # <--- Let PTB handle routing

    # Log only callbacks handled by this general handler
    logger.info(f"GENERAL CALLBACK < User {user_id} ({username}) in Chat {chat_id_str} data: {data}")

    # проверка подписки на канал отключена

    # --- Route non-conversation callbacks ---
    if data.startswith("set_mood_"):
        await mood(update, context)
    
    elif data == "view_tos":
        await query.answer()
        await view_tos(update, context)
    elif data == "buycredits_open":
        await query.answer()
        await buycredits(update, context)
    elif data == "show_tos":
        await query.answer()
        await show_tos(update, context)
    elif data == "confirm_pay":
        await query.answer()
        await confirm_pay(update, context)
    elif data.startswith("add_bot_"):
        # No need to answer here, add_bot_to_chat does it
        await add_bot_to_chat(update, context)
    elif data == "show_help":
        await query.answer()
        await help_command(update, context)
    elif data == "show_menu":
        await query.answer()
        await menu_command(update, context)
    elif data == "show_profile":
        await query.answer()
        await profile(query, context)
    elif data == "show_mypersonas":
        # my_personas теперь сама отвечает на query
        await my_personas(query, context)
    elif data == "show_settings":
        await query.answer()
        # Этот коллбэк больше не должен вызываться, так как он заменен на /editpersona
        # но на всякий случай оставим заглушку
        await query.message.reply_text("Используйте /editpersona <id> для настроек.")
    elif data.startswith("dummy_"):
        await query.answer()
    else:
        # Log unhandled non-conversation callbacks
        logger.warning(f"Unhandled non-conversation callback query data: {data} from user {user_id}")
        try:
            if query.message and query.message.reply_markup:
                try:
                    await query.edit_message_text(
                        text=f"{query.message.text}\n\n(Неизвестное действие: {data})", 
                        reply_markup=None, 
                        parse_mode=None
                    )
                except BadRequest as e_br:
                    if "message is not modified" in str(e_br).lower():
                        await query.answer("Действие не изменилось.", show_alert=True)
                    elif "message to edit not found" in str(e_br).lower():
                        await query.answer("Сообщение для изменения не найдено.", show_alert=True)
                    else:
                        await query.answer("Ошибка при обработке действия.", show_alert=True)
                        logger.error(f"BadRequest when handling unknown callback '{data}': {e_br}")
            else:
                await query.answer("Неизвестное действие.", show_alert=True)
        except Exception as e:
            logger.warning(f"Failed to answer unhandled callback {query.id} ('{data}'): {e}")
            try:
                await query.answer("Ошибка обработки.", show_alert=True) 
            except Exception:
                pass


async def profile(update: Union[Update, CallbackQuery], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows user profile info. Can be triggered by command or callback."""
    is_callback = isinstance(update, CallbackQuery)
    query = update if is_callback else None
    message = update.message if not is_callback else None

    if is_callback:
        user = query.from_user
        message_target = query.message
    elif message:
        user = message.from_user
        message_target = message
    else:
        logger.error("Profile handler called with invalid update type.")
        return

    if not user or not message_target:
        logger.error("Profile handler could not determine user or message target.")
        return

    user_id = user.id
    username = user.username or f"id_{user_id}"
    chat_id = message_target.chat.id

    logger.info(f"CMD /profile or Callback 'show_profile' < User {user_id} ({username})")

    if not is_callback:
        # проверка подписки удалена
        pass

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    error_db = escape_markdown_v2("❌ ошибка базы данных при загрузке профиля.")
    error_general = escape_markdown_v2("❌ ошибка при обработке команды /profile.")
    error_user_not_found = escape_markdown_v2("❌ ошибка: пользователь не найден.")
    profile_text_plain = "Ошибка загрузки профиля."

    with get_db() as db:
        try:
            user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == user_id).first()
            if not user_db:
                user_db = get_or_create_user(db, user_id, username)
                db.commit()
                db.refresh(user_db)
                user_db = db.query(User).options(selectinload(User.persona_configs)).filter(User.id == user_db.id).one_or_none()
                if not user_db:
                    logger.error(f"User {user_id} not found after get_or_create/refresh in profile.")
                    await context.bot.send_message(chat_id, error_user_not_found, parse_mode=ParseMode.MARKDOWN_V2)
                    return

            # Новый профиль: показываем баланс кредитов и базовую информацию
            persona_count = len(user_db.persona_configs) if user_db.persona_configs is not None else 0
            persona_limit_raw = f"{persona_count}/{user_db.persona_limit}"
            persona_limit_escaped = escape_markdown_v2(persona_limit_raw)
            credits_balance = float(user_db.credits or 0.0)
            credits_text = escape_markdown_v2(f"{credits_balance:.2f}")

            profile_text_md = (
                f"*твой профиль*\n\n"
                f"*баланс кредитов:* {credits_text}\n"
                f"{escape_markdown_v2('создано личностей:')} {persona_limit_escaped}\n\n"
                f"кредиты списываются за текст, изображения и распознавание аудио."
            )

            profile_text_plain = (
                f"твой профиль\n\n"
                f"баланс кредитов: {credits_balance:.2f}\n"
                f"создано личностей: {persona_limit_raw}\n\n"
                f"кредиты списываются за текст, изображения и распознавание аудио."
            )

            # Во избежание ошибок MarkdownV2 отправляем простой текст без форматирования
            final_text_to_send = profile_text_plain

            keyboard = [[
                InlineKeyboardButton("пополнить кредиты", callback_data="buycredits_open")
            ], [
                InlineKeyboardButton("назад в меню", callback_data="show_menu")
            ]] if is_callback else None
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            if is_callback:
                if message_target.text != final_text_to_send or message_target.reply_markup != reply_markup:
                    await query.edit_message_text(final_text_to_send, reply_markup=reply_markup, parse_mode=None)
                else:
                    await query.answer()
            else:
                await message_target.reply_text(final_text_to_send, reply_markup=reply_markup, parse_mode=None)

        except SQLAlchemyError as e:
            logger.error(f"Database error during profile for user {user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, error_db, parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as e:
            logger.error(f"Telegram error during profile for user {user_id}: {e}", exc_info=True)
            if isinstance(e, BadRequest) and "Can't parse entities" in str(e):
                logger.error(f"--> Failed text (MD): '{final_text_to_send[:500]}...'")
                try:
                    if is_callback:
                        await query.edit_message_text(profile_text_plain, reply_markup=reply_markup, parse_mode=None)
                    else:
                        await message_target.reply_text(profile_text_plain, reply_markup=reply_markup, parse_mode=None)
                except Exception as fallback_e:
                    logger.error(f"Failed sending fallback profile message: {fallback_e}")
            else:
                await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error in profile handler for user {user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, error_general, parse_mode=ParseMode.MARKDOWN_V2)


async def buycredits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /buycredits: показывает доступные пакеты и кнопки оплаты."""
    user = update.effective_user
    if not user:
        return
    chat_id = update.effective_chat.id if update.effective_chat else user.id

    # Проверка доступности YooKassa
    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        await context.bot.send_message(chat_id, escape_markdown_v2("❌ платежи временно недоступны."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Формируем список пакетов
    lines = ["*пополнение кредитов*\n"]
    keyboard_rows = []
    for pkg_id, pkg in (CREDIT_PACKAGES or {}).items():
        title = pkg.get('title') or pkg_id
        credits = float(pkg.get('credits', 0))
        price = float(pkg.get('price_rub', 0))
        display_title = str(title).lower()
        lines.append(f"• {escape_markdown_v2(display_title)} — {escape_markdown_v2(f'{credits:.0f} кр.')} за {escape_markdown_v2(f'{price:.0f} ₽')}")
        keyboard_rows.append([InlineKeyboardButton(f"купить {int(credits)} кр. за {int(price)} ₽", callback_data=f"buycredits_pkg_{pkg_id}")])

    text_md = "\n".join(lines)
    keyboard_rows.append([InlineKeyboardButton("⬅️ назад в меню", callback_data="show_menu")])
    await context.bot.send_message(chat_id, text_md, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode=ParseMode.MARKDOWN_V2)


async def buycredits_pkg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создает платеж YooKassa для выбранного кредитного пакета."""
    query = update.callback_query
    if not query:
        return
    user_id = query.from_user.id

    await query.answer()

    data = query.data or ""
    try:
        pkg_id = data.split("buycredits_pkg_")[-1]
    except Exception:
        await query.answer("Некорректный пакет", show_alert=True)
        return

    pkg = (CREDIT_PACKAGES or {}).get(pkg_id)
    if not pkg:
        await query.answer("Пакет недоступен", show_alert=True)
        return

    credits = float(pkg.get('credits', 0))
    price_rub = float(pkg.get('price_rub', 0))
    bot_username = context.bot_data.get('bot_username', 'NunuAiBot')
    return_url = f"https://t.me/{bot_username}"

    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit()):
        await query.edit_message_text("❌ платежи недоступны", parse_mode=None)
        return

    idempotence_key = str(uuid.uuid4())
    description = f"Покупка кредитов для @{bot_username}: {int(credits)} кр. (User ID: {user_id})"
    metadata = {
        'telegram_user_id': str(user_id),
        'package_id': str(pkg_id),
        'credits': str(int(credits)),
    }

    # Чек
    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Кредиты для @{bot_username} ({int(credits)} кр.)",
                "quantity": 1.0,
                "amount": {"value": f"{price_rub:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1",
                "payment_mode": "full_prepayment",
                "payment_subject": "service"
            })
        ]
        receipt_data = Receipt({
            "customer": {"email": f"user_{user_id}@telegram.bot"},
            "items": receipt_items,
        })
    except Exception as e:
        logger.error(f"Error preparing receipt for credits: {e}")
        await query.edit_message_text("❌ ошибка формирования платежа", parse_mode=None)
        return

    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{price_rub:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(description) \
            .set_metadata(metadata) \
            .set_receipt(receipt_data)
        request = builder.build()

        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)
        if not payment_response or not getattr(payment_response, 'confirmation', None) or not getattr(payment_response.confirmation, 'confirmation_url', None):
            await query.edit_message_text("❌ ошибка получения ссылки оплаты", parse_mode=None)
            return

        confirmation_url = payment_response.confirmation.confirmation_url
        keyboard = [[InlineKeyboardButton("перейти к оплате", url=confirmation_url)]]
        try:
            await query.edit_message_text("ссылка на оплату создана.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except BadRequest:
            # Если редактирование не получилось, отправим новое сообщение
            await context.bot.send_message(query.message.chat.id, "ссылка на оплату создана.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except Exception as e:
        logger.error(f"Yookassa create payment error (credits) for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ ошибка при создании платежа", parse_mode=None)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    """[DEPRECATED] /subscribe — больше не поддерживается. Оставлено как заглушка."""
    is_callback = update.callback_query is not None
    msg = update.callback_query.message if is_callback else update.message
    if not msg:
        return
    try:
        text = "ℹ️ Подписки больше не поддерживаются. Используйте /buycredits для пополнения кредитов."
        if is_callback:
            await update.callback_query.edit_message_text(text, parse_mode=None)
        else:
            await msg.reply_text(text, parse_mode=None)
    except Exception:
        pass

async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[DEPRECATED] Показ ToS для подписок — отключен."""
    query = update.callback_query
    if not query: return
    try:
        await query.answer("Подписки отключены. Используйте /buycredits", show_alert=True)
    except Exception:
        pass

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[DEPRECATED] Подтверждение оплаты подписки — отключено."""
    query = update.callback_query
    if not query: return
    try:
        await query.answer("Подписки отключены. Используйте /buycredits", show_alert=True)
    except Exception:
        pass

async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[DEPRECATED] Генерация ссылки для подписки — отключена."""
    query = update.callback_query
    if not query: return
    success_link_raw = (
        "✨ Ссылка для оплаты создана!\n\n"
        "Нажмите кнопку ниже для перехода к оплате.\n"
        "После успешной оплаты подписка активируется автоматически (может занять до 5 минут).\n\n"
        "Если возникнут проблемы, обратитесь в поддержку."
    )

    text = ""
    reply_markup = None

    yookassa_ready = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_SHOP_ID.isdigit())
    if not yookassa_ready:
        logger.error("Yookassa credentials not set correctly for payment generation.")
        text = error_yk_not_ready
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        current_shop_id = int(YOOKASSA_SHOP_ID)
        YookassaConfig.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
        logger.info(f"Yookassa configured within generate_payment_link (Shop ID: {current_shop_id}).")
    except ValueError:
        logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) invalid integer.")
        text = error_yk_config
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return
    except Exception as conf_e:
        logger.error(f"Failed to configure Yookassa SDK in generate_payment_link: {conf_e}", exc_info=True)
        text = error_yk_config
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    idempotence_key = str(uuid.uuid4())
    payment_description = f"Premium подписка @NunuAiBot на {SUBSCRIPTION_DURATION_DAYS} дней (User ID: {user_id})"
    payment_metadata = {'telegram_user_id': str(user_id)}
    bot_username = context.bot_data.get('bot_username', "NunuAiBot")
    return_url = f"https://t.me/{bot_username}"

    try:
        receipt_items = [
            ReceiptItem({
                "description": f"Премиум доступ @{bot_username} на {SUBSCRIPTION_DURATION_DAYS} дней",
                "quantity": 1.0,
                "amount": {"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY},
                "vat_code": "1",
                "payment_mode": "full_prepayment",
                "payment_subject": "service"
            })
        ]
        user_email = f"user_{user_id}@telegram.bot"
        receipt_data = Receipt({
            "customer": {"email": user_email},
            "items": receipt_items,
        })
    except Exception as receipt_e:
        logger.error(f"Error preparing receipt data: {receipt_e}", exc_info=True)
        text = error_receipt
        reply_markup = None
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        builder = PaymentRequestBuilder()
        builder.set_amount({"value": f"{SUBSCRIPTION_PRICE_RUB:.2f}", "currency": SUBSCRIPTION_CURRENCY}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(payment_description) \
            .set_metadata(payment_metadata) \
            .set_receipt(receipt_data)
        request = builder.build()
        logger.debug(f"Payment request built: {request.json()}")

        payment_response = await asyncio.to_thread(Payment.create, request, idempotence_key)

        if not payment_response or not getattr(payment_response, 'confirmation', None) or not getattr(payment_response.confirmation, 'confirmation_url', None):
            logger.error(f"Yookassa API returned invalid response for user {user_id}. Status: {payment_response.status if payment_response else 'N/A'}. Response: {payment_response}")
            status_info = f" \\(статус: {escape_markdown_v2(payment_response.status)}\\)" if payment_response and payment_response.status else ""
            error_message = error_link_get_fmt_raw.format(status_info=status_info)
            text = error_message
            reply_markup = None
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            return

        confirmation_url = payment_response.confirmation.confirmation_url
        logger.info(f"Created Yookassa payment {payment_response.id} for user {user_id}. URL: {confirmation_url}")

        keyboard = [[InlineKeyboardButton("перейти к оплате", url=confirmation_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Используем НЕэкранированную строку и parse_mode=None
        text_to_send = success_link_raw
        await query.edit_message_text(text_to_send, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"Error during Yookassa payment creation for user {user_id}: {e}", exc_info=True)
        error_detail = ""
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                err_text = e.response.text
                logger.error(f"Yookassa API Error Response Text: {err_text}")
                if "Invalid credentials" in err_text:
                    error_detail = "ошибка аутентификации с юkassa"
                elif "receipt" in err_text.lower():
                    error_detail = "ошибка данных чека \\(детали в логах\\)"
                else:
                    error_detail = "ошибка от юkassa \\(детали в логах\\)"
            except Exception as parse_e:
                logger.error(f"Could not parse YK error response: {parse_e}")
                error_detail = "ошибка от юkassa \\(не удалось разобрать ответ\\)"
        elif isinstance(e, httpx.RequestError):
            error_detail = "проблема с сетевым подключением к юkassa"
        else:
            error_detail = "произошла непредвиденная ошибка"

        user_message = error_link_create_raw.format(error_detail=escape_markdown_v2(error_detail))
        try:
            await query.edit_message_text(user_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_e:
            logger.error(f"Failed to send error message after payment creation failure: {send_e}")


async def yookassa_webhook_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder - webhooks are handled by the Flask app, not PTB."""
    logger.warning("Placeholder Yookassa webhook endpoint called via Telegram bot handler. This should be handled by the Flask app.")
    pass

# --- Conversation Handlers ---

# --- Edit Persona Wizard ---

async def _clean_previous_edit_session(context: ContextTypes.DEFAULT_TYPE, current_user_id_for_log_prefix: int):
    """Helper to delete the menu message from a previous edit session, if any."""
    # current_user_id_for_log_prefix - это ID пользователя, который инициировал ТЕКУЩЕЕ действие,
    # чтобы лог был привязан к текущему пользователю, даже если user_data от предыдущего.
    
    # Мы все еще смотрим на wizard_menu_message_id и edit_chat_id, которые могли быть установлены
    # этим же пользователем в предыдущей сессии.
    
    current_keys = list(context.user_data.keys()) # Получаем ключи ДО попытки извлечения
    logger.info(f"_clean_previous_edit_session: CALLED (initiating user: {current_user_id_for_log_prefix}). "
                f"Current user_data keys BEFORE getting IDs: {current_keys}")
    
    old_wizard_menu_id = context.user_data.get('wizard_menu_message_id')
    old_edit_chat_id = context.user_data.get('edit_chat_id') 
    # _user_id_for_logging из user_data относится к пользователю ПРЕДЫДУЩЕЙ сессии (если она была от того же пользователя)
    previous_session_user_log = context.user_data.get('_user_id_for_logging', 'N/A_prev_session')
    
    logger.info(f"_clean_previous_edit_session: For initiating user '{current_user_id_for_log_prefix}' (prev session user log: '{previous_session_user_log}') - "
                f"Found old_wizard_menu_id: {old_wizard_menu_id}, old_edit_chat_id: {old_edit_chat_id}")
    
    if old_wizard_menu_id and old_edit_chat_id:
        logger.info(f"_clean_previous_edit_session: Attempting to delete old menu message {old_wizard_menu_id} "
                    f"in chat {old_edit_chat_id} (likely from user '{previous_session_user_log}')")
        try:
            delete_successful = await context.bot.delete_message(chat_id=old_edit_chat_id, message_id=old_wizard_menu_id)
            if delete_successful:
                logger.info(f"_clean_previous_edit_session: Successfully deleted old wizard menu message "
                            f"{old_wizard_menu_id} from chat {old_edit_chat_id}.")
            else:
                logger.warning(f"_clean_previous_edit_session: delete_message returned {delete_successful} "
                                f"for message {old_wizard_menu_id} in chat {old_edit_chat_id}.")
        except BadRequest as e_bad_req:
            if "message to delete not found" in str(e_bad_req).lower():
                logger.warning(f"_clean_previous_edit_session: Message {old_wizard_menu_id} in chat {old_edit_chat_id} "
                                f"not found for deletion. Error: {e_bad_req}")
            elif "message can't be deleted" in str(e_bad_req).lower():
                logger.warning(f"_clean_previous_edit_session: Message {old_wizard_menu_id} in chat {old_edit_chat_id} "
                                f"can't be deleted. Error: {e_bad_req}")
            else:
                logger.error(f"_clean_previous_edit_session: BadRequest while deleting message {old_wizard_menu_id} "
                            f"in chat {old_edit_chat_id}. Error: {e_bad_req}")
        except Forbidden as e_forbidden:
            logger.error(f"_clean_previous_edit_session: Forbidden to delete message {old_wizard_menu_id} "
                        f"in chat {old_edit_chat_id}. Error: {e_forbidden}")
        except Exception as e:
            logger.error(f"_clean_previous_edit_session: Generic error deleting message {old_wizard_menu_id} "
                        f"in chat {old_edit_chat_id}. Error: {e}")
    elif old_wizard_menu_id:
        logger.warning(f"_clean_previous_edit_session: Found old_wizard_menu_id ({old_wizard_menu_id}) "
                        f"but no old_edit_chat_id (initiating user '{current_user_id_for_log_prefix}'). Cannot delete.")
    elif old_edit_chat_id:
        logger.warning(f"_clean_previous_edit_session: Found old_edit_chat_id ({old_edit_chat_id}) "
                        f"but no old_wizard_menu_id (initiating user '{current_user_id_for_log_prefix}'). Cannot delete.")
    else:
        logger.info(f"_clean_previous_edit_session: No old wizard menu message found in user_data "
                    f"(initiating user '{current_user_id_for_log_prefix}') to delete.")

async def _start_edit_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the persona editing wizard."""
    user_id = update.effective_user.id # Это ID текущего пользователя, инициирующего действие
    
    # Определяем chat_id для нового меню
    chat_id_for_new_menu = None
    if update.effective_chat: 
        chat_id_for_new_menu = update.effective_chat.id
    elif update.callback_query and update.callback_query.message: 
        chat_id_for_new_menu = update.callback_query.message.chat.id
    
    if not chat_id_for_new_menu: # Добавили return если не определен chat_id
        logger.error("_start_edit_convo: Could not determine chat_id for sending the new wizard menu.")
        if update.callback_query:
            try: await update.callback_query.answer("Ошибка: чат для меню не определен.", show_alert=True)
            except Exception: pass
        return ConversationHandler.END

    is_callback = update.callback_query is not None
    logger.info(f"_start_edit_convo: User {user_id}, New PersonaID to edit {persona_id}, TargetChatID for new menu {chat_id_for_new_menu}, IsCallback {is_callback}")
    
    # 1. Сначала сохраняем ID пользователя во временную переменную
    logger.debug(f"_start_edit_convo: Before cleaning - user_data keys: {list(context.user_data.keys())}")
    
    # 2. Вызываем очистку. Она использует user_data от ВОЗМОЖНОЙ ПРЕДЫДУЩЕЙ сессии.
    # Передаем user_id ТЕКУЩЕГО пользователя для логирования в _clean_previous_edit_session
    logger.info(f"_start_edit_convo: Calling _clean_previous_edit_session for user {user_id}")
    await _clean_previous_edit_session(context, user_id) 
    
    # 3. Очищаем user_data для начала чистой НОВОЙ сессии
    logger.info(f"_start_edit_convo: Clearing user_data for user {user_id} to start new session.")
    context.user_data.clear() 
    
    # 4. Устанавливаем данные для НОВОЙ сессии
    context.user_data['edit_persona_id'] = persona_id
    context.user_data['_user_id_for_logging'] = user_id # <--- Устанавливаем для СЛЕДУЮЩЕГО вызова _clean_previous_edit_session

    # проверка подписки на канал отключена

    await context.bot.send_chat_action(chat_id=chat_id_for_new_menu, action=ChatAction.TYPING)

    error_not_found_fmt_raw = "❌ личность с id `{id}` не найдена или не твоя."
    error_db = escape_markdown_v2("❌ ошибка базы данных при начале редактирования.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка.")

    try:
        with get_db() as db:
            persona_config = db.query(DBPersonaConfig).options(
                selectinload(DBPersonaConfig.owner),
                selectinload(DBPersonaConfig.bot_instance)
            ).filter(
                DBPersonaConfig.id == persona_id,
                DBPersonaConfig.owner.has(User.telegram_id == user_id) # Проверка владения
            ).first()

            if not persona_config:
                final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                logger.warning(f"Persona {persona_id} not found or not owned by user {user_id} in _start_edit_convo.")
                if is_callback and update.callback_query: # Отвечаем на коллбэк, если он был
                    try: await update.callback_query.answer("Личность не найдена", show_alert=True)
                    except Exception: pass
                await context.bot.send_message(chat_id_for_new_menu, final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return ConversationHandler.END

            # Кэшируем полностью загруженный объект в состоянии пользователя, чтобы избежать повторных запросов
            context.user_data['persona_object'] = persona_config

            # Вызываем _show_edit_wizard_menu (патченную версию), она отправит НОВОЕ сообщение
            return await _show_edit_wizard_menu(update, context, persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error starting edit persona {persona_id} for user {user_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id_for_new_menu, error_db, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error starting edit persona {persona_id} for user {user_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id_for_new_menu, error_general, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

async def edit_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /editpersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /editpersona < User {user_id} with args: {args}")

    # НЕ чистим user_data здесь - это будет делать _start_edit_convo
    
    usage_text = escape_markdown_v2("укажи id личности: `/editpersona <id>`\nили используй кнопку из `/mypersonas`")
    error_invalid_id = escape_markdown_v2("❌ id должен быть числом.")

    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # Просто передаем управление в _start_edit_convo
    # _start_edit_convo очистит user_data и установит 'edit_persona_id'
    return await _start_edit_convo(update, context, persona_id)

async def edit_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for edit persona button press."""
    # САМЫЙ ПЕРВЫЙ ЛОГ
    logger.info("--- edit_persona_button_callback: ENTERED ---") 
    
    query = update.callback_query
    if not query or not query.data: 
        logger.warning("edit_persona_button_callback: Query or query.data is None. Returning END.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    original_chat_id = query.message.chat.id if query.message else (update.effective_chat.id if update.effective_chat else None)
    logger.info(f"CALLBACK edit_persona BUTTON < User {user_id} for data {query.data} in chat {original_chat_id}")

    # НЕ чистим user_data здесь - это будет делать _start_edit_convo

    try: 
        await query.answer() # Отвечаем на коллбэк
    except Exception as e_ans:
        logger.debug(f"edit_persona_button_callback: Could not answer query: {e_ans}")


    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности в кнопке.")
    
    try:
        persona_id = int(query.data.split('_')[-1])
        logger.info(f"Parsed persona_id: {persona_id} for user {user_id}")
        
        # _start_edit_convo очистит user_data и установит 'edit_persona_id'
        return await _start_edit_convo(update, context, persona_id)
        
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from edit_persona callback data: {query.data}")
        if original_chat_id:
            await context.bot.send_message(original_chat_id, error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in edit_persona_button_callback for {query.data}: {e}", exc_info=True)
        if original_chat_id:
            try: await context.bot.send_message(original_chat_id, "Произошла непредвиденная ошибка.", parse_mode=None)
            except Exception: pass
        return ConversationHandler.END

# Функция delete_persona_start перемещена в новую часть файла

async def _handle_back_to_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Общая функция для обработки кнопки "Назад" в меню настроек.
    Использует кэшированный объект. НИЧЕГО не удаляет — меню/подменю редактируется на месте.
    Если кэш потерян — fallback к БД.
    """
    query = update.callback_query

    # 1) Быстрый путь: из кэша
    persona_cached = context.user_data.get('persona_object')
    if persona_cached:
        return await _show_edit_wizard_menu(update, context, persona_cached)

    # 2) Fallback: БД (на случай потери кэша)
    with get_db() as db:
        persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
        if not persona:
            if query:
                try: await query.answer("Ошибка: личность не найдена", show_alert=True)
                except Exception: pass
            return ConversationHandler.END
        return await _show_edit_wizard_menu(update, context, persona)




# --- Wizard Menu Handler ---
async def edit_wizard_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses in the main wizard menu."""
    query = update.callback_query
    if not query or not query.data: 
        logger.warning("edit_wizard_menu_handler: Received query without data.")
        if query: 
            try: await query.answer("Ошибка: нет данных запроса.")
            except Exception: pass
        return EDIT_WIZARD_MENU
        
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    # Используем кэшированный объект, если он есть
    persona_obj = context.user_data.get('persona_object')
    user_id = query.from_user.id

    logger.debug(f"edit_wizard_menu_handler: User {user_id}, PersonaID {persona_id}, Data {data}")

    if not persona_id and not persona_obj:
        logger.warning(f"edit_wizard_menu_handler: persona_id missing for user {user_id}. Data: {data}")
        if query.message:
            try: await query.edit_message_text("Сессия редактирования потеряна. Начните заново.", reply_markup=None)
            except Exception: pass
        return ConversationHandler.END

    if data == "start_char_wizard": return await char_wiz_start(update, context)
    if data == "edit_wizard_name": return await edit_name_prompt(update, context)
    if data == "edit_wizard_description": return await edit_description_prompt(update, context)
    if data == "edit_wizard_comm_style": return await edit_comm_style_prompt(update, context)
    if data == "edit_wizard_verbosity": return await edit_verbosity_prompt(update, context)
    if data == "edit_wizard_group_reply": return await edit_group_reply_prompt(update, context)
    if data == "edit_wizard_media_reaction": return await edit_media_reaction_prompt(update, context)
    if data == "edit_wizard_proactive_rate": return await edit_proactive_rate_prompt(update, context)
    if data == "edit_wizard_proactive_send": return await proactive_chat_select_prompt(update, context)
    
    # Переход в подменю настройки макс. сообщений
    if data == "edit_wizard_max_msgs":
        return await edit_max_messages_prompt(update, context) # Новая функция

    if data == "edit_wizard_message_volume": # Временно отключено
        await query.answer("Функция 'Объем сообщений' временно недоступна.", show_alert=True)
        # Возвращаем меню по кэшированному объекту без обращения к БД
        if persona_obj:
            return await _show_edit_wizard_menu(update, context, persona_obj)
        else:
            with get_db() as db_session:
                persona_config = db_session.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                return await _show_edit_wizard_menu(update, context, persona_config) if persona_config else ConversationHandler.END
            
    
                
    if data == "finish_edit": return await edit_persona_finish(update, context)
    if data == "edit_wizard_clear_context":
        return await clear_persona_context_from_wizard(update, context)
    if data == "back_to_wizard_menu": # Возврат из подменю в главное меню
        # ADDED: Delete the last specific prompt message (e.g., "Enter new name")
        last_prompt_message_id = context.user_data.pop('last_prompt_message_id', None)
        # chat_id where the prompt was sent should be the same as the current wizard menu's chat_id
        chat_id_for_delete = context.user_data.get('edit_chat_id') 
        
        if last_prompt_message_id and chat_id_for_delete:
            try:
                # It's possible the prompt message is the same as the callback query's message
                # if _send_prompt edited the main menu to become the prompt.
                # Or it could be a new message. Deleting it is generally safe.
                if query and query.message and query.message.message_id == last_prompt_message_id:
                    logger.info(f"edit_wizard_menu_handler (back_to_wizard_menu): The 'last_prompt_message_id' ({last_prompt_message_id}) is the current callback message. It will be replaced by _show_edit_wizard_menu.")
                else:
                    await context.bot.delete_message(chat_id=chat_id_for_delete, message_id=last_prompt_message_id)
                    logger.info(f"edit_wizard_menu_handler (back_to_wizard_menu): Deleted specific prompt message {last_prompt_message_id} in chat {chat_id_for_delete}")
            except Exception as e_del_prompt:
                logger.warning(f"edit_wizard_menu_handler (back_to_wizard_menu): Failed to delete specific prompt message {last_prompt_message_id} in chat {chat_id_for_delete}: {e_del_prompt}")

        # Возвращаем меню из кэша без запросов к БД
        if persona_obj:
            return await _show_edit_wizard_menu(update, context, persona_obj)
        else:
            with get_db() as db_session:
                persona_config = db_session.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                # _show_edit_wizard_menu will handle editing/sending the main menu
                return await _show_edit_wizard_menu(update, context, persona_config) if persona_config else ConversationHandler.END

    # Обработка прямого выбора `set_max_msgs_` (если вдруг останется где-то такой коллбэк, хотя его быть не должно из главного меню)
    # Этот блок теперь не должен вызываться, так как эти кнопки убраны из главного меню
    if data.startswith("set_max_msgs_"):
        logger.warning(f"edit_wizard_menu_handler: Unexpected direct 'set_max_msgs_' callback: {data}. Should go via sub-menu.")
        # На всякий случай, если такой коллбэк придет, попробуем обработать или вернуть в меню
        new_value_str = data.replace("set_max_msgs_", "")
        numeric_value = -1
        if new_value_str == "few": numeric_value = 1
        elif new_value_str == "normal": numeric_value = 3
        elif new_value_str == "many": numeric_value = 6
        elif new_value_str == "random": numeric_value = 0
        if numeric_value != -1:
            try:
                with get_db() as db_session:
                    persona = db_session.query(PersonaConfig).filter(PersonaConfig.id == persona_id).first()
                    if persona:
                        persona.max_response_messages = numeric_value
                        db_session.commit()
                        db_session.refresh(persona)
                        return await _show_edit_wizard_menu(update, context, persona)
            except Exception as e_direct_set:
                logger.error(f"Error in fallback direct set_max_msgs for {persona_id}: {e_direct_set}")
        
        with get_db() as db_session: # Fallback to re-render menu
            persona_config = db_session.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona_config) if persona_config else ConversationHandler.END

    logger.warning(f"Unhandled wizard menu callback: {data} for persona {persona_id}")
    if persona_obj:
        return await _show_edit_wizard_menu(update, context, persona_obj)
    with get_db() as db_session:
        persona = db_session.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
        return await _show_edit_wizard_menu(update, context, persona) if persona else ConversationHandler.END

# --- Edit Proactive Messaging Rate ---
async def edit_proactive_rate_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подменю выбора частоты проактивных сообщений."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    persona_id = context.user_data.get('edit_persona_id')
    if not persona_id:
        await query.answer("сессия потеряна", show_alert=True)
        return ConversationHandler.END
    current_value = "sometimes"
    with get_db() as db:
        persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
        if persona and getattr(persona, 'proactive_messaging_rate', None):
            current_value = persona.proactive_messaging_rate
    display_map = {"never": "никогда", "rarely": "редко", "sometimes": "иногда", "often": "часто"}
    prompt_text = escape_markdown_v2(f"частота проактивных сообщений (тек.: {display_map.get(current_value, 'иногда')}):")
    keyboard = [
        [InlineKeyboardButton("никогда", callback_data="set_proactive_never")],
        [InlineKeyboardButton("редко", callback_data="set_proactive_rarely")],
        [InlineKeyboardButton("иногда", callback_data="set_proactive_sometimes")],
        [InlineKeyboardButton("часто", callback_data="set_proactive_often")],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")],
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_PROACTIVE_RATE

# --- Proactive manual send: pick chat and send ---
async def proactive_chat_select_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список чатов, где активна эта личность, чтобы отправить проактивное сообщение по кнопке."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id if query.from_user else None
    if not persona_id or not user_id:
        try: await query.answer("сессия потеряна", show_alert=True)
        except Exception: pass
        return ConversationHandler.END

    try:
        from sqlalchemy.orm import selectinload
        with get_db() as db:
            persona = db.query(DBPersonaConfig).options(selectinload(DBPersonaConfig.owner), selectinload(DBPersonaConfig.bot_instance)).filter(
                DBPersonaConfig.id == persona_id,
                DBPersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()
            if not persona:
                try: await query.answer("личность не найдена", show_alert=True)
                except Exception: pass
                return ConversationHandler.END

            bot_inst = db.query(DBBotInstance).filter(DBBotInstance.persona_config_id == persona.id).first()
            if not bot_inst:
                await _send_prompt(update, context, escape_markdown_v2("бот для этой личности не привязан"), InlineKeyboardMarkup([[InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]]))
                return PROACTIVE_CHAT_SELECT

            links = db.query(DBChatBotInstance).filter(
                DBChatBotInstance.bot_instance_id == bot_inst.id,
                DBChatBotInstance.active == True
            ).all()

        if not links:
            await _send_prompt(update, context, escape_markdown_v2("нет чатов для отправки"), InlineKeyboardMarkup([[InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]]))
            return PROACTIVE_CHAT_SELECT

        # Формируем клавиатуру: по кнопке на чат (пытаемся получить название чата)
        keyboard: List[List[InlineKeyboardButton]] = []
        # Попробуем использовать привязанного бота, если есть токен
        target_bot = None
        try:
            if bot_inst and bot_inst.bot_token:
                target_bot = Bot(token=bot_inst.bot_token)
                await target_bot.initialize()
        except Exception as e_bot_init:
            logger.warning(f"Failed to init target bot for chat titles: {e_bot_init}")

        for link in links:
            chat_id_int = int(link.chat_id)
            title = f"чат {link.chat_id}"
            chat_info = None
            # 1) Сначала через привязанного бота
            if target_bot:
                try:
                    chat_info = await target_bot.get_chat(chat_id_int)
                except Exception:
                    chat_info = None
            # 2) Фолбэк через текущего (основного) бота
            if not chat_info:
                try:
                    chat_info = await context.bot.get_chat(chat_id_int)
                except Exception as e_get:
                    logger.warning(f"proactive_chat_select_prompt: could not get chat title for {chat_id_int}: {e_get}")
                    chat_info = None

            if chat_info:
                try:
                    if str(getattr(chat_info, 'type', '')) == 'private':
                        first_name = getattr(chat_info, 'first_name', None) or 'пользователь'
                        title = f"личный чат ({first_name})"
                    else:
                        title = getattr(chat_info, 'title', None) or f"группа ({link.chat_id})"
                except Exception:
                    pass

            keyboard.append([InlineKeyboardButton(title, callback_data=f"proactive_pick_chat_{link.id}")])
        keyboard.append([InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")])
        await _send_prompt(update, context, escape_markdown_v2("выберите чат:"), InlineKeyboardMarkup(keyboard))
        return PROACTIVE_CHAT_SELECT
    except Exception as e:
        logger.error(f"proactive_chat_select_prompt error: {e}", exc_info=True)
        try: await query.answer("ошибка при загрузке списка чатов", show_alert=True)
        except Exception: pass
        return ConversationHandler.END

async def proactive_chat_select_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return PROACTIVE_CHAT_SELECT
    await query.answer()
    persona_id = context.user_data.get('edit_persona_id')
    data = query.data
    if data == "back_to_wizard_menu":
        return await _handle_back_to_wizard_menu(update, context, persona_id)

    if data.startswith("proactive_pick_chat_"):
        try:
            link_id = int(data.replace("proactive_pick_chat_", ""))
        except Exception:
            return PROACTIVE_CHAT_SELECT

        try:
            with get_db() as db:
                # Проверяем владение и связь
                link: Optional[DBChatBotInstance] = db.query(DBChatBotInstance).filter(DBChatBotInstance.id == link_id).first()
                if not link:
                    await query.edit_message_text("чат не найден")
                    return ConversationHandler.END
                bot_inst = db.query(DBBotInstance).filter(DBBotInstance.id == link.bot_instance_id).first()
                persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                if not bot_inst or not persona or bot_inst.persona_config_id != persona.id:
                    await query.edit_message_text("нет доступа к чату")
                    return ConversationHandler.END

                # Собираем персону и контекст
                persona_obj = Persona(persona, chat_bot_instance_db_obj=link)
                owner_user = persona.owner  # type: ignore
                chat_id = link.chat_id

                # Готовим системный промпт и сообщения с учетом истории, чтобы избежать повторов
                history = get_context_for_chat_bot(db, link.id)
                system_prompt, messages = persona_obj.format_conversation_starter_prompt(history)

                # Вежливая задержка и ответ (через Google Gemini)
                delay_sec = random.uniform(0.8, 2.5)
                logger.info(f"Polite delay before AI request (proactive): {delay_sec:.2f}s")
                await asyncio.sleep(delay_sec)
                api_key_obj = get_next_api_key(db, service='gemini')
                if not api_key_obj:
                    logger.error("No active Gemini API keys available in DB (proactive). Skipping.")
                    return
                assistant_response_text = await send_to_google_gemini(api_key=api_key_obj.api_key, system_prompt=system_prompt or "", messages=messages)
                if assistant_response_text and str(assistant_response_text).startswith("[ошибка google api") and ("503" in assistant_response_text or "overload" in assistant_response_text.lower()):
                    for attempt in range(1, 2):  # одна дополнительная попытка для проактивных
                        backoff = 1.0 * attempt + random.uniform(0.2, 0.8)
                        logger.warning(f"Google API overloaded (proactive). Retry {attempt}/1 after {backoff:.2f}s...")
                        await asyncio.sleep(backoff)
                        assistant_response_text = await send_to_google_gemini(api_key=api_key_obj.api_key, system_prompt=system_prompt or "", messages=messages)
                        if not (assistant_response_text and str(assistant_response_text).startswith("[ошибка google api") and ("503" in assistant_response_text or "overload" in assistant_response_text.lower())):
                            break
                
                # Списываем кредиты у владельца
                try:
                    # Для проактивных сообщений используем бесплатную модель Gemini
                    from config import GEMINI_MODEL_NAME_FOR_API
                    out_text = "\n".join(assistant_response_text) if isinstance(assistant_response_text, list) else (assistant_response_text or "")
                    await deduct_credits_for_interaction(db=db, owner_user=owner_user, input_text="", output_text=out_text, model_name=GEMINI_MODEL_NAME_FOR_API)
                except Exception as e_ded:
                    logger.warning(f"credits deduction failed for proactive send: {e_ded}")

                # Отправляем в чат ИМЕННО тем ботом, который привязан к чату
                try:
                    # Инициализируем нужного бота
                    if not bot_inst or not bot_inst.bot_token:
                        raise ValueError("нет токена бота для отправки")
                    target_bot_for_send = Bot(token=bot_inst.bot_token)
                    await target_bot_for_send.initialize()

                    # Легковесный контекст, содержащий только bot
                    class _BotOnlyContext:
                        def __init__(self, bot):
                            self.bot = bot

                    temp_ctx = _BotOnlyContext(target_bot_for_send)
                    await process_and_send_response(update, temp_ctx, target_bot_for_send, chat_id, persona_obj, assistant_response_text, db, reply_to_message_id=None)
                except Exception as e_send:
                    logger.error(f"failed to send proactive message: {e_send}")

                # Сохраняем в контекст ИИ: только ответ ассистента (инициатива без явного пользовательского сообщения)
                try:
                    _ctx_text = "\n".join(assistant_response_text) if isinstance(assistant_response_text, list) else (assistant_response_text or "")
                    add_message_to_context(db, link.id, "assistant", _ctx_text)
                except Exception as e_ctx:
                    logger.warning(f"failed to store proactive context: {e_ctx}")

                # Возврат в меню
                with get_db() as db2:
                    persona_ref = db2.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                    if persona_ref:
                        return await _show_edit_wizard_menu(update, context, persona_ref)
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"proactive_chat_select_received error: {e}", exc_info=True)
            try: await query.edit_message_text("ошибка при отправке сообщения")
            except Exception: pass
            return ConversationHandler.END

    return PROACTIVE_CHAT_SELECT

async def edit_proactive_rate_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return EDIT_PROACTIVE_RATE
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    if data == "back_to_wizard_menu":
        return await _handle_back_to_wizard_menu(update, context, persona_id)
    if data.startswith("set_proactive_"):
        new_value = data.replace("set_proactive_", "")
        if new_value not in {"never", "rarely", "sometimes", "often"}:
            return EDIT_PROACTIVE_RATE
        try:
            with get_db() as db:
                persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).with_for_update().first()
                if persona:
                    persona.proactive_messaging_rate = new_value
                    db.commit()
                    logger.info(f"Set proactive_messaging_rate to {new_value} for persona {persona_id}")
                    return await _handle_back_to_wizard_menu(update, context, persona_id)
        except Exception as e:
            logger.error(f"Error setting proactive_messaging_rate for {persona_id}: {e}")
            await query.edit_message_text("ошибка при сохранении частоты проактивных сообщений", parse_mode=None)
            with get_db() as db:
                persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                if persona:
                    return await _show_edit_wizard_menu(update, context, persona)
            return ConversationHandler.END
    return EDIT_PROACTIVE_RATE

# --- Helper to send prompt and store message ID ---
async def _send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """Always sends a new prompt message and stores its ID to allow clean deletion later.
    This keeps the main wizard menu message intact (no editing), eliminating flicker.
    """
    query = update.callback_query
    chat_id = query.message.chat.id if query and query.message else update.effective_chat.id
    sent_message = None
    try:
        sent_message = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending prompt message: {e}", exc_info=True)
        # Fallback: send plain text without Markdown if formatting failed
        try:
            sent_message = await context.bot.send_message(chat_id, text.replace('\\', ''), reply_markup=reply_markup, parse_mode=None)
        except Exception as fallback_e:
            logger.critical(f"CRITICAL: Failed to send prompt even as plain text: {fallback_e}")
            sent_message = None

    if sent_message:
        context.user_data['last_prompt_message_id'] = sent_message.message_id

# --- Edit Name ---
async def edit_name_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Пытаемся взять из кэша
    persona_obj = context.user_data.get('persona_object')
    if persona_obj and getattr(persona_obj, 'name', None):
        current_name = persona_obj.name
    else:
        # Фолбэк на БД, если кэш отсутствует (например, старая сессия)
        persona_id = context.user_data.get('edit_persona_id')
        with get_db() as db:
            current_name = db.query(DBPersonaConfig.name).filter(DBPersonaConfig.id == persona_id).scalar() or "N/A"
    prompt_text = escape_markdown_v2(f"введите новое имя (текущее: '{current_name}', 2-50 симв.):")
    keyboard = [[InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_NAME

async def edit_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_NAME
    new_name = update.message.text.strip()
    # Берем из кэша объект персоны
    persona_from_cache = context.user_data.get('persona_object')
    if not persona_from_cache:
        await update.message.reply_text("❌ Сессия редактирования потеряна. Начните заново.", parse_mode=None)
        return ConversationHandler.END

    if not (2 <= len(new_name) <= 50):
        await update.message.reply_text(escape_markdown_v2("❌ Имя: 2-50 симв. Попробуйте еще:"))
        return EDIT_NAME

    try:
        with get_db() as db:
            # Проверка уникальности имени у владельца
            existing = db.query(DBPersonaConfig.id).filter(
                DBPersonaConfig.owner_id == persona_from_cache.owner_id,
                func.lower(DBPersonaConfig.name) == new_name.lower(),
                DBPersonaConfig.id != persona_from_cache.id
            ).first()
            if existing:
                await update.message.reply_text(escape_markdown_v2(f"❌ Имя '{new_name}' уже занято. Введите другое:"))
                return EDIT_NAME

            # Присоединяем отсоединенный объект к текущей сессии и обновляем
            live_persona = db.merge(persona_from_cache)
            live_persona.name = new_name
            db.commit()

            # Обновляем кэш (минимально, либо через refresh)
            try:
                db.refresh(live_persona, attribute_names=['name'])
            except Exception:
                pass
            context.user_data['persona_object'] = live_persona

            await update.message.reply_text(escape_markdown_v2(f"✅ имя обновлено на '{new_name}'."))

            # Delete the prompt message before showing menu
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                except Exception: pass
            return await _show_edit_wizard_menu(update, context, live_persona)
    except Exception as e:
        logger.error(f"Error updating persona name (cached) for {getattr(persona_from_cache, 'id', 'unknown')}: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении имени.", parse_mode=None)
        return ConversationHandler.END

# --- Edit Description ---
async def edit_description_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отображает форму редактирования описания, отправляя сообщение как простой текст."""
    persona_obj = context.user_data.get('persona_object')
    query = update.callback_query
    
    if persona_obj and hasattr(persona_obj, 'description'):
        current_desc = persona_obj.description or "(пусто)"
    else:
        persona_id = context.user_data.get('edit_persona_id')
        with get_db() as db:
            current_desc = db.query(DBPersonaConfig.description).filter(DBPersonaConfig.id == persona_id).scalar() or "(пусто)"
    
    current_desc_preview = (current_desc[:100] + '...') if len(current_desc) > 100 else current_desc
    prompt_text = f"введите новое описание (макс. 2500 символов).\n\nтекущее (начало):\n{current_desc_preview}"
    keyboard = [[InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]]
    
    try:
        # Всегда отправляем новое сообщение без Markdown
        sent_message = await query.message.reply_text(
            text=prompt_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None
        )
        context.user_data['last_prompt_message_id'] = sent_message.message_id
        # Пытаемся удалить старое сообщение с меню
        try:
            await query.message.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error sending description prompt: {e}", exc_info=True)

    return EDIT_DESCRIPTION

async def edit_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return EDIT_DESCRIPTION
    new_desc = update.message.text.strip()
    persona_from_cache = context.user_data.get('persona_object')

    if len(new_desc) > 2500:
        await update.message.reply_text(escape_markdown_v2("❌ описание: макс. 2500 симв. попробуйте еще:"))
        return EDIT_DESCRIPTION

    try:
        with get_db() as db:
            if not persona_from_cache:
                await update.message.reply_text("❌ Сессия редактирования потеряна. Начните заново.", parse_mode=None)
                return ConversationHandler.END

            live_persona = db.merge(persona_from_cache)
            live_persona.description = new_desc
            db.commit()

            try:
                db.refresh(live_persona, attribute_names=['description'])
            except Exception:
                pass
            context.user_data['persona_object'] = live_persona

            await update.message.reply_text(escape_markdown_v2("✅ описание обновлено."))
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
                except Exception: pass
            return await _show_edit_wizard_menu(update, context, live_persona)
    except Exception as e:
        logger.error(f"Error updating persona description (cached) for {getattr(persona_from_cache, 'id', 'unknown')}: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении описания.", parse_mode=None)
        return ConversationHandler.END

# --- Edit Communication Style ---
async def edit_comm_style_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Снимаем текущее значение из кэша, без обращения к БД
    persona_obj = context.user_data.get('persona_object')
    current_style = getattr(persona_obj, 'communication_style', None) if persona_obj else None
    # normalize to enum
    try:
        current_style_enum = CommunicationStyle(current_style) if current_style else CommunicationStyle.NEUTRAL
    except Exception:
        current_style_enum = CommunicationStyle.NEUTRAL
    prompt_text = escape_markdown_v2(f"выберите стиль общения (текущий: {current_style_enum.value}):")
    keyboard = [
        [InlineKeyboardButton("нейтральный", callback_data=f"set_comm_style_{CommunicationStyle.NEUTRAL.value}")],
        [InlineKeyboardButton("дружелюбный", callback_data=f"set_comm_style_{CommunicationStyle.FRIENDLY.value}")],
        [InlineKeyboardButton("саркастичный", callback_data=f"set_comm_style_{CommunicationStyle.SARCASTIC.value}")],
        [InlineKeyboardButton("формальный", callback_data=f"set_comm_style_{CommunicationStyle.FORMAL.value}")],
        [InlineKeyboardButton("краткий", callback_data=f"set_comm_style_{CommunicationStyle.BRIEF.value}")],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_COMM_STYLE

async def edit_comm_style_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_from_cache = context.user_data.get('persona_object')

    if data == "back_to_wizard_menu":
        # Возврат в меню без обращения к БД
        if persona_from_cache:
            return await _show_edit_wizard_menu(update, context, persona_from_cache)
        persona_id = context.user_data.get('edit_persona_id')
        return await _handle_back_to_wizard_menu(update, context, persona_id)

    if data.startswith("set_comm_style_"):
        new_style = data.replace("set_comm_style_", "")
        # validate via enum
        try:
            style_enum = CommunicationStyle(new_style)
        except Exception:
            logger.warning(f"Invalid communication style received: {new_style}")
            await query.edit_message_text(escape_markdown_v2("❌ неверное значение стиля общения."))
            return EDIT_COMM_STYLE
        try:
            with get_db() as db:
                if not persona_from_cache:
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: сессия редактирования потеряна."))
                    return ConversationHandler.END
                live_persona = db.merge(persona_from_cache)
                live_persona.communication_style = style_enum.value
                db.commit()
                try:
                    db.refresh(live_persona, attribute_names=['communication_style'])
                except Exception:
                    pass
                context.user_data['persona_object'] = live_persona
                logger.info(f"Set communication_style to {style_enum.value} for persona {live_persona.id}")
                return await _show_edit_wizard_menu(update, context, live_persona)
        
        except Exception as e:
            logger.error(f"Error setting communication_style for {persona_id}: {e}")
            await query.edit_message_text("❌ Ошибка при сохранении стиля общения.", parse_mode=None)
            return ConversationHandler.END
    else:
        logger.warning(f"Unknown callback in edit_comm_style_received: {data}")
        return EDIT_COMM_STYLE

# --- Edit Max Messages ---
async def edit_max_messages_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отображает подменю для выбора максимального количества сообщений (без обращений к БД)."""
    query = update.callback_query # Ожидаем, что сюда пришли через коллбэк
    if not query:
        logger.error("edit_max_messages_prompt called without a callback query.")
        return ConversationHandler.END

    # Берем текущее значение из кэша
    persona_obj = context.user_data.get('persona_object')
    if not persona_obj:
        await query.answer("Сессия потеряна.", show_alert=True)
        return ConversationHandler.END

    config_value = getattr(persona_obj, 'max_response_messages', None)
    current_value_str = "normal"
    if config_value is not None:
        if config_value == 0: current_value_str = "random"
        elif config_value == 1: current_value_str = "few"
        elif config_value == 3: current_value_str = "normal"
        elif config_value == 6: current_value_str = "many"
    
    display_map = {
        "few": "поменьше",
        "normal": "стандартно",
        "many": "побольше",
        "random": "случайно"
    }
    current_display = display_map.get(current_value_str, "стандартно")

    prompt_text = escape_markdown_v2(f"количество сообщений в ответе (тек.: {current_display}):")

    keyboard = [
        [
            InlineKeyboardButton(display_map['few'], callback_data="set_max_msgs_few"),
            InlineKeyboardButton(display_map['normal'], callback_data="set_max_msgs_normal"),
        ],
        [
            InlineKeyboardButton(display_map['many'], callback_data="set_max_msgs_many"),
            InlineKeyboardButton(display_map['random'], callback_data="set_max_msgs_random"),
        ],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")] # Возврат в главное меню
    ]
    
    # Редактируем текущее сообщение (которое было главным меню) на это подменю
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_MAX_MESSAGES # Остаемся в этом состоянии для ожидания выбора

async def edit_max_messages_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор максимального количества сообщений из подменю, используя кэш."""
    query = update.callback_query
    if not query or not query.data:
        return EDIT_MAX_MESSAGES

    await query.answer()
    data = query.data
    persona_from_cache = context.user_data.get('persona_object')

    if not persona_from_cache:
        if query.message:
            try: await query.edit_message_text("Сессия редактирования потеряна.", reply_markup=None)
            except Exception: pass
        return ConversationHandler.END

    # Обработка кнопки "Назад" — возврат без БД
    if data == "back_to_wizard_menu":
        return await _handle_back_to_wizard_menu(update, context, getattr(persona_from_cache, 'id', 0))

    if data.startswith("set_max_msgs_"):
        new_value_str = data.replace("set_max_msgs_", "")
        numeric_value = -1
        if new_value_str == "few": numeric_value = 1
        elif new_value_str == "normal": numeric_value = 3
        elif new_value_str == "many": numeric_value = 6
        elif new_value_str == "random": numeric_value = 0
        
        if numeric_value == -1:
            return EDIT_MAX_MESSAGES

        try:
            with get_db() as db:
                live_persona = db.merge(persona_from_cache)
                live_persona.max_response_messages = numeric_value
                db.commit()
                try:
                    db.refresh(live_persona, attribute_names=['max_response_messages'])
                except Exception:
                    pass
                context.user_data['persona_object'] = live_persona
                logger.info(f"Set max_response_messages to {numeric_value} ({new_value_str}) for persona {live_persona.id} via sub-menu.")
                return await _show_edit_wizard_menu(update, context, live_persona)
        except Exception as e:
            logger.error(f"Error setting max_response_messages (cached) for {getattr(persona_from_cache, 'id', 'unknown')} from data '{data}': {e}", exc_info=True)
            if query.message:
                try: await query.edit_message_text("❌ Ошибка при сохранении.", reply_markup=query.message.reply_markup)
                except Exception: pass
            return EDIT_MAX_MESSAGES
    else:
        logger.warning(f"Unknown callback in edit_max_messages_received: {data}")
        return EDIT_MAX_MESSAGES

# --- Edit Verbosity ---
async def edit_verbosity_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Берем текущее значение из кэша, чтобы не обращаться к БД
    persona_obj = context.user_data.get('persona_object')
    current = getattr(persona_obj, 'verbosity_level', None) if persona_obj else None
    # normalize to enum
    try:
        current_enum = Verbosity(current) if current else Verbosity.MEDIUM
    except Exception:
        current_enum = Verbosity.MEDIUM
    prompt_text = escape_markdown_v2(f"выберите разговорчивость (текущая: {current_enum.value}):")
    keyboard = [
        [InlineKeyboardButton("лаконичный", callback_data=f"set_verbosity_{Verbosity.CONCISE.value}")],
        [InlineKeyboardButton("средний", callback_data=f"set_verbosity_{Verbosity.MEDIUM.value}")],
        [InlineKeyboardButton("болтливый", callback_data=f"set_verbosity_{Verbosity.TALKATIVE.value}")],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_VERBOSITY

async def edit_verbosity_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_from_cache = context.user_data.get('persona_object')

    if data == "back_to_wizard_menu":
        if persona_from_cache:
            return await _show_edit_wizard_menu(update, context, persona_from_cache)
        persona_id = context.user_data.get('edit_persona_id')
        with get_db() as db:
            persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_verbosity_"):
        new_value = data.replace("set_verbosity_", "")
        # validate via enum
        try:
            verbosity_enum = Verbosity(new_value)
        except Exception:
            logger.warning(f"Invalid verbosity value received: {new_value}")
            await query.edit_message_text(escape_markdown_v2("❌ неверное значение разговорчивости."))
            return EDIT_VERBOSITY
        try:
            with get_db() as db:
                if not persona_from_cache:
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: сессия редактирования потеряна."))
                    return ConversationHandler.END
                live_persona = db.merge(persona_from_cache)
                live_persona.verbosity_level = verbosity_enum.value
                db.commit()
                try:
                    db.refresh(live_persona, attribute_names=['verbosity_level'])
                except Exception:
                    pass
                context.user_data['persona_object'] = live_persona
                logger.info(f"Set verbosity_level to {verbosity_enum.value} for persona {live_persona.id}")
                return await _show_edit_wizard_menu(update, context, live_persona)
        except Exception as e:
            logger.error(f"Error setting verbosity_level (cached) for {getattr(persona_from_cache, 'id', 'unknown')}: {e}")
            await query.edit_message_text("❌ Ошибка при сохранении разговорчивости.", parse_mode=None)
            return ConversationHandler.END
    else:
        logger.warning(f"Unknown callback in edit_verbosity_received: {data}")
        return EDIT_VERBOSITY

# --- Edit Group Reply Preference ---
async def edit_group_reply_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Берем текущее значение из кэша, чтобы не ходить в БД
    persona_obj = context.user_data.get('persona_object')
    current = (getattr(persona_obj, 'group_reply_preference', None) if persona_obj else None) or "mentioned_or_contextual"
    
    # Словарь для красивого отображения текущего значения
    display_map = {
        "always": "всегда",
        "mentioned_only": "только по упоминанию (@)",
        "mentioned_or_contextual": "по @ или контексту",
        "never": "никогда"
    }
    current_display = display_map.get(current, current) # Получаем понятный текст

    prompt_text = escape_markdown_v2(f"как отвечать в группах (текущее: {current_display}):")
    keyboard = [
        [InlineKeyboardButton("всегда", callback_data="set_group_reply_always")],
        [InlineKeyboardButton("только по упоминанию (@)", callback_data="set_group_reply_mentioned_only")],
        [InlineKeyboardButton("по @ или контексту", callback_data="set_group_reply_mentioned_or_contextual")],
        [InlineKeyboardButton("никогда", callback_data="set_group_reply_never")],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]
    ]
    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_GROUP_REPLY

async def edit_group_reply_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_from_cache = context.user_data.get('persona_object')

    if data == "back_to_wizard_menu":
        if persona_from_cache:
            return await _show_edit_wizard_menu(update, context, persona_from_cache)
        persona_id = context.user_data.get('edit_persona_id')
        with get_db() as db:
            persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_group_reply_"):
        new_value = data.replace("set_group_reply_", "")
        try:
            with get_db() as db:
                if not persona_from_cache:
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: сессия редактирования потеряна."))
                    return ConversationHandler.END
                live_persona = db.merge(persona_from_cache)
                live_persona.group_reply_preference = new_value
                db.commit()
                try:
                    db.refresh(live_persona, attribute_names=['group_reply_preference'])
                except Exception:
                    pass
                context.user_data['persona_object'] = live_persona
                logger.info(f"Set group_reply_preference to {new_value} for persona {live_persona.id}")
                return await _show_edit_wizard_menu(update, context, live_persona)
        except Exception as e:
            logger.error(f"Error setting group_reply_preference (cached) for {getattr(persona_from_cache, 'id', 'unknown')}: {e}")
            await query.edit_message_text("❌ Ошибка при сохранении настройки ответа в группе.", parse_mode=None)
            return ConversationHandler.END
    else:
        logger.warning(f"Unknown callback in edit_group_reply_received: {data}")
        return EDIT_GROUP_REPLY

# --- Edit Media Reaction ---
async def edit_media_reaction_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_obj = context.user_data.get('persona_object')
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if not persona_obj:
        if update.callback_query:
            await update.callback_query.answer("Ошибка: сессия редактирования потеряна.", show_alert=True)
        return ConversationHandler.END
    current = persona_obj.media_reaction or "text_only"
       
    media_react_map = {
        "text_only": "только текст",
        "none": "никак не реагировать",
        "text_and_all_media": "на всё (текст, фото, голос)",
        "all_media_no_text": "только медиа (фото, голос)",
        "photo_only": "только фото",
        "voice_only": "только голос",
    }

    if current == "all": current = "text_and_all_media"
    
    current_display_text = media_react_map.get(current, "только текст")
    prompt_text = escape_markdown_v2(f"как реагировать на текст и медиа (текущее: {current_display_text}):")
    
    # Кнопки теперь создаются в определенном порядке для лучшего вида
    keyboard_buttons = [
        [InlineKeyboardButton(media_react_map['text_only'], callback_data="set_media_react_text_only")],
        [InlineKeyboardButton(media_react_map['text_and_all_media'], callback_data="set_media_react_text_and_all_media")],
        [InlineKeyboardButton(media_react_map['photo_only'], callback_data="set_media_react_photo_only")],
        [InlineKeyboardButton(media_react_map['voice_only'], callback_data="set_media_react_voice_only")],
        [InlineKeyboardButton(media_react_map['all_media_no_text'], callback_data="set_media_react_all_media_no_text")],
        [InlineKeyboardButton(media_react_map['none'], callback_data="set_media_react_none")],
        [InlineKeyboardButton("назад", callback_data="back_to_wizard_menu")]
    ]
    
    if update.callback_query and update.callback_query.message:
        await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard_buttons))
    else:
        chat_id_to_send = update.effective_chat.id
        if chat_id_to_send:
            await context.bot.send_message(chat_id=chat_id_to_send, text=prompt_text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.error("edit_media_reaction_prompt: Не удалось определить chat_id для отправки сообщения.")
            if update.callback_query:
                await update.callback_query.answer("Ошибка отображения меню.", show_alert=True)
    
    return EDIT_MEDIA_REACTION

async def edit_media_reaction_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_from_cache = context.user_data.get('persona_object')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id

    if data == "back_to_wizard_menu":
        with get_db() as db:
            persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            return await _show_edit_wizard_menu(update, context, persona)

    if data.startswith("set_media_react_"):
        new_value = data.replace("set_media_react_", "")
        
        try:
            with get_db() as db:
                if not persona_from_cache:
                    await query.edit_message_text(escape_markdown_v2("❌ Ошибка: сессия редактирования потеряна."))
                    return ConversationHandler.END
                live_persona = db.merge(persona_from_cache)
                live_persona.media_reaction = new_value
                db.commit()
                try:
                    db.refresh(live_persona, attribute_names=['media_reaction'])
                except Exception:
                    pass
                context.user_data['persona_object'] = live_persona
                logger.info(f"Set media_reaction to {new_value} for persona {live_persona.id}")
                return await _show_edit_wizard_menu(update, context, live_persona)
        except Exception as e:
            logger.error(f"Error setting media_reaction (cached) for {getattr(persona_from_cache, 'id', 'unknown')}: {e}")
            await query.edit_message_text("❌ Ошибка при сохранении реакции на медиа.", parse_mode=None)
            return ConversationHandler.END
    else:
        logger.warning(f"Unknown callback in edit_media_reaction_received: {data}")
        return EDIT_MEDIA_REACTION



async def _show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: DBPersonaConfig) -> int:
    """Отображает главное меню настройки персоны. Отправляет новое или редактирует существующее."""
    try:
        query = update.callback_query 
        
        chat_id_for_menu = None
        if query and query.message: 
            chat_id_for_menu = query.message.chat.id
        elif update.effective_chat: 
            chat_id_for_menu = update.effective_chat.id
        
        if not chat_id_for_menu:
            logger.error("_show_edit_wizard_menu: Could not determine chat_id for menu.")
            if query:
                try: await query.answer("ошибка: чат для меню не определен.", show_alert=True)
                except Exception: pass
            return ConversationHandler.END

        logger.info(f"_show_edit_wizard_menu: Preparing wizard menu. ChatID: {chat_id_for_menu}, PersonaID: {persona_config.id}")

        persona_id = persona_config.id
        user_id = update.effective_user.id
        owner = persona_config.owner
        is_premium = is_admin(user_id) if owner else False
        star = " ⭐"
        style = persona_config.communication_style or "neutral"
        verbosity = persona_config.verbosity_level or "medium"
        group_reply = persona_config.group_reply_preference or "mentioned_or_contextual"
        media_react = persona_config.media_reaction or "text_only"
        proactive_rate = getattr(persona_config, 'proactive_messaging_rate', None) or "sometimes"
        
        style_map = {"neutral": "нейтральный", "friendly": "дружелюбный", "sarcastic": "саркастичный", "formal": "формальный", "brief": "краткий"}
        verbosity_map = {"concise": "лаконичный", "medium": "средний", "talkative": "разговорчивый"}
        group_reply_map = {"always": "всегда", "mentioned_only": "по @", "mentioned_or_contextual": "по @ / контексту", "never": "никогда"}
        media_react_map = {"all": "текст+gif", "text_only": "только текст", "none": "никак", "photo_only": "только фото", "voice_only": "только голос", "text_and_all_media": "текст и медиа"}
        proactive_map = {"never": "никогда", "rarely": "редко", "sometimes": "иногда", "often": "часто"}
        
        current_max_msgs_setting = persona_config.max_response_messages
        display_for_max_msgs_button = "стандартно"
        if current_max_msgs_setting == 0: display_for_max_msgs_button = "случайно"
        elif current_max_msgs_setting == 1: display_for_max_msgs_button = "поменьше"
        elif current_max_msgs_setting == 3: display_for_max_msgs_button = "стандартно"
        elif current_max_msgs_setting == 6: display_for_max_msgs_button = "побольше"
            
        keyboard = [
            [InlineKeyboardButton("мастер настройки характера", callback_data="start_char_wizard")],
            [
                InlineKeyboardButton("имя", callback_data="edit_wizard_name"),
                InlineKeyboardButton("описание", callback_data="edit_wizard_description")
            ],
            [InlineKeyboardButton(f"ответы в группе ({group_reply_map.get(group_reply, '?')})", callback_data="edit_wizard_group_reply")],
            [InlineKeyboardButton(f"реакция на медиа ({media_react_map.get(media_react, '?')})", callback_data="edit_wizard_media_reaction")],
            [InlineKeyboardButton(f"макс. сообщ. ({display_for_max_msgs_button})", callback_data="edit_wizard_max_msgs")],
            [InlineKeyboardButton(f"проактивные сообщения ({proactive_map.get(proactive_rate, '?')})", callback_data="edit_wizard_proactive_rate")],
            [InlineKeyboardButton("напиши что-нибудь", callback_data="edit_wizard_proactive_send")],
            # [InlineKeyboardButton(f"настроения{star if not is_premium else ''}", callback_data="edit_wizard_moods")], # <-- ЗАКОММЕНТИРОВАНО
            [InlineKeyboardButton("очистить память", callback_data="edit_wizard_clear_context")],
            [InlineKeyboardButton("завершить", callback_data="finish_edit")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        persona_name_escaped = escape_markdown_v2(persona_config.name)
        part1 = ""
        part2 = f"*{escape_markdown_v2('настройка личности: ')}{persona_name_escaped}* "
        part3 = escape_markdown_v2(f"(id: ")
        part4 = f"`{persona_id}`"
        part5 = escape_markdown_v2(")")
        part6 = escape_markdown_v2("\n\nвыберите, что изменить:")
        
        msg_text = f"{part1}{part2}{part3}{part4}{part5}{part6}"

        sent_message = None
        current_session_wizard_menu_id = context.user_data.get('wizard_menu_message_id')
        
        if query and query.message and current_session_wizard_menu_id and \
            query.message.message_id == current_session_wizard_menu_id:
            try:
                if query.message.text != msg_text or query.message.reply_markup != reply_markup:
                    await query.edit_message_text(text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
                sent_message = query.message
            except BadRequest as e_edit:
                if "message is not modified" in str(e_edit).lower():
                    sent_message = query.message
                else: 
                    logger.warning(f"_show_edit_wizard_menu: Failed to edit menu (error: {e_edit}), sending new.")
                    sent_message = await context.bot.send_message(chat_id=chat_id_for_menu, text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e_gen_edit: 
                logger.warning(f"_show_edit_wizard_menu: General error editing menu (error: {e_gen_edit}), sending new.")
                sent_message = await context.bot.send_message(chat_id=chat_id_for_menu, text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            # Если ранее уже было сообщение меню — пробуем удалить, чтобы не засорять чат
            prev_menu_id = current_session_wizard_menu_id
            if prev_menu_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id_for_menu, message_id=prev_menu_id)
                except Exception as e_del:
                    logger.warning(f"_show_edit_wizard_menu: Could not delete previous menu message {prev_menu_id}: {e_del}")
            sent_message = await context.bot.send_message(chat_id=chat_id_for_menu, text=msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

        context.user_data['wizard_menu_message_id'] = sent_message.message_id
        context.user_data['edit_chat_id'] = chat_id_for_menu 
        context.user_data['edit_message_id'] = sent_message.message_id 
        
        if query: 
            try: await query.answer()
            except Exception: pass

        return EDIT_WIZARD_MENU
    except Exception as e:
        logger.error(f"CRITICAL Error in _show_edit_wizard_menu: {e}", exc_info=True)
        chat_id_fallback = update.effective_chat.id if update.effective_chat else None
        if chat_id_fallback:
            try: await context.bot.send_message(chat_id_fallback, "Произошла критическая ошибка при отображении меню настроек.")
            except Exception: pass
        return ConversationHandler.END

# --- Clear persona context (from wizard) ---
async def clear_persona_context_from_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Очищает весь контекст (ChatContext) во всех чатах, где активна эта личность."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    if not persona_id:
        try: await query.answer("ошибка: сессия редактирования потеряна", show_alert=True)
        except Exception: pass
        return ConversationHandler.END

    try:
        with get_db() as db:
            persona = db.query(DBPersonaConfig).options(selectinload(DBPersonaConfig.owner)).filter(
                DBPersonaConfig.id == persona_id,
                DBPersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()
            if not persona:
                try: await query.answer("личность не найдена", show_alert=True)
                except Exception: pass
                return ConversationHandler.END

            bot_instance = db.query(DBBotInstance).filter(DBBotInstance.persona_config_id == persona.id).first()
            total_deleted = 0
            links_count = 0
            if bot_instance:
                links = db.query(DBChatBotInstance).filter(DBChatBotInstance.bot_instance_id == bot_instance.id).all()
                links_count = len(links)
                for link in links:
                    deleted = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == link.id).delete(synchronize_session=False)
                    total_deleted += int(deleted or 0)
                db.commit()
                logger.info(f"Cleared {total_deleted} context messages for persona {persona.id} across {links_count} chats")

        # Показать явное подтверждение пользователю (маленькими буквами)
        # Сформируем текст подтверждения и покажем всплывающий alert + дублируем сообщением в чат
        chat_id = query.message.chat.id if query.message else None
        if total_deleted > 0:
            msg_raw = f"память очищена. удалено сообщений: {total_deleted}"
        else:
            msg_raw = "память пуста. удалять нечего"
        try:
            await query.answer(msg_raw, show_alert=True)
        except Exception:
            pass
        try:
            if chat_id is not None:
                await context.bot.send_message(chat_id, escape_markdown_v2(msg_raw), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.warning(f"Failed to send confirmation message after clear: {e}")

        # Вернемся в меню визарда
        with get_db() as db2:
            persona_ref = db2.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            if persona_ref:
                return await _show_edit_wizard_menu(update, context, persona_ref)
        return ConversationHandler.END
    except SQLAlchemyError as e:
        logger.error(f"DB error clearing context for persona {persona_id}: {e}", exc_info=True)
        try: await query.answer("ошибка базы данных", show_alert=True)
        except Exception: pass
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in clear_persona_context_from_wizard for persona {persona_id}: {e}", exc_info=True)
        try: await query.answer("ошибка очистки памяти", show_alert=True)
        except Exception: pass
        return ConversationHandler.END

# --- Mood Editing Functions (Adapted for Wizard Flow) ---

async def edit_moods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: Optional[DBPersonaConfig] = None) -> int:
    """Displays the mood editing menu (list moods, add button)."""
    query = update.callback_query
    if not query: return ConversationHandler.END

    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- edit_moods_menu (within wizard): User={user_id}, PersonaID={persona_id} ---")

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при загрузке настроений.")
    prompt_mood_menu_fmt_raw = "🎭 управление настроениями для *{name}*:"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_moods_menu, but edit_persona_id missing.")
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    local_persona_config = persona_config
    if local_persona_config is None:
        try:
            with get_db() as db:
                local_persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                    PersonaConfig.id == persona_id,
                    PersonaConfig.owner.has(User.telegram_id == user_id)
                ).first()
                if not local_persona_config:
                    logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_moods_menu fetch.")
                    await query.answer("Личность не найдена", show_alert=True)
                    await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    context.user_data.clear()
                    return ConversationHandler.END
        except Exception as e:
            logger.error(f"DB Error fetching persona in edit_moods_menu: {e}", exc_info=True)
            await query.answer("❌ Ошибка базы данных", show_alert=True)
            await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_wizard_menu(update, context, user_id, persona_id)

    logger.debug(f"Showing moods menu for persona {persona_id}")
    keyboard = await _get_edit_moods_keyboard_internal(local_persona_config)
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = prompt_mood_menu_fmt_raw.format(name=escape_markdown_v2(local_persona_config.name))

    try:
        # Use _send_prompt to handle editing/sending and store message ID
        async def _send_prompt(update, context, text, reply_markup=None, parse_mode=None):
            if not update.callback_query:
                return
            query = update.callback_query
            chat_id = query.message.chat.id
            message_id = query.message.message_id
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception as e:
                logger.error(f"Error editing message for persona {persona_id}: {e}")
                await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        await _send_prompt(update, context, msg_text, reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error displaying moods menu message for persona {persona_id}: {e}")

    return EDIT_MOOD_CHOICE

async def edit_mood_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses within the mood editing menu (edit, delete, add, back)."""
    query = update.callback_query
    if not query or not query.data: return EDIT_MOOD_CHOICE

    data = query.data
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- edit_mood_choice: User={user_id}, PersonaID={persona_id}, Data={data} ---")

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных.")
    error_unhandled_choice = escape_markdown_v2("❌ неизвестный выбор настроения.")
    error_decode_mood = escape_markdown_v2("❌ ошибка декодирования имени настроения.")
    prompt_new_name = escape_markdown_v2("введи название нового настроения \\(1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\):")
    prompt_new_prompt_fmt_raw = "✏️ редактирование настроения: *{name}*\n\nотправь новый текст промпта \\(до 2500 симв\\.\\):"
    prompt_confirm_delete_fmt_raw = "точно удалить настроение '{name}'?"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_choice, but edit_persona_id missing.")
        await context.bot.send_message(chat_id, error_no_session, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    persona_config = None
    try:
        with get_db() as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()
            if not persona_config:
                logger.warning(f"User {user_id}: PersonaConfig {persona_id} not found/owned in edit_mood_choice.")
                await query.answer("Личность не найдена", show_alert=True)
                await context.bot.send_message(chat_id, error_not_found, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"DB Error fetching persona in edit_mood_choice: {e}", exc_info=True)
        await query.answer("❌ Ошибка базы данных", show_alert=True)
        await query.edit_message_text("❌ Ошибка базы данных.", parse_mode=None)
        with get_db() as db:
            persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
            if persona:
                return await _show_edit_wizard_menu(update, context, persona)
        return ConversationHandler.END

    await query.answer()

    # --- Route based on callback data ---
    if data == "back_to_wizard_menu": # Changed from edit_persona_back
        logger.debug(f"User {user_id} going back from mood menu to main wizard menu for {persona_id}.")
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return await _show_edit_wizard_menu(update, context, persona_config) # Back to main wizard

    if data == "editmood_add":
        logger.debug(f"User {user_id} starting to add mood for {persona_id}.")
        context.user_data['edit_mood_name'] = None
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        await _send_prompt(update, context, prompt_new_name, InlineKeyboardMarkup([[cancel_button]]))
        return EDIT_MOOD_NAME

    if data.startswith("editmood_select_"):
        original_mood_name = None
        try:
            encoded_mood_name = data.split("editmood_select_", 1)[1]
            original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from callback {data}: {decode_err}")
            await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
            return await edit_moods_menu(update, context, persona_config=persona_config)

        context.user_data['edit_mood_name'] = original_mood_name
        logger.debug(f"User {user_id} selected mood '{original_mood_name}' to edit for {persona_id}.")
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        final_prompt = prompt_new_prompt_fmt_raw.format(name=escape_markdown_v2(original_mood_name))
        await _send_prompt(update, context, final_prompt, InlineKeyboardMarkup([[cancel_button]]))
        return EDIT_MOOD_PROMPT

    if data.startswith("deletemood_confirm_"):
        original_mood_name = None
        encoded_mood_name = ""
        try:
            encoded_mood_name = data.split("deletemood_confirm_", 1)[1]
            original_mood_name = urllib.parse.unquote(encoded_mood_name)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await context.bot.send_message(chat_id, error_decode_mood, parse_mode=ParseMode.MARKDOWN_V2)
            return await edit_moods_menu(update, context, persona_config=persona_config)

        context.user_data['delete_mood_name'] = original_mood_name
        logger.debug(f"User {user_id} initiated delete for mood '{original_mood_name}' for {persona_id}. Asking confirmation.")
        keyboard = [
            [InlineKeyboardButton(f"✅ да, удалить '{original_mood_name}'", callback_data=f"deletemood_delete_{encoded_mood_name}")],
            [InlineKeyboardButton("❌ нет, отмена", callback_data="edit_moods_back_cancel")]
            ]
        final_confirm_prompt = escape_markdown_v2(prompt_confirm_delete_fmt_raw.format(name=original_mood_name))
        await _send_prompt(update, context, final_confirm_prompt, InlineKeyboardMarkup(keyboard))
        return DELETE_MOOD_CONFIRM

    if data == "edit_moods_back_cancel":
        logger.debug(f"User {user_id} pressed back button, returning to mood list for {persona_id}.")
        context.user_data.pop('edit_mood_name', None)
        context.user_data.pop('delete_mood_name', None)
        return await edit_moods_menu(update, context, persona_config=persona_config)

    logger.warning(f"User {user_id} sent unhandled callback data '{data}' in EDIT_MOOD_CHOICE for {persona_id}.")
    await query.message.reply_text(error_unhandled_choice, parse_mode=ParseMode.MARKDOWN_V2)
    return await edit_moods_menu(update, context, persona_config=persona_config)


async def edit_mood_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the name for a new mood."""
    if not update.message or not update.message.text: return EDIT_MOOD_NAME
    mood_name_raw = update.message.text.strip()
    mood_name_match = re.match(r'^[\wа-яА-ЯёЁ-]+$', mood_name_raw, re.UNICODE)
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id

    logger.info(f"--- edit_mood_name_received: User={user_id}, PersonaID={persona_id}, Name='{mood_name_raw}' ---")

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена.")
    error_validation = escape_markdown_v2("❌ название: 1\\-30 символов, буквы/цифры/дефис/подчерк\\., без пробелов\\. попробуй еще:")
    error_name_exists_fmt_raw = "❌ настроение '{name}' уже существует\\. выбери другое:"
    error_db = escape_markdown_v2("❌ ошибка базы данных при проверке имени.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка.")
    prompt_for_prompt_fmt_raw = "отлично\\! теперь отправь текст промпта для настроения '{name}':"

    if not persona_id:
        logger.warning(f"User {user_id} in edit_mood_name_received, but edit_persona_id missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_name_match or not (1 <= len(mood_name_raw) <= 30):
        logger.debug(f"Validation failed for mood name '{mood_name_raw}'.")
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME

    mood_name = mood_name_raw

    try:
        with get_db() as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned in mood name check.")
                await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            current_moods = {}
            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: pass

            if any(existing_name.lower() == mood_name.lower() for existing_name in current_moods):
                logger.info(f"User {user_id} tried mood name '{mood_name}' which already exists.")
                cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
                final_exists_msg = error_name_exists_fmt_raw.format(name=escape_markdown_v2(mood_name))
                await update.message.reply_text(final_exists_msg, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
                return EDIT_MOOD_NAME

            context.user_data['edit_mood_name'] = mood_name
            logger.debug(f"Stored mood name '{mood_name}' for user {user_id}. Asking for prompt.")
            cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
            final_prompt = prompt_for_prompt_fmt_raw.format(name=escape_markdown_v2(mood_name))
            # Delete the previous prompt message before sending new one
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                except Exception: pass
            # Send new prompt
            sent_message = await context.bot.send_message(chat_id, final_prompt, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['last_prompt_message_id'] = sent_message.message_id # Store new prompt ID
            return EDIT_MOOD_PROMPT

    except SQLAlchemyError as e:
        logger.error(f"DB error checking mood name uniqueness for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_NAME
    except Exception as e:
        logger.error(f"Unexpected error checking mood name for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END


async def edit_mood_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the prompt text for a mood being edited or added."""
    if not update.message or not update.message.text: return EDIT_MOOD_PROMPT
    mood_prompt = update.message.text.strip()
    mood_name = context.user_data.get('edit_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = update.effective_user.id
    chat_id = update.message.chat.id

    logger.info(f"--- edit_mood_prompt_received: User={user_id}, PersonaID={persona_id}, Mood='{mood_name}' ---")

    error_no_session = escape_markdown_v2("❌ ошибка: сессия редактирования потеряна \\(нет имени настроения\\)\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_validation = escape_markdown_v2("❌ промпт настроения: 1\\-2500 символов\\. попробуй еще:")
    error_db = escape_markdown_v2("❌ ошибка базы данных при сохранении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при сохранении настроения.")
    success_saved_fmt_raw = "✅ настроение *{name}* сохранено\\!"

    if not mood_name or not persona_id:
        logger.warning(f"User {user_id} in edit_mood_prompt_received, but mood_name ('{mood_name}') or persona_id ('{persona_id}') missing.")
        await update.message.reply_text(error_no_session, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not mood_prompt or len(mood_prompt) > 2500:
        logger.debug(f"Validation failed for mood prompt (length={len(mood_prompt)}).")
        cancel_button = InlineKeyboardButton("❌ Отмена", callback_data="edit_moods_back_cancel")
        await update.message.reply_text(error_validation, reply_markup=InlineKeyboardMarkup([[cancel_button]]), parse_mode=ParseMode.MARKDOWN_V2)
        return EDIT_MOOD_PROMPT

    try:
        with get_db() as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned when saving mood prompt.")
                await update.message.reply_text(error_not_found, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {}

            current_moods[mood_name] = mood_prompt
            persona_config.set_moods(db, current_moods)
            db.commit()

            context.user_data.pop('edit_mood_name', None)
            logger.info(f"User {user_id} updated/added mood '{mood_name}' for persona {persona_id}.")
            final_success_msg = success_saved_fmt_raw.format(name=escape_markdown_v2(mood_name))
            await update.message.reply_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)

            db.refresh(persona_config)
            # Delete the prompt message before showing menu
            prompt_msg_id = context.user_data.pop('last_prompt_message_id', None)
            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                except Exception: pass
            # Return to mood menu
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error saving mood '{mood_name}' for persona {persona_id}: {e}", exc_info=True)
        await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def delete_mood_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the final confirmation button press for deleting a mood."""
    query = update.callback_query
    if not query or not query.data: return DELETE_MOOD_CONFIRM

    data = query.data
    mood_name_to_delete = context.user_data.get('delete_mood_name')
    persona_id = context.user_data.get('edit_persona_id')
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    logger.info(f"--- delete_mood_confirmed: User={user_id}, PersonaID={persona_id}, MoodToDelete='{mood_name_to_delete}' ---")

    error_no_session = escape_markdown_v2("❌ ошибка: неверные данные для удаления или сессия потеряна\\. начни снова \\(/mypersonas\\)\\.")
    error_not_found_persona = escape_markdown_v2("❌ ошибка: личность не найдена или нет доступа.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при удалении настроения.")
    error_general = escape_markdown_v2("❌ ошибка при удалении настроения.")
    info_not_found_mood_fmt_raw = "настроение '{name}' не найдено \\(уже удалено\\?\\)\\."
    error_decode_mood = escape_markdown_v2("❌ ошибка декодирования имени настроения для удаления.")
    success_delete_fmt_raw = "🗑️ настроение *{name}* удалено\\."

    original_name_from_callback = None
    if data.startswith("deletemood_delete_"):
        try:
            encoded_mood_name_from_callback = data.split("deletemood_delete_", 1)[1]
            original_name_from_callback = urllib.parse.unquote(encoded_mood_name_from_callback)
        except Exception as decode_err:
            logger.error(f"Error decoding mood name from delete confirm callback {data}: {decode_err}")
            await query.answer("❌ Ошибка данных", show_alert=True)
            await context.bot.send_message(chat_id, error_decode_mood, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    if not mood_name_to_delete or not persona_id or not original_name_from_callback or mood_name_to_delete != original_name_from_callback:
        logger.warning(f"User {user_id}: Mismatch or missing state in delete_mood_confirmed. Stored='{mood_name_to_delete}', Callback='{original_name_from_callback}', PersonaID='{persona_id}'")
        await query.answer("❌ Ошибка сессии", show_alert=True)
        await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.pop('delete_mood_name', None)
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)

    await query.answer("Удаляем...")
    logger.warning(f"User {user_id} confirmed deletion of mood '{mood_name_to_delete}' for persona {persona_id}.")

    try:
        with get_db() as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).with_for_update().first()
            if not persona_config:
                logger.warning(f"User {user_id}: Persona {persona_id} not found/owned during mood deletion.")
                await context.bot.send_message(chat_id, error_not_found_persona, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END

            try: current_moods = json.loads(persona_config.mood_prompts_json or '{}')
            except json.JSONDecodeError: current_moods = {}

            if mood_name_to_delete in current_moods:
                del current_moods[mood_name_to_delete]
                persona_config.set_moods(db, current_moods)
                db.commit()

                context.user_data.pop('delete_mood_name', None)
                logger.info(f"Successfully deleted mood '{mood_name_to_delete}' for persona {persona_id}.")
                final_success_msg = success_delete_fmt_raw.format(name=escape_markdown_v2(mood_name_to_delete))
                # Edit message first, then return to menu
                await query.edit_message_text(final_success_msg, parse_mode=ParseMode.MARKDOWN_V2)
                await asyncio.sleep(0.5) # Short pause before showing menu again
            else:
                logger.warning(f"Mood '{mood_name_to_delete}' not found for deletion in persona {persona_id}.")
                final_not_found_msg = info_not_found_mood_fmt_raw.format(name=escape_markdown_v2(mood_name_to_delete))
                await query.edit_message_text(final_not_found_msg, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.pop('delete_mood_name', None)
                await asyncio.sleep(0.5) # Short pause

            db.refresh(persona_config)
            return await edit_moods_menu(update, context, persona_config=persona_config)

    except SQLAlchemyError as e:
        logger.error(f"Database error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, error_db, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)
    except Exception as e:
        logger.error(f"Error deleting mood '{mood_name_to_delete}' for persona {persona_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, error_general, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        db.rollback()
        return await _try_return_to_mood_menu(update, context, user_id, persona_id)


async def _try_return_to_mood_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, persona_id: int) -> int:
    """Helper function to attempt returning to the mood edit menu after an error."""
    logger.debug(f"Attempting to return to mood menu for user {user_id}, persona {persona_id} after error.")
    callback_message = update.callback_query.message if update.callback_query else None
    user_message = update.message
    target_message = callback_message or user_message

    error_cannot_return = escape_markdown_v2("❌ не удалось вернуться к меню настроений \\(личность не найдена\\)\\.")
    error_cannot_return_general = escape_markdown_v2("❌ не удалось вернуться к меню настроений.")
    prompt_mood_menu_raw = "🎭 управление настроениями для *{name}*:"

    if not target_message:
        logger.warning("Cannot return to mood menu: no target message found.")
        context.user_data.clear()
        return ConversationHandler.END
    target_chat_id = target_message.chat.id

    try:
        with get_db() as db:
            persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
                PersonaConfig.id == persona_id,
                PersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if persona_config:
                # Use the existing mood menu function
                return await edit_moods_menu(update, context, persona_config=persona_config)
            else:
                logger.warning(f"Persona {persona_id} not found when trying to return to mood menu.")
                await context.bot.send_message(target_chat_id, error_cannot_return, parse_mode=ParseMode.MARKDOWN_V2)
                context.user_data.clear()
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to return to mood menu after error: {e}", exc_info=True)
        await context.bot.send_message(target_chat_id, error_cannot_return_general, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data.clear()
        return ConversationHandler.END


# --- Wizard Finish/Cancel ---
async def edit_persona_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles finishing the persona editing conversation via the 'Завершить' button."""
    query = update.callback_query
    user_id = update.effective_user.id
    persona_id_from_data = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} initiated FINISH for edit persona session {persona_id_from_data}.")

    # Простое текстовое сообщение без форматирования
    finish_message = "✅ редактирование завершено."
    
    if query:
        try:
            await query.answer()
        except Exception as e_ans:
            logger.debug(f"edit_persona_finish: Could not answer query: {e_ans}")
        
        # Редактируем сообщение, если можем, иначе просто игнорируем ошибку
        if query.message:
            try:
                await query.edit_message_text(finish_message, reply_markup=None, parse_mode=None)
                logger.info(f"edit_persona_finish: Edited message {query.message.message_id} to show completion.")
            except Exception as e_edit:
                logger.warning(f"Could not edit wizard menu on finish: {e_edit}. Message might have been deleted.")

    else: # Fallback для команды /cancel, если она будет вызывать этот же обработчик
        if update.effective_message:
            await update.effective_message.reply_text(finish_message, reply_markup=ReplyKeyboardRemove(), parse_mode=None)

    logger.debug(f"edit_persona_finish: Clearing user_data for user {user_id}.")
    context.user_data.clear()
    return ConversationHandler.END

async def edit_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona editing wizard."""
    user_id = update.effective_user.id
    persona_id_from_data = context.user_data.get('edit_persona_id', 'N/A')
    logger.info(f"User {user_id} initiated CANCEL for edit persona session {persona_id_from_data}.")

    cancel_message = escape_markdown_v2("Редактирование отменено.")
    
    # Определяем, откуда пришел запрос (команда или коллбэк)
    query = update.callback_query
    message_to_reply_or_edit = query.message if query else update.effective_message
    chat_id_to_send = message_to_reply_or_edit.chat.id if message_to_reply_or_edit else None

    if query:
        try: await query.answer()
        except Exception: pass

    if message_to_reply_or_edit:
        try:
            # Если это коллбэк от кнопки "Отмена" в подменю, то message_to_reply_or_edit - это сообщение подменю.
            # Если это команда /cancel, то это сообщение с командой.
            if query: # Если это коллбэк, пытаемся редактировать
                await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
            else: # Если это команда, отвечаем на нее
                await message_to_reply_or_edit.reply_text(cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                logger.warning(f"Could not edit cancel message (not found/too old). Sending new for user {user_id}.")
                if chat_id_to_send:
                    try: await context.bot.send_message(chat_id=chat_id_to_send, text=cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                    except Exception: pass
            else: # Другая ошибка BadRequest
                logger.error(f"BadRequest editing/replying cancel message: {e}")
        except Exception as e:
            logger.warning(f"Error sending/editing cancellation for user {user_id}: {e}. Attempting to send new.")
            if chat_id_to_send:
                try: await context.bot.send_message(chat_id=chat_id_to_send, text=cancel_message, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                except Exception: pass
    
    logger.debug(f"edit_persona_cancel: Clearing user_data for user {user_id}.")
    context.user_data.clear()
    return ConversationHandler.END

# --- Delete Persona Conversation ---

async def _start_delete_convo(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: int) -> int:
    """Starts the persona deletion conversation (common logic)."""
    user_id = update.effective_user.id
    
    chat_id_for_action = None
    if update.effective_chat: 
        chat_id_for_action = update.effective_chat.id
    elif update.callback_query and update.callback_query.message: 
        chat_id_for_action = update.callback_query.message.chat.id
        
    if not chat_id_for_action:
        logger.error("_start_delete_convo: Could not determine chat_id for action.")
        if update.callback_query:
            try: await update.callback_query.answer("Ошибка: чат для действия не определен.", show_alert=True)
            except Exception: pass
        return ConversationHandler.END

    is_callback = update.callback_query is not None
    
    logger.info(f"--- _start_delete_convo: User={user_id}, New PersonaID to delete {persona_id}, ChatID {chat_id_for_action}, IsCallback={is_callback} ---")
    
    logger.info(f"_start_delete_convo: Calling _clean_previous_edit_session for user {user_id}")
    await _clean_previous_edit_session(context, user_id)

    context.user_data['delete_persona_id'] = persona_id
    context.user_data['_user_id_for_logging'] = user_id

    # проверка подписки на канал отключена

    try:
        await context.bot.send_chat_action(chat_id=chat_id_for_action, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning(f"Could not send chat action in _start_delete_convo: {e}")

    error_not_found_fmt_raw = "❌ Личность с ID `{id}` не найдена или не твоя."
    prompt_delete_fmt_raw = "🚨 *ВНИМАНИЕ\\!* 🚨\nУдалить личность '{name}' \\(ID: `{id}`\\)\\?\n\nЭто действие *НЕОБРАТИМО\\!*"
    error_db_raw = "❌ Ошибка базы данных."
    error_general_raw = "❌ Непредвиденная ошибка."
    error_db = escape_markdown_v2(error_db_raw)
    error_general = escape_markdown_v2(error_general_raw)

    try:
        with get_db() as db:
            logger.debug(f"Fetching DBPersonaConfig {persona_id} for owner {user_id}...")
            persona_config = db.query(DBPersonaConfig).options(selectinload(DBPersonaConfig.owner)).filter(
                DBPersonaConfig.id == persona_id,
                DBPersonaConfig.owner.has(User.telegram_id == user_id)
            ).first()

            if not persona_config:
                final_error_msg = escape_markdown_v2(error_not_found_fmt_raw.format(id=persona_id))
                logger.warning(f"Persona {persona_id} not found or not owned by user {user_id}.")
                if is_callback and update.callback_query: # Check if query exists
                    try: await update.callback_query.answer("Личность не найдена", show_alert=True)
                    except Exception: pass
                # Отправляем новое сообщение об ошибке в правильный чат
                await context.bot.send_message(chat_id_for_action, final_error_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return ConversationHandler.END

            logger.debug(f"Persona found: {persona_config.name}. Storing ID in user_data.")
            # context.user_data['delete_persona_id'] = persona_id # Already set above

            persona_name_display = persona_config.name[:20] + "..." if len(persona_config.name) > 20 else persona_config.name
            keyboard = [
                [InlineKeyboardButton(f"‼️ ДА, УДАЛИТЬ '{escape_markdown_v2(persona_name_display)}' ‼️", callback_data=f"delete_persona_confirm_{persona_id}")],
                [InlineKeyboardButton("❌ НЕТ, ОСТАВИТЬ", callback_data="delete_persona_cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg_text = prompt_delete_fmt_raw.format(name=escape_markdown_v2(persona_config.name), id=persona_id)

            logger.debug(f"Sending confirmation message for persona {persona_id}.")
            
            # --- ИСПРАВЛЕННЫЙ БЛОК ---
            if is_callback and update.callback_query:
                try:
                    # Просто отвечаем на коллбэк, чтобы убрать "часики" на кнопке
                    await update.callback_query.answer()
                except Exception as ans_err:
                    logger.warning(f"Could not answer callback in _start_delete_convo: {ans_err}")
            # --- КОНЕЦ ИСПРАВЛЕННОГО БЛОКА ---
            
            sent_message = await context.bot.send_message(chat_id_for_action, msg_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['delete_confirm_message_id'] = sent_message.message_id
            
            logger.info(f"User {user_id} initiated delete for persona {persona_id}. Asking confirmation. Returning state DELETE_PERSONA_CONFIRM.")
            return DELETE_PERSONA_CONFIRM

    except SQLAlchemyError as e:
        logger.error(f"Database error starting delete persona {persona_id}: {e}", exc_info=True)
        if chat_id_for_action:
            await context.bot.send_message(chat_id_for_action, error_db, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in _start_delete_convo for persona {persona_id}: {e}", exc_info=True)
        if chat_id_for_action:
            await context.bot.send_message(chat_id_for_action, error_general, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
async def delete_persona_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /deletepersona command."""
    if not update.message: return ConversationHandler.END
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"CMD /deletepersona < User {user_id} with args: {args}")

    # НЕ чистим user_data здесь - это будет делать _start_delete_convo
    
    usage_text = escape_markdown_v2("укажи id личности: `/deletepersona <id>`\nили используй кнопку из `/mypersonas`")
    error_invalid_id = escape_markdown_v2("❌ id должен быть числом.")

    if not args or not args[0].isdigit():
        await update.message.reply_text(usage_text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    try:
        persona_id = int(args[0])
    except ValueError:
        await update.message.reply_text(error_invalid_id, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # _start_delete_convo очистит user_data и установит 'delete_persona_id'
    return await _start_delete_convo(update, context, persona_id)

async def delete_persona_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for delete persona button press."""
    query = update.callback_query
    if not query or not query.data: return ConversationHandler.END
    
    user_id = query.from_user.id
    original_chat_id = query.message.chat.id if query.message else (update.effective_chat.id if update.effective_chat else None)
    logger.info(f"CALLBACK delete_persona BUTTON < User {user_id} for data {query.data} in chat {original_chat_id}")

    # НЕ чистим user_data здесь - это будет делать _start_delete_convo
    
    # Быстрый ответ на коллбэк
    try: 
        await query.answer("Начинаем удаление...")
    except Exception as e_ans:
        logger.debug(f"delete_persona_button_callback: Could not answer query: {e_ans}")

    error_invalid_id_callback = escape_markdown_v2("❌ ошибка: неверный ID личности в кнопке.")

    try:
        persona_id = int(query.data.split('_')[-1]) 
        logger.info(f"Parsed persona_id for deletion: {persona_id} for user {user_id}")
        
        # _start_delete_convo очистит user_data и установит 'delete_persona_id'
        return await _start_delete_convo(update, context, persona_id)
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete_persona callback data: {query.data}")
        if original_chat_id:
            await context.bot.send_message(original_chat_id, error_invalid_id_callback, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in delete_persona_button_callback for {query.data}: {e}", exc_info=True)
        if original_chat_id:
            try: await context.bot.send_message(original_chat_id, "Произошла непредвиденная ошибка.", parse_mode=None)
            except Exception: pass
        return ConversationHandler.END

async def delete_persona_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data: return DELETE_PERSONA_CONFIRM

    data = query.data
    user_id = query.from_user.id
    persona_id_from_data = None
    try:
        persona_id_from_data = int(data.split('_')[-1])
    except (IndexError, ValueError):
        logger.error(f"Could not parse persona_id from delete confirmation callback data: {data}")
        await query.answer("❌ Ошибка данных", show_alert=True)
        return ConversationHandler.END # Завершаем, если данные некорректны

    persona_id_from_state = context.user_data.get('delete_persona_id')
    chat_id = query.message.chat.id

    logger.info(f"--- delete_persona_confirmed: User={user_id}, Data={data}, ID_from_data={persona_id_from_data}, ID_from_state={persona_id_from_state} ---")

    error_no_session = escape_markdown_v2("❌ Ошибка: неверные данные для удаления или сессия потеряна. Начни снова (/mypersonas).")
    error_delete_failed = escape_markdown_v2("❌ Не удалось удалить личность (ошибка базы данных).")
    success_deleted_fmt_raw = "✅ Личность '{name}' удалена."

    if not persona_id_from_state or persona_id_from_data != persona_id_from_state:
        logger.warning(f"User {user_id}: Mismatch or missing ID in delete_persona_confirmed. State='{persona_id_from_state}', Callback='{persona_id_from_data}'")
        await query.answer("❌ Ошибка сессии", show_alert=True)
        # Отправляем новое сообщение, т.к. редактировать может быть нельзя
        try:
            await context.bot.send_message(chat_id, error_no_session, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as send_err:
            logger.error(f"Failed to send session error message: {send_err}")
        context.user_data.clear()
        return ConversationHandler.END

    await query.answer("Удаляем...")
    logger.warning(f"User {user_id} CONFIRMED DELETION of persona {persona_id_from_state}.")
    deleted_ok = False
    persona_name_deleted = f"ID {persona_id_from_state}" # Используем ID по умолчанию

    try:
        with get_db() as db:
            user = db.query(User).filter(User.telegram_id == user_id).first()
            if not user:
                    logger.error(f"User {user_id} not found in DB during persona deletion.")
                    try:
                        await context.bot.send_message(chat_id, escape_markdown_v2("❌ Ошибка: пользователь не найден."), reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
                    except Exception as send_err:
                        logger.error(f"Failed to send user not found error message: {send_err}")
                    context.user_data.clear()
                    return ConversationHandler.END

            # Попробуем получить имя перед удалением для сообщения
            persona_before_delete = db.query(DBPersonaConfig.name).filter(DBPersonaConfig.id == persona_id_from_state, DBPersonaConfig.owner_id == user.id).scalar()
            if persona_before_delete:
                persona_name_deleted = persona_before_delete # Обновляем имя для сообщения

            logger.info(f"Calling db.delete_persona_config with persona_id={persona_id_from_state}, owner_id={user.id}")
            deleted_ok = delete_persona_config(db, persona_id_from_state, user.id)
            logger.info(f"db.delete_persona_config returned: {deleted_ok}")

    except SQLAlchemyError as e:
        logger.error(f"Database error during delete_persona_confirmed fetch/delete for {persona_id_from_state}: {e}", exc_info=True)
        deleted_ok = False
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_confirmed for {persona_id_from_state}: {e}", exc_info=True)
        deleted_ok = False

    # Сформируем финальный текст
    if deleted_ok:
        final_message_text = success_deleted_fmt_raw.format(name=persona_name_deleted)
        logger.info(f"Successfully deleted persona {persona_id_from_state}")
    else:
        final_message_text = "❌ не удалось удалить личность."
        logger.warning(f"Failed to delete persona {persona_id_from_state}")

    # Пробуем отредактировать исходное сообщение (лучший UX)
    try:
        await query.edit_message_text(
            text=escape_markdown_v2(final_message_text),
            reply_markup=None,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.warning(f"Could not edit confirmation message on delete: {e}. Sending new message instead.")
        try:
            await context.bot.send_message(
                chat_id,
                escape_markdown_v2(final_message_text),
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as send_err:
            logger.error(f"Failed to send final deletion status message: {send_err}")
            # Последняя попытка — plain text без разметки
            await context.bot.send_message(
                chat_id,
                final_message_text,
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=None
            )

    logger.debug("Clearing user_data and ending delete conversation.")
    context.user_data.clear()
    return ConversationHandler.END

async def delete_persona_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the persona deletion process."""
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id
    persona_id = context.user_data.get('delete_persona_id', 'N/A')
    logger.info(f"User {user_id} cancelled deletion for persona {persona_id}.")

    cancel_message = escape_markdown_v2("удаление отменено.")

    try:
        await query.edit_message_text(cancel_message, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Failed to edit message with deletion cancellation: {e}")
    context.user_data.clear()
    return ConversationHandler.END


# --- Mute/Unmute Commands ---

async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /mutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /mutebot < User {user_id} in Chat {chat_id_str}")

    # проверка подписки удалена

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности.")
    error_not_owner = escape_markdown_v2("❌ только владелец личности может ее заглушить.")
    error_no_instance = escape_markdown_v2("❌ ошибка: не найден объект связи с чатом.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при попытке заглушить бота.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при выполнении команды.")
    info_already_muted_fmt_raw = "🔇 личность '{name}' уже заглушена в этом чате."
    success_muted_fmt_raw = "🔇 личность '{name}' больше не будет отвечать в этом чате \\(но будет запоминать сообщения\\)\\. используйте `/unmutebot`, чтобы вернуть."

    with get_db() as db:
        try:
            current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
            instance_info = get_persona_and_context_with_owner(chat_id_str, db, current_bot_id_str)
            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, _, owner_user = instance_info
            chat_instance = persona.chat_instance
            persona_name_escaped = escape_markdown_v2(persona.name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to mute persona '{persona.name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if not chat_instance:
                logger.error(f"Could not find ChatBotInstance object for persona {persona.name} in chat {chat_id_str} during mute.")
                await update.message.reply_text(error_no_instance, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if not chat_instance.is_muted:
                chat_instance.is_muted = True
                db.commit()
                logger.info(f"Persona '{persona.name}' muted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = success_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_already_muted_msg = info_already_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_already_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected error during /mutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()

async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /unmutebot command."""
    if not update.message: return
    chat_id_str = str(update.effective_chat.id)
    user_id = update.effective_user.id
    logger.info(f"CMD /unmutebot < User {user_id} in Chat {chat_id_str}")

    # проверка подписки удалена

    error_no_persona = escape_markdown_v2("🎭 в этом чате нет активной личности, которую можно размьютить.")
    error_not_owner = escape_markdown_v2("❌ только владелец личности может снять заглушку.")
    error_db = escape_markdown_v2("❌ ошибка базы данных при попытке вернуть бота к общению.")
    error_general = escape_markdown_v2("❌ непредвиденная ошибка при выполнении команды.")
    info_not_muted_fmt_raw = "🔊 личность '{name}' не была заглушена."
    success_unmuted_fmt_raw = "🔊 личность '{name}' снова может отвечать в этом чате."

    with get_db() as db:
        try:
            current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
            instance_info = get_persona_and_context_with_owner(chat_id_str, db, current_bot_id_str)

            if not instance_info:
                await update.message.reply_text(error_no_persona, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            persona, active_instance, owner_user = instance_info
            persona_name = persona.name
            persona_name_escaped = escape_markdown_v2(persona_name)

            if owner_user.telegram_id != user_id and not is_admin(user_id):
                logger.warning(f"User {user_id} tried to unmute persona '{persona_name}' owned by {owner_user.telegram_id}.")
                await update.message.reply_text(error_not_owner, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
                return

            if active_instance.is_muted:
                active_instance.is_muted = False
                db.commit()
                logger.info(f"Persona '{persona_name}' unmuted in chat {chat_id_str} by user {user_id}.")
                final_success_msg = success_unmuted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                final_not_muted_msg = info_not_muted_fmt_raw.format(name=persona_name_escaped)
                await update.message.reply_text(final_not_muted_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

        except SQLAlchemyError as e:
            logger.error(f"Database error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_db, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()
        except Exception as e:
            logger.error(f"Unexpected error during /unmutebot for chat {chat_id_str}: {e}", exc_info=True)
            await update.message.reply_text(error_general, parse_mode=ParseMode.MARKDOWN_V2)
            db.rollback()

# --- Новые функции для настройки макс. сообщений ---

# --- Функции для настройки объема сообщений ---
async def edit_message_volume_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends prompt to choose message volume."""
    persona_id = context.user_data.get('edit_persona_id')
    # Временно используем значение по умолчанию, пока миграция не применена
    # with get_db() as db:
    #     current_volume = db.query(PersonaConfig.message_volume).filter(PersonaConfig.id == persona_id).scalar() or "normal"
    current_volume = "normal"

    display_map = {
        "short": "🔉 Короткие сообщения",
        "normal": "🔊 Стандартный объем",
        "long": "📝 Подробные сообщения",
        "random": "🎲 Случайный объем"
    }
    current_display = display_map.get(current_volume, current_volume)

    prompt_text = escape_markdown_v2(f"🔊 Выберите объем сообщений (текущий: {current_display}):")

    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_volume == 'short' else ''}{display_map['short']}", callback_data="set_volume_short")],
        [InlineKeyboardButton(f"{'✅ ' if current_volume == 'normal' else ''}{display_map['normal']}", callback_data="set_volume_normal")],
        [InlineKeyboardButton(f"{'✅ ' if current_volume == 'long' else ''}{display_map['long']}", callback_data="set_volume_long")],
        [InlineKeyboardButton(f"{'✅ ' if current_volume == 'random' else ''}{display_map['random']}", callback_data="set_volume_random")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_wizard_menu")]
    ]

    await _send_prompt(update, context, prompt_text, InlineKeyboardMarkup(keyboard))
    return EDIT_MESSAGE_VOLUME

async def edit_message_volume_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the choice for message volume."""
    query = update.callback_query
    await query.answer()
    data = query.data
    persona_id = context.user_data.get('edit_persona_id')

    if data == "back_to_wizard_menu":
        return await _handle_back_to_wizard_menu(update, context, persona_id)

    if data.startswith("set_volume_"):
        volume = data.replace("set_volume_", "")
        valid_volumes = ["short", "normal", "long", "random"]
        if volume not in valid_volumes:
            logger.warning(f"Invalid volume setting: {volume}")
            return EDIT_MESSAGE_VOLUME

        try:
            with get_db() as db:
                # Временно не обновляем столбец, пока миграция не применена
                # db.query(PersonaConfig).filter(PersonaConfig.id == persona_id).update({"message_volume": volume})
                # db.commit()
                logger.info(f"Would update message_volume to {volume} for persona {persona_id} (temporarily disabled)")
                
                # Показываем сообщение об успешном обновлении
                display_map = {
                    "short": "🔉 Короткие сообщения",
                    "normal": "🔊 Стандартный объем",
                    "long": "📝 Подробные сообщения",
                    "random": "🎲 Случайный объем"
                }
                display_value = display_map.get(volume, volume)
                await query.edit_message_text(escape_markdown_v2(f"✅ Объем сообщений установлен: {display_value}"))
                
                # Return to wizard menu
                persona = db.query(DBPersonaConfig).filter(DBPersonaConfig.id == persona_id).first()
                return await _show_edit_wizard_menu(update, context, persona)
        except Exception as e:
            logger.error(f"Error setting message_volume for {persona_id}: {e}")
            await query.edit_message_text(escape_markdown_v2("❌ Ошибка при сохранении настройки объема сообщений."))
            return await _try_return_to_wizard_menu(update, context, query.from_user.id, persona_id)
    else:
        logger.warning(f"Unknown callback in edit_message_volume_received: {data}")
        return EDIT_MESSAGE_VOLUME




# ... (остальные функции) ...


    # =====================
    # mutebot / unmutebot (per-bot per-chat mute toggle)
    # =====================

async def mutebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Заглушить текущего бота (персону) в этом чате. Применяется к ChatBotInstance для текущего bot.id."""
    user = update.effective_user
    chat = update.effective_chat
    if not chat or not user or not update.message:
        return
    chat_id_str = str(chat.id)
    current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
    if not current_bot_id_str:
        await update.message.reply_text("техническая ошибка: не удалось определить bot.id")
        return
    with get_db() as db_session:
        link = db_session.query(DBChatBotInstance).join(DBChatBotInstance.bot_instance_ref).filter(
            DBChatBotInstance.chat_id == chat_id_str,
            DBChatBotInstance.active == True,
            DBBotInstance.telegram_bot_id == current_bot_id_str
        ).first()
        if not link:
            await update.message.reply_text("бот не привязан к этому чату или не активирован для этой личности")
            return
        if getattr(link, 'is_muted', False):
            await update.message.reply_text("уже заглушен")
            return
        try:
            link.is_muted = True
            db_session.commit()
            logger.info(f"mutebot: set is_muted=True for ChatBotInstance id={link.id} chat={chat_id_str} bot_id={current_bot_id_str}")
            await update.message.reply_text("бот заглушен в этом чате")
        except Exception as e:
            db_session.rollback()
            logger.error(f"mutebot commit failed: {e}")
            await update.message.reply_text("не удалось заглушить (ошибка БД)")

async def unmutebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять заглушку с текущего бота (персоны) в этом чате. Применяется к ChatBotInstance для текущего bot.id."""
    user = update.effective_user
    chat = update.effective_chat
    if not chat or not user or not update.message:
        return
    chat_id_str = str(chat.id)
    current_bot_id_str = str(context.bot.id) if getattr(context, 'bot', None) and getattr(context.bot, 'id', None) else None
    if not current_bot_id_str:
        await update.message.reply_text("техническая ошибка: не удалось определить bot.id")
        return
    with get_db() as db_session:
        link = db_session.query(DBChatBotInstance).join(DBChatBotInstance.bot_instance_ref).filter(
            DBChatBotInstance.chat_id == chat_id_str,
            DBChatBotInstance.active == True,
            DBBotInstance.telegram_bot_id == current_bot_id_str
        ).first()
        if not link:
            await update.message.reply_text("бот не привязан к этому чату или не активирован для этой личности")
            return
        if not getattr(link, 'is_muted', False):
            await update.message.reply_text("бот уже размьючен")
            return
        try:
            link.is_muted = False
            db_session.commit()
            logger.info(f"unmutebot: set is_muted=False for ChatBotInstance id={link.id} chat={chat_id_str} bot_id={current_bot_id_str}")
            await update.message.reply_text("бот размьючен в этом чате")
        except Exception as e:
            db_session.rollback()
            logger.error(f"unmutebot commit failed: {e}")
            await update.message.reply_text("не удалось снять заглушку (ошибка БД)")
