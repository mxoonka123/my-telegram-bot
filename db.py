# db.py

import json
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func, BIGINT
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional, Union, Tuple


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
    telegram_id = Column(BIGINT, unique=True, nullable=False) # Изменено на BIGINT
    username = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    is_subscribed = Column(Boolean, default=False)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)
    daily_message_count = Column(Integer, default=0)
    last_message_reset = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan", lazy="selectin")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan")

    @property
    def is_active_subscriber(self) -> bool:
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        # Админ всегда имеет максимальный лимит (хотя проверка check_and_update_user_limits его обходит)
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_DAILY_MESSAGE_LIMIT
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        if self.telegram_id == ADMIN_USER_ID:
             return PAID_PERSONA_LIMIT # Или другое большое число, если нужно больше
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        if self.telegram_id == ADMIN_USER_ID:
            return True

        # Ensure persona_configs relationship is loaded or available
        # Handle potential DetachedInstanceError if accessed improperly
        try:
            # Check if the relationship is loaded. This might still fail if detached.
            if 'persona_configs' not in self.__dict__:
                 logger.warning(f"Accessing can_create_persona for User {self.id} before persona_configs loaded. Count may be inaccurate.")
                 # Attempt to load or return False? For safety, return False or query DB.
                 # Let's return False to prevent creation if state is unclear.
                 return False
            count = len(self.persona_configs) if self.persona_configs is not None else 0
        except Exception as e:
             logger.error(f"Error accessing persona_configs for User {self.id} in can_create_persona: {e}", exc_info=True)
             # Query the count directly from DB as a fallback
             try:
                 from sqlalchemy.orm.session import Session
                 db_session = Session.object_session(self)
                 if db_session:
                     count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() or 0
                     logger.info(f"Queried persona count ({count}) for User {self.id} as fallback.")
                 else:
                     logger.error(f"Cannot get session for detached User {self.id} to query persona count.")
                     return False # Cannot determine count
             except Exception as db_e:
                 logger.error(f"Error querying persona count fallback for User {self.id}: {db_e}", exc_info=True)
                 return False # Cannot determine count

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
    mood_prompts_json = Column(Text, default=json.dumps(DEFAULT_MOOD_PROMPTS, ensure_ascii=False)) # ensure_ascii=False
    should_respond_prompt_template = Column(Text, nullable=False, default=DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE)
    spam_prompt_template = Column(Text, nullable=True, default=DEFAULT_SPAM_PROMPT_TEMPLATE)
    photo_prompt_template = Column(Text, nullable=True, default=DEFAULT_PHOTO_PROMPT_TEMPLATE)
    voice_prompt_template = Column(Text, nullable=True, default=DEFAULT_VOICE_PROMPT_TEMPLATE)
    max_response_messages = Column(Integer, default=3, nullable=False)

    owner = relationship("User", back_populates="persona_configs")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json or '{}')
            # Case-insensitive matching
            for key, value in moods.items():
                if key.lower() == mood_name.lower():
                    return value
            # Fallback to neutral if exact mood not found
            neutral_key = next((k for k in moods if k.lower() == "нейтрально"), None)
            if neutral_key: return moods[neutral_key]
            return "" # Return empty if even neutral isn't found
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
        # Ensure keys are strings and values are strings before dumping
        validated_moods = {str(k): str(v) for k, v in moods.items()}
        self.mood_prompts_json = json.dumps(validated_moods, ensure_ascii=False)
        flag_modified(self, "mood_prompts_json") # Mark as modified for SQLAlchemy

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=True) # Optional name for the instance itself

    persona_config = relationship("PersonaConfig", back_populates="bot_instances")
    owner = relationship("User", back_populates="bot_instances")
    chat_links = relationship("ChatBotInstance", backref="bot_instance_ref", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False) # Telegram Chat ID (can be large string)
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False)
    active = Column(Boolean, default=True)
    current_mood = Column(String, default="нейтрально")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationship to ChatContext messages
    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan")

    # Unique constraint: Only one active bot instance per chat at a time?
    # If allowing multiple bots, remove 'active' from constraint or handle logic elsewhere.
    # Current setup allows multiple inactive links, but only one active one (enforced by add_bot_to_chat logic)
    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False)
    message_order = Column(Integer, nullable=False, index=True) # Index for ordering
    role = Column(String, nullable=False) # 'user', 'assistant', 'system' (if needed)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        # Limit content length in repr for cleaner logs
        content_preview = (self.content[:50] + '...') if len(self.content) > 50 else self.content
        return f"<ChatContext(id={self.id}, cbi_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order}, content='{content_preview}')>"

