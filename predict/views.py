# views.py (updated - uses The Odds API fallback for odds)
import csv
import difflib
import hashlib
import os
import re
import json
import requests
import pandas as pd
from datetime import datetime, date, timedelta
from collections import defaultdict
from urllib.parse import quote, urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.contrib import messages
from django.core.cache import cache
from django.templatetags.static import static
from django.contrib.auth.decorators import login_required
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from celery import current_app
from django_celery_beat.models import PeriodicTask

from .models import ComboSlip, ComboSlipLeg, MatchOdds, MatchPrediction, TopPick
from .forms import ActualResultForm, PredictionForm, LivePredictionForm
from .constants import API_TOKEN, COMPETITIONS, ODDS_API_KEY as SETTINGS_ODDS_API_KEY
from .utils import (
    _market_odds_value,
    explain_pick_reasons,
    fetch_competition_scorers,
    fetch_matches_by_date,
    get_top_predictions as utils_get_top_predictions,
    get_top_predictions_for_variant,
    get_or_train_model_bundle,
    market_edge,
    predict_match_outcome,
    preprocess_api_data,
    #store_top_pick_for_date,
    score_top_pick_markets,
    train_models,
    get_league_table,
    fetch_training_data,
    fetch_matches_by_season,
    fetch_training_data_all_seasons,
    find_next_available_match_date,
    find_next_match_date,
    _team_name_aliases,
    preprocess_match_data,
    process_match_data,
    scoreline_predictions,
    update_actuals_for_top_picks,
    get_team_recent_form,
)
from .generate_logo_mapping import TEAM_LOGOS

# -------------------------
# CONFIG
# -------------------------
API_KEY = getattr(settings, "FOOTBALL_DATA_API_KEY", API_TOKEN)
BASE_URL = "https://api.football-data.org/v4"

# ODDS provider (the-odds-api)
ODDS_API_KEY = getattr(settings, "ODDS_API_KEY", SETTINGS_ODDS_API_KEY)
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
competitions = COMPETITIONS
# Map competition codes to The Odds API sport keys (extend as needed)
COMPETITION_SPORT_MAP = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "ELC": "soccer_gbr_championship",
    "CL": "soccer_uefa_champs_league",
    "BSA": "soccer_brazil_serie_a",
    "CLI": "soccer_copa_libertadores",  # approximate
    "WC": "soccer_fifa_world_cup",
    # add more mappings as needed
}

# Cache timeout for odds (seconds)
ODDS_CACHE_TIMEOUT = 60 * 10  # 10 minutes
PREFERRED_BOOKMAKERS = ["1xBet", "Tipico"]

# -------------------------
# Utilities for team normalization / fuzzy matching
# -------------------------
def fetch_odds(sport_key):
    """Fetch odds for one competition"""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "regions": "eu",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
        "apiKey": ODDS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception:
        return []


def extract_odds_from_bookmaker(bookmaker, game):
    odds_out = {
        "home": None,
        "draw": None,
        "away": None,
        "over25": None,
        "under25": None,
        "btts_yes": None,
    }
    for mk in bookmaker.get("markets", []):
        if mk["key"] == "h2h":
            for outcome in mk["outcomes"]:
                if outcome["name"] == game["home_team"]:
                    odds_out["home"] = outcome["price"]
                elif outcome["name"] == game["away_team"]:
                    odds_out["away"] = outcome["price"]
                elif outcome["name"].lower() in ["draw", "x"]:
                    odds_out["draw"] = outcome["price"]

        elif mk["key"] == "totals":
            for outcome in mk["outcomes"]:
                if outcome.get("point") == 2.5:
                    if outcome["name"].lower() == "over":
                        odds_out["over25"] = outcome["price"]
                    elif outcome["name"].lower() == "under":
                        odds_out["under25"] = outcome["price"]

        elif mk["key"] == "btts":
            for outcome in mk["outcomes"]:
                if outcome["name"].lower() == "yes":
                    odds_out["btts_yes"] = outcome["price"]

    return odds_out


def merge_odds_from_bookmakers(game):
    merged_odds = {
        "home": None,
        "draw": None,
        "away": None,
        "over25": None,
        "under25": None,
        "btts_yes": None,
        "btts_no": None,
        "bookmaker": None,
        "market_sources": {},
    }
    if not game.get("bookmakers"):
        return merged_odds

    ordered_bookmakers = sorted(
        game.get("bookmakers", []),
        key=lambda bookmaker: (
            0 if bookmaker.get("title") in PREFERRED_BOOKMAKERS else 1,
            PREFERRED_BOOKMAKERS.index(bookmaker.get("title")) if bookmaker.get("title") in PREFERRED_BOOKMAKERS else 999,
            bookmaker.get("title", ""),
        ),
    )

    for bookmaker in ordered_bookmakers:
        odds = extract_odds_from_bookmaker(bookmaker, game)
        for market_key in ("home", "draw", "away", "over25", "under25", "btts_yes", "btts_no"):
            if merged_odds[market_key] is None and odds.get(market_key) is not None:
                merged_odds[market_key] = odds.get(market_key)
                merged_odds["market_sources"][market_key] = bookmaker.get("title")
        if merged_odds["bookmaker"] is None and any(value is not None for value in odds.values()):
            merged_odds["bookmaker"] = bookmaker.get("title")

    return merged_odds


def normalize_team_name(team, names):
    match = difflib.get_close_matches(team, names, n=1, cutoff=0.7)
    return match[0] if match else team


def fixture_refresh_cache_key(competition_code, match_date):
    return f"fixture_refresh::{competition_code}::{match_date}"


def competition_odds_refresh_cache_key(competition_code):
    return f"competition_odds_refresh::{competition_code}"


def fixture_meta_cache_key(competition_code, match_date, home_team, away_team):
    return f"fixture_meta::{competition_code}::{match_date}::{home_team}::{away_team}"


def format_kickoff_time(utc_date):
    if not utc_date:
        return ""
    try:
        parsed = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        return timezone.localtime(parsed).strftime("%H:%M")
    except ValueError:
        return ""


def normalize_team_lookup_key(name):
    candidate = (name or "").strip().lower()
    candidate = re.sub(r"[^\w\s]", " ", candidate)
    candidate = re.sub(r"\b(fc|cf|ac|sc|afc|club|de|da|del)\b", " ", candidate)
    return re.sub(r"\s+", " ", candidate).strip()


def fetch_matches_for_status_refresh(competition_code, match_date):
    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    headers = {"X-Auth-Token": API_KEY}
    params = {"dateFrom": match_date, "dateTo": match_date}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json().get("matches", [])
    except requests.exceptions.RequestException as exc:
        print(f"[ERROR] Status refresh failed for {competition_code} {match_date}: {exc}")
        return []


def normalize_api_match_status(api_status):
    if api_status in {"FINISHED", "AWARDED"}:
        return "FINISHED"
    if api_status in {"IN_PLAY", "PAUSED", "LIVE"}:
        return "LIVE"
    if api_status in {"POSTPONED", "SUSPENDED", "CANCELLED"}:
        return api_status
    return "TIMED"


def refresh_prediction_statuses(competition_code, match_date, force=False):
    match_date_str = match_date.isoformat() if isinstance(match_date, date) else str(match_date)
    if not competition_code or not match_date_str:
        return 0

    refresh_key = fixture_refresh_cache_key(competition_code, match_date_str)
    if not force and cache.get(refresh_key):
        return 0

    matches = fetch_matches_for_status_refresh(competition_code, match_date_str)
    if not matches:
        cache.set(refresh_key, True, timeout=300)
        return 0

    predictions = {
        (normalize_team_lookup_key(p.home_team), normalize_team_lookup_key(p.away_team)): p
        for p in MatchPrediction.objects.filter(
            competition=competition_code,
            match_date=match_date_str,
        )
    }

    updated = 0
    for match in matches:
        home_team = match.get("homeTeam", {}).get("name")
        away_team = match.get("awayTeam", {}).get("name")
        if not home_team or not away_team:
            continue

        cache.set(
            fixture_meta_cache_key(competition_code, match_date_str, home_team, away_team),
            {"kickoff_time": format_kickoff_time(match.get("utcDate"))},
            timeout=60 * 60 * 12,
        )

        prediction = predictions.get(
            (normalize_team_lookup_key(home_team), normalize_team_lookup_key(away_team))
        )
        if not prediction:
            continue

        normalized_status = normalize_api_match_status(match.get("status"))
        changed = prediction.status != normalized_status
        prediction.status = normalized_status

        if normalized_status == "FINISHED":
            half_time_score = match.get("score", {}).get("halfTime", {})
            full_time_score = match.get("score", {}).get("fullTime", {})
            actual_ht_home_goals = half_time_score.get("home")
            actual_ht_away_goals = half_time_score.get("away")
            actual_home_goals = full_time_score.get("home")
            actual_away_goals = full_time_score.get("away")
            if prediction.actual_ht_home_goals != actual_ht_home_goals or prediction.actual_ht_away_goals != actual_ht_away_goals:
                changed = True
            prediction.actual_ht_home_goals = actual_ht_home_goals
            prediction.actual_ht_away_goals = actual_ht_away_goals
            if actual_home_goals is not None and actual_away_goals is not None:
                if prediction.actual_home_goals != actual_home_goals or prediction.actual_away_goals != actual_away_goals:
                    changed = True
                prediction.actual_home_goals = actual_home_goals
                prediction.actual_away_goals = actual_away_goals
                predicted_result = (
                    "Home" if (prediction.predicted_home_goals or 0) > (prediction.predicted_away_goals or 0)
                    else "Away" if (prediction.predicted_home_goals or 0) < (prediction.predicted_away_goals or 0)
                    else "Draw"
                )
                actual_result = (
                    "Home" if actual_home_goals > actual_away_goals
                    else "Away" if actual_home_goals < actual_away_goals
                    else "Draw"
                )
                prediction.is_accurate = (predicted_result == actual_result)

        if changed:
            prediction.save()
            updated += 1

    cache.set(refresh_key, True, timeout=300)
    return updated


def get_cached_kickoff_time(competition_code, match_date, home_team, away_team):
    match_date_str = match_date.isoformat() if isinstance(match_date, date) else str(match_date)
    cached = cache.get(fixture_meta_cache_key(competition_code, match_date_str, home_team, away_team), {})
    return cached.get("kickoff_time", "")


def _normalize_player_name(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def get_actual_scorer_names(competition_code, match_date, home_team, away_team):
    if not competition_code or not match_date:
        return None

    match_date_str = match_date.isoformat() if isinstance(match_date, date) else str(match_date)
    cache_key = f"actual_scorers::{competition_code}::{match_date_str}::{normalize_team_lookup_key(home_team)}::{normalize_team_lookup_key(away_team)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    matches = fetch_matches_by_date(API_KEY, competition_code, match_date_str)
    target_home_aliases = _team_name_aliases(home_team)
    target_away_aliases = _team_name_aliases(away_team)

    for match in matches:
        api_home = (match.get("homeTeam") or {}).get("name")
        api_away = (match.get("awayTeam") or {}).get("name")
        if not api_home or not api_away:
            continue
        if not (_team_name_aliases(api_home) & target_home_aliases and _team_name_aliases(api_away) & target_away_aliases):
            continue

        scorer_names = []
        for goal in match.get("goals", []) or []:
            scorer = goal.get("scorer") or {}
            scorer_name = scorer.get("name") or goal.get("scorerName") or goal.get("playerName")
            if scorer_name:
                scorer_names.append(scorer_name)

        normalized_names = {_normalize_player_name(name) for name in scorer_names if name}
        cache.set(cache_key, normalized_names, timeout=300)
        return normalized_names

    cache.set(cache_key, None, timeout=300)
    return None


def upsert_match_odds(prediction, odds, bookmaker=None):
    if not prediction or not odds:
        return None

    return MatchOdds.objects.update_or_create(
        match=prediction,
        defaults={
            "home_win": odds.get("home"),
            "draw": odds.get("draw"),
            "away_win": odds.get("away"),
            "over_2_5": odds.get("over25"),
            "under_2_5": odds.get("under25"),
            "btts_yes": odds.get("btts_yes"),
            "btts_no": odds.get("btts_no"),
            "bookmaker": bookmaker or odds.get("bookmaker"),
            "market_sources": odds.get("market_sources") or {},
        },
    )


def resolve_prediction_odds(prediction):
    try:
        return prediction.odds
    except MatchOdds.DoesNotExist:
        return None


def update_odds_in_db(competition_code, match_dates=None):
    """Fetch odds for a competition and update DB cache."""
    sport_key = COMPETITION_SPORT_MAP.get(competition_code)
    if not sport_key:
        return 0

    odds_data = fetch_odds(sport_key)
    saved_count = 0
    prediction_filters = {"competition": competition_code}
    if match_dates:
        prediction_filters["match_date__in"] = list(match_dates)
    else:
        prediction_filters["match_date__gte"] = date.today() - timedelta(days=1)

    candidate_predictions = list(
        MatchPrediction.objects.filter(**prediction_filters).order_by("match_date", "id")
    )
    prediction_map = {
        (normalize_team_lookup_key(p.home_team), normalize_team_lookup_key(p.away_team)): p
        for p in candidate_predictions
    }

    for game in odds_data:
        home, away = game["home_team"], game["away_team"]
        odds = merge_odds_from_bookmakers(game)
        prediction = prediction_map.get(
            (normalize_team_lookup_key(home), normalize_team_lookup_key(away))
        )
        if not prediction:
            continue
        if any(
            odds.get(market_key) is not None
            for market_key in ("home", "draw", "away", "over25", "under25", "btts_yes", "btts_no")
        ):
            upsert_match_odds(prediction, odds, bookmaker=odds.get("bookmaker"))
            saved_count += 1
    return saved_count


def refresh_competition_odds(competition_code, force=False, match_dates=None):
    if not competition_code:
        return 0
    refresh_key = competition_odds_refresh_cache_key(competition_code)
    if not force and cache.get(refresh_key):
        return 0
    updated = update_odds_in_db(competition_code, match_dates=match_dates)
    cache.set(refresh_key, True, timeout=300)
    return updated


def update_all_odds():
    """Fetch and update odds for ALL competitions in COMPETITION_SPORT_MAP"""
    total_saved = 0
    for comp in COMPETITION_SPORT_MAP.keys():
        total_saved += update_odds_in_db(comp)
    return total_saved

def view_odds(request):
    competition = request.GET.get("competition", "EPL")
    sport_key = COMPETITION_SPORT_MAP.get(competition, "soccer_epl")

    # For testing, just grab all odds data for the competition
    odds_data = fetch_odds(sport_key)

    return render(request, "predict/view_odds.html", {
        "competition": competition,
        "sport_key": sport_key,
        "data": odds_data,  # full data for debugging in template
    })

def fetch_odds_for_match(match, competition_code="EPL"):
    sport_key = COMPETITION_SPORT_MAP.get(competition_code, "soccer_epl")
    odds_data = fetch_odds(sport_key)

    if not odds_data or "error" in odds_data:
        return None

    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]

    # Collect available names from API
    api_names = []
    for game in odds_data:
        api_names.extend([game["home_team"], game["away_team"]])

    # Normalize names
    home_norm = normalize_team_name(home, api_names)
    away_norm = normalize_team_name(away, api_names)

    for game in odds_data:
        if (game["home_team"] == home_norm and game["away_team"] == away_norm) or \
           (game["home_team"] == away_norm and game["away_team"] == home_norm):
            return merge_odds_from_bookmakers(game)

    return None

    
