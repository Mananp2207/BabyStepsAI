import os
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from starlette.datastructures import UploadFile

import onboarding
import application_answer_assistant
from job_search_agent import USER
from secure_data import decrypt_text


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.old_db,self.old_docs=onboarding.DB,onboarding.DOCS
        self.old_key=os.environ.get("BABYSTEPSAI_KEY_PATH");os.environ["BABYSTEPSAI_KEY_PATH"]=str(root/"encryption.key")
        onboarding.DB=root/"test.sqlite3";onboarding.DOCS=root/"private-documents"
        with onboarding.connect() as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,active INTEGER,created_at TEXT)")
            db.execute("INSERT INTO resumes VALUES('resume-1',?,'resume.pdf','Resume.pdf',1,'now')",(USER,))
        onboarding.initialize()
        self.request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    def tearDown(self):
        onboarding.DB=self.old_db;onboarding.DOCS=self.old_docs
        if self.old_key is None:os.environ.pop("BABYSTEPSAI_KEY_PATH",None)
        else:os.environ["BABYSTEPSAI_KEY_PATH"]=self.old_key
        self.temp.cleanup()

    def test_optional_disclosure_is_encrypted_separate_revocable_and_never_ai_enabled(self):
        self.assertEqual(onboarding.get_sensitive_disclosures(self.request),{"disclosures":[],"ai_use":"not used by matching or answer drafting"})
        result=onboarding.save_sensitive_disclosure("gender",onboarding.SensitiveDisclosureUpdate(
            value="user-provided",confirmed=True,reuse_allowed=True),self.request)
        self.assertTrue(result["saved"]);self.assertFalse(result["ai_allowed"])
        with onboarding.connect() as db:
            row=db.execute("SELECT ciphertext FROM sensitive_disclosures WHERE attribute_key='gender'").fetchone()
        self.assertNotIn("user-provided",row["ciphertext"]);self.assertEqual(decrypt_text(row["ciphertext"]),"user-provided")
        listed=onboarding.get_sensitive_disclosures(self.request)["disclosures"]
        self.assertEqual(listed[0]["value"],"user-provided");self.assertTrue(listed[0]["confirmed_at"])
        self.assertFalse(listed[0]["ai_allowed"])
        onboarding.delete_sensitive_disclosure("gender",self.request)
        self.assertEqual(onboarding.get_sensitive_disclosures(self.request)["disclosures"],[])

    def test_declining_disclosure_stores_no_answer_and_untrusted_host_is_rejected(self):
        result=onboarding.save_sensitive_disclosure("pronouns",onboarding.SensitiveDisclosureUpdate(declined=True),self.request)
        self.assertTrue(result["confirmed"])
        with onboarding.connect() as db:
            row=db.execute("SELECT ciphertext,declined FROM sensitive_disclosures WHERE attribute_key='pronouns'").fetchone()
        self.assertEqual(row["ciphertext"],"");self.assertEqual(row["declined"],1)
        with self.assertRaises(Exception):onboarding.onboarding_state(SimpleNamespace(client=SimpleNamespace(host="192.168.1.10")))

    def test_onboarding_step_progress_is_read_back_from_sqlite(self):
        with onboarding.connect() as db:
            db.execute("INSERT INTO onboarding_progress(user_id,steps_json,updated_at) VALUES(?,?,?)",(USER,json.dumps({"answers":True}),"legacy"))
        saved=onboarding.update_onboarding_step("contact",onboarding.OnboardingStep(completed=True),self.request)
        self.assertTrue(saved["saved"])
        state=onboarding.onboarding_state(self.request)
        self.assertTrue(state["steps"]["contact"])
        self.assertTrue(state["steps"]["answers"])

    def test_relocation_step_progress_is_saved_and_preferences_remain_separate_from_residence(self):
        saved=onboarding.update_onboarding_step("locations",onboarding.OnboardingStep(completed=True),self.request)
        self.assertTrue(saved["saved"])
        state=onboarding.onboarding_state(self.request)
        self.assertTrue(state["steps"]["locations"])
        self.assertEqual(state["preferences"]["relocation_preference"],"unspecified")
        self.assertIn("Current residence is saved separately",onboarding.ONBOARDING_PAGE)
        self.assertIn("onboarding-relocation-",onboarding.ONBOARDING_PAGE)

    def test_onboarding_save_feedback_is_accessible_and_optional_contact_does_not_block(self):
        page=onboarding.ONBOARDING_PAGE
        self.assertIn("role=\"status\"",page)
        self.assertIn("aria-busy",page)
        self.assertIn("Saved — ",page)
        self.assertIn("verifyProfileSave",page)
        self.assertNotIn("Enter your name and email, then confirm both details.",page)
        self.assertIn("Optional disclosure choice saved and verified securely.",page)
        self.assertIn("ai_allowed:false",page)
        self.assertIn("if(!Array.isArray(value))throw Error",page)
        self.assertIn("I confirm this optional answer is accurate",page)
        self.assertIn("stable(authorization.settings[name])!==stable(value)",page)
        self.assertIn("position:static!important",page)
        self.assertNotIn("function renderAnswers(root)",page)

    def test_resume_continue_gate_is_explained_and_existing_active_resume_is_sufficient(self):
        page=application_answer_assistant.onboarding_page().body.decode("utf-8")
        self.assertIn("Upload and activate a resume before continuing.",page)
        self.assertIn("An active resume is already enough; you do not need to upload another one.",page)
        self.assertIn("Resume activation does not verify your identity.",page)
        self.assertIn("onboarding-button-accessibility",page)
        self.assertIn("#48566a",page)
        page=onboarding.ONBOARDING_PAGE
        self.assertEqual(len(__import__("re").findall(r"\['(?:resume|contact|links|experience|documents|authorization|locations|availability|disclosures|permissions|review)'",page.split("const steps=[",1)[1].split("];",1)[0])),11)
        self.assertNotIn("['answers','Answer Library'",page)
        self.assertIn("steps.filter(([key])=>!!state.steps[key]).length",page)
        self.assertIn("/candidate-profile#answers",page)
        self.assertIn('id="answers"',__import__("candidate_verification").PROFILE_PAGE)

    @unittest.skipUnless(os.getenv("BABYSTEPS_RUN_REAL_CHROMIUM")=="1","Set BABYSTEPS_RUN_REAL_CHROMIUM=1 to launch installed Chromium.")
    def test_real_chromium_onboarding_resume_states_themes_sizes_and_feedback(self):
        import candidate_verification
        from resume_profile_parser import extract_resume_profile
        page_html=application_answer_assistant.onboarding_page().body
        synthetic_text="""Avery Morgan\navery@example.test | +1 (555) 010-2040\nhttps://www.linkedin.com/in/avery-morgan https://github.com/averym\nEducation\nB.S. Computer Science, Example University, Graduation 2027, GPA: 3.8\nExperience\nSoftware Engineering Intern, Example Labs, 2024-2025\nBuilt a Python service\nProjects\nCampus Planner: React and TypeScript\nTechnical Skills\nPython\n"""
        parsed=extract_resume_profile(synthetic_text)
        mapped=candidate_verification._suggestions(parsed)
        identity_keys={"full_name","email","phone","linkedin","github"}
        profile_state={"resume_name":"Synthetic.pdf","fields":[
            {"key":key,"label":candidate_verification.FIELDS[key][1],"kind":candidate_verification.FIELDS[key][2],
             "value":value,"source":"resume_suggestion" if candidate_verification._has_profile_value(value) else "missing",
             "confirmed":False,"entry_confirmed":[False]*len(value) if isinstance(value,list) else []}
            for key,value in mapped.items() if key in candidate_verification.FIELDS]}
        state={"has_active_resume":False,"resume_name":None,"steps":{},"preferences":{},"live_submission_enabled":False}
        calls=[];upload_attempts=0
        class Handler(BaseHTTPRequestHandler):
            def _json(self,value,status=200):
                body=json.dumps(value).encode();self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            def do_GET(self):
                path=urlparse(self.path).path
                if path=="/onboarding":self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.end_headers();self.wfile.write(page_html)
                elif path=="/resumes/synthetic-resume/profile":self._json({"status":"completed_unverified","profile":{"full_name":"Riley Sample"}})
                elif path=="/api/onboarding/state":self._json(state)
                elif path=="/api/candidate-profile/verification":self._json(profile_state)
                elif path=="/api/candidate-profile/reconciliation":self._json({"previous_resume":"Prior synthetic resume.pdf","fields":[
                    {"key":"full_name","label":"Full name","existing_value":"Taylor Example","new_resume_value":mapped["full_name"],"existing_source":"confirmed from previous resume","new_source":"suggestion from active resume","recommended_action":"review_conflict","conflict":True},
                    {"key":"email","label":"Email address","existing_value":mapped["email"],"new_resume_value":mapped["email"],"existing_source":"confirmed from previous resume","new_source":"suggestion from active resume","recommended_action":"review_new_suggestion","conflict":False}]})
                elif path=="/api/application-answers":self._json([])
                elif path=="/api/application-documents":self._json({"documents":[]})
                elif path=="/api/sensitive-disclosures":self._json({"disclosures":[]})
                elif path=="/api/application-authorization":self._json({"version":9,"settings":{}})
                elif path=="/api/candidate-profile/completeness":self._json({"attention_items":[],"optional":[],"job_specific":[]})
                else:self.send_error(404)
            def do_POST(self):
                nonlocal upload_attempts
                calls.append(("POST",self.path))
                if self.path=="/resumes":
                    upload_attempts+=1
                    if upload_attempts==1:self._json({"detail":"No selectable text found."},400)
                    else:
                        state.update({"has_active_resume":True,"resume_name":"Synthetic.pdf"})
                        self._json({"id":"synthetic-resume","filename":"Synthetic.pdf","active":True})
                else:self.send_error(404)
            def do_PUT(self):
                body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)) or 0) or b"{}")
                calls.append(("PUT",self.path,body))
                if self.path=="/api/onboarding/steps/resume":
                    state["steps"]["resume"]=bool(body.get("completed"));self._json({"saved":True,"steps":state["steps"]})
                elif self.path=="/api/candidate-profile/verification":
                    values=body.get("values",{})
                    if any(key in values for key in ("education_entries","experience_entries","internship_entries","project_entries")):
                        self._json({"detail":"Chapter 02 must save only its own fields."},400);return
                    for item in profile_state["fields"]:
                        if item["key"] in values:
                            item["value"]=values[item["key"]]
                            item["confirmed"]=item["key"] in body.get("confirmed_fields",[])
                            item["source"]="user_confirmed" if item["confirmed"] else "user_provided"
                    self._json({"saved":True,"confirmed_fields":len(body.get("confirmed_fields",[]))})
                elif self.path.startswith("/api/onboarding/steps/"):
                    step=self.path.rsplit("/",1)[-1];state["steps"][step]=bool(body.get("completed"));self._json({"saved":True,"steps":state["steps"]})
                else:self.send_error(404)
            def log_message(self,*_args):pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler);port=server.server_address[1]
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={"width":1440,"height":1000})
                page_errors=[];page.on("pageerror",lambda error:page_errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/onboarding",wait_until="networkidle")
                page.get_by_text("No active resume",exact=True).wait_for()
                continue_button=page.locator("#continue")
                self.assertTrue(continue_button.is_disabled())
                self.assertIn("Upload and activate a resume",page.locator("#action-help").inner_text())
                self.assertTrue(page.locator("#back").is_disabled())
                self.assertIn("first chapter",page.locator("#back").get_attribute("title"))
                self.assertEqual(page.locator(".step").count(),11)
                for width in (1440,768,390):
                    page.set_viewport_size({"width":width,"height":900})
                    for theme in ("light","dark"):
                        page.evaluate("theme=>document.documentElement.dataset.bsaTheme=theme",theme)
                        for button in (page.locator("#continue"),page.locator("#back")):
                            colors=button.evaluate("e=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor,disabled:e.disabled,cursor:getComputedStyle(e).cursor})")
                            self.assertTrue(colors["disabled"])
                            self.assertEqual(colors["cursor"],"not-allowed")
                            self.assertNotIn(colors["bg"],["rgb(40, 89, 165)","rgb(169, 198, 255)"])
                            ratio=page.evaluate("([fg,bg])=>{const rgb=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4});const a=rgb(fg),b=rgb(bg),l=x=>.2126*x[0]+.7152*x[1]+.0722*x[2];const x=l(a),y=l(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}",[colors["fg"],colors["bg"]])
                            self.assertGreaterEqual(ratio,4.5)
                page.locator('input[type="file"]').set_input_files({"name":"Synthetic.pdf","mimeType":"application/pdf","buffer":b"%PDF synthetic test"})
                page.get_by_role("button",name="Upload and activate resume").click()
                page.get_by_text("Resume upload or activation could not be verified",exact=False).wait_for()
                self.assertTrue(continue_button.is_disabled())
                self.assertIn("readable text-based PDF under 5 MB",page.locator("#notice").inner_text())
                self.assertNotIn("/api/onboarding/steps/resume",[call[1] for call in calls])
                page.locator('input[type="file"]').set_input_files({"name":"Synthetic.pdf","mimeType":"application/pdf","buffer":b"%PDF synthetic test"})
                page.get_by_role("button",name="Upload and activate resume").click()
                page.get_by_text("Resume uploaded and verified as the active resume",exact=False).wait_for()
                self.assertIn("Synthetic.pdf",page.locator("#active-resume-status").inner_text())
                self.assertIn("Conflict: choose which value to review",page.locator("#step-content").inner_text())
                self.assertEqual(page.get_by_role("button",name="Stage existing value").count(),1)
                self.assertEqual(page.get_by_role("button",name="Stage new resume value").count(),1)
                page.get_by_role("button",name="Stage existing value").click()
                self.assertIn("selection staged",page.locator("#notice").inner_text())
                page.get_by_role("button",name="Stage new resume value").click()
                self.assertIn("explicitly confirm before saving",page.locator("#notice").inner_text())
                self.assertFalse(continue_button.is_disabled())
                self.assertEqual(page.locator(".step").count(),11)
                for width in (1440,768,390):
                    page.set_viewport_size({"width":width,"height":900})
                    for theme in ("light","dark"):
                        page.evaluate("theme=>document.documentElement.dataset.bsaTheme=theme",theme)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),width)
                        colors=continue_button.evaluate("e=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor,disabled:e.disabled})")
                        self.assertFalse(colors["disabled"])
                        self.assertEqual(colors["bg"],"rgb(169, 198, 255)" if theme=="dark" else "rgb(40, 89, 165)")
                        ratio=page.evaluate("([fg,bg])=>{const rgb=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4});const a=rgb(fg),b=rgb(bg),l=x=>.2126*x[0]+.7152*x[1]+.0722*x[2];const x=l(a),y=l(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}",[colors["fg"],colors["bg"]])
                        self.assertGreaterEqual(ratio,4.5)
                page.locator('input[type="file"]').focus();page.keyboard.press("Tab");page.keyboard.press("Tab");page.keyboard.press("Tab")
                self.assertEqual(page.evaluate("document.activeElement.id"),"continue")
                self.assertTrue(page.evaluate("document.activeElement.matches(':focus-visible')"))
                page.get_by_role("button",name="Save & continue").click()
                page.locator("#step-content").get_by_text("Personal details",exact=False).wait_for()
                self.assertEqual(page.locator('[data-key="full_name"]').input_value(),"Avery Morgan")
                self.assertEqual(page.locator('[data-key="email"]').input_value(),"avery@example.test")
                self.assertEqual(page.locator('[data-key="phone"]').input_value(),"+1 (555) 010-2040")
                for key in ("country","city","state_province","postal_code","address_line1"):
                    self.assertEqual(page.locator('[data-key="'+key+'"]').input_value(),"")
                self.assertIn("Suggested from the active resume",page.locator("#step-content").inner_text())
                self.assertFalse(page.locator('[data-confirm="full_name"]').is_checked())
                page.get_by_role("button",name="Accept all nonconflicting suggestions").click()
                self.assertFalse(page.locator('[data-confirm="full_name"]').is_checked())
                self.assertTrue(page.locator('[data-confirm="email"]').is_checked())
                self.assertTrue(page.locator('[data-confirm="phone"]').is_checked())
                page.locator('[data-confirm="full_name"]').check()
                page.get_by_role("button",name="Save & continue").click()
                page.locator("#step-content").get_by_text("Your links",exact=False).wait_for()
                self.assertIn(("full_name","Avery Morgan",True),[(x["key"],x["value"],x["confirmed"]) for x in profile_state["fields"] if x["key"]=="full_name"])
                contact_save=next(call[2] for call in calls if call[0]=="PUT" and call[1]=="/api/candidate-profile/verification")
                self.assertEqual(set(contact_save["values"]),set(onboarding.ONBOARDING_PAGE.split("const stepsForm={contact:[",1)[1].split("],links",1)[0].replace("'","").split(",")))
                self.assertTrue(any(item["key"]=="education_entries" and item["value"] for item in profile_state["fields"]))
                page.locator(".step-list .step").nth(2).click()
                self.assertEqual(page.locator('[data-key="linkedin"]').input_value(),"https://www.linkedin.com/in/avery-morgan")
                self.assertEqual(page.locator('[data-key="github"]').input_value(),"https://github.com/averym")
                page.locator(".step-list .step").nth(3).click()
                self.assertTrue(page.locator('[data-key="education_entries_0_institution"]').input_value())
                self.assertTrue(page.locator('[data-key="internship_entries_0_responsibilities"]').input_value())
                self.assertTrue(page.locator('[data-key="project_entries_0_description"]').input_value())
                self.assertIn("Python",page.locator('[data-key="skills"]').input_value())
                self.assertTrue(page.locator("#back").is_enabled())
                for theme in ("light","dark"):
                    page.evaluate("theme=>document.documentElement.dataset.bsaTheme=theme",theme)
                    back_colors=page.locator("#back").evaluate("e=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor})")
                    back_ratio=page.evaluate("([fg,bg])=>{const rgb=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4});const a=rgb(fg),b=rgb(bg),l=x=>.2126*x[0]+.7152*x[1]+.0722*x[2];const x=l(a),y=l(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}",[back_colors["fg"],back_colors["bg"]])
                    self.assertGreaterEqual(back_ratio,4.5)
                self.assertTrue(any(call[:2]==("PUT","/api/onboarding/steps/resume") for call in calls))
                page.locator(".step-list .step").nth(0).click()
                self.assertEqual(page.locator("#active-resume-status").inner_text().splitlines()[0],"Active resume")
                self.assertFalse(page.locator("#continue").is_disabled())
                page.locator("#continue").click()
                page.locator("#step-content").get_by_text("Personal details",exact=False).wait_for()
                self.assertEqual(upload_attempts,2)
                self.assertEqual(sum(call[0]=="PUT" and call[1]=="/api/onboarding/steps/resume" for call in calls),2)
                page.set_viewport_size({"width":1440,"height":900})
                for chapter in range(11):
                    page.locator(".step").nth(chapter).click()
                    self.assertFalse(page.locator("#continue").is_disabled(),f"Continue unexpectedly disabled on chapter {chapter+1} with an active resume.")
                    self.assertEqual(page.locator("#back").is_disabled(),chapter==0)
                    self.assertEqual(page.locator("#skip").is_visible(),chapter not in (0,1,10))
                    for theme in ("light","dark"):
                        page.evaluate("theme=>document.documentElement.dataset.bsaTheme=theme",theme)
                        chapter_button=page.locator("#continue").evaluate("e=>({fg:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor})")
                        self.assertEqual(chapter_button["bg"],"rgb(169, 198, 255)" if theme=="dark" else "rgb(40, 89, 165)")
                self.assertFalse(any("submit" in str(call).casefold() for call in calls))
                self.assertEqual(page_errors,[])
                browser.close()
        finally:
            server.shutdown();thread.join(timeout=2);server.server_close()

    async def test_private_document_upload_validates_signature_and_is_not_public_static(self):
        self.assertEqual(onboarding.list_application_documents(self.request),{"documents":[]})
        upload=UploadFile(filename="../../transcript.pdf",file=BytesIO(b"%PDF-valid private content"))
        result=await onboarding.upload_application_document(self.request,"transcript",upload)
        self.assertEqual(result["document_type"],"transcript")
        docs=onboarding.list_application_documents(self.request)["documents"]
        self.assertEqual(len(docs),1);self.assertEqual(docs[0]["original_name"],"transcript.pdf")
        self.assertTrue((onboarding.DOCS/(docs[0]["document_id"]+".pdf")).is_file())
        self.assertFalse((onboarding.DOCS/"transcript.pdf").exists())
        document_id=docs[0]["document_id"]
        self.assertTrue(onboarding.delete_application_document(document_id,self.request)["deleted"])
        self.assertEqual(onboarding.list_application_documents(self.request)["documents"],[])
        self.assertFalse((onboarding.DOCS/(document_id+".pdf")).exists())

    async def test_invalid_file_signature_is_rejected(self):
        upload=UploadFile(filename="not-a-pdf.pdf",file=BytesIO(b"plain text"))
        with self.assertRaises(Exception):await onboarding.upload_application_document(self.request,"transcript",upload)


if __name__=="__main__":unittest.main()
