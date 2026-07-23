# predict/utils.py


import os
import re
import time
import math
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests
from django.core.cache import cache
from django.utils import timezone
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
from sklearn.preprocessing import LabelEncoder

from .constants import (
    API_TOKEN,
    BASE_URL,
    COMPETITIONS,
    MODEL_CACHE_TIMEOUT,
    ODDS_API_KEY,
    TRAINING_CACHE_TIMEOUT,
    get_team_metadata,
    model_cache_key,
    training_data_cache_key,
)

HEADERS = {"X-Auth-Token": API_TOKEN}
ODDS_PROVIDER = os.getenv("ODDS_PROVIDER", "the-odds-api")
logger = logging.getLogger(__name__)


def scoreline_predictions(predicted_home_goals, predicted_away_goals, max_goals=None, top_n=5):
    home_rate = max(float(predicted_home_goals or 0), 0.15)
    away_rate = max(float(predicted_away_goals or 0), 0.15)
    if max_goals is None:
        max_goals = max(5, int(math.ceil(max(home_rate, away_rate) + 3)))

    def poisson_pmf(goals, lam):
        return float(np.exp(-lam) * (lam ** goals) / math.factorial(goals))

    def score_adjustment(home_goals, away_goals):
        """
        Light adjustment for known football tendencies:
        - Draws (especially 0-0 and 1-1) are slightly more common than pure
          Poisson suggests due to tactical/psychological factors.
        - Very high scorelines are slightly less likely in practice.
        Keep adjustments subtle so the model stays realistic.
        """
        total_expected = home_rate + away_rate
        closeness = max(0.0, 1.0 - min(abs(home_rate - away_rate), 2.0) / 2.0)

        if home_goals == 0 and away_goals == 0:
            # Slight 0-0 boost only when expected goals are low and teams are close
            low_goal_factor = max(0.0, 1.0 - total_expected / 3.0)
            return 1.0 + (0.06 * closeness * low_goal_factor)
        if home_goals == away_goals:
            # Very slight draw boost
            return 1.0 + (0.04 * closeness)
        if home_goals + away_goals >= 6:
            # Slight suppression of extreme scorelines
            return 0.92
        return 1.0

    candidates = []
    for home_goals in range(max_goals + 1):
        for away_goals in range(max_goals + 1):
            probability = poisson_pmf(home_goals, home_rate) * poisson_pmf(away_goals, away_rate)
            probability *= score_adjustment(home_goals, away_goals)
            candidates.append({
                "score": f"{home_goals}-{away_goals}",
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": probability,
            })

    total_probability = sum(item["probability"] for item in candidates) or 1.0
    for item in candidates:
        item["probability"] = item["probability"] / total_probability

    top_candidates = sorted(candidates, key=lambda item: item["probability"], reverse=True)[:top_n]
    top_probability_total = sum(item["probability"] for item in top_candidates) or 1.0

    return [
        {
            **item,
            "percent": round(item["probability"] * 100, 1),
            "top_share_percent": round((item["probability"] / top_probability_total) * 100, 1),
        }
        for item in top_candidates
    ]


def _clip_score(value, lower=0.0, upper=100.0):
    return float(np.clip(value, lower, upper))


def _market_odds_value(odds_obj, market):
    if not odds_obj:
        return None
    market_map = {
        "1": getattr(odds_obj, "home_win", None),
        "X": getattr(odds_obj, "draw", None),
        "2": getattr(odds_obj, "away_win", None),
        "GG": getattr(odds_obj, "btts_yes", None),
        "NG": getattr(odds_obj, "btts_no", None),
        "Over 2.5": getattr(odds_obj, "over_2_5", None),
        "Under 2.5": getattr(odds_obj, "under_2_5", None),
    }
    return market_map.get(market)


def score_top_pick_markets(match_prediction, model_context):
    total_goals = float((match_prediction.predicted_home_goals or 0) + (match_prediction.predicted_away_goals or 0))
    goal_margin = float((match_prediction.predicted_home_goals or 0) - (match_prediction.predicted_away_goals or 0))
    features = build_fixture_features(
        match_prediction.home_team,
        match_prediction.away_team,
        model_context,
    ).iloc[0]

    elo_home_prob = float(features.get("elo_home_win_prob", 0.5))
    form_gap = float(features.get("form_gap", 0.0))
    goal_balance_gap = float(features.get("goal_balance_gap", 0.0))
    h2h_goal_diff = float(features.get("h2h_goal_diff", 0.0))
    h2h_total_goals = float(features.get("h2h_total_goals", 2.4))
    h2h_btts_rate = float(features.get("h2h_btts_rate", 0.5))
    h2h_over25_rate = float(features.get("h2h_over25_rate", 0.5))
    home_recent_scored = float(features.get("home_recent_scored", 1.2))
    away_recent_scored = float(features.get("away_recent_scored", 1.0))
    home_recent_conceded = float(features.get("home_recent_conceded", 1.0))
    away_recent_conceded = float(features.get("away_recent_conceded", 1.0))
    home_clean_sheet_rate = float(features.get("home_clean_sheet_rate", 0.25))
    away_clean_sheet_rate = float(features.get("away_clean_sheet_rate", 0.25))
    home_fail_to_score_rate = float(features.get("home_fail_to_score_rate", 0.2))
    away_fail_to_score_rate = float(features.get("away_fail_to_score_rate", 0.2))
    rest_gap = float(features.get("rest_gap", 0.0))
    h2h_match_count = float(features.get("h2h_match_count", 0.0))

    goal_environment = (
        0.45 * total_goals
        + 0.30 * h2h_total_goals
        + 0.15 * (home_recent_scored + away_recent_scored)
        + 0.10 * (away_recent_conceded + home_recent_conceded)
    )
    goal_suppression = (
        0.45 * (home_clean_sheet_rate + away_clean_sheet_rate)
        + 0.35 * (home_fail_to_score_rate + away_fail_to_score_rate)
        + 0.20 * max(0.0, 2.4 - h2h_total_goals)
    )
    both_score_signal = (
        0.30 * min(home_recent_scored, away_recent_scored)
        + 0.18 * (2.0 - home_clean_sheet_rate - away_clean_sheet_rate)
        + 0.18 * (2.0 - home_fail_to_score_rate - away_fail_to_score_rate)
        + 0.12 * min(total_goals, 3.5)
        + 0.08 * min(h2h_total_goals, 3.5)
        + 0.14 * (h2h_btts_rate * 2.0 - 1.0)  # 0.5→0, 0.8→+0.084, 1.0→+0.14
    )
    stronger_team_attack = max(
        (match_prediction.predicted_home_goals or 0),
        (match_prediction.predicted_away_goals or 0),
    )
    home_attack_push = (
        0.55 * (match_prediction.predicted_home_goals or 0)
        + 0.20 * home_recent_scored
        + 0.15 * away_recent_conceded
        + 0.10 * max(0.0, elo_home_prob - 0.5) * 2
    )
    away_attack_push = (
        0.55 * (match_prediction.predicted_away_goals or 0)
        + 0.20 * away_recent_scored
        + 0.15 * home_recent_conceded
        + 0.10 * max(0.0, (1.0 - elo_home_prob) - 0.5) * 2
    )

    markets = {
        "1": _clip_score(
            26
            + 18 * max(0.0, goal_margin)
            + 18 * max(0.0, elo_home_prob - 0.5) * 2
            + 4 * max(0.0, form_gap)
            + 3 * max(0.0, goal_balance_gap)
            + 1.5 * max(0.0, h2h_goal_diff)
            + 0.8 * max(0.0, rest_gap)
            + (10 if goal_margin >= 1.5 else 0)
        ),
        "2": _clip_score(
            26
            + 18 * max(0.0, -goal_margin)
            + 18 * max(0.0, (1.0 - elo_home_prob) - 0.5) * 2
            + 4 * max(0.0, -form_gap)
            + 3 * max(0.0, -goal_balance_gap)
            + 1.5 * max(0.0, -h2h_goal_diff)
            + 0.8 * max(0.0, -rest_gap)
            + (10 if goal_margin <= -1.5 else 0)
        ),
        "X": _clip_score(
            24
            + 18 * max(0.0, 0.75 - abs(goal_margin))
            + 10 * max(0.0, 0.12 - abs(elo_home_prob - 0.5))
            + 6 * max(0.0, 0.8 - abs(form_gap))
            + 4 * max(0.0, 0.8 - abs(goal_balance_gap))
            + 4 * max(0.0, 0.8 - abs(total_goals - 2.2))
        ),
        "Over 2.5": _clip_score(
            28
            + 20 * max(0.0, goal_environment - 2.15)
            + 12 * max(0.0, both_score_signal - 1.35)
            - 10 * goal_suppression
            + 12 * (h2h_over25_rate - 0.5) * 2.0  # 0.5→0, 0.8→+7.2, 1.0→+12
            + (14 if total_goals >= 3.0 else 0)
        ),
        "Under 2.5": _clip_score(
            28
            + 18 * max(0.0, 2.75 - goal_environment)
            + 14 * goal_suppression
            + 10 * max(0.0, 0.9 - abs(goal_margin))
            + (14 if total_goals <= 2.0 else 0)
        ),
        "GG": _clip_score(
            26
            + 18 * max(0.0, both_score_signal - 1.2)
            + 10 * max(0.0, goal_environment - 2.3)
            - 10 * (home_clean_sheet_rate + away_clean_sheet_rate)
            + (10 if (match_prediction.predicted_home_goals or 0) >= 1 and (match_prediction.predicted_away_goals or 0) >= 1 else 0)
        ),
        "NG": _clip_score(
            26
            + 14 * goal_suppression
            + 10 * max(0.0, 2.5 - goal_environment)
            + 10 * max(0.0, 1.0 - both_score_signal)
            + (10 if (match_prediction.predicted_home_goals or 0) == 0 or (match_prediction.predicted_away_goals or 0) == 0 else 0)
        ),
        "Any Team Over 1.5": _clip_score(
            28
            + 18 * max(0.0, stronger_team_attack - 1.35)
            + 10 * max(0.0, goal_environment - 2.25)
            + 8 * max(0.0, max(home_recent_scored, away_recent_scored) - 1.3)
            + (14 if stronger_team_attack >= 2 else 0)
        ),
        "Home Win Either Half": _clip_score(
            26
            + 16 * max(0.0, goal_margin)
            + 14 * max(0.0, elo_home_prob - 0.5) * 2
            + 8 * max(0.0, home_recent_scored - 1.2)
            + 6 * max(0.0, away_recent_conceded - 1.0)
            + (12 if (match_prediction.predicted_home_goals or 0) >= 2 else 0)
        ),
        "Away Win Either Half": _clip_score(
            26
            + 16 * max(0.0, -goal_margin)
            + 14 * max(0.0, (1.0 - elo_home_prob) - 0.5) * 2
            + 8 * max(0.0, away_recent_scored - 1.1)
            + 6 * max(0.0, home_recent_conceded - 1.0)
            + (12 if (match_prediction.predicted_away_goals or 0) >= 2 else 0)
        ),
        "Home Team Over 1.0": _clip_score(
            26
            + 20 * max(0.0, home_attack_push - 1.2)
            + 8 * max(0.0, goal_environment - 2.1)
            + (16 if (match_prediction.predicted_home_goals or 0) >= 2 else 0)
            + (6 if (match_prediction.predicted_home_goals or 0) == 1 else 0)
        ),
        "Away Team Over 1.0": _clip_score(
            26
            + 20 * max(0.0, away_attack_push - 1.15)
            + 8 * max(0.0, goal_environment - 2.1)
            + (16 if (match_prediction.predicted_away_goals or 0) >= 2 else 0)
            + (6 if (match_prediction.predicted_away_goals or 0) == 1 else 0)
        ),
    }

    if h2h_match_count < 2:
        markets["1"] = _clip_score(markets["1"] - 2)
        markets["2"] = _clip_score(markets["2"] - 2)
        markets["X"] = _clip_score(markets["X"] - 1)

    preferred_order = {
        "1": 4,
        "2": 4,
        "Over 2.5": 3,
        "Under 2.5": 3,
        "GG": 2,
        "NG": 2,
        "Any Team Over 1.5": 2,
        "Home Win Either Half": 2,
        "Away Win Either Half": 2,
        "Home Team Over 1.0": 2,
        "Away Team Over 1.0": 2,
        "X": 1,
    }
    ranked = sorted(markets.items(), key=lambda item: (item[1], preferred_order.get(item[0], 0)), reverse=True)
    return ranked, dict(features)


