import json
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func, BIGINT, select, update as sql_update, delete
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import declarative_base
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError, ProgrammingError
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional, Union, Tuple
import psycopg # Direct import for specific error types if needed

# Import defaults from config
from config import (
    DATABASE_URL,
    DEFAULT_MOOD_PROMPTS,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    SUBSCRIPTION_DURATION_DAYS,
    MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    ADMIN_USER_ID
)

# --- Default Templates ---
# Simplified System Prompt v6 (Запреты и требования в начале)
DEFAULT_SYSTEM_PROMPT_TEMPLATE = '''Твоя роль: {persona_name}. Описание: {persona_description}.

ВАЖНО:
-   **НИКОГДА НЕ НАЧИНАЙ С ПРИВЕТСТВИЯ**, если это не самое первое сообщение в диалоге. Сразу отвечай по делу.
-   **РАЗБИВАЙ ДЛИННЫЕ ОТВЕТЫ** на 2-4 предложения, разделяя их `\n\n`.
-   **ПИШИ ТОЛЬКО СТРОЧНЫМИ БУКВАМИ**.

Твой стиль: {communication_style}, {verbosity_level}.
Твое настроение: {mood_name} ({mood_prompt}) - учитывай, если не "Нейтрально".

Прочитай историю диалога и ответь на последнее сообщение пользователя ({username}, id: {user_id}) в чате {chat_id}.
'''

# Simplified Should Respond Prompt v3 (Возвращаем детализацию + строгость)
DEFAULT_SHOULD_RESPOND_TEMPLATE = '''Проанализируй сообщение и контекст. Должен ли чат-бот @{bot_username} (личность {persona_name}) ответить на последнее сообщение пользователя: "{last_user_message}"?
Настройка ответа в группе: {group_reply_preference}.

Правила принятия решения:
- always: Всегда отвечать "Да".
- never: Всегда отвечать "Нет".
- mentioned_only: Ответить "Да" ТОЛЬКО если в сообщении есть упоминание @{bot_username}. Иначе "Нет".
- mentioned_or_contextual: Ответить "Да" если есть упоминание @{bot_username}, ИЛИ если сообщение является прямым ответом/продолжением предыдущей реплики бота, ИЛИ если сообщение явно обращено к боту по смыслу. В остальных случаях - "Нет".

История диалога (для mentioned_or_contextual):
{context_summary}

Твой ответ ДОЛЖЕН быть ТОЛЬКО ОДНИМ СЛОВОМ: "Да" или "Нет". Без каких-либо других слов или знаков препинания.
Ответ:'''

logger = logging.getLogger(__name__)

Base = declarative_base()

# How many messages to keep in the DB history per chat instance
MAX_CONTEXT_MESSAGES_STORED = 200

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BIGINT, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # Use server_default for DB time
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()) # Use server_default

    is_subscribed = Column(Boolean, default=False, index=True)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    daily_message_count = Column(Integer, default=0, nullable=False)
    last_message_reset = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")

    @property
    def is_active_subscriber(self) -> bool:
        if self.telegram_id == ADMIN_USER_ID: return True
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID: return PAID_DAILY_MESSAGE_LIMIT
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID: return PAID_PERSONA_LIMIT
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        if self.telegram_id == ADMIN_USER_ID: return True
        try:
            if 'persona_configs' in self.__dict__ and self.persona_configs is not None:
                 count = len(self.persona_configs)
                 logger.debug(f"Using loaded persona_configs count ({count}) for User {self.id}.")
                 return count < self.persona_limit
            elif hasattr(self, '_sa_instance_state') and self._sa_instance_state.session:
                 db_session = self._sa_instance_state.session
                 count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() or 0
                 logger.debug(f"Queried persona count ({count}) for User {self.id} as relation was not loaded.")
                 return count < self.persona_limit
            else:
                 logger.warning(f"Accessing can_create_persona for detached/transient User {self.id}. Cannot reliably determine count. Assuming False.")
                 return False
        except Exception as e:
             logger.error(f"Error checking persona count for User {self.id}: {e}", exc_info=True)
             return False

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username='{self.username}')>"

