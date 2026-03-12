from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from . import views
from django.contrib.auth.views import LoginView, LogoutView

urlpatterns = [

    path('results/', views.results_view, name='results'),
    path("train-model/", views.train_model_view, name="train_model"),
    path('cached-models/', views.cached_models_status, name='cached_models'),
    path("suggest-date/", views.suggest_match_date, name="suggest_match_date"),
    path('trigger-task-now/', views.trigger_task_now, name='trigger_task_now'),
    path("refresh-cache/", views.refresh_cache_now, name="refresh_cache_now"),
    path("clear-cache/", views.clear_cache_now, name="clear_cache_now"),
    path("refresh-league-table/", views.refresh_league_table_cache, name="refresh_league_table"),
    path("ceologin/", LoginView.as_view(template_name="predict/login.html"), name="ceologin"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path("admin-dashboard/", views.admin_task_dashboard, name="admin-dashboard"),
    path("team_logos/", views.team_logos_preview, name="team_logos"),
    path('live_predictions/', views.live_predictions_by_date, name='live_predictions'),
    path("league-table/<str:competition_code>/", views.league_table_view, name="league_table"),
    path("refresh-league-table/", views.refresh_league_table_cache, name="refresh_league_table"),
    path("actual-results/", views.actual_results_view, name="actual_results"),
    path("ajax/league-table/", views.ajax_league_table, name="ajax_league_table"),
    path("top-picks/", views.top_picks_view, name="top_picks"),
    path("top-picks/export/<str:format>/", views.export_top_picks, name="export_top_picks"),
    path("api/predictions/", views.api_predictions, name="api_predictions"),
    path("api/top-picks/", views.api_top_picks, name="api_top_picks"),
    path("api/league-table/<str:competition_code>/", views.league_table_api, name="api_league_table"),
    #path("view_odds", views.view_odds, name="view_odds"),






] + static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])
