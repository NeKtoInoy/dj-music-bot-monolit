FROM python:3.11-slim

# ffmpeg нужен для воспроизведения звука
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# -u: логи сразу в консоль docker
CMD ["python", "-u", "bot.py"]
