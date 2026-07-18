"""
Centralised cache-key registry for the IX-Tips project.

Every cache key is defined here with its pattern, TTL, and invalidation story
so that no two pieces of code invent the same key differently, and so that
discovery (what is cached? for how long?) doesn't require grepping the entire
codebase.

Usage:
    from predict.cache_keys import CacheKeys
    cache.set(CacheKeys.team_meta("Arsenal"), {...}, timeout=CacheKeys.TTL_TEAM_META)
"""

# ── TTL constants ─────────────────────────────────────────────────────────────

TTL = type("TTL", (), {
    "TEAM_META":          60 * 60 * 24 * 30,  # 30 days — team names/crests rarely change
    "STANDINGS":          60 * 60 * 6,         # 6 hours — league tables update after matchdays
    "TRAINING_DATA":      60 * 60 * 24 * 7,    # 7 days — past seasons never change
    "MODEL_BUNDLE":       60 * 60 * 6,         # 6 hours — model rebuilt daily but fine to be stale
    "TEAM_PROFILES":      60 * 60 * 48,        # 2 days — populated by daily warmform cron
    "SCORERS":            60 * 60 * 12,        # 12 hours — scorer standings update slowly
    "ODDS":               60 * 10,             # 10 minutes — odds change rapidly pre-match
    "LIVE_MATCH_CACHE":   60 * 60 * 6,         # 6 hours — kickoff times, status
    "NAME_NORMALIZATION": 300,                 # 5 minutes
}, frozen=True)


class CacheKeys:
    """All cache-key templates used in the project."""

    # ── Team & competition metadata ───────────────────────────────────────────
    @staticmethod
    def team_meta(team_name):
        return f"team_meta::{team_name}"

    @staticmethod
    def competition_meta(comp_code):
        return f"competition_meta::{comp_code}"

    @staticmethod
    def competition_cached(comp_code):
        return f"competition_cached::{comp_code}"

    # ── Training & model bundles ──────────────────────────────────────────────
    @staticmethod
    def training_data(comp_code):
        return f"training_data_{comp_code}"

    @staticmethod
    def model_bundle(comp_code):
        return f"model_bundle::{comp_code}"

    @staticmethod
    def team_profiles(comp_code):
        return f"team_profiles::{comp_code}"

    # ── Standings & fixtures ──────────────────────────────────────────────────
    @staticmethod
    def standings(comp_code):
        return f"standings_{comp_code}"

    @staticmethod
    def fixture_meta(comp_code, match_date, home, away):
        return f"fixture_meta::{comp_code}::{match_date}::{home}::{away}"

    @staticmethod
    def fixture_refresh(comp_code, match_date):
        return f"fixture_refresh::{comp_code}::{match_date}"

    @staticmethod
    def actual_scorers(comp_code, match_date, home, away):
        return (f"actual_scorers::{comp_code}::{match_date}"
                f"::{home}::{away}")

    # ── Odds ──────────────────────────────────────────────────────────────────
    @staticmethod
    def competition_odds_refresh(comp_code):
        return f"competition_odds_refresh::{comp_code}"

    # ── Scorers ───────────────────────────────────────────────────────────────
    @staticmethod
    def competition_scorers(comp_code):
        return f"competition_scorers::{comp_code}"

    # ── Combo slips ───────────────────────────────────────────────────────────
    @staticmethod
    def combo_tracking_summary():
        return "combo_slip_tracking_summary_v1"

    @staticmethod
    def recent_combo_slips(limit):
        return f"recent_saved_combo_slips_v2::{limit}"

    # ── Provider-specific (LF / UK) ───────────────────────────────────────────
    @staticmethod
    def lf_season_matches(comp_code):
        return f"lf_season_matches::{comp_code}"

    @staticmethod
    def uk_current(comp_code):
        return f"uk_current::{comp_code}"

    @staticmethod
    def uk_main(fduk_code, season_year):
        return f"uk_main::{fduk_code}::{season_year}"

    uk_fixtures = "uk_fixtures_all"

    # ── Health ────────────────────────────────────────────────────────────────
    health_ping = "health_check::ping"


# Re-export TTL constants from the module alongside the keys
TTL_TEAM_META = TTL.TEAM_META
TTL_STANDINGS = TTL.STANDINGS
TTL_TRAINING_DATA = TTL.TRAINING_DATA
TTL_MODEL_BUNDLE = TTL.MODEL_BUNDLE
TTL_TEAM_PROFILES = TTL.TEAM_PROFILES
TTL_SCORERS = TTL.SCORERS
TTL_ODDS = TTL.ODDS
