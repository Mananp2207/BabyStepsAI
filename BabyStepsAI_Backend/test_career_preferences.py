import json
import tempfile
import unittest
from pathlib import Path

import career_preferences as prefs


class CareerPreferenceSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = prefs.DB
        prefs.DB = Path(self.temp.name) / "career.sqlite3"
        with prefs.connect() as db:
            db.execute("CREATE TABLE career_preferences(user_id TEXT PRIMARY KEY, preferences_json TEXT NOT NULL, preference_version INTEGER NOT NULL DEFAULT 1, preferences_fingerprint TEXT NOT NULL DEFAULT '')")

    def tearDown(self):
        prefs.DB = self.old_db
        self.temp.cleanup()

    def preferences(self, **overrides):
        values = dict(categories=[], roles=[], levels=[], employment_types=[], location="", work_arrangement="Any", instructions="", max_applications_per_day=20)
        values.update(overrides)
        return prefs.Preferences(**values)

    def test_empty_optional_preferences_save_and_read_back(self):
        result = prefs.save_preferences(self.preferences())
        self.assertTrue(result["saved"])
        saved = prefs.get_preferences()
        self.assertEqual(saved["categories"], [])
        self.assertEqual(saved["roles"], [])
        self.assertEqual(saved["location"], "")
        self.assertEqual(saved["work_arrangement"], "Any")

    def test_failed_preference_validation_does_not_replace_persisted_values(self):
        prefs.save_preferences(self.preferences(location="United States"))
        with self.assertRaises(Exception):
            prefs.save_preferences(self.preferences(work_arrangement="Office-only"))
        self.assertEqual(prefs.get_preferences()["location"], "United States")
        self.assertEqual(prefs.get_preferences()["work_arrangement"], "Any")

    def test_preferences_page_verifies_persistence_and_marks_status_accessibly(self):
        page = prefs.PAGE
        self.assertIn("Preferences saved and verified", page)
        self.assertIn("aria-live=\"polite\"", page)
        self.assertIn(".invalid-field", page)
        self.assertIn("aria-invalid", page)
        self.assertIn("Unset preferences remain optional", page)
        self.assertIn("No success confirmation was shown", page)
        self.assertIn("Save failed:", page)
        self.assertIn('document.createElement("datalist")',page)

    def test_career_preferences_uses_shared_light_and_dark_theme_tokens(self):
        from ui_theme import apply_ui_theme
        themed=apply_ui_theme(prefs.PAGE)
        for token in ("--bsa-surface", "--cp-surface", "--cp-ink", "--cp-muted", "--cp-line"):
            self.assertIn(token,themed)
        self.assertIn('html[data-bsa-theme="dark"] section',themed)
        self.assertIn(".chip.active",themed)
        self.assertIn('aria-pressed',themed)
        self.assertIn("@media(max-width:640px)",themed)
        self.assertIn("#add-destination",themed)

    def test_relocation_destinations_are_conditional_but_retained_for_later_selection(self):
        page=prefs.PAGE
        self.assertIn('<div id="relocation-destinations" hidden>',page)
        self.assertIn('id="add-destination" type="button" hidden',page)
        self.assertIn('const active=$("relocation").value==="selected_locations"',page)
        self.assertIn('root.hidden=!active',page)
        self.assertIn('$("add-destination").hidden=!active',page)
        self.assertIn('relocationDestinations=Array.isArray(preferences.relocation_destinations)?preferences.relocation_destinations.map(x=>({...x})):[]',page)
        self.assertIn('relocation_destinations:relocationDestinations.filter',page)

    def test_relocation_destinations_are_conservatively_normalized_and_verified(self):
        result=prefs.save_preferences(self.preferences(
            relocation_preference="selected_locations",
            relocation_destinations=[{"country":"usa","state_province":"CA","city":" San Francisco "},
                                     {"country":"USA","state_province":"ca","city":"San Francisco"},
                                     {"country":"Canada","state_province":"Ontario","city":"Toronto"}]))
        self.assertTrue(result["saved"])
        saved=prefs.get_preferences()
        self.assertEqual(saved["relocation_preference"],"selected_locations")
        self.assertEqual(saved["relocation_destinations"],[
            {"country":"United States","state_province":"California","city":"San Francisco"},
            {"country":"Canada","state_province":"Ontario","city":"Toronto"}])

    def test_relocation_mode_rejects_unrecognized_values_without_overwriting(self):
        from fastapi import HTTPException
        prefs.save_preferences(self.preferences(relocation_preference="anywhere"))
        with self.assertRaises(HTTPException):
            prefs.save_preferences(self.preferences(relocation_preference="probably"))
        self.assertEqual(prefs.get_preferences()["relocation_preference"],"anywhere")

    def test_onboarding_and_preferences_expose_distinct_accessible_relocation_choices(self):
        import onboarding
        for page in (prefs.PAGE,onboarding.ONBOARDING_PAGE):
            for value in ("anywhere","selected_locations","per_job"):
                self.assertIn(value,page)
        self.assertIn("Current residence is saved separately",onboarding.ONBOARDING_PAGE)


if __name__ == "__main__":
    unittest.main()
