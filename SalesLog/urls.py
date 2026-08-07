"""
URL configuration for SalesLog project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic.base import RedirectView
from SalesLogApp import views as sales_views


_avatar_route = (
    f"{settings.MEDIA_URL.lstrip('/')}"
    'profile_avatars/<int:user_id>/<str:filename>'
)

urlpatterns = [
    path('', RedirectView.as_view(url='/SalesLogApp/', permanent=False)),
    path('admin/', admin.site.urls),
    path('SalesLogApp/', include('SalesLogApp.urls')),
    path('accounts/', include('allauth.urls')),
    path(
        _avatar_route,
        sales_views.profile_avatar_file,
        name='profile_avatar_file',
    ),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
