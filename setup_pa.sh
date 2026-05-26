#!/bin/bash
cd /home/genuariojulian/plataforma-certificados
pip install --user -r requirements.txt
export DJANGO_SECRET_KEY="pa-prod-s3cr3t-k3y-ch4ng3-m3-l4t3r-xyz789"
export DJANGO_DEBUG="False"
export DJANGO_ALLOWED_HOSTS="genuariojulian.pythonanywhere.com"
python manage.py migrate --run-syncdb
python manage.py collectstatic --no-input
echo "SETUP_COMPLETE"
