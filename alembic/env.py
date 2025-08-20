from __future__ import annotations
import os
import sys
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Добавляем корень проекта в sys.path, чтобы импортировать db.py и config.py
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Импортируем метаданные моделей и конфиг БД
from db import Base  # noqa: E402
import config as app_config  # noqa: E402

# Это Alembic Config объект, предоставляет доступ к значениям в .ini файле
alembic_config = context.config

# Настройка логов Alembic из ini-файла, если он указан
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# Метаданные моделей для автогенерации миграций
target_metadata = Base.metadata

# Получаем URL из приложения и принудительно используем psycopg (v3)
DATABASE_URL = app_config.DATABASE_URL
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if DATABASE_URL:
    alembic_config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """Запуск миграций в оффлайн-режиме.

    Формируются SQL-выражения, без создания Engine.
    """
    url = alembic_config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Запуск миграций в онлайн-режиме.

    Создаётся Engine и соединение.
    """
    connectable = engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
