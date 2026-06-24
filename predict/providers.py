"""
Data-provider abstraction.

Existing leagues are served by football-data.org ("FD"). To cover leagues that
football-data's plan doesn't include, additional leagues can be served by
API-Football ("AF", https://dashboard.api-football.com). This module fetches
from API-Football and normalises every response into the *football-data.org
shape* that the rest of the codebase already expects, so no downstream code
(ML models, views, mobile API) has to change.

Routing is decided per competition by COMPETITION_PROVIDERS in constants.py.
If APIFOOTBALL_KEY is not set, all AF calls return empty gracefully — so adding
AF leagues to the config is harmless until the key is configured.
"""
import logging
import os

import requests
from django.core.cache import cache

from .constants import (
    APIFOOTBALL_CALENDAR_YEAR,
    APIFOOTBALL_LEAGUE_IDS,
    COMPETITION_PROVIDERS,
    LIVEFOOTBALL_LEAGUE_IDS,
)

logger = logging.getLogger(__name__)

AF_BASE_URL = os.environ.get("APIFOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
AF_KEY = os.environ.get("APIFOOTBALL_KEY", "")

# Live-Football-Data (FotMob) on RapidAPI
LF_HOST = os.environ.get("LIVEFOOTBALL_HOST", "free-api-live-football-data.p.rapidapi.com")
LF_KEY = os.environ.get("LIVEFOOTBALL_KEY", "")
LF_SEASON_CACHE_TIMEOUT = 60 * 60 * 25  # one daily refresh; reads hit cache

# API-Football fixture status short codes -> football-data status strings
_AF_FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}
_AF_LIVE = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP"}
_AF_POSTPONED = {"PST"}
_AF_CANCELLED = {"CANC", "ABD"}


def get_provider(competition_code):
    return COMPETITION_PROVIDERS.get(competition_code, "FD")


def is_af(competition_code):
    return get_provider(competition_code) == "AF"


def is_lf(competition_code):
    return get_provider(competition_code) == "LF"


# ── Low-level request ─────────────────────────────────────────────────────────

