from allauth.account.views import EmailView, SignupView

from .auth_identity import NormalizedIdentityCollision


class NormalizedSignupView(SignupView):
    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except NormalizedIdentityCollision:
            return self.form_invalid(form)


class NormalizedEmailView(EmailView):
    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except NormalizedIdentityCollision:
            return self.form_invalid(form)
