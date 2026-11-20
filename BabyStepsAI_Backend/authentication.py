"""OIDC authentication and revocable server-side sessions for BabyStepsAI."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import shutil
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from tenant_context import AUTH_MODE, LEGACY_USER_ID, user_context
from database_adapter import connect_database, postgres_enabled


ROOT = Path(__file__).resolve().parent
AUTH_DB = Path(os.environ.get("BABYSTEPS_AUTH_DB", str(ROOT / "private_auth" / "babysteps_auth.sqlite3"))).expanduser()
router = APIRouter()
SESSION_COOKIE = "bsa_session"
SESSION_SECONDS = 12 * 60 * 60
AUTH_RATE_WINDOW = 60
AUTH_RATE_MAX = 15
AI_RATE_WINDOW = 60
AI_RATE_MAX = 30
_oauth = None
_tenant_ready: set[str] = set()
_tenant_init_lock = threading.RLock()
MAX_REQUEST_BYTES = 20 * 1024 * 1024


class _AuthAccessLogFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple):
            cleaned = []
            for value in args:
                if isinstance(value, str) and "/auth/" in value:
                    value = value.split("?", 1)[0]
                cleaned.append(value)
            record.args = tuple(cleaned)
        return True


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class _RequestTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Bound request bodies even when Content-Length is absent or dishonest."""
    def __init__(self, app, max_bytes=MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        try:
            if int(headers.get(b"content-length", b"0")) > self.max_bytes:
                return await JSONResponse({"detail": "Request body is too large."}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "Invalid request size."}, status_code=400)(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.max_bytes:
                    raise _RequestTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestTooLarge:
            await JSONResponse({"detail": "Request body is too large."}, status_code=413)(scope, receive, send)


def _utc(seconds: int | None = None) -> str:
    value = datetime.fromtimestamp(seconds, timezone.utc) if seconds is not None else datetime.now(timezone.utc)
    return value.isoformat()


def _connect():
    if not postgres_enabled():
        AUTH_DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(AUTH_DB.parent, 0o700)
    db = connect_database(AUTH_DB, timeout=30, factory=_ClosingConnection, global_schema=True)
    if isinstance(db,sqlite3.Connection):
        db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _set_db_context(db, **values):
    """Set RLS context values for PostgreSQL control-table operations."""
    if not postgres_enabled():
        return
    for key, value in values.items():
        db.execute("SELECT set_config(?, ?, false)", ("babysteps." + key, str(value or "")))


def initialize():
    if postgres_enabled():
        # Runtime startup must never create or migrate public objects.
        with _connect() as db:
            missing=[name for name in ("auth_users","auth_sessions","auth_rate_limits","legacy_account_claims")
                     if not db.execute("SELECT to_regclass(?) AS relation",("public."+name,)).fetchone()["relation"]]
        if missing:
            raise RuntimeError("PostgreSQL control schema is not initialized. Run the operator migration command.")
        return
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS auth_users(
            user_id TEXT PRIMARY KEY,issuer TEXT NOT NULL,subject TEXT NOT NULL,
            email TEXT NOT NULL,email_verified INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,last_login_at TEXT NOT NULL,
            UNIQUE(issuer,subject))""")
        db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(
            session_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,revoked_at TEXT,
            FOREIGN KEY(user_id) REFERENCES auth_users(user_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id,expires_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS auth_rate_limits(
            bucket TEXT PRIMARY KEY,window_started INTEGER NOT NULL,hits INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS legacy_account_claims(
            claim_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,
            requested_at TEXT NOT NULL,reviewed_at TEXT,operator_user_id TEXT,
            UNIQUE(user_id),CHECK(state IN ('pending','approved','rejected')))""")
    if os.name != "nt" and AUTH_DB.exists():
        os.chmod(AUTH_DB, 0o600)


def initialize_postgres_control_schema():
    """Create global authentication tables from the separate migrator role."""
    if not postgres_enabled():
        raise RuntimeError("A PostgreSQL migrator connection is required.")
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS auth_users(
            user_id TEXT PRIMARY KEY,issuer TEXT NOT NULL,subject TEXT NOT NULL,
            email TEXT NOT NULL,email_verified INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,last_login_at TEXT NOT NULL,UNIQUE(issuer,subject))""")
        db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(
            session_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,revoked_at TEXT,
            FOREIGN KEY(user_id) REFERENCES auth_users(user_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id,expires_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS auth_rate_limits(
            bucket TEXT PRIMARY KEY,window_started INTEGER NOT NULL,hits INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS legacy_account_claims(
            claim_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,
            requested_at TEXT NOT NULL,reviewed_at TEXT,operator_user_id TEXT,
            UNIQUE(user_id),CHECK(state IN ('pending','approved','rejected')))""")
        checksum=hashlib.sha256(b"bsa-control-schema-v1-auth").hexdigest()
        db.execute("INSERT INTO bsa_schema_migrations(schema_name,version,description,checksum) VALUES('public',1,'authentication and tenant control tables',?) ON CONFLICT(schema_name,version) DO NOTHING",(checksum,))