def get_top_predictions(limit=10, variant=1):
    return get_top_predictions_for_variant(limit=limit, variant=variant)
# -------------------------
# Helper: fetch actual results (existing)
# -------------------------
def fetch_actual_results(competition_code, match_date):
    matches = fetch_matches_for_status_refresh(competition_code, match_date)
    actual_results = []
    for match in matches:
        if match.get("status") != "FINISHED":
            continue
        home_team = match["homeTeam"]["name"]
        away_team = match["awayTeam"]["name"]
        full_time_score = match.get("score", {}).get("fullTime", {})
        actual_home_goals = full_time_score.get("home")
        actual_away_goals = full_time_score.get("away")
        if actual_home_goals is None or actual_away_goals is None:
            continue
        actual_result = (
            "Home" if actual_home_goals > actual_away_goals
            else "Away" if actual_home_goals < actual_away_goals
            else "Draw"
        )
        actual_results.append({
            "home_team": home_team,
            "away_team": away_team,
            "actual_home_goals": actual_home_goals,
            "actual_away_goals": actual_away_goals,
            "actual_result": actual_result,
        })
    return actual_results


# -------------------------
# LIVE PREDICTIONS (updated to include odds)
# -------------------------
def live_predictions_by_date(request):
    predictions = []
    message = ""
    form = LivePredictionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        match_date = form.cleaned_data['match_date'].strftime("%Y-%m-%d")
        competition_code = form.cleaned_data['competition']

        matches = fetch_matches_by_date(API_KEY, competition_code, match_date)
        if not matches:
            message = "No matches found for selected date."
        else:
            model_bundle = get_or_train_model_bundle(competition_code)
            if model_bundle is None:
                message = "No training data found for this competition."
            else:
                model_home, model_away, model_context = model_bundle

                actual_results = fetch_actual_results(competition_code, match_date)
                actual_result_map = {
                    (res['home_team'], res['away_team']): res for res in actual_results
                }

                for match in matches:
                    home = match['homeTeam']['name']
                    away = match['awayTeam']['name']
                    match_id = match.get('id')

                    try:
                        _, pred_home, pred_away = predict_match_outcome(
                            home,
                            away,
                            (model_home, model_away, model_context),
                        )

                        result = actual_result_map.get((home, away))

                        # Fetch odds using The Odds API fallback
                        odds = fetch_odds_for_match(match, competition_code)

                        # Persist prediction + odds to DB
                        prediction, created = MatchPrediction.objects.update_or_create(
                            match_id=match_id or f"{home}-{away}-{match.get('utcDate','')}",
                            defaults={
                                'match_date': match['utcDate'][:10],
                                'competition': competition_code,
                                'home_team': home,
                                'away_team': away,
                                'predicted_home_goals': int(round(pred_home)),
                                'predicted_away_goals': int(round(pred_away)),
                            }
                        )

                        # ✅ Save odds in MatchOdds model
                        if odds:
                            upsert_match_odds(prediction, odds)

                        

                        if result:
                            prediction.actual_home_goals = result['actual_home_goals']
                            prediction.actual_away_goals = result['actual_away_goals']
                            prediction.status = "FINISHED"
                            # mark accuracy
                            predicted_result = "Home" if pred_home > pred_away else ("Away" if pred_home < pred_away else "Draw")
                            prediction.is_accurate = (predicted_result == result["actual_result"])
                        prediction.save()
                        predictions.append(prediction)

                    except Exception as e:
                        print(f"[ERROR] Prediction failed for {home} vs {away}: {e}")
    else:
        form = LivePredictionForm()
    print(predictions)

    return render(request, "predict/live_predictions.html", {
        "form": form,
        "predictions": predictions,
        "message": message,
    })


# -------------------------
# UPDATED get_top_predictions (uses odds + confidence heuristics)
# -------------------------
from datetime import date
from .models import MatchPrediction
from .utils import get_team_metadata  # make sure this exists

# -------------------------
# store_top_pick_for_date (uses picks format above)
# -------------------------
def store_top_pick_for_date(predictions_by_date, specific_date=None, variant="1"):
    """
    Save top picks (from get_top_predictions-like structure) to TopPick DB.
    predictions_by_date: dict date_str -> list of picks
    """
    for date_str, picks in predictions_by_date.items():
        try:
            match_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        for p in picks:
            home_name = p["home_team"]
            away_name = p["away_team"]
            tip = p["tip"]
            confidence = p.get("confidence", 0)
            odds_val = p.get("odds", 0.0)

            TopPick.objects.update_or_create(
                match_date=match_date,
                home_team=home_name,
                away_team=away_name,
                variant=variant,
                defaults={
                    "tip": tip,
                    "confidence": confidence,
                    "odds": odds_val,
                    "actual_tip": None,
                    "is_correct": None,
                }
            )
    cache.delete("top_pick_slip_summary_v1")


# -------------------------
# Top picks view (updated to use our store/top logic)
# -------------------------
@require_GET
def top_picks_view(request):
    label_filter = request.GET.get("filter")
    match_date_str = request.GET.get("match_date")
    show_past = request.GET.get("past") == "1"
    variant = request.GET.get("variant", "1")
    variant = variant if variant in {"1", "2", "3", "4"} else "1"

    today = date.today()

    if match_date_str:
        try:
            match_date = date.fromisoformat(match_date_str)
        except ValueError:
            match_date = today
    else:
        upcoming_dates = TopPick.objects.filter(variant=variant, match_date__gte=today).order_by("match_date").values_list("match_date", flat=True).distinct()
        match_date = upcoming_dates.first() if upcoming_dates.exists() else today

    # Fetch from DB
    if show_past:
        if variant in {"3", "4"}:
            picks_qs = TopPick.objects.filter(variant=variant, match_date__lt=today).order_by("-confidence", "match_date")
        else:
            picks_qs = TopPick.objects.filter(variant=variant, match_date__lt=today).order_by("-match_date")
    else:
        if variant in {"3", "4"}:
            picks_qs = TopPick.objects.filter(variant=variant, match_date__gte=today).order_by("-confidence", "match_date")
        else:
            picks_qs = TopPick.objects.filter(match_date=match_date, variant=variant)

    pick_keys = list(
        picks_qs.values(
            "home_team",
            "away_team",
            "match_date",
        )
    )
    source = "cached"
    prediction_count_for_date = 0
    if not show_past and match_date:
        if variant in {"3", "4"}:
            prediction_count_for_date = MatchPrediction.objects.filter(match_date__gte=today).count()
        else:
            prediction_count_for_date = MatchPrediction.objects.filter(match_date=match_date).count()
    can_generate_top_picks = (
        not show_past
        and prediction_count_for_date > 0
        and not pick_keys
        and (
            (variant in {"3", "4"} and True)
            or match_date >= today
        )
    )

    if not pick_keys and can_generate_top_picks:
        generation_limit = 20 if variant == "4" else 10
        predictions_by_date = get_top_predictions(limit=generation_limit, variant=variant)
        if variant in {"3", "4"}:
            store_top_pick_for_date(predictions_by_date, variant=variant)
        else:
            target_key = match_date.strftime("%Y-%m-%d")
            if target_key in predictions_by_date:
                store_top_pick_for_date({target_key: predictions_by_date[target_key]}, variant=variant)
        if variant in {"3", "4"}:
            picks_qs = TopPick.objects.filter(variant=variant, match_date__gte=today).order_by("-confidence", "match_date")
        else:
            picks_qs = TopPick.objects.filter(match_date=match_date, variant=variant)
        pick_keys = list(
            picks_qs.values(
                "home_team",
                "away_team",
                "match_date",
            )
        )

    match_key_to_competition = {}
    match_key_to_prediction = {}
    if pick_keys:
        candidate_dates = sorted({p.get("match_date") for p in pick_keys if p.get("match_date")})
        prediction_rows = MatchPrediction.objects.filter(
            match_date__in=candidate_dates,
        ).select_related("odds")
        prediction_rows_by_date = _build_prediction_lookup(prediction_rows)

        for pick in pick_keys:
            competition_code = None
            matched_prediction = _match_prediction_from_lookup(
                prediction_rows_by_date,
                pick["match_date"],
                pick["home_team"],
                pick["away_team"],
            )
            if matched_prediction:
                competition_code = matched_prediction.competition
            key = (pick["match_date"], pick["home_team"], pick["away_team"])
            match_key_to_competition[key] = competition_code
            match_key_to_prediction[key] = matched_prediction

        for competition_code, refresh_date in sorted({
            (competition_code, pick_date)
            for (pick_date, _, _), competition_code in match_key_to_competition.items()
            if competition_code and pick_date
        }):
            refresh_prediction_statuses(competition_code, refresh_date)

    update_actuals_for_top_picks(picks_qs)

    if variant == "3":
        picks_qs = picks_qs[:10]
    elif variant == "4":
        picks_qs = picks_qs[:20]

    picks = list(
        picks_qs.values(
            "home_team",
            "away_team",
            "tip",
            "actual_tip",
            "is_correct",
            "confidence",
            "odds",
            "match_date",
        )
    )

    if label_filter:
        picks = [p for p in picks if p.get("tip") == label_filter]

    enriched_picks = []
    top_pick_model_bundles = {}
    for pick in picks:
        raw_home_team = pick.get("home_team")
        raw_away_team = pick.get("away_team")
        pick_key = (pick.get("match_date"), raw_home_team, raw_away_team)
        competition_code = match_key_to_competition.get(pick_key)
        matched_prediction = match_key_to_prediction.get(pick_key)
        competition_name = normalize_display_competition_name(
            competitions.get(competition_code, competition_code),
            code=competition_code,
        )
        metadata_home_name = matched_prediction.home_team if matched_prediction else raw_home_team
        metadata_away_name = matched_prediction.away_team if matched_prediction else raw_away_team
        meta_home = get_team_metadata(metadata_home_name)
        meta_away = get_team_metadata(metadata_away_name)
        home_name = normalize_display_team_name(
            meta_home.get("shortName"),
            fallback=raw_home_team,
            max_length=24,
        )
        away_name = normalize_display_team_name(
            meta_away.get("shortName"),
            fallback=raw_away_team,
            max_length=24,
        )
        odds_value = pick.get("odds")
        if odds_value is None and matched_prediction:
            prediction = matched_prediction
            if prediction:
                try:
                    odds_obj = prediction.odds
                except MatchOdds.DoesNotExist:
                    odds_obj = None
                if odds_obj:
                    if pick.get("tip") == "1":
                        odds_value = odds_obj.home_win
                    elif pick.get("tip") == "2":
                        odds_value = odds_obj.away_win
                    elif pick.get("tip") == "X":
                        odds_value = odds_obj.draw
                    elif pick.get("tip") == "Over 2.5":
                        odds_value = odds_obj.over_2_5
                    elif pick.get("tip") == "Under 2.5":
                        odds_value = odds_obj.under_2_5
                    elif pick.get("tip") == "GG":
                        odds_value = odds_obj.btts_yes
                    elif pick.get("tip") == "NG":
                        odds_value = odds_obj.btts_no
        enriched_pick = dict(pick)
        enriched_pick["home_team"] = home_name
        enriched_pick["away_team"] = away_name
        enriched_pick["fixture"] = f"{home_name} vs {away_name}"
        enriched_pick["home_logo"] = meta_home.get("crest")
        enriched_pick["away_logo"] = meta_away.get("crest")
        enriched_pick["home_initials"] = team_initials(home_name)
        enriched_pick["away_initials"] = team_initials(away_name)
        enriched_pick["competition"] = competition_name
        enriched_pick["competition_code"] = competition_code
        enriched_pick["competition_logo"] = (
            static(f"logos/{competition_code}.png") if competition_code in competitions else None
        )
        enriched_pick["odds"] = odds_value
        enriched_pick["implied_probability"] = None
        enriched_pick["edge"] = None
        enriched_pick["reasons"] = []
        enriched_pick["actual_score"] = None
        if matched_prediction and competition_code:
            if competition_code not in top_pick_model_bundles:
                top_pick_model_bundles[competition_code] = get_or_train_model_bundle(competition_code)
            bundle = top_pick_model_bundles.get(competition_code)
            model_context = bundle[2] if bundle and len(bundle) == 3 else {}
            _, feature_snapshot = score_top_pick_markets(matched_prediction, model_context)
            implied_probability, edge = market_edge(pick.get("confidence"), odds_value)
            enriched_pick["implied_probability"] = implied_probability
            enriched_pick["edge"] = edge
            enriched_pick["reasons"] = explain_pick_reasons(
                pick.get("tip"),
                feature_snapshot,
                matched_prediction,
            )
            if matched_prediction.actual_home_goals is not None and matched_prediction.actual_away_goals is not None:
                enriched_pick["actual_score"] = f"{matched_prediction.actual_home_goals} - {matched_prediction.actual_away_goals}"
        enriched_pick["match_time"] = get_cached_kickoff_time(
            competition_code,
            pick.get("match_date"),
            metadata_home_name,
            metadata_away_name,
        )
        enriched_pick["detail_url"] = build_match_detail_url(matched_prediction, source="top_picks") if matched_prediction else None
        enriched_picks.append(enriched_pick)

    paginator = Paginator(enriched_picks, 10)
    page_number = request.GET.get("page")
    paginated_picks = paginator.get_page(page_number)
    visible_picks = list(paginated_picks.object_list)
    priced_legs = [pick for pick in visible_picks if pick.get("odds")]
    accumulator_summary = None
    if visible_picks:
        combined_odds = 1.0
        for pick in priced_legs:
            combined_odds *= float(pick["odds"])
        accumulator_summary = {
            "legs": len(visible_picks),
            "priced_legs": len(priced_legs),
            "combined_odds": round(combined_odds, 2) if priced_legs else None,
            "average_confidence": round(
                sum(float(pick.get("confidence") or 0) for pick in visible_picks) / len(visible_picks),
                1,
            ),
            "fully_priced": len(priced_legs) == len(visible_picks),
        }

    selected_code = request.GET.get("competition", "PL")
    league_table = get_league_table(selected_code)
    return render(request, "predict/top_picks.html", {
        "prediction": paginated_picks,
        "page_obj": paginated_picks,
        "filter_label": label_filter,
        "source": source,
        "selected_date": match_date,
        "show_past": show_past,
        "prediction_count_for_date": prediction_count_for_date,
        "can_generate_top_picks": can_generate_top_picks,
        "variant": variant,
        "header_label": "Mshipi" if variant == "4" else ("All Upcoming Dates" if variant == "3" else match_date.strftime("%B %d, %Y")),
        "accumulator_summary": accumulator_summary,
        "slip_summary": build_top_pick_slip_summary(),
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
    })


@require_GET
def won_slips_view(request):
    selected_variant = request.GET.get("variant")
    selected_variant = selected_variant if selected_variant in {"1", "2", "3", "4"} else ""
    won_slips = build_won_slip_groups(selected_variant or None)
    selected_code = request.GET.get("competition", "PL")
    league_table = get_league_table(selected_code)

    return render(request, "predict/won_slips.html", {
        "won_slips": won_slips,
        "selected_variant": selected_variant,
        "variant_options": [
            ("", "All Selections"),
            ("1", "Sure 1"),
            ("2", "Sure 2"),
            ("3", "Running Bet"),
            ("4", "Mshipi"),
        ],
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
    })


