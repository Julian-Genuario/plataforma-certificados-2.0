import os
import sys

path = "/home/genuariojulian/plataforma-certificados"
if path not in sys.path:
    sys.path.insert(0, path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DJANGO_SECRET_KEY"] = "pa-prod-s3cr3t-k3y-ch4ng3-m3-l4t3r-xyz789"
os.environ["DJANGO_DEBUG"] = "False"
os.environ["DJANGO_ALLOWED_HOSTS"] = "genuariojulian.pythonanywhere.com"
os.environ["CSRF_TRUSTED_ORIGINS"] = "https://genuariojulian.pythonanywhere.com"

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
