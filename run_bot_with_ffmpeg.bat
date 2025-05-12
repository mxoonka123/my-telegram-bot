@echo off
set PATH=%PATH%;%CD%\ffmpeg-bin\ffmpeg-6.1.1-essentials_build\bin
echo FFmpeg добавлен в PATH.
echo Запуск бота...
python main.py
