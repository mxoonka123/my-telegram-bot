# 🚀 Рекомендации по оптимизации производительности Telegram-бота

## 📊 Анализ текущего состояния

После глубокого анализа кода выявлены следующие основные проблемы производительности:

### 🔴 Критические проблемы (требуют немедленного решения)

1. **База данных - главное узкое место**
   - Избыточное использование `selectinload()` и `joinedload()` на каждый запрос
   - Множественные блокировки строк через `with_for_update()` 
   - Отсутствие кеширования часто запрашиваемых данных
   - N+1 проблемы при загрузке связанных таблиц

2. **Обработка навигации по кнопкам**
   - Каждое нажатие кнопки = 3-5 запросов к БД
   - Нет кеширования данных для InlineKeyboard меню
   - Слишком много проверок доступа на каждое действие

3. **Парсинг ответов от LLM**
   - Избыточная логика с множественными regex fallback
   - 3-4 попытки парсинга JSON на каждый ответ
   - Нет кеша для системных промптов

## 💊 Решения для оптимизации

### 1. Оптимизация работы с БД

#### Добавить недостающие индексы:
```python
# В db.py добавить в классы:

class ChatBotInstance(Base):
    __table_args__ = (
        UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),
        Index('ix_chat_bot_active', 'chat_id', 'active'),  # NEW
        Index('ix_chat_bot_instance', 'bot_instance_id', 'active'),  # NEW
    )

class ChatContext(Base):
    __table_args__ = (
        Index('ix_context_chat_order', 'chat_bot_instance_id', 'message_order'),  # NEW
    )
```

#### Убрать избыточные selectinload:
```python
# БЫЛО:
user = db.query(User).options(
    selectinload(User.persona_configs).selectinload(DBPersonaConfig.bot_instance)
).filter(User.id == user_id).one()

# СТАЛО:
user = db.query(User).filter(User.id == user_id).one()
# Загружать связи только когда действительно нужны
if need_personas:
    personas = db.query(DBPersonaConfig).filter(
        DBPersonaConfig.owner_id == user.id
    ).all()
```

#### Добавить кеширование через Redis:
```python
import redis
import json
from functools import wraps

redis_client = redis.Redis(
    host='localhost', 
    port=6379, 
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1
)

def cache_result(ttl=60):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return json.loads(cached)
            except:
                pass
            
            result = await func(*args, **kwargs)
            
            try:
                redis_client.setex(
                    cache_key, 
                    ttl, 
                    json.dumps(result, default=str)
                )
            except:
                pass
                
            return result
        return wrapper
    return decorator

# Использование:
@cache_result(ttl=300)  # 5 минут
async def get_user_personas(user_id: int):
    # запрос к БД
    pass
```

### 2. Оптимизация обработки кнопок

#### Кешировать InlineKeyboard меню:
```python
# handlers.py
MENU_CACHE = {}

def get_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    cache_key = f"menu_{user_id}"
    
    if cache_key in MENU_CACHE:
        return MENU_CACHE[cache_key]
    
    keyboard = [
        [
            InlineKeyboardButton("профиль", callback_data="show_profile"),
            InlineKeyboardButton("мои личности", callback_data="show_mypersonas")
        ],
        [InlineKeyboardButton("помощь", callback_data="show_help")]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    MENU_CACHE[cache_key] = markup
    
    # Очистка кеша через 5 минут
    asyncio.create_task(clear_cache_after(cache_key, 300))
    
    return markup
```

#### Упростить callback обработчики:
```python
# БЫЛО: множественные запросы к БД
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    with get_db() as db:
        user = get_user(db, user_id)  # Запрос 1
        personas = get_personas(db, user.id)  # Запрос 2
        bot_instance = get_bot_instance(db, ...)  # Запрос 3
        # и т.д.

# СТАЛО: один оптимизированный запрос
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Кешированные данные из context
    user_data = context.user_data.get('cached_user')
    if not user_data or time.time() - user_data.get('ts', 0) > 300:
        with get_db() as db:
            user_data = load_user_data_optimized(db, user_id)
            context.user_data['cached_user'] = {
                'data': user_data,
                'ts': time.time()
            }
```

