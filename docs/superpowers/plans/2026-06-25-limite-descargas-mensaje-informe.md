# Límite de descargas, mensaje editable e informe PDF — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir configurar por evento cuántas veces puede descargar una persona, editar el mensaje de bloqueo desde el panel, y exportar un informe PDF del dashboard.

**Architecture:** Dos campos nuevos en el modelo `Event` (`download_limit`, `duplicate_message`). La lógica de bloqueo en `views.py` pasa de booleano a conteo. Un módulo nuevo `reports.py` reúne las métricas del dashboard y arma el PDF con reportlab; el dashboard y la vista de informe lo comparten.

**Tech Stack:** Django 4.x, reportlab (ya es dependencia), pypdf, SQLite. Tests con `django.test.TestCase`.

## Global Constraints

- Trabajar SIEMPRE en `C:\Users\Juli\plataforma-certificados-2.0` (no en `plataforma-certificados` ni en `Desktop\Proyectos`).
- Las descargas manuales (`DownloadLog.manual=True`) NUNCA cuentan para el límite (comportamiento actual).
- `download_limit = 0` significa SIN límite (nunca bloquea).
- El texto por defecto del bloqueo es la constante existente `DUPLICATE_MESSAGE` en `certificados/views.py` — no cambiar su contenido.
- El motivo del rechazo sigue siendo `reason="duplicate"` (no romper métricas del dashboard ni `RejectedAttempt.REASON_CHOICES`).
- Comandos Django desde la raíz del repo: `python manage.py test certificados`, `python manage.py makemigrations`, `python manage.py migrate`.
- Mensajes de commit en español, una línea de asunto + cuerpo opcional.

---

### Task 1: Campos `download_limit` y `duplicate_message` en `Event` + migración

**Files:**
- Modify: `certificados/models.py:21-36` (clase `Event`)
- Create: `certificados/migrations/0008_event_download_limit_duplicate_message.py` (generada)
- Test: `certificados/tests.py` (clase nueva `EventConfigFieldsTests`)

**Interfaces:**
- Produces: `Event.download_limit` (`PositiveIntegerField`, default `1`); `Event.duplicate_message` (`TextField`, `blank=True`, default `""`).

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `certificados/tests.py`:

```python
class EventConfigFieldsTests(TestCase):
    def test_defaults(self):
        e = Event.objects.create(name="Evento", slug="evento")
        self.assertEqual(e.download_limit, 1)
        self.assertEqual(e.duplicate_message, "")

    def test_can_store_custom_values(self):
        e = Event.objects.create(
            name="Otro", slug="otro", download_limit=3, duplicate_message="Hola"
        )
        e.refresh_from_db()
        self.assertEqual(e.download_limit, 3)
        self.assertEqual(e.duplicate_message, "Hola")
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python manage.py test certificados.tests.EventConfigFieldsTests`
Expected: FAIL con `TypeError` o `AttributeError` (campos inexistentes / migración faltante).

- [ ] **Step 3: Agregar los campos al modelo**

En `certificados/models.py`, dentro de `class Event`, después del campo `info_text` (línea ~33), agregar:

```python
    download_limit = models.PositiveIntegerField(
        default=1,
        help_text="Cuántas descargas públicas puede hacer la misma persona "
                  "antes de bloquearse. 0 = sin límite.",
    )
    duplicate_message = models.TextField(
        blank=True,
        default="",
        help_text="Mensaje que se muestra cuando la persona alcanza el límite "
                  "de descargas. Vacío = texto por defecto.",
    )
```

- [ ] **Step 4: Generar la migración**

Run: `python manage.py makemigrations certificados`
Expected: crea `0008_...` con `AddField` de los dos campos.

- [ ] **Step 5: Correr el test y verificar que pasa**

