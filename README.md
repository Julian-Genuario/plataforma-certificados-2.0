# 📜 Plataforma de Certificados

Aplicación web en **Django 5** para emitir, administrar y validar certificados a partir de listados de asistentes. Pensada para cursos, capacitaciones y eventos: se carga la lista de gente, se elige la plantilla y la plataforma genera los certificados en PDF listos para descargar.

## ✨ Funcionalidades

- **Generación de certificados en PDF** sobre una plantilla, con datos por asistente (`reportlab` + `pypdf` + `pillow`).
- **Importación de asistentes desde Excel** (`openpyxl`) — carga masiva sin cargar a mano.
- **Panel de administración** para gestionar eventos, plantillas y asistentes.
- **Validación de certificados** emitidos.
- **Tests** incluidos (`certificados/tests.py`).

## 🛠️ Stack

- **Backend:** Python 3 + Django 5.1
- **PDF/Imagen:** reportlab, pypdf, pillow
- **Datos:** openpyxl (Excel), SQLite por defecto
- **Server:** gunicorn + whitenoise
- **Deploy:** Docker, y configuración lista para Fly.io / Render / PythonAnywhere

## ⚙️ Configuración

La app se configura por **variables de entorno** (nada de secretos en el código):

| Variable | Descripción | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | Clave secreta de Django | dev key (cambiar en prod) |
| `DJANGO_DEBUG` | Modo debug | `True` |
| `DJANGO_ALLOWED_HOSTS` | Hosts permitidos (separados por coma) | `127.0.0.1,localhost` |
| `CSRF_TRUSTED_ORIGINS` | Orígenes confiables para CSRF | — |

En producción (`DEBUG=False`) se activan cookies seguras, HSTS y `SECURE_PROXY_SSL_HEADER`.

## ▶️ Cómo correrlo localmente

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Luego ingresá a `http://127.0.0.1:8000/`.

## 🧪 Tests

```bash
python manage.py test
```

## 🗂️ Estructura

```
├── config/          # Settings, URLs y WSGI del proyecto
├── certificados/    # App principal: modelos, vistas, panel, generación PDF, import Excel, tests
├── templates/       # Plantillas HTML
├── requirements.txt
└── manage.py
```

---

Hecho por [Julian Genuario](https://github.com/Julian-Genuario).
