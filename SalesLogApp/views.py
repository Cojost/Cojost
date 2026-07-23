from decimal import Decimal
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
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required

def commission_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not Commission.objects.filter(user=request.user).exists():
            return redirect('adjust_commission')
        return view_func(request, *args, **kwargs)
    return wrapper

@commission_required
def view_sales(request):
    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    if today.month == 12:
        start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        start_of_next_month = today.replace(month=today.month + 1, day=1)

    commission_instance = Commission.objects.filter(user=request.user).first()
    monthly_sales = list(Sale.objects.filter(
        user=request.user,
        date__gte=start_of_month,
        date__lt=start_of_next_month,
    ))
    total_count = sum((sale.count for sale in monthly_sales), Decimal('0'))
    sales_commission = sum(
        (sale.commission_total for sale in monthly_sales),
        Decimal('0'),
    )
    bonus_levels = BonusLevel.objects.filter(
        user=request.user,
        commission=commission_instance,
    )
    total_bonus = calculate_bonus(monthly_sales, bonus_levels)
    other_adjustments = CommissionAdjustment.objects.filter(
        user=request.user,
        commission=commission_instance,
        active=True,
    )
    total_adjustments = sum(
        (adjustment.signed_amount for adjustment in other_adjustments),
        Decimal('0'),
    )

    context = {
        'total_commission': sales_commission + total_bonus + total_adjustments,
        'total_count': total_count,
        'sales': Sale.objects.filter(user=request.user),
        'commission_instance': commission_instance,
    }
    request.session['total_count'] = float(total_count)
    return render(request, 'view_sales.html', context)

@commission_required
def add_sale(request):
    commission_instance = Commission.objects.filter(user=request.user).first()
    if request.method == 'POST':
        form = SaleForm(request.POST)
        if form.is_valid():
            sale = form.save(commit=False)
            sale.user = request.user
            sale.save()
            return redirect('view_sales')
    else:
        form = SaleForm()
    return render(request, 'add_sale.html', {
        'form': form,
        'commission_instance': commission_instance,
    })

@commission_required
def edit_sale(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id, user=request.user)

    if request.method == 'POST':
        form = SaleForm(request.POST, instance=sale)
        if form.is_valid():
            form.save()
            return redirect('view_sales')
    else:
        form = SaleForm(instance=sale)

    return render(request, 'edit_sale.html', {'form': form, 'sale': sale})

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
