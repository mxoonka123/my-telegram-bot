"""Add performance indexes and remove deprecated fields

Revision ID: performance_001
Revises: 20250820_200500_acl_credits
Create Date: 2025-09-28 18:45:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'performance_001'
down_revision = '20250820_200500_acl_credits'
branch_labels = None
depends_on = None


def upgrade():
    # Добавляем критически важные индексы для производительности
    
    # Индексы для chat_bot_instances - самые важные для навигации
    op.create_index('ix_chat_bot_active', 'chat_bot_instances', ['chat_id', 'active'])
    op.create_index('ix_chat_bot_instance', 'chat_bot_instances', ['bot_instance_id', 'active'])
    op.create_index('ix_chat_bot_muted', 'chat_bot_instances', ['chat_id', 'is_muted'])
    
    # Индексы для chat_contexts - ускорение загрузки истории
    op.create_index('ix_context_chat_order', 'chat_contexts', ['chat_bot_instance_id', 'message_order'])
    op.create_index('ix_context_timestamp', 'chat_contexts', ['chat_bot_instance_id', 'timestamp'])
    
    # Индексы для bot_instances
    op.create_index('ix_bot_instance_token', 'bot_instances', ['bot_token'])
    op.create_index('ix_bot_instance_telegram_id', 'bot_instances', ['telegram_bot_id'])
    op.create_index('ix_bot_instance_status', 'bot_instances', ['status', 'owner_id'])
    
    # Индексы для persona_configs
    op.create_index('ix_persona_owner_name', 'persona_configs', ['owner_id', 'name'])
    
    # Индексы для api_keys
    op.create_index('ix_api_key_service_active', 'api_keys', ['service', 'is_active', 'last_used_at'])
    
    # Удаляем DEPRECATED колонки из таблицы users
    op.drop_column('users', 'daily_message_count')
    op.drop_column('users', 'last_message_reset')
    
    # Добавляем колонку для кеширования промптов
    op.add_column('persona_configs',
        sa.Column('cached_system_prompt', sa.Text(), nullable=True)
    )
    op.add_column('persona_configs',
        sa.Column('cache_updated_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    # Удаляем индексы
    op.drop_index('ix_chat_bot_active', 'chat_bot_instances')
    op.drop_index('ix_chat_bot_instance', 'chat_bot_instances')
    op.drop_index('ix_chat_bot_muted', 'chat_bot_instances')
    op.drop_index('ix_context_chat_order', 'chat_contexts')
    op.drop_index('ix_context_timestamp', 'chat_contexts')
    op.drop_index('ix_bot_instance_token', 'bot_instances')
    op.drop_index('ix_bot_instance_telegram_id', 'bot_instances')
    op.drop_index('ix_bot_instance_status', 'bot_instances')
    op.drop_index('ix_persona_owner_name', 'persona_configs')
    op.drop_index('ix_api_key_service_active', 'api_keys')
    
    # Восстанавливаем удаленные колонки
    op.add_column('users',
        sa.Column('daily_message_count', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column('users',
        sa.Column('last_message_reset', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )
    
    # Удаляем добавленные колонки
    op.drop_column('persona_configs', 'cached_system_prompt')
    op.drop_column('persona_configs', 'cache_updated_at')
