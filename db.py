import json
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func, BIGINT
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload, selectinload # <<< ИЗМЕНЕНО: добавлен selectinload
from sqlalchemy.orm.attributes import flag_modified # <<< ДОБАВЛЕН ИМПОРТ
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional, Union, Tuple
import psycopg # Import for specific exception type

from config import (
    DATABASE_URL,
    DEFAULT_MOOD_PROMPTS,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE,
    DEFAULT_SPAM_PROMPT_TEMPLATE, DEFAULT_PHOTO_PROMPT_TEMPLATE, DEFAULT_VOICE_PROMPT_TEMPLATE,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    SUBSCRIPTION_DURATION_DAYS,
    MAX_CONTEXT_MESSAGES_SENT_TO_LLM,
    ADMIN_USER_ID
)


logger = logging.getLogger(__name__)

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BIGINT, unique=True, nullable=False)
    username = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    is_subscribed = Column(Boolean, default=False)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)
    daily_message_count = Column(Integer, default=0)
    last_message_reset = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # <<< ИЗМЕНЕНО: lazy="selectin" для persona_configs - загружает одним доп. запросом
    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan")

    @property
    def is_active_subscriber(self) -> bool:
        if self.telegram_id == ADMIN_USER_ID:
            return True
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_DAILY_MESSAGE_LIMIT
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_PERSONA_LIMIT
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        if self.telegram_id == ADMIN_USER_ID:
            return True
        count = 0
        try:
            # Проверяем, загружена ли уже коллекция (для оптимизации с lazy='selectin')
            # 'persona_configs' in self.__dict__ проверяет, есть ли атрибут в экземпляре
            # hasattr(self, '_sa_instance_state') проверяет, привязан ли объект к сессии
            # state.persistent проверяет, что объект не новый/не удаленный
            state = self._sa_instance_state
            if state.persistent and 'persona_configs' in self.__dict__ and self.persona_configs is not None:
                 # Если коллекция загружена (lazy='selectin' должен был это сделать), используем ее
                 count = len(self.persona_configs)
                 # logger.debug(f"Using loaded persona_configs count ({count}) for User {self.id}.")
            elif state.session:
                 # Если объект привязан к сессии, но коллекция почему-то не загружена, делаем запрос
                 # Это не должно происходить с lazy='selectin', но на всякий случай
                 db_session = state.session
                 count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() or 0
                 logger.debug(f"Queried persona count ({count}) for User {self.id} directly as relation was not loaded (unexpected with selectin).")
            else:
                 # Если объект отсоединен от сессии (detached), мы не можем безопасно получить данные
                 logger.warning(f"Accessing can_create_persona for potentially detached User {self.id} (TG ID: {self.telegram_id}). Cannot reliably determine count.")
                 # В этом случае лучше вернуть False или поднять ошибку, т.к. данные могут быть неактуальны
                 # Вернем False для безопасности
                 return False

        except Exception as e:
             logger.error(f"Error accessing persona_configs for User {self.id} (TG ID: {self.telegram_id}) in can_create_persona: {e}", exc_info=True)
             # Попытка запасного варианта с прямым запросом, если есть сессия
             try:
                 if hasattr(self, '_sa_instance_state') and self._sa_instance_state.session:
                     db_session = self._sa_instance_state.session
                     count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() or 0
                     logger.info(f"Queried persona count ({count}) for User {self.id} as fallback in exception block.")
                 else:
                     logger.error(f"Cannot get session for detached User {self.id} in exception block.")
                     return False
             except Exception as db_e:
                 logger.error(f"Error querying persona count fallback for User {self.id} in exception block: {db_e}", exc_info=True)
                 return False

        return count < self.persona_limit


    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username='{self.username}')>"