def oidc_config():
    return {
        "issuer": os.environ.get("BABYSTEPS_OIDC_ISSUER", "").strip().rstrip("/"),
        "client_id": os.environ.get("BABYSTEPS_OIDC_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("BABYSTEPS_OIDC_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.environ.get("BABYSTEPS_OIDC_REDIRECT_URI", "").strip(),
        "public_origin": os.environ.get("BABYSTEPS_PUBLIC_ORIGIN", "").strip().rstrip("/"),
        "session_secret": os.environ.get("BABYSTEPS_SESSION_SECRET", ""),
        "recovery_url": os.environ.get("BABYSTEPS_OIDC_RECOVERY_URL", "").strip(),
        "operator_subject": os.environ.get("BABYSTEPS_OPERATOR_SUBJECT", "").strip(),
        "cookie_secure": os.environ.get("BABYSTEPS_COOKIE_SECURE", "true").strip().casefold() == "true",
    }


def validate_oidc_config():
    config = oidc_config()
    missing = [key for key in ("issuer", "client_id", "client_secret", "redirect_uri", "public_origin", "session_secret") if not config[key]]
    if missing:
        raise RuntimeError("OIDC mode requires provider, callback, public-origin, and session-secret configuration.")
    if len(config["session_secret"]) < 32:
        raise RuntimeError("BABYSTEPS_SESSION_SECRET must contain at least 32 characters.")
    for key in ("issuer", "redirect_uri", "public_origin"):
        parsed = urlsplit(config[key])
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("OIDC URL configuration is invalid.")
        if not config["cookie_secure"] and parsed.scheme == "https" and key == "public_origin":
            raise RuntimeError("Secure session cookies cannot be disabled for an HTTPS public origin.")
    if urlsplit(config["issuer"]).scheme != "https":
        raise RuntimeError("The identity-provider issuer must use HTTPS.")
    if urlsplit(config["public_origin"]).scheme != "https" and config["cookie_secure"]:
        # Secure cookies can be used in local HTTPS tests, but public configuration
        # must not advertise an insecure origin.
        if not config["public_origin"].startswith("https://"):
            raise RuntimeError("OIDC public origin must use HTTPS.")
    if not config["cookie_secure"] and not config["public_origin"].startswith("http://127.0.0.1") and not config["public_origin"].startswith("http://localhost"):
        raise RuntimeError("Insecure cookies are allowed only for loopback tests.")
    callback = urlsplit(config["redirect_uri"])
    origin = urlsplit(config["public_origin"])
    if callback.netloc != origin.netloc or callback.scheme != origin.scheme or callback.path != "/auth/callback":
        raise RuntimeError("The OIDC callback must be /auth/callback on the configured public origin.")
    return config


def _copy_private_tree(source: Path, destination: Path):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise HTTPException(409, "Legacy storage contains a symbolic link; operator review is required.")
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            if os.name != "nt":
                os.chmod(target, 0o600)
        else:
            raise HTTPException(409, "Legacy storage contains an unsupported file entry.")


def add_oidc_flow_middleware(app):
    if AUTH_MODE != "oidc":
        return
    config = validate_oidc_config()
    app.add_middleware(
        SessionMiddleware, secret_key=config["session_secret"],
        session_cookie="bsa_oidc_flow", max_age=600, same_site="lax",
        https_only=config["cookie_secure"], path="/auth/callback",
    )


def _client():
    global _oauth
    if _oauth is None:
        config = validate_oidc_config()
        oauth = OAuth()
        oauth.register(
            name="baby_steps_oidc", client_id=config["client_id"],
            client_secret=config["client_secret"],
            server_metadata_url=config["issuer"] + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
        _oauth = oauth
    return _oauth.baby_steps_oidc


def _hash_session(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issue_session(user_id: str) -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=SESSION_SECONDS)
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        db.execute("INSERT INTO auth_sessions(session_hash,user_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)",
                   (_hash_session(raw), user_id, now.isoformat(), expires.isoformat(), now.isoformat()))
    return raw, expires.isoformat()


def _session_user(raw: str | None):
    if not raw or len(raw) > 256:
        return None
    with _connect() as db:
        _set_db_context(db, session_hash=_hash_session(raw))
        row = db.execute("""SELECT s.user_id,u.active,u.email_verified FROM auth_sessions s
            JOIN auth_users u ON u.user_id=s.user_id
            WHERE s.session_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
            (_hash_session(raw), _utc())).fetchone()
        if not row or not row["active"] or not row["email_verified"]:
            return None
        db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE session_hash=?", (_utc(), _hash_session(raw)))
        return row["user_id"]


def _revoke_session(raw: str | None):
    if not raw or len(raw) > 256:
        return
    with _connect() as db:
        _set_db_context(db, session_hash=_hash_session(raw))
        db.execute("UPDATE auth_sessions SET revoked_at=? WHERE session_hash=? AND revoked_at IS NULL", (_utc(), _hash_session(raw)))


def _client_ip(request: Request) -> str:
    # Never trust X-Forwarded-For without a separately configured trusted proxy.
    return getattr(request.client, "host", "unknown") if request.client else "unknown"


def _rate_limit(key: str, limit: int, window: int = AUTH_RATE_WINDOW):
    bucket = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()
    now = int(time.time())
    with _connect() as db:
        _set_db_context(db, rate_limit_bucket=bucket)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT window_started,hits FROM auth_rate_limits WHERE bucket=?", (bucket,)).fetchone()
        if not row or now - int(row["window_started"]) >= window:
            db.execute("INSERT INTO auth_rate_limits(bucket,window_started,hits) VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET window_started=excluded.window_started,hits=1", (bucket, now))
            return
        if int(row["hits"]) >= limit:
            raise HTTPException(429, "Too many requests. Wait and try again.", headers={"Retry-After": str(max(1, window - (now - int(row["window_started"]))))})
        db.execute("UPDATE auth_rate_limits SET hits=hits+1 WHERE bucket=?", (bucket,))


def _safe_recovery_url():
    config = oidc_config()
    value = config["recovery_url"]
    if not value:
        return ""
    parsed = urlsplit(value)
    issuer = urlsplit(config["issuer"])
    if parsed.scheme != "https" or parsed.hostname != issuer.hostname or parsed.username or parsed.password:
        raise RuntimeError("OIDC recovery URL must be HTTPS on the configured identity-provider host.")
    return value


def _set_cookie(response: Response, raw: str, expires_at: str):
    config = oidc_config()
    max_age = max(0, int((datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(SESSION_COOKIE, raw, max_age=max_age, expires=max_age,
                        path="/", secure=config["cookie_secure"], httponly=True,
                        samesite="lax")
    response.headers["Cache-Control"] = "no-store"


def _verify_origin(request: Request):
    expected = oidc_config()["public_origin"]
    origin = request.headers.get("origin", "").rstrip("/")
    if not origin or not hmac.compare_digest(origin, expected):
        raise HTTPException(403, "Request origin is not allowed.")


def _initialize_tenant(user_id: str):
    with _tenant_init_lock:
        if user_id in _tenant_ready:
            return
        import main
        import job_search_agent
        import career_preferences
        import candidate_verification
        import application_answer_assistant
        import application_agent
        import resume_job_matcher
        import sarathi
        import onboarding
        import job_description_enricher
        import workflow_orchestrator
        def initialize_domain():
            with user_context(user_id):
                main.initialize()
                job_search_agent.initialize()
                career_preferences.initialize()
                candidate_verification.initialize()
                application_answer_assistant.initialize()
                application_agent.initialize()
                resume_job_matcher.initialize()
                sarathi.initialize()
                onboarding.initialize()
                job_description_enricher.initialize()
                workflow_orchestrator.initialize()
        if postgres_enabled():
            from migrations import tenant_migration_status
            status=tenant_migration_status(user_id)
            if not status.get("provisioned") or status.get("pending"):
                raise HTTPException(503,"Account database setup is pending operator provisioning.")
        else:initialize_domain()
        _tenant_ready.add(user_id)


async def auth_middleware(request: Request, call_next):
    if AUTH_MODE != "oidc":
        # Keep the legacy development identity reachable only from loopback.
        host = getattr(request.client, "host", "") if request.client else ""
        try:
            import ipaddress
            if not ipaddress.ip_address(host).is_loopback:
                return JSONResponse({"detail": "Local development access is restricted to this device."}, status_code=403)
        except ValueError:
            pass  # TestClient and ASGI in-process clients have non-IP placeholders.
        return await call_next(request)

    path = request.url.path
    public = path == "/health" or path in {"/auth/login", "/auth/signup", "/auth/callback", "/auth/recover"}
    if path == "/auth/logout":
        public = request.method == "POST"
    response = None
    if not public:
        user_id = _session_user(request.cookies.get(SESSION_COOKIE))
        if not user_id:
            if path.startswith("/api/") or path.startswith("/auth/") or path.startswith("/resumes"):
                return JSONResponse({"detail": "Authentication is required."}, status_code=401, headers={"Cache-Control": "no-store"})
            return RedirectResponse("/auth/login", status_code=303, headers={"Cache-Control": "no-store"})
        request.state.user_id = user_id
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                _verify_origin(request)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers={"Cache-Control": "no-store"})
        try:
            _rate_private_request(request, user_id)
            with user_context(user_id):
                _initialize_tenant(user_id)
                response = await call_next(request)
        except HTTPException as error:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
    else:
        if path == "/auth/logout":
            try:
                _verify_origin(request)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers={"Cache-Control": "no-store"})
        if path in {"/auth/login", "/auth/signup", "/auth/recover"}:
            try:
                _rate_limit("auth:" + _client_ip(request), AUTH_RATE_MAX)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
        response = await call_next(request)
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def _rate_private_request(request: Request, user_id: str):
    path = request.url.path
    if path in {"/api/sarathi/chat", "/api/application-answers/draft", "/api/jobs/search", "/api/jobs/analyze-matches"}:
        _rate_limit("ai:" + user_id + ":" + path, AI_RATE_MAX, AI_RATE_WINDOW)


async def _begin_login(request: Request, signup: bool):
    try:
        config = validate_oidc_config()
        redirect_uri = config["redirect_uri"]
        # Google creates a BabyStepsAI account on first successful sign-in and
        # does not use provider-specific signup hints. Keep the hint for OIDC
        # providers that support a hosted signup screen.
        extra = {"screen_hint": "signup"} if signup and config["issuer"] != "https://accounts.google.com" else {}
        return await _client().authorize_redirect(request, redirect_uri, **extra)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Identity provider is unavailable; try again later.") from None


@router.get("/auth/login")
async def login(request: Request):
    if AUTH_MODE != "oidc":
        return HTMLResponse(_auth_page("Sign in is not configured. Set up OIDC mode and provider credentials."), status_code=503)
    if request.cookies.get(SESSION_COOKIE) and _session_user(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return await _begin_login(request, False)


@router.get("/auth/signup")
async def signup(request: Request):
    if AUTH_MODE != "oidc":
        return HTMLResponse(_auth_page("Account creation is not configured."), status_code=503)
    return await _begin_login(request, True)


@router.get("/auth/recover")
async def recover_account():
    if AUTH_MODE != "oidc":
        return HTMLResponse(_auth_page("Account recovery is not configured."), status_code=503)
    recovery = _safe_recovery_url()
    if not recovery:
        return HTMLResponse(_auth_page("Use your identity provider's account recovery page."), status_code=503)
    return RedirectResponse(recovery, status_code=303)


@router.get("/auth/callback")
async def oidc_callback(request: Request):
    if AUTH_MODE != "oidc":
        raise HTTPException(404, "Not found.")
    try:
        _rate_limit("auth-callback:" + _client_ip(request), AUTH_RATE_MAX * 2)
        token = await _client().authorize_access_token(request)
        claims = token.get("userinfo") or {}
        issuer = str(claims.get("iss") or oidc_config()["issuer"]).rstrip("/")
        subject = str(claims.get("sub") or "")
        email = str(claims.get("email") or "").strip().casefold()
        verified = claims.get("email_verified") is True
        if not subject or len(subject) > 255 or not email or len(email) > 320 or not verified:
            raise HTTPException(403, "The identity provider must return a verified email address.")
        with _connect() as db:
            _set_db_context(db, oidc_issuer=issuer, oidc_subject=subject)
            row = db.execute("SELECT user_id FROM auth_users WHERE issuer=? AND subject=? AND active=1", (issuer, subject)).fetchone()
            if row:
                user_id = row["user_id"]
                _set_db_context(db, user_id=user_id)
                db.execute("UPDATE auth_users SET email=?,email_verified=1,last_login_at=? WHERE user_id=?", (email, _utc(), user_id))
            else:
                user_id = str(__import__("uuid").uuid4())
                _set_db_context(db, user_id=user_id)
                db.execute("INSERT INTO auth_users(user_id,issuer,subject,email,email_verified,created_at,last_login_at) VALUES(?,?,?,?,1,?,?)",
                           (user_id, issuer, subject, email, _utc(), _utc()))
        raw, expires_at = _issue_session(user_id)
        request.session.clear()
        response = RedirectResponse("/", status_code=303)
        _set_cookie(response, raw, expires_at)
        return response
    except HTTPException:
        raise
    except Exception as error:
        # Provider responses and tokens must not appear in logs or error output.
        raise HTTPException(401, "Identity verification failed. Start sign-in again.") from None


@router.post("/auth/logout")
async def logout(request: Request):
    _verify_origin(request)
    raw = request.cookies.get(SESSION_COOKIE)
    _revoke_session(raw)
    response = RedirectResponse("/auth/login", status_code=303)
    config = oidc_config()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=config["cookie_secure"], httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout-all")
async def logout_all(request: Request):
    _verify_origin(request)
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Authentication is required.")
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_utc(), user_id))
    response = RedirectResponse("/auth/login", status_code=303)
    config = oidc_config()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=config["cookie_secure"], httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/me")
async def auth_me(request: Request):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Authentication is required.")
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        row = db.execute("SELECT email,email_verified FROM auth_users WHERE user_id=? AND active=1", (user_id,)).fetchone()
    if not row:
        raise HTTPException(401, "Authentication is required.")
    return {"authenticated": True, "email": row["email"], "email_verified": bool(row["email_verified"])}


@router.post("/auth/legacy-claim/request", status_code=202)
async def request_legacy_claim(request: Request):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "Authentication is required.")
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        row = db.execute("SELECT 1 FROM legacy_account_claims WHERE user_id=? AND state IN ('pending','approved')", (user_id,)).fetchone()
        if row:
            raise HTTPException(409, "A legacy account claim is already pending or has been approved.")
        claim_id = secrets.token_urlsafe(24)
        db.execute("INSERT INTO legacy_account_claims(claim_id,user_id,state,requested_at) VALUES(?,?,'pending',?)",
                   (claim_id, user_id, _utc()))
    return {"claim_id": claim_id, "status": "pending_operator_review", "data_transferred": False}


def _is_operator(user_id: str) -> bool:
    configured = oidc_config()["operator_subject"]
    if not configured:
        return False
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        row = db.execute("SELECT subject FROM auth_users WHERE user_id=? AND active=1", (user_id,)).fetchone()
    return bool(row and hmac.compare_digest(configured, row["subject"]))


@router.post("/auth/legacy-claim/{claim_id}/approve")
async def approve_legacy_claim_route(claim_id: str, request: Request):
    operator_id = getattr(request.state, "user_id", None)
    if not operator_id or not _is_operator(operator_id):
        raise HTTPException(403, "Operator verification is required.")
    return approve_legacy_claim(claim_id, operator_id)


@router.get("/auth/legacy-claim/status")
async def legacy_claim_status(request: Request):
    user_id = getattr(request.state, "user_id", None)
    with _connect() as db:
        _set_db_context(db, user_id=user_id)
        row = db.execute("SELECT state,requested_at,reviewed_at FROM legacy_account_claims WHERE user_id=?", (user_id,)).fetchone()
    return {"status": row["state"] if row else "not_requested",
            "requested_at": row["requested_at"] if row else None,
            "reviewed_at": row["reviewed_at"] if row else None,
            "data_transferred": bool(row and row["state"] == "approved")}


def approve_legacy_claim(claim_id: str, operator_user_id: str, *, source_db: Path | None = None):
    """Operator-only migration primitive; copies, never moves, legacy data."""
    if postgres_enabled():
        raise HTTPException(409, "PostgreSQL account claims must use the offline staging migration tool; no records were transferred.")
    configured_operator = oidc_config()["operator_subject"]
    if not configured_operator or not _is_operator(operator_user_id):
        raise HTTPException(403, "Operator verification is required.")
    with _connect() as db:
        claim = db.execute("SELECT user_id,state FROM legacy_account_claims WHERE claim_id=?", (claim_id,)).fetchone()
        if not claim or claim["state"] != "pending":
            raise HTTPException(404, "Pending claim not found.")
        user_id = claim["user_id"]
    from tenant_context import user_context, user_data_directory
    from main import DB as LEGACY_DB
    origin = source_db or LEGACY_DB
    with user_context(user_id):
        from tenant_context import database_path
        target = database_path(origin)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Claims are allowed only for a newly created, still-empty tenant.
        if target.exists():
            existing = sqlite3.connect(target)
            try:
                for (table,) in existing.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    safe_table = table.replace('"', '""')
                    if existing.execute(f'SELECT 1 FROM "{safe_table}" LIMIT 1').fetchone():
                        raise HTTPException(409, "This account already has data; the legacy claim was not applied.")
            finally:
                existing.close()
        stage = target.parent / (".claim-stage-" + hashlib.sha256(claim_id.encode()).hexdigest()[:16])
        if stage.exists():
            raise HTTPException(409, "A previous claim staging directory requires operator review.")
        stage.mkdir(mode=0o700)
        staged_db = stage / origin.name
        preserved_empty_db = stage / ".empty-before-claim.sqlite3"
        if target.exists():
            shutil.copy2(target, preserved_empty_db)
        source = sqlite3.connect("file:" + str(origin.resolve()) + "?mode=ro", uri=True)
        dest = sqlite3.connect(staged_db)
        try:
            source.backup(dest)
            dest.execute("PRAGMA foreign_keys=OFF")
            table_names = [r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in table_names:
                columns = {r[1] for r in dest.execute("PRAGMA table_info(\"" + table.replace("\"", "\"\"") + "\")")}
                if "user_id" in columns:
                    dest.execute("UPDATE \"" + table.replace("\"", "\"\"") + "\" SET user_id=? WHERE user_id=?", (user_id, LEGACY_USER_ID))
            dest.commit()
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Claim copy integrity check failed.")
        finally:
            source.close()
            dest.close()
        legacy_dirs = ("private_resumes", "private_documents", "private_application_artifacts")
        for dirname in legacy_dirs:
            source_dir = origin.parent / dirname
            target_dir = target.parent / dirname
            if target_dir.exists() and any(target_dir.iterdir()):
                shutil.rmtree(stage)
                raise HTTPException(409, "This account already has private files; the legacy claim was not applied.")
            if source_dir.exists():
                _copy_private_tree(source_dir, stage / dirname)
        for dirname in legacy_dirs:
            staged_dir = stage / dirname
            target_dir = target.parent / dirname
            if staged_dir.exists():
                if target_dir.exists():
                    target_dir.rmdir()
                os.replace(staged_dir, target_dir)
        if target.exists():
            target.unlink()
        os.replace(staged_db, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
        _tenant_ready.discard(user_id)
        try:
            _initialize_tenant(user_id)
        except Exception:
            target.unlink(missing_ok=True)
            if preserved_empty_db.exists():
                os.replace(preserved_empty_db, target)
            for dirname in legacy_dirs:
                shutil.rmtree(target.parent / dirname, ignore_errors=True)
            _tenant_ready.discard(user_id)
            shutil.rmtree(stage, ignore_errors=True)
            raise HTTPException(500, "The legacy copy could not be initialized; the empty account was restored.") from None
        shutil.rmtree(stage, ignore_errors=True)
    with _connect() as db:
        db.execute("UPDATE legacy_account_claims SET state='approved',reviewed_at=?,operator_user_id=? WHERE claim_id=? AND state='pending'",
                   (_utc(), operator_user_id, claim_id))
    return {"approved": True, "data_copied": True, "source_preserved": True}


def _auth_page(message: str = "") -> str:
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>BabyStepsAI sign in</title>
<main style=\"max-width:34rem;margin:12vh auto;padding:2rem;font:16px/1.5 system-ui\"><h1>BabyStepsAI</h1><p>Sign in securely with your account provider.</p><p>New here? Continue to provider signup. Password recovery and email verification are handled by the provider.</p><p>""" + safe + """</p><p><a href=\"/auth/login\">Sign in</a> · <a href=\"/auth/signup\">Create account</a> · <a href=\"/auth/recover\">Recover account</a></p></main></html>"""


def configure_auth_routes(app):
    if AUTH_MODE == "oidc":
        validate_oidc_config()
        initialize()
    app.add_middleware(RequestSizeLimitMiddleware)
    add_oidc_flow_middleware(app)
    app.include_router(router)
    app.middleware("http")(auth_middleware)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _AuthAccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(_AuthAccessLogFilter())
