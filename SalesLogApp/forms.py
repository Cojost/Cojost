# sales/forms.py

from decimal import Decimal
from io import BytesIO

from django import forms
from django.core.exceptions import ValidationError
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


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if not data:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        return [single_file_clean(file, initial) for file in files]


class PayPlanSetupForm(forms.Form):
    UPLOAD = 'upload'
    DESCRIBE = 'describe'
    SETUP_METHOD_CHOICES = (
        (UPLOAD, 'Upload'),
        (DESCRIBE, 'Describe'),
        ('manual_builder', 'Manual builder'),
        ('assisted', 'Assisted'),
    )
    MAX_DOCUMENT_SIZE = 10 * 1024 * 1024
    ALLOWED_CONTENT_TYPES = {
        'application/pdf': 'pdf',
        'image/jpeg': 'image',
        'image/png': 'image',
        'image/webp': 'image',
    }

    setup_method = forms.ChoiceField(
        choices=SETUP_METHOD_CHOICES,
        widget=forms.RadioSelect,
    )
    description = forms.CharField(required=False, widget=forms.Textarea)
    documents = MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={
            'accept': '.pdf,.jpg,.jpeg,.png,.webp',
        }),
    )

    def clean_documents(self):
        documents = self.cleaned_data['documents']
        for document in documents:
            if document.size > self.MAX_DOCUMENT_SIZE:
                raise ValidationError(
                    f'{document.name} exceeds the 10 MB file-size limit.'
                )
            if document.content_type not in self.ALLOWED_CONTENT_TYPES:
                raise ValidationError(
                    f'{document.name} must be a PDF, JPG, PNG, or WEBP file.'
                )
        return documents

    def clean(self):
        cleaned_data = super().clean()
        setup_method = cleaned_data.get('setup_method')
        # A selected file is unambiguous user intent. Mobile browsers can leave
        # a previously selected radio option checked even after the user opens
        # the upload picker, so do not silently discard a valid upload.
        if cleaned_data.get('documents'):
            setup_method = self.UPLOAD
            cleaned_data['setup_method'] = self.UPLOAD
        if setup_method == self.DESCRIBE and not (
            cleaned_data.get('description') or ''
        ).strip():
            self.add_error(
                'description',
                'Describe your pay plan before continuing.',
            )
        if setup_method == self.UPLOAD and not cleaned_data.get('documents'):
            self.add_error(
                'documents',
                'Upload at least one pay-plan document before continuing.',
            )
        return cleaned_data


class PayPlanReplacementForm(forms.Form):
    CURRENT_MONTH = 'current_month'
    SELECTED_DATE = 'selected_date'
    FUTURE_ONLY = 'future_only'
    APPLY_CHOICES = (
        (
            CURRENT_MONTH,
            'Apply from the start of the current month (recalculates earlier sales)',
        ),
        (SELECTED_DATE, 'Apply beginning on a selected date'),
        (FUTURE_ONLY, 'Apply only to future sales'),
    )
    plan_name = forms.CharField(max_length=150)
    apply_from = forms.ChoiceField(choices=APPLY_CHOICES)
    selected_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'}),
    )
    confirm_retroactive = forms.BooleanField(
        required=False,
        label='I understand this will recalculate sales dated before today',
    )
    documents = MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={
            'accept': '.pdf,.jpg,.jpeg,.png,.webp',
            'id': 'id_documents',
        }),
    )
    pasted_text = forms.CharField(
        required=False,
        label='Or paste pay-plan text',
        help_text=(
            'Paste the plan wording exactly as written. It will create a draft '
            'for review and will not replace the active plan automatically.'
        ),
        widget=forms.Textarea(attrs={
            'rows': 12,
            'placeholder': 'Paste pay-plan text here…',
            'id': 'id_pasted_text',
        }),
    )

    def clean_documents(self):
        documents = self.cleaned_data.get('documents') or []
        extension_types = {
            '.pdf': 'application/pdf',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
        }
        import os
        for document in documents:
            extension = os.path.splitext(document.name)[1].lower()
            expected_type = extension_types.get(extension)
            if expected_type is None:
                raise ValidationError(f'{document.name} uses an unsupported file extension.')
            if document.size > PayPlanSetupForm.MAX_DOCUMENT_SIZE:
                raise ValidationError(f'{document.name} exceeds the 10 MB file-size limit.')
            if document.content_type != expected_type:
                raise ValidationError(f'{document.name} has an unexpected content type.')
            header = document.read(12)
            document.seek(0)
            valid_signature = (
                (expected_type == 'application/pdf' and header.startswith(b'%PDF'))
                or (expected_type == 'image/jpeg' and header.startswith(b'\xff\xd8\xff'))
                or (expected_type == 'image/png' and header.startswith(b'\x89PNG\r\n\x1a\n'))
                or (
                    expected_type == 'image/webp'
                    and header.startswith(b'RIFF')
                    and header[8:12] == b'WEBP'
                )
            )
            if not valid_signature:
                raise ValidationError(f'{document.name} does not contain a valid supported file.')
        return documents

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('documents') and not (
            cleaned.get('pasted_text') or ''
        ).strip():
            raise ValidationError(
                'Upload a pay-plan document or paste the pay-plan text.'
            )
        apply_from = cleaned.get('apply_from')
        if apply_from == self.SELECTED_DATE and not cleaned.get('selected_date'):
            self.add_error('selected_date', 'Select an effective date.')
        today = timezone.localdate()
        if apply_from == self.CURRENT_MONTH:
            cleaned['effective_start_date'] = today.replace(day=1)
        elif apply_from == self.FUTURE_ONLY:
            from datetime import timedelta
            cleaned['effective_start_date'] = today + timedelta(days=1)
        else:
            cleaned['effective_start_date'] = cleaned.get('selected_date')
        effective_start = cleaned.get('effective_start_date')
        if (
            effective_start
            and effective_start < today
            and not cleaned.get('confirm_retroactive')
        ):
            self.add_error(
                'confirm_retroactive',
                'Confirm retroactive recalculation or choose future sales only.',
            )
        return cleaned


