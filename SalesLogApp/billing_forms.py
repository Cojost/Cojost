from django import forms


class FounderCodeRedemptionForm(forms.Form):
    founder_code = forms.CharField(
        max_length=100,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text='Codes are single-use and are never displayed again after generation.',
    )
