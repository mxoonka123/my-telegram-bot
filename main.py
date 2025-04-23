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
    # Используем Shop ID и Secret Key из config.py
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY:
        # Yookassa SDK ожидает Shop ID как строку или None, а Secret Key как строку
        # Передаем None для Shop ID, так как ключ используется для аутентификации вебхука
        YookassaConfig.configure(None, config.YOOKASSA_SECRET_KEY)
        flask_logger.info("Yookassa SDK configured for webhook handler using Secret Key.")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY not found in config for webhook handler.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook handler: {e}")

# --- Обработчик вебхуков Yookassa (почти как в webhook_server.py) ---
@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    flask_logger.info("Received request on /yookassa/webhook")
    try:
        request_body = request.get_data(as_text=True)
        # Логируем только начало, чтобы избежать слишком больших логов
        flask_logger.info(f"Webhook body (first 500 chars): {request_body[:500]}...")

        # Парсим уведомление
        notification = WebhookNotification(json.loads(request_body))
        payment = notification.object # Объект платежа

        flask_logger.info(f"Webhook event: {notification.event}, Payment ID: {payment.id}, Status: {payment.status}")

        # Обрабатываем только успешные платежи
        if notification.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Processing successful payment: {payment.id}")

            metadata = payment.metadata
            # Проверяем наличие telegram_user_id в метаданных
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}")
                return Response(status=200) # Возвращаем 200, чтобы ЮKassa не повторяла запрос

            try:
                # Пытаемся получить telegram_user_id как int
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' in metadata for payment {payment.id}. Value: {metadata.get('telegram_user_id')}. Error: {e}")
                return Response(status=200)

            flask_logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")

            db_session = None
            try:
                # Используем SessionLocal для создания сессии для этого запроса
                db_session = db.SessionLocal()
                # Ищем пользователя по telegram_id
                user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()

                if user:
                    # Вызываем функцию активации подписки, передавая внутренний user.id
                    if db.activate_subscription(db_session, user.id):
                        flask_logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")
                        # Опционально: отправить уведомление пользователю через бота
                        # Это нужно делать асинхронно, чтобы не блокировать Flask
                        # loop = asyncio.get_event_loop()
                        # if loop.is_running():
                        #     asyncio.run_coroutine_threadsafe(notify_user_success(telegram_user_id), loop)
                        # else:
                        #    flask_logger.warning("Cannot notify user: Event loop not running.")
                    else:
                        flask_logger.error(f"Failed to activate subscription in DB for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                else:
                    flask_logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except SQLAlchemyError as db_e:
                flask_logger.error(f"Database error during webhook processing for user {telegram_user_id} payment {payment.id}: {db_e}", exc_info=True)
                if db_session and db_session.is_active: # Проверяем активность перед rollback
                    db_session.rollback()
            except Exception as e:
                flask_logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                if db_session and db_session.is_active:
                    db_session.rollback()
            finally:
                if db_session:
                    db_session.close() # Всегда закрываем сессию

        # Отвечаем ЮKassa, что все ок (или что мы обработали как могли)
        return Response(status=200)

    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400, description="Invalid JSON") # Возвращаем 400 Bad Request
    except ValueError as ve: # Обработка ошибок Yookassa SDK при парсинге
         flask_logger.error(f"Webhook error: Could not parse Yookassa notification. Error: {ve}", exc_info=True)
         abort(400, description="Invalid Yookassa notification format")
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500, description="Internal server error") # Возвращаем 500 Internal Server Error

# --- Функция для запуска Flask в отдельном потоке ---
def run_flask():
    # Railway предоставляет порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080)) # Используем 8080 как дефолт
    flask_logger.info(f"Starting Flask server on 0.0.0.0:{port}")
    try:
        # Используем встроенный сервер Flask. Для продакшена можно заменить на Gunicorn или Waitress
        # Но для Railway встроенный сервер часто достаточен.
        # Убедись, что debug=False в продакшене
        flask_app.run(host='0.0.0.0', port=port, debug=False)
    except Exception as e:
        flask_logger.critical(f"Flask server failed to start or crashed: {e}", exc_info=True)

# --- Основная настройка бота ---
logging.basicConfig(
    level=logging.INFO, # Уровень INFO ловит INFO, WARNING, ERROR, CRITICAL
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Уменьшим "болтливость" некоторых библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Оставим INFO для PTB
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING) # Логи SQL запросов на уровне INFO

logger = logging.getLogger(__name__) # Логгер для main.py


async def post_init(application: Application):
    """Выполняется после инициализации Application."""
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}", exc_info=True)

    # Запускаем фоновые задачи бота
    logger.info("Starting background tasks...")
    application.job_queue.run_repeating(tasks.reset_daily_limits_task, interval=timedelta(hours=1), first=10, name="daily_limit_reset_check") # Check hourly, reset near midnight
    application.job_queue.run_repeating(tasks.check_subscription_expiry_task, interval=timedelta(hours=1), first=20, name="subscription_expiry_check", job_kwargs={'application': application}) # Check hourly
    # asyncio.create_task(tasks.spam_task(application)) # Запускаем спам-таск, если нужен
    logger.info("Background tasks scheduled.")


def main() -> None:
    logger.info("----- Bot Starting -----")
    logger.info("Creating database tables if they don't exist...")
    try:
        db.create_tables()
    except Exception as e:
        logger.critical(f"Database initialization failed: {e}. Exiting.")
        return # Не запускаем бота, если БД недоступна
    logger.info("Database setup complete.")

    # --- Запуск Flask сервера в отдельном потоке ---
    flask_thread = threading.Thread(target=run_flask, name="FlaskThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread started.")
    # Даем Flask немного времени на запуск (опционально)
    # import time
    # time.sleep(2)

    # --- Создание и настройка основного бота ---
    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    application = Application.builder().token(token).connect_timeout(30).read_timeout(30).post_init(post_init).build()
    logger.info("Application built.")

    # --- Настройка Conversation Handlers ---
    logger.info("Setting up Conversation Handlers...")
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('editpersona', handlers.edit_persona_start)],
        states={
            handlers.EDIT_PERSONA_CHOICE: [
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')
            ],
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back from text input
            ],
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                 CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back from text input
            ],
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')
            ],
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back from name input
            ],
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back from prompt input
            ],
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Cancel deletion
            ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel), # General cancel command
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$') # Cancel via button
        ],
        per_message=False, # Состояние одно на пользователя/чат
        conversation_timeout=timedelta(minutes=15).total_seconds() # Таймаут диалога
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
    logger.info("Conversation Handlers configured.")

    # --- Регистрация обработчиков ---
    logger.info("Registering handlers...")
    # Основные команды
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))

    # Управление личностями
    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    application.add_handler(edit_persona_conv_handler) # Обработчик диалога редактирования
    application.add_handler(delete_persona_conv_handler) # Обработчик диалога удаления
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))

    # Управление в чате
    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))     # <-- ДОБАВЛЕНО
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False)) # <-- ДОБАВЛЕНО

    # Обработка медиа и сообщений (должны быть ниже команд)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # Обработка колбэков (кроме тех, что в ConversationHandler)
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query, pattern='^set_mood_|^subscribe_')) # Добавь другие, если нужно

    # Обработчик ошибок (последним)
    application.add_error_handler(handlers.error_handler)
    logger.info("Handlers registered.")

    # --- Запуск бота ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True # Сбрасываем старые апдейты при старте
    )
    logger.info("----- Bot Stopped -----")


if __name__ == "__main__":
    main()
