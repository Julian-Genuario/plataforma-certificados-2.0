# Envío del certificado por email — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que la persona que descarga su certificado reciba además una copia por correo, encolada y enviada en segundo plano por un worker con reintentos.

**Architecture:** Tabla `EmailDelivery` como cola en la misma SQLite (WAL). El POST público encola (nunca envía en línea). Un comando `send_certificate_emails`, lanzado por un timer systemd cada minuto, reserva filas con UPDATE condicional, genera el PDF, envía por SMTP (`django.core.mail`) y aplica reintentos con espera creciente y topes por minuto/día. Proveedor agnóstico: todo por variables `EMAIL_*` en `/etc/certificados.env`.

**Tech Stack:** Django 6, SQLite WAL, `django.core.mail.EmailMultiAlternatives`, systemd timer, tests con `locmem` backend.

**Spec:** `docs/superpowers/specs/2026-09-08-envio-certificado-por-email-design.md`

## Global Constraints

- Destinatario SIEMPRE `Attendee.email` de la lista; nunca el tipeado.
- El correo no consume la descarga única (`DownloadLog.delivered_at` no se toca).
- Un solo registro activo (`pending/sending/sent`) por (event, attendee); reenvío manual marca el anterior `superseded`.
- Reintentos: 5 intentos, esperas 1, 5, 15, 60, 180 minutos; después `failed`.
- Topes: `MAIL_RATE_PER_MINUTE` (default 30) y `MAIL_DAILY_CAP` (default 450; 0 = sin tope).
- `sending` con más de 10 min vuelve a `pending`.
- Textos públicos en lenguaje neutro (infinitivo), tildes correctas.
- Nada de credenciales en el repo. Deploy con el one-liner del README + `verificar.sh` PASS.

---

### Task 1: Modelo `EmailDelivery`, campos de correo en `SiteSettings`, settings SMTP

**Files:**
- Modify: `certificados/models.py` (después de `SuspiciousAttendee`, y en `SiteSettings`)
- Modify: `config/settings.py` (bloque EMAIL al final)
- Create: `certificados/migrations/0014_emaildelivery_sitesettings_mail.py` (por `makemigrations`)
- Test: `certificados/tests.py` (clase `EmailDeliveryModelTests`)

**Interfaces:**
- Produces: `EmailDelivery(event, attendee, download_log, to_email, full_name, status, attempts, last_error, created_at, sent_at, next_attempt_at)` con `STATUS_*` constantes y `Meta.indexes` en `(status, next_attempt_at)`. `SiteSettings.mail_from_name/mail_from_email/mail_reply_to/mail_subject/mail_body`. Settings: `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `MAIL_RATE_PER_MINUTE`, `MAIL_DAILY_CAP`, `MAIL_STUCK_MINUTES`.

- [ ] **Step 1: Test que falla**

```python
class EmailDeliveryModelTests(TestCase):
    def test_defaults_and_active_uniqueness(self):
        from .models import EmailDelivery
        ev = Event.objects.create(name="Vac", slug="vac")
        att = Attendee.objects.create(event=ev, full_name="Juan Pérez", email="juan@mail.com")
        d = EmailDelivery.objects.create(event=ev, attendee=att, to_email=att.email, full_name=att.full_name)
        self.assertEqual(d.status, EmailDelivery.STATUS_PENDING)
        self.assertEqual(d.attempts, 0)
        self.assertIsNotNone(d.next_attempt_at)
        self.assertTrue(EmailDelivery.objects.active_for(ev, att).exists())

    def test_site_settings_mail_defaults(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        self.assertEqual(s.mail_from_name, "Brisa Enfermeros")
        self.assertIn("{evento}", s.mail_subject)
```

- [ ] **Step 2: Correr** `manage.py test certificados.tests.EmailDeliveryModelTests` → FAIL (ImportError).

- [ ] **Step 3: Implementación**

En `SiteSettings` (antes de `class Meta`):

```python
    # --- Correo con el certificado adjunto (Opción A) ---
    mail_from_name = models.CharField(max_length=120, default="Brisa Enfermeros")
    mail_from_email = models.EmailField(blank=True, default="",
        help_text="Vacío = usa EMAIL_HOST_USER del servidor.")
    mail_reply_to = models.EmailField(blank=True, default="")
    mail_subject = models.CharField(max_length=200, default="Tu certificado del {evento}")
    mail_body = models.TextField(default=DEFAULT_MAIL_BODY,
        help_text="Texto plano. Variables: {nombre}, {evento}.")
```

Arriba del modelo (junto a `DEFAULT_MAINTENANCE_MESSAGE`):

```python
DEFAULT_MAIL_BODY = (
    "Hola {nombre},\n\n"
    "Adjuntamos tu certificado de participación en el {evento}.\n\n"
    "Guardalo en tu dispositivo. Ante cualquier consulta, responder este correo.\n\n"
    "Brisa Enfermeros"
)
```

Al final de `models.py`:

```python
class EmailDeliveryQuerySet(models.QuerySet):
    def active_for(self, event, attendee):
        return self.filter(
            event=event, attendee=attendee,
            status__in=[EmailDelivery.STATUS_PENDING, EmailDelivery.STATUS_SENDING, EmailDelivery.STATUS_SENT],
        )


class EmailDelivery(models.Model):
    """Cola de correos con el certificado adjunto. Una fila = un envío a un
    inscripto. El worker (send_certificate_emails) la procesa cada minuto."""

    STATUS_PENDING = "pending"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"), (STATUS_SENDING, "Enviando"),
        (STATUS_SENT, "Enviado"), (STATUS_FAILED, "Fallido"),
        (STATUS_SUPERSEDED, "Reemplazado"),
    ]
    MAX_ATTEMPTS = 5

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="email_deliveries")
    attendee = models.ForeignKey(Attendee, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_deliveries")
    download_log = models.ForeignKey(DownloadLog, null=True, blank=True, on_delete=models.SET_NULL)
    to_email = models.EmailField()
    full_name = models.CharField(max_length=200)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    objects = EmailDeliveryQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.event.slug} → {self.to_email} ({self.status})"
