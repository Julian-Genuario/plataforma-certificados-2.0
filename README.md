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

## Deploy en el VPS (Hostinger)

La app corre en `/opt/certificados` (user `certif`, Gunicorn + Nginx, SQLite en WAL).
Para subir una versión nueva desde esta PC:

```bash
git archive HEAD | ssh -i ~/.ssh/hostinger_cert_vps root@179.197.65.129 \
  'tar -x -C /opt/certificados && chown -R certif:certif /opt/certificados \
   && chmod +x /opt/certificados/deploy/*.sh \
   && sudo -u certif /opt/certificados/.venv/bin/python /opt/certificados/manage.py migrate --noinput \
   && systemctl restart certificados'
```

Notas:
- `.gitattributes` fuerza LF en scripts/units y el índice guarda los `.sh` como
  755, así el archive sale listo para Linux. Igual el `chmod +x` de arriba es
  red de contención.
- `tar -x` no borra archivos que ya no existen en git: si se elimina o renombra
  algo del repo, borrarlo a mano en el VPS.
- Si se tocó un `.service`/`.timer` de `deploy/`, copiarlo a
  `/etc/systemd/system/` y hacer `systemctl daemon-reload` antes del restart.
- Verificación rápida post-deploy: `systemctl is-active certificados` y
  `journalctl -u certificados-watchdog -n 3` (tiene que decir *Finished*, no
  *203/EXEC*).
