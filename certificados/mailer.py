"""Cola de correos con el certificado adjunto (Opción A).

- enqueue_certificate_email(): lo llama el POST público. Nunca envía en línea.
- process_queue(): lo corre `manage.py send_certificate_emails` cada minuto
  (timer systemd). Reserva filas con UPDATE condicional (SQLite no tiene
  SKIP LOCKED), genera el PDF, envía por SMTP y aplica reintentos con espera
  creciente y topes por minuto/día.

Proveedor agnóstico: todo sale por el backend de correo de Django, que se
configura con las variables EMAIL_* de /etc/certificados.env (Gmail hoy,
Postmark/Resend mañana: mismo código).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils import timezone

from .models import CertificateTemplate, EmailDelivery, SiteSettings

log = logging.getLogger("certificados.mail")

# Espera antes de cada reintento (minutos), por número de intento fallido.
BACKOFF_MINUTES = [1, 5, 15, 60, 180]


def enqueue_certificate_email(event, attendee, full_name, download_log=None):
    """Encola un correo para el inscripto. Destinatario = email de la LISTA
    (nunca el tipeado). Devuelve (delivery, created). Si ya hay uno activo
    (pendiente, enviando o enviado) para ese inscripto y evento, no crea otro.
    """
    existing = (
        EmailDelivery.objects.active_for(event, attendee)
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing, False
    delivery = EmailDelivery.objects.create(
        event=event,
        attendee=attendee,
        download_log=download_log,
        to_email=attendee.email,
        full_name=attendee.full_name or full_name,
    )
    return delivery, True


def _render(text, delivery):
    return (text or "").replace("{nombre}", delivery.full_name).replace(
        "{evento}", delivery.event.name
    )


def build_message(delivery, site=None):
    """Arma el correo (texto + HTML) con el PDF adjunto. Levanta ValueError si
    el evento no tiene template."""
    from .views import build_pdf_bytes  # import tardío: views importa models

    site = site or SiteSettings.load()
    template = CertificateTemplate.objects.filter(event=delivery.event).first()
    if template is None:
        raise ValueError("El evento no tiene template de certificado cargado")
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
    paragraphs = [p.strip().replace("\n", "<br>") for p in body.split("\n\n") if p.strip()]
    html = "".join(f"<p>{p}</p>" for p in paragraphs)
    msg.attach_alternative(html, "text/html")
    msg.attach(f"certificado-{delivery.event.slug}.pdf", pdf, "application/pdf")
    return msg


def _send(msg, connection):
    msg.connection = connection
    msg.send(fail_silently=False)


def _recover_stuck(now):
    """Un `sending` viejo es un proceso que murió a mitad: vuelve a la cola."""
    limit = now - timedelta(minutes=settings.MAIL_STUCK_MINUTES)
    return EmailDelivery.objects.filter(
        status=EmailDelivery.STATUS_SENDING, started_at__lt=limit
    ).update(status=EmailDelivery.STATUS_PENDING)


def _sent_today(now):
    """Enviados en el día LOCAL (America/Argentina): `sent_at__date` compara
    en la zona horaria del proyecto, así que la fecha de referencia también
    tiene que ser local. Con `now.date()` (UTC) el tope diario se reseteaba a
    las 21:00 hora argentina."""
    return EmailDelivery.objects.filter(
        status=EmailDelivery.STATUS_SENT, sent_at__date=timezone.localdate(now)
    ).count()


def _claim(pk, now):
    """Reserva atómica: solo un proceso puede pasar la fila a `sending`."""
    return (
        EmailDelivery.objects.filter(pk=pk, status=EmailDelivery.STATUS_PENDING).update(
            status=EmailDelivery.STATUS_SENDING, started_at=now
        )
        == 1
    )


def process_queue(now=None, max_items=None):
    """Envía una tanda de correos pendientes. Devuelve conteos por resultado."""
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
        EmailDelivery.objects.filter(
            status=EmailDelivery.STATUS_PENDING, next_attempt_at__lte=now
        )
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:budget]
    )
    if not due:
        return result

    site = SiteSettings.load()
    connection = get_connection()
    try:
        connection.open()
    except Exception as exc:
        # SMTP caído o sin configurar: no quema intentos, reintenta al minuto.
        log.error("mailer: no se pudo abrir la conexión SMTP: %s", exc)
        EmailDelivery.objects.filter(pk__in=due).update(
            last_error=f"SMTP no disponible: {exc}"[:500],
            next_attempt_at=now + timedelta(minutes=1),
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
                    log.error("mailer: %s FALLÓ definitivamente (%s)", d, err)
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
                status=EmailDelivery.STATUS_SENT,
                attempts=d.attempts + 1,
                sent_at=timezone.now(),
                last_error="",
            )
            result["sent"] += 1
    finally:
        try:
            connection.close()
        except Exception:
            pass
    log.info("mailer: %s", result)
    return result
