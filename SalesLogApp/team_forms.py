from zoneinfo import available_timezones

from django import forms
from django.contrib.auth import get_user_model

from .models import Team, TeamComment, TeamMembership, TeamReaction


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
    username = forms.CharField(max_length=150)
    intended_email = forms.EmailField(
        required=False,
        label='Verified email (optional)',
        help_text='If supplied, the signed-in recipient must have verified this address.',
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        user_model = get_user_model()
        try:
            self.intended_user = user_model.objects.get(username__iexact=username)
        except user_model.DoesNotExist as exc:
            raise forms.ValidationError('No eligible registered account was found.') from exc
        return username

    def clean_intended_email(self):
        return self.cleaned_data.get('intended_email', '').strip().lower()


class InvitationCodeForm(forms.Form):
    invitation_code = forms.CharField(
        max_length=100,
        strip=True,
        widget=forms.PasswordInput(render_value=True),
    )


class SharingPreferenceForm(forms.Form):
    sharing_preference = forms.ChoiceField(choices=TeamMembership.SHARING_CHOICES)


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