@require_GET
def export_won_slips_pdf(request):
    selected_variant = request.GET.get("variant")
    selected_variant = selected_variant if selected_variant in {"1", "2", "3", "4"} else ""
    won_slips = build_won_slip_groups(selected_variant or None)

    response = HttpResponse(content_type="application/pdf")
    suffix = selected_variant or "all"
    response["Content-Disposition"] = f'attachment; filename="won_slips_{suffix}.pdf"'

    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph("Won Slips", styles["Heading2"]), Spacer(1, 5 * mm)]

    if not won_slips:
        story.append(Paragraph("No won slips found for the selected selection yet.", styles["BodyText"]))
    else:
        for slip in won_slips:
            summary = f"{slip['variant_label']} - {slip['match_date']} - {slip['legs_count']} legs"
            if slip.get("combined_odds"):
                summary += f" - {slip['combined_odds']:.2f} combined odds"
            story.append(Paragraph(summary, styles["Heading4"]))
            table_data = [["Fixture", "Tip", "FT", "Odds"]]
            for leg in slip["legs"]:
                table_data.append([
                    f"{leg.home_team} vs {leg.away_team}",
                    leg.tip,
                    leg.actual_score or "-",
                    "-" if leg.odds is None else f"{leg.odds:.2f}",
                ])
            table = Table(table_data, repeatRows=1, colWidths=[100 * mm, 28 * mm, 24 * mm, 22 * mm])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.extend([table, Spacer(1, 5 * mm)])

    doc.build(story)
    response.write(buffer.getvalue())
    return response


@require_GET
def combo_builder_view(request):
    try:
        size = int(request.GET.get("size", 5))
    except (TypeError, ValueError):
        size = 5
    market_filter = request.GET.get("market", "")
    style = request.GET.get("style", "safe")

    context = build_combo_builder_context(size=size, market_filter=market_filter, style=style)
    context["tracked_slip"] = auto_track_combo_slip(context, size=size, market_filter=market_filter, style=style)
    context["saved_slips"] = recent_saved_combo_slips()
    context["combo_tracking_summary"] = combo_slip_tracking_summary()
    selected_code = request.GET.get("competition", "PL")
    context["league_table"] = get_league_table(selected_code)
    context["competitions"] = competitions
    context["selected_competition"] = selected_code
    return render(request, "predict/combo_builder.html", context)


def get_market_groups():
    return {
        "all": {
            "label": "All Markets",
            "description": "Every supported market in one place.",
            "markets": [
                "1", "2", "X", "GG", "NG", "Over 2.5", "Under 2.5",
                "Any Team Over 1.5", "Home Win Either Half", "Away Win Either Half",
                "Home Team Over 1.0", "Away Team Over 1.0",
            ],
        },
        "results": {
            "label": "1X2",
            "description": "Straight match result picks.",
            "markets": ["1", "X", "2"],
        },
        "goals": {
            "label": "Goals",
            "description": "Totals and high-scoring team angles.",
            "markets": ["Over 2.5", "Under 2.5", "Any Team Over 1.5"],
        },
        "btts": {
            "label": "BTTS",
            "description": "Both teams to score or not to score.",
            "markets": ["GG", "NG"],
        },
        "team_goals": {
            "label": "Team Goals",
            "description": "Home and away team goal lines.",
            "markets": ["Home Team Over 1.0", "Away Team Over 1.0"],
        },
        "halves": {
            "label": "Either Half",
            "description": "Team to win at least one half.",
            "markets": ["Home Win Either Half", "Away Win Either Half"],
        },
    }


def get_market_scopes():
    return {
        "today": {"label": "Today"},
        "tomorrow": {"label": "Tomorrow"},
        "weekend": {"label": "Weekend"},
        "all": {"label": "All Upcoming"},
    }


def get_market_sort_options():
    return {
        "confidence": {"label": "Highest Confidence"},
        "edge": {"label": "Best Edge"},
        "kickoff": {"label": "Earliest Kickoff"},
    }


def _market_pick_time_sort_value(row):
    match_time = row.get("match_time")
    if not match_time:
        return "99:99"
    return str(match_time)


def build_market_pick_rows(group_key=None, market_name=None, scope_key=None, sort_key=None, limit_key=None, priced_only=False):
    market_groups = get_market_groups()
    market_scopes = get_market_scopes()
    market_sort_options = get_market_sort_options()
    all_markets = market_groups["all"]["markets"]
    selected_group = group_key or "goals"
    if selected_group not in market_groups:
        selected_group = "goals"
    selected_scope = scope_key or "all"
    if selected_scope not in market_scopes:
        selected_scope = "all"
    selected_sort = sort_key or "confidence"
    if selected_sort not in market_sort_options:
        selected_sort = "confidence"
    selected_limit = limit_key or "20"
    if selected_limit not in {"20", "50", "all"}:
        selected_limit = "20"
    group_markets = market_groups[selected_group]["markets"]
    selected_market = market_name or group_markets[0]
    if selected_market not in all_markets or selected_market not in group_markets:
        selected_market = group_markets[0]

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    weekend_start = today + timedelta(days=max(0, 5 - today.weekday()))
    weekend_end = weekend_start + timedelta(days=1)

    predictions = MatchPrediction.objects.select_related("odds").filter(match_date__gte=today)
    if selected_scope == "today":
        predictions = predictions.filter(match_date=today)
    elif selected_scope == "tomorrow":
        predictions = predictions.filter(match_date=tomorrow)
    elif selected_scope == "weekend":
        predictions = predictions.filter(match_date__range=(weekend_start, weekend_end))
    predictions = predictions.order_by("match_date", "competition", "home_team")
    model_bundles = {}
    rows = []
    category_summary = {
        key: {"count": 0, "confidence_total": 0.0}
        for key in market_groups
        if key != "all"
    }

    refresh_pairs = list(predictions.values_list("competition", "match_date").distinct())
    for competition_code, refresh_date in refresh_pairs:
        if competition_code and refresh_date:
            refresh_prediction_statuses(competition_code, refresh_date)

    for prediction in predictions:
        competition_code = prediction.competition
        if competition_code not in model_bundles:
            model_bundles[competition_code] = get_or_train_model_bundle(competition_code)
        bundle = model_bundles.get(competition_code)
        model_context = bundle[2] if bundle and len(bundle) == 3 else {}
        ranked_markets, feature_snapshot = score_top_pick_markets(prediction, model_context)
        ranked_map = {market: float(confidence) for market, confidence in ranked_markets}
        for category_key, category in market_groups.items():
            if category_key == "all":
                continue
            available = [
                ranked_map[market_name]
                for market_name in category["markets"]
                if market_name in ranked_map
            ]
            if available:
                category_summary[category_key]["count"] += 1
                category_summary[category_key]["confidence_total"] += max(available)
        target_market = next(((market, confidence) for market, confidence in ranked_markets if market == selected_market), None)
        if not target_market:
            continue

        confidence = float(target_market[1])
        try:
            odds_obj = prediction.odds
        except MatchOdds.DoesNotExist:
            odds_obj = None
        odds_value = _market_odds_value(odds_obj, selected_market)
        implied_probability, edge = market_edge(confidence, odds_value)

        actual_tip = None
        is_correct = None
        actual_score = None
        if prediction.actual_home_goals is not None and prediction.actual_away_goals is not None:
            home_g = prediction.actual_home_goals
            away_g = prediction.actual_away_goals
            ht_home_g = prediction.actual_ht_home_goals
            ht_away_g = prediction.actual_ht_away_goals
            actual_score = f"{home_g} - {away_g}"
            result_tip = "1" if home_g > away_g else "2" if home_g < away_g else "X"
            gg = home_g >= 1 and away_g >= 1
            over_2_5 = (home_g + away_g) > 2.5
            under_2_5 = not over_2_5
            nogg = not gg
            any_team_over_1_5 = max(home_g, away_g) >= 2
            home_team_over_1_0 = home_g >= 2
            away_team_over_1_0 = away_g >= 2
            home_team_over_1_0_push = home_g == 1
            away_team_over_1_0_push = away_g == 1
            home_second_half = (home_g - ht_home_g) if ht_home_g is not None else None
            away_second_half = (away_g - ht_away_g) if ht_away_g is not None else None
            home_win_either_half = (
                (ht_home_g is not None and ht_away_g is not None and ht_home_g > ht_away_g)
                or (home_second_half is not None and away_second_half is not None and home_second_half > away_second_half)
            )
            away_win_either_half = (
                (ht_home_g is not None and ht_away_g is not None and ht_away_g > ht_home_g)
                or (home_second_half is not None and away_second_half is not None and away_second_half > home_second_half)
            )

            if selected_market == "GG" and gg:
                actual_tip = "GG"
            elif selected_market == "NG" and nogg:
                actual_tip = "NG"
            elif selected_market == "Over 2.5" and over_2_5:
                actual_tip = "Over 2.5"
            elif selected_market == "Under 2.5" and under_2_5:
                actual_tip = "Under 2.5"
            elif selected_market == "Any Team Over 1.5" and any_team_over_1_5:
                actual_tip = "Any Team Over 1.5"
            elif selected_market == "Home Win Either Half" and home_win_either_half:
                actual_tip = "Home Win Either Half"
            elif selected_market == "Away Win Either Half" and away_win_either_half:
                actual_tip = "Away Win Either Half"
            elif selected_market == "Home Team Over 1.0" and home_team_over_1_0:
                actual_tip = "Home Team Over 1.0"
            elif selected_market == "Away Team Over 1.0" and away_team_over_1_0:
                actual_tip = "Away Team Over 1.0"
            elif selected_market == "Home Team Over 1.0" and home_team_over_1_0_push:
                actual_tip = "Refund"
            elif selected_market == "Away Team Over 1.0" and away_team_over_1_0_push:
                actual_tip = "Refund"
            else:
                actual_tip = result_tip
            is_correct = None if actual_tip == "Refund" else (selected_market == actual_tip)

        meta_home = get_team_metadata(prediction.home_team)
        meta_away = get_team_metadata(prediction.away_team)
        rows.append({
            "match_date": prediction.match_date,
            "match_time": get_cached_kickoff_time(prediction.competition, prediction.match_date, prediction.home_team, prediction.away_team),
            "competition": normalize_display_competition_name(
                competitions.get(prediction.competition, prediction.competition),
                code=prediction.competition,
            ),
            "competition_logo": static(f"logos/{prediction.competition}.png") if prediction.competition in competitions else None,
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=prediction.home_team, max_length=24),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=prediction.away_team, max_length=24),
            "home_logo": meta_home.get("crest"),
            "away_logo": meta_away.get("crest"),
            "tip": selected_market,
            "confidence": confidence,
            "odds": odds_value,
            "implied_probability": implied_probability,
            "edge": edge,
            "actual_score": actual_score,
            "actual_tip": actual_tip,
            "is_correct": is_correct,
            "reasons": explain_pick_reasons(selected_market, feature_snapshot, prediction),
            "detail_url": build_match_detail_url(prediction, source="predictions"),
        })

    if priced_only:
        rows = [item for item in rows if item.get("odds") is not None]

    if selected_sort == "edge":
        rows.sort(
            key=lambda item: (
                item.get("edge") is None,
                -(item.get("edge") or float("-inf")),
                item["match_date"],
                -item["confidence"],
            )
        )
    elif selected_sort == "kickoff":
        rows.sort(
            key=lambda item: (
                item["match_date"],
                _market_pick_time_sort_value(item),
                -item["confidence"],
            )
        )
    else:
        rows.sort(key=lambda item: (item["match_date"], -item["confidence"]))

    total_count = len(rows)
    if selected_limit != "all":
        rows = rows[:int(selected_limit)]

    return selected_group, selected_market, selected_scope, selected_sort, selected_limit, total_count, [
        {"key": key, "label": data["label"]}
        for key, data in market_groups.items()
        if key != "all"
    ], group_markets, [
        {"key": key, "label": data["label"]}
        for key, data in market_scopes.items()
    ], [
        {"key": key, "label": data["label"]}
        for key, data in market_sort_options.items()
    ], rows, category_summary


def market_picks_view(request):
    priced_only = request.GET.get("priced") == "1"
    selected_group, selected_market, selected_scope, selected_sort, selected_limit, total_count, market_groups, market_options, market_scopes, market_sort_options, rows, category_summary = build_market_pick_rows(
        request.GET.get("group"),
        request.GET.get("market"),
        request.GET.get("scope"),
        request.GET.get("sort"),
        request.GET.get("limit"),
        priced_only,
    )
    selected_view = request.GET.get("view", "card")
    if selected_view not in {"card", "table"}:
        selected_view = "card"
    priced_rows = [row for row in rows if row.get("odds") is not None]
    view_summary = {
        "count": len(rows),
        "total_count": total_count,
        "priced": len(priced_rows),
        "avg_confidence": round(
            sum(float(row.get("confidence") or 0) for row in rows) / len(rows),
            1,
        ) if rows else None,
        "avg_edge": round(
            sum(float(row.get("edge") or 0) for row in rows if row.get("edge") is not None) /
            max(1, sum(1 for row in rows if row.get("edge") is not None)),
            1,
        ) if any(row.get("edge") is not None for row in rows) else None,
    }
    priced_only_count = len(priced_rows)

    selected_code = request.GET.get("competition", "PL")
    league_table = get_league_table(selected_code)
    return render(request, "predict/market_picks.html", {
        "predictions": rows,
        "selected_market": selected_market,
        "selected_group": selected_group,
        "selected_scope": selected_scope,
        "selected_sort": selected_sort,
        "selected_limit": selected_limit,
        "priced_only": priced_only,
        "selected_view": selected_view,
        "market_groups": market_groups,
        "market_scopes": market_scopes,
        "market_sort_options": market_sort_options,
        "market_limit_options": [
            {"key": "20", "label": "Top 20"},
            {"key": "50", "label": "Top 50"},
            {"key": "all", "label": "All"},
        ],
        "active_filter_chips": [
            next((group["label"] for group in market_groups if group["key"] == selected_group), selected_group.title()),
            next((scope["label"] for scope in market_scopes if scope["key"] == selected_scope), selected_scope.title()),
            selected_market,
            next((sort_option["label"] for sort_option in market_sort_options if sort_option["key"] == selected_sort), selected_sort.title()),
            next((limit_option["label"] for limit_option in [
                {"key": "20", "label": "Top 20"},
                {"key": "50", "label": "Top 50"},
                {"key": "all", "label": "All"},
            ] if limit_option["key"] == selected_limit), selected_limit.upper()),
        ] + (["Priced Only"] if priced_only else []),
        "market_options": market_options,
        "view_summary": view_summary,
        "priced_only_count": priced_only_count,
        "market_category_cards": [
            {
                "key": key,
                "label": data["label"],
                "description": data["description"],
                "markets": data["markets"],
                "default_market": data["markets"][0],
                "pick_count": category_summary[key]["count"],
                "avg_confidence": round(category_summary[key]["confidence_total"] / category_summary[key]["count"], 1)
                if category_summary[key]["count"] else None,
            }
            for key, data in get_market_groups().items()
            if key != "all"
        ],
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
    })


