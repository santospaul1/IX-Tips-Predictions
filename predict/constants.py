import os
import json
import re
import unicodedata
from functools import lru_cache

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
    "UEL": "UEFA Europa League", "UECL": "UEFA Conference League",
    "ALG": "Algerian Ligue Professionnelle 1", "EGY": "Egyptian Premier League",
    "GHA": "Ghana Premier League", "KEN": "FKF Premier League",
    "MWI": "Malawi Super League", "MAR": "Botola Pro",
    "NGA": "Nigeria Professional Football League", "RWA": "Rwanda Premier League",
    "SEN": "Senegal Ligue 1", "ZAF": "South African Premiership",
    "TZA": "Tanzania Premier League", "UGA": "Uganda Premier League",
    "ZMB": "Zambia Super League", "ZWE": "Zimbabwe Premier Soccer League"
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
    "ALG": "OF", "EGY": "OF", "GHA": "OF", "KEN": "OF", "MWI": "OF", "MAR": "OF",
    "NGA": "OF", "RWA": "OF", "SEN": "OF", "ZAF": "OF", "TZA": "OF",
    "UGA": "OF", "ZMB": "OF", "ZWE": "OF",
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

OPENFOOTBALL_COMPETITIONS = {
    "ALG": {"country": "algeria", "suffix": "dz1", "calendar_year": False},
    "EGY": {"country": "egypt", "suffix": "eg1", "calendar_year": False},
    "GHA": {"country": "ghana", "suffix": "gh1", "calendar_year": False},
    "KEN": {"country": "kenya", "suffix": "ke1", "calendar_year": False},
    "MWI": {"country": "malawi", "suffix": "mw1", "calendar_year": True},
    "MAR": {"country": "morocco", "suffix": "ma1", "calendar_year": False},
    "NGA": {"country": "nigeria", "suffix": "ng1", "calendar_year": False},
    "RWA": {"country": "rwanda", "suffix": "rw1", "calendar_year": False},
    "SEN": {"country": "senegal", "suffix": "sn1", "calendar_year": False},
    "ZAF": {"country": "south-africa", "suffix": "za1", "calendar_year": False},
    "TZA": {"country": "tanzania", "suffix": "tz1", "calendar_year": False},
    "UGA": {"country": "uganda", "suffix": "ug1", "calendar_year": False},
    "ZMB": {"country": "zambia", "suffix": "zm1", "calendar_year": False},
    "ZWE": {"country": "zimbabwe", "suffix": "zw1", "calendar_year": True},
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

# Curated competition-strength ordering used to group public predictions.
# Remaining competitions follow alphabetically after these primary competitions.
COMPETITION_DISPLAY_ORDER = (
    "PL", "CL", "PD", "SA", "BL1", "FL1", "ELC", "DED", "PPL",
    "BEL", "TUR", "SCO", "BSA", "CLI", "SAU",
)

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
    "ALG": "dz", "EGY": "eg", "GHA": "gh", "KEN": "ke", "MWI": "mw",
    "MAR": "ma", "NGA": "ng", "RWA": "rw", "SEN": "sn", "ZAF": "za",
    "TZA": "tz", "UGA": "ug", "ZMB": "zm", "ZWE": "zw",
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
OPENFOOTBALL_TRAINING_CACHE_TIMEOUT = 60 * 60 * 24
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


# Country catalog used to resolve missing team crests without making a
# network request for every fixture. Image files remain hosted by football-logos.cc.
FOOTBALL_LOGO_COUNTRY_BY_COMPETITION = {
    "PL": "england", "ELC": "england", "ENG2": "england", "ENG3": "england",
    "ENG5": "england", "ENG6N": "england", "ENG6S": "england", "ENG7": "england",
    "EFLCUP": "england", "FACUP": "england",
    "PD": "spain", "ESP2": "spain", "COPAREY": "spain",
    "SA": "italy", "ITA2": "italy", "COPPA": "italy",
    "BL1": "germany", "GER2": "germany", "GER3": "germany", "DFB": "germany",
    "FL1": "france", "FRA2": "france",
    "DED": "netherlands", "NED2": "netherlands", "KNVB": "netherlands",
    "PPL": "portugal", "TACAP": "portugal",
    "AUT": "austria", "OFBCUP": "austria",
    "SWZ": "switzerland", "SWZCUP": "switzerland",
    "SCO": "scotland", "SCO2": "scotland", "SCO3": "scotland",
    "SCO4": "scotland", "SCOTCUP": "scotland", "SCOTLC": "scotland",
    "BEL": "belgium", "BELU21": "belgium", "TUR": "turkey", "GRE": "greece",
    "DEN": "denmark", "SWE": "sweden", "NOR": "norway", "ROU": "romania",
    "RUS": "russia", "IRE1": "republic-of-ireland", "IRENAT": "republic-of-ireland",
    "POL": "poland", "POL1": "poland", "POL2": "poland",
    "WAL1": "wales", "WAL2": "wales", "WAL3": "wales",
    "CRO": "croatia", "CZE": "czech-republic", "UKR": "ukraine",
    "CYP": "cyprus", "SVK": "slovakia", "SVN": "slovenia", "FIN": "finland",
    "KAZ": "kazakhstan", "BSA": "brazil", "BRA2": "brazil",
    "ARG": "argentina", "ARG2": "argentina", "MEX": "mexico", "MEX2": "mexico",
    "USA": "usa", "JPN": "japan", "AUS": "australia", "CHN": "china",
    "SAU": "saudi-arabia",
    "ALG": "algeria", "EGY": "egypt", "GHA": "ghana", "KEN": "kenya",
    "MWI": "malawi", "MAR": "morocco", "NGA": "nigeria", "RWA": "rwanda",
    "SEN": "senegal", "ZAF": "south-africa", "TZA": "tanzania",
    "UGA": "uganda", "ZMB": "zambia", "ZWE": "zimbabwe",
    # Continental competitions contain clubs from many countries.
    "CL": "tournaments", "UEL": "tournaments", "UECL": "tournaments",
    "CLI": "tournaments",
}

FOOTBALL_COMPETITION_LOGO_SLUGS = {
    "PL": ("england", "english-premier-league"),
    "PD": ("spain", "la-liga"),
    "SA": ("italy", "serie-a"),
    "BL1": ("germany", "bundesliga"),
    "FL1": ("france", "ligue-1"),
    "DED": ("netherlands", "eredivisie"),
    "PPL": ("portugal", "primeira-liga"),
    "ELC": ("england", "efl-championship"),
    "CL": ("tournaments", "uefa-champions-league"),
    "BSA": ("brazil", "brazilian-serie-a"),
    "CLI": ("tournaments", "conmebol-libertadores"),
    "SAU": ("saudi-arabia", "saudi-professional-league"),
    "BEL": ("belgium", "jupiler-pro-league"),
    "TUR": ("turkey", "super-lig"),
    "GRE": ("greece", "super-league-1"),
    "SCO": ("scotland", "scottish-premiership"),
    "GER2": ("germany", "2-bundesliga"),
    "ITA2": ("italy", "serie-b"),
    "ESP2": ("spain", "la-liga-2"),
    "FRA2": ("france", "ligue-2"),
    "JPN": ("japan", "j-league"),
    "CHN": ("china", "chinese-super-league"),
    "RUS": ("russia", "russian-premier-league"),
    "ENG2": ("england", "efl-league-one"),
    "ENG3": ("england", "efl-league-two"),
    "SCO2": ("scotland", "scottish-championship"),
    "SCO3": ("scotland", "scottish-league-one"),
    "SCO4": ("scotland", "scottish-league-two"),
    "ARG": ("argentina", "argentina-primera-division"),
    "AUT": ("austria", "austrian-football-bundesliga"),
    "FIN": ("finland", "veikkausliiga"),
    "MEX": ("mexico", "liga-mx"),
    "NOR": ("norway", "eliteserien"),
    "POL": ("poland", "ekstraklasa"),
    "ROU": ("romania", "superliga"),
    "SWE": ("sweden", "allsvenskan"),
    "SWZ": ("switzerland", "swiss-football-league"),
    "USA": ("usa", "mls"),
    "EFLCUP": ("england", "efl-cup"),
    "FACUP": ("england", "emirates-fa-cup"),
    "DFB": ("germany", "dfb-pokal"),
    "KNVB": ("netherlands", "knvb-cup"),
    "ENG5": ("england", "national-league"),
    "ENG6N": ("england", "national-league-north"),
    "ENG6S": ("england", "national-league-south"),
    "ENG7": ("england", "northern-premier-league"),
    "GER3": ("germany", "3-liga"),
    "DEN": ("denmark", "danish-super-liga"),
    "IRE1": ("republic-of-ireland", "league-of-ireland-first-division"),
    "POL1": ("poland", "1-liga"),
    "WAL1": ("wales", "cymru-premier"),
    "WAL2": ("wales", "cymru-north"),
    "WAL3": ("wales", "cymru-south"),
    "CRO": ("croatia", "croatian-football-league"),
    "CZE": ("czech-republic", "chance-liga"),
    "UKR": ("ukraine", "ukrainian-premier-league"),
    "CYP": ("cyprus", "cypriot-first-division"),
    "SVK": ("slovakia", "slovak-first-football-league"),
    "SVN": ("slovenia", "slovenian-prvaliga"),
    "KAZ": ("kazakhstan", "kazakhstan-premier-league"),
    "NED2": ("netherlands", "eerste-divisie"),
    "BRA2": ("brazil", "brazilian-serie-b"),
    "ARG2": ("argentina", "primera-nacional"),
    "MEX2": ("mexico", "liga-de-expansion-mx"),
    "AUS": ("australia", "a-league"),
    "UEL": ("tournaments", "uefa-europa-league"),
    "UECL": ("tournaments", "uefa-conference-league"),
}


def _normalize_football_logo_name(name):
    value = unicodedata.normalize("NFKD", str(name or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    # Provider feeds and logo catalogues often disagree on common club suffixes.
    # Strip only generic abbreviations; ambiguous global matches remain rejected.
    value = re.sub(r"\b(?:football club|soccer club|fc|cf|afc|sc|club)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@lru_cache(maxsize=1)
def _football_logo_indexes():
    catalog_path = os.path.join(os.path.dirname(__file__), "data", "football_logos.json")
    try:
        with open(catalog_path, encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except (OSError, ValueError):
        return {}, {}

    by_country = {}
    globally = {}
    for country_slug, country in catalog.get("countries", {}).items():
        country_index = {}
        for item in country.get("clubs", []):
            key = _normalize_football_logo_name(item.get("name"))
            if not key or not item.get("slug") or not item.get("hash"):
                continue
            country_index.setdefault(key, []).append(item)
            globally.setdefault(key, []).append((country_slug, item))
        by_country[country_slug] = country_index
    return by_country, globally


def _football_logo_url(country_slug, item):
    if not country_slug or not item:
        return None
    return (
        "https://assets.football-logos.cc/logos/"
        f"{country_slug}/512x512/{item['slug']}.{item['hash']}.png"
    )


# Provider names and UI short names mapped to the catalogue's canonical
# normalized names. Keep aliases explicit: fuzzy matching can assign the
# wrong crest to clubs with similar names.
FOOTBALL_TEAM_LOGO_ALIASES = {
    # England
    "man city": "manchester city",
    "city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "wolves": "wolverhampton wanderers",
    "west ham united": "west ham",
    "newcastle united": "newcastle",
    "nottingham forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "sheffield utd": "sheffield united",
    "sheff utd": "sheffield united",
    "sheffield weds": "sheffield wednesday",
    "qpr": "queens park rangers",
    "west brom": "west bromwich albion",
    "leicester city": "leicester",
    "luton": "luton town",
    "ipswich town": "ipswich",
    "afc bournemouth": "bournemouth",
    "brighton and hove albion": "brighton",
    "cardiff": "cardiff city",
    "wrexham": "wrexham",
    "blackburn": "blackburn rovers",
    "bolton": "bolton wanderers",
    "middlesbrough fc": "middlesbrough",
    "derby": "derby county",
    "portsmouth fc": "portsmouth",
    "preston": "preston north end",
    "stoke": "stoke city",
    "swansea": "swansea city",
    "watford fc": "watford",
    "coventry": "coventry city",
    "plymouth": "plymouth argyle",
    "oxford": "oxford united",
    "bristol rovers fc": "bristol rovers",
    "notts": "notts county",
    # Spain
    "athletic": "athletic bilbao",
    "athletic club": "athletic bilbao",
    "athletic bilbao": "athletic bilbao",
    "barca": "barcelona",
    "fc barcelona": "barcelona",
    "atleti": "atletico madrid",
    "atletico": "atletico madrid",
    "real sociedad de futbol": "real sociedad",
    "deportivo alaves": "deportivo alaves",
    "alaves": "deportivo alaves",
    "rayo": "rayo vallecano",
    "las palmas ud": "las palmas",
    "sporting gijon": "sporting gijon",
    "real valladolid cf": "real valladolid",
    "racing de santander": "racing santander",
    # Italy
    "ac milan": "milan",
    "internazionale": "inter",
    "inter milan": "inter",
    "juventus fc": "juventus",
    "roma": "as roma",
    "napoli fc": "napoli",
    "hellas": "hellas verona",
    "verona": "hellas verona",
    "sassuolo calcio": "sassuolo",
    "torino fc": "torino",
    "udinese calcio": "udinese",
    # Germany
    "bayern": "bayern munich",
    "fc bayern munich": "bayern munich",
    "bvb": "borussia dortmund",
    "dortmund": "borussia dortmund",
    "gladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    "leverkusen": "bayer leverkusen",
    "rb leipzig": "rb leipzig",
    "koln": "cologne",
    "fc koln": "cologne",
    "st pauli": "st pauli",
    "wolfsburg": "wolfsburg",
    # France
    "psg": "psg",
    "paris saint germain": "psg",
    "paris saint-germain": "psg",
    "olympique marseille": "marseille",
    "om": "marseille",
    "olympique lyon": "olympique lyonnais",
    "ol": "olympique lyonnais",
    "monaco": "as monaco",
    "rc lens": "rc lens",
    "losc": "lille",
    "saint etienne": "as saint etienne",
    # Netherlands
    "psv": "psv eindhoven",
    "psv eindhoven": "psv eindhoven",
    "feyenoord rotterdam": "feyenoord",
    "fc twente": "twente",
    "heerenveen": "sc heerenveen",
    "nec": "n e c nijmegen",
    # Portugal
    "sporting lisbon": "sporting cp",
    "sporting clube de portugal": "sporting cp",
    "sl benfica": "benfica",
    "fc porto": "fc porto",
    "vitoria guimaraes": "vitoria de guimaraes",
    "braga": "sc braga",
    # Belgium / Scotland / Turkey / Greece
    "anderlecht": "rsc anderlecht",
    "royal antwerp": "royal antwerp",
    "antwerp": "royal antwerp",
    "club brugge kv": "club brugge",
    "genk": "krc genk",
    "gent": "kaa gent",
    "union sg": "union saint gilloise",
    "celtic fc": "celtic",
    "rangers fc": "rangers",
    "hearts fc": "hearts",
    "hibs": "hibernian",
    "besiktas": "besiktas",
    "fener": "fenerbahce",
    "fenerbahce istanbul": "fenerbahce",
    "galatasaray istanbul": "galatasaray",
    "trabzonspor": "trabzonspor",
    "olympiakos": "olympiacos",
    "aek": "aek athens",
    "paok": "paok",
    "panathinaikos fc": "panathinaikos",
    # Saudi Arabia
    "al hilal saudi": "al hilal",
    "al nassr fc": "al nassr",
    "al ahli saudi": "al ahli",
    "al ittihad jeddah": "al ittihad",
    "al shabab riyadh": "al shabab",
    # Brazil
    "athletico pr": "athletico paranaense",
    "atletico mineiro": "atletico mineiro",
    "galo": "atletico mineiro",
    "corinthians sp": "corinthians",
    "palmeiras sp": "palmeiras",
    "flamengo rj": "flamengo",
    "sao paulo fc": "sao paulo",
    "internacional rs": "internacional",
    "gremio": "gremio",
    "santos": "santos fc",
    "charleroi": "royal charleroi",
    "rb bragantino": "rb bragantino",
    # Argentina / Mexico / USA / Japan
    "river": "river plate",
    "boca": "boca juniors",
    "racing club de avellaneda": "racing club",
    "independiente de avellaneda": "independiente",
    "club america": "club america",
    "america": "club america",
    "pumas": "unam pumas",
    "tigres": "tigres uanl",
    "chivas": "cd guadalajara",
    "guadalajara": "cd guadalajara",
    "la galaxy": "los angeles galaxy",
    "lafc": "los angeles football club",
    "nycfc": "new york city football club",
    "new york city fc": "new york city football club",
    "yokohama marinos": "yokohama f marinos",
    "urawa": "urawa reds",
}


def get_football_team_logo_url(team_name, competition_code=None):
    key = _normalize_football_logo_name(team_name)
    if not key:
        return None
    key = FOOTBALL_TEAM_LOGO_ALIASES.get(key, key)

    by_country, globally = _football_logo_indexes()
    country_slug = FOOTBALL_LOGO_COUNTRY_BY_COMPETITION.get(competition_code)
    if country_slug and country_slug != "tournaments":
        country_matches = by_country.get(country_slug, {}).get(key, [])
        if len(country_matches) == 1:
            return _football_logo_url(country_slug, country_matches[0])
        if len(country_matches) > 1:
            return None

    # International cups and unknown competition codes: only use a global
    # result when the normalized team name is unambiguous across countries.
    matches = globally.get(key, [])
    if len(matches) == 1:
        return _football_logo_url(*matches[0])
    return None


@lru_cache(maxsize=1)
def _football_competition_logo_index():
    catalog_path = os.path.join(os.path.dirname(__file__), "data", "football_logos.json")
    try:
        with open(catalog_path, encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except (OSError, ValueError):
        return {}
    return {
        country_slug: {
            item.get("slug"): item
            for item in country.get("leagues", [])
            if item.get("slug") and item.get("hash")
        }
        for country_slug, country in catalog.get("countries", {}).items()
    }


def get_football_competition_logo_url(competition_code):
    location = FOOTBALL_COMPETITION_LOGO_SLUGS.get(competition_code)
    if not location:
        return None
    country_slug, target_slug = location
    item = _football_competition_logo_index().get(country_slug, {}).get(target_slug)
    return _football_logo_url(country_slug, item) if item else None


def _is_country_flag_url(url):
    return str(url or "").startswith("https://flagcdn.com/")


def get_team_metadata(name, competition_code=None):
    key = team_meta_cache_key(name)
    meta = cache.get(key, {"shortName": name, "crest": "", "competition": competition_code})
    fallback_competition = competition_code or meta.get("competition")

    # A previous request may have cached a country flag in "crest". Preserve
    # real provider crests separately so a cached flag never masks them.
    cached_crest = meta.get("crest")
    provider_crest = meta.get("provider_crest")
    if cached_crest and not _is_country_flag_url(cached_crest):
        provider_crest = provider_crest or cached_crest

    # Always check the bundled club catalogue before accepting any fallback.
    team_logo = get_football_team_logo_url(name, fallback_competition)
    if not team_logo and meta.get("shortName") and meta.get("shortName") != name:
        team_logo = get_football_team_logo_url(meta["shortName"], fallback_competition)
    if team_logo:
        meta = {
            **meta,
            "crest": team_logo,
            "logo_source": "football-logos.cc",
        }
        cache.set(key, meta, timeout=TEAM_METADATA_CACHE_TIMEOUT)
        return meta

    # Provider badge is the second choice. Never mistake a cached flag for one.
    if provider_crest and not _is_country_flag_url(provider_crest):
        meta = {
            **meta,
            "crest": provider_crest,
            "provider_crest": provider_crest,
            "logo_source": meta.get("logo_source") if meta.get("logo_source") not in (
                None, "competition-country-flag", "football-logos.cc"
            ) else "provider",
        }
        cache.set(key, meta, timeout=TEAM_METADATA_CACHE_TIMEOUT)
        return meta

    # Country flag is strictly the final fallback when no club badge exists.
    flag = country_flag_url(fallback_competition) if fallback_competition else None
    if flag:
        meta = {
            **meta,
            "crest": flag,
            "logo_source": "competition-country-flag",
        }
        cache.set(key, meta, timeout=TEAM_METADATA_CACHE_TIMEOUT)
    return meta
