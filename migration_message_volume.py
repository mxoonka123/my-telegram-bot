import os
import psycopg2
from psycopg2 import sql
import logging
from config import DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def add_message_volume_column():
    try:
        # Parse DATABASE_URL
        conn_params = {}
        url_parts = DATABASE_URL.split('://')
        if len(url_parts) > 1:
            protocol, rest = url_parts
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
        
        # Establish connection
        with psycopg2.connect(**conn_params) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Check if column exists
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='persona_configs' 
                        AND column_name='message_volume'
                    )
                """)
                column_exists = cur.fetchone()[0]
                
                if not column_exists:
                    # Add column
                    cur.execute("""
                        ALTER TABLE persona_configs 
                        ADD COLUMN message_volume VARCHAR(20) DEFAULT 'normal' NOT NULL
                    """)
                    logger.info("Added message_volume column to persona_configs")
                
                # Update existing rows
                cur.execute("""
                    UPDATE persona_configs 
                    SET message_volume = 'normal' 
                    WHERE message_volume IS NULL OR message_volume = ''
                """)
                logger.info("Updated existing rows with default message_volume")
    
    except Exception as e:
        logger.error(f"Error in migration: {e}")

if __name__ == '__main__':
    add_message_volume_column()