# --- Constants ---
MAX_CONTEXT_MESSAGES_STORED = 200 # How many messages to keep in DB per chat instance

# --- Database Setup ---
if not DATABASE_URL:
    logger.critical("DATABASE_URL environment variable is not set!")
    # Fallback for local development, but should not happen in production
    DATABASE_URL = "sqlite:///./bot_data_fallback.db"
    logger.warning(f"Using fallback database: {DATABASE_URL}")

# Adjust connect_args for SQLite if needed (e.g., check_same_thread)
engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False} # Important for multithreaded access (like Flask + Bot)

engine = create_engine(DATABASE_URL, **engine_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# --- Context Manager for DB Session ---
def get_db() -> Session:
    """Provides a transactional scope around a series of operations."""
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database Session Error: {e}", exc_info=True)
        db.rollback() # Rollback on error
        raise # Re-raise the exception for handlers to potentially catch
    finally:
        db.close() # Always close the session

# --- User Management ---
def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    """Gets an existing user or creates a new one."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        logger.info(f"Creating new user for telegram_id {telegram_id} (Username: {username})")
        user = User(telegram_id=telegram_id, username=username)
        # Grant subscription to admin immediately
        if telegram_id == ADMIN_USER_ID:
            logger.info(f"Setting admin user {telegram_id} as subscribed indefinitely.")
            user.is_subscribed = True
            # Set a very far future date or handle None in is_active_subscriber if preferred
            user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
        db.add(user)
        try:
            db.commit()
            db.refresh(user) # Ensure all fields (like id) are populated
            logger.info(f"New user created with DB ID {user.id}")
        except SQLAlchemyError as e:
             logger.error(f"Failed to commit new user {telegram_id}: {e}", exc_info=True)
             db.rollback()
             raise # Re-raise to indicate failure
    elif user.telegram_id == ADMIN_USER_ID and not user.is_active_subscriber:
        # Ensure admin is always subscribed if found but not marked
        logger.info(f"Ensuring admin user {telegram_id} is subscribed.")
        user.is_subscribed = True
        user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
        try:
            db.commit()
            db.refresh(user)
        except SQLAlchemyError as e:
            logger.error(f"Failed to update admin subscription status for {telegram_id}: {e}", exc_info=True)
            db.rollback()
            # Don't raise here, it's a correction attempt, not critical failure

    # Update username if it has changed or was initially None
    if user.username != username and username is not None:
         logger.info(f"Updating username for user {telegram_id} from '{user.username}' to '{username}'")
         user.username = username
         try:
             db.commit()
             db.refresh(user)
         except SQLAlchemyError as e:
             logger.error(f"Failed to update username for {telegram_id}: {e}", exc_info=True)
             db.rollback()

    return user

# --- Limits and Subscription ---
def check_and_update_user_limits(db: Session, user: User) -> bool:
    """
    Checks if the user can send a message based on daily limits.
    Resets limit if a new day has started. Increments count if allowed.
    Commits changes (reset or increment). Returns True if message allowed, False otherwise.
    """
    if user.telegram_id == ADMIN_USER_ID:
        logger.debug(f"Admin user {user.telegram_id} bypasses message limit check.")
        return True # Admin always allowed

    now = datetime.now(timezone.utc)
    today = now.date()

    # Check if reset is needed
    reset_needed = (not user.last_message_reset) or (user.last_message_reset.date() < today)

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id} (Previous: {user.daily_message_count}, Last reset: {user.last_message_reset})")
        user.daily_message_count = 0
        user.last_message_reset = now

    # Check if limit is reached
    can_send = user.daily_message_count < user.message_limit

    if can_send:
        user.daily_message_count += 1
        logger.debug(f"User {user.telegram_id} message count incremented to {user.daily_message_count}/{user.message_limit}")
    else:
        logger.info(f"User {user.telegram_id} message limit reached ({user.daily_message_count}/{user.message_limit}).")

    # Commit changes (reset and/or increment)
    try:
        db.commit()
        # Refresh user object if reset happened to get updated values
        if reset_needed:
            db.refresh(user)
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit user limit update for {user.telegram_id}: {e}", exc_info=True)
         db.rollback()
         # If commit fails, we probably shouldn't allow the message
         return False

    return can_send

def activate_subscription(db: Session, user_id: int) -> bool:
    """Activates subscription for a user based on their internal DB ID."""
    user = db.query(User).filter(User.id == user_id).first() # Use filter().first() for safety
    if user:
        logger.info(f"Activating subscription for user {user.telegram_id} (DB ID: {user_id})")
        now = datetime.now(timezone.utc)
        # Calculate expiry date, handle existing subscription extension if needed
        start_date = max(now, user.subscription_expires_at) if user.is_active_subscriber and user.subscription_expires_at else now
        expiry_date = start_date + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

        user.is_subscribed = True
        user.subscription_expires_at = expiry_date
        # Optionally reset daily count upon new subscription? Or keep it? Resetting for now.
        user.daily_message_count = 0
        user.last_message_reset = now
        try:
            db.commit()
            logger.info(f"Subscription activated/extended for user {user.telegram_id} until {expiry_date}")
            return True
        except SQLAlchemyError as e:
             logger.error(f"Failed to commit subscription activation for {user.telegram_id}: {e}", exc_info=True)
             db.rollback()
             return False
    else:
         logger.warning(f"User with DB ID {user_id} not found for subscription activation.")
         return False

# --- Chat Instance and Context ---
def get_active_chat_bot_instance_with_relations(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    """
    Gets the active ChatBotInstance for a given chat_id,
    eagerly loading related BotInstance, PersonaConfig, and Owner User.
    """
    return db.query(ChatBotInstance)\
        .options(
            joinedload(ChatBotInstance.bot_instance_ref) # Load BotInstance
            .joinedload(BotInstance.persona_config),    # Then load PersonaConfig from BotInstance
            joinedload(ChatBotInstance.bot_instance_ref) # Load BotInstance again (or usecontains_eager if possible)
            .joinedload(BotInstance.owner)               # Then load Owner from BotInstance
        )\
        .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
        .first()

def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    """Gets the last N messages for the LLM context, ordered chronologically."""
    context_records = db.query(ChatContext)\
                        .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                        .order_by(ChatContext.message_order.desc())\
                        .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                        .all()
    # Reverse the list to get chronological order (oldest first) for the LLM
    return [{"role": c.role, "content": c.content} for c in reversed(context_records)]

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    """Adds a message to the chat context, handling truncation and cleanup."""
    max_content_length = 4000 # Limit content length to avoid DB issues
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for chat_bot_instance {chat_bot_instance_id}")
        content = content[:max_content_length - 3] + "..." # Truncate with ellipsis

    # --- Context Pruning ---
    # Get current count efficiently
    current_count = db.query(func.count(ChatContext.id)).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).scalar()

    # If count exceeds limit, delete the oldest message(s)
    if current_count >= MAX_CONTEXT_MESSAGES_STORED:
         # Find the message order number to delete up to
         message_to_keep_threshold = db.query(ChatContext.message_order)\
                                     .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                                     .order_by(ChatContext.message_order.desc())\
                                     .limit(1)\
                                     .offset(MAX_CONTEXT_MESSAGES_STORED - 1)\
                                     .scalar() # Get the order number of the (N-1)th message

         if message_to_keep_threshold is not None:
             deleted_count = db.query(ChatContext)\
                               .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                                       ChatContext.message_order < message_to_keep_threshold)\
                               .delete(synchronize_session=False) # Use False for performance
             logger.debug(f"Pruned {deleted_count} old context messages for instance {chat_bot_instance_id} (keeping {MAX_CONTEXT_MESSAGES_STORED}).")
             # No commit here, part of the larger transaction

    # --- Add New Message ---
    # Get the next message order number
    max_order = db.query(func.max(ChatContext.message_order))\
                  .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                  .scalar() or 0

    new_message = ChatContext(
        chat_bot_instance_id=chat_bot_instance_id,
        message_order=max_order + 1,
        role=role,
        content=content,
        timestamp=datetime.now(timezone.utc)
    )
    db.add(new_message)
    # Let the calling function handle the commit

def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    """Gets the current mood for a specific ChatBotInstance."""
    # Use scalar() to get the value directly or None if not found
    mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
    return mood if mood else "нейтрально" # Default to neutral

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    """Sets the mood for a ChatBotInstance. Commits the change."""
    # Use with_for_update to lock the row if high concurrency is expected (optional)
    chat_bot = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).first()
    if chat_bot:
        if chat_bot.current_mood != mood:
            logger.info(f"Setting mood from '{chat_bot.current_mood}' to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
            chat_bot.current_mood = mood
            try:
                db.commit() # Commit this specific change
                logger.info(f"Successfully set mood to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
            except SQLAlchemyError as e:
                logger.error(f"Failed to commit mood change for {chat_bot_instance_id}: {e}", exc_info=True)
                db.rollback()
        else:
             logger.debug(f"Mood already set to '{mood}' for ChatBotInstance {chat_bot_instance_id}. No change.")
    else:
        logger.warning(f"ChatBotInstance {chat_bot_instance_id} not found to set mood.")

# --- Bulk Operations (Example for Tasks) ---
def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    """Gets all active ChatBotInstances with relations needed for tasks."""
    # Eager load necessary relations for spam task
    return db.query(ChatBotInstance)\
        .filter(ChatBotInstance.active == True)\
        .options(
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.persona_config),
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.owner)
        ).all()

# --- Persona Config Management ---
def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Creates a new PersonaConfig. Commits the change."""
    logger.info(f"Attempting to create persona '{name}' for owner_id {owner_id}")
    # Use default description if none provided
    if description is None:
        description = f"ai бот по имени {name}."

    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
        # Other fields will use defaults from the model definition
    )
    db.add(persona)
    try:
        db.commit()
        db.refresh(persona) # Get the generated ID
        logger.info(f"Successfully created PersonaConfig '{name}' with ID {persona.id} for owner_id {owner_id}")
        return persona
    except IntegrityError as e:
        # Handle unique constraint violation (_owner_persona_name_uc)
        logger.warning(f"IntegrityError creating persona '{name}' for owner {owner_id}: {e}", exc_info=True)
        db.rollback()
        raise # Re-raise IntegrityError for the handler to catch specifically
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit new persona '{name}' for owner {owner_id}: {e}", exc_info=True)
         db.rollback()
         raise # Re-raise general SQLAlchemyError

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Gets all personas owned by a user (by internal user ID)."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).order_by(PersonaConfig.name).all()

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    """Gets a specific persona by name (case-insensitive) and owner ID."""
    # Use func.lower for case-insensitive comparison
    return db.query(PersonaConfig).filter(
        PersonaConfig.owner_id == owner_id,
        func.lower(PersonaConfig.name) == name.lower()
    ).first()