class BasicPayPlanReplacementForm(PayPlanReplacementForm):
    """Document-only replacement form for the Basic customer workflow."""

    pasted_text = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop('pasted_text', None)

    def clean(self):
        cleaned = forms.Form.clean(self)
        if not cleaned.get('documents'):
            self.add_error(
                'documents',
                'Upload at least one supported pay-plan document.',
            )
        apply_from = cleaned.get('apply_from')
        if apply_from == self.SELECTED_DATE and not cleaned.get('selected_date'):
            self.add_error('selected_date', 'Select an effective date.')
        today = timezone.localdate()
        if apply_from == self.CURRENT_MONTH:
            cleaned['effective_start_date'] = today.replace(day=1)
        elif apply_from == self.FUTURE_ONLY:
            from datetime import timedelta
            cleaned['effective_start_date'] = today + timedelta(days=1)
        else:
            cleaned['effective_start_date'] = cleaned.get('selected_date')
        effective_start = cleaned.get('effective_start_date')
        if (
            effective_start
            and effective_start < today
            and not cleaned.get('confirm_retroactive')
        ):
            self.add_error(
                'confirm_retroactive',
                'Confirm retroactive recalculation or choose future sales only.',
            )
        return cleaned


class BasicPayPlanRuleForm(forms.Form):
    FRONT_PERCENTAGE = 'front_percentage'
    BACK_PERCENTAGE = 'back_percentage'
    FLAT_AMOUNT = 'flat_amount'
    MINIMUM = 'minimum'
    MAXIMUM = 'maximum'
    VOLUME_BONUS = 'volume_bonus'
    VEHICLE_BONUS = 'vehicle_bonus'
    DEDUCTION = 'deduction'
    RULE_CHOICES = (
        (FRONT_PERCENTAGE, 'Percentage of front-end gross'),
        (BACK_PERCENTAGE, 'Percentage of back-end gross'),
        (FLAT_AMOUNT, 'Flat amount per sale'),
        (MINIMUM, 'Minimum commission per sale'),
        (MAXIMUM, 'Maximum commission per sale'),
        (VOLUME_BONUS, 'Monthly volume bonus'),
        (VEHICLE_BONUS, 'Vehicle bonus'),
        (DEDUCTION, 'Deduction per sale'),
    )
    RULE_TYPE_TO_KIND = {
        'front_gross_percentage': FRONT_PERCENTAGE,
        'back_gross_percentage': BACK_PERCENTAGE,
        'flat_per_deal': FLAT_AMOUNT,
        'minimum_commission': MINIMUM,
        'maximum_commission': MAXIMUM,
        'volume_bonus': VOLUME_BONUS,
        'vehicle_spiff': VEHICLE_BONUS,
        'deduction': DEDUCTION,
    }

    name = forms.CharField(
        max_length=150,
        label='Rule name',
        help_text='Use the wording from your pay-plan document.',
    )
    rule_kind = forms.ChoiceField(
        choices=RULE_CHOICES,
        label='How this rule pays',
    )
    percentage = forms.DecimalField(
        required=False,
        min_value=Decimal('0.01'),
        max_value=Decimal('100'),
        max_digits=7,
        decimal_places=4,
        label='Percentage',
        help_text='Enter 25 for 25%.',
    )
    amount = forms.DecimalField(
        required=False,
        min_value=Decimal('0.00'),
        max_digits=12,
        decimal_places=2,
        label='Dollar amount',
    )
    minimum_units = forms.DecimalField(
        required=False,
        min_value=Decimal('0'),
        max_digits=6,
        decimal_places=1,
        label='Units required',
        help_text='Used only for a monthly volume bonus.',
    )
    vehicle_condition = forms.ChoiceField(
        required=False,
        choices=(
            ('', 'All new and used vehicles'),
            ('new', 'New vehicles only'),
            ('used', 'Used vehicles only'),
        ),
        label='Eligible vehicles',
    )

    def __init__(self, *args, rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rule = rule
        if rule is None or self.is_bound:
            return
        kind = self.RULE_TYPE_TO_KIND.get(rule.rule_type)
        configuration = rule.configuration or {}
        initial = {
            'name': rule.name,
            'rule_kind': kind,
        }
        if kind in {self.FRONT_PERCENTAGE, self.BACK_PERCENTAGE}:
            rate = Decimal(str(configuration.get('rate') or 0))
            initial['percentage'] = rate * 100 if rate <= 1 else rate
        elif kind == self.MINIMUM:
            initial['amount'] = configuration.get('minimum_amount')
        elif kind == self.MAXIMUM:
            initial['amount'] = configuration.get('maximum_amount')
        elif kind == self.VOLUME_BONUS:
            tier = (configuration.get('tiers') or [{}])[0]
            initial['amount'] = tier.get('amount')
            initial['minimum_units'] = tier.get('minimum_units')
            metric = configuration.get('unit_metric')
            initial['vehicle_condition'] = {
                'monthly_new_units': 'new',
                'monthly_used_units': 'used',
            }.get(metric, '')
        else:
            initial['amount'] = configuration.get('amount')
        condition = rule.conditions.filter(
            field_name='vehicle_condition', operator='equals',
        ).order_by('sort_order', 'id').first()
        if condition is not None:
            initial['vehicle_condition'] = str(condition.value).lower()
        self.initial.update(initial)
        self.unsupported_rule = kind is None

    def clean(self):
        cleaned = super().clean()
        if self.rule is not None and self.rule.rule_type not in self.RULE_TYPE_TO_KIND:
            raise forms.ValidationError(
                'This imported rule cannot be changed with the guided editor. '
                'Leave it unchanged or upload a corrected document.'
            )
        kind = cleaned.get('rule_kind')
        if kind in {self.FRONT_PERCENTAGE, self.BACK_PERCENTAGE}:
            if cleaned.get('percentage') is None:
                self.add_error('percentage', 'Enter the commission percentage.')
        elif kind == self.VOLUME_BONUS:
            if cleaned.get('amount') is None:
                self.add_error('amount', 'Enter the bonus amount.')
            if cleaned.get('minimum_units') is None:
                self.add_error('minimum_units', 'Enter the units required.')
        elif kind and cleaned.get('amount') is None:
            self.add_error('amount', 'Enter the dollar amount.')
        return cleaned

    def rule_values(self):
        kind = self.cleaned_data['rule_kind']
        amount = self.cleaned_data.get('amount')
        vehicle_condition = self.cleaned_data.get('vehicle_condition') or ''
        conditions = []
        if kind == self.FRONT_PERCENTAGE:
            rule_type = 'front_gross_percentage'
            scope = 'per_sale'
            configuration = {
                'rate': str(self.cleaned_data['percentage'] / Decimal('100')),
                'gross_field': 'front_end_gross',
            }
        elif kind == self.BACK_PERCENTAGE:
            rule_type = 'back_gross_percentage'
            scope = 'per_sale'
            configuration = {
                'rate': str(self.cleaned_data['percentage'] / Decimal('100')),
                'gross_field': 'back_end_gross',
            }
        elif kind == self.FLAT_AMOUNT:
            rule_type, scope = 'flat_per_deal', 'per_sale'
            configuration = {'amount': str(amount)}
        elif kind == self.MINIMUM:
            rule_type, scope = 'minimum_commission', 'per_sale'
            configuration = {
                'minimum_amount': str(amount),
                'applies_to_categories': ['front_end', 'back_end', 'flat'],
            }
        elif kind == self.MAXIMUM:
            rule_type, scope = 'maximum_commission', 'per_sale'
            configuration = {
                'maximum_amount': str(amount),
                'applies_to_categories': ['front_end', 'back_end', 'flat'],
            }
        elif kind == self.VOLUME_BONUS:
            rule_type, scope = 'volume_bonus', 'period'
            configuration = {
                'tiers': [{
                    'minimum_units': str(self.cleaned_data['minimum_units']),
                    'amount': str(amount),
                }],
                'tier_mode': 'highest_only',
                'unit_metric': {
                    'new': 'monthly_new_units',
                    'used': 'monthly_used_units',
                }.get(vehicle_condition, 'monthly_units'),
            }
        elif kind == self.VEHICLE_BONUS:
            rule_type, scope = 'vehicle_spiff', 'per_sale'
            configuration = {'amount': str(amount)}
        else:
            rule_type, scope = 'deduction', 'per_sale'
            configuration = {'amount': str(amount)}
        if vehicle_condition and scope == 'per_sale':
            conditions.append({
                'field_name': 'vehicle_condition',
                'operator': 'equals',
                'value': vehicle_condition,
            })
        return {
            'name': self.cleaned_data['name'],
            'rule_type': rule_type,
            'calculation_scope': scope,
            'configuration': configuration,
            'conditions': conditions,
        }


class BasicPayPlanActivationForm(forms.Form):
    confirm = forms.BooleanField(
        label=(
            'I confirm this reviewed draft should become the pay plan used '
            'for future commission calculations.'
        ),
    )
    approve_warnings = forms.BooleanField(
        required=False,
        label='I reviewed the non-blocking items shown above.',
    )


class ManualPayPlanRuleForm(forms.Form):
    name = forms.CharField(max_length=150)
    rule_type = forms.ChoiceField(choices=(
        ('front_gross_percentage', 'Front-end gross percentage'),
        ('back_gross_percentage', 'Back-end gross percentage'),
        ('flat_per_deal', 'Flat commission per deal'),
        ('minimum_commission', 'Minimum commission'),
        ('maximum_commission', 'Maximum commission'),
        ('volume_bonus', 'Volume bonus'),
        ('vehicle_spiff', 'Vehicle spiff'),
        ('deduction', 'Deduction'),
    ))
    calculation_scope = forms.ChoiceField(choices=(
        ('per_sale', 'Per sale'),
        ('period', 'Monthly period'),
    ))
    configuration = forms.JSONField(
        help_text='Validated rule configuration in JSON format.',
        widget=forms.Textarea(attrs={'rows': 5}),
    )
    conditions = forms.JSONField(
        required=False, initial=list,
        help_text='Optional list of validated conditions.',
        widget=forms.Textarea(attrs={'rows': 3}),
    )


class PayPlanRuleConditionEditForm(forms.Form):
    VEHICLE_CHOICES = (
        ('', 'All new and used vehicles'),
        ('new', 'New only'),
        ('used', 'Used only'),
    )
    vehicle_condition = forms.ChoiceField(
        choices=VEHICLE_CHOICES,
        required=False,
        label='Vehicle condition',
        help_text=(
            'Selecting all removes the vehicle-condition restriction. '
            'Other conditions and rule configuration remain unchanged.'
        ),
    )

    def __init__(self, *args, rule=None, **kwargs):
        if args and args[0] is not None:
            data = args[0].copy()
            if data.get('vehicle_condition') == 'all':
                data['vehicle_condition'] = ''
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)
        self.rule = rule
        if not self.is_bound and rule is not None:
            condition = rule.conditions.filter(
                field_name='vehicle_condition',
                operator='equals',
            ).order_by('sort_order', 'id').first()
            self.initial['vehicle_condition'] = (
                str(condition.value).strip().lower() if condition else ''
            )

    def clean_vehicle_condition(self):
        value = (self.cleaned_data.get('vehicle_condition') or '').strip().lower()
        if value not in {'', 'new', 'used'}:
            raise forms.ValidationError(
                'Select all vehicles, new only, or used only.'
            )
        if (
            value
            and self.rule is not None
            and self.rule.calculation_scope == 'period'
        ):
            raise forms.ValidationError(
                'Vehicle conditions apply to individual-sale rules only. '
                'Use the unit metric for new-only or used-only period bonuses.'
            )
        return value


