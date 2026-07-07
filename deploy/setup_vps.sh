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
    SECRET=$(.venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())")
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

echo "==> systemd (gunicorn)"
install -m 644 deploy/certificados.service /etc/systemd/system/certificados.service
systemctl daemon-reload
systemctl enable --now certificados.service

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

echo "==> Listo. App en https://$DOMAIN"
