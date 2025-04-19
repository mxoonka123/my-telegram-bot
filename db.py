# db.py

import json
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import sessionmaker, relationship, Session, joinedload # <--- Добавили joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError # Добавили импорт SQLAlchemyError
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional, Union, Tuple

# Импортируем все необходимое из config
from config import (
    DATABASE_URL,
    DEFAULT_MOOD_PROMPTS,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE, DEFAULT_SHOULD_RESPOND_PROMPT_TEMPLATE,
    DEFAULT_SPAM_PROMPT_TEMPLATE, DEFAULT_PHOTO_PROMPT_TEMPLATE, DEFAULT_VOICE_PROMPT_TEMPLATE,
    FREE_PERSONA_LIMIT, PAID_PERSONA_LIMIT, FREE_DAILY_MESSAGE_LIMIT, PAID_DAILY_MESSAGE_LIMIT,
    SUBSCRIPTION_DURATION_DAYS
)
import logging # Добавили logging

logger = logging.getLogger(__name__) # Инициализировали logger

Base = declarative_base()

# --- Модели SQLAlchemy ---

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

    persona_configs = relationship("PersonaConfig", back_populates="owner", cascade="all, delete-orphan", lazy="selectin") # Changed lazy loading strategy
    bot_instances = relationship("BotInstance", back_populates="owner", cascade="all, delete-orphan")

    @property
    def is_active_subscriber(self) -> bool:
        # Проверка, что дата не None перед сравнением
        return self.is_subscribed and self.subscription_expires_at and self.subscription_expires_at > datetime.now(timezone.utc)

    @property
    def message_limit(self) -> int:
        return PAID_DAILY_MESSAGE_LIMIT if self.is_active_subscriber else FREE_DAILY_MESSAGE_LIMIT

    @property
    def persona_limit(self) -> int:
        return PAID_PERSONA_LIMIT if self.is_active_subscriber else FREE_PERSONA_LIMIT

    @property
    def can_create_persona(self) -> bool:
        # Используем счетчик напрямую из базы данных для надежности, если сессия закрыта
        # Или проверяем, загружена ли коллекция
        if self.persona_configs is not None: # Проверка, если коллекция уже загружена (например, через joinedload)
             count = len(self.persona_configs)
        else: # Если не загружена, делаем запрос (требует активной сессии)
             try:
                 # Requires an active session bound to this User instance, which might not always be the case
                 # This approach is less reliable if the User object is detached.
                 # A better approach would be to query the count separately when needed.
                 # For now, we'll rely on the collection being loaded or return False as a fallback.
                 logger.warning(f"Accessing can_create_persona on potentially detached User {self.id}. Persona count might be inaccurate.")
                 # Fallback assuming limit is reached if not loaded
                 return False # Or query if session is available: count = Session.object_session(self).query(func.count(PersonaConfig.id)).filter(PersonaConfig.owner_id == self.id).scalar() if Session.object_session(self) else 0

             except Exception as e:
                 logger.error(f"Error checking persona count for User {self.id}: {e}")
                 return False # Безопасный fallback

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
    max_response_messages = Column(Integer, default=3, nullable=False)

    owner = relationship("User", back_populates="persona_configs")
    bot_instances = relationship("BotInstance", back_populates="persona_config", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('owner_id', 'name', name='_owner_persona_name_uc'),)

    def get_mood_prompt(self, mood_name: str) -> str:
        try:
            moods = json.loads(self.mood_prompts_json or '{}') # Используем or '{}' для None
            # Ищем без учета регистра
            for key, value in moods.items():
                if key.lower() == mood_name.lower():
                    return value
            return "" # Возвращаем пустую строку, если не найдено
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
        # Обновление должно происходить через сессию
        self.mood_prompts_json = json.dumps(moods)
        flag_modified(self, "mood_prompts_json") # Помечаем как измененное
        # db_session.add(self) # Добавляем в сессию, если объект был detached
        # db_session.commit() # Коммит должен быть во внешней функции

    def __repr__(self):
        return f"<PersonaConfig(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"

