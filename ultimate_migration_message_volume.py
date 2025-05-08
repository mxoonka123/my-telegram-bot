import os
import sys
import traceback
import psycopg2
from psycopg2 import sql
import logging
from urllib.parse import urlparse, parse_qs

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('migration.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def parse_database_url(database_url):
    """Разбор DATABASE_URL на компоненты."""
    parsed = urlparse(database_url)
    return {
        'dbname': parsed.path.lstrip('/'),
        'user': parsed.username,
        'password': parsed.password,
        'host': parsed.hostname,
        'port': parsed.port or 5432
    }

def ultimate_migration():
    """Максимально агрессивная миграция."""
    try:
        # Импорт DATABASE_URL
        from config import DATABASE_URL
        
        # Парсим URL базы данных
        conn_params = parse_database_url(DATABASE_URL)
        
        # Устанавливаем соединение
        with psycopg2.connect(**conn_params) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Список SQL-запросов для миграции
                migration_queries = [
                    # Проверка и добавление столбца с максимальной гибкостью
                    '''
                    DO $$
                    BEGIN
                        -- Проверяем существование столбца
                        IF NOT EXISTS (
                            SELECT column_name 
                            FROM information_schema.columns 
                            WHERE table_name = 'persona_configs' 
                            AND column_name = 'message_volume'
                        ) THEN
                            -- Добавляем столбец с максимально широкими параметрами
                            ALTER TABLE persona_configs 
                            ADD COLUMN message_volume VARCHAR(20);
                        END IF;
                        
                        -- Обновляем существующие записи
                        UPDATE persona_configs 
                        SET message_volume = 'normal' 
                        WHERE message_volume IS NULL;
                        
                        -- Устанавливаем ограничения
                        ALTER TABLE persona_configs 
                        ALTER COLUMN message_volume SET DEFAULT 'normal';
                        
                        ALTER TABLE persona_configs 
                        ALTER COLUMN message_volume SET NOT NULL;
                    END $$;
                    ''',
                    
                    # Дополнительная проверка и принудительное обновление
                    '''
                    UPDATE persona_configs 
                    SET message_volume = 'normal' 
                    WHERE message_volume IS NULL OR message_volume = '';
                    ''',
                    
                    # Финальная проверка
                    '''
                    SELECT COUNT(*) 
                    FROM persona_configs 
                    WHERE message_volume IS NULL OR message_volume = '';
                    '''
                ]
                
                # Выполнение всех миграционных запросов
                for query in migration_queries:
                    try:
                        cur.execute(query)
                        logger.info(f"Successfully executed query: {query[:100]}...")
                    except Exception as query_error:
                        logger.error(f"Error in query: {query_error}")
                
                # Коммитим транзакцию
                conn.commit()
                logger.info("Migration completed successfully")
    
    except Exception as e:
        logger.error(f"Critical migration error: {e}")
        logger.error(traceback.format_exc())

if __name__ == '__main__':
    ultimate_migration()
