from decimal import Decimal
from datetime import datetime, timedelta
from functools import wraps
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from .models.sales import (
    Sale,
    Commission,
    BonusLevel,
    CommissionAdjustment,
    calculate_bonus,
)
from .forms import (
    SaleForm,
    CommissionAdjustmentForm,
    BonusLevelForm,
    OtherCommissionAdjustmentForm,
    modelformset_factory,
    UserLoginForm,
    CustomUserCreationForm,
)
from django.utils import timezone
from django.contrib.auth.views import LoginView
from django.contrib.auth import authenticate, login 
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.urls import reverse
from .models.sales import DailyActivity, MonthlyGoal
from .models.vehicles import VehicleMake, VehicleModel, normalize_catalog_name
from .forms import DailyActivityForm, MonthlyGoalForm
from .forms import AppearanceForm, AvatarForm
from .profile_context import get_user_profile
from .services import (
    activity_history_context,
    activity_month_context,
    sales_month_context,
)
from .models.sales import SaleType
from .sale_types import get_sale_type_handler

def commission_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not Commission.objects.filter(user=request.user).exists():
            return redirect('adjust_commission')
        return view_func(request, *args, **kwargs)
    return wrapper


def _selected_month(request):
    raw = request.GET.get('month') or request.POST.get('month')
    if raw:
        try:
            return datetime.strptime(raw, '%Y-%m').date().replace(day=1)
        except ValueError:
            pass
    return timezone.localdate().replace(day=1)


def _previous_month(month):
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def _history_range(request, selected_month):
    default_start = selected_month
    for _ in range(11):
        default_start = _previous_month(default_start)

    def parse(name, fallback):
        raw = request.GET.get(name)
        if not raw:
            return fallback
        try:
            return datetime.strptime(raw, '%Y-%m').date().replace(day=1)
        except ValueError:
            return fallback

    start = parse('history_start', default_start)
    end = parse('history_end', selected_month)
    if start > end:
        return default_start, selected_month
    # Bound reports to five years to avoid accidental unbounded work.
    cursor, count = end, 1
    while cursor > start and count <= 60:
        cursor = _previous_month(cursor)
        count += 1
    if count > 60:
        return default_start, selected_month
    return start, end


@login_required
def activity_goals(request, activity_id=None):
    user = request.user
    selected_month = _selected_month(request)
    activity_instance = get_object_or_404(
        DailyActivity, pk=activity_id, user=user
    ) if activity_id is not None else None
    if request.method == 'POST' and request.POST.get('form_type') == 'activity':
        form = DailyActivityForm(request.POST, instance=activity_instance)
        if form.is_valid():
            cleaned = form.cleaned_data
            with transaction.atomic():
                DailyActivity.objects.update_or_create(
                    user=user, date=cleaned['date'],
                    defaults={'leads_taken': cleaned['leads_taken'],
                              'phone_calls_made': cleaned['phone_calls_made']},
                )
            messages.success(request, 'Daily activity saved.')
            return redirect(f"{reverse('activity_goals')}?month={selected_month:%Y-%m}")
    else:
        form = DailyActivityForm(instance=activity_instance)
    goal = MonthlyGoal.objects.filter(user=user, month_start=selected_month).first()
    if request.method == 'POST' and request.POST.get('form_type') == 'goal':
        goal_form = MonthlyGoalForm(request.POST, instance=goal, month_start=selected_month)
        if goal_form.is_valid():
            month = goal_form.cleaned_data['month']
            MonthlyGoal.objects.update_or_create(
                user=user, month_start=month,
                defaults={'target_units': goal_form.cleaned_data['target_units'],
                          'target_commission': goal_form.cleaned_data['target_commission']},
            )
            messages.success(request, 'Monthly goals saved.')
            return redirect(f"{reverse('activity_goals')}?month={month:%Y-%m}")
    else:
        goal_form = MonthlyGoalForm(instance=goal, month_start=selected_month)
    context = activity_month_context(user, selected_month)
    history_start, history_end = _history_range(request, selected_month)
    context.update(activity_history_context(user, history_start, history_end))
    context.update({
        'activity_form': form, 'goal_form': goal_form, 'selected_month': selected_month,
    })
    return render(request, 'activity_goals.html', context)

