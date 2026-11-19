import os
import json

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
    # WC/EC commented out: football-data.org's plan returns no data for them, so
    # the daily jobs just waste API calls + log 429s. The prediction model also
    # isn't suited to national-team tournaments. Re-add if the plan ever covers them.
    # "WC": "FIFA World Cup",
    # "EC": "European Championship",
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
    # ── football-data.co.uk (provider "UK") — free CSVs, no key/quota ──
    # New European leagues not on the football-data.org plan. Fully self-served
    # (training from season files + fixtures from fixtures.csv + odds), so team
    # names stay consistent. Map our code -> FDUK code in FOOTBALL_DATA_UK_CODES.
    "BEL": "Belgian Pro League",
    "TUR": "Süper Lig",
    "GRE": "Super League Greece",
    "SCO": "Scottish Premiership",
    "GER2": "2. Bundesliga",
    "ITA2": "Serie B",
    "ESP2": "LaLiga 2",
    "FRA2": "Ligue 2",
    # ── Extra leagues from football-data.co.uk (free CSVs, single file) ──
    "JPN": "J1 League",
    "CHN": "Chinese Super League",
    "RUS": "Russian Premier League",
    # ── More UK leagues ──
    "ENG2": "League One",
    "ENG3": "League Two",
    "SCO2": "Scottish Championship",
    "SCO3": "Scottish League One",
    "SCO4": "Scottish League Two",
    "ARG": "Argentina Primera Division",
    "AUT": "Austrian Bundesliga",
    "FIN": "Finnish Veikkausliiga",
    "MEX": "Liga MX",
    "NOR": "Norwegian Eliteserien",
    "POL": "Polish Ekstraklasa",
    "ROU": "Romanian Liga I",
    "SWE": "Swedish Allsvenskan",
    "SWZ": "Swiss Super League",
    "USA": "Major League Soccer",
    "EFLCUP": "EFL Cup", "FACUP": "FA Cup", "COPAREY": "Copa del Rey",
    "COPPA": "Coppa Italia", "DFB": "DFB-Pokal", "KNVB": "KNVB Beker",
    "TACAP": "Taça de Portugal", "SCOTCUP": "Scottish Cup",
    "SCOTLC": "Scottish League Cup", "OFBCUP": "OFB Cup", "SWZCUP": "Swiss Cup",
    "ENG5": "National League", "ENG6N": "National League North",
    "ENG6S": "National League South", "ENG7": "Northern Premier League",
    "GER3": "3. Liga", "DEN": "Danish Superliga", "IRE1": "League of Ireland First Division",
    "IRENAT": "League of Ireland National League", "POL1": "I Liga", "POL2": "II Liga",
    "BELU21": "Pro League U21", "WAL1": "Cymru Premier", "WAL2": "Cymru North",
    "WAL3": "Cymru South", "CRO": "Croatian HNL", "CZE": "Czech Chance Liga",
    "UKR": "Ukrainian Premier League", "CYP": "Cyprus First Division",
    "SVK": "Slovak Nike Liga", "SVN": "Slovenian Prva Liga", "KAZ": "Kazakhstan Premier League",
    "NED2": "Eerste Divisie", "BRA2": "Brasileirão Série B",
    "ARG2": "Primera Nacional", "MEX2": "Liga de Expansión MX", "AUS": "A-Leagues",
    "UEL": "UEFA Europa League", "UECL": "UEFA Conference League"
}

# Which provider serves each competition. Anything not listed defaults to "FD".
COMPETITION_PROVIDERS = {
    # "MLS": "AF",
    # "LMX": "AF",
    # "SPL": "AF",
    "SAU": "LF",
    "BEL": "UK", "TUR": "UK", "GRE": "UK", "SCO": "UK",
    "GER2": "UK", "ITA2": "UK", "ESP2": "UK", "FRA2": "UK",
    "JPN": "UK", "CHN": "UK", "RUS": "UK",
    "ENG2": "UK", "ENG3": "UK", "SCO2": "UK", "SCO3": "UK", "SCO4": "UK",
    "ARG": "UK", "AUT": "UK", "FIN": "UK", "MEX": "UK", "NOR": "UK",
    "POL": "UK", "ROU": "UK", "SWE": "UK", "SWZ": "UK", "USA": "UK",
    # Open Matchday-only competitions use its public CSVs as their primary feed.
    "EFLCUP": "OM", "FACUP": "OM", "COPAREY": "OM", "COPPA": "OM", "DFB": "OM",
    "KNVB": "OM", "TACAP": "OM", "SCOTCUP": "OM", "SCOTLC": "OM", "OFBCUP": "OM",
    "SWZCUP": "OM", "ENG5": "OM", "ENG6N": "OM", "ENG6S": "OM", "ENG7": "OM",
    "GER3": "OM", "DEN": "OM", "IRE1": "OM", "IRENAT": "OM", "POL1": "OM",
    "POL2": "OM", "BELU21": "OM", "WAL1": "OM", "WAL2": "OM", "WAL3": "OM",
    "CRO": "OM", "CZE": "OM", "UKR": "OM", "CYP": "OM", "SVK": "OM",
    "SVN": "OM", "NED2": "OM", "BRA2": "OM", "ARG2": "OM", "MEX2": "OM",
    "AUS": "OM", "UEL": "OM", "UECL": "OM", "KAZ": "OM",
}

