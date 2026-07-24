# sales/forms.py

from decimal import Decimal
from io import BytesIO

from django import forms
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageOps
from .models.sales import (
    Sale, Commission, BonusLevel, CommissionAdjustment, DailyActivity, MonthlyGoal
)
from django.forms import modelformset_factory
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from .models import UserProfile
from .models.vehicles import (
    STOCK_VALIDATOR, VIN_VALIDATOR, Vehicle, VehicleMake, VehicleModel,
    display_catalog_name, next_vehicle_year, normalize_catalog_name,
)

class SaleForm(forms.ModelForm):
    COUNT_CHOICES = [
        (Decimal('2'), '2'),
        (Decimal('1'), '1'),
        (Decimal('0.5'), '0.5'),
    ]
    count = forms.TypedChoiceField(
        choices=COUNT_CHOICES,
        coerce=Decimal,
        label='Count',
        initial=Decimal('1'),
        widget=forms.RadioSelect,
    )
    backend = forms.DecimalField(
        label='Back end',
        min_value=0,
        decimal_places=2,
        max_digits=10,
        widget=forms.NumberInput(attrs={'step': '1', 'inputmode': 'numeric'}),
    )
    
    class Meta:
        model = Sale
        fields = [
            'customer', 'date', 'frontEnd', 'backend', 'dealNumber', 'count',
            'split_with_name',
        ]
        labels = {'split_with_name': 'Split With'}
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'})
        }

    def clean(self):
        cleaned_data = super().clean()
        count_value = cleaned_data.get('count')
        split_name = (cleaned_data.get('split_with_name') or '').strip()
        is_half_deal = count_value == Decimal('0.5')
        if is_half_deal and not split_name:
            self.add_error(
                'split_with_name',
                'Enter the name of the salesperson who shares this half deal.',
            )
        cleaned_data['split_with_name'] = split_name if is_half_deal else ''
        return cleaned_data

    def clean_backend(self):
        value = self.cleaned_data['backend']
        if value != value.to_integral_value():
            raise forms.ValidationError('Enter a whole dollar amount.')
        return value


class VehicleForm(forms.Form):
    year = forms.TypedChoiceField(label='Year', coerce=int)
    make = forms.CharField(max_length=100, widget=forms.TextInput(attrs={
        'role': 'combobox', 'aria-autocomplete': 'list', 'autocomplete': 'off',
    }))
    make_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    add_make = forms.BooleanField(required=False, widget=forms.HiddenInput)
    model = forms.CharField(max_length=100, widget=forms.TextInput(attrs={
        'role': 'combobox', 'aria-autocomplete': 'list', 'autocomplete': 'off',
        'disabled': True,
    }))
    model_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    add_model = forms.BooleanField(required=False, widget=forms.HiddenInput)
    mileage = forms.IntegerField(min_value=0, max_value=10_000_000)
    stock_number = forms.CharField(max_length=50)
    vin = forms.CharField(max_length=17)

    def __init__(self, *args, user=None, sale=None, require_vehicle=True, **kwargs):
        self.user = user
        self.sale = sale
        self.require_vehicle = require_vehicle
        initial = kwargs.setdefault('initial', {})
        vehicle = getattr(sale, 'vehicle', None) if sale else None
        if vehicle:
            initial.update({
                'year': vehicle.year, 'make': vehicle.make.name,
                'make_id': vehicle.make_id, 'model': vehicle.model.name,
                'model_id': vehicle.model_id, 'mileage': vehicle.mileage,
                'stock_number': vehicle.stock_number, 'vin': vehicle.vin,
            })
        super().__init__(*args, **kwargs)
        current = next_vehicle_year()
        self.fields['year'].choices = [(year, year) for year in range(current, 1999, -1)]
        if initial.get('make_id') or (self.is_bound and self.data.get('make')):
            self.fields['model'].widget.attrs.pop('disabled', None)
        if not require_vehicle and not vehicle:
            for field in self.fields.values():
                field.required = False

    def clean_stock_number(self):
        value = self.cleaned_data['stock_number'].strip().upper()
        if not value:
            return ''
        STOCK_VALIDATOR(value)
        return value

    def clean_vin(self):
        value = self.cleaned_data['vin'].strip().upper()
        if not value:
            return ''
        VIN_VALIDATOR(value)
        return value

    def clean(self):
        cleaned = super().clean()
        entered = any(
            str(self.data.get(name, '')).strip()
            for name in ('year', 'make', 'model', 'mileage', 'stock_number', 'vin')
        )
        if not self.require_vehicle and not entered:
            cleaned['skip_vehicle'] = True
            return cleaned
        for name in ('year', 'make', 'model', 'mileage', 'stock_number', 'vin'):
            if not str(self.data.get(name, '')).strip():
                self.add_error(name, 'This field is required.')
        make_name = display_catalog_name(cleaned.get('make'))
        model_name = display_catalog_name(cleaned.get('model'))
        make = VehicleMake.objects.filter(
            normalized_name=normalize_catalog_name(make_name), active=True
        ).first()
        if not make:
            if not cleaned.get('add_make'):
                self.add_error('make', f'Select a result or explicitly add “{make_name}”.')
        elif not cleaned.get('add_make') and cleaned.get('make_id') != make.pk:
            self.add_error('make', 'Select a valid make result.')
        model = None
        if make:
            model = VehicleModel.objects.filter(
                make=make, normalized_name=normalize_catalog_name(model_name), active=True
            ).first()
            if not model and not cleaned.get('add_model'):
                self.add_error('model', f'Select a result or explicitly add “{model_name}”.')
            elif model and not cleaned.get('add_model') and cleaned.get('model_id') != model.pk:
                self.add_error('model', 'Select a valid model result.')
        elif not cleaned.get('add_model'):
            self.add_error('model', f'Explicitly add “{model_name}” for the new make.')
        cleaned.update({'make_name': make_name, 'model_name': model_name,
                        'catalog_make': make, 'catalog_model': model})
        return cleaned

    def save(self, sale):
        if self.cleaned_data.get('skip_vehicle'):
            return None
        make = self.cleaned_data['catalog_make']
        if make is None:
            make, _ = VehicleMake.objects.get_or_create(
                normalized_name=normalize_catalog_name(self.cleaned_data['make_name']),
                defaults={'name': self.cleaned_data['make_name'], 'created_by': self.user},
            )
        model = self.cleaned_data['catalog_model']
        if model is None:
            model, _ = VehicleModel.objects.get_or_create(
                make=make,
                normalized_name=normalize_catalog_name(self.cleaned_data['model_name']),
                defaults={'name': self.cleaned_data['model_name'], 'created_by': self.user},
            )
        vehicle, _ = Vehicle.objects.update_or_create(
            sale=sale,
            defaults={
                'year': self.cleaned_data['year'], 'make': make, 'model': model,
                'mileage': self.cleaned_data['mileage'],
                'stock_number': self.cleaned_data['stock_number'],
                'vin': self.cleaned_data['vin'],
            },
        )
        return vehicle