@commission_required
def view_sales(request):
    context = sales_month_context(request.user, _selected_month(request))
    request.session['total_count'] = float(context['total_count'])
    return render(request, 'view_sales.html', context)


@login_required
def print_sales(request):
    context = sales_month_context(request.user, _selected_month(request))
    context.update({
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': f"{reverse('view_sales')}?month={context['selected_month']:%Y-%m}",
    })
    return render(request, 'reports/print_sales.html', context)


@login_required
def print_activity_goals(request):
    selected_month = _selected_month(request)
    context = activity_month_context(request.user, selected_month)
    context.update({
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': f"{reverse('activity_goals')}?month={selected_month:%Y-%m}",
    })
    return render(request, 'reports/print_activity_goals.html', context)


@login_required
def print_activity_history(request):
    selected_month = _selected_month(request)
    history_start, history_end = _history_range(request, selected_month)
    context = activity_history_context(request.user, history_start, history_end)
    context.update({
        'selected_month': selected_month,
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': (
            f"{reverse('activity_goals')}?month={selected_month:%Y-%m}"
            f"&history_start={history_start:%Y-%m}&history_end={history_end:%Y-%m}"
        ),
    })
    return render(request, 'reports/print_activity_history.html', context)


@login_required
def profile(request):
    user_profile = get_user_profile(request.user)
    password_form_class = (
        PasswordChangeForm if request.user.has_usable_password() else SetPasswordForm
    )
    if request.method == 'POST':
        action = request.POST.get('form_type')
        if action == 'appearance':
            appearance_form = AppearanceForm(request.POST, instance=user_profile)
            if appearance_form.is_valid():
                appearance_form.save()
                messages.success(request, 'Appearance settings saved.')
                return redirect('profile')
        elif action == 'reset_header_color':
            user_profile.reset_header_color()
            user_profile.full_clean()
            user_profile.save()
            messages.success(request, 'Header color reset to blue.')
            return redirect('profile')
        elif action == 'avatar':
            old_name = user_profile.avatar.name if user_profile.avatar else ''
            old_storage = user_profile.avatar.storage if user_profile.avatar else None
            avatar_form = AvatarForm(request.POST, request.FILES, instance=user_profile)
            if avatar_form.is_valid():
                avatar_form.save()
                if old_name and old_name != user_profile.avatar.name:
                    old_storage.delete(old_name)
                messages.success(request, 'Profile picture updated.')
                return redirect('profile')
        elif action == 'remove_avatar':
            if user_profile.avatar:
                storage = user_profile.avatar.storage
                old_name = user_profile.avatar.name
                user_profile.avatar = ''
                user_profile.save(update_fields=['avatar', 'updated_at'])
                storage.delete(old_name)
            messages.success(request, 'Profile picture removed.')
            return redirect('profile')
        elif action == 'password':
            password_form = password_form_class(request.user, request.POST)
            if password_form.is_valid():
                changed_user = password_form.save()
                update_session_auth_hash(request, changed_user)
                messages.success(request, 'Password updated securely.')
                return redirect('profile')

    appearance_form = locals().get(
        'appearance_form', AppearanceForm(instance=user_profile)
    )
    avatar_form = locals().get('avatar_form', AvatarForm(instance=user_profile))
    password_form = locals().get(
        'password_form', password_form_class(request.user)
    )
    return render(request, 'profile.html', {
        'profile': user_profile,
        'appearance_form': appearance_form,
        'avatar_form': avatar_form,
        'password_form': password_form,
        'password_is_set': request.user.has_usable_password(),
        'commission_instance': Commission.objects.filter(user=request.user).first(),
    })

