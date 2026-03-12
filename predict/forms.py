from django import forms
from datetime import date

from predict.constants import COMPETITIONS, COMPETITION_CHOICES

class PredictionForm(forms.Form):
    match_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        initial=date.today,
        label="Match Date"
    )
    competition = forms.ChoiceField(
        choices=COMPETITION_CHOICES,
        label="Competition"
    )

class LivePredictionForm(forms.Form):
    match_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    competition = forms.ChoiceField(choices=COMPETITION_CHOICES)

class ActualResultForm(forms.Form):
    match_date = forms.DateField(initial=date.today, widget=forms.DateInput(attrs={'type': 'date'}))
    competition = forms.ChoiceField(choices=[(k, v) for k, v in COMPETITIONS.items()])
