import json
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func, BIGINT
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
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
            if 'persona_configs' not in self.__dict__ and not hasattr(self, '_sa_instance_state'):
                 logger.warning(f"Accessing can_create_persona for potentially detached User {self.id}. Querying count directly.")
                 from sqlalchemy.orm.session import Session as SQLASession
                 db_session = SQLASession.object_session(self)
                 if db_session:
                      count = db_session.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() or 0
                 else:
                      logger.error(f"Cannot get session for detached User {self.id} to check persona count.")
                      return False
            elif self.persona_configs is not None:
                count = len(self.persona_configs)
            else:
                count = 0

        except Exception as e:
             logger.error(f"Error accessing persona_configs for User {self.id} in can_create_persona: {e}", exc_info=True)
             try:
                 from sqlalchemy.orm.session import Session as SQLASession
                 db_session = SQLASession.object_session(self)
                 if db_session:
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

    owner = relationship("User", back_populates="persona_configs")
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
        flag_modified(self, "mood_prompts_json")

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=True)

    persona_config = relationship("PersonaConfig", back_populates="bot_instances")
    owner = relationship("User", back_populates="bot_instances")
    chat_links = relationship("ChatBotInstance", backref="bot_instance_ref", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False)
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False)
    active = Column(Boolean, default=True)
    current_mood = Column(String, default="нейтрально")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    is_muted = Column(Boolean, default=False, nullable=False)

    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active}, muted={self.is_muted})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False)
    message_order = Column(Integer, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        content_preview = (self.content[:50] + '...') if len(self.content) > 50 else self.content
        return f"<ChatContext(id={self.id}, cbi_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order}, content='{content_preview}')>"

MAX_CONTEXT_MESSAGES_STORED = 200

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
    if DATABASE_URL.startswith("sqlite"):
        engine_args["connect_args"] = {"check_same_thread": False}
    elif DATABASE_URL.startswith("postgres"):
         # Ensure SSL is used by default with Supabase/cloud providers
         if 'sslmode' not in DATABASE_URL:
             logger.info("Adding sslmode=require to DATABASE_URL for PostgreSQL")
             engine_args["connect_args"] = {"sslmode": "require"}
         else:
             logger.info(f"sslmode is already present in DATABASE_URL")

    try:
        engine = create_engine(
            DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            # pool_pre_ping=True, # Can help detect stale connections early
            **engine_args
        )
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("Database engine and session maker initialized.")

        # Test connection immediately
        logger.info("Attempting to establish initial database connection...")
        with engine.connect() as connection:
             logger.info("Initial database connection successful.")

    except OperationalError as e:
         logger.critical(f"FATAL: Failed to create database engine or initial connection: {e}", exc_info=True)
         logger.critical("Please check your DATABASE_URL and network connectivity.")
         raise
    except Exception as e:
         logger.critical(f"FATAL: An unexpected error occurred during database initialization: {e}", exc_info=True)
         raise

def get_db() -> Session:
    if SessionLocal is None:
         logger.error("Database is not initialized. Call initialize_database() first.")
         raise RuntimeError("Database not initialized.")
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database Session Error: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception as rb_err:
             logger.error(f"Error during rollback: {rb_err}")
        raise
    finally:
        db.close()

def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    user = None
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            logger.info(f"Creating new user for telegram_id {telegram_id} (Username: {username})")
            user = User(telegram_id=telegram_id, username=username)
            if telegram_id == ADMIN_USER_ID:
                logger.info(f"Setting admin user {telegram_id} as subscribed indefinitely.")
                user.is_subscribed = True
                user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            db.add(user)
            db.flush()
            db.refresh(user)
            logger.info(f"New user created with DB ID {user.id}")

        elif user.telegram_id == ADMIN_USER_ID and not user.is_active_subscriber:
            logger.info(f"Ensuring admin user {telegram_id} is subscribed.")
            user.is_subscribed = True
            user.subscription_expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            db.flush()
            db.refresh(user)

        if user.username != username and username is not None:
             logger.info(f"Updating username for user {telegram_id} from '{user.username}' to '{username}'")
             user.username = username
             db.flush()
             db.refresh(user)

        db.commit() # Commit all changes for this user at the end

    except SQLAlchemyError as e:
         logger.error(f"DB Error in get_or_create_user for {telegram_id}: {e}", exc_info=True)
         db.rollback()
         raise
    return user

def check_and_update_user_limits(db: Session, user: User) -> bool:
    if user.telegram_id == ADMIN_USER_ID:
        return True

    now = datetime.now(timezone.utc)
    today = now.date()

    reset_needed = (not user.last_message_reset) or (user.last_message_reset.date() < today)

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id} (Previous: {user.daily_message_count}, Last reset: {user.last_message_reset})")
        user.daily_message_count = 0
        user.last_message_reset = now

    can_send = user.daily_message_count < user.message_limit

    if can_send:
        user.daily_message_count += 1
        logger.debug(f"User {user.telegram_id} message count incremented to {user.daily_message_count}/{user.message_limit}")

    try:
        db.commit() # Commit changes (reset or increment)
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit user limit update for {user.telegram_id}: {e}", exc_info=True)
         db.rollback()
         return False # Assume failure if commit fails

    if not can_send:
         logger.info(f"User {user.telegram_id} message limit reached ({user.daily_message_count}/{user.message_limit}).")

    return can_send


