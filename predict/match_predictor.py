# predict/match_predictor.py
from datetime import datetime, timedelta

from .utils import (
    fetch_matches_by_date,
    fetch_training_data_all_seasons,
    get_or_train_model_bundle,
    predict_match_outcome,
)
from .models import MatchPrediction
from .constants import API_TOKEN, COMPETITIONS

competitions = COMPETITIONS


def train_and_cache_models():
    for comp_code in COMPETITIONS:
        if fetch_training_data_all_seasons(comp_code).empty:
            print(f"[WARN] No training data for {comp_code}")
            continue
        get_or_train_model_bundle(comp_code, force_refresh=True)
        print(f"[INFO] Cached model bundle for {comp_code}")


def predict_and_store_fixtures_for_today():
    today = datetime.now().strftime("%Y-%m-%d")

    for comp_code, comp_name in competitions.items():
        matches = fetch_matches_by_date(API_TOKEN, comp_code, today)

        # If no matches today, try next 5 days
        if not matches:
            for i in range(1, 6):
                future_date = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
                matches = fetch_matches_by_date(API_TOKEN, comp_code, future_date)
                if matches:
                    today = future_date
                    break

        if not matches:
            continue

        model_bundle = get_or_train_model_bundle(comp_code)
        if model_bundle is None:
            continue
        model_home, model_away, model_context = model_bundle

        for match in matches:
            home_team = match['homeTeam']['name']
            away_team = match['awayTeam']['name']
            status = match["status"]

            # Check if prediction exists
            if MatchPrediction.objects.filter(
                competition=comp_name,
                home_team=home_team,
                away_team=away_team,
                match_date=today
            ).exists():
                continue

            predicted_result, home_goals, away_goals = predict_match_outcome(
                home_team, away_team, (model_home, model_away, model_context)
            )

            MatchPrediction.objects.create(
                competition=comp_name,
                home_team=home_team,
                away_team=away_team,
                match_date=today,
                predicted_result=predicted_result,
                predicted_home_goals=home_goals,
                predicted_away_goals=away_goals,
                status=status,
            )
