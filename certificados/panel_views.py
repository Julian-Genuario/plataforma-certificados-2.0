import csv
import zipfile
from io import BytesIO

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse, StreamingHttpResponse, FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics

from .models import (
    Event,
    CertificateTemplate,
    DownloadLog,
    RejectedAttempt,
    Attendee,
    SiteSettings,
    normalize_text,
)
from .views import build_pdf_bytes, _build_certificate_response, _get_client_ip, fit_font_size, baseline_offset
from .attendees_io import (
    parse_uploaded_file,
    parse_text,
    ParseError,
)
from .reports import gather_report_data, build_report_pdf


# ── Auth ─────────────────────────────────────

def panel_login(request):
    if request.user.is_authenticated:
        return redirect("panel_dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("panel_dashboard")
        else:
            messages.error(request, "Usuario o contrasena incorrectos.")
    return render(request, "panel/login.html")


@login_required(login_url="panel_login")
def panel_logout_view(request):
    logout(request)
    return redirect("panel_login")


# ── Dashboard ──────────────────────────────────

@login_required(login_url="panel_login")
def panel_dashboard(request):
    data = gather_report_data()
    t = data["totals"]

    recent_logs = DownloadLog.objects.select_related("event").order_by("-created_at")[:10]
    latest_event = Event.objects.order_by("-id").first()

    return render(request, "panel/dashboard.html", {
        "active_page": "dashboard",
        "total_events": t["total_events"],
        "active_events": t["active_events"],
        "total_downloads": t["total_downloads"],
        "today_downloads": t["today_downloads"],
        "chart_labels": [d["label"] for d in data["daily"]],
        "chart_data": [d["count"] for d in data["daily"]],
        "recent_logs": recent_logs,
        "latest_event": latest_event,
        "manual_downloads": t["manual_downloads"],
        "rejected_total": t["rejected_total"],
        "rejected_today": t["rejected_today"],
        "duplicate_total": t["duplicate_total"],
    })


@login_required(login_url="panel_login")
def panel_report_pdf(request):
    data = gather_report_data()
    pdf_bytes = build_report_pdf(data, timezone.now())
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="informe-certificados.pdf"'
    return response


# ── Events ──────────────────────────────────────

@login_required(login_url="panel_login")
def panel_events(request):
    events = Event.objects.annotate(download_count=Count("downloadlog")).order_by("-id")
    return render(request, "panel/events.html", {
        "active_page": "events",
        "events": events,
    })


@login_required(login_url="panel_login")
def panel_event_form(request, pk=None):
    event = get_object_or_404(Event, pk=pk) if pk else None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        if not slug:
            # Al editar, un slug vacío conserva el actual (cambiarlo rompe
            # los links/iframes ya difundidos); solo se genera al crear.
            slug = event.slug if event else slugify(name)
        active = request.POST.get("active") == "on"
        require_email = request.POST.get("require_email") == "on"
        info_text = request.POST.get("info_text", "").strip()
        duplicate_message = request.POST.get("duplicate_message", "").strip()
        try:
            download_limit = int(request.POST.get("download_limit", "1"))
            if download_limit < 0:
                download_limit = 1
        except (TypeError, ValueError):
            download_limit = 1

        if not name:
            messages.error(request, "El nombre es obligatorio.")
            return render(request, "panel/event_form.html", {
                "active_page": "events",
                "event": event,
            })

        if event:
            event.name = name
            event.slug = slug
            event.active = active
            event.require_email = require_email
            event.info_text = info_text
            event.download_limit = download_limit
            event.duplicate_message = duplicate_message
            event.save()
            messages.success(request, "Evento actualizado.")
        else:
            event = Event.objects.create(
                name=name,
                slug=slug,
                active=active,
                require_email=require_email,
                info_text=info_text,
                download_limit=download_limit,
                duplicate_message=duplicate_message,
            )
            messages.success(request, "Evento creado.")

        return redirect("panel_events")

    embed_url = ""
    public_url = ""
    if event:
        public_url = request.build_absolute_uri(
            reverse("event_page", kwargs={"slug": event.slug})
        )
        embed_url = public_url + "?embed=1"

    return render(request, "panel/event_form.html", {
        "active_page": "events",
        "event": event,
        "embed_url": embed_url,
        "public_url": public_url,
    })


@login_required(login_url="panel_login")
def panel_event_toggle(request, pk):
    event = get_object_or_404(Event, pk=pk)
    event.active = not event.active
    event.save()
    state = "activado" if event.active else "desactivado"
    messages.success(request, f"Evento {state}.")
    return redirect("panel_events")


@login_required(login_url="panel_login")
def panel_event_delete(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if request.method == "POST":
        event.delete()
        messages.success(request, "Evento eliminado.")
    return redirect("panel_events")


# ── Templates ──────────────────────────────────

@login_required(login_url="panel_login")
def panel_templates(request):
    templates = CertificateTemplate.objects.select_related("event").order_by("-id")
    events_without_template = Event.objects.exclude(
        id__in=CertificateTemplate.objects.values_list("event_id", flat=True)
    )
    return render(request, "panel/templates.html", {
        "active_page": "templates",
        "templates": templates,
        "events_without_template": events_without_template,
    })


@login_required(login_url="panel_login")
def panel_template_form(request, pk=None):
    template = get_object_or_404(CertificateTemplate, pk=pk) if pk else None

    if request.method == "POST":
        event_id = request.POST.get("event")
        mode = request.POST.get("mode", "coords")
        page_number = int(request.POST.get("page_number") or 0)
        x = float(request.POST.get("x") or 100)
        y = float(request.POST.get("y") or 300)
        font_size = float(request.POST.get("font_size") or 28)
        align = request.POST.get("align", "center")
        valign = request.POST.get("valign", "baseline")
        field_name = request.POST.get("field_name", "full_name")
        max_width = float(request.POST.get("max_width") or 0)

        if template:
            if request.FILES.get("pdf"):
                template.pdf = request.FILES["pdf"]
            template.mode = mode
            template.page_number = page_number
            template.x = x
            template.y = y
            template.font_size = font_size
            template.align = align
            template.valign = valign
            template.field_name = field_name
            template.max_width = max_width
            template.save()
            messages.success(request, "Template actualizado.")
        else:
            if not request.FILES.get("pdf"):
                messages.error(request, "Debes subir un archivo PDF.")
                return redirect("panel_template_create")
            event = get_object_or_404(Event, pk=event_id)
            template = CertificateTemplate.objects.create(
                event=event,
                pdf=request.FILES["pdf"],
                mode=mode,
                page_number=page_number,
                x=x,
                y=y,
                font_size=font_size,
                align=align,
                valign=valign,
                field_name=field_name,
                max_width=max_width,
            )
            messages.success(request, "Template creado.")

        return redirect("panel_templates")

    events_available = Event.objects.exclude(
        id__in=CertificateTemplate.objects.values_list("event_id", flat=True)
    )

    # Formateamos los números como string con punto decimal. Si se pasara el
    # float crudo, Django lo localiza a "421,125" (coma) y el <input type=number>
    # lo rechaza, dejando el campo vacío al editar. "%g" además limpia el ruido
    # de coma flotante (505.34999... -> 505.35).
    def _num(value, default):
        return ("%g" % float(value)) if value else default

    defaults = {
        "tpl_x": _num(template.x, "100") if template else "100",
        "tpl_y": _num(template.y, "300") if template else "300",
        "tpl_font_size": _num(template.font_size, "28") if template else "28",
        "tpl_page_number": int(template.page_number) if template and template.page_number else 0,
        "tpl_max_width": _num(template.max_width, "0") if template else "0",
        "tpl_valign": (template.valign or "baseline") if template else "baseline",
    }

    return render(request, "panel/template_form.html", {
        "active_page": "templates",
        "template": template,
        "events_available": events_available,
        **defaults,
    })


@login_required(login_url="panel_login")
def panel_template_delete(request, pk):
    template = get_object_or_404(CertificateTemplate, pk=pk)
    if request.method == "POST":
        template.delete()
        messages.success(request, "Template eliminado.")
    return redirect("panel_templates")


@login_required(login_url="panel_login")
def panel_template_preview(request, pk):
    """Generate a preview PNG showing name position on a coordinate grid."""
    import traceback

    template = get_object_or_404(CertificateTemplate, pk=pk)
    sample_name = request.GET.get("name", "Juan Perez")
    fmt = request.GET.get("fmt", "png")

    tpl_x = float(template.x or 100)
    tpl_y = float(template.y or 300)
    tpl_align = (template.align or "center").lower()
    tpl_valign = (template.valign or "baseline").lower()
    tpl_page = int(template.page_number or 0)
    # Tamaño ajustado al ancho del renglón (coincide con el PDF final).
    tpl_font_size = fit_font_size(
        sample_name, "Helvetica", template.font_size or 28, getattr(template, "max_width", 0)
    )

    try:
        reader = PdfReader(template.pdf.path)

        if tpl_page >= len(reader.pages):
            return HttpResponse("Pagina invalida", status=400)

        page = reader.pages[tpl_page]
        pdf_w = float(page.mediabox.width)
        pdf_h = float(page.mediabox.height)

        packet = BytesIO()
        c = canvas.Canvas(packet, pagesize=(pdf_w, pdf_h))
        c.setFont("Helvetica", tpl_font_size)

        tw = pdfmetrics.stringWidth(sample_name, "Helvetica", tpl_font_size)
        if tpl_align == "center":
            draw_x = tpl_x - tw / 2.0
        elif tpl_align == "right":
            draw_x = tpl_x - tw
        else:
            draw_x = tpl_x

        draw_y = tpl_y - baseline_offset("Helvetica", tpl_font_size, tpl_valign)
        c.drawString(draw_x, draw_y, sample_name)
        c.save()
        packet.seek(0)

        overlay = PdfReader(packet)
        writer = PdfWriter()
        for i, p in enumerate(reader.pages):
            if i == tpl_page:
                p.merge_page(overlay.pages[0])
            writer.add_page(p)

        if fmt == "pdf":
            out = BytesIO()
            writer.write(out)
            out.seek(0)
            response = HttpResponse(out.read(), content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="preview.pdf"'
            return response

        from PIL import Image, ImageDraw, ImageFont

        scale = 1.5
        img_w = int(pdf_w * scale)
        img_h = int(pdf_h * scale)
        img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        draw.rectangle([0, 0, img_w - 1, img_h - 1], outline=(200, 200, 200), width=2)

        for gx in range(0, int(pdf_w) + 1, 100):
            px = int(gx * scale)
            draw.line([(px, 0), (px, img_h)], fill=(235, 235, 235))
            draw.text((px + 3, 3), str(gx), fill=(170, 170, 170))
        for gy in range(0, int(pdf_h) + 1, 100):
            py = img_h - int(gy * scale)
            draw.line([(0, py), (img_w, py)], fill=(235, 235, 235))
            draw.text((3, py - 14), str(gy), fill=(170, 170, 170))

        cx = int(tpl_x * scale)
        cy = img_h - int(tpl_y * scale)
        draw.line([(cx - 25, cy), (cx + 25, cy)], fill=(239, 68, 68), width=3)
        draw.line([(cx, cy - 25), (cx, cy + 25)], fill=(239, 68, 68), width=3)
        draw.ellipse([(cx - 6, cy - 6), (cx + 6, cy + 6)], outline=(239, 68, 68), width=2)

        pil_font_size = max(10, int(tpl_font_size * scale * 0.7))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", pil_font_size)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", pil_font_size)
            except (OSError, IOError):
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), sample_name, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        if tpl_align == "center":
            tx = cx - text_w // 2
        elif tpl_align == "right":
            tx = cx - text_w
        else:
            tx = cx

        # Anclaje vertical en pixeles (cy = punto Y). top -> texto cuelga debajo,
        # baseline -> texto apoyado sobre la linea, igual sentido que el PDF real.
        if tpl_valign == "top":
            text_top = cy
        elif tpl_valign == "middle":
            text_top = cy - text_h // 2
        else:
            text_top = cy - text_h
        draw.text((tx, text_top), sample_name, fill=(30, 60, 90), font=font)

        info = f"PDF: {int(pdf_w)}x{int(pdf_h)}pt  |  X={tpl_x}  Y={tpl_y}  |  Fuente: {tpl_font_size}pt  |  Alin: {tpl_align}/{tpl_valign}"
        draw.rectangle([(0, img_h - 28), (img_w, img_h)], fill=(245, 245, 245))
        draw.text((10, img_h - 22), info, fill=(120, 120, 120))

        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return HttpResponse(out.read(), content_type="image/png")

    except Exception:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (600, 200), (255, 240, 240))
        draw = ImageDraw.Draw(img)
        error_text = traceback.format_exc()
        lines = error_text.strip().split("\n")
        y_pos = 10
        for line in lines[-5:]:
            draw.text((10, y_pos), line[:80], fill=(180, 0, 0))
            y_pos += 20
        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return HttpResponse(out.read(), content_type="image/png")


