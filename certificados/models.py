import unicodedata

from django.db import models
from django.utils import timezone


def normalize_text(value):
    """Lowercase, strip accents (NFKD) and collapse internal whitespace."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def normalize_email(value):
    if not value:
        return ""
    return str(value).strip().lower()


DEFAULT_MAIL_BODY = (
    "Hola {nombre},\n\n"
    "Adjuntamos tu certificado de participación en el {evento}.\n\n"
    "Guardalo en tu dispositivo. Ante cualquier consulta, responder este correo.\n\n"
    "Brisa Enfermeros"
)

DEFAULT_MAINTENANCE_MESSAGE = (
    "Estamos recibiendo muchas visitas en este momento. "
    "Volver a intentar en unos minutos."
)


class SiteSettings(models.Model):
    """Configuración global del sitio (apariencia + mantenimiento).

    Es un singleton: siempre existe una sola fila (pk=1). Usar
    ``SiteSettings.load()`` para obtenerla/crearla.
    """

    color_fondo = models.CharField(
        max_length=7,
        default="#ffffff",
        help_text="Color de fondo de las páginas públicas (hex, ej: #ffffff).",
    )
    color_mensaje = models.CharField(
        max_length=7,
        default="#1d4ed8",
        help_text="Color del título y el mensaje principal (hex, ej: #1d4ed8).",
    )
    titulo = models.CharField(
        max_length=120,
        default="Descargar certificado",
        help_text="Título que se muestra en la página de inicio.",
    )
    mensaje = models.TextField(
        blank=True,
        default="Seleccionar el evento e ingresar el nombre completo y el email "
                "para descargar el certificado de participación.",
        help_text="Mensaje/instrucción debajo del título en la página de inicio.",
    )
    mantenimiento = models.BooleanField(
        default=False,
        help_text="Si está activado, el sitio público muestra el mensaje de "
                  "mantenimiento. El panel sigue accesible.",
    )
    mensaje_mantenimiento = models.TextField(
        blank=True,
        default=DEFAULT_MAINTENANCE_MESSAGE,
        help_text="Mensaje que se muestra cuando el modo mantenimiento está activo.",
    )

    # --- Correo con el certificado adjunto (Opción A) ---
    mail_from_name = models.CharField(
        max_length=120, default="Brisa Enfermeros",
        help_text="Nombre visible del remitente.",
    )
    mail_from_email = models.EmailField(
        blank=True, default="",
        help_text="Casilla remitente. Vacío = la casilla configurada en el servidor (EMAIL_HOST_USER).",
    )
    mail_reply_to = models.EmailField(
        blank=True, default="",
        help_text="A dónde llegan las respuestas. Vacío = el remitente.",
    )
    mail_subject = models.CharField(
        max_length=200, default="Tu certificado del {evento}",
        help_text="Asunto. Variables: {nombre}, {evento}.",
    )
    mail_body = models.TextField(
        default=DEFAULT_MAIL_BODY,
        help_text="Texto del correo (texto plano). Variables: {nombre}, {evento}.",
    )

    class Meta:
        verbose_name = "Configuración del sitio"
        verbose_name_plural = "Configuración del sitio"

    def __str__(self):
        return "Configuración del sitio"

    def save(self, *args, **kwargs):
        # Forzar singleton: siempre pk=1.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Event(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    active = models.BooleanField(default=True)
    require_email = models.BooleanField(
        default=True,
        help_text="Si está activado, el form pide email obligatorio y, si hay inscriptos cargados, valida nombre+email contra la lista.",
    )
    info_text = models.TextField(
        blank=True,
        default="",
        help_text="Texto informativo que se muestra en la página pública del evento, debajo del email.",
    )
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
    free_download = models.BooleanField(
        default=False,
        help_text="Si está activado, cualquier persona puede descargar aunque "
                  "no esté en la lista de inscriptos (descarga libre).",
    )

    def __str__(self):
        return self.name


class CertificateTemplate(models.Model):
    MODE_CHOICES = [
        ("coords", "Escribir por coordenadas"),
        ("field", "Campo rellenable (PDF Form Field)"),
    ]

    event = models.OneToOneField(Event, on_delete=models.CASCADE)
    pdf = models.FileField(upload_to="templates/")
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default="coords")

    # Config para coords
    page_number = models.PositiveIntegerField(default=0)
    x = models.FloatField(default=100)
    y = models.FloatField(default=300)
    font_size = models.FloatField(default=28)
    align = models.CharField(max_length=10, default="center")  # left/center/right
    # Anclaje vertical: qué parte del texto cae sobre el punto Y.
    #   baseline = base de las letras (comportamiento histórico)
    #   middle   = centro vertical del texto
    #   top      = tope del texto
    valign = models.CharField(max_length=10, default="baseline")  # baseline/middle/top
    # Ancho del renglón (pt). 0 = sin auto-ajuste (tamaño fijo). Si > 0, el
    # nombre reduce su tamaño de fuente para entrar en este ancho.
    max_width = models.FloatField(default=0)

    # Config para field
    field_name = models.CharField(max_length=100, blank=True, default="full_name")

    def __str__(self):
        return f"Template - {self.event.name}"


class DownloadLog(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    name_entered = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    manual = models.BooleanField(
        default=False,
        help_text="Generado manualmente desde el panel",
    )
    # Identidad normalizada, usada para detectar descargas duplicadas.
    attendee = models.ForeignKey(
        "Attendee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="downloads",
    )
    name_normalized = models.CharField(max_length=200, db_index=True, blank=True, default="")
    email_normalized = models.EmailField(db_index=True, blank=True, default="")
    # Momento en que el PDF se ENTREGÓ de verdad (respuesta directa o primer
    # uso del link firmado). El link es de un solo uso: con esto seteado, ni
    # el link ni la ventana de gracia vuelven a entregar el archivo.
    delivered_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.event.slug} - {self.name_entered}"


class RejectedAttempt(models.Model):
    """Intento de descarga rechazado (no habilitado, duplicado, datos faltantes)."""

    REASON_CHOICES = [
        ("not_in_list", "No está en la lista de inscriptos"),
        ("duplicate", "Certificado ya descargado"),
        ("missing_email", "Email no ingresado"),
        ("missing_name", "Nombre no ingresado"),
    ]

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="rejected_attempts"
    )
    name_entered = models.CharField(max_length=200, blank=True, default="")
    email_entered = models.EmailField(blank=True, default="")
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event.slug} - {self.name_entered} ({self.reason})"


class Attendee(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="attendees")
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    full_name_normalized = models.CharField(max_length=200, db_index=True)
    email_normalized = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    download_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Limite propio de descargas. Vacio = usa el limite global del evento.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "email_normalized"],
                name="unique_event_email",
            ),
        ]
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        self.full_name_normalized = normalize_text(self.full_name)
        self.email_normalized = normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} <{self.email}>"


class SuspiciousAttendee(models.Model):
    """Fila de un import con nombre que pinta a dato corrupto/placeholder
    (????, números en el nombre, palabra repetida, etc.). No se crea como
    Attendee automáticamente: queda acá para que alguien la revise a mano
    y decida aprobarla o descartarla."""

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="suspicious_attendees"
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "email"],
                name="unique_event_suspicious_email",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event.slug} - {self.full_name} ({self.reason})"


class EmailDeliveryQuerySet(models.QuerySet):
    def active_for(self, event, attendee):
        """Entregas vivas (pendiente/enviando/enviada) de un inscripto en un evento."""
        return self.filter(
            event=event,
            attendee=attendee,
            status__in=[
                EmailDelivery.STATUS_PENDING,
                EmailDelivery.STATUS_SENDING,
                EmailDelivery.STATUS_SENT,
            ],
        )


class EmailDelivery(models.Model):
    """Cola de correos con el certificado adjunto. Una fila = un envío a un
    inscripto. La procesa `manage.py send_certificate_emails` cada minuto
    (timer systemd `certificados-mailer`)."""

    STATUS_PENDING = "pending"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_SENDING, "Enviando"),
        (STATUS_SENT, "Enviado"),
        (STATUS_FAILED, "Fallido"),
        (STATUS_SUPERSEDED, "Reemplazado"),
    ]
    MAX_ATTEMPTS = 5

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="email_deliveries")
    attendee = models.ForeignKey(
        Attendee, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_deliveries"
    )
    download_log = models.ForeignKey(DownloadLog, null=True, blank=True, on_delete=models.SET_NULL)
    to_email = models.EmailField()
    full_name = models.CharField(max_length=200)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    objects = EmailDeliveryQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.event.slug} -> {self.to_email} ({self.status})"
