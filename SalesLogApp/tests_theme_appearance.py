import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import UserProfile


class ThemeAppearanceTests(TestCase):
    REQUIRED_THEME_TOKENS = {
        '--page-background',
        '--surface-background',
        '--elevated-surface',
        '--muted-surface',
        '--primary-text',
        '--secondary-text',
        '--muted-text',
        '--border-color',
        '--input-background',
        '--input-text',
        '--placeholder-text',
        '--information-background',
        '--information-text',
        '--warning-background',
        '--warning-text',
        '--error-background',
        '--error-text',
        '--link-color',
        '--focus-color',
        '--shadow-color',
        '--action-border',
        '--danger-border',
    }

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username='theme-owner',
            password='theme-password',
            is_staff=True,
        )
        profile = cls.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.theme_mode = UserProfile.DARK
        profile.header_color = 'purple'
        profile.save(update_fields=[
            'commission_system',
            'theme_mode',
            'header_color',
            'updated_at',
        ])

    def setUp(self):
        self.client.force_login(self.user)
        self.styles = self.read_static('SalesLogApp/css/styles.css')

    @staticmethod
    def read_static(name):
        path = finders.find(name)
        if not path:
            raise AssertionError(f'Static asset {name!r} was not found.')
        return Path(path).read_text(encoding='utf-8')

    @staticmethod
    def rule(css, selector, occurrence=0):
        pattern = re.compile(
            rf'(?m)^\s*{re.escape(selector)}\s*\{{([^{{}}]*)\}}',
        )
        matches = pattern.findall(css)
        if len(matches) <= occurrence:
            raise AssertionError(f'CSS rule {selector!r} was not found.')
        return matches[occurrence]

    @classmethod
    def properties(cls, css, selector, occurrence=0):
        return {
            name: value.strip()
            for name, value in re.findall(
                r'(--[\w-]+)\s*:\s*([^;]+);',
                cls.rule(css, selector, occurrence),
            )
        }

    @staticmethod
    def selector_declarations(css, selector):
        declarations = []
        for selector_list, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
            selectors = {
                item.strip().splitlines()[-1].strip()
                for item in selector_list.split(',')
            }
            if selector not in selectors:
                continue
            declarations.append({
                name: value.strip()
                for name, value in re.findall(
                    r'(--[\w-]+|[\w-]+)\s*:\s*([^;]+);',
                    body,
                )
            })
        return declarations

    def assert_selector_uses(self, selector, property_name, value):
        matches = self.selector_declarations(self.styles, selector)
        self.assertTrue(matches, f'CSS selector {selector!r} was not found.')
        self.assertTrue(
            any(block.get(property_name) == value for block in matches),
            f'{selector!r} does not set {property_name}: {value}.',
        )

    @staticmethod
    def relative_luminance(hex_color):
        value = hex_color.removeprefix('#')
        channels = [
            int(value[index:index + 2], 16) / 255
            for index in (0, 2, 4)
        ]
        channels = [
            channel / 12.92
            if channel <= .04045
            else ((channel + .055) / 1.055) ** 2.4
            for channel in channels
        ]
        return (
            .2126 * channels[0]
            + .7152 * channels[1]
            + .0722 * channels[2]
        )

    @classmethod
    def contrast_ratio(cls, first, second):
        first_luminance = cls.relative_luminance(first)
        second_luminance = cls.relative_luminance(second)
        lighter = max(first_luminance, second_luminance)
        darker = min(first_luminance, second_luminance)
        return (lighter + .05) / (darker + .05)

    def set_appearance(self, theme_mode, header_color='purple'):
        profile = self.user.sales_profile
        profile.theme_mode = theme_mode
        profile.header_color = header_color
        profile.save(update_fields=[
            'theme_mode',
            'header_color',
            'updated_at',
        ])

    def test_saved_dark_theme_is_rendered_at_root_and_persists_across_pages(self):
        for route in ('pay_plan_assistant', 'profile'):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<html lang="en" data-theme="dark">')
                self.assertContains(response, 'header-theme-purple')

    def test_light_and_system_modes_remain_supported(self):
        for theme_mode in (UserProfile.LIGHT, UserProfile.SYSTEM):
            with self.subTest(theme_mode=theme_mode):
                self.set_appearance(theme_mode, 'blue')
                response = self.client.get(reverse('pay_plan_assistant'))
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response,
                    f'data-theme="{theme_mode}"',
                )
                self.assertContains(response, 'header-theme-blue')

    def test_light_dark_and_system_define_complete_semantic_contract(self):
        for selector in (
            ':root',
            'html[data-theme="dark"]',
            'html[data-theme="system"]',
        ):
            with self.subTest(selector=selector):
                properties = self.properties(self.styles, selector)
                self.assertTrue(
                    self.REQUIRED_THEME_TOKENS.issubset(properties),
                    self.REQUIRED_THEME_TOKENS.difference(properties),
                )
                self.assertTrue(all(properties[token] for token in properties))

    def test_semantic_text_and_interface_pairs_meet_contrast_targets(self):
        text_pairs = (
            ('--primary-text', '--page-background'),
            ('--secondary-text', '--page-background'),
            ('--muted-text', '--page-background'),
            ('--primary-text', '--surface-background'),
            ('--input-text', '--input-background'),
            ('--placeholder-text', '--input-background'),
            ('--information-text', '--information-background'),
            ('--warning-text', '--warning-background'),
            ('--error-text', '--error-background'),
            ('--success-text', '--success-background'),
            ('--link-color', '--surface-background'),
            ('--action-text', '--action-background'),
            ('--action-text', '--action-hover-background'),
            ('--danger-text', '--danger-background'),
            ('--danger-text', '--danger-hover-background'),
            ('--selection-text', '--selection-background'),
        )
        interface_pairs = (
            ('--border-color', '--surface-background'),
            ('--focus-color', '--page-background'),
            ('--information-border', '--information-background'),
            ('--warning-border', '--warning-background'),
            ('--error-border', '--error-background'),
            ('--action-border', '--surface-background'),
            ('--danger-border', '--surface-background'),
        )
        for selector in (
            ':root',
            'html[data-theme="dark"]',
            'html[data-theme="system"]',
        ):
            palette = self.properties(self.styles, selector)
            for foreground, background in text_pairs:
                with self.subTest(
                    selector=selector,
                    foreground=foreground,
                    background=background,
                ):
                    self.assertGreaterEqual(
                        self.contrast_ratio(
                            palette[foreground],
                            palette[background],
                        ),
                        4.5,
                    )
            for foreground, background in interface_pairs:
                with self.subTest(
                    selector=selector,
                    foreground=foreground,
                    background=background,
                ):
                    self.assertGreaterEqual(
                        self.contrast_ratio(
                            palette[foreground],
                            palette[background],
                        ),
                        3,
                    )

    def test_root_page_and_reusable_surfaces_consume_theme_tokens(self):
        contracts = (
            ('html', 'min-height', '100%'),
            ('html', 'background', 'var(--page-background)'),
            ('body', 'min-height', '100vh'),
            ('body', 'background', 'var(--page-background)'),
            ('.page', 'background', 'var(--page-background)'),
            ('.page', 'color', 'var(--primary-text)'),
            ('.card', 'background', 'var(--surface-background)'),
            ('.main-content', 'background', 'var(--surface-background)'),
            ('.summary', 'background', 'var(--elevated-surface)'),
            ('.vehicle-dialog', 'background', 'var(--elevated-surface)'),
            ('.vehicle-dialog', 'color', 'var(--primary-text)'),
            ('th', 'background', 'var(--table-header-background)'),
        )
        for selector, property_name, value in contracts:
            with self.subTest(
                selector=selector,
                property_name=property_name,
            ):
                self.assert_selector_uses(selector, property_name, value)

    def test_help_panels_have_explicit_information_and_warning_colors(self):
        response = self.client.get(reverse('pay_plan_assistant'))
        self.assertContains(response, 'class="notice assistant-help-panel"', count=2)
        self.assertContains(response, 'Effective date:')
        self.assertContains(response, 'Helpful examples:')

        self.assert_selector_uses(
            '.notice',
            'border-left',
            '4px solid var(--information-border)',
        )
        self.assert_selector_uses(
            '.notice',
            'background',
            'var(--information-background)',
        )
        self.assert_selector_uses(
            '.notice',
            'color',
            'var(--information-text)',
        )
        self.assert_selector_uses(
            '.notice-warning',
            'background',
            'var(--warning-background)',
        )
        self.assert_selector_uses(
            '.notice-warning',
            'color',
            'var(--warning-text)',
        )

    def test_assistant_subtitle_controls_and_native_widgets_use_tokens(self):
        response = self.client.get(reverse('pay_plan_assistant'))
        self.assertContains(response, 'class="page pay-plan-assistant-page"')
        self.assertContains(
            response,
            'Your active plan stays unchanged until you review and confirm',
        )
        self.assertContains(response, 'name="request_text"')
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'name="confirm_retroactive"')
        self.assertContains(response, '>Review interpretation</button>')
        self.assertContains(response, '>Cancel</a>')
        self.assertContains(response, 'class="card assistant-history"')

        control_contracts = (
            ('.intro p', 'color', 'var(--muted-text)'),
            ('textarea', 'width', '100%'),
            ('textarea', 'max-width', '100%'),
            ('textarea', 'background', 'var(--input-background)'),
            ('textarea', 'color', 'var(--input-text)'),
            ('textarea', 'color-scheme', 'inherit'),
            ('textarea::placeholder', 'color', 'var(--placeholder-text)'),
            ('textarea::placeholder', 'opacity', '1'),
            ('input[type="checkbox"]', 'accent-color', 'var(--action-background)'),
            ('input[type="checkbox"]', 'color-scheme', 'inherit'),
            ('form label', 'display', 'block'),
            ('form label', 'color', 'var(--primary-text)'),
            ('.button-primary', 'border-color', 'var(--action-border)'),
            ('.button-danger', 'border-color', 'var(--danger-border)'),
        )
        for selector, property_name, value in control_contracts:
            with self.subTest(
                selector=selector,
                property_name=property_name,
            ):
                self.assert_selector_uses(selector, property_name, value)
        self.assertIn('color-scheme: dark;', self.rule(
            self.styles,
            'html[data-theme="dark"]',
        ))

    def test_focus_selection_validation_and_disabled_states_are_theme_aware(self):
        contracts = (
            (':focus-visible', 'outline', '3px solid var(--focus-color)'),
            ('::selection', 'background', 'var(--selection-background)'),
            ('.errorlist', 'color', 'var(--error-text) !important'),
            ('.button:disabled', 'background', 'var(--disabled-background)'),
            ('.status-active', 'background', 'var(--success-background)'),
            ('.status-draft', 'background', 'var(--warning-background)'),
            ('.danger-link:hover', 'color', 'var(--error-text)'),
        )
        for selector, property_name, value in contracts:
            with self.subTest(selector=selector):
                self.assert_selector_uses(selector, property_name, value)

    def test_header_palette_is_independent_from_display_mode(self):
        response = self.client.get(reverse('pay_plan_assistant'))
        self.assertContains(response, 'data-theme="dark"')
        self.assertContains(response, 'header-theme-purple')

        dark_properties = self.properties(
            self.styles,
            'html[data-theme="dark"]',
        )
        self.assertNotIn('--header-background', dark_properties)
        self.assertNotIn('--header-foreground', dark_properties)
        for color, _label in UserProfile.HEADER_COLOR_CHOICES:
            self.assertIn(f'.header-theme-{color}', self.styles)

    def test_print_report_remains_light_and_separate_from_application_theme(self):
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.LEGACY
        profile.save(update_fields=['commission_system', 'updated_at'])
        response = self.client.get(reverse('print_sales'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-theme="dark"')
        self.assertNotContains(response, 'styles.css')
        self.assertContains(response, 'print-reports.css')

        print_styles = self.read_static('print-reports.css')
        self.assertRegex(
            print_styles,
            re.compile(
                r'@media print\s*\{.*?body\s*\{[^}]*'
                r'background:\s*white;[^}]*color:\s*black;',
                re.S,
            ),
        )
        app_print_styles = self.styles.split('@media print', 1)[1]
        app_print_palette = self.selector_declarations(
            app_print_styles,
            ':root',
        )[0]
        for token in (
            '--page-background',
            '--surface-background',
            '--elevated-surface',
            '--information-background',
            '--warning-background',
            '--error-background',
            '--success-background',
            '--input-background',
            '--table-header-background',
        ):
            with self.subTest(print_token=token):
                self.assertEqual(app_print_palette[token], '#ffffff')
        for token in (
            '--primary-text',
            '--information-text',
            '--warning-text',
            '--error-text',
            '--success-text',
            '--input-text',
            '--link-color',
        ):
            with self.subTest(print_token=token):
                self.assertEqual(app_print_palette[token], '#000000')
        print_header = self.selector_declarations(
            app_print_styles,
            'body[class*="header-theme-"]',
        )[0]
        self.assertEqual(print_header['--header-background'], '#ffffff')
        self.assertEqual(print_header['--header-foreground'], '#000000')

    def test_global_theme_contract_is_not_duplicated_in_base_template(self):
        template = Path(
            settings.BASE_DIR,
            'SalesLogApp/templates/SalesLogApp/base.html',
        ).read_text(encoding='utf-8')
        assistant = Path(
            settings.BASE_DIR,
            'SalesLogApp/templates/pay_plan_assistant.html',
        ).read_text(encoding='utf-8')

        self.assertNotIn('<style', template)
        self.assertEqual(
            template.count("static 'SalesLogApp/css/styles.css'"),
            1,
        )
        self.assertIn('data-theme="{{ appearance.theme_mode }}"', template)
        self.assertNotIn('theme-dark', template)
        self.assertNotIn('dark-mode', template)
        self.assertNotIn('style=', assistant)