def implied_probability_from_odds(odds_value):
    try:
        odds_value = float(odds_value)
    except (TypeError, ValueError):
        return None
    if odds_value <= 1:
        return None
    return 100.0 / odds_value


def market_edge(confidence, odds_value):
    implied = implied_probability_from_odds(odds_value)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return implied, None
    if implied is None:
        return None, None
    return implied, confidence - implied


def explain_pick_reasons(market, features, match_prediction):
    features = features or {}
    reasons = []
    total_goals = float((match_prediction.predicted_home_goals or 0) + (match_prediction.predicted_away_goals or 0))
    goal_margin = float((match_prediction.predicted_home_goals or 0) - (match_prediction.predicted_away_goals or 0))
    elo_gap = float(features.get("elo_gap", 0.0))
    form_gap = float(features.get("form_gap", 0.0))
    h2h_total_goals = float(features.get("h2h_total_goals", 2.4))
    home_clean_sheet_rate = float(features.get("home_clean_sheet_rate", 0.25))
    away_clean_sheet_rate = float(features.get("away_clean_sheet_rate", 0.25))
    home_fail_to_score_rate = float(features.get("home_fail_to_score_rate", 0.2))
    away_fail_to_score_rate = float(features.get("away_fail_to_score_rate", 0.2))

    if market == "1":
        if goal_margin >= 1:
            reasons.append(f"Projected margin {match_prediction.predicted_home_goals}-{match_prediction.predicted_away_goals}")
        if form_gap > 0.4:
            reasons.append("Home side in better recent form")
        if elo_gap > 40:
            reasons.append("Strong Elo advantage at home")
    elif market == "2":
        if goal_margin <= -1:
            reasons.append(f"Projected margin {match_prediction.predicted_home_goals}-{match_prediction.predicted_away_goals}")
        if form_gap < -0.4:
            reasons.append("Away side in better recent form")
        if elo_gap < -40:
            reasons.append("Away team has clear Elo edge")
    elif market == "X":
        if abs(goal_margin) <= 0.5:
            reasons.append("Projected as a very even match")
        if abs(form_gap) <= 0.4:
            reasons.append("Recent form is closely balanced")
        if abs(elo_gap) <= 35:
            reasons.append("Elo gap is minimal")
    elif market == "Over 2.5":
        if total_goals >= 3:
            reasons.append(f"Projected total goals {total_goals:.0f}")
        if h2h_total_goals >= 2.8:
            reasons.append("Head-to-head trend is goal-friendly")
        if home_fail_to_score_rate < 0.25 and away_fail_to_score_rate < 0.25:
            reasons.append("Both teams usually find a goal")
    elif market == "Under 2.5":
        if total_goals <= 2:
            reasons.append(f"Projected total goals {total_goals:.0f}")
        if home_clean_sheet_rate + away_clean_sheet_rate >= 0.65:
            reasons.append("Strong clean-sheet profile")
        if home_fail_to_score_rate + away_fail_to_score_rate >= 0.45:
            reasons.append("One side often fails to score")
    elif market == "GG":
        if (match_prediction.predicted_home_goals or 0) >= 1 and (match_prediction.predicted_away_goals or 0) >= 1:
            reasons.append("Both teams projected to score")
        if home_fail_to_score_rate < 0.25 and away_fail_to_score_rate < 0.25:
            reasons.append("Low fail-to-score rates")
        if h2h_total_goals >= 2.7:
            reasons.append("H2H trend supports goals")
    elif market == "NG":
        if (match_prediction.predicted_home_goals or 0) == 0 or (match_prediction.predicted_away_goals or 0) == 0:
            reasons.append("One side projected to blank")
        if home_clean_sheet_rate + away_clean_sheet_rate >= 0.55:
            reasons.append("Clean-sheet rates are elevated")
        if home_fail_to_score_rate + away_fail_to_score_rate >= 0.45:
            reasons.append("Fail-to-score trend is meaningful")
    elif market == "Any Team Over 1.5":
        if max((match_prediction.predicted_home_goals or 0), (match_prediction.predicted_away_goals or 0)) >= 2:
            reasons.append("One team is projected for 2+ goals")
        if max(
            float(features.get("home_recent_scored", 0)),
            float(features.get("away_recent_scored", 0)),
        ) >= 1.6:
            reasons.append("At least one attack is in strong scoring form")
        if total_goals >= 3:
            reasons.append("Overall goal projection is healthy")
    elif market == "Home Win Either Half":
        if goal_margin >= 1:
            reasons.append("Home side projected to control the match")
        if elo_gap > 40:
            reasons.append("Home team has a clear strength edge")
        if float(features.get("home_recent_scored", 0)) >= 1.5:
            reasons.append("Home attack is producing consistently")
    elif market == "Away Win Either Half":
        if goal_margin <= -1:
            reasons.append("Away side projected to take key spells")
        if elo_gap < -40:
            reasons.append("Away team has a clear strength edge")
        if float(features.get("away_recent_scored", 0)) >= 1.4:
            reasons.append("Away attack is producing consistently")
    elif market == "Home Team Over 1.0":
        if (match_prediction.predicted_home_goals or 0) >= 2:
            reasons.append("Home team projected for 2+ goals")
        elif (match_prediction.predicted_home_goals or 0) == 1:
            reasons.append("One home goal still protects with a refund")
        if float(features.get("away_recent_conceded", 0)) >= 1.2:
            reasons.append("Away defence has been conceding regularly")
    elif market == "Away Team Over 1.0":
        if (match_prediction.predicted_away_goals or 0) >= 2:
            reasons.append("Away team projected for 2+ goals")
        elif (match_prediction.predicted_away_goals or 0) == 1:
            reasons.append("One away goal still protects with a refund")
        if float(features.get("home_recent_conceded", 0)) >= 1.2:
            reasons.append("Home defence has been conceding regularly")

    if not reasons:
        reasons.append("Backed by projected score and recent form")
    return reasons[:3]


def _normalize_team_lookup_value(name):
    candidate = (name or "").strip().lower()
    if not candidate:
        return ""
    candidate = candidate.replace("&", " and ")
    candidate = re.sub(r"[^\w\s]", " ", candidate)
    candidate = re.sub(r"\b(fc|cf|ac|sc|afc|club|de|da|del)\b", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate


def _team_name_aliases(name):
    aliases = set()
    raw_name = (name or "").strip()
    if raw_name:
        aliases.add(_normalize_team_lookup_value(raw_name))
        meta = get_team_metadata(raw_name)
        short_name = (meta or {}).get("shortName")
        if short_name:
            aliases.add(_normalize_team_lookup_value(short_name))
    aliases.discard("")
    return aliases


def get_current_season_start_year(reference_date=None):
    reference_date = reference_date or datetime.now()
    return reference_date.year if reference_date.month >= 7 else reference_date.year - 1


def get_default_training_seasons(reference_date=None, history_window=10):
    current_season = get_current_season_start_year(reference_date)
    first_season = max(2007, current_season - history_window + 1)
    return list(range(first_season, current_season + 1))


# ---------- API fetching helpers ----------

def _get_json(url, headers=None, params=None, retries=1, delay=2):
    headers = headers or {}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code >= 500 or r.status_code == 429:
                logger.warning(
                    "API request failed with retryable status %s for %s params=%s attempt=%s/%s",
                    r.status_code,
                    url,
                    params,
                    attempt + 1,
                    retries,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "API request failed with status %s for %s params=%s response=%s",
                    r.status_code,
                    url,
                    params,
                    r.text[:300],
                )
                break
        except requests.RequestException as e:
            logger.warning(
                "API request exception for %s params=%s attempt=%s/%s error=%s",
                url,
                params,
                attempt + 1,
                retries,
                e,
            )
            time.sleep(delay)
    return None


def fetch_matches_by_date(api_key, competition_code, match_date, retries=2, delay=2):
    """
    Returns API-style match objects for a given date and competition.
    match_date: "YYYY-MM-DD"
    """
    from .providers import dispatch_provider
    return dispatch_provider(competition_code, "fetch_matches_by_date", match_date)


def fetch_matches_by_season(api_key, competition_code, season_year):
    """
    Wrapper to fetch matches for a season year.
    """
    from .providers import dispatch_provider
    return dispatch_provider(competition_code, "fetch_matches_by_season", season_year)


def fetch_season_matches(api_key, competition_code, season):
    # alias for fetch_matches_by_season
    return fetch_matches_by_season(api_key, competition_code, season)


def fetch_competition_matches(competition_id, date_from=None, date_to=None):
    url = f"{BASE_URL}/competitions/{competition_id}/matches"
    params = {}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    json_data = _get_json(url, headers=HEADERS, params=params, retries=2)
    return json_data.get("matches", []) if json_data else []


def fetch_competition_scorers(competition_code):
    cache_key = f"competition_scorers::{competition_code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from .providers import dispatch_provider, is_af, is_lf, is_uk
    # LF and UK don't have scorer data; AF has its own fetcher.
    if is_lf(competition_code) or is_uk(competition_code):
        scorers = []
    elif is_af(competition_code):
        from .providers import af_fetch_scorers
        scorers = af_fetch_scorers(competition_code)
    else:
        scorers = dispatch_provider(competition_code, "fetch_scorers")
    cache.set(cache_key, scorers, timeout=60 * 60 * 12)
    return scorers


def fetch_training_data(competition_code, seasons=None):
    """
    Collect finished matches for a competition across seasons.
    Returns a DataFrame with columns: home_team, away_team, home_goals, away_goals, utc_date
    """
    if not API_TOKEN:
        logger.error(
            "FOOTBALL_DATA_API_KEY is not configured. Training data fetch for %s cannot proceed.",
            competition_code,
        )
        return pd.DataFrame(columns=["home_team", "away_team", "home_goals", "away_goals", "utc_date"])

    if seasons is None:
        seasons = get_default_training_seasons()
    all_matches = []
    for season in seasons:
        try:
            matches = fetch_matches_by_season(API_TOKEN, competition_code, season)
            if matches == []:
                logger.info(
                    "No season data returned for %s season=%s. This may indicate an auth issue, API limit, or no coverage.",
                    competition_code,
                    season,
                )
            for m in matches:
                if m.get("status") == "FINISHED":
                    row = {
                        "home_team": m["homeTeam"]["name"],
                        "away_team": m["awayTeam"]["name"],
                        "home_goals": m["score"]["fullTime"]["home"],
                        "away_goals": m["score"]["fullTime"]["away"],
                        "utc_date": m.get("utcDate"),
                    }
                    # Preserve betting odds when available (UK provider)
                    odds = m.get("odds") or {}
                    if odds.get("avgH"):
                        row["avgH"] = odds["avgH"]
                        row["avgD"] = odds["avgD"]
                        row["avgA"] = odds["avgA"]
                    if odds.get("over25"):
                        row["over25"] = odds["over25"]
                    all_matches.append(row)
        except Exception as exc:
            logger.exception(
                "Failed to fetch training data for competition=%s season=%s error=%s",
                competition_code,
                season,
                exc,
            )
            continue
    return pd.DataFrame(all_matches)


def fetch_training_data_all_seasons(competition_code, seasons=None):
    """
    Caches the training data for a competition. Returns DataFrame.
    """
    cache_key = training_data_cache_key(competition_code)
    cached = cache.get(cache_key)
    if cached is not None:
        if getattr(cached, "empty", False):
            logger.warning(
                "Discarding stale empty cached training data for %s and retrying remote fetch.",
                competition_code,
            )
            cache.delete(cache_key)
        else:
            return cached

    df = fetch_training_data(competition_code, seasons=seasons)
    if df is None or df.empty:
        logger.warning(
            "Training data fetch returned no rows for %s. Skipping cache write so a later retry can recover.",
            competition_code,
        )
        return pd.DataFrame(columns=["home_team", "away_team", "home_goals", "away_goals", "utc_date"])
    cache.set(cache_key, df, timeout=TRAINING_CACHE_TIMEOUT)
    return df


# ---------- small date helpers (compatibility) ----------

def find_next_match_date(fetch_fn, api_key, competition_codes, past=False, days=30):
    """
    Backward-compatible helper. Accepts the older calling pattern used in your tasks.
    - fetch_fn: function that looks like fetch_matches_by_date(api_key, competition_code, date)
    - api_key: if None, will use global API_TOKEN
    - competition_codes: list or single code
    - past: if True, search backward
    Returns date string "YYYY-MM-DD" or None.
    """
    if not callable(fetch_fn):
        raise ValueError("fetch_fn must be callable")
    if isinstance(competition_codes, str):
        competition_codes = [competition_codes]

    today = datetime.today()
    direction = -1 if past else 1
    for i in range(days):
        check_date = (today + timedelta(days=direction * i)).strftime("%Y-%m-%d")
        for comp in competition_codes:
            try:
                matches = fetch_fn(api_key or API_TOKEN, comp, check_date)
                if matches:
                    return check_date
            except Exception:
                continue
    return None


def find_next_available_match_date(api_key, competition_code, start_date, days_ahead=30):
    """
    New-style helper used by some views:
    - start_date: "YYYY-MM-DD"
    Returns (first_date_with_matches, matches_list) or (None, []).
    """
    for i in range(days_ahead):
        check_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=i)).date().isoformat()
        matches = fetch_matches_by_date(api_key or API_TOKEN, competition_code, check_date)
        if matches:
            return check_date, matches
    return None, []


