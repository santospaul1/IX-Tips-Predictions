from unittest import TestCase
from unittest.mock import patch

from predict.fixture_sources import fetch_fixture_matches_by_date


class FixtureAggregationTests(TestCase):
    def setUp(self):
        self.fixture = {
            "id": "uk-1",
            "utcDate": "2026-11-10T15:00:00Z",
            "status": "TIMED",
            "homeTeam": {"name": "Alpha FC", "crest": None},
            "awayTeam": {"name": "Beta", "crest": None},
            "score": {"fullTime": {"home": None, "away": None}},
        }

    @patch("predict.fixture_sources._provider_order", return_value=["UK", "AF"])
    @patch("predict.fixture_sources._dispatch_explicit_provider")
    def test_tries_fallback_and_deduplicates_same_fixture(self, dispatch, provider_order):
        second = {
            **self.fixture,
            "id": 12345,
            "homeTeam": {"name": "Alpha", "crest": "crest.png"},
        }
        dispatch.side_effect = [[self.fixture], [second]]

        matches = fetch_fixture_matches_by_date(None, "BEL", "2026-11-10")

        self.assertEqual(len(matches), 1)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(matches[0]["id"], "uk-1")

    @patch("predict.fixture_sources._provider_order", return_value=["UK", "AF"])
    @patch("predict.fixture_sources._dispatch_explicit_provider")
    def test_provider_failure_does_not_block_fallback(self, dispatch, provider_order):
        dispatch.side_effect = [RuntimeError("provider offline"), [self.fixture]]

        matches = fetch_fixture_matches_by_date(None, "BEL", "2026-11-10")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["homeTeam"]["name"], "Alpha FC")

    @patch("predict.fixture_sources._provider_order", return_value=["UK"])
    @patch("predict.fixture_sources._dispatch_explicit_provider", return_value=[])
    def test_empty_primary_response_returns_empty_list(self, dispatch, provider_order):
        self.assertEqual(fetch_fixture_matches_by_date(None, "BEL", "2026-11-10"), [])
