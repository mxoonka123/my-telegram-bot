FROM python:3.11-slim

# Установка зависимостей системы
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    wget \
    unzip \
    gcc \
    g++ \
    python3-dev \
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
    unzip -q vosk-model-small-ru-0.22.zip -d model_vosk_ru && \
    rm vosk-model-small-ru-0.22.zip

# Запуск приложения
CMD ["python", "main.py"]
