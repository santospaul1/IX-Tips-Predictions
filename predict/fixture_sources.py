"""
Multi-provider fixture aggregation.

Historical match ingestion and upcoming fixture discovery are deliberately
separate. COMPETITION_PROVIDERS continues to choose the primary source for
historical data and league metadata. Fixture sources can be configured
independently with FIXTURE_PROVIDER_ORDER.

Example:
    FIXTURE_PROVIDER_ORDER='{"BEL":["UK","AF"],"TUR":["UK","AF"]}'
"""
import json
import logging
import os
import re

from .providers import dispatch_provider, get_provider, AF_KEY
from .constants import APIFOOTBALL_LEAGUE_IDS

logger = logging.getLogger(__name__)

try:
    FIXTURE_PROVIDER_ORDER = json.loads(os.getenv("FIXTURE_PROVIDER_ORDER", "{}"))
    if not isinstance(FIXTURE_PROVIDER_ORDER, dict):
        FIXTURE_PROVIDER_ORDER = {}
except (TypeError, ValueError, json.JSONDecodeError):
    logger.exception("Invalid FIXTURE_PROVIDER_ORDER JSON; using primary providers")
    FIXTURE_PROVIDER_ORDER = {}


def _provider_order(competition_code):
    configured = FIXTURE_PROVIDER_ORDER.get(competition_code)
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)):
        configured = [get_provider(competition_code)]

    providers = []
    for provider in configured:
        provider = str(provider).strip().upper()
        if provider in {"FD", "UK", "LF", "AF"} and provider not in providers:
            providers.append(provider)

    # API-Football is an independent fallback only when both its key and
    # competition league ID are configured. It never replaces the primary.
    if AF_KEY and competition_code in APIFOOTBALL_LEAGUE_IDS and "AF" not in providers:
        providers.append("AF")
    return providers


def _team_key(value):
    value = (value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _fixture_key(match):
    home = (match.get("homeTeam") or {}).get("name")
    away = (match.get("awayTeam") or {}).get("name")
    kickoff = (match.get("utcDate") or "")[:16]
    if not home or not away or not kickoff:
        return None
    return kickoff, _team_key(home), _team_key(away)


def _has_result(match):
    score = (match.get("score") or {}).get("fullTime") or {}
    return score.get("home") is not None and score.get("away") is not None


def _dispatch_explicit_provider(provider, competition_code, match_date):
    """Call one named provider without changing the competition history source."""
    from . import providers

    handler = providers._PROVIDER_DISPATCH.get(provider, {}).get("fetch_matches_by_date")
    if handler is None:
        logger.warning("No fixture handler registered for provider %s", provider)
        return []
    return handler(competition_code, match_date)


def fetch_fixture_matches_by_date(api_key, competition_code, match_date):
    """
    Fetch fixtures from the configured provider chain and merge duplicates.

    api_key remains in the signature for compatibility with the existing date
    helpers; credentials are owned by each provider.
    """
    merged = {}
    for provider in _provider_order(competition_code):
        try:
            matches = _dispatch_explicit_provider(provider, competition_code, match_date)
        except Exception:
            logger.exception(
                "Fixture provider %s failed for %s on %s",
                provider, competition_code, match_date,
            )
            continue

        for match in matches or []:
            key = _fixture_key(match)
            if key is None:
                continue
            existing = merged.get(key)
            if existing is None or (_has_result(match) and not _has_result(existing)):
                merged[key] = match

    return sorted(
        merged.values(),
        key=lambda item: (item.get("utcDate") or "", (item.get("homeTeam") or {}).get("name") or ""),
    )
