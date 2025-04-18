import json
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import sessionmaker, relationship, Session
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union, Tuple

from config import (
    DATABASE_URL,
    DEFAULT_MOOD_PROMPTS,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE,
    DEFAULT_SPAM_PROMPT_TEMPLATE, DEFAULT_PHOTO_PROMPT_TEMPLATE, DEFAULT_VOICE_PROMPT_TEMPLATE
)


Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))
    is_paying = Column(Boolean, default=False)
    subscription_end_date = Column(DateTime, nullable=True)

    persona_configs = relationship("PersonaConfig", back_populates="owner")
    bot_instances = relationship("BotInstance", back_populates="owner")

    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username='{self.username}')>"

class PersonaConfig(Base):
    __tablename__ = 'persona_configs'
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)

    system_prompt_template = Column(Text, nullable=False, default=DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    mood_prompts_json = Column(Text, default=json.dumps(DEFAULT_MOOD_PROMPTS))
    should_respond_prompt_template = Column(Text, nullable=False, default=DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE)
    spam_prompt_template = Column(Text, nullable=True, default=DEFAULT_SPAM_PROMPT_TEMPLATE)
    photo_prompt_template = Column(Text, nullable=True, default=DEFAULT_PHOTO_PROMPT_TEMPLATE)
    voice_prompt_template = Column(Text, nullable=True, default=DEFAULT_VOICE_PROMPT_TEMPLATE)

    owner = relationship("User", back_populates="persona_configs")
    bot_instances = relationship("BotInstance", back_populates="persona_config")

    def get_mood_prompt(self, mood_name: str) -> str:
        moods = json.loads(self.mood_prompts_json)
        return moods.get(mood_name.lower(), "")

    def get_mood_names(self) -> List[str]:
        moods = json.loads(self.mood_prompts_json)
        return list(moods.keys())

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
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
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id'), nullable=False)
    active = Column(Boolean, default=True)
    current_mood = Column(String, default="neutralno")
    created_at = Column(DateTime, default=datetime.now(timezone.utc))

    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan")


    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active})>"


class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id'), nullable=False)
    message_order = Column(Integer, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.now(timezone.utc))

    # backref="chat_bot_instance" определен в ChatBotInstance


    def __repr__(self):
        return f"<ChatContext(id={self.id}, chat_bot_instance_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order})>"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Optional[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, username=username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def get_chat_bot_instance(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    return db.query(ChatBotInstance).filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True).first()


def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int, limit: int = 200) -> List[Dict[str, str]]:
    context_records = db.query(ChatContext)\
                        .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                        .order_by(ChatContext.message_order)\
                        .limit(limit)\
                        .all()
    return [{"role": c.role, "content": c.content} for c in context_records]

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str, max_context: int = 200):
    current_count = db.query(ChatContext).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).count()
    if current_count >= max_context:
        oldest_message_to_keep = db.query(ChatContext)\
                                    .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                                    .order_by(ChatContext.message_order)\
                                    .limit(1)\
                                    .offset(current_count - max_context + 1)\
                                    .first()

        if oldest_message_to_keep:
             db.query(ChatContext)\
                .filter(
                    ChatContext.chat_bot_instance_id == chat_bot_instance_id,
                    ChatContext.message_order < oldest_message_to_keep.message_order
                ).delete(synchronize_session='auto')

    max_order = db.query(ChatContext)\
                  .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                  .order_by(ChatContext.message_order.desc())\
                  .value(ChatContext.message_order) or 0

    new_message = ChatContext(
        chat_bot_instance_id=chat_bot_instance_id,
        message_order=max_order + 1,
        role=role,
        content=content,
        timestamp=datetime.now(timezone.utc)
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)


def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    chat_bot = db.query(ChatBotInstance).get(chat_bot_instance_id)
    return chat_bot.current_mood if chat_bot else "нейтрально"

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    chat_bot = db.query(ChatBotInstance).get(chat_bot_instance_id)
    if chat_bot:
        chat_bot.current_mood = mood
        db.commit()
        db.refresh(chat_bot)

def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    return db.query(ChatBotInstance).filter(ChatBotInstance.active == True).all()


def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Создает новую конфигурацию персоны с дефолтными шаблонами."""
    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
        system_prompt_template=DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        mood_prompts_json=json.dumps(DEFAULT_MOOD_PROMPTS),
        should_respond_prompt_template=DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE,
        spam_prompt_template=DEFAULT_SPAM_PROMPT_TEMPLATE,
        photo_prompt_template=DEFAULT_PHOTO_PROMPT_TEMPLATE,
        voice_prompt_template=DEFAULT_VOICE_PROMPT_TEMPLATE
    )
    db.add(persona)
    db.commit()
    db.refresh(persona)
    return persona

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Возвращает список персон, принадлежащих пользователю."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).all()

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    """Возвращает персону по имени и владельцу."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id, PersonaConfig.name == name).first()

def get_persona_by_id_and_owner(db: Session, owner_id: int, persona_id: int) -> Optional[PersonaConfig]:
    """Возвращает персону по ID и владельцу."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id, PersonaConfig.id == persona_id).first()


def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
    """Создает экземпляр бота на основе конфига персоны."""
    bot_instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        name=name
    )
    db.add(bot_instance)
    db.commit()
    db.refresh(bot_instance)
    return bot_instance

def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    """Возвращает экземпляр бота по ID."""
    return db.query(BotInstance).get(instance_id)


def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> ChatBotInstance:
    """Связывает экземпляр бота с чатом."""
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id
    ).first()

    if chat_link:
        chat_link.active = True
        db.commit()
        db.refresh(chat_link)
        return chat_link
    else:
        chat_link = ChatBotInstance(
            chat_id=chat_id,
            bot_instance_id=bot_instance_id,
            active=True,
            current_mood="neutralno"
        )
        db.add(chat_link)
        db.commit()
        db.refresh(chat_link)
        return chat_link

def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
    """Деактивирует связь экземпляра бота с чатом."""
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id,
        ChatBotInstance.active == True
    ).first()
    if chat_link:
        chat_link.active = False
        db.commit()
        return True
    return False

def delete_persona_config(db: Session, persona_id: int) -> bool:
    """Удаляет конфигурацию персоны."""
    persona = db.query(PersonaConfig).get(persona_id)
    if persona:
        db.delete(persona)
        db.commit()
        return True
    return False

def create_tables():
    """Создает все таблицы в базе данных."""
    Base.metadata.create_all(engine)