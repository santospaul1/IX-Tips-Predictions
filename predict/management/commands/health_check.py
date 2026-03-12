from django.core.cache import cache
from django.core.management.base import BaseCommand

from predict.constants import API_TOKEN, BASE_URL, ODDS_API_KEY
from predict.utils import _get_json


class Command(BaseCommand):
    help = "Check local config, Redis cache, and football-data API connectivity."

    def handle(self, *args, **options):
        failures = 0

        self.stdout.write("Checking configuration")
        failures += self._check_value("FOOTBALL_DATA_API_KEY", API_TOKEN)
        failures += self._check_value("ODDS_API_KEY", ODDS_API_KEY, required=False)
        self.stdout.write(self.style.SUCCESS(f"BASE_URL: {BASE_URL}"))

        self.stdout.write("Checking cache")
        failures += self._check_cache()

        self.stdout.write("Checking football-data API")
        failures += self._check_football_data_api()

        if failures:
            self.stdout.write(self.style.ERROR(f"Health check failed with {failures} issue(s)."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Health check passed."))

    def _check_value(self, name, value, required=True):
        if value:
            self.stdout.write(self.style.SUCCESS(f"{name}: configured (length={len(value)})"))
            return 0

        if required:
            self.stdout.write(self.style.ERROR(f"{name}: missing"))
            return 1

        self.stdout.write(self.style.WARNING(f"{name}: missing (optional)"))
        return 0

    def _check_cache(self):
        cache_key = "health_check::ping"
        try:
            cache.set(cache_key, "ok", timeout=30)
            value = cache.get(cache_key)
            cache.delete(cache_key)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Cache unavailable: {exc}"))
            return 1

        if value != "ok":
            self.stdout.write(self.style.ERROR(f"Cache read/write failed: expected 'ok', got {value!r}"))
            return 1

        self.stdout.write(self.style.SUCCESS("Cache read/write OK"))
        return 0

    def _check_football_data_api(self):
        if not API_TOKEN:
            self.stdout.write(self.style.ERROR("Football-data API check skipped: missing token"))
            return 1

        competitions_url = f"{BASE_URL}/competitions/PL/matches"
        payload = _get_json(
            competitions_url,
            headers={"X-Auth-Token": API_TOKEN},
            params={"season": 2025},
            retries=1,
            delay=1,
        )

        if payload is None:
            self.stdout.write(
                self.style.ERROR(
                    "Football-data API request failed. Check DNS, outbound network, API token, and provider status."
                )
            )
            return 1

        match_count = len(payload.get("matches", []))
        self.stdout.write(self.style.SUCCESS(f"Football-data API reachable. Sample match count: {match_count}"))
        return 0
