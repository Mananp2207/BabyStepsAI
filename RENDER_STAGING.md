# Render Free staging

This is a staging configuration for intermittent, supervised use. It is not a
production availability commitment. Render Free sleeps after 15 minutes without
inbound traffic, typically takes about a minute to wake, may restart at any
time, and has an ephemeral filesystem. It provides 0.1 CPU and 512 MB RAM.
There are no free Render worker or cron service types. The app can serve normal
requests without a separate worker; user-started background work is in-process
and may be interrupted by sleep/restart. Do not use this service for unattended
Auto Apply, employer preparation, or submission.

## Create the service

1. Confirm the credentials exposed in the earlier screenshot have been rotated.
   Do not continue until rotation and the security checks in the release checklist
   are complete.
2. In Render, create a Blueprint from this repository and select the
   `feature/phase-11-workflow` branch. The Blueprint sets `plan: free`, uses the
   root Docker context, and disables automatic deploys. Do not add a payment
   method or enable paid features. With no payment method, Render suspends the
   service/builds when included usage is exhausted rather than charging for
   overages.
3. Confirm the service's actual `*.onrender.com` URL. It can differ if the
   service name is unavailable. In Render's private environment settings, set:

   - `BABYSTEPS_PUBLIC_ORIGIN=https://<actual-service>.onrender.com`
   - `BABYSTEPS_OIDC_REDIRECT_URI=https://<actual-service>.onrender.com/auth/callback`
   - `BABYSTEPS_OIDC_CLIENT_ID` and `BABYSTEPS_OIDC_CLIENT_SECRET` from the
     rotated Google Web OAuth client
   - `BABYSTEPS_DATABASE_URL` to the Supabase **Session Pooler**, port 5432,
     using the restricted `bsa_runtime` database role and `sslmode=require`
   - `BABYSTEPS_TENANT_DB_SECRET` from the protected deployment secret store
   - `SUPABASE_URL` and `SUPABASE_SECRET_KEY` for the same project; the key must
     be a server-side `sb_secret_` key

   Never add a Supabase administrator or migrator URL to the web service. Do not
   put secrets in this file, Git, build arguments, or logs. Render's `sync: false`
   values are requested on initial Blueprint creation; add or edit them manually
   in the service environment when syncing an existing Blueprint.
4. In Google Cloud Console, add the exact
   `https://<actual-service>.onrender.com/auth/callback` URI to the OAuth Web
   client. Keep the issuer `https://accounts.google.com`. Google consent-screen
   publishing/testing status may limit who can sign in.
5. Set `SUPABASE_PRIVATE_BUCKET=babystepsai-private` and verify that the bucket is
   private. Use the project's session pooler, not transaction pooler port 6543.
   The application expects schemas and tenant roles to have been provisioned
   separately; this staging service does not run migrations or import local data.
6. Keep Supabase Storage and PostgreSQL secrets only in Render's encrypted
   environment settings. `GROQ_API_KEY` is optional; if used, add it only after
   rotation and only with the applicable applicant consent controls in the app.
   Do not configure local Ollama or point this service at SQLite.
7. Review environment settings and security tests, then manually trigger the
   first deploy. The Blueprint has `autoDeployTrigger: off` to prevent a push
   from deploying automatically. Check `GET /health` and complete synthetic,
   authenticated browser checks before sharing the URL.

## Runtime expectations

The Dockerfile starts Uvicorn on `0.0.0.0:$PORT` (Render supplies `PORT`,
defaulting to 10000) and uses `/health` for the service health check. The app's
OIDC mode disables the local Auto Apply scheduler. It still has in-process
executors for some discovery/matching work; a sleeping or restarted Free
instance can interrupt those jobs. The browser automation/Playwright-dependent
employer preparation is not part of the staging service. Keep all final
submission paths disabled.

Render Free has no persistent disk. `BABYSTEPS_DATA_ROOT` is intentionally under
`/tmp`; it is disposable cache only. PostgreSQL and private Supabase Storage must
hold durable account data and uploaded files. The health check is not a database
readiness guarantee. Monitor Render's memory, restart, bandwidth, and build
usage panels; a memory limit of 512 MB is tight for a Python application and
native dependencies, so staging may restart or fail under concurrent/heavy work.

## Free-tier backup and recovery

Supabase Free does not provide downloadable automatic database backups. At a
regular operator-defined interval, use the Supabase CLI to create a logical
database dump and copy it to encrypted storage outside Supabase. Separately
download and checksum private Storage objects; a database dump includes Storage
metadata, not the object contents. Keep the tenant secret and other necessary
recovery keys in a separate encrypted secret backup. Restrict backup access and
retention. Periodically restore the dump and objects into an isolated test
project/local fixture, verify account and object references, then securely
remove the test copy. Do not test restores against the live project. There is no
free automatic recovery-point objective; a missed manual backup can mean data
loss, and a paused Supabase Free project requires operator reactivation.

## Release gate

This configuration is **not authorization to deploy**. Do not deploy until the
operator confirms screenshot-exposed credentials have been rotated, the
Google callback is configured, the restricted runtime database and tenant
roles are verified, the private bucket is verified, and authenticated
two-user/browser security checks pass against synthetic accounts. No local
applicant data should be migrated. Keep public beta NO-GO while any of these
conditions is outstanding.
