FROM python:3.11-slim

# Гарантируем немедленный вывод логов и корректную работу с UTF-8
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

# Установка зависимостей системы
RUN apt-get update && apt-get install -y \
    locales \
    ffmpeg \
    libsndfile1 \
    wget \
    unzip \
    gcc \
    g++ \
    && sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода приложения
COPY . .

# Скачивание и распаковка модели Vosk
RUN mkdir -p model_vosk_ru && \
    wget -q https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip && \
    unzip -q vosk-model-small-ru-0.22.zip && \
    mv vosk-model-small-ru-0.22/* model_vosk_ru/ && \
    rm -rf vosk-model-small-ru-0.22.zip vosk-model-small-ru-0.22

# Healthcheck для платформы (опционально)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD wget -qO- http://127.0.0.1:${PORT:-8080}/healthz || exit 1

# Запуск приложения (unbuffered)
CMD ["python", "-u", "main.py"]
