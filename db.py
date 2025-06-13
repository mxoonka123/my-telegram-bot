import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, JSON, func, delete
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload, selectinload
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError

from config import DATABASE_URL, SUBSCRIPTION_DURATION_DAYS

logger = logging.getLogger(__name__)

# Database setup
Base = declarative_base()
engine = None
SessionLocal = None

# Default prompts and templates
DEFAULT_MOOD_PROMPTS = {
    "нейтрально": "ты спокоен и рассудителен",
    "весело": "ты в отличном настроении, шутишь и радуешься",
    "грустно": "ты немного печален, но все еще отзывчив",
    "злобно": "ты раздражен и немного агрессивен в ответах",
    "флиртующе": "ты кокетливо общаешься, используешь намеки"
}

BASE_PROMPT_SUFFIX = """
Важные правила:
- не используй заглавные буквы
- отвечай естественно, как живой человек
- можешь использовать эмодзи, но в меру
- если хочешь отправить гифку, вставь прямую ссылку на gif
"""

INTERNET_INFO_PROMPT = """
У тебя есть доступ к актуальной информации через интернет.
Можешь отвечать на вопросы о текущих событиях, новостях, погоде.
"""

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """[СИСТЕМНОЕ СООБЩЕНИЕ]
Ты - {persona_name}, {persona_description}.

Твой стиль общения: {communication_style}.
Уровень многословности: {verbosity_level}.

Твоё текущее настроение: {mood_name}. {mood_prompt}

Пользователь: {username} (ID: {user_id}) в чате {chat_id}
Последнее сообщение пользователя: {last_user_message}

{BASE_PROMPT_SUFFIX}
"""

def init_database():
    """Initialize database connection."""
    global engine, SessionLocal
    
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
        # Create tables
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

@contextmanager
def get_db():
    """Get database session context manager."""
    if SessionLocal is None:
        init_database()
    
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database session error: {e}")
        raise
    finally:
        db.close()

# Database Models
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    is_premium = Column(Boolean, default=False, nullable=False)
    premium_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Message limits
    messages_sent_this_month = Column(Integer, default=0, nullable=False)
    last_message_reset = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    personas = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan")

