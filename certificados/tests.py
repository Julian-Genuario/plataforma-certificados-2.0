import shutil
import tempfile
from io import BytesIO

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from reportlab.pdfgen import canvas

import io

from .models import Event, CertificateTemplate, DownloadLog, RejectedAttempt, Attendee, normalize_text
from .views import DUPLICATE_MESSAGE, fit_font_size, baseline_offset
from .attendees_io import parse_uploaded_file, parse_text


def _make_pdf_bytes():
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 400))
    c.drawString(100, 200, "plantilla")
    c.save()
    return buf.getvalue()


MEDIA = tempfile.mkdtemp()


def _age_logs(minutes=30):
    """Retro-data todos los DownloadLog para salir de la ventana de gracia
    de re-descarga (REDOWNLOAD_GRACE) y probar el bloqueo por duplicado."""
    from django.utils import timezone
    from datetime import timedelta
    DownloadLog.objects.update(created_at=timezone.now() - timedelta(minutes=minutes))


@override_settings(MEDIA_ROOT=MEDIA)
class DownloadFlowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.event = Event.objects.create(name="Vacunologia", slug="vacuno", require_email=True)
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug})

    def test_first_download_succeeds(self):
        resp = self.client.post(self.url, {"full_name": "juan perez", "email": "JUAN@mail.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Certificado listo")
        log = DownloadLog.objects.get()
        self.assertIsNotNone(log.attendee)
        self.assertEqual(RejectedAttempt.objects.count(), 0)

    def test_duplicate_download_blocked(self):
        self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        _age_logs()
        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        self.assertEqual(resp.status_code, 302)  # redirect con mensaje de error
        self.assertEqual(DownloadLog.objects.count(), 1)  # no se registra segunda descarga
        rej = RejectedAttempt.objects.get()
        self.assertEqual(rej.reason, "duplicate")
        # El mensaje exacto del cronograma se muestra al usuario.
        follow = self.client.post(
            self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"}, follow=True
        )
        self.assertContains(follow, "contacto.brisaplus@brisasg.com.ar")

    def test_duplicate_blocked_after_list_reimport(self):
        # El flujo real de sync: la persona descarga, después se re-sube el
        # export con "Reemplazar lista existente" (borra y recrea inscriptos).
        # La descarga previa tiene que seguir bloqueada.
        self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        self.assertEqual(DownloadLog.objects.count(), 1)
        _age_logs()

        self.event.attendees.all().delete()
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")

        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 1)
        self.assertEqual(RejectedAttempt.objects.get().reason, "duplicate")

    def test_duplicate_blocked_after_reimport_name_only_event(self):
        # Mismo caso pero en un evento sin email requerido (valida solo nombre).
        self.event.require_email = False
        self.event.save()
        self.client.post(self.url, {"full_name": "Juan Pérez"})
        self.assertEqual(DownloadLog.objects.count(), 1)
        _age_logs()

        self.event.attendees.all().delete()
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")

        resp = self.client.post(self.url, {"full_name": "Juan Pérez"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 1)
        self.assertEqual(RejectedAttempt.objects.get().reason, "duplicate")

    def test_not_in_list_rejected(self):
        resp = self.client.post(self.url, {"full_name": "Otro Nombre", "email": "otro@mail.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 0)
        self.assertEqual(RejectedAttempt.objects.get().reason, "not_in_list")

    def test_typo_in_name_still_succeeds_if_email_matches(self):
        # El email es la referencia; un error de tipeo en el nombre no
        # bloquea la descarga.
        resp = self.client.post(self.url, {"full_name": "Juna Peres", "email": "juan@mail.com"})
        self.assertEqual(resp.status_code, 200)
        log = DownloadLog.objects.get()
        self.assertIsNotNone(log.attendee)

    def test_certificate_uses_registered_name_not_typed_name(self):
        # El PDF (y el log) usan el nombre cargado en la lista, no el que
        # escribio la persona con el error de tipeo.
        self.client.post(self.url, {"full_name": "juan peres", "email": "juan@mail.com"})
        log = DownloadLog.objects.get()
        self.assertEqual(log.name_entered, "Juan Pérez")

    def test_correct_name_wrong_email_still_rejected(self):
        # El nombre exacto no alcanza si el email no matchea: el email
        # sigue siendo el dato de referencia.
        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": "otro@mail.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 0)
        self.assertEqual(RejectedAttempt.objects.get().reason, "not_in_list")

    def test_missing_email_rejected(self):
        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(RejectedAttempt.objects.get().reason, "missing_email")

    def test_message_constant_matches_cronograma(self):
        self.assertIn("ya fue descargado", DUPLICATE_MESSAGE)
        self.assertIn("contacto.brisaplus@brisasg.com.ar", DUPLICATE_MESSAGE)


class BrisaplusImportTests(TestCase):
    def _upload(self, content, name):
        f = io.BytesIO(content if isinstance(content, bytes) else content.encode("utf-8"))
        f.name = name
        return f

    def test_brisaplus_html_export_combines_name_and_surname(self):
        html = (
            "<html><body><table>"
            "<tr><th>Nombre</th><th>Apellido</th><th>Email</th><th>Pais</th></tr>"
            "<tr><td>Juan</td><td>Pérez</td><td>juan@mail.com</td><td>Argentina</td></tr>"
            "<tr><td>María</td><td>Gómez</td><td>maria@mail.com</td><td>Chile</td></tr>"
            "<tr><td>Sin</td><td>Mail</td><td>no-es-mail</td><td>X</td></tr>"
            "</table></body></html>"
        )
        clean, errors, skipped = parse_uploaded_file(self._upload(html, "exportado-usuarios.xls"))
        self.assertEqual(clean[0], ("Juan Pérez", "juan@mail.com"))
        self.assertEqual(clean[1], ("María Gómez", "maria@mail.com"))
        self.assertEqual(len(clean), 2)
        self.assertEqual(len(errors), 1)  # email inválido reportado
        self.assertEqual(skipped, 0)

    def test_only_active_subscription_filter(self):
        html = (
            "<html><body><table>"
            "<tr><th>Nombre</th><th>Apellido</th><th>Email</th><th>Activado Suscripción</th></tr>"
            "<tr><td>Ana</td><td>Activa</td><td>ana@mail.com</td><td>SI</td></tr>"
            "<tr><td>Beto</td><td>Inactivo</td><td>beto@mail.com</td><td>NO</td></tr>"
            "<tr><td>Caro</td><td>Activa</td><td>caro@mail.com</td><td>SI</td></tr>"
            "</table></body></html>"
        )
        clean, errors, skipped = parse_uploaded_file(
            self._upload(html, "exp.xls"), only_active=True
        )
        emails = [e for _, e in clean]
        self.assertEqual(emails, ["ana@mail.com", "caro@mail.com"])
        self.assertEqual(skipped, 1)  # Beto omitido por suscripción NO
        # Sin el filtro, entran los tres.
        clean2, _, skipped2 = parse_uploaded_file(self._upload(html, "exp.xls"), only_active=False)
        self.assertEqual(len(clean2), 3)
        self.assertEqual(skipped2, 0)

    def test_xls_that_is_not_html_is_rejected(self):
        from .attendees_io import ParseError
        with self.assertRaises(ParseError):
            parse_uploaded_file(self._upload(b"\xd0\xcf\x11\xe0binary", "viejo.xls"))

    def test_paste_still_works(self):
        clean, errors, skipped = parse_text("Juan Perez, juan@mail.com\nMaria Gomez, maria@mail.com")
        self.assertEqual(len(clean), 2)
        self.assertEqual(errors, [])
        self.assertEqual(skipped, 0)


class SuspiciousNameTests(TestCase):
    def test_normal_name_not_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("Juan Pérez")
        self.assertFalse(is_sus)

    def test_question_marks_are_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("???? ????")
        self.assertTrue(is_sus)
        self.assertIn("encoding", reason)

    def test_digits_in_name_are_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("Juan123 Perez")
        self.assertTrue(is_sus)

    def test_single_letter_is_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("X")
        self.assertTrue(is_sus)

    def test_repeated_word_is_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("test test")
        self.assertTrue(is_sus)

    def test_placeholder_word_is_suspicious(self):
        from .attendees_io import is_suspicious_name
        is_sus, reason = is_suspicious_name("asd qwerty")
        self.assertTrue(is_sus)


class PanelAttendeesImportBulkCreateTests(TestCase):
    """El import usa bulk_create (no Attendee.objects.create en loop) para
    no exceder el timeout del worker con listas grandes. bulk_create no
    llama a save(), asi que hay que verificar que los campos normalizados
    se sigan completando igual."""

    def setUp(self):
        self.user = User.objects.create_user("admin4", password="x", is_staff=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Vacunologia", slug="vacunologia")
        Attendee.objects.create(event=self.event, full_name="Ya Existe", email="ya@existe.com")

    def test_bulk_import_creates_and_normalizes_fields(self):
        pasted = "Juan Pérez, juan@mail.com\nMaría Gómez, maria@mail.com"
        resp = self.client.post(
            reverse("panel_attendees_import", kwargs={"event_pk": self.event.pk}),
            {"pasted_text": pasted},
        )
        self.assertRedirects(resp, reverse("panel_attendees", kwargs={"event_pk": self.event.pk}))
        self.assertEqual(self.event.attendees.count(), 3)
        juan = self.event.attendees.get(email="juan@mail.com")
        self.assertEqual(juan.email_normalized, "juan@mail.com")
        self.assertEqual(juan.full_name_normalized, normalize_text("Juan Pérez"))

    def test_bulk_import_skips_duplicates_against_existing(self):
        pasted = "Ya Existe, ya@existe.com\nNueva Persona, nueva@mail.com"
        self.client.post(
            reverse("panel_attendees_import", kwargs={"event_pk": self.event.pk}),
            {"pasted_text": pasted},
        )
        self.assertEqual(self.event.attendees.count(), 2)


class PanelSuspiciousAttendeesTests(TestCase):
    def setUp(self):
        from .models import SuspiciousAttendee
        self.SuspiciousAttendee = SuspiciousAttendee
        self.user = User.objects.create_user("admin5", password="x", is_staff=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Vacunologia", slug="vacunologia")

    def test_import_routes_suspicious_names_away_from_attendees(self):
        pasted = "Juan Perez, juan@mail.com\n???? ????, raro@mail.com\nAna123, ana@mail.com"
        resp = self.client.post(
            reverse("panel_attendees_import", kwargs={"event_pk": self.event.pk}),
            {"pasted_text": pasted},
        )
        self.assertRedirects(resp, reverse("panel_attendees", kwargs={"event_pk": self.event.pk}))
        self.assertEqual(self.event.attendees.count(), 1)
        self.assertEqual(self.event.attendees.first().email, "juan@mail.com")
        self.assertEqual(self.event.suspicious_attendees.count(), 2)

    def test_approve_creates_attendee_and_removes_from_queue(self):
        item = self.SuspiciousAttendee.objects.create(
            event=self.event, full_name="Raro123", email="raro@mail.com", reason="contiene números"
        )
        resp = self.client.post(
            reverse("panel_suspicious_approve", kwargs={"event_pk": self.event.pk, "pk": item.pk})
        )
        self.assertRedirects(resp, reverse("panel_suspicious_attendees", kwargs={"event_pk": self.event.pk}))
        self.assertEqual(self.event.attendees.count(), 1)
        self.assertEqual(self.event.suspicious_attendees.count(), 0)

    def test_discard_removes_without_creating_attendee(self):
        item = self.SuspiciousAttendee.objects.create(
            event=self.event, full_name="Raro123", email="raro@mail.com", reason="contiene números"
        )
        self.client.post(
            reverse("panel_suspicious_discard", kwargs={"event_pk": self.event.pk, "pk": item.pk})
        )
        self.assertEqual(self.event.attendees.count(), 0)
        self.assertEqual(self.event.suspicious_attendees.count(), 0)

    def test_approve_all_creates_all_and_empties_queue(self):
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Uno1", email="uno@mail.com", reason="x")
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Dos2", email="dos@mail.com", reason="x")
        self.client.post(
            reverse("panel_suspicious_approve_all", kwargs={"event_pk": self.event.pk})
        )
        self.assertEqual(self.event.attendees.count(), 2)
        self.assertEqual(self.event.suspicious_attendees.count(), 0)

    def test_discard_all_empties_queue_without_creating_attendees(self):
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Uno1", email="uno@mail.com", reason="x")
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Dos2", email="dos@mail.com", reason="x")
        self.client.post(
            reverse("panel_suspicious_discard_all", kwargs={"event_pk": self.event.pk})
        )
        self.assertEqual(self.event.attendees.count(), 0)
        self.assertEqual(self.event.suspicious_attendees.count(), 0)

    def test_reimporting_same_broken_row_does_not_duplicate_queue(self):
        pasted = "???? ????, raro@mail.com"
        for _ in range(2):
            self.client.post(
                reverse("panel_attendees_import", kwargs={"event_pk": self.event.pk}),
                {"pasted_text": pasted},
            )
        self.assertEqual(self.event.suspicious_attendees.count(), 1)

    def test_reimporting_broken_row_with_uppercase_email_does_not_500(self):
        # Caso real (Jornada Wellness 21-08): la cola guarda el email crudo
        # ("Gorue70@gmail.com") pero el dedup compara contra el normalizado,
        # no lo detecta como duplicado y el bulk_create viola la constraint
        # unique_event_suspicious_email -> IntegrityError -> 500.
        pasted = "Gloria Isabel 0rue, Gorue70@gmail.com"
        for _ in range(2):
            resp = self.client.post(
                reverse("panel_attendees_import", kwargs={"event_pk": self.event.pk}),
                {"pasted_text": pasted},
            )
            self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.event.suspicious_attendees.count(), 1)

    def test_badge_shown_on_attendees_page_when_pending(self):
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Uno1", email="uno@mail.com", reason="x")
        resp = self.client.get(reverse("panel_attendees", kwargs={"event_pk": self.event.pk}))
        self.assertContains(resp, "nombre sospechoso")


@override_settings(MEDIA_ROOT=MEDIA)
class TemplateUploadTests(TestCase):
    """El panel debe aceptar imágenes como template (convirtiéndolas a PDF)
    y rechazar archivos ilegibles con un mensaje, nunca guardarlos tal cual.
    Caso real 27-08: se subió un JPEG como template y toda descarga tiraba
    500 (pypdf: "invalid pdf header") hasta reemplazar el archivo a mano."""

    def setUp(self):
        self.user = User.objects.create_user("admin7", password="x", is_staff=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Jornada", slug="jornada", require_email=True)

    def _make_jpeg_bytes(self):
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (160, 113), "#0a5c36").save(buf, format="JPEG")
        return buf.getvalue()

    def test_uploading_jpeg_creates_working_pdf_template(self):
        resp = self.client.post(reverse("panel_template_create"), {
            "event": self.event.pk,
            "pdf": SimpleUploadedFile("diseno.jpeg", self._make_jpeg_bytes(), content_type="image/jpeg"),
        })
        self.assertRedirects(resp, reverse("panel_templates"))
        template = CertificateTemplate.objects.get(event=self.event)
        self.assertTrue(template.pdf.name.endswith(".pdf"))
        from pypdf import PdfReader
        reader = PdfReader(template.pdf.path)  # no debe explotar
        self.assertEqual(len(reader.pages), 1)

    def test_editing_with_jpeg_also_converts(self):
        template = CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        resp = self.client.post(reverse("panel_template_edit", kwargs={"pk": template.pk}), {
            "event": self.event.pk,
            "pdf": SimpleUploadedFile("nuevo.jpg", self._make_jpeg_bytes(), content_type="image/jpeg"),
        })
        self.assertRedirects(resp, reverse("panel_templates"))
        template.refresh_from_db()
        self.assertTrue(template.pdf.name.endswith(".pdf"))
        from pypdf import PdfReader
        PdfReader(template.pdf.path)

    def test_unreadable_file_rejected_with_message(self):
        resp = self.client.post(reverse("panel_template_create"), {
            "event": self.event.pk,
            "pdf": SimpleUploadedFile("roto.pdf", b"esto no es un pdf ni una imagen"),
        }, follow=True)
        self.assertEqual(CertificateTemplate.objects.count(), 0)
        self.assertContains(resp, "no se pudo leer")

    def test_download_with_corrupt_template_fails_friendly_not_500(self):
        # Defensa extra: si un template ilegible quedó en la base igual
        # (cargado antes de la validación), la descarga no debe tirar 500.
        template = CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("roto.pdf", b"no soy un pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        resp = self.client.post(
            reverse("download_certificate", kwargs={"slug": self.event.slug}),
            {"full_name": "Juan Pérez", "email": "juan@mail.com"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "certificado no se puede generar")


class PublicSafetyNetTests(TestCase):
    """Ninguna vista pública puede terminar en la pantalla genérica de error:
    un bug imprevisto tiene que loguearse y devolver a la persona al
    formulario con un mensaje de reintento."""

    def setUp(self):
        self.event = Event.objects.create(name="Jornada", slug="jornada", require_email=True)

    def test_unexpected_error_in_download_returns_to_form_with_message(self):
        from unittest.mock import patch
        with patch(
            "certificados.views._build_certificate_response",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(
                reverse("download_certificate", kwargs={"slug": self.event.slug}),
                {"full_name": "Juan", "email": "juan@mail.com"},
                follow=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Volver a intentar")

    def test_unexpected_error_in_home_download_redirects_home(self):
        from unittest.mock import patch
        with patch(
            "certificados.views._build_certificate_response",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(
                reverse("download_from_home"),
                {"event_slug": self.event.slug, "full_name": "Juan", "email": "a@b.com"},
                follow=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Volver a intentar")

    def test_404_passes_through_untouched(self):
        resp = self.client.get(reverse("event_page", kwargs={"slug": "no-existe"}))
        self.assertEqual(resp.status_code, 404)

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")

    def test_healthz_bypasses_maintenance_mode(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        s.mantenimiento = True
        s.save()
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)

    def test_download_works_without_cookies_like_safari_iframe(self):
        # Safari/iPhone en iframe bloquea cookies de terceros: sin cookie de
        # CSRF el POST debe funcionar igual (caso real 27-08: 4 intentos con
        # 403 seguidos). Cliente con chequeo CSRF real y sin cookies.
        from django.test import Client, override_settings
        client = Client(enforce_csrf_checks=True)
        with override_settings(MEDIA_ROOT=MEDIA):
            CertificateTemplate.objects.create(
                event=self.event,
                pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
                mode="coords",
            )
            Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
            resp = client.post(
                reverse("download_certificate", kwargs={"slug": self.event.slug}) + "?embed=1",
                {"full_name": "Juan Pérez", "email": "juan@mail.com"},
            )
        # En embed la respuesta es la pantalla intermedia con el link firmado.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Certificado listo")

    def test_csrf_failure_shows_friendly_page_on_panel_login(self):
        # El panel conserva CSRF: sin token debe verse la página amigable,
        # no el 403 técnico de Django.
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        resp = client.post(reverse("panel_login"), {"username": "x", "password": "y"})
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "No pudimos verificar", status_code=403)


@override_settings(MEDIA_ROOT=MEDIA)
class RedownloadGraceAndEmbedFlowTests(TestCase):
    """Casos del testeo de Brisa (27-08): doble click quemaba el cupo sin
    entregar archivo, y Safari en iframe no descargaba nada."""

    def setUp(self):
        self.event = Event.objects.create(
            name="Congreso", slug="congreso", require_email=True, download_limit=1
        )
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug})
        self.datos = {"full_name": "Juan Pérez", "email": "juan@mail.com"}

    def test_direct_flow_uses_ready_page_and_single_use_link(self):
        # Desde el 09-09 el link directo usa el mismo flujo que el iframe:
        # pantalla "Certificado listo" + link de un solo uso. Tras entregar,
        # un nuevo envío (aun en la gracia) es duplicado.
        import re
        r1 = self.client.post(self.url, self.datos)
        self.assertEqual(r1.status_code, 200)
        self.assertContains(r1, "Certificado listo")
        log = DownloadLog.objects.get(event=self.event)
        self.assertIsNone(log.delivered_at)
        pdf = re.search(r'href="[^"]*(/e/congreso/descargar/[^"]+/)"', r1.content.decode()).group(1)
        self.assertEqual(self.client.get(pdf)["Content-Type"], "application/pdf")
        log.refresh_from_db()
        self.assertIsNotNone(log.delivered_at)
        r2 = self.client.post(self.url, self.datos)
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(DownloadLog.objects.filter(event=self.event).count(), 1)

    def test_after_grace_window_duplicate_blocks_again(self):
        self.client.post(self.url, self.datos)
        log = DownloadLog.objects.get(event=self.event)
        from django.utils import timezone
        from datetime import timedelta
        DownloadLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(minutes=30)
        )
        resp = self.client.post(self.url, self.datos, follow=True)
        self.assertContains(resp, "ya fue descargado")

    def test_embed_post_returns_ready_page_with_signed_link(self):
        resp = self.client.post(self.url + "?embed=1", self.datos)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Certificado listo")
        self.assertContains(resp, "/descargar/")
        # El link firmado de la página baja el PDF de verdad.
        import re
        m = re.search(r'href="[^"]*(/e/congreso/descargar/[^"]+/)"', resp.content.decode())
        self.assertIsNotNone(m)
        pdf_resp = self.client.get(m.group(1))
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
        # Link de UN solo uso: el segundo toque vuelve al form con aviso y
        # no suma logs.
        again = self.client.get(m.group(1), follow=True)
        self.assertEqual(again.status_code, 200)
        self.assertContains(again, "un solo uso")
        self.assertEqual(DownloadLog.objects.filter(event=self.event).count(), 1)

    def test_embed_ready_page_has_image_preview_and_share_button(self):
        resp = self.client.post(self.url + "?embed=1", self.datos)
        self.assertContains(resp, "/imagen/")
        self.assertContains(resp, "Guardar en el teléfono")
        self.assertContains(resp, 'id="share-btn"')

    def test_image_token_returns_jpeg_without_logging(self):
        resp = self.client.post(self.url + "?embed=1", self.datos)
        import re
        m = re.search(r'src="[^"]*(/e/congreso/imagen/[^"]+/)"', resp.content.decode())
        self.assertIsNotNone(m)
        img = self.client.get(m.group(1))
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img["Content-Type"], "image/jpeg")
        self.assertEqual(img.content[:2], bytes([0xFF, 0xD8]))  # magic JPEG
        self.assertGreater(len(img.content), 1000)
        self.client.get(m.group(1))
        self.assertEqual(DownloadLog.objects.filter(event=self.event).count(), 1)

    def test_tampered_image_token_redirects_with_message(self):
        resp = self.client.get(
            reverse("download_image_token", kwargs={"slug": self.event.slug, "token": "basura:invalida"}),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "venció")

    def test_tampered_token_redirects_with_message(self):
        resp = self.client.get(
            reverse("download_token", kwargs={"slug": self.event.slug, "token": "basura:invalida"}),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "venció")

    def test_direct_non_embed_uses_ready_page_without_embed_class(self):
        resp = self.client.post(self.url, self.datos)
        self.assertContains(resp, "Certificado listo")
        self.assertNotContains(resp, 'class="embed"')
        self.assertContains(resp, 'id="done" hidden')


class PublicUrlTests(TestCase):
    def test_event_page_url_has_single_e_prefix(self):
        # La URL pública que se difunde: /e/<slug>/, no /e/e/<slug>/.
        self.assertEqual(reverse("event_page", kwargs={"slug": "vacunologia"}), "/e/vacunologia/")


class FitFontSizeTests(TestCase):
    def test_no_max_width_returns_base(self):
        self.assertEqual(fit_font_size("Juan Perez", "Helvetica", 28, 0), 28)

    def test_short_name_keeps_base(self):
        self.assertEqual(fit_font_size("Ana", "Helvetica", 28, 500), 28)

    def test_long_name_shrinks_to_fit(self):
        from reportlab.pdfbase import pdfmetrics
        name = "María Fernanda Rodríguez Etcheverry de los Santos"
        size = fit_font_size(name, "Helvetica", 40, 200)
        self.assertLess(size, 40)
        self.assertLessEqual(pdfmetrics.stringWidth(name, "Helvetica", size), 200.5)

    def test_never_below_min(self):
        size = fit_font_size("x" * 500, "Helvetica", 40, 10, min_size=6)
        self.assertEqual(size, 6)


class BaselineOffsetTests(TestCase):
    def test_baseline_is_zero(self):
        self.assertEqual(baseline_offset("Helvetica", 28, "baseline"), 0.0)

    def test_unknown_or_empty_falls_back_to_baseline(self):
        self.assertEqual(baseline_offset("Helvetica", 28, ""), 0.0)
        self.assertEqual(baseline_offset("Helvetica", 28, "nonsense"), 0.0)

    def test_top_equals_ascent(self):
        # Helvetica ascent = 718/1000.
        self.assertAlmostEqual(baseline_offset("Helvetica", 100, "top"), 71.8, places=3)

    def test_middle_is_half_of_top(self):
        top = baseline_offset("Helvetica", 80, "top")
        middle = baseline_offset("Helvetica", 80, "middle")
        self.assertAlmostEqual(middle, top / 2.0, places=6)

    def test_scales_linearly_with_font_size(self):
        self.assertAlmostEqual(
            baseline_offset("Helvetica", 40, "top"),
            2 * baseline_offset("Helvetica", 20, "top"),
            places=6,
        )


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
        _age_logs()
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

    def test_custom_message_shown_when_set(self):
        self.event.duplicate_message = "Ya retiraste tu certificado, capo."
        self.event.save()
        self._download()  # consume el cupo (límite default 1)
        _age_logs()
        follow = self.client.post(
            self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"}, follow=True
        )
        self.assertContains(follow, "Ya retiraste tu certificado, capo.")

    def test_default_message_when_empty(self):
        self.assertEqual(self.event.duplicate_message, "")
        self._download()
        _age_logs()
        follow = self.client.post(
            self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"}, follow=True
        )
        self.assertContains(follow, "contacto.brisaplus@brisasg.com.ar")


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

    def test_edit_with_blank_slug_keeps_existing(self):
        # Un slug vacío al editar NO debe regenerarse del nombre:
        # cambiaría la URL y rompería los iframes/links ya difundidos.
        self._post(name="Evento Renombrado", slug="")
        self.event.refresh_from_db()
        self.assertEqual(self.event.slug, "evento")
        self.assertEqual(self.event.name, "Evento Renombrado")

    def test_edit_renaming_keeps_slug_sent_by_form(self):
        # El form de edición manda el slug actual prellenado; renombrar
        # el evento no lo toca.
        self._post(name="Otro Nombre", slug="evento")
        self.event.refresh_from_db()
        self.assertEqual(self.event.slug, "evento")

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


class PanelAttendeesClearTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("admin3", password="x", is_staff=True)
        self.client.force_login(self.user)
        self.event_a = Event.objects.create(name="Evento A", slug="evento-a")
        self.event_b = Event.objects.create(name="Evento B", slug="evento-b")
        Attendee.objects.create(event=self.event_a, full_name="Uno", email="uno@test.com")
        Attendee.objects.create(event=self.event_a, full_name="Dos", email="dos@test.com")
        Attendee.objects.create(event=self.event_b, full_name="Otro", email="otro@test.com")

    def test_clear_only_deletes_target_event(self):
        self.client.post(reverse("panel_attendees_clear", kwargs={"event_pk": self.event_a.pk}))
        self.assertEqual(self.event_a.attendees.count(), 0)
        self.assertEqual(self.event_b.attendees.count(), 1)

    def test_clear_redirects_to_safe_next(self):
        next_url = reverse("panel_attendees_all") + "?event=" + str(self.event_a.pk)
        resp = self.client.post(
            reverse("panel_attendees_clear", kwargs={"event_pk": self.event_a.pk}),
            {"next": next_url},
        )
        self.assertRedirects(resp, next_url)

    def test_clear_ignores_unsafe_next(self):
        resp = self.client.post(
            reverse("panel_attendees_clear", kwargs={"event_pk": self.event_a.pk}),
            {"next": "https://evil.example.com/"},
        )
        self.assertRedirects(resp, reverse("panel_attendees", kwargs={"event_pk": self.event_a.pk}))

    def test_global_list_shows_clear_button_when_filtered(self):
        resp = self.client.get(reverse("panel_attendees_all") + f"?event={self.event_a.pk}")
        self.assertContains(resp, "Limpiar todos (Evento A)")

    def test_global_list_hides_clear_button_without_filter(self):
        resp = self.client.get(reverse("panel_attendees_all"))
        self.assertNotContains(resp, "Limpiar todos (")


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


class SiteSettingsTests(TestCase):
    def test_load_is_singleton(self):
        from .models import SiteSettings
        a = SiteSettings.load()
        b = SiteSettings.load()
        self.assertEqual(a.pk, 1)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(SiteSettings.objects.count(), 1)
        # Guardar una segunda instancia no crea otra fila.
        a.color_fondo = "#000000"
        a.save()
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_defaults(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        self.assertEqual(s.color_fondo, "#ffffff")
        self.assertEqual(s.color_mensaje, "#1d4ed8")
        self.assertFalse(s.mantenimiento)


class MaintenanceModeTests(TestCase):
    def setUp(self):
        Event.objects.create(name="Evento", slug="ev", require_email=False)

    def test_off_by_default_home_ok(self):
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)

    def test_on_blocks_public_with_503(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        s.mantenimiento = True
        s.mensaje_mantenimiento = "Volver en unos minutos."
        s.save()
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 503)
        self.assertContains(resp, "Volver en unos minutos.", status_code=503)

    def test_on_still_allows_panel_login(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        s.mantenimiento = True
        s.save()
        resp = self.client.get(reverse("panel_login"))
        self.assertEqual(resp.status_code, 200)


class PublicAppearanceTests(TestCase):
    def setUp(self):
        Event.objects.create(name="Evento", slug="ev", require_email=False)

    def test_home_uses_background_color(self):
        from .models import SiteSettings
        s = SiteSettings.load()
        s.color_fondo = "#eef2ff"
        s.titulo = "Mis certificados"
        s.save()
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, "#eef2ff")
        self.assertContains(resp, "Mis certificados")


class EmbedTests(TestCase):
    def setUp(self):
        self.event = Event.objects.create(name="Evento", slug="ev", require_email=False)

    def test_event_embed_no_xframe_header(self):
        resp = self.client.get("/e/ev/?embed=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.headers.get("X-Frame-Options"))
        self.assertContains(resp, 'class="embed"')

    def test_home_embed_no_xframe_header(self):
        resp = self.client.get("/?embed=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.headers.get("X-Frame-Options"))

    def test_panel_keeps_xframe_header(self):
        resp = self.client.get(reverse("panel_login"))
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")

    def test_standalone_has_no_embed_class(self):
        resp = self.client.get("/e/ev/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'class="embed"')

    def test_embed_form_action_carries_param(self):
        resp = self.client.get("/e/ev/?embed=1")
        self.assertContains(resp, "?embed=1")


class HeaderlessImportTests(TestCase):
    """Archivos sin fila de encabezado (caso real 03-09: xlsx exportado de
    Excel con Nombre | Apellido | Email y sin títulos → 14.879 'Email inválido')."""

    def _xlsx(self, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return SimpleUploadedFile("sin_encabezado.xlsx", buf.read())

    def test_three_columns_without_header_uses_email_cell(self):
        clean, errors, skipped = parse_uploaded_file(self._xlsx([
            ["Juana Victoria ", "Zuta Chávez ", "juani.zuta@gmail.com"],
            ["Santiago", "Muchut", "santimuchut@live.com"],
        ]))
        self.assertEqual(errors, [])
        self.assertEqual(clean, [
            ("Juana Victoria Zuta Chávez", "juani.zuta@gmail.com"),
            ("Santiago Muchut", "santimuchut@live.com"),
        ])

    def test_email_in_first_column_without_header(self):
        clean, errors, _ = parse_uploaded_file(self._xlsx([
            ["ana@x.com", "Ana", "Pérez"],
        ]))
        self.assertEqual(errors, [])
        self.assertEqual(clean, [("Ana Pérez", "ana@x.com")])

    def test_two_columns_without_header_still_works(self):
        clean, errors, _ = parse_text("Juan Perez, juan@x.com\nmaria@x.com; Maria Gomez")
        self.assertEqual(errors, [])
        self.assertEqual(clean, [("Juan Perez", "juan@x.com"), ("Maria Gomez", "maria@x.com")])

    def test_row_without_any_email_reports_error(self):
        clean, errors, _ = parse_uploaded_file(self._xlsx([
            ["Juana", "Zuta", "sin-arroba"],
        ]))
        self.assertEqual(clean, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "Email inválido")

    def test_real_shape_name_as_email_column_is_not_swallowed(self):
        # Fila corrupta del export real: un email en la columna del nombre y
        # otro en la de email. Debe tomarse el email de la columna email y el
        # resto queda como nombre (después lo filtra la cola de sospechosos
        # o el revisor), nunca descartar la fila en silencio.
        clean, errors, _ = parse_uploaded_file(self._xlsx([
            ["patriciamacaine@gmail.com", "Macaine", "patrymacaine15@gmail.com"],
        ]))
        self.assertEqual(errors, [])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0][1], "patrymacaine15@gmail.com")


class PanelStatsResetTests(TestCase):
    """Botón 'Reiniciar estadísticas' del dashboard."""

    def setUp(self):
        self.admin = User.objects.create_user(
            "superadmin", password="x", is_staff=True, is_superuser=True
        )
        self.event = Event.objects.create(name="Evento", slug="evento")
        self.attendee = Attendee.objects.create(
            event=self.event, full_name="Ana Perez", email="ana@x.com"
        )
        DownloadLog.objects.create(event=self.event, name_entered="Ana Perez")
        DownloadLog.objects.create(event=self.event, name_entered="Juan", manual=True)
        RejectedAttempt.objects.create(
            event=self.event, name_entered="Otro", reason="not_found"
        )
        self.url = reverse("panel_stats_reset")

    def _post(self, confirm="REINICIAR"):
        return self.client.post(self.url, {"confirm": confirm})

    def test_reset_deletes_logs_and_rejections_but_keeps_attendees(self):
        self.client.force_login(self.admin)
        resp = self._post()
        self.assertRedirects(resp, reverse("panel_dashboard"))
        self.assertEqual(DownloadLog.objects.count(), 0)
        self.assertEqual(RejectedAttempt.objects.count(), 0)
        self.assertEqual(Attendee.objects.count(), 1)
        self.assertEqual(Event.objects.count(), 1)

    def test_reset_requires_confirmation_word(self):
        self.client.force_login(self.admin)
        for bad in ("", "reiniciar", "REINICIA", "SI"):
            resp = self._post(confirm=bad)
            self.assertRedirects(resp, reverse("panel_dashboard"))
            self.assertEqual(DownloadLog.objects.count(), 2, bad)
            self.assertEqual(RejectedAttempt.objects.count(), 1, bad)

    def test_reset_requires_superuser(self):
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(DownloadLog.objects.count(), 2)

    def test_reset_requires_login(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("panel_login"), resp["Location"])
        self.assertEqual(DownloadLog.objects.count(), 2)

    def test_reset_rejects_get(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(DownloadLog.objects.count(), 2)

    def test_dashboard_shows_button_only_to_superuser(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("panel_dashboard"))
        self.assertContains(resp, "Reiniciar estadísticas")
        self.assertContains(resp, self.url)

        staff = User.objects.create_user("staff2", password="x", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("panel_dashboard"))
        self.assertNotContains(resp, "Reiniciar estadísticas")

    def test_after_reset_attendee_can_download_again(self):
        # Borrar los logs levanta el bloqueo de duplicados: es el efecto
        # buscado (limpiar pruebas antes del evento), y queda documentado.
        self.client.force_login(self.admin)
        self._post()
        self.assertFalse(
            DownloadLog.objects.filter(attendee=self.attendee).exists()
        )


class PanelCsvExportTests(TestCase):
    """Los exports CSV del panel se generan por streaming; antes reventaban
    en la primera fila (csv.writer sobre BytesIO) y el archivo salia vacio."""

    def setUp(self):
        self.user = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Congreso", slug="congreso", require_email=True)
        DownloadLog.objects.create(event=self.event, name_entered="Mariela Alvarenga", ip="203.0.113.5", user_agent="UA")
        RejectedAttempt.objects.create(
            event=self.event, name_entered="Nadie", email_entered="nadie@x.com", reason="not_in_list"
        )

    def _csv(self, name):
        resp = self.client.get(reverse(name))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        body = b"".join(resp.streaming_content).decode("utf-8")
        return body

    def test_logs_export_has_header_and_rows(self):
        body = self._csv("panel_logs_export")
        lines = [l for l in body.splitlines() if l]
        self.assertEqual(lines[0].split(",")[:3], ["Evento", "Nombre", "Tipo"])
        self.assertEqual(len(lines), 2)
        self.assertIn("Mariela Alvarenga", lines[1])
        self.assertIn("203.0.113.5", lines[1])

    def test_rejected_export_has_header_and_rows(self):
        body = self._csv("panel_rejected_export")
        lines = [l for l in body.splitlines() if l]
        self.assertEqual(lines[0].split(",")[:3], ["Evento", "Nombre", "Email"])
        self.assertEqual(len(lines), 2)
        self.assertIn("nadie@x.com", lines[1])


class ClientIpTests(TestCase):
    """Detras de nginx + socket unix REMOTE_ADDR viene vacio: la IP real
    llega en X-Real-IP (o al final de X-Forwarded-For)."""

    def setUp(self):
        self.event = Event.objects.create(name="Vacunologia", slug="vacuno-ip", require_email=True)
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug})
        self.data = {"full_name": "Juan Pérez", "email": "juan@mail.com"}

    def test_x_real_ip_is_logged(self):
        self.client.post(self.url, self.data, HTTP_X_REAL_IP="203.0.113.9", REMOTE_ADDR="")
        self.assertEqual(DownloadLog.objects.get().ip, "203.0.113.9")

    def test_last_forwarded_for_entry_is_used(self):
        self.client.post(
            self.url, self.data, HTTP_X_FORWARDED_FOR="10.0.0.1, 2800:810::1", REMOTE_ADDR=""
        )
        self.assertEqual(DownloadLog.objects.get().ip, "2800:810::1")

    def test_remote_addr_fallback(self):
        self.client.post(self.url, self.data, REMOTE_ADDR="198.51.100.7")
        self.assertEqual(DownloadLog.objects.get().ip, "198.51.100.7")

    def test_garbage_header_does_not_break_download(self):
        resp = self.client.post(self.url, self.data, HTTP_X_REAL_IP="no-es-una-ip", REMOTE_ADDR="")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(DownloadLog.objects.get().ip)

    def test_rejected_attempt_gets_ip_too(self):
        self.client.post(
            self.url, {"full_name": "Otro", "email": "otro@mail.com"}, HTTP_X_REAL_IP="203.0.113.9", REMOTE_ADDR=""
        )
        self.assertEqual(RejectedAttempt.objects.get().ip, "203.0.113.9")


@override_settings(MEDIA_ROOT=MEDIA)
class DownloadDoneScreenTests(TestCase):
    """La pantalla 'Certificado listo' (flujo embed) trae la pantalla final
    'Felicitaciones, descarga finalizada' con redirección automática a la web
    del organizador a los 15 segundos + botón para ir ya."""

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
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug}) + "?embed=1"

    def test_ready_page_has_done_screen_and_redirect(self):
        from .views import POST_DOWNLOAD_REDIRECT_URL, POST_DOWNLOAD_REDIRECT_SECONDS
        resp = self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Certificado listo")
        self.assertContains(resp, 'id="done" hidden')
        self.assertContains(resp, "Felicitaciones")
        self.assertContains(resp, "Descarga finalizada")
        self.assertContains(resp, 'href="%s" target="_top"' % POST_DOWNLOAD_REDIRECT_URL)
        self.assertContains(resp, '<span id="countdown">%d</span>' % POST_DOWNLOAD_REDIRECT_SECONDS)
        self.assertEqual(POST_DOWNLOAD_REDIRECT_SECONDS, 15)
        self.assertEqual(POST_DOWNLOAD_REDIRECT_URL, "https://www.brisaplus.com")


@override_settings(MEDIA_ROOT=MEDIA)
class SingleUseDownloadTests(TestCase):
    """Regla 08-09-2026: el certificado se entrega UNA sola vez. El link
    firmado del flujo embed muere al primer uso; la ventana de gracia solo
    re-ofrece el certificado si todavía no se entregó."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.event = Event.objects.create(name="Vac", slug="vac", require_email=True, download_limit=1)
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": self.event.slug}) + "?embed=1"
        self.datos = {"full_name": "Juan Pérez", "email": "juan@mail.com"}

    def _links(self, resp):
        import re
        html = resp.content.decode()
        pdf = re.search(r'href="[^"]*(/e/vac/descargar/[^"]+/)"', html).group(1)
        img = re.search(r'src="[^"]*(/e/vac/imagen/[^"]+/)"', html).group(1)
        return pdf, img

    def test_link_serves_once_then_bounces(self):
        pdf, img = self._links(self.client.post(self.url, self.datos))
        log = DownloadLog.objects.get()
        self.assertIsNone(log.delivered_at)
        self.assertEqual(self.client.get(pdf)["Content-Type"], "application/pdf")
        log.refresh_from_db()
        self.assertIsNotNone(log.delivered_at)
        second = self.client.get(pdf)
        self.assertEqual(second.status_code, 302)
        page = self.client.get(second["Location"])
        self.assertContains(page, "un solo uso")
        self.assertEqual(DownloadLog.objects.count(), 1)
        self.assertEqual(RejectedAttempt.objects.filter(reason="duplicate").count(), 1)

    def test_image_preview_keeps_working_after_pdf_used(self):
        pdf, img = self._links(self.client.post(self.url, self.datos))
        self.client.get(pdf)
        self.assertEqual(self.client.get(img).status_code, 200)
        self.assertEqual(self.client.get(img).status_code, 200)

    def test_grace_reoffers_only_while_not_delivered(self):
        first = self.client.post(self.url, self.datos)
        self.assertContains(first, "Certificado listo")
        # Cerró la pantalla sin tocar el link: al re-enviar se le vuelve a
        # ofrecer el mismo certificado (mismo log).
        again = self.client.post(self.url, self.datos)
        self.assertContains(again, "Certificado listo")
        self.assertEqual(DownloadLog.objects.count(), 1)
        pdf, _ = self._links(again)
        self.assertEqual(self.client.get(pdf).status_code, 200)
        # Ya entregado: re-enviar dentro de la gracia es duplicado.
        third = self.client.post(self.url, self.datos)
        self.assertEqual(third.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 1)

    def test_legacy_token_without_log_still_serves(self):
        from django.core import signing
        from .views import DOWNLOAD_TOKEN_SALT
        token = signing.dumps({"e": self.event.pk, "n": "Juan Pérez"}, salt=DOWNLOAD_TOKEN_SALT)
        url = reverse("download_token", kwargs={"slug": "vac", "token": token})
        self.assertEqual(self.client.get(url)["Content-Type"], "application/pdf")

    def test_ready_page_no_longer_promises_reusable_link(self):
        resp = self.client.post(self.url, self.datos)
        self.assertNotContains(resp, "se puede tocar más de una vez")
        self.assertContains(resp, "una sola vez")
        self.assertNotContains(resp, "undo-done")


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
        self.assertIn("{nombre}", s.mail_body)


@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MAIL_RATE_PER_MINUTE=100, MAIL_DAILY_CAP=0)
class MailerTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from django.core import mail
        mail.outbox = []
        self.event = Event.objects.create(name="X Congreso", slug="xc", require_email=True)
        CertificateTemplate.objects.create(
            event=self.event,
            pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"),
            mode="coords",
        )
        self.att = Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")

    def test_enqueue_dedupes_per_attendee(self):
        from .mailer import enqueue_certificate_email
        d1, c1 = enqueue_certificate_email(self.event, self.att, "juan perez")
        d2, c2 = enqueue_certificate_email(self.event, self.att, "otro nombre")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(d1.pk, d2.pk)
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
        self.assertEqual(name, "certificado-xc.pdf")
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        html = [a for a in msg.alternatives if a[1] == "text/html"]
        self.assertEqual(len(html), 1)
        d = EmailDelivery.objects.get()
        self.assertEqual(d.status, "sent")
        self.assertIsNotNone(d.sent_at)
        self.assertEqual(d.attempts, 1)

    def test_failure_schedules_retry_then_fails(self):
        from unittest import mock
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        from django.utils import timezone
        from datetime import timedelta
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        with mock.patch("certificados.mailer._send", side_effect=RuntimeError("smtp caído")):
            r = process_queue()
        d = EmailDelivery.objects.get()
        self.assertEqual(r["retried"], 1)
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.attempts, 1)
        self.assertIn("smtp caído", d.last_error)
        self.assertGreater(d.next_attempt_at, timezone.now() + timedelta(seconds=50))
        with mock.patch("certificados.mailer._send", side_effect=RuntimeError("x")):
            for _ in range(4):
                EmailDelivery.objects.filter(pk=d.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
                process_queue()
        d.refresh_from_db()
        self.assertEqual(d.status, "failed")
        self.assertEqual(d.attempts, 5)

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
        for i in range(3):
            a = Attendee.objects.create(event=self.event, full_name=f"Persona {i}", email=f"p{i}@mail.com")
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

    def test_missing_template_is_retried_with_clear_error(self):
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        CertificateTemplate.objects.all().delete()
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        r = process_queue()
        self.assertEqual(r["retried"], 1)
        self.assertIn("template", EmailDelivery.objects.get().last_error.lower())

    def test_smtp_connection_down_skips_without_burning_attempts(self):
        from unittest import mock
        from .mailer import enqueue_certificate_email, process_queue
        from .models import EmailDelivery
        enqueue_certificate_email(self.event, self.att, "Juan Pérez")
        with mock.patch("django.core.mail.backends.locmem.EmailBackend.open", side_effect=OSError("sin red")):
            r = process_queue()
        d = EmailDelivery.objects.get()
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(d.attempts, 0)
        self.assertEqual(d.status, "pending")
        self.assertIn("sin red", d.last_error)


@override_settings(MEDIA_ROOT=MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", MAIL_DAILY_CAP=0)
class SendCommandTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def test_command_processes_queue(self):
        from io import StringIO
        from django.core.management import call_command
        from .mailer import enqueue_certificate_email
        ev = Event.objects.create(name="Vac", slug="vac")
        CertificateTemplate.objects.create(
            event=ev, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords"
        )
        att = Attendee.objects.create(event=ev, full_name="Ana", email="ana@mail.com")
        enqueue_certificate_email(ev, att, "Ana")
        out = StringIO()
        call_command("send_certificate_emails", stdout=out)
        self.assertIn("sent=1", out.getvalue())

    def test_command_max_limits_batch(self):
        from io import StringIO
        from django.core.management import call_command
        from .mailer import enqueue_certificate_email
        ev = Event.objects.create(name="Vac", slug="vac")
        CertificateTemplate.objects.create(
            event=ev, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords"
        )
        for i in range(3):
            a = Attendee.objects.create(event=ev, full_name=f"P{i}", email=f"p{i}@mail.com")
            enqueue_certificate_email(ev, a, a.full_name)
        out = StringIO()
        call_command("send_certificate_emails", max=2, stdout=out)
        self.assertIn("sent=2", out.getvalue())


@override_settings(MEDIA_ROOT=MEDIA)
class PublicEmailOptInTests(TestCase):
    """Opción A: casilla 'Enviarme también una copia por email' marcada por
    defecto; encola solo para inscriptos verificados, al email de la LISTA."""

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
        self.att = Attendee.objects.create(event=self.event, full_name="Juan Pérez", email="juan@mail.com")
        self.url = reverse("download_certificate", kwargs={"slug": "vac"})

    def test_form_has_checked_checkbox(self):
        resp = self.client.get(reverse("event_page", kwargs={"slug": "vac"}))
        self.assertContains(resp, 'name="send_email"')
        self.assertContains(resp, "Enviarme también una copia por email")
        self.assertContains(resp, 'value="on" checked')
        home = self.client.get(reverse("home"))
        self.assertContains(home, 'name="send_email"')

    def test_checked_enqueues_to_list_email_and_shows_notice(self):
        from .models import EmailDelivery
        resp = self.client.post(
            self.url + "?embed=1", {"full_name": "juan perez", "email": "JUAN@mail.com", "send_email": "on"}
        )
        self.assertContains(resp, "Te enviamos una copia a")
        self.assertContains(resp, "juan@mail.com")
        d = EmailDelivery.objects.get()
        self.assertEqual(d.to_email, "juan@mail.com")
        self.assertEqual(d.full_name, "Juan Pérez")
        self.assertIsNotNone(d.download_log)
        self.assertEqual(d.status, "pending")

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
        self.assertContains(resp, "Te enviamos una copia a")
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

    def test_home_form_enqueues(self):
        from .models import EmailDelivery
        self.client.post(reverse("download_from_home"), {"event_slug": "vac", "full_name": "Juan Pérez", "email": "juan@mail.com", "send_email": "on"})
        self.assertEqual(EmailDelivery.objects.count(), 1)


class PanelMailTests(TestCase):
    def setUp(self):
        from .models import EmailDelivery
        self.user = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.event = Event.objects.create(name="Vac", slug="vac")
        self.att = Attendee.objects.create(event=self.event, full_name="Ana López", email="ana@mail.com")
        self.d = EmailDelivery.objects.create(
            event=self.event, attendee=self.att, to_email="ana@mail.com", full_name="Ana López",
            status="failed", last_error="SMTP 550",
        )

    def test_list_filters_and_shows_error(self):
        resp = self.client.get(reverse("panel_mail") + "?status=failed&search=ana")
        self.assertContains(resp, "Ana López")
        self.assertContains(resp, "SMTP 550")
        self.assertNotContains(self.client.get(reverse("panel_mail") + "?status=sent"), "Ana López")

    def test_resend_creates_new_and_supersedes_old(self):
        from .models import EmailDelivery
        self.client.post(reverse("panel_mail_resend", kwargs={"pk": self.d.pk}))
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, "superseded")
        new = EmailDelivery.objects.exclude(pk=self.d.pk).get()
        self.assertEqual(new.status, "pending")
        self.assertEqual(new.to_email, "ana@mail.com")

    def test_export_csv(self):
        resp = self.client.get(reverse("panel_mail_export"))
        body = b"".join(resp.streaming_content).decode("utf-8")
        self.assertTrue(body.startswith("Evento,Nombre,Email,Estado"))
        self.assertIn("ana@mail.com", body)
        self.assertIn("SMTP 550", body)

    def test_dashboard_shows_mail_tile(self):
        resp = self.client.get(reverse("panel_dashboard"))
        self.assertContains(resp, "Correos enviados")
        self.assertContains(resp, "1 fallido")

    def test_nav_has_link(self):
        self.assertContains(self.client.get(reverse("panel_dashboard")), reverse("panel_mail"))


@override_settings(MEDIA_ROOT=MEDIA)
class SiteSettingsMailTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def _base(self, **extra):
        data = {"color_fondo": "#ffffff", "color_mensaje": "#1d4ed8", "titulo": "T", "mensaje": "M",
                "mail_from_name": "Brisa", "mail_from_email": "certificados@brisa.test",
                "mail_reply_to": "hola@brisa.test", "mail_subject": "Certificado {evento}", "mail_body": "Hola {nombre}"}
        data.update(extra)
        return data

    def test_saves_mail_fields(self):
        from .models import SiteSettings
        self.client.post(reverse("panel_site_settings"), self._base())
        s = SiteSettings.load()
        self.assertEqual(s.mail_from_email, "certificados@brisa.test")
        self.assertEqual(s.mail_reply_to, "hola@brisa.test")
        self.assertEqual(s.mail_subject, "Certificado {evento}")
        self.assertEqual(s.mail_body, "Hola {nombre}")

    def test_invalid_sender_rejected(self):
        from .models import SiteSettings
        resp = self.client.post(reverse("panel_site_settings"), self._base(mail_from_email="no-es-un-mail"), follow=True)
        self.assertContains(resp, "no es válido")
        self.assertEqual(SiteSettings.load().mail_from_email, "")

    def test_empty_body_falls_back_to_default(self):
        from .models import SiteSettings, DEFAULT_MAIL_BODY
        self.client.post(reverse("panel_site_settings"), self._base(mail_body="", mail_subject=""))
        s = SiteSettings.load()
        self.assertEqual(s.mail_body, DEFAULT_MAIL_BODY)
        self.assertEqual(s.mail_subject, "Tu certificado del {evento}")

    def test_page_shows_mail_block(self):
        resp = self.client.get(reverse("panel_site_settings"))
        self.assertContains(resp, 'name="mail_body"')
        self.assertContains(resp, "Enviar correo de prueba")

    def test_test_mail_enqueues_to_given_address(self):
        from .models import EmailDelivery
        ev = Event.objects.create(name="Vac", slug="vac")
        CertificateTemplate.objects.create(
            event=ev, pdf=SimpleUploadedFile("t.pdf", _make_pdf_bytes(), content_type="application/pdf"), mode="coords"
        )
        resp = self.client.post(reverse("panel_mail_test"), {"event": ev.pk, "to": "yo@test.com"}, follow=True)
        d = EmailDelivery.objects.get()
        self.assertEqual(d.to_email, "yo@test.com")
        self.assertEqual(d.full_name, "Nombre de Prueba")
        self.assertContains(resp, "encolado")

    def test_test_mail_rejects_bad_address(self):
        from .models import EmailDelivery
        ev = Event.objects.create(name="Vac", slug="vac")
        resp = self.client.post(reverse("panel_mail_test"), {"event": ev.pk, "to": "nada"}, follow=True)
        self.assertEqual(EmailDelivery.objects.count(), 0)
        self.assertContains(resp, "email válido")
