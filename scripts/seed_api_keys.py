import logging
from typing import List

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

# Локальные импорты проекта
import db
from db import ApiKey, get_db, initialize_database, create_tables

logger = logging.getLogger("seed_api_keys")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GEMINI_KEYS: List[str] = [
    "AIzaSyDSTA6MCjwPs_wXJW9ADQXiCHG76NmwaRg",
    "AIzaSyBSIsUJ46ph5o0pZ63fjdiw00nLpFdSsWA",
    "AIzaSyDF1N82APSK_JAZ5oQT_zTqXqAbkj_d6II",
    "AIzaSyCoByqcLLlgCfhsMooPk5ZA-AVXJARNNhs",
    "AIzaSyAUX0SH8EA7uvun_Mn8E1j2pUhlD8W8IJk",
    "AIzaSyBWXHj8OZcgm-drwa4tMI35iM35uDcDycI",
    "AIzaSyDpbbrB3RoxOCKXAIG9AlK8-uexIvJgb_4",
    "AIzaSyAc4VP4JMvJE6w5GOG9aDlh8mdieOcic7o",
    "AIzaSyDs8n5zeXKUsf4SG0K9RacvVvkkGJoCsa0",
    "AIzaSyDV_zYm2xQcGJ2SMLlqnP7dSaaWnpXni38",
    "AIzaSyBfa9EUg3-8QYNFJhargC68MxwqqdMbuE0",
]

SERVICE_NAME = "gemini"
DEFAULT_COMMENT = "seeded via scripts/seed_api_keys.py"


def upsert_gemini_keys(keys: List[str]) -> None:
    """Добавляет в БД недостающие ключи Gemini. Уже существующие пропускает.
    Безопасно к множественным запускам.
    """
    if not keys:
        logger.info("no keys provided, nothing to do")
        return

    with get_db() as session:
        added, skipped = 0, 0
        for raw_key in keys:
            key = (raw_key or "").strip()
            if not key:
                continue
            try:
                # Быстрая проверка наличия
                exists = session.query(ApiKey).filter(ApiKey.api_key == key).first()
                if exists:
                    if not exists.is_active:
                        exists.is_active = True
                        session.add(exists)
                    skipped += 1
                    continue

                entity = ApiKey(
                    service=SERVICE_NAME,
                    api_key=key,
                    is_active=True,
                    comment=DEFAULT_COMMENT,
                )
                session.add(entity)
                session.flush()  # ранняя проверка уникальности
                added += 1
            except IntegrityError:
                session.rollback()
                skipped += 1
            except SQLAlchemyError as e:
                session.rollback()
                logger.error(f"db error while inserting api key ...{key[-6:]}: {e}")
                # продолжаем остальные ключи
        try:
            session.commit()
        except Exception as e:
            logger.error(f"commit failed: {e}")
            session.rollback()
        logger.info(f"Done. Added: {added}, skipped(existing): {skipped}")


def main() -> None:
    # Инициализируем БД и таблицы, если не созданы
    initialize_database()
    create_tables()
    upsert_gemini_keys(GEMINI_KEYS)


if __name__ == "__main__":
    main()
