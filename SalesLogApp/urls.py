# sales/urls.py
from django.urls import path
from .views import view_sales, add_sale, view_commission, edit_sale, delete_sale, adjust_commission, register  
from django.views.generic.base import RedirectView
from django.contrib.auth import views as auth_views
from . import views


urlpatterns = [
    path('view_sales/', view_sales, name='view_sales'),
    path('add_sale/', add_sale, name='add_sale'), 
    path('view_commission/', view_commission, name='view_commission'),
    path('favicon.ico', RedirectView.as_view(url='/static/favicon.ico', permanent=True)),
    path('sales/edit_sale/<int:sale_id>/', edit_sale, name='edit_sale'),
    path('delete_sale/<int:sale_id>/', delete_sale, name='delete_sale'),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('adjust_commission/', adjust_commission, name='adjust_commission'),
    path('adjust_commission/<int:commission_id>/', adjust_commission, name='adjust_commission_by_id'),
    path('register/', register, name='register'),
    path('edit_bonus/', register, name='edit_bonus'),
    path('add_bonus/', views.add_bonus, name='add_bonus'),
]


 




