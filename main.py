import logging
import asyncio
import os
from datetime import timedelta
import json

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
logging.getLogger("hypercorn").setLevel(logging.INFO)

# --- Импорты ваших модулей ---
import db
import handlers
import tasks
import config
from utils import escape_markdown_v2

# --- Импорты библиотек ---
from telegram.ext import (
    Application, Defaults, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler
)
from telegram import Update, BotCommand, Bot
from telegram.constants import ParseMode

from flask import Flask, request, abort, Response
from yookassa import Configuration as YookassaConfig
from yookassa.domain.notification import WebhookNotification
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from asgiref.wsgi import WsgiToAsgi
import threading

# --- Новые импорты для Telegra.ph ---
from telegraph import Telegraph
from telegraph.exceptions import TelegraphException
from handlers import formatted_tos_text_for_bot


# --- 1. Определение веб-сервера (Flask) ---
flask_app = Flask(__name__)
flask_logger = logging.getLogger('flask_webhook')

@flask_app.get("/")
def root_health():
    return "ok", 200

@flask_app.get("/healthz")
def healthz():
    return "ok", 200

try:
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY and config.YOOKASSA_SHOP_ID.isdigit():
        YookassaConfig.configure(account_id=int(config.YOOKASSA_SHOP_ID), secret_key=config.YOOKASSA_SECRET_KEY)
        flask_logger.info(f"Yookassa SDK configured for webhook (Shop ID: {config.YOOKASSA_SHOP_ID}).")
    else:
        flask_logger.warning("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY invalid/missing.")
except Exception as e:
    flask_logger.error(f"Failed to configure Yookassa SDK for webhook: {e}")

# Глобальные переменные для доступа к PTB Application и его event loop из вебхука
application_instance: Application | None = None
application_loop: asyncio.AbstractEventLoop | None = None
bot_swap_lock = threading.RLock()

async def process_telegram_update(update_data, token: str, bot_username_for_log: str) -> None:
    """Асинхронная функция для полной обработки одного Telegram-апдейта.
    Выполняет безопасную подмену application_instance.bot на временный инициализированный Bot,
    обрабатывает апдейт и гарантированно восстанавливает исходного бота.
    """
    global application_instance, bot_swap_lock
    if not application_instance:
        return

    user_bot = Bot(token=token)
    original_bot = application_instance.bot
    try:
        # КЛЮЧЕВОЕ: получаем getMe/username, чтобы CommandHandler мог корректно парсить команды вида /cmd@username
        await user_bot.initialize()
        update = Update.de_json(update_data, user_bot)

        # Подмена бота в защищенной секции
        with bot_swap_lock:
            application_instance.bot = user_bot

        await application_instance.process_update(update)

    except Exception as e:
        flask_logger.error(f"error processing telegram webhook for @{bot_username_for_log}: {e}", exc_info=True)
    finally:
        # Восстановление исходного бота и корректное выключение временного
        with bot_swap_lock:
            application_instance.bot = original_bot
        try:
            await user_bot.shutdown()
        except Exception:
            pass

@flask_app.route('/telegram/<string:token>', methods=['POST'])
def handle_telegram_webhook(token: str):
    """Синхронный обработчик, который запускает асинхронную обработку апдейта."""
    global application_instance, application_loop
    if not application_instance or application_loop is None:
        flask_logger.error("telegram webhook received but application is not fully initialized.")
        return Response(status=500)

    # Проверка токена и секрета по БД
    try:
        from db import get_db, BotInstance  # локальный импорт, чтобы избежать циклов
        with get_db() as db_session:
            bot_instance = db_session.query(BotInstance).filter(BotInstance.bot_token == token).first()
    except Exception as e:
        flask_logger.error(f"db error while fetching bot_instance for token ...{token[-6:]}: {e}")
        return Response(status=500)

    if not bot_instance or bot_instance.status != 'active':
        flask_logger.warning(f"webhook for unknown/inactive token ...{token[-6:]} (status={getattr(bot_instance, 'status', None)})")
        return Response(status=404)

    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or request.headers.get("x-telegram-bot-api-secret-token")
    if bot_instance.webhook_secret and secret_header != bot_instance.webhook_secret:
        flask_logger.error(f"invalid secret for bot @{bot_instance.telegram_username} (id={bot_instance.telegram_bot_id})")
        return Response(status=403)

    # Готовим апдейт
    try:
        update_data = request.get_json(force=True)
    except Exception:
        return Response(status=400)

    # Планируем асинхронную обработку апдейта в event loop PTB
    try:
        asyncio.run_coroutine_threadsafe(
            process_telegram_update(update_data, token, bot_instance.telegram_username or "unknown"),
            application_loop
        )
    except Exception as e:
        flask_logger.error(f"failed to schedule telegram update processing: {e}")
        return Response(status=500)

    # Возвращаем 200 сразу; обработка идет в фоне
    return Response(status=200)

