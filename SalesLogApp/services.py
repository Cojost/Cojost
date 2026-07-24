import calendar
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone

from .models.sales import (
    ArchivedSale,
    BonusLevel,
    Commission,
    CommissionAdjustment,
    DailyActivity,
    MonthlyGoal,
    Sale,
    calculate_bonus,
)
from .sale_types import get_sale_type_handler

ZERO = Decimal('0')


@transaction.atomic
def archive_sale(sale):
    """Snapshot an owned sale and its vehicle, then remove the live record."""
    archived = ArchivedSale.objects.create(
        user=sale.user, customer=sale.customer, dealNumber=sale.dealNumber,
        count=sale.count, split_with_name=sale.split_with_name,
        frontEnd=sale.frontEnd, backend=sale.backend, date=sale.date,
        sale_type=sale.sale_type,
    )
    get_sale_type_handler(sale.sale_type).archive_details(sale, archived)
    sale.delete()
    return archived


def month_bounds(month_start):
    start = month_start.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def commission_totals(user, sales):
    sales = list(sales)
    commission = Commission.objects.filter(user=user).first()
    units = sum((sale.count for sale in sales), ZERO)
    if not commission:
        return {'units': units, 'front': ZERO, 'back': ZERO, 'bonus': ZERO,
                'adjustments': ZERO, 'total': ZERO}
    front = sum((commission.calculate_front_end(s.frontEnd) for s in sales), ZERO)
    back = sum((commission.calculate_backend(s.backend) for s in sales), ZERO)
    levels = BonusLevel.objects.filter(user=user, commission=commission, active=True)
    bonus = calculate_bonus(sales, levels)
    adjustments = sum((
        item.signed_amount for item in CommissionAdjustment.objects.filter(
            user=user, commission=commission, active=True
        )
    ), ZERO)
    return {'units': units, 'front': front, 'back': back, 'bonus': bonus,
            'adjustments': adjustments, 'total': front + back + bonus + adjustments}


def month_metrics(user, month_start):
    start, end = month_bounds(month_start)
    activity = DailyActivity.objects.filter(user=user, date__gte=start, date__lt=end)
    activity_totals = activity.aggregate(
        leads=Sum('leads_taken'), calls=Sum('phone_calls_made')
    )
    sales = list(Sale.objects.filter(user=user, date__gte=start, date__lt=end))
    # Only explicitly owned archive rows are eligible.
    archived = list(ArchivedSale.objects.filter(user=user, date__gte=start, date__lt=end))
    totals = commission_totals(user, sales + archived)
    leads = Decimal(activity_totals['leads'] or 0)
    calls = Decimal(activity_totals['calls'] or 0)
    units = totals['units']
    return {
        'leads': leads, 'calls': calls, 'units': units, 'commission': totals['total'],
        'lead_to_unit_rate': units / leads if leads else None,
        'calls_per_lead': calls / leads if leads else None,
        'calls_per_unit': calls / units if units else None,
    }


