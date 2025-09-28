"""Add performance indexes v2

Revision ID: performance_indexes_v2
Revises: api_keys_table
Create Date: 2025-09-28

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'performance_indexes_v2'
down_revision = '20250928_performance'  # Исправлено: правильная ссылка на предыдущую миграцию
branch_labels = None
depends_on = None


def upgrade():
    """Добавляем индексы для оптимизации производительности"""
    
    # Индекс для быстрого поиска пользователей по telegram_id (если еще нет)
    op.create_index(
        'ix_users_telegram_id_credits', 
        'users', 
        ['telegram_id', 'credits'],
        if_not_exists=True
    )
    
    # Составной индекс для persona_configs
    op.create_index(
        'ix_persona_configs_owner_id_name',
        'persona_configs',
        ['owner_id', 'name'],
        if_not_exists=True
    )
    
    # Индекс для bot_instances
    op.create_index(
        'ix_bot_instances_persona_config_id_status',
        'bot_instances',
        ['persona_config_id', 'status'],
        if_not_exists=True
    )
    
    # Индекс для chat_bot_instances
    op.create_index(
        'ix_chat_bot_instances_chat_id_bot_instance_id_active',
        'chat_bot_instances',
        ['chat_id', 'bot_instance_id', 'active'],
        if_not_exists=True
    )
    
    # Индекс для chat_contexts
    op.create_index(
        'ix_chat_contexts_chat_bot_instance_id_message_order',
        'chat_contexts',
        ['chat_bot_instance_id', 'message_order'],
        if_not_exists=True
    )


def downgrade():
    """Удаляем индексы"""
    op.drop_index('ix_users_telegram_id_credits', 'users')
    op.drop_index('ix_persona_configs_owner_id_name', 'persona_configs')
    op.drop_index('ix_bot_instances_persona_config_id_status', 'bot_instances')
    op.drop_index('ix_chat_bot_instances_chat_id_bot_instance_id_active', 'chat_bot_instances')
    op.drop_index('ix_chat_contexts_chat_bot_instance_id_message_order', 'chat_contexts')
