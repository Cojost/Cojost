import calendar
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone

from .access import uses_new_engine
from .archive_aggregation import (
    ArchivedSaleAggregationAdapter,
    archived_month_commission_totals,
    load_owned_sale_records,
)
from .commission_service import CommissionEngineService
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
        vehicle_condition=sale.vehicle_condition,
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


def commission_totals(user, sales, *, period_start=None, period_end=None):
    sales = list(sales)
    commission = Commission.objects.filter(user=user).first()
    units = sum((sale.unit_credit for sale in sales), ZERO)
    if uses_new_engine(user):
        diagnostics = CommissionEngineService.calculate_sales(
            user,
            sales,
            period_start=period_start,
            period_end=period_end,
        )
        return {
            'units': units,
            'front': diagnostics['total_front'],
            'back': diagnostics['total_back'],
            'bonus': diagnostics['total_bonus'],
            'adjustments': ZERO,
            'total': diagnostics['total_commission'],
            'diagnostics': diagnostics,
        }
    if not commission:
        return {'units': units, 'front': ZERO, 'back': ZERO, 'bonus': ZERO,
                'adjustments': ZERO, 'total': ZERO}
    front = sum((s.calculate_frontEnd for s in sales), ZERO)
    back = sum((s.calculate_backend for s in sales), ZERO)
    levels = BonusLevel.objects.filter(user=user, commission=commission, active=True)
    bonus = calculate_bonus(sales, levels)
    adjustments = sum((
        item.signed_amount for item in CommissionAdjustment.objects.filter(
            user=user, commission=commission, active=True
        )
    ), ZERO)
    return {'units': units, 'front': front, 'back': back, 'bonus': bonus,
            'adjustments': adjustments, 'total': front + back + bonus + adjustments}


def _bonus_tier_rows(tiers, bonus_units, current_tier=None, next_tier=None):
    """Normalize configured tiers into display-only progress rows."""
    rows = []
    next_found = False
    included_tier_ids = set()
    tier_groups = {}
    for tier in tiers:
        if tier.get('_rule_id') is not None:
            tier_groups.setdefault(tier['_rule_id'], []).append(tier)
    for rule_tiers in tier_groups.values():
        qualifying = [
            tier for tier in rule_tiers
            if bonus_units >= Decimal(str(tier.get('minimum_units', 0)))
            and (
                tier.get('maximum_units') in (None, '')
                or bonus_units <= Decimal(str(tier['maximum_units']))
            )
        ]
        if not qualifying:
            continue
        if rule_tiers[0].get('_tier_mode') == 'cumulative':
            included_tier_ids.update(id(tier) for tier in qualifying)
        else:
            included_tier_ids.add(id(max(
                qualifying,
                key=lambda tier: Decimal(str(tier.get('amount', 0))),
            )))
    current_minimum = (
        Decimal(str(current_tier.get('minimum_units', 0)))
        if current_tier else None
    )
    next_minimum = (
        Decimal(str(next_tier.get('minimum_units', 0)))
        if next_tier else None
    )
    for tier in sorted(
        tiers, key=lambda item: Decimal(str(item.get('minimum_units', 0)))
    ):
        minimum = Decimal(str(tier.get('minimum_units', 0)))
        maximum_value = tier.get('maximum_units')
        maximum = (
            Decimal(str(maximum_value))
            if maximum_value not in (None, '') else None
        )
        amount = Decimal(str(tier.get('amount', 0)))
        is_configured_match = id(tier) in included_tier_ids
        is_current = (
            not tier_groups
            and current_minimum is not None
            and minimum == current_minimum
            and amount == Decimal(str(current_tier.get('amount', 0)))
        )
        is_next = (
            not next_found
            and next_minimum is not None
            and minimum == next_minimum
        )
        if is_current or is_configured_match:
            status = 'current'
            status_label = 'Included now'
            units_needed = None
        elif is_next:
            status = 'next'
            units_needed = max(Decimal('0'), minimum - bonus_units)
            unit_label = 'unit' if units_needed == Decimal('1') else 'units'
            units_text = format(units_needed.normalize(), 'f')
            status_label = f'{units_text} {unit_label} away'
            next_found = True
        elif minimum <= bonus_units:
            status = 'passed'
            status_label = 'Passed'
            units_needed = None
        else:
            status = 'available'
            status_label = 'Available'
            units_needed = minimum - bonus_units
        rows.append({
            'minimum_units': minimum,
            'maximum_units': maximum,
            'amount': amount,
            'status': status,
            'status_label': status_label,
            'units_needed': units_needed,
        })
    return rows


