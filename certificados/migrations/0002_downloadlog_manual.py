from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("certificados", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="downloadlog",
            name="manual",
            field=models.BooleanField(
                default=False,
                help_text="Generado manualmente desde el panel",
            ),
        ),
    ]