class PersonaConfig(Base):
    __tablename__ = "persona_configs"
    
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    
    # Communication settings
    communication_style = Column(String(50), default="neutral", nullable=False)
    verbosity_level = Column(String(50), default="medium", nullable=False)
    group_reply_preference = Column(String(50), default="mentioned_or_contextual", nullable=False)
    media_reaction = Column(String(50), default="text_only", nullable=False)
    
    # Response settings
    max_response_messages = Column(Integer, default=3, nullable=False)
    # message_volume = Column(String(20), default="normal", nullable=False)  # short, normal, long, random  <- ВРЕМЕННО ОТКЛЮЧЕНО
    
    system_prompt_template = Column(Text, nullable=False, default=DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    
    # Moods as JSON
    mood_prompts_json = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    owner = relationship("User", back_populates="personas")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan")

class BotInstance(Base):
    __tablename__ = "bot_instances"
    
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    persona_config_id = Column(Integer, ForeignKey("persona_configs.id"), nullable=False)
    instance_name = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    owner = relationship("User", back_populates="bot_instances")
    persona_config = relationship("PersonaConfig", back_populates="bot_instances")
    chat_instances = relationship("ChatBotInstance", back_populates="bot_instance", cascade="all, delete-orphan")

class ChatBotInstance(Base):
    __tablename__ = "chat_bot_instances"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_instance_id = Column(Integer, ForeignKey("bot_instances.id"), nullable=False)
    chat_id = Column(Integer, nullable=False)
    chat_type = Column(String(50), nullable=False)  # private, group, supergroup, channel
    is_active = Column(Boolean, default=True, nullable=False)
    current_mood = Column(String(50), default="нейтрально", nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    bot_instance = relationship("BotInstance", back_populates="chat_instances")
    context_messages = relationship("ChatContext", back_populates="chat_bot_instance", cascade="all, delete-orphan")

class ChatContext(Base):
    __tablename__ = "chat_contexts"
    
    id = Column(Integer, primary_key=True, index=True)
    chat_bot_instance_id = Column(Integer, ForeignKey("chat_bot_instances.id"), nullable=False)
    message_text = Column(Text, nullable=False)
    sender_type = Column(String(20), nullable=False)  # user, bot
    sender_id = Column(Integer, nullable=True)  # telegram user id
    sender_name = Column(String(255), nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    chat_bot_instance = relationship("ChatBotInstance", back_populates="context_messages")

# Database helper functions
def get_or_create_user(db: Session, telegram_id: int, username: str = None, first_name: str = None, last_name: str = None) -> User:
    """Get existing user or create a new one."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name
        )
        db.add(user)
        db.flush()
        logger.info(f"Created new user: {telegram_id}")
    else:
        # Update user info if changed
        updated = False
        if user.username != username:
            user.username = username
            updated = True
        if user.first_name != first_name:
            user.first_name = first_name
            updated = True
        if user.last_name != last_name:
            user.last_name = last_name
            updated = True
        
        if updated:
            user.updated_at = datetime.now(timezone.utc)
            logger.info(f"Updated user info: {telegram_id}")
    
    return user

def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Create a new persona configuration."""
    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description or f"личность по имени {name}",
        mood_prompts_json=json.dumps(DEFAULT_MOOD_PROMPTS)
    )
    db.add(persona)
    db.flush()
    logger.info(f"Created persona config: {name} for user {owner_id}")
    return persona

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Get all persona configurations for a user."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).all()

def get_persona_by_name_and_owner(db: Session, name: str, owner_id: int) -> Optional[PersonaConfig]:
    """Get persona by name and owner."""
    return db.query(PersonaConfig).filter(
        PersonaConfig.name == name,
        PersonaConfig.owner_id == owner_id
    ).first()

def get_persona_by_id_and_owner(db: Session, persona_id: int, owner_id: int) -> Optional[PersonaConfig]:
    """Get persona by ID and owner."""
    return db.query(PersonaConfig).filter(
        PersonaConfig.id == persona_id,
        PersonaConfig.owner_id == owner_id
    ).first()

def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    """Delete a persona configuration."""
    persona = get_persona_by_id_and_owner(db, persona_id, owner_id)
    if persona:
        db.delete(persona)
        logger.info(f"Deleted persona config: {persona.name} (ID: {persona_id})")
        return True
    return False

def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, instance_name: str) -> BotInstance:
    """Create a new bot instance."""
    instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        instance_name=instance_name
    )
    db.add(instance)
    db.flush()
    logger.info(f"Created bot instance: {instance_name}")
    return instance

def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: int, chat_type: str = "private") -> ChatBotInstance:
    """Link bot instance to a chat."""
    # Check if already linked
    existing = db.query(ChatBotInstance).filter(
        ChatBotInstance.bot_instance_id == bot_instance_id,
        ChatBotInstance.chat_id == chat_id
    ).first()
    
    if existing:
        existing.is_active = True
        existing.updated_at = datetime.now(timezone.utc)
        return existing
    
    chat_instance = ChatBotInstance(
        bot_instance_id=bot_instance_id,
        chat_id=chat_id,
        chat_type=chat_type
    )
    db.add(chat_instance)
    db.flush()
    logger.info(f"Linked bot instance {bot_instance_id} to chat {chat_id}")
    return chat_instance

def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int, limit: int = 10) -> List[ChatContext]:
    """Get context messages for a chat bot instance."""
    return db.query(ChatContext).filter(
        ChatContext.chat_bot_instance_id == chat_bot_instance_id
    ).order_by(ChatContext.created_at.desc()).limit(limit).all()