class PersonaConfig(Base):
    __tablename__ = 'persona_configs'
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    system_prompt_template = Column(Text, nullable=False, default=DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    mood_prompts_json = Column(Text, default=json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False))
    should_respond_prompt_template = Column(Text, nullable=False, default=DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE)
    spam_prompt_template = Column(Text, nullable=True, default=DEFAULT_SPAM_PROMPT_TEMPLATE)
    photo_prompt_template = Column(Text, nullable=True, default=DEFAULT_PHOTO_PROMPT_TEMPLATE)
    voice_prompt_template = Column(Text, nullable=True, default=DEFAULT_VOICE_PROMPT_TEMPLATE)
    max_response_messages = Column(Integer, default=3, nullable=False)

    # lazy='selectin' for owner helps load it efficiently when needed (like in premium checks)
    # back_populates должен совпадать с именем relationship в User
    owner = relationship("User", back_populates="persona_configs", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            for key, value in moods.items():
                if key.lower() == mood_name.lower():
                    return value
            neutral_key = next((k for k in moods if k.lower() == "нейтрально"), None)
            if neutral_key: return moods[neutral_key]
            return ""
        except json.JSONDecodeError:
             logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
             return ""

    def get_mood_names(self) -> List[str]:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            return list(moods.keys())
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in mood_prompts_json for PersonaConfig {self.id}")
            return []

    def set_moods(self, db_session: Session, moods: Dict[str, str]):
        validated_moods = {str(k): str(v) for k, v in moods.items()}
        self.mood_prompts_json = json.dumps(validated_moods, ensure_ascii=False)
        flag_modified(self, "mood_prompts_json") # <<< Убедимся, что изменение JSON отслеживается

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=True)

    persona_config = relationship("PersonaConfig", back_populates="bot_instances")
    # lazy='selectin' для owner
    owner = relationship("User", back_populates="bot_instances", lazy="selectin")
    # <<< ИЗМЕНЕНО: back_populates для chat_links >>>
    chat_links = relationship("ChatBotInstance", back_populates="bot_instance_ref", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False) # Telegram chat IDs can be very large negative numbers
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False)
    active = Column(Boolean, default=True)
    current_mood = Column(String, default="нейтрально")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    is_muted = Column(Boolean, default=False, nullable=False)

    # <<< ИЗМЕНЕНО: Добавлен back_populates, selectinload >>>
    bot_instance_ref = relationship("BotInstance", back_populates="chat_links", lazy="selectin")
    # lazy='selectin' для context может быть избыточным, если контекст большой, оставим lazy='dynamic' или 'select'
    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan", lazy="select")

    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active}, muted={self.is_muted})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False)
    message_order = Column(Integer, nullable=False, index=True) # Index for faster ordering/deletion
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        content_preview = (self.content[:50] + '...') if len(self.content) > 50 else self.content
        return f"<ChatContext(id={self.id}, cbi_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order}, content='{content_preview}')>"

# Максимальное количество сообщений, хранимых в базе данных для одного ChatBotInstance
MAX_CONTEXT_MESSAGES_STORED = 200 # Можно увеличить, если нужно хранить больше истории

engine = None
SessionLocal = None

