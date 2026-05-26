import shutil
import tempfile
from io import BytesIO

from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from reportlab.pdfgen import canvas

from .models import Event, CertificateTemplate, DownloadLog, RejectedAttempt, Attendee
from .views import DUPLICATE_MESSAGE


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
