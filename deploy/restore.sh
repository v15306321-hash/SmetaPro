#!/bin/sh
# Восстановление: ./deploy/restore.sh /var/backups/furniture/db-YYYYMMDD-HHMM.dump
set -eu
cd "$(dirname "$0")/.."
docker compose stop web bot
docker compose exec -T db pg_restore -U furniture -d furniture --clean --if-exists < "$1"
docker compose start web bot
echo "restore ok"
