FROM python:3.11-slim

# Установка ffmpeg и зависимостей
RUN apt-get update && apt-get install -y ffmpeg git

# Копируем код
WORKDIR /app
COPY . .

# Устанавливаем зависимости
RUN pip install -r requirements.txt

# Запуск
CMD ["python", "bot.py"]