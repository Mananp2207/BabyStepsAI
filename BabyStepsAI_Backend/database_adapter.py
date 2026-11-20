"""SQLite default and pooled PostgreSQL connections for the application data."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from pathlib import Path

from tenant_context import AUTH_MODE, LEGACY_USER_ID, current_user_id


_POOL = None
_TENANT_POOLS: dict[str, object] = {}
_POOL_LOCK = threading.RLock()
_CONTROL_LOCK = threading.RLock()
_SCHEMA_CACHE: dict[str,str] = {}
_MIGRATION_MODE = threading.local()
_BACKEND = Path(__file__).resolve().parent
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def postgres_url() -> str:
    return (os.environ.get("BABYSTEPS_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()


def postgres_enabled() -> bool:
    return bool(postgres_url())


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                from psycopg_pool import ConnectionPool
                from psycopg.conninfo import conninfo_to_dict

                connection_options = conninfo_to_dict(postgres_url())
                host = connection_options.get("host", "").casefold()
                if host and host not in {"localhost", "127.0.0.1", "::1"}:
                    if connection_options.get("sslmode", "prefer") not in {"require", "verify-ca", "verify-full"}:
                        raise RuntimeError("Remote PostgreSQL connections must require TLS.")
                _POOL = ConnectionPool(
                    conninfo=postgres_url(), min_size=1,
                    max_size=max(4, int(os.environ.get("BABYSTEPS_DB_POOL_SIZE", "8"))),
                    timeout=float(os.environ.get("BABYSTEPS_DB_POOL_TIMEOUT", "10")),
                    open=False,
                )
                _POOL.open(wait=True)
    return _POOL


def _tenant_pool(user_id: str):
    """Return a pool authenticated as the tenant's restricted DB role."""
    import hmac
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg_pool import ConnectionPool

    schema = tenant_schema(user_id)
    with _POOL_LOCK:
        pool = _TENANT_POOLS.get(schema)
        if pool is not None:
            return pool
        secret = os.environ.get("BABYSTEPS_TENANT_DB_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("Per-tenant PostgreSQL credentials are not configured.")
        options = conninfo_to_dict(postgres_url())
        role = "bsa_t_" + schema.removeprefix("tenant_")
        password = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
        if options.get("host", "").casefold().endswith(".pooler.supabase.com"):
            external_user = options.get("user", "")
            if "." not in external_user:
                raise RuntimeError("Supabase pooler project username is not configured.")
            project_ref = external_user.rsplit(".", 1)[1]
            role += "." + project_ref
        options.update({"user": role, "password": password})
        pool = ConnectionPool(conninfo=make_conninfo(**options), min_size=0, max_size=1,
                              timeout=float(os.environ.get("BABYSTEPS_DB_POOL_TIMEOUT", "10")),
                              max_idle=60, open=False)
        pool.open(wait=True)
        _TENANT_POOLS[schema] = pool
        return pool


def migration_database_context():
    """Use the operator migration connection while applying schema DDL."""
    from contextlib import contextmanager

    @contextmanager
    def _context():
        previous = getattr(_MIGRATION_MODE, "active", False)
        _MIGRATION_MODE.active = True
        try:
            yield
        finally:
            _MIGRATION_MODE.active = previous
    return _context()


def tenant_schema(user_id: str | None = None) -> str:
    identity = user_id or current_user_id()
    return "tenant_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]


def _control_schema():
    pool = _pool()
    connection = pool.getconn()
    try:
        connection.execute("SET search_path TO public")
        connection.execute("""CREATE TABLE IF NOT EXISTS bsa_tenant_registry(
            user_id TEXT PRIMARY KEY,schema_name TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        connection.execute("""CREATE TABLE IF NOT EXISTS bsa_schema_migrations(
            schema_name TEXT NOT NULL,version INTEGER NOT NULL,description TEXT NOT NULL,
            checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(schema_name,version))""")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        pool.putconn(connection)


def registered_tenant_schema(user_id: str | None = None) -> str:
    """Resolve an already provisioned tenant without creating database objects."""
    identity = user_id or current_user_id()
    if not identity or len(identity) > 200 or (AUTH_MODE == "oidc" and identity == LEGACY_USER_ID):
        raise RuntimeError("An authenticated account context is required for PostgreSQL access.")
    expected = tenant_schema(identity)
    with _pool().connection() as connection:
        connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(identity,))
        row = connection.execute(
            "SELECT schema_name FROM public.bsa_tenant_registry WHERE user_id=%s",
            (identity,),
        ).fetchone()
    if not row or row[0] != expected:
        raise RuntimeError("The account's PostgreSQL schema has not been provisioned.")
    return expected


def ensure_tenant_schema(user_id: str | None = None) -> str:
    identity = user_id or current_user_id()
    if not identity or len(identity) > 200 or (AUTH_MODE == "oidc" and identity == LEGACY_USER_ID):
        raise RuntimeError("An authenticated account context is required for PostgreSQL access.")
    cached=_SCHEMA_CACHE.get(identity)
    if cached:return cached
    schema = tenant_schema(identity)
    _control_schema()
    pool = _pool()
    with _CONTROL_LOCK:
        connection = pool.getconn()
        try:
            connection.execute("SET search_path TO public")
            connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(identity,))
            connection.execute("CREATE SCHEMA IF NOT EXISTS \"" + schema + "\"")
            connection.execute("INSERT INTO bsa_tenant_registry(user_id,schema_name) VALUES(%s,%s) ON CONFLICT(user_id) DO NOTHING",
                               (identity, schema))
            registered = connection.execute("SELECT schema_name FROM bsa_tenant_registry WHERE user_id=%s", (identity,)).fetchone()
            if not registered or registered[0] != schema:
                raise RuntimeError("PostgreSQL tenant schema ownership check failed.")
            connection.execute("SET search_path TO \"" + schema + "\", public")
            connection.execute(
                f'''CREATE OR REPLACE VIEW "{schema}".sqlite_master AS
                SELECT 'table'::text AS type,table_name::text AS name,NULL::text AS sql
                FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'
                UNION ALL
                SELECT 'index'::text AS type,indexname::text AS name,indexdef::text AS sql
                FROM pg_indexes WHERE schemaname=current_schema()'''
            )
            connection.commit()
            _SCHEMA_CACHE[identity]=schema
        except Exception:
            connection.rollback()
            raise
        finally:
            pool.putconn(connection)
    return schema


def _adapt_value(value):
    if type(value).__name__ == "_UserSqlValue":
        return str(value)
    return value


def _qmarks_to_psycopg(sql: str) -> str:
    out=[]; index=0; state="normal"; dollar_tag=None
    while index < len(sql):
        char=sql[index]
        nxt=sql[index+1] if index+1<len(sql) else ""
        if state=="normal":
            if char=="'": state="single"
            elif char=='"': state="double"
            elif char=="-" and nxt=="-": state="line";out.extend((char,nxt));index+=2;continue
            elif char=="/" and nxt=="*": state="block";out.extend((char,nxt));index+=2;continue
            elif char=="$":
                match=re.match(r"\$[A-Za-z_0-9]*\$",sql[index:])
                if match: dollar_tag=match.group(0);state="dollar";out.append(dollar_tag);index+=len(dollar_tag);continue
            elif char=="?": out.append("%s");index+=1;continue
        elif state=="single":
            if char=="'" and nxt=="'": out.extend((char,nxt));index+=2;continue
            if char=="'": state="normal"
        elif state=="double":
            if char=='"' and nxt=='"': out.extend((char,nxt));index+=2;continue
            if char=='"': state="normal"
        elif state=="line":
            if char=="\n": state="normal"
        elif state=="block":
            if char=="*" and nxt=="/": out.extend((char,nxt));index+=2;state="normal";continue
        elif state=="dollar" and sql.startswith(dollar_tag,index):
            out.append(dollar_tag);index+=len(dollar_tag);state="normal";continue
        out.append(char);index+=1
    return "".join(out)


def _rewrite_sql(sql: str) -> str:
    text=sql.strip()
    pragma=re.fullmatch(r"PRAGMA\s+table_info\s*\(\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\)\s*;?",text,re.I)
    if pragma:
        name=pragma.group(1).replace("'","''")
        return ("SELECT (c.ordinal_position-1)::integer AS cid,c.column_name::text AS name,"
                "c.data_type::text AS type,CASE WHEN c.is_nullable='NO' THEN 1 ELSE 0 END::integer AS notnull,"
                "c.column_default::text AS dflt_value,CASE WHEN EXISTS (SELECT 1 FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage k USING(constraint_catalog,constraint_schema,constraint_name) "
                "WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema=c.table_schema AND tc.table_name=c.table_name "
                "AND k.column_name=c.column_name) THEN 1 ELSE 0 END::integer AS pk "
                "FROM information_schema.columns c WHERE c.table_schema=current_schema() AND c.table_name='"+name+"' ORDER BY c.ordinal_position")
    if re.fullmatch(r"PRAGMA\s+foreign_keys\s*=\s*ON\s*;?",text,re.I):
        return "SELECT 1 WHERE false"
    if re.fullmatch(r"PRAGMA\s+integrity_check\s*;?",text,re.I):
        return "SELECT 'ok'::text AS integrity_check"
    text=re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",text,flags=re.I)
    ignored=bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b",text,re.I))
    if ignored:
        text=re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO\b","INSERT INTO",text,count=1,flags=re.I)
        text=text.rstrip().removesuffix(";")+" ON CONFLICT DO NOTHING"
    text=re.sub(r"\s+COLLATE\s+NOCASE\b","",text,flags=re.I)
    if re.match(r"BEGIN\s+IMMEDIATE\b",text,re.I):
        return "SELECT pg_advisory_xact_lock(hashtext(current_schema()))"
    return _qmarks_to_psycopg(text)


class HybridRow(dict):
    """Mapping row that also supports SQLite-style numeric indexing."""
    def __init__(self, values):
        super().__init__(values)
        self._ordered=tuple(values.values())

    def __getitem__(self,key):
        return self._ordered[key] if isinstance(key,int) else super().__getitem__(key)


class PostgresCursor:
    def __init__(self,cursor): self._cursor=cursor
    @property
    def rowcount(self): return self._cursor.rowcount
    @property
    def lastrowid(self): return None
    def fetchone(self):
        row=self._cursor.fetchone()
        return HybridRow(row) if row is not None and not isinstance(row,HybridRow) else row
    def fetchall(self):
        return [HybridRow(row) if not isinstance(row,HybridRow) else row for row in self._cursor.fetchall()]
    def __iter__(self):
        for row in self._cursor:
            yield HybridRow(row) if not isinstance(row,HybridRow) else row


def _raise_compatible(error):
    try:
        import psycopg
        if isinstance(error,psycopg.IntegrityError): raise sqlite3.IntegrityError(str(error)) from error
        if isinstance(error,psycopg.OperationalError): raise sqlite3.OperationalError(str(error)) from error
        if isinstance(error,psycopg.Error): raise sqlite3.DatabaseError(str(error)) from error
    except ImportError:
        pass
    raise error


def _split_script(script: str):
    # The current schema script uses plain DDL without semicolons in literals.
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class PostgresConnection:
    def __init__(self,user_id: str | None,global_schema: bool=False):
        self.schema="public" if global_schema else registered_tenant_schema(user_id)
        self._pool=_pool() if global_schema or getattr(_MIGRATION_MODE,"active",False) else _tenant_pool(user_id or current_user_id())
        self._connection=self._pool.getconn()
        self._closed=False
        try:
            if global_schema:
                self._connection.execute("SET search_path TO public")
                self._connection.execute("SELECT set_config('babysteps.tenant_schema','',false)")
                self._connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(current_user_id(),))
            else:
                self._connection.execute("SET search_path TO \""+self.schema+"\", public")
                self._connection.execute("SELECT set_config('babysteps.tenant_schema',%s,false)",(self.schema,))
                self._connection.execute("SELECT set_config('babysteps.user_id',%s,false)",(user_id or current_user_id(),))
        except Exception:
            self._connection.rollback();self._pool.putconn(self._connection);self._closed=True
            raise

    @staticmethod
    def _params(parameters):
        if parameters is None:return None
        if isinstance(parameters,dict):return {key:_adapt_value(value) for key,value in parameters.items()}
        return tuple(_adapt_value(value) for value in parameters)

    def execute(self,sql,parameters=None):
        if self._closed:raise sqlite3.ProgrammingError("Database connection is closed.")
        statement=_rewrite_sql(sql)
        try:
            cursor=self._connection.cursor(row_factory=__import__("psycopg.rows",fromlist=["dict_row"]).dict_row)
            cursor.execute(statement,self._params(parameters))
            return PostgresCursor(cursor)
        except Exception as error:
            _raise_compatible(error)

    def executemany(self,sql,seq_of_parameters):
        statement=_rewrite_sql(sql)
        try:
            cursor=self._connection.cursor()
            cursor.executemany(statement,[self._params(params) for params in seq_of_parameters])
            return PostgresCursor(cursor)
        except Exception as error:
            _raise_compatible(error)

    def executescript(self,script):
        result=None
        for statement in _split_script(script):result=self.execute(statement)
        return result

    def commit(self):
        try:self._connection.commit()
        except Exception as error:_raise_compatible(error)
    def rollback(self):self._connection.rollback()
    def close(self):
        if self._closed:return
        try:self._connection.rollback()
        finally:self._pool.putconn(self._connection);self._closed=True
    def __enter__(self):return self
    def __exit__(self,exc_type,exc_value,tb):
        try:
            if exc_type is None:self.commit()
            else:self.rollback()
        finally:
            if not self._closed:
                self._pool.putconn(self._connection);self._closed=True
        return False


def connect_database(path: str | os.PathLike, *, timeout=30, factory=None, global_schema=False):
    url=postgres_url()
    if url:
        return PostgresConnection(None if global_schema else current_user_id(),global_schema=global_schema)
    kwargs={"timeout":timeout}
    if factory is not None:kwargs["factory"]=factory
    db=sqlite3.connect(path,**kwargs)
    if Path(path).resolve().parent==_BACKEND and os.name!="nt":os.chmod(path,0o600)
    return db


def close_postgres_pool():
    global _POOL
    with _POOL_LOCK:
        for pool in _TENANT_POOLS.values():
            pool.close()
        _TENANT_POOLS.clear()
        if _POOL is not None:
            _POOL.close();_POOL=None
