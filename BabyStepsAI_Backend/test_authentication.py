"""Synthetic tests for authenticated sessions and tenant isolation."""
import os
import sqlite3
import socket
import threading
import time
import tempfile
import unittest
import uuid
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import authentication as auth
import tenant_context
from tenant_context import current_user_id, database_path, user_context


class AuthenticationIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="bsa-auth-tests-")
        cls.root = Path(cls.temp.name)
        os.environ["BABYSTEPS_DATA_ROOT"] = str(cls.root / "tenants")
        os.environ["BABYSTEPS_AUTH_DB"] = str(cls.root / "auth.sqlite3")
        os.environ["BABYSTEPS_PUBLIC_ORIGIN"] = "http://127.0.0.1:8000"
        os.environ["BABYSTEPS_OIDC_ISSUER"] = "https://identity.example"
        os.environ["BABYSTEPS_OIDC_CLIENT_ID"] = "synthetic-client"
        os.environ["BABYSTEPS_OIDC_CLIENT_SECRET"] = "synthetic-secret"
        os.environ["BABYSTEPS_OIDC_REDIRECT_URI"] = "http://127.0.0.1:8000/auth/callback"
        os.environ["BABYSTEPS_SESSION_SECRET"] = "s" * 48
        os.environ["BABYSTEPS_COOKIE_SECURE"] = "false"
        cls.old_context = (tenant_context._DATA_ROOT, tenant_context._DATA_ROOT_CONFIGURED, tenant_context.AUTH_MODE)
        cls.old_auth_mode = auth.AUTH_MODE
        tenant_context._DATA_ROOT = cls.root / "tenants"
        tenant_context._DATA_ROOT_CONFIGURED = True
        tenant_context.AUTH_MODE = "oidc"
        auth.AUTH_DB = Path(os.environ["BABYSTEPS_AUTH_DB"])
        auth.initialize()
        auth.AUTH_MODE = "oidc"
        cls.real_tenant_init = staticmethod(auth._initialize_tenant)
        auth._initialize_tenant = lambda _user_id: None
        cls.users = {"a": "synthetic-user-a", "b": "synthetic-user-b"}
        with auth._connect() as db:
            for key, user_id in cls.users.items():
                auth._set_db_context(db, user_id=user_id)
                db.execute("INSERT INTO auth_users(user_id,issuer,subject,email,email_verified,created_at,last_login_at) VALUES(?,?,?,?,1,?,?) ON CONFLICT(user_id) DO UPDATE SET email=excluded.email,email_verified=1,last_login_at=excluded.last_login_at",
                           (user_id, "https://identity.example", "subject-" + key, key + "@example.test", auth._utc(), auth._utc()))
        cls.sessions = {key: auth._issue_session(user_id)[0] for key, user_id in cls.users.items()}
        app = FastAPI()
        auth.configure_auth_routes(app)

        @app.get("/health")
        async def health():
            return {"ok": True}

        @app.get("/api/records/{kind}/{record_id}")
        async def read_record(kind: str, record_id: str):
            db = sqlite3.connect(database_path(cls.root / "records.sqlite3"))
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,record_id TEXT,payload TEXT,PRIMARY KEY(kind,record_id))")
            row = db.execute("SELECT payload FROM records WHERE kind=? AND record_id=?", (kind, record_id)).fetchone()
            db.close()
            if not row:
                raise HTTPException(404, "Record not found.")
            return {"payload": row["payload"]}

        @app.post("/api/records/{kind}/{record_id}")
        async def write_record(kind: str, record_id: str, request: Request):
            data = await request.json()
            db = sqlite3.connect(database_path(cls.root / "records.sqlite3"))
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,record_id TEXT,payload TEXT,PRIMARY KEY(kind,record_id))")
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, record_id, data["payload"]))
            db.commit()
            db.close()
            return {"saved": True, "owner": current_user_id()}

        cls.app = app

    @classmethod
    def tearDownClass(cls):
        auth._tenant_ready.clear()
        auth.AUTH_MODE = cls.old_auth_mode
        tenant_context._DATA_ROOT, tenant_context._DATA_ROOT_CONFIGURED, tenant_context.AUTH_MODE = cls.old_context
        cls.temp.cleanup()

    def setUp(self):
        self.sessions = {key: auth._issue_session(user_id)[0] for key, user_id in self.users.items()}
        self.client_a = TestClient(self.app, follow_redirects=False)
        self.client_b = TestClient(self.app, follow_redirects=False)
        self.client_a.cookies.set(auth.SESSION_COOKIE, self.sessions["a"])
        self.client_b.cookies.set(auth.SESSION_COOKIE, self.sessions["b"])
        self.origin = {"Origin": os.environ["BABYSTEPS_PUBLIC_ORIGIN"]}

    def tearDown(self):
        self.client_a.close()
        self.client_b.close()

    def test_two_users_cannot_read_or_change_each_others_private_records(self):
        kinds = ("profile", "resume", "sarathi", "application", "workflow")
        for kind in kinds:
            response = self.client_b.post(f"/api/records/{kind}/known-id", headers=self.origin, json={"payload": "belongs to B"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client_b.get(f"/api/records/{kind}/known-id").json()["payload"], "belongs to B")
            self.assertEqual(self.client_a.get(f"/api/records/{kind}/known-id").status_code, 404)
        self.assertEqual(self.client_a.post("/api/records/profile/known-id", headers=self.origin, json={"payload": "A"}).status_code, 200)
        self.assertEqual(self.client_b.get("/api/records/profile/known-id").json()["payload"], "belongs to B")

    def test_anonymous_access_csrf_logout_and_session_expiration(self):
        anonymous = TestClient(self.app, follow_redirects=False)
        self.assertEqual(anonymous.get("/api/records/profile/x").status_code, 401)
        anonymous.close()
        self.assertEqual(self.client_a.post("/api/records/profile/x", json={"payload": "no origin"}).status_code, 403)
        response = self.client_a.post("/auth/logout", headers=self.origin)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client_a.get("/auth/me").status_code, 401)
        expired, _ = auth._issue_session(self.users["a"])
        with auth._connect() as db:
            auth._set_db_context(db, user_id=self.users["a"])
            db.execute("UPDATE auth_sessions SET expires_at=? WHERE session_hash=?", ("2000-01-01T00:00:00+00:00", auth._hash_session(expired)))
        self.assertIsNone(auth._session_user(expired))
        first = auth._issue_session(self.users["a"])[0]
        second = auth._issue_session(self.users["a"])[0]
        self.client_a.cookies.set(auth.SESSION_COOKIE, first)
        self.assertEqual(self.client_a.post("/auth/logout-all", headers=self.origin).status_code, 303)
        self.assertIsNone(auth._session_user(first))
        self.assertIsNone(auth._session_user(second))

    def test_rate_limit_and_auth_provider_signup_redirect(self):
        key = "auth-test-" + str(id(self))
        auth._rate_limit(key, 1)
        with self.assertRaises(Exception) as raised:
            auth._rate_limit(key, 1)
        self.assertEqual(getattr(raised.exception, "status_code", None), 429)

        class FakeProvider:
            async def authorize_redirect(self, request, redirect_uri, **kwargs):
                return auth.RedirectResponse("https://identity.example/signup?screen_hint=" + kwargs.get("screen_hint", "login"))

        with patch.object(auth, "_client", return_value=FakeProvider()):
            response = self.client_a.get("/auth/signup")
        self.assertEqual(response.status_code, 307)
        self.assertIn("screen_hint=signup", response.headers["location"])

        class CallbackProvider:
            async def authorize_access_token(self, request):
                return {"userinfo": {"iss": "https://identity.example", "sub": "chromium-synthetic", "email": "chromium@example.test", "email_verified": True}}

        with patch.object(auth, "_client", return_value=CallbackProvider()):
            callback = self.client_a.get("/auth/callback?code=synthetic&state=synthetic")
        self.assertEqual(callback.status_code, 303)
        cookie = callback.headers["set-cookie"]
        self.assertIn("httponly", cookie.casefold())
        self.assertIn("samesite=lax", cookie.casefold())
        self.assertIn("bsa_session", cookie)

    @unittest.skipUnless(os.environ.get("BABYSTEPS_RUN_REAL_CHROMIUM") == "1", "real Chromium is opt-in")
    def test_authenticated_session_in_real_chromium(self):
        from playwright.sync_api import sync_playwright
        import uvicorn

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        origin = f"http://127.0.0.1:{port}"
        previous_origin = os.environ.get("BABYSTEPS_PUBLIC_ORIGIN")
        os.environ["BABYSTEPS_PUBLIC_ORIGIN"] = origin
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="critical")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(origin + "/health", timeout=0.2)
                    break
                except Exception:
                    time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_cookies([{"name": auth.SESSION_COOKIE, "value": self.sessions["a"], "url": origin,
                                      "httpOnly": True, "secure": False, "sameSite": "Lax"}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                response = page.goto(origin + "/auth/me")
                self.assertEqual(response.status, 200)
                self.assertIn("a@example.test", page.locator("body").inner_text())
                result = page.evaluate("fetch('/api/records/profile/chromium', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payload:'synthetic'})}).then(r=>r.json())")
                self.assertTrue(result["saved"])
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            if previous_origin is None:
                os.environ.pop("BABYSTEPS_PUBLIC_ORIGIN", None)
            else:
                os.environ["BABYSTEPS_PUBLIC_ORIGIN"] = previous_origin

    def test_tenant_paths_and_private_files_are_separate(self):
        from tenant_context import private_storage_path
        with user_context(self.users["a"]):
            a_db = database_path(self.root / "application.sqlite3")
            a_files = private_storage_path(Path(__file__).resolve().parent / "private_resumes")
        with user_context(self.users["b"]):
            b_db = database_path(self.root / "application.sqlite3")
            b_files = private_storage_path(Path(__file__).resolve().parent / "private_resumes")
        self.assertNotEqual(a_db, b_db)
        self.assertNotEqual(a_db.parent, b_db.parent)
        self.assertNotEqual(a_files, b_files)

    def test_request_size_limit_and_oidc_config_fail_closed(self):
        app = FastAPI()
        app.add_middleware(auth.RequestSizeLimitMiddleware, max_bytes=8)
        @app.post("/echo")
        async def echo(request: Request):
            return {"size": len(await request.body())}
        self.assertEqual(TestClient(app).post("/echo", content=b"123456789").status_code, 413)
        with patch.dict(os.environ, {"BABYSTEPS_OIDC_ISSUER": "", "BABYSTEPS_OIDC_CLIENT_ID": "",
                                     "BABYSTEPS_OIDC_CLIENT_SECRET": "", "BABYSTEPS_OIDC_REDIRECT_URI": "",
                                     "BABYSTEPS_PUBLIC_ORIGIN": "", "BABYSTEPS_SESSION_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                auth.validate_oidc_config()

    def test_google_oidc_configuration_uses_discovery_and_pkce(self):
        config = {
            "BABYSTEPS_OIDC_ISSUER": "https://accounts.google.com",
            "BABYSTEPS_OIDC_CLIENT_ID": "synthetic-google-client",
            "BABYSTEPS_OIDC_CLIENT_SECRET": "synthetic-only-secret",
            "BABYSTEPS_OIDC_REDIRECT_URI": "https://beta.example.test/auth/callback",
            "BABYSTEPS_PUBLIC_ORIGIN": "https://beta.example.test",
            "BABYSTEPS_SESSION_SECRET": "x" * 48,
            "BABYSTEPS_COOKIE_SECURE": "true",
        }

        class FakeClient:
            pass

        class FakeOAuth:
            def register(self, **kwargs):
                self.registered = kwargs
                self.baby_steps_oidc = FakeClient()

        previous = auth._oauth
        try:
            auth._oauth = None
            with patch.dict(os.environ, config, clear=False), patch.object(auth, "OAuth", FakeOAuth):
                validated = auth.validate_oidc_config()
                client = auth._client()
            self.assertEqual(validated["issuer"], "https://accounts.google.com")
            self.assertIsInstance(client, FakeClient)
            registered = auth._oauth.registered
            self.assertEqual(registered["server_metadata_url"], "https://accounts.google.com/.well-known/openid-configuration")
            self.assertEqual(registered["client_kwargs"]["scope"], "openid email profile")
            self.assertEqual(registered["client_kwargs"]["code_challenge_method"], "S256")

            class GoogleProvider:
                async def authorize_redirect(self, request, redirect_uri, **kwargs):
                    self.kwargs = kwargs
                    return auth.RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth")

            google = GoogleProvider()
            with patch.dict(os.environ, config, clear=False), patch.object(auth, "_client", return_value=google):
                response = self.client_a.get("/auth/signup")
            self.assertEqual(response.status_code, 307)
            self.assertNotIn("screen_hint", google.kwargs)
        finally:
            auth._oauth = previous

    def test_real_private_apis_scope_resume_sarathi_application_and_workflow_records(self):
        import main
        import sarathi
        import application_agent
        import onboarding
        import workflow_orchestrator

        suffix=uuid.uuid4().hex
        resume_id="resume-b-"+suffix
        conversation_id="conversation-b-"+suffix
        document_id="document-b-"+suffix
        application_id="application-b-"+suffix
        workflow_id="workflow-b-"+suffix

        for user_id in self.users.values():
            self.real_tenant_init(user_id)

        with user_context(self.users["b"]):
            with workflow_orchestrator.database() as db: db.execute("DELETE FROM user_workflows WHERE user_id=?", (current_user_id(),))
            with application_agent.connect() as db: db.execute("DELETE FROM applications WHERE user_id=?", (current_user_id(),))
            with onboarding.connect() as db: db.execute("DELETE FROM application_documents WHERE user_id=?", (current_user_id(),))
            with sarathi.connect() as db: db.execute("DELETE FROM sarathi_conversations WHERE user_id=?", (current_user_id(),))
            with main.connect() as db: db.execute("DELETE FROM resumes WHERE user_id=?", (current_user_id(),))

        with user_context(self.users["b"]):
            with main.connect() as db:
                db.execute("INSERT INTO resumes(id,user_id,original_name,stored_name,created_at,active,extracted_text) VALUES(?,?,'synthetic.pdf','synthetic.pdf','now',1,'synthetic')", (resume_id,current_user_id()))
            with sarathi.connect() as db:
                db.execute("INSERT INTO sarathi_conversations VALUES(?,?,'Synthetic','now','now')", (conversation_id,current_user_id()))
            with onboarding.connect() as db:
                db.execute("INSERT INTO application_documents(document_id,user_id,resume_id,document_type,original_name,stored_name,mime_type,size_bytes,sha256,created_at,updated_at) VALUES(?,?,?,'other','synthetic.pdf',?,'application/pdf',3,'synthetic-hash','now','now')", (document_id,current_user_id(),resume_id,document_id+".pdf"))
            doc_dir = tenant_context.private_storage_path(onboarding.DOCS)
            (doc_dir / (document_id+".pdf")).write_bytes(b"pdf")
            with application_agent.connect() as db:
                db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,mode,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,'Synthetic Co','Engineer','https://jobs.example.test/1','review','dry_run','{}','now','now')", (application_id,current_user_id(),resume_id,"job-"+suffix))
            with workflow_orchestrator.database() as db:
                db.execute("INSERT INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,created_at,updated_at) VALUES(?,?,'synthetic','paused','paused','now','now')", (workflow_id,current_user_id()))

        real = TestClient(main.app, follow_redirects=False)
        real.cookies.set(auth.SESSION_COOKIE, self.sessions["a"])
        empty = real.get("/resumes")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), [])
        self.assertEqual(real.get(f"/resumes/{resume_id}/profile").status_code, 404)
        self.assertEqual(real.get("/api/onboarding/state").json()["has_active_resume"], False)
        self.assertEqual(real.get("/api/sarathi/conversations").json(), [])
        self.assertEqual(real.get(f"/api/sarathi/conversations/{conversation_id}").status_code, 404)
        self.assertEqual(real.get(f"/api/application-documents/{document_id}/download").status_code, 404)
        self.assertEqual(real.get("/api/applications").json(), [])
        self.assertEqual(real.get(f"/api/applications/{application_id}/attempts").status_code, 404)
        self.assertIsNone(real.get("/api/workflows/current").json()["workflow"])
        self.assertEqual(real.post(f"/api/workflows/{workflow_id}/pause", headers=self.origin).status_code, 404)
        real.close()

        own = TestClient(main.app, follow_redirects=False)
        own.cookies.set(auth.SESSION_COOKIE, self.sessions["b"])
        self.assertEqual(len(own.get("/resumes").json()), 1)
        self.assertTrue(own.get("/api/onboarding/state").json()["has_active_resume"])
        self.assertEqual(len(own.get("/api/sarathi/conversations").json()), 1)
        self.assertEqual(own.get(f"/api/application-documents/{document_id}/download").content, b"pdf")
        self.assertEqual(len(own.get("/api/applications").json()), 1)
        current = own.get("/api/workflows/current").json()["workflow"]
        self.assertEqual(current["workflow_id"], workflow_id)
        self.assertEqual(current["status"], "paused")
        own.close()

    def test_operator_approved_legacy_claim_copies_only_to_claimant_and_preserves_source(self):
        claimant, operator = "claimant-synthetic", "operator-synthetic"
        with auth._connect() as db:
            for uid, subject in ((claimant, "claimant-sub"), (operator, "operator-sub")):
                auth._set_db_context(db, user_id=uid)
                db.execute("INSERT INTO auth_users(user_id,issuer,subject,email,email_verified,created_at,last_login_at) VALUES(?,?,?,?,1,?,?)",
                           (uid, "https://identity.example", subject, uid + "@example.test", auth._utc(), auth._utc()))
            auth._set_db_context(db, user_id=claimant)
            db.execute("INSERT INTO legacy_account_claims(claim_id,user_id,state,requested_at) VALUES('synthetic-claim',?,'pending',?)", (claimant, auth._utc()))
        source_root = self.root / "legacy-source"
        source_root.mkdir()
        source_db = source_root / "legacy.sqlite3"
        with sqlite3.connect(source_db) as db:
            db.execute("CREATE TABLE profile(user_id TEXT,full_name TEXT)")
            db.execute("INSERT INTO profile VALUES('local-test-user','Synthetic Person')")
        resumes = source_root / "private_resumes"
        resumes.mkdir()
        (resumes / "synthetic.pdf").write_bytes(b"synthetic resume")
        with patch.dict(os.environ, {"BABYSTEPS_OPERATOR_SUBJECT": "operator-sub"}):
            result = auth.approve_legacy_claim("synthetic-claim", operator, source_db=source_db)
        self.assertTrue(result["approved"])
        with user_context(claimant):
            target = database_path(source_db)
            tenant_resume = target.parent / "private_resumes" / "synthetic.pdf"
        self.assertTrue(tenant_resume.is_file())
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute("SELECT user_id FROM profile").fetchone()[0], claimant)
        with sqlite3.connect(source_db) as db:
            self.assertEqual(db.execute("SELECT user_id FROM profile").fetchone()[0], "local-test-user")
        self.assertEqual((resumes / "synthetic.pdf").read_bytes(), b"synthetic resume")


if __name__ == "__main__":
    unittest.main()
