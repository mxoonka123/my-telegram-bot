# run_bot.py
import logging
from telegram.ext import Application, Defaults, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler
from telegram import Update, BotCommand
from telegram.constants import ParseMode, ChatType, ChatMemberStatus
from datetime import timedelta
import handlers
import db  # Импортируем db напрямую
import config
import tasks

# --- Bot Post-Initialization (копируем из main.py) ---
async def post_init(application: Application):
    """Runs after the bot application is initialized."""
    # Устанавливаем глобальный экземпляр для вебхуков, хоть он и не будет использоваться здесь
    # Это для совместимости с кодом Flask, если он вдруг понадобится
    # В идеале, эту зависимость тоже нужно убрать
    # global application_instance
    # application_instance = application
    try:
        me = await application.bot.get_me()
        logging.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username

        commands = [
            BotCommand("start", "🚀 Начало работы"),
            BotCommand("menu", "🧭 Главное меню"),
            BotCommand("help", "❓ Помощь"),
            BotCommand("subscribe", "⭐ Подписка"),
            BotCommand("profile", "👤 Профиль"),
        ]
        await application.bot.set_my_commands(commands)
        logging.info("Bot menu button commands set.")

    except Exception as e:
        logging.error(f"Failed during post_init (get_me or setting commands): {e}", exc_info=True)

    logging.info("Starting background tasks...")
    if application.job_queue:
        application.job_queue.run_repeating(
            tasks.check_subscription_expiry_task,
            interval=timedelta(minutes=30),
            first=timedelta(seconds=30),
            name="subscription_expiry_check",
            data=application
        )
        logging.info("Background tasks scheduled.")
    else:
        logging.warning("JobQueue not available, background tasks not scheduled.")

def main_bot_runner() -> None:
    """Starts the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # 1. Инициализируем базу данных
    try:
        logging.info("Initializing database connection for bot worker...")
        db.initialize_database()
        logging.info("Database connection initialized for bot worker.")
    except Exception as e:
        logging.critical(f"FATAL: Bot worker failed to initialize database connection: {e}", exc_info=True)
        return

    # 2. Создаем таблицы
    try:
        logging.info("Verifying/creating database tables for bot worker...")
        db.create_tables()
        logging.info("Database tables verified/created successfully for bot worker.")
    except Exception as e:
        logging.critical(f"FATAL: Bot worker failed to create/verify database tables: {e}", exc_info=True)
        return

    # 3. Собираем и запускаем приложение Telegram Bot
    logging.info("Initializing Telegram Bot Application for polling...")
    
    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.MARKDOWN_V2, block=False))
        .pool_timeout(20.0)
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .connection_pool_size(50)
        .post_init(post_init)
        .build()
    )
    
    # --- Conversation Handlers Definition ---
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')
        ],
        states={
            handlers.EDIT_WIZARD_MENU: [CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^edit_wizard_|^finish_edit$|^back_to_wizard_menu$|^set_max_msgs_')],
            handlers.EDIT_NAME: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_name_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_DESCRIPTION: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_description_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_COMM_STYLE: [CallbackQueryHandler(handlers.edit_comm_style_received, pattern='^set_comm_style_|^back_to_wizard_menu$')],
            handlers.EDIT_VERBOSITY: [CallbackQueryHandler(handlers.edit_verbosity_received, pattern='^set_verbosity_|^back_to_wizard_menu$')],
            handlers.EDIT_GROUP_REPLY: [CallbackQueryHandler(handlers.edit_group_reply_received, pattern='^set_group_reply_|^back_to_wizard_menu$')],
            handlers.EDIT_MEDIA_REACTION: [CallbackQueryHandler(handlers.edit_media_reaction_received, pattern='^set_media_react_|^back_to_wizard_menu$')],
            handlers.EDIT_MOODS_ENTRY: [CallbackQueryHandler(handlers.edit_moods_entry, pattern='^edit_wizard_moods$'), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_MOOD_CHOICE: [CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^editmood_add$'), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$'), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.EDIT_MOOD_NAME: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_mood_name_received), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.EDIT_MOOD_PROMPT: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_mood_prompt_received), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.DELETE_MOOD_CONFIRM: [CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'), CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')],
            handlers.EDIT_MAX_MESSAGES: [CallbackQueryHandler(handlers.edit_max_messages_received, pattern='^set_max_msgs_'), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
        },
        fallbacks=[CommandHandler('cancel', handlers.edit_persona_cancel), CallbackQueryHandler(handlers.edit_persona_finish, pattern='^finish_edit$'), CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_wizard$')],
        per_message=False, name="edit_persona_wizard", conversation_timeout=timedelta(minutes=15).total_seconds(), allow_reentry=True
    )
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('deletepersona', handlers.delete_persona_start), CallbackQueryHandler(handlers.delete_persona_button_callback, pattern=r'^delete_persona_\d+$')],
        states={handlers.DELETE_PERSONA_CONFIRM: [CallbackQueryHandler(handlers.delete_persona_confirmed, pattern='^delete_persona_confirm_'), CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')]},
        fallbacks=[CommandHandler('cancel', handlers.delete_persona_cancel), CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')],
        per_message=False, name="delete_persona_conversation", conversation_timeout=timedelta(minutes=5).total_seconds(), allow_reentry=True
    )
    
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("menu", handlers.menu_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))
    application.add_handler(CommandHandler("createpersona", handlers.create_persona))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas))
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat))
    application.add_handler(CommandHandler("mood", handlers.mood))
    application.add_handler(CommandHandler("reset", handlers.reset))
    application.add_handler(CommandHandler("clear", handlers.reset))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot))
    application.add_handler(MessageHandler(handlers.filters.PHOTO & ~handlers.filters.COMMAND, handlers.handle_photo))
    application.add_handler(MessageHandler(handlers.filters.VOICE & ~handlers.filters.COMMAND, handlers.handle_voice))
    application.add_handler(MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.handle_message))
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))
    application.add_error_handler(handlers.error_handler)
    
    logging.info("Handlers registered for bot worker.")
    
    # 4. Запускаем polling
    logging.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, timeout=30)
    logging.info("----- Bot Polling Stopped -----")

if __name__ == "__main__":
    main_bot_runner()
