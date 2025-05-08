import logging
from sqlalchemy import create_engine, Column, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
import psycopg
from config import DATABASE_URL
from db import Base, PersonaConfig, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def add_message_volume_column():
    try:
        # Create a session
        Session = sessionmaker(bind=engine)
        session = Session()

        # Add the new column with a default value
        engine.execute('ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS message_volume VARCHAR(20) DEFAULT \'normal\' NOT NULL')
        
        logger.info("Successfully added message_volume column to persona_configs table.")
        
        # Commit the changes
        session.commit()
    except SQLAlchemyError as e:
        logger.error(f"Error adding message_volume column: {e}")
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    add_message_volume_column()
