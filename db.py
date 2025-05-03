import json
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func, BIGINT, select, update as sql_update
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
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, # Keep system template base
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    SUBSCRIPTION_DURATION_DAYS,
    MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    ADMIN_USER_ID
)

# --- Default Templates (Add missing one) ---
# Simplified System Prompt
DEFAULT_SYSTEM_PROMPT_TEMPLATE = '''Ты — {persona_name}. {persona_description}

ТВОЯ ГЛАВНАЯ ЗАДАЧА: Отвечай в диалоге, ПОЛНОСТЬЮ ИГНОРИРУЯ ВСЕ ПРИВЕТСТВИЯ, если диалог уже начат (смотри историю сообщений). ПРОДОЛЖАЙ БЕСЕДУ, учитывая предыдущие сообщения.

*   **Стиль:** {communication_style}, {verbosity_level}.
*   **Настроение:** {mood_name} ({mood_prompt} - используй, если настроение не "Нейтрально").
*   **GIF:** Можешь вставить ОДНУ ссылку на GIF (https://...gif).
*   **Формат:** Только *курсив* или **жирный** текст. БЕЗ списков, заголовков, блоков кода.
*   **Длина:** Старайся отвечать в 1-3 сообщениях.
'''

# Simplified Should Respond Prompt
DEFAULT_SHOULD_RESPOND_TEMPLATE = '''Должен ли бот @{bot_username} (личность {persona_name}) ответить на сообщение "{last_user_message}" в групповом чате?
Настройка ответа: {group_reply_preference}.

Ответь ТОЛЬКО "Да" или "Нет".

Правила:
- always: Да
- never: Нет
- mentioned_only: Да, если есть @{bot_username}. Иначе Нет.
- mentioned_or_contextual: Да, если есть @{bot_username} ИЛИ сообщение - ответ на реплику бота, ИЛИ явно обращено к боту. Иначе Нет.

ИСТОРИЯ (для contextual):
{context_summary}

Ответ (Да/Нет):
'''

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

    # Lazy='selectin' is generally good, but be mindful of query performance
    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")

    @property
    def is_active_subscriber(self) -> bool:
        # Admin is always considered active
        if self.telegram_id == ADMIN_USER_ID:
            return True
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_DAILY_MESSAGE_LIMIT # Use paid limit for admin
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_PERSONA_LIMIT # Use paid limit for admin
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        # Admin can always create
        if self.telegram_id == ADMIN_USER_ID:
            return True
        # Check if relationship is loaded, otherwise query (handle detached state carefully)
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
                 # Detached or transient instance - cannot reliably check count without query
                 logger.warning(f"Accessing can_create_persona for detached/transient User {self.id}. Cannot reliably determine count. Assuming False.")
                 # Safer to return False if we can't query
                 # If you need this to work for transient objects before adding to session,
                 # you might need to pass the session or handle differently.
                 return False
        except Exception as e:
             logger.error(f"Error checking persona count for User {self.id}: {e}", exc_info=True)
             return False # Be restrictive on error

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username='{self.username}')>"

