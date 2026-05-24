from django.urls import path
from . import api_views

urlpatterns = [
    # Auth
    path("auth/token/", api_views.ApiTokenObtainView.as_view(), name="api_token_obtain"),
    path("auth/token/refresh/", api_views.ApiTokenRefreshView.as_view(), name="api_token_refresh"),

    # Meta
    path("competitions/", api_views.api_competitions, name="api_competitions"),
    path("summary/", api_views.api_summary, name="api_summary"),

    # Predictions
    path("predictions/", api_views.api_predictions_v1, name="api_predictions_v1"),
    path("top-picks/", api_views.api_top_picks_v1, name="api_top_picks_v1"),
    path("correct-score/", api_views.api_correct_score, name="api_correct_score"),
    path("anytime-scorer/", api_views.api_anytime_scorer, name="api_anytime_scorer"),
    path("market-picks/", api_views.api_market_picks, name="api_market_picks"),
    path("combo/", api_views.api_combo_slips, name="api_combo_slips"),
    path("won-slips/", api_views.api_won_slips, name="api_won_slips"),

    # League table
    path("league-table/", api_views.api_league_table_v1, name="api_league_table_v1"),
]
