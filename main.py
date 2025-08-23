import logging
import asyncio
import os
from datetime import timedelta
import signal
import json
import uuid

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
    ConversationHandler, ChatMemberHandler
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
        # Восстановление исходного бота. Не выключаем временный user_bot сразу,
        # чтобы не ломать отложенные send_message ("HTTPXRequest is not initialized").
        with bot_swap_lock:
            application_instance.bot = original_bot
        # Пропускаем await user_bot.shutdown(); ресурсы будут собраны GC/реюзнуты.

@flask_app.route('/telegram/<string:token>', methods=['POST'])
def handle_telegram_webhook(token: str):
    """Синхронный обработчик, который запускает асинхронную обработку апдейта."""
    global application_instance, application_loop
    if not application_instance or application_loop is None:
        flask_logger.error("telegram webhook received but application is not fully initialized.")
        return Response(status=500)

    # Проверка токена и секрета по БД
    try:
        from db import get_db, BotInstance, User  # локальный импорт, чтобы избежать циклов
        from sqlalchemy.orm import selectinload
        with get_db() as db_session:
            bot_instance = (
                db_session.query(BotInstance)
                .options(selectinload(BotInstance.owner))
                .filter(BotInstance.bot_token == token)
                .first()
            )
    except Exception as e:
        flask_logger.error(f"db error while fetching bot_instance for token ...{token[-6:]}: {e}")
        return Response(status=500)

    if not bot_instance or bot_instance.status != 'active':
        # Самовосстановление для основного бота: если токен совпадает, пытаемся заново создать/активировать инстанс
        try:
            if token == getattr(config, 'TELEGRAM_TOKEN', None):
                flask_logger.warning("main bot token received but instance is unknown/inactive -> attempting self-heal upsert")
                with db.get_db() as _s:
                    # владелец = первый админ
                    owner_tg_id = None
                    try:
                        owner_tg_id = (config.ADMIN_USER_ID[0] if getattr(config, 'ADMIN_USER_ID', None) else None)
                    except Exception:
                        owner_tg_id = None
                    if owner_tg_id:
                        owner = _s.query(db.User).filter(db.User.telegram_id == owner_tg_id).first() or db.get_or_create_user(_s, owner_tg_id, username="admin")
                        persona = _s.query(db.PersonaConfig).filter(
                            db.PersonaConfig.owner_id == owner.id,
                            db.PersonaConfig.name == 'Main Bot'
                        ).first() or db.create_persona_config(_s, owner_id=owner.id, name='Main Bot', description='System main bot persona')
                        # Узнаем данные бота из application_instance
                        me_id = application_instance and application_instance.bot_data.get('main_bot_id')
                        me_username = application_instance and application_instance.bot_data.get('main_bot_username')
                        inst, st = db.set_bot_instance_token(_s, owner.id, persona.id, token, me_id or "", me_username or "")
                        try:
                            if inst is not None and hasattr(inst, 'access_level') and inst.access_level != 'public':
                                inst.access_level = 'public'
                                _s.commit()
                        except Exception:
                            _s.rollback()
                # Возвращаем 200: Telegram перешлёт апдейты снова, а инстанс уже будет восстановлен
                return Response(status=200)
        except Exception as _heal_err:
            flask_logger.error(f"self-heal upsert for main bot failed: {_heal_err}", exc_info=True)
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

    # --- ACL: проверка доступа ---
    try:
        actor_id = None
        if isinstance(update_data, dict):
            # самые частые случаи
            actor_id = (
                update_data.get('message', {}).get('from', {}).get('id') or
                update_data.get('edited_message', {}).get('from', {}).get('id') or
                (update_data.get('callback_query', {}) or {}).get('from', {}).get('id') or
                (update_data.get('my_chat_member', {}) or {}).get('from', {}).get('id') or
                (update_data.get('chat_member', {}) or {}).get('from', {}).get('id')
            )

        # Если не нашли отправителя (например, channel_post) — просто пропускаем ACL (не пользовательская инициатива)
        if actor_id:
            owner_tg_id = bot_instance.owner.telegram_id if bot_instance.owner else None
            access_level = (bot_instance.access_level or 'owner_only').lower()

            allowed = False
            # 1) Полный доступ для админов системы независимо от владения ботом/ACL
            try:
                if int(actor_id) in (getattr(config, 'ADMIN_USER_ID', []) or []):
                    allowed = True
            except Exception:
                pass
            # 2) Владелец бота
            if not allowed and owner_tg_id and int(actor_id) == int(owner_tg_id):
                allowed = True  # владелец всегда имеет доступ
            # 3) Режимы доступа
            elif not allowed and access_level == 'public':
                allowed = True
            elif not allowed and access_level == 'owner_only':
                allowed = False
            elif not allowed and access_level == 'whitelist':
                try:
                    import json as _json
                    wl = _json.loads(bot_instance.whitelisted_users_json or '[]')
                    wl_ids = {int(x) for x in wl if str(x).strip()}
                    allowed = int(actor_id) in wl_ids
                except Exception:
                    allowed = False

            if not allowed:
                # молча игнорируем апдейт для неавторизованных пользователей
                flask_logger.info(
                    f"access denied for user {actor_id} on bot @{bot_instance.telegram_username} (access_level={access_level})"
                )
                return Response(status=200)
    except Exception as e:
        flask_logger.error(f"acl check failed (fallback to deny): {e}", exc_info=True)
        return Response(status=200)

    # --- Disable commands on attached (non-main) bots, except a small allowlist ---
    try:
        is_command_update = False
        is_callback_update = False
        is_private_chat = False
        text = ''
        if isinstance(update_data, dict):
            # message/edited_message branch
            msg = update_data.get('message') or update_data.get('edited_message')
            if msg:
                entities = msg.get('entities') or []
                text = msg.get('text') or ''
                is_command_update = any((e or {}).get('type') == 'bot_command' for e in entities) or text.startswith('/')
                try:
                    chat_type_val = ((msg.get('chat') or {}).get('type'))
                    is_private_chat = str(chat_type_val) == 'private'
                except Exception:
                    is_private_chat = False
            # callback_query branch (нажатия на inline-кнопки)
            if update_data.get('callback_query'):
                is_callback_update = True

        main_bot_id = application_instance and application_instance.bot_data.get('main_bot_id')
        current_bot_id = bot_instance.telegram_bot_id
        # 0) Главный бот: игнорируем только НЕ-командные апдейты в НЕ-приватных чатах и не callback'и
        #    Разрешаем:
        #      - команды
        #      - callback_query (кнопки)
        #      - любые сообщения в приватных чатах (для ввода токена и т.п.)
        if (not is_command_update) and (not is_callback_update) and (not is_private_chat) and \
           main_bot_id and str(main_bot_id) == str(current_bot_id or ''):
            flask_logger.info(
                f"skip non-command non-callback non-private update for main bot @{bot_instance.telegram_username} (id={current_bot_id})"
            )
            return Response(status=200)
        # 1) Attached-боты: блокируем команды, кроме allowlist
        if is_command_update and main_bot_id and str(main_bot_id) != str(current_bot_id or ''):
            # Полный запрет команд на attached-ботах
            allowed_on_attached = set()
            cmd_name = None
            try:
                first_token = (text or '').split()[0].lower()
                # учитываем формат /cmd@username
                cmd_name = first_token.split('@')[0]
            except Exception:
                cmd_name = None
            if cmd_name not in allowed_on_attached:
                flask_logger.info(
                    f"skip command update for attached bot @{bot_instance.telegram_username} (bot_id={current_bot_id}, main_id={main_bot_id})"
                )
                return Response(status=200)
    except Exception as e:
        flask_logger.error(f"error while checking command disable for attached bots: {e}")

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
            metadata = payment.metadata or {}
            if 'telegram_user_id' not in metadata:
                flask_logger.error(f"Webhook error: 'telegram_user_id' missing in metadata for payment {payment.id}.")
                return Response(status=200)

            telegram_user_id = int(metadata['telegram_user_id'])
            pkg_id = metadata.get('package_id')
            credits_to_add = 0.0
            try:
                credits_to_add = float(metadata.get('credits', 0))
            except Exception:
                credits_to_add = 0.0

            # Начисление кредитов в БД
            with db.get_db() as db_session:
                user = db_session.query(db.User).filter(db.User.telegram_id == telegram_user_id).first()
                if not user:
                    flask_logger.error(f"Webhook: user {telegram_user_id} not found for payment {payment.id}.")
                    return Response(status=200)

                if credits_to_add <= 0:
                    # Попытка определить из конфига по package_id
                    try:
                        if pkg_id and pkg_id in (config.CREDIT_PACKAGES or {}):
                            credits_to_add = float(config.CREDIT_PACKAGES[pkg_id]['credits'])
                    except Exception:
                        pass

                user.credits = float(user.credits or 0) + float(credits_to_add or 0)
                db_session.commit()

                flask_logger.info(f"Credited {credits_to_add} credits to user {telegram_user_id} via webhook. New balance: {user.credits}")

                # Отправка уведомления пользователю
                if application_instance:
                    try:
                        pkg_title = None
                        try:
                            if pkg_id and pkg_id in (config.CREDIT_PACKAGES or {}):
                                pkg_title = config.CREDIT_PACKAGES[pkg_id].get('title')
                        except Exception:
                            pkg_title = None
                        credited_part = f"Зачислено {credits_to_add:.0f} кредитов" if credits_to_add else "Оплата успешно проведена"
                        pkg_part = f" ({pkg_title})" if pkg_title else ""
                        success_text_raw = (
                            f"✅ {credited_part}{pkg_part}.\n"
                            f"Текущий баланс: {user.credits:.2f} кредитов.\n\n"
                            f"Спасибо за поддержку! 🎉"
                        )
                        success_text_escaped = escape_markdown_v2(success_text_raw)
                        asyncio.run_coroutine_threadsafe(
                            application_instance.bot.send_message(
                                chat_id=telegram_user_id,
                                text=success_text_escaped,
                                parse_mode=ParseMode.MARKDOWN_V2
                            ),
                            application_loop
                        )
                    except Exception as notify_e:
                        flask_logger.error(f"Failed to notify user {telegram_user_id} about credit top-up: {notify_e}")
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
            handlers.EDIT_WIZARD_MENU: [
                CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^edit_wizard_|^finish_edit$|^back_to_wizard_menu$|^set_max_msgs_|^start_char_wizard$')
            ],
            handlers.EDIT_NAME: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_name_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_DESCRIPTION: [MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.edit_description_received), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_GROUP_REPLY: [CallbackQueryHandler(handlers.edit_group_reply_received, pattern='^set_group_reply_|^back_to_wizard_menu$')],
            handlers.EDIT_MEDIA_REACTION: [CallbackQueryHandler(handlers.edit_media_reaction_received, pattern='^set_media_react_|^back_to_wizard_menu$')],
            handlers.EDIT_MAX_MESSAGES: [CallbackQueryHandler(handlers.edit_max_messages_received, pattern='^set_max_msgs_'), CallbackQueryHandler(handlers.edit_wizard_menu_handler, pattern='^back_to_wizard_menu$')],
            handlers.EDIT_PROACTIVE_RATE: [CallbackQueryHandler(handlers.edit_proactive_rate_received, pattern='^set_proactive_|^back_to_wizard_menu$')],
            # Character Setup Wizard states
            handlers.CHAR_WIZ_BIO: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_bio_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_TRAITS: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_traits_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_SPEECH: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_speech_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_LIKES: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_likes_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_DISLIKES: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_dislikes_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_GOALS: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_goals_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
            handlers.CHAR_WIZ_TABOOS: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.char_wiz_taboos_received),
                CallbackQueryHandler(handlers.char_wiz_skip, pattern='^charwiz_skip$'),
                CallbackQueryHandler(handlers.char_wiz_cancel, pattern='^charwiz_cancel$')
            ],
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
    # --- Botsettings (ACL/Whitelist) Conversation ---
    botsettings_conv = ConversationHandler(
        entry_points=[CommandHandler('botsettings', handlers.botsettings_start)],
        states={
            handlers.BOTSET_SELECT: [CallbackQueryHandler(handlers.botsettings_pick, pattern=r'^botset_pick_\d+$')],
            handlers.BOTSET_MENU: [
                CallbackQueryHandler(handlers.botsettings_set_access, pattern=r'^botset_access_(public|whitelist|owner_only)$'),
                CallbackQueryHandler(handlers.botsettings_mute, pattern=r'^botset_mute$'),
                CallbackQueryHandler(handlers.botsettings_unmute, pattern=r'^botset_unmute$'),
                CallbackQueryHandler(handlers.botsettings_wl_show, pattern=r'^botset_wl_show$'),
                CallbackQueryHandler(handlers.botsettings_wl_add_prompt, pattern=r'^botset_wl_add$'),
                CallbackQueryHandler(handlers.botsettings_wl_remove_prompt, pattern=r'^botset_wl_remove$'),
                CallbackQueryHandler(handlers.botsettings_back, pattern=r'^botset_back$'),
                CallbackQueryHandler(handlers.botsettings_close, pattern=r'^botset_close$'),
            ],
            handlers.BOTSET_WHITELIST_ADD: [
                MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.botsettings_wl_add_receive),
                CallbackQueryHandler(handlers.botsettings_back, pattern=r'^botset_back$')
            ],
            handlers.BOTSET_WHITELIST_REMOVE: [
                CallbackQueryHandler(handlers.botsettings_wl_remove_confirm, pattern=r'^botset_wl_del_\d+$'),
                CallbackQueryHandler(handlers.botsettings_back, pattern=r'^botset_back$')
            ],
        },
        fallbacks=[CommandHandler('cancel', handlers.botsettings_close)],
        per_message=False, name="botsettings_conv", conversation_timeout=timedelta(minutes=10).total_seconds(), allow_reentry=True
    )
    application.add_handler(botsettings_conv)

    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("buycredits", handlers.buycredits))
    application.add_handler(CommandHandler("createpersona", handlers.create_persona))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas))
    application.add_handler(CommandHandler("mood", handlers.mood))
    application.add_handler(CommandHandler("reset", handlers.reset))
    application.add_handler(CommandHandler("clear", handlers.reset))
    # Разрешаем mute/unmute для каждого бота отдельно
    application.add_handler(CommandHandler("mutebot", handlers.mutebot))
    application.add_handler(CommandHandler("unmutebot", handlers.unmutebot))
    application.add_handler(MessageHandler(handlers.filters.PHOTO & ~handlers.filters.COMMAND, handlers.handle_photo))
    application.add_handler(MessageHandler(handlers.filters.VOICE & ~handlers.filters.COMMAND, handlers.handle_voice))
    application.add_handler(MessageHandler(handlers.filters.TEXT & ~handlers.filters.COMMAND, handlers.handle_message))
    # Обработчик обновлений статуса бота в чатах (для автопривязки/отвязки в группах)
    application.add_handler(ChatMemberHandler(handlers.on_my_chat_member, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(handlers.buycredits_pkg_callback, pattern=r'^buycredits_pkg_'))
    application.add_handler(CallbackQueryHandler(handlers.buycredits, pattern=r'^buycredits_open$'))
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))
    application.add_error_handler(handlers.error_handler)
    logger.info("All handlers registered.")

    # --- Запуск фоновых задач и веб-сервера в контексте приложения ---
    async with application:
        # Инициализация приложения
        await application.initialize()

        # Режим запуска: webhook (по умолчанию для Railway) или polling
        run_mode = os.environ.get("RUN_MODE", "webhook").strip().lower()
        logger.info(f"RUN_MODE={run_mode}")

        # Общая пост-инициализация
        me = await application.bot.get_me()
        logger.info(f"Bot started as @{me.username} (ID: {me.id})")
        application.bot_data['bot_username'] = me.username
        application.bot_data['main_bot_id'] = me.id
        commands = [
            BotCommand("start", "начало работы"),
            BotCommand("menu", "главное меню"),
            BotCommand("help", "помощь"),
            BotCommand("profile", "профиль и баланс"),
            BotCommand("buycredits", "пополнить кредиты"),
            BotCommand("botsettings", "настройки бота (ACL)"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Bot menu commands set.")

        # --- Авто-upsert главного бота в БД и установка вебхука ---
        try:
            if not config.WEBHOOK_URL_BASE:
                logger.warning("WEBHOOK_URL_BASE не задан, пропускаю авто-настройку вебхука для главного бота.")
            else:
                with db.get_db() as db_session:
                    # Определяем владельца: используем первого ADMIN_USER_ID, если задан
                    owner_tg_id = None
                    try:
                        owner_tg_id = (config.ADMIN_USER_ID[0] if getattr(config, 'ADMIN_USER_ID', None) else None)
                    except Exception:
                        owner_tg_id = None

                    if not owner_tg_id:
                        logger.warning("ADMIN_USER_ID пуст. Пропускаю авто-upsert главного бота (нет владельца).")
                    else:
                        # Получаем/создаем пользователя-владельца
                        user = db_session.query(db.User).filter(db.User.telegram_id == owner_tg_id).first()
                        if not user:
                            user = db.get_or_create_user(db_session, owner_tg_id, username="admin")
                            db_session.commit();
                            try:
                                db_session.refresh(user)
                            except Exception:
                                pass

                        # Получаем/создаем специальную персону для главного бота
                        persona = db_session.query(db.PersonaConfig).filter(
                            db.PersonaConfig.owner_id == user.id,
                            db.PersonaConfig.name == 'Main Bot'
                        ).first()
                        if not persona:
                            persona = db.create_persona_config(db_session, owner_id=user.id, name='Main Bot', description='System main bot persona')
                            db_session.commit();
                            try:
                                db_session.refresh(persona)
                            except Exception:
                                pass

                        # Создаем/обновляем BotInstance для главного бота
                        instance, status = db.set_bot_instance_token(
                            db_session,
                            owner_id=user.id,
                            persona_config_id=persona.id,
                            token=config.TELEGRAM_TOKEN,
                            bot_id=me.id,
                            bot_username=me.username
                        )
                        # Делаем главный бот публичным
                        try:
                            if instance is not None and hasattr(instance, 'access_level') and instance.access_level != 'public':
                                instance.access_level = 'public'
                                db_session.commit()
                        except Exception:
                            db_session.rollback()

                        # Устанавливаем webhook для главного бота
                        webhook_url = f"{config.WEBHOOK_URL_BASE}/telegram/{config.TELEGRAM_TOKEN}"
                        secret = str(uuid.uuid4())
                        try:
                            await application.bot.set_webhook(
                                url=webhook_url,
                                allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
                                secret_token=secret
                            )
                            # Сохраняем секрет/время/статус
                            from datetime import datetime, timezone as _tz
                            try:
                                if instance is not None:
                                    if hasattr(instance, 'webhook_secret'):
                                        instance.webhook_secret = secret
                                    if hasattr(instance, 'last_webhook_set_at'):
                                        instance.last_webhook_set_at = datetime.now(_tz.utc)
                                    if hasattr(instance, 'status'):
                                        instance.status = 'active'
                                    db_session.commit()
                            except Exception as e_commit:
                                logger.error(f"Auto-upsert main bot: commit failed after set_webhook: {e_commit}", exc_info=True)
                                db_session.rollback()
                            logger.info(f"Main bot webhook set to {webhook_url}")
                        except Exception as e_webhook:
                            logger.error(f"Failed to set webhook for main bot @{me.username}: {e_webhook}", exc_info=True)
                            try:
                                if instance is not None and hasattr(instance, 'status'):
                                    instance.status = 'webhook_error'
                                    db_session.commit()
                            except Exception:
                                db_session.rollback()
        except Exception as e_auto:
            logger.error(f"Auto-upsert of main bot failed: {e_auto}", exc_info=True)

        # Подготовка к graceful shutdown
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
        except NotImplementedError:
            # На Windows сигналов может не быть — игнорируем
            pass

        global application_loop
        application_loop = asyncio.get_running_loop()

        web_server_task = None

        # Общая фон. задача проактивных сообщений
        proactive_task = None

        if run_mode == 'webhook':
            # Запускаем только веб-сервер для приема вебхуков; polling не запускаем
            port = int(os.environ.get("PORT", 8080))
            hypercorn_config = HypercornConfig()
            hypercorn_config.bind = [f"0.0.0.0:{port}"]

            asgi_app = WsgiToAsgi(flask_app)

            # Запускаем PTB (без polling), чтобы работали контексты/очереди
            await application.start()
            # Старт фоновой задачи проактивных сообщений
            try:
                proactive_task = asyncio.create_task(tasks.proactive_messaging_task(application))
            except Exception as e:
                logger.error(f"failed to start proactive_messaging_task: {e}")

            web_server_task = asyncio.create_task(serve(asgi_app, hypercorn_config))
            logger.info(f"Web server running on port {port} (webhook mode). Waiting for shutdown signal...")

            # Ждём сигнал остановки
            await stop_event.wait()

            logger.info("Shutdown signal received. Stopping web server and application...")
            if web_server_task:
                web_server_task.cancel()
                try:
                    await web_server_task
                except asyncio.CancelledError:
                    pass

            # Останавливаем фоновую задачу
            if proactive_task:
                proactive_task.cancel()
                try:
                    await proactive_task
                except asyncio.CancelledError:
                    pass

            await application.stop()
            await application.shutdown()

        else:
            # Polling mode: запускаем только polling без веб-сервера
            await application.start()
            logger.info("Starting polling (no web server)...")
            # Старт фоновой задачи проактивных сообщений
            proactive_task = None
            try:
                proactive_task = asyncio.create_task(tasks.proactive_messaging_task(application))
            except Exception as e:
                logger.error(f"failed to start proactive_messaging_task: {e}")

            await application.updater.start_polling()

            # Ждем сигнал остановки
            await stop_event.wait()

            logger.info("Shutdown signal received. Stopping polling and application...")
            # Останавливаем фоновую задачу
            if proactive_task:
                proactive_task.cancel()
                try:
                    await proactive_task
                except asyncio.CancelledError:
                    pass
            await application.updater.stop()
            await application.stop()
            await application.shutdown()


# --- 3. Точка входа ---
if __name__ == "__main__":
    logger.info("--- Application starting up ---")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Application failed to run due to a critical error: {e}", exc_info=True)