def initialize_database():
    global engine, SessionLocal
    if not DATABASE_URL:
        logger.critical("DATABASE_URL environment variable is not set!")
        raise ValueError("DATABASE_URL environment variable is not set!")
    if DATABASE_URL.startswith("postgres"):
        logger.info(f"Initializing PostgreSQL database connection pool for: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    else:
         logger.info(f"Initializing database connection pool for: {DATABASE_URL}")

    engine_args = {}
    db_url_str = DATABASE_URL # <<< ИЗМЕНЕНО: Используем оригинальный URL по умолчанию

    if DATABASE_URL.startswith("sqlite"):
        engine_args["connect_args"] = {"check_same_thread": False}
    elif DATABASE_URL.startswith("postgres"):
         # Ensure SSL is used by default with Supabase/cloud providers if not specified
         if 'sslmode' not in DATABASE_URL:
             logger.info("Adding sslmode=require to DATABASE_URL for PostgreSQL")
             from sqlalchemy.engine.url import make_url
             try:
                 url = make_url(DATABASE_URL)
                 # Add sslmode=require if not present in query parameters
                 if 'sslmode' not in (url.query or {}):
                     url = url.set(query=dict(url.query or {}, sslmode='require'))
                     db_url_str = str(url)
                     logger.info(f"Modified DATABASE_URL: {db_url_str.split('@')[1] if '@' in db_url_str else db_url_str}") # Log without credentials
                 else:
                     logger.info(f"sslmode is already present in DATABASE_URL query parameters.")
             except Exception as url_e:
                  logger.error(f"Failed to parse or modify DATABASE_URL: {url_e}. Using original URL.")
                  db_url_str = DATABASE_URL # Use original on error
         else:
             logger.info(f"sslmode is explicitly present in DATABASE_URL string.")

         engine_args.update({
             "pool_size": 10,
             "max_overflow": 20,
             "pool_timeout": 30,
             "pool_pre_ping": True, # Recommended for cloud DBs
         })

    try:
        # Create engine with potentially modified URL and arguments
        engine = create_engine(db_url_str, **engine_args)

        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("Database engine and session maker initialized.")

        # Test connection immediately
        logger.info("Attempting to establish initial database connection...")
        with engine.connect() as connection:
             logger.info("Initial database connection successful.")

    except OperationalError as e:
         # Check for specific Supabase/Postgres connection issues if possible
         err_str = str(e).lower()
         if "password authentication failed" in err_str:
             logger.critical(f"FATAL: Database password authentication failed. Check credentials in DATABASE_URL.")
         elif "database" in err_str and "does not exist" in err_str:
             logger.critical(f"FATAL: Database specified in DATABASE_URL does not exist.")
         elif "connection refused" in err_str or "timed out" in err_str or "could not translate host name" in err_str:
             logger.critical(f"FATAL: Could not connect to database host. Check hostname/address, port, network access (firewalls), and if DB server is running.")
         else:
             logger.critical(f"FATAL: Database operational error during initialization: {e}", exc_info=True)
         logger.critical(f"Used DB URL (potentially modified): {db_url_str.split('@')[1] if '@' in db_url_str else db_url_str}")
         logger.critical("Please check your DATABASE_URL and network connectivity.")
         raise
    except Exception as e:
         logger.critical(f"FATAL: An unexpected error occurred during database initialization: {e}", exc_info=True)
         raise


def get_db(): # <<< ИЗМЕНЕНО: Возвращает генератор сессии >>>
    if SessionLocal is None:
         logger.error("Database is not initialized. Call initialize_database() first.")
         raise RuntimeError("Database not initialized.")
    db = SessionLocal()
    try:
        yield db
        # Commit is handled explicitly in the handlers now
        # db.commit() # REMOVED automatic commit
    except SQLAlchemyError as e:
        logger.error(f"Database Session Error: {e}", exc_info=True)
        try:
            db.rollback() # Rollback on any SQLAlchemy error within the 'with' block
            logger.info("Database transaction rolled back due to error.")
        except Exception as rb_err:
             logger.error(f"Error during rollback: {rb_err}")
        raise # Re-raise the original exception after rollback attempt
    except Exception as e: # Catch other potential exceptions within the 'with' block
        logger.error(f"Non-SQLAlchemy error in 'get_db' context: {e}", exc_info=True)
        try:
            db.rollback()
            logger.info("Database transaction rolled back due to non-SQLAlchemy error.")
        except Exception as rb_err:
            logger.error(f"Error during rollback on non-SQLAlchemy error: {rb_err}")
        raise # Re-raise the original exception
    finally:
        db.close() # Always close the session


def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    user = None
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        needs_commit = False # <<< ИЗМЕНЕНО: Флаг для отслеживания изменений
        if not user:
            logger.info(f"Creating new user for telegram_id {telegram_id} (Username: {username})")
            user = User(telegram_id=telegram_id, username=username)
            if telegram_id == ADMIN_USER_ID:
                logger.info(f"Setting admin user {telegram_id} as subscribed indefinitely.")
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            db.add(user)
            db.flush() # Flush to get the user ID if needed immediately (e.g., for relations)
            # db.refresh(user) # Refresh after commit in handler if needed
            logger.info(f"New user created and flushed (Telegram ID: {telegram_id})")
            needs_commit = True # Commit is needed for new user
        else: # User exists
            if user.telegram_id == ADMIN_USER_ID and not user.is_active_subscriber:
                logger.info(f"Ensuring admin user {telegram_id} is subscribed.")
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
                needs_commit = True

            if user.username != username and username is not None:
                 logger.info(f"Updating username for user {telegram_id} from '{user.username}' to '{username}'")
                 user.username = username
                 needs_commit = True

        # <<< ИЗМЕНЕНО: Commit только если были изменения >>>
        # if needs_commit:
        #     db.commit() # REMOVED - Commit handled by calling function context manager
        #     logger.debug(f"Committed changes for user {telegram_id} in get_or_create_user")

    except SQLAlchemyError as e:
         logger.error(f"DB Error in get_or_create_user for {telegram_id}: {e}", exc_info=True)
         # Rollback will be handled by get_db context manager
         raise # Re-raise to signal failure
    return user # Return the user object (possibly modified)


# <<< ИЗМЕНЕНО: Убран db.commit() >>>
def check_and_update_user_limits(db: Session, user: User) -> bool:
    """
    Checks user message limits, resets daily count if needed, increments count.
    DOES NOT COMMIT changes. The caller must commit.
    Returns True if the user can send a message, False otherwise.
    """
    if user.telegram_id == ADMIN_USER_ID:
        return True # Admin always has limits

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Use the timestamp for comparison
    reset_needed = (user.last_message_reset is None) or (user.last_message_reset < today_start)

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id} (Previous: {user.daily_message_count}, Last reset: {user.last_message_reset})")
        user.daily_message_count = 0
        user.last_message_reset = now
        # Mark the user object as dirty, but don't commit here
        # SQLAlchemy usually tracks attribute changes automatically.

    can_send = user.daily_message_count < user.message_limit

    if can_send:
        user.daily_message_count += 1
        # Mark the user object as dirty, but don't commit here
        logger.debug(f"User {user.telegram_id} message count incremented to {user.daily_message_count}/{user.message_limit}")

    # REMOVED db.commit() HERE - Commit will happen in the handler's context manager

    if not can_send:
         logger.info(f"User {user.telegram_id} message limit reached ({user.daily_message_count}/{user.message_limit}).")

    return can_send