def activate_subscription(db: Session, user_id: int) -> bool:
    user = None
    try:
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if user:
            logger.info(f"Activating subscription for user {user.telegram_id} (DB ID: {user_id})")
            now = datetime.now(timezone.utc)
            start_date = max(now, user.subscription_expires_at) if user.is_active_subscriber and user.subscription_expires_at else now
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

def get_active_chat_bot_instance_with_relations(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    try:
        return db.query(ChatBotInstance)\
            .options(
                joinedload(ChatBotInstance.bot_instance_ref)
                .joinedload(BotInstance.persona_config),
                joinedload(ChatBotInstance.bot_instance_ref)
                .joinedload(BotInstance.owner)
            )\
            .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
            .first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting active chatbot instance for chat {chat_id}: {e}", exc_info=True)
        return None


def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    try:
        context_records = db.query(ChatContext)\
                            .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                            .order_by(ChatContext.message_order.desc())\
                            .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                            .all()
        return [{"role": c.role, "content": c.content} for c in reversed(context_records)]
    except SQLAlchemyError as e:
        logger.error(f"DB error getting context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        return []

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    max_content_length = 4000
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for chat_bot_instance {chat_bot_instance_id}")
        content = content[:max_content_length - 3] + "..."

    try:
        current_count = db.query(func.count(ChatContext.id)).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).scalar()

        if current_count >= MAX_CONTEXT_MESSAGES_STORED:
             oldest_to_keep_order = db.query(ChatContext.message_order)\
                                     .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                                     .order_by(ChatContext.message_order.desc())\
                                     .limit(1)\
                                     .offset(MAX_CONTEXT_MESSAGES_STORED - 1)\
                                     .scalar()

             if oldest_to_keep_order is not None:
                 deleted_count = db.query(ChatContext)\
                                   .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                                           ChatContext.message_order < oldest_to_keep_order)\
                                   .delete(synchronize_session=False)
                 logger.debug(f"Pruned {deleted_count} old context messages for instance {chat_bot_instance_id} (keeping ~{MAX_CONTEXT_MESSAGES_STORED}).")
                 db.flush()

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
        db.flush()
    except SQLAlchemyError as e:
        logger.error(f"DB error adding message to context for instance {chat_bot_instance_id}: {e}", exc_info=True)
        db.rollback() # Rollback only the context add attempt
        raise # Re-raise so the caller knows it failed

def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    try:
        mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
        return mood if mood else "нейтрально"
    except SQLAlchemyError as e:
        logger.error(f"DB error getting mood for instance {chat_bot_instance_id}: {e}", exc_info=True)
        return "нейтрально"


def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    try:
        chat_bot = db.query(ChatBotInstance).filter(ChatBotInstance.id == chat_bot_instance_id).first()
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


def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    try:
        return db.query(ChatBotInstance)\
            .filter(ChatBotInstance.active == True)\
            .options(
                joinedload(ChatBotInstance.bot_instance_ref)
                .joinedload(BotInstance.persona_config),
                joinedload(ChatBotInstance.bot_instance_ref)
                .joinedload(BotInstance.owner)
            ).all()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting all active instances: {e}", exc_info=True)
        return []

