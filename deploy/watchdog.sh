#!/bin/bash
# Watchdog de la plataforma: reinicia gunicorn SOLO si la app está caída o
# colgada, nunca si está ocupada. Corre cada minuto via certificados-watchdog.timer.
# El healthcheck toca la base, así que también detecta una DB colgada.
#
# Prueba de carga 06-10-2026 (5.000 personas a la vez): /healthz quedó en la
# fila detrás de todos, tardó >10 s, el watchdog viejo lo tomó como cuelgue y
# reinició en plena carga -> ~4.800 errores 502. Un "lento" se convertía en
# caída. Ahora, si healthz no responde pero la app está quemando CPU, está
# trabajando: se deja tranquila (un worker trabado de verdad lo mata gunicorn
# solo con --timeout).
set -u

# HTTPS resolviendo al propio nginx local: por HTTP puro nginx contesta 301
# (redirect a https) y el watchdog reiniciaba en falso una vez por minuto.
HOST="srv1812254.hstgr.cloud"
SOCK="/run/certificados/gunicorn.sock"
# Uso promedio de CPU de la app (en milésimas de núcleo) a partir del cual se
# considera "ocupada". 1000 = un núcleo entero; colgada está cerca de 0.
BUSY_MILLICORES=1000

log() { echo "$1" | systemd-cat -t certificados-watchdog -p warning; }

# 1. Caída dura: el servicio no corre o no hay socket -> reinicio inmediato.
if ! systemctl is-active --quiet certificados || [ ! -S "$SOCK" ]; then
    log "certificados no activo o sin socket; reiniciando"
    systemctl restart certificados
    exit 0
fi

check() {
    curl -s -o /dev/null -w '%{http_code}' -m 15 \
        --resolve "$HOST:443:127.0.0.1" "https://$HOST/healthz" 2>/dev/null
}

cpu_ns() {
    local v
    v=$(systemctl show certificados -p CPUUsageNSec --value 2>/dev/null)
    [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}

c0=$(cpu_ns); t0=$(date +%s%N)

code=$(check)
[ "$code" = "200" ] && exit 0
sleep 10
code=$(check)
[ "$code" = "200" ] && exit 0

# 2. healthz no respondió dos veces: ¿ocupada o colgada?
c1=$(cpu_ns); t1=$(date +%s%N)
busy=$(( (c1 - c0) * 1000 / (t1 - t0) ))
if [ "$busy" -ge "$BUSY_MILLICORES" ]; then
    log "healthz devolvio '$code' pero la app esta OCUPADA (${busy} milinucleos): no se reinicia"
    exit 0
fi

log "healthz devolvio '$code' dos veces y la app esta sin actividad (${busy} milinucleos); reiniciando certificados"
systemctl restart certificados
