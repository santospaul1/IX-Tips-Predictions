from unittest import TestCase
from unittest.mock import patch

from predict.fixture_sources import _provider_order
from predict.utils import fetch_training_data, get_12_predictions, get_daily_market_predictions
from predict.constants import COMPETITIONS, OPEN_MATCHDAY_COMPETITIONS
from django.utils import timezone
from predict.providers import (
    _om_row_to_match,
    om_fetch_matches_by_date,
    supports_open_matchday,
)


class OpenMatchdayProviderTests(TestCase):
    def test_finished_csv_row_is_normalized_to_internal_match_shape(self):
        match = _om_row_to_match(
            {
                "date": "2026-11-21",
                "time": "17:30",
                "home": "Club A",
                "away": "Club B",
                "ft_home": "2",
                "ft_away": "1",
                "ht_home": "1",
                "ht_away": "0",
            },
            "BEL",
            "2627",
            "results",
        )

        self.assertEqual(match["status"], "FINISHED")
        self.assertEqual(match["homeTeam"]["name"], "Club A")
        self.assertEqual(match["score"]["fullTime"], {"home": 2, "away": 1})
        self.assertEqual(match["score"]["halfTime"], {"home": 1, "away": 0})
        self.assertEqual(match["utcDate"], "2026-11-21T17:30:00+01:00")
        self.assertLessEqual(len(match["id"]), 50)
        self.assertEqual(
            match["id"],
            _om_row_to_match(
                {
                    "date": "2026-11-21", "time": "17:30",
                    "home": "Club A", "away": "Club B",
                    "ft_home": "2", "ft_away": "1",
                }, "BEL", "2627", "results"
            )["id"],
        )

    def test_fixture_row_does_not_treat_blank_scores_as_zero(self):
        match = _om_row_to_match(
            {
                "date": "2026-11-22",
                "time": "",
                "home": "Club A",
                "away": "Club B",
                "ft_home": "",
                "ft_away": "",
            },
            "BEL",
            "2627",
            "fixtures",
        )

        self.assertEqual(match["status"], "TIMED")
        self.assertIsNone(match["score"]["fullTime"]["home"])
        self.assertIsNone(match["score"]["fullTime"]["away"])
        self.assertEqual(match["utcDate"], "2026-11-22")

    def test_only_supported_competitions_use_open_matchday(self):
        with patch("predict.providers.OM_ENABLED", True):
            self.assertTrue(supports_open_matchday("BEL"))
            self.assertTrue(supports_open_matchday("JPN"))
            self.assertFalse(supports_open_matchday("SAU"))

    def test_open_matchday_is_appended_after_the_primary_fixture_provider(self):
        with patch("predict.fixture_sources.FIXTURE_PROVIDER_ORDER", {}), \
             patch("predict.fixture_sources.get_provider", return_value="UK"), \
             patch("predict.fixture_sources.supports_open_matchday", return_value=True), \
             patch("predict.fixture_sources.AF_KEY", ""):
            self.assertEqual(_provider_order("BEL"), ["UK", "OM"])

    def test_date_fetch_filters_the_current_feed_by_match_day(self):
        matches = [
            {"utcDate": "2026-11-21T15:00:00+01:00", "homeTeam": {"name": "A"}},
            {"utcDate": "2026-11-22T15:00:00+01:00", "homeTeam": {"name": "B"}},
        ]
        with patch("predict.providers._om_current_matches", return_value=matches):
            result = om_fetch_matches_by_date("BEL", "2026-11-21")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["homeTeam"]["name"], "A")



