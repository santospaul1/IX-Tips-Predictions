# predict/utils.py


import os
import re
import time
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


def get_default_training_seasons(reference_date=None, history_window=3):
    current_season = get_current_season_start_year(reference_date)
    first_season = max(2023, current_season - history_window + 1)
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
    url = f"{BASE_URL}/matches"
    headers = {"X-Auth-Token": api_key or API_TOKEN}
    match_date_obj = datetime.strptime(match_date, "%Y-%m-%d")
    params = {
        "competitions": competition_code,
        "dateFrom": match_date_obj.strftime("%Y-%m-%d"),
        "dateTo": (match_date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    }
    json_data = _get_json(url, headers=headers, params=params, retries=retries, delay=delay)
    if not json_data:
        return []
    return json_data.get("matches", [])


def fetch_matches_by_season(api_key, competition_code, season_year):
    """
    Wrapper to fetch matches for a season year.
    """
    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    headers = {"X-Auth-Token": api_key or API_TOKEN}
    params = {"season": season_year}
    json_data = _get_json(url, headers=headers, params=params, retries=2)
    if not json_data:
        return []
    return json_data.get("matches", [])


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
                    all_matches.append({
                        "home_team": m["homeTeam"]["name"],
                        "away_team": m["awayTeam"]["name"],
                        "home_goals": m["score"]["fullTime"]["home"],
                        "away_goals": m["score"]["fullTime"]["away"],
                        "utc_date": m.get("utcDate")
                    })
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
        return {
            "home_points": 1.35,
            "goal_diff": 0.0,
            "total_goals": float(default_total_goals),
            "match_count": 0.0,
        }

    home_points = []
    home_goal_diff = []
    total_goals = []
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

    return {
        "home_points": _weighted_mean(home_points, 1.35),
        "goal_diff": _weighted_mean(home_goal_diff, 0.0),
        "total_goals": _weighted_mean(total_goals, default_total_goals),
        "match_count": float(len(recent_matches)),
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


def _build_feature_row(home_team, away_team, team_profiles, h2h_profiles, league_defaults, lookback, current_date, elo_ratings=None):
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
    h2h_summary = _summarize_head_to_head(
        home_team,
        away_team,
        h2h_profiles.get(tuple(sorted((home_team, away_team))), []),
        lookback=max(3, min(lookback, 5)),
        default_total_goals=league_defaults["home_goals"] + league_defaults["away_goals"],
    )

    return {
        "home_recent_scored": home_summary["recent_scored"],
        "home_recent_conceded": home_summary["recent_conceded"],
        "away_recent_scored": away_summary["recent_scored"],
        "away_recent_conceded": away_summary["recent_conceded"],
        "home_form": home_summary["form"],
        "away_form": away_summary["form"],
        "home_goal_diff_form": home_summary["goal_diff_form"],
        "away_goal_diff_form": away_summary["goal_diff_form"],
        "home_clean_sheet_rate": home_summary["clean_sheet_rate"],
        "away_clean_sheet_rate": away_summary["clean_sheet_rate"],
        "home_fail_to_score_rate": home_summary["fail_to_score_rate"],
        "away_fail_to_score_rate": away_summary["fail_to_score_rate"],
        "home_rest_days": home_summary["rest_days"],
        "away_rest_days": away_summary["rest_days"],
        "home_strength": home_summary["recent_scored"] - away_summary["recent_conceded"],
        "away_strength": away_summary["recent_scored"] - home_summary["recent_conceded"],
        "form_gap": home_summary["form"] - away_summary["form"],
        "goal_balance_gap": home_summary["goal_diff_form"] - away_summary["goal_diff_form"],
        "venue_attack_gap": home_summary["recent_scored"] - away_summary["recent_scored"],
        "venue_defense_gap": away_summary["recent_conceded"] - home_summary["recent_conceded"],
        "rest_gap": home_summary["rest_days"] - away_summary["rest_days"],
        "h2h_home_points": h2h_summary["home_points"],
        "h2h_goal_diff": h2h_summary["goal_diff"],
        "h2h_total_goals": h2h_summary["total_goals"],
        "h2h_match_count": h2h_summary["match_count"],
        "home_elo": home_elo,
        "away_elo": away_elo,
        "elo_gap": home_elo - away_elo,
        "elo_home_win_prob": _expected_home_result_from_elo(home_elo, away_elo),
        "home_advantage": league_defaults["home_goals"] - league_defaults["away_goals"],
    }


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
        "home_goal_diff_form",
        "away_goal_diff_form",
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
        "home_elo",
        "away_elo",
        "elo_gap",
        "elo_home_win_prob",
        "home_advantage",
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
    team_profiles = defaultdict(_new_team_profile)
    h2h_profiles = defaultdict(list)
    elo_ratings = defaultdict(lambda: 1500.0)
    feature_rows = []

    for _, row in history.iterrows():
        home = row["home_team"]
        away = row["away_team"]
        match_date = row.get("utc_date")
        feature_rows.append(
            _build_feature_row(
                home,
                away,
                team_profiles,
                h2h_profiles,
                league_defaults,
                lookback,
                match_date,
                elo_ratings,
            )
        )

        home_goals = float(row["home_goals"])
        away_goals = float(row["away_goals"])

        _update_team_profile(team_profiles[home], home_goals, away_goals, "home", match_date)
        _update_team_profile(team_profiles[away], away_goals, home_goals, "away", match_date)
        _update_elo_ratings(elo_ratings, home, away, home_goals, away_goals)
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
    Trains two regressors for home and away goals.
    - X: DataFrame (may contain string team names or numeric columns)
    - y_home, y_away: Series
    Returns: (model_home, model_away, label_encoder_or_none)
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

    model_home = RandomForestRegressor(
        n_estimators=300,
        random_state=42,
        min_samples_leaf=2,
        n_jobs=-1,
    )
    model_away = RandomForestRegressor(
        n_estimators=300,
        random_state=42,
        min_samples_leaf=2,
        n_jobs=-1,
    )

    model_home.fit(X_tr, yh_tr, sample_weight=sw_tr)
    model_away.fit(X_tr, ya_tr, sample_weight=sw_tr)

    # quick metrics (best-effort)
    try:
        home_rmse = np.sqrt(mean_squared_error(yh_te, model_home.predict(X_te)))
        away_rmse = np.sqrt(mean_squared_error(ya_te, model_away.predict(X_te)))
    except Exception:
        home_rmse = away_rmse = None

    print(f"[ML] Trained models; home_rmse={home_rmse}, away_rmse={away_rmse}")

    return model_home, model_away, label_encoder


def train_competition_models(training_df, lookback=8):
    X, y_home, y_away, model_context = build_training_features(training_df, lookback=lookback)
    sample_weight = np.linspace(0.35, 1.0, num=len(X)) if len(X) else None
    model_home, model_away, _ = train_models(X, y_home, y_away, sample_weight=sample_weight)
    return model_home, model_away, model_context


def get_or_train_model_bundle(competition_code, force_refresh=False):
    cache_key = model_cache_key(competition_code)
    if not force_refresh:
        cached_bundle = cache.get(cache_key)
        if (
            isinstance(cached_bundle, tuple)
            and len(cached_bundle) == 3
            and isinstance(cached_bundle[2], dict)
            and "feature_columns" in cached_bundle[2]
            and "team_profiles" in cached_bundle[2]
        ):
            return cached_bundle

    training_df = fetch_training_data_all_seasons(competition_code)
    if training_df.empty:
        return None

    bundle = train_competition_models(training_df)
    cache.set(cache_key, bundle, timeout=MODEL_CACHE_TIMEOUT)
    return bundle


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

    ph_ = int(round(np.clip(ph, 0, 10)))
    pa_ = int(round(np.clip(pa, 0, 10)))

    if ph_ > pa_:
        result = "Home Win"
    elif pa_ > ph_:
        result = "Away Win"
    else:
        result = "Draw"

    return result, ph_, pa_


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
            if (model_home is not None) and (model_away is not None):
                if isinstance(le, dict):
                    _, ph, pa = predict_match_outcome(home, away, (model_home, model_away, le))
                elif le is not None:
                    try:
                        input_df = pd.DataFrame({"home_team": [home], "away_team": [away]})
                        input_df["home_team"] = le.transform(input_df["home_team"])
                        input_df["away_team"] = le.transform(input_df["away_team"])
                    except Exception:
                        print(f"[WARN] Unknown team(s) {home} / {away} for encoder; skipping")
                        continue

                    try:
                        ph = model_home.predict(input_df)[0]
                        pa = model_away.predict(input_df)[0]
                    except Exception:
                        ph = float(model_home.predict(input_df)[0]) if hasattr(model_home, "predict") else 1.0
                        pa = float(model_away.predict(input_df)[0]) if hasattr(model_away, "predict") else 1.0
                else:
                    _, ph, pa = predict_match_outcome(home, away, (model_home, model_away, None))

                predicted_home_goals = int(round(np.clip(ph, 0, 10)))
                predicted_away_goals = int(round(np.clip(pa, 0, 10)))
            else:
                # No models supplied -> try to read existing predictions in match (if matches are dicts with prediction)
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
            print(f"[ERROR] save_predictions failed for match {match}: {e}")
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
                odds_obj, _ = MatchOdds.objects.get_or_create(match=mp)
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
                odds_obj.save()
                updated += 1

    return updated


# ---------- top picks helpers (unchanged from your code, but included) ----------
def get_top_predictions(limit=10):
    today = date.today()
    matches = MatchPrediction.objects.select_related("odds").filter(match_date__gte=today).order_by("match_date")

    picks_by_date = {}
    tip_priority = {"Over 2.5": 3, "GG": 2, "1": 1, "2": 1, "X": 0}

    for m in matches:
        tips = []
        meta_home = get_team_metadata(m.home_team)
        meta_away = get_team_metadata(m.away_team)

        margin = m.predicted_home_goals - m.predicted_away_goals

        # --- Model-based tips ---
        if abs(margin) >= 1.5:
            if margin > 0:
                tips.append(("1", min(abs(margin) * 10, 40)))  # Home win
            else:
                tips.append(("2", min(abs(margin) * 10, 40)))  # Away win
        elif abs(margin) <= 0.4:
            tips.append(("X", 20))  # Draw

        if m.predicted_home_goals >= 1 and m.predicted_away_goals >= 1:
            tips.append(("GG", 25))  # Both teams to score

        total_goals = m.predicted_home_goals + m.predicted_away_goals
        if total_goals > 2.5:
            tips.append(("Over 2.5", min((total_goals - 2.5) * 12, 30)))

        if tips:
            # ✅ Force "Over 2.5" tip if total goals ≥ 3
            if total_goals >= 3:
                best_tip = ("Over 2.5", 100)
            else:
                best_tip = sorted(
                    tips,
                    key=lambda x: (x[1], tip_priority.get(x[0], 0)),
                    reverse=True
                )[0]

            odds_value = None
            try:
                odds_obj = m.odds
            except MatchOdds.DoesNotExist:
                odds_obj = None
            if odds_obj:
                if best_tip[0] == "1":
                    odds_value = odds_obj.home_win
                elif best_tip[0] == "2":
                    odds_value = odds_obj.away_win
                elif best_tip[0] == "X":
                    odds_value = odds_obj.draw
                elif best_tip[0] == "Over 2.5":
                    odds_value = odds_obj.over_2_5
                elif best_tip[0] == "GG":
                    odds_value = odds_obj.btts_yes

            match_day = m.match_date.strftime("%Y-%m-%d")
            picks_by_date.setdefault(match_day, []).append({
                "home_team": meta_home.get("shortName", m.home_team),
                "away_team": meta_away.get("shortName", m.away_team),
                "tip": best_tip[0],
                "confidence": f"{best_tip[1]:.0f}",
                "match_date": match_day,
                "odds": odds_value  # ✅ attach bookmaker odds
            })

    # --- Sort and limit ---
    for date_str in picks_by_date:
        picks_by_date[date_str] = sorted(
            picks_by_date[date_str],
            key=lambda x: int(x["confidence"]),
            reverse=True
        )[:limit]

    return picks_by_date



def store_top_pick_for_date(predictions_by_date):
    if TopPick is None:
        return 0
    all_picks = []
    for date_str, picks in (predictions_by_date or {}).items():
        try:
            match_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        TopPick.objects.filter(match_date=match_date).delete()
        for p in picks:
            all_picks.append(TopPick(
                match_date=match_date,
                home_team=p["home_team"],
                away_team=p["away_team"],
                tip=p["tip"],
                confidence=p.get("confidence", 0),
                odds=p.get("odds"),
            ))
    if all_picks:
        TopPick.objects.bulk_create(all_picks)
        return len(all_picks)
    return 0


def update_actuals_for_top_picks(picks_qs):
    """
    Given a queryset of TopPick, update actual_tip/is_correct using MatchPrediction actual fields.
    """
    if TopPick is None:
        return 0
    to_update = picks_qs.filter(actual_tip__isnull=True)
    updated = 0
    for pick in to_update:
        pick_home_aliases = _team_name_aliases(pick.home_team)
        pick_away_aliases = _team_name_aliases(pick.away_team)

        if not pick_home_aliases or not pick_away_aliases:
            continue

        # try to match the corresponding MatchPrediction
        match_qs = MatchPrediction.objects.filter(match_date=pick.match_date)
        found = None
        for mp in match_qs:
            if (
                _team_name_aliases(mp.home_team) & pick_home_aliases
                and _team_name_aliases(mp.away_team) & pick_away_aliases
            ):
                found = mp
                break
        if not found:
            continue
        if found.actual_home_goals is None or found.actual_away_goals is None:
            continue
        home_g = found.actual_home_goals
        away_g = found.actual_away_goals
        result_tip = "1" if home_g > away_g else "2" if home_g < away_g else "X"
        gg = home_g >= 1 and away_g >= 1
        over_2_5 = (home_g + away_g) > 2.5
        actual_tip = "GG" if pick.tip == "GG" and gg else "Over 2.5" if pick.tip == "Over 2.5" and over_2_5 else result_tip
        pick.actual_tip = actual_tip
        pick.is_correct = (pick.tip == actual_tip)
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

    url = f"{BASE_URL}/competitions/{competition}/standings"
    json_data = _get_json(url, headers={"X-Auth-Token": API_TOKEN}, retries=2)
    if not json_data:
        return []
    # defensive: some competitions may not have standings structure
    try:
        table = json_data.get("standings", [])[0].get("table", [])
    except Exception:
        table = []
    cache.set(cache_key, table, timeout=60 * 60 * 6)
    return table


def fetch_and_cache_team_metadata():
    """
    Populate cache keys:
      - competition_meta::<code>
      - team_meta::<team name>
    """
    for comp_code, comp_name in COMPETITIONS.items():
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
