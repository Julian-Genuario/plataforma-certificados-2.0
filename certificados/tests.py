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

    def test_badge_shown_on_attendees_page_when_pending(self):
        self.SuspiciousAttendee.objects.create(event=self.event, full_name="Uno1", email="uno@mail.com", reason="x")
        resp = self.client.get(reverse("panel_attendees", kwargs={"event_pk": self.event.pk}))
        self.assertContains(resp, "nombre sospechoso")


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