def add_message_to_context(db: Session, chat_bot_instance_id: int, message_text: str, 
                          sender_type: str, sender_id: int = None, sender_name: str = None):
    """Add a message to chat context."""
    context = ChatContext(
        chat_bot_instance_id=chat_bot_instance_id,
        message_text=message_text,
        sender_type=sender_type,
        sender_id=sender_id,
        sender_name=sender_name
    )
    db.add(context)
    logger.debug(f"Added message to context: {sender_type} - {message_text[:50]}...")

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str) -> bool:
    """Set mood for a chat bot instance."""
    chat_instance = db.query(ChatBotInstance).filter(
        ChatBotInstance.id == chat_bot_instance_id
    ).first()
    
    if chat_instance:
        chat_instance.current_mood = mood
        chat_instance.updated_at = datetime.now(timezone.utc)
        logger.info(f"Set mood to '{mood}' for chat bot instance {chat_bot_instance_id}")
        return True
    return False

def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    """Get current mood for a chat bot instance."""
    chat_instance = db.query(ChatBotInstance).filter(
        ChatBotInstance.id == chat_bot_instance_id
    ).first()
    
    return chat_instance.current_mood if chat_instance else "нейтрально"

def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    """Get all active chat bot instances with their related data."""
    return db.query(ChatBotInstance).options(
        joinedload(ChatBotInstance.bot_instance).joinedload(BotInstance.persona_config)
    ).filter(ChatBotInstance.is_active == True).all()

def get_persona_and_context_with_owner(db: Session, chat_id: int) -> Optional[Tuple[PersonaConfig, ChatBotInstance, User]]:
    """Get persona config, chat bot instance, and owner for a chat."""
    result = db.query(ChatBotInstance).options(
        joinedload(ChatBotInstance.bot_instance).joinedload(BotInstance.persona_config),
        joinedload(ChatBotInstance.bot_instance).joinedload(BotInstance.owner)
    ).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.is_active == True
    ).first()
    
    if result:
        return result.bot_instance.persona_config, result, result.bot_instance.owner
    return None

def check_and_update_user_limits(db: Session, user_id: int) -> Tuple[bool, int, int]:
    """Check and update user message limits."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False, 0, 0
    
    now = datetime.now(timezone.utc)
    
    # Reset monthly counter if needed
    if user.last_message_reset:
        if now.month != user.last_message_reset.month or now.year != user.last_message_reset.year:
            user.messages_sent_this_month = 0
            user.last_message_reset = now
    else:
        user.last_message_reset = now
    
    from config import FREE_USER_MONTHLY_MESSAGE_LIMIT, PREMIUM_USER_MONTHLY_MESSAGE_LIMIT
    
    # Determine limits based on subscription
    if user.is_premium and user.premium_until and user.premium_until > now:
        limit = PREMIUM_USER_MONTHLY_MESSAGE_LIMIT
    else:
        limit = FREE_USER_MONTHLY_MESSAGE_LIMIT
        # Deactivate premium if expired
        if user.is_premium and (not user.premium_until or user.premium_until <= now):
            user.is_premium = False
            user.premium_until = None
    
    can_send = user.messages_sent_this_month < limit
    if can_send:
        user.messages_sent_this_month += 1
    
    remaining = max(0, limit - user.messages_sent_this_month)
    
    return can_send, remaining, limit

def activate_subscription(db: Session, user_id: int) -> bool:
    """Activate premium subscription for a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    
    now = datetime.now(timezone.utc)
    
    # If user already has premium, extend it
    if user.is_premium and user.premium_until and user.premium_until > now:
        user.premium_until += timedelta(days=SUBSCRIPTION_DURATION_DAYS)
    else:
        user.premium_until = now + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
    
    user.is_premium = True
    user.updated_at = now
    
    logger.info(f"Activated premium subscription for user {user.telegram_id} until {user.premium_until}")
    return True

# Initialize database when module is imported
if DATABASE_URL:
    try:
        init_database()
    except Exception as e:
        logger.error(f"Failed to initialize database on import: {e}")