class PersonaConfig(Base):
    __tablename__ = 'persona_configs'
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    communication_style = Column(Text, default="neutral", nullable=False)
    verbosity_level = Column(Text, default="medium", nullable=False)
    group_reply_preference = Column(Text, default="mentioned_or_contextual", nullable=False)
    media_reaction = Column(Text, default="text_only", nullable=False)

    mood_prompts_json = Column(Text, default=lambda: json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False, sort_keys=True))
    max_response_messages = Column(Integer, default=3, nullable=False)

    system_prompt_template = Column(Text, nullable=False, default=DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    should_respond_prompt_template = Column(Text, nullable=True, default=DEFAULT_SHOULD_RESPOND_TEMPLATE)

    owner = relationship("User", back_populates="persona_configs", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            normalized_mood_name = mood_name.lower()
            found_key = next((k for k in moods if k.lower() == normalized_mood_name), None)
            if found_key: return moods[found_key]
            neutral_key = next((k for k in moods if k.lower() == "нейтрально"), None)
            if neutral_key:
                logger.debug(f"Mood '{mood_name}' not found, using 'нейтрально' fallback.")
                return moods[neutral_key]
            logger.warning(f"Mood '{mood_name}' not found and no 'нейтрально' fallback for PersonaConfig {self.id}.")
            return ""
        except json.JSONDecodeError:
             logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
             neutral_key = next((k for k, v in DEFAULT_MOOD_PROMPTS.items() if k.lower() == "нейтрально"), None)
             return DEFAULT_MOOD_PROMPTS.get(neutral_key, "")

    def get_mood_names(self) -> List[str]:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            return list(moods.keys())
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
            return list(DEFAULT_MOOD_PROMPTS.keys())

    def set_moods(self, db_session: Session, moods: Dict[str, str]):
        validated_moods = {str(k): str(v) for k, v in moods.items()}
        new_json = json.dumps(validated_moods, ensure_ascii=False, sort_keys=True)
        if self.mood_prompts_json != new_json:
            self.mood_prompts_json = new_json
            flag_modified(self, "mood_prompts_json")
            logger.debug(f"Marked mood_prompts_json as modified for PersonaConfig {self.id}")
        else:
            logger.debug(f"Moods JSON unchanged for PersonaConfig {self.id}")

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=True)

    persona_config = relationship("PersonaConfig", back_populates="bot_instances", lazy="selectin")
    owner = relationship("User", back_populates="bot_instances", lazy="selectin")
    chat_links = relationship("ChatBotInstance", back_populates="bot_instance_ref", cascade="all, delete-orphan", lazy="selectin")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False, index=True)
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    active = Column(Boolean, default=True, index=True)
    current_mood = Column(String, default="нейтрально", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_muted = Column(Boolean, default=False, nullable=False)

    bot_instance_ref = relationship("BotInstance", back_populates="chat_links", lazy="selectin")
    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan", lazy="dynamic")

    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active}, muted={self.is_muted})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    message_order = Column(Integer, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        content_preview = (self.content[:50] + '...') if len(self.content) > 50 else self.content
        return f"<ChatContext(id={self.id}, cbi_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order}, content='{content_preview}')>"

# --- Database Setup ---
engine = None
SessionLocal = None

def initialize_database():
    global engine, SessionLocal
    # --- УПРОЩЕНИЕ ---
    # Просто берем URL как есть из переменной окружения
    db_url_str = DATABASE_URL
    if not db_url_str:
        logger.critical("DATABASE_URL environment variable is not set!")
        raise ValueError("DATABASE_URL environment variable is not set!")

    db_log_url = db_url_str.split('@')[-1] if '@' in db_url_str else db_url_str
    logger.info(f"Initializing database connection pool for: {db_log_url}")
    # Логируем URL, который БУДЕТ использован (маскируем пароль)
    try:
        # Попробуем импортировать make_url только для логирования
        from sqlalchemy.engine.url import make_url
        log_url_display = make_url(db_url_str).render_as_string(hide_password=True)
    except Exception:
        log_url_display = "Could not parse DATABASE_URL for logging."
    logger.info(f"Using DATABASE_URL directly: {log_url_display}")
    # --- КОНЕЦ УПРОЩЕНИЯ ---

    engine_args = {}
    if db_url_str.startswith("sqlite"):
        engine_args["connect_args"] = {"check_same_thread": False}
    elif db_url_str.startswith("postgres"):
        # Базовые настройки пула
        engine_args.update({
            "pool_size": 10,
            "max_overflow": 5,
            "pool_timeout": 30,
            "pool_recycle": 1800,
            "pool_pre_ping": True,
        })

    try:
        # Создаем engine с URL из переменной и базовыми engine_args
        engine = create_engine(db_url_str, **engine_args, echo=False)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("Database engine and session maker initialized.")

        logger.info("Attempting to establish initial database connection...")
        with engine.connect() as connection:
             logger.info("Database connection successful.")
             # Можно убрать проверку prepared_statement_cache_size, т.к. мы задали его в URL

    except OperationalError as e:
         err_str = str(e).lower()
         if "password authentication failed" in err_str or "wrong password" in err_str:
             logger.critical(f"FATAL: Database password authentication failed for {db_log_url}.")
             logger.critical(f"Verify the password in the DATABASE_URL variable in Railway matches the Supabase DB password.")
         elif "database" in err_str and "does not exist" in err_str:
             logger.critical(f"FATAL: Database specified in DATABASE_URL does not exist ({db_log_url}).")
         elif "connection refused" in err_str or "timed out" in err_str or "could not translate host name" in err_str:
             logger.critical(f"FATAL: Could not connect to database host {db_log_url}.")
         else:
             logger.critical(f"FATAL: Database operational error during initialization for {db_log_url}: {e}", exc_info=True)
         raise
    except ProgrammingError as e:
        logger.critical(f"FATAL: Database programming error during initialization for {db_log_url}: {e}", exc_info=True)
        raise
    except Exception as e:
         logger.critical(f"FATAL: An unexpected error occurred during database initialization for {db_log_url}: {e}", exc_info=True)
         raise

def get_db():
    if SessionLocal is None:
         logger.error("Database is not initialized. Call initialize_database() first.")
         raise RuntimeError("Database not initialized.")
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database Session Error: {e}", exc_info=True)
        try: db.rollback()
        except Exception as rb_err: logger.error(f"Error during rollback: {rb_err}")
        raise
    except Exception as e:
        logger.error(f"Non-SQLAlchemy error in 'get_db' context: {e}", exc_info=True)
        try: db.rollback()
        except Exception as rb_err: logger.error(f"Error during rollback on non-SQLAlchemy error: {rb_err}")
        raise
    finally:
        db.close()

def create_tables():
    """Creates database tables based on the defined models IF THEY DON'T EXIST."""
    if engine is None:
         logger.critical("Database engine is not initialized. Cannot create tables.")
         raise RuntimeError("Database engine not initialized.")
    logger.info("Attempting to create database tables if they don't exist...")
    try:
        Base.metadata.create_all(engine)
        logger.info("Database tables verified/created successfully.")
    except (OperationalError, psycopg.OperationalError, ProgrammingError) as op_err:
         db_log_url_on_error = str(engine.url).split('@')[-1] if '@' in str(engine.url) else str(engine.url)
         logger.critical(f"FATAL: Database error during create_tables for {db_log_url_on_error}: {op_err}", exc_info=False)
         logger.critical("Check DATABASE_URL, schema permissions, network connectivity, and DB server status.")
         raise
    except Exception as e:
        db_log_url_on_error = str(engine.url).split('@')[-1] if '@' in str(engine.url) else str(engine.url)
        logger.critical(f"FATAL: Failed to create/verify database tables for {db_log_url_on_error}: {e}", exc_info=True)
        raise

# --- User Operations ---

def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    """Gets or creates a user. Ensures admin has subscription. DOES NOT COMMIT."""
    user = None
    try:
        user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == telegram_id).first()
        if user:
            modified = False
            if user.username != username and username is not None:
                 user.username = username
                 modified = True
            if user.telegram_id == ADMIN_USER_ID and not user.is_active_subscriber:
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
                modified = True
            if modified:
                flag_modified(user, "username")
                flag_modified(user, "is_subscribed")
                flag_modified(user, "subscription_expires_at")
                logger.info(f"User {telegram_id} updated (username/admin status). Pending commit.")
        else:
            logger.info(f"Creating new user for telegram_id {telegram_id} (Username: {username})")
            user = User(telegram_id=telegram_id, username=username)
            if telegram_id == ADMIN_USER_ID:
                logger.info(f"Setting admin user {telegram_id} as subscribed indefinitely upon creation.")
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            db.add(user)
            db.flush()
            logger.info(f"New user created and flushed (Telegram ID: {telegram_id}, DB ID: {user.id}). Pending commit.")

    except SQLAlchemyError as e:
         logger.error(f"DB Error in get_or_create_user for {telegram_id}: {e}", exc_info=True)
         raise
    return user

