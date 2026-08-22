from django.contrib import admin
from django.test import SimpleTestCase

from .models import Team, TeamInvitation, TeamMembership


class TeamAdminRegistrationTests(SimpleTestCase):
    def test_operational_team_models_are_registered_read_only(self):
        for model in (Team, TeamMembership, TeamInvitation):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_add_permission(None))
                self.assertFalse(model_admin.has_change_permission(None))
                self.assertFalse(model_admin.has_delete_permission(None))

    def test_invitation_digest_is_not_exposed_in_admin(self):
        invitation_admin = admin.site._registry[TeamInvitation]
        self.assertIn('token_digest', invitation_admin.exclude)
        self.assertNotIn('token_digest', invitation_admin.readonly_fields)
