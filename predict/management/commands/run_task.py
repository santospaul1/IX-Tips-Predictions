"""
Run scheduled IX-Tips jobs without Celery/Redis.

Each job is invoked synchronously by supercronic (see /code/crontab).
This replaces the old Celery worker + beat setup, which polled the Redis
broker 24/7 and exhausted the Upstash monthly command limit.

Usage:
    python manage.py run_task <job> [--date YYYY-MM-DD]
"""
import time

from django.core.management.base import BaseCommand

from predict.constants import COMPETITIONS


class Command(BaseCommand):
    help = "Run a scheduled IX-Tips job (no Celery/Redis required)."

    JOBS = (
        "predictions",  # generate predictions for upcoming fixtures
        "training",     # cache training data for all competitions
        "odds",         # refresh odds + rebuild top picks
        "combo",        # rebuild combo slips
        "tables",       # refresh league standings
        "metadata",     # refresh team metadata (crests, names)
        "live",         # refresh live status, scores, top picks
        "warmform",     # build model bundles -> populate shared team_profiles (recent form)
        "lfrefresh",    # refresh Live-Football-Data season dumps (once/day, quota-bound)
    )

    def add_arguments(self, parser):
        parser.add_argument("job", choices=self.JOBS)
        parser.add_argument("--date", default=None, help="Optional match date (YYYY-MM-DD)")

    def handle(self, *args, **opts):
        job = opts["job"]
        match_date = opts.get("date")
        started = time.time()
        self.stdout.write(f"[run_task] starting '{job}'")

        # Imported lazily so a failing import in one task doesn't block others
        from predict import tasks

        if job == "predictions":
            self._run_predictions(tasks, match_date)
        elif job == "training":
            tasks.cache_training_data()
        elif job == "odds":
            tasks.refresh_daily_odds_cache()
        elif job == "combo":
            tasks.refresh_combo_slips()
        elif job == "tables":
            tasks.refresh_all_league_tables()
        elif job == "metadata":
            tasks.update_metadata_task()
        elif job == "live":
            tasks.refresh_live_match_data()
        elif job == "warmform":
            self._warm_form()
        elif job == "lfrefresh":
            self._lf_refresh()

        elapsed = time.time() - started
        self.stdout.write(f"[run_task] finished '{job}' in {elapsed:.1f}s")

    def _warm_form(self):
        """
        Build each competition's model bundle so its team_profiles land in the
        shared cache, making recent form available on every machine.
        """
        from predict.utils import get_or_train_model_bundle

        for comp in COMPETITIONS:
            self.stdout.write(f"[run_task] warming form for {comp}")
            try:
                get_or_train_model_bundle(comp)
            except Exception as exc:
                self.stderr.write(f"[run_task] {comp} warmform failed: {exc}")

    def _lf_refresh(self):
        """
        Force the once-daily season-dump fetch for each Live-Football-Data league
        so all other reads that day hit the cache (respects the 100/month quota).
        """
        from predict.constants import LIVEFOOTBALL_LEAGUE_IDS
        from predict.providers import lf_refresh_season

        for comp in LIVEFOOTBALL_LEAGUE_IDS:
            try:
                n = lf_refresh_season(comp)
                self.stdout.write(f"[run_task] LF refreshed {comp}: {n} matches")
            except Exception as exc:
                self.stderr.write(f"[run_task] LF refresh {comp} failed: {exc}")

    def _run_predictions(self, tasks, match_date):
        """
        Sequential replacement for the old Celery staggered scheduler.
        Runs one competition at a time to keep peak memory low.
        """
        # Pre-compute cross-league ELO once before the competition loop so the
        # first competition doesn't block while computing it (cold training-data
        # caches would cause 30+ min of API fetches inside the first prediction).
        try:
            from predict.utils import _get_global_elo
            gelo = _get_global_elo()
            self.stdout.write(f"[run_task] global ELO ready: {len(gelo)} teams")
        except Exception as e:
            self.stderr.write(f"[run_task] global ELO skipped: {e}")

        for comp in COMPETITIONS:
            self.stdout.write(f"[run_task] predicting {comp}")
            try:
                tasks.predict_next_fixtures_for_competition(comp, match_date)
            except Exception as exc:  # one competition failing shouldn't stop the rest
                self.stderr.write(f"[run_task] {comp} failed: {exc}")
