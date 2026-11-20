# Authentication and account isolation

BabyStepsAI uses Authlib's maintained OpenID Connect client with a managed identity provider. The provider handles registration, passwords, password recovery, and verified email. BabyStepsAI does not store passwords.

## Local development

The default `BABYSTEPS_AUTH_MODE=local` retains the existing local applicant database and is restricted to loopback clients. It is not suitable for a network deployment.

For a local Google OIDC test, run Uvicorn from `BabyStepsAI_Backend` with `--env-file .env --host 127.0.0.1 --port 8765`. Register the exact callback `http://127.0.0.1:8765/auth/callback` in the Google Web Application client. The ignored backend `.env` should contain `BABYSTEPS_AUTH_MODE=oidc`, issuer `https://accounts.google.com`, matching callback and public origin `http://127.0.0.1:8765`, a private random `BABYSTEPS_SESSION_SECRET`, and `BABYSTEPS_COOKIE_SECURE=false`. The local config must not load the root `.env` or point `BABYSTEPS_DATABASE_URL` at Supabase; use isolated `BABYSTEPS_AUTH_DB` and `BABYSTEPS_DATA_ROOT` paths while testing. Insecure cookies are accepted only for loopback URLs.

## Free production provider: Google OIDC

The existing Authlib integration is a generic OIDC relying party. Google supports its authorization-code flow, discovery metadata, ID-token signing/validation, email and `email_verified` claims, and the S256 PKCE challenge configured by the application. First-time Google sign-in creates a BabyStepsAI account; Google handles the user's signup, password, recovery, and email verification. BabyStepsAI does not receive or store Google passwords.

In Google Cloud Console, configure the OAuth consent screen/branding and create an OAuth client of type **Web application**. Add the exact production callback `https://<public-host>/auth/callback` as an authorized redirect URI. Use the Google issuer `https://accounts.google.com`. The configured app requests only `openid email profile`; it does not request Google API access. Google may restrict an external app left in Testing to listed test users, so public signup requires completing Google's publishing requirements for the consent screen.

Set `BABYSTEPS_AUTH_MODE=oidc` and configure `BABYSTEPS_OIDC_ISSUER`, `BABYSTEPS_OIDC_CLIENT_ID`, `BABYSTEPS_OIDC_CLIENT_SECRET`, `BABYSTEPS_OIDC_REDIRECT_URI`, `BABYSTEPS_PUBLIC_ORIGIN`, and `BABYSTEPS_SESSION_SECRET` in the backend deployment's secret store. The callback must be exactly `/auth/callback` on the HTTPS public origin. Keep `BABYSTEPS_COOKIE_SECURE=true`. Leave `BABYSTEPS_OIDC_RECOVERY_URL` unset for Google so the app instructs users to use their Google account recovery flow. No Supabase database or Storage key is used for authentication.

Supabase Auth's OAuth Server can act as an OIDC identity provider and its discovery URL follows the issuer path. However, its current OAuth Server flow redirects to an authorization UI that the relying party must build and host, including Supabase Auth sign-in and consent. That feature is currently documented as beta and is not a drop-in replacement for this application's existing login/signup route. It would require a separate UI and flow implementation before BabyStepsAI could use it safely.

The application stores only a random opaque session cookie in the browser. Session identifiers are hashed in the authentication database and expire after 12 hours. Logout revokes the current session; `/auth/logout-all` revokes all sessions for the signed-in account. OIDC state, nonce, ID-token validation, and PKCE are handled by Authlib. Logging out of BabyStepsAI revokes its local session; it does not sign the user out of their Google account.

SQLite development accounts receive separate local databases and private file directories under `BABYSTEPS_DATA_ROOT`. PostgreSQL tenant operations connect through a deterministic per-tenant login derived from `BABYSTEPS_TENANT_DB_SECRET`; each login has grants on only its own schema. The shared `bsa_runtime` role is limited to control/authentication tables and cannot use tenant schemas. Forced RLS is defense in depth; schema privileges are the primary tenant boundary. Keep `BABYSTEPS_MIGRATOR_DATABASE_URL` and `BABYSTEPS_TENANT_DB_SECRET` only in operator/deployment secret stores, never in frontend code. The operator migrator needs CREATEROLE to provision restricted tenant logins; it is never used for web traffic. Tenant roles have no role/database creation, superuser, or RLS-bypass attributes. Resume PDFs and extracted resume text are not individually encrypted by the application. Use Supabase's encrypted private storage and protect the sensitive-data encryption key in a separate secret manager. Back up the database, object storage, and required encryption keys together.

For PostgreSQL, run `python migrations.py init-control` and then `python migrations.py migrate --tenant-id <verified-auth-user-id>` with the migration credential. The API never creates tenant schemas at request time; an unprovisioned account receives a service-unavailable response until an operator applies the versioned migration. The migration status command is read-only. Do not give the web service the migration URL.

For private Supabase Storage, set `BABYSTEPS_STORAGE_BACKEND=supabase`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and `SUPABASE_PRIVATE_BUCKET` only in the backend secret store. The legacy `SUPABASE_SERVICE_ROLE_KEY` JWT is supported as a migration fallback. New `sb_secret_` keys are sent only in the `apikey` header; legacy JWTs also use the Bearer authorization header. Run `python storage_setup.py` from an operator environment to create or verify a private bucket. Object keys use a tenant hash and opaque file name; the app does not issue public or signed URLs. Local development continues to use per-user private directories. The deployment operator must configure TLS termination, trusted proxy behavior, health monitoring, and tested backup restoration before launch.

## Existing local applicant data

OIDC accounts start empty. No legacy records are automatically assigned. An authenticated account may request a legacy claim; the request copies nothing. Only the configured operator subject can approve it. Approval copies the legacy SQLite database and private files into an empty account, rewrites legacy ownership keys in the copy, verifies database integrity, and preserves the source. Do not approve a claim until the operator has independently verified the applicant's identity and authority over the local data.

## Scope and safeguards

All non-allowlisted routes require a verified session in OIDC mode. Unsafe methods require an exact same-origin `Origin` header. Private responses are marked `no-store`, request bodies are capped at 20 MiB, and authentication and AI endpoints use persistent rate limits. The Auto Apply scheduler does not start in OIDC mode, and employer submission remains disabled.

The Google OAuth client, consent-screen publication, HTTPS origin, private bucket provisioning, database SSL enforcement, production secrets, backup/restore process, and monitored tenant provisioning still require deployment-specific configuration and verification. Authentication code alone does not make a public deployment ready.
