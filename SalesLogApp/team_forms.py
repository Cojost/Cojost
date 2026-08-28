from zoneinfo import available_timezones

from django import forms

from .models import Team, TeamComment, TeamReaction


TIMEZONE_CHOICES = [(name, name) for name in sorted(available_timezones())]


class TeamCreateForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=TIMEZONE_CHOICES)

    class Meta:
        model = Team
        fields = ('name', 'timezone', 'monthly_unit_goal', 'display_mode')
        labels = {'monthly_unit_goal': 'Optional monthly unit goal'}
        help_texts = {
            'monthly_unit_goal': 'Unit count only. No commission or gross data is shared.',
        }

    def clean_name(self):
        return self.cleaned_data['name'].strip()


class TeamSettingsForm(TeamCreateForm):
    pass


class TeamGoalForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ('monthly_unit_goal',)
        labels = {'monthly_unit_goal': 'Optional monthly unit goal'}
        help_texts = {
            'monthly_unit_goal': 'Unit count only. No commission or gross data is shared.',
        }


class TeamInviteForm(forms.Form):
    intended_email = forms.EmailField(
        label='Email address',
        help_text=(
            'We will send a one-time invitation code and registration link. '
            'The recipient must verify this exact address before joining.'
        ),
    )

    def clean_intended_email(self):
        return self.cleaned_data['intended_email'].strip().lower()


class InvitationCodeForm(forms.Form):
    invitation_code = forms.CharField(
        max_length=100,
        strip=True,
        widget=forms.PasswordInput(render_value=True),
    )


class TeamCommentForm(forms.ModelForm):
    class Meta:
        model = TeamComment
        fields = ('body',)
        labels = {'body': 'Comment'}
        widgets = {
            'body': forms.Textarea(attrs={'rows': 3, 'maxlength': 500}),
        }

    def clean_body(self):
        body = self.cleaned_data['body'].strip()
        if not body:
            raise forms.ValidationError('Enter a comment.')
        return body


class ReactionForm(forms.Form):
    code = forms.ChoiceField(choices=TeamReaction.CODE_CHOICES)
