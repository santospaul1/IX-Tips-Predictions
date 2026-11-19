import logging
from datetime import date

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

from .constants import COMPETITIONS
from .models import MatchPrediction, TopPick
from .fixture_sources import fetch_fixture_matches_by_date, fetch_upcoming_fixture_groups
from .utils import (
    fetch_and_cache_team_metadata,
    fetch_matches_by_date,
    fetch_training_data_all_seasons,
    find_next_match_date,
    find_upcoming_match_dates,
    get_league_table,
    get_or_train_model_bundle,
    get_top_predictions_for_variant,
    _collect_top_pick_candidates,
    save_predictions,
    store_top_pick_for_date
)

@shared_task
def schedule_predictions_staggered(match_date=None):
    delay = 0
    for comp in COMPETITIONS:
        logger.info(f" Scheduling prediction for {comp} in {delay} seconds")
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
    from .views import refresh_competition_odds

    logger.info("Running prediction for %s on %s", competition_code, match_date or "auto")
    if not match_date:
        fixtures_by_date = fetch_upcoming_fixture_groups(
            None, competition_code, days_ahead=45,
        )
    else:
        fixtures_by_date = {
            str(match_date)[:10]: fetch_fixture_matches_by_date(None, competition_code, match_date)
        }
    match_dates_to_use = sorted(fixtures_by_date)

    if not match_dates_to_use:
        result = {"competition": competition_code, "status": "no_fixtures", "training_entries": 0, "fixture_count": 0, "saved_predictions": 0, "dates_checked": []}
        logger.warning("PREDICTION_COVERAGE %s", result)
        return result

    df = cache.get(f"training_data_{competition_code}")
    if df is None:
        # fetch_training_data_all_seasons owns the provider-specific cache TTL.
        df = fetch_training_data_all_seasons(competition_code)

    training_entries = len(df) if df is not None else 0
    if df is None or df.empty:
        result = {"competition": competition_code, "status": "no_training_data", "training_entries": training_entries, "fixture_count": 0, "saved_predictions": 0, "dates_checked": match_dates_to_use}
        logger.warning("PREDICTION_COVERAGE %s", result)
        return result

    model_bundle = get_or_train_model_bundle(competition_code)
    if model_bundle is None:
        result = {"competition": competition_code, "status": "model_unavailable", "training_entries": training_entries, "fixture_count": 0, "saved_predictions": 0, "dates_checked": match_dates_to_use}
        logger.warning("PREDICTION_COVERAGE %s", result)
        return result

    model_home, model_away, model_context = model_bundle
    total_saved = 0
    total_fixtures = 0
    dates_with_fixtures = []
    for match_date_to_use in match_dates_to_use:
        logger.info("Processing %s for %s", competition_code, match_date_to_use)
        matches = fixtures_by_date.get(match_date_to_use) or []
        if not matches:
            logger.warning("No fixtures for %s on %s", competition_code, match_date_to_use)
            continue

        total_fixtures += len(matches)
        dates_with_fixtures.append(match_date_to_use)
        predictions = save_predictions(
            matches, model_home, model_away, model_context,
            match_date=match_date_to_use, competition_code=competition_code,
        )
        total_saved += len(predictions)
        refresh_competition_odds(
            competition_code, force=True,
            match_dates=[date.fromisoformat(match_date_to_use)],
        )

    result = {
        "competition": competition_code,
        "status": "generated" if total_saved else ("fixtures_found_no_predictions" if total_fixtures else "no_fixtures"),
        "training_entries": training_entries,
        "fixture_count": total_fixtures,
        "saved_predictions": total_saved,
        "dates_checked": match_dates_to_use,
        "dates_with_fixtures": dates_with_fixtures,
    }
    logger.info("PREDICTION_COVERAGE %s", result)
    return result

@shared_task
def cache_training_data():
    logger.info("Starting training data caching")
    from django.core.cache import caches
    from .constants import model_cache_key
    from .providers import supports_open_matchday
    from .utils import _MODEL_REGISTRY

    model_store = caches["model_cache"]
    for comp in COMPETITIONS:
        key = f"training_data_{comp}"

        # OM is refreshed daily. Remove its older (possibly seven-day) cache
        # entry before fetching so a new published results CSV is picked up.
        if supports_open_matchday(comp):
            cache.delete(key)

        df = fetch_training_data_all_seasons(comp)
        if df is not None and not df.empty:
            # fetch_training_data_all_seasons owns cache writes and applies the
            # shorter OM-specific TTL. Do not overwrite it with seven days.
            if supports_open_matchday(comp):
                _MODEL_REGISTRY.pop(comp, None)
                try:
                    model_store.delete(model_cache_key(comp))
                except Exception as exc:
                    logger.warning("Could not invalidate cached model for %s: %s", comp, exc)
            logger.info("Cached %s records for %s", len(df), comp)
        else:
            logger.info("No data fetched for %s. Check provider diagnostics.", comp)