Run: `python manage.py test certificados.tests.EventConfigFieldsTests`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add certificados/models.py certificados/migrations/0008_*.py certificados/tests.py
git commit -m "feat(model): download_limit y duplicate_message configurables por evento"
```

---

### Task 2: Bloqueo por conteo configurable en `views.py`

**Files:**
- Modify: `certificados/views.py:193-214` (bloque de descargas duplicadas en `_build_certificate_response`)
- Test: `certificados/tests.py` (clase nueva `DownloadLimitTests`)

**Interfaces:**
- Consumes: `Event.download_limit` (Task 1).
- Produces: bloqueo cuando `download_limit != 0 and prior_count >= download_limit`. Sigue redirigiendo con `reason="duplicate"`.

- [ ] **Step 1: Escribir los tests que fallan**

Agregar al final de `certificados/tests.py` (reusa el patrón de `DownloadFlowTests`):

```python
@override_settings(MEDIA_ROOT=MEDIA)
class DownloadLimitTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.event = Event.objects.create(name="Vac", slug="vac", require_email=True)
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug})

    def _download(self):
        return self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})

    def test_limit_two_allows_two_blocks_third(self):
        self.event.download_limit = 2
        self.event.save()
        self.assertEqual(self._download().status_code, 200)
        self.assertEqual(self._download().status_code, 200)
        resp = self._download()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 2)
        self.assertEqual(RejectedAttempt.objects.get().reason, "duplicate")

    def test_limit_zero_never_blocks(self):
        self.event.download_limit = 0
        self.event.save()
        for _ in range(3):
            self.assertEqual(self._download().status_code, 200)
        self.assertEqual(DownloadLog.objects.count(), 3)
        self.assertEqual(RejectedAttempt.objects.count(), 0)

    def test_manual_downloads_do_not_count(self):
        # Una entrega manual previa no debe consumir el cupo público (límite 1).
        DownloadLog.objects.create(
            event=self.event, name_entered="Juan Pérez",
            name_normalized="juan perez", email_normalized="juan@mail.com",
            manual=True,
        )
        self.assertEqual(self._download().status_code, 200)
```

- [ ] **Step 2: Correr los tests y verificar que fallan**

Run: `python manage.py test certificados.tests.DownloadLimitTests`
Expected: FAIL — `test_limit_two_allows_two_blocks_third` bloquea en la 2ª (lógica booleana actual).

- [ ] **Step 3: Reemplazar el booleano por conteo**

En `certificados/views.py`, reemplazar el bloque (líneas ~193-214):

```python
        # Bloqueo de descargas duplicadas: ¿esta persona ya descargó este certificado?
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
            already_downloaded = prior.filter(identity).exists()
        elif email:
            already_downloaded = prior.filter(
                email_normalized=normalize_email(email)
            ).exists()
        else:
            already_downloaded = prior.filter(
                name_normalized=normalize_text(full_name)
            ).exists()
        if already_downloaded:
            return _fail(DUPLICATE_MESSAGE, "duplicate")
```

por:

```python
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
            return _fail(DUPLICATE_MESSAGE, "duplicate")
```

- [ ] **Step 4: Correr la suite de descargas completa**

Run: `python manage.py test certificados.tests.DownloadLimitTests certificados.tests.DownloadFlowTests`
Expected: PASS — incluidos los tests existentes de duplicado (límite default 1 mantiene el bloqueo en la 2ª descarga).

- [ ] **Step 5: Commit**

```bash
git add certificados/views.py certificados/tests.py
git commit -m "feat(descargas): bloqueo por limite configurable en vez de una sola descarga"
```

---

### Task 3: Mensaje de bloqueo editable

**Files:**
- Modify: `certificados/views.py` (la línea `return _fail(DUPLICATE_MESSAGE, "duplicate")` editada en Task 2)
- Test: `certificados/tests.py` (agregar 2 tests a `DownloadLimitTests`)

**Interfaces:**
- Consumes: `Event.duplicate_message` (Task 1), constante `DUPLICATE_MESSAGE`.
- Produces: el usuario ve `event.duplicate_message` si no está vacío, si no el texto por defecto.

- [ ] **Step 1: Escribir los tests que fallan**

Agregar dos métodos a la clase `DownloadLimitTests`:

```python
    def test_custom_message_shown_when_set(self):
        self.event.duplicate_message = "Ya retiraste tu certificado, capo."
        self.event.save()
        self._download()  # consume el cupo (límite default 1)
        follow = self.client.post(
            self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"}, follow=True
        )
        self.assertContains(follow, "Ya retiraste tu certificado, capo.")

    def test_default_message_when_empty(self):
        self.assertEqual(self.event.duplicate_message, "")
        self._download()
        follow = self.client.post(
            self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"}, follow=True
        )
        self.assertContains(follow, "contacto.brisaplus@brisasg.com.ar")