def activate_subscription(db: Session, user_id: int) -> bool:
    """Activates subscription for a user based on internal DB ID and commits."""
    user = None
    try:
        # Use with_for_update to lock the row during update
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if user:
            logger.info(f"Activating subscription for user {user.telegram_id} (DB ID: {user_id})")
            now = datetime.now(timezone.utc)
            # Extend subscription if already active
            start_date = max(now, user.subscription_expires_at) if user.is_active_subscriber and user.subscription_expires_at else now
            expiry_date = start_date + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

            user.is_subscribed = True
            user.subscription_expires_at = expiry_date
            user.daily_message_count = 0 # Reset count on subscription activation/renewal
            user.last_message_reset = now
            db.commit() # Commit subscription activation immediately
            logger.info(f"Subscription activated/extended for user {user.telegram_id} until {expiry_date}")
            return True
        else:
             logger.warning(f"User with DB ID {user_id} not found for subscription activation.")
             return False
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit subscription activation for DB user {user_id}: {e}", exc_info=True)
         db.rollback() # Rollback this specific activation attempt
         return False


def get_active_chat_bot_instance_with_relations(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    """Fetches the active ChatBotInstance for a chat, loading related objects."""
    try:
        # <<< ИЗМЕНЕНО: Использование selectinload для более контролируемой загрузки связей >>>
        return db.query(ChatBotInstance)\
            .options(
                selectinload(ChatBotInstance.bot_instance_ref) # Загружаем BotInstance
                .selectinload(BotInstance.persona_config)      # Загружаем PersonaConfig из BotInstance
                .selectinload(PersonaConfig.owner),            # Загружаем Owner из PersonaConfig
                selectinload(ChatBotInstance.bot_instance_ref) # Повторная загрузка BotInstance (может быть избыточна, но для ясности)
                .selectinload(BotInstance.owner)               # Загружаем Owner напрямую из BotInstance
            )\
            .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
            .first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting active chatbot instance for chat {chat_id}: {e}", exc_info=True)
        return None


def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    """Retrieves the last N messages for the LLM context."""
    try:
        context_records = db.query(ChatContext)\
                            .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                            .order_by(ChatContext.message_order.desc())\
                            .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                            .all()
        # Reverse the list so the oldest message is first
        return [{"role": c.role, "content": c.content} for c in reversed(context_records)]
    except SQLAlchemyError as e:
        logger.error(f"DB error getting context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        # DO NOT rollback here, as this function might be called mid-transaction
        return [] # Return empty list on error


# <<< ИЗМЕНЕНО: Убраны db.flush() >>>
def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    """
    Adds a message to the context history for a ChatBotInstance.
    Handles pruning of old messages. DOES NOT COMMIT OR FLUSH.
    Raises SQLAlchemyError on database issues.
    """
    max_content_length = 4000 # Limit content length to avoid potential DB issues
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for chat_bot_instance {chat_bot_instance_id}")
        content = content[:max_content_length - 3] + "..."

    try:
        # NOTE: Querying count/max might trigger autoflush if session has autoflush=True,
        # but our SessionLocal is configured with autoflush=False.
        current_count = db.query(func.count(ChatContext.id)).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).scalar()

        # Pruning logic
        if current_count >= MAX_CONTEXT_MESSAGES_STORED:
             # Find the message_order of the oldest message we want to KEEP
             oldest_to_keep_order = db.query(ChatContext.message_order)\
                                     .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                                     .order_by(ChatContext.message_order.desc())\
                                     .limit(1)\
                                     .offset(MAX_CONTEXT_MESSAGES_STORED - 1)\
                                     .scalar() # Get the single value

             if oldest_to_keep_order is not None:
                 # Delete messages older than the one we want to keep
                 deleted_count = db.query(ChatContext)\
                                   .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                                           ChatContext.message_order < oldest_to_keep_order)\
                                   .delete(synchronize_session=False) # 'fetch' might be safer but slower if needed
                 logger.debug(f"Pruned {deleted_count} old context messages for instance {chat_bot_instance_id} (keeping ~{MAX_CONTEXT_MESSAGES_STORED}).")
                 # REMOVED db.flush() - Changes will be flushed on commit

        # Find the current max order to determine the next order number
        max_order = db.query(func.max(ChatContext.message_order))\
                      .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                      .scalar() or 0 # Default to 0 if no messages exist

        # Create the new message object and add it to the session
        new_message = ChatContext(
            chat_bot_instance_id=chat_bot_instance_id,
            message_order=max_order + 1,
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc)
        )
        db.add(new_message) # Stage the new message for insertion
        # REMOVED db.flush() - Let the final commit handle it

    except SQLAlchemyError as e:
        logger.error(f"DB error preparing message for context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        # No rollback needed here, the main handler's context manager will do it.
        raise # Re-raise so the caller knows it failed and the transaction is rolled back


def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    """Gets the current mood for a ChatBotInstance."""
    try:
        # Query only the specific column for efficiency
        mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
        return mood if mood else "нейтрально" # Default if None/empty
    except SQLAlchemyError as e:
        logger.error(f"DB error getting mood for instance {chat_bot_instance_id}: {e}", exc_info=True)
        # Do not rollback here
        return "нейтрально" # Return default on error


def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    """Sets the mood for a ChatBotInstance and commits the change."""
    try:
        chat_bot = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).first()
        if chat_bot:
            if chat_bot.current_mood != mood:
                logger.info(f"Setting mood from '{chat_bot.current_mood}' to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
                chat_bot.current_mood = mood
                db.commit() # Commit mood change immediately
                logger.info(f"Successfully set mood to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
            else:
                 logger.debug(f"Mood already set to '{mood}' for ChatBotInstance {chat_bot_instance_id}. No change.")
        else:
            logger.warning(f"ChatBotInstance {chat_bot_instance_id} not found to set mood.")
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit mood change for {chat_bot_instance_id}: {e}", exc_info=True)
        db.rollback() # Rollback this specific mood change attempt


def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    """Gets all active ChatBotInstances with relations for tasks like spamming."""
    try:
        # <<< ИЗМЕНЕНО: Использование selectinload >>>
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

def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Creates a new PersonaConfig and commits."""
    logger.info(f"Attempting to create persona '{name}' for owner_id {owner_id}")
    if description is None:
        description = f"ai бот по имени {name}." # Default description

    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
        # Other fields use defaults from the model definition
    )
    try:
        db.add(persona)
        db.commit() # Commit the new persona
        db.refresh(persona) # Refresh to get the generated ID and defaults
        logger.info(f"Successfully created PersonaConfig '{name}' with ID {persona.id} for owner_id {owner_id}")
        return persona
    except IntegrityError as e: # Catch constraint violation (e.g., duplicate name)
        logger.warning(f"IntegrityError creating persona '{name}' for owner {owner_id}: {e}", exc_info=False)
        db.rollback()
        raise # Re-raise to be handled by the caller
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit new persona '{name}' for owner {owner_id}: {e}", exc_info=True)
         db.rollback()
         raise # Re-raise


def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Gets all personas owned by a user."""
    try:
        # <<< ИЗМЕНЕНО: Явно загружаем owner для каждой персоны >>>
        return db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(PersonaConfig.owner_id == owner_id).order_by(PersonaConfig.name).all()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting personas for owner {owner_id}: {e}", exc_info=True)
        return []

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    """Gets a specific persona by name (case-insensitive) and owner."""
    try:
        # <<< ИЗМЕНЕНО: Явно загружаем owner >>>
        return db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
            PersonaConfig.owner_id == owner_id,
            func.lower(PersonaConfig.name) == name.lower() # Case-insensitive comparison
        ).first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting persona by name '{name}' for owner {owner_id}: {e}", exc_info=True)
        return None


def get_persona_by_id_and_owner(db: Session, owner_telegram_id: int, persona_id: int) -> Optional[PersonaConfig]:
    """Gets a specific persona by its ID, ensuring ownership via owner's Telegram ID."""
    logger.debug(f"Searching for PersonaConfig id={persona_id} owned by telegram_id={owner_telegram_id}")
    try:
        # Find the user first by telegram_id
        user = db.query(User).filter(User.telegram_id == owner_telegram_id).first()
        if not user:
            logger.warning(f"User with telegram_id {owner_telegram_id} not found when searching for persona {persona_id}")
            return None
        logger.debug(f"Found User with id={user.id} for telegram_id={owner_telegram_id}")
        # Now find the persona by its ID and the user's internal ID
        # <<< ИЗМЕНЕНО: Используем selectinload для owner >>>
        persona_config = db.query(PersonaConfig).options(selectinload(PersonaConfig.owner)).filter(
            PersonaConfig.owner_id == user.id, # Check ownership using internal ID
            PersonaConfig.id == persona_id
        ).first()
        if not persona_config:
            logger.warning(f"PersonaConfig id={persona_id} not found for owner User id={user.id} (telegram_id={owner_telegram_id})")
            return None
        logger.debug(f"Successfully found PersonaConfig id={persona_id} for owner User id={user.id}")
        return persona_config
    except SQLAlchemyError as e:
        logger.error(f"DB error getting persona by ID {persona_id} for owner {owner_telegram_id}: {e}", exc_info=True)
        return None

def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    """Deletes a PersonaConfig by its ID and owner's internal ID, and commits."""
    logger.warning(f"Attempting to delete PersonaConfig {persona_id} owned by User ID {owner_id}")
    try:
        persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == owner_id).first()
        if persona:
            db.delete(persona) # Mark for deletion
            db.commit() # Commit the deletion
            logger.info(f"Successfully deleted PersonaConfig {persona_id}")
            return True
        else:
            logger.warning(f"PersonaConfig {persona_id} not found or not owned by User ID {owner_id} for deletion.")
            return False
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit deletion of PersonaConfig {persona_id}: {e}", exc_info=True)
        db.rollback() # Rollback the failed deletion attempt
        return False