@require_POST
def save_combo_slip_view(request):
    try:
        size = int(request.POST.get("size", 5))
    except (TypeError, ValueError):
        size = 5
    market_filter = request.POST.get("market", "")
    style = request.POST.get("style", "safe")
    context = build_combo_builder_context(size=size, market_filter=market_filter, style=style)
    combo_rows = context.get("combo_rows") or []
    summary = context.get("summary")

    if not combo_rows or not summary:
        messages.error(request, "No combo is available to save for the current selection.")
        return redirect(f"{reverse('combo_builder')}?size={size}&market={quote(market_filter)}&style={style}")

    slip_name = (request.POST.get("name") or "").strip()
    if not slip_name:
        market_label = market_filter or "Mixed"
        slip_name = f"{style.title()} {size}-Leg {market_label}"

    slip = ComboSlip.objects.create(
        name=slip_name,
        size=summary["legs"],
        market_filter=market_filter,
        style=style,
        combined_odds=summary.get("combined_odds"),
        average_confidence=summary.get("average_confidence") or 0,
        priced_legs=summary.get("priced_legs") or 0,
        auto_generated=False,
        signature=None,
    )
    ComboSlipLeg.objects.bulk_create([
        ComboSlipLeg(
            slip=slip,
            match_date=row["match_date"],
            competition=row["competition"],
            home_team=row["home_team"],
            away_team=row["away_team"],
            tip=row["tip"],
            confidence=row["confidence"],
            odds=row.get("odds"),
        )
        for row in combo_rows
    ])

    messages.success(request, f'Saved combo slip "{slip.name}".')
    return redirect(f"{reverse('combo_builder')}?size={size}&market={quote(market_filter)}&style={style}")


@require_POST
def generate_all_combo_slips_view(request):
    stats = generate_all_combo_slips()
    messages.success(
        request,
        f"Tracked combos refreshed. Created {stats['created']}, already tracked {stats['existing']}, skipped {stats['skipped']}.",
    )
    return redirect(reverse("combo_builder"))


@require_POST
def regenerate_top_picks(request):
    match_date_str = request.POST.get("match_date")
    variant = request.POST.get("variant", "1")
    variant = variant if variant in {"1", "2", "3", "4"} else "1"

    if match_date_str:
        try:
            match_date = date.fromisoformat(match_date_str)
        except ValueError:
            match_date = timezone.localdate()
    else:
        match_date = timezone.localdate()

    target_date = match_date.isoformat()
    if match_date < timezone.localdate():
        return redirect(f"{reverse('top_picks')}?match_date={target_date}&variant={variant}")

    if variant in {"3", "4"}:
        prediction_count = MatchPrediction.objects.filter(match_date__gte=timezone.localdate()).count()
    else:
        prediction_count = MatchPrediction.objects.filter(match_date=match_date).count()

    if prediction_count == 0:
        return redirect(f"{reverse('top_picks')}?match_date={target_date}&variant={variant}")

    generation_limit = 20 if variant == "4" else 10
    predictions_by_date = get_top_predictions(limit=generation_limit, variant=variant)
    if variant in {"3", "4"}:
        TopPick.objects.filter(variant=variant, match_date__gte=timezone.localdate()).delete()
        store_top_pick_for_date(predictions_by_date, variant=variant)
        return redirect(f"{reverse('top_picks')}?variant={variant}")

    if target_date in predictions_by_date:
        TopPick.objects.filter(match_date=match_date, variant=variant).delete()
        store_top_pick_for_date({target_date: predictions_by_date[target_date]}, variant=variant)
        return redirect(f"{reverse('top_picks')}?match_date={target_date}&variant={variant}")
    return redirect(f"{reverse('top_picks')}?match_date={target_date}&variant={variant}")


# -------------------------
# Remaining views (mostly unchanged) - results, training, admin dashboard, export, etc.
# -------------------------
@login_required
def admin_task_dashboard(request):
    tasks = PeriodicTask.objects.all()
    task_info = []
    for task in tasks:
        args = json.loads(task.args or "[]")
        kwargs = json.loads(task.kwargs or "{}")
        task_info.append({
            "name": task.name,
            "task": task.task,
            "task_label": format_task_label(task.task),
            "enabled": task.enabled,
            "last_run_at": task.last_run_at,
            "interval": task.interval,
            "crontab": task.crontab,
            "args": args,
            "kwargs": kwargs,
            "last_triggered": task.date_changed,
        })

    cache_info = []
    for comp in COMPETITIONS:
        key = f"training_data_{comp}"
        df = cache.get(key)
        cache_info.append({
            "competition": comp,
            "cached": df is not None,
            "entries": len(df) if df is not None else 0
        })

    return render(request, "predict/admin_dashboard.html", {
        "tasks": task_info,
        "cache_info": cache_info,
        "competitions": COMPETITIONS,
        "today": timezone.localdate(),
    })


@login_required
@require_POST
def trigger_task_now(request):
    task_path = request.POST.get("task_path", "").strip()
    allowed_tasks = set(
        PeriodicTask.objects.exclude(task__isnull=True).exclude(task="").values_list("task", flat=True)
    )
    if task_path not in allowed_tasks:
        return JsonResponse({"success": False, "message": "Task is not allowed."}, status=400)

    try:
        current_app.send_task(task_path)
        return JsonResponse({"success": True, "message": f"{task_path} triggered successfully."})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def refresh_cache_now(request):
    comp = request.POST.get("competition")
    if comp not in COMPETITIONS:
        return JsonResponse({"success": False, "message": "Invalid competition."}, status=400)

    df = fetch_training_data_all_seasons(comp)
    if not df.empty:
        cache.set(f"training_data_{comp}", df, timeout=60 * 60 * 24 * 7)
        return JsonResponse({"success": True, "message": f"Cache refreshed for {comp}"})
    return JsonResponse({"success": False, "message": f"No data fetched for {comp}."}, status=502)


@login_required
@require_POST
def clear_cache_now(request):
    comp = request.POST.get("competition")
    if comp not in COMPETITIONS:
        return JsonResponse({"success": False, "message": "Invalid competition."}, status=400)

    cache.delete(f"training_data_{comp}")
    return JsonResponse({"success": True, "message": f"Cache cleared for {comp}"})


def results_view(request):
    matches = MatchPrediction.objects.filter(status="FINISHED").order_by('-match_date')
    for match in matches:
        match.correct = (match.predicted_home_goals is not None and match.predicted_away_goals is not None and
                         ((match.predicted_home_goals > match.predicted_away_goals and match.actual_home_goals > match.actual_away_goals) or
                          (match.predicted_home_goals < match.predicted_away_goals and match.actual_home_goals < match.actual_away_goals) or
                          (match.predicted_home_goals == match.predicted_away_goals and match.actual_home_goals == match.actual_away_goals)))
    return render(request, "predict/results.html", {"matches": matches})


def train_model_view(request):
    message = ""
    if request.method == "POST":
        competition_code = request.POST.get("competition")
        if not competition_code:
            message = "Please select a competition."
        else:
            seasons = list(range(2019, datetime.now().year + 1))
            all_data = []
            for season in seasons:
                data = fetch_matches_by_season(API_KEY, competition_code, season)
                if data:
                    df = pd.DataFrame(data)
                    if not df.empty:
                        df["home_team"] = df["homeTeam"].apply(lambda x: x["name"])
                        df["away_team"] = df["awayTeam"].apply(lambda x: x["name"])
                        df["home_goals"] = df["score"].apply(lambda x: x["fullTime"]["home"])
                        df["away_goals"] = df["score"].apply(lambda x: x["fullTime"]["away"])
                        all_data.append(df[["home_team", "away_team", "home_goals", "away_goals"]])

            if all_data:
                final_df = pd.concat(all_data, ignore_index=True)
                X, y_home, y_away, label_encoder = preprocess_api_data(final_df)
                model_dict = train_models(X, y_home, y_away)
                cache.set(f"{competition_code}_models", (model_dict, label_encoder), timeout=604800)
                message = f"Model trained and cached for {competitions.get(competition_code, competition_code)}."
            else:
                message = "No data available to train the model."

    return render(request, "predict/train_model.html", {
        "competitions": competitions,
        "message": message
    })


def cached_models_status(request):
    status = {}
    for code, name in competitions.items():
        key = f"{code}_models"
        status[name] = cache.get(key) is not None
    return JsonResponse(status)


def suggest_match_date(request):
    comp = request.GET.get("competition")
    date_str = request.GET.get("date")
    api_key = os.getenv("FOOTBALL_DATA_API_KEY", API_KEY)

    if not comp or not date_str:
        return JsonResponse({"error": "Missing parameters"}, status=400)

    next_date, matches = find_next_available_match_date(api_key, comp, date_str)
    return JsonResponse({
        "next_available_date": next_date,
        "match_count": len(matches),
    })


def view_predictions(request):
    competition = request.GET.get("competition")
    date_q = request.GET.get("date")
    predictions = MatchPrediction.objects.all()

    if competition:
        predictions = predictions.filter(competition=competition)

    if date_q:
        predictions = predictions.filter(match_date=date_q)

    return render(request, "predict/view_predictions.html", {
        "predictions": predictions,
        "competition": competition,
        "date": date_q
    })


def view_cache_status(request):
    cache_status = []
    for comp in COMPETITIONS:
        key = f"training_data_{comp}"
        df = cache.get(key)
        if df is not None:
            cache_status.append({
                "competition": comp,
                "cached": True,
                "entries": len(df)
            })
        else:
            cache_status.append({
                "competition": comp,
                "cached": False,
                "entries": 0
            })
    return render(request, "predict/cache_status.html", {"cache_status": cache_status})


def safe_logo_name(team_name):
    return quote(f"{team_name}.png")


def competition_logo(code):
    return static(f"logos/{code}.png")


TEAM_LOGO_DIR = os.path.join("static", "logos")
TEAM_LOGO_FILES = [f for f in os.listdir(TEAM_LOGO_DIR) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]


def fuzzy_match_logo(team_name):
    normalized_team = team_name.lower().replace("fc", "").replace(".", "").strip()
    name_map = {}
    for file in TEAM_LOGO_FILES:
        file_base = file.lower().replace("fc", "").replace(".", "").replace(".png", "").replace(".jpg", "").replace(".jpeg", "").strip()
        name_map[file_base] = file
    close_matches = difflib.get_close_matches(normalized_team, name_map.keys(), n=1, cutoff=0.6)
    if close_matches:
        matched_base = close_matches[0]
        return name_map[matched_base]
    return "default.png"


def get_team_metadata(name):
    return cache.get(f"team_meta::{name}", {"shortName": name, "crest": None})


def normalize_display_team_name(name, fallback=None, max_length=14):
    candidate = (name or fallback or "").strip()
    if not candidate:
        return ""

    candidate = re.sub(r"\s+", " ", candidate.replace(".", " ")).strip()
    replacements = {
        "United": "Utd",
        "Rovers": "Rov",
        "Wanderers": "Wand",
        "Athletic": "Ath",
        "Atletico": "Atleti",
        "Hotspur": "Spurs",
        "Saint": "St",
        "Sankt": "St",
        "Santa": "Sta",
        "Borussia": "B.",
        "Sporting": "Sport",
        "Deportivo": "Dep.",
        "Internacional": "Inter",
    }
    drop_words = {"FC", "CF", "AC", "SC", "AFC", "CFC", "Club", "de", "da", "del", "the"}
    preserved_tokens = {"PSG", "AEK", "PAOK", "CFR", "HJK", "AIK", "IFK", "BSC", "VfB", "TSG", "PEC"}

    words = []
    for raw_word in candidate.split():
        raw_upper = raw_word.upper()
        if raw_upper in drop_words:
            continue
        if raw_word in preserved_tokens or raw_upper in preserved_tokens:
            words.append(raw_word if raw_word in preserved_tokens else raw_upper)
            continue
        normalized_word = raw_word.capitalize() if raw_word.islower() else raw_word
        words.append(replacements.get(normalized_word, normalized_word))

    compact = " ".join(words) if words else candidate
    if len(compact) <= max_length:
        return compact

    if len(words) >= 2:
        abbreviated = " ".join(
            [f"{word[0]}." for word in words[:-1] if word] + [words[-1]]
        )
        if len(abbreviated) <= max_length:
            return abbreviated

    return compact[: max_length - 1].rstrip() + "…"


def normalize_display_competition_name(name, code=None, max_length=12):
    candidate = (name or code or "").strip()
    if not candidate:
        return "Unknown"

    explicit_names = {
        "PL": "PL",
        "PD": "LL",
        "SA": "SA",
        "BL1": "BL",
        "FL1": "L1",
        "DED": "ERD",
        "PPL": "PPL",
        "ELC": "ELC",
        "CL": "UCL",
        "EC": "EURO",
        "BSA": "BSA",
        "CLI": "LIB",
        "WC": "WC",
    }
    if code in explicit_names:
        return explicit_names[code]

    cleanup_map = {
        "UEFA ": "",
        "FIFA ": "",
        "Champions League": "UCL",
        "Europa League": "UEL",
        "Conference League": "UECL",
        "World Cup": "WC",
        "European Championship": "EURO",
        "Copa Libertadores": "LIB",
        "Premier League": "PL",
        "La Liga": "LL",
        "Bundesliga": "BL",
        "Serie A": "SA",
        "Ligue 1": "L1",
        "Eredivisie": "ERD",
        "Championship": "ELC",
    }
    normalized = re.sub(r"\s+", " ", candidate).strip()
    for source_text, replacement in cleanup_map.items():
        normalized = normalized.replace(source_text, replacement)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"


def team_initials(name):
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", (name or "").strip())
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return "?"
    if len(tokens) == 1:
        return tokens[0][:2].upper()
    return f"{tokens[0][0]}{tokens[1][0]}".upper()


NAME_TO_CODE = {v.lower(): k for k, v in competitions.items()}


def format_task_label(task_path):
    if not task_path:
        return "Unknown Task"
    task_name = task_path.rsplit(".", 1)[-1]
    return re.sub(r"\s+", " ", task_name.replace("_", " ")).strip().title()


def build_match_detail_url(prediction, source=None):
    if not prediction:
        return None
    query = {
        "match_date": prediction.match_date.isoformat(),
        "competition": prediction.competition,
        "home_team": prediction.home_team,
        "away_team": prediction.away_team,
    }
    if source:
        query["from"] = source
    return f"{reverse('match_detail')}?{urlencode(query)}"