# --- !!! THIS IS THE CORRECTED FUNCTION !!! ---
def get_persona_by_id_and_owner(db: Session, owner_telegram_id: int, persona_id: int) -> Optional[PersonaConfig]:
    """
    Находит PersonaConfig по её ID и Telegram ID владельца.

    Args:
        db: Сессия SQLAlchemy.
        owner_telegram_id: Telegram ID пользователя (из update.effective_user.id).
        persona_id: ID искомой PersonaConfig.

    Returns:
        Найденный объект PersonaConfig или None.
    """
    logger.debug(f"Searching for PersonaConfig id={persona_id} owned by telegram_id={owner_telegram_id}")

    # 1. Найти пользователя (User) по его Telegram ID (owner_telegram_id).
    user = db.query(User).filter(User.telegram_id == owner_telegram_id).first()

    # Если пользователь с таким Telegram ID не найден в базе, вернуть None.
    if not user:
        logger.warning(f"User with telegram_id {owner_telegram_id} not found when searching for persona {persona_id}")
        return None
    logger.debug(f"Found User with id={user.id} for telegram_id={owner_telegram_id}")

    # 2. Найти PersonaConfig по её ID (persona_id) и ID найденного пользователя (user.id).
    #    Связь идет через PersonaConfig.owner_id == User.id
    persona_config = db.query(PersonaConfig).filter(
        PersonaConfig.owner_id == user.id, # <-- ИСПОЛЬЗУЕМ user.id (ВНУТРЕННИЙ ID)
        PersonaConfig.id == persona_id     # <-- ID самой личности
    ).first()

    # Если личность с таким ID не найдена для этого пользователя, вернуть None.
    if not persona_config:
        logger.warning(f"PersonaConfig id={persona_id} not found for owner User id={user.id} (telegram_id={owner_telegram_id})")
        return None

    # Если всё нашлось, вернуть найденный объект PersonaConfig.
    logger.debug(f"Successfully found PersonaConfig id={persona_id} for owner User id={user.id}")
    return persona_config
