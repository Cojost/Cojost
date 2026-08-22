from django import forms

from .billing_plans import (
    BILLING_INTERVAL_CHOICES,
    MONTH,
    PLAN_CHOICES,
    PRO,
    checkout_intervals,
    checkout_tiers,
)


class BillingPlanSelectionForm(forms.Form):
    tier = forms.ChoiceField(choices=PLAN_CHOICES)
    billing_interval = forms.ChoiceField(choices=BILLING_INTERVAL_CHOICES)

    def __init__(self, *args, founder=False, **kwargs):
        super().__init__(*args, **kwargs)
        allowed_tiers = set(checkout_tiers(founder=founder))
        allowed_intervals = set(checkout_intervals())
        self.fields['tier'].choices = [
            choice for choice in PLAN_CHOICES if choice[0] in allowed_tiers
        ]
        self.fields['billing_interval'].choices = [
            choice
            for choice in BILLING_INTERVAL_CHOICES
            if choice[0] in allowed_intervals
        ]
        self.fields['tier'].initial = PRO
        self.fields['billing_interval'].initial = MONTH

    def clean(self):
        cleaned_data = super().clean()
        if not self.is_bound:
            return cleaned_data
        allowed_keys = {'csrfmiddlewaretoken', 'tier', 'billing_interval'}
        if set(self.data.keys()) - allowed_keys:
            raise forms.ValidationError('Unsupported billing selection.')
        for field_name in ('tier', 'billing_interval'):
            if hasattr(self.data, 'getlist'):
                values = self.data.getlist(field_name)
            else:
                value = self.data.get(field_name)
                values = value if isinstance(value, (list, tuple)) else [value]
            if len(values) != 1:
                raise forms.ValidationError('Choose one billing option.')
        return cleaned_data


class FounderCodeRedemptionForm(forms.Form):
    founder_code = forms.CharField(
        max_length=100,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text='Codes are single-use and are never displayed again after generation.',
    )
