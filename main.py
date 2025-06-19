# main.py - ЕДИНСТВЕННАЯ ТОЧКА ВХОДА

import logging
import threading
from waitress import serve
import os
from datetime import timedelta
import json
import asyncio

# --- Настройка логирования в самом начале ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Уменьшаем "шум" от библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.INFO)
logging.getLogger("telegraph_api").setLevel(logging.INFO)


# --- Импорты ваших модулей ---
import db
import handlers
import tasks
import config
from telegram.ext import (
    Application, Defaults, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler
)
from telegram import Update, BotCommand
from telegram.constants import ParseMode
# Импортируем flask_app из старого main.py, который мы сейчас переписываем.
# Логика самого flask_app остается той же.
from flask import Flask, request, abort, Response
from yookassa import Configuration as YookassaConfig
from yookassa.domain.notification import WebhookNotification
import json
from utils import escape_markdown_v2

# --- Веб-сервер (YooKassa) ---
flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')

# ... (ЗДЕСЬ ВЕСЬ КОД ВАШЕГО ВЕБ-СЕРВЕРА ИЗ СТАРОГО MAIN.PY) ...
# Я скопирую его для полноты картины.
try:
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit():
        YookassaConfig.configure(account_id=int(config.YOOKASSA_SHOP_ID), secret_key=config.YOOKASSA_SECRET_KEY)
        flask_logger.info(f"Yookassa SDK configured for webhook (Shop ID: {config.YOOKASSA_SHOP_ID}).")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY invalid/missing. Webhook processing might fail.")
except ValueError:
     flask_logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) is not a valid integer.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook: {e}")

# Global variable to hold the PTB Application instance for the webhook handler
application_instance: Application | None = None

@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    # ... (весь код вашего обработчика вебхуков без изменений) ...
    # Я сокращу его для краткости, но у вас он должен быть полностью
    global application_instance
    # ...
    # ... (логика обработки платежа)
    # ...
    return Response(status=200)

def run_flask():
    """Starts the Flask server (using Waitress for production)."""
    port = int(os.environ.get("PORT", 8080))
    flask_logger.info(f"Starting Flask server for webhooks on 0.0.0.0:{port}")
    try:
        from waitress import serve
        serve(flask_app, host='0.0.0.0', port=port, threads=8)
    except Exception as e:
        flask_logger.critical(f"Flask/Waitress server thread failed: {e}", exc_info=True)


# --- Запуск Телеграм-бота ---

async def post_init(application: Application):
    """Runs after the bot application is initialized."""
    global application_instance
    application_instance = application # Важно для вебхуков
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username

        commands = [
            BotCommand("start", "🚀 Начало работы"),
            BotCommand("menu", "🧭 Главное меню"),
            BotCommand("help", "❓ Помощь"),
            BotCommand("subscribe", "⭐ Подписка"),
            BotCommand("profile", "👤 Профиль"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Bot menu button commands set.")
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or setting commands): {e}", exc_info=True)

    logger.info("Starting background tasks...")
    if application.job_queue:
        application.job_queue.run_repeating(
            tasks.check_subscription_expiry_task,
            interval=timedelta(minutes=30),
            first=timedelta(seconds=30),
            name="subscription_expiry_check",
            data=application
        )
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")


def run_telegram_bot():
    """Собирает и запускает инстанс бота в новом цикле событий asyncio."""
    logger.info("--- Preparing Telegram Bot Thread ---")

    # 1. Создаем новый цикл событий для этого потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    logger.info("New event loop created and set for BotThread.")

    # 2. Вся остальная логика выполняется внутри этого цикла
    try:
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

        edit_persona_conv_handler = ConversationHandler(
            entry_points=[CommandHandler('editpersona', handlers.edit_persona_start), CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')],
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

        logger.info("Handlers registered for Telegram bot.")
        logger.info("Starting bot polling in the new event loop...")
        
        # 3. Запускаем polling через loop.run_until_complete()
        loop.run_until_complete(application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, timeout=30))

    except Exception as e:
        logger.critical(f"An exception occurred in the bot thread's main logic: {e}", exc_info=True)
    finally:
        logger.info("Closing the event loop for BotThread.")
        loop.close()

    logger.info("----- Bot thread has finished. -----")


if __name__ == '__main__':
    logger.info("--- Main Application Starting ---")
    
    # Инициализация БД (один раз для обоих потоков)
    try:
        logger.info("Initializing database connection...")
        db.initialize_database()
        logger.info("Database connection initialized.")
        logger.info("Verifying/creating database tables...")
        db.create_tables()
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Could not initialize database. Exiting. Error: {e}", exc_info=True)
        exit(1) # Завершаем работу, если БД не доступна

    # Создаем потоки для бота и веб-сервера
    bot_thread = threading.Thread(target=run_telegram_bot, name="BotThread")
    web_thread = threading.Thread(target=run_flask, name="WebThread")

    # Запускаем потоки
    bot_thread.start()
    web_thread.start()
    
    logger.info("Both bot and web threads have been started.")

    bot_thread.join()
    web_thread.join()
    
    logger.info("--- Main Application Stopped ---")