# ── Manual generation ──────────────────────────────

@login_required(login_url="panel_login")
def panel_generate(request):
    """Page with two forms: individual and bulk. POST = individual generation."""
    events = Event.objects.order_by("name")

    if request.method == "POST":
        event_id = request.POST.get("event")
        full_name = (request.POST.get("full_name") or "").strip()
        event = get_object_or_404(Event, pk=event_id)
        return _build_certificate_response(event, full_name, request, manual=True)

    return render(request, "panel/generate.html", {
        "active_page": "generate",
        "events": events,
    })


@login_required(login_url="panel_login")
def panel_generate_bulk(request):
    """POST only: generate a ZIP of certificates for a list of names."""
    if request.method != "POST":
        return redirect("panel_generate")

    event_id = request.POST.get("event")
    event = get_object_or_404(Event, pk=event_id)
    template = get_object_or_404(CertificateTemplate, event=event)

    names_raw = request.POST.get("names", "")
    uploaded = request.FILES.get("names_file")

    raw_lines = []
    if uploaded:
        try:
            content = uploaded.read().decode("utf-8", errors="ignore")
        except Exception:
            content = ""
        raw_lines.extend(content.splitlines())
    if names_raw:
        raw_lines.extend(names_raw.splitlines())

    names = []
    for raw in raw_lines:
        n = raw.strip().strip(",").strip()
        if not n:
            continue
        if len(n) > 80:
            continue
        names.append(n)

    if not names:
        messages.error(request, "No se encontraron nombres válidos.")
        return redirect("panel_generate")

    MAX_NAMES = 500
    if len(names) > MAX_NAMES:
        messages.error(request, f"Máximo {MAX_NAMES} nombres por lote. Recibiste {len(names)}.")
        return redirect("panel_generate")

    zip_buffer = BytesIO()
    seen = {}
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    client_ip = _get_client_ip(request)

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            try:
                pdf_bytes = build_pdf_bytes(template, name)
            except ValueError:
                continue

            DownloadLog.objects.create(
                event=event,
                name_entered=name,
                name_normalized=normalize_text(name),
                ip=client_ip,
                user_agent=user_agent,
                manual=True,
            )

            base = slugify(name) or "certificado"
            seen[base] = seen.get(base, 0) + 1
            filename = f"{base}.pdf" if seen[base] == 1 else f"{base}-{seen[base]}.pdf"
            zf.writestr(filename, pdf_bytes)

    zip_buffer.seek(0)
    response = HttpResponse(zip_buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="certificados-{event.slug}.zip"'
    return response


# ── Logs ────────────────────────────────────────

@login_required(login_url="panel_login")
def panel_logs(request):
    logs = DownloadLog.objects.select_related("event").order_by("-created_at")

    event_filter = request.GET.get("event")
    search = request.GET.get("search", "").strip()
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    tipo_filter = request.GET.get("tipo", "")

    if event_filter:
        logs = logs.filter(event_id=event_filter)
    if search:
        logs = logs.filter(name_entered__icontains=search)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    if tipo_filter == "manual":
        logs = logs.filter(manual=True)
    elif tipo_filter == "publico":
        logs = logs.filter(manual=False)

    events = Event.objects.order_by("name")

    page = int(request.GET.get("page", 1))
    per_page = 25
    total = logs.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    logs_page = logs[(page - 1) * per_page : page * per_page]

    return render(request, "panel/logs.html", {
        "active_page": "logs",
        "logs": logs_page,
        "events": events,
        "event_filter": event_filter,
        "search": search,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "tipo_filter": tipo_filter,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@login_required(login_url="panel_login")
def panel_logs_export(request):
    logs = DownloadLog.objects.select_related("event").order_by("-created_at")

    event_filter = request.GET.get("event")
    search = request.GET.get("search", "").strip()
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    tipo_filter = request.GET.get("tipo", "")

    if event_filter:
        logs = logs.filter(event_id=event_filter)
    if search:
        logs = logs.filter(name_entered__icontains=search)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    if tipo_filter == "manual":
        logs = logs.filter(manual=True)
    elif tipo_filter == "publico":
        logs = logs.filter(manual=False)

    def generate():
        row_buffer = BytesIO()
        writer = csv.writer(row_buffer, dialect="excel")
        writer.writerow(["Evento", "Nombre", "Tipo", "Fecha", "IP", "User Agent"])
        yield row_buffer.getvalue().decode("utf-8")
        row_buffer.seek(0)
        row_buffer.truncate()

        for log in logs.iterator():
            writer.writerow([
                log.event.name,
                log.name_entered,
                "Manual" if log.manual else "Publico",
                log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                log.ip or "",
                log.user_agent,
            ])
            yield row_buffer.getvalue().decode("utf-8")
            row_buffer.seek(0)
            row_buffer.truncate()

    response = StreamingHttpResponse(generate(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="descargas.csv"'
    return response


# ── Intentos rechazados ─────────────────────────

def _filter_rejected(request):
    qs = RejectedAttempt.objects.select_related("event").order_by("-created_at")
    event_filter = request.GET.get("event")
    search = request.GET.get("search", "").strip()
    reason_filter = request.GET.get("reason", "")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")

    if event_filter:
        qs = qs.filter(event_id=event_filter)
    if search:
        qs = qs.filter(Q(name_entered__icontains=search) | Q(email_entered__icontains=search))
    if reason_filter:
        qs = qs.filter(reason=reason_filter)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs


@login_required(login_url="panel_login")
def panel_rejected(request):
    qs = _filter_rejected(request)
    events = Event.objects.order_by("name")

    page = int(request.GET.get("page", 1) or 1)
    per_page = 25
    total = qs.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    rows = qs[(page - 1) * per_page : page * per_page]

    return render(request, "panel/rejected.html", {
        "active_page": "rejected",
        "rejected": rows,
        "events": events,
        "reason_choices": RejectedAttempt.REASON_CHOICES,
        "event_filter": request.GET.get("event", ""),
        "search": request.GET.get("search", "").strip(),
        "reason_filter": request.GET.get("reason", ""),
        "date_from": request.GET.get("date_from") or "",
        "date_to": request.GET.get("date_to") or "",
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@login_required(login_url="panel_login")
def panel_rejected_export(request):
    qs = _filter_rejected(request)
    reason_labels = dict(RejectedAttempt.REASON_CHOICES)

    def generate():
        row_buffer = BytesIO()
        writer = csv.writer(row_buffer, dialect="excel")
        writer.writerow(["Evento", "Nombre", "Email", "Motivo", "Fecha", "IP"])
        yield row_buffer.getvalue().decode("utf-8")
        row_buffer.seek(0)
        row_buffer.truncate()

        for r in qs.iterator():
            writer.writerow([
                r.event.name,
                r.name_entered,
                r.email_entered,
                reason_labels.get(r.reason, r.reason),
                r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                r.ip or "",
            ])
            yield row_buffer.getvalue().decode("utf-8")
            row_buffer.seek(0)
            row_buffer.truncate()

    response = StreamingHttpResponse(generate(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="intentos-rechazados.csv"'
    return response


# ── Users ───────────────────────────────────────

@login_required(login_url="panel_login")
def panel_users(request):
    users = User.objects.filter(is_staff=True).order_by("-date_joined")
    return render(request, "panel/users.html", {
        "active_page": "users",
        "users": users,
    })


@login_required(login_url="panel_login")
def panel_user_form(request, pk=None):
    user_obj = get_object_or_404(User, pk=pk) if pk else None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "").strip()
        is_superuser = request.POST.get("is_superuser") == "on"

        if not username:
            messages.error(request, "El usuario es obligatorio.")
            return render(request, "panel/user_form.html", {
                "active_page": "users",
                "user_obj": user_obj,
            })

        if user_obj:
            user_obj.username = username
            user_obj.email = email
            user_obj.is_superuser = is_superuser
            if password:
                user_obj.set_password(password)
            user_obj.save()
            messages.success(request, "Usuario actualizado.")
        else:
            if not password:
                messages.error(request, "La contrasena es obligatoria para nuevos usuarios.")
                return render(request, "panel/user_form.html", {
                    "active_page": "users",
                    "user_obj": user_obj,
                })
            if User.objects.filter(username=username).exists():
                messages.error(request, "Ese nombre de usuario ya existe.")
                return render(request, "panel/user_form.html", {
                    "active_page": "users",
                    "user_obj": user_obj,
                })
            user_obj = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                is_staff=True,
                is_superuser=is_superuser,
            )
            messages.success(request, "Usuario creado.")

        return redirect("panel_users")

    return render(request, "panel/user_form.html", {
        "active_page": "users",
        "user_obj": user_obj,
    })


@login_required(login_url="panel_login")
def panel_user_delete(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        if user_obj == request.user:
            messages.error(request, "No podes eliminar tu propio usuario.")
        else:
            user_obj.delete()
            messages.success(request, "Usuario eliminado.")
    return redirect("panel_users")


# ── Apariencia / Configuración del sitio ────────

def _valid_hex(value, fallback):
    """Devuelve un color hex válido (#rrggbb) o el fallback."""
    value = (value or "").strip()
    if len(value) == 7 and value[0] == "#":
        try:
            int(value[1:], 16)
            return value.lower()
        except ValueError:
            pass
    return fallback


@login_required(login_url="panel_login")
def panel_site_settings(request):
    site = SiteSettings.load()

    if request.method == "POST":
        site.color_fondo = _valid_hex(request.POST.get("color_fondo"), "#ffffff")
        site.color_mensaje = _valid_hex(request.POST.get("color_mensaje"), "#1d4ed8")
        site.titulo = (request.POST.get("titulo") or "").strip() or "Descargar certificado"
        site.mensaje = (request.POST.get("mensaje") or "").strip()
        site.mantenimiento = request.POST.get("mantenimiento") == "on"
        site.mensaje_mantenimiento = (request.POST.get("mensaje_mantenimiento") or "").strip()
        site.save()
        messages.success(request, "Configuración guardada.")
        return redirect("panel_site_settings")

    home_public_url = request.build_absolute_uri(reverse("home"))
    home_embed_url = home_public_url + "?embed=1"

    return render(request, "panel/site_settings.html", {
        "active_page": "apariencia",
        "site": site,
        "home_embed_url": home_embed_url,
        "home_public_url": home_public_url,
    })


# ── Attendees ───────────────────────────────────

@login_required(login_url="panel_login")
def panel_attendees(request, event_pk):
    event = get_object_or_404(Event, pk=event_pk)
    search = (request.GET.get("search") or "").strip()
    attendees = event.attendees.all()
    if search:
        attendees = attendees.filter(
            Q(full_name__icontains=search) | Q(email__icontains=search)
        )
    total = attendees.count()
    return render(request, "panel/attendees.html", {
        "active_page": "attendees",
        "event": event,
        "attendees": attendees[:500],
        "search": search,
        "total": total,
        "truncated": total > 500,
    })


def _do_attendees_import(request, event):
    """Process POST data and import attendees for the given event.

    Returns (created, duplicated, errors, skipped_inactive). Caller is
    responsible for showing messages and redirecting.
    """
    from .models import normalize_email

    uploaded = request.FILES.get("file")
    pasted = request.POST.get("pasted_text", "")
    replace_existing = request.POST.get("replace_existing") == "on"
    # Por defecto importamos solo suscripciones activas (export de brisaplus).
    only_active = request.POST.get("only_active") == "on"

    clean = []
    errors = []
    skipped_inactive = 0
    if uploaded:
        file_clean, file_errors, file_skipped = parse_uploaded_file(uploaded, only_active=only_active)
        clean.extend(file_clean)
        errors.extend(file_errors)
        skipped_inactive += file_skipped
    if pasted.strip():
        text_clean, text_errors, text_skipped = parse_text(pasted, only_active=only_active)
        clean.extend(text_clean)
        errors.extend(text_errors)
        skipped_inactive += text_skipped

    if replace_existing:
        event.attendees.all().delete()

    existing_emails = set(event.attendees.values_list("email_normalized", flat=True))

    created = 0
    duplicated = 0
    for name, email in clean:
        email_norm = normalize_email(email)
        if email_norm in existing_emails:
            duplicated += 1
            continue
        existing_emails.add(email_norm)
        Attendee.objects.create(event=event, full_name=name, email=email)
        created += 1

    return created, duplicated, errors, skipped_inactive


def _summarize_import(request, event, created, duplicated, errors, skipped_inactive=0):
    msg = f"{created} inscriptos importados."
    if duplicated:
        msg += f" {duplicated} duplicados omitidos."
    if skipped_inactive:
        msg += f" {skipped_inactive} sin suscripción activa omitidos."
    if errors:
        msg += f" {len(errors)} filas con errores."
    messages.success(request, msg)
    if errors:
        request.session[f"attendee_errors_{event.pk}"] = errors[:200]


@login_required(login_url="panel_login")
def panel_attendees_import(request, event_pk):
    event = get_object_or_404(Event, pk=event_pk)

    if request.method == "POST":
        try:
            created, duplicated, errors, skipped_inactive = _do_attendees_import(request, event)
        except ParseError as exc:
            messages.error(request, str(exc))
            return redirect("panel_attendees_import", event_pk=event.pk)

        if not created and not duplicated and not errors and not skipped_inactive:
            messages.error(request, "No se cargaron datos. Subí un archivo o pegá la lista.")
            return redirect("panel_attendees_import", event_pk=event.pk)

        _summarize_import(request, event, created, duplicated, errors, skipped_inactive)
        return redirect("panel_attendees", event_pk=event.pk)

    session_errors = request.session.pop(f"attendee_errors_{event.pk}", None)
    return render(request, "panel/attendees_import.html", {
        "active_page": "attendees",
        "event": event,
        "errors": session_errors,
    })


@login_required(login_url="panel_login")
def panel_attendee_delete(request, event_pk, pk):
    event = get_object_or_404(Event, pk=event_pk)
    attendee = get_object_or_404(Attendee, pk=pk, event=event)
    if request.method == "POST":
        attendee.delete()
        messages.success(request, "Inscripto eliminado.")
    return redirect("panel_attendees", event_pk=event.pk)


@login_required(login_url="panel_login")
def panel_attendees_clear(request, event_pk):
    event = get_object_or_404(Event, pk=event_pk)
    next_url = request.POST.get("next", "")
    if request.method == "POST":
        count = event.attendees.count()
        event.attendees.all().delete()
        messages.success(request, f"{count} inscriptos eliminados.")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("panel_attendees", event_pk=event.pk)


@login_required(login_url="panel_login")
def panel_attendees_all(request):
    """Global view: all attendees across events with filter by event and search."""
    event_filter = request.GET.get("event") or ""
    search = (request.GET.get("search") or "").strip()

    qs = Attendee.objects.select_related("event").order_by("-created_at")
    if event_filter:
        qs = qs.filter(event_id=event_filter)
    if search:
        qs = qs.filter(
            Q(full_name__icontains=search) | Q(email__icontains=search)
        )

    total = qs.count()
    page = int(request.GET.get("page", 1) or 1)
    per_page = 50
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    attendees = qs[(page - 1) * per_page : page * per_page]

    events = Event.objects.annotate(
        attendee_count=Count("attendees"),
    ).order_by("name")

    selected_event = None
    if event_filter:
        selected_event = next((ev for ev in events if str(ev.pk) == event_filter), None)

    return render(request, "panel/attendees_all.html", {
        "active_page": "attendees",
        "attendees": attendees,
        "events": events,
        "event_filter": event_filter,
        "selected_event": selected_event,
        "search": search,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@login_required(login_url="panel_login")
def panel_attendees_import_all(request):
    """Global import: pick an event from a dropdown and upload."""
    events = Event.objects.order_by("name")

    if request.method == "POST":
        event_id = request.POST.get("event")
        if not event_id:
            messages.error(request, "Elegí un evento.")
            return redirect("panel_attendees_import_all")
        event = get_object_or_404(Event, pk=event_id)

        try:
            created, duplicated, errors, skipped_inactive = _do_attendees_import(request, event)
        except ParseError as exc:
            messages.error(request, str(exc))
            return redirect("panel_attendees_import_all")

        if not created and not duplicated and not errors and not skipped_inactive:
            messages.error(request, "No se cargaron datos. Subí un archivo o pegá la lista.")
            return redirect("panel_attendees_import_all")

        _summarize_import(request, event, created, duplicated, errors, skipped_inactive)
        return redirect("panel_attendees", event_pk=event.pk)

    return render(request, "panel/attendees_import_all.html", {
        "active_page": "attendees",
        "events": events,
    })
