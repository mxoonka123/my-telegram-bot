import logging
import asyncio
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler
)

import config
import db
import handlers
import tasks

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Обработчик ошибок ---
# УДАЛИТЬ ЭТУ ФУНКЦИЮ ИЗ main.py:
# async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
#    logger.error("Exception while handling an update:", exc_info=context.error)

# ... остальной код main.py без изменений ...

async def post_init(application: Application):
    me = await application.bot.get_me()
    global BOT_USERNAME
    BOT_USERNAME = me.username
    logger.info(f"Bot started as @{BOT_USERNAME}")

    asyncio.create_task(tasks.spam_task(application))


def main() -> None:
    logger.info("Creating database tables if they don't exist...")
    db.create_tables()
    logger.info("Database setup complete.")

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))

    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))

    application.add_handler(CommandHandler("mood", handlers.mood, block=False))

    application.add_handler(CommandHandler("reset", handlers.reset, block=False))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query, block=False))

    # --- Добавляем обработчик ошибок (уже импортирован из handlers) ---
    application.add_error_handler(handlers.error_handler) # <-- Эта строка остается

    application.post_init = post_init

    logger.info("Starting bot polling...")
    application.run_polling(poll_interval=1)

if __name__ == "__main__":
    main()