def check_and_update_user_limits(db: Session, user: User) -> bool:
    """Checks user message limits, resets daily count if needed, increments count. DOES NOT COMMIT OR FLUSH."""
    if user.telegram_id == ADMIN_USER_ID:
        return True

    now = datetime.now(timezone.utc)
    last_reset = user.last_message_reset
    if last_reset and last_reset.tzinfo is None:
         last_reset = last_reset.replace(tzinfo=timezone.utc)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    reset_needed = (last_reset is None) or (last_reset < today_start)
    updated = False

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id} (Previous: {user.daily_message_count}, Last reset: {last_reset}).")
        user.daily_message_count = 0
        user.last_message_reset = now
        updated = True

    can_send = user.daily_message_count < user.message_limit

    if can_send:
        user.daily_message_count += 1
        logger.debug(f"User {user.telegram_id} message count incremented to {user.daily_message_count}/{user.message_limit}.")
        updated = True
    else:
         logger.info(f"User {user.telegram_id} message limit reached ({user.daily_message_count}/{user.message_limit}).")

    if updated:
        flag_modified(user, "daily_message_count")
        flag_modified(user, "last_message_reset")
        logger.debug(f"User {user.telegram_id} limits state modified. Pending commit.")

    return can_send

def activate_subscription(db: Session, user_id: int) -> bool:
    """Activates subscription for a user based on internal DB ID and commits."""
    user = None
    try:
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if user:
            logger.info(f"Activating subscription for user {user.telegram_id} (DB ID: {user_id})")
            now = datetime.now(timezone.utc)
            start_date = now
            if user.is_active_subscriber and user.subscription_expires_at:
                if user.subscription_expires_at > now:
                     start_date = user.subscription_expires_at
                else:
                     logger.warning(f"User {user.telegram_id} had expired subscription ({user.subscription_expires_at}) but was marked active. Starting new subscription from now.")

            expiry_date = start_date + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

            user.is_subscribed = True
            user.subscription_expires_at = expiry_date
            user.daily_message_count = 0
            user.last_message_reset = now
            db.commit()
            logger.info(f"Subscription activated/extended for user {user.telegram_id} until {expiry_date}")
            return True
        else:
             logger.warning(f"User with DB ID {user_id} not found for subscription activation.")
             return False
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit subscription activation for DB user {user_id}: {e}", exc_info=True)
         db.rollback()
         return False

