#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
# Это КЛЮЧЕВАЯ строка. Если python main.py упадет, весь скрипт остановится с ошибкой,
# и Railway покажет, что деплой не удался.
set -e

# --- 1. Загрузка модели Vosk ---
echo "--- Starting model download script ---"
python download_model.py
echo "--- Model download script finished ---"

# --- 2. Запуск основного приложения ---
echo "--- Starting main bot application (main.py) ---"
python main.py
echo "--- Main bot application stopped ---"
