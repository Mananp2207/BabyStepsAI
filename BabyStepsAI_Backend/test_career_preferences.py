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


if __name__ == "__main__":
    unittest.main()