@flask_app.route('/yookassa/webhook', methods=['POST'])
def handle_yookassa_webhook():
    """Обработчик вебхуков от YooKassa."""
    global application_instance, application_loop
    event_json = None
    try:
        event_json = request.get_json(force=True)
        # (Весь ваш код для обработки вебхука остается здесь без изменений)
        # Для краткости я его сокращу, но у вас он должен быть полностью
        notification_object = WebhookNotification(event_json)
        payment = notification_object.object
        flask_logger.info(f"Processing event: {notification_object.event}, Payment ID: {payment.id}, Status: {payment.status}")

        if notification_object.event == 'payment.succeeded' and payment.status == 'succeeded':
            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' missing in metadata for payment {payment.id}.")
                return Response(status=200)

            telegram_user_id = int(metadata['telegram_user_id'])
            
            # Активация подписки в БД
            with db.get_db() as db_session:
                user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                if user:
                    if db.activate_subscription(db_session, user.id):
                        flask_logger.info(f"Subscription activated for user {telegram_user_id} via webhook.")
                        # Отправка уведомления пользователю
                        if application_instance:
                            success_text_raw = (f"✅ ваша премиум подписка успешно активирована!\n"
                                                f"срок действия: {config.SUBSCRIPTION_DURATION_DAYS} дней.\n\n"
                                                f"спасибо за поддержку! 🎉")
                            success_text_escaped = escape_markdown_v2(success_text_raw)
                            
                            # Запускаем отправку в цикле событий asyncio
                            asyncio.run_coroutine_threadsafe(
                                application_instance.bot.send_message(
                                    chat_id=telegram_user_id,
                                    text=success_text_escaped,
                                    parse_mode=ParseMode.MARKDOWN_V2
                                ),
                                application_loop
                            )
        return Response(status=200)
    except Exception as e:
        flask_logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500)


async def create_or_update_tos_page(application: Application) -> None:
    """Creates or updates the Terms of Service page on Telegra.ph."""
    if not config.TELEGRAPH_ACCESS_TOKEN:
        logger.warning("TELEGRAPH_ACCESS_TOKEN not set. Cannot create or update ToS page.")
        return

    try:
        telegraph = Telegraph(access_token=config.TELEGRAPH_ACCESS_TOKEN)
        
        # Заменяем переносы строк на тег <p> для лучшего форматирования
        html_content = "".join(f"<p>{line}</p>" for line in formatted_tos_text_for_bot.splitlines() if line.strip())
        
        response = telegraph.create_page(
            title="Пользовательское соглашение",
            html_content=html_content,
            author_name=config.TELEGRAPH_AUTHOR_NAME,
            author_url=config.TELEGRAPH_AUTHOR_URL
        )
        
        tos_url = response['url']
        application.bot_data['tos_url'] = tos_url
        logger.info(f"Successfully created/updated ToS page: {tos_url}")

    except TelegraphException as e:
        logger.error(f"Failed to create ToS page on Telegra.ph: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while creating ToS page: {e}", exc_info=True)