@shared_task
def refresh_all_league_tables():
    for code in COMPETITIONS:
        logger.info(f" Refreshing league table for {code}")
        get_league_table(code)

@shared_task
def update_metadata_task():
    fetch_and_cache_team_metadata()

@shared_task
def store_daily_top_pick():
    stored = {}
    for variant in ("1", "2", "3", "4", "5", "6", "7"):
        # (delete now handled atomically inside store_top_pick_for_date)
        limit = 20 if variant == "4" else (12 if variant in {"5", "6", "7"} else 10)
        predictions = get_top_predictions_for_variant(limit=limit, variant=variant)
        stored[variant] = store_top_pick_for_date(predictions, variant=variant)
    return stored

@shared_task
def refresh_daily_odds_cache():
    from .views import update_all_odds

    updated = update_all_odds()
    stored_top_picks = {}
    for variant in ("1", "2", "3", "4", "5", "6", "7"):
        limit = 20 if variant == "4" else (12 if variant in {"5", "6", "7"} else 10)
        top_predictions = get_top_predictions_for_variant(limit=limit, variant=variant)
        stored_top_picks[variant] = store_top_pick_for_date(top_predictions, variant=variant)
    return {
        "odds_updates": updated,
        "stored_top_picks": stored_top_picks,
    }

@shared_task
def refresh_live_match_data():
    """Run one live refresh at a time; stale locks expire after fifteen minutes."""
    lock_key = "task_lock::refresh_live_match_data"
    try:
        acquired = cache.add(lock_key, True, timeout=15 * 60)
    except Exception:
        logger.exception("Could not acquire live refresh lock; continuing without lock")
        acquired = False
    if not acquired:
        # Distinguish a cache outage from another active run: the cache.add
        # failure path above also reaches here, but the task should still work.
        try:
            if cache.get(lock_key):
                logger.info("Skipping live refresh because a previous run is still active")
                return {"status": "skipped", "reason": "already_running"}
        except Exception:
            pass
        acquired = True
    try:
        return _refresh_live_match_data_impl()
    finally:
        if acquired:
            try:
                cache.delete(lock_key)
            except Exception:
                logger.warning("Could not release live refresh lock")


def _refresh_live_match_data_impl():
    """Refresh statuses, unfinished predictions and daily top-pick markets."""
    from .views import refresh_prediction_statuses

    from datetime import timedelta
    today = date.today()
    yesterday = today - timedelta(days=1)
    refresh_pairs = (
        MatchPrediction.objects.filter(match_date__gte=yesterday)
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

    # Recalculate every unfinished prediction with the already-cached model.
    # Settled matches are excluded here and protected again in save_predictions.
    unfinished = MatchPrediction.objects.filter(
        match_date__gte=today,
        status__in=["TIMED", "LIVE"],
    ).order_by("competition", "match_date")
    grouped_unfinished = {}
    for prediction in unfinished:
        grouped_unfinished.setdefault(prediction.competition, []).append(prediction)

    prediction_updates = 0
    for competition, rows in grouped_unfinished.items():
        bundle = get_or_train_model_bundle(competition)
        if not bundle or len(bundle) != 3:
            continue
        matches = [{
            "id": row.match_id,
            "utcDate": row.match_date.isoformat(),
            "status": row.status,
            "homeTeam": {"name": row.home_team},
            "awayTeam": {"name": row.away_team},
        } for row in rows if row.match_id]
        if matches:
            updated_rows = save_predictions(
                matches, bundle[0], bundle[1], bundle[2],
                competition_code=competition,
            )
            prediction_updates += len(updated_rows)

    # Score each upcoming fixture once. Standard variants use the top four;
    # 12, GG, Over 2.5 and Mshipi can inspect the full ranked market list.
    rank_all_candidates = _collect_top_pick_candidates(rank_limit=12)
    rank_four_candidates = {
        date_str: [candidate for candidate in candidates if candidate["rank_index"] < 4]
        for date_str, candidates in rank_all_candidates.items()
    }

    stored_top_picks = {}
    for variant in ("1", "2", "3", "4", "5", "6", "7"):
        candidates = rank_all_candidates if variant in {"4", "5", "6", "7"} else rank_four_candidates
        limit = 20 if variant == "4" else (12 if variant in {"5", "6", "7"} else 10)
        top_predictions = get_top_predictions_for_variant(
            limit=limit,
            variant=variant,
            candidates_by_date=candidates,
        )
        stored_top_picks[variant] = store_top_pick_for_date(top_predictions, variant=variant)

    return {
        "status_updates": status_updates,
        "prediction_updates": prediction_updates,
        "unfinished_fixtures_checked": sum(len(rows) for rows in grouped_unfinished.values()),
        "stored_top_picks": stored_top_picks,
        "dates_checked": [str(entry["match_date"]) for entry in refresh_pairs],
    }


@shared_task
def refresh_combo_slips():
    from .views import generate_all_combo_slips

    return generate_all_combo_slips()
