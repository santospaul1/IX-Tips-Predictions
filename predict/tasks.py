from datetime import date

from celery import shared_task
from django.core.cache import cache

from .constants import API_TOKEN, COMPETITIONS, TRAINING_CACHE_TIMEOUT
from .models import MatchPrediction
from .utils import (
    fetch_and_cache_team_metadata,
    fetch_matches_by_date,
    fetch_training_data_all_seasons,
    find_next_match_date,
    get_league_table,
    get_or_train_model_bundle,
    get_top_predictions,
    save_predictions,
    store_top_pick_for_date
)

@shared_task
def schedule_predictions_staggered(match_date=None):
    delay = 0
    for comp in COMPETITIONS:
        print(f"[INFO] Scheduling prediction for {comp} in {delay} seconds")
        predict_next_fixtures_for_competition.apply_async(
            args=[comp, match_date],
            countdown=delay
        )
        delay += 180  # now using 2 minutes instead of 1

@shared_task
def trigger_staggered_scheduling():
    schedule_predictions_staggered.delay()

@shared_task
def predict_next_fixtures_for_competition(competition_code, match_date=None):
     
    print(f"[INFO] Running prediction for {competition_code} on {match_date if match_date else 'auto'}")

    if not match_date:
        match_date_to_use = find_next_match_date(fetch_matches_by_date, None, [competition_code])
        if not match_date_to_use:
            return
    else:
        match_date_to_use = match_date

    print(f"[INFO] Processing competition: {competition_code} for {match_date_to_use}")
    matches = fetch_matches_by_date(API_TOKEN, competition_code, match_date_to_use)
    if not matches:
        print(f"[WARN] No matches found for {competition_code} on {match_date_to_use}")
        return

    df = cache.get(f"training_data_{competition_code}")
    if df is None:
        df = fetch_training_data_all_seasons(competition_code)
        cache.set(f"training_data_{competition_code}", df, timeout=TRAINING_CACHE_TIMEOUT)

    if df.empty:
        print(f"[WARN] No training data for {competition_code}")
        return

    model_bundle = get_or_train_model_bundle(competition_code)
    if model_bundle is None:
        print(f"[WARN] No model bundle available for {competition_code}")
        return
    model_home, model_away, model_context = model_bundle

    predictions = save_predictions(
        matches, model_home, model_away, model_context,
        match_date=match_date_to_use,
        competition_code=competition_code
    )

    print(f"[INFO] Saved {len(predictions)} predictions for {competition_code}")

@shared_task
def cache_training_data():
    print("[CACHE] Starting training data caching")
    if not API_TOKEN:
        print("[CACHE] FOOTBALL_DATA_API_KEY is missing. Skipping training-data caching.")
        return
    for comp in COMPETITIONS:
        key = f"training_data_{comp}"
        df = fetch_training_data_all_seasons(comp)

        if df is not None and not df.empty:
            cache.set(key, df, timeout=TRAINING_CACHE_TIMEOUT)
            print(f"[CACHE] Cached {len(df)} records for {comp}")
        else:
            print(f"[CACHE] No data fetched for {comp}. Check prior API error logs for auth/network failures.")


@shared_task
def refresh_all_league_tables():
    for code in COMPETITIONS:
        print(f"[AUTO] Refreshing league table for {code}")
        get_league_table(code)

@shared_task
def update_metadata_task():
    fetch_and_cache_team_metadata()

@shared_task
def store_daily_top_pick():
    predictions = get_top_predictions(limit=10)
    store_top_pick_for_date(predictions)

@shared_task
def refresh_daily_odds_cache():
    from .views import update_all_odds

    updated = update_all_odds()
    top_predictions = get_top_predictions(limit=10)
    stored_top_picks = store_top_pick_for_date(top_predictions)
    return {
        "odds_updates": updated,
        "stored_top_picks": stored_top_picks,
    }

@shared_task
def refresh_live_match_data():
    """
    Refresh live match status, actual scores, cached kickoff metadata, odds, and top picks.
    Runs in the background so the UI does not need to do the expensive refresh itself.
    """
    from .views import refresh_prediction_statuses

    today = date.today()
    refresh_pairs = (
        MatchPrediction.objects.filter(match_date__gte=today)
        .values("match_date", "competition")
        .distinct()
    )

    status_updates = 0
    for entry in refresh_pairs:
        match_date = entry["match_date"]
        competition = entry["competition"]
        if not competition or not match_date:
            continue
        status_updates += refresh_prediction_statuses(competition, match_date, force=True)

    top_predictions = get_top_predictions(limit=10)
    stored_top_picks = store_top_pick_for_date(top_predictions)

    return {
        "status_updates": status_updates,
        "stored_top_picks": stored_top_picks,
        "dates_checked": [str(entry["match_date"]) for entry in refresh_pairs],
    }
