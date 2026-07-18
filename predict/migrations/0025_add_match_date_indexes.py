# Add db_index=True on match_date fields — every prediction/top-pick query
# filters or joins on match_date, so this avoids full table scans.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("predict", "0024_add_predicted_rates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="matchprediction",
            name="match_date",
            field=models.DateField(db_index=True),
        ),
        migrations.AlterField(
            model_name="toppick",
            name="match_date",
            field=models.DateField(db_index=True),
        ),
        migrations.AlterField(
            model_name="comboslipleg",
            name="match_date",
            field=models.DateField(db_index=True),
        ),
    ]