def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    logger.info(f"Attempting to create persona '{name}' for owner_id {owner_id}")
    if description is None:
        description = f"ai бот по имени {name}."

    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
    )
    try:
        db.add(persona)
        db.commit()
        db.refresh(persona)
        logger.info(f"Successfully created PersonaConfig '{name}' with ID {persona.id} for owner_id {owner_id}")
        return persona
    except IntegrityError as e:
        logger.warning(f"IntegrityError creating persona '{name}' for owner {owner_id}: {e}", exc_info=False)
        db.rollback()
        raise
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit new persona '{name}' for owner {owner_id}: {e}", exc_info=True)
         db.rollback()
         raise

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    try:
        return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).order_by(PersonaConfig.name).all()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting personas for owner {owner_id}: {e}", exc_info=True)
        return []

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    try:
        return db.query(PersonaConfig).filter(
            PersonaConfig.owner_id == owner_id,
            func.lower(PersonaConfig.name) == name.lower()
        ).first()
    except SQLAlchemyError as e:
        logger.error(f"DB error getting persona by name '{name}' for owner {owner_id}: {e}", exc_info=True)
        return None


def get_persona_by_id_and_owner(db: Session, owner_telegram_id: int, persona_id: int) -> Optional[PersonaConfig]:
    logger.debug(f"Searching for PersonaConfig id={persona_id} owned by telegram_id={owner_telegram_id}")
    try:
        user = db.query(User).filter(User.telegram_id == owner_telegram_id).first()
        if not user:
            logger.warning(f"User with telegram_id {owner_telegram_id} not found when searching for persona {persona_id}")
            return None
        logger.debug(f"Found User with id={user.id} for telegram_id={owner_telegram_id}")
        persona_config = db.query(PersonaConfig).filter(
            PersonaConfig.owner_id == user.id,
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
    logger.warning(f"Attempting to delete PersonaConfig {persona_id} owned by User ID {owner_id}")
    try:
        persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == owner_id).first()
        if persona:
            db.delete(persona)
            db.commit()
            logger.info(f"Successfully deleted PersonaConfig {persona_id}")
            return True
        else:
            logger.warning(f"PersonaConfig {persona_id} not found or not owned by User ID {owner_id} for deletion.")
            return False
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit deletion of PersonaConfig {persona_id}: {e}", exc_info=True)
        db.rollback()
        return False


def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
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
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit new BotInstance for persona {persona_config_id}: {e}", exc_info=True)
        db.rollback()
        raise

def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    try:
        return db.query(BotInstance).get(instance_id)
    except SQLAlchemyError as e:
        logger.error(f"DB error getting bot instance by ID {instance_id}: {e}", exc_info=True)
        return None


def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> Optional[ChatBotInstance]:
    chat_link = None
    try:
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id,
            ChatBotInstance.bot_instance_id == bot_instance_id
        ).first()

        if chat_link:
            if not chat_link.active:
                 logger.info(f"Reactivating existing ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
                 chat_link.active = True
                 chat_link.current_mood = "нейтрально"
                 chat_link.is_muted = False
                 db.flush() # Flush reactivation changes
            else:
                logger.debug(f"ChatBotInstance link for bot {bot_instance_id} in chat {chat_id} is already active.")
        else:
            logger.info(f"Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id}")
            chat_link = ChatBotInstance(
                chat_id=chat_id,
                bot_instance_id=bot_instance_id,
                active=True,
                current_mood="нейтрально",
                is_muted=False
            )
            db.add(chat_link)
            db.flush() # Flush creation

        # Commit changes (reactivation or creation)
        db.commit()
        if chat_link: db.refresh(chat_link)
        return chat_link

    except SQLAlchemyError as e:
         logger.error(f"Failed linking bot instance {bot_instance_id} to chat {chat_id}: {e}", exc_info=True)
         db.rollback()
         return None


def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
    try:
        chat_link = db.query(ChatBotInstance).filter(
            ChatBotInstance.chat_id == chat_id,
            ChatBotInstance.bot_instance_id == bot_instance_id,
            ChatBotInstance.active == True
        ).first()
        if chat_link:
            logger.info(f"Deactivating ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
            chat_link.active = False
            db.commit()
            return True
        else:
            logger.warning(f"No active ChatBotInstance found for bot {bot_instance_id} in chat {chat_id} to deactivate.")
            return False
    except SQLAlchemyError as e:
            logger.error(f"Failed to commit deactivation for ChatBotInstance bot {bot_instance_id} chat {chat_id}: {e}", exc_info=True)
            db.rollback()
            return False



def create_tables():
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