```

- [ ] **Step 2: Correr los tests y verificar que el custom falla**

Run: `python manage.py test certificados.tests.DownloadLimitTests.test_custom_message_shown_when_set`
Expected: FAIL — todavía se muestra el texto fijo.

- [ ] **Step 3: Usar el mensaje del evento**

En `certificados/views.py`, cambiar la línea del bloqueo:

```python
        if event.download_limit and prior_count >= event.download_limit:
            return _fail(DUPLICATE_MESSAGE, "duplicate")
```

por:

```python
        if event.download_limit and prior_count >= event.download_limit:
            return _fail(event.duplicate_message or DUPLICATE_MESSAGE, "duplicate")
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `python manage.py test certificados.tests.DownloadLimitTests`
Expected: PASS (todos).

- [ ] **Step 5: Commit**

```bash
git add certificados/views.py certificados/tests.py
git commit -m "feat(descargas): mensaje de bloqueo editable por evento"
```

---

### Task 4: UI del panel — leer/guardar los dos campos en el form del evento

**Files:**
- Modify: `certificados/panel_views.py:125-166` (`panel_event_form`)
- Modify: `certificados/templates/panel/event_form.html` (agregar inputs)
- Test: `certificados/tests.py` (clase nueva `PanelEventFormTests`)

**Interfaces:**
- Consumes: `Event.download_limit`, `Event.duplicate_message` (Task 1).
- Produces: el POST de `panel_event_edit`/`panel_event_create` guarda ambos campos. Entero inválido o vacío → `download_limit = 1`.

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `certificados/tests.py`:

```python
from django.contrib.auth.models import User

class PanelEventFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("admin", password="x", is_staff=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Evento", slug="evento")

    def _post(self, **extra):
        data = {"name": "Evento", "slug": "evento", "active": "on", "require_email": "on"}
        data.update(extra)
        return self.client.post(
            reverse("panel_event_edit", kwargs={"pk": self.event.pk}), data
        )

    def test_saves_limit_and_message(self):
        self._post(download_limit="3", duplicate_message="Texto custom")
        self.event.refresh_from_db()
        self.assertEqual(self.event.download_limit, 3)
        self.assertEqual(self.event.duplicate_message, "Texto custom")

    def test_blank_limit_defaults_to_one(self):
        self._post(download_limit="")
        self.event.refresh_from_db()
        self.assertEqual(self.event.download_limit, 1)

    def test_invalid_limit_defaults_to_one(self):
        self._post(download_limit="abc")
        self.event.refresh_from_db()
        self.assertEqual(self.event.download_limit, 1)
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python manage.py test certificados.tests.PanelEventFormTests`
Expected: FAIL — el view ignora `download_limit`/`duplicate_message`, `download_limit` queda en 1 pero `test_saves_limit_and_message` falla (esperaba 3 / "Texto custom").

- [ ] **Step 3: Leer y guardar los campos en el view**

En `certificados/panel_views.py`, dentro de `panel_event_form`, después de `info_text = request.POST.get("info_text", "").strip()` (línea ~134), agregar:

```python
        duplicate_message = request.POST.get("duplicate_message", "").strip()
        try:
            download_limit = int(request.POST.get("download_limit", "1"))
            if download_limit < 0:
                download_limit = 1
        except (TypeError, ValueError):
            download_limit = 1
```

Luego, en la rama `if event:` (update), agregar después de `event.info_text = info_text`:

```python
            event.download_limit = download_limit
            event.duplicate_message = duplicate_message
```

Y en la rama `else:` (`Event.objects.create(...)`), agregar los kwargs:

```python
                download_limit=download_limit,
                duplicate_message=duplicate_message,
```

- [ ] **Step 4: Agregar los inputs al template**

En `certificados/templates/panel/event_form.html`, después del `form-group` de `info_text` (línea ~54, antes del `<div style="display:flex;gap:12px;">` de los botones), agregar:

```html
        <div class="form-group">
            <label>Límite de descargas por persona</label>
            <input type="number" name="download_limit" min="0" step="1"
                   value="{{ event.download_limit|default:1 }}">
            <div class="form-help">Cuántas veces puede descargar la misma persona antes de bloquearse. 0 = sin límite.</div>
        </div>

        <div class="form-group">
            <label>Mensaje al alcanzar el límite</label>
            <textarea name="duplicate_message" rows="3" placeholder="Vacío = se usa el mensaje por defecto de Brisa+.">{{ event.duplicate_message|default:'' }}</textarea>
            <div class="form-help">Se muestra cuando la persona ya llegó al límite de descargas. Si lo dejás vacío, se usa el texto por defecto.</div>
        </div>
```

Nota: `{{ event.download_limit|default:1 }}` muestra `1` al crear (event es `None`). Para un evento con límite `0`, `default:1` lo mostraría como `1` — pero `0` es falsy en el filtro `default`. Usar en su lugar:

```html
                   value="{% if event %}{{ event.download_limit }}{% else %}1{% endif %}">
```

(reemplaza el `value="{{ event.download_limit|default:1 }}"` de arriba para que un límite 0 guardado se muestre como 0, no como 1.)

- [ ] **Step 5: Correr el test y verificar que pasa**

