import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import UserProfile


class ProfileTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        User = get_user_model()
        self.user = User.objects.create_user('profile-owner', password='Old-pass-123!')
        self.other = User.objects.create_user('profile-other', password='Other-pass-123!')

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.media_dir, ignore_errors=True)

    def image_upload(self, name='avatar.png', size=(40, 40), image_format='PNG'):
        output = BytesIO()
        Image.new('RGB', size, '#336699').save(output, format=image_format)
        content_type = {
            'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'
        }[image_format]
        return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)

    @staticmethod
    def application_styles():
        return Path(finders.find('SalesLogApp/css/styles.css')).read_text(encoding='utf-8')

    def test_profile_created_for_new_users(self):
        self.assertTrue(UserProfile.objects.filter(user=self.user).exists())

    def test_missing_existing_profile_is_recreated_safely(self):
        UserProfile.objects.filter(user=self.user).delete()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)
        self.assertEqual(UserProfile.objects.filter(user=self.user).count(), 1)

    def test_profile_requires_login(self):
        self.assertEqual(self.client.get(reverse('profile')).status_code, 302)

    def test_blue_is_default_header_color(self):
        self.assertEqual(self.user.sales_profile.header_color, 'blue')

    def test_all_header_colors_can_be_saved_and_are_user_isolated(self):
        self.client.force_login(self.user)
        for color, _label in UserProfile.HEADER_COLOR_CHOICES:
            with self.subTest(color=color):
                response = self.client.post(reverse('profile'), {
                    'form_type': 'appearance',
                    'theme_mode': 'dark',
                    'header_color': color,
                    'user': self.other.pk,
                })
                self.assertRedirects(response, reverse('profile'))
                self.user.sales_profile.refresh_from_db()
                self.assertEqual(self.user.sales_profile.header_color, color)
                self.assertEqual(self.user.sales_profile.theme_mode, 'dark')
                self.assertEqual(self.other.sales_profile.header_color, 'blue')
        page = self.client.get(reverse('profile'))
        self.assertContains(page, 'data-theme="dark"')
        self.assertContains(page, 'header-theme-purple')
        styles = self.application_styles()
        self.assertIn('--page-background:', styles)
        self.assertIn('--graph-primary:', styles)
        self.assertIn('html[data-theme="dark"]', styles)
        self.assertContains(page, 'getStewLogChartColors')
        self.assertFalse(hasattr(self.user.sales_profile, 'background_color'))
        self.assertFalse(hasattr(self.user.sales_profile, 'graph_primary_color'))
        self.assertFalse(hasattr(self.user.sales_profile, 'graph_secondary_color'))

    def test_invalid_header_color_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('profile'), {
            'form_type': 'appearance', 'theme_mode': 'light',
            'header_color': 'not-a-real-color',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('header_color', response.context['appearance_form'].errors)
        self.user.sales_profile.refresh_from_db()
        self.assertEqual(self.user.sales_profile.header_color, 'blue')

    def test_reset_header_does_not_change_display_mode(self):
        profile = self.user.sales_profile
        profile.theme_mode = 'dark'
        profile.header_color = 'red'
        profile.save()
        self.client.force_login(self.user)
        self.client.post(reverse('profile'), {'form_type': 'reset_header_color'})
        profile.refresh_from_db()
        self.assertEqual(profile.theme_mode, 'dark')
        self.assertEqual(profile.header_color, 'blue')

    def test_palette_foregrounds_and_anonymous_default(self):
        page = self.client.get(reverse('account_login'))
        content = self.application_styles()
        self.assertContains(page, 'header-theme-blue')
        for color in ('red', 'orange', 'green', 'blue', 'gray', 'pink', 'purple'):
            self.assertRegex(
                content,
                rf'\.header-theme-{color}\s*\{{[^}}]*'
                rf'--header-foreground:\s*#ffffff;',
            )
        self.assertRegex(
            content,
            r'\.header-theme-yellow\s*\{[^}]*'
            r'--header-background:\s*#facc15;[^}]*'
            r'--header-foreground:\s*#111827;',
        )

    def test_header_and_menu_share_the_selected_palette(self):
        profile = self.user.sales_profile
        profile.header_color = 'yellow'
        profile.save()
        self.client.force_login(self.user)

        page = self.client.get(reverse('profile'))
        content = self.application_styles()

        self.assertContains(page, '<body class="header-theme-yellow">', html=False)
        self.assertIn('background: var(--header-background)', content)
        self.assertGreaterEqual(
            content.count('color: var(--header-foreground)'),
            2,
        )
        self.assertRegex(
            content,
            r'--menu-hover-background:\s*rgba\(17,\s*24,\s*39,\s*\.10\)',
        )
        self.assertRegex(
            content,
            r'--menu-active-background:\s*rgba\(17,\s*24,\s*39,\s*\.18\)',
        )
        self.assertIn('.menu a.active', content)

    def test_upload_replace_and_remove_avatar(self):
        self.client.force_login(self.user)
        self.client.post(reverse('profile'), {
            'form_type': 'avatar', 'avatar': self.image_upload(),
        })
        profile = UserProfile.objects.get(user=self.user)
        first_name = profile.avatar.name
        self.assertTrue(profile.avatar.storage.exists(first_name))
        self.client.post(reverse('profile'), {
            'form_type': 'avatar',
            'avatar': self.image_upload('replacement.webp', image_format='WEBP'),
        })
        profile.refresh_from_db()
        self.assertNotEqual(profile.avatar.name, first_name)
        self.assertFalse(profile.avatar.storage.exists(first_name))
        replacement = profile.avatar.name
        self.client.post(reverse('profile'), {'form_type': 'remove_avatar'})
        profile.refresh_from_db()
        self.assertFalse(profile.avatar)
        self.assertFalse(profile.avatar.storage.exists(replacement))

    def test_avatar_upload_has_stored_url_and_authenticated_response(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('profile'), {
            'form_type': 'avatar',
            'avatar': self.image_upload(),
        })
        self.assertRedirects(response, reverse('profile'))
        profile = UserProfile.objects.get(user=self.user)
        self.assertTrue(profile.avatar.storage.exists(profile.avatar.name))
        self.assertEqual(
            profile.avatar.url,
            f'/media/profile_avatars/{self.user.pk}/'
            f'{Path(profile.avatar.name).name}',
        )
        page = self.client.get(reverse('profile'))
        self.assertContains(page, profile.avatar.url)
        served = self.client.get(profile.avatar.url)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served['Content-Type'], 'image/png')
        self.assertTrue(b''.join(served.streaming_content).startswith(b'\x89PNG'))

    def test_missing_avatar_file_falls_back_without_broken_image(self):
        profile = self.user.sales_profile
        profile.avatar = (
            f'profile_avatars/{self.user.pk}/missing-avatar.png'
        )
        profile.save(update_fields=['avatar', 'updated_at'])
        self.client.force_login(self.user)
        page = self.client.get(reverse('profile'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, profile.avatar.url)
        self.assertContains(page, 'Default profile picture')
        self.assertEqual(self.client.get(profile.avatar.url).status_code, 404)

    def test_avatar_file_is_owner_scoped(self):
        self.client.force_login(self.user)
        self.client.post(reverse('profile'), {
            'form_type': 'avatar',
            'avatar': self.image_upload(),
        })
        profile = UserProfile.objects.get(user=self.user)
        avatar_url = profile.avatar.url
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(avatar_url).status_code, 404)
        self.assertFalse(self.other.sales_profile.avatar)

    def test_bad_and_oversized_avatars_are_rejected(self):
        self.client.force_login(self.user)
        for upload in (
            SimpleUploadedFile('bad.gif', b'GIF89a', content_type='image/gif'),
            SimpleUploadedFile(
                'huge.png', b'x' * (2 * 1024 * 1024 + 1),
                content_type='image/png',
            ),
        ):
            response = self.client.post(reverse('profile'), {
                'form_type': 'avatar', 'avatar': upload,
            })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['avatar_form'].errors)

    def test_avatar_post_cannot_target_another_user(self):
        self.client.force_login(self.user)
        self.client.post(reverse('profile'), {
            'form_type': 'avatar', 'user': self.other.pk,
            'avatar': self.image_upload(),
        })
        self.assertFalse(self.other.sales_profile.avatar)

    def test_password_change_requires_current_password_and_keeps_session(self):
        self.client.force_login(self.user)
        failed = self.client.post(reverse('profile'), {
            'form_type': 'password', 'old_password': 'wrong',
            'new_password1': 'New-secure-pass-456!',
            'new_password2': 'New-secure-pass-456!',
        })
        self.assertContains(failed, 'old password')
        success = self.client.post(reverse('profile'), {
            'form_type': 'password', 'old_password': 'Old-pass-123!',
            'new_password1': 'New-secure-pass-456!',
            'new_password2': 'New-secure-pass-456!',
        })
        self.assertRedirects(success, reverse('profile'))
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)

    def test_unusable_password_uses_secure_set_password_form(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post(reverse('profile'), {
            'form_type': 'password',
            'new_password1': 'Set-secure-pass-456!',
            'new_password2': 'Set-secure-pass-456!',
        })
        self.assertRedirects(response, reverse('profile'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Set-secure-pass-456!'))

    def test_print_and_anonymous_pages_do_not_require_profile_theme(self):
        self.client.force_login(self.user)
        self.user.sales_profile.theme_mode = 'dark'
        self.user.sales_profile.save()
        printed = self.client.get(reverse('print_sales'))
        self.assertNotContains(printed, 'data-theme="dark"')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('account_login')).status_code, 200)
