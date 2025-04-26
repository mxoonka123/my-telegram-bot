import logging
import asyncio
import os
import threading
from datetime import timedelta
from flask import Flask, request, abort, Response
from yookassa import Configuration as YookassaConfig
from yookassa.domain.notification import WebhookNotification
import json
from sqlalchemy.exc import SQLAlchemyError, OperationalError

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler, Defaults
)
from telegram.constants import ParseMode as TelegramParseMode # Import with alias
from telegraph_api import Telegraph
from pydantic import ValidationError


import config
import db
import handlers
import tasks


flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')


# Configure Yookassa SDK for Webhook handler
try:
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit():
        # Configure with Secret Key for webhook validation
        YookassaConfig.configure(account_id=None, secret_key=config.YOOKASSA_SECRET_KEY)
        flask_logger.info("Yookassa SDK configured for webhook handler using Secret Key.")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY invalid/missing in config for webhook handler.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook handler: {e}")


@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    event_json = None
    try:
        event_json = request.get_json(force=True) # More robust than get_data
        flask_logger.info(f"Webhook received: Event='{event_json.get('event')}', Type='{event_json.get('type')}'")
        flask_logger.debug(f"Webhook body: {json.dumps(event_json)}") # Log full body in debug

        # Validate configuration again inside request context if needed
        if not config.YOOKASSA_SECRET_KEY:
            flask_logger.error("YOOKASSA_SECRET_KEY not configured. Cannot process webhook.")
            return Response("Server configuration error", status=500)
        if not YookassaConfig.secret_key:
            try:
                 YookassaConfig.configure(None, config.YOOKASSA_SECRET_KEY)
                 flask_logger.info("Yookassa SDK re-configured within webhook handler.")
            except Exception as conf_e:
                 flask_logger.error(f"Failed to re-configure Yookassa SDK in webhook: {conf_e}")
                 return Response("Server configuration error", status=500)

        # Process notification
        notification_object = WebhookNotification(event_json)
        payment = notification_object.object # This is the Payment object

        flask_logger.info(f"Processing event: {notification_object.event}, Payment ID: {payment.id}, Status: {payment.status}")

        # Check for successful payment
        if notification_object.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Successful payment detected: {payment.id}")

            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}. Metadata: {metadata}")
                return Response(status=200) # Acknowledge webhook even if metadata is missing

            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' in metadata for payment {payment.id}. Value: {metadata.get('telegram_user_id')}. Error: {e}")
                return Response(status=200) # Acknowledge webhook

            flask_logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")

            # --- Database Interaction ---
            db_session = None
            try:
                with db.get_db() as db_session:
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()

                    if user:
                        if db.activate_subscription(db_session, user.id): # Handles commit internally
                            flask_logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")
                            # TODO: Optionally send a confirmation message to the user via the bot
                        else:
                            # activate_subscription already logged the error
                            flask_logger.error(f"db.activate_subscription failed for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except SQLAlchemyError as e:
                 # Rollback is handled by get_db context manager
                 flask_logger.error(f"Database error during subscription activation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
            except Exception as e:
                 flask_logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                 if db_session and db_session.is_active:
                     try: db_session.rollback()
                     except: pass
            # --- End Database Interaction ---

        elif notification_object.event == 'payment.canceled':
             flask_logger.info(f"Payment {payment.id} was canceled.")
             # Handle cancellation if needed (e.g., logging, user notification)
        else:
             flask_logger.info(f"Ignoring webhook event type '{notification_object.event}' with status '{payment.status}'")


        # Always return 200 OK to Yookassa if notification was parsed successfully
        return Response(status=200)

    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400, description="Invalid JSON")
    except ValueError as ve:
         # Catches errors from WebhookNotification constructor if format is wrong
         flask_logger.error(f"Webhook error: Could not parse Yookassa notification. Error: {ve}", exc_info=True)
         flask_logger.debug(f"Received data: {request.get_data(as_text=True)}")
         abort(400, description="Invalid Yookassa notification format")
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        # Log the body for debugging unknown errors
        try: flask_logger.debug(f"Webhook body on error: {request.get_data(as_text=True)}")
        except: pass
        abort(500, description="Internal server error")


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_logger.info(f"Starting Flask server on 0.0.0.0:{port}")
    try:
        # Use Gunicorn or another production server in production instead of Flask's dev server
        # For Railway, it often manages this automatically if Procfile is set up.
        # Assuming Railway uses gunicorn based on requirements.txt:
        # The command `gunicorn main:flask_app` would be run by Railway.
        # This function might just be for local testing if Railway runs gunicorn directly.
        # If running locally:
        flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        flask_logger.critical(f"Flask server thread failed to start or crashed: {e}", exc_info=True)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO) # More detailed TG logs can be helpful
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING) # Set to INFO for SQL query debugging
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Flask dev server logs
logging.getLogger("telegraph_api").setLevel(logging.INFO) # Log telegraph info


logger = logging.getLogger(__name__)


