import json
from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from predict.models import MatchPrediction


class PredictionBiasDiagnosticTests(TestCase):
    def test_reports_signed_goal_bias_and_draw_precision(self):
        MatchPrediction.objects.create(
            match_id="diag-001", home_team="H", away_team="A",
            match_date=date(2026, 11, 1), competition="PL",
            predicted_home_goals=1, predicted_away_goals=1,
            actual_home_goals=2, actual_away_goals=1, status="FINISHED",
        )
        MatchPrediction.objects.create(
            match_id="diag-002", home_team="H2", away_team="A2",
            match_date=date(2026, 11, 2), competition="PL",
            predicted_home_goals=0, predicted_away_goals=2,
            actual_home_goals=0, actual_away_goals=1, status="FINISHED",
        )
        out = StringIO()
        call_command("diagnose_prediction_bias", json=True, stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report["matches"], 2)
        self.assertEqual(report["home_goal_bias_pred_minus_actual"], -0.5)
        self.assertEqual(report["away_goal_bias_pred_minus_actual"], 0.5)
        self.assertEqual(report["draws"]["predicted"], 1)
        self.assertEqual(report["draws"]["actual"], 0)
        self.assertEqual(report["draws"]["precision"], 0.0)
        self.assertEqual(report["draws"]["recall"], 0.0)
        self.assertEqual(report["by_competition"]["PL"]["matches"], 2)

    def test_empty_dataset_returns_null_rates(self):
        out = StringIO()
        call_command("diagnose_prediction_bias", json=True, stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report["matches"], 0)
        self.assertIsNone(report["home_goal_bias_pred_minus_actual"])
        self.assertIsNone(report["draws"]["precision"])