Run: `python manage.py test certificados.tests.PanelEventFormTests`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add certificados/panel_views.py certificados/templates/panel/event_form.html certificados/tests.py
git commit -m "feat(panel): editar limite de descargas y mensaje de bloqueo en el form del evento"
```

---

### Task 5: Informe PDF del dashboard

**Files:**
- Create: `certificados/reports.py`
- Modify: `certificados/panel_views.py` (refactor `panel_dashboard` para usar el helper + nueva vista `panel_report_pdf`)
- Modify: `certificados/panel_urls.py:7` (agregar ruta del informe)
- Modify: `certificados/templates/panel/dashboard.html:4` (botón "Exportar informe" en `topbar_actions`)
- Test: `certificados/tests.py` (clase nueva `ReportPdfTests`)

**Interfaces:**
- Consumes: `Event`, `DownloadLog`, `RejectedAttempt`, `Attendee`.
- Produces:
  - `reports.gather_report_data()` → `dict` con claves `totals` (dict: `total_events`, `active_events`, `total_downloads`, `today_downloads`, `manual_downloads`, `rejected_total`, `rejected_today`, `duplicate_total`), `daily` (list de `{"label": str, "count": int}`, 7 entradas), `events` (list de `{"name": str, "downloads": int, "rejected": int, "attendees": int}`).
  - `reports.build_report_pdf(data, generated_at)` → `bytes` (PDF).
  - Vista `panel_report_pdf(request)` → `HttpResponse` `application/pdf`, ruta `panel_report_pdf`.

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `certificados/tests.py`:

```python
@override_settings(MEDIA_ROOT=MEDIA)
class ReportPdfTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user("admin2", password="x", is_staff=True)
        self.client.force_login(self.user)

    def test_report_pdf_empty_db(self):
        resp = self.client.get(reverse("panel_report_pdf"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_report_pdf_with_data(self):
        ev = Event.objects.create(name="Vac", slug="vac")
        Attendee.objects.create(event=ev, full_name="Juan Pérez", email="juan@mail.com")
        DownloadLog.objects.create(event=ev, name_entered="Juan Pérez")
        resp = self.client.get(reverse("panel_report_pdf"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_report_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("panel_report_pdf"))
        self.assertEqual(resp.status_code, 302)  # redirige al login

    def test_gather_report_data_shape(self):
        from certificados.reports import gather_report_data
        ev = Event.objects.create(name="Vac", slug="vac")
        DownloadLog.objects.create(event=ev, name_entered="Juan")
        data = gather_report_data()
        self.assertEqual(data["totals"]["total_downloads"], 1)
        self.assertEqual(len(data["daily"]), 7)
        self.assertEqual(data["events"][0]["name"], "Vac")
        self.assertEqual(data["events"][0]["downloads"], 1)
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python manage.py test certificados.tests.ReportPdfTests`
Expected: FAIL — `NoReverseMatch: 'panel_report_pdf'` y `ModuleNotFoundError: certificados.reports`.

- [ ] **Step 3: Crear `certificados/reports.py`**

```python
from datetime import timedelta

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
from io import BytesIO

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
```

- [ ] **Step 4: Agregar la vista `panel_report_pdf` y refactorizar el dashboard**

En `certificados/panel_views.py`, agregar el import arriba (junto a los demás imports locales, después de `from .attendees_io import ...`):

```python
from .reports import gather_report_data, build_report_pdf
```

Reemplazar el cuerpo de `panel_dashboard` (líneas ~63-111) para reusar el helper, manteniendo `recent_logs` y `latest_event`:

```python
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
```

(El import de `timezone` ya existe en `panel_views.py`; los de `Count`/`TruncDate`/`timedelta` quedan sin uso tras el refactor — eliminarlos sólo si no los usa otra vista. `Count` lo usan `panel_events` y `panel_attendees_all`, así que NO borrarlo; `TruncDate` y `timedelta` quedan huérfanos y se pueden quitar de los imports.)

- [ ] **Step 5: Agregar la ruta**

En `certificados/panel_urls.py`, después de la línea del dashboard (`path("", v.panel_dashboard, name="panel_dashboard"),`), agregar:

```python
    path("informe.pdf", v.panel_report_pdf, name="panel_report_pdf"),
```

- [ ] **Step 6: Agregar el botón al dashboard**

En `certificados/templates/panel/dashboard.html`, después de la línea `{% block page_title %}Dashboard{% endblock %}` (línea ~4), agregar:

```html
{% block topbar_actions %}
<a href="{% url 'panel_report_pdf' %}" class="btn btn-secondary btn-sm">Exportar informe</a>
{% endblock %}
```

- [ ] **Step 7: Correr los tests y verificar que pasan**

Run: `python manage.py test certificados.tests.ReportPdfTests`
Expected: PASS (4 tests).

- [ ] **Step 8: Correr toda la suite**

Run: `python manage.py test certificados`
Expected: PASS (toda la suite, sin regresiones).

- [ ] **Step 9: Commit**

```bash
git add certificados/reports.py certificados/panel_views.py certificados/panel_urls.py certificados/templates/panel/dashboard.html certificados/tests.py
git commit -m "feat(panel): exportar informe PDF del dashboard"
```

---

## Deploy a PythonAnywhere (manual, post-merge)

No es parte del plan de código pero queda anotado: en PA falta deployar este cambio **y** el anterior (0007/valign). Pasos: `git pull` → `python manage.py migrate` (corre 0007 y 0008) → Reload. Renovar "Run until 1 month from today" de paso (free tier expira ~2026-07-11).

## Notas de self-review

- **Cobertura del spec:** límite configurable (Task 1+2), `0`=ilimitado (Task 2), manuales no cuentan (Task 2), mensaje editable con default (Task 1+3), UI en event_form (Task 4), informe PDF con totales+7 días+por evento (Task 5), helper compartido (Task 5), migración 0008 (Task 1). Todo cubierto.
- **Tipos:** `gather_report_data()` / `build_report_pdf(data, generated_at)` usados consistentemente en Task 5. Claves del dict (`totals`, `daily`, `events`) idénticas entre helper, PDF y tests.
- **Sin placeholders:** todo el código está completo.
