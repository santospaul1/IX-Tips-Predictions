from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predict", "0021_matchodds_market_sources"),
    ]

    operations = [
        migrations.CreateModel(
            name="ComboSlip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("size", models.PositiveIntegerField(default=5)),
                ("market_filter", models.CharField(blank=True, default="", max_length=50)),
                ("style", models.CharField(choices=[("safe", "Safe"), ("value", "Value")], default="safe", max_length=10)),
                ("combined_odds", models.FloatField(blank=True, null=True)),
                ("average_confidence", models.FloatField(default=0)),
                ("priced_legs", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ComboSlipLeg",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("match_date", models.DateField()),
                ("competition", models.CharField(max_length=20)),
                ("home_team", models.CharField(max_length=100)),
                ("away_team", models.CharField(max_length=100)),
                ("tip", models.CharField(max_length=50)),
                ("confidence", models.FloatField(default=0)),
                ("odds", models.FloatField(blank=True, null=True)),
                ("slip", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="legs", to="predict.comboslip")),
            ],
            options={"ordering": ["match_date", "home_team"]},
        ),
    ]