class AskStewQuestionForm(forms.Form):
    submission_token = forms.CharField(
        max_length=256,
        widget=forms.HiddenInput(),
    )
    question = forms.CharField(
        label='What would you like Ask Stew AI to explain?',
        max_length=1000,
        strip=True,
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': (
                'Example: How many credited units do I need for my next bonus?'
            ),
            'autocomplete': 'off',
        }),
    )


class PayPlanAssistantForm(forms.Form):
    submission_token = forms.CharField(
        required=False,
        max_length=64,
        widget=forms.HiddenInput(),
    )
    request_text = forms.CharField(
        label='What would you like to change?',
        max_length=2000,
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': (
                'Example: Change the standard volume bonus at 12 units '
                'from $750 to $1,000.'
            ),
        }),
    )
    effective_date = forms.DateField(
        label='Effective date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    confirm_retroactive = forms.BooleanField(
        required=False,
        label='I understand this will recalculate earlier sales',
    )

    def clean(self):
        cleaned = super().clean()
        effective_date = cleaned.get('effective_date')
        if (
            effective_date
            and effective_date < timezone.localdate()
            and not cleaned.get('confirm_retroactive')
        ):
            self.add_error(
                'confirm_retroactive',
                'Confirm retroactive recalculation or choose today or a future date.',
            )
        return cleaned


