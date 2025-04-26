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
import aiohttp
import httpx # Используется для Telegra.ph

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler, Defaults
)
from telegram.constants import ParseMode as TelegramParseMode
# Оставляем импорт Telegraph только для get_account_info и exceptions
from telegraph_api import Telegraph, exceptions as telegraph_exceptions
# Pydantic импортирован в requirements.txt, но здесь не используется напрямую

import config # Импортируем весь модуль config
import db
import handlers # Импортируем handlers
import tasks

# --- Flask App for Yookassa Webhook ---
# Эта часть остается без изменений
flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')

try:
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit():
        YookassaConfig.configure(account_id=int(config.YOOKASSA_SHOP_ID), secret_key=config.YOOKASSA_SECRET_KEY)
        flask_logger.info(f"Yookassa SDK configured for webhook (Shop ID: {config.YOOKASSA_SHOP_ID}).")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY invalid/missing.")
except ValueError:
     flask_logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) is not a valid integer.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook: {e}")

@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    event_json = None
    try:
        event_json = request.get_json(force=True)
        flask_logger.info(f"Webhook received: Event='{event_json.get('event')}', Type='{event_json.get('type')}'")
        flask_logger.debug(f"Webhook body: {json.dumps(event_json)}")

        if not config.YOOKASSA_SECRET_KEY or not config.YOOKASSA_SHOP_ID or not config.YOOKASSA_SHOP_ID.isdigit():
            flask_logger.error("YOOKASSA not configured correctly. Cannot process webhook.")
            return Response("Server configuration error", status=500)
        try:
             current_shop_id = int(config.YOOKASSA_SHOP_ID)
             if not YookassaConfig.secret_key or YookassaConfig.account_id != current_shop_id:
                  YookassaConfig.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
                  flask_logger.info("Yookassa SDK re-configured within webhook handler.")
        except ValueError:
             flask_logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) invalid integer during webhook re-config.")
             return Response("Server configuration error", status=500)
        except Exception as conf_e:
             flask_logger.error(f"Failed to re-configure Yookassa SDK in webhook: {conf_e}")
             return Response("Server configuration error", status=500)

        notification_object = WebhookNotification(event_json)
        payment = notification_object.object
        flask_logger.info(f"Processing event: {notification_object.event}, Payment ID: {payment.id}, Status: {payment.status}")

        if notification_object.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Successful payment detected: {payment.id}")
            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' missing in metadata for payment {payment.id}.")
                return Response(status=200) # Return 200 so YK doesn't retry
            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' {metadata.get('telegram_user_id')} for payment {payment.id}. Error: {e}")
                return Response(status=200)

            flask_logger.info(f"Attempting subscription activation for TG User ID: {telegram_user_id}")
            db_session = None
            try:
                with db.get_db() as db_session: # Use context manager
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                    if user:
                        if db.activate_subscription(db_session, user.id): # activate_subscription handles commit
                            flask_logger.info(f"Subscription activated for user {telegram_user_id} (DB ID: {user.id}) via webhook payment {payment.id}.")
                        else:
                            flask_logger.error(f"db.activate_subscription failed for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with TG ID {telegram_user_id} not found for payment {payment.id}.")
            except SQLAlchemyError as e:
                 flask_logger.error(f"DB error during subscription activation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
            except Exception as e:
                 flask_logger.error(f"Unexpected error during DB operation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                 # Rollback handled by context manager if needed

        elif notification_object.event == 'payment.canceled':
             flask_logger.info(f"Payment {payment.id} was canceled.")
        else:
             flask_logger.info(f"Ignoring webhook event '{notification_object.event}' status '{payment.status}'")
        return Response(status=200)
    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400, description="Invalid JSON")
    except ValueError as ve:
         flask_logger.error(f"Webhook error: Could not parse YK notification. Error: {ve}", exc_info=True)
         flask_logger.debug(f"Received data: {request.get_data(as_text=True)}")
         abort(400, description="Invalid YK notification format")
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        try: flask_logger.debug(f"Webhook body on error: {request.get_data(as_text=True)}")
        except: pass
        abort(500, description="Internal server error")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_logger.info(f"Starting Flask server on 0.0.0.0:{port}")
    try:
        # Use waitress or gunicorn in production instead of Flask's dev server
        # For Railway, it often uses gunicorn automatically based on Procfile
        # flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        # Assuming gunicorn handles execution via Procfile, this function might just be for local testing
        # If run directly on Railway without gunicorn, use waitress:
        from waitress import serve
        serve(flask_app, host='0.0.0.0', port=port)
    except Exception as e:
        flask_logger.critical(f"Flask/Waitress server thread failed: {e}", exc_info=True)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Set higher levels for noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO) # Keep INFO for bot interactions
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING) # Usually WARNING is enough
# logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO) # Use INFO to see SQL queries if needed
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Flask/Waitress internal logs
logging.getLogger("telegraph_api").setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Telegra.ph Setup ---
async def setup_telegraph_page(application: Application):
    # Эта функция остается без изменений по сравнению с предыдущей версией
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None
    access_token = config.TELEGRAPH_ACCESS_TOKEN
    if not access_token:
        logger.error("TELEGRAPH_ACCESS_TOKEN not set. Cannot create/update ToS page.")
        return
    try:
        telegraph = Telegraph(access_token=access_token)
        account_info = await telegraph.get_account_info(fields=['author_name', 'page_count'])
        logger.info(f"Telegraph account info check successful: {account_info}")
    except Exception as e:
        logger.error(f"Failed to get Telegraph account info (token might be invalid): {e}", exc_info=True)
        return

    author_name = "@NunuAiBot"
    tos_title = f"Пользовательское Соглашение @NunuAiBot"
    page_url = None
    try:
        tos_content_raw = handlers.TOS_TEXT
        if not tos_content_raw or not isinstance(tos_content_raw, str):
             logger.error("handlers.TOS_TEXT is empty or not a string. Cannot create ToS page.")
             return
        tos_content_formatted=tos_content_raw.format(subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",subscription_currency=config.SUBSCRIPTION_CURRENCY).replace("**", "")
        paragraphs_raw = tos_content_formatted.splitlines()
        content_node_array = [{"tag": "p", "children": [p.strip()]} for p in paragraphs_raw if p.strip()]
        if not content_node_array:
            logger.error("content_node_array empty after processing. Cannot create page.")
            return

        telegraph_api_url = "https://api.telegra.ph/createPage"
        payload={"access_token": access_token,"title": tos_title,"author_name": author_name,"content": json.dumps(content_node_array),"return_content": False}
        logger.info(f"Sending direct request to {telegraph_api_url}...")
        logger.debug(f"Payload (content truncated): access_token=..., title='{tos_title}', author_name='{author_name}', content='{payload['content'][:100]}...', return_content=False")
        async with httpx.AsyncClient() as client:
            response = await client.post(telegraph_api_url, data=payload)
        logger.info(f"Telegraph API direct response status: {response.status_code}")
        if response.status_code == 200:
            try:
                response_data = response.json()
                logger.debug(f"Telegraph API direct response JSON: {response_data}")
                if response_data.get("ok"):
                    result = response_data.get("result")
                    if result and isinstance(result, dict) and result.get("url"):
                        page_url = result["url"]
                        logger.info(f"Successfully created/updated Telegra.ph page via direct request: {page_url}")
                    else: logger.error(f"Telegraph API direct request OK=true, but result invalid/missing. Result: {result}")
                else:
                    error_message = response_data.get("error", "Unknown error")
                    logger.error(f"Telegraph API direct request returned error: {error_message}")
                    if "CONTENT_TEXT_REQUIRED" in error_message: logger.error(f">>> Received CONTENT_TEXT_REQUIRED! Check payload: {payload['content']}")
            except json.JSONDecodeError: logger.error(f"Failed to decode JSON response from Telegraph API direct request. Response text: {response.text}")
            except Exception as parse_err: logger.error(f"Error parsing successful Telegraph API direct response: {parse_err}", exc_info=True)
        else: logger.error(f"Telegraph API direct request failed status {response.status_code}. Text: {response.text}")
    except httpx.RequestError as http_err: logger.error(f"HTTPX network error during direct Telegraph request: {http_err}", exc_info=True)
    except json.JSONDecodeError as json_err: logger.error(f"Failed to dump content_node_array to JSON: {json_err}", exc_info=True)
    except Exception as e: logger.error(f"Unexpected error during direct Telegra.ph page creation: {e}", exc_info=True)

    if page_url:
        application.bot_data['tos_url'] = page_url
        logger.info(f"Final ToS URL set in bot_data: {page_url}")
    else: logger.error("Failed to obtain Telegra.ph page URL using direct request.")


# --- Bot Initialization ---
async def post_init(application: Application):
    """Post-initialization tasks."""
    try:
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        # Store bot username for later use if needed
        application.bot_data['bot_username'] = me.username
        # Setup Telegra.ph page in background
        asyncio.create_task(setup_telegraph_page(application))
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or scheduling setup_telegraph): {e}", exc_info=True)

    # Start background tasks (APScheduler)
    logger.info("Starting background tasks...")
    if application.job_queue:
        application.job_queue.run_repeating(tasks.reset_daily_limits_task, interval=timedelta(hours=1), first=timedelta(seconds=15), name="daily_limit_reset_check")
        application.job_queue.run_repeating(tasks.check_subscription_expiry_task, interval=timedelta(hours=1), first=timedelta(seconds=30), name="subscription_expiry_check", data=application) # Pass application object
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")

