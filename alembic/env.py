from __future__ import annotations
import os
import sys
from logging.config import fileConfig
import logging
import time
from sqlalchemy import engine_from_config, pool, create_engine
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
    logger = logging.getLogger("alembic.env")
    # Строим engine вручную, чтобы передать connect_args как в приложении
    engine_kwargs = {
        "poolclass": pool.NullPool,
    }
    connect_args = {}
    if DATABASE_URL and DATABASE_URL.startswith("postgresql+psycopg://"):
        # Параметры для psycopg3 — те же, что и в db.initialize_database()
        connect_args["prepare_threshold"] = None  # отключаем prepared statements
        connect_args["options"] = "-c statement_timeout=60000 -c idle_in_transaction_session_timeout=60000"
        # Используем общий конфиг приложения: DB_CONNECT_TIMEOUT (секунды)
        try:
            connect_args["connect_timeout"] = int(getattr(app_config, "DB_CONNECT_TIMEOUT", 60))
        except Exception:
            connect_args["connect_timeout"] = 60
        engine_kwargs["connect_args"] = connect_args

    connectable = create_engine(DATABASE_URL, **engine_kwargs)

    # Пытаемся подключиться с ретраями (например, если БД ещё не готова)
    max_attempts = 5
    delay = 2.0
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    compare_type=True,
                    compare_server_default=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
                last_err = None
                break
        except Exception as e:
            last_err = e
            logger.warning(f"Alembic: DB connect attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(delay)
                delay = min(delay * 2, 15.0)  # экспоненциальная задержка до 15с
            else:
                logger.error("Alembic: all reconnection attempts failed.")
                raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
