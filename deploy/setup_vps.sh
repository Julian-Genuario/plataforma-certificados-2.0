#!/usr/bin/env bash
# Provisiona la Plataforma de Certificados en un VPS Ubuntu 24.04 (correr como root).
# Asume que el código ya está en /opt/certificados (git clone o rsync).
set -euo pipefail

APP_DIR=/opt/certificados
DOMAIN=srv1812254.hstgr.cloud
APP_USER=certif
ENV_FILE=/etc/certificados.env

echo "==> Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip nginx git ufw certbot python3-certbot-nginx

echo "==> Usuario de la app"
id -u "$APP_USER" >/dev/null 2>&1 || adduser --system --group --home "$APP_DIR" --no-create-home "$APP_USER"

echo "==> Virtualenv + dependencias"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt gunicorn

echo "==> Archivo de entorno (se crea una sola vez)"
if [ ! -f "$ENV_FILE" ]; then
    SECRET=$(.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))")
    cat > "$ENV_FILE" <<EOF
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=$DOMAIN
CSRF_TRUSTED_ORIGINS=https://$DOMAIN
EOF
    chmod 640 "$ENV_FILE"
    chown root:"$APP_USER" "$ENV_FILE"
fi

echo "==> Migraciones + estáticos"
set -a; . "$ENV_FILE"; set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput

echo "==> Permisos"
chown -R "$APP_USER":www-data "$APP_DIR"

echo "==> Kernel: colas de conexión para picos (ver el .conf)"
install -m 644 deploy/99-certificados-sysctl.conf /etc/sysctl.d/99-certificados-sysctl.conf
sysctl --system >/dev/null

echo "==> systemd (gunicorn)"
install -m 644 deploy/certificados.service /etc/systemd/system/certificados.service
install -m 644 deploy/certificados-mailer.service /etc/systemd/system/certificados-mailer.service
install -m 644 deploy/certificados-mailer.timer /etc/systemd/system/certificados-mailer.timer
systemctl daemon-reload
systemctl enable --now certificados.service
systemctl enable --now certificados-mailer.timer

echo "==> Página de fallback de Nginx"
mkdir -p /var/www/certificados-fallback
install -m 644 deploy/maintenance.html /var/www/certificados-fallback/maintenance.html

echo "==> Nginx"
install -m 644 deploy/certificados.nginx.conf /etc/nginx/sites-available/certificados
ln -sf /etc/nginx/sites-available/certificados /etc/nginx/sites-enabled/certificados
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> Firewall"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

echo "==> HTTPS (Let's Encrypt) para $DOMAIN"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m genuario.julian@gmail.com --redirect || \
    echo "!! certbot falló (revisar que $DOMAIN resuelva a la IP del VPS). Reintentar: certbot --nginx -d $DOMAIN"

echo "==> Nginx: cola de conexiones del 443 y límites para picos"
# certbot escribe "listen 443 ssl;": se le agrega backlog (el default de Linux
# es 511, chico para miles de personas a la vez). Solo un listen por puerto
# puede llevar backlog, y este es el único sitio en el 443.
sed -i -E 's/^(\s*listen 443 ssl);/\1 backlog=65535;/; s/^(\s*listen \[::\]:443 ssl);/\1 backlog=65535;/' \
    /etc/nginx/sites-available/certificados
sed -i -E 's/^(\s*)worker_connections [0-9]+;/\1worker_connections 16384;/; s/^worker_rlimit_nofile [0-9]+;/worker_rlimit_nofile 65535;/' \
    /etc/nginx/nginx.conf
grep -q '^worker_rlimit_nofile' /etc/nginx/nginx.conf || sed -i '1i worker_rlimit_nofile 65535;' /etc/nginx/nginx.conf
nginx -t && systemctl reload nginx

echo "==> Listo. App en https://$DOMAIN"
