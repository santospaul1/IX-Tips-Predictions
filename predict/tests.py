from unittest import TestCase

import pandas as pd

from predict.views import (
    normalize_display_competition_name,
    normalize_display_team_name,
    team_initials,
)
from predict.utils import build_fixture_features, build_training_features


class PredictionFeatureEngineeringTests(TestCase):
    def test_fixture_features_reflect_current_form_and_venue_splits(self):
        history = pd.DataFrame(
            [
                {
                    "home_team": "Alpha",
                    "away_team": "Beta",
                    "home_goals": 3,
                    "away_goals": 0,
                    "utc_date": "2025-01-01",
                },
                {
                    "home_team": "Alpha",
                    "away_team": "Gamma",
                    "home_goals": 2,
                    "away_goals": 0,
                    "utc_date": "2025-01-08",
                },
                {
                    "home_team": "Delta",
                    "away_team": "Beta",
                    "home_goals": 2,
                    "away_goals": 0,
                    "utc_date": "2025-01-15",
                },
                {
                    "home_team": "Epsilon",
                    "away_team": "Beta",
                    "home_goals": 1,
                    "away_goals": 0,
                    "utc_date": "2025-01-22",
                },
            ]
        )

        _, _, _, context = build_training_features(history, lookback=4)
        features = build_fixture_features("Alpha", "Beta", context).iloc[0]

        self.assertGreater(features["home_form"], features["away_form"])
        self.assertGreater(features["home_recent_scored"], features["away_recent_scored"])
        self.assertGreater(features["home_strength"], features["away_strength"])
        self.assertGreaterEqual(features["h2h_home_points"], 1.5)
        self.assertGreater(features["elo_gap"], 0)
        self.assertGreater(features["elo_home_win_prob"], 0.5)

    def test_feature_schema_contains_bias_reduction_signals(self):
        history = pd.DataFrame(
            [
                {
                    "home_team": "Alpha",
                    "away_team": "Beta",
                    "home_goals": 1,
                    "away_goals": 1,
                    "utc_date": "2025-02-01",
                }
            ]
        )

        X, _, _, context = build_training_features(history, lookback=3)

        self.assertIn("home_rest_days", X.columns)
        self.assertIn("away_clean_sheet_rate", X.columns)
        self.assertIn("venue_attack_gap", X.columns)
        self.assertIn("h2h_goal_diff", X.columns)
        self.assertIn("home_elo", X.columns)
        self.assertIn("elo_gap", X.columns)
        self.assertIn("elo_home_win_prob", X.columns)
        self.assertEqual(list(X.columns), context["feature_columns"])


class TeamDisplayFormattingTests(TestCase):
    def test_normalize_display_team_name_removes_common_suffixes(self):
        self.assertEqual(
            normalize_display_team_name("Bayer 04 Leverkusen FC", max_length=24),
            "Bayer 04 Leverkusen",
        )
        self.assertEqual(
            normalize_display_team_name("CS Cristal", max_length=24),
            "CS Cristal",
        )

    def test_normalize_display_team_name_shortens_long_names(self):
        self.assertEqual(
            normalize_display_team_name("Brighton and Hove Albion", max_length=18),
            "B. a. H. Albion",
        )

    def test_team_initials_handles_multiword_and_single_names(self):
        self.assertEqual(team_initials("Arsenal"), "AR")
        self.assertEqual(team_initials("Botafogo FR"), "BF")

    def test_normalize_display_competition_name_prefers_known_labels(self):
        self.assertEqual(normalize_display_competition_name("UEFA Champions League", code="CL"), "UCL")
        self.assertEqual(normalize_display_competition_name("Campeonato Brasileiro Serie A", code="BSA"), "BSA")

    def test_normalize_display_competition_name_cleans_unknown_labels(self):
        self.assertEqual(
            normalize_display_competition_name("FIFA World Cup Qualifiers"),
            "WC Qualifie…",
        )
