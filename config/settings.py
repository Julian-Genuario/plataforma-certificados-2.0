import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "certificados",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "certificados.middleware.MaintenanceModeMiddleware",
]

try:
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
except ImportError:
    pass

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "certificados.context_processors.site_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # Aguante bajo concurrencia (miles de descargas simultáneas escriben
        # DownloadLog/RejectedAttempt). Sin esto, SQLite en modo default tira
        # "database is locked" bajo carga → pantalla de error para la gente.
        # - WAL: lectores y escritor dejan de bloquearse entre sí
        # - timeout/busy_timeout: esperar el lock en vez de explotar
        # - IMMEDIATE: la transacción de escritura toma el lock al entrar
        #   (evita el deadlock por upgrade de lock a mitad de transacción)
        "OPTIONS": {
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA busy_timeout=15000;"
            ),
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es"
TIME_ZONE = "America/Argentina/Buenos_Aires"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
try:
    import whitenoise  # noqa: F401
    STORAGES = {
        # Debe incluir "default" o Django no encuentra el storage de uploads
        # (subida de PDF de plantilla) cuando se define STORAGES explícitamente.
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
except ImportError:
    pass

# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# CSRF
# Fallo de CSRF (cookies bloqueadas en iframe, sesión vieja) → página amigable
# con link para reabrir el formulario, no el 403 técnico de Django.
CSRF_FAILURE_VIEW = "certificados.views.csrf_failure"
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Hardening de producción (PythonAnywhere / cualquier host con HTTPS).
if not DEBUG:
    # PA termina TLS en su proxy y reenvía este header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Permite que el formulario público funcione embebido por iframe en otro
    # dominio (cookies de terceros). Requiere HTTPS (Secure ya está arriba).
    SESSION_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SAMESITE = "None"
    SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Sin esto, con DEBUG=False Django descarta los tracebacks de los errores 500
# en silencio (el handler de consola por defecto filtra con require_debug_true):
# el 500 del import del 21-08 no dejó rastro en journald. Con este config los
# errores de request salen por stderr y gunicorn los manda al journal.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        # Errores atrapados por la red de contención de vistas públicas.
        "certificados": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# --- Correo con el certificado adjunto. Proveedor agnóstico: SMTP por env. ---
# Gmail de prueba hoy, Postmark/Resend para el congreso: mismo código, otras
# variables en /etc/certificados.env. Sin EMAIL_HOST el worker deja todo
# pendiente y reintenta al minuto (no quema intentos).
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1").lower() in ("1", "true", "yes")
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "certificados@localhost")
MAIL_RATE_PER_MINUTE = int(os.environ.get("MAIL_RATE_PER_MINUTE", "30"))
MAIL_DAILY_CAP = int(os.environ.get("MAIL_DAILY_CAP", "450"))  # 0 = sin tope
MAIL_STUCK_MINUTES = 10
