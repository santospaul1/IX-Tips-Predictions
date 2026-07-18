"""
IX-Tips REST API — v1
All endpoints consumed by the Flutter mobile app.
"""
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .constants import COMPETITIONS, country_flag_url
from .models import MatchPrediction, TopPick, ComboSlip
from .views import (
    build_correct_score_rows,
    build_anytime_scorer_rows,
    get_league_table,
    _resolve_prediction_date,
    get_team_metadata,
    normalize_display_team_name,
    normalize_display_competition_name,
    get_cached_kickoff_time,
)
from .utils import (
    scoreline_predictions,
    get_team_recent_form,
)

competitions = COMPETITIONS

# ── helpers ──────────────────────────────────────────────────────────────────

def _prediction_to_dict(p, team_meta_cache=None, form_cache=None, kickoff_cache=None):
    """Serialize a MatchPrediction. Pass pre-fetched dicts to avoid N cache reads."""
    if team_meta_cache is not None:
        meta_home = team_meta_cache.get(p.home_team) or get_team_metadata(p.home_team)
        meta_away = team_meta_cache.get(p.away_team) or get_team_metadata(p.away_team)
    else:
        meta_home = get_team_metadata(p.home_team)
        meta_away = get_team_metadata(p.away_team)
    winner = None
    if p.predicted_home_goals is not None and p.predicted_away_goals is not None:
        if p.predicted_home_goals > p.predicted_away_goals:
            winner = "H"
        elif p.predicted_away_goals > p.predicted_home_goals:
            winner = "A"
        else:
            winner = "D"

    actual_result = None
    actual_winner = None
    if p.actual_home_goals is not None and p.actual_away_goals is not None:
        actual_result = f"{p.actual_home_goals}-{p.actual_away_goals}"
        if p.actual_home_goals > p.actual_away_goals:
            actual_winner = "H"
        elif p.actual_away_goals > p.actual_home_goals:
            actual_winner = "A"
        else:
            actual_winner = "D"

    odds_obj = getattr(p, "odds", None)
    display_odds = None
    if winner and odds_obj:
        odds_map = {"H": odds_obj.home_win, "D": odds_obj.draw, "A": odds_obj.away_win}
        display_odds = odds_map.get(winner)

    return {
        "id": p.id,
        "competition": normalize_display_competition_name(
            competitions.get(p.competition, p.competition), code=p.competition
        ),
        "competition_code": p.competition,
        "competition_logo": f"logos/{p.competition}.png",
        "match_date": str(p.match_date),
        "match_time": (
            (kickoff_cache or {}).get((p.competition, str(p.match_date), p.home_team, p.away_team))
            or get_cached_kickoff_time(p.competition, p.match_date, p.home_team, p.away_team)
        ),
        "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=p.home_team),
        "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=p.away_team),
        "home_logo": meta_home.get("crest") or country_flag_url(p.competition),
        "away_logo": meta_away.get("crest") or country_flag_url(p.competition),
        "home_form": (form_cache or {}).get((p.home_team, p.competition), []),
        "away_form": (form_cache or {}).get((p.away_team, p.competition), []),
        "predicted_home_goals": p.predicted_home_goals,
        "predicted_away_goals": p.predicted_away_goals,
        # Raw rate parameters (Poisson λ) — absent for old pre-migration predictions
        "predicted_home_rate": getattr(p, "predicted_home_rate", None),
        "predicted_away_rate": getattr(p, "predicted_away_rate", None),
        "winner": winner,
        "display_odds": display_odds,
        "status": p.status,
        "actual_result": actual_result,
        "actual_winner": actual_winner,
        "winner_correct": winner is not None and actual_winner is not None and winner == actual_winner,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

class ApiTokenObtainView(TokenObtainPairView):
    """POST /api/v1/auth/token/ — username + password → access + refresh tokens."""
    permission_classes = [AllowAny]


class ApiTokenRefreshView(TokenRefreshView):
    """POST /api/v1/auth/token/refresh/ — refresh → new access token."""
    permission_classes = [AllowAny]


# ── Competitions ──────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_competitions(request):
    """GET /api/v1/competitions/ — list of supported competitions."""
    data = [
        {"code": code, "name": name, "logo": f"logos/{code}.png"}
        for code, name in competitions.items()
    ]
    return Response({"competitions": data})


# ── Predictions ───────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_predictions_v1(request):
    """
    GET /api/v1/predictions/
    Params: date (YYYY-MM-DD), competition (code), page, page_size
    """
    date_str = request.GET.get("date") or timezone.localdate().isoformat()
    competition = request.GET.get("competition")
    page = max(1, int(request.GET.get("page", 1)))
    page_size = min(50, max(5, int(request.GET.get("page_size", 20))))

    qs = (MatchPrediction.objects
          .filter(match_date=date_str)
          .select_related("odds")       # avoids N queries inside _prediction_to_dict
          .order_by("competition", "home_team"))
    if competition:
        qs = qs.filter(competition=competition)

    total = qs.count()
    start = (page - 1) * page_size
    predictions = list(qs[start : start + page_size])

    # Batch-resolve all per-row lookups — O(unique_teams) instead of O(2N).
    team_names = {p.home_team for p in predictions} | {p.away_team for p in predictions}
    team_meta = {name: get_team_metadata(name) for name in team_names}

    form_pairs = {(p.home_team, p.competition) for p in predictions} | \
                 {(p.away_team, p.competition) for p in predictions}
    form_cache = {(t, c): get_team_recent_form(t, c) for t, c in form_pairs}

    kickoff_cache = {
        (p.competition, str(p.match_date), p.home_team, p.away_team):
            get_cached_kickoff_time(p.competition, p.match_date, p.home_team, p.away_team)
        for p in predictions
    }

    return Response({
        "date": date_str,
        "total": total,
        "page": page,
        "page_size": page_size,
        "predictions": [_prediction_to_dict(p, team_meta_cache=team_meta,
                                            form_cache=form_cache,
                                            kickoff_cache=kickoff_cache)
                        for p in predictions],
    })


# ── Top Picks ─────────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_top_picks_v1(request):
    """
    GET /api/v1/top-picks/
    Params: date (YYYY-MM-DD), variant (1-4)
    """
    date_str = request.GET.get("date") or timezone.localdate().isoformat()
    variant = request.GET.get("variant", "1")

    picks_qs = TopPick.objects.filter(match_date=date_str, variant=variant).order_by("-confidence")
    picks = list(picks_qs)

    # Batch-resolve: one query for scores, one pass for team metadata.
    match_scores = {}
    for mp in MatchPrediction.objects.filter(match_date=date_str).values(
        "home_team", "away_team", "actual_home_goals", "actual_away_goals"
    ):
        if mp["actual_home_goals"] is not None and mp["actual_away_goals"] is not None:
            match_scores[(mp["home_team"], mp["away_team"])] = (
                f"{mp['actual_home_goals']}-{mp['actual_away_goals']}"
            )

    team_names = {p.home_team for p in picks} | {p.away_team for p in picks}
    team_meta = {name: get_team_metadata(name) for name in team_names}

    data = []
    for p in picks:
        meta_home = team_meta.get(p.home_team, {"shortName": p.home_team})
        meta_away = team_meta.get(p.away_team, {"shortName": p.away_team})
        actual_score = match_scores.get((p.home_team, p.away_team))
        data.append({
            "match_date": str(p.match_date),
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=p.home_team),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=p.away_team),
            "home_logo": meta_home.get("crest") or country_flag_url(p.competition),
            "away_logo": meta_away.get("crest") or country_flag_url(p.competition),
            "tip": p.tip,
            "confidence": p.confidence,
            "odds": p.odds,
            "is_correct": p.is_correct,
            "actual_tip": p.actual_tip,
            "actual_score": actual_score,
            "variant": p.variant,
            "variant_label": dict(TopPick.VARIANT_CHOICES).get(p.variant, p.variant),
        })

    # slip-level stats
    total = len(data)
    settled = [d for d in data if d["is_correct"] is not None]
    won = [d for d in settled if d["is_correct"]]

    return Response({
        "date": date_str,
        "variant": variant,
        "picks": data,
        "stats": {
            "total": total,
            "settled": len(settled),
            "won": len(won),
            "accuracy": round(len(won) / len(settled) * 100, 1) if settled else None,
        },
    })