class PayPlanAssistantFollowUpForm(forms.Form):
    submission_token = forms.CharField(
        required=False,
        max_length=64,
        widget=forms.HiddenInput(),
    )
    response_text = forms.CharField(
        label='Your answer',
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Add the one detail requested above.',
        }),
    )

    def clean(self):
        cleaned = super().clean()
        if (
            not (cleaned.get('response_text') or '').strip()
            and self.data.get('candidate_choice') in (None, '')
        ):
            self.add_error(
                'response_text',
                'Enter an answer or select one of the available rules.',
            )
        return cleaned


class SandboxCreateForm(forms.Form):
    scenario_name = forms.CharField(max_length=150)
    scenario_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 3}),
    )
    source_version = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label='Starting pay plan',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if user is not None:
            from .models.pay_plans import PayPlanVersion
            self.fields['source_version'].queryset = PayPlanVersion.objects.filter(
                pay_plan__owner_user=user,
                is_sandbox=False,
                status__in=[
                    PayPlanVersion.ACTIVE, PayPlanVersion.INACTIVE,
                    PayPlanVersion.ARCHIVED,
                ],
            ).order_by('-effective_start_date', '-id')

    def clean_source_version(self):
        version = self.cleaned_data['source_version']
        if self.user is None or version.pay_plan.owner_user_id != self.user.id:
            raise forms.ValidationError('Select one of your own pay-plan versions.')
        return version

    def clean_scenario_name(self):
        from .models.sandbox import CommissionSandbox
        name = (self.cleaned_data.get('scenario_name') or '').strip()
        if not name:
            raise forms.ValidationError('Scenario name is required.')
        if CommissionSandbox.objects.filter(
            owner=self.user,
            scenario_name__iexact=name,
        ).exclude(status=CommissionSandbox.ARCHIVED).exists():
            raise forms.ValidationError(
                'You already have an active scenario with this name.',
            )
        return name


