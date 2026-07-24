from django.dispatch import receiver
from allauth.account.signals import user_signed_up
from django.shortcuts import redirect
from django.urls import reverse
from .models import Commission 
from .models import UserProfile
from django.conf import settings
from django.db.models.signals import post_save


@receiver(user_signed_up)
def redirect_to_commission_setup(request, user, **kwargs):
    # Create a commission entry for the newly registered user, if not already present
    commission, created = Commission.objects.get_or_create(user=user)
    
    # Redirect to the adjust commission page with the `commission_id`
    return redirect(reverse('adjust_commission_by_id', kwargs={'commission_id': commission.id}))


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_sales_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)
