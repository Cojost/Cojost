# sales/urls.py
from django.urls import path
from .views import view_sales, add_sale, view_commission, edit_sale, delete_sale, adjust_commission, register  
from django.views.generic.base import RedirectView
from django.contrib.auth import views as auth_views
from . import views
from . import team_views
from . import billing_views


urlpatterns = [
    path('', RedirectView.as_view(pattern_name='view_sales', permanent=False)),
    path('view_sales/', view_sales, name='view_sales'),
    path('view_sales/print/', views.print_sales, name='print_sales'),
    path('add_sale/', add_sale, name='add_sale'), 
    path('vehicle-catalog/makes/', views.vehicle_make_search, name='vehicle_make_search'),
    path('vehicle-catalog/models/', views.vehicle_model_search, name='vehicle_model_search'),
    path('view_commission/', view_commission, name='view_commission'),
    path('favicon.ico', RedirectView.as_view(url='/static/favicon.ico', permanent=True)),
    path('sales/edit_sale/<int:sale_id>/', edit_sale, name='edit_sale'),
    path('delete_sale/<int:sale_id>/', delete_sale, name='delete_sale'),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('adjust_commission/', adjust_commission, name='adjust_commission'),
    path('adjust_commission/<int:commission_id>/', adjust_commission, name='adjust_commission_by_id'),
    path('register/', register, name='register'),
    path('pay-plan/setup/', views.pay_plan_setup, name='pay_plan_setup'),
    path('pay-plan/setup/review/', views.pay_plan_review, name='pay_plan_review'),
    path('commission/pay-plan/', views.my_pay_plan, name='my_pay_plan'),
    path('commission/pay-plan/replace/', views.replace_pay_plan, name='replace_pay_plan'),
    path('commission/pay-plan/reload/', views.reload_pay_plan, name='reload_pay_plan'),
    path(
        'commission/pay-plan/edit/',
        views.edit_pay_plan_manually,
        name='edit_pay_plan_manually',
    ),
    path(
        'commission/ask-stew/',
        views.ask_stew_ai,
        name='ask_stew_ai',
    ),
    path(
        'commission/pay-plan/assistant/',
        views.pay_plan_assistant,
        name='pay_plan_assistant',
    ),
    path(
        'commission/pay-plan/review/<int:version_id>/',
        views.replacement_pay_plan_review,
        name='replacement_pay_plan_review',
    ),
    path(
        'commission/pay-plan/review/<int:version_id>/activate/',
        views.confirm_pay_plan_activation,
        name='confirm_pay_plan_activation',
    ),
    path('commission/pay-plan/history/', views.pay_plan_history, name='pay_plan_history'),
    path(
        'commission/pay-plan/rules/<int:version_id>/',
        views.pay_plan_rules,
        name='pay_plan_rules',
    ),
    path(
        'commission/pay-plan/rules/<int:version_id>/<int:rule_id>/edit/',
        views.edit_pay_plan_rule,
        name='edit_pay_plan_rule',
    ),
    path(
        'commission/pay-plan/recalculate/',
        views.recalculate_pay_plan_commissions,
        name='recalculate_pay_plan_commissions',
    ),
    path(
        'commission/pay-plan/eligibility/',
        views.pay_plan_eligibility,
        name='pay_plan_eligibility',
    ),
    path(
        'commission/sandbox/',
        views.commission_sandbox_index,
        name='commission_sandbox_index',
    ),
    path(
        'commission/sandbox/compare/',
        views.commission_sandbox_compare,
        name='commission_sandbox_compare',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/',
        views.commission_sandbox_detail,
        name='commission_sandbox_detail',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/save/',
        views.commission_sandbox_save,
        name='commission_sandbox_save',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/save-as/',
        views.commission_sandbox_save_as,
        name='commission_sandbox_save_as',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/duplicate/',
        views.commission_sandbox_duplicate,
        name='commission_sandbox_duplicate',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/rename/',
        views.commission_sandbox_rename,
        name='commission_sandbox_rename',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/rules/add/',
        views.commission_sandbox_rule,
        name='commission_sandbox_rule_add',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/rules/<int:rule_id>/edit/',
        views.commission_sandbox_rule,
        name='commission_sandbox_rule_edit',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/rules/<int:rule_id>/<str:action>/',
        views.commission_sandbox_rule_action,
        name='commission_sandbox_rule_action',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/replay/',
        views.commission_sandbox_replay,
        name='commission_sandbox_replay',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/hypothetical/',
        views.commission_sandbox_hypothetical,
        name='commission_sandbox_hypothetical',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/hypothetical/<int:hypothetical_id>/edit/',
        views.commission_sandbox_hypothetical_edit,
        name='commission_sandbox_hypothetical_edit',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/hypothetical/<int:hypothetical_id>/delete/',
        views.commission_sandbox_hypothetical_delete,
        name='commission_sandbox_hypothetical_delete',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/project/',
        views.commission_sandbox_project,
        name='commission_sandbox_project',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/activate/',
        views.commission_sandbox_activate,
        name='commission_sandbox_activate',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/archive/',
        views.commission_sandbox_archive,
        name='commission_sandbox_archive',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/restore/',
        views.commission_sandbox_restore,
        name='commission_sandbox_restore',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/recalculate/',
        views.commission_sandbox_recalculate,
        name='commission_sandbox_recalculate',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/reset/',
        views.commission_sandbox_reset,
        name='commission_sandbox_reset',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/convert/',
        views.commission_sandbox_convert,
        name='commission_sandbox_convert',
    ),
    path(
        'commission/sandbox/<uuid:sandbox_id>/delete/',
        views.commission_sandbox_delete,
        name='commission_sandbox_delete',
    ),
    path('edit_bonus/', register, name='edit_bonus'),
    path('add_bonus/', views.add_bonus, name='add_bonus'),
    path('activity-goals/', views.activity_goals, name='activity_goals'),
    path('stew-nudges/dismiss/', views.dismiss_stew_nudge, name='dismiss_stew_nudge'),
    path('activity-goals/print/', views.print_activity_goals, name='print_activity_goals'),
    path('activity-goals/history/print/', views.print_activity_history, name='print_activity_history'),
    path('profile/', views.profile, name='profile'),
    path('activity-goals/activity/<int:activity_id>/', views.activity_goals, name='edit_activity'),
    path('billing/', billing_views.billing_overview, name='billing_overview'),
    path('billing/checkout/start/', billing_views.billing_checkout_start, name='billing_checkout_start'),
    path('billing/checkout/success/', billing_views.billing_checkout_success, name='billing_checkout_success'),
    path('billing/checkout/cancel/', billing_views.billing_checkout_cancel, name='billing_checkout_cancel'),
    path('billing/founder/redeem/', billing_views.billing_founder_redeem, name='billing_founder_redeem'),
    path('billing/portal/', billing_views.billing_portal, name='billing_portal'),
    path('teams/', team_views.team_home, name='team_home'),
    path('teams/create/', team_views.team_create, name='team_create'),
    path('teams/invitations/accept/', team_views.team_invitation_accept, name='team_invitation_accept'),
    path('teams/<uuid:team_id>/', team_views.team_detail, name='team_detail'),
    path('teams/<uuid:team_id>/settings/', team_views.team_settings, name='team_settings'),
    path('teams/<uuid:team_id>/invitations/', team_views.team_invite, name='team_invite'),
    path('teams/<uuid:team_id>/invitations/<uuid:invitation_id>/revoke/', team_views.team_invitation_revoke, name='team_invitation_revoke'),
    path('teams/<uuid:team_id>/sharing/', team_views.team_sharing, name='team_sharing'),
    path('teams/<uuid:team_id>/activity/<uuid:activity_id>/comments/', team_views.team_comment_add, name='team_comment_add'),
    path('teams/<uuid:team_id>/activity/<uuid:activity_id>/comments/<uuid:comment_id>/edit/', team_views.team_comment_edit, name='team_comment_edit'),
    path('teams/<uuid:team_id>/activity/<uuid:activity_id>/comments/<uuid:comment_id>/delete/', team_views.team_comment_delete, name='team_comment_delete'),
    path('teams/<uuid:team_id>/activity/<uuid:activity_id>/comments/<uuid:comment_id>/hide/', team_views.team_comment_hide, name='team_comment_hide'),
    path('teams/<uuid:team_id>/activity/<uuid:activity_id>/reactions/', team_views.team_reaction_toggle, name='team_reaction_toggle'),
    path('teams/<uuid:team_id>/members/<uuid:membership_id>/remove/', team_views.team_member_remove, name='team_member_remove'),
    path('teams/<uuid:team_id>/members/<uuid:membership_id>/transfer/', team_views.team_transfer_ownership, name='team_transfer_ownership'),
    path('teams/<uuid:team_id>/members/<uuid:membership_id>/<str:action>/', team_views.team_member_role, name='team_member_role'),
    path('teams/<uuid:team_id>/leave/', team_views.team_leave, name='team_leave'),
    path('teams/<uuid:team_id>/deactivate/', team_views.team_deactivate, name='team_deactivate'),
]


 




