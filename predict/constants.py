import os

from django.core.cache import cache
from django.conf import settings

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "ELC": "Championship",
    "CL": "UEFA Champions League",
    "EC": "European Championship",
    "BSA": "Campeonato Brasileiro Serie A",
    "CLI": "Copa Libertadores",
    "WC": "FIFA World Cup",
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