# --- 2. Основная асинхронная функция запуска ---
async def main():
    """Запускает бота и веб-сервер в одной асинхронной среде."""
    
    # --- Инициализация БД ---
    logger.info("Initializing database...")
    try:
        db.initialize_database()
        db.create_tables()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Database initialization failed: {e}", exc_info=True)
        return

    # --- Создание экземпляра бота ---
    logger.info("Building PTB application...")
    global application_instance
    
    # Создаем билдер
    builder = Application.builder().token(config.TELEGRAM_TOKEN)
    
    # Настраиваем параметры
    builder.defaults(Defaults(parse_mode=ParseMode.MARKDOWN_V2, block=False))
    builder.pool_timeout(20.0).connect_timeout(20.0).read_timeout(30.0).write_timeout(30.0)
    builder.connection_pool_size(50)
    
    # Собираем приложение
    application = builder.build()
    application_instance = application # Сохраняем для вебхука

    # --- Публикация ToS ---
    await create_or_update_tos_page(application)

    # --- Регистрация хендлеров ---
    # (Вся ваша логика регистрации ConversationHandler, CommandHandler и т.д.)
    # --- Conversation Handlers Definition ---
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('editpersona', handlers.edit_persona_start), CallbackQueryHandler(handlers.edit_persona_button_callback, pattern=r'^edit_persona_\d+$')],
        states={
            handlers.EDIT_WIZARD_MENU: [CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^edit_wizard_|^finish_edit$|^back_to_wizard_menu$|^set_max_msgs_')],
            handlers.EDIT_NAME: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_name_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_DESCRIPTION: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_description_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_COMM_STYLE: [CallbackQueryHandler(handlers.edit_comm_style_received, pattern='^set_comm_style_|^back_to_wizard_menu$')],
            handlers.EDIT_VERBOSITY: [CallbackQueryHandler(handlers.edit_verbosity_received, pattern='^set_verbosity_|^back_to_wizard_menu$')],
            handlers.EDIT_GROUP_REPLY: [CallbackQueryHandler(handlers.edit_group_reply_received, pattern='^set_group_reply_|^back_to_wizard_menu$')],
            handlers.EDIT_MEDIA_REACTION: [CallbackQueryHandler(handlers.edit_media_reaction_received, pattern='^set_media_react_|^back_to_wizard_menu$')],
            handlers.EDIT_MAX_MESSAGES: [CallbackQueryHandler(handlers.edit_max_messages_received, pattern='^set_max_msgs_'), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
        },
        fallbacks=[CommandHandler('cancel', handlers.edit_persona_cancel), CallbackQueryHandler(handlers.edit_persona_finish, pattern='^finish_edit$'), CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_wizard$')],
        per_message=False, name="edit_persona_wizard", conversation_timeout=timedelta(minutes=15).total_seconds(), allow_reentry=True
    )
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('deletepersona', handlers.delete_persona_start), CallbackQueryHandler(handlers.delete_persona_button_callback, pattern=r'^delete_persona_\d+$')],
        states={handlers.DELETE_PERSONA_CONFIRM: [CallbackQueryHandler(handlers.delete_persona_confirmed, pattern=r'^delete_persona_confirm_\d+$'), CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')]},
        fallbacks=[CommandHandler('cancel', handlers.delete_persona_cancel), CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')],
        per_message=False, name="delete_persona_conversation", conversation_timeout=timedelta(minutes=5).total_seconds(), allow_reentry=True
    )
    bind_bot_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handlers.bind_bot_start, pattern=r'^bind_bot_\d+$')],
        states={
            handlers.REGISTER_BOT_TOKEN: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.bind_bot_token_received)]
        },
        fallbacks=[CommandHandler('cancel', handlers.edit_persona_cancel)],
        per_message=False, name="bind_bot_token_flow", conversation_timeout=timedelta(minutes=5).total_seconds(), allow_reentry=True
    )
    application.add_handler(edit_persona_conv_handler)
    application.add_handler(bind_bot_conv_handler)
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
    logger.info("All handlers registered.")

    # --- Запуск фоновых задач и веб-сервера в контексте приложения ---
    async with application:
        # Задачи, которые должны быть запущены до run_polling
        await application.initialize()

        # --- post_init логика ---
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username
        commands = [
            BotCommand("start", "начало работы"),
            BotCommand("menu", "главное меню"),
            BotCommand("help", "помощь"),
            BotCommand("subscribe", "подписка"),
            BotCommand("profile", "профиль"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Bot menu commands set.")
        
        # Запускаем фоновую задачу для проверки подписок
        if application.job_queue:
            application.job_queue.run_repeating(
                tasks.check_subscription_expiry_task,
                interval=timedelta(minutes=30),
                first=timedelta(seconds=10),
                name="subscription_expiry_check",
                data=application
            )
            logger.info("Subscription check task scheduled.")
        
        # --- Запуск веб-сервера ---
        port = int(os.environ.get("PORT", 8080))
        hypercorn_config = HypercornConfig()
        hypercorn_config.bind = [f"0.0.0.0:{port}"]
        
        # Запускаем веб-сервер как фоновую задачу asyncio
        # Flask (WSGI) оборачиваем в ASGI для Hypercorn
        asgi_app = WsgiToAsgi(flask_app)
        web_server_task = asyncio.create_task(serve(asgi_app, hypercorn_config))
        logger.info(f"Web server scheduled to run on port {port}.")
        
        # --- Запуск бота ---
        logger.info("Starting bot polling...")
        await application.start()
        await application.updater.start_polling()

        # Сохраняем текущий event loop для дальнейшей прокладки апдейтов из вебхука
        global application_loop
        application_loop = asyncio.get_running_loop()
        
        # Ждем, пока одна из задач не завершится (что не должно произойти)
        await web_server_task
        
        # Если мы сюда дошли, что-то пошло не так, останавливаем бота
        await application.updater.stop()
        await application.stop()


# --- 3. Точка входа ---
if __name__ == "__main__":
    logger.info("--- Application starting up ---")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application failed to run due to a critical error: {e}", exc_info=True)