# --- Persona Operations ---

def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Creates a new PersonaConfig with default settings."""
    try:
        default_moods = json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False, sort_keys=True)

        new_persona = PersonaConfig(
            owner_id=owner_id,
            name=name,
            description=description,
            communication_style="neutral",
            verbosity_level="medium",
            group_reply_preference="mentioned_or_contextual",
            media_reaction="text_only",
            mood_prompts_json=default_moods,
            max_response_messages=3,
            system_prompt_template=DEFAULT_SYSTEM_PROMPT_TEMPLATE,
            should_respond_prompt_template=DEFAULT_SHOULD_RESPOND_TEMPLATE
        )
        db.add(new_persona)
        db.commit()
        db.refresh(new_persona)
        logger.info(f"Successfully created persona '{new_persona.name}' (ID: {new_persona.id}) for owner_id {owner_id}")
        return new_persona
    except IntegrityError as e:
        logger.warning(f"IntegrityError creating persona '{name}' for owner {owner_id}: {e}")
        db.rollback()
        raise
    except SQLAlchemyError as e:
        logger.error(f"Database error creating persona '{name}' for owner {owner_id}: {e}", exc_info=True)
        db.rollback()
        raise
    except Exception as e:
        logger.error(f"Unexpected error creating persona '{name}' for owner {owner_id}: {e}", exc_info=True)
        db.rollback()
        raise

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Gets all personas owned by a user."""
    try:
        return db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(PersonaConfig.owner_id == owner_id).order_by(PersonaConfig.name).all()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting personas for owner {owner_id}: {e}", exc_info=True)
        return []

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    """Gets a specific persona by name (case-insensitive) and owner."""
    try:
        return db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
            PersonaConfig.owner_id == owner_id,
            func.lower(PersonaConfig.name) == name.lower()
        ).first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting persona by name '{name}' for owner {owner_id}: {e}", exc_info=True)
        return None

