"""Opt-in tests for restricted PostgreSQL runtime and migration roles."""
from __future__ import annotations

import os
import sqlite3
import unittest
import uuid
from contextlib import contextmanager


POSTGRES_TESTS=(os.environ.get("BABYSTEPS_RUN_POSTGRES_TESTS")=="1" and
                bool(os.environ.get("BABYSTEPS_DATABASE_URL")) and
                bool(os.environ.get("BABYSTEPS_MIGRATOR_DATABASE_URL")))


@contextmanager
def _using_url(url):
    from database_adapter import close_postgres_pool
    old=os.environ.get("BABYSTEPS_DATABASE_URL")
    close_postgres_pool();os.environ["BABYSTEPS_DATABASE_URL"]=url
    try:yield
    finally:
        close_postgres_pool()
        if old is None:os.environ.pop("BABYSTEPS_DATABASE_URL",None)
        else:os.environ["BABYSTEPS_DATABASE_URL"]=old


@unittest.skipUnless(POSTGRES_TESTS,"requires explicit runtime and migrator PostgreSQL URLs")
class PostgresTenantAdapterTests(unittest.TestCase):
    def test_forced_rls_blocks_explicit_cross_schema_read_and_write(self):
        from database_adapter import _pool,close_postgres_pool,connect_database,ensure_tenant_schema,migration_database_context
        from migrations import _secure_tenant_schema
        from tenant_context import user_context
        suffix=uuid.uuid4().hex
        tenants=("synthetic-rls-a-"+suffix,"synthetic-rls-b-"+suffix)
        schemas=[]
        migrator=os.environ["BABYSTEPS_MIGRATOR_DATABASE_URL"]
        try:
            with _using_url(migrator):
                for tenant in tenants:
                    schema=ensure_tenant_schema(tenant);schemas.append(schema)
                    with migration_database_context(),user_context(tenant),connect_database("unused.sqlite") as db:
                        db.execute("CREATE TABLE tenant_probe(id TEXT PRIMARY KEY, owner TEXT NOT NULL)")
                        db.execute("INSERT INTO tenant_probe(id,owner) VALUES(?,?)",("fixture",tenant))
                    _secure_tenant_schema(schema,tenant)
            schema_a,schema_b=schemas
            for schema in schemas:
                role="bsa_t_"+schema.removeprefix("tenant_")
                with _pool().connection() as audit:
                    attrs=audit.execute("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s",(role,)).fetchone()
                    self.assertEqual(tuple(attrs),(True,False,False,False,False))
                    self.assertFalse(audit.execute("SELECT has_database_privilege(%s,current_database(),'CREATE')",(role,)).fetchone()[0])
            with user_context(tenants[0]),connect_database("unused.sqlite") as db:
                own=db.execute("SELECT owner FROM tenant_probe WHERE id=?",("fixture",)).fetchone()
                self.assertEqual(own["owner"],tenants[0])
                db.execute("SELECT set_config('babysteps.tenant_schema',%s,false)",(schema_b,))
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute(f'SELECT owner FROM "{schema_b}".tenant_probe WHERE id=?',("fixture",)).fetchone()
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute(f'INSERT INTO "{schema_b}".tenant_probe(id,owner) VALUES(?,?)',("cross-write",tenants[0]))
            with user_context(tenants[1]),connect_database("unused.sqlite") as db:
                self.assertEqual(db.execute("SELECT owner FROM tenant_probe WHERE id=?",("fixture",)).fetchone()["owner"],tenants[1])
                self.assertEqual(db.execute("SELECT current_setting('babysteps.tenant_schema')").fetchone()[0],schema_b)
            with user_context(tenants[0]),connect_database("unused.sqlite") as db:
                self.assertEqual(db.execute("SELECT current_setting('babysteps.tenant_schema')").fetchone()[0],schema_a)
            self.assertEqual(_pool().get_stats().get("requests_waiting",0),0)
        finally:close_postgres_pool()

    def test_runtime_role_has_no_database_or_rls_admin_privileges(self):
        from database_adapter import _pool
        runtime=os.environ.get("BABYSTEPS_RUNTIME_DB_ROLE","bsa_runtime")
        connection=_pool().getconn()
        try:
            attrs=connection.execute("SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s",(runtime,)).fetchone()
            self.assertIsNotNone(attrs)
            self.assertEqual(tuple(attrs),(False,False,False,False))
            self.assertFalse(connection.execute("SELECT has_database_privilege(%s,current_database(),'CREATE')",(runtime,)).fetchone()[0])
            for schema in ("auth","storage","realtime","vault","extensions","graphql","graphql_public","pgbouncer"):
                self.assertFalse(connection.execute("SELECT has_schema_privilege(%s,%s,'USAGE')",(runtime,schema)).fetchone()[0],schema)
            tenant_schemas=[r[0] for r in connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%'").fetchall()]
            for schema in tenant_schemas:
                self.assertFalse(connection.execute("SELECT has_schema_privilege(%s,%s,'USAGE')",(runtime,schema)).fetchone()[0],schema)
            controls=("auth_users","auth_sessions","auth_rate_limits","legacy_account_claims","bsa_tenant_registry","bsa_schema_migrations")
            for table in controls:
                security=connection.execute("SELECT c.relrowsecurity,c.relforcerowsecurity,(SELECT count(*) FROM pg_policies p WHERE p.schemaname='public' AND p.tablename=c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=%s",(table,)).fetchone()
                self.assertIsNotNone(security,table)
                self.assertEqual(tuple(security),(True,True,1),table)
        finally:_pool().putconn(connection)

    def test_versioned_migration_is_idempotent_and_retryable(self):
        from database_adapter import close_postgres_pool,connect_database,migration_database_context
        from migrations import _apply_tenant_schema,migrate_tenant,tenant_migration_status
        from tenant_context import user_context
        tenant="synthetic-migration-"+uuid.uuid4().hex
        migrator=os.environ["BABYSTEPS_MIGRATOR_DATABASE_URL"]
        try:
            with _using_url(migrator):
                schema=migrate_tenant(tenant)
                self.assertEqual(migrate_tenant(tenant),schema)
                self.assertEqual([x["version"] for x in tenant_migration_status(tenant)["applied"]],[1,2])
                retry_tenant="synthetic-retry-"+uuid.uuid4().hex
                calls={"n":0}
                def fail_once():
                    calls["n"]+=1
                    with migration_database_context(),user_context(retry_tenant),connect_database("unused.sqlite") as db:
                        db.execute("CREATE TABLE IF NOT EXISTS retry_probe(id INTEGER PRIMARY KEY)")
                    if calls["n"]==1:raise RuntimeError("synthetic failure")
                with self.assertRaises(RuntimeError):_apply_tenant_schema(retry_tenant,fail_once)
                self.assertEqual(tenant_migration_status(retry_tenant)["applied"],[])
                _apply_tenant_schema(retry_tenant,fail_once)
                self.assertEqual([x["version"] for x in tenant_migration_status(retry_tenant)["applied"]],[1,2])
                self.assertEqual(calls["n"],2)
        finally:close_postgres_pool()


if __name__=="__main__":unittest.main()
