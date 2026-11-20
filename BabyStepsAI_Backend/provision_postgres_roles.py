"""One-time, operator-run Supabase runtime/migrator role hardening.

Reads the existing administrator URL from the ignored local .env, creates two
restricted roles, applies schema-bound forced RLS to existing tenant tables,
and replaces the application URL with the runtime role. It never prints URLs
or passwords. Keep the resulting migrator URL out of the web-service runtime.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT=Path(__file__).resolve().parent.parent
ENV_FILE=ROOT/".env"
RUNTIME_ROLE="bsa_runtime"
MIGRATOR_ROLE="bsa_migrator"
APP_TABLES=("auth_users","auth_sessions","auth_rate_limits","legacy_account_claims",
            "bsa_tenant_registry","bsa_schema_migrations")
MANAGED_SCHEMAS=("auth","storage","realtime","vault","extensions","graphql","graphql_public","pgbouncer")


def _read_env():
    if not ENV_FILE.is_file():raise RuntimeError("Private .env file is missing.")
    values={}
    for line in ENV_FILE.read_text().splitlines():
        value=line.strip()
        if not value or value.startswith("#") or "=" not in value:continue
        key,raw=value.split("=",1);raw=raw.strip()
        if len(raw)>=2 and raw[0]==raw[-1] and raw[0] in "'\"":raw=raw[1:-1]
        values[key.strip()]=raw
    return values


def _url(role,password,options):
    project_ref=options["user"].rsplit(".",1)[-1]
    username=quote(role+"."+project_ref,safe="")
    return "postgresql://"+username+":"+quote(password,safe="")+"@"+options["host"]+":"+options.get("port","5432")+"/"+quote(options.get("dbname","postgres"),safe="")+"?sslmode=require"


def _role(connection,name,password,*,create_roles=False):
    exists=connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(name,)).fetchone()
    attrs=sql.SQL("LOGIN NOSUPERUSER NOCREATEDB ")+sql.SQL("CREATEROLE" if create_roles else "NOCREATEROLE")+sql.SQL(" NOINHERIT NOBYPASSRLS")
    statement=(sql.SQL("ALTER ROLE {} WITH ").format(sql.Identifier(name))+attrs+sql.SQL(" PASSWORD ")+sql.Literal(password)) if exists else (
        sql.SQL("CREATE ROLE {} WITH ").format(sql.Identifier(name))+attrs+sql.SQL(" PASSWORD ")+sql.Literal(password))
    connection.execute(statement)


def apply_database_security(connection):
    runtime=os.environ.get("BABYSTEPS_RUNTIME_DB_ROLE",RUNTIME_ROLE)
    migrator=MIGRATOR_ROLE
    dbname=connection.execute("SELECT current_database()").fetchone()[0]
    admin=connection.execute("SELECT current_user").fetchone()[0]
    connection.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(sql.Identifier(dbname),sql.Identifier(migrator)))
    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname),sql.Identifier(runtime)))
    connection.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(migrator)))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime)))
    connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(runtime)))
    connection.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(sql.Identifier(dbname),sql.Identifier(runtime)))

    # Grant temporary membership to the administrator to transfer ownership
    # safely, then remove it before committing.
    member=connection.execute("SELECT pg_has_role(%s,%s,'SET')",(admin,migrator)).fetchone()[0]
    if not member:
        connection.execute(sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE").format(sql.Identifier(migrator),sql.Identifier(admin)))

    rows=connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()
    names={row[0] for row in rows}
    required=set(APP_TABLES)
    if not required.issubset(names):raise RuntimeError("Public application control tables are incomplete; run the control migration first.")
    for table in APP_TABLES:
        if table in {"bsa_tenant_registry","bsa_schema_migrations"}:
            connection.execute(sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table),sql.Identifier(runtime)))
        else:
            connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON public.{} TO {}").format(sql.Identifier(table),sql.Identifier(runtime)))
        connection.execute(sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(sql.Identifier(table),sql.Identifier(migrator)))

    schema_rows=connection.execute("SELECT schema_name FROM public.bsa_tenant_registry ORDER BY schema_name").fetchall()
    for (schema,) in schema_rows:
        if not schema.startswith("tenant_") or not schema[7:].isalnum():raise RuntimeError("Unsafe schema name in tenant registry.")
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(runtime)))
        tables=connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",(schema,)).fetchall()
        for (table,) in tables:
            target=sql.Identifier(schema,table)
            connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(target,sql.Identifier(runtime)))
        sequences=connection.execute("SELECT sequencename FROM pg_sequences WHERE schemaname=%s",(schema,)).fetchall()
        for (sequence,) in sequences:
            connection.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON SEQUENCE {}.{} TO {}").format(sql.Identifier(schema),sql.Identifier(sequence),sql.Identifier(runtime)))
        views=connection.execute("SELECT viewname FROM pg_views WHERE schemaname=%s ORDER BY viewname",(schema,)).fetchall()
        for (view,) in views:
            target=sql.Identifier(schema,view)
            connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(target,sql.Identifier(runtime)))
            connection.execute(sql.SQL("ALTER VIEW {} OWNER TO {}").format(target,sql.Identifier(migrator)))
        connection.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(schema),sql.Identifier(migrator)))
        for (table,) in tables:
            connection.execute(sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(sql.Identifier(schema),sql.Identifier(table),sql.Identifier(migrator)))

    # DDL on tenant tables must run as their new owner. The runtime role is
    # never a member of the migration role.
    connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(migrator)))
    for (schema,) in schema_rows:
        tables=connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",(schema,)).fetchall()
        for (table,) in tables:
            target=sql.Identifier(schema,table)
            connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(target))
            connection.execute(sql.SQL("DROP POLICY IF EXISTS bsa_tenant_schema_isolation ON {}").format(target))
            predicate=sql.SQL("current_setting('babysteps.tenant_schema', true) = {}").format(sql.Literal(schema))
            connection.execute(sql.SQL("CREATE POLICY bsa_tenant_schema_isolation ON {} USING ({}) WITH CHECK ({})").format(target,predicate,predicate))
    connection.execute("RESET ROLE")
    if not member:connection.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(migrator),sql.Identifier(admin)))
    return len(schema_rows),sum(len(connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s",(schema,)).fetchall()) for (schema,) in schema_rows)


def _replace_env(values):
    known={"BABYSTEPS_DATABASE_URL","BABYSTEPS_MIGRATOR_DATABASE_URL","BABYSTEPS_RUNTIME_DB_ROLE"}
    output=[];written=set()
    for line in ENV_FILE.read_text().splitlines():
        stripped=line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key=stripped.split("=",1)[0].strip()
            if key in known:
                if key not in written:output.append(key+"="+values[key]);written.add(key)
                continue
        output.append(line)
    for key in known-written:output.append(key+"="+values[key])
    fd,tmp=tempfile.mkstemp(prefix=".env.bsa-",dir=ENV_FILE.parent)
    try:
        with os.fdopen(fd,"w") as handle:handle.write("\n".join(output)+"\n");handle.flush();os.fsync(handle.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,ENV_FILE);os.chmod(ENV_FILE,0o600)
    finally:
        try:os.unlink(tmp)
        except FileNotFoundError:pass


def provision():
    values=_read_env();admin_url=values.get("BABYSTEPS_DATABASE_URL") or values.get("BABYSTEPS_ADMIN_DATABASE_URL")
    if not admin_url:raise RuntimeError("A temporary administrator BABYSTEPS_DATABASE_URL is required in the private .env.")
    options=conninfo_to_dict(admin_url)
    if options.get("sslmode") not in {"require","verify-ca","verify-full"} or not options.get("host","").endswith(".pooler.supabase.com") or options.get("port")!="5432" or not options.get("user","").startswith("postgres."):
        raise RuntimeError("Refusing role provisioning: expected the verified Supabase Session Pooler endpoint.")
    runtime_password=secrets.token_urlsafe(36);migrator_password=secrets.token_urlsafe(36)
    runtime_url=_url(RUNTIME_ROLE,runtime_password,options);migrator_url=_url(MIGRATOR_ROLE,migrator_password,options)
    # Keep a private recovery copy of the previous configuration before replacing it.
    backup_dir=Path(tempfile.mkdtemp(prefix="bsa_env_before_role_change_",dir="/private/tmp"));os.chmod(backup_dir,0o700)
    backup=backup_dir/"env.backup";shutil.copy2(ENV_FILE,backup);os.chmod(backup,0o600)
    connection=psycopg.connect(admin_url,connect_timeout=15)
    try:
        with connection.transaction():
            _role(connection,MIGRATOR_ROLE,migrator_password,create_roles=True)
            _role(connection,RUNTIME_ROLE,runtime_password)
            schemas,tables=apply_database_security(connection)
        new_values=dict(values)
        new_values.update({"BABYSTEPS_DATABASE_URL":runtime_url,"BABYSTEPS_MIGRATOR_DATABASE_URL":migrator_url,"BABYSTEPS_RUNTIME_DB_ROLE":RUNTIME_ROLE})
        _replace_env(new_values)
        return {"runtime_role":RUNTIME_ROLE,"migrator_role":MIGRATOR_ROLE,"tenant_schemas_secured":schemas,"tenant_tables_secured":tables,"env_backup":str(backup)}
    finally:connection.close()


if __name__=="__main__":
    print(provision())
