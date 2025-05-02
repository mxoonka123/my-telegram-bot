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
import httpx
from typing import Optional
import re # <<< ДОБАВЛЕНО

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler, Defaults
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import TelegramError, Forbidden, BadRequest

from telegraph_api import Telegraph, exceptions as telegraph_exceptions

import config
import db # db.py should be fixed now (no circular import)
import handlers
import tasks
from utils import escape_markdown_v2

flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')

# --- Yookassa Configuration ---
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
application_instance: Optional[Application] = None

# --- Flask Webhook Handler ---
@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    global application_instance
    event_json = None
    try:
        event_json = request.get_json(force=True)
        # <<< ДОБАВЛЕНО: Логирование ID платежа в начале >>>
        payment_id_log = event_json.get('object', {}).get('id', 'N/A')
        flask_logger.info(f"Webhook received: Event='{event_json.get('event')}', Type='{event_json.get('type')}', PaymentID='{payment_id_log}'")
        flask_logger.debug(f"Webhook body: {json.dumps(event_json)}") # Log full body for debugging

        # --- Re-check Yookassa config before processing ---
        if not config.YOOKASSA_SECRET_KEY or not config.YOOKASSA_SHOP_ID or not config.YOOKASSA_SHOP_ID.isdigit():
            flask_logger.error("YOOKASSA not configured correctly. Cannot process webhook.")
            return Response("Server configuration error", status=500)
        try:
             # Ensure SDK is configured with current values (in case they changed via env vars)
             current_shop_id = int(config.YOOKASSA_SHOP_ID)
             if not hasattr(YookassaConfig, 'secret_key') or not YookassaConfig.secret_key or \
                not hasattr(YookassaConfig, 'account_id') or YookassaConfig.account_id != current_shop_id:
                  YookassaConfig.configure(account_id=current_shop_id, secret_key=config.YOOKASSA_SECRET_KEY)
                  flask_logger.info("Yookassa SDK re-configured within webhook handler.")
        except ValueError:
             flask_logger.error(f"YOOKASSA_SHOP_ID ({config.YOOKASSA_SHOP_ID}) invalid integer during webhook re-config.")
             return Response("Server configuration error", status=500)
        except Exception as conf_e:
             flask_logger.error(f"Failed to re-configure Yookassa SDK in webhook: {conf_e}")
             return Response("Server configuration error", status=500)

        # --- Process Notification ---
        notification_object = WebhookNotification(event_json)
        payment = notification_object.object
        flask_logger.info(f"Processing event: {notification_object.event}, Payment ID: {payment.id}, Status: {payment.status}")

        # --- Handle Successful Payment ---
        if notification_object.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Successful payment detected: {payment.id}")
            metadata = payment.metadata
            # Check for required metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' missing in metadata for payment {payment.id}.")
                return Response(status=200) # Acknowledge webhook even on error
            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' {metadata.get('telegram_user_id')} for payment {payment.id}. Error: {e}")
                return Response(status=200) # Acknowledge webhook

            # --- Activate Subscription in DB ---
            flask_logger.info(f"Attempting subscription activation for TG User ID: {telegram_user_id} from Payment ID: {payment.id}") # <<< Уточнено логгирование
            activation_success = False
            user_db_id = None

            try:
                with db.get_db() as db_session:
                    # <<< ДОБАВЛЕНО: Логирование перед поиском пользователя >>>
                    flask_logger.info(f"Webhook Payment {payment.id}: Searching for user with TG ID {telegram_user_id}")
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                    if user:
                        user_db_id = user.id
                        # <<< ДОБАВЛЕНО: Логирование перед активацией >>>
                        flask_logger.info(f"Webhook Payment {payment.id}: Found user DB ID {user_db_id}. Calling activate_subscription.")
                        # activate_subscription handles commit/rollback internally
                        if db.activate_subscription(db_session, user.id):
                            flask_logger.info(f"Subscription activated for user {telegram_user_id} (DB ID: {user_db_id}) via webhook payment {payment.id}.")
                            activation_success = True
                        else:
                            # activate_subscription already logged the error
                            flask_logger.error(f"db.activate_subscription returned False for user {telegram_user_id} (DB ID: {user_db_id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with TG ID {telegram_user_id} not found for payment {payment.id}.")
            except SQLAlchemyError as e:
                 flask_logger.error(f"DB error during subscription activation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
            except Exception as e:
                 flask_logger.error(f"Unexpected error during DB operation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)

            # --- Send Confirmation Message to User ---
            if activation_success:
                app = application_instance # Get the PTB application instance
                if app and app.bot:
                    # Construct raw text for the message
                    success_text_raw = (
                        f"✅ Ваша премиум подписка успешно активирована!\n"
                        f"Срок действия: {config.SUBSCRIPTION_DURATION_DAYS} дней.\n\n"
                        f"Спасибо за поддержку! 🎉\n\n"
                        f"Используйте /profile для просмотра статуса."
                    )
                    # Escape the whole string for MarkdownV2
                    success_text_escaped = escape_markdown_v2(success_text_raw)

                    # Send message asynchronously using the bot's event loop
                    try:
                        # Get the running event loop if available
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None # No running loop (might happen in some thread contexts)

                    if loop and loop.is_running():
                         # Schedule coroutine safely from another thread
                         future = asyncio.run_coroutine_threadsafe(
                            app.bot.send_message(chat_id=telegram_user_id, text=success_text_escaped, parse_mode=ParseMode.MARKDOWN_V2),
                            loop
                         )
                         try:
                             # Wait for the result with a timeout
                             future.result(timeout=10)
                             flask_logger.info(f"Sent activation confirmation to user {telegram_user_id}")
                         except asyncio.TimeoutError:
                             flask_logger.error(f"Timeout sending activation confirmation to user {telegram_user_id}")
                         except TelegramError as te:
                             flask_logger.error(f"Telegram error sending activation message to {telegram_user_id}: {te}")
                             if isinstance(te, BadRequest) and hasattr(te, 'message') and "parse" in te.message.lower():
                                flask_logger.error(f"--> Failed activation text (escaped): '{success_text_escaped[:200]}...'")
                         except Exception as send_e:
                             flask_logger.error(f"Failed to send activation message to user {telegram_user_id}: {send_e}", exc_info=True)
                    else:
                         # Fallback: Create a temporary event loop if none is running
                         flask_logger.warning("No running event loop found for webhook notification. Creating temporary loop.")
                         temp_loop = asyncio.new_event_loop()
                         try:
                              temp_loop.run_until_complete(app.bot.send_message(chat_id=telegram_user_id, text=success_text_escaped, parse_mode=ParseMode.MARKDOWN_V2))
                              flask_logger.info(f"Sent activation confirmation to user {telegram_user_id} using temporary loop.")
                         except TelegramError as te:
                              flask_logger.error(f"Telegram error sending activation message (temp loop) to {telegram_user_id}: {te}")
                              if isinstance(te, BadRequest) and hasattr(te, 'message') and "parse" in te.message.lower():
                                  flask_logger.error(f"--> Failed activation text (escaped, temp loop): '{success_text_escaped[:200]}...'")
                         except Exception as send_e:
                              flask_logger.error(f"Failed to send activation message (temp loop) to user {telegram_user_id}: {send_e}", exc_info=True)
                         finally:
                              temp_loop.close() # Close the temporary loop

                else:
                    flask_logger.warning("Cannot send activation confirmation: Bot application instance not found in webhook context.")

        # --- Handle Other Payment Events (Optional) ---
        elif notification_object.event == 'payment.canceled':
             flask_logger.info(f"Payment {payment.id} was canceled.")
             # Optionally notify user or take other actions
        elif notification_object.event == 'payment.waiting_for_capture':
             # This usually happens if capture=False in payment request
             flask_logger.info(f"Payment {payment.id} is waiting for capture.")
        else:
             # Ignore other events like refunds, etc., or handle them as needed
             flask_logger.info(f"Ignoring webhook event '{notification_object.event}' status '{payment.status}'")

        # --- Acknowledge Webhook ---
        # Always return 200 OK to Yookassa to prevent retries
        return Response(status=200)

    except json.JSONDecodeError:
        flask_logger.error("Webhook error: Invalid JSON received.")
        abort(400, description="Invalid JSON")
    except ValueError as ve:
         # Error parsing Yookassa notification object
         flask_logger.error(f"Webhook error: Could not parse YK notification. Error: {ve}", exc_info=True)
         flask_logger.debug(f"Received data: {request.get_data(as_text=True)}") # Log raw data on error
         abort(400, description="Invalid YK notification format")
    except Exception as e:
        # Catch-all for unexpected errors in the webhook handler
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        try: flask_logger.debug(f"Webhook body on error: {request.get_data(as_text=True)}")
        except: pass
        abort(500, description="Internal server error")


# --- Flask Server Runner ---
def run_flask():
    """Starts the Flask server (using Waitress for production)."""
    port = int(os.environ.get("PORT", 8080)) # Get port from environment or default
    flask_logger.info(f"Starting Flask server for webhooks on 0.0.0.0:{port}")
    try:
        # Use Waitress for a production-ready server
        from waitress import serve
        serve(flask_app, host='0.0.0.0', port=port, threads=8) # Adjust threads as needed
    except ImportError:
        # Fallback to Flask's development server if Waitress isn't installed
        # WARNING: Not suitable for production!
        flask_logger.warning("Waitress not found, falling back to Flask dev server (NOT FOR PRODUCTION!)")
        flask_app.run(host='0.0.0.0', port=port)
    except Exception as e:
        # Log critical errors if the server fails to start/run
        flask_logger.critical(f"Flask/Waitress server thread failed: {e}", exc_info=True)

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO, # Set base level to INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Reduce verbosity of noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO) # Keep Telegram API info
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Keep PTB extension info
logging.getLogger("sqlalchemy").setLevel(logging.WARNING) # Reduce SQLAlchemy noise
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Reduce Flask/Waitress request noise
logging.getLogger("telegraph_api").setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.INFO) # Keep Waitress info

logger = logging.getLogger(__name__) # Logger for this module

# --- Telegra.ph Setup ---
async def setup_telegraph_page(application: Application):
    """Creates or updates the Terms of Service page on Telegra.ph."""
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None # Initialize in bot_data
    access_token = config.TELEGRAPH_ACCESS_TOKEN
    if not access_token:
        logger.error("TELEGRAPH_ACCESS_TOKEN not set. Cannot create/update ToS page.")
        return

    # Use bot username if available, otherwise default
    author_name = application.bot_data.get('bot_username', "NunuAiBot")
    tos_title = f"Пользовательское Соглашение @{author_name}"
    page_url = None

    try:
        # Use the raw ToS text from handlers.py, remove bold markers for Telegraph
        # <<< ИЗМЕНЕНО: Убираем ** и * для Telegra.ph >>>
        tos_content_raw_for_telegraph = handlers.TOS_TEXT_RAW.replace("**", "").replace("*", "")
        if not tos_content_raw_for_telegraph or not isinstance(tos_content_raw_for_telegraph, str):
             logger.error("handlers.TOS_TEXT_RAW is empty or not a string. Cannot create ToS page.")
             return

        # Format with current config values
        tos_content_formatted_for_telegraph = tos_content_raw_for_telegraph.format(
            subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
            subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
            subscription_currency=config.SUBSCRIPTION_CURRENCY
        )

        # Convert plain text paragraphs to Telegraph node format
        # <<< ИЗМЕНЕНО: Обрабатываем строки, начинающиеся с цифры и точки как заголовки h4 >>>
        # <<< ИЗМЕНЕНО: Обрабатываем строки, начинающиеся с • или * как элементы списка ul/li >>>
        content_node_array = []
        current_list_items = []
        for p_raw in tos_content_formatted_for_telegraph.strip().splitlines():
            p = p_raw.strip()
            if not p: continue

            # Check for list items
            if p.startswith("• ") or p.startswith("* "):
                current_list_items.append({"tag": "li", "children": [p[2:].strip()]})
                continue # Continue collecting list items

            # If we were collecting list items, add the list now
            if current_list_items:
                content_node_array.append({"tag": "ul", "children": current_list_items})
                current_list_items = [] # Reset list

            # Check for headers (e.g., "1. О чем...")
            if re.match(r"^\d+\.\s+", p):
                content_node_array.append({"tag": "h4", "children": [p]})
            # Check for sub-headers (e.g., "3.1. Что...")
            elif re.match(r"^\d+\.\d+\.\s+", p):
                content_node_array.append({"tag": "h4", "children": [p]}) # Use h4 for sub-headers too
            else:
                # Default paragraph
                content_node_array.append({"tag": "p", "children": [p]})

        # Add any remaining list items at the end
        if current_list_items:
            content_node_array.append({"tag": "ul", "children": current_list_items})

        if not content_node_array:
            logger.error("content_node_array empty after processing. Cannot create page.")
            return

        # Convert node array to JSON string
        # <<< ИЗМЕНЕНО: Убеждаемся, что это JSON-строка МАССИВА >>>
        content_json_string = json.dumps(content_node_array, ensure_ascii=False)
        logger.debug(f"Telegraph content node array JSON: {content_json_string[:500]}...") # Log start of JSON

        # --- Direct HTTP Request to Telegra.ph API ---
        telegraph_api_url = "https://api.telegra.ph/createPage" # Or editPage if you store the path
        payload = {
            "access_token": access_token,
            "title": tos_title,
            "author_name": author_name,
            "content": content_json_string, # Pass content as JSON string array
            "return_content": False # We only need the URL
        }

        logger.info(f"Sending direct request to {telegraph_api_url} to create/update ToS page...")
        logger.debug(f"Telegraph payload: {payload}") # Log payload before sending

        async with httpx.AsyncClient() as client:
            # <<< ИЗМЕНЕНО: Используем json=payload для отправки JSON >>>
            response = await client.post(telegraph_api_url, json=payload)

        logger.info(f"Telegraph API direct response status: {response.status_code}")
        response.raise_for_status() # Raise HTTP errors

        response_data = response.json()
        logger.debug(f"Telegraph API direct response JSON: {response_data}")

        # Check response and extract URL
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
            # Log specific errors if known
            if "CONTENT_INVALID" in error_message or "PAGE_SAVE_FAILED" in error_message:
                 logger.error(f">>> Received error possibly related to content format! Check payload JSON structure or content.")
                 logger.debug(f"Content JSON sent: {content_json_string}")
            elif "ACCESS_TOKEN_INVALID" in error_message:
                 logger.error(">>> TELEGRAPH_ACCESS_TOKEN is invalid!")

    except httpx.HTTPStatusError as http_err:
         # Handle HTTP errors from Telegraph API
         logger.error(f"HTTP Status error during direct Telegraph request: {http_err.response.status_code} - {http_err.response.text}", exc_info=False)
    except httpx.RequestError as http_err:
         # Handle network errors connecting to Telegraph
         logger.error(f"HTTPX network error during direct Telegraph request: {http_err}", exc_info=True)
    except json.JSONDecodeError as json_err:
         # Handle errors encoding content or decoding response
         logger.error(f"Failed to dump content_node_array to JSON or decode response: {json_err}", exc_info=True)
    except Exception as e:
         # Catch-all for other errors during Telegraph setup
         logger.error(f"Unexpected error during direct Telegra.ph page creation: {e}", exc_info=True)

    # Store the final URL (or None if failed) in bot_data
    if page_url:
        application.bot_data['tos_url'] = page_url
        logger.info(f"Final ToS URL set in bot_data: {page_url}")
    else:
        logger.error("Failed to obtain Telegra.ph page URL using direct request.")


# --- Bot Post-Initialization ---
async def post_init(application: Application):
    """Runs after the bot application is initialized."""
    global application_instance
    application_instance = application # Store instance for webhook handler
    try:
        # Get bot info and store username
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username
        # Schedule Telegra.ph page setup
        asyncio.create_task(setup_telegraph_page(application))
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or scheduling setup_telegraph): {e}", exc_info=True)

    # --- Schedule Background Tasks ---
    logger.info("Starting background tasks...")
    if application.job_queue:
        # Task to reset daily message limits
        application.job_queue.run_repeating(
            tasks.reset_daily_limits_task,
            interval=timedelta(minutes=15), # Check every 15 minutes
            first=timedelta(seconds=15),    # Start 15 seconds after bot starts
            name="daily_limit_reset_check"
        )
        # Task to check for expired subscriptions
        application.job_queue.run_repeating(
            tasks.check_subscription_expiry_task,
            interval=timedelta(minutes=30), # Check every 30 minutes
            first=timedelta(seconds=30),    # Start 30 seconds after bot starts
            name="subscription_expiry_check",
            data=application # Pass application instance to the task
        )
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")


