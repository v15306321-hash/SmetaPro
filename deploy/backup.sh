#!/bin/sh
# Ежедневная резервная копия БД и файлов (PDF, чеки, логотипы). Пример cron:
#   15 3 * * * /opt/furniture/deploy/backup.sh >> /var/log/furniture-backup.log 2>&1
set -eu
cd "$(dirname "$0")/.."
DEST=${BACKUP_DIR:-/var/backups/furniture}
STAMP=$(date +%Y%m%d-%H%M)
KEEP_DAYS=${KEEP_DAYS:-14}
mkdir -p "$DEST"
docker compose exec -T db pg_dump -U furniture -d furniture -Fc > "$DEST/db-$STAMP.dump"
docker compose run --rm -T --no-deps -v "$DEST":/backup web tar czf "/backup/storage-$STAMP.tgz" -C /app storage
find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $STAMP"
