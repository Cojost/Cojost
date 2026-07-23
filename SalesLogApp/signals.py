from django.dispatch import receiver
from allauth.account.signals import user_signed_up
from django.shortcuts import redirect
from django.urls import reverse
from .models import Commission 


@receiver(user_signed_up)
def redirect_to_commission_setup(request, user, **kwargs):
    # Create a commission entry for the newly registered user, if not already present
    commission, created = Commission.objects.get_or_create(user=user)
    
    # Redirect to the adjust commission page with the `commission_id`
    return redirect(reverse('adjust_commission_by_id', kwargs={'commission_id': commission.id}))