# football-data.co.uk mapping: our code -> (kind, fduk_code).
#   "main"  -> per-season file  /mmz4281/<season>/<code>.csv
#   "extra" -> single combined file /new/<code>.csv (e.g. USA, MEX, ARG, BRA)
FOOTBALL_DATA_UK_CODES = {
    "BEL":  ("main", "B1"),
    "TUR":  ("main", "T1"),
    "GRE":  ("main", "G1"),
    "SCO":  ("main", "SC0"),
    "GER2": ("main", "D2"),
    "ITA2": ("main", "I2"),
    "ESP2": ("main", "SP2"),
    "FRA2": ("main", "F2"),
    "JPN": ("extra", "JPN"),
    "CHN": ("extra", "CHN"),
    "RUS": ("extra", "RUS"),
    "ENG2": ("main", "E2"), "ENG3": ("main", "E3"),
    "SCO2": ("main", "SC1"), "SCO3": ("main", "SC2"), "SCO4": ("main", "SC3"),
    "ARG": ("extra", "ARG"), "AUT": ("extra", "AUT"), "FIN": ("extra", "FIN"),
    "MEX": ("extra", "MEX"), "NOR": ("extra", "NOR"), "POL": ("extra", "POL"),
    "ROU": ("extra", "ROU"), "SWE": ("extra", "SWE"), "SWZ": ("extra", "SWZ"),
    "USA": ("extra", "USA"),
}


# open-matchday (OM) — public daily CSV compilation for non-commercial use.
# Exact folder names from the open-matchday competition index.
# The feed publishes the current season only; it supplements existing providers
# and is primary for competitions without another configured provider.
OPEN_MATCHDAY_COMPETITIONS = {
    "PL": "ENG_1_premier-league",
    "PD": "ESP_1_la-liga",
    "BL1": "GER_1_bundesliga",
    "SA": "ITA_1_serie-a",
    "FL1": "FRA_1_ligue-1",
    "CL": "UEFA_1_champions-league",
    "UEL": "UEFA_1_europa-league",
    "UECL": "UEFA_1_conference-league",
    "EFLCUP": "ENG_CUP_efl-cup",
    "FACUP": "ENG_CUP_fa-cup",
    "COPAREY": "ESP_CUP_copa-del-rey",
    "COPPA": "ITA_CUP_coppa-italia",
    "DFB": "GER_CUP_dfb-pokal",
    "KNVB": "NED_CUP_knvb-beker",
    "TACAP": "POR_CUP_taca-de-portugal",
    "SCOTCUP": "SCO_CUP_scottish-cup",
    "SCOTLC": "SCO_CUP_league-cup",
    "OFBCUP": "AUT_CUP_ofb-cup",
    "SWZCUP": "SUI_CUP_swiss-cup",
    "ELC": "ENG_2_championship",
    "ENG2": "ENG_3_league-one",
    "ENG3": "ENG_4_league-two",
    "ENG5": "ENG_5_national-league",
    "ENG6N": "ENG_6_national-league-north",
    "ENG6S": "ENG_6_national-league-south",
    "ENG7": "ENG_7_northern-premier-league",
    "SCO": "SCO_1_premiership",
    "SCO2": "SCO_2_championship",
    "SCO3": "SCO_3_league-one",
    "SCO4": "SCO_4_league-two",
    "GER2": "GER_2_2-bundesliga",
    "GER3": "GER_3_3-liga",
    "PPL": "POR_1_primeira-liga",
    "DED": "NED_1_eredivisie",
    "BEL": "BEL_1_pro-league",
    "TUR": "TUR_1_super-lig",
    "GRE": "GRE_1_super-league",
    "SWZ": "SUI_1_super-league",
    "AUT": "AUT_1_bundesliga",
    "DEN": "DEN_1_superliga",
    "SWE": "SWE_1_allsvenskan",
    "NOR": "NOR_1_eliteserien",
    "ROU": "ROU_1_liga-1",
    "RUS": "RUS_1_premier-liga",
    "IRE1": "IRE_2_first-division",
    "IRENAT": "IRE_3_national-league",
    "POL": "POL_1_ekstraklasa",
    "POL1": "POL_2_division-1",
    "POL2": "POL_3_division-2",
    "BELU21": "BEL_U21_pro-league",
    "WAL1": "WAL_1_cymru-premier",
    "WAL2": "WAL_2_cymru-north",
    "WAL3": "WAL_3_cymru-south",
    "CRO": "CRO_1_hnl",
    "CZE": "CZE_1_chance-liga",
    "UKR": "UKR_1_premier-league",
    "CYP": "CYP_1_first-division",
    "SVK": "SVK_1_nike-liga",
    "SVN": "SVN_1_prva-liga",
    "FIN": "FIN_1_veikkausliiga",
    "KAZ": "KAZ_1_premier-league",
    "ESP2": "ESP_2_segunda-division",
    "ITA2": "ITA_2_serie-b",
    "FRA2": "FRA_2_ligue-2",
    "NED2": "NED_2_eerste-divisie",
    "BSA": "BRA_1_serie-a",
    "BRA2": "BRA_2_serie-b",
    "ARG": "ARG_1_liga-profesional",
    "ARG2": "ARG_2_primera-nacional",
    "MEX": "MEX_1_liga-mx",
    "MEX2": "MEX_2_liga-expansion",
    "USA": "USA_1_mls",
    "JPN": "JPN_1_j1-league",
    "AUS": "AUS_1_a-league",
    "CHN": "CHN_1_super-league",
}