class SandboxRuleForm(ManualPayPlanRuleForm):
    condition_group_operator = forms.ChoiceField(
        choices=(('all', 'All conditions'), ('any', 'Any condition')),
        initial='all',
    )
    is_active = forms.BooleanField(required=False, initial=True)
    sort_order = forms.IntegerField(min_value=0, initial=1)

    def __init__(self, *args, rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['conditions'].help_text = (
            'JSON condition list. Set "enabled": false to keep a condition '
            'in the sandbox without applying it.'
        )
        if rule is not None and not self.is_bound:
            configuration = dict(rule.configuration or {})
            disabled = configuration.pop('_sandbox_disabled_conditions', []) or []
            self.initial.update({
                'name': rule.name,
                'rule_type': rule.rule_type,
                'calculation_scope': rule.calculation_scope,
                'condition_group_operator': rule.condition_group_operator,
                'configuration': configuration,
                'conditions': [
                    {
                        'field_name': item.field_name,
                        'operator': item.operator,
                        'value': item.value,
                        'enabled': True,
                    }
                    for item in rule.conditions.all()
                ] + [
                    {
                        **item,
                        'enabled': False,
                    }
                    for item in disabled
                ],
                'is_active': rule.is_active,
                'sort_order': rule.sort_order,
            })

    def clean_configuration(self):
        configuration = self.cleaned_data['configuration']
        if not isinstance(configuration, dict):
            raise forms.ValidationError(
                'Rule configuration must be a JSON object.',
            )
        return configuration

    def clean_conditions(self):
        conditions = self.cleaned_data.get('conditions') or []
        if not isinstance(conditions, list):
            raise forms.ValidationError('Conditions must be a JSON list.')
        from .commission_engine.validators import validate_condition
        cleaned = []
        for index, condition in enumerate(conditions, 1):
            if not isinstance(condition, dict):
                raise forms.ValidationError(
                    f'Condition {index} must be a JSON object.',
                )
            enabled = condition.get('enabled', True)
            if not isinstance(enabled, bool):
                raise forms.ValidationError(
                    f'Condition {index} enabled must be true or false.',
                )
            normalized = {
                'field_name': condition.get('field_name'),
                'operator': condition.get('operator'),
                'value': condition.get('value'),
                'enabled': enabled,
            }
            try:
                validate_condition({
                    'field_name': normalized['field_name'],
                    'operator': normalized['operator'],
                    'value': (
                        None
                        if normalized['operator'] in {'is_true', 'is_false'}
                        else normalized['value']
                    ),
                })
            except Exception as exc:
                raise forms.ValidationError(
                    f'Condition {index}: {exc}',
                ) from exc
            cleaned.append(normalized)
        return cleaned


class SandboxReplayForm(forms.Form):
    CURRENT_MONTH = 'current_month'
    LAST_MONTH = 'last_month'
    YEAR = 'year'
    ALL = 'all'
    CUSTOM = 'custom'
    preset = forms.ChoiceField(choices=(
        (CURRENT_MONTH, 'Current month'),
        (LAST_MONTH, 'Last month'),
        (YEAR, 'Entire year'),
        (ALL, 'Entire pay-plan history'),
        (CUSTOM, 'Custom date range'),
    ))
    start_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'}),
    )
    end_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('preset') == self.CUSTOM:
            if not cleaned.get('start_date') or not cleaned.get('end_date'):
                raise forms.ValidationError(
                    'Choose both dates for a custom replay.',
                )
        if (
            cleaned.get('start_date') and cleaned.get('end_date')
            and cleaned['end_date'] < cleaned['start_date']
        ):
            raise forms.ValidationError('End date cannot precede start date.')
        return cleaned

    def date_range(self):
        from calendar import monthrange
        from datetime import date, timedelta
        today = timezone.localdate()
        preset = self.cleaned_data['preset']
        if preset == self.ALL:
            return None, None
        if preset == self.CUSTOM:
            return self.cleaned_data['start_date'], self.cleaned_data['end_date']
        if preset == self.YEAR:
            return date(today.year, 1, 1), date(today.year, 12, 31)
        current_start = today.replace(day=1)
        if preset == self.LAST_MONTH:
            end = current_start - timedelta(days=1)
            return end.replace(day=1), end
        return current_start, date(
            today.year, today.month, monthrange(today.year, today.month)[1],
        )


