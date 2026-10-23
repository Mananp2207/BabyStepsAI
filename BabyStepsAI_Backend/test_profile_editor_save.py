import tempfile
import unittest
from pathlib import Path

import main


class LegacyProfileEditorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = main.DB
        main.DB = Path(self.temp.name) / "profile.sqlite3"
        with main.connect() as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
            db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO resumes VALUES('r1',?,'r1.pdf','Resume.pdf','now',1,'')", (main.USER,))

    def tearDown(self):
        main.DB = self.old_db
        self.temp.cleanup()

    def test_profile_editor_save_is_persisted_and_readable(self):
        expected = {key: [] for key in main.PROFILE_FIELDS}
        expected["technical_skills"] = ["Python"]
        result = main.save_profile("r1", expected)
        self.assertEqual(result["status"], "user_reviewed")
        persisted = main.read_profile("r1")
        self.assertEqual(persisted["status"], "user_reviewed")
        self.assertEqual(persisted["profile"], expected)

    def test_profile_editor_ui_checks_readback_and_reports_failure(self):
        self.assertIn("Profile corrections saved and verified.", main.home())
        self.assertIn("No success confirmation was shown", main.home())
        self.assertIn("aria-live=\"polite\"", main.home())
