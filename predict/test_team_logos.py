from unittest import TestCase
from unittest.mock import patch

from predict.constants import _normalize_football_logo_name


class FootballLogoNameNormalizationTests(TestCase):
    def test_strips_common_provider_club_suffixes(self):
        self.assertEqual(_normalize_football_logo_name("Kaizer Chiefs FC"), "kaizer chiefs")
        self.assertEqual(_normalize_football_logo_name("Mamelodi Sundowns SC"), "mamelodi sundowns")
        self.assertEqual(_normalize_football_logo_name("AFC Leopards"), "leopards")

    def test_preserves_meaningful_name_tokens(self):
        self.assertEqual(_normalize_football_logo_name("Manchester United"), "manchester united")
        self.assertEqual(_normalize_football_logo_name("Young Africans SC"), "young africans")


class ApiTeamLogoFallbackTests(TestCase):
    def test_prefers_catalogue_badge(self):
        from predict.api_views import _team_logo

        with patch("predict.api_views.get_football_team_logo_url", return_value="catalog.png"):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(
                    _team_logo("Arsenal", "PL", {"crest": "provider.png"}),
                    "catalog.png",
                )

    def test_uses_catalogue_before_competition_flag(self):
        from predict.api_views import _team_logo

        with patch("predict.api_views.get_football_team_logo_url", return_value="catalog.png"):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(_team_logo("Arsenal", "PL", {}), "catalog.png")

    def test_uses_provider_crest_when_catalogue_has_no_match(self):
        from predict.api_views import _team_logo

        with patch("predict.api_views.get_football_team_logo_url", return_value=None):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(_team_logo("Arsenal", "PL", {"crest": "provider.png"}), "provider.png")

    def test_uses_flag_when_no_club_badge_is_available(self):
        from predict.api_views import _team_logo

        with patch("predict.api_views.get_football_team_logo_url", return_value=None):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(_team_logo("Unknown Club", "PL", {}), "flag.png")

    def test_provider_crest_is_used_before_a_cached_country_flag(self):
        from predict.api_views import _team_logo

        metadata = {
            "crest": "https://flagcdn.com/w160/gb-eng.png",
            "provider_crest": "https://provider.example/club.png",
        }
        with patch("predict.api_views.get_football_team_logo_url", return_value=None):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(
                    _team_logo("Unknown Club", "PL", metadata),
                    "https://provider.example/club.png",
                )

    def test_cached_country_flag_is_only_used_as_last_resort(self):
        from predict.api_views import _team_logo

        metadata = {"crest": "https://flagcdn.com/w160/gb-eng.png"}
        with patch("predict.api_views.get_football_team_logo_url", return_value=None):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(_team_logo("Unknown Club", "PL", metadata), "flag.png")

    def test_tries_provider_short_name_before_using_provider_crest(self):
        from predict.api_views import _team_logo

        with patch(
            "predict.api_views.get_football_team_logo_url",
            side_effect=[None, "short-name-catalog.png"],
        ):
            with patch("predict.api_views.country_flag_url", return_value="flag.png"):
                self.assertEqual(
                    _team_logo("Arsenal Football Club", "PL", {"shortName": "Arsenal", "crest": "provider.png"}),
                    "short-name-catalog.png",
                )

class CachedMetadataLogoFallbackTests(TestCase):
    def test_resolved_catalogue_logo_is_cached_for_subsequent_page_requests(self):
        from predict.constants import TEAM_METADATA_CACHE_TIMEOUT, get_team_metadata

        cached_meta = {"shortName": "Arsenal", "crest": "", "competition": "PL"}
        with patch("predict.constants.cache.get", return_value=cached_meta):
            with patch("predict.constants.get_football_team_logo_url", return_value="catalogue-arsenal.png"):
                with patch("predict.constants.cache.set") as cache_set:
                    metadata = get_team_metadata("Arsenal", "PL")

        self.assertEqual(metadata["crest"], "catalogue-arsenal.png")
        cache_set.assert_called_once_with(
            "team_meta::Arsenal",
            metadata,
            timeout=TEAM_METADATA_CACHE_TIMEOUT,
        )


class TeamLogoDashboardTests(TestCase):
    def test_competition_logo_ignores_stale_256_catalogue_cache(self):
        from django.test import override_settings
        from predict.views import resolve_competition_logo

        stale = "https://assets.football-logos.cc/logos/belgium/256x256/jupiler-pro-league.hash.png"
        current = "https://assets.football-logos.cc/logos/belgium/512x512/jupiler-pro-league.hash.png"
        with patch("predict.views.os.path.exists", return_value=False):
            with patch("predict.views.cache.get", return_value={"crest": stale}):
                with patch("predict.views.get_football_competition_logo_url", return_value=current):
                    self.assertEqual(resolve_competition_logo("BEL"), current)


    def test_displays_standings_teams_and_resolved_logo_metadata(self):
        from django.test import RequestFactory
        from predict.views import team_logos_preview

        request = RequestFactory().get("/team_logos/")
        standings = [{"team": {"name": "Arsenal", "shortName": "ARS", "crest": "provider-crest.png"}}]
        with patch("predict.views.competitions", {"PL": "Premier League"}):
            with patch("predict.views.get_league_table", return_value=standings):
                with patch("predict.views.get_team_metadata", return_value={
                    "shortName": "ARS", "crest": "catalog-crest.png", "logo_source": "catalogue"
                }):
                    with patch("predict.views.resolve_competition_logo", return_value="league-logo.png"):
                        response = team_logos_preview(request)

        self.assertEqual(response.status_code, 200)
        rendered = response.content.decode("utf-8")
        self.assertIn("Arsenal", rendered)
        self.assertIn("catalog-crest.png", rendered)
        self.assertIn("provider-crest.png", rendered)
        self.assertIn("Source: catalogue", rendered)
        self.assertIn('data-provider-crest="provider-crest.png"', rendered)
        self.assertIn("image fallback", rendered)


    def test_selected_league_loads_only_that_competition(self):
        from django.test import RequestFactory
        from predict.views import team_logos_preview

        request = RequestFactory().get("/team_logos/?league=PL")
        standings = [{"team": {"name": "Arsenal", "shortName": "ARS", "crest": "provider-crest.png"}}]
        with patch("predict.views.competitions", {"PL": "Premier League", "SA": "Serie A"}):
            with patch("predict.views.get_league_table", return_value=standings) as table:
                with patch("predict.views.get_team_metadata", return_value={
                    "shortName": "ARS", "crest": "catalog-crest.png", "logo_source": "catalogue"
                }):
                    with patch("predict.views.resolve_competition_logo", return_value="league-logo.png"):
                        response = team_logos_preview(request)

        table.assert_called_once_with("PL")
        self.assertEqual(response.status_code, 200)
        rendered = response.content.decode("utf-8")
        self.assertIn("Premier League", rendered)
        self.assertIn("Arsenal", rendered)
        self.assertEqual(rendered.count("Serie A"), 1)  # Only the dropdown option; no Serie A standings are rendered.