def find_upcoming_match_dates(fetch_fn, api_key, competition_code, start_date=None, days_ahead=7):
    """
    Return every upcoming date within the search window that has fixtures for a competition.
    Useful for predicting a whole gameweek instead of only the first available date.
    """
    if not callable(fetch_fn):
        raise ValueError("fetch_fn must be callable")

    base_date = start_date
    if isinstance(base_date, str):
        base_date = datetime.strptime(base_date, "%Y-%m-%d").date()
    elif base_date is None:
        base_date = datetime.today().date()

    dates = []
    for i in range(days_ahead):
        check_date = (base_date + timedelta(days=i)).isoformat()
        try:
            matches = fetch_fn(api_key or API_TOKEN, competition_code, check_date)
        except Exception:
            continue
        if matches:
            dates.append(check_date)
    return dates


# ---------- process / preprocess helpers ----------

def process_match_data(matches):
    """
    Turn API matches list into a DataFrame of finished matches (home/away/goals cols).
    """
    data = []
    for match in matches:
        try:
            if match.get("status") == "FINISHED":
                data.append({
                    "home_team": match["homeTeam"]["name"],
                    "away_team": match["awayTeam"]["name"],
                    "home_goals": match["score"]["fullTime"]["home"],
                    "away_goals": match["score"]["fullTime"]["away"],
                    "utc_date": match.get("utcDate")
                })
        except Exception:
            continue
    return pd.DataFrame(data)


