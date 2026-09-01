#!/bin/bash
# Chequeo integral de la plataforma de certificados. Imprime una linea por
# control y termina con PASS o FAIL. Correr como root en el VPS:
#   /opt/certificados/deploy/verificar.sh
# Se corre al inicio y al final de cada sesion de trabajo y despues de cada deploy.
HOST=srv1812254.hstgr.cloud
DB=/opt/certificados/db.sqlite3
BK=/var/backups/certificados
fail=0
ok()   { printf "  OK    %s\n" "$1"; }
bad()  { printf "  FAIL  %s\n" "$1"; fail=1; }

# 1. servicios
for u in certificados nginx certificados-watchdog.timer certificados-backup.timer; do
  [ "$(systemctl is-active "$u")" = active ] && ok "$u activo" || bad "$u NO activo"
done

# 2. health y pagina publica por HTTPS (mismo camino que un usuario)
code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" --resolve "$HOST:443:127.0.0.1" "https://$HOST/healthz")
[ "$code" = 200 ] && ok "/healthz 200" || bad "/healthz devolvio $code"
code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" --resolve "$HOST:443:127.0.0.1" "https://$HOST/e/congreso-test/")
[ "$code" = 200 ] && ok "pagina del evento 200" || bad "pagina del evento devolvio $code"
# 2b. por IPv6 e IPv4 PUBLICAS (el DNS publica AAAA: si nginx no escucha en
#     IPv6, los iPhones en redes v6 ven el iframe en blanco y no dejan log —
#     paso el 01-09-2026). Se resuelve contra 1.1.1.1 porque /etc/hosts del
#     VPS mapea el nombre a 127.0.1.1.
for v in AAAA A; do
  ip=$(dig +short "$v" "$HOST" @1.1.1.1 2>/dev/null | tail -1)
  if [ -z "$ip" ]; then bad "DNS publico sin registro $v para $HOST"; continue; fi
  [ "$v" = AAAA ] && r="[$ip]" || r="$ip"
  code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" --resolve "$HOST:443:$r" "https://$HOST/healthz")
  [ "$code" = 200 ] && ok "/healthz via $v $ip -> 200" || bad "/healthz via $v $ip -> $code"
done

# 3. watchdog: su ultima corrida tiene que haber terminado bien
res=$(systemctl show certificados-watchdog.service -p Result --value)
[ "$res" = success ] && ok "watchdog ultima corrida: $res" || bad "watchdog ultima corrida: $res (revisar journalctl -u certificados-watchdog)"

# 4. backup: el mas nuevo tiene menos de 26 h
newest=$(ls -1t "$BK"/db-*.sqlite3.gz 2>/dev/null | head -1)
if [ -n "$newest" ]; then
  age=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
  [ "$age" -lt 26 ] && ok "backup mas nuevo: $(basename "$newest") (${age}h)" || bad "backup viejo: $(basename "$newest") tiene ${age}h"
else
  bad "no hay ningun backup en $BK"
fi
res=$(systemctl show certificados-backup.service -p Result --value)
[ "$res" = success ] && ok "backup ultima corrida: $res" || bad "backup ultima corrida: $res"

# 5. scripts ejecutables y sin CR (la causa del 203/EXEC del 28-08)
for f in /opt/certificados/deploy/*.sh; do
  [ -x "$f" ] || bad "$(basename "$f") sin permiso de ejecucion"
  grep -q $'\r' "$f" && bad "$(basename "$f") tiene CRLF"
done
ok "scripts de deploy ejecutables y en LF"

# 6. SSL: dias restantes
exp=$(echo | openssl s_client -servername "$HOST" -connect 127.0.0.1:443 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$exp" ]; then
  days=$(( ( $(date -d "$exp" +%s) - $(date +%s) ) / 86400 ))
  [ "$days" -gt 14 ] && ok "SSL vence en $days dias" || bad "SSL vence en $days dias"
else
  bad "no pude leer el certificado SSL"
fi

# 7. disco y memoria
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$use" -lt 85 ] && ok "disco ${use}% usado" || bad "disco ${use}% usado"
avail=$(free -m | awk '/Mem:/{print $7}')
[ "$avail" -gt 1024 ] && ok "memoria disponible ${avail} MB" || bad "memoria disponible ${avail} MB"

# 8. integridad de la DB (quick_check no bloquea escrituras en WAL)
chk=$(sqlite3 "$DB" "PRAGMA quick_check;" 2>&1 | head -1)
[ "$chk" = ok ] && ok "DB quick_check ok" || bad "DB quick_check: $chk"
jm=$(sqlite3 "$DB" "PRAGMA journal_mode;" 2>&1)
[ "$jm" = wal ] && ok "DB en modo WAL" || bad "DB journal_mode=$jm (esperaba wal)"

# 9. errores de la app en la ultima hora (tracebacks reales, no SIGTERM de reinicios)
errs=$(journalctl -u certificados --since "1 hour ago" --no-pager 2>/dev/null | grep -c -E "Traceback|Internal Server Error")
[ "$errs" -eq 0 ] && ok "sin tracebacks en la ultima hora" || bad "$errs tracebacks en la ultima hora (journalctl -u certificados)"

echo
if [ $fail -eq 0 ]; then echo "RESULTADO: PASS"; else echo "RESULTADO: FAIL"; exit 1; fi