def _build_market_rows(prediction, feature_snapshot):
    try:
        odds_obj = prediction.odds
    except MatchOdds.DoesNotExist:
        odds_obj = None

    bundle = get_or_train_model_bundle(prediction.competition)
    model_context = bundle[2] if bundle and len(bundle) == 3 else {}
    ranked_markets, feature_snapshot = score_top_pick_markets(prediction, model_context)

    market_rows = []
    for market, confidence in ranked_markets:
        odds_value = None
        market_source = None
        if odds_obj:
            if market == "1":
                odds_value = odds_obj.home_win
                market_source = (odds_obj.market_sources or {}).get("home")
            elif market == "2":
                odds_value = odds_obj.away_win
                market_source = (odds_obj.market_sources or {}).get("away")
            elif market == "X":
                odds_value = odds_obj.draw
                market_source = (odds_obj.market_sources or {}).get("draw")
            elif market == "Over 2.5":
                odds_value = odds_obj.over_2_5
                market_source = (odds_obj.market_sources or {}).get("over25")
            elif market == "Under 2.5":
                odds_value = odds_obj.under_2_5
                market_source = (odds_obj.market_sources or {}).get("under25")
            elif market == "GG":
                odds_value = odds_obj.btts_yes
                market_source = (odds_obj.market_sources or {}).get("btts_yes")
            elif market == "NG":
                odds_value = odds_obj.btts_no
                market_source = (odds_obj.market_sources or {}).get("btts_no")

        implied_probability, edge = market_edge(confidence, odds_value)
        market_rows.append({
            "market": market,
            "confidence": confidence,
            "odds": odds_value,
            "market_source": market_source,
            "implied_probability": implied_probability,
            "edge": edge,
            "reasons": explain_pick_reasons(market, feature_snapshot, prediction),
        })

    return market_rows, feature_snapshot


def build_match_detail_context(prediction, source=None):
    meta_home = get_team_metadata(prediction.home_team)
    meta_away = get_team_metadata(prediction.away_team)
    home_name = normalize_display_team_name(meta_home.get("shortName"), fallback=prediction.home_team, max_length=28)
    away_name = normalize_display_team_name(meta_away.get("shortName"), fallback=prediction.away_team, max_length=28)

    try:
        odds_obj = prediction.odds
    except MatchOdds.DoesNotExist:
        odds_obj = None

    market_rows, feature_snapshot = _build_market_rows(prediction, {})
    scorelines = scoreline_predictions(prediction.predicted_home_goals, prediction.predicted_away_goals)

    _, scorer_rows = build_anytime_scorer_rows(prediction.match_date.isoformat())
    scorer_row = next(
        (
            row for row in scorer_rows
            if _team_name_aliases(row["home_team"]) & _team_name_aliases(home_name)
            and _team_name_aliases(row["away_team"]) & _team_name_aliases(away_name)
        ),
        None,
    )

    actual_result = None
    if prediction.actual_home_goals is not None and prediction.actual_away_goals is not None:
        actual_result = f"{prediction.actual_home_goals} - {prediction.actual_away_goals}"

    return {
        "prediction": prediction,
        "source": source,
        "detail_url": build_match_detail_url(prediction, source=source),
        "competition_name": normalize_display_competition_name(
            competitions.get(prediction.competition, prediction.competition),
            code=prediction.competition,
        ),
        "competition_logo": static(f"logos/{prediction.competition}.png") if prediction.competition in competitions else None,
        "home_team": home_name,
        "away_team": away_name,
        "home_logo": meta_home.get("crest"),
        "away_logo": meta_away.get("crest"),
        "match_time": get_cached_kickoff_time(
            prediction.competition,
            prediction.match_date,
            prediction.home_team,
            prediction.away_team,
        ),
        "bookmaker": getattr(odds_obj, "bookmaker", None) if odds_obj else None,
        "predicted_score": f"{prediction.predicted_home_goals} - {prediction.predicted_away_goals}",
        "actual_result": actual_result,
        "market_rows": market_rows[:6],
        "scorelines": scorelines,
        "top_score": scorelines[0] if scorelines else None,
        "scorer_row": scorer_row,
        "feature_snapshot": {
            "form_gap": round(float(feature_snapshot.get("form_gap", 0.0)), 2),
            "elo_gap": round(float(feature_snapshot.get("elo_gap", 0.0)), 1),
            "h2h_total_goals": round(float(feature_snapshot.get("h2h_total_goals", 0.0)), 2),
            "h2h_match_count": int(float(feature_snapshot.get("h2h_match_count", 0.0))),
            "home_recent_scored": round(float(feature_snapshot.get("home_recent_scored", 0.0)), 2),
            "away_recent_scored": round(float(feature_snapshot.get("away_recent_scored", 0.0)), 2),
            "home_recent_conceded": round(float(feature_snapshot.get("home_recent_conceded", 0.0)), 2),
            "away_recent_conceded": round(float(feature_snapshot.get("away_recent_conceded", 0.0)), 2),
            "home_clean_sheet_rate": round(float(feature_snapshot.get("home_clean_sheet_rate", 0.0)) * 100, 1),
            "away_clean_sheet_rate": round(float(feature_snapshot.get("away_clean_sheet_rate", 0.0)) * 100, 1),
            "home_fail_to_score_rate": round(float(feature_snapshot.get("home_fail_to_score_rate", 0.0)) * 100, 1),
            "away_fail_to_score_rate": round(float(feature_snapshot.get("away_fail_to_score_rate", 0.0)) * 100, 1),
        },
    }


def _clear_combo_cache():
    for key in (
        "recent_saved_combo_slips_v2::8",
        "recent_saved_combo_slips_v2::500",
        "combo_slip_tracking_summary_v1",
    ):
        cache.delete(key)


def _build_prediction_lookup(prediction_rows):
    rows_by_date = defaultdict(list)
    for prediction_row in prediction_rows:
        rows_by_date[prediction_row.match_date].append((
            _team_name_aliases(prediction_row.home_team),
            _team_name_aliases(prediction_row.away_team),
            prediction_row,
        ))
    return rows_by_date


def _match_prediction_from_lookup(rows_by_date, match_date, home_team, away_team):
    home_aliases = _team_name_aliases(home_team)
    away_aliases = _team_name_aliases(away_team)
    if not home_aliases or not away_aliases:
        return None

    for row_home_aliases, row_away_aliases, prediction_row in rows_by_date.get(match_date, []):
        if row_home_aliases & home_aliases and row_away_aliases & away_aliases:
            return prediction_row
    return None


def build_top_pick_slip_summary():
    cache_key = "top_pick_slip_summary_v1"
    cached_summary = cache.get(cache_key)
    if cached_summary is not None:
        return cached_summary

    variant_labels = {
        "1": "Sure 1",
        "2": "Sure 2",
        "3": "Running Bet",
        "4": "Mshipi",
    }
    grouped_rows = defaultdict(list)
    for row in TopPick.objects.values("variant", "match_date", "actual_tip", "is_correct"):
        grouped_rows[(row["variant"], row["match_date"])].append(row)

    summary = {
        variant: {
            "variant": variant,
            "label": label,
            "won": 0,
            "settled": 0,
            "total": 0,
        }
        for variant, label in variant_labels.items()
    }

    for (variant, _match_date), rows in grouped_rows.items():
        if variant not in summary:
            continue
        summary[variant]["total"] += 1
        if all(row.get("actual_tip") is not None for row in rows):
            summary[variant]["settled"] += 1
            if all(bool(row.get("is_correct")) for row in rows):
                summary[variant]["won"] += 1

    result = [summary[key] for key in ("1", "2", "3", "4")]
    cache.set(cache_key, result, timeout=300)
    return result


def build_won_slip_groups(selected_variant=None):
    variant_labels = {
        "1": "Sure 1",
        "2": "Sure 2",
        "3": "Running Bet",
        "4": "Mshipi",
    }
    slips = defaultdict(list)
    qs = TopPick.objects.all().order_by("-match_date", "variant", "home_team")
    if selected_variant in variant_labels:
        qs = qs.filter(variant=selected_variant)

    pick_dates = set(qs.values_list("match_date", flat=True))
    prediction_rows = MatchPrediction.objects.filter(match_date__in=pick_dates)
    prediction_rows_by_date = _build_prediction_lookup(prediction_rows)

    for pick in qs:
        matched_prediction = _match_prediction_from_lookup(
            prediction_rows_by_date,
            pick.match_date,
            pick.home_team,
            pick.away_team,
        )
        pick.actual_score = None
        metadata_home_name = matched_prediction.home_team if matched_prediction else pick.home_team
        metadata_away_name = matched_prediction.away_team if matched_prediction else pick.away_team
        meta_home = get_team_metadata(metadata_home_name)
        meta_away = get_team_metadata(metadata_away_name)
        pick.display_home_team = normalize_display_team_name(
            meta_home.get("shortName"),
            fallback=pick.home_team,
            max_length=24,
        )
        pick.display_away_team = normalize_display_team_name(
            meta_away.get("shortName"),
            fallback=pick.away_team,
            max_length=24,
        )
        pick.home_logo = meta_home.get("crest")
        pick.away_logo = meta_away.get("crest")
        pick.home_initials = team_initials(pick.display_home_team)
        pick.away_initials = team_initials(pick.display_away_team)
        if matched_prediction and matched_prediction.actual_home_goals is not None and matched_prediction.actual_away_goals is not None:
            pick.actual_score = f"{matched_prediction.actual_home_goals} - {matched_prediction.actual_away_goals}"
        slips[(pick.variant, pick.match_date)].append(pick)

    groups = []
    for (variant, match_date), picks in slips.items():
        if not picks:
            continue
        if not all(p.actual_tip is not None for p in picks):
            continue
        if not all(bool(p.is_correct) for p in picks):
            continue

        combined_odds = 1.0
        priced_legs = 0
        for pick in picks:
            if pick.odds:
                combined_odds *= float(pick.odds)
                priced_legs += 1

        groups.append({
            "variant": variant,
            "variant_label": variant_labels.get(variant, variant),
            "match_date": match_date,
            "legs": picks,
            "legs_count": len(picks),
            "priced_legs": priced_legs,
            "combined_odds": round(combined_odds, 2) if priced_legs else None,
        })

    return sorted(groups, key=lambda item: (item["match_date"], item["variant"]), reverse=True)


def build_combo_builder_context(size=5, market_filter="", style="safe"):
    today = timezone.localdate()
    size = size if size in {3, 5, 10} else 5
    style = style if style in {"safe", "value"} else "safe"

    predictions = MatchPrediction.objects.select_related("odds").filter(match_date__gte=today).order_by("match_date", "competition", "home_team")
    model_bundles = {}
    candidates = []

    for prediction in predictions:
        competition_code = prediction.competition
        if competition_code not in model_bundles:
            model_bundles[competition_code] = get_or_train_model_bundle(competition_code)
        bundle = model_bundles.get(competition_code)
        model_context = bundle[2] if bundle and len(bundle) == 3 else {}
        ranked_markets, feature_snapshot = score_top_pick_markets(prediction, model_context)
        if not ranked_markets:
            continue

        try:
            odds_obj = prediction.odds
        except MatchOdds.DoesNotExist:
            odds_obj = None

        ranked_slice = ranked_markets[: (4 if style == "value" or market_filter else 2)]
        for rank_index, (market, confidence) in enumerate(ranked_slice):
            if market_filter and market != market_filter:
                continue
            odds_value = _market_odds_value(odds_obj, market)
            implied_probability, edge = market_edge(confidence, odds_value)
            candidates.append({
                "prediction": prediction,
                "market": market,
                "confidence": float(confidence),
                "odds": odds_value,
                "edge": edge if edge is not None else -999.0,
                "rank_index": rank_index,
                "reasons": explain_pick_reasons(market, feature_snapshot, prediction),
            })

    if style == "value":
        candidates.sort(key=lambda item: ((item["edge"] if item["edge"] is not None else -999.0), item["confidence"]), reverse=True)
    else:
        candidates.sort(key=lambda item: (item["confidence"] - (item["rank_index"] * 3), item["prediction"].match_date.toordinal() * -1), reverse=True)

    selected = []
    used_fixtures = set()
    for candidate in candidates:
        fixture_key = (
            candidate["prediction"].match_date,
            candidate["prediction"].home_team,
            candidate["prediction"].away_team,
        )
        if fixture_key in used_fixtures:
            continue
        selected.append(candidate)
        used_fixtures.add(fixture_key)
        if len(selected) >= size:
            break

    combo_rows = []
    combined_odds = 1.0
    priced_legs = 0
    for item in selected:
        prediction = item["prediction"]
        meta_home = get_team_metadata(prediction.home_team)
        meta_away = get_team_metadata(prediction.away_team)
        if item["odds"]:
            combined_odds *= float(item["odds"])
            priced_legs += 1
        combo_rows.append({
            "match_date": prediction.match_date,
            "match_time": get_cached_kickoff_time(prediction.competition, prediction.match_date, prediction.home_team, prediction.away_team),
            "competition": normalize_display_competition_name(
                competitions.get(prediction.competition, prediction.competition),
                code=prediction.competition,
            ),
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=prediction.home_team, max_length=24),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=prediction.away_team, max_length=24),
            "home_logo": meta_home.get("crest"),
            "away_logo": meta_away.get("crest"),
            "tip": item["market"],
            "confidence": item["confidence"],
            "odds": item["odds"],
            "edge": item["edge"] if item["edge"] != -999.0 else None,
            "reasons": item["reasons"],
            "detail_url": build_match_detail_url(prediction, source="combo_builder"),
        })

    summary = None
    if combo_rows:
        summary = {
            "legs": len(combo_rows),
            "priced_legs": priced_legs,
            "combined_odds": round(combined_odds, 2) if priced_legs else None,
            "average_confidence": round(sum(row["confidence"] for row in combo_rows) / len(combo_rows), 1),
            "style": style.title(),
            "market_filter": market_filter or "Mixed Markets",
        }

    return {
        "combo_rows": combo_rows,
        "summary": summary,
        "selected_size": size,
        "selected_market": market_filter,
        "selected_style": style,
        "size_options": [3, 5, 10],
        "market_options": [
            "",
            "1",
            "2",
            "X",
            "GG",
            "NG",
            "Over 2.5",
            "Under 2.5",
            "Any Team Over 1.5",
            "Home Win Either Half",
            "Away Win Either Half",
            "Home Team Over 1.0",
            "Away Team Over 1.0",
        ],
    }