async def setup_telegraph_page(application: Application):
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None # Reset URL at start

    if not config.TELEGRAPH_ACCESS_TOKEN:
        logger.error("TELEGRAPH_ACCESS_TOKEN is not set. Cannot create/update ToS page.")
        return

    telegraph = None
    try:
        telegraph = Telegraph(access_token=config.TELEGRAPH_ACCESS_TOKEN)
        account_info = await telegraph.get_account_info(fields=['author_name', 'page_count'])
        logger.info(f"Telegraph account info: {account_info}")
    except Exception as e:
        logger.error(f"Failed to initialize Telegraph or get account info: {e}", exc_info=True)
        return

    author_name = "@NunuAiBot"
    tos_title = f"Пользовательское Соглашение @NunuAiBot"

    try:
        tos_content_raw = handlers.TOS_TEXT
        # --- Use simple string content ---
        tos_content_formatted = tos_content_raw.format(
            subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
            subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
            subscription_currency=config.SUBSCRIPTION_CURRENCY
        ).replace("**", "") # Remove markdown bold, keep newlines
        # --------------------------------

        logger.debug(f"Attempting to create Telegra.ph page with string content (length: {len(tos_content_formatted)}).")

        # --- Create Page with String Content ---
        page_data = await telegraph.create_page(
             title=tos_title,
             content=tos_content_formatted, # Pass the formatted string directly
             author_name=author_name,
        )
        logger.debug(f"Telegra.ph create_page response: {page_data}")

        if isinstance(page_data, dict) and 'url' in page_data:
            page_url = page_data['url']
            application.bot_data['tos_url'] = page_url
            logger.info(f"Successfully created/updated Telegra.ph ToS page: {page_url}")
        else:
            logger.error(f"Could not extract URL from Telegra.ph API response: {page_data}")

    except ValidationError as e:
         logger.error(f"Pydantic ValidationError during Telegra.ph operation: {e}", exc_info=True)
    except Exception as e:
        # Catching specific TelegraphError if possible
        if "CONTENT_TEXT_REQUIRED" in str(e):
             logger.error(f"Telegraph Error: CONTENT_TEXT_REQUIRED. Library might still expect node format. Content length: {len(tos_content_formatted)}", exc_info=True)
        elif "CONTENT_FORMAT_INVALID" in str(e):
             logger.error(f"Telegraph Error: CONTENT_FORMAT_INVALID. Content: {tos_content_formatted[:200]}...", exc_info=True)
        else:
             logger.error(f"Failed to setup Telegra.ph page: {e}", exc_info=True)


async def post_init(application: Application):
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        await setup_telegraph_page(application)
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or setup_telegraph): {e}", exc_info=True)

    logger.info("Starting background tasks...")
    if application.job_queue:
        application.job_queue.run_repeating(tasks.reset_daily_limits_task, interval=timedelta(hours=1), first=timedelta(minutes=1), name="daily_limit_reset_check")
        application.job_queue.run_repeating(tasks.check_subscription_expiry_task, interval=timedelta(hours=1), first=timedelta(minutes=2), name="subscription_expiry_check", data=application)
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")

def main() -> None:
    logger.info("----- Bot Starting -----")

    # --- Database Initialization ---
    try:
        db.initialize_database() # Creates engine, sessionmaker, tests connection
        db.create_tables() # Creates tables if they don't exist
    except (OperationalError, SQLAlchemyError, RuntimeError, ValueError) as e:
         # These errors from initialize_database or create_tables are fatal
         logger.critical(f"Database initialization failed: {e}. Exiting.")
         return # Stop execution if DB init fails
    except Exception as e:
         logger.critical(f"An unexpected critical error occurred during DB setup: {e}. Exiting.", exc_info=True)
         return
    logger.info("Database setup complete.")

    # --- Flask Webhook Thread ---
    # Run Flask in a daemon thread so it doesn't block bot shutdown
    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    # --- Telegram Bot Application ---
    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    # Set default parse mode
    bot_defaults = Defaults(parse_mode=TelegramParseMode.MARKDOWN)

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60) # Added write timeout
        .defaults(bot_defaults) # Apply defaults
        .post_init(post_init)
        .build()
    )
    logger.info("Application built.")

    # --- Conversation Handlers ---
    logger.info("Setting up Conversation Handlers...")
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')
            ],
        states={
            handlers.EDIT_PERSONA_CHOICE: [CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')],
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back from field input
                ],
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back from max messages input
                ],
            handlers.EDIT_MOOD_CHOICE: [CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')],
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
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Cancel from delete confirm
                ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$'),
            # Optional: Timeout handler?
        ],
        per_message=False, # Use one state per user+chat
        name="edit_persona_conversation", # Optional: Name for debugging
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
        name="delete_persona_conversation",
        conversation_timeout=timedelta(minutes=5).total_seconds()
    )
    logger.info("Conversation Handlers configured.")

    # --- Register Handlers ---
    logger.info("Registering handlers...")
    # Basic Commands
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))

    # Persona Management (Commands first)
    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    # Conversation handlers added below commands they start
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)

    # Chat Management
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))
    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False))

    # Message Handlers (lower priority than commands)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    # Add caption filter? users might describe photos in captions
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # CallbackQuery Handler (should have higher priority than message handlers if possible, but PTB handles it)
    # Ensure this handler doesn't capture conversation callbacks by checking context inside
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler (last)
    application.add_error_handler(handlers.error_handler)
    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, # Process all update types
        drop_pending_updates=True # Avoid processing old updates on restart
    )
    logger.info("----- Bot Stopped -----")


if __name__ == "__main__":
    main()