class OpenMatchdayTrainingMergeTests(TestCase):
    def _finished(self, source_id, home, away, day, hg, ag, odds=None):
        match = {
            "id": source_id,
            "utcDate": f"{day}T15:00:00Z",
            "status": "FINISHED",
            "homeTeam": {"name": home},
            "awayTeam": {"name": away},
            "score": {"fullTime": {"home": hg, "away": ag}},
        }
        if odds:
            match["odds"] = odds
        return match

    @patch("predict.providers.supports_open_matchday", return_value=True)
    @patch("predict.providers.om_fetch_matches_by_season")
    @patch("predict.utils.fetch_matches_by_season")
    def test_training_merges_om_rows_and_keeps_primary_odds(
        self, primary_fetch, om_fetch, provider_support
    ):
        primary_fetch.return_value = [
            self._finished("uk-1", "Alpha FC", "Beta", "2026-11-01", 2, 0,
                           {"avgH": 1.8, "avgD": 3.4, "avgA": 4.2})
        ]
        om_fetch.return_value = [
            self._finished("om-duplicate", "Alpha", "Beta FC", "2026-11-01", 2, 0),
            self._finished("om-new", "Gamma", "Delta", "2026-11-02", 1, 1),
        ]

        df = fetch_training_data("BEL", seasons=[2026])

        self.assertEqual(len(df), 2)
        alpha = df[df["home_team"].str.startswith("Alpha")].iloc[0]
        self.assertEqual(alpha["avgH"], 1.8)
        self.assertIn("Gamma", set(df["home_team"]))

    @patch("predict.providers.supports_open_matchday", return_value=True)
    @patch("predict.providers.om_fetch_matches_by_season")
    @patch("predict.utils.fetch_matches_by_season", return_value=[])
    def test_results_only_league_still_trains_when_no_fixtures_exist(
        self, primary_fetch, om_fetch, provider_support
    ):
        om_fetch.return_value = [
            self._finished("om-1", "China Club A", "China Club B", "2026-11-08", 4, 0)
        ]

        df = fetch_training_data("CHN", seasons=[2026])

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["home_goals"], 4)


class OpenMatchdayCoverageAndDailyPickTests(TestCase):
    def test_all_75_open_matchday_competitions_are_registered(self):
        self.assertEqual(len(OPEN_MATCHDAY_COMPETITIONS), 75)
        self.assertTrue(set(OPEN_MATCHDAY_COMPETITIONS).issubset(set(COMPETITIONS)))

    def test_twelve_picks_are_limited_to_today_and_capped_at_twelve(self):
        today = timezone.localdate().isoformat()
        tomorrow = (timezone.localdate() + __import__("datetime").timedelta(days=1)).isoformat()
        candidates = {
            today: [
                {
                    "home_team": f"Home {i}", "away_team": f"Away {i}",
                    "tip": "1" if i % 2 else "2",
                    "confidence_value": 90 - i, "rank_index": 0,
                    "odds": None,
                }
                for i in range(13)
            ],
            tomorrow: [{
                "home_team": "Tomorrow FC", "away_team": "Future FC",
                "tip": "1", "confidence_value": 99, "rank_index": 0,
                "odds": None,
            }],
        }

        picks = get_12_predictions(candidates_by_date=candidates)

        self.assertEqual(list(picks), [today])
        self.assertEqual(len(picks[today]), 12)
        self.assertTrue(all(item["match_date"] == today for item in picks[today]))

    def test_daily_market_lists_only_return_the_requested_market_for_today(self):
        today = timezone.localdate().isoformat()
        tomorrow = (timezone.localdate() + __import__("datetime").timedelta(days=1)).isoformat()
        candidates = {
            today: [
                {"home_team": "A", "away_team": "B", "tip": "GG", "confidence_value": 87, "rank_index": 0},
                {"home_team": "C", "away_team": "D", "tip": "Over 2.5", "confidence_value": 82, "rank_index": 1},
            ],
            tomorrow: [
                {"home_team": "E", "away_team": "F", "tip": "GG", "confidence_value": 99, "rank_index": 0},
            ],
        }

        gg = get_daily_market_predictions("GG", candidates_by_date=candidates)
        over = get_daily_market_predictions("Over 2.5", candidates_by_date=candidates)

        self.assertEqual([item["tip"] for item in gg[today]], ["GG"])
        self.assertEqual([item["tip"] for item in over[today]], ["Over 2.5"])
        self.assertEqual(len(gg[today]), 1)
        self.assertEqual(len(over[today]), 1)
