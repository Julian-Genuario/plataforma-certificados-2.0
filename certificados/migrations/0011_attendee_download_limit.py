from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("certificados", "0010_event_free_download"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendee",
            name="download_limit",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text="Limite propio de descargas. Vacio = usa el limite global del evento.",
            ),
        ),
    ]
