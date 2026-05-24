from django.db import models

# models.py

class MatchPrediction(models.Model):
    match_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    home_team = models.CharField(max_length=100)
    away_team = models.CharField(max_length=100)
    match_date = models.DateField()
    competition = models.CharField(max_length=20)

    predicted_home_goals = models.IntegerField(null=True, blank=True)
    predicted_away_goals = models.IntegerField(null=True, blank=True)
    predicted_result = models.CharField(max_length=10, null=True, blank=True)  # Home / Away / Draw

    # ✅ Betting markets
    market_over_1_5 = models.BooleanField(default=False)
    market_over_2_5 = models.BooleanField(default=False)
    market_under_1_5 = models.BooleanField(default=False)
    market_under_2_5 = models.BooleanField(default=False)
    market_gg = models.BooleanField(default=False)
    market_nogg = models.BooleanField(default=False)

    # ✅ Odds (if later connected to odds API)
    
    odds_gg = models.FloatField(null=True, blank=True)  # Add this
    odds_over_25 = models.FloatField(null=True, blank=True)
    odds_home = models.FloatField(null=True, blank=True)
    odds_draw = models.FloatField(null=True, blank=True)
    odds_away = models.FloatField(null=True, blank=True)

    # ✅ Actual match outcome
    actual_home_goals = models.IntegerField(null=True, blank=True)
    actual_away_goals = models.IntegerField(null=True, blank=True)
    actual_ht_home_goals = models.IntegerField(null=True, blank=True)
    actual_ht_away_goals = models.IntegerField(null=True, blank=True)
    is_accurate = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default="TIMED")

    def __str__(self):
        return f"{self.home_team} vs {self.away_team} ({self.match_date})"

class TopPick(models.Model):
    VARIANT_CHOICES = (
        ("1", "Sure 1"),
        ("2", "Sure 2"),
        ("3", "Running Bet"),
        ("4", "Mshipi"),
    )

    match_date = models.DateField()
    home_team = models.CharField(max_length=100)
    away_team = models.CharField(max_length=100)
    variant = models.CharField(max_length=1, choices=VARIANT_CHOICES, default="1")
    tip = models.CharField(max_length=50)  # e.g. '1', '2', 'X', 'Over 2.5', 'GG'
    confidence = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)
    actual_tip = models.CharField(max_length=50, blank=True, null=True)  # new
    is_correct = models.BooleanField(null=True,blank=True) 
    odds = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = ('match_date', 'home_team', 'away_team', 'variant')
        ordering = ['match_date']

    def __str__(self):
        return f"{self.match_date} | {self.variant} | {self.home_team} vs {self.away_team} - {self.tip} ({self.confidence}%)"
    
class MatchOdds(models.Model):
    match = models.OneToOneField("MatchPrediction", on_delete=models.CASCADE, related_name="odds")
    home_win = models.FloatField(null=True, blank=True)
    draw = models.FloatField(null=True, blank=True)
    away_win = models.FloatField(null=True, blank=True)
    over_2_5 = models.FloatField(null=True, blank=True)
    under_2_5 = models.FloatField(null=True, blank=True)
    btts_yes = models.FloatField(null=True, blank=True)
    btts_no = models.FloatField(null=True, blank=True)
    bookmaker = models.CharField(max_length=100, null=True, blank=True)
    market_sources = models.JSONField(default=dict, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.market_sources is None:
            self.market_sources = {}
        super().save(*args, **kwargs)

    def best_market(self):
        return {
            "1": self.home_win,
            "X": self.draw,
            "2": self.away_win,
            "O2.5": self.over_2_5,
            "U2.5": self.under_2_5,
            "GG": self.btts_yes,
            "NG": self.btts_no,
        }


class ComboSlip(models.Model):
    STYLE_CHOICES = (
        ("safe", "Safe"),
        ("value", "Value"),
    )

    name = models.CharField(max_length=120)
    size = models.PositiveIntegerField(default=5)
    market_filter = models.CharField(max_length=50, blank=True, default="")
    style = models.CharField(max_length=10, choices=STYLE_CHOICES, default="safe")
    combined_odds = models.FloatField(null=True, blank=True)
    average_confidence = models.FloatField(default=0)
    priced_legs = models.PositiveIntegerField(default=0)
    auto_generated = models.BooleanField(default=False)
    signature = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ComboSlipLeg(models.Model):
    slip = models.ForeignKey(ComboSlip, on_delete=models.CASCADE, related_name="legs")
    match_date = models.DateField()
    competition = models.CharField(max_length=20)
    home_team = models.CharField(max_length=100)
    away_team = models.CharField(max_length=100)
    tip = models.CharField(max_length=50)
    confidence = models.FloatField(default=0)
    odds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["match_date", "home_team"]

    def __str__(self):
        return f"{self.home_team} vs {self.away_team} - {self.tip}"
