#!/bin/bash
# Watchdog de la plataforma: si /healthz no responde 200 dos veces seguidas,
# reinicia gunicorn. Corre cada minuto via certificados-watchdog.timer.
# El healthcheck toca la base, así que también detecta una DB colgada.
set -u

URL="http://127.0.0.1/healthz"
HOST="srv1812254.hstgr.cloud"

check() {
    curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Host: $HOST" "$URL" 2>/dev/null
}

code=$(check)
if [ "$code" = "200" ]; then
    exit 0
fi

sleep 5
code=$(check)
if [ "$code" = "200" ]; then
    exit 0
fi

echo "healthz devolvio '$code' dos veces; reiniciando certificados" | systemd-cat -t certificados-watchdog -p warning
systemctl restart certificados
