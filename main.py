# main.py

import logging
import asyncio
import os # Добавили os
import threading # Добавили threading
from datetime import timedelta
from flask import Flask, request, abort, Response # Добавили Flask
from yookassa import Configuration as YookassaConfig # Переименовали, чтобы не конфликтовать с Flask app.config
from yookassa.domain.notification import WebhookNotification # Добавили WebhookNotification
import json # Добавили json
from sqlalchemy.exc import SQLAlchemyError # Добавили SQLAlchemyError

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler
)

# --- Импорты из твоего проекта ---
import config
import db
import handlers
import tasks

# --- Настройка Flask App ---
flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook') # Отдельный логгер для Flask

# --- Инициализация Yookassa для вебхуков ---
try:
    if config.YOOKASSA_SECRET_KEY:
        YookassaConfig.configure(None, config.YOOKASSA_SECRET_KEY)
        flask_logger.info("Yookassa SDK configured for webhook handler.")
    else:
        flask_logger.warning("YOOKASSA_SECRET_KEY not found for webhook handler.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook handler: {e}")

# --- Обработчик вебхуков Yookassa (почти как в webhook_server.py) ---
@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    flask_logger.info("Received request on /yookassa/webhook")
    try:
        request_body = request.get_data(as_text=True)
        flask_logger.info(f"Webhook body: {request_body[:500]}...")

        notification = WebhookNotification(json.loads(request_body))
        payment = notification.object

        flask_logger.info(f"Webhook event: {notification.event}, Payment ID: {payment.id}, Status: {payment.status}")

        if notification.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Processing successful payment: {payment.id}")

            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}")
                return Response(status=200)

            telegram_user_id = metadata['telegram_user_id']
            flask_logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")

            db_session = None
            try:
                db_session = db.SessionLocal()
                user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()

                if user:
                    if db.activate_subscription(db_session, user.id):
                        flask_logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")
                        # Тут можно добавить логику для отправки уведомления пользователю через application.bot
                        # Но это нужно делать осторожно, чтобы не блокировать поток Flask
                        # Например, через asyncio.run_coroutine_threadsafe, если application доступен
                    else:
                        flask_logger.error(f"Failed to activate subscription in DB for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                else:
                    flask_logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except SQLAlchemyError as db_e:
                flask_logger.error(f"Database error during webhook processing for user {telegram_user_id} payment {payment.id}: {db_e}", exc_info=True)
                if db_session:
                    db_session.rollback()
            except Exception as e:
                flask_logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                if db_session and db_session.is_active:
                    db_session.rollback()
            finally:
                if db_session:
                    db_session.close()

        return Response(status=200)

    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400)
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500)

# --- Функция для запуска Flask в отдельном потоке ---
def run_flask():
    port = int(os.environ.get("PORT", 8080)) # Берем порт из окружения Railway
    flask_logger.info(f"Starting Flask server on 0.0.0.0:{port}")
    try:
        # Используем встроенный сервер Flask для простоты,
        # хотя для продакшена рекомендован gunicorn/waitress
        flask_app.run(host='0.0.0.0', port=port, debug=False)
    except Exception as e:
        flask_logger.critical(f"Flask server failed to start: {e}", exc_info=True)

# --- Основная настройка бота ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def post_init(application: Application):
    me = await application.bot.get_me()
    logger.info(f"Bot started as @{me.username}")

    # Запускаем фоновые задачи бота
    asyncio.create_task(tasks.spam_task(application))
    asyncio.create_task(tasks.reset_daily_limits_task())
    asyncio.create_task(tasks.check_subscription_expiry_task(application))


def main() -> None:
    logger.info("Creating database tables if they don't exist...")
    db.create_tables()
    logger.info("Database setup complete.")

    # --- Запуск Flask сервера в отдельном потоке ---
    # Важно: Запускаем ДО создания Application, чтобы порт был свободен
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask thread started.")
    # Даем Flask немного времени на запуск (опционально, но может помочь)
    # import time
    # time.sleep(2)

    # --- Создание и настройка основного бота ---
    application = Application.builder().token(config.TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).build()


    edit_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('editpersona', handlers.edit_persona_start)],
        states={
            handlers.EDIT_PERSONA_CHOICE: [
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')
            ],
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$')
            ],
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                 CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$')
            ],
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')
            ],
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')
            ],
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')
            ],
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$')
            ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$')
        ],
        per_message=False,
        conversation_timeout=timedelta(minutes=15).total_seconds()
    )


    delete_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('deletepersona', handlers.delete_persona_start)],
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
        conversation_timeout=timedelta(minutes=5).total_seconds()
    )


    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))

    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))

    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query, pattern='^set_mood_|^subscribe_'))

    application.add_error_handler(handlers.error_handler)

    application.post_init = post_init

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