def get_persona_by_id_and_owner(db: Session, owner_telegram_id: int, persona_id: int) -> Optional[PersonaConfig]:
    """Gets a specific persona by its ID, ensuring ownership via owner's Telegram ID."""
    logger.debug(f"Searching for PersonaConfig id={persona_id} owned by telegram_id={owner_telegram_id}")
    try:
        persona_config = db.query(PersonaConfig)\
            .join(User, PersonaConfig.owner_id == User.id)\
            .options(selectinload(PersonaConfig.owner))\
            .filter(
                User.telegram_id == owner_telegram_id,
                PersonaConfig.id == persona_id
            ).first()
        if not persona_config:
            logger.warning(f"PersonaConfig id={persona_id} not found for owner telegram_id={owner_telegram_id}")
            return None
        return persona_config
    except SQLAlchemyError as e:
        logger.error(f"DB error getting persona by ID {persona_id} for owner {owner_telegram_id}: {e}", exc_info=True)
        return None

def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    """Deletes a PersonaConfig by its ID and owner's internal ID, and commits."""
    logger.warning(f"--- delete_persona_config: Attempting to delete PersonaConfig ID={persona_id} owned by User ID={owner_id} ---")
    try:
        logger.debug(f"Querying for PersonaConfig id={persona_id} owner_id={owner_id}...")
        # Загружаем сразу связанные bot_instances и их chat_links для эффективности
        persona = db.query(PersonaConfig).options(
            selectinload(PersonaConfig.bot_instances).selectinload(BotInstance.chat_links)
        ).filter(
            PersonaConfig.id == persona_id,
            PersonaConfig.owner_id == owner_id
        ).first() # Убираем with_for_update, т.к. будем удалять вручную частично

        if persona:
            persona_name = persona.name
            logger.info(f"Found PersonaConfig {persona_id} ('{persona_name}'). Proceeding with deletion.")

            # --- НАЧАЛО: Ручное удаление ChatContext ---
            chat_bot_instance_ids_to_clear = []
            if persona.bot_instances:
                for bot_instance in persona.bot_instances:
                    if bot_instance.chat_links:
                        chat_bot_instance_ids_to_clear.extend([link.id for link in bot_instance.chat_links])

            if chat_bot_instance_ids_to_clear:
                logger.info(f"Manually deleting ChatContext records for ChatBotInstance IDs: {chat_bot_instance_ids_to_clear}")
                try:
                    # Создаем SQL запрос на удаление контекста
                    stmt = delete(ChatContext).where(ChatContext.chat_bot_instance_id.in_(chat_bot_instance_ids_to_clear))
                    result = db.execute(stmt)
                    deleted_ctx_count = result.rowcount
                    logger.info(f"Manually deleted {deleted_ctx_count} ChatContext records.")
                    # НЕ делаем commit здесь, все будет в одном коммите в конце
                except SQLAlchemyError as ctx_del_err:
                    logger.error(f"SQLAlchemyError during manual ChatContext deletion for persona {persona_id}: {ctx_del_err}", exc_info=True)
                    logger.debug("Rolling back transaction due to ChatContext deletion error.")
                    db.rollback()
                    return False # Ошибка при удалении контекста
            else:
                logger.info(f"No related ChatContext records found to delete for persona {persona_id}.")
            # --- КОНЕЦ: Ручное удаление ChatContext ---

            # Теперь удаляем саму персону (каскад сработает для BotInstance и ChatBotInstance)
            logger.debug(f"Calling db.delete() for persona {persona_id}. Attempting commit...")
            db.delete(persona)
            db.commit()
            logger.info(f"Successfully committed deletion of PersonaConfig {persona_id} (Name: '{persona_name}')")
            return True
        else:
            logger.warning(f"PersonaConfig {persona_id} not found or not owned by User ID {owner_id} for deletion.")
            return False
    except SQLAlchemyError as e:
        logger.error(f"SQLAlchemyError during commit/delete of PersonaConfig {persona_id}: {e}", exc_info=True)
        logger.debug(f"Rolling back transaction for persona {persona_id} deletion.")
        db.rollback()
        return False
    except Exception as e:
        logger.error(f"Unexpected error during delete_persona_config for {persona_id}: {e}", exc_info=True)
        logger.debug(f"Rolling back transaction for persona {persona_id} deletion due to unexpected error.")
        db.rollback()
        return False

