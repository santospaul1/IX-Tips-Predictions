from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import MatchPrediction


class CompetitionTeamDiscoveryTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="team-discovery-admin",
            password="test-password",
        )
        self.client.force_authenticate(user=self.user)
        self.url = reverse("api_competition_teams")

    def test_returns_teams_from_saved_predictions_even_without_standings(self):
        MatchPrediction.objects.create(
            match_id="team-discovery-1",
            competition="PL",
            match_date=date(2026, 11, 21),
            home_team="Arsenal",
            away_team="Chelsea",
        )
        MatchPrediction.objects.create(
            match_id="team-discovery-2",
            competition="PL",
            match_date=date(2026, 11, 21),
            home_team="Liverpool",
            away_team="Arsenal",
        )
        MatchPrediction.objects.create(
            match_id="team-discovery-3",
            competition="PD",
            match_date=date(2026, 11, 21),
            home_team="Barcelona",
            away_team="Real Madrid",
        )

        response = self.client.get(self.url, {"competition": "PL"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source"], "match_predictions")
        self.assertEqual(response.data["fixture_count"], 2)
        self.assertEqual(
            [team["name"] for team in response.data["teams"]],
            ["Arsenal", "Chelsea", "Liverpool"],
        )

    def test_can_limit_discovery_to_selected_fixture_date(self):
        MatchPrediction.objects.create(
            match_id="team-discovery-today",
            competition="PL",
            match_date=date(2026, 11, 20),
            home_team="Manchester City",
            away_team="Everton",
        )
        MatchPrediction.objects.create(
            match_id="team-discovery-tomorrow",
            competition="PL",
            match_date=date(2026, 11, 21),
            home_team="Arsenal",
            away_team="Chelsea",
        )

        response = self.client.get(
            self.url,
            {"competition": "PL", "date": "2026-11-21"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["fixture_count"], 1)
        self.assertEqual(
            [team["name"] for team in response.data["teams"]],
            ["Arsenal", "Chelsea"],
        )

    def test_rejects_unknown_competition(self):
        response = self.client.get(self.url, {"competition": "UNKNOWN"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["teams"], [])

    def test_rejects_invalid_date(self):
        response = self.client.get(
            self.url,
            {"competition": "PL", "date": "tomorrow"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["teams"], [])

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(self.url, {"competition": "PL"})

        # SessionAuthentication does not issue an HTTP authentication
        # challenge, so DRF correctly returns 403 for an anonymous request.
        self.assertEqual(response.status_code, 403)