# --- !!! END OF CORRECTED FUNCTION !!! ---


def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    """Deletes a PersonaConfig by its ID and owner's internal ID. Commits."""
    logger.warning(f"Attempting to delete PersonaConfig {persona_id} owned by User ID {owner_id}")
    # Find the persona first using the internal owner_id
    persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == owner_id).first()
    if persona:
        db.delete(persona)
        try:
            db.commit()
            logger.info(f"Successfully deleted PersonaConfig {persona_id}")
            return True
        except SQLAlchemyError as e:
            logger.error(f"Failed to commit deletion of PersonaConfig {persona_id}: {e}", exc_info=True)
            db.rollback()
            return False
    else:
        logger.warning(f"PersonaConfig {persona_id} not found or not owned by User ID {owner_id} for deletion.")
        return False

# --- Bot Instance Management ---
def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
    """Creates a new BotInstance. Commits the change."""
    logger.info(f"Creating BotInstance for persona_id {persona_config_id}, owner_id {owner_id}")
    bot_instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        name=name # Optional instance name
    )
    db.add(bot_instance)
    try:
        db.commit()
        db.refresh(bot_instance)
        logger.info(f"Successfully created BotInstance {bot_instance.id} for persona {persona_config_id}")
        return bot_instance
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit new BotInstance for persona {persona_config_id}: {e}", exc_info=True)
        db.rollback()
        raise

