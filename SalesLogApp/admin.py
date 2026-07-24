from django.contrib import admin
from .models.sales import DailyActivity, MonthlyGoal
from .models import (
    ArchivedVehicle, UserProfile, Vehicle, VehicleMake, VehicleModel,
)

admin.site.register(DailyActivity)
admin.site.register(MonthlyGoal)
admin.site.register(UserProfile)


@admin.register(VehicleMake)
class VehicleMakeAdmin(admin.ModelAdmin):
    list_display = ('name', 'verified', 'active', 'created_by')
    list_filter = ('verified', 'active')
    search_fields = ('name', 'normalized_name')


@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'make', 'verified', 'active', 'created_by')
    list_filter = ('verified', 'active', 'make')
    search_fields = ('name', 'normalized_name', 'make__name')


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('year', 'make', 'model', 'stock_number', 'vin', 'sale')
    list_filter = ('year', 'make', 'model')
    search_fields = ('vin', 'stock_number')


@admin.register(ArchivedVehicle)
class ArchivedVehicleAdmin(admin.ModelAdmin):
    list_display = ('year', 'make_name', 'model_name', 'stock_number', 'vin')
    list_filter = ('year', 'make_name', 'model_name')
    search_fields = ('vin', 'stock_number')
