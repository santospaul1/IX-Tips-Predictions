# Generated migration — adds float fields for raw regression predicted rates.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predict", "0023_comboslip_auto_generated_signature"),
    ]

    operations = [
        migrations.AddField(
            model_name="matchprediction",
            name="predicted_home_rate",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="matchprediction",
            name="predicted_away_rate",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
