import os
import sys
import psycopg2
import logging
from urllib.parse import urlparse

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

# Импортируем DATABASE_URL
from config import DATABASE_URL

def parse_database_url(url):
    """Разбор URL базы данных на компоненты."""
    parsed = urlparse(url)
    return {
        'dbname': parsed.path.lstrip('/'),
        'user': parsed.username,
        'password': parsed.password,
        'host': parsed.hostname,
        'port': parsed.port or 5432
    }

def run_migration():
    """Выполнение миграции для добавления столбца message_volume."""
    try:
        # Парсим URL базы данных
        conn_params = parse_database_url(DATABASE_URL)
        logger.info(f"Connecting to database: {conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}")
        
        # Устанавливаем соединение
        with psycopg2.connect(**conn_params) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Проверяем существование столбца
                cur.execute("""
                    SELECT EXISTS (
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name='persona_configs' AND column_name='message_volume'
                    )
                """)
                column_exists = cur.fetchone()[0]
                
                if column_exists:
                    logger.info("Column message_volume already exists. Skipping migration.")
                else:
                    # Добавляем столбец
                    logger.info("Adding message_volume column...")
                    cur.execute("""
                        ALTER TABLE persona_configs 
                        ADD COLUMN message_volume VARCHAR(20) DEFAULT 'normal' NOT NULL
                    """)
                    logger.info("Column message_volume added successfully.")
                
                # Обновляем существующие записи
                cur.execute("""
                    UPDATE persona_configs 
                    SET message_volume = 'normal' 
                    WHERE message_volume IS NULL OR message_volume = ''
                """)
                logger.info("Updated existing rows with default message_volume value.")
                
                # Проверяем результат
                cur.execute("SELECT COUNT(*) FROM persona_configs")
                total_rows = cur.fetchone()[0]
                
                cur.execute("SELECT COUNT(*) FROM persona_configs WHERE message_volume = 'normal'")
                updated_rows = cur.fetchone()[0]
                
                logger.info(f"Migration completed. Total rows: {total_rows}, Updated rows: {updated_rows}")
                
                # Выводим структуру таблицы для проверки
                cur.execute("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_name = 'persona_configs'
                    ORDER BY ordinal_position
                """)
                columns = cur.fetchall()
                logger.info("Table structure after migration:")
                for col in columns:
                    logger.info(f"Column: {col[0]}, Type: {col[1]}, Nullable: {col[2]}, Default: {col[3]}")
    
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == '__main__':
    run_migration()
