import unittest

from ui_theme import apply_ui_theme
from job_search_agent import PAGE
import candidate_verification
import application_agent


class UIThemeTests(unittest.TestCase):
    def test_shared_navigation_theme_toggle_and_accessible_mode_persist(self):
        page=apply_ui_theme('<!doctype html><html><head></head><body><main>Home</main></body></html>')
        for text in ('Dashboard','My Resume','Candidate Profile','Career Preferences','Find Jobs','Applications','Settings'):
            self.assertIn(text,page)
        self.assertIn('data-bsa-theme="dark"',page)
        self.assertIn('aria-pressed',page)
        self.assertIn('localStorage.setItem(key,next)',page)
        self.assertIn('focus-visible',page)

    def test_theme_injection_is_idempotent(self):
        once=apply_ui_theme('<html><head></head><body></body></html>')
        self.assertEqual(apply_ui_theme(once),once)

    def test_profile_component_theme_selectors_do_not_skip_shared_controller(self):
        page=apply_ui_theme(candidate_verification.PROFILE_PAGE)
        self.assertIn('html[data-bsa-theme="dark"] .chip',page)
        self.assertIn('class="bsa-theme-toggle"',page)
        self.assertIn('id="bsa-theme-controller"',page)

    def test_find_jobs_import_panel_has_light_and_dark_contrast_rules(self):
        page=apply_ui_theme(PAGE)
        self.assertIn('<main class="panel">',page)
        self.assertIn('class="import-panel"',page)
        self.assertIn(':root{color-scheme:light',page)
        self.assertIn('--bsa-surface:#fff',page)
        self.assertIn('html[data-bsa-theme="dark"] main',page)
        self.assertIn('html[data-bsa-theme="dark"] label',page)
        self.assertIn('html[data-bsa-theme="dark"] input',page)
        self.assertIn('html[data-bsa-theme="dark"] .diagnostics',page)
        self.assertIn('html[data-bsa-theme="dark"] .error',page)
        self.assertIn('html[data-bsa-theme="dark"] hr',page)
        self.assertIn('id="jobUrl"',page)
        self.assertIn('id="importJob"',page)

    def test_application_compatibility_panel_uses_light_and_dark_theme_tokens(self):
        page=apply_ui_theme(application_agent.APPLICATIONS_PAGE)
        self.assertIn('class="compatibility-tool"',page)
        self.assertIn('--bsa-surface:#fff',page)
        self.assertIn('--bsa-ink:#17263b',page)
        self.assertIn('--bsa-surface:#182337',page)
        self.assertIn('--bsa-ink:#e7edf6',page)
        self.assertIn('background:var(--bsa-surface)',page)
        self.assertIn('color:var(--bsa-ink)',page)
        self.assertIn('border-color:var(--bsa-line)',page)
        self.assertIn('html[data-bsa-theme="dark"] input',page)
        self.assertIn('Official employer job URL',page)


if __name__=='__main__': unittest.main()
