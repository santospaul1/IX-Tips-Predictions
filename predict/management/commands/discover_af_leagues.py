"""
List API-Football league IDs so you can confirm the values in
constants.APIFOOTBALL_LEAGUE_IDS for your account.

Usage:
    python manage.py discover_af_leagues                 # all leagues (long)
    python manage.py discover_af_leagues --country USA
    python manage.py discover_af_leagues --search "Pro League"

Requires APIFOOTBALL_KEY to be set.
"""
from django.core.management.base import BaseCommand

from predict.providers import _af_get


class Command(BaseCommand):
    help = "List API-Football league IDs (filter by --country or --search)."

    def add_arguments(self, parser):
        parser.add_argument("--country", default=None, help="Country name, e.g. USA, Mexico")
        parser.add_argument("--search", default=None, help="Substring to match in the league name")

    def handle(self, *args, **opts):
        params = {}
        if opts.get("country"):
            params["country"] = opts["country"]
        if opts.get("search"):
            params["search"] = opts["search"]

        rows = _af_get("leagues", params)
        if not rows:
            self.stdout.write("No leagues returned (is APIFOOTBALL_KEY set and valid?).")
            return

        self.stdout.write(f"{'ID':>6}  {'COUNTRY':<20}  TYPE      NAME")
        self.stdout.write("-" * 70)
        for row in rows:
            league = row.get("league", {}) or {}
            country = (row.get("country", {}) or {}).get("name", "")
            seasons = row.get("seasons", []) or []
            latest = seasons[-1].get("year") if seasons else "?"
            self.stdout.write(
                f"{league.get('id', '?'):>6}  {country:<20}  "
                f"{(league.get('type') or ''):<8}  {league.get('name', '')} (latest season {latest})"
            )