# API-Football numeric league IDs (verified against the account on 2026-05-27).
# Optional API-Football league IDs can be supplied without a code change:
# APIFOOTBALL_LEAGUE_IDS='{"BEL": 144, "TUR": 203}'
try:
    APIFOOTBALL_LEAGUE_IDS = {
        str(code).upper(): int(league_id)
        for code, league_id in json.loads(os.getenv("APIFOOTBALL_LEAGUE_IDS", "{}")).items()
    }
except (TypeError, ValueError, json.JSONDecodeError):
    APIFOOTBALL_LEAGUE_IDS = {}

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

# Flag code per competition (flagcdn.com), used as the logo fallback when a
# team has no badge — better than showing nothing or just the name.
COMPETITION_COUNTRY = {
    "PL": "gb-eng", "ELC": "gb-eng", "PD": "es", "SA": "it", "BL1": "de",
    "FL1": "fr", "DED": "nl", "PPL": "pt", "CL": "eu", "BSA": "br",
    "BEL": "be", "TUR": "tr", "GRE": "gr", "SCO": "gb-sct", "GER2": "de",
    "ITA2": "it", "ESP2": "es", "FRA2": "fr", "SAU": "sa",
    "JPN": "jp", "CHN": "cn", "RUS": "ru",
    "ENG2": "gb-eng", "ENG3": "gb-eng",
    "SCO2": "gb-sct", "SCO3": "gb-sct", "SCO4": "gb-sct",
    "ARG": "ar", "AUT": "at", "FIN": "fi", "MEX": "mx", "NOR": "no",
    "POL": "pl", "ROU": "ro", "SWE": "se", "SWZ": "ch", "USA": "us",
    "EFLCUP": "gb-eng", "FACUP": "gb-eng", "COPAREY": "es", "COPPA": "it",
    "DFB": "de", "KNVB": "nl", "TACAP": "pt", "SCOTCUP": "gb-sct",
    "SCOTLC": "gb-sct", "OFBCUP": "at", "SWZCUP": "ch", "ENG5": "gb-eng",
    "ENG6N": "gb-eng", "ENG6S": "gb-eng", "ENG7": "gb-eng", "GER3": "de",
    "DEN": "dk", "IRE1": "ie", "IRENAT": "ie", "POL1": "pl", "POL2": "pl",
    "BELU21": "be", "WAL1": "gb-wls", "WAL2": "gb-wls", "WAL3": "gb-wls",
    "CRO": "hr", "CZE": "cz", "UKR": "ua", "CYP": "cy", "SVK": "sk",
    "SVN": "si", "KAZ": "kz", "NED2": "nl", "BRA2": "br", "ARG2": "ar",
    "MEX2": "mx", "AUS": "au", "UEL": "eu", "UECL": "eu",
}


def country_flag_url(competition_code):
    code = COMPETITION_COUNTRY.get(competition_code)
    return f"https://flagcdn.com/w160/{code}.png" if code else None

API_TOKEN = getattr(settings, "FOOTBALL_DATA_API_KEY", os.getenv("FOOTBALL_DATA_API_KEY", ""))
ODDS_API_KEY = getattr(settings, "ODDS_API_KEY", os.getenv("ODDS_API_KEY", ""))
BASE_URL = getattr(settings, "FOOTBALL_DATA_BASE_URL", os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4"))

TEAM_METADATA_CACHE_TIMEOUT = 60 * 60 * 24 * 30
STANDINGS_CACHE_TIMEOUT = 60 * 60 * 6
TRAINING_CACHE_TIMEOUT = 60 * 60 * 24 * 7
OPEN_MATCHDAY_TRAINING_CACHE_TIMEOUT = 60 * 60 * 24  # daily public-feed refresh
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


def get_team_metadata(name, competition_code=None):
    meta = cache.get(team_meta_cache_key(name), {"shortName": name, "crest": None})
    # UK/OM fixture feeds do not include team crests. Use the league country
    # flag when no actual team badge is available.
    fallback_competition = competition_code or meta.get("competition")
    if not meta.get("crest") and fallback_competition:
        flag = country_flag_url(fallback_competition)
        if flag:
            meta = {**meta, "crest": flag}
    return meta