# ── Correct Score ─────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_correct_score(request):
    """
    GET /api/v1/correct-score/
    Params: date (YYYY-MM-DD)
    """
    date_str = request.GET.get("date")
    selected_date, rows, stats = build_correct_score_rows(date_str)
    return Response({
        "date": selected_date,
        "predictions": rows,
        "stats": stats,
    })


# ── Anytime Scorer ────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_anytime_scorer(request):
    """
    GET /api/v1/anytime-scorer/
    Params: date (YYYY-MM-DD)
    """
    date_str = request.GET.get("date")
    selected_date, rows = build_anytime_scorer_rows(date_str)
    return Response({
        "date": selected_date,
        "matches": rows,
    })


# ── Market Picks ──────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_market_picks(request):
    """
    GET /api/v1/market-picks/
    Params: group (goals|cards|corners|combo), market, scope (today|tomorrow|weekend|all),
            sort (confidence|odds), limit (20|50|all), priced_only (true/false)
    """
    from .views import build_market_pick_rows
    group = request.GET.get("group")
    market = request.GET.get("market")
    scope = request.GET.get("scope", "today")
    sort = request.GET.get("sort", "confidence")
    limit = request.GET.get("limit", "20")
    priced_only = request.GET.get("priced_only", "false").lower() == "true"

    result = build_market_pick_rows(
        group_key=group,
        market_name=market,
        scope_key=scope,
        sort_key=sort,
        limit_key=limit,
        priced_only=priced_only,
    )
    # result is (selected_group, selected_market, selected_scope, selected_sort,
    #            selected_limit, total_count, market_groups, group_markets,
    #            market_scopes, market_sort_options, rows, category_summary)
    selected_group, selected_market, selected_scope, selected_sort, selected_limit, total_count, market_groups, group_markets, market_scopes, market_sort_options, rows, category_summary = result
    # make match_date serialisable
    for row in rows:
        if hasattr(row.get("match_date"), "isoformat"):
            row["match_date"] = row["match_date"].isoformat()
    return Response({
        "selected_group": selected_group,
        "selected_market": selected_market,
        "selected_scope": selected_scope,
        "total": total_count,
        "market_groups": market_groups,
        "group_markets": group_markets,
        "picks": rows,
        "category_summary": category_summary,
    })


