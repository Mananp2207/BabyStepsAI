"""Authenticated encryption for sensitive local application data.

The Fernet key is supplied by BABYSTEPSAI_ENCRYPTION_KEY or kept in a private
per-user config directory. There is deliberately no plaintext fallback.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class EncryptionUnavailable(RuntimeError):
    pass


def _fernet() -> Fernet:
    configured = os.environ.get("BABYSTEPSAI_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise EncryptionUnavailable("BABYSTEPSAI_ENCRYPTION_KEY is invalid.") from error

    key_path = Path(os.environ.get(
        "BABYSTEPSAI_KEY_PATH",
        "~/.config/babystepsai/encryption.key",
    )).expanduser()
    try:
        key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(Fernet.generate_key())
        if os.name != "nt":
            if stat.S_IMODE(key_path.parent.stat().st_mode) & 0o077:
                os.chmod(key_path.parent, 0o700)
            if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
                os.chmod(key_path, 0o600)
        key = key_path.read_bytes().strip()
        return Fernet(key)
    except (OSError, ValueError) as error:
        raise EncryptionUnavailable(
            "Sensitive data is locked because the private encryption key is unavailable. "
            "Configure BABYSTEPSAI_ENCRYPTION_KEY or BABYSTEPSAI_KEY_PATH."
        ) from error


def encrypt_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Sensitive values must be text before encryption.")
    try:
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    except EncryptionUnavailable:
        raise
    except Exception as error:
        raise EncryptionUnavailable("Sensitive data could not be encrypted.") from error


def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except EncryptionUnavailable:
        raise
    except (InvalidToken, UnicodeEncodeError, ValueError) as error:
        raise EncryptionUnavailable(
            "Sensitive data could not be unlocked. Check the configured encryption key."
        ) from error
