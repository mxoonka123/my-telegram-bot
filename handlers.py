import logging
import httpx
import random
import asyncio
import re
import uuid
import json
from datetime import datetime, timezone, timedelta
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
)
from telegram.error import BadRequest # Для отлова ошибок редактирования сообщений
from telegram.helpers import escape_markdown # Для экранирования в MarkdownV2

from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func
from typing import List, Dict, Any, Optional, Union, Tuple

from yookassa import Configuration as YookassaConfig
from yookassa import Payment
from yookassa.domain.models.currency import Currency
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem


# Используем импорт всего модуля config
import config

from db import (
    get_context_for_chat_bot, add_message_to_context,
    set_mood_for_chat_bot, get_mood_for_chat_bot, get_or_create_user,
    create_persona_config, get_personas_by_owner, get_persona_by_name_and_owner,
    get_persona_by_id_and_owner, check_and_update_user_limits, activate_subscription,
    create_bot_instance, link_bot_instance_to_chat, delete_persona_config,
    get_db, get_active_chat_bot_instance_with_relations, SessionLocal, # Добавлен SessionLocal
    User, PersonaConfig, BotInstance, ChatBotInstance, ChatContext
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, get_time_info

logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID

# Состояния разговора
EDIT_PERSONA_CHOICE, EDIT_FIELD, EDIT_MOOD_CHOICE, EDIT_MOOD_NAME, EDIT_MOOD_PROMPT, DELETE_MOOD_CONFIRM, DELETE_PERSONA_CONFIRM, EDIT_MAX_MESSAGES = range(8)

# Отображение полей для пользователя
FIELD_MAP = {
    "name": "имя",
    "description": "описание",
    "system_prompt_template": "системный промпт",
    "should_respond_prompt_template": "промпт 'отвечать?'",
    "spam_prompt_template": "промпт спама",
    "photo_prompt_template": "промпт фото",
    "voice_prompt_template": "промпт голоса",
    "max_response_messages": "макс. сообщений в ответе"
}

# Текст Пользовательского Соглашения
# Используем f-string для подстановки значений прямо здесь
TOS_TEXT = f"""
**📜 Пользовательское Соглашение Сервиса @NunuAiBot**

Привет\! Добро пожаловать в @NunuAiBot\! Мы очень рады, что вы с нами\. Это Соглашение — документ, который объясняет правила использования нашего Сервиса\. Прочитайте его, пожалуйста\.

Дата последнего обновления: 01\.03\.2025

**1\. О чем это Соглашение?**
1\.1\. Это Пользовательское Соглашение \(или просто "Соглашение"\) — договор между вами \(далее – "Пользователь" или "Вы"\) и нами \(владельцем Telegram\-бота @NunuAiBot, далее – "Сервис" или "Мы"\)\. Оно описывает условия использования Сервиса\.
1\.2\. Начиная использовать наш Сервис \(просто отправляя боту любое сообщение или команду\), Вы подтверждаете, что прочитали, поняли и согласны со всеми условиями этого Соглашения\. Если Вы не согласны хотя бы с одним пунктом, пожалуйста, прекратите использование Сервиса\.
1\.3\. Наш Сервис предоставляет Вам интересную возможность создавать и общаться с виртуальными собеседниками на базе искусственного интеллекта \(далее – "Личности" или "AI\-собеседники"\)\.

**2\. Про подписку и оплату**
2\.1\. Мы предлагаем два уровня доступа: бесплатный и Premium \(платный\)\. Возможности и лимиты для каждого уровня подробно описаны внутри бота, например, в командах `/profile` и `/subscribe`\.
2\.2\. Платная подписка дает Вам расширенные возможности и увеличенные лимиты на период в {config.SUBSCRIPTION_DURATION_DAYS} дней\.
2\.3\. Стоимость подписки составляет {config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY} за {config.SUBSCRIPTION_DURATION_DAYS} дней\.
2\.4\. Оплата проходит через безопасную платежную систему Yookassa\. Важно: мы не получаем и не храним Ваши платежные данные \(номер карты и т\.п\.\)\. Все безопасно\.
2\.5\. **Политика возвратов:** Покупая подписку, Вы получаете доступ к расширенным возможностям Сервиса сразу же после оплаты\. Поскольку Вы получаете услугу немедленно, оплаченные средства за этот период доступа, к сожалению, **не подлежат возврату**\.
2\.6\. В редких случаях, если Сервис окажется недоступен по нашей вине в течение длительного времени \(более 7 дней подряд\), и у Вас будет активная подписка, Вы можете написать нам в поддержку \(контакт указан в биографии бота и в нашем Telegram\-канале\)\. Мы рассмотрим возможность продлить Вашу подписку на срок недоступности Сервиса\. Решение принимается индивидуально\.

**3\. Ваши и наши права и обязанности**
3\.1\. Что ожидается от Вас \(Ваши обязанности\):
*   Использовать Сервис только в законных целях и не нарушать никакие законы при его использовании\.
*   Не пытаться вмешаться в работу Сервиса или получить несанкционированный доступ\.
*   Не использовать Сервис для рассылки спама, вредоносных программ или любой запрещенной информации\.
*   Если требуется \(например, для оплаты\), предоставлять точную и правдивую информацию\.
*   Поскольку у Сервиса нет возрастных ограничений, Вы подтверждаете свою способность принять условия настоящего Соглашения\.
3\.2\. Что можем делать мы \(Наши права\):
*   Мы можем менять условия этого Соглашения\. Если это произойдет, мы уведомим Вас, опубликовав новую версию Соглашения в нашем Telegram\-канале или иным доступным способом в рамках Сервиса\. Ваше дальнейшее использование Сервиса будет означать согласие с изменениями\.
*   Мы можем временно приостановить или полностью прекратить Ваш доступ к Сервису, если Вы нарушите условия этого Соглашения\.
*   Мы можем изменять сам Сервис: добавлять или убирать функции, менять лимиты или стоимость подписки\.

**4\. Важное предупреждение об ограничении ответственности**
4\.1\. Сервис предоставляется "как есть"\. Это значит, что мы не можем гарантировать его идеальную работу без сбоев или ошибок\. Технологии иногда подводят, и мы не несем ответственности за возможные проблемы, возникшие не по нашей прямой вине\.
4\.2\. Помните, Личности — это искусственный интеллект\. Их ответы генерируются автоматически и могут быть неточными, неполными, странными или не соответствующими Вашим ожиданиям или реальности\. Мы не несем никакой ответственности за содержание ответов, сгенерированных AI\-собеседниками\. Не воспринимайте их как истину в последней инстанции или профессиональный совет\.
4\.3\. Мы не несем ответственности за любые прямые или косвенные убытки или ущерб, который Вы могли понести в результате использования \(или невозможности использования\) Сервиса\.

**5\. Про Ваши данные \(Конфиденциальность\)**
5\.1\. Для работы Сервиса нам приходится собирать и обрабатывать минимальные данные: Ваш Telegram ID \(для идентификации аккаунта\), имя пользователя Telegram \(username, если есть\), информацию о Вашей подписке, информацию о созданных Вами Личностях, а также историю Ваших сообщений с Личностями \(это нужно AI для поддержания контекста разговора\)\.
5\.2\. Мы предпринимаем разумные шаги для защиты Ваших данных, но, пожалуйста, помните, что передача информации через Интернет никогда не может быть абсолютно безопасной\.

**6\. Действие Соглашения**
6\.1\. Настоящее Соглашение начинает действовать с момента, как Вы впервые используете Сервис, и действует до момента, пока Вы не перестанете им пользоваться или пока Сервис не прекратит свою работу\.

**7\. Интеллектуальная Собственность**
7\.1\. Вы сохраняете все права на контент \(текст\), который Вы создаете и вводите в Сервис в процессе взаимодействия с AI\-собеседниками\.
7\.2\. Вы предоставляете нам неисключительную, безвозмездную, действующую по всему миру лицензию на использование Вашего контента исключительно в целях предоставления, поддержания и улучшения работы Сервиса \(например, для обработки Ваших запросов, сохранения контекста диалога, анонимного анализа для улучшения моделей, если применимо\)\.
7\.3\. Все права на сам Сервис \(код бота, дизайн, название, графические элементы и т\.д\.\) принадлежат владельцу Сервиса\.
7\.4\. Ответы, сгенерированные AI\-собеседниками, являются результатом работы алгоритмов искусственного интеллекта\. Вы можете использовать полученные ответы в личных некоммерческих целях, но признаете, что они созданы машиной и не являются Вашей или нашей интеллектуальной собственностью в традиционном понимании\.

**8\. Заключительные положения**
8\.1\. Все споры и разногласия решаются путем переговоров\. Если это не поможет, споры будут рассматриваться в соответствии с законодательством Российской Федерации\.
8\.2\. По всем вопросам, касающимся настоящего Соглашения или работы Сервиса, Вы можете обращаться к нам через контакты, указанные в биографии бота и в нашем Telegram\-канале\.
"""


# --- Обработчик ошибок ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и информирует пользователя, если возможно."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        err_text = "упс... произошла непредвиденная ошибка. Попробуйте позже."
        try:
            # Используем escape_markdown для безопасной отправки
            await update.effective_message.reply_text(escape_markdown(err_text, version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

# --- Вспомогательные функции ---
def get_persona_and_context_with_owner(chat_id: str, db: Session) -> Optional[Tuple[Persona, List[Dict[str, str]], User]]:
    """Получает активную Персону, ее контекст и владельца (User) для указанного chat_id."""
    try:
        chat_instance = get_active_chat_bot_instance_with_relations(db, chat_id)
        if not chat_instance: logger.debug(f"No active chatbot instance for chat {chat_id}"); return None
        bot_instance = chat_instance.bot_instance_ref
        if not bot_instance or not bot_instance.persona_config or not bot_instance.owner: logger.error(f"ChatBotInstance {chat_instance.id} missing linked data."); return None
        persona_config = bot_instance.persona_config; owner_user = bot_instance.owner
        persona = Persona(persona_config, chat_instance)
        context_list = get_context_for_chat_bot(db, chat_instance.id)
        return persona, context_list, owner_user
    except ValueError as e: logger.error(f"Persona init failed: {e}", exc_info=True); return None
    except SQLAlchemyError as e: logger.error(f"DB error in get_persona_...: {e}", exc_info=True); return None
    except Exception as e: logger.error(f"Unexpected error in get_persona_...: {e}", exc_info=True); return None

async def send_to_langdock(system_prompt: str, messages: List[Dict[str, str]]) -> str:
    """Отправляет запрос к Langdock API и возвращает текстовый ответ."""
    if not config.LANGDOCK_API_KEY: logger.error("LANGDOCK_API_KEY missing."); return "ошибка: ключ api не настроен."
    headers = {"Authorization": f"Bearer {config.LANGDOCK_API_KEY}", "Content-Type": "application/json"}
    messages_to_send = messages[-config.MAX_CONTEXT_MESSAGES_SENT_TO_LLM:]
    payload = {"model": config.LANGDOCK_MODEL, "system": system_prompt, "messages": messages_to_send, "max_tokens": 1024, "temperature": 0.75, "top_p": 0.95, "stream": False}
    url = f"{config.LANGDOCK_BASE_URL.rstrip('/')}/v1/messages"
    logger.debug(f"Sending request to Langdock: {url} with {len(messages_to_send)} messages.")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client: resp = await client.post(url, json=payload, headers=headers)
        logger.debug(f"Langdock response status: {resp.status_code}"); resp.raise_for_status(); data = resp.json()
        logger.debug(f"Langdock response data (first 200 chars): {str(data)[:200]}")
        full_response = ""; content = data.get("content")
        if isinstance(content, list): full_response = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
        elif isinstance(content, dict) and "text" in content: full_response = content["text"]
        elif "choices" in data and isinstance(data["choices"], list) and data["choices"]: choice = data["choices"][0]; message = choice.get("message"); full_response = message["content"] if isinstance(message, dict) and "content" in message else choice.get("text", "")
        elif isinstance(data.get("response"), str): full_response = data["response"]
        if not full_response: logger.warning(f"Could not extract text from Langdock response: {data}"); return "хм, не смог прочитать ответ ai..."
        return full_response.strip()
    except httpx.ReadTimeout: logger.error("Langdock timed out."); return "слишком долго думал..."
    except httpx.HTTPStatusError as e: logger.error(f"Langdock HTTP error: {e.response.status_code} - {e.response.text}", exc_info=True); user_message = f"ой, произошла ошибка при связи с ai ({e.response.status_code})..."; return user_message
    except httpx.RequestError as e: logger.error(f"Langdock request error: {e}", exc_info=True); return "не могу связаться с ai (сеть)..."
    except json.JSONDecodeError as e: logger.error(f"Langdock JSON decode error: {e}. Resp: {resp.text if 'resp' in locals() else 'N/A'}"); return "получил непонятный ответ от ai..."
    except Exception as e: logger.error(f"Unexpected error Langdock comm: {e}", exc_info=True); return "внутренняя ошибка генерации."

async def process_and_send_response(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, chat_id: str, persona: Persona, full_bot_response_text: str, db: Session):
    """Обрабатывает ответ AI, добавляет в контекст, отправляет пользователю."""
    if not full_bot_response_text or not full_bot_response_text.strip(): logger.warning(f"Empty AI response chat {chat_id}"); return
    logger.debug(f"Processing AI response chat {chat_id}, persona {persona.name}. Raw len: {len(full_bot_response_text)}")
    response_content_to_save = full_bot_response_text.strip()
    if persona.chat_instance:
        try: add_message_to_context(db, persona.chat_instance.id, "assistant", response_content_to_save); logger.debug("AI response staged.")
        except SQLAlchemyError as e: logger.error(f"Re-raising DB Error context add assist {persona.chat_instance.id}."); raise
        except Exception as e: logger.error(f"Unexpected Error context add assist {persona.chat_instance.id}: {e}", exc_info=True); raise
    else: logger.error(f"No chat_instance for persona {persona.name} context add."); raise ValueError(f"Internal state error: chat_instance None")
    all_text_content = response_content_to_save; gif_links = extract_gif_links(all_text_content)
    for gif in gif_links: all_text_content = re.sub(re.escape(gif), "", all_text_content, flags=re.IGNORECASE).strip()
    text_parts_to_send = postprocess_response(all_text_content); logger.debug(f"Postprocessed into {len(text_parts_to_send)} parts.")
    max_messages=3; try: max_messages = persona.config.max_response_messages if persona.config and isinstance(persona.config.max_response_messages, int) and 1<=persona.config.max_response_messages<=10 else 3
    except Exception as e: logger.error(f"Error getting max_response_messages: {e}. Defaulting 3.")
    if len(text_parts_to_send) > max_messages: logger.info(f"Limiting response parts {len(text_parts_to_send)}->{max_messages}"); text_parts_to_send = text_parts_to_send[:max_messages]; text_parts_to_send[-1] += "..." if text_parts_to_send else ""
    gif_send_tasks = []; [gif_send_tasks.append(context.bot.send_animation(chat_id=chat_id, animation=gif_url)) for gif_url in gif_links]; await asyncio.gather(*gif_send_tasks, return_exceptions=True) if gif_send_tasks else None
    if text_parts_to_send:
        is_group_chat = update and update.effective_chat and update.effective_chat.type in ["group", "supergroup"]
        for i, part in enumerate(text_parts_to_send):
            part = part.strip();
            if not part: continue
            if is_group_chat: try: await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING); await asyncio.sleep(random.uniform(0.8, 1.5)) except Exception as e: logger.warning(f"Typing action fail {chat_id}: {e}")
            try:
                logger.debug(f"Sending part {i+1}/{len(text_parts_to_send)} chat {chat_id}: '{part[:50]}...'")
                # Используем MarkdownV2 по умолчанию, но текст ответа AI не экранируем, т.к. он может содержать разметку
                await context.bot.send_message(chat_id=chat_id, text=part, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e: # Если LLM выдал невалидную разметку
                if "can't parse entities" in str(e).lower():
                    logger.warning(f"Invalid MarkdownV2 from LLM, sending as plain text. Error: {e}")
                    try: await context.bot.send_message(chat_id=chat_id, text=part) # Отправляем как обычный текст
                    except Exception as send_plain_e: logger.error(f"Failed to send plain text part {i+1} chat {chat_id}: {send_plain_e}", exc_info=True); break
                else: logger.error(f"Send text part {i+1} BadRequest {chat_id}: {e}", exc_info=True); break # Другая ошибка BadRequest
            except Exception as e: logger.error(f"Send text part {i+1} fail {chat_id}: {e}", exc_info=True); break
            if i < len(text_parts_to_send) - 1: await asyncio.sleep(random.uniform(0.4, 0.9))

async def send_limit_exceeded_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Отправляет сообщение о превышении лимита."""
    # Экранируем текст для MarkdownV2
    text = (
        f"упс\! 😕 лимит сообщений \({user.daily_message_count}/{user.message_limit}\) на сегодня достигнут\.\n\n"
        f"✨ **хочешь безлимита?** ✨\n"
        f"подписка за {config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}/мес дает:\n"
        f"✅ **{config.PAID_DAILY_MESSAGE_LIMIT}** сообщений в день\n"
        f"✅ до **{config.PAID_PERSONA_LIMIT}** личностей\n"
        f"✅ полная настройка промптов и настроений\n\n"
        "👇 жми /subscribe или кнопку ниже\!"
    )
    keyboard = [[InlineKeyboardButton("🚀 получить подписку!", callback_data="subscribe_info")]]; reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        target_chat_id = update.effective_chat.id;
        if target_chat_id: await context.bot.send_message(target_chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else: logger.warning(f"No chat for limit msg user {user.telegram_id}.")
    except Exception as e: logger.error(f"Failed send limit msg user {user.telegram_id}: {e}")

# --- Основной обработчик сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения от пользователя."""
    if not update.message or not (update.message.text or update.message.caption): return
    chat_id = str(update.effective_chat.id); user_id = update.effective_user.id; username = update.effective_user.username or f"user_{user_id}"; message_text = (update.message.text or update.message.caption or "").strip()
    if not message_text: return
    logger.info(f"MSG < User {user_id} ({username}) Chat {chat_id}: {message_text[:100]}")
    with next(get_db()) as db:
        try:
            persona_info = get_persona_and_context_with_owner(chat_id, db)
            if not persona_info: logger.debug(f"No active persona chat {chat_id}"); return
            persona, _, owner = persona_info; logger.debug(f"Handling msg persona '{persona.name}' owner {owner.id} (TG:{owner.telegram_id})")
            can_send = check_and_update_user_limits(db, owner)
            if not can_send: logger.info(f"Owner {owner.telegram_id} limit hit"); await send_limit_exceeded_message(update, context, owner); return
            if persona.chat_instance: add_message_to_context(db, persona.chat_instance.id, "user", f"{username}: {message_text}"); logger.debug("User msg staged.")
            else: logger.error(f"No chat_instance persona {persona.name}"); await update.message.reply_text(escape_markdown("системная ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if persona.chat_instance and persona.chat_instance.is_muted: logger.debug(f"Persona '{persona.name}' muted chat {chat_id}"); db.commit(); logger.info(f"Committed changes muted persona {persona.name}."); return
            moods=persona.get_all_mood_names(); matched_mood=next((m for m in moods if m.lower()==message_text.lower()),None)
            if matched_mood: logger.info(f"Msg matched mood '{matched_mood}'."); await mood(update, context); return
            should_respond=True; is_group=update.effective_chat.type in ["group","supergroup"]
            if is_group and persona.should_respond_prompt_template: prompt=persona.format_should_respond_prompt(message_text);
                if prompt: try: ctx=get_context_for_chat_bot(db,persona.chat_instance.id); decision=await send_to_langdock(prompt,ctx); ans=decision.strip().lower(); logger.debug(f"should_respond AI: '{ans}'"); should_respond=ans.startswith("д") except Exception as e: logger.error(f"should_respond LLM error: {e}",exc_info=True); should_respond=True
                else: should_respond=True
                if not should_respond: logger.debug(f"Not responding by AI decision."); db.commit(); logger.info(f"Committed changes no AI response."); return
            ctx_ai=get_context_for_chat_bot(db,persona.chat_instance.id);
            if ctx_ai is None: logger.error(f"Failed get context AI {persona.name}"); await update.message.reply_text(escape_markdown("ошибка получения контекста.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return; logger.debug(f"Got {len(ctx_ai)} msgs for AI.")
            sys_prompt=persona.format_system_prompt(user_id,username,message_text)
            if not sys_prompt or "ошибка форматирования" in sys_prompt: logger.error(f"Sys prompt format fail {persona.name}. Prompt: '{sys_prompt}'"); await update.message.reply_text(escape_markdown("ошибка подготовки ответа.", version=2), parse_mode=ParseMode.MARKDOWN_V2); db.commit(); logger.info(f"Committed changes prompt format fail."); return
            logger.debug(f"Formatted main sys prompt {persona.name}."); resp_text=await send_to_langdock(sys_prompt,ctx_ai); logger.debug(f"Got response: {resp_text[:100]}...")
            await process_and_send_response(update,context,chat_id,persona,resp_text,db)
            db.commit(); logger.info(f"Success handle_message commit chat {chat_id}, persona {persona.name}.")
        except SQLAlchemyError as e: logger.error(f"DB error handle_message: {e}", exc_info=True); try: await update.message.reply_text(escape_markdown("ошибка бд.", version=2), parse_mode=ParseMode.MARKDOWN_V2) except: pass
        except Exception as e: logger.error(f"General error handle_message chat {chat_id}: {e}", exc_info=True); try: await update.message.reply_text(escape_markdown("непредвиденная ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2) except: pass

# --- Обработчики медиа ---
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str) -> None:
    """Обрабатывает фото и голосовые сообщения."""
    if not update.message: return
    chat_id = str(update.effective_chat.id); user_id = update.effective_user.id; username = update.effective_user.username or f"user_{user_id}"
    logger.info(f"Received {media_type} user {user_id} ({username}) chat {chat_id}")
    with next(get_db()) as db:
        try:
            persona_info = get_persona_and_context_with_owner(chat_id, db)
            if not persona_info: return
            persona, _, owner = persona_info; logger.debug(f"Handling {media_type} persona '{persona.name}' owner {owner.id}")
            can_send = check_and_update_user_limits(db, owner)
            if not can_send: logger.info(f"Owner {owner.telegram_id} limit hit media."); await send_limit_exceeded_message(update, context, owner); return
            prompt_template=None; ctx_placeholder=""; sys_formatter=None
            if media_type=="photo": prompt_template=persona.photo_prompt_template; ctx_placeholder=f"{username}: прислал(а) фото."; sys_formatter=persona.format_photo_prompt
            elif media_type=="voice": prompt_template=persona.voice_prompt_template; ctx_placeholder=f"{username}: прислал(а) гс."; sys_formatter=persona.format_voice_prompt
            else: logger.error(f"Unsupported media_type '{media_type}'"); return
            if persona.chat_instance: add_message_to_context(db, persona.chat_instance.id, "user", ctx_placeholder); logger.debug(f"Added media placeholder {media_type}.")
            else: logger.error(f"No chat_instance media {persona.name}"); if update.effective_message: await update.effective_message.reply_text(escape_markdown("системная ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if persona.chat_instance and persona.chat_instance.is_muted: logger.debug(f"Persona '{persona.name}' muted media."); db.commit(); logger.info(f"Committed changes media muted."); return
            if not prompt_template or not sys_formatter: logger.info(f"Persona {persona.name} no {media_type} template."); db.commit(); logger.info(f"Committed changes media no template."); return
            ctx_ai=get_context_for_chat_bot(db, persona.chat_instance.id)
            if ctx_ai is None: logger.error(f"Failed get context AI media {persona.name}"); if update.effective_message: await update.effective_message.reply_text(escape_markdown("ошибка получения контекста.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            sys_prompt=sys_formatter()
            if not sys_prompt or "ошибка форматирования" in sys_prompt: logger.error(f"Failed format {media_type} prompt {persona.name}. Prompt:'{sys_prompt}'"); if update.effective_message: await update.effective_message.reply_text(escape_markdown(f"ошибка подготовки ответа.", version=2), parse_mode=ParseMode.MARKDOWN_V2); db.commit(); logger.info(f"Committed changes media prompt fail."); return
            logger.debug(f"Formatted {media_type} sys prompt."); resp_text=await send_to_langdock(sys_prompt,ctx_ai); logger.debug(f"Got response {media_type}: {resp_text[:100]}...")
            await process_and_send_response(update,context,chat_id,persona,resp_text,db)
            db.commit(); logger.info(f"Success handle_media {media_type} commit chat {chat_id}, persona {persona.name}.")
        except SQLAlchemyError as e: logger.error(f"DB error handle_media ({media_type}): {e}", exc_info=True); if update.effective_message: await update.effective_message.reply_text(escape_markdown("ошибка бд медиа.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"General error handle_media {media_type} chat {chat_id}: {e}", exc_info=True); if update.effective_message: await update.effective_message.reply_text(escape_markdown("ошибка обработки медиа.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo: return; await handle_media(update, context, "photo")
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice: return; await handle_media(update, context, "voice")

# --- Обработчики команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает /start, показывает приветствие и кнопки действий."""
    if not update.message: return
    user_id = update.effective_user.id; username = update.effective_user.username or f"id_{user_id}"; chat_id = str(update.effective_chat.id)
    logger.info(f"CMD /start < User {user_id} ({username}) Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            persona_info_tuple = get_persona_and_context_with_owner(chat_id, db)
            reply_text = ""; keyboard = []; bot_username = context.bot_data.get('bot_username', 'NunuAiBot')
            escaped_bot_username = escape_markdown(f"@{bot_username}", version=2)

            if persona_info_tuple and update.effective_chat.type != 'private':
                persona, _, _ = persona_info_tuple
                escaped_persona_name = escape_markdown(persona.name, version=2)
                reply_text = f"привет\! я {escaped_persona_name}\. я уже активен в этом чате\.\nИспользуй /help для списка команд\."
            else:
                check_and_update_user_limits(db, user)
                current_persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0
                db.commit(); logger.debug(f"Committed changes /start user {user_id}."); db.refresh(user)
                status = "⭐ Premium" if user.is_active_subscriber else "🆓 Free"; expires_text = f" до {user.subscription_expires_at.strftime('%d.%m.%Y')}" if user.is_active_subscriber and user.subscription_expires_at else ""
                escaped_expires = escape_markdown(expires_text, version=2)
                reply_text = (
                    f"привет\! 👋 я {escaped_bot_username}, бот для создания ai\-собеседников\.\n\n"
                    f"*твой статус:* **{escape_markdown(status, version=2)}**{escaped_expires}\n"
                    f"личности: {current_persona_count}/{user.persona_limit} \| "
                    f"сообщения сегодня: {user.daily_message_count}/{user.message_limit}\n\n"
                    "чтобы начать, выбери действие:"
                )
                keyboard = [[InlineKeyboardButton("👤 Мои личности", callback_data="show_my_personas")], [InlineKeyboardButton("➕ Создать личность", callback_data="create_persona_info")], [InlineKeyboardButton("🛒 Подписка", callback_data="subscribe_info")]]
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else ReplyKeyboardRemove()
            await update.message.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /start user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка загрузки данных.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /start user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка обработки /start.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает справку по командам."""
    if not update.message: return
    user_id = update.effective_user.id; chat_id = str(update.effective_chat.id); is_private_chat = update.effective_chat.type == 'private'
    logger.info(f"CMD /help < User {user_id} Chat {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    help_text_general = (
         "*🤖 Основные команды \(доступны всегда\):*\n"
         "`/start` \- Начальное меню и статус\n"
         "`/help` \- Эта справка\n"
         "`/profile` \- Твой статус подписки и лимиты\n"
         "`/subscribe` \- Инфо о подписке и оплата\n"
         "`/mypersonas` \- Просмотр и управление твоими личностями\n"
         "`/createpersona <имя> [описание]` \- Создать новую личность\n"
         "`/addbot <id>` \- Добавить личность в *групповой чат* \(ID из `/mypersonas`\)"
    )
    help_text_chat = (
         "\n\n*💬 Команды для чата \(где активна личность\):*\n"
         "`/mood [настроение]` \- Сменить настроение\n"
         "`/reset` \- Очистить память\n"
         "`/mutebot` \- Запретить отвечать\n"
         "`/unmutebot` \- Разрешить отвечать"
    )
    full_help_text = help_text_general
    if not is_private_chat:
         with next(get_db()) as db: persona_info = get_persona_and_context_with_owner(chat_id, db)
         if persona_info: full_help_text += help_text_chat
         else: full_help_text += "\n\n_\(Команды для чата доступны после добавления личности через `/addbot <id>`\)_"
    await update.message.reply_text(full_help_text, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)

async def mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Изменяет настроение активной личности в чате или показывает меню выбора."""
    is_callback = update.callback_query is not None; msg = update.callback_query.message if is_callback else update.message; if not msg: return
    chat_id=str(msg.chat.id); user=update.effective_user; user_id=user.id; username=user.username or f"id_{user_id}"; logger.info(f"CMD /mood or Action < User {user_id} ({username}) Chat {chat_id}")
    inst=None; persona=None; moods=[]; cur_mood="нейтрально"; p_id="unknown"
    with next(get_db()) as db:
        try: info = get_persona_and_context_with_owner(chat_id, db)
            if not info: reply=escape_markdown("В этом чате нет активной личности. 🤷‍♂️\nДобавь личность командой `/addbot <id>` (ID можно узнать через /mypersonas в личке со мной).", version=2); logger.debug(f"No active persona chat {chat_id} /mood."); try: await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN_V2) if is_callback else await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2) catch Exception as send_err: logger.error(f"Err send no persona: {send_err}"); return
            persona,_,owner=info; inst=persona.chat_instance; p_id=persona.id; escaped_persona_name = escape_markdown(persona.name, version=2)
            if not inst: logger.error(f"No ChatBotInstance {persona.name} chat {chat_id}"); await msg.reply_text(escape_markdown("Ошибка: нет экземпляра бота.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if inst.is_muted: logger.debug(f"Persona '{persona.name}' muted."); reply=escape_markdown(f"Личность '{persona.name}' заглушена (/unmutebot).", version=2); try: await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN_V2) if is_callback else await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2) catch Exception as send_err: logger.error(f"Err send muted: {send_err}"); return
            moods=persona.get_all_mood_names(); cur_mood=inst.current_mood or "нейтрально"
            if not moods: reply = escape_markdown(f"У личности '{persona.name}' не настроены настроения.", version=2); logger.warning(f"{persona.name} no moods."); try: await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN_V2) if is_callback else await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2) catch Exception as send_err: logger.error(f"Err send no moods: {send_err}"); return
        except SQLAlchemyError as e: logger.error(f"DB error fetch /mood chat {chat_id}: {e}", exc_info=True); await msg.reply_text(escape_markdown("ошибка бд.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
        except Exception as e: logger.error(f"Error fetch /mood chat {chat_id}: {e}", exc_info=True); await msg.reply_text(escape_markdown("непредвиденная ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    moods_lower={m.lower():m for m in moods}; arg_lower=None; target_mood=None
    if is_callback and query.data.startswith("set_mood_"): parts=query.data.split('_'); arg_lower="_".join(parts[2:-1]).lower() if len(parts)>=4 else None; target_mood=moods_lower.get(arg_lower)
    elif not is_callback: text=""; args=context.args; if args: text=" ".join(args); elif update.message and update.message.text: poss=update.message.text.strip(); text=poss if poss.lower() in moods_lower else ""
        if text: arg_lower=text.lower(); target_mood=moods_lower.get(arg_lower)
    escaped_cur_mood = escape_markdown(cur_mood, version=2)
    if target_mood and inst: set_mood_for_chat_bot(SessionLocal(), inst.id, target_mood); reply=f"Настроение для '{escaped_persona_name}' теперь: *{escape_markdown(target_mood, version=2)}*"; logger.info(f"Mood set {persona.name} chat {chat_id} -> {target_mood}.")
    else: kbd=[[InlineKeyboardButton(m.capitalize(), callback_data=f"set_mood_{m.lower()}_{p_id}")] for m in moods]; markup=InlineKeyboardMarkup(kbd); reply=f"Текущее настроение: *{escaped_cur_mood}*\. Выбери новое для '{escaped_persona_name}':" if not arg_lower else f"Не знаю настроения '{escape_markdown(arg_lower, version=2)}'\. Выбери из списка:"; logger.debug(f"{'Invalid mood' if arg_lower else 'Sent'} mood selection chat {chat_id}.")
    try: if is_callback: await query.edit_message_text(reply, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2) if (query.message.text != reply or query.message.reply_markup != markup) else await query.answer(f"Настроение: {target_mood}" if target_mood else None)
        else: await msg.reply_text(reply, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e: logger.warning(f"Failed to edit mood message (maybe not modified?): {e}")
    except Exception as send_err: logger.error(f"Error sending mood msg/kbd: {send_err}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return; chat_id=str(update.effective_chat.id); user_id=update.effective_user.id; username=update.effective_user.username or f"id_{user_id}"; logger.info(f"CMD /reset < User {user_id} ({username}) Chat {chat_id}"); await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            info = get_persona_and_context_with_owner(chat_id, db)
            if not info: reply=escape_markdown("В этом чате нет активной личности для сброса. 🤷‍♂️\nДобавь личность командой `/addbot <id>`.", version=2); await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2); return
            persona, _, owner = info; escaped_persona_name = escape_markdown(persona.name, version=2)
            if owner.telegram_id != user_id and not is_admin(user_id): logger.warning(f"User {user_id} attempted reset {persona.name} owned by {owner.telegram_id}"); await update.message.reply_text(escape_markdown("Только владелец может сбросить память.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            inst = persona.chat_instance
            if not inst: logger.error(f"Reset cmd: No ChatBotInstance {persona.name}"); await update.message.reply_text(escape_markdown("Ошибка: не найден экземпляр бота.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            deleted_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == inst.id).delete(synchronize_session='fetch')
            db.commit(); logger.info(f"Deleted {deleted_count} context msgs {inst.id} ('{persona.name}') chat {chat_id} by {user_id}.")
            await update.message.reply_text(f"Память личности '{escaped_persona_name}' очищена\.", parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /reset chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка БД при сбросе.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /reset chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка при сбросе.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def create_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return; user_id=update.effective_user.id; username=update.effective_user.username or f"id_{user_id}"; chat_id=str(update.effective_chat.id); logger.info(f"CMD /createpersona < User {user_id} ({username}) args: {context.args}"); await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    args=context.args
    if not args: await update.message.reply_text(escape_markdown("укажи имя:\n`/createpersona <имя> [описание]`\n_имя: 2-50, описание: до 1500 (необяз.)_", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    name=args[0]; desc=" ".join(args[1:]) if len(args)>1 else None; escaped_name = escape_markdown(name, version=2)
    if len(name)<2 or len(name)>50: await update.message.reply_text(escape_markdown("имя: 2-50 символов.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    if desc and len(desc)>1500: await update.message.reply_text(escape_markdown("описание: макс 1500.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    with next(get_db()) as db:
        try:
            user=get_or_create_user(db, user_id, username); count=db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar()
            user_for_check = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).one() # Для can_create_persona
            if not user_for_check.can_create_persona: logger.warning(f"User {user_id} limit hit ({count}/{user.persona_limit})."); status="⭐ Premium" if user.is_active_subscriber else "🆓 Free"; text=f"упс\! лимит личностей \({count}/{user.persona_limit}\) для статуса **{escape_markdown(status, version=2)}**\. 😟\nИспользуй /subscribe"; await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2); return
            existing=get_persona_by_name_and_owner(db, user.id, name);
            if existing: await update.message.reply_text(f"имя '{escaped_name}' уже есть\. выбери другое\.", parse_mode=ParseMode.MARKDOWN_V2); return
            new_p=create_persona_config(db, user.id, name, desc); escaped_new_name = escape_markdown(new_p.name, version=2)
            await update.message.reply_text(f"✅ личность '{escaped_new_name}' создана\!\nid: `{new_p.id}`\nуправляй через /mypersonas", parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} created persona: '{new_p.name}' (ID: {new_p.id})")
        except IntegrityError: logger.warning(f"IntegrityError create_persona user {user_id} name '{name}'."); await update.message.reply_text(f"ошибка: имя '{escaped_name}' уже занято\.", parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"SQLAlchemyError create_persona user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка бд.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error create_persona user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def my_personas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_callback = update.callback_query is not None; message = update.callback_query.message if is_callback else update.message; if not message: return
    user_id = update.effective_user.id; username = update.effective_user.username or f"id_{user_id}"; chat_id = str(message.chat.id); logger.info(f"CMD /mypersonas or CB < User {user_id} ({username}) Chat {chat_id}")
    if is_callback: await update.callback_query.answer()
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username)
            user_with_personas = db.query(User).options(joinedload(User.persona_configs)).filter(User.id == user.id).first()
            if not user_with_personas: logger.error(f"User {user_id} not found my_personas."); await message.reply_text(escape_markdown("Ошибка: юзер не найден.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            personas = sorted(user_with_personas.persona_configs, key=lambda p: p.name.lower()) if user_with_personas.persona_configs else []
            limit = user_with_personas.persona_limit; count = len(personas)
            db.commit()
            if not personas: text = f"у тебя нет личностей \(лимит: {count}/{limit}\)\.\nСоздай: `/createpersona <имя>`"; markup = None
            else: text = f"*Твои личности \({count}/{limit}\):*\n\nНажми на имя личности для редактирования\.\nДобавить в чат: `/addbot <id>` \(используй в нужном чате\)\."; kbd = []
                for p in personas: escaped_p_name=escape_markdown(p.name, version=2); kbd.append([InlineKeyboardButton(f"👤 {escaped_p_name} (ID: {p.id})", callback_data=f"edit_persona_{p.id}")]); kbd.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_persona_{p.id}")])
                markup = InlineKeyboardMarkup(kbd)
            if is_callback: query=update.callback_query; await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2) if (query.message.text != text or query.message.reply_markup != markup) else None
            else: await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"User {user_id} mypersonas. Sent {count} personas.")
        except SQLAlchemyError as e: logger.error(f"DB error /mypersonas user {user_id}: {e}", exc_info=True); await message.reply_text(escape_markdown("ошибка загрузки личностей.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /mypersonas user {user_id}: {e}", exc_info=True); await message.reply_text(escape_markdown("ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def add_bot_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_id: Optional[int] = None) -> None:
    is_callback = update.callback_query is not None; msg = update.callback_query.message if is_callback else update.message; if not msg: return
    user=update.effective_user; user_id=user.id; username=user.username or f"id_{user_id}"; chat_id=str(msg.chat.id); chat_title=msg.chat.title or f"Chat {chat_id}"; local_pid = persona_id
    if is_callback and local_pid is None: try: local_pid=int(query.data.split('_')[-1]) catch (IndexError, ValueError): logger.error(f"Parse pid fail CB: {query.data}"); await query.answer("Ошибка ID.", show_alert=True); return
    elif not is_callback: args=context.args; logger.info(f"CMD /addbot < User {user_id} ({username}) Chat '{chat_title}' ({chat_id}) args: {args}"); if not args or len(args)!=1 or not args[0].isdigit(): await msg.reply_text(escape_markdown("формат: `/addbot <id>`\n(ID из /mypersonas)", version=2), parse_mode=ParseMode.MARKDOWN_V2); return; try: local_pid=int(args[0]) catch ValueError: await msg.reply_text(escape_markdown("ID числом.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    if local_pid is None: logger.error("add_bot: pid is None."); await query.answer("Ошибка ID.", show_alert=True) if is_callback else await msg.reply_text(escape_markdown("Ошибка ID.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    if is_callback: await query.answer("Добавляем...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            persona=get_persona_by_id_and_owner(db, user_id, local_pid); escaped_pid = escape_markdown(str(local_pid), version=2)
            if not persona: response=f"личность id `{escaped_pid}` не найдена или не твоя."; await query.edit_message_text(response, parse_mode=ParseMode.MARKDOWN_V2) if is_callback else await msg.reply_text(response, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2); return
            escaped_persona_name = escape_markdown(persona.name, version=2)
            existing=db.query(ChatBotInstance).filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True).options(joinedload(ChatBotInstance.bot_instance_ref)).first()
            if existing:
                if existing.bot_instance_ref and existing.bot_instance_ref.persona_config_id == local_pid: response=f"личность '{escaped_persona_name}' уже активна."; await query.answer(response, show_alert=True) if is_callback else await msg.reply_text(response, parse_mode=ParseMode.MARKDOWN_V2); return
                else: logger.info(f"Deactivating prev bot {existing.bot_instance_id} chat {chat_id}"); existing.active = False
            owner_int_id=persona.owner_id; bot_inst=db.query(BotInstance).filter(BotInstance.persona_config_id == local_pid).first()
            if not bot_inst: logger.info(f"Creating BotInstance persona {local_pid}"); bot_inst=BotInstance(owner_id=owner_int_id, persona_config_id=local_pid, name=f"Inst:{persona.name}"[:50]); db.add(bot_inst); db.flush(); db.refresh(bot_inst); logger.info(f"Staged BotInstance {bot_inst.id}")
            chat_link=db.query(ChatBotInstance).filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.bot_instance_id == bot_inst.id).first(); needs_clear=False
            if chat_link:
                if not chat_link.active: logger.info(f"Reactivating CBI {chat_link.id}"); chat_link.active=True; chat_link.current_mood="нейтрально"; chat_link.is_muted=False; needs_clear=True
                else: logger.debug(f"CBI link {bot_inst.id} chat {chat_id} already active.")
            else: logger.info(f"Creating CBI link bot {bot_inst.id} chat {chat_id}"); chat_link=ChatBotInstance(chat_id=chat_id, bot_instance_id=bot_inst.id, active=True, current_mood="нейтрально", is_muted=False); db.add(chat_link); needs_clear=True
            if needs_clear and chat_link: db.flush(); db.refresh(chat_link); deleted=db.query(ChatContext).filter(ChatContext.chat_bot_instance_id==chat_link.id).delete(synchronize_session='fetch'); logger.debug(f"Cleared {deleted} context {chat_link.id}.")
            db.commit(); logger.info(f"Committed add_bot_to_chat bot {bot_inst.id} (Persona {local_pid}) chat {chat_id}. CBI ID: {chat_link.id if chat_link else 'N/A'}")
            response=f"✅ личность '{escaped_persona_name}' \(id: `{escaped_pid}`\) активирована в этом чате\!"; response+=" Память очищена\." if needs_clear else ""
            await context.bot.send_message(chat_id=chat_id, text=response, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2)
            if is_callback: try: await query.delete_message() catch Exception as del_err: logger.warning(f"Could not delete CB msg: {del_err}")
        except IntegrityError as e: logger.warning(f"IntegrityError addbot pid {local_pid} chat {chat_id}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=escape_markdown("произошла ошибка целостности данных (возможно, конфликт активации), попробуйте еще раз.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /addbot pid {local_pid} chat {chat_id}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=escape_markdown("ошибка бд.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error adding bot {local_pid} chat {chat_id}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=escape_markdown("ошибка активации.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; if not query or not query.data: return
    chat_id_obj=query.message.chat if query.message else None; chat_id=str(chat_id_obj.id) if chat_id_obj else "Unknown"; user=query.from_user; user_id=user.id; username=user.username or f"id_{user_id}"; data=query.data; logger.info(f"CALLBACK < User {user_id} ({username}) Chat {chat_id} data: {data}")
    if data == "show_my_personas": await query.answer(); await my_personas(update, context)
    elif data == "create_persona_info": await query.answer(); await query.edit_message_text(escape_markdown("Чтобы создать новую личность, используй команду:\n`/createpersona <имя> [описание]`\n\n*Пример:* `/createpersona мой_бот я веселый бот для друзей`", version=2), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")]]), parse_mode=ParseMode.MARKDOWN_V2)
    elif data == "subscribe_info": await query.answer(); await subscribe(update, context, from_callback=True)
    elif data == "back_to_start": await query.answer(); await query.delete_message() if query.message else None; await start(update, context) # Удаляем сообщение с подсказкой и вызываем start
    elif data == "view_tos": await query.answer(); await view_tos(update, context)
    elif data == "confirm_pay": await query.answer(); await confirm_pay(update, context)
    elif data == "subscribe_pay": await query.answer("Создаю ссылку..."); await generate_payment_link(update, context)
    elif data.startswith("set_mood_"): await query.answer(); await mood(update, context)
    elif data.startswith("add_bot_"): logger.warning(f"Received add_bot_ CB '{data}'."); await query.answer("Действие не поддерживается здесь", show_alert=True) # Убрали кнопку, но обработчик оставим
    elif data.startswith("dummy_"): await query.answer()
    elif data.startswith(("edit_persona_", "delete_persona_", "edit_field_", "edit_mood", "deletemood", "cancel_edit", "edit_persona_back", "delete_persona_confirm_", "delete_persona_cancel")): logger.debug(f"CB '{data}' routed to ConvHandler."); pass
    else: logger.warning(f"Unhandled CB data: {data} user {user_id}"); try: await query.answer("Неизвестно") catch Exception as e: logger.warning(f"Failed answer unhandled CB {query.id}: {e}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return; user_id = update.effective_user.id; username = update.effective_user.username or f"id_{user_id}"; logger.info(f"CMD /profile < User {user_id} ({username})"); await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    with next(get_db()) as db:
        try:
            user = get_or_create_user(db, user_id, username); check_and_update_user_limits(db, user);
            persona_count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == user.id).scalar() or 0; db.commit(); logger.debug(f"Committed changes /profile user {user_id}."); db.refresh(user)
            is_active = user.is_active_subscriber; status = "⭐ Premium" if is_active else "🆓 Free"; expires = f"активна до: {user.subscription_expires_at.strftime('%d.%m.%Y %H:%M')} UTC" if is_active and user.subscription_expires_at else "нет активной подписки"
            escaped_expires = escape_markdown(expires, version=2); escaped_status = escape_markdown(status, version=2)
            text = (f"👤 **твой профиль**\n\nстатус: **{escaped_status}**\n{escaped_expires}\n\n**лимиты:**\nсообщения сегодня: {user.daily_message_count}/{user.message_limit}\nсоздано личностей: {persona_count}/{user.persona_limit}\n\n")
            if not is_active: text += "🚀 хочешь больше? жми /subscribe \!"
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /profile user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка бд.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /profile user {user_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> None:
    user=update.effective_user; user_id=user.id; username=user.username or f"id_{user_id}"; logger.info(f"CMD /subscribe or CB < User {user_id} ({username})")
    msg=update.callback_query.message if from_callback else update.message; if not msg: return
    yk_ready = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit())
    text=""; markup=None
    if not yk_ready: text = escape_markdown("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)", version=2); logger.warning("YK creds not set/invalid subscribe handler.")
    else:
        text = (
            f"✨ **премиум подписка \({config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}/мес\)** ✨\n\n"
            "получи максимум возможностей:\n"
            f"✅ **{config.PAID_DAILY_MESSAGE_LIMIT}** сообщений в день \(вместо {config.FREE_DAILY_MESSAGE_LIMIT}\)\n"
            f"✅ **{config.PAID_PERSONA_LIMIT}** личностей \(вместо {config.FREE_PERSONA_LIMIT}\)\n"
            f"✅ полная настройка всех промптов\n"
            f"✅ создание и редакт\. своих настроений\n\n"
            # f"✅ приоритетная поддержка \(если будет\)\n\n"
            f"подписка действует {config.SUBSCRIPTION_DURATION_DAYS} дней\."
        )
        kbd = [[InlineKeyboardButton("📜 Условия использования", callback_data="view_tos")], [InlineKeyboardButton("✅ Принять Условия и перейти к оплате", callback_data="confirm_pay")]]
        if from_callback: kbd.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")])
        markup = InlineKeyboardMarkup(kbd)
    try:
        if from_callback: query=update.callback_query; await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2) if (query.message.text != text or query.message.reply_markup != markup) else None
        else: await msg.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e: logger.warning(f"Failed edit subscribe msg (not modified?): {e}")
    except Exception as e: logger.error(f"Failed send/edit subscribe msg user {user_id}: {e}"); if from_callback: try: await context.bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2) catch Exception as send_e: logger.error(f"Failed send fallback subscribe msg user {user_id}: {send_e}")

async def view_tos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query=update.callback_query; if not query or not query.message: return; user_id=query.from_user.id; logger.info(f"User {user_id} requested ToS.")
    tos_url = context.bot_data.get('tos_url'); text = ""; kbd = []
    if tos_url: kbd = [[InlineKeyboardButton("📜 Открыть Соглашение (Telegra.ph)", url=tos_url)], [InlineKeyboardButton("⬅️ Назад к Подписке", callback_data="subscribe_info")]]; text = escape_markdown("Пожалуйста, ознакомьтесь с Пользовательским Соглашением, открыв его по ссылке ниже:", version=2)
    else: logger.error(f"ToS URL not found bot_data user {user_id}."); text = escape_markdown("❌ Не удалось загрузить ссылку на Пользовательское Соглашение. Попробуйте позже или запросите у администратора.", version=2); kbd = [[InlineKeyboardButton("⬅️ Назад к Подписке", callback_data="subscribe_info")]]
    markup=InlineKeyboardMarkup(kbd)
    try: await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN_V2) if (query.message.text != text or query.message.reply_markup != markup) else None
    except BadRequest as e: logger.warning(f"Failed edit ToS msg (not modified?): {e}")
    except Exception as e: logger.error(f"Failed show ToS user {user_id}: {e}"); await query.answer("Не удалось отобразить.", show_alert=True)

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query=update.callback_query; if not query or not query.message: return; user_id=query.from_user.id; logger.info(f"User {user_id} confirmed ToS.")
    tos_url=context.bot_data.get('tos_url'); yk_ready=bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit()); text=""; markup=None
    if not yk_ready: text=escape_markdown("К сожалению, функция оплаты сейчас недоступна. 😥 (проблема с настройками)", version=2); markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]); logger.warning("YK creds not set/invalid confirm_pay.")
    else: tos_link=f"[Пользовательским Соглашением]({tos_url})" if tos_url else "Пользовательским Соглашением"; text = f"✅ Отлично\!\n\nНажимая кнопку 'Оплатить' ниже, вы подтверждаете, что ознакомились и полностью согласны с {tos_link}\.\n\n👇"; kbd = [[InlineKeyboardButton(f"💳 Оплатить {config.SUBSCRIPTION_PRICE_RUB:.0f} {config.SUBSCRIPTION_CURRENCY}", callback_data="subscribe_pay")]];
        if tos_url: kbd.append([InlineKeyboardButton("📜 Условия (прочитано)", url=tos_url)])
        kbd.append([InlineKeyboardButton("⬅️ Назад к описанию", callback_data="subscribe_info")]); markup=InlineKeyboardMarkup(kbd)
    try: await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=not bool(tos_url), parse_mode=ParseMode.MARKDOWN_V2) if (query.message.text != text or query.message.reply_markup != markup) else None
    except BadRequest as e: logger.warning(f"Failed edit confirm_pay msg (not modified?): {e}")
    except Exception as e: logger.error(f"Failed show final payment confirm user {user_id}: {e}")

async def generate_payment_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = query.from_user.id; logger.info(f"--- generate_payment_link ENTERED user {user_id} ---")
    yk_ready = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit()); if not yk_ready: logger.error("YK creds invalid payment gen."); await query.edit_message_text(escape_markdown("❌ ошибка: сервис оплаты не настроен.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    shop_id_str=config.YOOKASSA_SHOP_ID; secret=config.YOOKASSA_SECRET_KEY; shop_id_int=0
    try: shop_id_int = int(shop_id_str);
        if not YookassaConfig.secret_key or YookassaConfig.account_id != shop_id_int: logger.info(f"Configuring YK SDK gen_link. Shop: {shop_id_str}, Secret Set: {bool(secret)}"); YookassaConfig.configure(shop_id_int, secret);
        if not (YookassaConfig.account_id == shop_id_int and YookassaConfig.secret_key == secret): raise RuntimeError("Failed YK config check.")
        logger.info("YK SDK configured successfully.")
    except ValueError: logger.error(f"YK Shop ID '{shop_id_str}' invalid int."); await query.edit_message_text(escape_markdown("❌ ошибка: неверный формат ID магазина.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    except Exception as conf_e: logger.error(f"Failed YK config gen_link: {conf_e}", exc_info=True); await query.edit_message_text(escape_markdown("❌ ошибка конфигурации платежей.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    idempotence=str(uuid.uuid4()); desc=f"Premium @NunuAiBot {config.SUBSCRIPTION_DURATION_DAYS} дней (User: {user_id})"; meta={'telegram_user_id': str(user_id)}; bot_user="YourBot"; try: me=await context.bot.get_me(); bot_user=me.username or bot_user catch Exception: pass; return_url=f"https://t.me/{bot_user}"
    try: price=f"{config.SUBSCRIPTION_PRICE_RUB:.2f}"; items=[ReceiptItem({"description":f"Премиум @{bot_user} {config.SUBSCRIPTION_DURATION_DAYS} дн.","quantity":1.0,"amount":{"value":price,"currency":config.SUBSCRIPTION_CURRENCY},"vat_code":"1","payment_mode":"full_prepayment","payment_subject":"service"})]; email=f"user_{user_id}@telegram.bot"; receipt=Receipt({"customer":{"email":email},"items":items}); logger.debug("Receipt prepared.")
    except Exception as r_e: logger.error(f"Error prep receipt: {r_e}", exc_info=True); await query.edit_message_text(escape_markdown("❌ ошибка данных чека.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
    try: builder=PaymentRequestBuilder(); builder.set_amount({"value":price,"currency":config.SUBSCRIPTION_CURRENCY}).set_capture(True).set_confirmation({"type":"redirect","return_url":return_url}).set_description(desc).set_metadata(meta).set_receipt(receipt); request=builder.build()
        logger.info(f"Attempt YK Payment.create. Shop: {YookassaConfig.account_id}, Idemp: {idempotence}"); logger.debug(f"Payment request: {request.json()}")
        payment = await asyncio.to_thread(Payment.create, request, idempotence)
        if not payment or not payment.confirmation or not payment.confirmation.confirmation_url: logger.error(f"YK API invalid resp user {user_id}. Status: {payment.status if payment else 'N/A'}. Resp: {payment}"); err_msg=escape_markdown("❌ не удалось получить ссылку от платежей", version=2); err_msg+=f" \(статус: {payment.status}\)" if payment and payment.status else ""; err_msg+="\\.\nПопробуй позже."; await query.edit_message_text(err_msg, parse_mode=ParseMode.MARKDOWN_V2); return
        confirm_url=payment.confirmation.confirmation_url; logger.info(f"Created YK payment {payment.id} user {user_id}. URL: {confirm_url}")
        kbd=[[InlineKeyboardButton("🔗 перейти к оплате", url=confirm_url)], [InlineKeyboardButton("⬅️ Назад", callback_data="subscribe_info")]]; markup=InlineKeyboardMarkup(kbd)
        await query.edit_message_text(escape_markdown("✅ ссылка для оплаты создана!\n\nнажми кнопку ниже. после успеха подписка активируется (может занять пару минут).", version=2), reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e: logger.error(f"Error YK payment create user {user_id}: {e}", exc_info=True); user_msg="❌ не удалось создать ссылку\. "; err_data=None
        if hasattr(e,'response') and hasattr(e.response,'json'): try: err_data=e.response.json() except: pass
        if err_data and err_data.get('type')=='error': code=err_data.get('code'); desc=err_data.get('description') or err_data.get('message'); logger.error(f"YK API Error: Code={code}, Desc={desc}, Data={err_data}"); user_msg+=f"\({escape_markdown(desc or code or 'детали в логах', version=2)}\)"
        elif isinstance(e,httpx.RequestError): user_msg+="Проблема сети с ЮKassa\."
        else: user_msg+="Непредвиденная ошибка\."; user_msg+="\nПопробуй позже\."; try: await query.edit_message_text(user_msg, parse_mode=ParseMode.MARKDOWN_V2) catch Exception as send_e: logger.error(f"Failed send error after payment fail: {send_e}")

# --- Conversation Handlers (полные версии) ---
# Вставляем сюда полные версии функций ConversationHandler из предыдущего ответа
# _start_edit_convo, edit_persona_start, edit_persona_button_callback, edit_persona_choice,
# edit_field_update, edit_max_messages_update, _get_edit_persona_keyboard,
# _try_return_to_edit_menu, _try_return_to_mood_menu, edit_moods_menu, edit_mood_choice,
# edit_mood_name_received, edit_mood_prompt_received, delete_mood_confirmed,
# _get_edit_moods_keyboard_internal, edit_persona_cancel, _start_delete_convo,
# delete_persona_start, delete_persona_button_callback, delete_persona_confirmed,
# delete_persona_cancel

# --- Mute/Unmute Handlers ---
async def mute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return; chat_id=str(update.effective_chat.id); user_id=update.effective_user.id; logger.info(f"CMD /mutebot < User {user_id} Chat {chat_id}")
    with next(get_db()) as db:
        try: info=get_persona_and_context_with_owner(chat_id, db);
            if not info: reply=escape_markdown("В этом чате нет активной личности. 🤷‍♂️\nДобавь: `/addbot <id>`.", version=2); await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2); return
            persona, _, owner = info; inst = persona.chat_instance; escaped_name = escape_markdown(persona.name, version=2)
            if owner.telegram_id != user_id and not is_admin(user_id): await update.message.reply_text(escape_markdown("Только владелец может заглушить.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if not inst: await update.message.reply_text(escape_markdown("Ошибка: нет объекта связи.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if not inst.is_muted: inst.is_muted=True; db.commit(); logger.info(f"Persona '{persona.name}' muted chat {chat_id} by {user_id}."); await update.message.reply_text(f"✅ Личность '{escaped_name}' заглушена\. \(используй /unmutebot\)", parse_mode=ParseMode.MARKDOWN_V2)
            else: await update.message.reply_text(f"Личность '{escaped_name}' уже заглушена\.", parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /mutebot chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка БД.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /mutebot chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

async def unmute_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return; chat_id=str(update.effective_chat.id); user_id=update.effective_user.id; logger.info(f"CMD /unmutebot < User {user_id} Chat {chat_id}")
    with next(get_db()) as db:
        try:
            inst = db.query(ChatBotInstance).options(joinedload(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.owner), joinedload(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.persona_config)).filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True).first()
            if not inst: reply=escape_markdown("В этом чате нет активной личности. 🤷‍♂️\nДобавь: `/addbot <id>`.", version=2); await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN_V2); return
            owner = inst.bot_instance_ref.owner if inst.bot_instance_ref else None; name_raw = inst.bot_instance_ref.persona_config.name if inst.bot_instance_ref and inst.bot_instance_ref.persona_config else "Неизвестная"; name = escape_markdown(name_raw, version=2)
            if not owner: await update.message.reply_text(escape_markdown("Ошибка: нет владельца.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if owner.telegram_id != user_id and not is_admin(user_id): await update.message.reply_text(escape_markdown("Только владелец может снять заглушку.", version=2), parse_mode=ParseMode.MARKDOWN_V2); return
            if inst.is_muted: inst.is_muted=False; db.commit(); logger.info(f"Persona '{name_raw}' unmuted chat {chat_id} by {user_id}."); await update.message.reply_text(f"✅ Личность '{name}' снова может отвечать\.", parse_mode=ParseMode.MARKDOWN_V2)
            else: await update.message.reply_text(f"Личность '{name}' не была заглушена\.", parse_mode=ParseMode.MARKDOWN_V2)
        except SQLAlchemyError as e: logger.error(f"DB error /unmutebot chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка БД.", version=2), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e: logger.error(f"Error /unmutebot chat {chat_id}: {e}", exc_info=True); await update.message.reply_text(escape_markdown("Ошибка.", version=2), parse_mode=ParseMode.MARKDOWN_V2)

# --- Конец handlers.py ---
