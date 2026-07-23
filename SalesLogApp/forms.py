# sales/forms.py

from decimal import Decimal

from django import forms
from .models.sales import Sale, Commission, BonusLevel, CommissionAdjustment
from django.forms import modelformset_factory
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User

class SaleForm(forms.ModelForm):
    COUNT_CHOICES = [
        (2, '2'),
        (1, '1'),
        (0.5, '0.5'),
    ]
    count = forms.ChoiceField(choices=COUNT_CHOICES, label='Count', initial=1, widget=forms.RadioSelect)
    backend = forms.DecimalField(
        label='Back end',
        min_value=0,
        decimal_places=0,
        max_digits=10,
        widget=forms.NumberInput(attrs={'step': '1', 'inputmode': 'numeric'}),
    )
    
    class Meta:
        model = Sale
        fields = ['customer', 'date', 'frontEnd', 'backend', 'dealNumber', 'count']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'})
        }

class BonusLevelForm(forms.ModelForm):
    class Meta:
        model = BonusLevel
        fields = ['count_threshold', 'amount', 'active']


class OtherCommissionAdjustmentForm(forms.ModelForm):
    amount = forms.DecimalField(
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'min': '0', 'step': '0.01'}),
    )

    class Meta:
        model = CommissionAdjustment
        fields = ['description', 'kind', 'amount', 'active']
        widgets = {
            'description': forms.TextInput(attrs={'placeholder': 'Example: CSI bonus'}),
        }

        

BonusLevelFormSet = forms.modelformset_factory(BonusLevel, form=BonusLevelForm, extra=1, can_delete=True)

# Create a formset for multiple bonus levels
BonusLevelFormSet = modelformset_factory(
    BonusLevel,
    fields=('count_threshold', 'amount', 'active'),
    extra=0,  # No extra forms
    can_delete=True  # Allow deletion
)


class CommissionAdjustmentForm(forms.ModelForm):
    total_calculated_front_end = forms.DecimalField(
        label='Commission rate (%)',
        required=False,
        min_value=0,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Example: 25'}),
        help_text='Enter 25 for 25%.',
    )
    total_calculated_back_end = forms.DecimalField(
        label='Commission rate (%)',
        required=False,
        min_value=0,
        max_value=999.9,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Example: 5'}),
        help_text='Enter 5 for 5%.',
    )

    class Meta:
        model = Commission
        fields = [
            'frontend_minimum', 'frontend_maximum', 'total_calculated_front_end', 'opt_out_front',
            'backend_minimum', 'backend_maximum', 'total_calculated_back_end', 'opt_out_back',
            
        ]
        widgets = {
            'frontend_minimum': forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Optional'}),
            'frontend_maximum': forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Optional'}),
            'backend_minimum': forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Optional'}),
            'backend_maximum': forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'Optional'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['frontend_minimum'].label = 'Minimum commission ($)'
        self.fields['frontend_maximum'].label = 'Maximum commission ($)'
        self.fields['backend_minimum'].label = 'Minimum commission ($)'
        self.fields['backend_maximum'].label = 'Maximum commission ($)'
        self.fields['opt_out_front'].label = 'Do not calculate front-end commission'
        self.fields['opt_out_back'].label = 'Do not calculate back-end commission'

        if not self.is_bound and self.instance:
            for field_name in ('total_calculated_front_end', 'total_calculated_back_end'):
                value = getattr(self.instance, field_name, None)
                if value is not None:
                    self.initial[field_name] = value * 100

    def clean(self):
        cleaned_data = super().clean()
        
        # Extract fields from cleaned data
        frontend_minimum = cleaned_data.get('frontend_minimum')
        frontend_maximum = cleaned_data.get('frontend_maximum')
        total_calculated_front_end = cleaned_data.get('total_calculated_front_end')
        opt_out_front = cleaned_data.get('opt_out_front')

        backend_minimum = cleaned_data.get('backend_minimum')
        backend_maximum = cleaned_data.get('backend_maximum')
        total_calculated_back_end = cleaned_data.get('total_calculated_back_end')
        opt_out_back = cleaned_data.get('opt_out_back')

        if total_calculated_front_end is not None:
            cleaned_data['total_calculated_front_end'] = total_calculated_front_end / 100
        if total_calculated_back_end is not None:
            cleaned_data['total_calculated_back_end'] = total_calculated_back_end / 100
        elif not opt_out_back:
            cleaned_data['total_calculated_back_end'] = Decimal('0')

        # Front-end validation
        if not opt_out_front:
            if total_calculated_front_end is None:
                self.add_error('total_calculated_front_end', "You must set a front-end commission percentage or opt-out.")
            if frontend_minimum is not None and frontend_maximum is not None and frontend_minimum > frontend_maximum:
                self.add_error('frontend_maximum', "Maximum front-end value must be greater than or equal to minimum front-end value.")
        
        # Back-end validation
        if not opt_out_back:
            if backend_minimum is not None and backend_maximum is not None and backend_minimum > backend_maximum:
                self.add_error('backend_maximum', "Maximum back-end value must be greater than or equal to minimum back-end value.")
        
        return cleaned_data

class UserLoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Username'}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Password'}))

class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Username'}))
    password1 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Password'}))
    password2 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Confirm Password'}))

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")
    
    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user
