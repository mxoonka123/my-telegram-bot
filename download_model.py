import os
import requests
import zipfile
import logging
import shutil
import sys # <-- Важный импорт для завершения с ошибкой

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Путь для скачивания и распаковки модели во временной папке
MODEL_DIR = "/tmp/model_vosk_ru"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
ZIP_FILE_NAME = "/tmp/vosk_model.zip"
# Имя папки, которое создается ВНУТРИ архива
EXTRACTED_FOLDER_NAME = 'vosk-model-small-ru-0.22'

def download_and_unzip_model():
    """
    Downloads and extracts the model to a temporary directory on every start.
    Exits with a non-zero status code on failure.
    """
    try:
        logging.info(f"Model will be downloaded to temporary directory: '{MODEL_DIR}'.")

        # Скачиваем архив
        logging.info(f"Downloading model from {MODEL_URL}...")
        response = requests.get(MODEL_URL, stream=True)
        response.raise_for_status()

        with open(ZIP_FILE_NAME, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logging.info("Download complete.")

        # Распаковываем архив во временную папку
        logging.info(f"Unzipping '{ZIP_FILE_NAME}' to /tmp/ ...")
        with zipfile.ZipFile(ZIP_FILE_NAME, 'r') as zip_ref:
            zip_ref.extractall('/tmp/')
        logging.info("Unzip complete.")

        # --- НОВАЯ, БОЛЕЕ НАДЕЖНАЯ ЛОГИКА ---
        # Проверяем, существует ли целевая папка (от прошлого неудачного запуска) и удаляем ее
        if os.path.exists(MODEL_DIR):
            logging.warning(f"Target directory '{MODEL_DIR}' already exists. Cleaning up before moving.")
            shutil.rmtree(MODEL_DIR)

        # Переименовываем распакованную папку в нашу целевую.
        # Это одна атомарная операция, она надежнее, чем копирование по файлам.
        source_path = f"/tmp/{EXTRACTED_FOLDER_NAME}"
        logging.info(f"Renaming '{source_path}' to '{MODEL_DIR}'...")
        os.rename(source_path, MODEL_DIR)
        
        logging.info("Model setup complete.")

    except Exception as e:
        logging.error(f"FATAL: An error occurred during model setup: {e}", exc_info=True)
        # --- САМОЕ ГЛАВНОЕ ИЗМЕНЕНИЕ ---
        # Завершаем скрипт с кодом ошибки 1.
        # Это остановит выполнение цепочки `&&` и Railway покажет ошибку в деплое.
        sys.exit(1)
    finally:
        # Удаляем zip-архив в любом случае
        if os.path.exists(ZIP_FILE_NAME):
            os.remove(ZIP_FILE_NAME)
            logging.info(f"Removed temporary file '{ZIP_FILE_NAME}'.")

if __name__ == "__main__":
    download_and_unzip_model()