def recent_saved_combo_slips(limit=8):
    cache_key = f"recent_saved_combo_slips_v2::{limit}"
    cached_rows = cache.get(cache_key)
    if cached_rows is not None:
        return cached_rows

    slips = ComboSlip.objects.prefetch_related("legs").all()[:limit]
    combo_dates = sorted({leg.match_date for slip in slips for leg in slip.legs.all()})
    prediction_rows = MatchPrediction.objects.filter(match_date__in=combo_dates)
    prediction_rows_by_date = _build_prediction_lookup(prediction_rows)

    rows = []
    for slip in slips:
        enriched_legs = []
        slip_competitions_to_refresh = set()
        for leg in slip.legs.all():
            matched_prediction = _match_prediction_from_lookup(
                prediction_rows_by_date,
                leg.match_date,
                leg.home_team,
                leg.away_team,
            )

            if matched_prediction and matched_prediction.competition:
                slip_competitions_to_refresh.add((matched_prediction.competition, matched_prediction.match_date))

        for competition_code, refresh_date in sorted(slip_competitions_to_refresh):
            refresh_prediction_statuses(competition_code, refresh_date)

        for leg in slip.legs.all():
            matched_prediction = _match_prediction_from_lookup(
                prediction_rows_by_date,
                leg.match_date,
                leg.home_team,
                leg.away_team,
            )

            leg_state = {
                "record": leg,
                "actual_score": None,
                "actual_tip": None,
                "is_correct": None,
            }
            if matched_prediction and matched_prediction.actual_home_goals is not None and matched_prediction.actual_away_goals is not None:
                home_g = matched_prediction.actual_home_goals
                away_g = matched_prediction.actual_away_goals
                ht_home_g = matched_prediction.actual_ht_home_goals
                ht_away_g = matched_prediction.actual_ht_away_goals
                result_tip = "1" if home_g > away_g else "2" if home_g < away_g else "X"
                gg = home_g >= 1 and away_g >= 1
                over_2_5 = (home_g + away_g) > 2.5
                under_2_5 = not over_2_5
                nogg = not gg
                any_team_over_1_5 = max(home_g, away_g) >= 2
                home_team_over_1_0 = home_g >= 2
                away_team_over_1_0 = away_g >= 2
                home_team_over_1_0_push = home_g == 1
                away_team_over_1_0_push = away_g == 1
                home_second_half = (home_g - ht_home_g) if ht_home_g is not None else None
                away_second_half = (away_g - ht_away_g) if ht_away_g is not None else None
                home_win_either_half = (
                    (ht_home_g is not None and ht_away_g is not None and ht_home_g > ht_away_g)
                    or (
                        home_second_half is not None
                        and away_second_half is not None
                        and home_second_half > away_second_half
                    )
                )
                away_win_either_half = (
                    (ht_home_g is not None and ht_away_g is not None and ht_away_g > ht_home_g)
                    or (
                        home_second_half is not None
                        and away_second_half is not None
                        and away_second_half > home_second_half
                    )
                )

                if leg.tip == "GG" and gg:
                    actual_tip = "GG"
                elif leg.tip == "NG" and nogg:
                    actual_tip = "NG"
                elif leg.tip == "Over 2.5" and over_2_5:
                    actual_tip = "Over 2.5"
                elif leg.tip == "Under 2.5" and under_2_5:
                    actual_tip = "Under 2.5"
                elif leg.tip == "Any Team Over 1.5" and any_team_over_1_5:
                    actual_tip = "Any Team Over 1.5"
                elif leg.tip == "Home Win Either Half" and home_win_either_half:
                    actual_tip = "Home Win Either Half"
                elif leg.tip == "Away Win Either Half" and away_win_either_half:
                    actual_tip = "Away Win Either Half"
                elif leg.tip == "Home Team Over 1.0" and home_team_over_1_0:
                    actual_tip = "Home Team Over 1.0"
                elif leg.tip == "Away Team Over 1.0" and away_team_over_1_0:
                    actual_tip = "Away Team Over 1.0"
                elif leg.tip == "Home Team Over 1.0" and home_team_over_1_0_push:
                    actual_tip = "Refund"
                elif leg.tip == "Away Team Over 1.0" and away_team_over_1_0_push:
                    actual_tip = "Refund"
                else:
                    actual_tip = result_tip

                leg_state["actual_score"] = f"{home_g} - {away_g}"
                leg_state["actual_tip"] = actual_tip
                leg_state["is_correct"] = None if actual_tip == "Refund" else (leg.tip == actual_tip)

            enriched_legs.append(leg_state)

        settled_legs = [leg for leg in enriched_legs if leg["is_correct"] is not None or leg["actual_tip"] == "Refund"]
        has_wrong_leg = any(leg["is_correct"] is False for leg in enriched_legs)
        has_pending_leg = any(
            leg["is_correct"] is None and leg["actual_tip"] != "Refund"
            for leg in enriched_legs
        )
        all_correct = bool(enriched_legs) and all(leg["is_correct"] is True for leg in enriched_legs)
        has_refund_only_gaps = any(leg["actual_tip"] == "Refund" for leg in enriched_legs)

        if all_correct:
            slip_status = "won"
            slip_status_label = "Won Combo"
        elif has_wrong_leg:
            slip_status = "lost"
            slip_status_label = "Lost Combo"
        elif has_pending_leg and not has_wrong_leg:
            slip_status = "active"
            slip_status_label = "Active Combo"
        elif settled_legs and has_refund_only_gaps:
            slip_status = "void"
            slip_status_label = "Refund / Void"
        else:
            slip_status = "pending"
            slip_status_label = "Pending"

        rows.append({
            "id": slip.id,
            "name": slip.name,
            "size": slip.size,
            "market_filter": slip.market_filter or "Mixed Markets",
            "style": slip.get_style_display(),
            "combined_odds": slip.combined_odds,
            "average_confidence": slip.average_confidence,
            "priced_legs": slip.priced_legs,
            "auto_generated": slip.auto_generated,
            "created_at": slip.created_at,
            "slip_status": slip_status,
            "slip_status_label": slip_status_label,
            "legs": enriched_legs,
        })
    cache.set(cache_key, rows, timeout=180)
    return rows


def combo_slip_tracking_summary(all_slips=None):
    if all_slips is None:
        cache_key = "combo_slip_tracking_summary_v1"
        cached_summary = cache.get(cache_key)
        if cached_summary is not None:
            return cached_summary
        all_slips = recent_saved_combo_slips(limit=500)

    summary = {
        "total": len(all_slips),
        "won": sum(1 for slip in all_slips if slip["slip_status"] == "won"),
        "active": sum(1 for slip in all_slips if slip["slip_status"] == "active"),
        "lost": sum(1 for slip in all_slips if slip["slip_status"] == "lost"),
    }
    cache.set("combo_slip_tracking_summary_v1", summary, timeout=180)
    return summary


def generate_all_combo_slips():
    created_count = 0
    existing_count = 0
    skipped_count = 0
    market_options = build_combo_builder_context().get("market_options", [])

    for size in (3, 5, 10):
        for style in ("safe", "value"):
            for market_filter in market_options:
                context = build_combo_builder_context(
                    size=size,
                    market_filter=market_filter,
                    style=style,
                )
                if not context.get("combo_rows") or not context.get("summary"):
                    skipped_count += 1
                    continue

                tracked_slip, created = auto_track_combo_slip(
                    context,
                    size=size,
                    market_filter=market_filter,
                    style=style,
                    return_created=True,
                )
                if tracked_slip is None:
                    skipped_count += 1
                    continue
                if created:
                    created_count += 1
                else:
                    existing_count += 1

    return {
        "created": created_count,
        "existing": existing_count,
        "skipped": skipped_count,
    }


def build_combo_history_payload(status_filter="all"):
    valid_statuses = {"all", "won", "active", "lost", "void", "pending"}
    if status_filter not in valid_statuses:
        status_filter = "all"

    saved_slips = recent_saved_combo_slips(limit=500)
    tracking_summary = combo_slip_tracking_summary(saved_slips)
    if status_filter != "all":
        saved_slips = [slip for slip in saved_slips if slip["slip_status"] == status_filter]

    return {
        "saved_slips": saved_slips,
        "combo_tracking_summary": tracking_summary,
        "status_filter": status_filter,
        "status_options": [
            {"key": "all", "label": "All"},
            {"key": "won", "label": "Won"},
            {"key": "active", "label": "Active"},
            {"key": "lost", "label": "Lost"},
            {"key": "void", "label": "Refund / Void"},
            {"key": "pending", "label": "Pending"},
        ],
    }


@require_GET
def combo_history_view(request):
    context = build_combo_history_payload(request.GET.get("status", "all"))
    selected_code = request.GET.get("competition", "PL")
    context["league_table"] = get_league_table(selected_code)
    context["competitions"] = competitions
    context["selected_competition"] = selected_code
    return render(request, "predict/combo_history.html", context)


def build_combo_signature(combo_rows, size, market_filter, style):
    signature_payload = {
        "size": size,
        "market_filter": market_filter or "",
        "style": style,
        "legs": [
            {
                "match_date": str(row["match_date"]),
                "competition": row["competition"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "tip": row["tip"],
                "confidence": round(float(row["confidence"]), 4),
                "odds": round(float(row["odds"]), 4) if row.get("odds") is not None else None,
            }
            for row in combo_rows
        ],
    }
    return hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def auto_track_combo_slip(context, size, market_filter="", style="safe", return_created=False):
    combo_rows = context.get("combo_rows") or []
    summary = context.get("summary")
    if not combo_rows or not summary:
        return (None, False) if return_created else None

    signature = build_combo_signature(combo_rows, size=size, market_filter=market_filter, style=style)
    market_label = market_filter or "Mixed Markets"
    slip, created = ComboSlip.objects.get_or_create(
        signature=signature,
        defaults={
            "name": f"Auto {style.title()} {summary['legs']}-Leg {market_label}",
            "size": summary["legs"],
            "market_filter": market_filter,
            "style": style,
            "combined_odds": summary.get("combined_odds"),
            "average_confidence": summary.get("average_confidence") or 0,
            "priced_legs": summary.get("priced_legs") or 0,
            "auto_generated": True,
        },
    )

    if created:
        ComboSlipLeg.objects.bulk_create([
            ComboSlipLeg(
                slip=slip,
                match_date=row["match_date"],
                competition=row["competition"],
                home_team=row["home_team"],
                away_team=row["away_team"],
                tip=row["tip"],
                confidence=row["confidence"],
                odds=row.get("odds"),
            )
            for row in combo_rows
        ])
        _clear_combo_cache()
    else:
        changed = False
        if slip.combined_odds != summary.get("combined_odds"):
            slip.combined_odds = summary.get("combined_odds")
            changed = True
        if slip.average_confidence != (summary.get("average_confidence") or 0):
            slip.average_confidence = summary.get("average_confidence") or 0
            changed = True
        if slip.priced_legs != (summary.get("priced_legs") or 0):
            slip.priced_legs = summary.get("priced_legs") or 0
            changed = True
        if changed:
            slip.save(update_fields=["combined_odds", "average_confidence", "priced_legs"])
            _clear_combo_cache()

    if return_created:
        return slip, created
    return slip


def predictions_view(request):
    match_date = request.GET.get("match_date")
    predictions = MatchPrediction.objects.select_related("odds").all().order_by("match_date")

    if match_date:
        predictions = predictions.filter(match_date=match_date)
    else:
        today = timezone.localdate()
        available_dates = list(
            MatchPrediction.objects.filter(match_date__gte=today)
            .order_by("match_date")
            .values_list("match_date", flat=True)
            .distinct()
        )
        selected_default_date = None
        if today in available_dates:
            selected_default_date = today
        elif available_dates:
            selected_default_date = available_dates[0]

        if selected_default_date:
            match_date = selected_default_date.isoformat()
            predictions = predictions.filter(match_date=selected_default_date)
        else:
            predictions = predictions.exclude(status="FINISHED")

    refresh_pairs = list(predictions.values_list("competition", "match_date").distinct())
    for competition_code, refresh_date in refresh_pairs:
        if competition_code and refresh_date:
            refresh_prediction_statuses(competition_code, refresh_date)

    predictions = predictions.select_related("odds")

    selected_code = request.GET.get("competition")
    if not selected_code:
        first_match = predictions.first()
        if first_match:
            selected_code = NAME_TO_CODE.get(first_match.competition.lower().strip(), "PL")
        else:
            selected_code = "PL"

    league_table = get_league_table(selected_code)
    top_predictions = get_top_predictions(limit=10)

    display_data = []
    for p in predictions:
        meta_home = get_team_metadata(p.home_team)
        meta_away = get_team_metadata(p.away_team)
        home_display_name = normalize_display_team_name(
            meta_home.get("shortName"),
            fallback=p.home_team,
        )
        away_display_name = normalize_display_team_name(
            meta_away.get("shortName"),
            fallback=p.away_team,
        )

        comp_code = p.competition if p.competition in competitions else NAME_TO_CODE.get(p.competition.lower().strip(), "default")
        competition_logo_path = static(f"logos/{comp_code}.png")

        actual_result = "-:-"
        actual_winner = None
        if p.status == "FINISHED" and p.actual_home_goals is not None:
            actual_result = f"{p.actual_home_goals} - {p.actual_away_goals}"
            if p.actual_home_goals > p.actual_away_goals:
                actual_winner = "1"
            elif p.actual_home_goals < p.actual_away_goals:
                actual_winner = "2"
            else:
                actual_winner = "X"

        if (p.predicted_home_goals or 0) > (p.predicted_away_goals or 0):
            winner = "1"
        elif (p.predicted_home_goals or 0) < (p.predicted_away_goals or 0):
            winner = "2"
        else:
            winner = "X"

        odds = resolve_prediction_odds(p)
        if winner == "1":
            display_odds = getattr(odds, "home_win", None) if odds else getattr(p, "odds_home", None)
        elif winner == "2":
            display_odds = getattr(odds, "away_win", None) if odds else getattr(p, "odds_away", None)
        else:
            display_odds = getattr(odds, "draw", None) if odds else getattr(p, "odds_draw", None)

        display_data.append({
            "home_team": home_display_name,
            "away_team": away_display_name,
            "home_form": get_team_recent_form(p.home_team, comp_code),
            "away_form": get_team_recent_form(p.away_team, comp_code),
            "predicted_home_goals": p.predicted_home_goals,
            "predicted_away_goals": p.predicted_away_goals,
            "match_date": p.match_date.strftime("%Y-%m-%d"),
            "match_time": get_cached_kickoff_time(comp_code, p.match_date, p.home_team, p.away_team),
            "competition": p.competition,
            "competition_code": comp_code,
            "status": p.status,
            "actual_home_goals": p.actual_home_goals,
            "actual_away_goals": p.actual_away_goals,
            "actual_result": actual_result,
            "home_logo": meta_home.get("crest", static("logos/default.png")),
            "away_logo": meta_away.get("crest", static("logos/default.png")),
            "competition_logo": competition_logo_path,
            "winner": winner,
            "actual_winner": actual_winner if p.status == "FINISHED" else None,
            "odds_home": getattr(p, "odds_home", None),
            "odds_draw": getattr(p, "odds_draw", None),
            "odds_away": getattr(p, "odds_away", None),
            "odds_gg": getattr(p, "odds_gg", None),
            "odds_over_25": getattr(p, "odds_over_25", None),
            "odds": odds,
            "display_odds": display_odds,
            "detail_url": build_match_detail_url(p, source="predictions"),
        })

    for row in league_table:
        team_name = row["team"]["name"]
        meta = cache.get(f"team_meta::{team_name}", {})
        row["team"]["shortName"] = normalize_display_team_name(
            meta.get("shortName"),
            fallback=team_name,
        )
        row["team"]["crest"] = meta.get("crest", static("logos/default.png"))

    paginator = Paginator(display_data, 10)
    page_number = request.GET.get("page")
    paginated_predictions = paginator.get_page(page_number)
    return render(request, "predict/predictions_view.html", {
        "predictions": paginated_predictions,
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
        "selected_date": match_date,
        "page_obj": paginated_predictions,
        "top_predictions": top_predictions,
    })


def _resolve_prediction_date(match_date_str):
    if match_date_str:
        return match_date_str, MatchPrediction.objects.filter(match_date=match_date_str)

    today = timezone.localdate()
    available_dates = list(
        MatchPrediction.objects.filter(match_date__gte=today)
        .order_by("match_date")
        .values_list("match_date", flat=True)
        .distinct()
    )
    selected_default_date = today if today in available_dates else (available_dates[0] if available_dates else None)
    if not selected_default_date:
        return None, MatchPrediction.objects.none()
    return selected_default_date.isoformat(), MatchPrediction.objects.filter(match_date=selected_default_date)


def build_correct_score_rows(match_date_str=None):
    selected_date, predictions = _resolve_prediction_date(match_date_str)
    predictions = predictions.order_by("match_date", "competition", "home_team")
    rows = []

    # Cache model bundles per competition to avoid re-loading for every fixture
    _model_cache = {}

    def _raw_goal_expectations(prediction):
        """
        Re-run the ML model to get raw float goal expectations (e.g. 1.72, 0.83)
        instead of the rounded integers stored in the DB (e.g. 2, 1).
        This produces much more realistic Poisson scoreline distributions.
        Falls back to the DB integer values if the model isn't available.
        """
        comp_code = prediction.competition
        if comp_code not in _model_cache:
            try:
                bundle = get_or_train_model_bundle(comp_code)
                _model_cache[comp_code] = bundle
            except Exception:
                _model_cache[comp_code] = None

        bundle = _model_cache.get(comp_code)
        if bundle is None:
            # Fallback: use DB integers (the old behaviour)
            return float(prediction.predicted_home_goals or 0), float(prediction.predicted_away_goals or 0)

        model_home, model_away, model_context = bundle
        try:
            from .utils import build_fixture_features
            import numpy as np
            X = build_fixture_features(prediction.home_team, prediction.away_team, model_context).fillna(0)
            raw_home = float(np.clip(model_home.predict(X)[0], 0, 10))
            raw_away = float(np.clip(model_away.predict(X)[0], 0, 10))
            return raw_home, raw_away
        except Exception:
            return float(prediction.predicted_home_goals or 0), float(prediction.predicted_away_goals or 0)

    for prediction in predictions:
        raw_home, raw_away = _raw_goal_expectations(prediction)

        top_scores = scoreline_predictions(raw_home, raw_away)
        if not top_scores:
            continue

        meta_home = get_team_metadata(prediction.home_team)
        meta_away = get_team_metadata(prediction.away_team)

        is_finished = prediction.status == "FINISHED"
        actual_home = prediction.actual_home_goals
        actual_away = prediction.actual_away_goals
        actual_score_str = f"{actual_home}-{actual_away}" if (is_finished and actual_home is not None and actual_away is not None) else None
        top_score_str = top_scores[0]["score"] if top_scores else None
        top_correct = actual_score_str is not None and top_score_str == actual_score_str
        any_correct = actual_score_str is not None and any(s["score"] == actual_score_str for s in top_scores)

        rows.append({
            "match_date": prediction.match_date.strftime("%Y-%m-%d"),
            "match_time": get_cached_kickoff_time(
                prediction.competition,
                prediction.match_date,
                prediction.home_team,
                prediction.away_team,
            ),
            "competition": normalize_display_competition_name(
                competitions.get(prediction.competition, prediction.competition),
                code=prediction.competition,
            ),
            "competition_logo": static(f"logos/{prediction.competition}.png") if prediction.competition in competitions else None,
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=prediction.home_team),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=prediction.away_team),
            "home_logo": meta_home.get("crest"),
            "away_logo": meta_away.get("crest"),
            "top_score": top_scores[0],
            "other_scores": top_scores[1:],
            "detail_url": build_match_detail_url(prediction, source="correct_score"),
            "is_finished": is_finished,
            "actual_score": actual_score_str,
            "top_correct": top_correct,
            "any_correct": any_correct,
        })

    finished_rows = [r for r in rows if r["is_finished"] and r["actual_score"] is not None]
    stats = {
        "finished": len(finished_rows),
        "top_correct": sum(1 for r in finished_rows if r["top_correct"]),
        "any_correct": sum(1 for r in finished_rows if r["any_correct"]),
    }
    stats["top_accuracy"] = round(stats["top_correct"] / stats["finished"] * 100, 1) if stats["finished"] else None
    stats["any_accuracy"] = round(stats["any_correct"] / stats["finished"] * 100, 1) if stats["finished"] else None

    return selected_date, rows, stats


