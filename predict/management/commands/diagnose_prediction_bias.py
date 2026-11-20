import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from predict.models import MatchPrediction


def outcome(h, a):
    return "Home" if h > a else "Away" if h < a else "Draw"


def rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


class Command(BaseCommand):
    help = "Read-only diagnostics for saved score forecasts, bias and draws."

    def add_arguments(self, parser):
        parser.add_argument("--competition")
        parser.add_argument("--min-date")
        parser.add_argument("--max-date")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        qs = MatchPrediction.objects.filter(
            status="FINISHED", actual_home_goals__isnull=False,
            actual_away_goals__isnull=False, predicted_home_goals__isnull=False,
            predicted_away_goals__isnull=False,
        )
        if opts.get("competition"):
            qs = qs.filter(competition=opts["competition"].upper())
        if opts.get("min_date"):
            qs = qs.filter(match_date__gte=opts["min_date"])
        if opts.get("max_date"):
            qs = qs.filter(match_date__lte=opts["max_date"])

        rows = qs.values("competition", "predicted_home_goals", "predicted_away_goals",
                         "actual_home_goals", "actual_away_goals").iterator(chunk_size=2000)
        n = ph_sum = pa_sum = ah_sum = aa_sum = home_abs = away_abs = 0
        home_bias = away_bias = 0
        pred_results, actual_results = Counter(), Counter()
        predicted_scores, actual_scores = Counter(), Counter()
        draws_pred = draws_actual = draws_correct = 0
        comp = defaultdict(lambda: {"n": 0, "hb": 0, "ab": 0, "hae": 0, "aae": 0,
                                   "dp": 0, "da": 0, "dc": 0})
        for r in rows:
            ph, pa, ah, aa = (r["predicted_home_goals"], r["predicted_away_goals"],
                              r["actual_home_goals"], r["actual_away_goals"])
            n += 1
            ph_sum += ph; pa_sum += pa; ah_sum += ah; aa_sum += aa
            hb, ab = ph-ah, pa-aa
            home_bias += hb; away_bias += ab
            home_abs += abs(hb); away_abs += abs(ab)
            pr, ar = outcome(ph, pa), outcome(ah, aa)
            pred_results[pr] += 1; actual_results[ar] += 1
            predicted_scores[f"{ph}-{pa}"] += 1; actual_scores[f"{ah}-{aa}"] += 1
            is_pred_draw, is_actual_draw = pr == "Draw", ar == "Draw"
            draws_pred += int(is_pred_draw); draws_actual += int(is_actual_draw)
            draws_correct += int(is_pred_draw and is_actual_draw)
            c = comp[r["competition"]]
            c["n"] += 1; c["hb"] += hb; c["ab"] += ab
            c["hae"] += abs(hb); c["aae"] += abs(ab)
            c["dp"] += int(is_pred_draw); c["da"] += int(is_actual_draw)
            c["dc"] += int(is_pred_draw and is_actual_draw)

        comp_report = {}
        for code, c in sorted(comp.items()):
            k = c["n"]
            comp_report[code] = {"matches": k, "home_goal_bias": rate(c["hb"], k),
                "away_goal_bias": rate(c["ab"], k), "home_goals_mae": rate(c["hae"], k),
                "away_goals_mae": rate(c["aae"], k), "predicted_draws": c["dp"],
                "actual_draws": c["da"], "correct_draw_predictions": c["dc"],
                "draw_precision": rate(c["dc"], c["dp"]), "draw_recall": rate(c["dc"], c["da"])}
        report = {"matches": n, "mean_predicted_home_goals": rate(ph_sum, n),
            "mean_actual_home_goals": rate(ah_sum, n), "mean_predicted_away_goals": rate(pa_sum, n),
            "mean_actual_away_goals": rate(aa_sum, n), "home_goal_bias_pred_minus_actual": rate(home_bias, n),
            "away_goal_bias_pred_minus_actual": rate(away_bias, n),
            "combined_goal_bias_per_team_pred_minus_actual": rate(home_bias+away_bias, 2*n),
            "home_goals_mae": rate(home_abs, n), "away_goals_mae": rate(away_abs, n),
            "predicted_outcomes": dict(pred_results), "actual_outcomes": dict(actual_results),
            "draws": {"predicted": draws_pred, "actual": draws_actual, "correct": draws_correct,
                      "precision": rate(draws_correct, draws_pred), "recall": rate(draws_correct, draws_actual)},
            "predicted_scoreline_frequency": dict(predicted_scores.most_common()),
            "actual_scoreline_frequency": dict(actual_scores.most_common()),
            "by_competition": comp_report}
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        self.stdout.write("IX-TIPS SCORE BIAS DIAGNOSTIC")
        self.stdout.write("=" * 32)
        self.stdout.write(f"Matches: {n}")
        for key in ("mean_predicted_home_goals", "mean_actual_home_goals",
                    "mean_predicted_away_goals", "mean_actual_away_goals",
                    "home_goal_bias_pred_minus_actual", "away_goal_bias_pred_minus_actual",
                    "combined_goal_bias_per_team_pred_minus_actual", "home_goals_mae", "away_goals_mae"):
            self.stdout.write(f"{key}: {report[key]}")
        self.stdout.write(f"Predicted outcomes: {report['predicted_outcomes']}")
        self.stdout.write(f"Actual outcomes: {report['actual_outcomes']}")
        self.stdout.write(f"Draw diagnostics: {report['draws']}")
        self.stdout.write("BY COMPETITION")
        self.stdout.write(json.dumps(comp_report, indent=2))
        self.stdout.write("PREDICTED SCORELINES")
        self.stdout.write(json.dumps(report["predicted_scoreline_frequency"], indent=2))
        self.stdout.write("ACTUAL SCORELINES")
        self.stdout.write(json.dumps(report["actual_scoreline_frequency"], indent=2))
        self.stdout.write("Read-only: no prediction, cache or training data modified.")