def format_bonus_amount(amount):
    amount = Decimal(str(amount or 0))
    if amount < 0:
        return f'-${abs(amount):,.2f}'
    return f'${amount:,.2f}'


def _bonus_component_rows(diagnostics, expected_total):
    """Group authoritative bonus line items without changing their total."""
    grouped = {}

    def include(item, scope):
        amount = Decimal(str(item.get('amount') or 0))
        rule_name = (item.get('rule_name') or '').strip()
        rule_type = item.get('rule_type') or ''
        fallback_names = {
            'volume_bonus': 'Monthly volume bonus',
            'per_unit_bonus': 'Per-unit bonus',
            'period_qualification_bonus': 'Monthly qualification bonus',
            'survey_count_bonus': 'NPS survey bonus',
            'acquisition_bonus': 'Acquisition bonus',
            'vehicle_spiff': 'Vehicle bonus',
        }
        label = rule_name or fallback_names.get(rule_type, 'Other bonus')
        rule_id = item.get('rule_id')
        key = (scope, rule_id or (rule_type, label))
        component = grouped.setdefault(key, {
            'label': label,
            'rule_id': rule_id,
            'rule_type': rule_type,
            'amount': ZERO,
            'count': 0,
            'scope': scope,
            'explanation': item.get('explanation') or '',
        })
        component['amount'] += amount
        component['count'] += 1

    unit_bonus = diagnostics.get('unit_bonus') or {}
    for item in unit_bonus.get('line_items') or []:
        include(item, 'period')

    for result in diagnostics.get('results') or []:
        for item in result.line_items:
            if item.get('applied') and item.get('category') in {'bonus', 'spiff'}:
                include(item, 'deal')

    rows = []
    for component in grouped.values():
        if component['scope'] == 'period':
            component['detail'] = 'Monthly bonus'
        elif component['count'] == 1:
            component['detail'] = '1 deal'
        else:
            component['detail'] = f"{component['count']} deals"
        component['display_amount'] = format_bonus_amount(component['amount'])
        rows.append(component)

    itemized_total = sum((row['amount'] for row in rows), ZERO)
    remainder = Decimal(str(expected_total or 0)) - itemized_total
    if remainder:
        rows.append({
            'label': 'Other bonus',
            'amount': remainder,
            'display_amount': format_bonus_amount(remainder),
            'count': 1,
            'scope': 'other',
            'detail': 'Included in the calculated bonus total',
            'explanation': '',
        })
    return rows


