import os
import psycopg2
from psycopg2 import sql
import logging
from config import DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def force_add_message_volume_column():
    try:
        # Разбираем DATABASE_URL вручную
        url_parts = DATABASE_URL.split('://')
        if len(url_parts) > 1:
            _, rest = url_parts
            conn_parts = rest.split('/')
            if len(conn_parts) > 1:
                credentials, database = conn_parts
                cred_parts = credentials.split('@')
                if len(cred_parts) > 1:
                    username_password, host_port = cred_parts
                    username, password = username_password.split(':')
                    host, port = host_port.split(':')
                    
                    conn_params = {
                        'dbname': database,
                        'user': username,
                        'password': password,
                        'host': host,
                        'port': port
                    }
                    
                    # Устанавливаем соединение
                    with psycopg2.connect(**conn_params) as conn:
                        conn.autocommit = True
                        with conn.cursor() as cur:
                            # Принудительное добавление столбца
                            cur.execute("""
                            DO $$
                            BEGIN
                                BEGIN
                                    ALTER TABLE persona_configs 
                                    ADD COLUMN message_volume VARCHAR(20);
                                EXCEPTION WHEN duplicate_column THEN
                                    RAISE NOTICE 'Column message_volume already exists';
                                END;

                                -- Обновляем существующие записи
                                UPDATE persona_configs 
                                SET message_volume = 'normal' 
                                WHERE message_volume IS NULL;
                                
                                -- Устанавливаем ограничения NOT NULL и DEFAULT
                                ALTER TABLE persona_configs 
                                ALTER COLUMN message_volume SET DEFAULT 'normal';
                                
                                ALTER TABLE persona_configs 
                                ALTER COLUMN message_volume SET NOT NULL;
                            END $$;
                            """)
                            
                            logger.info("Forced migration of message_volume column completed successfully")
    
    except Exception as e:
        logger.error(f"Error in forced migration: {e}")

if __name__ == '__main__':
    force_add_message_volume_column()