```

(`from django.utils import timezone` arriba de models.py.)

`config/settings.py` al final:

```python
# --- Correo (certificado adjunto). Proveedor agnóstico: SMTP por env. ---
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1").lower() in ("1", "true", "yes")
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "certificados@localhost")
MAIL_RATE_PER_MINUTE = int(os.environ.get("MAIL_RATE_PER_MINUTE", "30"))
MAIL_DAILY_CAP = int(os.environ.get("MAIL_DAILY_CAP", "450"))
MAIL_STUCK_MINUTES = 10
```

- [ ] **Step 4:** `makemigrations certificados -n emaildelivery_sitesettings_mail`; correr tests → PASS.
- [ ] **Step 5: Commit** `feat(mail): modelo EmailDelivery + config SMTP por env`.

---

### Task 2: `certificados/mailer.py` — encolar, armar y procesar

**Files:**
- Create: `certificados/mailer.py`
- Test: `certificados/tests.py` (clase `MailerTests`)

**Interfaces:**
- Consumes: `EmailDelivery`, `SiteSettings.load()`, `build_pdf_bytes(template, name)` de `views.py` (importar dentro de la función para evitar import circular).
- Produces:
  - `enqueue_certificate_email(event, attendee, full_name, download_log=None) -> (delivery, created: bool)`.
  - `build_message(delivery, site) -> EmailMultiAlternatives` (asunto/cuerpo con `{nombre}`/`{evento}`, adjunto `certificado-<slug>.pdf`).
  - `process_queue(now=None, max_items=None) -> dict(sent=int, failed=int, retried=int, skipped=int)`.
  - `BACKOFF_MINUTES = [1, 5, 15, 60, 180]`.

- [ ] **Step 1: Tests que fallan**

```python
@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MAIL_RATE_PER_MINUTE=100, MAIL_DAILY_CAP=0)
class MailerTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True); super().tearDownClass()

    def setUp(self):
        from django.core import mail
        mail.outbox = []
        self.event = Event.objects.create(name="X Congreso", slug="xc", require_email=True)
        CertificateTemplate.objects.create(event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords")
        self.att = Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")

    def test_enqueue_dedupes_per_attendee(self):
        from .mailer import enqueue_certificate_email
        d1, c1 = enqueue_certificate_email(self.event, self.att, "juan perez")
        d2, c2 = enqueue_certificate_email(self.event, self.att, "otro nombre")
        self.assertTrue(c1); self.assertFalse(c2); self.assertEqual(d1.pk, d2.pk)
        self.assertEqual(d1.to_email, "juan@mail.com")
        self.assertEqual(d1.full_name, "Juan Pérez")  # el de la lista, no el tipeado

    def test_process_sends_with_pdf_attached(self):
        from django.core import mail
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        r = process_queue()
        self.assertEqual(r["sent"], 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["juan@mail.com"])
        self.assertIn("X Congreso", msg.subject)
        self.assertIn("Juan Pérez", msg.body)
        name, content, mimetype = msg.attachments[0]
        self.assertEqual(name, "certificado-xc.pdf"); self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        d = EmailDelivery.objects.get(); self.assertEqual(d.status, "sent"); self.assertIsNotNone(d.sent_at)

    def test_failure_schedules_retry_then_fails(self):
        from unittest import mock
        from .mailer import enqueue_certificate_email, process_queue, BACKOFF_MINUTES
        from .models import EmailDelivery
        from django.utils import timezone
        from datetime import timedelta
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        with mock.patch("certificados.mailer._send", side_effect=RuntimeError("smtp caído")):
            r = process_queue()
        d = EmailDelivery.objects.get()
        self.assertEqual(r["retried"], 1); self.assertEqual(d.status, "pending"); self.assertEqual(d.attempts, 1)
        self.assertIn("smtp caído", d.last_error)
        self.assertGreater(d.next_attempt_at, timezone.now() + timedelta(seconds=50))
        # 4 fallos más → failed
        with mock.patch("certificados.mailer._send", side_effect=RuntimeError("x")):
            for _ in range(4):
                EmailDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
                process_queue()
        d.refresh_from_db(); self.assertEqual(d.status, "failed"); self.assertEqual(d.attempts, 5)

    def test_not_due_is_skipped(self):
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        from django.utils import timezone
        from datetime import timedelta
        d, _ = enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        EmailDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(process_queue()["sent"], 0)

    def test_rate_and_daily_cap(self):
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        for i in range(3):
            a = Attendee.objects.create(event=self.event, full_name=f"P {i}", email=f"p{i}@mail.com")
            enqueue_certificate_email(self.event, a, a.full_name)
        with self.settings(MAIL_RATE_PER_MINUTE=2):
            self.assertEqual(process_queue()["sent"], 2)
        with self.settings(MAIL_DAILY_CAP=2):
            self.assertEqual(process_queue()["sent"], 0)  # ya van 2 hoy
        self.assertEqual(process_queue()["sent"], 1)

    def test_stuck_sending_is_recovered(self):
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        from django.utils import timezone
        from datetime import timedelta
        d, _ = enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        EmailDelivery.objects.filter(pk=d.pk).update(status="sending", started_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(process_queue()["sent"], 1)

    def test_missing_template_fails_without_retry_storm(self):
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        CertificateTemplate.objects.all().delete()
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        r = process_queue()
        self.assertEqual(r["retried"], 1)
        self.assertIn("template", EmailDelivery.objects.get().last_error.lower())
```

- [ ] **Step 2: Correr** `manage.py test certificados.tests.MailerTests` → FAIL (no module mailer).

- [ ] **Step 3: Implementación** `certificados/mailer.py`:

```python
"""Cola de correos con el certificado adjunto (Opción A).

- enqueue_certificate_email(): lo llama el POST público. Nunca envía en línea.
- process_queue(): lo corre `manage.py send_certificate_emails` cada minuto
  (timer systemd). Reserva filas con UPDATE condicional (SQLite no tiene
  SKIP LOCKED), genera el PDF, envía por SMTP y aplica reintentos con espera
  creciente y topes por minuto/día.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import transaction
from django.utils import timezone

from .models import CertificateTemplate, EmailDelivery, SiteSettings

log = logging.getLogger("certificados.mail")

BACKOFF_MINUTES = [1, 5, 15, 60, 180]


def enqueue_certificate_email(event, attendee, full_name, download_log=None):
    """Encola un correo para el inscripto. Destinatario = email de la lista.
    Devuelve (delivery, created). Si ya hay uno activo, no crea otro."""
    existing = EmailDelivery.objects.active_for(event, attendee).order_by("-created_at").first()
    if existing:
        return existing, False
    d = EmailDelivery.objects.create(
        event=event, attendee=attendee, download_log=download_log,
        to_email=attendee.email, full_name=attendee.full_name or full_name,
    )
    return d, True


def _render(text, delivery):
    return text.replace("{nombre}", delivery.full_name).replace("{evento}", delivery.event.name)


def build_message(delivery, site=None):
    from .views import build_pdf_bytes  # import tardío: views importa models
    site = site or SiteSettings.load()
    template = CertificateTemplate.objects.filter(event=delivery.event).first()
    if template is None:
        raise ValueError("El evento no tiene template cargado")
    pdf = build_pdf_bytes(template, delivery.full_name)
    from_email = site.mail_from_email or settings.DEFAULT_FROM_EMAIL
    sender = f"{site.mail_from_name} <{from_email}>" if site.mail_from_name else from_email
    body = _render(site.mail_body, delivery)
    msg = EmailMultiAlternatives(
        subject=_render(site.mail_subject, delivery),
        body=body,
        from_email=sender,
        to=[delivery.to_email],
        reply_to=[site.mail_reply_to] if site.mail_reply_to else None,
    )
    html = "<p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    msg.attach_alternative(html, "text/html")
    msg.attach(f"certificado-{delivery.event.slug}.pdf", pdf, "application/pdf")
    return msg


def _send(msg, connection):
    msg.connection = connection
    msg.send(fail_silently=False)


def _recover_stuck(now):
    limit = now - timedelta(minutes=settings.MAIL_STUCK_MINUTES)
    return EmailDelivery.objects.filter(
        status=EmailDelivery.STATUS_SENDING, started_at__lt=limit
    ).update(status=EmailDelivery.STATUS_PENDING)


def _sent_today(now):
    return EmailDelivery.objects.filter(
        status=EmailDelivery.STATUS_SENT, sent_at__date=now.date()
    ).count()


def _claim(pk, now):
    """Reserva atómica: solo un proceso puede pasar la fila a `sending`."""
    return EmailDelivery.objects.filter(
        pk=pk, status=EmailDelivery.STATUS_PENDING
    ).update(status=EmailDelivery.STATUS_SENDING, started_at=now) == 1


def process_queue(now=None, max_items=None):
    now = now or timezone.now()
    result = {"sent": 0, "failed": 0, "retried": 0, "skipped": 0}
    _recover_stuck(now)

    budget = settings.MAIL_RATE_PER_MINUTE
    if settings.MAIL_DAILY_CAP:
        budget = min(budget, max(0, settings.MAIL_DAILY_CAP - _sent_today(now)))
    if max_items is not None:
        budget = min(budget, max_items)
    if budget <= 0:
        return result

    due = list(
        EmailDelivery.objects.filter(status=EmailDelivery.STATUS_PENDING, next_attempt_at__lte=now)
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:budget]
    )
    if not due:
        return result

    site = SiteSettings.load()
    connection = get_connection()
    try:
        connection.open()
    except Exception as exc:  # SMTP caído: reintentar todo más tarde sin quemar intentos
        log.error("mailer: no se pudo abrir la conexión SMTP: %s", exc)
        EmailDelivery.objects.filter(pk__in=due).update(
            last_error=f"SMTP no disponible: {exc}"[:500], next_attempt_at=now + timedelta(minutes=1)
        )
        result["skipped"] = len(due)
        return result

    try:
        for pk in due:
            if not _claim(pk, now):
                result["skipped"] += 1
                continue
            d = EmailDelivery.objects.select_related("event").get(pk=pk)
            try:
                msg = build_message(d, site)
                _send(msg, connection)
            except Exception as exc:
                attempts = d.attempts + 1
                err = f"{type(exc).__name__}: {exc}"[:500]
                if attempts >= EmailDelivery.MAX_ATTEMPTS:
                    status, nxt = EmailDelivery.STATUS_FAILED, d.next_attempt_at
                    result["failed"] += 1
                    log.error("mailer: %s FALLÓ definitivo (%s)", d, err)
                else:
                    wait = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
                    status, nxt = EmailDelivery.STATUS_PENDING, now + timedelta(minutes=wait)
                    result["retried"] += 1
                    log.warning("mailer: %s reintento %d en %d min (%s)", d, attempts, wait, err)
                EmailDelivery.objects.filter(pk=pk).update(
                    status=status, attempts=attempts, last_error=err, next_attempt_at=nxt
                )
                continue
            EmailDelivery.objects.filter(pk=pk).update(
                status=EmailDelivery.STATUS_SENT, attempts=d.attempts + 1, sent_at=timezone.now(), last_error=""
            )
            result["sent"] += 1
    finally:
        try:
            connection.close()
        except Exception:
            pass
    log.info("mailer: %s", result)
    return result
```

- [ ] **Step 4:** tests → PASS. (Si `locmem.open()` no existe como método: `EmailBackend.open()` sí existe en todos los backends de Django.)
- [ ] **Step 5: Commit** `feat(mail): cola de envío con reintentos, topes y recuperación`.

---

### Task 3: Comando `send_certificate_emails` + timer systemd + verificar.sh

**Files:**
- Create: `certificados/management/__init__.py`, `certificados/management/commands/__init__.py`, `certificados/management/commands/send_certificate_emails.py`
- Create: `deploy/certificados-mailer.service`, `deploy/certificados-mailer.timer`
- Modify: `deploy/verificar.sh` (bloques 1 y 9), `deploy/setup_vps.sh` (habilitar timer), `README.md` (sección Deploy: variables EMAIL_*)
- Test: `certificados/tests.py` (`SendCommandTests`)

**Interfaces:**
- Produces: `manage.py send_certificate_emails [--max N]` imprime `sent=.. failed=.. retried=.. skipped=..`.

- [ ] **Step 1: Test**

```python
@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", MAIL_DAILY_CAP=0)
class SendCommandTests(TestCase):
    def test_command_processes_queue(self):
        from io import StringIO
        from django.core.management import call_command
        from .mailer import enqueue_certificate_email
        ev = Event.objects.create(name="Vac", slug="vac")
        CertificateTemplate.objects.create(event=ev, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords")
        att = Attendee.objects.create(event=ev, full_name="Ana", email="ana@mail.com")
        enqueue_certificate_email(ev, att, "Ana")
        out = StringIO(); call_command("send_certificate_emails", stdout=out)
        self.assertIn("sent=1", out.getvalue())
```

- [ ] **Step 2:** FAIL (Unknown command).
- [ ] **Step 3: Implementación**

```python
# certificados/management/commands/send_certificate_emails.py
from django.core.management.base import BaseCommand
from certificados.mailer import process_queue


class Command(BaseCommand):
    help = "Envía los correos pendientes con el certificado adjunto (una tanda)."

    def add_arguments(self, parser):
        parser.add_argument("--max", type=int, default=None, help="tope de correos en esta corrida")

    def handle(self, *args, **opts):
        r = process_queue(max_items=opts["max"])
        self.stdout.write(" ".join(f"{k}={v}" for k, v in r.items()))
```

`deploy/certificados-mailer.service`:

```
[Unit]
Description=Envío de certificados por email (una tanda por minuto)
After=network.target

[Service]
Type=oneshot
User=certif
Group=www-data
WorkingDirectory=/opt/certificados
EnvironmentFile=/etc/certificados.env
ExecStart=/opt/certificados/.venv/bin/python /opt/certificados/manage.py send_certificate_emails
```

`deploy/certificados-mailer.timer`:

```
[Unit]
Description=Cada minuto: enviar correos pendientes de certificados

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
Persistent=false

[Install]
WantedBy=timers.target
```

`verificar.sh`: en el bloque 1 agregar `certificados-mailer.timer` a la lista de unidades; agregar bloque 10:

```bash
# 10. cola de correos: sin trabados ni pendientes viejos
res=$(systemctl show certificados-mailer.service -p Result --value)
[ "$res" = success ] && ok "mailer ultima corrida: $res" || bad "mailer ultima corrida: $res (journalctl -u certificados-mailer)"
old=$(sqlite3 "$DB" "select count(*) from certificados_emaildelivery where status='pending' and next_attempt_at < datetime('now','-30 minutes') and attempts=0;")
[ "$old" -eq 0 ] && ok "sin correos pendientes de mas de 30 min" || bad "$old correos pendientes hace mas de 30 min (¿timer parado? ¿SMTP sin configurar?)"
stuck=$(sqlite3 "$DB" "select count(*) from certificados_emaildelivery where status='sending' and started_at < datetime('now','-10 minutes');")
[ "$stuck" -eq 0 ] && ok "sin correos trabados en 'sending'" || bad "$stuck correos trabados en sending"
```

README (sección Deploy): nota "Si se tocó `deploy/*.service|timer`: copiar a `/etc/systemd/system/`, `daemon-reload`, `enable --now certificados-mailer.timer`" + lista de variables `EMAIL_*`/`MAIL_*` con el ejemplo de Gmail y el de Postmark.

- [ ] **Step 4:** tests → PASS. `bash -n deploy/verificar.sh` sin errores.
- [ ] **Step 5: Commit** `feat(mail): comando send_certificate_emails + timer systemd + verificar.sh`.

---

### Task 4: Formulario público — casilla, encolado y aviso en "Certificado listo"

**Files:**
- Modify: `certificados/templates/certificados/event_page.html` (después del campo Email), `certificados/templates/certificados/home.html` (ídem)
- Modify: `certificados/views.py` (`_build_certificate_response`: leer `send_email`, encolar tras crear/reusar log; contexto `mail_notice`)
- Modify: `certificados/templates/certificados/download_ready.html` (aviso verde debajo del botón PDF)
- Test: `certificados/tests.py` (`PublicEmailOptInTests`)

**Interfaces:**
- Consumes: `enqueue_certificate_email(event, attendee, full_name, download_log)`.
- Produces: POST param `send_email` ("on" cuando la casilla está marcada); contexto `mail_to` (str o "") y `mail_already` (bool) en `download_ready.html`.

- [ ] **Step 1: Tests**

```python
@override_settings(MEDIA_ROOT=MEDIA)
class PublicEmailOptInTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True); super().tearDownClass()

    def setUp(self):
        self.event = Event.objects.create(name="Vac", slug="vac", require_email=True)
        CertificateTemplate.objects.create(event=self.event, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords")
        self.att = Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": "vac"})

    def test_form_has_checked_checkbox(self):
        resp = self.client.get(reverse("event_page", kwargs={"slug": "vac"}))
        self.assertContains(resp, 'name="send_email"')
        self.assertContains(resp, "Enviarme también una copia por email")
        self.assertContains(resp, 'checked')

    def test_checked_enqueues_to_list_email_and_shows_notice(self):
        from .models import EmailDelivery
        resp = self.client.post(self.url + "?embed=1", {"full_name": "juan perez", "email": "JUAN@mail.com", "send_email": "on"})
        self.assertContains(resp, "Te enviamos una copia a")
        self.assertContains(resp, "juan@mail.com")
        d = EmailDelivery.objects.get()
        self.assertEqual(d.to_email, "juan@mail.com"); self.assertEqual(d.full_name, "Juan Pérez")
        self.assertIsNotNone(d.download_log)

    def test_unchecked_does_not_enqueue(self):
        from .models import EmailDelivery
        resp = self.client.post(self.url + "?embed=1", {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        self.assertNotContains(resp, "Te enviamos una copia")
        self.assertEqual(EmailDelivery.objects.count(), 0)

    def test_not_in_list_never_enqueues(self):
        from .models import EmailDelivery
        self.client.post(self.url, {"full_name": "Nadie", "email": "nadie@mail.com", "send_email": "on"})
        self.assertEqual(EmailDelivery.objects.count(), 0)

    def test_direct_flow_enqueues_too(self):
        from .models import EmailDelivery
        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com", "send_email": "on"})
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertEqual(EmailDelivery.objects.count(), 1)

    def test_second_request_within_grace_says_already_sent(self):
        self.client.post(self.url + "?embed=1", {"full_name": "Juan Pérez", "email": "juan@mail.com", "send_email": "on"})
        resp = self.client.post(self.url + "?embed=1", {"full_name": "Juan Pérez", "email": "juan@mail.com", "send_email": "on"})
        self.assertContains(resp, "Ya te enviamos una copia a")

    def test_email_does_not_consume_single_use(self):
        import re
        resp = self.client.post(self.url + "?embed=1", {"full_name": "Juan Pérez", "email": "juan@mail.com", "send_email": "on"})
        pdf = re.search(r'href="[^"]*(/e/vac/descargar/[^"]+/)"', resp.content.decode()).group(1)
        self.assertEqual(self.client.get(pdf)["Content-Type"], "application/pdf")
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementación**

Casilla (mismo bloque en `event_page.html` dentro de `{% if event.require_email %}` después del input Email, y en `home.html` después del Email):

```html
            <label class="check-row">
                <input type="checkbox" name="send_email" value="on" checked>
                <span>Enviarme también una copia por email</span>
            </label>
```

CSS (en ambos templates, junto a `.form-group`):

```css
        .check-row { display: flex; gap: 10px; align-items: flex-start; font-size: 14px; color: #1a1d23; margin: -8px 0 24px; line-height: 1.4; cursor: pointer; }
        .check-row input { width: 18px; height: 18px; margin-top: 2px; accent-color: var(--mensaje); flex: none; }
```

`views.py` — en `_build_certificate_response`, después del bloque que crea/reusa `log` y antes de `if not manual and request.GET.get("embed"):`:

```python
    mail_to, mail_already = "", False
    if (
        not manual
        and matched_attendee is not None
        and (request.POST.get("send_email") or "") == "on"
    ):
        from .mailer import enqueue_certificate_email
        delivery, created = enqueue_certificate_email(event, matched_attendee, full_name, download_log=log)
        mail_to, mail_already = delivery.to_email, not created
```

y en el `render(... download_ready.html ...)` agregar `"mail_to": mail_to, "mail_already": mail_already`.

`download_ready.html`, debajo del `<a class="download-btn" ...>` y antes de `<br>`:

```html
        {% if mail_to %}
        <div class="mail-ok">
            {% if mail_already %}Ya te enviamos una copia a{% else %}Te enviamos una copia a{% endif %}
            <strong>{{ mail_to }}</strong>. Si no llega en unos minutos, revisar correo no deseado.
        </div>
        {% endif %}
```

CSS: `.mail-ok { background:#ecfdf5; border:1.5px solid #047857; color:#047857; padding:12px 14px; border-radius:10px; font-size:14px; font-weight:500; margin:14px 0 0; text-align:left; }`.

- [ ] **Step 4:** tests → PASS (suite completa).
- [ ] **Step 5: Commit** `feat(mail): casilla "copia por email" en el form público + aviso en Certificado listo`.

---

### Task 5: Panel — sección Correos (lista, filtros, reenviar, CSV) + dashboard + nav

**Files:**
- Modify: `certificados/panel_views.py` (nuevas vistas `panel_mail`, `panel_mail_resend`, `panel_mail_export`; dashboard: `mail_sent`, `mail_failed`, `mail_pending`)
- Modify: `certificados/panel_urls.py`
- Create: `certificados/templates/panel/mail.html`
- Modify: `certificados/templates/panel/base.html` (nav: "Correos" entre Rechazados y SISTEMA), `certificados/templates/panel/dashboard.html` (tarjeta "Correos enviados" con sub "N fallidos · N pendientes")
- Test: `certificados/tests.py` (`PanelMailTests`)

**Interfaces:**
- Produces: rutas `panel_mail` (`correos/`), `panel_mail_export` (`correos/exportar/`), `panel_mail_resend` (`correos/<int:pk>/reenviar/`, POST).

- [ ] **Step 1: Tests**

```python
class PanelMailTests(TestCase):
    def setUp(self):
        from .models import EmailDelivery
        self.user = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Vac", slug="vac")
        self.att = Attendee.objects.create(event=self.event, full_name="Ana López", email="ana@mail.com")
        self.d = EmailDelivery.objects.create(event=self.event, attendee=self.att, to_email="ana@mail.com", full_name="Ana López", status="failed", last_error="SMTP 550")

    def test_list_filters_and_shows_error(self):
        resp = self.client.get(reverse("panel_mail") + "?status=failed&search=ana")
        self.assertContains(resp, "Ana López"); self.assertContains(resp, "SMTP 550")
        self.assertNotContains(self.client.get(reverse("panel_mail") + "?status=sent"), "Ana López")

    def test_resend_creates_new_and_supersedes_old(self):
        from .models import EmailDelivery
        self.client.post(reverse("panel_mail_resend", kwargs={"pk": self.d.pk}))
        self.d.refresh_from_db(); self.assertEqual(self.d.status, "superseded")
        new = EmailDelivery.objects.exclude(pk=self.d.pk).get()
        self.assertEqual(new.status, "pending"); self.assertEqual(new.to_email, "ana@mail.com")

    def test_export_csv(self):
        resp = self.client.get(reverse("panel_mail_export"))
        body = b"".join(resp.streaming_content).decode("utf-8")
        self.assertIn("ana@mail.com", body); self.assertTrue(body.startswith("Evento,Nombre,Email,Estado"))

    def test_dashboard_shows_mail_tile(self):
        self.assertContains(self.client.get(reverse("panel_dashboard")), "Correos enviados")
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementación** (patrón calcado de `panel_logs`/`panel_logs_export`):

```python
@login_required(login_url="panel_login")
def panel_mail(request):
    from .models import EmailDelivery
    qs = EmailDelivery.objects.select_related("event").order_by("-created_at")
    event_filter = request.GET.get("event") or ""
    status = request.GET.get("status", "")
    search = (request.GET.get("search") or "").strip()
    if event_filter:
        qs = qs.filter(event_id=event_filter)
    if status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(Q(full_name__icontains=search) | Q(to_email__icontains=search))
    page = int(request.GET.get("page", 1)); per_page = 25
    total = qs.count(); total_pages = max(1, (total + per_page - 1) // per_page); page = min(max(page, 1), total_pages)
    counts = {row["status"]: row["n"] for row in EmailDelivery.objects.values("status").annotate(n=Count("id"))}
    return render(request, "panel/mail.html", {
        "active_page": "mail", "items": qs[(page - 1) * per_page: page * per_page],
        "events": Event.objects.order_by("name"), "event_filter": event_filter, "status": status,
        "search": search, "page": page, "total_pages": total_pages, "total": total,
        "counts": counts, "statuses": EmailDelivery.STATUS_CHOICES,
    })


@login_required(login_url="panel_login")
@require_POST
def panel_mail_resend(request, pk):
    from .models import EmailDelivery
    old = get_object_or_404(EmailDelivery, pk=pk)
    old.status = EmailDelivery.STATUS_SUPERSEDED; old.save(update_fields=["status"])
    EmailDelivery.objects.create(event=old.event, attendee=old.attendee, download_log=old.download_log,
                                 to_email=old.to_email, full_name=old.full_name)
    messages.success(request, f"Reenvío encolado para {old.to_email}.")
    return redirect(request.POST.get("next") or "panel_mail")


@login_required(login_url="panel_login")
def panel_mail_export(request):
    from .models import EmailDelivery
    qs = EmailDelivery.objects.select_related("event").order_by("-created_at")
    def generate():
        buf = StringIO(); w = csv.writer(buf, dialect="excel")
        w.writerow(["Evento", "Nombre", "Email", "Estado", "Intentos", "Creado", "Enviado", "Error"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for d in qs.iterator():
            w.writerow([d.event.name, d.full_name, d.to_email, d.get_status_display(), d.attempts,
                        d.created_at.strftime("%Y-%m-%d %H:%M:%S"), d.sent_at.strftime("%Y-%m-%d %H:%M:%S") if d.sent_at else "", d.last_error])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    resp = StreamingHttpResponse(generate(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="correos.csv"'
    return resp
```

Dashboard: agregar al contexto `"mail_sent": EmailDelivery.objects.filter(status="sent").count(), "mail_failed": ...("failed"), "mail_pending": ...(status__in=["pending","sending"])` y una tarjeta:

```html
    <div class="stat-card">
        <div class="stat-label">Correos enviados</div>
        <div class="stat-value stat-info">{{ mail_sent }}</div>
        <div class="stat-sub">{{ mail_failed }} fallido{{ mail_failed|pluralize }} &middot; {{ mail_pending }} pendiente{{ mail_pending|pluralize }}</div>
    </div>
```

`panel_urls.py`:

```python
    path("correos/", v.panel_mail, name="panel_mail"),
    path("correos/exportar/", v.panel_mail_export, name="panel_mail_export"),
    path("correos/<int:pk>/reenviar/", v.panel_mail_resend, name="panel_mail_resend"),
```

`base.html` nav (después de Rechazados):

```html
        <a href="{% url 'panel_mail' %}" class="{% if active_page == 'mail' %}active{% endif %}">
            <span class="nav-icon">&#9993;</span> Correos
        </a>
```

`mail.html`: extender `panel/base.html`; filtros (evento, estado, búsqueda, Filtrar/Limpiar); resumen de conteos por estado como chips; tabla Nombre / Email / Evento / Estado (badge: sent=success, pending/sending=info, failed=danger, superseded=gris) / Intentos / Fecha (sent_at o created_at) / Error (monospace 12px, truncado a 80 con title completo) / Acción: form POST a `panel_mail_resend` con botón "Reenviar" (para failed/sent) y `{% csrf_token %}` + `<input type=hidden name=next value="{{ request.get_full_path }}">`. Paginación igual que `logs.html`.

- [ ] **Step 4:** tests → PASS.
- [ ] **Step 5: Commit** `feat(panel): sección Correos + tarjeta en dashboard`.

---

### Task 6: Apariencia — bloque "Correo" + correo de prueba

**Files:**
- Modify: `certificados/panel_views.py` (`panel_site_settings`: leer 5 campos; nueva vista `panel_mail_test`)
- Modify: `certificados/templates/panel/site_settings.html` (bloque "Correo con el certificado")
- Modify: `certificados/panel_urls.py` (`correos/prueba/`)
- Test: `certificados/tests.py` (`SiteSettingsMailTests`)

- [ ] **Step 1: Tests**

```python
class SiteSettingsMailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def test_saves_mail_fields(self):
        from .models import SiteSettings
        self.client.post(reverse("panel_site_settings"), {"color_fondo": "#ffffff", "color_mensaje": "#1d4ed8", "titulo": "T", "mensaje": "M",
            "mail_from_name": "Brisa", "mail_from_email": "certificados@brisa.test", "mail_reply_to": "hola@brisa.test",
            "mail_subject": "Certificado {evento}", "mail_body": "Hola {nombre}"})
        s = SiteSettings.load()
        self.assertEqual(s.mail_from_email, "certificados@brisa.test"); self.assertEqual(s.mail_body, "Hola {nombre}")

    def test_test_mail_enqueues_to_given_address(self):
        from .models import EmailDelivery
        ev = Event.objects.create(name="Vac", slug="vac")
        CertificateTemplate.objects.create(event=ev, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords")
        resp = self.client.post(reverse("panel_mail_test"), {"event": ev.pk, "to": "yo@test.com"}, follow=True)
        d = EmailDelivery.objects.get(); self.assertEqual(d.to_email, "yo@test.com"); self.assertEqual(d.full_name, "Nombre de Prueba")
        self.assertContains(resp, "encolado")
```

- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implementación**: en `panel_site_settings` POST leer `mail_from_name` (default "Brisa Enfermeros"), `mail_from_email`, `mail_reply_to`, `mail_subject` (default si vacío), `mail_body` (default si vacío). Vista:

```python
@login_required(login_url="panel_login")
@require_POST
def panel_mail_test(request):
    from .models import EmailDelivery
    event = get_object_or_404(Event, pk=request.POST.get("event"))
    to = (request.POST.get("to") or "").strip()
    try:
        validate_email(to)
    except ValidationError:
        messages.error(request, "Ingresar un email válido para la prueba.")
        return redirect("panel_site_settings")
    EmailDelivery.objects.create(event=event, attendee=None, to_email=to, full_name="Nombre de Prueba")
    messages.success(request, f"Correo de prueba encolado para {to}. Sale en el próximo minuto; ver estado en Correos.")
    return redirect("panel_site_settings")
```

Template: card "Correo con el certificado" con los 5 campos (help: variables `{nombre}` y `{evento}`; remitente vacío = casilla del servidor), y debajo un `<form method="post" action="{% url 'panel_mail_test' %}">` con select de evento + input email + botón "Enviar correo de prueba".

- [ ] **Step 4:** tests → PASS.
- [ ] **Step 5: Commit** `feat(panel): configuración del correo en Apariencia + correo de prueba`.

---

### Task 7: Deploy al VPS (sin credenciales todavía)

- [ ] Backup pre-deploy: `sqlite3 .backup /var/backups/certificados/db-pre-deploy-<fecha>.sqlite3`.
- [ ] One-liner del README (migra 0014) + copiar `deploy/certificados-mailer.{service,timer}` a `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now certificados-mailer.timer`.
- [ ] Sin `EMAIL_HOST` el backend SMTP falla al abrir conexión → `process_queue` marca `skipped` y reintenta al minuto sin quemar intentos (tal como está diseñado). Verificar en journal que el timer corre `Finished` y no hay tracebacks.
- [ ] `verificar.sh` → PASS (el chequeo de "pendientes >30 min" solo falla si alguien encoló sin SMTP; en ese caso avisar a Julián).
- [ ] Probar en prod: POST embed con `send_email=on` para juli@test.com → aparece "Te enviamos una copia a juli@test.com" y queda `pending` en Panel → Correos. Borrar log + delivery de prueba.
- [ ] Cuando llegue la casilla Gmail: agregar `EMAIL_HOST=smtp.gmail.com EMAIL_PORT=587 EMAIL_USE_TLS=1 EMAIL_HOST_USER=... EMAIL_HOST_PASSWORD=...` a `/etc/certificados.env` (root, 600), `systemctl restart certificados`, correo de prueba desde Apariencia, y `verificar.sh`.