def bonus_breakdown(user, totals, diagnostics):
    """Describe the authoritative bonus total without recalculating it."""
    if uses_new_engine(user):
        unit_bonus = diagnostics.get('unit_bonus') or {}
        bonus_units = Decimal(str(unit_bonus.get('bonus_units', totals['units'])))
        tiers = _bonus_tier_rows(
            unit_bonus.get('tiers') or [],
            bonus_units,
            unit_bonus.get('current_tier'),
            unit_bonus.get('next_tier'),
        )
        tier_modes = {
            tier.get('_tier_mode', 'highest_only')
            for tier in (unit_bonus.get('tiers') or [])
        }
        return {
            'total': totals['bonus'],
            'display_total': format_bonus_amount(totals['bonus']),
            'items': _bonus_component_rows(diagnostics, totals['bonus']),
            'unit_bonus': diagnostics.get('period_unit_bonus', ZERO),
            'deal_bonus': diagnostics.get('total_deal_bonus', ZERO),
            'bonus_units': bonus_units,
            'tiers': tiers,
            'qualification_pending': unit_bonus.get('qualification_pending', False),
            'tier_policy': (
                'cumulative' if tier_modes == {'cumulative'}
                else 'highest_only' if tier_modes <= {'highest_only'}
                else 'mixed'
            ),
        }

    commission = Commission.objects.filter(user=user).first()
    levels = list(
        BonusLevel.objects.filter(
            user=user, commission=commission, active=True,
        ).order_by('count_threshold', 'id')
    ) if commission else []
    raw_tiers = [
        {
            'minimum_units': level.count_threshold,
            'maximum_units': None,
            'amount': level.amount,
        }
        for level in levels
    ]
    qualifying = [
        tier for tier in raw_tiers
        if totals['units'] >= Decimal(str(tier['minimum_units']))
    ]
    current_tier = qualifying[-1] if qualifying else None
    next_tier = next((
        tier for tier in raw_tiers
        if Decimal(str(tier['minimum_units'])) > totals['units']
    ), None)
    return {
        'total': totals['bonus'],
        'display_total': format_bonus_amount(totals['bonus']),
        'items': ([{
            'label': 'Unit bonus',
            'amount': totals['bonus'],
            'display_amount': format_bonus_amount(totals['bonus']),
            'count': 1,
            'scope': 'period',
            'detail': 'Monthly bonus',
            'explanation': '',
        }] if totals['bonus'] else []),
        'unit_bonus': totals['bonus'],
        'deal_bonus': ZERO,
        'bonus_units': totals['units'],
        'tiers': _bonus_tier_rows(
            raw_tiers, totals['units'], current_tier, next_tier,
        ),
        'qualification_pending': False,
        'tier_policy': 'highest_only',
    }


def reporting_commission_totals(
    user, records, *, period_start=None, period_end=None,
):
    """Use the accepted live/archive reporting policy for a record collection."""

    records = list(records)
    if any(
        isinstance(record, ArchivedSaleAggregationAdapter)
        for record in records
    ):
        return archived_month_commission_totals(user, records)
    return {
        **commission_totals(
            user,
            records,
            period_start=period_start,
            period_end=period_end,
        ),
        'commission_complete': True,
        'commission_source': 'live_sales',
        'commission_diagnostic': '',
    }


