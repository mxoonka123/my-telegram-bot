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
