from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("predict", "0027_batch_scoped_top_pick_uniqueness"),
    ]

    operations = [
        migrations.AlterField(
            model_name="toppick",
            name="variant",
            field=models.CharField(
                choices=[
                    ("1", "Sure 1"),
                    ("2", "Sure 2"),
                    ("3", "Running Bet"),
                    ("4", "Mshipi"),
                    ("5", "12"),
                    ("6", "GG"),
                    ("7", "Over 2.5"),
                ],
                default="1",
                max_length=1,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="toppick",
            name="unique_regular_top_pick_fixture_variant",
        ),
        migrations.AddConstraint(
            model_name="toppick",
            constraint=models.UniqueConstraint(
                fields=("match_date", "home_team", "away_team", "variant"),
                condition=models.Q(variant__in=["1", "2", "5", "6", "7"]),
                name="unique_regular_top_pick_fixture_variant",
            ),
        ),
    ]
}