def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    """Gets a BotInstance by its primary key ID."""
    return db.query(BotInstance).get(instance_id) # .get() is efficient for PK lookup

def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> Optional[ChatBotInstance]:
    """
    Links a BotInstance to a chat. If link exists but inactive, reactivates it.
    If link doesn't exist, creates it. Does NOT commit automatically.
    """
    # Check if a link (active or inactive) already exists
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id
    ).first()

    if chat_link:
        if not chat_link.active:
             logger.info(f"Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
             chat_link.active = True
             chat_link.current_mood = "нейтрально" # Reset mood on reactivation
             # No commit here
        else:
            logger.debug(f"ChatBotInstance link for bot {bot_instance_id} in chat {chat_id} is already active.")
        return chat_link
    else:
        # Create a new link
        logger.info(f"Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id}")
        chat_link = ChatBotInstance(
            chat_id=chat_id,
            bot_instance_id=bot_instance_id,
            active=True,
            current_mood="нейтрально" # Default mood for new link
        )
        db.add(chat_link)
        # No commit here, let caller handle transaction
        return chat_link

def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
    """
    Deactivates the link between a bot instance and a chat. Commits the change.
    Returns True if deactivated, False otherwise.
    """
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id,
        ChatBotInstance.active == True # Only deactivate active links
    ).first()
    if chat_link:
        logger.info(f"Deactivating ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
        chat_link.active = False
        try:
            db.commit()
            return True
        except SQLAlchemyError as e:
            logger.error(f"Failed to commit deactivation for ChatBotInstance {chat_link.id}: {e}", exc_info=True)
            db.rollback()
            return False
    else:
        logger.warning(f"No active ChatBotInstance found for bot {bot_instance_id} in chat {chat_id} to deactivate.")
        return False


# --- Table Creation ---
def create_tables():
    """Creates all defined tables in the database if they don't exist."""
    logger.info("Attempting to create database tables if they don't exist...")
    try:
        Base.metadata.create_all(engine)
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        # Use critical level for fatal errors during startup
        logger.critical(f"FATAL: Failed to create/verify database tables: {e}", exc_info=True)
        raise # Stop the application if DB setup fails
