import os
import requests
import zipfile
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ИЗМЕНЕНИЕ: Используем временную папку /tmp ---
# Путь для скачивания и распаковки модели.
# Этот путь будет уникален для каждого запуска контейнера.
MODEL_DIR = "/tmp/model_vosk_ru"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip" # URL на маленькую русскую модель
# Имя zip-файла теперь тоже будет во временной папке
ZIP_FILE_NAME = "/tmp/vosk_model.zip"

def download_and_unzip_model():
    """
    Downloads and extracts the model to a temporary directory on every start.
    """
    # --- ИЗМЕНЕНИЕ: Убираем проверку, так как /tmp всегда пустая при старте ---
    # if os.path.exists(MODEL_DIR) and os.path.exists(os.path.join(MODEL_DIR, 'am/final.mdl')):
    #     logging.info(f"Vosk model already exists in '{MODEL_DIR}'. Skipping download.")
    #     return

    logging.info(f"Model will be downloaded to temporary directory: '{MODEL_DIR}'.")

    logging.info(f"Model directory '{MODEL_DIR}' not found or incomplete. Starting download...")
    
    # Создаем папку, если ее нет
    os.makedirs(MODEL_DIR, exist_ok=True)

    try:
        # Скачиваем архив
        logging.info(f"Downloading model from {MODEL_URL}...")
        response = requests.get(MODEL_URL, stream=True)
        response.raise_for_status() # Проверка на ошибки HTTP
        
        with open(ZIP_FILE_NAME, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logging.info("Download complete.")

        # Распаковываем архив
        logging.info(f"Unzipping '{ZIP_FILE_NAME}'...")
        with zipfile.ZipFile(ZIP_FILE_NAME, 'r') as zip_ref:
            # Находим имя папки внутри архива (обычно оно совпадает с именем архива)
            # Например, vosk-model-small-ru-0.22
            top_level_dir = zip_ref.namelist()[0].split('/')[0]
            zip_ref.extractall() # Распаковываем все
        
        # Перемещаем содержимое из распакованной папки в нашу целевую папку
        logging.info(f"Moving files from '{top_level_dir}' to '{MODEL_DIR}'...")
        for item in os.listdir(top_level_dir):
            os.rename(os.path.join(top_level_dir, item), os.path.join(MODEL_DIR, item))
        
        # Удаляем пустую папку и архив
        os.rmdir(top_level_dir)
        logging.info("Model setup complete.")

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to download model: {e}")
    except zipfile.BadZipFile:
        logging.error("Failed to unzip model. The downloaded file might be corrupted.")
    except Exception as e:
        logging.error(f"An unexpected error occurred during model setup: {e}", exc_info=True)
    finally:
        # Удаляем zip-архив в любом случае
        if os.path.exists(ZIP_FILE_NAME):
            os.remove(ZIP_FILE_NAME)
            logging.info(f"Removed temporary file '{ZIP_FILE_NAME}'.")

if __name__ == "__main__":
    download_and_unzip_model()
