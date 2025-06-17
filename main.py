# --- RADICAL DIAGNOSTICS main.py ---
import logging
import sys
import os
import asyncio
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. НЕУБИВАЕМОЕ ЛОГИРОВАНИЕ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

logger.info("--- DIAGNOSTICS: main.py execution started ---")

# 2. ПРОВЕРКА КЛЮЧЕВЫХ ПЕРЕМЕННЫХ
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    logger.critical("--- DIAGNOSTICS: CRITICAL ERROR: TELEGRAM_TOKEN is NOT SET! ---")
    sys.exit(1) # Завершаем с ошибкой, чтобы Railway показал сбой

# ... остальной код ...
        logger.debug(f"Telegraph payload (first 500 chars of content): { {k: (v[:500] + '...' if k=='content' and isinstance(v, str) and len(v) > 500 else v) for k, v in payload.items()} }")

        async with httpx.AsyncClient() as client:
            response = await client.post(telegraph_api_url, json=payload)

        logger.info(f"Telegraph API direct response status: {response.status_code}")
        response_data = response.json()
        logger.debug(f"Telegraph API direct response JSON: {response_data}")

        if response_data.get("ok"):
            result = response_data.get("result")
            if result and isinstance(result, dict) and result.get("url"):
                page_url = result["url"]
                logger.info(f"Successfully created/updated Telegra.ph page via direct request: {page_url}")
            else:
                logger.error(f"Telegraph API direct request OK=true, but result invalid/missing url. Result: {result}")
        else:
            error_message = response_data.get("error", "Unknown error")
            logger.error(f"Telegraph API direct request returned error: {error_message}")
            if "CONTENT_INVALID" in error_message or "PAGE_SAVE_FAILED" in error_message:
                 logger.error(f">>> Received error possibly related to content format! Check payload JSON structure or content.")
                 logger.debug(f"Content JSON sent: {content_json_string}")
            elif "ACCESS_TOKEN_INVALID" in error_message:
                 logger.error(">>> TELEGRAPH_ACCESS_TOKEN is invalid!")
            response.raise_for_status() # Raise HTTP errors for non-OK responses

    except httpx.HTTPStatusError as http_err:
         logger.error(f"HTTP Status error during direct Telegraph request: {http_err.response.status_code} - {http_err.response.text}", exc_info=False)
    except httpx.RequestError as http_err:
         logger.error(f"HTTPX network error during direct Telegraph request: {http_err}", exc_info=True)
    except json.JSONDecodeError as json_err:
         logger.error(f"Failed to dump content_node_array to JSON or decode response: {json_err}", exc_info=True)
    except Exception as e:
         logger.error(f"Unexpected error during direct Telegra.ph page creation: {e}", exc_info=True)

    if page_url:
        application.bot_data['tos_url'] = page_url
        logger.info(f"Final ToS URL set in bot_data: {page_url}")
    else:
        logger.error("Failed to obtain Telegra.ph page URL using direct request.")