# --- Bot Instance Operations ---

def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
    """Creates a new BotInstance and commits."""
    logger.info(f"Creating BotInstance for persona_id {persona_config_id}, owner_id {owner_id}")
    bot_instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        name=name
    )
    try:
        db.add(bot_instance)
        db.commit()
        db.refresh(bot_instance)
        logger.info(f"Successfully created BotInstance {bot_instance.id} for persona {persona_config_id}")
        return bot_instance
    except IntegrityError as e:
         logger.warning(f"IntegrityError creating BotInstance for persona {persona_config_id}: {e}")
         db.rollback()
         raise
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit new BotInstance for persona {persona_config_id}: {e}", exc_info=True)
        db.rollback()
        raise

def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    """Gets a BotInstance by its primary key ID, loading relations."""
    try:
        return db.query(BotInstance).options(
            selectinload(BotInstance.persona_config).selectinload(PersonaConfig.owner),
            selectinload(BotInstance.owner)
        ).filter(BotInstance.id == instance_id).first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting bot instance by ID {instance_id}: {e}", exc_info=True)
        return None

# --- Chat Link Operations ---

def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: Union[str, int]) -> Optional[ChatBotInstance]:
    """Links a BotInstance to a chat. Creates or reactivates the link. Commits the change."""
    chat_link = None
    chat_id_str = str(chat_id)
    created_new = False
    session_valid = True
    try:
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id_str,
            ChatBotInstance.bot_instance_id == bot_instance_id
        ).with_for_update().first()

        if chat_link:
            needs_commit = False
            if not chat_link.active:
                 logger.info(f"[link_bot_instance] Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id_str}")
                 chat_link.active = True
                 chat_link.current_mood = "нейтрально"
                 chat_link.is_muted = False
                 needs_commit = True
                 try:
                     deleted_ctx_result = chat_link.context.delete(synchronize_session='fetch')
                     deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                     logger.debug(f"[link_bot_instance] Cleared {deleted_ctx} context messages for reactivated ChatBotInstance {chat_link.id}.")
                 except Exception as del_ctx_err:
                     logger.error(f"[link_bot_instance] Error clearing context during reactivation: {del_ctx_err}", exc_info=True)
            else:
                logger.info(f"[link_bot_instance] ChatBotInstance link for bot {bot_instance_id} in chat {chat_id_str} is already active. Clearing context on re-add request.")
                try:
                    deleted_ctx_result = chat_link.context.delete(synchronize_session='fetch')
                    deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                    logger.debug(f"[link_bot_instance] Cleared {deleted_ctx} context messages for already active ChatBotInstance {chat_link.id}.")
                    needs_commit = True
                except Exception as del_ctx_err:
                     logger.error(f"[link_bot_instance] Error clearing context for already active link: {del_ctx_err}", exc_info=True)

            if needs_commit:
                try:
                    logger.debug(f"[link_bot_instance] Committing changes for existing link ID {chat_link.id}. Active: {chat_link.active}")
                    db.commit()
                    logger.debug(f"[link_bot_instance] Commit successful for existing link ID {chat_link.id}.")
                    try:
                        db.refresh(chat_link)
                        logger.debug(f"[link_bot_instance] Refreshed existing link state ID: {chat_link.id}, Active: {chat_link.active}")
                    except SQLAlchemyError as refresh_err:
                         logger.error(f"[link_bot_instance] Refresh FAILED after commit for existing link ID {chat_link.id}: {refresh_err}", exc_info=True)
                except SQLAlchemyError as commit_err:
                     logger.error(f"[link_bot_instance] Commit FAILED for existing link ID {chat_link.id}: {commit_err}", exc_info=True)
                     db.rollback()
                     session_valid = False
                     chat_link = None
        else:
            logger.info(f"[link_bot_instance] Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id_str}")
            created_new = True
            chat_link = ChatBotInstance(
                chat_id=chat_id_str,
                bot_instance_id=bot_instance_id,
                active=True,
                current_mood="нейтрально",
                is_muted=False
            )
            try:
                 db.add(chat_link)
                 logger.debug(f"[link_bot_instance] Added new link to session. Active: {chat_link.active}. Attempting commit...")
                 db.commit()
                 logger.info(f"[link_bot_instance] Commit successful for new link. Assigned ID: {chat_link.id}. Active: {chat_link.active}")
                 try:
                     db.refresh(chat_link)
                     logger.debug(f"[link_bot_instance] Refreshed new link state ID: {chat_link.id}, Active: {chat_link.active}")
                 except SQLAlchemyError as refresh_err:
                     logger.error(f"[link_bot_instance] Refresh FAILED after commit for new link ID {chat_link.id}: {refresh_err}", exc_info=True)
            except SQLAlchemyError as commit_err:
                 logger.error(f"[link_bot_instance] Commit FAILED during new link creation: {commit_err}", exc_info=True)
                 db.rollback()
                 session_valid = False
                 chat_link = None

        if chat_link and session_valid:
             logger.debug(f"[link_bot_instance] Returning ChatBotInstance object (ID: {chat_link.id}, Active: {chat_link.active})")
        elif not session_valid:
             logger.warning("[link_bot_instance] Returning None because commit failed.")
        else:
             logger.warning("[link_bot_instance] Returning None (link not found or not created).")

        return chat_link

    except IntegrityError as e:
         logger.warning(f"IntegrityError linking bot instance {bot_instance_id} to chat {chat_id_str}: {e}")
         db.rollback()
         return None
    except SQLAlchemyError as e:
         logger.error(f"Failed linking bot instance {bot_instance_id} to chat {chat_id_str}: {e}", exc_info=True)
         db.rollback()
         return None