def build_anytime_scorer_rows(match_date_str=None):
    selected_date, predictions = _resolve_prediction_date(match_date_str)
    predictions = predictions.order_by("match_date", "competition", "home_team")
    scorer_cache = {}
    rows = []

    refresh_pairs = list(predictions.values_list("competition", "match_date").distinct())
    for competition_code, refresh_date in refresh_pairs:
        if competition_code and refresh_date:
            refresh_prediction_statuses(competition_code, refresh_date)

    predictions = predictions.select_related("odds")

    def scorer_candidates_for_team(team_name, competition_code):
        if competition_code not in scorer_cache:
            scorer_cache[competition_code] = fetch_competition_scorers(competition_code)
        team_aliases = _team_name_aliases(team_name)
        candidates = []
        for scorer in scorer_cache.get(competition_code, []):
            team_info = scorer.get("team", {}) or {}
            scorer_team_name = team_info.get("name")
            if not scorer_team_name:
                continue
            if not (_team_name_aliases(scorer_team_name) & team_aliases):
                continue
            player_info = scorer.get("player", {}) or {}
            candidates.append({
                "name": player_info.get("name") or "Unknown",
                "team": scorer_team_name,
                "goals": scorer.get("goals") or 0,
                "assists": scorer.get("assists") or 0,
                "penalties": scorer.get("penalties") or 0,
            })
        return sorted(candidates, key=lambda item: (item["goals"], item["penalties"], item["assists"]), reverse=True)

    for prediction in predictions:
        competition_code = prediction.competition
        home_candidates = scorer_candidates_for_team(prediction.home_team, competition_code)
        away_candidates = scorer_candidates_for_team(prediction.away_team, competition_code)
        if not home_candidates and not away_candidates:
            continue

        home_weight = max(float(prediction.predicted_home_goals or 0), 0.6)
        away_weight = max(float(prediction.predicted_away_goals or 0), 0.6)

        ranked_candidates = []
        for candidate in home_candidates[:3]:
            ranked_candidates.append({
                **candidate,
                "side": "home",
                "score": (candidate["goals"] * 1.0) + (candidate["penalties"] * 0.35) + (candidate["assists"] * 0.1) + (home_weight * 1.2),
            })
        for candidate in away_candidates[:3]:
            ranked_candidates.append({
                **candidate,
                "side": "away",
                "score": (candidate["goals"] * 1.0) + (candidate["penalties"] * 0.35) + (candidate["assists"] * 0.1) + (away_weight * 1.2),
            })

        ranked_candidates.sort(key=lambda item: item["score"], reverse=True)
        top_candidate = ranked_candidates[0]
        alternates = ranked_candidates[1:4]

        actual_scorers = None
        if prediction.status == "FINISHED":
            actual_scorers = get_actual_scorer_names(
                competition_code,
                prediction.match_date,
                prediction.home_team,
                prediction.away_team,
            )
            if actual_scorers is not None:
                top_candidate["did_score"] = _normalize_player_name(top_candidate["name"]) in actual_scorers
                for alternate in alternates:
                    alternate["did_score"] = _normalize_player_name(alternate["name"]) in actual_scorers

        meta_home = get_team_metadata(prediction.home_team)
        meta_away = get_team_metadata(prediction.away_team)
        rows.append({
            "match_date": prediction.match_date.strftime("%Y-%m-%d"),
            "match_time": get_cached_kickoff_time(
                competition_code,
                prediction.match_date,
                prediction.home_team,
                prediction.away_team,
            ),
            "competition": normalize_display_competition_name(
                competitions.get(competition_code, competition_code),
                code=competition_code,
            ),
            "competition_logo": static(f"logos/{competition_code}.png") if competition_code in competitions else None,
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=prediction.home_team),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=prediction.away_team),
            "home_logo": meta_home.get("crest"),
            "away_logo": meta_away.get("crest"),
            "predicted_score": f"{prediction.predicted_home_goals} - {prediction.predicted_away_goals}",
            "status": prediction.status,
            "actual_score": (
                f"{prediction.actual_home_goals} - {prediction.actual_away_goals}"
                if prediction.actual_home_goals is not None and prediction.actual_away_goals is not None
                else None
            ),
            "top_pick": top_candidate,
            "alternates": alternates,
            "detail_url": build_match_detail_url(prediction, source="anytime_scorer"),
        })

    return selected_date, rows


def correct_score_view(request):
    match_date, rows, score_stats = build_correct_score_rows(request.GET.get("match_date"))
    selected_date = match_date or timezone.localdate().isoformat()
    selected_code = request.GET.get("competition", "PL")
    league_table = get_league_table(selected_code)

    return render(request, "predict/correct_score.html", {
        "predictions": rows,
        "score_stats": score_stats,
        "selected_date": selected_date,
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
    })


def anytime_scorer_view(request):
    match_date, rows = build_anytime_scorer_rows(request.GET.get("match_date"))
    selected_date = match_date or timezone.localdate().isoformat()
    selected_code = request.GET.get("competition", "PL")
    league_table = get_league_table(selected_code)

    return render(request, "predict/anytime_scorer.html", {
        "predictions": rows,
        "selected_date": selected_date,
        "is_live": bool(rows),
        "league_table": league_table,
        "competitions": competitions,
        "selected_competition": selected_code,
    })


def match_detail_view(request):
    match_date = request.GET.get("match_date")
    competition = request.GET.get("competition")
    home_team = request.GET.get("home_team")
    away_team = request.GET.get("away_team")
    source = request.GET.get("from") or "predictions"

    prediction = get_object_or_404(
        MatchPrediction,
        match_date=match_date,
        competition=competition,
        home_team=home_team,
        away_team=away_team,
    )

    context = build_match_detail_context(prediction, source=source)
    return render(request, "predict/match_detail.html", context)


# AJAX league table view (unchanged)
def ajax_league_table(request):
    comp = request.GET.get("competition", "PL")
    table = get_league_table(comp)
    for row in table:
        team = row.get("team", {})
        name = team.get("name", "")
        meta = cache.get(f"team_meta::{name}", {})
        team["shortName"] = meta.get("shortName") or team.get("shortName") or name
        team["crest"] = meta.get("crest") or team.get("crest") or static("logos/default.png")
    html = render_to_string("partials/league_table.html", {"league_table": table})
    return JsonResponse({"html": html})


def team_logos_preview(request):
    grouped_teams = defaultdict(list)

    # NOTE: cache.iter_keys may not exist depending on your cache backend
    # this portion retains earlier logic but might need adaptation for your cache
    for comp_code in COMPETITIONS:
        # scan keys in cache is backend-dependent; keep simple: attempt to load from known teams
        pass

    # Fallback simple preview (if cache keys scanning not available)
    preview_data = []
    for comp_code, comp_name in competitions.items():
        preview_data.append({
            "competition": comp_name,
            "competition_code": comp_code,
            "logo": static(f"logos/{comp_code}.png"),
            "teams": []
        })

    return render(request, "predict/team_logos_preview.html", {
        "preview_data": preview_data
    })


def match_team_logo(team_name):
    simplified_team_names = [f.lower().replace('.png', '').replace('.jpg', '').replace('.jpeg', '') for f in TEAM_LOGO_FILES]
    match = difflib.get_close_matches(team_name.lower(), simplified_team_names, n=1, cutoff=0.6)
    if match:
        for f in TEAM_LOGO_FILES:
            if match[0] in f.lower():
                return f
    return "default.png"


def flatten_competitions(comp_dict):
    return {code: name for region in comp_dict.values() for code, name in region.items()}


def league_table_view(request, competition_code):
    table = get_league_table(competition_code)
    for team in table:
        team_name = team["team"]["name"]
        team["team"]["logo"] = match_team_logo(team_name)

    competition_name = flatten_competitions(COMPETITIONS).get(competition_code, competition_code)

    return render(request, "predict/league_table.html", {
        "table": table,
        "competition_code": competition_code,
        "competition_name": competition_name,
        "competitions_grouped": COMPETITIONS,  # For dropdown with regions
        "competition_logo": static(f"logos/{competition_code}.png")
    })


@login_required
@require_POST
def refresh_league_table_cache(request):
    comp = request.POST.get("competition")
    if comp not in COMPETITIONS:
        return JsonResponse({"success": False, "message": "Invalid competition."}, status=400)

    get_league_table(comp)
    return JsonResponse({"success": True, "message": f"Refreshed {comp}"})


def actual_results_view(request):
    form = ActualResultForm(request.GET or None)
    results = []

    if form.is_valid():
        comp = form.cleaned_data['competition']
        match_date_str = form.cleaned_data['match_date'].strftime('%Y-%m-%d')

        results = fetch_actual_results(comp, match_date_str)
        updated_count = 0

        for result in results:
            home = result["home_team"]
            away = result["away_team"]

            prediction = MatchPrediction.objects.filter(
                competition=comp,
                home_team=home,
                away_team=away,
                match_date=form.cleaned_data['match_date']
            ).first()

            if prediction:
                prediction.actual_home_goals = result["actual_home_goals"]
                prediction.actual_away_goals = result["actual_away_goals"]
                prediction.actual_result = result["actual_result"]
                prediction.actual_score = f"{result['actual_home_goals']} - {result['actual_away_goals']}"
                prediction.status = "FINISHED"
                prediction.save()
                updated_count += 1

        if updated_count:
            messages.success(request, f"{updated_count} match result(s) updated successfully.")
        else:
            messages.warning(request, "No matching predictions found to update.")

    return render(request, "predict/actual_results.html", {
        "form": form,
        "results": results,
    })


def refresh_top_picks(request):
    today = date.today()
    for variant in ("1", "2", "3", "4"):
        TopPick.objects.filter(variant=variant, match_date__gte=today).delete()
        top_predictions = get_top_predictions(limit=(20 if variant == "4" else 10), variant=variant)
        store_top_pick_for_date(top_predictions, variant=variant)
    return redirect("top-picks_view")


