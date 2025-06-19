import logging
import threading
from waitress import serve
import os

# --- Настройка логирования в самом начале ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Уменьшаем "шум" от библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.INFO)

# --- Импорты ваших модулей ---
# Импортируем flask_app из main.py, который теперь будет просто модулем с веб-логикой
from main import flask_app, run_flask
# Импортируем логику запуска бота из run_bot.py
from run_bot import main_bot_runner

def run_bot_thread():
    """Функция-обертка для запуска бота в отдельном потоке."""
    logging.info("Starting Telegram bot thread...")
    try:
        main_bot_runner()
    except Exception as e:
        logging.critical(f"Telegram bot thread failed critically: {e}", exc_info=True)

def run_web_thread():
    """Функция-обертка для запуска веб-сервера в отдельном потоке."""
    logging.info("Starting Flask web server thread...")
    try:
        run_flask() # Используем вашу функцию run_flask из main.py
    except Exception as e:
        logging.critical(f"Flask web server thread failed critically: {e}", exc_info=True)


if __name__ == '__main__':
    logging.info("--- Main Application Starting ---")
    
    # Создаем потоки для бота и веб-сервера
    bot_thread = threading.Thread(target=run_bot_thread, name="BotThread")
    web_thread = threading.Thread(target=run_web_thread, name="WebThread")

    # Запускаем потоки
    bot_thread.start()
    web_thread.start()
    
    logging.info("Both bot and web threads have been started.")

    # Ожидаем завершения потоков (они будут работать, пока основной процесс жив)
    bot_thread.join()
    web_thread.join()
    
    logging.info("--- Main Application Stopped ---")
