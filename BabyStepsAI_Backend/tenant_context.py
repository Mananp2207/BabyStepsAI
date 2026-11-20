"""Request-scoped tenant identity, database, and private-file paths.

The local prototype keeps its historical database path. In OIDC mode each
verified account gets an isolated database and file tree; no account id is
accepted from request parameters.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path


LEGACY_USER_ID = "local-test-user"
AUTH_MODE = os.environ.get("BABYSTEPS_AUTH_MODE", "local").strip().casefold()
if AUTH_MODE not in {"local", "oidc"}:
    raise RuntimeError("BABYSTEPS_AUTH_MODE must be 'local' or 'oidc'.")
_USER_ID = contextvars.ContextVar("babysteps_user_id", default=LEGACY_USER_ID)
_BACKEND_ROOT = Path(__file__).resolve().parent
_DATA_ROOT_CONFIGURED = bool(os.environ.get("BABYSTEPS_DATA_ROOT"))
_DATA_ROOT = Path(os.environ.get("BABYSTEPS_DATA_ROOT", str(_BACKEND_ROOT / "private_users"))).expanduser()


def current_user_id() -> str:
    value = _USER_ID.get()
    if not value:
        raise RuntimeError("An authenticated user context is required.")
    return value


@contextmanager
def user_context(user_id: str):
    if not isinstance(user_id, str) or not user_id or len(user_id) > 200:
        raise ValueError("Invalid authenticated user identity.")
    token = _USER_ID.set(user_id)
    try:
        yield
    finally:
        _USER_ID.reset(token)


class _UserSqlValue:
    """SQLite-adapted user value; resolves in the current request context."""
    __slots__ = ()

    def __conform__(self, protocol):
        if protocol is sqlite3.PrepareProtocol:
            return current_user_id()
        return None

    def __str__(self):
        return current_user_id()

    def __repr__(self):
        return repr(current_user_id())

    def __eq__(self, other):
        return current_user_id() == str(other)

    def __hash__(self):
        return hash(current_user_id())


USER_ID = _UserSqlValue()


def user_data_directory(user_id: str | None = None) -> Path:
    identity = user_id or current_user_id()
    # Hashing prevents identifiers or provider subjects appearing in paths.
    directory = _DATA_ROOT / hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(_DATA_ROOT, 0o700)
        os.chmod(directory, 0o700)
    return directory


def database_path(original: str | os.PathLike) -> Path:
    path = Path(original)
    if AUTH_MODE != "oidc" and (not _DATA_ROOT_CONFIGURED or path.resolve().parent != _BACKEND_ROOT):
        return path
    tenant = user_data_directory()
    result = tenant / path.name
    if os.name != "nt":
        if result.is_symlink():
            raise RuntimeError("Tenant database path cannot be a symbolic link.")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(result, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    return result


def private_storage_path(base: str | os.PathLike) -> Path:
    path = Path(base)
    if AUTH_MODE != "oidc" and (not _DATA_ROOT_CONFIGURED or path.resolve().parent != _BACKEND_ROOT):
        return path
    result = user_data_directory() / path.name
    result.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(result, 0o700)
    return result


def submit_with_context(executor, function, *args, **kwargs):
    """Submit background work with the caller's authenticated tenant context."""
    context = contextvars.copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


def new_context_thread(thread_type, *, target, args=(), kwargs=None, **options):
    context = contextvars.copy_context()
    return thread_type(target=context.run, args=(target, *args), kwargs=kwargs or {}, **options)


class TenantThreadPoolExecutor(_ThreadPoolExecutor):
    """Thread pool that carries the verified request tenant into each job."""
    def submit(self, fn, /, *args, **kwargs):
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)