@commission_required
def add_sale(request):
    commission_instance = Commission.objects.filter(user=request.user).first()
    handler = get_sale_type_handler(SaleType.AUTOMOTIVE)
    if request.method == 'POST':
        form = SaleForm(request.POST)
        vehicle_form = handler.build_form(
            request.POST, user=request.user, require_vehicle=True
        )
        sale_valid = form.is_valid()
        vehicle_valid = vehicle_form.is_valid()
        if sale_valid and vehicle_valid:
            with transaction.atomic():
                sale = form.save(commit=False)
                sale.user = request.user
                sale.save()
                handler.save_details(vehicle_form, sale)
            return redirect('view_sales')
    else:
        form = SaleForm()
        vehicle_form = handler.build_form(user=request.user, require_vehicle=True)
    return render(request, 'add_sale.html', {
        'form': form,
        'vehicle_form': vehicle_form,
        'commission_instance': commission_instance,
    })

@commission_required
def edit_sale(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id, user=request.user)
    handler = get_sale_type_handler(sale.sale_type)

    if request.method == 'POST':
        form = SaleForm(request.POST, instance=sale)
        vehicle_form = handler.build_form(
            request.POST, user=request.user, sale=sale, require_vehicle=False
        )
        sale_valid = form.is_valid()
        vehicle_valid = vehicle_form.is_valid()
        if sale_valid and vehicle_valid:
            with transaction.atomic():
                sale = form.save()
                handler.save_details(vehicle_form, sale)
            return redirect('view_sales')
    else:
        form = SaleForm(instance=sale)
        vehicle_form = handler.build_form(
            user=request.user, sale=sale, require_vehicle=False
        )

    return render(request, 'edit_sale.html', {
        'form': form, 'vehicle_form': vehicle_form, 'sale': sale,
    })


@login_required
def vehicle_make_search(request):
    term = request.GET.get('q', '').strip()
    if len(term) > 100:
        return JsonResponse({'results': []}, status=400)
    queryset = VehicleMake.objects.filter(active=True)
    if term:
        queryset = queryset.filter(normalized_name__contains=normalize_catalog_name(term))
    results = list(queryset.order_by('name').values('id', 'name')[:20])
    return JsonResponse({'results': results})


@login_required
def vehicle_model_search(request):
    term = request.GET.get('q', '').strip()
    try:
        make_id = int(request.GET.get('make_id', ''))
    except (TypeError, ValueError):
        return JsonResponse({'results': []}, status=400)
    if len(term) > 100 or not VehicleMake.objects.filter(pk=make_id, active=True).exists():
        return JsonResponse({'results': []}, status=400)
    queryset = VehicleModel.objects.filter(make_id=make_id, active=True)
    if term:
        queryset = queryset.filter(normalized_name__contains=normalize_catalog_name(term))
    return JsonResponse({'results': list(
        queryset.order_by('name').values('id', 'name')[:20]
    )})

@commission_required
def delete_sale(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id, user=request.user)

    if request.method == 'POST':
        sale.delete()
        return redirect('view_sales')

    return render(request, 'delete_sale.html', {'sale': sale})