class PersonaConfig(Base):
    __tablename__ = 'persona_configs'
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True) # Основное описание роли/характера

    # --- Structured behavior fields ---
    # Defaults should match the ALTER TABLE commands
    communication_style = Column(Text, default="neutral", nullable=False)
    verbosity_level = Column(Text, default="medium", nullable=False)
    group_reply_preference = Column(Text, default="mentioned_or_contextual", nullable=False)
    media_reaction = Column(Text, default="text_only", nullable=False)
    # --- End structured fields ---

    # Editable settings
    mood_prompts_json = Column(Text, default=lambda: json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False, sort_keys=True))
    max_response_messages = Column(Integer, default=3, nullable=False)

    # Base system prompt template (still potentially useful for advanced users or future features)
    system_prompt_template = Column(Text, nullable=False, default=DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    # Other templates are generated dynamically by Persona class, so columns can be removed
    # OR kept nullable if needed for backward compatibility / advanced mode
    should_respond_prompt_template = Column(Text, nullable=True)
    # spam_prompt_template = Column(Text, nullable=True)
    # photo_prompt_template = Column(Text, nullable=True)
    # voice_prompt_template = Column(Text, nullable=True)

    owner = relationship("User", back_populates="persona_configs", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            normalized_mood_name = mood_name.lower()
            # Direct case-insensitive lookup
            found_key = next((k for k in moods if k.lower() == normalized_mood_name), None)
            if found_key:
                return moods[found_key]

            # Fallback to 'нейтрально'
            neutral_key = next((k for k in moods if k.lower() == "нейтрально"), None)
            if neutral_key:
                logger.debug(f"Mood '{mood_name}' not found, using 'нейтрально' fallback.")
                return moods[neutral_key]

            logger.warning(f"Mood '{mood_name}' not found and no 'нейтрально' fallback for PersonaConfig {self.id}.")
            return "" # Return empty if no specific mood and no neutral fallback
        except json.JSONDecodeError:
             logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
             # Try returning default neutral prompt if possible
             neutral_key = next((k for k, v in DEFAULT_MOOD_PROMPTS.items() if k.lower() == "нейтрально"), None)
             return DEFAULT_MOOD_PROMPTS.get(neutral_key, "")

    def get_mood_names(self) -> List[str]:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            return list(moods.keys())
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
            return list(DEFAULT_MOOD_PROMPTS.keys()) # Return default keys on error

    def set_moods(self, db_session: Session, moods: Dict[str, str]):
        """Updates the mood prompts JSON, marking the field as modified."""
        # Ensure keys/values are strings before dumping
        validated_moods = {str(k): str(v) for k, v in moods.items()}
        new_json = json.dumps(validated_moods, ensure_ascii=False, sort_keys=True)
        if self.mood_prompts_json != new_json:
            self.mood_prompts_json = new_json
            flag_modified(self, "mood_prompts_json") # Important for JSON/mutable types
            logger.debug(f"Marked mood_prompts_json as modified for PersonaConfig {self.id}")
        else:
            logger.debug(f"Moods JSON unchanged for PersonaConfig {self.id}")

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True) # Denormalized for easier queries/ownership checks
    name = Column(String, nullable=True) # Optional name for the instance itself

    persona_config = relationship("PersonaConfig", back_populates="bot_instances", lazy="selectin")
    owner = relationship("User", back_populates="bot_instances", lazy="selectin")
    chat_links = relationship("ChatBotInstance", back_populates="bot_instance_ref", cascade="all, delete-orphan", lazy="selectin")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False, index=True) # Use String for flexibility
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    active = Column(Boolean, default=True, index=True)
    current_mood = Column(String, default="нейтрально", nullable=False) # Add nullable=False
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_muted = Column(Boolean, default=False, nullable=False) # Add nullable=False

    bot_instance_ref = relationship("BotInstance", back_populates="chat_links", lazy="selectin")
    # Use dynamic loading for context to avoid loading potentially large history unless needed
    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan", lazy="dynamic")

    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),) # Only one instance of a specific BotInstance per chat

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active}, muted={self.is_muted})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    message_order = Column(Integer, nullable=False, index=True) # To maintain order
    role = Column(String, nullable=False) # 'user' or 'assistant'
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
    if not DATABASE_URL:
        logger.critical("DATABASE_URL environment variable is not set!")
        raise ValueError("DATABASE_URL environment variable is not set!")

    db_log_url = DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL
    logger.info(f"Initializing database connection pool for: {db_log_url}")

    engine_args = {}
    db_url_str = DATABASE_URL

    if DATABASE_URL.startswith("sqlite"):
        engine_args["connect_args"] = {"check_same_thread": False}
    elif DATABASE_URL.startswith("postgres"):
        if 'sslmode' not in DATABASE_URL:
            logger.info("Adding sslmode=require to DATABASE_URL for PostgreSQL")
            from sqlalchemy.engine.url import make_url
            try:
                url = make_url(DATABASE_URL)
                if 'sslmode' not in (url.query or {}):
                    url = url.set(query=dict(url.query or {}, sslmode='require'))
                    db_url_str = str(url)
                    db_log_url_mod = db_url_str.split('@')[-1] if '@' in db_url_str else db_url_str
                    logger.info(f"Modified DATABASE_URL: {db_log_url_mod}")
                else:
                    logger.info("sslmode is already present in DATABASE_URL query parameters.")
            except Exception as url_e:
                 logger.error(f"Failed to parse or modify DATABASE_URL: {url_e}. Using original URL.")
                 db_url_str = DATABASE_URL # Fallback
        else:
             logger.info("sslmode is explicitly present in DATABASE_URL string.")

        # Recommended pool settings for production PostgreSQL
        engine_args.update({
            "pool_size": 10,
            "max_overflow": 5,
            "pool_timeout": 30,
            "pool_recycle": 1800, # Recycle connections every 30 mins
            "pool_pre_ping": True,
        })

    try:
        engine = create_engine(db_url_str, **engine_args, echo=False) # echo=False for production
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("Database engine and session maker initialized.")
        # Test connection
        logger.info("Attempting to establish initial database connection...")
        with engine.connect() as connection:
             logger.info("Initial database connection successful.")
    except OperationalError as e:
         err_str = str(e).lower()
         if "password authentication failed" in err_str:
             logger.critical(f"FATAL: Database password authentication failed for {db_log_url}.")
         elif "database" in err_str and "does not exist" in err_str:
             logger.critical(f"FATAL: Database specified in DATABASE_URL does not exist ({db_log_url}).")
         elif "connection refused" in err_str or "timed out" in err_str or "could not translate host name" in err_str:
             logger.critical(f"FATAL: Could not connect to database host {db_log_url}.")
         else:
             logger.critical(f"FATAL: Database operational error during initialization for {db_log_url}: {e}", exc_info=True)
         raise # Re-raise the critical error
    except ProgrammingError as e: # Catch errors like wrong password type etc.
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
        # Try to load personas eagerly if the user might exist
        user = db.query(User).options(selectinload(User.persona_configs)).filter(User.telegram_id == telegram_id).first()
        if user:
            # User exists, check if username or admin status needs update
            modified = False
            if user.username != username and username is not None:
                 user.username = username
                 modified = True
            # Ensure admin always has active subscription status in DB
            if user.telegram_id == ADMIN_USER_ID and not user.is_active_subscriber:
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc) # Effectively infinite
                modified = True
            if modified:
                flag_modified(user, "username") # Mark potentially modified fields
                flag_modified(user, "is_subscribed")
                flag_modified(user, "subscription_expires_at")
                logger.info(f"User {telegram_id} updated (username/admin status). Pending commit.")
                # Don't flush here, let the caller manage transaction boundaries
        else:
            logger.info(f"Creating new user for telegram_id {telegram_id} (Username: {username})")
            user = User(telegram_id=telegram_id, username=username)
            if telegram_id == ADMIN_USER_ID:
                logger.info(f"Setting admin user {telegram_id} as subscribed indefinitely upon creation.")
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            db.add(user)
            db.flush() # Assigns an ID to the user object without committing
            logger.info(f"New user created and flushed (Telegram ID: {telegram_id}, DB ID: {user.id}). Pending commit.")
            # After flush, the persona_configs relation is empty, no need to load it yet

    except SQLAlchemyError as e:
         logger.error(f"DB Error in get_or_create_user for {telegram_id}: {e}", exc_info=True)
         raise
    return user