class DailyActivityForm(forms.ModelForm):
    class Meta:
        model = DailyActivity
        fields = ['date', 'leads_taken', 'phone_calls_made']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'leads_taken': forms.NumberInput(attrs={'min': 0}),
            'phone_calls_made': forms.NumberInput(attrs={'min': 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial['date'] = timezone.localdate()

    def clean_date(self):
        value = self.cleaned_data['date']
        if value > timezone.localdate():
            raise forms.ValidationError('Activity dates cannot be in the future.')
        return value


class MonthlyGoalForm(forms.ModelForm):
    month = forms.DateField(
        input_formats=['%Y-%m'],
        widget=forms.DateInput(format='%Y-%m', attrs={'type': 'month'}),
    )

    class Meta:
        model = MonthlyGoal
        fields = ['target_units', 'target_commission']
        widgets = {
            'target_units': forms.NumberInput(attrs={'min': 0, 'step': '0.5'}),
            'target_commission': forms.NumberInput(attrs={'min': 0, 'step': '0.01'}),
        }

    def __init__(self, *args, month_start=None, **kwargs):
        super().__init__(*args, **kwargs)
        selected = month_start or getattr(self.instance, 'month_start', None)
        if selected:
            self.initial['month'] = selected

    def clean_month(self):
        value = self.cleaned_data['month']
        return value.replace(day=1)


class AppearanceForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['theme_mode', 'header_color']
        labels = {
            'theme_mode': 'Display Mode',
            'header_color': 'Header Color',
        }
        widgets = {'header_color': forms.RadioSelect}


class AvatarForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['avatar']
        widgets = {
            'avatar': forms.ClearableFileInput(
                attrs={'accept': 'image/jpeg,image/png,image/webp'}
            ),
        }

    def clean_avatar(self):
        upload = self.cleaned_data.get('avatar')
        if not upload:
            return upload
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError('Profile pictures must be 2 MB or smaller.')
        try:
            upload.seek(0)
            image = Image.open(upload)
            if image.format not in {'JPEG', 'PNG', 'WEBP'}:
                raise forms.ValidationError('Upload a JPEG, PNG, or WebP image.')
            if image.width > 10000 or image.height > 10000:
                raise forms.ValidationError('Profile picture dimensions are too large.')
            image.verify()
            upload.seek(0)
            image = Image.open(upload)
            image = ImageOps.exif_transpose(image)
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            if image.mode not in {'RGB', 'RGBA'}:
                image = image.convert('RGBA')
            output = BytesIO()
            image.save(output, format='PNG', optimize=True)
        except forms.ValidationError:
            raise
        except Exception as exc:
            raise forms.ValidationError('Upload a valid image file.') from exc
        return ContentFile(output.getvalue(), name='avatar.png')

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
                    display_value = value * 100
                    if field_name == 'total_calculated_back_end':
                        display_value = display_value.quantize(Decimal('0.01'))
                    self.initial[field_name] = display_value

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
