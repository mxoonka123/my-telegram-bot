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

# OpenRouter Settings
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL_NAME = "google/gemini-2.0-flash-001"

# Gemini Settings (using OpenRouter key by default)
GEMINI_API_KEY = OPENROUTER_API_KEY

if not OPENROUTER_API_KEY:
    logger.warning("WARNING: Переменная окружения OPENROUTER_API_KEY не установлена!")
else:
    logger.info("INFO: OPENROUTER_API_KEY успешно загружена.")
# Для отладки можно добавить print для нового ключа, если нужно
# print(f"DEBUG config.py: OPENROUTER_API_KEY is defined, length: {len(OPENROUTER_API_KEY) if OPENROUTER_API_KEY else 0}")
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

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "") # ID магазина
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "") # Секретный ключ

# URL вашего приложения на Railway
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL_BASE", "")

# LANGDOCK_BASE_URL = os.getenv("LANGDOCK_BASE_URL", "https://api.langdock.com/anthropic/eu/")
# LANGDOCK_MODEL = os.getenv("LANGDOCK_MODEL", "claude-3-5-sonnet-20240620")

# Параметры подписки и лимиты
SUBSCRIPTION_PRICE_RUB = 699.00
SUBSCRIPTION_CURRENCY = "RUB"
SUBSCRIPTION_DURATION_DAYS = 30
FREE_PERSONA_LIMIT = 1
PAID_PERSONA_LIMIT = 10
FREE_USER_MONTHLY_MESSAGE_LIMIT = 30 # Бесплатные пользователи: 50 сообщений в месяц

# Настройки контекста и ответов
MAX_CONTEXT_MESSAGES_SENT_TO_LLM = 30 # Сколько последних сообщений отправлять в LLM
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