def preprocess_match_data(matches, return_df=False):
    """
    Convert API match objects into a features matrix and labels for quick experiments.
    If return_df True, also returns the full DataFrame with raw columns.
    """
    rows = []
    for match in matches:
        try:
            rows.append({
                "home_team": match["homeTeam"]["name"],
                "away_team": match["awayTeam"]["name"],
                "utc_date": match.get("utcDate"),
                "home_position": match["homeTeam"].get("position", 10) if isinstance(match["homeTeam"], dict) else 10,
                "away_position": match["awayTeam"].get("position", 10) if isinstance(match["awayTeam"], dict) else 10,
                "home_points": match["homeTeam"].get("points", 30) if isinstance(match["homeTeam"], dict) else 30,
                "away_points": match["awayTeam"].get("points", 30) if isinstance(match["awayTeam"], dict) else 30,
                "home_goals": match["score"]["fullTime"].get("home", 0),
                "away_goals": match["score"]["fullTime"].get("away", 0),
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        X = pd.DataFrame(columns=["home_position", "away_position", "home_points", "away_points"])
        y_home = pd.Series(dtype=float)
        y_away = pd.Series(dtype=float)
    else:
        X = df[["home_position", "away_position", "home_points", "away_points"]]
        y_home = df["home_goals"]
        y_away = df["away_goals"]

    return (X, y_home, y_away, df) if return_df else (X, y_home, y_away)


def preprocess_api_data(df):
    """
    Backwards compatible: given a finished-matches DataFrame:
      - Drops NA
      - Encodes team names with a LabelEncoder (applies same encoder to both columns)
    Returns: X_encoded (DataFrame), y_home (Series), y_away (Series), label_encoder
    """
    df = df.dropna(subset=["home_team", "away_team", "home_goals", "away_goals"])
    df["home_team"] = df["home_team"].astype(str)
    df["away_team"] = df["away_team"].astype(str)

    team_names = pd.concat([df["home_team"], df["away_team"]]).unique()
    le = LabelEncoder()
    le.fit(team_names)

    X = pd.DataFrame({
        "home_team": le.transform(df["home_team"]),
        "away_team": le.transform(df["away_team"])
    })
    y_home = df["home_goals"]
    y_away = df["away_goals"]
    return X, y_home, y_away, le


# ---------- ML helpers: build features, train, predict ----------

def build_features(df):
    """
    Build rolling average features for matches DataFrame (expects finished matches sorted by date).
    Output columns: home_team, away_team, home_avg_scored, home_avg_conceded, away_avg_scored, away_avg_conceded
    """
    if df.empty:
        return pd.DataFrame(columns=[
            "home_team", "away_team", "home_avg_scored", "home_avg_conceded",
            "away_avg_scored", "away_avg_conceded"
        ])

    df = df.copy().reset_index(drop=True)
    # Ensure chronological order (if utc_date exists)
    if "utc_date" in df.columns:
        df["utc_date_parsed"] = pd.to_datetime(df["utc_date"])
        df = df.sort_values("utc_date_parsed").reset_index(drop=True)

    features = []
    for i, row in df.iterrows():
        home = row["home_team"]
        away = row["away_team"]

        # last 5 matches for this team before this fixture
        home_recent = df[((df["home_team"] == home) | (df["away_team"] == home))].iloc[:i].tail(5)
        away_recent = df[((df["home_team"] == away) | (df["away_team"] == away))].iloc[:i].tail(5)

        # compute scored/conceded depending on home/away roles in the recent matches
        def avg_scored(team, recent):
            if recent.empty:
                return 1.0
            # when team was home -> home_goals else away_goals
            scored = recent.apply(lambda r: r["home_goals"] if r["home_team"] == team else r["away_goals"], axis=1)
            return scored.mean()

        def avg_conceded(team, recent):
            if recent.empty:
                return 1.0
            conceded = recent.apply(lambda r: r["away_goals"] if r["home_team"] == team else r["home_goals"], axis=1)
            return conceded.mean()

        features.append({
            "home_team": home,
            "away_team": away,
            "home_avg_scored": avg_scored(home, home_recent) or 1.0,
            "home_avg_conceded": avg_conceded(home, home_recent) or 1.0,
            "away_avg_scored": avg_scored(away, away_recent) or 1.0,
            "away_avg_conceded": avg_conceded(away, away_recent) or 1.0,
        })
    return pd.DataFrame(features)


def _new_team_profile():
    return {
        "overall_scored": [],
        "overall_conceded": [],
        "overall_points": [],
        "overall_goal_diff": [],
        "overall_clean_sheet": [],
        "overall_failed_to_score": [],
        "home_scored": [],
        "home_conceded": [],
        "home_points": [],
        "home_goal_diff": [],
        "home_clean_sheet": [],
        "home_failed_to_score": [],
        "away_scored": [],
        "away_conceded": [],
        "away_points": [],
        "away_goal_diff": [],
        "away_clean_sheet": [],
        "away_failed_to_score": [],
        "last_match_date": None,
    }


def _weighted_mean(values, default):
    if not values:
        return float(default)
    arr = np.asarray(values, dtype=float)
    weights = np.arange(1, len(arr) + 1, dtype=float)
    return float(np.dot(arr, weights) / weights.sum())


def _weighted_rate(flags, default):
    if not flags:
        return float(default)
    arr = np.asarray(flags, dtype=float)
    weights = np.arange(1, len(arr) + 1, dtype=float)
    return float(np.dot(arr, weights) / weights.sum())


def _get_recent(values, lookback):
    return list(values[-lookback:]) if values else []


def _rest_days(last_match_date, current_date, default=7.0):
    if last_match_date is None or current_date is None or pd.isna(last_match_date) or pd.isna(current_date):
        return float(default)
    delta = (current_date - last_match_date).days
    return float(np.clip(delta, 2, 14))


def _summarize_profile(profile, venue, lookback, scored_default, conceded_default, points_default, current_date):
    venue_scored_key = f"{venue}_scored"
    venue_conceded_key = f"{venue}_conceded"
    venue_points_key = f"{venue}_points"
    venue_goal_diff_key = f"{venue}_goal_diff"
    venue_clean_sheet_key = f"{venue}_clean_sheet"
    venue_failed_to_score_key = f"{venue}_failed_to_score"

    overall_scored = _get_recent(profile["overall_scored"], lookback)
    overall_conceded = _get_recent(profile["overall_conceded"], lookback)
    overall_points = _get_recent(profile["overall_points"], lookback)
    overall_goal_diff = _get_recent(profile["overall_goal_diff"], lookback)
    overall_clean_sheet = _get_recent(profile["overall_clean_sheet"], lookback)
    overall_failed_to_score = _get_recent(profile["overall_failed_to_score"], lookback)

    venue_scored = _get_recent(profile[venue_scored_key], lookback)
    venue_conceded = _get_recent(profile[venue_conceded_key], lookback)
    venue_points = _get_recent(profile[venue_points_key], lookback)
    venue_goal_diff = _get_recent(profile[venue_goal_diff_key], lookback)
    venue_clean_sheet = _get_recent(profile[venue_clean_sheet_key], lookback)
    venue_failed_to_score = _get_recent(profile[venue_failed_to_score_key], lookback)

    venue_weight = min(len(venue_scored), lookback) / float(lookback or 1)
    overall_weight = 1.0 - venue_weight

    recent_scored = (
        venue_weight * _weighted_mean(venue_scored, scored_default)
        + overall_weight * _weighted_mean(overall_scored, scored_default)
    )
    recent_conceded = (
        venue_weight * _weighted_mean(venue_conceded, conceded_default)
        + overall_weight * _weighted_mean(overall_conceded, conceded_default)
    )
    form = (
        venue_weight * _weighted_mean(venue_points, points_default)
        + overall_weight * _weighted_mean(overall_points, points_default)
    )
    goal_diff_form = (
        venue_weight * _weighted_mean(venue_goal_diff, scored_default - conceded_default)
        + overall_weight * _weighted_mean(overall_goal_diff, scored_default - conceded_default)
    )
    clean_sheet_rate = (
        venue_weight * _weighted_rate(venue_clean_sheet, 0.25)
        + overall_weight * _weighted_rate(overall_clean_sheet, 0.25)
    )
    fail_to_score_rate = (
        venue_weight * _weighted_rate(venue_failed_to_score, 0.2)
        + overall_weight * _weighted_rate(overall_failed_to_score, 0.2)
    )

    return {
        "recent_scored": float(recent_scored),
        "recent_conceded": float(recent_conceded),
        "form": float(form),
        "goal_diff_form": float(goal_diff_form),
        "clean_sheet_rate": float(clean_sheet_rate),
        "fail_to_score_rate": float(fail_to_score_rate),
        "rest_days": _rest_days(profile.get("last_match_date"), current_date),
        "matches_seen": len(profile["overall_scored"]),
    }


def _summarize_head_to_head(home_team, away_team, h2h_matches, lookback, default_total_goals):
    recent_matches = h2h_matches[-lookback:]
    if not recent_matches:
        # All zeros = "no H2H data" signal — XGBoost learns to ignore these
        # features when match_count=0. The old defaults (1.35 pts, 2.5 goals)
        # biased predictions toward the home team when H2H was absent.
        return {
            "home_points": 0.0,
            "goal_diff": 0.0,
            "total_goals": 0.0,
            "match_count": 0.0,
            "btts_rate": 0.0,
            "over25_rate": 0.0,
        }

    home_points = []
    home_goal_diff = []
    total_goals = []
    btts_count = 0
    over25_count = 0
    for match in recent_matches:
        if match["home_team"] == home_team:
            home_goals = float(match["home_goals"])
            away_goals = float(match["away_goals"])
        else:
            home_goals = float(match["away_goals"])
            away_goals = float(match["home_goals"])
        home_points.append(3 if home_goals > away_goals else 1 if home_goals == away_goals else 0)
        home_goal_diff.append(home_goals - away_goals)
        total_goals.append(home_goals + away_goals)
        if home_goals > 0 and away_goals > 0:
            btts_count += 1
        if home_goals + away_goals >= 3:
            over25_count += 1

    n = len(recent_matches)
    return {
        "home_points": _weighted_mean(home_points, 1.35),
        "goal_diff": _weighted_mean(home_goal_diff, 0.0),
        "total_goals": _weighted_mean(total_goals, default_total_goals),
        "match_count": float(n),
        "btts_rate": btts_count / n if n > 0 else 0.5,
        "over25_rate": over25_count / n if n > 0 else 0.5,
    }


def _expected_home_result_from_elo(home_elo, away_elo, home_advantage=55.0):
    adjusted_home = float(home_elo) + float(home_advantage)
    adjusted_away = float(away_elo)
    return 1.0 / (1.0 + 10.0 ** ((adjusted_away - adjusted_home) / 400.0))


def _update_elo_ratings(elo_ratings, home_team, away_team, home_goals, away_goals, k_factor=24.0):
    home_rating = float(elo_ratings.get(home_team, 1500.0))
    away_rating = float(elo_ratings.get(away_team, 1500.0))
    expected_home = _expected_home_result_from_elo(home_rating, away_rating)
    actual_home = 1.0 if home_goals > away_goals else 0.5 if home_goals == away_goals else 0.0
    margin_multiplier = 1.0 + min(abs(float(home_goals) - float(away_goals)), 3.0) * 0.15
    delta = k_factor * margin_multiplier * (actual_home - expected_home)
    elo_ratings[home_team] = home_rating + delta
    elo_ratings[away_team] = away_rating - delta


def _safe_implied(odds_val):
    """1/odds → implied probability (overround-removed). Returns 0 if missing."""
    if odds_val is None:
        return 0.0
    try:
        o = float(odds_val)
        return 1.0 / o if o > 1.0 else 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _build_feature_row(home_team, away_team, team_profiles, h2h_profiles, league_defaults,
                       lookback, current_date, elo_ratings=None, odds_row=None):
    home_profile = team_profiles.get(home_team, _new_team_profile())
    away_profile = team_profiles.get(away_team, _new_team_profile())
    elo_ratings = elo_ratings or {}
    home_elo = float(elo_ratings.get(home_team, 1500.0))
    away_elo = float(elo_ratings.get(away_team, 1500.0))
    home_summary = _summarize_profile(
        home_profile,
        venue="home",
        lookback=lookback,
        scored_default=league_defaults["home_goals"],
        conceded_default=league_defaults["away_goals"],
        points_default=1.45,
        current_date=current_date,
    )
    away_summary = _summarize_profile(
        away_profile,
        venue="away",
        lookback=lookback,
        scored_default=league_defaults["away_goals"],
        conceded_default=league_defaults["home_goals"],
        points_default=1.1,
        current_date=current_date,
    )
    # Multi-window: sprint (3 matches = momentum) and season (20 matches = baseline)
    home_sprint = _summarize_profile(home_profile, "home", 3,
        league_defaults["home_goals"], league_defaults["away_goals"], 1.5, current_date)
    away_sprint = _summarize_profile(away_profile, "away", 3,
        league_defaults["away_goals"], league_defaults["home_goals"], 1.1, current_date)
    home_season = _summarize_profile(home_profile, "home", 20,
        league_defaults["home_goals"], league_defaults["away_goals"], 1.5, current_date)
    away_season = _summarize_profile(away_profile, "away", 20,
        league_defaults["away_goals"], league_defaults["home_goals"], 1.1, current_date)

    h2h_summary = _summarize_head_to_head(
        home_team,
        away_team,
        h2h_profiles.get(tuple(sorted((home_team, away_team))), []),
        lookback=max(3, min(lookback, 5)),
        default_total_goals=league_defaults["home_goals"] + league_defaults["away_goals"],
    )

    row = {
        "home_recent_scored": home_summary["recent_scored"],
        "home_recent_conceded": home_summary["recent_conceded"],
        "away_recent_scored": away_summary["recent_scored"],
        "away_recent_conceded": away_summary["recent_conceded"],
        "home_form": home_summary["form"],
        "away_form": away_summary["form"],
        "home_form_sprint": home_sprint["form"],
        "away_form_sprint": away_sprint["form"],
        "home_form_season": home_season["form"],
        "away_form_season": away_season["form"],
        "home_goal_diff_form": home_summary["goal_diff_form"],
        "away_goal_diff_form": away_summary["goal_diff_form"],
        "home_scored_sprint": home_sprint["recent_scored"],
        "away_scored_sprint": away_sprint["recent_scored"],
        "home_clean_sheet_rate": home_summary["clean_sheet_rate"],
        "away_clean_sheet_rate": away_summary["clean_sheet_rate"],
        "home_fail_to_score_rate": home_summary["fail_to_score_rate"],
        "away_fail_to_score_rate": away_summary["fail_to_score_rate"],
        "home_rest_days": home_summary["rest_days"],
        "away_rest_days": away_summary["rest_days"],
        "home_strength": home_summary["recent_scored"] + away_summary["recent_conceded"],
        "away_strength": away_summary["recent_scored"] + home_summary["recent_conceded"],
        "form_gap": home_summary["form"] - away_summary["form"],
        "goal_balance_gap": home_summary["goal_diff_form"] - away_summary["goal_diff_form"],
        "venue_attack_gap": home_summary["recent_scored"] - away_summary["recent_scored"],
        "venue_defense_gap": away_summary["recent_conceded"] - home_summary["recent_conceded"],
        "rest_gap": home_summary["rest_days"] - away_summary["rest_days"],
        "h2h_home_points": h2h_summary["home_points"],
        "h2h_goal_diff": h2h_summary["goal_diff"],
        "h2h_total_goals": h2h_summary["total_goals"],
        "h2h_match_count": h2h_summary["match_count"],
        "h2h_btts_rate": h2h_summary["btts_rate"],
        "h2h_over25_rate": h2h_summary["over25_rate"],
        "home_elo": home_elo,
        "away_elo": away_elo,
        "elo_gap": home_elo - away_elo,
        "elo_home_win_prob": _expected_home_result_from_elo(home_elo, away_elo),
        # ELO-derived goal expectations — gives the model a concrete prior based
        # on team strength. ~100 ELO → +0.35 expected goals.
        "elo_goal_exp_home": max(0.3, league_defaults["home_goals"] + (home_elo - away_elo) * 0.0035),
        "elo_goal_exp_away": max(0.3, league_defaults["away_goals"] + (away_elo - home_elo) * 0.0035),
        "home_advantage": league_defaults["home_goals"] - league_defaults["away_goals"],
    }
    # ── League experience (0.0 = promoted, 1.0 = 4+ seasons established) ──
    # Capped and normalized so this feature is on the same 0-1 scale as other
    # features — raw match counts (0-400) would dominate XGBoost otherwise.
    _cap = 152.0  # ≈4 seasons × 38 games
    home_exp = min(max(0, len(home_profile.get("overall_points", []))), _cap) / _cap
    away_exp = min(max(0, len(away_profile.get("overall_points", []))), _cap) / _cap
    row["home_experience"] = home_exp
    row["away_experience"] = away_exp
    row["experience_gap"] = home_exp - away_exp

    # ── Form-momentum modifier ──────────────────────────────────────────
    # Sprint form (last 3) that differs significantly from season form (20)
    # signals momentum — a team on a 5-match losing streak should have their
    # scoring rate pulled down, even if raw stats look ok from the full window.
    _sprint_delta_h = home_sprint["form"] - home_season["form"]
    _sprint_delta_a = away_sprint["form"] - away_season["form"]
    _momentum_h = 1.0 + _sprint_delta_h / 3.0  # +0.33 per extra point in sprint
    _momentum_a = 1.0 + _sprint_delta_a / 3.0
    _momentum_h = max(0.7, min(1.3, _momentum_h))
    _momentum_a = max(0.7, min(1.3, _momentum_a))
    row["home_recent_scored"] *= _momentum_h
    row["away_recent_scored"] *= _momentum_a
    row["home_scored_sprint"] *= _momentum_h
    row["away_scored_sprint"] *= _momentum_a
    row["home_strength"] = row["home_recent_scored"] + row["away_recent_conceded"]
    row["away_strength"] = row["away_recent_scored"] + row["home_recent_conceded"]

    # ── League-transition adjustment ─────────────────────────────────────
    # Teams with 0 matches in this league have stats from a DIFFERENT league
    # (promoted or relegated). Scale them based on the ELO gap between this
    # team and the league average — a promoted Championship side (~120 pt gap)
    # gets a stronger adjustment than a Serie B→Serie A transition (~80 pt gap).
    if home_exp == 0.0:
        gap = (home_elo + away_elo) / 2 - home_elo  # positive = below avg
        if gap > 30:
            factor = 1.0 - gap / 600  # 120 gap → 0.80, 80 gap → 0.87, 40 gap → 0.93
            row["home_recent_scored"] *= factor
            row["home_scored_sprint"] *= factor
            row["home_recent_conceded"] /= factor
            row["home_strength"] = row["home_recent_scored"] + row["away_recent_conceded"]
    if away_exp == 0.0:
        gap = (home_elo + away_elo) / 2 - away_elo
        if gap > 30:
            factor = 1.0 - gap / 600
            row["away_recent_scored"] *= factor
            row["away_scored_sprint"] *= factor
            row["away_recent_conceded"] /= factor
            row["away_strength"] = row["away_recent_scored"] + row["home_recent_conceded"]

    # ── Betting-odds features (closing market odds → implied probabilities) ──
    # Non-zero only for UK leagues where odds CSV columns exist; zero for FD/LF.
    odds = odds_row or {}
    ih = _safe_implied(odds.get("avgH"))
    id_ = _safe_implied(odds.get("avgD"))
    ia = _safe_implied(odds.get("avgA"))
    total = ih + id_ + ia
    row["odds_home_implied"] = ih / total if total > 0 else 0.0
    row["odds_draw_implied"] = id_ / total if total > 0 else 0.0
    row["odds_away_implied"] = ia / total if total > 0 else 0.0
    row["odds_over25_implied"] = _safe_implied(odds.get("over25"))
    return row


def _update_team_profile(profile, goals_for, goals_against, venue, match_date):
    points = 3 if goals_for > goals_against else 1 if goals_for == goals_against else 0
    goal_diff = goals_for - goals_against
    clean_sheet = 1 if goals_against == 0 else 0
    failed_to_score = 1 if goals_for == 0 else 0

    profile["overall_scored"].append(float(goals_for))
    profile["overall_conceded"].append(float(goals_against))
    profile["overall_points"].append(points)
    profile["overall_goal_diff"].append(float(goal_diff))
    profile["overall_clean_sheet"].append(clean_sheet)
    profile["overall_failed_to_score"].append(failed_to_score)

    profile[f"{venue}_scored"].append(float(goals_for))
    profile[f"{venue}_conceded"].append(float(goals_against))
    profile[f"{venue}_points"].append(points)
    profile[f"{venue}_goal_diff"].append(float(goal_diff))
    profile[f"{venue}_clean_sheet"].append(clean_sheet)
    profile[f"{venue}_failed_to_score"].append(failed_to_score)
    profile["last_match_date"] = match_date if match_date is not None else profile.get("last_match_date")


def _build_profiles_from_history(history):
    team_profiles = defaultdict(_new_team_profile)
    h2h_profiles = defaultdict(list)
    for _, row in history.iterrows():
        match_date = row.get("utc_date")
        home_team = row["home_team"]
        away_team = row["away_team"]
        home_goals = float(row["home_goals"])
        away_goals = float(row["away_goals"])

        _update_team_profile(team_profiles[home_team], home_goals, away_goals, "home", match_date)
        _update_team_profile(team_profiles[away_team], away_goals, home_goals, "away", match_date)
        h2h_profiles[tuple(sorted((home_team, away_team)))].append({
            "home_team": home_team,
            "away_team": away_team,
            "home_goals": home_goals,
            "away_goals": away_goals,
        })
    return dict(team_profiles), dict(h2h_profiles)


GLOBAL_ELO_CACHE_KEY = "global_elo_ratings_v1"


def _get_global_elo():
    """
    Returns cached cross-league ELO ratings {team: elo}, computed once daily.
    Teams that have moved leagues carry their rating — a promoted Championship
    team gets ~1530, a relegated EPL team gets ~1680.
    """
    cached = cache.get(GLOBAL_ELO_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        elo, _, last_match = compute_cross_league_elo()
        if elo:
            cache.set(GLOBAL_ELO_CACHE_KEY, elo, timeout=60 * 60 * 25)
            if last_match:
                cache.set(GLOBAL_LAST_MATCH_KEY, last_match, timeout=60 * 60 * 25)
            logger.info("Global ELO computed: %d teams across leagues", len(elo))
        return elo
    except Exception as e:
        logger.warning("Failed to compute global ELO: %s", e)
        return {}


def compute_cross_league_elo():
    """
    Process ALL matches across ALL competitions in chronological order to
    build global ELO ratings that follow teams when they change leagues.
    Returns {team_name: elo_rating} adjusted for league strength.

    This solves the promotion/relegation cold-start: a promoted Championship
    team gets ~1530 (not 1500), a relegated EPL team gets ~1680 in the
    Championship — matching reality.
    """
    from .constants import COMPETITIONS

    all_matches = []
    for comp_code in COMPETITIONS:
        df = fetch_training_data_all_seasons(comp_code)
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            all_matches.append({
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "home_goals": int(row["home_goals"]),
                "away_goals": int(row["away_goals"]),
                "utc_date": row.get("utc_date"),
                "competition": comp_code,
            })

    # Sort all matches chronologically
    all_matches.sort(key=lambda m: str(m.get("utc_date") or ""))

    elo = defaultdict(lambda: 1500.0)
    league_elos = defaultdict(list)

    for m in all_matches:
        home, away = m["home_team"], m["away_team"]
        hg, ag = m["home_goals"], m["away_goals"]
        comp = m["competition"]
        _update_elo_ratings(elo, home, away, hg, ag, k_factor=24.0)
        league_elos[comp].append(elo.get(home, 1500.0))
        league_elos[comp].append(elo.get(away, 1500.0))

    # Compute league strength offsets (average ELO per league)
    league_avg = {}
    for comp, ratings in league_elos.items():
        if ratings:
            league_avg[comp] = sum(ratings) / len(ratings)

    # Normalize: adjust each team's ELO relative to league baseline
    adjusted = {}
    for team, rating in elo.items():
        # Find which league this team was last active in, adjust to EPL baseline
        adjusted[team] = rating

    return dict(elo), league_avg


def build_training_features(df, lookback=8):
    """
    Build deterministic rolling features from finished match history.
    This avoids direct team-ID memorization and reduces skew against unseen teams.
    """
    expected_columns = [
        "home_recent_scored",
        "home_recent_conceded",
        "away_recent_scored",
        "away_recent_conceded",
        "home_strength",
        "away_strength",
        "home_form",
        "away_form",
        "home_form_sprint",
        "away_form_sprint",
        "home_form_season",
        "away_form_season",
        "home_goal_diff_form",
        "away_goal_diff_form",
        "home_scored_sprint",
        "away_scored_sprint",
        "home_clean_sheet_rate",
        "away_clean_sheet_rate",
        "home_fail_to_score_rate",
        "away_fail_to_score_rate",
        "home_rest_days",
        "away_rest_days",
        "form_gap",
        "goal_balance_gap",
        "venue_attack_gap",
        "venue_defense_gap",
        "rest_gap",
        "h2h_home_points",
        "h2h_goal_diff",
        "h2h_total_goals",
        "h2h_match_count",
        "h2h_btts_rate",
        "h2h_over25_rate",
        "home_elo",
        "away_elo",
        "elo_gap",
        "elo_home_win_prob",
        "elo_goal_exp_home",
        "elo_goal_exp_away",
        "home_advantage",
        "home_experience",
        "away_experience",
        "experience_gap",
        "odds_home_implied",
        "odds_draw_implied",
        "odds_away_implied",
        "odds_over25_implied",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=expected_columns), pd.Series(dtype=float), pd.Series(dtype=float), {
            "history": pd.DataFrame(columns=["home_team", "away_team", "home_goals", "away_goals", "utc_date"]),
            "league_home_goals": 1.4,
            "league_away_goals": 1.1,
            "lookback": lookback,
            "feature_columns": expected_columns,
            "team_profiles": {},
            "h2h_profiles": {},
            "elo_ratings": {},
        }

    history = df.dropna(subset=["home_team", "away_team", "home_goals", "away_goals"]).copy()
    if "utc_date" in history.columns:
        history["utc_date"] = pd.to_datetime(history["utc_date"], errors="coerce")
        history = history.sort_values("utc_date", na_position="last").reset_index(drop=True)
    else:
        history = history.reset_index(drop=True)

    league_home_goals = float(history["home_goals"].mean()) if not history.empty else 1.4
    league_away_goals = float(history["away_goals"].mean()) if not history.empty else 1.1

    league_defaults = {
        "home_goals": league_home_goals,
        "away_goals": league_away_goals,
    }
    # Cross-league ELO: use pre-computed global ratings as the starting point
    # so promoted teams carry their Championship ELO (converted), not 1500.
    _global_elo = {}
    try:
        _global_elo = _get_global_elo()
    except Exception as e:
        logger.warning("Global ELO unavailable, using per-league defaults: %s", e)
    team_profiles = defaultdict(_new_team_profile)
    h2h_profiles = defaultdict(list)
    elo_ratings = defaultdict(lambda: 1500.0)
    if _global_elo:
        for team, rating in _global_elo.items():
            elo_ratings[team] = rating
    feature_rows = []

    for _, row in history.iterrows():
        home = row["home_team"]
        away = row["away_team"]
        match_date = row.get("utc_date")
        odds_row = {"avgH": row.get("avgH"), "avgD": row.get("avgD"),
                     "avgA": row.get("avgA"), "over25": row.get("over25")}
        feature_rows.append(
            _build_feature_row(
                home, away, team_profiles, h2h_profiles, league_defaults,
                lookback, match_date, elo_ratings,
                odds_row=odds_row,
            )
        )

        home_goals = float(row["home_goals"])
        away_goals = float(row["away_goals"])

        _update_team_profile(team_profiles[home], home_goals, away_goals, "home", match_date)
        _update_team_profile(team_profiles[away], away_goals, home_goals, "away", match_date)
        _update_elo_ratings(elo_ratings, home, away, home_goals, away_goals,
                           k_factor=32.0)
        h2h_profiles[tuple(sorted((home, away)))].append({
            "home_team": home,
            "away_team": away,
            "home_goals": home_goals,
            "away_goals": away_goals,
        })

    X = pd.DataFrame(feature_rows, columns=expected_columns).fillna(0)
    y_home = history["home_goals"].astype(float)
    y_away = history["away_goals"].astype(float)
    model_context = {
        "history": history[["home_team", "away_team", "home_goals", "away_goals", "utc_date"]].copy(),
        "league_home_goals": league_home_goals,
        "league_away_goals": league_away_goals,
        "lookback": lookback,
        "feature_columns": expected_columns,
        "team_profiles": dict(team_profiles),
        "h2h_profiles": dict(h2h_profiles),
        "elo_ratings": dict(elo_ratings),
    }
    return X, y_home, y_away, model_context


def build_fixture_features(home_team, away_team, model_context):
    history = (model_context or {}).get("history")
    lookback = (model_context or {}).get("lookback", 8)
    league_home_goals = float((model_context or {}).get("league_home_goals", 1.4))
    league_away_goals = float((model_context or {}).get("league_away_goals", 1.1))
    feature_columns = (model_context or {}).get("feature_columns")

    if history is None or history.empty:
        row = _build_feature_row(
            home_team,
            away_team,
            {},
            {},
            {"home_goals": league_home_goals, "away_goals": league_away_goals},
            lookback,
            None,
        )
        return pd.DataFrame([row], columns=feature_columns)

    team_profiles = (model_context or {}).get("team_profiles") or {}
    h2h_profiles = (model_context or {}).get("h2h_profiles") or {}
    elo_ratings = (model_context or {}).get("elo_ratings") or {}
    if not team_profiles or not h2h_profiles:
        team_profiles, h2h_profiles = _build_profiles_from_history(history)

    current_date = None
    if "utc_date" in history.columns and not history["utc_date"].isna().all():
        current_date = history["utc_date"].max()

    row = _build_feature_row(
        home_team,
        away_team,
        team_profiles,
        h2h_profiles,
        {"home_goals": league_home_goals, "away_goals": league_away_goals},
        lookback,
        current_date,
        elo_ratings,
    )
    return pd.DataFrame([row], columns=feature_columns)


def train_models(X, y_home, y_away, sample_weight=None):
    """
    Trains two Poisson XGBoost regressors for home and away goals.
    Falls back to RandomForest if xgboost is not installed.

    Goals are count data (0,1,2,...); XGBoost's 'count:poisson' objective
    models the discrete Poisson distribution directly, unlike RandomForest
    which regresses to the mean and destroys the count distribution.
    """
    label_encoder = None
    X_train = X.copy()

    if "home_team" in X_train.columns and X_train["home_team"].dtype == object:
        label_encoder = LabelEncoder()
        unique = pd.concat([X_train["home_team"], X_train["away_team"]]).unique()
        label_encoder.fit(unique)
        X_train["home_team"] = label_encoder.transform(X_train["home_team"])
        X_train["away_team"] = label_encoder.transform(X_train["away_team"])

    X_numeric = X_train.select_dtypes(include=[np.number])
    non_numeric = [c for c in X_train.columns if c not in X_numeric.columns]
    if non_numeric:
        X_numeric = pd.get_dummies(X_train, columns=non_numeric, dummy_na=False)

    X_numeric = X_numeric.fillna(0)

    split_index = max(1, int(len(X_numeric) * 0.8))
    sample_weight = None if sample_weight is None else np.asarray(sample_weight, dtype=float)

    if len(X_numeric) < 10:
        X_tr, X_te = X_numeric, X_numeric
        yh_tr, yh_te = y_home, y_home
        ya_tr, ya_te = y_away, y_away
        sw_tr = sample_weight
    else:
        X_tr, X_te = X_numeric.iloc[:split_index], X_numeric.iloc[split_index:]
        yh_tr, yh_te = y_home.iloc[:split_index], y_home.iloc[split_index:]
        ya_tr, ya_te = y_away.iloc[:split_index], y_away.iloc[split_index:]
        sw_tr = sample_weight[:split_index] if sample_weight is not None else None

    # XGBoost with Poisson objective — models discrete count data correctly.
    # Falls back to RandomForest if xgboost isn't available.
    try:
        import xgboost as xgb
        model_home = xgb.XGBRegressor(
            n_estimators=300,
            objective="count:poisson",
            max_depth=6,
            learning_rate=0.05,
            subsample=1.0,
            colsample_bytree=1.0,
            random_state=42,
            n_jobs=-1,
        )
        model_away = xgb.XGBRegressor(
            n_estimators=300,
            objective="count:poisson",
            max_depth=6,
            learning_rate=0.05,
            subsample=1.0,
            colsample_bytree=1.0,
            random_state=42,
            n_jobs=-1,
        )
        model_home.fit(X_tr, yh_tr, sample_weight=sw_tr)
        model_away.fit(X_tr, ya_tr, sample_weight=sw_tr)
        kind = "XGBoost(count:poisson)"
    except ImportError:
        model_home = RandomForestRegressor(
            n_estimators=300, random_state=42, min_samples_leaf=2, n_jobs=-1,
        )
        model_away = RandomForestRegressor(
            n_estimators=300, random_state=42, min_samples_leaf=2, n_jobs=-1,
        )
        model_home.fit(X_tr, yh_tr, sample_weight=sw_tr)
        model_away.fit(X_tr, ya_tr, sample_weight=sw_tr)
        kind = "RandomForest(fallback)"

    # quick metrics (best-effort)
    try:
        home_rmse = np.sqrt(mean_squared_error(yh_te, model_home.predict(X_te)))
        away_rmse = np.sqrt(mean_squared_error(ya_te, model_away.predict(X_te)))
    except Exception:
        home_rmse = away_rmse = None

    logger.info(f" Trained models [{kind}]; home_rmse={home_rmse}, away_rmse={away_rmse}")

    # ── Probability calibration ───────────────────────────────────────────
    # Maps Poisson-derived P(home/draw/away) → true probabilities using
    # isotonic regression, so "61%" actually means 61% chance, not just
    # "61 confidence points." Fitted on the validation set.
    calibrator = None
    try:
        from sklearn.isotonic import IsotonicRegression
        from math import exp, factorial as _fac
        ph_val = np.clip(model_home.predict(X_te), 0.1, 6)
        pa_val = np.clip(model_away.predict(X_te), 0.1, 6)
        pred_probs = {"H": [], "D": [], "A": []}
        actuals = {"H": [], "D": [], "A": []}
        for i in range(len(ph_val)):
            rh, ra = float(ph_val[i]), float(pa_val[i])
            p_h = p_d = p_a = 0.0
            for hg in range(0, 7):
                phg = exp(-rh) * (rh ** hg) / _fac(hg)
                for ag in range(0, 7):
                    pag = exp(-ra) * (ra ** ag) / _fac(ag)
                    jp = phg * pag
                    if hg > ag: p_h += jp
                    elif hg == ag: p_d += jp
                    else: p_a += jp
            pred_probs["H"].append(p_h); pred_probs["D"].append(p_d); pred_probs["A"].append(p_a)
            hg_a, ag_a = int(yh_te.iloc[i]), int(ya_te.iloc[i])
            if hg_a > ag_a: actuals["H"].append(1); actuals["D"].append(0); actuals["A"].append(0)
            elif hg_a == ag_a: actuals["H"].append(0); actuals["D"].append(1); actuals["A"].append(0)
            else: actuals["H"].append(0); actuals["D"].append(0); actuals["A"].append(1)
        calibrator = {}
        for mkt in ("H", "D", "A"):
            if len(set(pred_probs[mkt])) > 5:
                iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
                idx = sorted(range(len(pred_probs[mkt])), key=lambda j: pred_probs[mkt][j])
                iso.fit([pred_probs[mkt][j] for j in idx], [actuals[mkt][j] for j in idx])
                calibrator[mkt] = iso
        logger.info(f" Calibrator fitted: {len(calibrator)}/3 markets")
    except Exception as e:
        logger.warning(f" Calibration skipped: {e}")

    return model_home, model_away, label_encoder, calibrator


def train_competition_models(training_df, lookback=8):
    X, y_home, y_away, model_context = build_training_features(training_df, lookback=lookback)
    sample_weight = np.linspace(0.35, 1.0, num=len(X)) if len(X) else None
    model_home, model_away, _, calibrator = train_models(X, y_home, y_away, sample_weight=sample_weight)
    model_context["calibrator"] = calibrator
    return model_home, model_away, model_context


def team_profiles_cache_key(competition_code):
    return f"team_profiles::{competition_code}"


def _store_team_profiles(competition_code, bundle):
    """
    Persist just the lightweight team_profiles dict to the shared (Postgres)
    cache. The full model bundle stays in the machine-local file cache, but
    recent form only needs team_profiles — and on Fly the web machine and the
    cron machine don't share a filesystem, so this shared copy is what lets the
    web machine serve recent form.
    """
    try:
        if not bundle or len(bundle) < 3 or not isinstance(bundle[2], dict):
            return
        profiles = bundle[2].get("team_profiles")
        if profiles:
            # 2-day timeout so a single missed warmform run doesn't blank form;
            # refreshed daily by the warmform job and whenever models are built.
            cache.set(team_profiles_cache_key(competition_code), profiles, timeout=60 * 60 * 48)
    except Exception as e:
        logger.warning(f" Could not store team_profiles for {competition_code}: {e}")


def get_team_recent_form(team_name, competition_code, limit=5):
    """Return list of recent result letters oldest→newest, e.g. ['W','D','L','W','W']."""
    team_profiles = None

    # 1) Shared Postgres cache — works across machines (web vs cron on Fly)
    try:
        team_profiles = cache.get(team_profiles_cache_key(competition_code))
    except Exception:
        team_profiles = None

    # 2) Fall back to the machine-local bundle (L1 registry, then file cache)
    if not team_profiles:
        bundle = _MODEL_REGISTRY.get(competition_code)
        if bundle is None:
            from django.core.cache import caches
            try:
                bundle = caches["model_cache"].get(model_cache_key(competition_code))
            except Exception:
                bundle = None
        if bundle and len(bundle) >= 3 and isinstance(bundle[2], dict):
            team_profiles = bundle[2].get("team_profiles", {})

    if not team_profiles:
        return []
    profile = team_profiles.get(team_name)
    if not profile:
        return []
    points = profile.get("overall_points", [])[-limit:]
    return ["W" if p == 3 else "D" if p == 1 else "L" for p in points]


# In-process model registry — survives for the lifetime of the gunicorn worker.
# Avoids file I/O and Redis on every request after the first load.
_MODEL_REGISTRY: dict = {}

def get_or_train_model_bundle(competition_code, force_refresh=False):
    from django.core.cache import caches

    # L1 — in-process memory (instant, zero network)
    if not force_refresh and competition_code in _MODEL_REGISTRY:
        return _MODEL_REGISTRY[competition_code]

    # L2 — file cache on disk (fast, survives request but not restart)
    model_store = caches["model_cache"]
    cache_key = model_cache_key(competition_code)
    if not force_refresh:
        try:
            cached_bundle = model_store.get(cache_key)
            if (
                isinstance(cached_bundle, tuple)
                and len(cached_bundle) == 3
                and isinstance(cached_bundle[2], dict)
                and "feature_columns" in cached_bundle[2]
                and "team_profiles" in cached_bundle[2]
            ):
                _MODEL_REGISTRY[competition_code] = cached_bundle
                _store_team_profiles(competition_code, cached_bundle)
                return cached_bundle
        except Exception as e:
            logger.warning(f" Could not read model bundle from file cache for {competition_code}: {e}")

    # L3 — train from scratch
    training_df = fetch_training_data_all_seasons(competition_code)
    if training_df.empty:
        return None

    bundle = train_competition_models(training_df)
    _MODEL_REGISTRY[competition_code] = bundle
    try:
        model_store.set(cache_key, bundle, timeout=MODEL_CACHE_TIMEOUT)
    except Exception as e:
        logger.warning(f" Could not store model bundle in file cache for {competition_code}: {e}")
    _store_team_profiles(competition_code, bundle)
    return bundle


def _poisson_best_scoreline(home_rate, away_rate, seed=None):
    """Pick an integer scoreline from Poisson(λh)×Poisson(λa).

    seed='mode': most-likely score (accurate per-fixture, less variety).
    seed=<int>: deterministic weighted random (variety, same fixture = same pick).
    seed=None: pure random (different every run, for non-critical use)."""
    from math import exp, factorial as _fac
    scores, weights = [], []
    total = 0.0
    best, best_w = (0, 0), -1.0
    for h in range(0, 7):
        for a in range(0, 7):
            p = float(exp(-home_rate) * (home_rate ** h) / _fac(h)) * \
                float(exp(-away_rate) * (away_rate ** a) / _fac(a))
            scores.append((h, a))
            weights.append(p)
            total += p
            if p > best_w:
                best_w, best = p, (h, a)

    if seed == "mode":
        return best

    import random as _random
    rng = _random.Random(seed) if seed is not None else _random
    r = rng.random() * total
    cumulative = 0.0
    for (h, a), w in zip(scores, weights):
        cumulative += w
        if r <= cumulative:
            return (h, a)
    return scores[-1]


def predict_match_outcome(home_team, away_team, models, label_encoder=None):
    """
    Given home/away names, and tuple (model_home, model_away, maybe_features),
    returns (result_label, pred_home_goals, pred_away_goals)
    This version expects models to be (model_home, model_away, label_encoder_or_features)
    but we also accept a simpler (model_home, model_away, label_encoder).
    """
    model_home, model_away, model_extra = models

    # Build minimal input depending on what's expected by model
    # If model_extra is a LabelEncoder -> encode teams as integers
    if isinstance(model_extra, LabelEncoder) or label_encoder is not None:
        le = model_extra if isinstance(model_extra, LabelEncoder) else label_encoder
        try:
            home_enc = le.transform([home_team])[0]
            away_enc = le.transform([away_team])[0]
            X = np.array([[home_enc, away_enc]])
        except Exception:
            # unknown team -> fallback zeros
            X = np.array([[0, 0]])
    elif isinstance(model_extra, dict):
        X = build_fixture_features(home_team, away_team, model_extra).fillna(0)
    else:
        # If model expects numeric features (no encoder), try building row from model_extra (features DF)
        try:
            features_df = model_extra  # expected to be DataFrame with feature schema
            # safe-construct a row with means of team's features
            row = {}
            if isinstance(features_df, pd.DataFrame) and not features_df.empty:
                row["home_avg_scored"] = features_df.loc[features_df["home_team"] == home_team, "home_avg_scored"].mean() or 1.0
                row["home_avg_conceded"] = features_df.loc[features_df["home_team"] == home_team, "home_avg_conceded"].mean() or 1.0
                row["away_avg_scored"] = features_df.loc[features_df["away_team"] == away_team, "away_avg_scored"].mean() or 1.0
                row["away_avg_conceded"] = features_df.loc[features_df["away_team"] == away_team, "away_avg_conceded"].mean() or 1.0
                X = pd.DataFrame([row]).fillna(0)
            else:
                X = pd.DataFrame([[0, 0]], columns=["home_avg_scored", "home_avg_conceded"])
        except Exception:
            X = np.array([[0, 0]])

    # predict
    try:
        # if X is ndarray with 2 columns (home_enc, away_enc)
        if isinstance(X, np.ndarray):
            ph = model_home.predict(X)[0]
            pa = model_away.predict(X)[0]
        else:
            ph = model_home.predict(X)[0]
            pa = model_away.predict(X)[0]
    except Exception:
        # fallback
        ph = 1.0
        pa = 1.0

    # Keep raw float rates (Poisson λ).  Rounding to ints destroys the
    # distribution — real football sees clean sheets in ~30% of matches, but
    # int(round(x)) never produces 0 when the regression always outputs ≥ 0.6.
    raw_h = float(np.clip(ph, 0.1, 6))
    raw_a = float(np.clip(pa, 0.1, 6))

    # ── Adaptive ELO blend ───────────────────────────────────────────────
    # When XGBoost disagrees strongly with ELO, blend in the ELO-based goal
    # expectation. Blend weight adapts to:
    #   (a) ELO gap magnitude — large gaps trust ELO more
    #   (b) League experience — promoted teams (0 matches) trust ELO more;
    #       established teams (30+ matches) let XGBoost take over
    if isinstance(model_extra, dict):
        elo_h = float(model_extra.get("elo_ratings", {}).get(home_team, 1500))
        elo_a = float(model_extra.get("elo_ratings", {}).get(away_team, 1500))
        elo_diff = elo_h - elo_a
        league_hg = float(model_extra.get("league_home_goals", 1.4))
        league_ag = float(model_extra.get("league_away_goals", 1.1))
        elo_exp_h = max(0.3, league_hg + elo_diff * 0.0035)
        elo_exp_a = max(0.3, league_ag - elo_diff * 0.0035)
        # Per-team experience modifier — promoted teams (0 matches) get more
        # ELO weight on THEIR goal prediction. Established teams trust XGBoost.
        h_exp = len(model_extra.get("team_profiles", {}).get(home_team, {}).get("overall_points", []))
        a_exp = len(model_extra.get("team_profiles", {}).get(away_team, {}).get("overall_points", []))
        w_h = min(0.85, abs(elo_diff) / 250) * max(0.4, 1.0 - h_exp / 80)
        w_a = min(0.85, abs(elo_diff) / 250) * max(0.4, 1.0 - a_exp / 80)
        raw_h = raw_h * (1 - w_h) + elo_exp_h * w_h
        raw_a = raw_a * (1 - w_a) + elo_exp_a * w_a

    disp_h, disp_a = _poisson_best_scoreline(raw_h, raw_a, seed="mode")

    if disp_h > disp_a:
        result = "Home Win"
    elif disp_a > disp_h:
        result = "Away Win"
    else:
        result = "Draw"

    # ── Apply probability calibrator if available ──────────────────────────
    calibrated = None
    if isinstance(model_extra, dict):
        cal = model_extra.get("calibrator")
        if cal:
            from math import exp, factorial as _fac
            p_h = p_d = p_a = 0.0
            for hg in range(0, 7):
                phg = exp(-raw_h) * (raw_h ** hg) / _fac(hg)
                for ag in range(0, 7):
                    pag = exp(-raw_a) * (raw_a ** ag) / _fac(ag)
                    jp = phg * pag
                    if hg > ag: p_h += jp
                    elif hg == ag: p_d += jp
                    else: p_a += jp
            calibrated = {
                "H": float(cal["H"].predict([p_h])[0]) if "H" in cal else p_h,
                "D": float(cal["D"].predict([p_d])[0]) if "D" in cal else p_d,
                "A": float(cal["A"].predict([p_a])[0]) if "A" in cal else p_a,
            }

    return result, disp_h, disp_a, raw_h, raw_a, calibrated


# ---------- saving predictions (compatibility with tasks.py) ----------

# Import models here to avoid circular import when this module is imported by Django startup code
try:
    from .models import MatchPrediction, TopPick, MatchOdds
except Exception:
    # If models are not importable (e.g., during unit tests), define placeholders
    MatchPrediction = None
    TopPick = None
    MatchOdds = None


def save_predictions(matches, model_home=None, model_away=None, le=None, match_date=None, competition_code=None, actual_result_map=None):
    """
    Backwards-compatible save_predictions used by your tasks.py:
      - matches: list of API match objects (expected keys: homeTeam, awayTeam, id, utcDate)
      - model_home/model_away: models trained on X where X was label-encoded with LabelEncoder le
      - le: LabelEncoder used for team encoding
      - match_date, competition_code: used for DB fields
      - actual_result_map: optional dict keyed by (home, away)
    Returns list of MatchPrediction instances (or dicts if models unavailable)
    """
    saved = []
    # If we don't have Django models available (e.g., during test), return structured dicts
    use_db = MatchPrediction is not None

    for match in matches:
        try:
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            match_id = match.get("id", None)
            utc = match.get("utcDate", None)
            mdate = match_date or (utc[:10] if utc else None)

            # If models provided: prepare input for prediction
            predicted_home_rate = None
            predicted_away_rate = None
            if (model_home is not None) and (model_away is not None):
                if isinstance(le, dict):
                    _, predicted_home_goals, predicted_away_goals, predicted_home_rate, predicted_away_rate, _calib = \
                        predict_match_outcome(home, away, (model_home, model_away, le))
                elif le is not None:
                    try:
                        input_df = pd.DataFrame({"home_team": [home], "away_team": [away]})
                        input_df["home_team"] = le.transform(input_df["home_team"])
                        input_df["away_team"] = le.transform(input_df["away_team"])
                    except Exception:
                        logger.warning(f" Unknown team(s) {home} / {away} for encoder; skipping")
                        continue

                    try:
                        predicted_home_rate = float(model_home.predict(input_df)[0])
                        predicted_away_rate = float(model_away.predict(input_df)[0])
                    except Exception:
                        predicted_home_rate = 1.0
                        predicted_away_rate = 1.0

                    predicted_home_goals, predicted_away_goals = _poisson_best_scoreline(
                        predicted_home_rate, predicted_away_rate,
                        seed=hash((home, away)) & 0xFFFFFFFF)
                else:
                    _, predicted_home_goals, predicted_away_goals, predicted_home_rate, predicted_away_rate, _calib = \
                        predict_match_outcome(home, away, (model_home, model_away, None))
            else:
                # No models supplied -> try to read existing predictions in match
                predicted_home_goals = int(match.get("predicted_home_goals", 0))
                predicted_away_goals = int(match.get("predicted_away_goals", 0))

            # classify predicted result & markets
            if predicted_home_goals > predicted_away_goals:
                predicted_result = "Home"
            elif predicted_away_goals > predicted_home_goals:
                predicted_result = "Away"
            else:
                predicted_result = "Draw"

            total_goals = predicted_home_goals + predicted_away_goals
            market_over_1_5 = total_goals >= 2
            market_over_2_5 = total_goals >= 3
            market_under_1_5 = total_goals < 2
            market_under_2_5 = total_goals < 3
            market_gg = predicted_home_goals > 0 and predicted_away_goals > 0
            market_nogg = not market_gg

            # optional: odds placeholders (left None unless odds fetcher sets them)
            odds_home = None
            odds_draw = None
            odds_away = None

            if use_db:
                obj, created = MatchPrediction.objects.update_or_create(
                    match_id=match_id,
                    defaults={
                        "match_date": mdate,
                        "competition": competition_code or match.get("competition", None),
                        "home_team": home,
                        "away_team": away,
                        "predicted_home_goals": predicted_home_goals,
                        "predicted_away_goals": predicted_away_goals,
                        "predicted_home_rate": predicted_home_rate,
                        "predicted_away_rate": predicted_away_rate,
                        "predicted_result": predicted_result,
                        "market_over_1_5": market_over_1_5,
                        "market_over_2_5": market_over_2_5,
                        "market_under_1_5": market_under_1_5,
                        "market_under_2_5": market_under_2_5,
                        "market_gg": market_gg,
                        "market_nogg": market_nogg,
                        "odds_home": odds_home,
                        "odds_draw": odds_draw,
                        "odds_away": odds_away,
                        "status": "TIMED",
                    }
                )
                # If actual_result_map provided and contains this fixture, update actuals & accuracy
                if actual_result_map:
                    key = (home, away)
                    v = actual_result_map.get(key)
                    if v:
                        obj.actual_home_goals = v.get("actual_home_goals", None)
                        obj.actual_away_goals = v.get("actual_away_goals", None)
                        # compute accuracy if predicted present
                        if obj.actual_home_goals is not None and obj.predicted_home_goals is not None:
                            predicted_res = "Home" if obj.predicted_home_goals > obj.predicted_away_goals else "Away" if obj.predicted_home_goals < obj.predicted_away_goals else "Draw"
                            actual_res = "Home" if obj.actual_home_goals > obj.actual_away_goals else "Away" if obj.actual_home_goals < obj.actual_away_goals else "Draw"
                            obj.is_accurate = (predicted_res == actual_res)
                            obj.status = "FINISHED"
                        obj.save()

                saved.append(obj)
            else:
                # return dict representation (helpful in tests)
                saved.append({
                    "match_id": match_id,
                    "match_date": mdate,
                    "competition": competition_code or match.get("competition"),
                    "home_team": home,
                    "away_team": away,
                    "predicted_home_goals": predicted_home_goals,
                    "predicted_away_goals": predicted_away_goals,
                    "predicted_result": predicted_result,
                    "markets": {
                        "over_1_5": market_over_1_5,
                        "over_2_5": market_over_2_5,
                        "gg": market_gg,
                    }
                })

        except Exception as e:
            logger.error(f" save_predictions failed for match {match}: {e}")
            continue

    return saved


# ---------- odds helpers (optional) ----------

def fetch_odds_for_date(odds_api_key, sport_key="soccer_epl", regions="uk,eu", markets="h2h,total", odds_format="decimal"):
    """
    Uses The Odds API (https://the-odds-api.com) format by default. This is optional.
    Returns list of odds data or empty list if not enabled.
    """
    if not odds_api_key:
        return []

    # Example: the-odds-api endpoint (v4) -- adjust if using RapidAPI
    if ODDS_PROVIDER == "the-odds-api":
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {
            "apiKey": odds_api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format
        }
        data = _get_json(url, headers=None, params=params, retries=2)
        return data or []

    # Add RapidAPI or other providers here
    return []


def attach_odds_to_predictions(match_predictions, odds_list):
    """
    Given a queryset/list of MatchPrediction objects and odds_list (raw from provider),
    try to match by team names and attach best odds to MatchOdds model.
    """
    if not match_predictions or not odds_list:
        return 0
    if MatchPrediction is None:
        return 0

    updated = 0
    # basic name normalization helper
    def normalize(name):
        return name.lower().replace(".", "").replace("fc", "").strip()

    # Build mapping from normalized names to MatchPrediction(s)
    mp_map = {}
    for mp in match_predictions:
        key = (normalize(mp.home_team), normalize(mp.away_team))
        mp_map.setdefault(key, []).append(mp)

    for game in odds_list:
        # the_odds_api uses "home_team" & "away_team" fields
        home = game.get("home_team") or game.get("home")
        away = game.get("away_team") or game.get("away")
        if not home or not away:
            continue
        key = (normalize(home), normalize(away))
        mps = mp_map.get(key, [])
        if not mps:
            # try reverse key (some providers flip home/away naming)
            key_rev = (normalize(away), normalize(home))
            mps = mp_map.get(key_rev, [])

        if not mps:
            continue

        # get best bookmaker (first) with markets
        bookmakers = game.get("bookmakers", []) or game.get("bookmakers", [])
        bookmaker = bookmakers[0] if bookmakers else None
        if not bookmaker:
            continue

        markets = bookmaker.get("markets", []) if bookmaker else []
        # find h2h and over_under and btts
        for mp in mps:
            # create or update MatchOdds
            if MatchOdds:
                odds_obj, _ = MatchOdds.objects.get_or_create(
                    match=mp,
                    defaults={"market_sources": {}},
                )
            else:
                odds_obj = None

            for market in markets:
                key_m = market.get("key")
                outcomes = market.get("outcomes", [])
                if key_m == "h2h":
                    # outcomes: [{'name':teamname,'price':x}, {'name':'Draw','price':y}, ...]
                    for o in outcomes:
                        n = o.get("name", "").lower()
                        p = o.get("price")
                        if normalize(n) == normalize(home) and odds_obj:
                            odds_obj.home_win = p
                        elif n == "draw" and odds_obj:
                            odds_obj.draw = p
                        elif normalize(n) == normalize(away) and odds_obj:
                            odds_obj.away_win = p
                elif key_m in ("over_under", "total_goals"):
                    for o in outcomes:
                        nm = o.get("name", "")
                        p = o.get("price")
                        if "Over 2.5" in nm and odds_obj:
                            odds_obj.over_2_5 = p
                        if "Under 2.5" in nm and odds_obj:
                            odds_obj.under_2_5 = p
                elif key_m in ("btts", "both_to_score"):
                    for o in outcomes:
                        nm = o.get("name", "")
                        p = o.get("price")
                        if nm.lower() in ("yes", "y", "true") and odds_obj:
                            odds_obj.btts_yes = p
                        if nm.lower() in ("no", "n", "false") and odds_obj:
                            odds_obj.btts_no = p

            if odds_obj:
                odds_obj.bookmaker = bookmaker.get("title", "") if bookmaker else None
                if odds_obj.market_sources is None:
                    odds_obj.market_sources = {}
                odds_obj.save()
                updated += 1

    return updated


def _collect_top_pick_candidates(rank_limit=4):
    today = date.today()
    matches = MatchPrediction.objects.select_related("odds").filter(match_date__gte=today).order_by("match_date")

    picks_by_date_candidates = defaultdict(list)
    model_bundles = {}

    for m in matches:
        competition_code = m.competition
        if competition_code not in model_bundles:
            model_bundles[competition_code] = get_or_train_model_bundle(competition_code)

        bundle = model_bundles.get(competition_code)
        if bundle is None or len(bundle) != 3:
            continue  # no model for this competition — skip, don't generate low-quality picks
        model_context = bundle[2] if isinstance(bundle[2], dict) else {}
        ranked_markets, _ = score_top_pick_markets(m, model_context)
        if not ranked_markets:
            continue

        try:
            odds_obj = m.odds
        except MatchOdds.DoesNotExist:
            odds_obj = None

        match_day = m.match_date.strftime("%Y-%m-%d")
        for rank_index, (market, confidence) in enumerate(ranked_markets[:rank_limit]):
            picks_by_date_candidates[match_day].append({
                "home_team": m.home_team,
                "away_team": m.away_team,
                "tip": market,
                "confidence": f"{confidence:.0f}",
                "confidence_value": float(confidence),
                "match_date": match_day,
                "odds": _market_odds_value(odds_obj, market),
                "rank_index": rank_index,
            })

    return picks_by_date_candidates


# ---------- top picks helpers ----------
def get_top_predictions(limit=10, variant=1):
    picks_by_date_candidates = _collect_top_pick_candidates(rank_limit=4)
    market_caps = {
        "1": 3,
        "2": 3,
        "X": 2,
        "Over 2.5": 3,
        "Under 2.5": 2,
        "GG": 2,
        "NG": 2,
        "Any Team Over 1.5": 2,
        "Home Win Either Half": 2,
        "Away Win Either Half": 2,
        "Home Team Over 1.0": 2,
        "Away Team Over 1.0": 2,
    }

    picks_by_date = {}
    for date_str, candidates in picks_by_date_candidates.items():
        selected = []
        used_fixtures = set()
        market_counts = defaultdict(int)

        ordered_candidates = sorted(
            candidates,
            key=lambda item: (item["confidence_value"] - (item["rank_index"] * 4), -item["rank_index"]),
            reverse=True,
        )
        preferred_rank_floor = max(0, int(variant) - 1)
        variant_candidates = [item for item in ordered_candidates if item["rank_index"] >= preferred_rank_floor]
        if not variant_candidates:
            variant_candidates = ordered_candidates

        for candidate in variant_candidates:
            fixture_key = (candidate["home_team"], candidate["away_team"])
            market = candidate["tip"]
            if fixture_key in used_fixtures:
                continue
            if market_counts[market] >= market_caps.get(market, limit):
                continue
            selected.append(candidate)
            used_fixtures.add(fixture_key)
            market_counts[market] += 1
            if len(selected) >= limit:
                break

        if len(selected) < min(limit, len({(c["home_team"], c["away_team"]) for c in candidates})):
            for candidate in ordered_candidates:
                fixture_key = (candidate["home_team"], candidate["away_team"])
                if fixture_key in used_fixtures:
                    continue
                selected.append(candidate)
                used_fixtures.add(fixture_key)
                if len(selected) >= limit:
                    break

        picks_by_date[date_str] = [
            {
                "home_team": item["home_team"],
                "away_team": item["away_team"],
                "tip": item["tip"],
                "confidence": f"{item['confidence_value']:.0f}",
                "match_date": item["match_date"],
                "odds": item["odds"],
            }
            for item in sorted(selected, key=lambda x: x["confidence_value"], reverse=True)
        ]

    return picks_by_date


def get_running_bet_predictions(limit=10):
    base_predictions = get_top_predictions(limit=limit, variant=1)
    aggregated_candidates = []

    for date_str, picks in base_predictions.items():
        for pick in picks:
            aggregated_candidates.append({
                "home_team": pick["home_team"],
                "away_team": pick["away_team"],
                "tip": pick["tip"],
                "confidence": pick["confidence"],
                "confidence_value": float(pick.get("confidence") or 0),
                "match_date": pick["match_date"],
                "odds": pick.get("odds"),
            })

    aggregated_candidates.sort(
        key=lambda item: (item["confidence_value"], item["match_date"]),
        reverse=True,
    )
    selected = aggregated_candidates[:limit]

    grouped = defaultdict(list)
    for pick in selected:
        grouped[pick["match_date"]].append({
            "home_team": pick["home_team"],
            "away_team": pick["away_team"],
            "tip": pick["tip"],
            "confidence": pick["confidence"],
            "match_date": pick["match_date"],
            "odds": pick.get("odds"),
        })

    return dict(grouped)


def get_mshipi_predictions(limit=20):
    market_targets = [
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
    ]
    market_caps = {
        "1": 3,
        "2": 3,
        "X": 2,
        "GG": 2,
        "NG": 2,
        "Over 2.5": 3,
        "Under 2.5": 3,
        "Any Team Over 1.5": 3,
        "Home Win Either Half": 2,
        "Away Win Either Half": 2,
        "Home Team Over 1.0": 2,
        "Away Team Over 1.0": 2,
    }
    candidates_by_date = _collect_top_pick_candidates(rank_limit=6)
    aggregated_candidates = []

    for date_str, day_candidates in candidates_by_date.items():
        for candidate in day_candidates:
            aggregated_candidates.append({
                **candidate,
                "match_date": date_str,
                "confidence_value": float(candidate.get("confidence_value") or candidate.get("confidence") or 0),
            })

    ordered_candidates = sorted(
        aggregated_candidates,
        key=lambda item: (item["confidence_value"] - (item["rank_index"] * 3), item["match_date"]),
        reverse=True,
    )

    selected = []
    selected_keys = set()
    market_counts = defaultdict(int)

    for market in market_targets:
        for candidate in ordered_candidates:
            key = (candidate["match_date"], candidate["home_team"], candidate["away_team"], candidate["tip"])
            if candidate["tip"] != market or key in selected_keys:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            market_counts[market] += 1
            break

    for candidate in ordered_candidates:
        if len(selected) >= limit:
            break
        key = (candidate["match_date"], candidate["home_team"], candidate["away_team"], candidate["tip"])
        if key in selected_keys:
            continue
        market = candidate["tip"]
        if market_counts[market] >= market_caps.get(market, 2):
            continue
        selected.append(candidate)
        selected_keys.add(key)
        market_counts[market] += 1

    grouped = defaultdict(list)
    for candidate in sorted(selected[:limit], key=lambda item: item["confidence_value"], reverse=True):
        grouped[candidate["match_date"]].append({
            "home_team": candidate["home_team"],
            "away_team": candidate["away_team"],
            "tip": candidate["tip"],
            "confidence": f"{candidate['confidence_value']:.0f}",
            "match_date": candidate["match_date"],
            "odds": candidate.get("odds"),
        })

    return dict(grouped)


def get_top_predictions_for_variant(limit=10, variant="1"):
    variant = str(variant)
    if variant == "3":
        return get_running_bet_predictions(limit=limit)
    if variant == "4":
        return get_mshipi_predictions(limit=max(limit, 20))
    return get_top_predictions(limit=limit, variant=int(variant))


def store_top_pick_for_date(predictions_by_date, variant="1"):
    """
    Atomically replaces TopPicks for the given dates+variant. Wrapped in a
    transaction so external readers never see an empty table between the
    delete and the bulk_create (would show as "no picks" in the mobile app).
    """
    from django.db import transaction

    if TopPick is None:
        return 0
    all_picks = []
    with transaction.atomic():
        for date_str, picks in (predictions_by_date or {}).items():
            try:
                match_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                continue
            TopPick.objects.filter(match_date=match_date, variant=variant).delete()
            for p in picks:
                all_picks.append(TopPick(
                    match_date=match_date,
                    home_team=p["home_team"],
                    away_team=p["away_team"],
                    variant=variant,
                    tip=p["tip"],
                    confidence=p.get("confidence", 0),
                    odds=p.get("odds"),
                ))
        if all_picks:
            TopPick.objects.bulk_create(all_picks)
            # Invalidate downstream caches so the mobile app picks up new picks.
            cache.delete("top_pick_slip_summary_v1")
            for date_str in (predictions_by_date or {}):
                cache.delete(f"summary_v1::{date_str}")
    return len(all_picks)


def update_actuals_for_top_picks(picks_qs):
    """
    Given a queryset of TopPick, update actual_tip/is_correct using MatchPrediction actual fields.
    """
    if TopPick is None:
        return 0
    to_update = list(picks_qs.filter(actual_tip__isnull=True))
    if not to_update:
        return 0

    prediction_rows = MatchPrediction.objects.filter(
        match_date__in=sorted({pick.match_date for pick in to_update})
    )
    prediction_rows_by_date = defaultdict(list)
    for prediction_row in prediction_rows:
        prediction_rows_by_date[prediction_row.match_date].append((
            _team_name_aliases(prediction_row.home_team),
            _team_name_aliases(prediction_row.away_team),
            prediction_row,
        ))

    updated = 0
    for pick in to_update:
        pick_home_aliases = _team_name_aliases(pick.home_team)
        pick_away_aliases = _team_name_aliases(pick.away_team)

        if not pick_home_aliases or not pick_away_aliases:
            continue

        # try to match the corresponding MatchPrediction
        found = None
        for home_aliases, away_aliases, prediction_row in prediction_rows_by_date.get(pick.match_date, []):
            if home_aliases & pick_home_aliases and away_aliases & pick_away_aliases:
                found = prediction_row
                break
        if not found:
            continue
        if found.actual_home_goals is None or found.actual_away_goals is None:
            continue
        home_g = found.actual_home_goals
        away_g = found.actual_away_goals
        ht_home_g = found.actual_ht_home_goals
        ht_away_g = found.actual_ht_away_goals
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
        if pick.tip == "GG" and gg:
            actual_tip = "GG"
        elif pick.tip == "NG" and nogg:
            actual_tip = "NG"
        elif pick.tip == "Over 2.5" and over_2_5:
            actual_tip = "Over 2.5"
        elif pick.tip == "Under 2.5" and under_2_5:
            actual_tip = "Under 2.5"
        elif pick.tip == "Any Team Over 1.5" and any_team_over_1_5:
            actual_tip = "Any Team Over 1.5"
        elif pick.tip == "Home Win Either Half" and home_win_either_half:
            actual_tip = "Home Win Either Half"
        elif pick.tip == "Away Win Either Half" and away_win_either_half:
            actual_tip = "Away Win Either Half"
        elif pick.tip == "Home Team Over 1.0" and home_team_over_1_0:
            actual_tip = "Home Team Over 1.0"
        elif pick.tip == "Away Team Over 1.0" and away_team_over_1_0:
            actual_tip = "Away Team Over 1.0"
        elif pick.tip == "Home Team Over 1.0" and home_team_over_1_0_push:
            actual_tip = "Refund"
        elif pick.tip == "Away Team Over 1.0" and away_team_over_1_0_push:
            actual_tip = "Refund"
        else:
            actual_tip = result_tip
        pick.actual_tip = actual_tip
        pick.is_correct = None if actual_tip == "Refund" else (pick.tip == actual_tip)
        pick.save()
        updated += 1
    return updated


# ---------- standings & metadata ----------

def get_league_table(competition):
    """
    Returns standings (cached). Uses football-data's /standings endpoint.
    """
    cache_key = f"standings_{competition}"
    cache.set(f"{cache_key}_updated", timezone.now(), timeout=60 * 60 * 6)
    cached = cache.get(cache_key)
    if cached:
        return cached

    from .providers import dispatch_provider, is_af
    if is_af(competition):
        from .providers import af_fetch_standings
        table = af_fetch_standings(competition)
    else:
        table = dispatch_provider(competition, "fetch_standings")
    cache.set(cache_key, table, timeout=60 * 60 * 6)
    return table


def fetch_and_cache_team_metadata():
    """
    Populate cache keys:
      - competition_meta::<code>
      - team_meta::<team name>
    """
    from .providers import dispatch_provider, is_fd
    for comp_code, comp_name in COMPETITIONS.items():
        if not is_fd(comp_code):
            _, teams = dispatch_provider(comp_code, "fetch_teams")
            if not teams:
                continue
            comp_meta = {"name": comp_name, "crest": ""}
        else:
            url = f"{BASE_URL}/competitions/{comp_code}/teams"
            json_data = _get_json(url, headers={"X-Auth-Token": API_TOKEN}, retries=2)
            if not json_data:
                continue
            teams = json_data.get("teams", [])
            comp_meta = {
                "name": json_data.get("competition", {}).get("name", comp_name),
                "crest": json_data.get("competition", {}).get("emblem", "")
            }
        cache.set(f"competition_meta::{comp_code}", comp_meta, timeout=60 * 60 * 24 * 30)
        for team in teams:
            team_name = team.get("name")
            if not team_name:
                continue
            team_meta = {
                "shortName": team.get("shortName", team_name),
                "crest": team.get("crest", ""),
                "competition": comp_code
            }
            cache.set(f"team_meta::{team_name}", team_meta, timeout=60 * 60 * 24 * 30)

    return True
