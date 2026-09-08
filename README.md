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
  El envío de correos corre por `certificados-mailer.timer` (cada minuto,
  `manage.py send_certificate_emails`): la primera vez,
  `systemctl enable --now certificados-mailer.timer`.
- **Correo (certificado adjunto):** se configura en `/etc/certificados.env`,
  sin tocar código. Gmail de prueba (contraseña de aplicación, tope 500/día):

  ```
  EMAIL_HOST=smtp.gmail.com
  EMAIL_PORT=587
  EMAIL_USE_TLS=1
  EMAIL_HOST_USER=casilla@gmail.com
  EMAIL_HOST_PASSWORD=xxxx xxxx xxxx xxxx
  MAIL_RATE_PER_MINUTE=30
  MAIL_DAILY_CAP=450
  ```

  Postmark (producción): `EMAIL_HOST=smtp.postmarkapp.com`, usuario y
  contraseña = el Server API Token, `MAIL_DAILY_CAP=0`, `DEFAULT_FROM_EMAIL`
  con una casilla del dominio verificado (SPF + DKIM + DMARC en el DNS de
  Brisa). Después de cambiar el env: `systemctl restart certificados`
  (el timer lee el env en cada corrida). Sin `EMAIL_HOST`, los correos quedan
  pendientes y se reintentan cada minuto sin gastar intentos.
- **Verificación obligatoria** post-deploy (y al inicio/fin de cada sesión):
  `ssh ... /opt/certificados/deploy/verificar.sh` → tiene que terminar en
  `RESULTADO: PASS`. Chequea servicios, health, watchdog, edad del backup,
  scripts, SSL, disco, memoria, integridad de la DB y tracebacks recientes.