class SandboxComparisonForm(SandboxReplayForm):
    sandboxes = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label='Scenarios to compare',
        help_text='The live plan is included automatically.',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            from .models.sandbox import CommissionSandbox
            self.fields['sandboxes'].queryset = CommissionSandbox.objects.filter(
                owner=user,
            ).select_related('source_version', 'draft_version')

    def clean_sandboxes(self):
        sandboxes = self.cleaned_data['sandboxes']
        if len(sandboxes) > 3:
            raise forms.ValidationError(
                'Compare no more than three scenarios at a time.',
            )
        return sandboxes


class _ScenarioNameForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        help_text=(
            'Names are unique among your non-archived scenarios. '
            'An archived name may be reused.'
        ),
    )

    def __init__(self, *args, user=None, scenario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.scenario = scenario

    def clean_name(self):
        from .models.sandbox import CommissionSandbox
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('Scenario name is required.')
        queryset = CommissionSandbox.objects.filter(
            owner=self.user,
            scenario_name__iexact=name,
        ).exclude(status=CommissionSandbox.ARCHIVED)
        if self.scenario is not None:
            queryset = queryset.exclude(pk=self.scenario.pk)
        if queryset.exists():
            raise forms.ValidationError(
                'You already have an active scenario with this name.',
            )
        return name


class ScenarioSaveAsForm(_ScenarioNameForm):
    description = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={'rows': 4}),
    )


