from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from .access import get_commission_system


ZERO = Decimal('0')
ARCHIVE_SNAPSHOT_UNAVAILABLE = 'archive_snapshot_unavailable'
HISTORICAL_PLAN_INCOMPLETE = 'historical_pay_plan_incomplete'
HISTORICAL_PLAN = 'historical_pay_plan'

_ARCHIVE_FIELD_REQUIREMENTS = {
    'vehicle_condition',
    'acquisition_source',
    'make',
    'model',
    'year',
    'mileage',
    'is_cpo',
    'deal_credit',
}
_ELIGIBILITY_REQUIREMENTS = {
    'green_pea',
    'nps_finance_eligible',
    'ar_requirement_met',
    'training_requirements_met',
    'call_requirement_met',
    'video_requirement_met',
    'nps_bonus_eligible',
    'nps_qualifying_surveys',
    'nps_low_score_surveys',
    'holiday_bonus_eligible',
    'holiday_bonus_forfeited',
}


@dataclass(frozen=True)
class ArchivedSaleAggregationAdapter:
    """Expose only the sale-compatible facts preserved by an archive row."""

    source: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    @property
    def id(self) -> int:
        # Live and archive primary keys use independent sequences. A negative
        # reporting-only identifier prevents diagnostic map collisions.
        return -self.source.pk

    @property
    def pk(self) -> int:
        return self.id

    @property
    def unit_credit(self) -> Decimal:
        return Decimal(str(self.source.count or 0))

    @property
    def commission_credit_multiplier(self) -> Decimal:
        if self.unit_credit == Decimal('0.5'):
            return Decimal('0.5')
        return Decimal('1.0')

    def _vehicle_value(self, field_name: str) -> Any:
        try:
            return getattr(self.source.vehicle, field_name)
        except (AttributeError, ObjectDoesNotExist):
            return None

    @property
    def make(self) -> Any:
        return self._vehicle_value('make_name')

    @property
    def model(self) -> Any:
        return self._vehicle_value('model_name')

    @property
    def year(self) -> Any:
        return self._vehicle_value('year')

    @property
    def mileage(self) -> Any:
        return self._vehicle_value('mileage')

    @property
    def is_cpo(self) -> None:
        return None

    @property
    def deal_credit(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class OwnedSaleRecordSet:
    """A bounded, owner-scoped set of live and archived sale records."""

    records: tuple[Any, ...]
    duplicate_archive_count: int


def load_owned_sale_records(
    user: Any,
    *,
    start_date: date,
    end_date: date,
) -> OwnedSaleRecordSet:
    """Load a half-open owner/date range and apply the archive identity policy."""

    from .models import ArchivedSale, Sale

    live_sales = list(
        Sale.objects.filter(
            user=user,
            date__gte=start_date,
            date__lt=end_date,
        )
    )
    archived_rows = list(
        ArchivedSale.objects.filter(
            user=user,
            date__gte=start_date,
            date__lt=end_date,
        ).select_related('vehicle')
    )
    live_identities = {
        (sale.user_id, sale.sale_type, sale.dealNumber)
        for sale in live_sales
    }
    retained_archives = [
        sale for sale in archived_rows
        if (sale.user_id, sale.sale_type, sale.dealNumber) not in live_identities
    ]
    return OwnedSaleRecordSet(
        records=tuple(live_sales) + tuple(
            ArchivedSaleAggregationAdapter(sale) for sale in retained_archives
        ),
        duplicate_archive_count=len(archived_rows) - len(retained_archives),
    )


def _incomplete(units: Decimal, status: str, message: str) -> dict[str, Any]:
    return {
        'units': units,
        'front': None,
        'back': None,
        'bonus': None,
        'adjustments': None,
        'total': None,
        'diagnostics': None,
        'commission_complete': False,
        'commission_source': status,
        'commission_diagnostic': message,
    }


def _required_fields(version: Any) -> set[str]:
    fields = set(
        version.rules.filter(is_active=True).values_list(
            'conditions__field_name', flat=True,
        )
    )
    for configuration in version.rules.filter(is_active=True).values_list(
        'configuration', flat=True,
    ):
        configuration = configuration or {}
        for key in ('qualifying_count_field', 'low_score_count_field'):
            value = configuration.get(key)
            if value:
                fields.add(value)
    fields.discard(None)
    return fields


def _has_required_archive_inputs(
    user: Any,
    sale: ArchivedSaleAggregationAdapter,
    version: Any,
) -> bool:
    from .models import PayPlanEligibility

    if (
        version.activated_at is None
        or version.activated_at.date() > sale.archived_on
        or version.rules.filter(
            is_active=True, updated_at__date__gt=sale.archived_on,
        ).exists()
    ):
        return False
    required = _required_fields(version)
    for field_name in required & _ARCHIVE_FIELD_REQUIREMENTS:
        if getattr(sale, field_name, None) in (None, ''):
            return False
    required_eligibility = required & _ELIGIBILITY_REQUIREMENTS
    if required_eligibility:
        eligibility = PayPlanEligibility.objects.filter(
            user=user,
            month_start=sale.date.replace(day=1),
        ).first()
        if eligibility is None:
            return False
        nullable_values = {
            'green_pea': eligibility.green_pea,
            'nps_finance_eligible': eligibility.nps_finance_eligible,
            'ar_requirement_met': eligibility.ar_requirement_met,
            'training_requirements_met': eligibility.training_requirements_met,
            'call_requirement_met': eligibility.call_requirement_met,
            'video_requirement_met': eligibility.video_requirement_met,
            'holiday_bonus_eligible': eligibility.holiday_bonus_eligible,
        }
        if any(
            nullable_values.get(field_name) is None
            for field_name in required_eligibility & nullable_values.keys()
        ):
            return False
        if (
            'nps_bonus_eligible' in required_eligibility
            and eligibility.nps_status == eligibility.NPS_PENDING
        ):
            return False
    return True


def archived_month_commission_totals(
    user: Any,
    sales: list[Any],
) -> dict[str, Any]:
    """Calculate a mixed live/archive month without inventing archive payout."""

    from .commission_engine.engine import (
        calculate_period_commission,
        resolve_pay_plan_version,
    )
    from .commission_engine.exceptions import CommissionEngineError
    from .commission_service import CommissionEngineService
    from .models import UserProfile

    sales = list(sales)
    units = sum((Decimal(str(sale.unit_credit)) for sale in sales), ZERO)
    archived = [
        sale for sale in sales
        if isinstance(sale, ArchivedSaleAggregationAdapter)
    ]
    if not archived:
        raise ValueError('Archived month aggregation requires an archive record.')

    if get_commission_system(user) != UserProfile.PAY_PLAN_V2:
        return _incomplete(
            units,
            ARCHIVE_SNAPSHOT_UNAVAILABLE,
            'Archived commission is unavailable because archived sales do not '
            'store a commission snapshot.',
        )

    try:
        for sale in archived:
            version = resolve_pay_plan_version(user, sale.date)
            if not _has_required_archive_inputs(user, sale, version):
                return _incomplete(
                    units,
                    HISTORICAL_PLAN_INCOMPLETE,
                    'Archived commission is unavailable because a complete '
                    'historical pay-plan calculation source could not be verified.',
                )
        # This strict preflight verifies the effective-dated period source and
        # all owner checks before the reporting service consumes any totals.
        calculate_period_commission(
            user,
            sales,
            min(sale.date for sale in sales),
            max(sale.date for sale in sales),
        )
    except (CommissionEngineError, ObjectDoesNotExist, TypeError, ValueError):
        return _incomplete(
            units,
            HISTORICAL_PLAN_INCOMPLETE,
            'Archived commission is unavailable because a complete historical '
            'pay-plan calculation source could not be verified.',
        )

    try:
        diagnostics = CommissionEngineService.calculate_sales(
            user,
            sales,
            allow_historical_versions=True,
        )
    except (
        CommissionEngineError,
        ObjectDoesNotExist,
        AttributeError,
        TypeError,
        ValueError,
    ):
        return _incomplete(
            units,
            HISTORICAL_PLAN_INCOMPLETE,
            'Archived commission is unavailable because a complete historical '
            'pay-plan calculation source could not be verified.',
        )
    if diagnostics['excluded_count'] or diagnostics['partial_count']:
        return _incomplete(
            units,
            HISTORICAL_PLAN_INCOMPLETE,
            'Archived commission is unavailable because a complete historical '
            'pay-plan calculation source could not be verified.',
        )
    return {
        'units': units,
        'front': diagnostics['total_front'],
        'back': diagnostics['total_back'],
        'bonus': diagnostics['total_bonus'],
        'adjustments': ZERO,
        'total': diagnostics['total_commission'],
        'diagnostics': diagnostics,
        'commission_complete': True,
        'commission_source': HISTORICAL_PLAN,
        'commission_diagnostic': '',
    }