def unlink_bot_instance_from_chat(db: Session, chat_id: Union[str, int], bot_instance_id: int) -> bool:
    """Deactivates a ChatBotInstance link (marks active=False) and commits."""
    chat_id_str = str(chat_id)
    try:
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id_str,
            ChatBotInstance.bot_instance_id == bot_instance_id,
            ChatBotInstance.active == True
        ).with_for_update().first()

        if chat_link:
            logger.info(f"Deactivating ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id_str}")
            chat_link.active = False
            db.commit()
            return True
        else:
            logger.warning(f"No active ChatBotInstance found for bot {bot_instance_id} in chat {chat_id_str} to deactivate.")
            return False
    except SQLAlchemyError as e:
            logger.error(f"Failed to commit deactivation for ChatBotInstance bot {bot_instance_id} chat {chat_id_str}: {e}", exc_info=True)
            db.rollback()
            return False

def get_active_chat_bot_instance_with_relations(db: Session, chat_id: Union[str, int]) -> Optional[ChatBotInstance]:
    """Fetches the active ChatBotInstance for a chat, loading related objects efficiently."""
    chat_id_str = str(chat_id)
    logger.debug(f"[get_active_chat_bot_instance] Searching for instance in chat_id='{chat_id_str}'")
    instance = None
    try:
        any_instance = db.query(ChatBotInstance).filter(ChatBotInstance.chat_id == chat_id_str).first()
        if any_instance and not any_instance.active:
            logger.warning(f"[get_active_chat_bot_instance] Found an INACTIVE instance for chat_id='{chat_id_str}' (ID: {any_instance.id}). This might be the issue.")
        elif not any_instance:
             logger.warning(f"[get_active_chat_bot_instance] No instance (active or inactive) found at all for chat_id='{chat_id_str}'.")

        instance = db.query(ChatBotInstance)\
            .options(
                selectinload(ChatBotInstance.bot_instance_ref)
                .selectinload(BotInstance.persona_config)
                .selectinload(PersonaConfig.owner),
                selectinload(ChatBotInstance.bot_instance_ref)
                .selectinload(BotInstance.owner)
            )\
            .filter(ChatBotInstance.chat_id == chat_id_str, ChatBotInstance.active == True)\
            .first()

        if instance:
            logger.debug(f"[get_active_chat_bot_instance] Found ACTIVE instance (with options): ID={instance.id}, BotInstanceID={instance.bot_instance_id}, PersonaID={instance.bot_instance_ref.persona_config_id if instance.bot_instance_ref else 'N/A'}")
        else:
            pass
        return instance
    except (SQLAlchemyError, ProgrammingError) as e:
        if isinstance(e, ProgrammingError) and "does not exist" in str(e).lower():
             logger.error(f"Database schema error getting active chatbot instance for chat {chat_id_str}: {e}. Columns missing!")
        elif "operator does not exist" in str(e).lower() and ("character varying = bigint" in str(e).lower() or "bigint = character varying" in str(e).lower()):
             logger.error(f"Type mismatch error querying ChatBotInstance for chat_id '{chat_id}': {e}. Check model and DB schema for chat_id.", exc_info=False)
        else:
             logger.error(f"DB error getting active chatbot instance for chat {chat_id_str}: {e}", exc_info=True)
        return None

