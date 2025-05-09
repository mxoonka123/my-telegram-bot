"""
Исправление структуры меню настроек персоны - обеспечивает правильное отображение
опций количества сообщений в подменю, а не в основном меню.
"""
from __future__ import annotations
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler
from db import PersonaConfig, get_db
import handlers as _h

_logger = logging.getLogger(__name__)

# Оригинальные функции меню
_original_edit_max_messages_prompt = _h.edit_max_messages_prompt
_original_show_edit_wizard_menu = _h._show_edit_wizard_menu

# Исправленная функция отображения основного меню настройки персоны
async def fixed_show_edit_wizard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, persona_config: PersonaConfig) -> int:
    """Отображает главное меню настройки без кнопок выбора количества сообщений."""
    # Получаем исходные данные
    query = update.callback_query
    message = update.effective_message if not query else query.message
    chat_id = message.chat.id
    persona_id = persona_config.id
    user_id = update.effective_user.id

    # Определяем подпись для кнопки количества сообщений
    val = persona_config.max_response_messages
    if val == 1:
        label = "Поменьше"
    elif val == 3:
        label = "Стандарт"
    elif val == 6:
        label = "Побольше"
    else:
        label = "Случайно"

    # Строим клавиатуру с корректной структурой
    keyboard = [
        [
            InlineKeyboardButton("✏️ Имя", callback_data="edit_wizard_name"),
            InlineKeyboardButton("📜 Описание", callback_data="edit_wizard_description")
        ],
        [InlineKeyboardButton(f"💬 Стиль ({persona_config.communication_style or 'Нейтральный'})", callback_data="edit_wizard_comm_style")],
        [InlineKeyboardButton(f"🗣️ Разговорчивость ({persona_config.verbosity_level or 'Средний'})", callback_data="edit_wizard_verbosity")],
        [InlineKeyboardButton(f"👥 Ответы в группе ({persona_config.group_reply_preference or 'По @ / Контексту'})", callback_data="edit_wizard_group_reply")],
        [InlineKeyboardButton(f"🖼️ Реакция на медиа ({persona_config.media_reaction or 'Только текст'})", callback_data="edit_wizard_media_reaction")],
        [InlineKeyboardButton(f"🗨️ Макс. сообщ. ({label})", callback_data="edit_wizard_max_msgs")],
        [InlineKeyboardButton(f"🔊 Объем сообщений", callback_data="edit_wizard_message_volume")],
        [InlineKeyboardButton(f"🎭 Настроения", callback_data="edit_wizard_moods")],
        [InlineKeyboardButton("✅ Завершить", callback_data="finish_edit")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Формируем текст сообщения
    msg_text = f"⚙️ *Настройка личности: {_h.escape_markdown_v2(persona_config.name)}* \\(ID: `{persona_id}`\\)\n\nВыберите, что изменить:"

    try:
        # Обработка с более надежными проверками на ошибки
        try:
            if query and query.message:
                # Попытка отредактировать существующее сообщение
                try:
                    await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode=_h.ParseMode.MARKDOWN_V2)
                    context.user_data['edit_message_id'] = query.message.message_id
                    context.user_data['edit_chat_id'] = chat_id
                    return _h.EDIT_WIZARD_MENU
                except Exception as edit_err:
                    _logger.warning(f"Не удалось отредактировать сообщение: {edit_err}. Отправляем новое сообщение.")
                    # Если редактирование не удалось, отправляем новое сообщение
            
            # Удаляем предыдущее сообщение с подсказкой, если оно есть
            if context.user_data.get('last_prompt_message_id'):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data['last_prompt_message_id'])
                except Exception as del_err:
                    _logger.warning(f"Could not delete previous prompt message: {del_err}")
                context.user_data.pop('last_prompt_message_id', None)
            
            # Создаем новое сообщение с меню
            sent_message = await context.bot.send_message(chat_id, msg_text, reply_markup=reply_markup, parse_mode=_h.ParseMode.MARKDOWN_V2)
            
            # Сохраняем ID нового сообщения
            context.user_data['edit_message_id'] = sent_message.message_id
            context.user_data['edit_chat_id'] = chat_id
            context.user_data['wizard_menu_message_id'] = sent_message.message_id
        except Exception as menu_e:
            _logger.error(f"Ошибка при создании меню настройки для персоны {persona_id}: {menu_e}")
            try:
                await context.bot.send_message(chat_id, _h.escape_markdown_v2("❌ Ошибка отображения меню. Попробуйте снова."), parse_mode=_h.ParseMode.MARKDOWN_V2)
            except Exception:
                await context.bot.send_message(chat_id, "❌ Ошибка отображения меню. Попробуйте снова.")
            return ConversationHandler.END
    except Exception as e:
        _logger.error(f"Error showing wizard menu for persona {persona_id}: {e}")
        await context.bot.send_message(chat_id, _h.escape_markdown_v2("❌ Ошибка отображения меню."), parse_mode=_h.ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    return _h.EDIT_WIZARD_MENU

# Применяем исправление
_h._show_edit_wizard_menu = fixed_show_edit_wizard_menu

_logger.info("menu_fix: Исправлена структура меню настроек персоны.")
