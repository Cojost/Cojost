# SalesLogApp/templatetags/commission_filters.py
from django import template
from decimal import Decimal

register = template.Library()

@register.filter
def calculate_front_end(front_end_value, commission_instance):
    return commission_instance.calculate_front_end(front_end_value)

@register.filter
def calculate_backend(back_end_value, commission_instance):
    return commission_instance.calculate_backend(back_end_value)
