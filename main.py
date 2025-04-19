import logging
import asyncio
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
    # Removed global BOT_USERNAME as it's not strictly needed often
    # Can get username via context.bot.username if required
    me = await application.bot.get_me()
    logger.info(f"Bot started as @{me.username}")

    # Start background tasks
    asyncio.create_task(tasks.spam_task(application))
    asyncio.create_task(tasks.reset_daily_limits_task())
    asyncio.create_task(tasks.check_subscription_expiry_task())

def main() -> None:
    logger.info("Creating database tables if they don't exist...")
    db.create_tables()
    logger.info("Database setup complete.")

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()

    # --- Edit Persona Conversation Handler ---
    edit_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('editpersona', handlers.edit_persona_start)],
        states={
            handlers.EDIT_PERSONA_CHOICE: [CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_field_|^edit_moods$|^cancel_edit$')],
            handlers.EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_field_update)],
            handlers.EDIT_MOOD_CHOICE: [CallbackQueryHandler(handlers.edit_mood_choice, pattern='^editmood_|^deletemood_confirm_|^edit_persona_back$')],
            handlers.EDIT_MOOD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_name_received)],
            handlers.EDIT_MOOD_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.edit_mood_prompt_received)],
            handlers.DELETE_MOOD_CONFIRM: [CallbackQueryHandler(handlers.delete_mood_confirmed, pattern='^deletemood_delete_'),
                                           CallbackQueryHandler(handlers.edit_mood_choice, pattern='^edit_moods_back$')] # Back to mood list on cancel
        },
        fallbacks=[
            CommandHandler('cancel', handlers.edit_persona_cancel),
            CallbackQueryHandler(handlers.edit_persona_cancel, pattern='^cancel_edit$'),
            CallbackQueryHandler(handlers.edit_persona_choice, pattern='^edit_persona_back$') # Allow back from mood menu
            ],
        per_message=False # Use one conversation per user+chat
    )

    # --- Delete Persona Conversation Handler ---
    delete_persona_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('deletepersona', handlers.delete_persona_start)],
        states={
            handlers.DELETE_PERSONA_CONFIRM: [CallbackQueryHandler(handlers.delete_persona_confirmed, pattern='^delete_persona_confirm_'),
                                            CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')]
        },
        fallbacks=[CommandHandler('cancel', handlers.delete_persona_cancel), CallbackQueryHandler(handlers.delete_persona_cancel, pattern='^delete_persona_cancel$')]
    )


    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("profile", handlers.profile))
    application.add_handler(CommandHandler("subscribe", handlers.subscribe))

    application.add_handler(CommandHandler("createpersona", handlers.create_persona, block=False))
    application.add_handler(CommandHandler("mypersonas", handlers.my_personas, block=False))
    application.add_handler(edit_persona_conv_handler) # Add conversation handler for editing
    application.add_handler(delete_persona_conv_handler) # Add conversation handler for deleting
    application.add_handler(CommandHandler("addbot", handlers.add_bot_to_chat, block=False))

    application.add_handler(CommandHandler("mood", handlers.mood, block=False))
    application.add_handler(CommandHandler("reset", handlers.reset, block=False))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handlers.handle_photo, block=False))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handlers.handle_voice, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message, block=False))

    # General callback handler (must be after specific ones like ConversationHandler)
    # Handles mood setting, subscription buttons etc.
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query, pattern='^set_mood_|^subscribe_'))

    application.add_error_handler(handlers.error_handler)

    application.post_init = post_init

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
