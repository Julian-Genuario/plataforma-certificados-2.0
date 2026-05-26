import unicodedata

from django.db import models


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

    def __str__(self):
        return f"{self.event.slug} - {self.name_entered}"


class Attendee(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="attendees")
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    full_name_normalized = models.CharField(max_length=200, db_index=True)
    email_normalized = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

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
