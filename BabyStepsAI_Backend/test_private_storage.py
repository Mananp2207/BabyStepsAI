from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import private_storage
import tenant_context
from tenant_context import user_context


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="bsa-private-storage-")
        self.old=(tenant_context._DATA_ROOT,tenant_context._DATA_ROOT_CONFIGURED,tenant_context.AUTH_MODE)
        self.old_env={key:os.environ.get(key) for key in ("BABYSTEPS_STORAGE_BACKEND","SUPABASE_URL","SUPABASE_SERVICE_ROLE_KEY","SUPABASE_SECRET_KEY","SUPABASE_PRIVATE_BUCKET")}
        tenant_context._DATA_ROOT=Path(self.temp.name)/"users"
        tenant_context._DATA_ROOT_CONFIGURED=True
        tenant_context.AUTH_MODE="oidc"
        os.environ["BABYSTEPS_STORAGE_BACKEND"]="local"

    def tearDown(self):
        tenant_context._DATA_ROOT,tenant_context._DATA_ROOT_CONFIGURED,tenant_context.AUTH_MODE=self.old
        for key,value in self.old_env.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temp.cleanup()

    def test_local_files_are_private_per_user(self):
        base=Path(self.temp.name)/"resumes"
        with user_context("synthetic-user-a"):
            path=private_storage.store_private_file(base,"resume.pdf",b"%PDF-1.4\nsynthetic\n%%EOF","application/pdf")
            self.assertEqual(private_storage.resolve_private_file(base,"resume.pdf").read_bytes(),b"%PDF-1.4\nsynthetic\n%%EOF")
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with user_context("synthetic-user-b"):
            with self.assertRaises(FileNotFoundError):private_storage.resolve_private_file(base,"resume.pdf")

    def test_supabase_upload_download_and_delete_are_tenant_scoped(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SERVICE_ROLE_KEY="synthetic-secret",SUPABASE_PRIVATE_BUCKET="private-files")
        base=Path(self.temp.name)/"documents";calls=[]
        blob=b"%PDF-1.4\nsynthetic\n%%EOF"
        def fake_request(method,url,**kwargs):
            calls.append((method,url,kwargs))
            if method=="GET":return SimpleNamespace(status_code=200,content=blob)
            return SimpleNamespace(status_code=200 if method=="DELETE" else 201,content=b"")
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            with user_context("synthetic-user-a"):
                private_storage.store_private_file(base,"document.pdf",blob,"application/pdf")
                path=private_storage._cache_path(base,"document.pdf");path.unlink()
                self.assertEqual(private_storage.resolve_private_file(base,"document.pdf").read_bytes(),blob)
                self.assertTrue(private_storage.delete_private_file(base,"document.pdf"))
            owner=hashlib.sha256(b"synthetic-user-a").hexdigest()
            self.assertTrue(all("/"+owner+"/document.pdf" in c[1] or c[0]=="DELETE" for c in calls))
            self.assertTrue(all(c[2]["headers"]["Authorization"].startswith("Bearer ") for c in calls))
            with user_context("synthetic-user-b"):
                key_b=private_storage._object_key("synthetic-user-b","document.pdf")
            self.assertNotEqual(key_b,owner+"/document.pdf")

    def test_new_supabase_secret_key_uses_apikey_without_bearer(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SECRET_KEY="sb_secret_synthetic_only",SUPABASE_SERVICE_ROLE_KEY="legacy-must-not-win",
                          SUPABASE_PRIVATE_BUCKET="private-files")
        captured=[]
        def fake_request(method,url,**kwargs):
            captured.append(kwargs["headers"])
            return SimpleNamespace(status_code=200,content=b"")
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            cfg=private_storage._config()
            private_storage._request("GET","bucket/private-files",config=cfg)
        self.assertEqual(captured[0]["apikey"],"sb_secret_synthetic_only")
        self.assertNotIn("Authorization",captured[0])

    def test_legacy_service_role_jwt_keeps_bearer_compatibility(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SERVICE_ROLE_KEY="eyJ.synthetic.legacy",SUPABASE_SECRET_KEY="",
                          SUPABASE_PRIVATE_BUCKET="private-files")
        captured=[]
        def fake_request(method,url,**kwargs):
            captured.append(kwargs["headers"])
            return SimpleNamespace(status_code=200,content=b"")
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            private_storage._request("GET","bucket/private-files")
        self.assertEqual(captured[0]["apikey"],"eyJ.synthetic.legacy")
        self.assertEqual(captured[0]["Authorization"],"Bearer eyJ.synthetic.legacy")

    def test_publishable_key_is_rejected_for_private_storage(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SECRET_KEY="sb_publishable_synthetic",SUPABASE_SERVICE_ROLE_KEY="",
                          SUPABASE_PRIVATE_BUCKET="private-files")
        with self.assertRaisesRegex(RuntimeError,"server-side secret key"):
            private_storage._config()

    def test_tenant_cannot_download_another_users_object(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SERVICE_ROLE_KEY="synthetic-secret",SUPABASE_PRIVATE_BUCKET="private-files")
        base=Path(self.temp.name)/"objects";objects={}
        def fake_request(method,url,**kwargs):
            key=url.split("/object/",1)[-1]
            key=key.removeprefix("authenticated/")
            if method=="POST":
                objects[key]=kwargs["data"]
                return SimpleNamespace(status_code=201,content=b"")
            if method=="GET":
                value=objects.get(key)
                return SimpleNamespace(status_code=200 if value is not None else 404,content=value or b"")
            if method=="DELETE":
                return SimpleNamespace(status_code=200,content=b"")
            raise AssertionError(method)
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            with user_context("synthetic-user-a"):
                private_storage.store_private_file(base,"resume.pdf",b"%PDF-1.4\nA\n%%EOF","application/pdf")
                private_storage._cache_path(base,"resume.pdf").unlink()
            with user_context("synthetic-user-b"):
                with self.assertRaises(FileNotFoundError):
                    private_storage.resolve_private_file(base,"resume.pdf")

    def test_upload_rejects_content_type_and_signature_mismatch(self):
        with user_context("synthetic-user-a"):
            with self.assertRaisesRegex(ValueError,"extension and content type"):
                private_storage.store_private_file(Path(self.temp.name),"resume.pdf",b"%PDF-1.4","image/png")
            with self.assertRaisesRegex(ValueError,"valid PDF"):
                private_storage.store_private_file(Path(self.temp.name),"resume.pdf",b"not a pdf","application/pdf")

    def test_public_bucket_is_rejected(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SERVICE_ROLE_KEY="synthetic-secret",SUPABASE_PRIVATE_BUCKET="private-files")
        with patch.object(private_storage.requests,"request",return_value=SimpleNamespace(status_code=200,json=lambda:{"public":True})):
            with self.assertRaisesRegex(RuntimeError,"public"):
                private_storage.ensure_private_bucket()

    def test_bucket_creation_requires_private_access_and_upload_limits(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SERVICE_ROLE_KEY="synthetic-secret",SUPABASE_PRIVATE_BUCKET="private-files")
        calls=[]
        def fake_request(method,url,**kwargs):
            calls.append((method,url,kwargs))
            if method=="GET":return SimpleNamespace(status_code=404)
            return SimpleNamespace(status_code=201,json=lambda:{"public":False})
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            self.assertEqual(private_storage.ensure_private_bucket(),{"bucket_private":True,"bucket_ready":True})
        body=calls[-1][2]["json"]
        self.assertFalse(body["public"])
        self.assertEqual(body["file_size_limit"],private_storage.MAX_OBJECT_BYTES)
        self.assertEqual(set(body["allowed_mime_types"]),set(private_storage.SUPPORTED_MIME_TYPES))

    def test_read_only_bucket_verification_checks_privacy_mime_and_size(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SECRET_KEY="sb_secret_synthetic_only",SUPABASE_PRIVATE_BUCKET="private-files")
        details={"public":False,"file_size_limit":private_storage.MAX_OBJECT_BYTES,
                 "allowed_mime_types":list(private_storage.SUPPORTED_MIME_TYPES)}
        calls=[]
        def fake_request(method,url,**kwargs):
            calls.append(method)
            return SimpleNamespace(status_code=200,json=lambda:details)
        with patch.object(private_storage.requests,"request",side_effect=fake_request):
            result=private_storage.verify_private_bucket()
        self.assertEqual(calls,["GET"])
        self.assertTrue(result["bucket_private"])
        self.assertEqual(result["file_size_limit"],private_storage.MAX_OBJECT_BYTES)
        self.assertEqual(set(result["allowed_mime_types"]),set(private_storage.SUPPORTED_MIME_TYPES))

    def test_read_only_bucket_verification_never_creates_missing_bucket(self):
        os.environ.update(BABYSTEPS_STORAGE_BACKEND="supabase",SUPABASE_URL="https://sample.supabase.co",
                          SUPABASE_SECRET_KEY="sb_secret_synthetic_only",SUPABASE_PRIVATE_BUCKET="private-files")
        calls=[]
        with patch.object(private_storage.requests,"request",side_effect=lambda method,*args,**kwargs:(calls.append(method) or SimpleNamespace(status_code=404))):
            with self.assertRaisesRegex(RuntimeError,"does not exist"):
                private_storage.verify_private_bucket()
        self.assertEqual(calls,["GET"])

    def test_traversal_and_oversize_uploads_are_rejected(self):
        with user_context("synthetic-user-a"):
            with self.assertRaises(ValueError):private_storage.store_private_file(Path(self.temp.name),"../resume.pdf",b"x","application/pdf")
            with self.assertRaises(ValueError):private_storage.store_private_file(Path(self.temp.name),"large.pdf",b"x"*(private_storage.MAX_OBJECT_BYTES+1),"application/pdf")


if __name__=="__main__":unittest.main()