# <<< ИЗМЕНЕНО: Обновлены Conversation Handlers и добавлены новые callback handlers >>>
def main() -> None:
    """Start the bot."""
    logger.info("----- Bot Starting -----")

    # --- Database Setup ---
    try:
        db.initialize_database()
        db.create_tables()
    except (OperationalError, SQLAlchemyError, RuntimeError, ValueError) as e:
         logger.critical(f"Database initialization failed: {e}. Exiting.")
         return
    except Exception as e:
         logger.critical(f"An unexpected critical error during DB setup: {e}. Exiting.", exc_info=True)
         return
    logger.info("Database setup complete.")

    # --- Start Flask Webhook Thread ---
    # Make sure your Procfile/Dockerfile starts *this* main.py script,
    # which then starts Flask in a thread.
    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    # --- Telegram Bot Application Setup ---
    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    # Set default parse mode for all messages sent by the bot
    bot_defaults = Defaults(parse_mode=TelegramParseMode.MARKDOWN_V2) # Using V2 for more features

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .defaults(bot_defaults) # Apply defaults
        .post_init(post_init)
        .build()
    )
    logger.info("Telegram Application built.")

    # --- Conversation Handlers ---
    # Edit Persona Conversation (remains largely the same, relies on specific callbacks)
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_') # From /mypersonas button
            ],
        states={
            handlers.EDIT_PERSONA_CHOICE: [CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')],
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back to main menu
                ],
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back to main menu
                ],
            handlers.EDIT_MOOD_CHOICE: [CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')],
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back to mood list
                ],
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back to mood list
                ],
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'), # Confirm delete
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Cancel delete -> back to mood list
                ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel), # Command to cancel
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$'), # General cancel button
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^edit_moods_back_cancel$'), # Explicit cancel from mood name/prompt/delete confirm
        ],
        per_message=False, # Allow multiple users independently
        name="edit_persona_conversation",
        conversation_timeout=timedelta(minutes=15).total_seconds() # Timeout
    )

    # Delete Persona Conversation (remains the same)
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('deletepersona', handlers.delete_persona_start),
            CallbackQueryHandler(handlers.delete_persona_button_callback, pattern='^delete_persona_') # From /mypersonas button
            ],
        states={
            handlers.DELETE_PERSONA_CONFIRM: [
                CallbackQueryHandler(handlers.delete_persona_confirmed, pattern='^delete_persona_confirm_'), # Confirm delete
                CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$') # Cancel delete
                ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.delete_persona_cancel), # Command to cancel
            CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$') # Cancel button
            ],
        per_message=False,
        name="delete_persona_conversation",
        conversation_timeout=timedelta(minutes=5).total_seconds()
    )
    logger.info("Conversation Handlers configured.")

    # --- Register Handlers ---
    logger.info("Registering handlers...")

    # Commands
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe)) # Shows initial subscribe info
    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    # Conversation handlers need to be added before general handlers that might capture their entry points
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))
    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False))

    # Messages (Order matters: more specific filters first if needed)
    # block=False allows concurrent processing of messages
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    # Handle text messages last among message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # Callback Queries (Handles button presses)
    # IMPORTANT: This handler should come *after* ConversationHandlers if they also use CallbackQueryHandler entry points
    # The ConversationHandlers will try to handle the callback first based on state.
    # If no conversation is active or the callback doesn't match a state transition,
    # this general handler will process it.
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler (Should be last)
    application.add_error_handler(handlers.error_handler)

    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, # Process all update types
        drop_pending_updates=True # Ignore updates received while bot was down
    )
    logger.info("----- Bot Stopped -----")

if __name__ == "__main__":
    main()
