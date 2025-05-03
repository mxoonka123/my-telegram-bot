# --- START OF FILE main.py ---

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
import re # <<< Убедитесь, что импорт есть

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
import handlers # handlers.py теперь содержит новые состояния
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
        payment_id_log = event_json.get('object', {}).get('id', 'N/A')
        flask_logger.info(f"Webhook received: Event='{event_json.get('event')}', Type='{event_json.get('type')}', PaymentID='{payment_id_log}'")
        flask_logger.debug(f"Webhook body: {json.dumps(event_json)}")

        if not config.YOOKASSA_SECRET_KEY or not config.YOOKASSA_SHOP_ID or not config.YOOKASSA_SHOP_ID.isdigit():
            flask_logger.error("YOOKASSA not configured correctly. Cannot process webhook.")
            return Response("Server configuration error", status=500)
        try:
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

        notification_object = WebhookNotification(event_json)
        payment = notification_object.object
        flask_logger.info(f"Processing event: {notification_object.event}, Payment ID: {payment.id}, Status: {payment.status}")

        if notification_object.event == 'payment.succeeded' and payment.status == 'succeeded':
            flask_logger.info(f"Successful payment detected: {payment.id}")
            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' missing in metadata for payment {payment.id}.")
                return Response(status=200)
            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' {metadata.get('telegram_user_id')} for payment {payment.id}. Error: {e}")
                return Response(status=200)

            flask_logger.info(f"Attempting subscription activation for TG User ID: {telegram_user_id} from Payment ID: {payment.id}")
            activation_success = False
            user_db_id = None

            try:
                with db.get_db() as db_session:
                    flask_logger.info(f"Webhook Payment {payment.id}: Searching for user with TG ID {telegram_user_id}")
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                    if user:
                        user_db_id = user.id
                        flask_logger.info(f"Webhook Payment {payment.id}: Found user DB ID {user_db_id}. Calling activate_subscription.")
                        if db.activate_subscription(db_session, user.id):
                            flask_logger.info(f"Subscription activated for user {telegram_user_id} (DB ID: {user_db_id}) via webhook payment {payment.id}.")
                            activation_success = True
                        else:
                            flask_logger.error(f"db.activate_subscription returned False for user {telegram_user_id} (DB ID: {user_db_id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with TG ID {telegram_user_id} not found for payment {payment.id}.")
            except SQLAlchemyError as e:
                 flask_logger.error(f"DB error during subscription activation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
            except Exception as e:
                 flask_logger.error(f"Unexpected error during DB operation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)

            if activation_success:
                app = application_instance
                if app and app.bot:
                    success_text_raw = (
                        f"✅ ваша премиум подписка успешно активирована!\n"
                        f"срок действия: {config.SUBSCRIPTION_DURATION_DAYS} дней.\n\n"
                        f"спасибо за поддержку! 🎉\n\n"
                        f"используйте /profile для просмотра статуса."
                    )
                    success_text_escaped = escape_markdown_v2(success_text_raw)

                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop and loop.is_running():
                         future = asyncio.run_coroutine_threadsafe(
                            app.bot.send_message(chat_id=telegram_user_id, text=success_text_escaped, parse_mode=ParseMode.MARKDOWN_V2),
                            loop
                         )
                         try:
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
                              temp_loop.close()

                else:
                    flask_logger.warning("Cannot send activation confirmation: Bot application instance not found in webhook context.")

        elif notification_object.event == 'payment.canceled':
             flask_logger.info(f"Payment {payment.id} was canceled.")
        elif notification_object.event == 'payment.waiting_for_capture':
             flask_logger.info(f"Payment {payment.id} is waiting for capture.")
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


# --- Flask Server Runner ---
def run_flask():
    """Starts the Flask server (using Waitress for production)."""
    port = int(os.environ.get("PORT", 8080))
    flask_logger.info(f"Starting Flask server for webhooks on 0.0.0.0:{port}")
    try:
        from waitress import serve
        serve(flask_app, host='0.0.0.0', port=port, threads=8)
    except ImportError:
        flask_logger.warning("Waitress not found, falling back to Flask dev server (NOT FOR PRODUCTION!)")
        flask_app.run(host='0.0.0.0', port=port)
    except Exception as e:
        flask_logger.critical(f"Flask/Waitress server thread failed: {e}", exc_info=True)

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("telegraph_api").setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.INFO)

logger = logging.getLogger(__name__) # Logger for this module

# --- Telegra.ph Setup ---
async def setup_telegraph_page(application: Application):
    """Creates or updates the Terms of Service page on Telegra.ph."""
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None
    access_token = config.TELEGRAPH_ACCESS_TOKEN
    if not access_token:
        logger.error("TELEGRAPH_ACCESS_TOKEN not set. Cannot create/update ToS page.")
        return

    bot_username = application.bot_data.get('bot_username', "NunuAiBot")
    author_name = bot_username
    tos_title = f"Пользовательское Соглашение @{bot_username}"
    page_url = None

    try:
        tos_content_raw_for_telegraph = handlers.TOS_TEXT_RAW.replace("**", "").replace("*", "")
        if not tos_content_raw_for_telegraph or not isinstance(tos_content_raw_for_telegraph, str):
             logger.error("handlers.TOS_TEXT_RAW is empty or not a string. Cannot create ToS page.")
             return

        tos_content_formatted_for_telegraph = tos_content_raw_for_telegraph.format(
            subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
            subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
            subscription_currency=config.SUBSCRIPTION_CURRENCY
        )

        content_node_array = []
        current_list_items = []
        for p_raw in tos_content_formatted_for_telegraph.strip().splitlines():
            p = p_raw.strip()
            if not p: continue

            if p.startswith("• ") or p.startswith("* "):
                current_list_items.append({"tag": "li", "children": [p[2:].strip()]})
                continue

            if current_list_items:
                content_node_array.append({"tag": "ul", "children": current_list_items})
                current_list_items = []

            if re.match(r"^\d+\.\s+", p):
                content_node_array.append({"tag": "h4", "children": [p]})
            elif re.match(r"^\d+\.\d+\.\s+", p):
                content_node_array.append({"tag": "h4", "children": [p]})
            else:
                content_node_array.append({"tag": "p", "children": [p]})

        if current_list_items:
            content_node_array.append({"tag": "ul", "children": current_list_items})

        if not content_node_array:
            logger.error("content_node_array empty after processing. Cannot create page.")
            return

        content_json_string = json.dumps(content_node_array, ensure_ascii=False)
        logger.debug(f"Telegraph content node array JSON: {content_json_string[:500]}...")

        telegraph_api_url = "https://api.telegra.ph/createPage"
        payload = {
            "access_token": access_token,
            "title": tos_title,
            "author_name": author_name,
            "content": content_json_string,
            "return_content": False
        }

        logger.info(f"Sending direct request to {telegraph_api_url} to create/update ToS page...")
        logger.debug(f"Telegraph payload: {payload}")

        async with httpx.AsyncClient() as client:
            response = await client.post(telegraph_api_url, json=payload)

        logger.info(f"Telegraph API direct response status: {response.status_code}")
        response.raise_for_status()

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
        asyncio.create_task(setup_telegraph_page(application))
    except Exception as e:
        logger.error(f"Failed during post_init (get_me or scheduling setup_telegraph): {e}", exc_info=True)

    logger.info("Starting background tasks...")
    if application.job_queue:
        application.job_queue.run_repeating(
            tasks.reset_daily_limits_task,
            interval=timedelta(minutes=15),
            first=timedelta(seconds=15),
            name="daily_limit_reset_check"
        )
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


# --- Main Function ---
def main() -> None:
    """Starts the bot, Flask server, and background tasks."""
    logger.info("----- Bot Starting -----")

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

    flask_thread = threading.Thread(target=run_flask, name="FlaskWebhookThread", daemon=True)
    flask_thread.start()
    logger.info("Flask thread for Yookassa webhook started.")

    logger.info("Initializing Telegram Bot Application...")
    token = config.TELEGRAM_TOKEN
    if not token:
        logger.critical("TELEGRAM_TOKEN not found in config. Exiting.")
        return

    bot_defaults = Defaults(parse_mode=ParseMode.MARKDOWN_V2)

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .defaults(bot_defaults)
        .post_init(post_init)
        .build()
    )
    logger.info("Telegram Application built.")

    # --- Conversation Handlers Definition ---

    # <<< ИЗМЕНЕНО: Обновление ConversationHandler для визарда редактирования >>>
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('editpersona', handlers.edit_persona_start),
            CallbackQueryHandler(handlers.edit_persona_button_callback, pattern='^edit_persona_')
        ],
        states={
            # Главное меню визарда
            handlers.EDIT_WIZARD_MENU: [
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^edit_wizard_|^finish_edit$')
            ],
            # Состояния для ввода текстовых полей
            handlers.EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_name_received)],
            handlers.EDIT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_description_received)],
            # Состояния для выбора опций (обработчики получают callback)
            handlers.EDIT_COMM_STYLE: [CallbackQueryHandler(handlers.edit_comm_style_received, pattern='^set_comm_style_|^back_to_wizard_menu$')],
            handlers.EDIT_VERBOSITY: [CallbackQueryHandler(handlers.edit_verbosity_received, pattern='^set_verbosity_|^back_to_wizard_menu$')],
            handlers.EDIT_GROUP_REPLY: [CallbackQueryHandler(handlers.edit_group_reply_received, pattern='^set_group_reply_|^back_to_wizard_menu$')],
            handlers.EDIT_MEDIA_REACTION: [CallbackQueryHandler(handlers.edit_media_reaction_received, pattern='^set_media_react_|^back_to_wizard_menu$')],
            # Точка входа в под-диалог настроений
            handlers.EDIT_MOODS_ENTRY: [CallbackQueryHandler(handlers.edit_moods_entry)], # Этот обработчик переведет в EDIT_MOOD_CHOICE
            # Состояния под-диалога настроений (остаются как были)
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^back_to_wizard_menu$|^edit_moods_back_cancel$') # Добавлен back_to_wizard_menu
            ],
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back/Cancel button
            ],
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Back/Cancel button
            ],
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'), # Confirm delete
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Cancel delete
            ]
        },
        fallbacks=[ # Обработчики для выхода из диалога
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^finish_edit$'), # Завершение через кнопку
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^edit_moods_back_cancel$'), # Отмена из под-диалога настроений
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^back_to_wizard_menu$'), # Кнопка "Назад" в шагах визарда тоже может отменять
        ],
        per_message=False,
        name="edit_persona_wizard", # Новое имя для ясности
        conversation_timeout=timedelta(minutes=15).total_seconds()
    )

    # Delete Persona Conversation (без изменений)
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
    application.add_handler(CommandHandler("start", handlers.start, block=False))
    application.add_handler(CommandHandler("help", handlers.help_command, block=False))
    application.add_handler(CommandHandler("menu", handlers.menu_command, block=False))
    application.add_handler(CommandHandler("profile", handlers.profile, block=False))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe, block=False))
    application.add_handler(CommandHandler("placeholders", handlers.placeholders_command, block=False))

    # Persona Management Commands
    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))

    # Add Conversation Handlers
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(delete_persona_conv_handler)

    # In-Chat Commands
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))
    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))
    application.add_handler(CommandHandler("mutebot", handlers.mute_bot, block=False))
    application.add_handler(CommandHandler("unmutebot", handlers.unmute_bot, block=False))

    # Message Handlers
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # Callback Query Handler (for non-conversation buttons)
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler
    application.add_error_handler(handlers.error_handler)

    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=20,
    )
    logger.info("----- Bot Stopped -----")

if __name__ == "__main__":
    main()

# --- END OF FILE main.py ---