def check_and_update_user_limits(db: Session, user: User) -> bool:
    """Checks user message limits, resets daily count if needed, increments count. DOES NOT COMMIT OR FLUSH."""
    if user.telegram_id == ADMIN_USER_ID:
        return True # Admin has no limits

    now = datetime.now(timezone.utc)
    # Ensure last_message_reset is timezone-aware if loaded from DB
    last_reset = user.last_message_reset
    if last_reset and last_reset.tzinfo is None:
         # Assume UTC if timezone info is missing (might happen with older data or SQLite)
         last_reset = last_reset.replace(tzinfo=timezone.utc)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    reset_needed = (last_reset is None) or (last_reset < today_start)
    updated = False

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id} (Previous: {user.daily_message_count}, Last reset: {last_reset}).")
        user.daily_message_count = 0
        user.last_message_reset = now # Use current time for reset
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
                # Extend from expiry date only if it's in the future
                if user.subscription_expires_at > now:
                     start_date = user.subscription_expires_at
                else: # If expiry is in the past but somehow still marked active, start from now
                     logger.warning(f"User {user.telegram_id} had expired subscription ({user.subscription_expires_at}) but was marked active. Starting new subscription from now.")

            expiry_date = start_date + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

            user.is_subscribed = True
            user.subscription_expires_at = expiry_date
            user.daily_message_count = 0 # Reset daily count on activation/renewal
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
        # Prepare default mood prompts if necessary
        default_moods = json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False, sort_keys=True)

        new_persona = PersonaConfig(
            owner_id=owner_id,
            name=name,
            description=description,
            # Provide defaults for all potentially NOT NULL fields
            communication_style="neutral",
            verbosity_level="medium",
            group_reply_preference="mentioned_or_contextual",
            media_reaction="text_only",
            mood_prompts_json=default_moods,
            max_response_messages=3,
            system_prompt_template=DEFAULT_SYSTEM_PROMPT_TEMPLATE,
            should_respond_prompt_template=DEFAULT_SHOULD_RESPOND_TEMPLATE # <-- Added default
        )
        db.add(new_persona)
        db.commit()
        db.refresh(new_persona)
        logger.info(f"Successfully created persona '{new_persona.name}' (ID: {new_persona.id}) for owner_id {owner_id}")
        return new_persona
    except IntegrityError as e:
        logger.warning(f"IntegrityError creating persona '{name}' for owner {owner_id}: {e}")
        db.rollback()
        raise # Re-raise for the handler to catch
    except SQLAlchemyError as e:
        logger.error(f"Database error creating persona '{name}' for owner {owner_id}: {e}", exc_info=True)
        db.rollback()
        raise # Re-raise for the handler
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
    logger.warning(f"Attempting to delete PersonaConfig {persona_id} owned by User ID {owner_id}")
    try:
        persona = db.query(PersonaConfig).filter(
            PersonaConfig.id == persona_id,
            PersonaConfig.owner_id == owner_id
        ).with_for_update().first()

        if persona:
            persona_name = persona.name
            logger.info(f"Deleting PersonaConfig {persona_id} ('{persona_name}')...")
            db.delete(persona) # Cascades deletion
            db.commit()
            logger.info(f"Successfully deleted PersonaConfig {persona_id} (Name: '{persona_name}')")
            return True
        else:
            logger.warning(f"PersonaConfig {persona_id} not found or not owned by User ID {owner_id} for deletion.")
            # If not found, maybe it was already deleted - consider success?
            # Let's return False for clarity that *this operation* didn't delete it.
            return False
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit deletion of PersonaConfig {persona_id}: {e}", exc_info=True)
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
    try:
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id_str,
            ChatBotInstance.bot_instance_id == bot_instance_id
        ).with_for_update().first()

        if chat_link:
            needs_commit = False
            if not chat_link.active:
                 logger.info(f"Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id_str}")
                 chat_link.active = True
                 chat_link.current_mood = "нейтрально"
                 chat_link.is_muted = False
                 needs_commit = True
                 # Clear context on reactivation
                 deleted_ctx_result = chat_link.context.delete(synchronize_session='fetch')
                 deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                 logger.debug(f"Cleared {deleted_ctx} context messages for reactivated ChatBotInstance {chat_link.id}.")
            else:
                logger.info(f"ChatBotInstance link for bot {bot_instance_id} in chat {chat_id_str} is already active. Clearing context on re-add request.")
                deleted_ctx_result = chat_link.context.delete(synchronize_session='fetch')
                deleted_ctx = deleted_ctx_result if isinstance(deleted_ctx_result, int) else 0
                logger.debug(f"Cleared {deleted_ctx} context messages for already active ChatBotInstance {chat_link.id}.")
                needs_commit = True

            if needs_commit: db.commit()
        else:
            logger.info(f"Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id_str}")
            chat_link = ChatBotInstance(
                chat_id=chat_id_str,
                bot_instance_id=bot_instance_id,
                active=True,
                current_mood="нейтрально",
                is_muted=False
            )
            db.add(chat_link)
            db.commit() # Commit the new link

        if chat_link:
            db.refresh(chat_link)
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
            # Consider context clearing here if desired
            # chat_link.context.delete(synchronize_session='fetch')
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
    logger.debug(f"[get_active_chat_bot_instance] Searching for active instance in chat_id='{chat_id_str}'") # Log input
    try:
        instance = db.query(ChatBotInstance)\
            .options(
                # Efficiently load nested relationships needed for Persona
                selectinload(ChatBotInstance.bot_instance_ref) # -> BotInstance
                .selectinload(BotInstance.persona_config)      # -> PersonaConfig
                .selectinload(PersonaConfig.owner),            # -> User (owner of persona)
                # Also load owner directly from BotInstance if needed elsewhere
                selectinload(ChatBotInstance.bot_instance_ref)
                .selectinload(BotInstance.owner)               # -> User (owner of instance)
            )\
            .filter(ChatBotInstance.chat_id == chat_id_str, ChatBotInstance.active == True)\
            .first()
        
        # --- NEW Log --- 
        if instance:
            logger.debug(f"[get_active_chat_bot_instance] Found active instance: ID={instance.id}, BotInstanceID={instance.bot_instance_id}, PersonaID={instance.bot_instance_ref.persona_config_id if instance.bot_instance_ref else 'N/A'}")
        else:
            logger.warning(f"[get_active_chat_bot_instance] No active instance found for chat_id='{chat_id_str}' using filter (active=True). Query returned None.")
        # --- End NEW Log ---
        return instance
    except (SQLAlchemyError, ProgrammingError) as e: # Catch ProgrammingError if schema is wrong
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
        chat_instance = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).first()
        if not chat_instance:
            logger.warning(f"ChatBotInstance {chat_bot_instance_id} not found in get_context_for_chat_bot.")
            return []

        context_records = chat_instance.context\
                            .order_by(ChatContext.message_order.desc())\
                            .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                            .all()
        return [{"role": c.role, "content": c.content} for c in reversed(context_records)] # Chronological order
    except SQLAlchemyError as e:
        logger.error(f"DB error getting context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        return []

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    """Adds a message to the context history, performing pruning. DOES NOT COMMIT OR FLUSH."""
    max_content_length = 4000 # Limit per message stored
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for CBI {chat_bot_instance_id}")
        content = content[:max_content_length - 3] + "..."

    try:
        chat_instance = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).first()
        if not chat_instance:
             raise SQLAlchemyError(f"ChatBotInstance {chat_bot_instance_id} not found")

        # --- Context Pruning ---
        current_count = chat_instance.context.count()
        if current_count >= MAX_CONTEXT_MESSAGES_STORED:
             # Find the message_order of the oldest message to keep
             message_to_keep_from = chat_instance.context \
                                      .order_by(ChatContext.message_order.desc()) \
                                      .limit(1) \
                                      .offset(MAX_CONTEXT_MESSAGES_STORED - 1) \
                                      .first()
             if message_to_keep_from:
                 threshold_order = message_to_keep_from.message_order
                 # Delete messages older than the threshold
                 deleted_count_result = chat_instance.context \
                                   .filter(ChatContext.message_order < threshold_order) \
                                   .delete(synchronize_session='fetch') # Use 'fetch' for safety with dynamic relations
                 deleted_count = deleted_count_result if isinstance(deleted_count_result, int) else 0
                 logger.debug(f"Pruned {deleted_count} old context messages for instance {chat_bot_instance_id} (threshold order {threshold_order}). Pending commit.")
             else:
                 logger.warning(f"Could not determine threshold order for pruning context for instance {chat_bot_instance_id}")

        # --- Add New Message ---
        max_order_record = chat_instance.context.order_by(ChatContext.message_order.desc()).first()
        max_order = max_order_record.message_order if max_order_record else 0

        new_message = ChatContext(
            chat_bot_instance_id=chat_bot_instance_id,
            message_order=max_order + 1,
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc) # Explicitly set timestamp here
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
        return mood or "нейтрально" # Default if None or empty
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
