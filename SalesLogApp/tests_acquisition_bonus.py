from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from .commission_service import CommissionEngineService
from .models import PayPlanRule, Sale, UserProfile


class AcquisitionBonusTests(TestCase):
    def test_eligible_acquisition_is_exclusive_flat_350(self):
        user = get_user_model().objects.create_user(username='acquisition-owner')
        user.sales_profile.commission_system = UserProfile.PAY_PLAN_V2
        user.sales_profile.save()
        version = user.pay_plan_assignments.get().pay_plan_version
        version.rules.all().delete()
        excluded_sources = ['street_curb', 'current_service_customer']
        for order, (name, rule_type, config) in enumerate([
            ('Front 25%', 'front_gross_percentage', {
                'rate': '0.25', 'gross_field': 'front_end_gross',
            }),
            ('Minimum $250', 'minimum_commission', {
                'minimum_amount': '250', 'applies_to_categories': ['front_end'],
            }),
        ], start=1):
            rule = PayPlanRule.objects.create(
                pay_plan_version=version, name=name, rule_type=rule_type,
                calculation_scope='per_sale', configuration=config,
                sort_order=order,
            )
            rule.conditions.create(
                field_name='acquisition_source', operator='not_in',
                value=excluded_sources,
            )
        acquisition = PayPlanRule.objects.create(
            pay_plan_version=version, name='Acquisition $350',
            rule_type='acquisition_bonus', calculation_scope='per_sale',
            configuration={'amount': '350'}, sort_order=3,
        )
        acquisition.conditions.create(
            field_name='acquisition_source', operator='in',
            value=excluded_sources,
        )
        sale = Sale.objects.create(
            user=user, customer='Acquired Vehicle', dealNumber=909090,
            count='1.0', frontEnd='1', backend='1',
            date=version.effective_start_date,
            vehicle_condition='used', acquisition_source='street_curb',
        )

        result = CommissionEngineService.calculate_sale(user, sale)

        self.assertEqual(result.total_commission, 350)
        self.assertEqual(result.bonus_commission, 350)
        self.assertEqual(result.acquisition_bonus, 350)
        self.assertEqual(result.frontend_commission, 0)
        self.assertEqual(result.backend_commission, 0)
        self.assertEqual(result.status, 'calculated')
