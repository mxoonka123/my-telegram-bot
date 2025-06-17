# run_bot.py
import logging
from telegram.ext import Application
from telegram import Update
import handlers
import main as main_app  # Импортируем для вызова функций инициализации

def main() -> None:
    """Starts the bot."""
    # 1. Инициализируем базу данных (как в main.py)
    try:
        logging.info("Initializing database connection for bot worker...")
        main_app.db.initialize_database()
        logging.info("Database connection initialized.")
    except Exception as e:
        logging.critical(f"FATAL: Failed to initialize database connection: {e}", exc_info=True)
        return

    # 2. Создаем таблицы (как в main.py)
    try:
        logging.info("Verifying/creating database tables for bot worker...")
        main_app.db.create_tables()
        logging.info("Database tables verified/created successfully.")
    except Exception as e:
        logging.critical(f"FATAL: Failed to create/verify database tables: {e}", exc_info=True)
        return

    # 3. Собираем и запускаем приложение Telegram Bot (код взят из вашего main.py)
    logging.info("Initializing Telegram Bot Application for polling...")
    
    # Создаем экземпляр ApplicationBuilder, как в вашем main.py
    application = (
        Application.builder()
        .token(main_app.TELEGRAM_TOKEN)
        .defaults(main_app.Defaults(parse_mode=main_app.ParseMode.MARKDOWN_V2, block=False))
        .pool_timeout(20.0)
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .connection_pool_size(50)
        .post_init(main_app.post_init) # post_init устанавливает команды и т.д.
        .build()
    )
    
    # Копируем регистрацию всех хендлеров из вашего main.py
    # Conversation Handlers
    application.add_handler(main_app.edit_persona_conv_handler)
    application.add_handler(main_app.delete_persona_conv_handler)
    
    # Basic Commands
    application.add_handler(main_app.CommandHandler("start", handlers.start))
    application.add_handler(main_app.CommandHandler("help", handlers.help_command))
    application.add_handler(main_app.CommandHandler("menu", handlers.menu_command))
    application.add_handler(main_app.CommandHandler("profile", handlers.profile))
    application.add_handler(main_app.CommandHandler("subscribe", handlers.subscribe))

    # Persona Management Commands
    application.add_handler(main_app.CommandHandler("createpersona", handlers.create_persona))
    application.add_handler(main_app.CommandHandler("mypersonas", handlers.my_personas))

    # In-Chat Commands
    application.add_handler(main_app.CommandHandler("addbot", handlers.add_bot_to_chat))
    application.add_handler(main_app.CommandHandler("mood", handlers.mood))
    application.add_handler(main_app.CommandHandler("reset", handlers.reset))
    application.add_handler(main_app.CommandHandler("clear", handlers.reset))
    application.add_handler(main_app.CommandHandler("mutebot", handlers.mute_bot))
    application.add_handler(main_app.CommandHandler("unmutebot", handlers.unmute_bot))
    
    # Message Handlers
    application.add_handler(main_app.MessageHandler(main_app.filters.PHOTO & ~main_app.filters.COMMAND, handlers.handle_photo))
    application.add_handler(main_app.MessageHandler(main_app.filters.VOICE & ~main_app.filters.COMMAND, handlers.handle_voice))
    application.add_handler(main_app.MessageHandler(main_app.filters.TEXT & ~main_app.filters.COMMAND, handlers.handle_message))
    
    # General Callback Query Handler
    application.add_handler(main_app.CallbackQueryHandler(handlers.handle_callback_query))
    
    # Error Handler
    application.add_error_handler(handlers.error_handler)
    
    logging.info("Handlers registered for bot worker.")
    
    # 4. Запускаем polling
    logging.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=30,
    )
    logging.info("----- Bot Polling Stopped -----")

if __name__ == "__main__":
    main()