def ceil_decimal(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def round_up_half(value):
    return (value * Decimal('2')).to_integral_value(rounding=ROUND_CEILING) / Decimal('2')


def forecast(user, month_start, current, target_units, target_commission):
    completed = []
    cursor = month_start.replace(day=1)
    earliest_dates = [
        DailyActivity.objects.filter(user=user).aggregate(value=Min('date'))['value'],
        Sale.objects.filter(user=user).aggregate(value=Min('date'))['value'],
        ArchivedSale.objects.filter(user=user).aggregate(value=Min('date'))['value'],
    ]
    earliest = min((value for value in earliest_dates if value), default=cursor)
    while cursor > earliest.replace(day=1):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        metric = month_metrics(user, cursor)
        if metric['leads'] or metric['units'] or metric['commission']:
            completed.append(metric)
    # Prefer the nearest three completed months; if fewer exist, use all available.
    if len(completed) >= 3:
        completed = completed[:3]
    historical_leads = sum((m['leads'] for m in completed), ZERO)
    historical_calls = sum((m['calls'] for m in completed), ZERO)
    historical_units = sum((m['units'] for m in completed), ZERO)
    historical_commission = sum((m['commission'] for m in completed), ZERO)
    remaining_units = max(target_units - current['units'], ZERO)
    remaining_commission = max(target_commission - current['commission'], ZERO)
    result = {
        'remaining_units': remaining_units,
        'remaining_commission': remaining_commission,
        'available': bool(historical_leads and historical_units and historical_commission),
    }
    if not result['available']:
        return result
    rate = historical_units / historical_leads
    calls_per_lead = historical_calls / historical_leads
    average_commission = historical_commission / historical_units
    leads_units = ceil_decimal(remaining_units / rate)
    financial_units = round_up_half(remaining_commission / average_commission)
    leads_financial = ceil_decimal(financial_units / rate)
    recommended = max(leads_units, leads_financial)
    result.update({
        'lead_to_unit_rate': rate,
        'calls_per_lead': calls_per_lead,
        'average_commission_per_unit': average_commission,
        'leads_for_unit_goal': leads_units,
        'estimated_financial_units': financial_units,
        'leads_for_financial_goal': leads_financial,
        'recommended_remaining_leads': recommended,
        'estimated_remaining_calls': ceil_decimal(Decimal(recommended) * calls_per_lead),
    })
    return result


def remaining_selling_days(month_start):
    today = timezone.localdate()
    start, end = month_bounds(month_start)
    cursor = max(today, start)
    # Fallback: Monday-Saturday; no holiday calendar exists in this project.
    return sum(1 for offset in range((end - cursor).days)
               if (cursor + timedelta(days=offset)).weekday() != calendar.SUNDAY)


def sales_month_context(user, month_start):
    start, end = month_bounds(month_start)
    sales = Sale.objects.filter(
        user=user, date__gte=start, date__lt=end
    ).select_related('vehicle__make', 'vehicle__model').order_by('date', 'dealNumber')
    totals = commission_totals(user, sales)
    return {
        'selected_month': start,
        'sales': sales,
        'total_count': totals['units'],
        'total_front_end': totals['front'],
        'total_back_end': totals['back'],
        'total_bonus': totals['bonus'],
        'total_adjustments': totals['adjustments'],
        'total_commission': totals['total'],
        'commission_instance': Commission.objects.filter(user=user).first(),
    }


def activity_month_context(user, month_start):
    selected_month = month_start.replace(day=1)
    goal = MonthlyGoal.objects.filter(
        user=user, month_start=selected_month
    ).first()
    target_units = goal.target_units if goal else ZERO
    target_commission = goal.target_commission if goal else ZERO
    current = month_metrics(user, selected_month)
    projection = forecast(
        user, selected_month, current, target_units, target_commission
    )
    unit_percent = (
        current['units'] / target_units * 100 if target_units else ZERO
    )
    commission_percent = (
        current['commission'] / target_commission * 100
        if target_commission else ZERO
    )
    selling_days = remaining_selling_days(selected_month)
    daily_pace = (
        Decimal(projection['recommended_remaining_leads']) / selling_days
        if projection.get('available') and selling_days else None
    )
    start, end = month_bounds(selected_month)
    month_activity = DailyActivity.objects.filter(
        user=user, date__gte=start, date__lt=end
    ).order_by('-date')
    return {
        'selected_month': selected_month,
        'goal': goal,
        'current': current,
        'forecast': projection,
        'target_units': target_units,
        'target_commission': target_commission,
        'unit_percent': unit_percent,
        'unit_progress': min(unit_percent, Decimal('100')),
        'commission_percent': commission_percent,
        'commission_progress': min(commission_percent, Decimal('100')),
        'daily_pace': daily_pace,
        'remaining_selling_days': selling_days,
        'month_activity': month_activity,
        'recent_activity': month_activity[:14],
        'commission_instance': Commission.objects.filter(user=user).first(),
    }


def activity_history_context(user, start_month, end_month):
    history = []
    cursor = end_month.replace(day=1)
    first = start_month.replace(day=1)
    while cursor >= first:
        metric = month_metrics(user, cursor)
        goal = MonthlyGoal.objects.filter(user=user, month_start=cursor).first()
        metric.update({
            'month': cursor,
            'goal': goal,
            'unit_reached': bool(goal and metric['units'] >= goal.target_units),
            'financial_reached': bool(
                goal and metric['commission'] >= goal.target_commission
            ),
        })
        history.append(metric)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return {
        'history': history,
        'history_start': first,
        'history_end': end_month.replace(day=1),
    }
