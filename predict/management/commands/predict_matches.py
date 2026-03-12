from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils.timezone import now

from predict.constants import API_TOKEN, COMPETITIONS
from predict.models import MatchPrediction
from predict.utils import fetch_matches_by_date, get_or_train_model_bundle, predict_match_outcome


class Command(BaseCommand):
    help = "Predict upcoming fixtures and store them in the database"

    def handle(self, *args, **options):
        date_checked = now().date()
        max_days_ahead = 7

        for day in range(max_days_ahead):
            match_date = (date_checked + timedelta(days=day)).strftime("%Y-%m-%d")
            found_fixtures = False

            for comp_code, comp_name in COMPETITIONS.items():
                matches = fetch_matches_by_date(API_TOKEN, comp_code, match_date)
                if not matches:
                    continue

                model_bundle = get_or_train_model_bundle(comp_code)
                if model_bundle is None:
                    self.stdout.write(self.style.WARNING(f"No model bundle available for {comp_code}"))
                    continue

                model_home, model_away, model_context = model_bundle
                found_fixtures = True

                for match in matches:
                    home_team = match["homeTeam"]["name"]
                    away_team = match["awayTeam"]["name"]
                    match_id = match.get("id") or f"{home_team}-{away_team}-{match.get('utcDate', '')}"

                    if MatchPrediction.objects.filter(match_id=match_id).exists():
                        continue

                    predicted_result, home_goals, away_goals = predict_match_outcome(
                        home_team,
                        away_team,
                        (model_home, model_away, model_context),
                    )

                    MatchPrediction.objects.create(
                        match_id=match_id,
                        competition=comp_name,
                        home_team=home_team,
                        away_team=away_team,
                        match_date=match_date,
                        predicted_result=predicted_result,
                        predicted_home_goals=home_goals,
                        predicted_away_goals=away_goals,
                        market_over_1_5=(home_goals + away_goals) >= 2,
                        market_over_2_5=(home_goals + away_goals) >= 3,
                        market_under_1_5=(home_goals + away_goals) < 2,
                        market_under_2_5=(home_goals + away_goals) < 3,
                        market_gg=home_goals > 0 and away_goals > 0,
                        market_nogg=home_goals == 0 or away_goals == 0,
                        status=match.get("status", "TIMED"),
                    )

            if found_fixtures:
                self.stdout.write(self.style.SUCCESS(f"Predictions stored for {match_date}"))
                return

        self.stdout.write(self.style.WARNING("No fixtures found in the next 7 days."))
