import json
import sqlite3
import tempfile
import unittest
import asyncio
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import candidate_verification as profile
from job_search_agent import USER


class CandidateVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.db"
        self.old_db = profile.DB
        profile.DB = self.db
        with profile.connect() as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
            db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO resumes VALUES('r1',?,'r1.pdf','Original.pdf','2026-01-01',1,'')", (USER,))
            db.execute("INSERT INTO resume_profiles VALUES('r1','completed_unverified',?,'', '2026-01-01')", (json.dumps({"technical_skills": ["Python"], "education": [{"degree": "B.S.", "institution": "Example University"}]}),))
        profile.initialize()

    def tearDown(self):
        profile.DB = self.old_db
        self.temp.cleanup()

    def test_resume_facts_are_suggestions_until_each_field_is_confirmed(self):
        data = profile.get_verification()
        skills = next(x for x in data["fields"] if x["key"] == "technical_skills")
        self.assertEqual(skills["value"], ["Python"])
        self.assertFalse(skills["confirmed"])
        self.assertEqual(skills["source"], "resume_suggestion")

    def test_individual_fields_and_sensitive_answers_require_explicit_confirmation(self):
        result = profile.update_verification(profile.VerificationUpdate(
            values={"full_name": "A Candidate", "technical_skills": ["Python", "SQL"],
                    "work_authorization": "United States"},
            confirmed_fields=["full_name", "technical_skills"]
        ))
        self.assertEqual(result["confirmed_fields"], 2)
        _, facts, answers = self._app_candidate()
        self.assertEqual(facts["full_name"], "A Candidate")
        self.assertEqual(facts["technical_skills"], ["Python", "SQL"])
        self.assertNotIn("work_authorization", facts)
        self.assertNotIn("work_authorization", answers)

    def test_graduation_month_year_saves_loads_changes_and_can_be_cleared(self):
        profile.update_verification(profile.VerificationUpdate(
            values={"graduation_date":"2026-12"}, confirmed_fields=["graduation_date"]))
        data=profile.get_verification(); date=next(x for x in data["fields"] if x["key"]=="graduation_date")
        self.assertEqual(date["value"],"2026-12"); self.assertTrue(date["confirmed"])
        _,facts,_=self._app_candidate()
        self.assertEqual(facts["graduation_year"],"2026"); self.assertEqual(facts["graduation_month"],"12")
        profile.update_verification(profile.VerificationUpdate(values={"graduation_date":"2027-05"},confirmed_fields=["graduation_date"]))
        self.assertEqual(next(x for x in profile.get_verification()["fields"] if x["key"]=="graduation_date")["value"],"2027-05")
        profile.update_verification(profile.VerificationUpdate(values={"graduation_date":""},confirmed_fields=[]))
        date=next(x for x in profile.get_verification()["fields"] if x["key"]=="graduation_date")
        self.assertEqual(date["value"],""); self.assertFalse(date["confirmed"])

    def test_picker_is_month_only_accessible_and_formats_month_year(self):
        data=profile.get_verification()
        field=next(x for x in data["fields"] if x["key"]=="graduation_date")
        self.assertEqual(field["kind"],"month")
        page=profile.PROFILE_PAGE
        self.assertIn("type==='month'?'month':type",page)
        self.assertIn("input.type!=='month'",page)
        self.assertIn("inputMode='numeric'",page)
        self.assertIn("month:'long',year:'numeric'",page)

    def test_legacy_separate_month_year_verification_migrates_for_picker(self):
        with profile.connect() as db:
            db.execute("INSERT INTO candidate_verifications VALUES('r1',?,?,?,?)",(USER,json.dumps({"graduation_month":"12","graduation_year":"2026"}),json.dumps({"graduation_month":True,"graduation_year":True}),"2026-03-01"))
        date=next(x for x in profile.get_verification()["fields"] if x["key"]=="graduation_date")
        self.assertEqual(date["value"],"2026-12"); self.assertTrue(date["confirmed"])

    def test_invalid_month_format_is_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            profile.update_verification(profile.VerificationUpdate(values={"graduation_date":"2026-13"}))

    def test_structured_education_experience_internships_projects_and_skills(self):
        profile.update_verification(profile.VerificationUpdate(values={
            "education_entries":[{"institution":"Example U","degree":"B.S.","major":"Computer Science","graduation_date":"2026-12","gpa":"3.8","currently_enrolled":False}],
            "experience_entries":[{"job_title":"Developer Intern","employer":"Acme","location":"Austin","employment_type":"Internship","start_date":"2025-05","end_date":"2025-08","currently_working":False,"responsibilities":["Built APIs"],"technologies":["Python"]}],
            "internship_entries":[{"job_title":"Research Intern","employer":"Lab","start_date":"2024-06","end_date":"2024-08","currently_working":False,"responsibilities":["Evaluated models"],"technologies":["PyTorch"]}],
            "project_entries":[{"name":"Portfolio App","description":"Personal project","technologies":["React"],"github_url":"https://github.com/example/app","demo_url":"https://example.test","start_date":"2024-01","end_date":"2024-03","accomplishments":["Shipped demo"]}],
            "skills":["Python","Custom Tool"],"location":"Austin, TX"},
            confirmed_fields=["education_entries:0","experience_entries:0","internship_entries:0","project_entries:0","skills","location"]))
        _,facts,_=self._app_candidate()
        self.assertEqual(facts["education"][0]["graduation_date"],"2026-12")
        self.assertEqual(facts["work_experience"][0]["employer"],"Acme")
        self.assertEqual(facts["internships"][0]["employer"],"Lab")
        self.assertEqual(facts["projects"][0]["name"],"Portfolio App")
        self.assertEqual(facts["technical_skills"],["Python","Custom Tool"])
        self.assertEqual(facts["current_location"],"Austin, TX")

    def test_structured_dates_reject_invalid_ranges_and_bad_months(self):
        from fastapi import HTTPException
        base={"job_title":"Engineer","employer":"Example","start_date":"2025-10","end_date":"2025-02","currently_working":False}
        with self.assertRaises(HTTPException): profile.update_verification(profile.VerificationUpdate(values={"experience_entries":[base]}))
        base["end_date"]="2025-13"
        with self.assertRaises(HTTPException): profile.update_verification(profile.VerificationUpdate(values={"experience_entries":[base]}))

    def test_resume_suggestions_are_structured_and_unconfirmed(self):
        with profile.connect() as db:
            db.execute("UPDATE resume_profiles SET profile_json=? WHERE resume_id='r1'",(json.dumps({
                "education":[{"institution":"Example U","degree":"B.S.","major":"CS","graduation_year":"2026"}],
                "work_experience":[{"position":"Developer","institution":"Example Co","start_year":"2024","responsibilities":["Built tools"]}],
                "projects":[{"name":"Demo","description":"Created a demo","technologies":["Go"]}],"technical_skills":["Go"]}),))
        data=profile.get_verification(); by_key={x["key"]:x for x in data["fields"]}
        self.assertEqual(by_key["education_entries"]["value"][0]["institution"],"Example U")
        self.assertEqual(by_key["experience_entries"]["value"][0]["job_title"],"Developer")
        self.assertEqual(by_key["project_entries"]["value"][0]["name"],"Demo")
        self.assertFalse(by_key["education_entries"]["entry_confirmed"][0])

    def test_custom_confirmed_answer_can_be_edited_and_revoked(self):
        profile.save_application_answer("work-sample", profile.AnswerUpdate(
            question="Describe your preferred work sample", answer="A small tested project.", confirmed=True
        ))
        entries = profile.list_answers()
        self.assertEqual(entries[0]["answer"], "A small tested project.")
        profile.save_application_answer("work-sample", profile.AnswerUpdate(
            question="Describe your preferred work sample", answer="Updated answer.", confirmed=True
        ))
        self.assertEqual(profile.list_answers()[0]["answer"], "Updated answer.")
        profile.revoke_application_answer("work-sample")
        self.assertEqual(profile.list_answers(), [])

    def test_new_resume_version_does_not_inherit_previous_confirmation(self):
        profile.update_verification(profile.VerificationUpdate(values={"full_name": "Confirmed Name"}, confirmed_fields=["full_name"]))
        with profile.connect() as db:
            db.execute("UPDATE resumes SET active=0 WHERE id='r1'")
            db.execute("INSERT INTO resumes VALUES('r2',?,'r2.pdf','New.pdf','2026-02-01',1,'')", (USER,))
            db.execute("INSERT INTO resume_profiles VALUES('r2','completed_unverified','{}',NULL,'2026-02-01')")
        data = profile.get_verification()
        full_name = next(x for x in data["fields"] if x["key"] == "full_name")
        self.assertFalse(full_name["confirmed"])
        self.assertEqual(full_name["value"], "")
        with profile.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_verifications WHERE resume_id='r1'").fetchone()[0], 1)

    def test_resume_replacement_keeps_previous_file_and_profile_record(self):
        import main
        import pymupdf
        from fastapi import UploadFile
        old_main_db,old_storage=main.DB,main.STORAGE
        old_enqueue=main.enqueue
        root=Path(self.temp.name)/"upload-test"; root.mkdir(); storage=root/"private_resumes"; storage.mkdir()
        db_path=root/"upload.sqlite"
        main.DB=db_path; main.STORAGE=storage
        old_file=storage/"old-version.pdf"; old_file.write_bytes(b"old resume file")
        with main.connect() as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,original_name TEXT,stored_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
            db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO resumes VALUES('old',?,'old.pdf','old-version.pdf','2026-01-01',1,'old text')",(USER,))
            db.execute("INSERT INTO resume_profiles VALUES('old','user_reviewed','{}',NULL,'2026-01-01')")
        pdf=pymupdf.open(); page=pdf.new_page(); page.insert_text((72,72),"A test resume with selectable content")
        data=pdf.tobytes(); pdf.close()
        main.enqueue=lambda _resume_id:{"status":"queued"}
        try:
            result=asyncio.run(main.upload_resume(UploadFile(filename="replacement.pdf",file=BytesIO(data))))
            self.assertTrue(old_file.exists())
            with main.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0],2)
                self.assertEqual(db.execute("SELECT active FROM resumes WHERE id='old'").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM resume_profiles WHERE resume_id='old'").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT active FROM resumes WHERE id=?",(result["id"],)).fetchone()[0],1)
        finally:
            main.DB,main.STORAGE,main.enqueue=old_main_db,old_storage,old_enqueue

    def test_resume_cleanup_plan_is_conservative_and_never_deletes_files_or_history(self):
        import main
        from fastapi import HTTPException
        old_main_db,old_storage=main.DB,main.STORAGE
        main.DB=self.db
        main.STORAGE=Path(self.temp.name)/"private_resumes"; main.STORAGE.mkdir()
        old_file=main.STORAGE/"r1.pdf"; old_file.write_bytes(b"eligible old PDF")
        try:
            with main.connect() as db:
                db.execute("ALTER TABLE resumes ADD COLUMN active INTEGER NOT NULL DEFAULT 0") if "active" not in {r[1] for r in db.execute("PRAGMA table_info(resumes)")} else None
                db.execute("UPDATE resumes SET active=0 WHERE id='r1'")
                db.execute("INSERT INTO resumes VALUES('r2',?,'Current.pdf','current.pdf','2026-02-01',1,'')",(USER,))
                db.execute("CREATE TABLE applications(id TEXT PRIMARY KEY,user_id TEXT,resume_id TEXT,state TEXT)")
                db.execute("INSERT INTO applications VALUES('a1',?,'r1','failed')",(USER,))
            plan={x["resume_id"]:x for x in main.safe_resume_cleanup_plan()}
            self.assertFalse(plan["r2"]["eligible"])
            self.assertFalse(plan["r1"]["eligible"])
            with main.connect() as db: db.execute("UPDATE applications SET state='ready_for_review' WHERE id='a1'")
            plan={x["resume_id"]:x for x in main.safe_resume_cleanup_plan()}
            self.assertTrue(plan["r1"]["eligible"])
            self.assertTrue(old_file.exists())
            with self.assertRaises(HTTPException): main.cleanup_old_resume_pdf("r1","")
            self.assertTrue(old_file.exists())
            result=main.cleanup_old_resume_pdf("r1","DELETE_OLD_RESUME_PDF")
            self.assertTrue(result["removed"]); self.assertTrue(result["history_preserved"])
            self.assertFalse(old_file.exists())
            with main.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM applications").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0],2)
        finally:
            main.DB,main.STORAGE=old_main_db,old_storage

    def _app_candidate(self):
        import application_agent
        old,old_storage = application_agent.DB,application_agent.STORAGE
        application_agent.DB = self.db
        application_agent.STORAGE=Path(self.temp.name)/"private_resumes"
        application_agent.STORAGE.mkdir(exist_ok=True)
        (application_agent.STORAGE/"r1.pdf").write_bytes(b"test pdf")
        try:
            with patch.object(application_agent, "connect", profile.connect):
                return application_agent._candidate()
        finally:
            application_agent.DB,application_agent.STORAGE = old,old_storage
    def test_location_controls_are_opt_in_one_shot_and_preserve_manual_fallback(self):
        page=profile.PROFILE_PAGE
        for text in ("Use My Current Location","Enter Location Manually","Not Now","navigator.geolocation.getCurrentPosition","babysteps-location-denied","Coordinates were not saved","residence or mailing address"):
            self.assertIn(text,page)


if __name__ == "__main__":
    unittest.main()