def export_top_picks(request, format):
    match_date_str = request.GET.get("match_date")
    variant = request.GET.get("variant", "1")
    variant = variant if variant in {"1", "2", "3", "4"} else "1"

    if variant in {"3", "4"} and not match_date_str:
        match_date = timezone.localdate()
    else:
        try:
            try:
                match_date = datetime.strptime(match_date_str, "%Y-%m-%d").date()
            except ValueError:
                match_date = datetime.strptime(match_date_str, "%B %d, %Y").date()
        except Exception as e:
            return HttpResponseBadRequest(f"Invalid date format: {e}")

    if variant == "3":
        picks = list(
            TopPick.objects.filter(variant=variant, match_date__gte=timezone.localdate())
            .order_by("-confidence", "match_date")[:10]
        )
    elif variant == "4":
        picks = list(
            TopPick.objects.filter(variant=variant, match_date__gte=timezone.localdate())
            .order_by("-confidence", "match_date")[:20]
        )
    else:
        picks = list(TopPick.objects.filter(match_date=match_date, variant=variant))

    if format == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="top_picks_{match_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Match Date", "Home", "Away", "Tip", "Confidence", "Odds", "Actual Tip", "Correct?"])
        for p in picks:
            writer.writerow([p.match_date, p.home_team, p.away_team, p.tip, p.confidence, getattr(p, "odds", ""), p.actual_tip, p.is_correct])
        return response

    elif format == "pdf":
        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="top_picks_{match_date}.pdf"'
        import io
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        def competition_logo_path(competition_code):
            if not competition_code:
                return None
            logo_path = os.path.join(settings.BASE_DIR, "static", "logos", f"{competition_code}.png")
            return logo_path if os.path.exists(logo_path) else None

        def load_team_logo(logo_source):
            if not logo_source:
                return None
            if logo_source.startswith("/static/"):
                local_path = os.path.join(settings.BASE_DIR, logo_source.lstrip("/"))
                if os.path.exists(local_path):
                    return local_path
                return None
            if logo_source.startswith("http://") or logo_source.startswith("https://"):
                try:
                    image_response = requests.get(logo_source, timeout=5)
                    image_response.raise_for_status()
                    return io.BytesIO(image_response.content)
                except requests.RequestException:
                    return None
            if os.path.exists(logo_source):
                return logo_source
            return None

        def logo_cell(source, width=8 * mm, height=8 * mm):
            resolved = load_team_logo(source)
            if not resolved:
                return ""
            image = Image(resolved, width=width, height=height)
            image.hAlign = "CENTER"
            return image

        prediction_rows = MatchPrediction.objects.filter(
            match_date__in=sorted({pick.match_date for pick in picks})
        ).select_related("odds")
        prediction_rows_by_date = defaultdict(list)
        for prediction_row in prediction_rows:
            prediction_rows_by_date[prediction_row.match_date].append(prediction_row)

        resolved_predictions = {}
        for pick in picks:
            matched_prediction = None
            pick_home_aliases = _team_name_aliases(pick.home_team)
            pick_away_aliases = _team_name_aliases(pick.away_team)
            for prediction_row in prediction_rows_by_date.get(pick.match_date, []):
                if (
                    _team_name_aliases(prediction_row.home_team) & pick_home_aliases
                    and _team_name_aliases(prediction_row.away_team) & pick_away_aliases
                ):
                    matched_prediction = prediction_row
                    break
            resolved_predictions[(pick.match_date, pick.home_team, pick.away_team)] = matched_prediction

        styles = getSampleStyleSheet()
        title_style = styles["Heading2"]
        cell_style = styles["BodyText"]
        cell_style.fontName = "Helvetica"
        cell_style.fontSize = 9
        cell_style.leading = 11

        story = [
            Paragraph(f"Top Picks - {match_date}", title_style),
            Spacer(1, 6 * mm),
        ]

        table_data = [[
            Paragraph("<b>Date</b>", cell_style),
            Paragraph("<b>Comp</b>", cell_style),
            "",
            Paragraph("<b>Home</b>", cell_style),
            "",
            Paragraph("<b>Away</b>", cell_style),
            "",
            Paragraph("<b>Tip</b>", cell_style),
            Paragraph("<b>Odds</b>", cell_style),
        ]]

        for pick in picks:
            matched_prediction = resolved_predictions.get((pick.match_date, pick.home_team, pick.away_team))
            competition_code = matched_prediction.competition if matched_prediction else None
            metadata_home_name = matched_prediction.home_team if matched_prediction else pick.home_team
            metadata_away_name = matched_prediction.away_team if matched_prediction else pick.away_team
            home_meta = get_team_metadata(metadata_home_name)
            away_meta = get_team_metadata(metadata_away_name)
            home_name = normalize_display_team_name(
                home_meta.get("shortName"),
                fallback=pick.home_team,
                max_length=28,
            )
            away_name = normalize_display_team_name(
                away_meta.get("shortName"),
                fallback=pick.away_team,
                max_length=28,
            )
            kickoff_time = get_cached_kickoff_time(
                competition_code,
                pick.match_date,
                metadata_home_name,
                metadata_away_name,
            )
            date_text = pick.match_date.strftime("%Y-%m-%d")
            if kickoff_time:
                date_text = f"{date_text}<br/>{kickoff_time}"

            table_data.append([
                Paragraph(date_text, cell_style),
                logo_cell(competition_logo_path(competition_code)),
                Paragraph(normalize_display_competition_name(competitions.get(competition_code, competition_code), code=competition_code), cell_style),
                logo_cell(home_meta.get("crest")),
                Paragraph(home_name, cell_style),
                logo_cell(away_meta.get("crest")),
                Paragraph(away_name, cell_style),
                Paragraph(f"<b>{pick.tip}</b>", cell_style),
                Paragraph("-" if pick.odds is None else f"{pick.odds:.2f}", cell_style),
            ])

        table = Table(
            table_data,
            colWidths=[28 * mm, 12 * mm, 18 * mm, 12 * mm, 36 * mm, 12 * mm, 36 * mm, 24 * mm, 20 * mm],
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
            ("ALIGN", (3, 0), (3, -1), "CENTER"),
            ("ALIGN", (5, 0), (5, -1), "CENTER"),
            ("ALIGN", (7, 0), (7, -1), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)

        doc = SimpleDocTemplate(
            response,
            pagesize=landscape(A4),
            leftMargin=12 * mm,
            rightMargin=12 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
        )
        doc.build(story)
        return response

    else:
        return HttpResponse("Invalid format", status=400)


def export_correct_score(request, format):
    match_date, rows, _ = build_correct_score_rows(request.GET.get("match_date"))
    match_date = match_date or timezone.localdate().isoformat()

    if format == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="correct_score_{match_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Match Date", "Time", "Competition", "Home", "Away", "Best Score", "Best Probability", "Alternates"])
        for row in rows:
            alternates = ", ".join(f"{item['score']} ({item['percent']}%)" for item in row.get("other_scores", []))
            writer.writerow([
                row["match_date"],
                row.get("match_time", ""),
                row["competition"],
                row["home_team"],
                row["away_team"],
                row["top_score"]["score"],
                row["top_score"]["percent"],
                alternates,
            ])
        return response

    if format == "pdf":
        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="correct_score_{match_date}.pdf"'
        import io
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
        styles = getSampleStyleSheet()
        story = [Paragraph(f"Correct Score - {match_date}", styles["Heading2"]), Spacer(1, 6 * mm)]
        table_data = [["Date", "Time", "Comp", "Fixture", "Best Score", "Alternates"]]
        for row in rows:
            alternates = ", ".join(f"{item['score']} ({item['percent']}%)" for item in row.get("other_scores", []))
            table_data.append([
                row["match_date"],
                row.get("match_time", ""),
                row["competition"],
                f"{row['home_team']} vs {row['away_team']}",
                f"{row['top_score']['score']} ({row['top_score']['percent']}%)",
                alternates,
            ])
        table = Table(table_data, repeatRows=1, colWidths=[24 * mm, 18 * mm, 20 * mm, 60 * mm, 30 * mm, 90 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)
        doc.build(story)
        response.write(buffer.getvalue())
        return response

    return HttpResponse("Invalid format", status=400)


def export_anytime_scorer(request, format):
    match_date, rows = build_anytime_scorer_rows(request.GET.get("match_date"))
    match_date = match_date or timezone.localdate().isoformat()

    if format == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="anytime_scorer_{match_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Match Date", "Time", "Competition", "Home", "Away", "Projected Score", "Top Pick", "Alternates"])
        for row in rows:
            alternates = ", ".join(player["name"] for player in row.get("alternates", []))
            writer.writerow([
                row["match_date"],
                row.get("match_time", ""),
                row["competition"],
                row["home_team"],
                row["away_team"],
                row["predicted_score"],
                row["top_pick"]["name"],
                alternates,
            ])
        return response

    if format == "pdf":
        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="anytime_scorer_{match_date}.pdf"'
        import io
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
        styles = getSampleStyleSheet()
        story = [Paragraph(f"Anytime Scorer - {match_date}", styles["Heading2"]), Spacer(1, 6 * mm)]
        table_data = [["Date", "Time", "Comp", "Fixture", "Projected Score", "Top Pick", "Alternates"]]
        for row in rows:
            alternates = ", ".join(player["name"] for player in row.get("alternates", []))
            table_data.append([
                row["match_date"],
                row.get("match_time", ""),
                row["competition"],
                f"{row['home_team']} vs {row['away_team']}",
                row["predicted_score"],
                row["top_pick"]["name"],
                alternates,
            ])
        table = Table(table_data, repeatRows=1, colWidths=[24 * mm, 18 * mm, 20 * mm, 58 * mm, 24 * mm, 40 * mm, 70 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)
        doc.build(story)
        response.write(buffer.getvalue())
        return response

    return HttpResponse("Invalid format", status=400)


def export_market_picks(request, format):
    priced_only = request.GET.get("priced") == "1"
    selected_group, selected_market, selected_scope, selected_sort, selected_limit, _total_count, _market_groups, _market_options, _market_scopes, _market_sort_options, rows, _category_summary = build_market_pick_rows(
        request.GET.get("group"),
        request.GET.get("market"),
        request.GET.get("scope"),
        request.GET.get("sort"),
        request.GET.get("limit"),
        priced_only,
    )
    priced_suffix = "priced_" if priced_only else ""
    safe_market = f"{selected_scope}_{selected_sort}_{priced_suffix}{selected_limit}_{selected_market}".lower().replace(" ", "_").replace(".", "").replace("/", "_")

    if format == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="market_picks_{safe_market}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Match Date", "Time", "Competition", "Home", "Away", "Market", "Confidence", "Odds", "Edge", "FT", "Result"])
        for row in rows:
            if row["actual_tip"] == "Refund":
                result_state = "Refund"
            elif row["is_correct"] is True:
                result_state = "Won"
            elif row["is_correct"] is False:
                result_state = "Lost"
            else:
                result_state = "Pending"
            writer.writerow([
                row["match_date"],
                row.get("match_time", ""),
                row["competition"],
                row["home_team"],
                row["away_team"],
                row["tip"],
                round(float(row["confidence"]), 1),
                "" if row["odds"] is None else row["odds"],
                "" if row["edge"] is None else round(float(row["edge"]), 1),
                row.get("actual_score") or "",
                result_state,
            ])
        return response

    if format == "pdf":
        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="market_picks_{safe_market}.pdf"'
        import io
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
        styles = getSampleStyleSheet()
        story = [
            Paragraph(
                f"Market Picks - {selected_market} ({selected_group.title()} / {selected_scope.title()} / {selected_sort.title()} / "
                f"{'Priced Only / ' if priced_only else ''}{selected_limit.upper()})",
                styles["Heading2"],
            ),
            Spacer(1, 6 * mm),
        ]
        table_data = [["Date", "Time", "Comp", "Fixture", "Market", "Conf", "Odds", "FT", "Result"]]
        for row in rows:
            if row["actual_tip"] == "Refund":
                result_state = "Refund"
            elif row["is_correct"] is True:
                result_state = "Won"
            elif row["is_correct"] is False:
                result_state = "Lost"
            else:
                result_state = "Pending"
            table_data.append([
                str(row["match_date"]),
                row.get("match_time", ""),
                row["competition"],
                f"{row['home_team']} vs {row['away_team']}",
                row["tip"],
                f"{float(row['confidence']):.1f}%",
                "-" if row["odds"] is None else f"{float(row['odds']):.2f}",
                row.get("actual_score") or "-",
                result_state,
            ])
        table = Table(table_data, repeatRows=1, colWidths=[24 * mm, 18 * mm, 22 * mm, 62 * mm, 32 * mm, 18 * mm, 18 * mm, 18 * mm, 20 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)
        doc.build(story)
        response.write(buffer.getvalue())
        return response

    return HttpResponse("Invalid format", status=400)


def export_combo_history(request, format):
    context = build_combo_history_payload(request.GET.get("status", "all"))
    saved_slips = context["saved_slips"]
    status_filter = context["status_filter"]

    if format == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="combo_history_{status_filter}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Name", "Status", "Style", "Legs", "Market", "Avg Confidence", "Priced Legs", "Combined Odds", "Saved At"])
        for slip in saved_slips:
            writer.writerow([
                slip["name"],
                slip["slip_status_label"],
                slip["style"],
                slip["size"],
                slip["market_filter"],
                round(float(slip["average_confidence"] or 0), 1),
                slip["priced_legs"],
                "" if slip["combined_odds"] is None else slip["combined_odds"],
                slip["created_at"].strftime("%Y-%m-%d %H:%M"),
            ])
        return response

    if format == "pdf":
        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="combo_history_{status_filter}.pdf"'
        import io
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
        styles = getSampleStyleSheet()
        story = [Paragraph(f"Combo History - {status_filter.title()}", styles["Heading2"]), Spacer(1, 6 * mm)]
        table_data = [["Name", "Status", "Style", "Legs", "Market", "Avg Conf", "Priced", "Odds", "Saved"]]
        for slip in saved_slips:
            table_data.append([
                slip["name"],
                slip["slip_status_label"],
                slip["style"],
                str(slip["size"]),
                slip["market_filter"],
                f"{float(slip['average_confidence'] or 0):.1f}%",
                str(slip["priced_legs"]),
                "-" if slip["combined_odds"] is None else f"{float(slip['combined_odds']):.2f}",
                slip["created_at"].strftime("%Y-%m-%d %H:%M"),
            ])
        table = Table(table_data, repeatRows=1, colWidths=[52 * mm, 26 * mm, 20 * mm, 14 * mm, 36 * mm, 20 * mm, 18 * mm, 18 * mm, 30 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FF")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D6DCE8")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)
        doc.build(story)
        response.write(buffer.getvalue())
        return response

    return HttpResponse("Invalid format", status=400)


def backfill_viewids():
    matches = MatchPrediction.objects.all()
    for match in matches:
        if not getattr(match, "match_id", None):
            composite_id = f"{match.home_team}-{match.away_team}-{match.match_date}"
            # try to save to field name available (match.match_id or match.matchid)
            if hasattr(match, "match_id"):
                match.match_id = composite_id
            elif hasattr(match, "matchid"):
                match.matchid = composite_id
            match.save()
    print("Backfilling complete.")



from django.views.decorators.http import require_GET
from django.http import JsonResponse
from .models import MatchPrediction, TopPick

@require_GET
def api_predictions(request):
    competition = request.GET.get("competition")
    date_q = request.GET.get("date")

    predictions = MatchPrediction.objects.all()

    if competition:
        predictions = predictions.filter(competition=competition)

    if date_q:
        predictions = predictions.filter(match_date=date_q)

    data = [
        {
            "id": p.id,
            "competition": p.competition,
            "match_date": str(p.match_date),
            "home_team": p.home_team,
            "away_team": p.away_team,
            "predicted_home_goals": p.predicted_home_goals,
            "predicted_away_goals": p.predicted_away_goals,
            "actual_home_goals": p.actual_home_goals,
            "actual_away_goals": p.actual_away_goals,
            "status": p.status,
        }
        for p in predictions
    ]
    return JsonResponse({"predictions": data})

@require_GET
def api_top_picks(request):
    picks = TopPick.objects.all().order_by("-match_date")[:20]
    data = [
        {
            "home_team": p.home_team,
            "away_team": p.away_team,
            "tip": p.tip,
            "confidence": p.confidence,
            "odds": p.odds,
            "match_date": str(p.match_date),
            "is_correct": p.is_correct,
        }
        for p in picks
    ]
    return JsonResponse({"top_picks": data})

def league_table_api(request, competition_code):
    table = cache.get(f"league_table_{competition_code}", [])
    return JsonResponse({"table": table})
