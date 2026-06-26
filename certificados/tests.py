import shutil
import tempfile
from io import BytesIO

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from reportlab.pdfgen import canvas

import io

from .models import Event, CertificateTemplate, DownloadLog, RejectedAttempt, Attendee
from .views import DUPLICATE_MESSAGE, fit_font_size, baseline_offset
from .attendees_io import parse_uploaded_file, parse_text


def _make_pdf_bytes():
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 400))
    c.drawString(100, 200, "plantilla")
    c.save()
    return buf.getvalue()


MEDIA = tempfile.mkdtemp()


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
        self.assertEqual(resp["Content-Type"], "application/pdf")
        log = DownloadLog.objects.get()
        self.assertIsNotNone(log.attendee)
        self.assertEqual(RejectedAttempt.objects.count(), 0)

    def test_duplicate_download_blocked(self):
        self.client.post(self.url, {"full_name": "Juan Pérez", "email": "juan@mail.com"})
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
