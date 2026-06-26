from datetime import timedelta
from io import BytesIO

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .models import Event, DownloadLog, RejectedAttempt


def gather_report_data():
    """Reúne las métricas del dashboard en un dict serializable.

    Devuelve {"totals": {...}, "daily": [{"label","count"}], "events": [...]}.
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today_start - timedelta(days=6)

    totals = {
        "total_events": Event.objects.count(),
        "active_events": Event.objects.filter(active=True).count(),
        "total_downloads": DownloadLog.objects.count(),
        "today_downloads": DownloadLog.objects.filter(created_at__gte=today_start).count(),
        "manual_downloads": DownloadLog.objects.filter(manual=True).count(),
        "rejected_total": RejectedAttempt.objects.count(),
        "rejected_today": RejectedAttempt.objects.filter(created_at__gte=today_start).count(),
        "duplicate_total": RejectedAttempt.objects.filter(reason="duplicate").count(),
    }

    daily_qs = (
        DownloadLog.objects
        .filter(created_at__gte=week_ago)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
    )
    daily_map = {str(d["day"]): d["count"] for d in daily_qs}
    daily = []
    for i in range(7):
        day = (week_ago + timedelta(days=i)).date()
        daily.append({"label": day.strftime("%d/%m"), "count": daily_map.get(str(day), 0)})

    events_qs = Event.objects.annotate(
        downloads=Count("downloadlog", distinct=True),
        rejected=Count("rejected_attempts", distinct=True),
        attendees_n=Count("attendees", distinct=True),
    ).order_by("name")
    events = [
        {
            "name": e.name,
            "downloads": e.downloads,
            "rejected": e.rejected,
            "attendees": e.attendees_n,
        }
        for e in events_qs
    ]

    return {"totals": totals, "daily": daily, "events": events}


def build_report_pdf(data, generated_at):
    """Arma el PDF del informe a partir de gather_report_data(). Devuelve bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    normal = styles["Normal"]
    subtle = ParagraphStyle("subtle", parent=normal, textColor=colors.grey, fontSize=9)

    elements = []
    elements.append(Paragraph("Informe de certificados", h1))
    elements.append(Paragraph(
        "Generado el " + generated_at.strftime("%d/%m/%Y %H:%M"), subtle))
    elements.append(Spacer(1, 0.6 * cm))

    t = data["totals"]
    elements.append(Paragraph("Resumen", h2))
    totals_rows = [
        ["Métrica", "Valor"],
        ["Eventos activos", f"{t['active_events']} de {t['total_events']}"],
        ["Descargas totales", str(t["total_downloads"])],
        ["Descargas hoy", str(t["today_downloads"])],
        ["Entregas manuales", str(t["manual_downloads"])],
        ["Intentos rechazados", str(t["rejected_total"])],
        ["Rechazados hoy", str(t["rejected_today"])],
        ["Bloqueos por límite", str(t["duplicate_total"])],
    ]
    totals_table = Table(totals_rows, colWidths=[8 * cm, 6 * cm])
    totals_table.setStyle(_table_style())
    elements.append(totals_table)
    elements.append(Spacer(1, 0.6 * cm))

    elements.append(Paragraph("Últimos 7 días", h2))
    daily_rows = [["Día", "Descargas"]] + [[d["label"], str(d["count"])] for d in data["daily"]]
    daily_table = Table(daily_rows, colWidths=[8 * cm, 6 * cm])
    daily_table.setStyle(_table_style())
    elements.append(daily_table)
    elements.append(Spacer(1, 0.6 * cm))

    elements.append(Paragraph("Por evento", h2))
    if data["events"]:
        ev_rows = [["Evento", "Descargas", "Rechazados", "Inscriptos"]] + [
            [e["name"], str(e["downloads"]), str(e["rejected"]), str(e["attendees"])]
            for e in data["events"]
        ]
        ev_table = Table(ev_rows, colWidths=[7 * cm, 3 * cm, 3 * cm, 3 * cm])
        ev_table.setStyle(_table_style())
        elements.append(ev_table)
    else:
        elements.append(Paragraph("Sin eventos cargados.", normal))

    doc.build(elements)
    return buf.getvalue()


def _table_style():
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])
