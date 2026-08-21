from django import forms

from .billing_plans import PLAN_CHOICES, PRO, checkout_tiers


class BillingPlanSelectionForm(forms.Form):
    plan = forms.ChoiceField(choices=PLAN_CHOICES)

    def __init__(self, *args, founder=False, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = set(checkout_tiers(founder=founder))
        self.fields['plan'].choices = [
            choice for choice in PLAN_CHOICES if choice[0] in allowed
        ]
        self.fields['plan'].initial = PRO


class FounderCodeRedemptionForm(forms.Form):
    founder_code = forms.CharField(
        max_length=100,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text='Codes are single-use and are never displayed again after generation.',
    )