def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
    """Creates a new BotInstance and commits."""
    logger.info(f"Creating BotInstance for persona_id {persona_config_id}, owner_id {owner_id}")
    bot_instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        name=name # Optional name for the instance
    )
    try:
        db.add(bot_instance)
        db.commit() # Commit the new instance
        db.refresh(bot_instance) # Get the generated ID
        logger.info(f"Successfully created BotInstance {bot_instance.id} for persona {persona_config_id}")
        return bot_instance
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit new BotInstance for persona {persona_config_id}: {e}", exc_info=True)
        db.rollback()
        raise # Re-raise


def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    """Gets a BotInstance by its primary key ID."""
    try:
        # .get() is efficient for primary key lookups, but doesn't support options like selectinload easily.
        # Using query().get() or filter() allows options.
        # <<< ИЗМЕНЕНО: Явно загружаем связи >>>
        return db.query(BotInstance).options(
            selectinload(BotInstance.persona_config).selectinload(PersonaConfig.owner),
            selectinload(BotInstance.owner)
        ).filter(BotInstance.id == instance_id).first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting bot instance by ID {instance_id}: {e}", exc_info=True)
        return None


def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> Optional[ChatBotInstance]:
    """
    Links a BotInstance to a chat, creating or reactivating the link. Commits the change.
    Also clears context upon activation/linking.
    """
    chat_link = None
    try:
        # Check if a link (active or inactive) already exists
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id,
            ChatBotInstance.bot_instance_id == bot_instance_id
        ).first()

        if chat_link:
            # If link exists but is inactive, reactivate it
            if not chat_link.active:
                 logger.info(f"Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
                 chat_link.active = True
                 chat_link.current_mood = "нейтрально" # Reset mood on reactivation
                 chat_link.is_muted = False # Unmute on reactivation
                 # Clear context when reactivating
                 deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                 logger.debug(f"Cleared {deleted_ctx} context messages for reactivated ChatBotInstance {chat_link.id}.")
                 # db.flush() # Flush reactivation changes - Not needed, commit handles it
            else:
                # Link already exists and is active, nothing to do structurally,
                # but maybe log this state?
                logger.info(f"ChatBotInstance link for bot {bot_instance_id} in chat {chat_id} is already active.")
                # Clear context if re-added via command
                deleted_ctx = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_link.id).delete(synchronize_session='fetch')
                logger.debug(f"Cleared {deleted_ctx} context messages for already active ChatBotInstance {chat_link.id} upon re-adding command.")

        else:
            # Link doesn't exist, create a new one
            logger.info(f"Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id}")
            chat_link = ChatBotInstance(
                chat_id=chat_id,
                bot_instance_id=bot_instance_id,
                active=True,
                current_mood="нейтрально",
                is_muted=False
            )
            db.add(chat_link)
            # Context will be empty initially, no need to clear.
            # db.flush() # Flush creation - Not needed, commit handles it

        # Commit changes (reactivation or creation, and context deletion if reactivated/re-added)
        db.commit()
        if chat_link:
            db.refresh(chat_link) # Ensure the object has the latest state (like ID if newly created)
        return chat_link

    except SQLAlchemyError as e:
         logger.error(f"Failed linking bot instance {bot_instance_id} to chat {chat_id}: {e}", exc_info=True)
         db.rollback() # Rollback the failed link attempt
         return None