@commission_required
def view_commission(request):
    user = request.user
    # Sale creation belongs to Add Sale, where complete vehicle validation is enforced.
    if request.method == 'POST':
        return redirect('add_sale')
    commission_instance = get_object_or_404(Commission, user=user)
    bonus_levels = BonusLevel.objects.filter(
        user=user,
        commission=commission_instance,
    )
    other_adjustments = CommissionAdjustment.objects.filter(
        user=user,
        commission=commission_instance,
        active=True,
    )
    total_adjustments = sum(
        (adjustment.signed_amount for adjustment in other_adjustments),
        Decimal('0'),
    )

    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    if today.month == 12:
        start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        start_of_next_month = today.replace(month=today.month + 1, day=1)

    def current_month_sales():
        return Sale.objects.filter(
            user=user,
            date__gte=start_of_month,
            date__lt=start_of_next_month,
        )

    def calculate_totals_and_bonuses(sales):
        total_count = sum(s.count for s in sales)
        total_front_end = sum(commission_instance.calculate_front_end(s.frontEnd) for s in sales)
        total_back_end = sum(commission_instance.calculate_backend(s.backend) for s in sales)
        total_bonus = calculate_bonus(sales, bonus_levels)
        return total_count, total_front_end, total_back_end, total_bonus

    if request.method == 'POST':
        form = SaleForm(request.POST)
        if form.is_valid():
            sale = form.save(commit=False)
            sale.user = user
            sale.save()

            # Recalculate totals after saving the sale
            sales = current_month_sales()
            total_count, total_calculated_front_end, total_calculated_back_end, total_bonus = calculate_totals_and_bonuses(sales)

            return render(request, 'view_commission.html', {
                'commission_instance': commission_instance,
                'form': SaleForm(),
                'total_count': total_count,
                'total_front_end': total_calculated_front_end,
                'total_back_end': total_calculated_back_end,
                'total_bonus': total_bonus,
                'other_adjustments': other_adjustments,
                'total_adjustments': total_adjustments,
                'total_commission': total_calculated_front_end + total_calculated_back_end + total_bonus + total_adjustments,
                'sales': sales
            })
    else:
        form = SaleForm()

    sales = current_month_sales()
    total_count, total_calculated_front_end, total_calculated_back_end, total_bonus = calculate_totals_and_bonuses(sales)

    return render(request, 'view_commission.html', {
        'commission_instance': commission_instance,
        'form': form,
        'total_count': total_count,
        'total_front_end': total_calculated_front_end,
        'total_back_end': total_calculated_back_end,
        'total_bonus': total_bonus,
        'other_adjustments': other_adjustments,
        'total_adjustments': total_adjustments,
        'total_commission': total_calculated_front_end + total_calculated_back_end + total_bonus + total_adjustments,
        'sales': sales
    })




@login_required
def adjust_commission(request, commission_id=None):
    user = request.user

    # Fetch the commission instance and associated bonus levels
    if commission_id:
        commission_instance = get_object_or_404(Commission, pk=commission_id, user=request.user)
    else:
        commission_instance, created = Commission.objects.get_or_create(user=request.user)
    
    # Query for existing bonus levels linked to this commission
    bonus_levels = BonusLevel.objects.filter(user=request.user, commission=commission_instance)
    other_adjustments = CommissionAdjustment.objects.filter(
        user=request.user,
        commission=commission_instance,
    )
    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    if today.month == 12:
        start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        start_of_next_month = today.replace(month=today.month + 1, day=1)
    sales = Sale.objects.filter(
        user=user,
        date__gte=start_of_month,
        date__lt=start_of_next_month,
    )
    
    total_count = sum(sale.count for sale in sales)


    # Define the formset for managing multiple BonusLevel entries
    BonusLevelFormSet = modelformset_factory(
        BonusLevel,
        fields=('count_threshold', 'amount', 'active'),
        form=BonusLevelForm,
        extra=1,  # Allows adding a new bonus level
        can_delete=True  # Allows deleting bonus levels
    )
    OtherAdjustmentFormSet = modelformset_factory(
        CommissionAdjustment,
        form=OtherCommissionAdjustmentForm,
        extra=1,
        can_delete=True,
    )

    if request.method == 'POST':
        # Handle form and formset submission
        form = CommissionAdjustmentForm(request.POST, instance=commission_instance)
        formset = BonusLevelFormSet(
            request.POST,
            queryset=bonus_levels,
            prefix='unit_bonus',
        )
        adjustment_formset = OtherAdjustmentFormSet(
            request.POST,
            queryset=other_adjustments,
            prefix='other_adjustment',
        )

        if form.is_valid() and formset.is_valid() and adjustment_formset.is_valid():
            # Save the commission adjustments
            form.save()

            # Save the bonus levels, linking them to the correct commission and user
            bonus_levels = formset.save(commit=False)  # Defer saving to add relationships
            for bonus_level in bonus_levels:
                bonus_level.commission = commission_instance  # Associate with commission
                bonus_level.user = request.user  # Ensure the bonus level is linked to the user
                bonus_level.save()

            for bonus_level in formset.deleted_objects:
                bonus_level.delete()

            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.commission = commission_instance
                adjustment.user = request.user
                adjustment.save()

            for adjustment in adjustment_formset.deleted_objects:
                adjustment.delete()

            return redirect('view_commission')  # Redirect after successful save
    else:
        # Handle GET request, initializing the form and formset with existing data
        form = CommissionAdjustmentForm(instance=commission_instance)
        formset = BonusLevelFormSet(
            queryset=bonus_levels,
            prefix='unit_bonus',
        )
        adjustment_formset = OtherAdjustmentFormSet(
            queryset=other_adjustments,
            prefix='other_adjustment',
        )

    chart_data = {
        "labels": [level.count_threshold for level in bonus_levels],
        "data": [float(level.amount) for level in bonus_levels],
    }
    # Query for current bonus levels for displaying in the template
    current_bonus_levels = bonus_levels

    return render(request, 'adjust_commission.html', {
        'form': form,
        'formset': formset,
        'adjustment_formset': adjustment_formset,
        'commission': commission_instance,
        'current_bonus_levels': current_bonus_levels,
        'total_count': total_count,
        'chart_data': chart_data,
    })