# --- Bot Post-Initialization ---
async def post_init(application: Application):
    """Runs after the bot application is initialized."""
    global application_instance
    application_instance = application
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username

        # --- Устанавливаем кнопку меню --- 
        commands = [
            BotCommand("start", "🚀 Начало работы"),
            BotCommand("menu", "🧭 Главное меню"),
            BotCommand("help", "❓ Помощь"),
            BotCommand("subscribe", "⭐ Подписка"),
            BotCommand("profile", "👤 Профиль"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Bot menu button commands set.")
        # --- Конец установки кнопки меню --- 

        # Schedule Telegraph setup after getting bot info
        asyncio.create_task(setup_telegraph_page(application))
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or setting commands or scheduling setup_telegraph): {e}", exc_info=True)

    logger.info("Starting background tasks...")
    if application.job_queue:
        # Run subscription check reasonably often
        application.job_queue.run_repeating(
            tasks.check_subscription_expiry_task,
            interval=timedelta(minutes=30),
            first=timedelta(seconds=30),
            name="subscription_expiry_check",
            data=application # Pass application instance to the task if needed
        )
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")


# --- Main Function ---
def main():
    """Starts the bot, Flask server, and background tasks."""
    global application_instance

    # Initialize database connection FIRST
    try:
        logger.info("Initializing database connection...")
        db.initialize_database() # Initialize the engine
        logger.info("Database connection initialized.")
    except Exception as e:
        logger.critical(f"FATAL: Failed to initialize database connection: {e}", exc_info=True)
        logger.critical("Bot cannot start without a working database connection. EXITING.")
        # Завершаем процесс с кодом ошибки 1, чтобы Railway точно зарегистрировал сбой.
        sys.exit(1)

    # THEN create tables
    try:
        logger.info("Attempting to create database tables (if they don't exist)...")
        db.create_tables()  # Now engine should be available
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Failed to create/verify database tables: {e}", exc_info=True)
        logger.critical("Bot cannot start if table creation/verification fails. Exiting.")
        return # Exit if table creation fails

    logger.info("----- Bot Starting -----")

    # The following block for db.initialize_database() is now redundant as it's called above.
    # We can remove it or ensure it doesn't cause issues if called декоративно.
    # For now, let's comment it out to avoid double initialization or confusion.
    # try:
    #     logger.info("Re-Verifying database connection (already initialized)...")
    #     # db.initialize_database() # Already called
    #     logger.info("Database connection re-verified.")
    # except Exception as e:
    #     logger.warning(f"Issue during re-verification of DB connection (should be harmless): {e}", exc_info=True)



    # --- Start Flask Webhook Server ---
    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    # --- Initialize Telegram Bot ---
    logger.info("Initializing Telegram Bot Application...")
    token = TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config or environment. Exiting.")
        return

    # Sensible defaults
    bot_defaults = Defaults(
        parse_mode=ParseMode.MARKDOWN_V2,
        block=False # Run handlers concurrently by default
    )

        # Configure the application with timeouts directly
    application = (
        Application.builder()
        .token(token)
        .defaults(bot_defaults)
        # Увеличиваем таймауты для большей стабильности на Railway
        .pool_timeout(20.0)      # Время ожидания соединения из пула
        .connect_timeout(20.0)   # Таймаут на установку соединения
        .read_timeout(30.0)      # Таймаут на чтение ответа
        .write_timeout(30.0)     # Таймаут на отправку запроса
        .connection_pool_size(50) # Увеличиваем размер пула соединений
        .post_init(post_init)
        .build()
    )
    logger.info("Telegram Application built.")

    # --- Conversation Handlers Definition ---

    # Edit Persona Wizard Conversation Handler
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')
        ],
        states={
            handlers.EDIT_WIZARD_MENU: [CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^edit_wizard_|^finish_edit$|^back_to_wizard_menu$|^set_max_msgs_')],
            handlers.EDIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_name_received),
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$') # ADDED
            ],
            handlers.EDIT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_description_received),
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$') # ADDED
            ],
            handlers.EDIT_COMM_STYLE: [CallbackQueryHandler(handlers.edit_comm_style_received, pattern='^set_comm_style_|^back_to_wizard_menu$')],
            handlers.EDIT_VERBOSITY: [CallbackQueryHandler(handlers.edit_verbosity_received, pattern='^set_verbosity_|^back_to_wizard_menu$')],
            handlers.EDIT_GROUP_REPLY: [CallbackQueryHandler(handlers.edit_group_reply_received, pattern='^set_group_reply_|^back_to_wizard_menu$')],
            handlers.EDIT_MEDIA_REACTION: [CallbackQueryHandler(handlers.edit_media_reaction_received, pattern='^set_media_react_|^back_to_wizard_menu$')],
            handlers.EDIT_MOODS_ENTRY: [
                CallbackQueryHandler(handlers.edit_moods_entry, pattern='^edit_wizard_moods$'),
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')
            ],
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^editmood_add$'),
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$'),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')
            ],
            handlers.EDIT_MOOD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.EDIT_MOOD_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.DELETE_MOOD_CONFIRM: [CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            # Add the state for handling max messages selection
            handlers.EDIT_MAX_MESSAGES: [
                CallbackQueryHandler(handlers.edit_max_messages_received, pattern='^set_max_msgs_'), # Обработка выбора
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$') # Кнопка Назад
            ],
            # Add the state for handling message volume selection
            # handlers.EDIT_MESSAGE_VOLUME: [
            #     CallbackQueryHandler(handlers.edit_message_volume_received, pattern='^set_volume_|^back_to_wizard_menu$') # Handle volume selection and back button
            # ]
        },
        fallbacks=[ # Общие точки выхода из диалога
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_finish, pattern='^finish_edit$'),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_wizard$'),
        ],
        per_message=False,
        name="edit_persona_wizard",
        conversation_timeout=timedelta(minutes=15).total_seconds(),
        allow_reentry=True  # Разрешить повторный вход, даже если предыдущий диалог все еще активен
    )

    # Delete Persona Conversation Handler
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('deletepersona', handlers.delete_persona_start),
            CallbackQueryHandler(handlers.delete_persona_button_callback, pattern=r'^delete_persona_\d+$')  # Матчит только delete_persona_123, но не delete_persona_confirm_123
            ],
        states={
            handlers.DELETE_PERSONA_CONFIRM: [
                CallbackQueryHandler(handlers.delete_persona_confirmed, pattern='^delete_persona_confirm_'),
                CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')
                ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.delete_persona_cancel),
            CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')
            ],
        per_message=False,
        name="delete_persona_conversation",
        conversation_timeout=timedelta(minutes=5).total_seconds(),
        allow_reentry=True  # Разрешить повторный вход, даже если предыдущий диалог все еще активен
    )
    logger.info("Conversation Handlers configured.")

    # --- Register Handlers ---
    logger.info("Registering handlers...")

    # Basic Commands
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("menu", handlers.menu_command))
    # application.add_handler(CommandHandler('editpersona', handlers.edit_persona_start))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))
    # --- REMOVED placeholders command ---
    # application.add_handler(CommandHandler("placeholders", handlers.placeholders_command))

    # Persona Management Commands
    application.add_handler(CommandHandler("createpersona", handlers.create_persona))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas))

    # Add Conversation Handlers
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)

    # In-Chat Commands
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat))
    application.add_handler(CommandHandler("mood", handlers.mood))
    application.add_handler(CommandHandler("reset", handlers.reset))
    application.add_handler(CommandHandler("clear", handlers.reset)) # Теперь /clear - это псевдоним для /reset
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot))

    # Message Handlers (ensure correct filters and order)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice))
    # Text handler should be after specific media handlers if they are exclusive
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))

    # General Callback Query Handler (for buttons *not* in conversations)
    # Ensure this is added *after* ConversationHandlers if patterns might overlap
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler (should be last)
    application.add_error_handler(handlers.error_handler)

    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    print("DEBUG: main.py - Inside main(), before application.run_polling()")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True, # Good for development, consider False for production if needed
        timeout=30, # Increase polling timeout
    )
    logger.info("----- Bot Stopped -----")

if __name__ == "__main__":
    main()
