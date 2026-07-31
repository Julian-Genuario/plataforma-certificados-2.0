from io import BytesIO

from django.contrib import messages
from django.db.models import Q
from django.http import FileResponse, HttpResponseBadRequest
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


def _find_attendee(event, full_name, email):
    """Return the matching Attendee for this event, or None if not found."""
    name_norm = normalize_text(full_name)
    email_norm = normalize_email(email)
    if not name_norm or not email_norm:
        return None
    exact = event.attendees.filter(
        full_name_normalized=name_norm,
        email_normalized=email_norm,
    ).first()
    if exact:
        return exact
    # Feature 5: mismo email y el nombre registrado contenido en el ingresado
    # (la persona puede sumar nombres: 'emilia fernandez' -> 'maria emilia fernandez').
    entered = set(name_norm.split())
    for att in event.attendees.filter(email_normalized=email_norm):
        reg = set(att.full_name_normalized.split())
        if reg and reg.issubset(entered) and len(entered - reg) <= 2:
            return att
    return None


def _title_case(name):
    """Primera letra de cada palabra en mayuscula, resto en minuscula (respeta acentos)."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in (name or "").split())


def _build_certificate_response(event, full_name, request, manual=False, email="", failure_redirect=None):
    """Validate, log and generate the certificate PDF as a FileResponse.

    Validation rules (skipped when manual=True):
    - If event.require_email is True: email is mandatory.
    - If event has attendees loaded: (name, email) must match a registered
      attendee. Match is case- and accent-insensitive.
    - Otherwise: free download.
    """
    if not full_name:
        return HttpResponseBadRequest("Nombre vacío.")
    if len(full_name) > 200:
        return HttpResponseBadRequest("Nombre demasiado largo (máx 200).")

    def _fail(msg, reason):
        from urllib.parse import urlencode as _urlencode
        _log_rejected(event, request, reason, name=full_name, email=email)
        messages.error(request, msg)
        # Preservar embed + los datos ingresados para repoblar el form.
        _params = {}
        if request.GET.get("embed"):
            _params["embed"] = "1"
        if full_name:
            _params["nombre"] = full_name
        if email:
            _params["email"] = email
        _qs = ("?" + _urlencode(_params)) if _params else ""
        if failure_redirect == "home":
            return redirect(reverse("home") + _qs)
        return redirect(reverse("event_page", kwargs={"slug": event.slug}) + _qs)

    matched_attendee = None

    if not manual:
        has_attendees = event.attendees.exists() and not event.free_download

        if event.require_email and not email:
            return _fail("Ingresar el email.", "missing_email")

        if has_attendees:
            if not event.require_email:
                # Lista cargada pero no se pide email: solo validamos por nombre
                name_norm = normalize_text(full_name)
                matched_attendee = event.attendees.filter(full_name_normalized=name_norm).first()
            else:
                matched_attendee = _find_attendee(event, full_name, email)
            if matched_attendee is None:
                _prev = RejectedAttempt.objects.filter(event=event, reason="not_in_list")
                if email:
                    _prev = _prev.filter(email_entered__iexact=email)
                else:
                    _prev = _prev.filter(name_entered__iexact=full_name)
                if _prev.exists():
                    _msg = (
                        "No encontramos su suscripción activa en Brisa+. Contactarse con "
                        "contacto.brisaplus@brisasg.com.ar para recibir el link de pago y "
                        "completar su activación a Brisa+ para poder bajar el Certificado solicitado."
                    )
                else:
                    _msg = (
                        "No figura en la lista de suscriptores activos de Brisa+. Verificar los "
                        "datos ingresados, asegurar que sean los mismos utilizados para su registro "
                        "en Brisa+ y volver a intentar."
                    )
                return _fail(_msg, "not_in_list")
            # Feature 5/6: se conserva el nombre ingresado (Title Case al generar).

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
        effective_limit = event.download_limit
        if matched_attendee is not None and matched_attendee.download_limit is not None:
            effective_limit = matched_attendee.download_limit
        if effective_limit and prior_count >= effective_limit:
            return _fail(event.duplicate_message or DUPLICATE_MESSAGE, "duplicate")

    # Feature 6: nombre del certificado siempre en Title Case.
    full_name = _title_case(full_name)
    template = get_object_or_404(CertificateTemplate, event=event)

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

    try:
        pdf_bytes = build_pdf_bytes(template, full_name)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    _safe = full_name.replace("/", "-").strip() or "certificado"
    filename = "Certificado " + _safe + ".pdf"
    return FileResponse(BytesIO(pdf_bytes), as_attachment=True, filename=filename)


def server_error(request):
    """handler500: página de error amigable (requiere DEBUG=False)."""
    return render(request, "errors/500.html", status=500)


@xframe_options_exempt
def home_page(request):
    events = Event.objects.filter(active=True).order_by("name")
    return render(request, "certificados/home.html", {"events": events})


@xframe_options_exempt
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
def event_page(request, slug):
    event = get_object_or_404(Event, slug=slug, active=True)
    return render(request, "certificados/event_page.html", {"event": event})


@xframe_options_exempt
def download_certificate(request, slug):
    if request.method != "POST":
        return HttpResponseBadRequest("Método no permitido.")

    event = get_object_or_404(Event, slug=slug, active=True)
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    return _build_certificate_response(event, full_name, request, email=email)