# ── Combo Slips ───────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_combo_slips(request):
    """
    GET /api/v1/combo/
    Params: date (YYYY-MM-DD)
    """
    date_str = request.GET.get("date") or timezone.localdate().isoformat()
    slips = ComboSlip.objects.filter(legs__match_date=date_str).distinct().order_by("-created_at")[:20]

    data = []
    for slip in slips:
        legs = []
        for leg in slip.legs.all().order_by("match_date", "home_team"):
            meta_home = get_team_metadata(leg.home_team)
            meta_away = get_team_metadata(leg.away_team)
            legs.append({
                "match_date": str(leg.match_date),
                "competition": normalize_display_competition_name(
                    competitions.get(leg.competition, leg.competition), code=leg.competition
                ),
                "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=leg.home_team),
                "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=leg.away_team),
                "home_logo": meta_home.get("crest") or country_flag_url(leg.competition),
                "away_logo": meta_away.get("crest") or country_flag_url(leg.competition),
                "tip": leg.tip,
                "confidence": leg.confidence,
                "odds": leg.odds,
            })
        data.append({
            "id": slip.id,
            "name": slip.name,
            "size": slip.size,
            "style": slip.style,
            "combined_odds": slip.combined_odds,
            "average_confidence": slip.average_confidence,
            "created_at": slip.created_at.isoformat(),
            "legs": legs,
        })

    return Response({"date": date_str, "slips": data})


# ── Won Slips ─────────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_won_slips(request):
    """
    GET /api/v1/won-slips/
    Params: date (YYYY-MM-DD), variant (1-4)
    """
    date_str = request.GET.get("date") or timezone.localdate().isoformat()
    variant = request.GET.get("variant")

    qs = TopPick.objects.filter(match_date=date_str, is_correct=True).order_by("-confidence")
    if variant:
        qs = qs.filter(variant=variant)

    data = []
    for p in qs:
        meta_home = get_team_metadata(p.home_team)
        meta_away = get_team_metadata(p.away_team)
        data.append({
            "match_date": str(p.match_date),
            "home_team": normalize_display_team_name(meta_home.get("shortName"), fallback=p.home_team),
            "away_team": normalize_display_team_name(meta_away.get("shortName"), fallback=p.away_team),
            "home_logo": meta_home.get("crest") or country_flag_url(p.competition),
            "away_logo": meta_away.get("crest") or country_flag_url(p.competition),
            "tip": p.tip,
            "actual_tip": p.actual_tip,
            "confidence": p.confidence,
            "odds": p.odds,
            "variant": p.variant,
            "variant_label": dict(TopPick.VARIANT_CHOICES).get(p.variant, p.variant),
        })

    return Response({"date": date_str, "won_picks": data, "total": len(data)})


# ── League Table ──────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_league_table_v1(request):
    """
    GET /api/v1/league-table/
    Params: competition (code, default PL)
    """
    competition_code = request.GET.get("competition", "PL")
    table = get_league_table(competition_code)
    return Response({
        "competition": competition_code,
        "competition_name": competitions.get(competition_code, competition_code),
        "table": table,
    })


# ── Summary (home screen) ─────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([AllowAny])
def api_summary(request):
    """
    GET /api/v1/summary/
    Returns counts for today to populate the home screen dashboard.
    Cached for 5 min — the live-refresh cron runs on the same interval.
    """
    from django.core.cache import cache

    today = timezone.localdate().isoformat()
    ck = f"summary_v1::{today}"
    cached = cache.get(ck)
    if cached is not None:
        return Response(cached)

    # Single aggregation per table instead of 4 separate COUNT queries.
    tp_qs = TopPick.objects.filter(match_date=today)
    top_picks_count = tp_qs.count()
    settled_today = tp_qs.filter(is_correct__isnull=False).count()
    won_today = tp_qs.filter(is_correct=True).count()

    predictions_count = MatchPrediction.objects.filter(match_date=today).count()

    data = {
        "date": today,
        "predictions": predictions_count,
        "top_picks": top_picks_count,
        "won_today": won_today,
        "settled_today": settled_today,
        "accuracy_today": round(won_today / settled_today * 100, 1) if settled_today else None,
    }
    cache.set(ck, data, timeout=300)
    return Response(data)
