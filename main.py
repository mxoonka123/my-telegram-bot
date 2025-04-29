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
import httpx # Используется для Telegra.ph
from typing import Optional # <<< ДОБАВЛЕНО ИСПРАВЛЕНИЕ

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler, Defaults
)
# <<< ИЗМЕНЕНО: Добавлен импорт для проверки подписки >>>
from telegram.constants import ParseMode, ChatMemberStatus # <<< ИЗМЕНЕНО: Используем ParseMode вместо TelegramParseMode
from telegram.error import TelegramError, Forbidden, BadRequest # <<< ИЗМЕНЕНО: Добавлены импорты

# Оставляем импорт Telegraph только для get_account_info и exceptions
from telegraph_api import Telegraph, exceptions as telegraph_exceptions
# Pydantic импортирован в requirements.txt, но здесь не используется напрямую

import config # Импортируем весь модуль config
import db
import handlers # Импортируем handlers
import tasks
from utils import escape_markdown_v2 # <<< ДОБАВЛЕНО

# --- Flask App for Yookassa Webhook ---
# Эта часть остается без изменений, но с улучшенным логированием и конфигурированием YK
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


# <<< ДОБАВЛЕНО: Глобальная переменная для application >>>
application_instance: Optional[Application] = None

@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    global application_instance # <<< ДОБАВЛЕНО: Используем глобальную переменную
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
             # Проверяем, инициализирован ли SDK правильно
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
                return Response(status=200) # Return 200 so YK doesn't retry
            try:
                telegram_user_id = int(metadata['telegram_user_id'])
            except (ValueError, TypeError) as e:
                flask_logger.error(f"Webhook error: Invalid 'telegram_user_id' {metadata.get('telegram_user_id')} for payment {payment.id}. Error: {e}")
                return Response(status=200)

            flask_logger.info(f"Attempting subscription activation for TG User ID: {telegram_user_id}")
            db_session = None
            activation_success = False # Флаг для отслеживания успеха активации
            user_db_id = None # ID пользователя в БД для логирования

            try:
                # Используем context manager get_db
                with db.get_db() as db_session:
                    user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                    if user:
                        user_db_id = user.id
                        if db.activate_subscription(db_session, user.id): # activate_subscription handles commit
                            flask_logger.info(f"Subscription activated for user {telegram_user_id} (DB ID: {user_db_id}) via webhook payment {payment.id}.")
                            activation_success = True
                        else:
                            flask_logger.error(f"db.activate_subscription failed for user {telegram_user_id} (DB ID: {user_db_id}) payment {payment.id}.")
                    else:
                        flask_logger.error(f"User with TG ID {telegram_user_id} not found for payment {payment.id}.")
            except SQLAlchemyError as e:
                 flask_logger.error(f"DB error during subscription activation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                 # Rollback handled by context manager
            except Exception as e:
                 flask_logger.error(f"Unexpected error during DB operation webhook user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                 # Rollback handled by context manager if needed

            # <<< ИЗМЕНЕНО: Отправляем уведомление ПОСЛЕ завершения работы с БД >>>
            if activation_success:
                app = application_instance # Получаем application из глобальной области видимости
                if app and app.bot:
                    # Формируем текст с MarkdownV2
                    success_text = (
                        escape_markdown_v2("✅ Ваша премиум подписка успешно активирована\\!\n") +
                        escape_markdown_v2(f"Срок действия: {config.SUBSCRIPTION_DURATION_DAYS} дней\\.\n\n") +
                        escape_markdown_v2("Спасибо за поддержку\\! 🎉\n\n") +
                        escape_markdown_v2("Используйте /profile для просмотра статуса\\.")
                    )
                    # Запускаем отправку в event loop, если он есть, иначе создаем новый
                    try:
                        # Пытаемся получить текущий цикл событий или создать новый
                        loop = asyncio.get_running_loop()
                    except RuntimeError: # 'RuntimeError: There is no current event loop...'
                        loop = None # Не создаем новый цикл здесь

                    if loop and loop.is_running():
                         # Если цикл есть и запущен (т.е. основной поток бота), используем его
                         future = asyncio.run_coroutine_threadsafe(
                            app.bot.send_message(chat_id=telegram_user_id, text=success_text, parse_mode=ParseMode.MARKDOWN_V2),
                            loop
                         )
                         try:
                             future.result(timeout=10) # Ждем завершения с таймаутом
                             flask_logger.info(f"Sent activation confirmation to user {telegram_user_id}")
                         except asyncio.TimeoutError:
                             flask_logger.error(f"Timeout sending activation confirmation to user {telegram_user_id}")
                         except TelegramError as te:
                             flask_logger.error(f"Telegram error sending activation message to {telegram_user_id}: {te}")
                             if isinstance(te, BadRequest) and "parse" in te.message.lower():
                                flask_logger.error(f"--> Failed activation text (escaped): '{success_text[:200]}...'")
                         except Exception as send_e:
                             flask_logger.error(f"Failed to send activation message to user {telegram_user_id}: {send_e}", exc_info=True)
                    else:
                         # Если цикла нет или он не запущен (например, при чистом запуске Flask без бота)
                         # создаем временный цикл только для этой задачи
                         flask_logger.warning("No running event loop found for webhook notification. Creating temporary loop.")
                         temp_loop = asyncio.new_event_loop()
                         try:
                              temp_loop.run_until_complete(app.bot.send_message(chat_id=telegram_user_id, text=success_text, parse_mode=ParseMode.MARKDOWN_V2))
                              flask_logger.info(f"Sent activation confirmation to user {telegram_user_id} using temporary loop.")
                         except TelegramError as te:
                              flask_logger.error(f"Telegram error sending activation message (temp loop) to {telegram_user_id}: {te}")
                              if isinstance(te, BadRequest) and "parse" in te.message.lower():
                                  flask_logger.error(f"--> Failed activation text (escaped, temp loop): '{success_text[:200]}...'")
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

        return Response(status=200) # Всегда отвечаем 200 OK Юкассе
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
    port = int(os.environ.get("PORT", 8080)) # Railway устанавливает переменную PORT
    flask_logger.info(f"Starting Flask server for webhooks on 0.0.0.0:{port}")
    try:
        # Используем waitress, т.к. он добавлен в requirements.txt
        from waitress import serve
        serve(flask_app, host='0.0.0.0', port=port, threads=8) # Увеличим количество потоков
    except ImportError:
        flask_logger.warning("Waitress not found, falling back to Flask dev server (NOT FOR PRODUCTION!)")
        flask_app.run(host='0.0.0.0', port=port)
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
logging.getLogger('waitress').setLevel(logging.INFO) # Логи веб-сервера

logger = logging.getLogger(__name__)

# --- Telegra.ph Setup ---
async def setup_telegraph_page(application: Application):
    logger.info("Setting up Telegra.ph ToS page...")
    application.bot_data['tos_url'] = None
    access_token = config.TELEGRAPH_ACCESS_TOKEN
    if not access_token:
        logger.error("TELEGRAPH_ACCESS_TOKEN not set. Cannot create/update ToS page.")
        return

    # --- ИСПОЛЬЗУЕМ ПРЯМОЙ ЗАПРОС К API ---
    author_name = "@NunuAiBot" # Или ваше имя автора
    tos_title = f"Пользовательское Соглашение @NunuAiBot"
    page_url = None

    try:
        # Берем НЕЭКРАНИРОВАННЫЙ текст с ** из handlers.py
        tos_content_raw_for_telegraph = handlers.TOS_TEXT_RAW
        if not tos_content_raw_for_telegraph or not isinstance(tos_content_raw_for_telegraph, str):
             logger.error("handlers.TOS_TEXT_RAW is empty or not a string. Cannot create ToS page.")
             return

        # Форматируем и убираем ** для Telegra.ph
        tos_content_formatted_for_telegraph = tos_content_raw_for_telegraph.format(
            subscription_duration=config.SUBSCRIPTION_DURATION_DAYS,
            subscription_price=f"{config.SUBSCRIPTION_PRICE_RUB:.0f}",
            subscription_currency=config.SUBSCRIPTION_CURRENCY
        ).replace("**", "") # Убираем жирный шрифт Markdown

        # Преобразуем строки в структуру узлов Telegra.ph
        paragraphs_raw = tos_content_formatted_for_telegraph.strip().splitlines()
        # Создаем узлы <p> для Telegra.ph, пропускаем пустые строки
        content_node_array = [{"tag": "p", "children": [p.strip()]} for p in paragraphs_raw if p.strip()]

        if not content_node_array:
            logger.error("content_node_array empty after processing. Cannot create page.")
            return

        # Сериализуем узлы в JSON строку для API
        content_json_string = json.dumps(content_node_array, ensure_ascii=False)

        telegraph_api_url = "https://api.telegra.ph/createPage"
        payload = {
            "access_token": access_token,
            "title": tos_title,
            "author_name": author_name,
            "content": content_json_string, # Передаем JSON строку
            "return_content": False # Нам не нужен контент в ответе
        }

        logger.info(f"Sending direct request to {telegraph_api_url} to create/update ToS page...")
        # logger.debug(f"Payload (content truncated): access_token=..., title='{tos_title}', author_name='{author_name}', content='{payload['content'][:200]}...', return_content=False")

        async with httpx.AsyncClient() as client:
            response = await client.post(telegraph_api_url, data=payload) # Используем data для form-encoded

        logger.info(f"Telegraph API direct response status: {response.status_code}")
        response.raise_for_status() # Вызовет исключение для кодов 4xx/5xx

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
    # --- КОНЕЦ ПРЯМОГО ЗАПРОСА К API ---


# --- Bot Initialization ---
async def post_init(application: Application):
    """Post-initialization tasks."""
    global application_instance # <<< ДОБАВЛЕНО: Сохраняем application для вебхука
    application_instance = application
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
        # Проверяем каждые 15 минут, чтобы быстрее сбросить лимиты после полуночи по UTC
        application.job_queue.run_repeating(tasks.reset_daily_limits_task, interval=timedelta(minutes=15), first=timedelta(seconds=15), name="daily_limit_reset_check")
        # Проверяем каждые 30 минут
        application.job_queue.run_repeating(tasks.check_subscription_expiry_task, interval=timedelta(minutes=30), first=timedelta(seconds=30), name="subscription_expiry_check", data=application) # Pass application object
        logger.info("Background tasks scheduled.")
    else:
        logger.warning("JobQueue not available, background tasks not scheduled.")


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
    bot_defaults = Defaults(parse_mode=ParseMode.MARKDOWN_V2) # Using V2 for more features

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30) # <<< ДОБАВЛЕНО: Таймаут для пула соединений
        .defaults(bot_defaults) # Apply defaults
        .post_init(post_init)
        .build()
    )
    logger.info("Telegram Application built.")

    # --- Conversation Handlers (Остаются без изменений в структуре) ---
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

    # Commands (block=False рекомендуется для асинхронности)
    application.add_handler(CommandHandler("start", handlers.start, block=False))
    application.add_handler(CommandHandler("help", handlers.help_command, block=False))
    application.add_handler(CommandHandler("profile", handlers.profile, block=False))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe, block=False)) # Shows initial subscribe info
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
    # block=False не нужен для CallbackQueryHandler, они обычно быстрые
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Error Handler (Should be last)
    application.add_error_handler(handlers.error_handler)

    logger.info("Handlers registered.")

    # --- Start Bot ---
    logger.info("Starting bot polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, # Process all update types
        drop_pending_updates=True, # Ignore updates received while bot was down
        timeout=20, # Таймаут для getUpdates
        read_timeout=30, # Таймаут для чтения ответа от Telegram
    )
    logger.info("----- Bot Stopped -----")

if __name__ == "__main__":
    main()