def month_metrics(user, month_start):
    start, end = month_bounds(month_start)
    activity = DailyActivity.objects.filter(user=user, date__gte=start, date__lt=end)
    activity_totals = activity.aggregate(
        leads=Sum('leads_taken'), calls=Sum('phone_calls_made')
    )
    record_set = load_owned_sale_records(
        user,
        start_date=start,
        end_date=end,
    )
    records = record_set.records
    totals = reporting_commission_totals(
        user,
        records,
        period_start=start,
        period_end=end - timedelta(days=1),
    )
    total_gross = sum(
        (
            Decimal(str(sale.frontEnd or 0))
            + Decimal(str(sale.backend or 0))
            for sale in records
        ),
        ZERO,
    )
    leads = Decimal(activity_totals['leads'] or 0)
    calls = Decimal(activity_totals['calls'] or 0)
    units = totals['units']
    return {
        'leads': leads, 'calls': calls, 'units': units,
        'total_gross': total_gross, 'commission': totals['total'],
        'commission_complete': totals['commission_complete'],
        'commission_source': totals['commission_source'],
        'commission_diagnostic': totals['commission_diagnostic'],
        'duplicate_archive_count': record_set.duplicate_archive_count,
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
    commission_history_complete = all(
        m.get('commission_complete', True) for m in completed
    )
    historical_commission = sum(
        (m['commission'] for m in completed if m['commission'] is not None),
        ZERO,
    )
    remaining_units = max(target_units - current['units'], ZERO)
    commission_complete = current.get('commission_complete', True)
    remaining_commission = (
        max(target_commission - current['commission'], ZERO)
        if commission_complete and current['commission'] is not None else None
    )
    result = {
        'remaining_units': remaining_units,
        'remaining_commission': remaining_commission,
        'available': bool(
            commission_complete
            and commission_history_complete
            and historical_leads
            and historical_units
            and historical_commission
        ),
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
    totals = commission_totals(
        user,
        sales,
        period_start=start,
        period_end=end - timedelta(days=1),
    )
    diagnostics = totals.get('diagnostics') or CommissionEngineService.calculate_sales(user, list(sales))
    draw_progress = diagnostics.get('draw_progress')
    draw_amount = (
        Decimal(str(draw_progress.get('amount') or 0))
        if draw_progress else ZERO
    )
    active_plan = CommissionEngineService.active_plan_summary(user)
    sale_diagnostics_by_id = {item.sale_id: item for item in diagnostics['results']}
    for sale in sales:
        sale.commission_result = sale_diagnostics_by_id.get(sale.id)
    return {
        'selected_month': start,
        'sales': sales,
        'total_count': totals['units'],
        'total_front_end': totals['front'],
        'total_back_end': totals['back'],
        'total_bonus': totals['bonus'],
        'bonus_breakdown': bonus_breakdown(user, totals, diagnostics),
        'total_adjustments': totals['adjustments'],
        'total_commission': totals['total'],
        'display_total_commission': format_bonus_amount(totals['total']),
        'draw_amount': draw_amount,
        'total_commission_after_draw': totals['total'] - draw_amount,
        'commission_instance': Commission.objects.filter(user=user).first(),
        'commission_diagnostics': diagnostics,
        'sale_diagnostics_by_id': sale_diagnostics_by_id,
        'active_plan_summary': active_plan,
    }


def activity_month_context(user, month_start):
    selected_month = month_start.replace(day=1)
    goal = MonthlyGoal.objects.filter(
        user=user, month_start=selected_month
    ).first()
    target_units = goal.target_units if goal else ZERO
    target_total_gross = goal.target_total_gross if goal else ZERO
    target_commission = goal.target_commission if goal else ZERO
    current = month_metrics(user, selected_month)
    unit_percent = (
        current['units'] / target_units * 100 if target_units else ZERO
    )
    commission_percent = None
    if current['commission_complete']:
        commission_percent = (
            current['commission'] / target_commission * 100
            if target_commission else ZERO
        )
    gross_percent = (
        current['total_gross'] / target_total_gross * 100
        if target_total_gross else ZERO
    )
    start, end = month_bounds(selected_month)
    month_activity = DailyActivity.objects.filter(
        user=user, date__gte=start, date__lt=end
    ).order_by('-date')
    return {
        'selected_month': selected_month,
        'goal': goal,
        'current': current,
        'target_units': target_units,
        'target_total_gross': target_total_gross,
        'target_commission': target_commission,
        'unit_percent': unit_percent,
        'unit_progress': min(max(unit_percent, ZERO), Decimal('100')),
        'gross_percent': gross_percent,
        'gross_progress': min(max(gross_percent, ZERO), Decimal('100')),
        'commission_percent': commission_percent,
        'commission_progress': (
            min(max(commission_percent, ZERO), Decimal('100'))
            if commission_percent is not None else None
        ),
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
            'gross_reached': bool(
                goal and metric['total_gross'] >= goal.target_total_gross
            ),
            'financial_reached': (
                bool(goal and metric['commission'] >= goal.target_commission)
                if metric['commission_complete'] else None
            ),
        })
        history.append(metric)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return {
        'history': history,
        'history_start': first,
        'history_end': end_month.replace(day=1),
    }