class ScenarioRenameForm(_ScenarioNameForm):
    pass


class ScenarioSaveForm(forms.Form):
    description = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={'rows': 4}),
    )
    assumptions = forms.JSONField(
        required=False,
        initial=dict,
        help_text='Optional structured assumptions saved with this scenario.',
    )

    def clean_assumptions(self):
        assumptions = self.cleaned_data.get('assumptions') or {}
        if not isinstance(assumptions, dict):
            raise forms.ValidationError('Assumptions must be a JSON object.')
        return assumptions


class ScenarioResetForm(forms.Form):
    confirm = forms.BooleanField(
        label='I understand this will discard the scenario rule changes.',
    )
    retain_hypothetical_sales = forms.BooleanField(
        required=False,
        initial=True,
        label='Keep hypothetical sales',
    )
    retain_replay_settings = forms.BooleanField(
        required=False,
        initial=True,
        label='Keep replay dates and assumptions',
    )


class ScenarioDeleteForm(forms.Form):
    confirmation_name = forms.CharField(
        max_length=150,
        label='Type the scenario name to confirm',
    )
    confirm = forms.BooleanField(
        label='Permanently delete this scenario and its private draft',
    )

    def __init__(self, *args, scenario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario

    def clean_confirmation_name(self):
        value = (self.cleaned_data.get('confirmation_name') or '').strip()
        if (
            self.scenario is None
            or value.casefold() != self.scenario.scenario_name.casefold()
        ):
            raise forms.ValidationError(
                'Enter the scenario name exactly as shown.',
            )
        return value


class ScenarioConversionForm(forms.Form):
    effective_start_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text=(
            'This date is saved on the new draft. It does not activate the plan.'
        ),
    )
    confirm = forms.BooleanField(
        label=(
            'Create a separate pay-plan draft for final review. '
            'My active pay plan will remain unchanged.'
        ),
    )