# --- Context Operations ---

def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    """Retrieves the last N messages for the LLM context."""
    try:
        context_records = db.query(ChatContext.role, ChatContext.content)\
                            .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                            .order_by(ChatContext.message_order.desc())\
                            .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                            .all()

        return [{"role": role, "content": content} for role, content in reversed(context_records)]
    except SQLAlchemyError as e:
        logger.error(f"DB error getting context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        return []

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    """Adds a message to the context history, performing pruning. DOES NOT COMMIT OR FLUSH."""
    max_content_length = 4000
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for CBI {chat_bot_instance_id}")
        content = content[:max_content_length - 3] + "..."

    try:
        instance_exists = db.query(ChatBotInstance.id).filter(ChatBotInstance.id == chat_bot_instance_id).scalar() is not None
        if not instance_exists:
             raise SQLAlchemyError(f"ChatBotInstance {chat_bot_instance_id} not found")

        subq = db.query(
            func.count(ChatContext.id).label('count'),
            func.max(ChatContext.message_order).label('max_order')
        ).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).subquery()

        result = db.query(subq.c.count, subq.c.max_order).first()
        current_count = result.count or 0
        max_order = result.max_order or 0

        if current_count >= MAX_CONTEXT_MESSAGES_STORED:
             limit_offset_query = db.query(ChatContext.message_order)\
                                     .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                                     .order_by(ChatContext.message_order.desc())\
                                     .limit(1)\
                                     .offset(MAX_CONTEXT_MESSAGES_STORED - 1)

             threshold_order_result = limit_offset_query.scalar_one_or_none()

             if threshold_order_result is not None:
                 threshold_order = threshold_order_result
                 delete_stmt = delete(ChatContext).where(
                     ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                     ChatContext.message_order < threshold_order
                 )
                 deleted_result = db.execute(delete_stmt)
                 deleted_count = deleted_result.rowcount
                 logger.debug(f"Pruned {deleted_count} old context messages for instance {chat_bot_instance_id} (threshold order {threshold_order}). Pending commit.")
             else:
                 logger.warning(f"Could not determine threshold order for pruning context for instance {chat_bot_instance_id}")

        new_message = ChatContext(
            chat_bot_instance_id=chat_bot_instance_id,
            message_order=max_order + 1,
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc)
        )
        db.add(new_message)
        logger.debug(f"Prepared new context message (order {max_order + 1}, role {role}) for instance {chat_bot_instance_id}. Pending commit.")

    except SQLAlchemyError as e:
        logger.error(f"DB error preparing message for context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        raise

# --- Mood Operations ---

def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    """Gets the current mood for a ChatBotInstance."""
    try:
        mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
        return mood or "нейтрально"
    except SQLAlchemyError as e:
        logger.error(f"DB error getting mood for instance {chat_bot_instance_id}: {e}", exc_info=True)
        return "нейтрально"

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    """Sets the mood for a ChatBotInstance and commits the change."""
    try:
        chat_bot = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).with_for_update().first()
        if chat_bot:
            if chat_bot.current_mood != mood:
                logger.info(f"Setting mood from '{chat_bot.current_mood}' to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
                chat_bot.current_mood = mood
                db.commit()
                logger.info(f"Successfully set mood to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
            else:
                 logger.debug(f"Mood already set to '{mood}' for ChatBotInstance {chat_bot_instance_id}. No change.")
        else:
            logger.warning(f"ChatBotInstance {chat_bot_instance_id} not found to set mood.")
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit mood change for {chat_bot_instance_id}: {e}", exc_info=True)
        db.rollback()

# --- Bulk Operations / Tasks ---

def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    """Gets all active ChatBotInstances with relations for tasks."""
    try:
        return db.query(ChatBotInstance)\
            .filter(ChatBotInstance.active == True)\
            .options(
                selectinload(ChatBotInstance.bot_instance_ref)
                .selectinload(BotInstance.persona_config)
                .selectinload(PersonaConfig.owner),
                selectinload(ChatBotInstance.bot_instance_ref)
                .selectinload(BotInstance.owner)
            ).all()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting all active instances: {e}", exc_info=True)
        return []
