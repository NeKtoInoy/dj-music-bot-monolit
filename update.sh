#!/usr/bin/env bash
# Обновить бота на сервере: ./update.sh
# Трогает только контейнер и образ dj-bot, остальные контейнеры (VPN и т.п.) не задевает.
set -e
cd "$(dirname "$0")"

git pull
docker build -t dj-bot .
docker rm -f dj-bot 2>/dev/null || true
docker run -d --name dj-bot --restart unless-stopped \
  --memory 512m --cap-add SYS_NICE \
  -v dj-bot-cache:/root/.cache \
  --env-file .env dj-bot

sleep 5
docker logs --tail 10 dj-bot