# --- Main Function ---
def main() -> None:
    """Starts the bot, Flask server, and background tasks."""
    logger.info("----- Bot Starting -----")

    # --- Initialize Database ---
    try:
        db.initialize_database()
        db.create_tables()
    except (OperationalError, SQLAlchemyError, RuntimeError, ValueError) as e:
         # Handle critical DB setup errors
         logger.critical(f"Database initialization failed: {e}. Exiting.")
         return # Stop execution if DB fails
    except Exception as e:
         logger.critical(f"An unexpected critical error during DB setup: {e}. Exiting.", exc_info=True)
         return
    logger.info("Database setup complete.")

    # --- Start Flask Webhook Server in a Separate Thread ---
    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    # --- Initialize Telegram Bot Application ---
    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    # Set default parse mode to MarkdownV2
    bot_defaults = Defaults(parse_mode=ParseMode.MARKDOWN_V2)

    # Build the application
    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30) # Connection timeout
        .read_timeout(60)    # Read timeout
        .write_timeout(60)   # Write timeout
        .pool_timeout(30)    # Pool timeout (for http connections)
        .defaults(bot_defaults) # Apply default settings
        .post_init(post_init)   # Run post_init function after setup
        .build()
    )
    logger.info("Telegram Application built.")

    # --- Conversation Handlers Definition ---
    # Edit Persona Conversation
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start), # Start via command
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_') # Start via button
            ],
        states={
            # State: Choosing what to edit
            handlers.EDIT_PERSONA_CHOICE: [
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')
                ],
            # State: Waiting for new text field value
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back button
                ],
            # State: Waiting for new max messages value
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Back button
                ],
            # State: Mood editing menu
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')
                ],
            # State: Waiting for new mood name
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back/Cancel button
                ],
            # State: Waiting for new mood prompt
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back/Cancel button
                ],
            # State: Confirming mood deletion
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'), # Confirm delete
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Cancel delete
                ]
        },
        fallbacks=[ # Handlers to exit the conversation
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$'),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^edit_moods_back_cancel$'), # Also cancel from mood sub-states
        ],
        per_message=False, # Conversation state tied to user, not message
        name="edit_persona_conversation",
        conversation_timeout=timedelta(minutes=15).total_seconds() # Timeout after 15 mins inactivity
    )

    # Delete Persona Conversation
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('deletepersona', handlers.delete_persona_start), # Start via command
            CallbackQueryHandler(handlers.delete_persona_button_callback, pattern='^delete_persona_') # Start via button
            ],
        states={
            # State: Waiting for confirmation
            handlers.DELETE_PERSONA_CONFIRM: [
                CallbackQueryHandler(handlers.delete_persona_confirmed, pattern='^delete_persona_confirm_'), # Confirm button
                CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$') # Cancel button
                ]
        },
        fallbacks=[ # Handlers to exit
            CommandHandler('cancel', handlers.delete_persona_cancel),
            CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')
            ],
        per_message=False,
        name="delete_persona_conversation",
        conversation_timeout=timedelta(minutes=5).total_seconds() # Shorter timeout for deletion confirm
    )
    logger.info("Conversation Handlers configured.")

    # --- Register Handlers ---
    logger.info("Registering handlers...")

    # Basic Commands
    application.add_handler(CommandHandler("start", handlers.start, block=False))
    application.add_handler(CommandHandler("help", handlers.help_command, block=False))
    application.add_handler(CommandHandler("menu", handlers.menu_command, block=False)) # Add /menu command
    application.add_handler(CommandHandler("profile", handlers.profile, block=False))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe, block=False))

    # Persona Management Commands
    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    # Edit and Delete commands are entry points for Conversation Handlers below

    # Add Conversation Handlers (order matters if commands overlap)
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)

    # In-Chat Commands (for active personas)
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))
    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False))

    # Message Handlers (should come after commands and conversations)
    # Handle photos/voices first
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    # Handle text messages last among message types
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # Callback Query Handler (handles button presses NOT part of conversations)
    # Needs to be added AFTER conversation handlers if they use callbacks in non-entry states
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler (must be last)
    application.add_error_handler(handlers.error_handler)

    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, # Process all update types
        drop_pending_updates=True,       # Ignore updates received while bot was offline
        timeout=20,                      # Polling timeout
        # read_timeout=30, # Use ApplicationBuilder settings instead
    )
    logger.info("----- Bot Stopped -----")

if __name__ == "__main__":
    main()