### 3. Оптимизация парсинга LLM ответов

#### Упростить парсинг JSON:
```python
def parse_llm_response(text: str) -> List[str]:
    """Упрощенный парсинг без множественных fallback"""
    # Убрать markdown обертку если есть
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json\n"):
            text = text[5:]
    
    try:
        # Пробуем как JSON
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "response" in parsed:
            resp = parsed["response"]
            if isinstance(resp, list):
                return [str(x) for x in resp if x]
            return [str(resp)] if resp else []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except:
        # Простой fallback - разбить по строкам
        return [line.strip() for line in text.split('\n') if line.strip()]
    
    return [text]  # Крайний случай
```

### 4. Асинхронные улучшения

#### Заменить threading.RLock на asyncio.Lock:
```python
# БЫЛО:
bot_swap_lock = threading.RLock()

# СТАЛО:
bot_swap_lock = asyncio.Lock()

async def process_update():
    async with bot_swap_lock:
        # обработка
        pass
```

#### Использовать connection pool для внешних API:
```python
import httpx

# Глобальный клиент с пулом соединений
http_client = httpx.AsyncClient(
    limits=httpx.Limits(
        max_keepalive_connections=10,
        max_connections=20,
        keepalive_expiry=30
    ),
    timeout=httpx.Timeout(30.0)
)

async def send_to_openrouter(...):
    # Использовать глобальный клиент
    response = await http_client.post(...)
```

### 5. Очистка кода

#### Удалить неиспользуемый код:
- Убрать DEPRECATED поля из БД (daily_message_count, last_message_reset)
- Удалить закомментированные функции подписок
- Убрать пустые `except: pass` блоки где возможно

#### Уменьшить логирование:
```python
# Использовать уровни логирования правильно
logger.debug(...)  # Детальная отладка - выключено в проде
logger.info(...)   # Важные события
logger.warning(...) # Предупреждения
logger.error(...)  # Только реальные ошибки
```

### 6. Конфигурация для продакшена

#### Настройки БД в config.py:
```python
# Увеличить пул соединений
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))  # было 5
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "30"))  # было 10

# Добавить настройки кеша
CACHE_TTL_USER = 300  # 5 минут
CACHE_TTL_PERSONA = 600  # 10 минут
CACHE_TTL_MENU = 1800  # 30 минут
```

#### Настройки PTB:
```python
# main.py
builder.concurrent_updates(True)  # Уже есть
builder.connection_pool_size(100)  # Увеличить с 50
builder.pool_timeout(30.0)  # Увеличить с 20
```

## 📈 Ожидаемые результаты

После внедрения этих оптимизаций:

1. **Скорость отклика кнопок**: с 2-3 сек до 100-300 мс
2. **Нагрузка на БД**: снижение на 70-80%
3. **Использование памяти**: снижение на 30-40%
4. **Общая производительность**: увеличение в 3-5 раз

## 🔄 План внедрения

1. **Фаза 1 (1-2 дня)**: Добавить индексы и убрать лишние selectinload
2. **Фаза 2 (2-3 дня)**: Внедрить Redis кеширование
3. **Фаза 3 (1 день)**: Оптимизировать парсинг LLM
4. **Фаза 4 (1 день)**: Очистить код от мусора
5. **Фаза 5 (тестирование)**: Нагрузочное тестирование и финальная настройка

## ⚠️ Важные замечания

- Все изменения БД требуют создания миграций через Alembic
- Тестировать изменения сначала на dev окружении
- Мониторить производительность после каждого этапа
- Делать бэкапы БД перед критическими изменениями

---

*Документ подготовлен после глубокого анализа кода. Рекомендации расставлены по приоритету влияния на производительность.*
