#!/usr/bin/env python3
"""Проверка состояния миграций Alembic"""

from alembic.config import Config
from alembic import command
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_migrations():
    """Проверяет состояние миграций"""
    try:
        alembic_cfg = Config("alembic.ini")
        
        print("\n=== ТЕКУЩИЕ HEADS ===")
        command.heads(alembic_cfg, verbose=True)
        
        print("\n=== ИСТОРИЯ МИГРАЦИЙ ===")
        command.history(alembic_cfg, verbose=True)
        
        print("\n=== ТЕКУЩАЯ ВЕРСИЯ В БД ===")
        command.current(alembic_cfg, verbose=True)
        
        print("\n=== ВЕТКИ ===")
        command.branches(alembic_cfg, verbose=True)
        
    except Exception as e:
        logger.error(f"Ошибка при проверке миграций: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_migrations()
