from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predict", "0022_comboslip_comboslipleg"),
    ]

    operations = [
        migrations.AddField(
            model_name="comboslip",
            name="auto_generated",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="comboslip",
            name="signature",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
    ]
