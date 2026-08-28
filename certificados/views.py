import logging
from functools import wraps
from io import BytesIO

from django.contrib import messages
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics

from .models import (
    Event,
    CertificateTemplate,
    DownloadLog,
    RejectedAttempt,
    Attendee,
    normalize_text,
    normalize_email,
)


# Mensaje exacto definido en el cronograma para descargas duplicadas.
DUPLICATE_MESSAGE = (
    "Verificamos que este certificado ya fue descargado. "
    "Para consultas, contactarse con contacto.brisaplus@brisasg.com.ar."
)

logger = logging.getLogger(__name__)


def public_safety_net(view_func):
    """Última línea de defensa de las vistas públicas.

    Si algo explota de forma imprevista, el traceback completo va al log
    (journald) y la persona vuelve al formulario con un mensaje para
    reintentar — nunca la pantalla genérica de error. Los 404 (evento
    inexistente/inactivo) pasan de largo: son intencionales.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except Http404:
            raise
        except Exception:
            logger.exception(
                "Error inesperado en vista pública %s", view_func.__name__
            )
            try:
                # PRG solo para POST; un GET roto que redirigiera a sí mismo
                # entraría en loop de redirects.
                if request.method != "POST":
                    return render(request, "errors/500.html", status=500)
                messages.error(
                    request,
                    "No pudimos procesar el pedido. Volver a intentar en unos segundos.",
                )
                embed_qs = "?embed=1" if request.GET.get("embed") else ""
                slug = kwargs.get("slug") or (request.POST.get("event_slug") or "").strip()
                if slug and Event.objects.filter(slug=slug).exists():
                    return redirect(reverse("event_page", kwargs={"slug": slug}) + embed_qs)
                return redirect(reverse("home") + embed_qs)
            except Exception:
                logger.exception("El fallback de public_safety_net también falló")
                return render(request, "errors/500.html", status=500)

    return wrapper


def healthz(request):
    """Healthcheck para el watchdog: toca la base y responde ok."""
    Event.objects.exists()
    return HttpResponse("ok", content_type="text/plain")


def _get_client_ip(request):
    return request.META.get("REMOTE_ADDR")


def _log_rejected(event, request, reason, name="", email=""):
    """Registra un intento de descarga rechazado para métricas y seguimiento."""
    RejectedAttempt.objects.create(
        event=event,
        name_entered=(name or "")[:200],
        email_entered=(email or "")[:254],
        reason=reason,
        ip=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )


def fit_font_size(text, font_name, base_size, max_width, min_size=6.0):
    """Return a font size <= base_size so that `text` fits within `max_width`.

    If max_width <= 0 (auto-ajuste desactivado) or the text already fits,
    returns base_size unchanged. Never returns below min_size.
    """
    base_size = float(base_size)
    if not text or not max_width or max_width <= 0:
        return base_size
    width = pdfmetrics.stringWidth(text, font_name, base_size)
    if width <= max_width:
        return base_size
    return max(min_size, base_size * (max_width / width))


def baseline_offset(font_name, font_size, valign):
    """Cuánto restar al Y para anclar el texto según `valign`.

    `drawString` siempre dibuja en el baseline. Para anclar por el tope o el
    centro corremos el Y usando las métricas de la fuente.
        baseline -> 0 (sin corrimiento)
        top      -> ascent
        middle   -> ascent / 2
    """
    valign = (valign or "baseline").lower()
    if valign == "baseline":
        return 0.0
    face = pdfmetrics.getFont(font_name).face
    ascent_pt = face.ascent / 1000.0 * float(font_size)
    if valign == "top":
        return ascent_pt
    if valign == "middle":
        return ascent_pt / 2.0
    return 0.0


def build_pdf_bytes(template, full_name):
    """Generate the certificate PDF bytes for the given template + name.

    Raises ValueError on bad page number.
    """
    reader = PdfReader(template.pdf.path)
    writer = PdfWriter()

    page_index = template.page_number
    if page_index >= len(reader.pages):
        raise ValueError("page_number inválido para este PDF.")

    for i, page in enumerate(reader.pages):
        if i == page_index:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)

            packet = BytesIO()
            c = canvas.Canvas(packet, pagesize=(width, height))

            font_name = "Helvetica"
            font_size = fit_font_size(
                full_name, font_name, template.font_size, getattr(template, "max_width", 0)
            )
            c.setFont(font_name, font_size)

            x = float(template.x)
            y = float(template.y)

            align = (template.align or "center").lower()
            text_width = pdfmetrics.stringWidth(full_name, font_name, font_size)

            if align == "center":
                draw_x = x - (text_width / 2.0)
            elif align == "right":
                draw_x = x - text_width
            else:
                draw_x = x

            draw_y = y - baseline_offset(font_name, font_size, getattr(template, "valign", "baseline"))
            c.drawString(draw_x, draw_y, full_name)
            c.save()

            packet.seek(0)
            overlay_pdf = PdfReader(packet)
            overlay_page = overlay_pdf.pages[0]

            page.merge_page(overlay_page)

        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _find_attendee_by_email(event, email):
    """Return the matching Attendee for this event by email alone, or None.

    El email es el dato de referencia (más confiable que lo que la persona
    tipea a mano); el nombre no se exige exacto, así una persona que
    escribe mal su nombre igual recibe el certificado. El nombre correcto
    sale de la lista de inscriptos, no de lo que ingresó."""
    email_norm = normalize_email(email)
    if not email_norm:
        return None
    return event.attendees.filter(email_normalized=email_norm).first()


def _build_certificate_response(event, full_name, request, manual=False, email="", failure_redirect=None):
    """Validate, log and generate the certificate PDF as a FileResponse.

    Validation rules (skipped when manual=True):
    - If event.require_email is True: email is mandatory, and matching
      against the attendee list is by email alone (el nombre tipeado no
      necesita coincidir exacto: tolera errores de tipeo). El certificado
      se genera con el nombre cargado en la lista, no con lo que escribió
      la persona.
    - If event.require_email is False: matches by name (case- and
      accent-insensitive) since no email is collected.
    - If event has no attendees loaded: free download.
    """
    if not full_name:
        return HttpResponseBadRequest("Nombre vacío.")
    if len(full_name) > 200:
        return HttpResponseBadRequest("Nombre demasiado largo (máx 200).")

    def _fail(msg, reason):
        _log_rejected(event, request, reason, name=full_name, email=email)
        messages.error(request, msg)
        # Preservar el modo embed para que el error se renderice dentro del iframe.
        embed_qs = "?embed=1" if request.GET.get("embed") else ""
        if failure_redirect == "home":
            return redirect(reverse("home") + embed_qs)
        return redirect(reverse("event_page", kwargs={"slug": event.slug}) + embed_qs)

    matched_attendee = None

    if not manual:
        has_attendees = event.attendees.exists()

        if event.require_email and not email:
            return _fail("Ingresar el email.", "missing_email")

        if has_attendees:
            if not event.require_email:
                # Lista cargada pero no se pide email: solo validamos por nombre
                name_norm = normalize_text(full_name)
                matched_attendee = event.attendees.filter(full_name_normalized=name_norm).first()
            else:
                matched_attendee = _find_attendee_by_email(event, email)
            if matched_attendee is None:
                return _fail(
                    "No figura en la lista de inscriptos. Verificar los datos ingresados.",
                    "not_in_list",
                )
            full_name = matched_attendee.full_name

        # Bloqueo por límite de descargas: ¿cuántas veces descargó ya esta persona?
        prior = DownloadLog.objects.filter(event=event, manual=False)
        if matched_attendee is not None:
            # No alcanza con el FK: al reimportar la lista con "Reemplazar",
            # los inscriptos se recrean y el FK del log queda en NULL. La
            # identidad normalizada del log mantiene el bloqueo entre imports.
            identity = Q(attendee=matched_attendee)
            if event.require_email:
                identity |= Q(email_normalized=matched_attendee.email_normalized)
            else:
                identity |= Q(name_normalized=matched_attendee.full_name_normalized)
            prior_count = prior.filter(identity).count()
        elif email:
            prior_count = prior.filter(
                email_normalized=normalize_email(email)
            ).count()
        else:
            prior_count = prior.filter(
                name_normalized=normalize_text(full_name)
            ).count()
        if event.download_limit and prior_count >= event.download_limit:
            return _fail(event.duplicate_message or DUPLICATE_MESSAGE, "duplicate")

    template = get_object_or_404(CertificateTemplate, event=event)

    try:
        pdf_bytes = build_pdf_bytes(template, full_name)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    except Exception:
        # Template ilegible (p.ej. una imagen subida como PDF, caso 27-08)
        # o generación rota: error amigable en vez de 500. Importante: sin
        # registrar DownloadLog, la persona no recibió nada.
        msg = "El certificado no se puede generar en este momento. Avisar al organizador del evento."
        if manual:
            return HttpResponseBadRequest(msg)
        return _fail(msg, "template_error")

    # Se registra la descarga recién acá: si la generación falla, el intento
    # no debe consumir el límite de descargas de la persona.
    DownloadLog.objects.create(
        event=event,
        name_entered=full_name,
        name_normalized=normalize_text(full_name),
        email_normalized=normalize_email(email),
        attendee=matched_attendee,
        ip=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        manual=manual,
    )

    filename = f"certificado-{event.slug}.pdf"
    return FileResponse(BytesIO(pdf_bytes), as_attachment=True, filename=filename)


def server_error(request):
    """handler500: página de error amigable (requiere DEBUG=False)."""
    return render(request, "errors/500.html", status=500)


@xframe_options_exempt
@public_safety_net
def home_page(request):
    events = Event.objects.filter(active=True).order_by("name")
    return render(request, "certificados/home.html", {"events": events})


@xframe_options_exempt
@public_safety_net
def download_from_home(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Método no permitido.")

    slug = (request.POST.get("event_slug") or "").strip()
    if not slug:
        return HttpResponseBadRequest("Evento no seleccionado.")

    event = get_object_or_404(Event, slug=slug, active=True)
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    return _build_certificate_response(
        event, full_name, request, email=email, failure_redirect="home"
    )


@xframe_options_exempt
@public_safety_net
def event_page(request, slug):
    event = get_object_or_404(Event, slug=slug, active=True)
    return render(request, "certificados/event_page.html", {"event": event})


@xframe_options_exempt
@public_safety_net
def download_certificate(request, slug):
    if request.method != "POST":
        return HttpResponseBadRequest("Método no permitido.")

    event = get_object_or_404(Event, slug=slug, active=True)
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    return _build_certificate_response(event, full_name, request, email=email)
