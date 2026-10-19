import unittest

from ui_theme import apply_ui_theme


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


if __name__=='__main__': unittest.main()
