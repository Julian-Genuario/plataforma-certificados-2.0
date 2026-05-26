# Contenido para pegar en el archivo WSGI de PythonAnywhere
# (pestaña Web -> "WSGI configuration file"). Reemplazá "genuariojulian"
# por tu usuario de PA si es distinto.
import os
import sys

PA_USER = "genuariojulian"
path = f"/home/{PA_USER}/plataforma-certificados-2.0"
if path not in sys.path:
    sys.path.insert(0, path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

# Clave secreta persistente, fuera del repo público (se genera en el 1er arranque).
import secrets
_secret_file = f"/home/{PA_USER}/.django_secret_key"
try:
    with open(_secret_file) as _fh:
        _secret = _fh.read().strip()
except FileNotFoundError:
    _secret = secrets.token_urlsafe(64)
    with open(_secret_file, "w") as _fh:
        _fh.write(_secret)
os.environ["DJANGO_SECRET_KEY"] = _secret

os.environ["DJANGO_DEBUG"] = "False"
os.environ["DJANGO_ALLOWED_HOSTS"] = f"{PA_USER}.pythonanywhere.com"
os.environ["CSRF_TRUSTED_ORIGINS"] = f"https://{PA_USER}.pythonanywhere.com"

import django
django.setup()
try:
    from django.core.management import call_command
    call_command("migrate", "--noinput", verbosity=0)
except Exception:
    import logging
    import traceback
    logging.error("Auto-migrate failed:\n%s", traceback.format_exc())

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
