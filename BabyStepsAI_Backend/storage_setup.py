"""Read-only verification of the configured existing private bucket."""
from __future__ import annotations

import os
from pathlib import Path

from private_storage import verify_private_bucket


def _load_local_env():
    env=Path(__file__).resolve().parent.parent/".env"
    if not env.is_file():return
    for line in env.read_text().splitlines():
        value=line.strip()
        if not value or value.startswith("#") or "=" not in value:continue
        key,raw=value.split("=",1);raw=raw.strip()
        if len(raw)>=2 and raw[0]==raw[-1] and raw[0] in "'\"":raw=raw[1:-1]
        os.environ.setdefault(key.strip(),raw)


if __name__=="__main__":
    _load_local_env()
    print(verify_private_bucket())
