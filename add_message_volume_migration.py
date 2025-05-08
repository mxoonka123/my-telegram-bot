from sqlalchemy import create_engine, Column, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from config import DATABASE_URL
from db import Base, engine

def add_message_volume_column():
    try:
        # Create a session
        Session = sessionmaker(bind=engine)
        session = Session()

        # Execute raw SQL to add column if not exists
        session.execute(text('''
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='persona_configs' AND column_name='message_volume'
                ) THEN
                    ALTER TABLE persona_configs 
                    ADD COLUMN message_volume VARCHAR(20) DEFAULT 'normal' NOT NULL;
                END IF;
            END $$;
        '''))
        
        # Update existing rows to have default value
        session.execute(text('''
            UPDATE persona_configs 
            SET message_volume = 'normal' 
            WHERE message_volume IS NULL;
        '''))
        
        # Commit the changes
        session.commit()
        print('Successfully added message_volume column to persona_configs table.')
    
    except SQLAlchemyError as e:
        print(f'Error adding message_volume column: {e}')
        session.rollback()
    finally:
        session.close()

if __name__ == '__main__':
    add_message_volume_column()
