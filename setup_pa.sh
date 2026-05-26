#!/bin/bash
# Setup para PythonAnywhere (free). Ejecutar en una consola Bash de PA.
# Si tu usuario de PA NO es "genuariojulian", reemplazalo en TODO el archivo
# (y en wsgi_pa.py).
set -e

PA_USER="genuariojulian"
PROJECT_DIR="/home/${PA_USER}/plataforma-certificados-2.0"

cd "${PROJECT_DIR}"

# Virtualenv dedicado (Python 3.12 en PA)
if [ ! -d "${PROJECT_DIR}/.venv" ]; then
    python3.12 -m venv .venv
fi
source .venv/bin/activate

pip install -r requirements.txt

# Clave secreta persistente fuera del repo (misma que usa wsgi_pa.py)
SECRET_FILE="/home/${PA_USER}/.django_secret_key"
if [ ! -f "${SECRET_FILE}" ]; then
    python -c "import secrets; open('${SECRET_FILE}','w').write(secrets.token_urlsafe(64))"
fi
export DJANGO_SECRET_KEY="$(cat ${SECRET_FILE})"
export DJANGO_DEBUG="False"
export DJANGO_ALLOWED_HOSTS="${PA_USER}.pythonanywhere.com"
export CSRF_TRUSTED_ORIGINS="https://${PA_USER}.pythonanywhere.com"

python manage.py migrate --run-syncdb
python manage.py collectstatic --no-input

echo ""
echo "SETUP_COMPLETE. Ahora cree el admin con:"
echo "  python manage.py createsuperuser"
