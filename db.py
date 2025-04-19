import json
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import sessionmaker, relationship, Session, flag_modified # Добавили flag_modified
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional, Union, Tuple

from config import (
    DATABASE_URL,
    DEFAULT_MOOD_PROMPTS,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE,
    DEFAULT_SPAM_PROMPT_TEMPLATE, DEFAULT_PHOTO_PROMPT_TEMPLATE, DEFAULT_VOICE_PROMPT_TEMPLATE,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    SUBSCRIPTION_DURATION_DAYS
)


Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    is_subscribed = Column(Boolean, default=False)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)
    daily_message_count = Column(Integer, default=0)
    last_message_reset = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan")
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan")

    @property
    def is_active_subscriber(self) -> bool:
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        # Ensure persona_configs is loaded if accessed lazily, or handle None
        # count = db.query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() # Alternative if relationship is lazy
        count = len(self.persona_configs) if self.persona_configs is not None else 0
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
    mood_prompts_json = Column(Text, default=json.dumps(DEFAULT_MOOD_PROMPTS))
    should_respond_prompt_template = Column(Text, nullable=False, default=DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE)
    spam_prompt_template = Column(Text, nullable=True, default=DEFAULT_SPAM_PROMPT_TEMPLATE)
    photo_prompt_template = Column(Text, nullable=True, default=DEFAULT_PHOTO_PROMPT_TEMPLATE)
    voice_prompt_template = Column(Text, nullable=True, default=DEFAULT_VOICE_PROMPT_TEMPLATE)

    # Новое поле: Максимальное количество сообщений в одном ответе
    max_response_messages = Column(Integer, default=3, nullable=False) # По умолчанию 3 сообщения

    owner = relationship("User", back_populates="persona_configs")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json)
            return moods.get(mood_name.lower(), "")
        except (json.JSONDecodeError, TypeError):
             return ""


    def get_mood_names(self) -> List[str]:
        try:
            moods = json.loads(self.mood_prompts_json)
            return list(moods.keys())
        except (json.JSONDecodeError, TypeError):
            return []

    def set_moods(self, moods: Dict[str, str]):
        self.mood_prompts_json = json.dumps(moods)

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

    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan")


    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),)

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active})>"


class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False)
    message_order = Column(Integer, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


    def __repr__(self):
        return f"<ChatContext(id={self.id}, chat_bot_instance_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order})>"

# --- Константы для контекста ---
MAX_CONTEXT_MESSAGES_STORED = 200 # Сколько храним в БД
MAX_CONTEXT_MESSAGES_SENT_TO_LLM = 40 # Сколько отправляем в LLM (УМЕНЬШЕНО)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Session: # Убрал Optional, так как мы ожидаем сессию
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
    # Ensure relationships are loaded if needed immediately, or handle potential lazy loading errors
    # db.refresh(user, ['persona_configs']) # Example if needed right after creation
    return user

def check_and_update_user_limits(db: Session, user: User) -> bool:
    now = datetime.now(timezone.utc)
    today = now.date()

    if not user.last_message_reset or user.last_message_reset.date() < today:
        user.daily_message_count = 0
        user.last_message_reset = now
        # Need commit here if we reset
        db.commit()
        db.refresh(user) # Refresh state after commit


    if user.daily_message_count < user.message_limit:
        # Increment count
        user.daily_message_count += 1
        db.commit() # Commit the increment
        return True
    else:
        # Limit exceeded, no increment, commit was done if reset happened
        return False

def activate_subscription(db: Session, user_id: int) -> bool:
    user = db.query(User).get(user_id)
    if user:
        now = datetime.now(timezone.utc)
        expiry_date = now + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
        user.is_subscribed = True
        user.subscription_expires_at = expiry_date
        user.daily_message_count = 0
        user.last_message_reset = now
        db.commit()
        return True
    return False


def get_chat_bot_instance(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    return db.query(ChatBotInstance).filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True).options(
        # Явно загружаем связи, чтобы избежать проблем с ленивой загрузкой вне сессии
        relationship(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.persona_config),
        relationship(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.owner)
    ).first()


def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    # Используем константу MAX_CONTEXT_MESSAGES_SENT_TO_LLM
    context_records = db.query(ChatContext)\
                        .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                        .order_by(ChatContext.message_order.desc())\
                        .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                        .all()
    # Возвращаем в правильном хронологическом порядке (старые -> новые)
    return [{"role": c.role, "content": c.content} for c in reversed(context_records)]

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    # Используем константу MAX_CONTEXT_MESSAGES_STORED
    current_count = db.query(func.count(ChatContext.id)).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).scalar()

    if current_count >= MAX_CONTEXT_MESSAGES_STORED:
         # Находим самое старое сообщение для удаления
         oldest_message = db.query(ChatContext)\
                           .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                           .order_by(ChatContext.message_order.asc())\
                           .first()
         if oldest_message:
             db.delete(oldest_message)
             # Не делаем commit здесь, сделаем после добавления нового


    # Находим максимальный текущий порядок
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
    # Commit после всех операций с контекстом (удаление + добавление)
    db.commit()


def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
    return mood if mood else "нейтрально"

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    chat_bot = db.query(ChatBotInstance).get(chat_bot_instance_id)
    if chat_bot:
        chat_bot.current_mood = mood
        db.commit()


def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    # Также явно загружаем связи для задачи спама
    return db.query(ChatBotInstance).filter(ChatBotInstance.active == True).options(
        relationship(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.persona_config),
        relationship(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.owner)
    ).all()


def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
        # Остальные поля имеют дефолты в модели
    )
    db.add(persona)
    db.commit()
    db.refresh(persona)
    return persona

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).all()

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id, PersonaConfig.name == name).first()

def get_persona_by_id_and_owner(db: Session, owner_id: int, persona_id: int) -> Optional[PersonaConfig]:
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id, PersonaConfig.id == persona_id).first()


def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
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
    return db.query(BotInstance).get(instance_id)


def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> Optional[ChatBotInstance]:
    # Ищем существующую связь
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id
    ).first()

    if chat_link:
        # Если найдена и неактивна, активируем
        if not chat_link.active:
             chat_link.active = True
             db.commit()
             db.refresh(chat_link)
        return chat_link # Возвращаем существующую (возможно, только что активированную)
    else:
        # Связи нет, создаем новую
        chat_link = ChatBotInstance(
            chat_id=chat_id,
            bot_instance_id=bot_instance_id,
            active=True,
            current_mood="нейтрально"
        )
        db.add(chat_link)
        db.commit()
        db.refresh(chat_link)
        return chat_link

def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
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

def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    persona = db.query(PersonaConfig).filter(PersonaConfig.id == persona_id, PersonaConfig.owner_id == owner_id).first()
    if persona:
        db.delete(persona)
        db.commit()
        return True
    return False

def create_tables():
    Base.metadata.create_all(engine)
