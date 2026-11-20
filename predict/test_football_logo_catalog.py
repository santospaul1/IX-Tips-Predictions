from unittest.mock import patch

from django.test import SimpleTestCase

from predict.constants import (
    get_football_competition_logo_url,
    FOOTBALL_COMPETITION_LOGO_SLUGS,
    get_football_team_logo_url,
    get_team_metadata,
)


class FootballLogoCatalogTests(SimpleTestCase):
    def test_resolves_team_logo_from_competition_country(self):
        url = get_football_team_logo_url("Liverpool FC", "PL")
        self.assertTrue(url.startswith("https://assets.football-logos.cc/logos/england/512x512/liverpool."))
        self.assertTrue(url.endswith(".png"))

    def test_resolves_competition_logo(self):
        url = get_football_competition_logo_url("PL")
        self.assertTrue(url.startswith("https://assets.football-logos.cc/logos/england/512x512/english-premier-league."))
        self.assertTrue(url.endswith(".png"))

    def test_every_configured_competition_has_a_catalog_logo(self):
        for competition_code in FOOTBALL_COMPETITION_LOGO_SLUGS:
            with self.subTest(competition_code=competition_code):
                self.assertIsNotNone(get_football_competition_logo_url(competition_code))

    def test_catalog_logo_replaces_stale_provider_crest(self):
        provider_crest = "https://provider.example/crest.png"
        with patch("predict.constants.cache.get", return_value={"shortName": "Liverpool", "crest": provider_crest}):
            with patch("predict.constants.cache.set"):
                metadata = get_team_metadata("Liverpool FC", "PL")
        self.assertTrue(metadata["crest"].startswith("https://assets.football-logos.cc/logos/england/512x512/liverpool."))
        self.assertEqual(metadata["logo_source"], "football-logos.cc")

    def test_resolves_common_provider_aliases(self):
        url = get_football_team_logo_url("Man City", "PL")
        self.assertTrue(url.startswith("https://assets.football-logos.cc/logos/england/512x512/manchester-city."))

    def test_resolves_provider_aliases_across_competitions(self):
        cases = [
            ("Athletic Club", "PD", "/spain/512x512/athletic-club."),
            ("Paris Saint-Germain", "FL1", "/france/512x512/paris-saint-germain."),
            ("Olympique Marseille", "FL1", "/france/512x512/marseille."),
            ("Olympique Lyon", "FL1", "/france/512x512/lyon."),
            ("Sporting Lisbon", "PPL", "/portugal/512x512/sporting-cp."),
            ("BVB", "BL1", "/germany/512x512/borussia-dortmund."),
            ("Al Hilal Saudi", "SAU", "/saudi-arabia/512x512/al-hilal."),
            ("Athletico PR", "BSA", "/brazil/512x512/athletico-paranaense."),
        ]
        for team_name, competition, expected_path in cases:
            with self.subTest(team_name=team_name):
                url = get_football_team_logo_url(team_name, competition)
                self.assertIsNotNone(url)
                self.assertIn(expected_path, url)

    def test_resolves_charleroi_alias_to_catalogue_crest(self):
        url = get_football_team_logo_url("Charleroi", "BEL")
        self.assertIsNotNone(url)
        self.assertIn("/belgium/512x512/charleroi.", url)

    def test_uses_catalog_team_logo_when_provider_has_no_crest(self):
        with patch("predict.constants.cache.get", return_value={"shortName": "Liverpool FC", "crest": None}):
            with patch("predict.constants.cache.set"):
                metadata = get_team_metadata("Liverpool FC", "PL")
        self.assertTrue(metadata["crest"].startswith("https://assets.football-logos.cc/logos/england/512x512/liverpool."))
        self.assertEqual(metadata["logo_source"], "football-logos.cc")

    def test_uses_country_flag_when_team_is_not_in_catalog(self):
        with patch("predict.constants.cache.get", return_value={"shortName": "Unknown FC", "crest": None}):
            with patch("predict.constants.cache.set"):
                metadata = get_team_metadata("Unknown FC", "TUR")
        self.assertEqual(metadata["crest"], "https://flagcdn.com/w160/tr.png")

    def test_cached_flag_does_not_hide_a_provider_crest(self):
        provider_crest = "https://provider.example/club.png"
        cached = {
            "shortName": "Unknown FC",
            "crest": "https://flagcdn.com/w160/tr.png",
            "provider_crest": provider_crest,
            "logo_source": "competition-country-flag",
        }
        with patch("predict.constants.cache.get", return_value=cached):
            with patch("predict.constants.get_football_team_logo_url", return_value=None):
                with patch("predict.constants.cache.set"):
                    metadata = get_team_metadata("Unknown FC", "TUR")
        self.assertEqual(metadata["crest"], provider_crest)
        self.assertEqual(metadata["logo_source"], "provider")

    def test_catalogue_is_checked_even_when_a_flag_is_cached(self):
        catalogue_logo = "https://assets.football-logos.cc/logos/turkey/512x512/club.hash.png"
        cached = {
            "shortName": "Known Club",
            "crest": "https://flagcdn.com/w160/tr.png",
            "logo_source": "competition-country-flag",
        }
        with patch("predict.constants.cache.get", return_value=cached):
            with patch("predict.constants.get_football_team_logo_url", return_value=catalogue_logo):
                with patch("predict.constants.cache.set"):
                    metadata = get_team_metadata("Known Club", "TUR")
        self.assertEqual(metadata["crest"], catalogue_logo)
        self.assertEqual(metadata["logo_source"], "football-logos.cc")

    def test_ambiguous_global_team_name_is_not_guessed(self):
        self.assertIsNone(get_football_team_logo_url("Atlas", "CL"))

    def test_ambiguous_same_country_team_name_is_not_guessed(self):
        self.assertIsNone(get_football_team_logo_url("Racing", "ARG"))
