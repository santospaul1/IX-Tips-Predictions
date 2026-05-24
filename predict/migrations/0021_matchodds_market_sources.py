from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predict", "0020_alter_toppick_variant"),
    ]

    operations = [
        migrations.AddField(
            model_name="matchodds",
            name="market_sources",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
