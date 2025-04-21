import logging
import json
import os
from flask import Flask, request, abort, Response
from yookassa import Configuration, WebhookNotification

# --- Настройки логгирования (как в основном боте) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Импорт из твоего проекта ---
# Убедись, что эти импорты работают из этого файла
# Возможно, потребуется настроить PYTHONPATH или структуру проекта
# Либо скопировать нужные определения (User, activate_subscription, get_db) сюда
try:
    from db import User, activate_subscription, get_db, SessionLocal
    from config import YOOKASSA_SECRET_KEY # Нужен только секретный ключ для инициализации Yookassa тут
    from sqlalchemy.exc import SQLAlchemyError
except ImportError as e:
    logger.critical(f"Failed to import necessary modules from db/config: {e}")
    # Если импорты не работают, приложение не сможет запуститься правильно
    # Нужно либо исправить пути импорта, либо дублировать код
    # Пока оставляем так, предполагая, что импорт сработает на Railway
    raise

# --- Инициализация Flask ---
app = Flask(__name__)

# --- Инициализация Yookassa (достаточно один раз) ---
try:
    # Здесь не нужен Shop ID, только Secret Key для библиотеки
    if YOOKASSA_SECRET_KEY:
        Configuration.configure(None, YOOKASSA_SECRET_KEY)
        logger.info("Yookassa SDK configured for webhook server.")
    else:
        logger.warning("YOOKASSA_SECRET_KEY not found for webhook server.")
except Exception as e:
    logger.error(f"Failed to configure Yookassa SDK for webhook server: {e}")

# --- Маршрут для приема вебхуков ---
@app.route('/yookassa/webhook', methods=['POST'])
def yookassa_webhook():
    # IP-адреса Yookassa для проверки (можно найти в документации Yookassa)
    # Это базовый способ проверки, что запрос пришел от Yookassa
    yookassa_ips = {
        '185.71.76.0/27',
        '185.71.77.0/27',
        '77.75.153.0/25',
        '77.75.156.11',
        '77.75.156.35',
        '2a02:5180:0:1509::/64',
        '2a02:5180:0:2655::/64',
        '2a02:5180:0:1533::/64',
        # Добавь другие адреса, если они появятся в документации
    }
    # Проверка IP (опционально, но рекомендуется)
    # request_ip = request.remote_addr # Простой способ
    # if request_ip not in yookassa_ips: # Упрощенная проверка, нужна проверка подсетей
    #     logger.warning(f"Received webhook from untrusted IP: {request_ip}")
    #     abort(403) # Forbidden

    try:
        # Получаем тело запроса
        request_body = request.get_data(as_text=True)
        logger.info(f"Received Yookassa webhook: {request_body[:500]}...") # Логируем часть данных

        # Парсим уведомление с помощью библиотеки Yookassa
        notification = WebhookNotification(json.loads(request_body))
        payment = notification.object # Объект платежа

        logger.info(f"Webhook event: {notification.event}, Payment ID: {payment.id}, Status: {payment.status}")

        # --- Обработка только успешных платежей ---
        if notification.event == 'payment.succeeded' and payment.status == 'succeeded':
            logger.info(f"Processing successful payment: {payment.id}")

            # Извлекаем ID пользователя Telegram из метаданных
            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}")
                # Отвечаем OK, чтобы Юкасса не повторяла, но логируем ошибку
                return Response(status=200)

            telegram_user_id = metadata['telegram_user_id']
            logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")

            # --- Работа с базой данных ---
            db_session = None
            try:
                db_session = SessionLocal() # Создаем новую сессию БД
                # Находим пользователя по Telegram ID
                user = db_session.query(User).filter(User.telegram_id == telegram_user_id).first()

                if user:
                    # Активируем подписку (функция из db.py)
                    if activate_subscription(db_session, user.id): # Передаем сессию и ID пользователя из НАШЕЙ БД
                        logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")
                        # --- Опционально: Отправка уведомления пользователю ---
                        # Прямая отправка сообщения из этого процесса сложна, если основной бот работает на polling.
                        # Лучше, чтобы пользователь увидел статус в /profile или сделать отдельную логику уведомлений в основном боте.
                        # Например, можно установить флаг в БД user.needs_activation_notification = True,
                        # а в основном боте сделать задачу, которая проверяет этот флаг и отправляет сообщение.
                    else:
                        logger.error(f"Failed to activate subscription in DB for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                else:
                    logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except SQLAlchemyError as db_e:
                logger.error(f"Database error during webhook processing for user {telegram_user_id} payment {payment.id}: {db_e}", exc_info=True)
                if db_session:
                    db_session.rollback() # Откатываем транзакцию при ошибке БД
            except Exception as e:
                logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                if db_session and db_session.is_active: # Проверяем активность перед откатом
                    db_session.rollback()
            finally:
                if db_session:
                    db_session.close() # Всегда закрываем сессию

        # --- Ответ Yookassa ---
        # Нужно ответить 200 OK, чтобы Yookassa поняла, что уведомление получено
        return Response(status=200)

    except json.JSONDecodeError:
        logger.error("Webhook error: Invalid JSON received.")
        abort(400) # Bad Request
    except Exception as e:
        logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500) # Internal Server Error

# --- Запуск Flask-сервера (для локального теста или через gunicorn) ---
if __name__ == '__main__':
    # Для локального запуска: python webhook_server.py
    # Важно: Flask по умолчанию слушает только localhost (127.0.0.1),
    # для доступа извне (например, через ngrok) нужно слушать 0.0.0.0
    # PORT можно взять из переменных окружения, если Railway его предоставляет
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False) # debug=False для продакшена!
