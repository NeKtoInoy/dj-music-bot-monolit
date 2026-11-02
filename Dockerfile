FROM python:3.12-slim

# ffmpeg — для звука, deno — чтобы yt-dlp мог решать JS-задачки YouTube
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# -u: логи сразу в docker logs
CMD ["python", "-u", "bot.py"]
