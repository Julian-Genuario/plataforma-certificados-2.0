from io import BytesIO

from django.contrib import messages
from django.http import FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics

from .models import (
    Event,
    CertificateTemplate,
    DownloadLog,
    Attendee,
    normalize_text,
    normalize_email,
)


def _get_client_ip(request):
    return request.META.get("REMOTE_ADDR")


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
            font_size = float(template.font_size)
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

            c.drawString(draw_x, y, full_name)
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
    return event.attendees.filter(
        full_name_normalized=name_norm,
        email_normalized=email_norm,
    ).first()


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

    def _fail(msg):
        messages.error(request, msg)
        if failure_redirect == "home":
            return redirect("home")
        return redirect("event_page", slug=event.slug)

    if not manual:
        has_attendees = event.attendees.exists()

        if event.require_email and not email:
            return _fail("Tenés que ingresar tu email.")

        if has_attendees:
            if not event.require_email:
                # Lista cargada pero no se pide email: solo validamos por nombre
                name_norm = normalize_text(full_name)
                match = event.attendees.filter(full_name_normalized=name_norm).first()
            else:
                match = _find_attendee(event, full_name, email)
            if match is None:
                return _fail("No te encontramos en la lista de inscriptos. Verificá los datos.")
            full_name = match.full_name

    template = get_object_or_404(CertificateTemplate, event=event)

    DownloadLog.objects.create(
        event=event,
        name_entered=full_name,
        ip=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        manual=manual,
    )

    try:
        pdf_bytes = build_pdf_bytes(template, full_name)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    filename = f"certificado-{event.slug}.pdf"
    return FileResponse(BytesIO(pdf_bytes), as_attachment=True, filename=filename)


def home_page(request):
    events = Event.objects.filter(active=True).order_by("name")
    return render(request, "certificados/home.html", {"events": events})


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


def event_page(request, slug):
    event = get_object_or_404(Event, slug=slug, active=True)
    return render(request, "certificados/event_page.html", {"event": event})


def download_certificate(request, slug):
    if request.method != "POST":
        return HttpResponseBadRequest("Método no permitido.")

    event = get_object_or_404(Event, slug=slug, active=True)
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    return _build_certificate_response(event, full_name, request, email=email)