@login_required
def add_bonus(request):
    user = request.user
    commission_instance = get_object_or_404(Commission, user=user)
    BonusLevelFormSet = modelformset_factory(
        BonusLevel,
        form=BonusLevelForm,
        extra=1,  # Allows adding a new bonus level
        can_delete=True  # Allows deleting bonus levels
    )
    
    if request.method == 'POST':
        owned_levels = BonusLevel.objects.filter(user=user, commission=commission_instance)
        formset = BonusLevelFormSet(request.POST, queryset=owned_levels)
        if formset.is_valid():
            bonus_levels = formset.save(commit=False)
            for bonus in bonus_levels:
                bonus.commission = commission_instance
                bonus.user = user
                bonus.save()
            return redirect('view_commission')
    else:
        formset = BonusLevelFormSet(queryset=BonusLevel.objects.filter(user=user, commission=commission_instance))

    return render(request, 'add_bonus.html', {'formset': formset})

class UserLoginView(LoginView):
    template_name = 'login.html'
    authentication_form = UserLoginForm


def register(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            user.backend = 'django.contrib.auth.backends.ModelBackend'  # Set backend
            user = authenticate(username=user.username, password=form.cleaned_data['password1'])
            if user is not None:
                login(request, user)
                # Retrieve or create a commission instance
                commission, created = Commission.objects.get_or_create(user=user)
                # Redirect to adjust commission page with commission_id
                return redirect('adjust_commission_by_id', commission_id=commission.id)
            else:
                form.add_error(None, 'Authentication failed.')
        else:
            form.add_error(None, 'Form is not valid.')
    else:
        form = CustomUserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

def create_default_bonus_levels(user):
    commission = Commission.objects.filter(user=user).first()
    if commission:
        BonusLevel.objects.create(user=user, level=1, amount=1000.00, commission=commission)
        BonusLevel.objects.create(user=user, level=2, amount=2000.00, commission=commission)
        # Add more levels as required

def update_bonus_level(request, level_id):
    if request.method == 'POST':
        level = get_object_or_404(BonusLevel, id=level_id)
        new_amount = request.POST.get('bonus-amount')
        level.amount = new_amount
        level.save()

        # Send back the updated bonus level to refresh the chart
        updated_level = {
            'id': level.id,
            'level': level.level,
            'amount': level.amount,
        }
        return JsonResponse({'success': True, 'updated_level': updated_level})

    return JsonResponse({'success': False}, status=400)
