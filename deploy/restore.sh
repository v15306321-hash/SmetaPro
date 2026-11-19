#!/bin/sh
# Восстановление: ./deploy/restore.sh /var/backups/furniture/db-YYYYMMDD-HHMM.dump [storage-....tgz]
set -eu
cd "$(dirname "$0")/.."
docker compose stop web bot
docker compose exec -T db pg_restore -U furniture -d furniture --clean --if-exists < "$1"
if [ "${2:-}" ]; then
  docker compose run --rm -T --no-deps -v "$(dirname "$2")":/backup web sh -c "rm -rf /app/storage/* && tar xzf /backup/$(basename "$2") -C /app"
fi
docker compose start web bot
echo "restore ok"
