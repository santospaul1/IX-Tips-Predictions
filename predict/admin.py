from django.contrib import admin
from .models import TopPick


# Register your models here.
from django.contrib import admin
from django.contrib import messages
from .models import TopPick

@admin.action(description="Clear all Top Picks")
def clear_all_top_picks(modeladmin, request, queryset):
    TopPick.objects.all().delete()
    messages.success(request, "✅ All TopPick entries have been deleted.")

@admin.register(TopPick)
class TopPickAdmin(admin.ModelAdmin):
    list_display = ('match_date', 'home_team', 'away_team', 'tip', 'confidence', 'actual_tip', 'is_correct')
    actions = [clear_all_top_picks]
