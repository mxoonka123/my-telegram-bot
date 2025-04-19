import logging
import asyncio
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler, ConversationHandler
)

import config
import db
import handlers
import tasks

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def post_init(application: Application):
    me = await application.bot.get_me()
    logger.info(f"Bot started as @{me.username}")

    # Убедимся, что задачи запускаются
    asyncio.create_task(tasks.spam_task(application))
    asyncio.create_task(tasks.reset_daily_limits_task())
    asyncio.create_task(tasks.check_subscription_expiry_task(application)) # Передаем application для уведомлений


def main() -> None:
    logger.info("Creating database tables if they don't exist...")
    db.create_tables()
    logger.info("Database setup complete.")

    # Укажем таймауты для соединений
    application = Application.builder().token(config.TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).build()

    # --- Edit Persona Conversation Handler ---
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('editpersona', handlers.edit_persona_start)],
        states={
            handlers.EDIT_PERSONA_CHOICE: [
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$|^edit_persona_back$')
            ],
            handlers.EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update),
                CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Кнопка Назад
            ],
            # Новое состояние для ввода числа
            handlers.EDIT_MAX_MESSAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_max_messages_update),
                 CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Кнопка Назад
            ],
            handlers.EDIT_MOOD_CHOICE: [
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$|^edit_moods_back_cancel$')
            ],
            handlers.EDIT_MOOD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Кнопка Назад
            ],
            handlers.EDIT_MOOD_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Кнопка Назад
            ],
            handlers.DELETE_MOOD_CONFIRM: [
                CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'),
                CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back_cancel$') # Кнопка Назад/Отмена
            ]
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$') # Общая отмена
        ],
        per_message=False, # Одна беседа на пользователя+чат
        conversation_timeout=timedelta(minutes=15).total_seconds() # Таймаут беседы 15 минут
    )

    # --- Delete Persona Conversation Handler ---
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
        conversation_timeout=timedelta(minutes=5).total_seconds() # Таймаут удаления 5 минут
    )


    # --- Добавляем обработчики ---
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))

    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    application.add_handler(edit_persona_conv_handler) # Добавляем ConvHandler для редактирования
    application.add_handler(delete_persona_conv_handler)# Добавляем ConvHandler для удаления
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))

    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))

    # Обработчик текста должен идти ПОСЛЕ ConvHandlers, чтобы не перехватывать их ввод
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # Обработчик коллбэков для кнопок вне диалогов (подписка, смена настроения из /mood)
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query, pattern='^set_mood_|^subscribe_'))

    # Обработчик ошибок
    application.add_error_handler(handlers.error_handler)

    # Функция после инициализации
    application.post_init = post_init

    logger.info("Starting bot polling...")
    # Запускаем с обработкой всех типов обновлений
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True) # drop_pending_updates может помочь при перезапусках


if __name__ == "__main__":
    main()
