from django.core.management.base import BaseCommand
from faker import Faker
import random
from datetime import timedelta, datetime
from predict.models import MatchPrediction

fake = Faker()

COMPETITIONS = ["Premier League", "Serie A", "Bundesliga", "La Liga", "Ligue 1"]
TEAM_NAMES = [
    "Arsenal FC", "Manchester City", "Chelsea FC", "Liverpool FC",
    "Juventus", "AC Milan", "Inter Milan", "Roma",
    "Bayern Munich", "Dortmund", "RB Leipzig",
    "Real Madrid", "Barcelona", "Atletico Madrid",
    "PSG", "Marseille", "Lyon"
]

class Command(BaseCommand):
    help = "Generate fake match predictions with realistic dates and results"

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=50, help='Number of fake predictions to generate')
        parser.add_argument('--clear', action='store_true', help='Clear existing predictions before generating new ones')

    def handle(self, *args, **options):
        count = options['count']
        clear_existing = options['clear']

        if clear_existing:
            MatchPrediction.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing predictions cleared."))

        generated = 0
        while generated < count:
            home, away = random.sample(TEAM_NAMES, 2)
            match_date = fake.date_between(start_date="-30d", end_date="+7d")
            status = "FINISHED" if match_date < datetime.today().date() else "TIMED"

            predicted_home_goals = random.randint(0, 4)
            predicted_away_goals = random.randint(0, 4)

            if status == "FINISHED":
                actual_home_goals = random.randint(0, 4)
                actual_away_goals = random.randint(0, 4)
            else:
                actual_home_goals = None
                actual_away_goals = None

            MatchPrediction.objects.create(
                home_team=home,
                away_team=away,
                competition=random.choice(COMPETITIONS),
                match_date=match_date,
                predicted_home_goals=predicted_home_goals,
                predicted_away_goals=predicted_away_goals,
                actual_home_goals=actual_home_goals,
                actual_away_goals=actual_away_goals,
                status=status
            )
            generated += 1

        self.stdout.write(self.style.SUCCESS(f"{generated} fake predictions generated successfully."))
