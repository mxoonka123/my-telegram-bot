import asyncio
import logging
import random
import httpx
import re
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes
from telegram.error import TelegramError, BadRequest, Forbidden # Added Forbidden
from typing import List, Dict, Any, Optional, Union, Tuple
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError, ProgrammingError # Added ProgrammingError
from sqlalchemy import func, select, update as sql_update

from db import (
    get_all_active_chat_bot_instances, SessionLocal, User, ChatBotInstance, BotInstance,
    get_db, PersonaConfig
)
from persona import Persona
from utils import postprocess_response, extract_gif_links, escape_markdown_v2
from config import FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_USER_MONTHLY_MESSAGE_LIMIT # <-- ИСПРАВЛЕННЫЙ ИМПОРТ

logger = logging.getLogger(__name__)

async def check_subscription_expiry_task(context: ContextTypes.DEFAULT_TYPE):
    """[DEPRECATED] Задача проверки подписок отключена, так как подписочная модель удалена."""
    try:
        logger.info("check_subscription_expiry_task called but deprecated. No action taken.")
    except Exception:
        pass


async def proactive_messaging_task(application: Application) -> None:
    """Периодически инициирует проактивные сообщения, учитывая настройку частоты у персон.

    Частоты:
    - never: никогда не отправлять
    - rarely: низкая вероятность
    - sometimes: средняя вероятность
    - often: повышенная вероятность

    Реализация намеренно простая: в каждом цикле опрашиваем активные ChatBotInstance, смотрим персону чата,
    по вероятности решаем, отправлять ли короткий пинг-сообщение в чат.
    """
    logger.info("proactive_messaging_task: старт")
    # Базовые интервалы между итерациями цикла (джиттер добавим)
    base_sleep_sec = 60
    # Веса вероятностей на попытку отправки (0..1)
    prob_map = {
        "never": 0.0,
        "rarely": 0.05,
        "sometimes": 0.15,
        "often": 0.35,
    }

    while True:
        try:
            # лёгкий джиттер, чтобы не биться в ровную сетку
            sleep_this = base_sleep_sec + random.randint(-10, 15)
            if sleep_this < 30:
                sleep_this = 30

            # Получаем все активные ChatBotInstance
            with get_db() as db:
                instances = get_all_active_chat_bot_instances(db)

                for inst in instances:
                    try:
                        # Пропускаем, если нет связанной персоны
                        persona: Optional[PersonaConfig] = getattr(inst, 'persona_config', None)
                        if not persona:
                            continue

                        rate = getattr(persona, 'proactive_messaging_rate', None) or 'sometimes'
                        prob = prob_map.get(rate, 0.0)
                        if prob <= 0:
                            continue  # never

                        # Простая вероятностная проверка
                        if random.random() > prob:
                            continue

                        chat_id = inst.chat_id
                        # Отправляем короткое ненавязчивое сообщение-пинг (строго русский, нижний регистр)
                        text = "как дела?"  # placeholder, можно позже сделать умнее на основе контекста
                        try:
                            await application.bot.send_message(chat_id=chat_id, text=text, parse_mode=None, disable_notification=True)
                        except (BadRequest, Forbidden) as te:
                            logger.warning(f"proactive message send failed for chat {chat_id}: {te}")
                        except TelegramError as te:
                            logger.warning(f"telegram error while proactive send to {chat_id}: {te}")
                    except Exception as per_inst_e:
                        logger.exception(f"error in proactive loop per instance: {per_inst_e}")

        except Exception as e:
            logger.exception(f"proactive_messaging_task loop error: {e}")
        finally:
            try:
                await asyncio.sleep(sleep_this)
            except Exception:
                await asyncio.sleep(60)
