#!/bin/bash
# Backup diario de la base y los templates de la plataforma de certificados.
# La base se copia con la API de backup de SQLite (consistente aunque haya
# escrituras en curso); se guardan los ultimos 14 dias en /var/backups/certificados.
set -eu

DEST=/var/backups/certificados
STAMP=$(date +%F)
mkdir -p "$DEST"

sqlite3 /opt/certificados/db.sqlite3 ".backup $DEST/db-$STAMP.sqlite3"
gzip -f "$DEST/db-$STAMP.sqlite3"
tar -czf "$DEST/media-$STAMP.tar.gz" -C /opt/certificados media

# Conservar solo los ultimos 14 dias de cada cosa.
ls -1t "$DEST"/db-*.sqlite3.gz 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$DEST"/media-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm --

echo "backup ok: $STAMP" | systemd-cat -t certificados-backup -p info
