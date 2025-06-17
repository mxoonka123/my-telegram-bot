#!/bin/sh
# Этот скрипт гарантирует последовательный запуск

echo "--- run.sh: Starting model download ---"
python download_model.py

echo "--- run.sh: Starting main bot application ---"
python main.py
