import os

from django.core.cache import cache
from django.conf import settings

COMPETITIONS = {
    # ── football-data.org (provider "FD") ──
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "ELC": "Championship",
    "CL": "UEFA Champions League",
    "BSA": "Campeonato Brasileiro Serie A",
    "CLI": "Copa Libertadores",
    "WC": "FIFA World Cup",
    "EC": "European Championship",
    # ── API-Football (provider "AF") — shelved: free plan blocks current seasons.
    # Re-enable by uncommenting here + in COMPETITION_PROVIDERS/APIFOOTBALL_LEAGUE_IDS.
    # "MLS": "Major League Soccer",
    # "LMX": "Liga MX",
    # "SPL": "Saudi Pro League",
    # ── Live-Football-Data on RapidAPI (provider "LF") — extra leagues ──
    # Fetched as a once-daily full-season dump (fixtures + results in one call)
    # to respect the 100 requests/month free quota. Add leagues with their
    # FotMob IDs in LIVEFOOTBALL_LEAGUE_IDS below.
    "SAU": "Saudi Pro League",
}

# Which provider serves each competition. Anything not listed defaults to "FD".
COMPETITION_PROVIDERS = {
    # "MLS": "AF",
    # "LMX": "AF",
    # "SPL": "AF",
    "SAU": "LF",
}

# API-Football numeric league IDs (verified against the account on 2026-05-27).
APIFOOTBALL_LEAGUE_IDS = {
    # "MLS": 253,
    # "LMX": 262,
    # "SPL": 307,
}

# AF leagues that run on a single calendar year (season == year). Everything
# else is treated as a split (Aug–May) season where season == start year.
APIFOOTBALL_CALENDAR_YEAR = {"MLS", "LMX"}

# Live-Football-Data (FotMob) league IDs for provider "LF". Verified:
#   536 = Saudi Pro League, 47 = Premier League (example).
# Find a league's ID from its FotMob URL: fotmob.com/leagues/<ID>/overview/...
# (MLS/Liga MX IDs to be confirmed once their seasons resume after the WC.)
LIVEFOOTBALL_LEAGUE_IDS = {
    "SAU": 536,
}

COMPETITION_CHOICES = [(code, name) for code, name in COMPETITIONS.items()]
competitions = COMPETITIONS
NAME_TO_CODE = {name.lower(): code for code, name in COMPETITIONS.items()}

API_TOKEN = getattr(settings, "FOOTBALL_DATA_API_KEY", os.getenv("FOOTBALL_DATA_API_KEY", ""))
ODDS_API_KEY = getattr(settings, "ODDS_API_KEY", os.getenv("ODDS_API_KEY", ""))
BASE_URL = getattr(settings, "FOOTBALL_DATA_BASE_URL", os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4"))

TEAM_METADATA_CACHE_TIMEOUT = 60 * 60 * 24 * 30
STANDINGS_CACHE_TIMEOUT = 60 * 60 * 6
TRAINING_CACHE_TIMEOUT = 60 * 60 * 24 * 7
MODEL_CACHE_TIMEOUT = 60 * 60 * 6


def team_meta_cache_key(team_name):
    return f"team_meta::{team_name}"


def competition_cached_key(comp_code):
    return f"competition_cached::{comp_code}"


def standings_cache_key(comp_code):
    return f"standings_{comp_code}"


def training_data_cache_key(comp_code):
    return f"training_data_{comp_code}"


def model_cache_key(comp_code):
    return f"model_bundle::{comp_code}"


def get_team_metadata(name):
    return cache.get(team_meta_cache_key(name), {"shortName": name, "crest": None})