def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
    """Deactivates a ChatBotInstance link and commits."""
    try:
        # Find the specific active link to deactivate
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id,
            ChatBotInstance.bot_instance_id == bot_instance_id,
            ChatBotInstance.active == True # Only deactivate active links
        ).first()
        if chat_link:
            logger.info(f"Deactivating ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
            chat_link.active = False
            db.commit() # Commit the deactivation
            return True
        else:
            # Link is already inactive or doesn't exist for this specific bot in this chat
            logger.warning(f"No active ChatBotInstance found for bot {bot_instance_id} in chat {chat_id} to deactivate.")
            return False # Indicate no change was made
    except SQLAlchemyError as e:
            logger.error(f"Failed to commit deactivation for ChatBotInstance bot {bot_instance_id} chat {chat_id}: {e}", exc_info=True)
            db.rollback() # Rollback failed deactivation
            return False


def create_tables():
    """Creates database tables based on the defined models."""
    if engine is None:
         logger.critical("Database engine is not initialized. Cannot create tables.")
         raise RuntimeError("Database engine not initialized.")
    logger.info("Attempting to create database tables if they don't exist...")
    try:
        Base.metadata.create_all(engine)
        logger.info("Database tables verified/created successfully.")
    except (OperationalError, psycopg.OperationalError) as op_err:
         logger.critical(f"FATAL: Database connection error during create_tables: {op_err}", exc_info=False) # Keep info concise
         logger.critical("Check DATABASE_URL, network connectivity, and DB server status.")
         raise # Re-raise to stop the application
    except Exception as e:
        logger.critical(f"FATAL: Failed to create/verify database tables: {e}", exc_info=True)
        raise
