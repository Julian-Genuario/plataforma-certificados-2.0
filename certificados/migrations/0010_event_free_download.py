from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("certificados", "0009_sitesettings"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="free_download",
            field=models.BooleanField(
                default=False,
                help_text="Si est\u00e1 activado, cualquier persona puede descargar aunque no est\u00e9 en la lista de inscriptos (descarga libre).",
            ),
        ),
    ]
