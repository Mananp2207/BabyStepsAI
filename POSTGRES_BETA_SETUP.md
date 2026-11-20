# PostgreSQL beta preparation

BabyStepsAI uses Psycopg with per-account PostgreSQL schemas. Tenant traffic
connects as a distinct, non-admin database login with privileges on that
tenant's schema only. The shared runtime login is restricted to authentication
and control tables. Forced RLS provides defense in depth; schema USAGE and
table grants form the database-enforced cross-tenant boundary because a
session-set custom GUC by itself is not an authorization credential.

## Runtime configuration

Set only the following in the web service's secret store:

- `BABYSTEPS_DATABASE_URL`: `bsa_runtime` Session Pooler connection, port 5432,
  with TLS required.
- `BABYSTEPS_RUNTIME_DB_ROLE=bsa_runtime`.
- `BABYSTEPS_TENANT_DB_SECRET`: a unique server-side secret of at least 32
  characters, used to derive tenant database login passwords. Preserve it in
  backup/recovery and never put it in browser configuration.
- `BABYSTEPS_AUTH_MODE=oidc` and the Authlib OIDC variables documented in
  `BabyStepsAI_Backend/AUTHENTICATION.md`.
- `BABYSTEPS_STORAGE_BACKEND=supabase`, `SUPABASE_URL`,
  `SUPABASE_SECRET_KEY`, and `SUPABASE_PRIVATE_BUCKET`. The legacy
  `SUPABASE_SERVICE_ROLE_KEY` is supported only as a migration fallback.
- `BABYSTEPS_COOKIE_SECURE=true` and the HTTPS public origin.

Never configure `BABYSTEPS_MIGRATOR_DATABASE_URL` in the web service. Keep it
in a short-lived operator migration job. The migrator has `CREATEROLE` only so
it can provision per-tenant logins; it has no SUPERUSER or BYPASSRLS. The
runtime and tenant roles have no database/schema/role creation privileges and
cannot bypass RLS.

## Migrations and tenant provisioning

Run `python provision_postgres_roles.py` once from an operator environment
while the private `.env` contains the administrator Session Pooler URL. It
creates separate runtime and migrator roles, transfers ownership to the
migrator, revokes shared-runtime tenant grants, and stores role URLs in the
ignored, mode-0600 `.env`. Do not run it against applicant data without a
verified backup.

Initialize global tables using the migration credential:

```sh
python migrations.py init-control
python migrations.py migrate --tenant-id <verified-auth-user-id>
```

The tenant ID must come from a verified account record. Runtime requests do
not create schemas. An account without migration version 1 receives a 503
until an operator provisions its schema. `python migrations.py status` is
read-only.

`migrations.py migrate --tenant-id ...` creates or repairs that tenant's
restricted login and schema grants. Tenant roles are deterministically derived
from the authenticated user ID and `BABYSTEPS_TENANT_DB_SECRET`; losing that
secret requires operator role-password recovery. The current Supabase Free
database should be treated as a small supervised beta because each tenant
requires a PostgreSQL role and a small per-tenant connection pool.

## Private Supabase Storage

Set the storage variables only in the backend secret store. Then run
`python storage_setup.py` from an operator environment to create or verify a
private bucket. Objects are accessed through authenticated backend routes,
under a SHA-256 tenant prefix and opaque file name. The application never
returns public URLs or signed URLs. Uploads enforce extension, MIME signature,
and size checks. Local development remains on per-user private files. Supabase
Storage credentials are not required for local mode. No live bucket is
provisioned until the private service key is configured.

## Release limitations

The current project still requires Supabase SSL enforcement to be enabled and
verified on both pooler legs, private bucket provisioning, production OIDC
configuration, backup/restore drills, and supervised tenant provisioning.
The service-role key must be available in the backend secret store before
remote storage can be enabled. New user registration does not automatically
run elevated migrations; beta accounts must be provisioned by an operator.
SQLite-to-PostgreSQL data import is not implemented and local applicant data
must not be copied automatically.
