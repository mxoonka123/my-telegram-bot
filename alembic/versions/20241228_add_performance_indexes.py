"""Add performance indexes for optimization

Revision ID: performance_indexes_001
Revises: 
Create Date: 2024-12-28 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'performance_indexes_001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """Добавляем критические индексы для ускорения запросов"""
    
    # Индексы для таблицы users
    op.create_index('ix_users_credits', 'users', ['credits'], unique=False)
    op.create_index('ix_users_telegram_id_credits', 'users', ['telegram_id', 'credits'], unique=False)
    
    # Индексы для таблицы persona_configs
    op.create_index('ix_persona_configs_owner_id', 'persona_configs', ['owner_id'], unique=False)
    
    # Индексы для таблицы bot_instances
    op.create_index('ix_bot_instances_status_owner', 'bot_instances', ['status', 'owner_id'], unique=False)
    op.create_index('ix_bot_instances_telegram_bot_id', 'bot_instances', ['telegram_bot_id'], unique=False)
    
    # Индексы для таблицы chat_bot_instances - САМЫЕ ВАЖНЫЕ!
    op.create_index('ix_chat_bot_active', 'chat_bot_instances', ['chat_id', 'active'], unique=False)
    op.create_index('ix_chat_bot_instance', 'chat_bot_instances', ['bot_instance_id', 'active'], unique=False)
    op.create_index('ix_chat_bot_muted', 'chat_bot_instances', ['is_muted', 'active'], unique=False)
    
    # Индексы для таблицы chat_contexts - критично для производительности!
    op.create_index('ix_context_chat_order', 'chat_contexts', 
                   ['chat_bot_instance_id', 'message_order'], unique=False)
    op.create_index('ix_context_timestamp', 'chat_contexts', ['timestamp'], unique=False)
    
    # Индексы для таблицы api_keys
    op.create_index('ix_api_keys_service_active', 'api_keys', ['service', 'is_active'], unique=False)


def downgrade():
    """Удаляем индексы"""
    op.drop_index('ix_users_credits', table_name='users')
    op.drop_index('ix_users_telegram_id_credits', table_name='users')
    op.drop_index('ix_persona_configs_owner_id', table_name='persona_configs')
    op.drop_index('ix_bot_instances_status_owner', table_name='bot_instances')
    op.drop_index('ix_bot_instances_telegram_bot_id', table_name='bot_instances')
    op.drop_index('ix_chat_bot_active', table_name='chat_bot_instances')
    op.drop_index('ix_chat_bot_instance', table_name='chat_bot_instances')
    op.drop_index('ix_chat_bot_muted', table_name='chat_bot_instances')
    op.drop_index('ix_context_chat_order', table_name='chat_contexts')
    op.drop_index('ix_context_timestamp', table_name='chat_contexts')
    op.drop_index('ix_api_keys_service_active', table_name='api_keys')
