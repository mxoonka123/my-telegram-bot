# -*- coding: utf-8 -*-
import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Настройка логгера
logger = logging.getLogger(__name__)

# ID администратора. Можно указать несколько через запятую.
# Загружаем как строку из переменных окружения
ADMIN_USER_ID_STR = os.getenv("ADMIN_USER_ID", "1324596928")
ADMIN_USER_ID = []
try:
    # Сразу преобразуем строку в список int
    ADMIN_USER_ID = [int(uid.strip()) for uid in ADMIN_USER_ID_STR.split(',') if uid.strip()]
    logger.info(f"Admin IDs loaded: {ADMIN_USER_ID}")
except (ValueError, TypeError):
    logger.error(f"Could not parse ADMIN_USER_ID: '{ADMIN_USER_ID_STR}'. Make sure it's a comma-separated list of numbers.")
CHANNEL_ID = "@NuNuAiChannel" # ID или юзернейм вашего канала

# Premium User Limits
PREMIUM_USER_MONTHLY_MESSAGE_LIMIT = 1000
PREMIUM_USER_MESSAGE_TOKEN_LIMIT = 120
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# Gemini Settings (native Google API only)
# API-ключи теперь хранятся в БД (таблица ApiKey) и выбираются динамически.
# Модели
OPENROUTER_MODEL_NAME = os.getenv("OPENROUTER_MODEL_NAME", "google/gemini-2.5-pro")
GEMINI_MODEL_NAME_FOR_API = os.getenv("GEMINI_MODEL_NAME_FOR_API", "gemini-2.5-flash-lite")

# Бесплатные ответы на фото: включите, чтобы фото всегда шли в бесплатную Gemini и кредиты не списывались
# Можно переопределить через переменные окружения.
FREE_IMAGE_RESPONSES = os.getenv("FREE_IMAGE_RESPONSES", "true").lower() in ("1", "true", "yes", "y")

# Явно указываем бесплатную модель для изображений, если хотим отличать от текстовой
GEMINI_FREE_IMAGE_MODEL = os.getenv("GEMINI_FREE_IMAGE_MODEL", "gemini-2.5-flash-lite")

# Отдельная модель OpenRouter для фотографий (если у пользователя есть кредиты)
# Явно устанавливаем ту же модель, что и для текста, чтобы избежать ошибок с недоступными моделями по умолчанию.
# При необходимости можно переопределить через переменную окружения OPENROUTER_IMAGE_MODEL_NAME.
# Используем единую мощную модель Google Gemini 2.5 Pro для изображений по умолчанию (можно переопределить через env).
OPENROUTER_IMAGE_MODEL_NAME = os.getenv("OPENROUTER_IMAGE_MODEL_NAME", "google/gemini-2.5-pro")
# Базовый URL теперь не содержит имя модели, оно будет подставляться при вызове
GEMINI_API_BASE_URL_TEMPLATE = os.getenv(
    "GEMINI_API_BASE_URL_TEMPLATE",
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.critical("CRITICAL: Переменная окружения DATABASE_URL не установлена!")
    # Для большей надежности в продакшене, можно раскомментировать следующую строку,
    # чтобы бот падал, если DATABASE_URL не установлена.
    # raise RuntimeError("CRITICAL: Переменная окружения DATABASE_URL не установлена. Бот не может запуститься.")
else:
    # Логируем только часть строки подключения для безопасности
    db_url_log_display = DATABASE_URL[:DATABASE_URL.find('@') + 1 if '@' in DATABASE_URL else 30] + "..." if len(DATABASE_URL) > 30 else DATABASE_URL
    logger.info(f"INFO: DATABASE_URL успешно загружена из окружения: {db_url_log_display}")

# Database Pool Settings (configurable via env)
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "90"))  # Таймаут подключения к БД в секундах

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "") # ID магазина
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "") # Секретный ключ

# URL вашего приложения на Railway
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL_BASE", "")

# Параметры подписки и лимиты
SUBSCRIPTION_PRICE_RUB = 699.00
SUBSCRIPTION_CURRENCY = "RUB"
SUBSCRIPTION_DURATION_DAYS = 30
FREE_PERSONA_LIMIT = 10
PAID_PERSONA_LIMIT = 10
FREE_USER_MONTHLY_PHOTO_LIMIT = 2 # 2 фото для бесплатных пользователей, как и запрашивалось
PREMIUM_USER_MONTHLY_PHOTO_LIMIT = 15
FREE_USER_MONTHLY_MESSAGE_LIMIT = 30 # Бесплатные пользователи: 50 сообщений в месяц

# Настройки контекста и ответов
MAX_CONTEXT_MESSAGES_SENT_TO_LLM = 200 # Сколько последних сообщений отправлять в LLM
# DEFAULT_MAX_RESPONSE_MESSAGES = 3 # Это значение теперь в db.py как default для колонки