def _af_get(path, params=None):
    """Call API-Football and return the `response` list (handles paging)."""
    if not AF_KEY:
        logger.info("APIFOOTBALL_KEY not set; skipping API-Football call %s", path)
        return []

    headers = {"x-apisports-key": AF_KEY}
    params = dict(params or {})
    results = []
    page = 1
    while True:
        params["page"] = page
        try:
            r = requests.get(f"{AF_BASE_URL}/{path}", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("API-Football request failed: %s params=%s error=%s", path, params, exc)
            break

        if data.get("errors"):
            logger.warning("API-Football returned errors for %s: %s", path, data["errors"])
            break

        results.extend(data.get("response", []) or [])
        paging = data.get("paging", {}) or {}
        if page >= (paging.get("total") or 1):
            break
        page += 1
    return results


# ── Season helpers ────────────────────────────────────────────────────────────

def _af_season_for_date(d):
    """
    API-Football season = the starting year. Split-year leagues (Aug–May) use
    the start year; calendar-year leagues use the calendar year.
    """
    return d.year


def _af_season_for_split_year(d):
    return d.year if d.month >= 7 else d.year - 1


def af_season(competition_code, d):
    if competition_code in APIFOOTBALL_CALENDAR_YEAR:
        return _af_season_for_date(d)
    return _af_season_for_split_year(d)


# ── Normalisation (API-Football -> football-data shape) ───────────────────────

def _af_status(short):
    if short in _AF_FINISHED:
        return "FINISHED"
    if short in _AF_LIVE:
        return "IN_PLAY"
    if short in _AF_POSTPONED:
        return "POSTPONED"
    if short in _AF_CANCELLED:
        return "CANCELLED"
    return "TIMED"


def _normalize_fixture(fx):
    fixture = fx.get("fixture", {}) or {}
    teams = fx.get("teams", {}) or {}
    goals = fx.get("goals", {}) or {}
    score = fx.get("score", {}) or {}
    ht = score.get("halftime", {}) or {}
    ft = score.get("fulltime", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    return {
        "id": fixture.get("id"),
        "utcDate": fixture.get("date"),  # ISO 8601 with offset; fromisoformat-safe
        "status": _af_status((fixture.get("status", {}) or {}).get("short")),
        "homeTeam": {"name": home.get("name"), "crest": home.get("logo")},
        "awayTeam": {"name": away.get("name"), "crest": away.get("logo")},
        "score": {
            "fullTime": {
                "home": ft.get("home") if ft.get("home") is not None else goals.get("home"),
                "away": ft.get("away") if ft.get("away") is not None else goals.get("away"),
            },
            "halfTime": {"home": ht.get("home"), "away": ht.get("away")},
        },
    }


# ── Public fetch functions (mirror utils/views football-data fetchers) ────────

def af_fetch_matches_by_date(competition_code, match_date):
    """match_date: 'YYYY-MM-DD'. Returns football-data-shaped match dicts."""
    from datetime import datetime

    league_id = APIFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return []
    d = datetime.strptime(match_date, "%Y-%m-%d").date()
    rows = _af_get("fixtures", {
        "league": league_id,
        "season": af_season(competition_code, d),
        "date": match_date,
    })
    return [_normalize_fixture(fx) for fx in rows]


def af_fetch_matches_by_season(competition_code, season_year):
    league_id = APIFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return []
    rows = _af_get("fixtures", {"league": league_id, "season": season_year})
    return [_normalize_fixture(fx) for fx in rows]


def af_fetch_scorers(competition_code):
    """Returns football-data-shaped scorer dicts."""
    from datetime import date

    league_id = APIFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return []
    season = af_season(competition_code, date.today())
    rows = _af_get("players/topscorers", {"league": league_id, "season": season})
    scorers = []
    for row in rows:
        player = row.get("player", {}) or {}
        stats = (row.get("statistics") or [{}])[0]
        team = stats.get("team", {}) or {}
        goals = stats.get("goals", {}) or {}
        penalty = stats.get("penalty", {}) or {}
        scorers.append({
            "player": {"name": player.get("name")},
            "team": {"name": team.get("name")},
            "goals": goals.get("total") or 0,
            "assists": goals.get("assists") or 0,
            "penalties": penalty.get("scored") or 0,
        })
    return scorers


def af_fetch_standings(competition_code):
    """Returns football-data-shaped standings table rows."""
    from datetime import date

    league_id = APIFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return []
    season = af_season(competition_code, date.today())
    rows = _af_get("standings", {"league": league_id, "season": season})
    if not rows:
        return []
    try:
        groups = rows[0]["league"]["standings"]  # list of groups
        flat = groups[0] if groups else []
    except (KeyError, IndexError, TypeError):
        return []

    table = []
    for r in flat:
        team = r.get("team", {}) or {}
        all_stats = r.get("all", {}) or {}
        goals = all_stats.get("goals", {}) or {}
        table.append({
            "position": r.get("rank"),
            "team": {
                "name": team.get("name"),
                "shortName": team.get("name"),
                "crest": team.get("logo"),
                "tla": None,
            },
            "playedGames": all_stats.get("played"),
            "won": all_stats.get("win"),
            "draw": all_stats.get("draw"),
            "lost": all_stats.get("lose"),
            "points": r.get("points"),
            "goalsFor": goals.get("for"),
            "goalsAgainst": goals.get("against"),
            "goalDifference": r.get("goalsDiff"),
        })
    return table


def af_fetch_teams(competition_code):
    """
    Returns (competition_meta, teams) where teams are football-data-shaped:
    [{"name":, "shortName":, "crest":}], so fetch_and_cache_team_metadata can
    populate the same cache keys.
    """
    from datetime import date

    league_id = APIFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return None, []
    season = af_season(competition_code, date.today())
    rows = _af_get("teams", {"league": league_id, "season": season})
    teams = []
    for row in rows:
        team = row.get("team", {}) or {}
        teams.append({
            "name": team.get("name"),
            "shortName": team.get("name"),
            "crest": team.get("logo"),
        })
    return None, teams


# ══ Live-Football-Data (FotMob via RapidAPI) — provider "LF" ══════════════════
# A single /football-get-all-matches-by-league call returns the whole current
# season (finished + upcoming) for a league. We cache that dump for ~a day and
# derive fixtures-by-date, training data, and standings from it — so each league
# costs ~1 request/day, fitting the 100/month free quota.

def _lf_get(path):
    """Call Live-Football-Data and return the `response` dict (or {})."""
    if not LF_KEY:
        logger.info("LIVEFOOTBALL_KEY not set; skipping call %s", path)
        return {}
    headers = {"x-rapidapi-host": LF_HOST, "x-rapidapi-key": LF_KEY}
    try:
        r = requests.get(f"https://{LF_HOST}/{path}", headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("Live-Football-Data request failed: %s error=%s", path, exc)
        return {}
    if data.get("status") not in (None, "success"):
        logger.warning("Live-Football-Data non-success for %s: %s", path, data.get("status"))
    return data.get("response", {}) or {}


def _normalize_lf_match(m):
    home = m.get("home", {}) or {}
    away = m.get("away", {}) or {}
    st = m.get("status", {}) or {}
    finished = bool(st.get("finished"))
    if finished:
        status = "FINISHED"
    elif st.get("cancelled"):
        status = "CANCELLED"
    elif st.get("started"):
        status = "IN_PLAY"
    else:
        status = "TIMED"
    return {
        "id": m.get("id"),
        "utcDate": st.get("utcTime"),  # ISO 8601 with Z
        "status": status,
        "homeTeam": {"name": home.get("name"), "crest": None},
        "awayTeam": {"name": away.get("name"), "crest": None},
        "score": {
            "fullTime": {
                "home": home.get("score") if finished else None,
                "away": away.get("score") if finished else None,
            },
            "halfTime": {"home": None, "away": None},
        },
    }


def _lf_season_matches(competition_code, force_refresh=False):
    """Cached full-season match dump (normalised), the single source for LF."""
    league_id = LIVEFOOTBALL_LEAGUE_IDS.get(competition_code)
    if not league_id:
        return []
    ck = f"lf_season_matches::{competition_code}"
    if not force_refresh:
        cached = cache.get(ck)
        if cached is not None:
            return cached
    response = _lf_get(f"football-get-all-matches-by-league?leagueid={league_id}")
    raw = response.get("matches", []) if isinstance(response, dict) else []
    matches = [_normalize_lf_match(m) for m in raw if m.get("home") and m.get("away")]
    cache.set(ck, matches, timeout=LF_SEASON_CACHE_TIMEOUT)
    return matches


def lf_refresh_season(competition_code):
    """Force the once-daily API call (used by the lfrefresh cron job)."""
    return len(_lf_season_matches(competition_code, force_refresh=True))


def lf_fetch_matches_by_date(competition_code, match_date):
    """match_date 'YYYY-MM-DD' — filter the cached season dump by kickoff date."""
    day = str(match_date)[:10]
    return [m for m in _lf_season_matches(competition_code)
            if (m.get("utcDate") or "")[:10] == day]


def lf_fetch_matches_by_season(competition_code, season_year=None):
    """Return the full season dump (training uses the FINISHED ones)."""
    return _lf_season_matches(competition_code)


def lf_fetch_standings(competition_code):
    """Compute a standings table from finished matches in the cached dump."""
    matches = _lf_season_matches(competition_code)
    rows = {}

    def _row(name):
        return rows.setdefault(name, {
            "team": {"name": name, "shortName": name, "crest": None, "tla": None},
            "playedGames": 0, "won": 0, "draw": 0, "lost": 0,
            "points": 0, "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0,
        })

    for m in matches:
        if m["status"] != "FINISHED":
            continue
        h, a = m["homeTeam"]["name"], m["awayTeam"]["name"]
        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if not h or not a or hg is None or ag is None:
            continue
        hr, ar = _row(h), _row(a)
        hr["playedGames"] += 1; ar["playedGames"] += 1
        hr["goalsFor"] += hg; hr["goalsAgainst"] += ag
        ar["goalsFor"] += ag; ar["goalsAgainst"] += hg
        if hg > ag:
            hr["won"] += 1; hr["points"] += 3; ar["lost"] += 1
        elif hg < ag:
            ar["won"] += 1; ar["points"] += 3; hr["lost"] += 1
        else:
            hr["draw"] += 1; ar["draw"] += 1; hr["points"] += 1; ar["points"] += 1

    table = list(rows.values())
    for r in table:
        r["goalDifference"] = r["goalsFor"] - r["goalsAgainst"]
    table.sort(key=lambda r: (r["points"], r["goalDifference"], r["goalsFor"]), reverse=True)
    for i, r in enumerate(table, 1):
        r["position"] = i
    return table


def lf_fetch_teams(competition_code):
    """Derive team list (names only) from the cached dump — no extra API call."""
    names = set()
    for m in _lf_season_matches(competition_code):
        if m["homeTeam"]["name"]:
            names.add(m["homeTeam"]["name"])
        if m["awayTeam"]["name"]:
            names.add(m["awayTeam"]["name"])
    teams = [{"name": n, "shortName": n, "crest": None} for n in sorted(names)]
    return None, teams
