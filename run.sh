#!/bin/sh
# Этот скрипт гарантирует последовательный запуск

echo "--- run.sh: Starting model download ---"
python download_model.py

echo "--- run.sh: Applying database migrations (alembic upgrade head) ---"
python -m alembic upgrade head || {
  echo "alembic upgrade failed; continuing without migration (WARNING)"
}

echo "--- run.sh: Starting main bot application ---"
python main.py