class BotInstance(Base):
    __tablename__ = 'bot_instances'
    id = Column(Integer, primary_key=True)
    persona_config_id = Column(Integer, ForeignKey('persona_configs.id', ondelete='CASCADE'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False) # Владелец персоны
    name = Column(String, nullable=True)

    persona_config = relationship("PersonaConfig", back_populates="bot_instances")
    owner = relationship("User", back_populates="bot_instances") # Связь с владельцем персоны
    chat_links = relationship("ChatBotInstance", backref="bot_instance_ref", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<BotInstance(id={self.id}, name='{self.name}', persona_config_id={self.persona_config_id}, owner_id={self.owner_id})>"

class ChatBotInstance(Base):
    __tablename__ = 'chat_bot_instances'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False) # ID чата Telegram
    bot_instance_id = Column(Integer, ForeignKey('bot_instances.id', ondelete='CASCADE'), nullable=False)
    active = Column(Boolean, default=True)
    current_mood = Column(String, default="нейтрально")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    context = relationship("ChatContext", backref="chat_bot_instance", order_by="ChatContext.message_order", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('chat_id', 'bot_instance_id', name='_chat_bot_uc'),) # Уникальная связь бота и чата

    def __repr__(self):
        return f"<ChatBotInstance(id={self.id}, chat_id='{self.chat_id}', bot_instance_id={self.bot_instance_id}, active={self.active})>"

class ChatContext(Base):
    __tablename__ = 'chat_contexts'
    id = Column(Integer, primary_key=True)
    chat_bot_instance_id = Column(Integer, ForeignKey('chat_bot_instances.id', ondelete='CASCADE'), nullable=False)
    message_order = Column(Integer, nullable=False) # Порядок сообщений в контексте
    role = Column(String, nullable=False) # 'user' или 'assistant'
    content = Column(Text, nullable=False) # Текст сообщения
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ChatContext(id={self.id}, chat_bot_instance_id={self.chat_bot_instance_id}, role='{self.role}', order={self.message_order})>"

# --- Константы и Настройки Базы Данных ---

MAX_CONTEXT_MESSAGES_STORED = 200 # Сколько храним в БД
MAX_CONTEXT_MESSAGES_SENT_TO_LLM = 40 # Сколько отправляем в LLM

# Создаем engine (соединение с БД)
# Лучше проверять DATABASE_URL на None
if not DATABASE_URL:
    logger.error("DATABASE_URL environment variable is not set!")
    # Можно либо выйти, либо использовать дефолтную SQLite для локальной отладки
    DATABASE_URL = "sqlite:///./bot_data_fallback.db" # Пример fallback
    logger.warning(f"Using fallback database: {DATABASE_URL}")

engine = create_engine(DATABASE_URL)

# Создаем фабрику сессий
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# --- Функции для работы с БД (Data Access Layer) ---

def get_db() -> Session:
    """Генератор сессий SQLAlchemy."""
    db = SessionLocal()
    try:
        yield db
    except SQLAlchemyError as e:
        logger.error(f"Database Session Error: {e}", exc_info=True)
        db.rollback() # Откатываем транзакцию при ошибке в сессии
        raise # Передаем исключение дальше
    finally:
        db.close()

def get_or_create_user(db: Session, telegram_id: int, username: str = None) -> User:
    """Получает или создает пользователя в БД."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        logger.info(f"Creating new user for telegram_id {telegram_id}")
        user = User(telegram_id=telegram_id, username=username)
        db.add(user)
        try:
            db.commit()
            db.refresh(user) # Обновляем объект после коммита
        except SQLAlchemyError as e:
             logger.error(f"Failed to commit new user {telegram_id}: {e}", exc_info=True)
             db.rollback()
             raise # Передаем ошибку выше
    return user

def check_and_update_user_limits(db: Session, user: User) -> bool:
    """Проверяет и обновляет дневной лимит сообщений пользователя."""
    now = datetime.now(timezone.utc)
    today = now.date()

    # Проверяем, нужно ли сбросить счетчик
    # Используем date() для сравнения только дат
    reset_needed = (not user.last_message_reset) or (user.last_message_reset.date() < today)

    if reset_needed:
        logger.info(f"Resetting daily message count for user {user.telegram_id}")
        user.daily_message_count = 0
        user.last_message_reset = now
        # Коммит будет ниже после проверки лимита или инкремента

    # Проверяем лимит
    can_send = user.daily_message_count < user.message_limit

    if can_send:
        user.daily_message_count += 1
        logger.debug(f"User {user.telegram_id} message count incremented to {user.daily_message_count}/{user.message_limit}")
    else:
        logger.info(f"User {user.telegram_id} message limit reached ({user.daily_message_count}/{user.message_limit}).")

    try:
        # Коммитим изменения (сброс или инкремент)
        db.commit()
        if reset_needed:
            db.refresh(user) # Обновляем объект user, если был сброс
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit user limit update for {user.telegram_id}: {e}", exc_info=True)
         db.rollback()
         # Если коммит не удался, считаем, что лимит не пройден (безопасный вариант)
         return False

    return can_send

def activate_subscription(db: Session, user_id: int) -> bool:
    """Активирует подписку для пользователя."""
    user = db.query(User).get(user_id) # Получаем пользователя по его ID в таблице users
    if user:
        logger.info(f"Activating subscription for user {user.telegram_id} (DB ID: {user_id})")
        now = datetime.now(timezone.utc)
        # Рассчитываем дату окончания подписки
        expiry_date = now + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
        user.is_subscribed = True
        user.subscription_expires_at = expiry_date
        # Сбрасываем счетчики при активации
        user.daily_message_count = 0
        user.last_message_reset = now
        try:
            db.commit()
            logger.info(f"Subscription activated for user {user.telegram_id} until {expiry_date}")
            return True
        except SQLAlchemyError as e:
             logger.error(f"Failed to commit subscription activation for {user.telegram_id}: {e}", exc_info=True)
             db.rollback()
             return False
    else:
         logger.warning(f"User with DB ID {user_id} not found for subscription activation.")
         return False

def get_active_chat_bot_instance_with_relations(db: Session, chat_id: str) -> Optional[ChatBotInstance]:
    """Получает активный ChatBotInstance со связанными объектами."""
    return db.query(ChatBotInstance)\
        .options(
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.persona_config),
            joinedload(ChatBotInstance.bot_instance_ref)
            .joinedload(BotInstance.owner) # Загружаем и владельца
        )\
        .filter(ChatBotInstance.chat_id == chat_id, ChatBotInstance.active == True)\
        .first()

def get_context_for_chat_bot(db: Session, chat_bot_instance_id: int) -> List[Dict[str, str]]:
    """Получает последние N сообщений контекста для LLM."""
    context_records = db.query(ChatContext)\
                        .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                        .order_by(ChatContext.message_order.desc())\
                        .limit(MAX_CONTEXT_MESSAGES_SENT_TO_LLM)\
                        .all()
    # Возвращаем в правильном хронологическом порядке
    return [{"role": c.role, "content": c.content} for c in reversed(context_records)]

def add_message_to_context(db: Session, chat_bot_instance_id: int, role: str, content: str):
    """Добавляет сообщение в контекст, удаляя старое при превышении лимита хранения."""
    # Ограничиваем длину контента перед сохранением
    max_content_length = 4000 # Примерный лимит, можно настроить
    if len(content) > max_content_length:
        logger.warning(f"Truncating context message content from {len(content)} to {max_content_length} chars for chat_bot_instance {chat_bot_instance_id}")
        content = content[:max_content_length] + "..."

    # Считаем текущее количество
    current_count = db.query(func.count(ChatContext.id)).filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id).scalar()

    # Удаляем старое, если нужно
    if current_count >= MAX_CONTEXT_MESSAGES_STORED:
         oldest_message = db.query(ChatContext)\
                           .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                           .order_by(ChatContext.message_order.asc())\
                           .first()
         if oldest_message:
             db.delete(oldest_message)
             logger.debug(f"Deleted oldest context message {oldest_message.id} for instance {chat_bot_instance_id}")

    # Находим максимальный порядок
    max_order = db.query(func.max(ChatContext.message_order))\
                  .filter(ChatContext.chat_bot_instance_id == chat_bot_instance_id)\
                  .scalar() or 0

    # Создаем и добавляем новое сообщение
    new_message = ChatContext(
        chat_bot_instance_id=chat_bot_instance_id,
        message_order=max_order + 1,
        role=role,
        content=content,
        timestamp=datetime.now(timezone.utc)
    )
    db.add(new_message)

    # Коммит делаем во внешней функции (например, в handle_message) после всех операций
    # db.commit() # Убрали коммит отсюда

def get_mood_for_chat_bot(db: Session, chat_bot_instance_id: int) -> str:
    """Получает текущее настроение для ChatBotInstance."""
    mood = db.query(ChatBotInstance.current_mood).filter(ChatBotInstance.id == chat_bot_instance_id).scalar()
    return mood if mood else "нейтрально"

def set_mood_for_chat_bot(db: Session, chat_bot_instance_id: int, mood: str):
    """Устанавливает настроение для ChatBotInstance."""
    chat_bot = db.query(ChatBotInstance).get(chat_bot_instance_id)
    if chat_bot:
        if chat_bot.current_mood != mood:
            chat_bot.current_mood = mood
            try:
                db.commit()
                logger.info(f"Set mood to '{mood}' for ChatBotInstance {chat_bot_instance_id}")
            except SQLAlchemyError as e:
                logger.error(f"Failed to commit mood change for {chat_bot_instance_id}: {e}", exc_info=True)
                db.rollback()
    else:
        logger.warning(f"ChatBotInstance {chat_bot_instance_id} not found to set mood.")

def get_all_active_chat_bot_instances(db: Session) -> List[ChatBotInstance]:
    """Получает ВСЕ активные ChatBotInstance со связями для фоновых задач."""
    # Используем ИСПРАВЛЕННЫЙ синтаксис joinedload
    return db.query(ChatBotInstance).filter(ChatBotInstance.active == True).options(
        joinedload(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.persona_config), # <-- Исправлено
        joinedload(ChatBotInstance.bot_instance_ref).joinedload(BotInstance.owner)           # <-- Исправлено
    ).all()

def create_persona_config(db: Session, owner_id: int, name: str, description: str = None) -> PersonaConfig:
    """Создает новую конфигурацию персоны."""
    logger.info(f"Creating persona '{name}' for owner_id {owner_id}")
    persona = PersonaConfig(
        owner_id=owner_id,
        name=name,
        description=description,
        # Остальные поля имеют дефолты в модели
    )
    db.add(persona)
    # Коммит должен быть здесь, так как это атомарная операция создания
    try:
        db.commit()
        db.refresh(persona)
        return persona
    except SQLAlchemyError as e:
         logger.error(f"Failed to commit new persona '{name}' for owner {owner_id}: {e}", exc_info=True)
         db.rollback()
         raise # Передаем ошибку выше

def get_personas_by_owner(db: Session, owner_id: int) -> List[PersonaConfig]:
    """Возвращает список персон пользователя."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id).order_by(PersonaConfig.name).all() # Добавили сортировку

def get_persona_by_name_and_owner(db: Session, owner_id: int, name: str) -> Optional[PersonaConfig]:
    """Находит персону по имени и владельцу (без учета регистра)."""
    return db.query(PersonaConfig).filter(
        PersonaConfig.owner_id == owner_id,
        func.lower(PersonaConfig.name) == name.lower() # Поиск без учета регистра
    ).first()

def get_persona_by_id_and_owner(db: Session, owner_id: int, persona_id: int) -> Optional[PersonaConfig]:
    """Находит персону по ID и владельцу."""
    return db.query(PersonaConfig).filter(PersonaConfig.owner_id == owner_id, PersonaConfig.id == persona_id).first()

def create_bot_instance(db: Session, owner_id: int, persona_config_id: int, name: str = None) -> BotInstance:
    """Создает экземпляр бота."""
    logger.info(f"Creating BotInstance for persona_id {persona_config_id}, owner_id {owner_id}")
    bot_instance = BotInstance(
        owner_id=owner_id,
        persona_config_id=persona_config_id,
        name=name
    )
    db.add(bot_instance)
    # Коммит должен быть здесь
    try:
        db.commit()
        db.refresh(bot_instance)
        return bot_instance
    except SQLAlchemyError as e:
        logger.error(f"Failed to commit new BotInstance for persona {persona_config_id}: {e}", exc_info=True)
        db.rollback()
        raise

def get_bot_instance_by_id(db: Session, instance_id: int) -> Optional[BotInstance]:
    """Находит экземпляр бота по ID."""
    return db.query(BotInstance).get(instance_id)

def link_bot_instance_to_chat(db: Session, bot_instance_id: int, chat_id: str) -> Optional[ChatBotInstance]:
    """Связывает или активирует экземпляр бота в чате."""
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id
    ).first()

    if chat_link:
        if not chat_link.active:
             logger.info(f"Reactivating ChatBotInstance {chat_link.id} for bot {bot_instance_id} in chat {chat_id}")
             chat_link.active = True
             # Коммит делаем во внешней функции (add_bot_to_chat)
        return chat_link
    else:
        logger.info(f"Creating new ChatBotInstance link for bot {bot_instance_id} in chat {chat_id}")
        chat_link = ChatBotInstance(
            chat_id=chat_id,
            bot_instance_id=bot_instance_id,
            active=True,
            current_mood="нейтрально"
        )
        db.add(chat_link)
        # Коммит делаем во внешней функции (add_bot_to_chat)
        return chat_link # Возвращаем новый объект, он получит ID после коммита

def unlink_bot_instance_from_chat(db: Session, chat_id: str, bot_instance_id: int) -> bool:
    """Деактивирует связь бота с чатом."""
    chat_link = db.query(ChatBotInstance).filter(
        ChatBotInstance.chat_id == chat_id,
        ChatBotInstance.bot_instance_id == bot_instance_id,
        ChatBotInstance.active == True
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
    return False

def delete_persona_config(db: Session, persona_id: int, owner_id: int) -> bool:
    """Удаляет конфигурацию персоны и все связанные данные каскадно."""
    logger.warning(f"Attempting to delete PersonaConfig {persona_id} owned by {owner_id}")
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
        logger.warning(f"PersonaConfig {persona_id} not found or not owned by {owner_id} for deletion.")
        return False

def create_tables():
    """Создает все таблицы в базе данных, если их нет."""
    logger.info("Attempting to create database tables if they don't exist...")
    try:
        Base.metadata.create_all(engine)
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Failed to create/verify database tables: {e}", exc_info=True)
        # В реальном приложении здесь может быть логика выхода или повторных попыток
        raise # Прерываем выполнение, если таблицы не созданы