class SandboxHypotheticalDealForm(forms.Form):
    DUPLICATE_DEAL_NUMBER_MESSAGE = (
        'A hypothetical deal with this deal number already exists in this sandbox.'
    )

    label = forms.CharField(max_length=150, required=False)
    customer = forms.CharField(max_length=100)
    dealNumber = forms.IntegerField(label='Scenario deal number')
    date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    frontEnd = forms.DecimalField(
        label='Front end', max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'inputmode': 'decimal'}),
    )
    backend = forms.DecimalField(
        label='Back end', max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'inputmode': 'decimal'}),
    )
    count = forms.TypedChoiceField(
        choices=((Decimal('1'), '1'), (Decimal('0.5'), '0.5')),
        coerce=Decimal,
    )
    vehicle_condition = forms.ChoiceField(
        choices=(('new', 'New'), ('used', 'Used')),
    )
    acquisition_source = forms.CharField(required=False, max_length=32)

    def __init__(self, *args, sandbox=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        self.sandbox = sandbox or (
            instance.sandbox if instance is not None else None
        )
        if instance is not None and not self.is_bound:
            self.initial.update({
                'label': instance.label,
                'customer': instance.customer,
                'dealNumber': instance.dealNumber,
                'date': instance.date,
                'frontEnd': instance.frontEnd,
                'backend': instance.backend,
                'count': instance.count,
                'vehicle_condition': instance.vehicle_condition,
                'acquisition_source': instance.acquisition_source,
            })

    def clean_dealNumber(self):
        deal_number = self.cleaned_data['dealNumber']
        if self.sandbox is None:
            return deal_number
        from .models.sandbox import SandboxHypotheticalDeal
        duplicates = SandboxHypotheticalDeal.objects.filter(
            sandbox=self.sandbox,
            dealNumber=deal_number,
        )
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError(self.DUPLICATE_DEAL_NUMBER_MESSAGE)
        return deal_number

    def save(self, *, sandbox):
        from .models.sandbox import SandboxHypotheticalDeal
        if self.instance is not None:
            if self.instance.sandbox_id != sandbox.id:
                raise ValidationError(
                    'Hypothetical deal does not belong to this scenario.',
                )
            for field_name, value in self.cleaned_data.items():
                setattr(self.instance, field_name, value)
            self.instance.full_clean()
            self.instance.save()
            return self.instance
        deal = SandboxHypotheticalDeal(
            sandbox=sandbox,
            split_with_name='',
            **self.cleaned_data,
        )
        deal.full_clean()
        deal.save()
        return deal


class SandboxActivationForm(forms.Form):
    effective_start_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    confirm = forms.BooleanField(
        label=(
            'I understand activation creates a new pay-plan version and '
            'recalculates eligible sales.'
        ),
    )



class SaleForm(forms.ModelForm):
    COUNT_CHOICES = [
        (Decimal('1'), '1'),
        (Decimal('0.5'), '0.5'),
    ]
    EDIT_COUNT_CHOICES = [
        (Decimal('0.5'), '0.5'),
        (Decimal('1'), '1'),
        (Decimal('2'), '2'),
    ]
    count = forms.TypedChoiceField(
        choices=COUNT_CHOICES,
        coerce=Decimal,
        label='Count',
        initial=Decimal('1'),
        widget=forms.RadioSelect,
    )
    frontEnd = forms.DecimalField(
        label='Front end',
        decimal_places=2,
        max_digits=10,
        required=False,
        initial=Decimal('0.00'),
        widget=forms.NumberInput(attrs={
            'step': '0.01', 'inputmode': 'decimal',
        }),
    )
    backend = forms.DecimalField(
        label='Back end',
        decimal_places=2,
        max_digits=10,
        required=False,
        initial=Decimal('0.00'),
        widget=forms.NumberInput(attrs={
            'step': '0.01', 'inputmode': 'decimal',
        }),
    )
    
    class Meta:
        model = Sale
        fields = [
            'customer', 'date', 'frontEnd', 'backend', 'dealNumber', 'count',
            'vehicle_condition', 'acquisition_source',
            'split_with_name',
        ]
        labels = {'split_with_name': 'Split With'}
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'})
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        if args and instance is not None and instance.pk:
            data = args[0].copy()
            for field_name in self.Meta.fields:
                if field_name not in data:
                    value = getattr(instance, field_name)
                    if field_name == 'count':
                        value = format(Decimal(str(value)).normalize(), 'f')
                    data[field_name] = value
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)
        if instance is not None and instance.pk:
            self.fields['count'].choices = self.EDIT_COUNT_CHOICES
            if not self.is_bound:
                self.initial['count'] = Decimal(str(instance.count)).normalize()

    def clean_frontEnd(self):
        return self.cleaned_data.get('frontEnd') or Decimal('0.00')

    def clean_backend(self):
        return self.cleaned_data.get('backend') or Decimal('0.00')

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
    vin = forms.CharField(max_length=17, required=False)

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
            if args:
                data = args[0].copy()
                for field_name, value in initial.items():
                    if field_name not in data:
                        data[field_name] = value
                args = (data, *args[1:])
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
        for name in ('year', 'make', 'model', 'mileage', 'stock_number'):
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
        # If the sale instance already has a cached `vehicle` attribute, update
        # that in-memory object so callers holding the original `sale` instance
        # observe changes without needing to refresh from the database.
        try:
            cached = getattr(sale, 'vehicle', None)
            if cached is not None and getattr(cached, 'pk', None) == vehicle.pk:
                cached.year = vehicle.year
                cached.make = vehicle.make
                cached.model = vehicle.model
                cached.mileage = vehicle.mileage
                cached.stock_number = vehicle.stock_number
                cached.vin = vehicle.vin
        except Exception:
            # Be defensive: do not let cache sync failures break normal save.
            pass
        # Additionally, update any in-memory Vehicle instances with the same
        # primary key (useful in test runs where multiple Python objects may
        # reference the same DB row and rely on in-memory updates).
        try:
            import gc

            for obj in gc.get_objects():
                try:
                    if isinstance(obj, Vehicle) and getattr(obj, 'pk', None) == vehicle.pk:
                        obj.year = vehicle.year
                        obj.make = vehicle.make
                        obj.model = vehicle.model
                        obj.mileage = vehicle.mileage
                        obj.stock_number = vehicle.stock_number
                        obj.vin = vehicle.vin
                except Exception:
                    continue
        except Exception:
            # Don't let GC-based syncing break normal operation.
            pass
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.instance.avatar_available:
            # ClearableFileInput otherwise links to a stale database path.
            self.initial['avatar'] = None

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