# Messaging settings configuration automatically added per user request
MESSAGE_SENDING_SETTINGS = {
    "message_options": {
        "few": {"max_messages": 1, "message_volume": "short"},
        "more": {"max_messages": 5, "message_volume": "voluminous"},
        "default": {"max_messages": 3, "message_volume": "normal"}
    },
    "random_choice_enabled": False,
}

# Загрузка конфигурации из переменных окружения

# Дополнительные настройки и патчи
if not TELEGRAM_TOKEN:
    logger.warning("TELEGRAM_TOKEN не установлен. Используется резервный токен.")
    TELEGRAM_TOKEN = os.getenv('BACKUP_TELEGRAM_TOKEN', '')

TELEGRAPH_AUTHOR_NAME = os.getenv("TELEGRAPH_AUTHOR_NAME", "NuNuAiBot") # Имя автора для страниц Telegra.ph
TELEGRAPH_AUTHOR_URL = os.getenv("TELEGRAPH_AUTHOR_URL", "https://t.me/NuNuAiChannel") # Ссылка на автора для страниц Telegra.ph
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", None) # Токен для Telegra.ph

# --- OpenRouter Settings ---
# API key for OpenRouter (set via environment variable)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# OpenRouter Chat Completions endpoint (OpenAI compatible)
OPENROUTER_API_BASE_URL = os.getenv("OPENROUTER_API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
# Default model for paid users via OpenRouter
OPENROUTER_MODEL_NAME = os.getenv("OPENROUTER_MODEL_NAME", "google/gemini-2.5-pro")

  # --- Цены кредитной системы (базовые, можно менять без деплоя через env) ---
  # Стоимость указывается в кредитах за 1k токенов или за единицу медиа.
  # input_tokens/output_tokens — асимметричные ставки.
CREDIT_COSTS = {
    "input_tokens_per_1k": float(os.getenv("CREDIT_INPUT_PER_1K", "0.2")),
    "output_tokens_per_1k": float(os.getenv("CREDIT_OUTPUT_PER_1K", "0.6")),
    "image_per_item": float(os.getenv("CREDIT_IMAGE_PER_ITEM", "2.5")),
    "audio_per_minute": float(os.getenv("CREDIT_AUDIO_PER_MIN", "1.0")),
}

# Коэффициент для разных моделей (по умолчанию 1.0 для текущей модели Gemini)
# ВАЖНО: можно задавать множитель на модель через переменную окружения вида
# CREDIT_MULTIPLIER_<MODEL_NAME>, где дефисы и точки заменяются на подчёркивания и всё в UPPERCASE.
# Пример: для "gemini-2.5-pro" используйте CREDIT_MULTIPLIER_GEMINI_2_5_PRO
MODEL_PRICE_MULTIPLIERS = {
    GEMINI_MODEL_NAME_FOR_API: float(
        os.getenv(
            f"CREDIT_MULTIPLIER_{GEMINI_MODEL_NAME_FOR_API.replace('-', '_').replace('.', '_').upper()}",
            "1.0",
        )
    ),
    # Multiplier for OpenRouter model (name normalized for env var)
    OPENROUTER_MODEL_NAME: float(
        os.getenv(
            f"CREDIT_MULTIPLIER_{OPENROUTER_MODEL_NAME.replace('/', '_').replace('-', '_').replace('.', '_').upper()}",
            "3.0",
        )
    ),
}

# Минимальный буфер выходных токенов для предварительной проверки баланса
CREDIT_MIN_OUTPUT_TOKENS = int(os.getenv("CREDIT_MIN_OUTPUT_TOKENS", "200"))
# Минимальная тарификация по голосу в минутах
CREDIT_MIN_AUDIO_MINUTES = float(os.getenv("CREDIT_MIN_AUDIO_MINUTES", "1.0"))

# Стартовые кредиты для новых пользователей
NEW_USER_TRIAL_CREDITS = float(os.getenv("NEW_USER_TRIAL_CREDITS", "10.0"))

# Пакеты кредитов для покупки (id -> {credits, price_rub, title})
# Значения по умолчанию можно переопределять через окружение в будущем
CREDIT_PACKAGES = {
    "starter": {"credits": 50.0, "price_rub": 249.0, "title": "starter 50"},
    "basic":   {"credits": 150.0, "price_rub": 699.0, "title": "basic 150"},
    "pro":     {"credits": 400.0, "price_rub": 1499.0, "title": "pro 400"},
    "ultra":   {"credits": 1200.0, "price_rub": 3499.0, "title": "ultra 1200"},
}

# --- Low Balance Warning ---
# Порог в кредитах, при котором пользователю будет отправлено уведомление о низком балансе
LOW_BALANCE_WARNING_THRESHOLD = float(os.getenv("LOW_BALANCE_WARNING_THRESHOLD", "50.0"))
