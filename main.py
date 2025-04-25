# main.py

import logging
import asyncio
import os
import threading
from datetime import timedelta
from flask import Flask, request, abort, Response
from yookassa import Configuration as YookassaConfig
from yookassa.domain.notification import WebhookNotification
import json
from sqlalchemy.exc import SQLAlchemyError

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler
)
# Импортируем только основной класс Telegraph
from telegraph_api import Telegraph
# Импортируем ValidationError для явной обработки
from pydantic import ValidationError


import config
import db
import handlers
import tasks


flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')


try:
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY:
        if config.YOOKASSA_SECRET_KEY:
            YookassaConfig.configure(None, config.YOOKASSA_SECRET_KEY)
            flask_logger.info("Yookassa SDK configured for webhook handler using Secret Key.")
        else:
            flask_logger.warning("YOOKASSA_SECRET_KEY is empty in config for webhook handler.")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY not found in config for webhook handler.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook handler: {e}")


@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    flask_logger.info("Received request on /yookassa/webhook")
    try:
        request_body = request.get_data(as_text=True)
        flask_logger.info(f"Webhook body (first 500 chars): {request_body[:500]}...")

        if not config.YOOKASSA_SECRET_KEY:
            flask_logger.error("YOOKASSA_SECRET_KEY not configured. Cannot process webhook.")
            return Response("Yookassa Secret Key not configured", status=500)

        try:
             if not YookassaConfig.secret_key:
                 YookassaConfig.configure(None, config.YOOKASSA_SECRET_KEY)
                 flask_logger.info("Yookassa SDK re-configured within webhook handler.")
        except Exception as conf_e:
             flask_logger.error(f"Failed to re-configure Yookassa SDK in webhook: {conf_e}")
             return Response("Yookassa configuration error", status=500)


        notification = WebhookNotification(json.loads(request_body))
        payment = notification.object

        flask_logger.info(f"Webhook event: {notification.event}, Payment ID: {payment.id}, Status: {payment.status}")

        if notification.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Processing successful payment: {payment.id}")

            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}")
                return Response(status=200)

            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' in metadata for payment {payment.id}. Value: {metadata.get('telegram_user_id')}. Error: {e}")
                return Response(status=200)

            flask_logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")

            try:
                with db.get_db() as db_session:
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()

                    if user:
                        if db.activate_subscription(db_session, user.id):
                            flask_logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")
                        else:
                            flask_logger.error(f"Failed to activate subscription in DB for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except Exception as e:
                flask_logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)

        return Response(status=200)

    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400, description="Invalid JSON")
    except ValueError as ve:
         flask_logger.error(f"Webhook error: Could not parse Yookassa notification. Error: {ve}", exc_info=True)
         abort(400, description="Invalid Yookassa notification format")
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500, description="Internal server error")


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_logger.info(f"Starting Flask server on 0.0.0.0:{port}")
    try:
        flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        flask_logger.critical(f"Flask server thread failed to start or crashed: {e}", exc_info=True)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def setup_telegraph_page(application: Application):
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None # Сразу ставим None, чтобы не было старого URL

    if not config.TELEGRAPH_ACCESS_TOKEN:
        logger.error("TELEGRAPH_ACCESS_TOKEN is not set. Cannot create/update ToS page.")
        return

    telegraph = Telegraph(access_token=config.TELEGRAPH_ACCESS_TOKEN)
    author_name = "@NunuAiBot" # Имя автора
    tos_title = f"Пользовательское Соглашение @NunuAiBot" # Заголовок страницы

    try:
        # --- Подготовка контента ---
        # Берем текст из handlers.py
        tos_content_raw = handlers.TOS_TEXT
        # Форматируем его с актуальными данными из config
        tos_content_formatted = tos_content_raw.format(
            subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
            subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
            subscription_currency=config.SUBSCRIPTION_CURRENCY
        )
        # Убираем ** Markdown, API его не поймет как форматирование текста
        plain_text_content = tos_content_formatted.replace("**", "")

        # --- Создание/Редактирование страницы ---
        page_data = None
        try:
             # Всегда создаем новую страницу (упрощение для обхода ошибок валидации)
            logger.info(f"Attempting to create Telegra.ph page with title: {tos_title}")
            # Передаем простой текст в 'content'
            page_data = await telegraph.create_page(
                 title=tos_title,
                 content=plain_text_content, # <--- Передаем СТРОКУ
                 author_name=author_name,
                 # return_content=False # Попробуем без этого параметра
            )
            logger.debug(f"Telegra.ph API create_page raw response: {page_data}")

            # Пытаемся извлечь URL из ответа
            if isinstance(page_data, dict) and 'url' in page_data:
                application.bot_data['tos_url'] = page_data['url']
                logger.info(f"Successfully created Telegra.ph page URL: {page_data['url']}")
            else:
                 logger.error(f"Could not extract URL from Telegra.ph API response: {page_data}")

        # Ловим ошибки валидации Pydantic и другие при создании/редактировании
        except (ValidationError, TypeError, Exception) as page_err:
             logger.error(f"Error during Telegra.ph page creation/editing: {page_err}", exc_info=True)
             # URL останется None

    except Exception as e:
        # Общая ошибка (например, при форматировании текста)
        logger.error(f"Failed to setup Telegra.ph page (outer try): {e}", exc_info=True)
        # URL останется None


async def post_init(application: Application):
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        await setup_telegraph_page(application)
    except Exception as e:
        logger.error(f"Failed to get bot info or setup Telegra.ph: {e}", exc_info=True)

    logger.info("Starting background tasks...")
    application.job_queue.run_repeating(tasks.reset_daily_limits_task, interval=timedelta(hours=1), first=timedelta(minutes=1), name="daily_limit_reset_check")
    application.job_queue.run_repeating(tasks.check_subscription_expiry_task, interval=timedelta(hours=1), first=timedelta(minutes=2), name="subscription_expiry_check", data=application)
    logger.info("Background tasks scheduled.")

def main() -> None:
    logger.info("----- Bot Starting -----")
    logger.info("Creating database tables if they don't exist...")
    try:
        db.create_tables()
    except Exception as e:
        logger.critical(f"Database initialization failed: {e}. Exiting.")
        return
    logger.info("Database setup complete.")

    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    application = Application.builder().token(token).connect_timeout(30).read_timeout(60).post_init(post_init).build()
    logger.info("Application built.")

    logger.info("Setting up Conversation Handlers...")
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')
            ],
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
        entry_points=[
            CommandHandler('deletepersona', handlers.delete_persona_start),
            CallbackQueryHandler(handlers.delete_persona_button_callback, pattern='^delete_persona_')
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
        conversation_timeout=timedelta(minutes=5).total_seconds()
    )
    logger.info("Conversation Handlers configured.")

    logger.info("Registering handlers...")
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
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    application.add_error_handler(handlers.error_handler)
    logger.info("Handlers registered.")

    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
    logger.info("----- Bot Stopped -----")


if __name__ == "__main__":
    main()
