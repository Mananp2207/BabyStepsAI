import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import UploadFile

import onboarding
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
        result=onboarding.save_sensitive_disclosure("gender",onboarding.SensitiveDisclosureUpdate(
            value="user-provided",confirmed=True,reuse_allowed=True),self.request)
        self.assertTrue(result["saved"]);self.assertFalse(result["ai_allowed"])
        with onboarding.connect() as db:
            row=db.execute("SELECT ciphertext FROM sensitive_disclosures WHERE attribute_key='gender'").fetchone()
        self.assertNotIn("user-provided",row["ciphertext"]);self.assertEqual(decrypt_text(row["ciphertext"]),"user-provided")
        self.assertEqual(onboarding.get_sensitive_disclosures(self.request)["disclosures"][0]["value"],"user-provided")
        onboarding.delete_sensitive_disclosure("gender",self.request)
        self.assertEqual(onboarding.get_sensitive_disclosures(self.request)["disclosures"],[])

    def test_declining_disclosure_stores_no_answer_and_untrusted_host_is_rejected(self):
        result=onboarding.save_sensitive_disclosure("pronouns",onboarding.SensitiveDisclosureUpdate(declined=True),self.request)
        self.assertTrue(result["confirmed"])
        with onboarding.connect() as db:
            row=db.execute("SELECT ciphertext,declined FROM sensitive_disclosures WHERE attribute_key='pronouns'").fetchone()
        self.assertEqual(row["ciphertext"],"");self.assertEqual(row["declined"],1)
        with self.assertRaises(Exception):onboarding.onboarding_state(SimpleNamespace(client=SimpleNamespace(host="192.168.1.10")))

    async def test_private_document_upload_validates_signature_and_is_not_public_static(self):
        upload=UploadFile(filename="../../transcript.pdf",file=BytesIO(b"%PDF-valid private content"))
        result=await onboarding.upload_application_document(self.request,"transcript",upload)
        self.assertEqual(result["document_type"],"transcript")
        docs=onboarding.list_application_documents(self.request)["documents"]
        self.assertEqual(len(docs),1);self.assertEqual(docs[0]["original_name"],"transcript.pdf")
        self.assertTrue((onboarding.DOCS/(docs[0]["document_id"]+".pdf")).is_file())
        self.assertFalse((onboarding.DOCS/"transcript.pdf").exists())

    async def test_invalid_file_signature_is_rejected(self):
        upload=UploadFile(filename="not-a-pdf.pdf",file=BytesIO(b"plain text"))
        with self.assertRaises(Exception):await onboarding.upload_application_document(self.request,"transcript",upload)


if __name__=="__main__":unittest.main()
