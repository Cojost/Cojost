from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models.pay_plans import PayPlanEligibility


def coerce_optional_boolean(value):
    return {'true': True, 'false': False, '': None}[value]


class PayPlanEligibilityForm(forms.ModelForm):
    BOOLEAN_CHOICES = (
        ('', 'Pending or unknown'),
        ('true', 'Yes'),
        ('false', 'No'),
    )
    month_start = forms.DateField(
        input_formats=['%Y-%m'],
        widget=forms.DateInput(format='%Y-%m', attrs={'type': 'month'}),
        label='Eligibility month',
    )

    green_pea = forms.TypedChoiceField(
        choices=BOOLEAN_CHOICES, coerce=coerce_optional_boolean,
        empty_value=None, required=False, label='Green Pea program applies',
    )
    training_requirements_met = forms.TypedChoiceField(
        choices=BOOLEAN_CHOICES, coerce=coerce_optional_boolean,
        empty_value=None, required=False, label='Training requirements met',
    )
    ar_requirement_met = forms.TypedChoiceField(
        choices=BOOLEAN_CHOICES, coerce=coerce_optional_boolean,
        empty_value=None, required=False, label='AR requirement met',
    )
    call_requirement_met = forms.TypedChoiceField(
        choices=BOOLEAN_CHOICES, coerce=coerce_optional_boolean,
        empty_value=None, required=False, label='Call requirement met',
    )
    video_requirement_met = forms.TypedChoiceField(
        choices=BOOLEAN_CHOICES, coerce=coerce_optional_boolean,
        empty_value=None, required=False, label='Co-Video requirement met',
    )

    class Meta:
        model = PayPlanEligibility
        fields = [
            'month_start', 'green_pea', 'nps_status', 'ar_requirement_met',
            'training_requirements_met', 'call_requirement_met',
            'video_requirement_met', 'nps_qualifying_surveys',
            'nps_low_score_surveys', 'holiday_bonus_eligible',
            'holiday_bonus_forfeited', 'notes',
        ]

    def __init__(
        self,
        *args,
        enabled_requirements=None,
        read_only=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        enabled = set(enabled_requirements or [])
        field_requirements = {
            'green_pea': 'green_pea',
            'nps_status': 'nps',
            'ar_requirement_met': 'ar',
            'training_requirements_met': 'training',
            'call_requirement_met': 'calls',
            'video_requirement_met': 'video',
            'nps_qualifying_surveys': 'nps_bonus',
            'nps_low_score_surveys': 'nps_bonus',
            'holiday_bonus_eligible': 'holiday',
            'holiday_bonus_forfeited': 'holiday',
        }
        for field_name, requirement in field_requirements.items():
            if (
                field_name == 'nps_status'
                and enabled & {'nps', 'nps_bonus'}
            ):
                continue
            if requirement not in enabled:
                self.fields.pop(field_name, None)
        if read_only:
            for field in self.fields.values():
                field.disabled = True

    def clean_month_start(self):
        month_start = self.cleaned_data['month_start'].replace(day=1)
        if month_start != timezone.localdate().replace(day=1):
            raise ValidationError(
                'Only the current month can be updated. Past eligibility is kept as history.'
            )
        return month_start


class DashboardNPSBonusForm(forms.ModelForm):
    nps_status = forms.ChoiceField(
        label='Current NPS status',
        choices=PayPlanEligibility.NPS_CHOICES,
        widget=forms.RadioSelect,
    )
    nps_qualifying_surveys = forms.IntegerField(
        label='Qualifying surveys',
        min_value=0,
        widget=forms.NumberInput(attrs={
            'min': '0', 'step': '1', 'inputmode': 'numeric',
        }),
    )
    nps_low_score_surveys = forms.IntegerField(
        label='Low-score surveys',
        min_value=0,
        widget=forms.NumberInput(attrs={
            'min': '0', 'step': '1', 'inputmode': 'numeric',
        }),
    )

    class Meta:
        model = PayPlanEligibility
        fields = [
            'nps_status',
            'nps_qualifying_surveys',
            'nps_low_score_surveys',
        ]
