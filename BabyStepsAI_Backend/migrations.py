"""Versioned, additive PostgreSQL schema bootstrap and status reporting.

Version 1 delegates idempotent table/column creation to the existing domain
initializers. The runner records the version only after every initializer
completes, so a partial run is retried safely. It never drops data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

from database_adapter import _pool, ensure_tenant_schema, registered_tenant_schema, postgres_enabled


MIGRATIONS=(
    (1,"application schema bootstrap",hashlib.sha256(b"bsa-tenant-schema-v1-additive").hexdigest()),
    (2,"forced tenant row security",hashlib.sha256(b"bsa-tenant-schema-v2-forced-rls").hexdigest()),
)
CONTROL_RLS_CHECKSUM=hashlib.sha256(b"bsa-control-schema-v2-forced-rls-and-tenant-roles").hexdigest()


def _apply_tenant_schema(user_id: str, initialize_domain):
    schema=ensure_tenant_schema(user_id)
    pool=_pool();lock_connection=pool.getconn()
    lock_key="baby-steps-migration:"+schema
    try:
        lock_connection.autocommit=True
        lock_connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(user_id,))
        lock_connection.execute("SELECT set_config('babysteps.tenant_schema',%s,false)",(schema,))
        lock_connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))",(lock_key,))
        current=lock_connection.execute("SELECT COALESCE(MAX(version),0) FROM public.bsa_schema_migrations WHERE schema_name=%s",(schema,)).fetchone()[0]
        for version,description,checksum in MIGRATIONS:
            if current>=version:continue
            # Initializers are additive and idempotent. The version is committed
            # only after all of them succeed, permitting safe retry after error.
            if version==1:initialize_domain()
            if version==2:_secure_tenant_schema(schema,user_id)
            lock_connection.execute("INSERT INTO public.bsa_schema_migrations(schema_name,version,description,checksum) VALUES(%s,%s,%s,%s) ON CONFLICT(schema_name,version) DO NOTHING",
                                   (schema,version,description,checksum))
            current=version
        # Always repair policy/grant drift even when both versions were applied.
        _secure_tenant_schema(schema,user_id)
        return schema
    finally:
        try:lock_connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))",(lock_key,))
        except Exception:pass
        lock_connection.autocommit=False
        pool.putconn(lock_connection)


def tenant_migration_status(user_id: str):
    try:
        schema=registered_tenant_schema(user_id)
    except RuntimeError:
        return {"schema":None,"applied":[],"pending":[{"version":v,"description":d} for v,d,_ in MIGRATIONS],"provisioned":False}
    with _pool().connection() as connection:
        connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(user_id,))
        connection.execute("SELECT set_config('babysteps.tenant_schema',%s,false)",(schema,))
        rows=connection.execute("SELECT version,description,checksum,applied_at FROM public.bsa_schema_migrations WHERE schema_name=%s ORDER BY version",(schema,)).fetchall()
    return {"schema":schema,"applied":[{"version":r[0],"description":r[1],"checksum":r[2],"applied_at":r[3].isoformat() if r[3] else None} for r in rows],
            "pending":[{"version":version,"description":description} for version,description,_ in MIGRATIONS if version not in {r[0] for r in rows}],"provisioned":True}


def _secure_tenant_schema(schema: str, user_id: str | None = None):
    """Apply fail-closed schema-bound RLS and least-privilege table grants."""
    from psycopg import sql

    if not schema.startswith("tenant_") or not schema[7:].isalnum():
        raise ValueError("Invalid tenant schema identifier.")
    pool=_pool();connection=pool.getconn()
    secret=os.environ.get("BABYSTEPS_TENANT_DB_SECRET", "")
    if len(secret)<32:
        pool.putconn(connection)
        raise RuntimeError("BABYSTEPS_TENANT_DB_SECRET must be configured before provisioning tenant roles.")
    if user_id is None:
        row=connection.execute("SELECT user_id FROM public.bsa_tenant_registry WHERE schema_name=%s",(schema,)).fetchone()
        user_id=row[0] if row else None
    if not user_id or schema != "tenant_"+hashlib.sha256(user_id.encode()).hexdigest()[:48]:
        pool.putconn(connection)
        raise RuntimeError("Tenant schema ownership could not be verified.")
    from psycopg import sql
    import hmac
    role="bsa_t_"+schema.removeprefix("tenant_")
    try:
        connection.execute("SET search_path TO public")
        can_create=connection.execute("SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone()
        if not can_create or not can_create[0]:
            raise RuntimeError("The operator migration role needs CREATEROLE to provision isolated tenant logins.")
        password=hmac.new(secret.encode(),user_id.encode(),hashlib.sha256).hexdigest()
        role_exists=connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(role,)).fetchone()
        attrs=sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS CONNECTION LIMIT 2 PASSWORD ")+sql.Literal(password)
        if not role_exists:
            connection.execute(sql.SQL("CREATE ROLE {} WITH ").format(sql.Identifier(role))+attrs)
        else:
            existing=connection.execute("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s",(role,)).fetchone()
            if not existing or tuple(existing)!=(True,False,False,False,False):
                raise RuntimeError("An existing tenant login has unexpected privileges; operator repair is required.")
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(connection.execute("SELECT current_database()").fetchone()[0]),sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM bsa_runtime").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM bsa_runtime").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM bsa_runtime").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        connection.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE bsa_migrator IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        connection.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE bsa_migrator IN SCHEMA {} GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        rows=connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",(schema,)).fetchall()
        for row in rows:
            table=row[0]
            target=sql.Identifier(schema,table)
            policy=sql.Identifier("bsa_tenant_schema_isolation")
            predicate=sql.SQL("current_setting('babysteps.tenant_schema', true) = {}").format(sql.Literal(schema))
            connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(policy,target))
            connection.execute(sql.SQL("CREATE POLICY {} ON {} USING ({}) WITH CHECK ({})").format(policy,target,predicate,predicate))
            connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(target,sql.Identifier(role)))
        views=connection.execute("SELECT viewname FROM pg_views WHERE schemaname=%s ORDER BY viewname",(schema,)).fetchall()
        for (view,) in views:
            connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(schema,view),sql.Identifier(role)))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        pool.putconn(connection)


def control_migration_status():
    if not postgres_enabled():return {"backend":"sqlite","versioned_migrations":False}
    rows=_pool().getconn()
    try:
        rows.execute("SELECT set_config('babysteps.tenant_schema','',false)")
        exists=rows.execute("SELECT to_regclass('public.bsa_schema_migrations')").fetchone()[0]
        if not exists:return {"backend":"postgresql","applied":[],"initialized":False}
        result=rows.execute("SELECT version,description,checksum,applied_at FROM public.bsa_schema_migrations WHERE schema_name='public' ORDER BY version").fetchall()
        return {"backend":"postgresql","applied":[{"version":r[0],"description":r[1],"checksum":r[2],"applied_at":r[3].isoformat() if r[3] else None} for r in result],"initialized":True}
    finally:_pool().putconn(rows)


def _initialize_domain_schema(user_id: str):
    import main,job_search_agent,career_preferences,candidate_verification
    import application_answer_assistant,application_agent,resume_job_matcher,sarathi
    import onboarding,job_description_enricher,workflow_orchestrator
    from tenant_context import user_context
    from database_adapter import migration_database_context
    with migration_database_context(), user_context(user_id):
        main.initialize();job_search_agent.initialize();career_preferences.initialize()
        candidate_verification.initialize();application_answer_assistant.initialize()
        application_agent.initialize();resume_job_matcher.initialize();sarathi.initialize()
        onboarding.initialize();job_description_enricher.initialize();workflow_orchestrator.initialize()


def migrate_tenant(user_id: str):
    if not postgres_enabled():raise RuntimeError("BABYSTEPS_DATABASE_URL is not configured.")
    return _apply_tenant_schema(user_id,lambda:_initialize_domain_schema(user_id))


def initialize_control_schema():
    if not postgres_enabled():raise RuntimeError("BABYSTEPS_MIGRATOR_DATABASE_URL is not configured.")
    from database_adapter import _control_schema
    _control_schema()
    import authentication
    authentication.initialize_postgres_control_schema()
    _secure_control_schema()
    with _pool().connection() as connection:
        connection.execute("SELECT set_config('babysteps.tenant_schema','',false)")
        connection.execute("INSERT INTO public.bsa_schema_migrations(schema_name,version,description,checksum) VALUES('public',2,'forced control-table row security',%s) ON CONFLICT(schema_name,version) DO NOTHING",(CONTROL_RLS_CHECKSUM,))


def _secure_control_schema():
    """Force row policies on shared control tables; runtime context is explicit."""
    from psycopg import sql
    pool=_pool();connection=pool.getconn()
    policies={
        "auth_users":("(user_id=current_setting('babysteps.user_id',true) OR (issuer=current_setting('babysteps.oidc_issuer',true) AND subject=current_setting('babysteps.oidc_subject',true)) OR EXISTS (SELECT 1 FROM public.auth_sessions s WHERE s.user_id=auth_users.user_id AND s.session_hash=current_setting('babysteps.session_hash',true)))",
                     "(user_id=current_setting('babysteps.user_id',true) OR (issuer=current_setting('babysteps.oidc_issuer',true) AND subject=current_setting('babysteps.oidc_subject',true)))"),
        "auth_sessions":("(session_hash=current_setting('babysteps.session_hash',true) OR user_id=current_setting('babysteps.user_id',true))",
                         "(session_hash=current_setting('babysteps.session_hash',true) OR user_id=current_setting('babysteps.user_id',true))"),
        "auth_rate_limits":("bucket=current_setting('babysteps.rate_limit_bucket',true)",
                            "bucket=current_setting('babysteps.rate_limit_bucket',true)"),
        "legacy_account_claims":("user_id=current_setting('babysteps.user_id',true)",
                                 "user_id=current_setting('babysteps.user_id',true)"),
        "bsa_tenant_registry":("user_id=current_setting('babysteps.user_id',true)",
                                "user_id=current_setting('babysteps.user_id',true)"),
        "bsa_schema_migrations":("(schema_name='public' OR schema_name=current_setting('babysteps.tenant_schema',true))",
                                  "(schema_name='public' OR schema_name=current_setting('babysteps.tenant_schema',true))"),
    }
    try:
        for table,(using,check) in policies.items():
            target=sql.Identifier("public",table)
            connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("DROP POLICY IF EXISTS bsa_control_isolation ON {}").format(target))
            connection.execute(sql.SQL("CREATE POLICY bsa_control_isolation ON {} USING ({}) WITH CHECK ({})").format(target,sql.SQL(using),sql.SQL(check)))
            if table in {"auth_users","auth_sessions","auth_rate_limits","legacy_account_claims"}:
                connection.execute(sql.SQL("REVOKE DELETE ON {} FROM bsa_runtime").format(target))
        connection.commit()
    except Exception:
        connection.rollback();raise
    finally:
        pool.putconn(connection)


def _load_operator_env():
    env=__import__("pathlib").Path(__file__).resolve().parent.parent/".env"
    if not env.is_file():return
    for line in env.read_text().splitlines():
        value=line.strip()
        if not value or value.startswith("#") or "=" not in value:continue
        key,raw=value.split("=",1);raw=raw.strip()
        if len(raw)>=2 and raw[0]==raw[-1] and raw[0] in "'\"":raw=raw[1:-1]
        os.environ.setdefault(key.strip(),raw)


def _cli():
    _load_operator_env()
    parser=argparse.ArgumentParser(description="Manage versioned PostgreSQL schemas using the separate migrator credential.")
    parser.add_argument("command",choices=("status","init-control","migrate"))
    parser.add_argument("--tenant-id",default="",help="Authenticated tenant UUID to inspect; never used as an authorization credential.")
    args=parser.parse_args()
    if args.command in {"init-control","migrate"}:
        migrator=os.environ.get("BABYSTEPS_MIGRATOR_DATABASE_URL","").strip()
        if not migrator:parser.error("BABYSTEPS_MIGRATOR_DATABASE_URL must be configured for migrations.")
        from database_adapter import close_postgres_pool
        close_postgres_pool()
        os.environ["BABYSTEPS_DATABASE_URL"]=migrator
        if args.command=="init-control":
            initialize_control_schema()
            print(json.dumps(control_migration_status(),indent=2,sort_keys=True));return
        if not args.tenant_id:parser.error("--tenant-id is required for migrate.")
        initialize_control_schema()
        print(json.dumps({"tenant_schema":migrate_tenant(args.tenant_id),"status":tenant_migration_status(args.tenant_id)},indent=2,sort_keys=True));return
    result=control_migration_status()
    if args.tenant_id:result["tenant"]=tenant_migration_status(args.tenant_id)
    print(json.dumps(result,indent=2,sort_keys=True))


if __name__=="__main__":_cli()
