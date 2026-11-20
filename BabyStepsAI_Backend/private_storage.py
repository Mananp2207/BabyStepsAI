"""Private local or Supabase Storage backend for applicant files.

Supabase objects are addressed only through the authenticated FastAPI backend.
The bucket is private and object names are prefixed with a one-way tenant hash.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from tenant_context import current_user_id, private_storage_path

MAX_OBJECT_BYTES = 15 * 1024 * 1024
SUPPORTED_MIME_TYPES=("application/pdf","application/vnd.openxmlformats-officedocument.wordprocessingml.document","image/png","image/jpeg")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")


def _config():
    backend=os.environ.get("BABYSTEPS_STORAGE_BACKEND","local").strip().casefold()
    if backend not in {"local","supabase"}:
        raise RuntimeError("BABYSTEPS_STORAGE_BACKEND must be local or supabase.")
    if backend=="local":return None
    base=os.environ.get("SUPABASE_URL","").strip().rstrip("/")
    # Supabase's current server key is an opaque sb_secret_ API key. Keep the
    # legacy JWT setting as a backwards-compatible fallback during migration.
    key=(os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    bucket=os.environ.get("SUPABASE_PRIVATE_BUCKET","babysteps-private").strip()
    parsed=urlsplit(base)
    if parsed.scheme!="https" or not parsed.hostname or not parsed.hostname.endswith(".supabase.co") or parsed.username or parsed.password:
        raise RuntimeError("Supabase Storage requires the HTTPS project URL.")
    if not key or not _SAFE_NAME.fullmatch(bucket):
        raise RuntimeError("Supabase Storage credentials or bucket configuration are missing.")
    if key.startswith("sb_") and not key.startswith("sb_secret_"):
        raise RuntimeError("Supabase Storage requires a server-side secret key, not a publishable key.")
    return {"base":base,"key":key,"bucket":bucket,"legacy_jwt":not key.startswith("sb_secret_")}


def supabase_storage_enabled() -> bool:
    return _config() is not None


def _name(filename: str) -> str:
    name=str(filename or "")
    if not _SAFE_NAME.fullmatch(name) or Path(name).name!=name or name in {".",".."}:
        raise ValueError("Private object name is invalid.")
    return name


def _object_key(user_id: str, filename: str) -> str:
    owner=hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
    return owner+"/"+_name(filename)


def _request(method: str, endpoint: str, *, config=None, **kwargs):
    cfg=config or _config()
    if cfg is None:raise RuntimeError("Supabase Storage is not enabled.")
    headers={"apikey":cfg["key"]}
    if cfg["legacy_jwt"]:
        headers["Authorization"]="Bearer "+cfg["key"]
    headers.update(kwargs.pop("headers",{}))
    try:
        response=requests.request(method,cfg["base"]+"/storage/v1/"+endpoint,headers=headers,timeout=(5,30),verify=True,**kwargs)
    except requests.RequestException:
        raise RuntimeError("Private storage is temporarily unavailable.") from None
    return response


def _cache_path(base, filename: str) -> Path:
    root=private_storage_path(base)
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=(root/_name(filename)).resolve()
    if target.parent!=root.resolve():raise ValueError("Private object path escaped its owner directory.")
    return target


def _write_private(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".bsa-",dir=str(path.parent))
    try:
        with os.fdopen(fd,"wb") as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        if os.name!="nt":os.chmod(tmp,0o600)
        os.replace(tmp,path)
        if os.name!="nt":os.chmod(path,0o600)
    finally:
        try:os.unlink(tmp)
        except FileNotFoundError:pass


def _validate_content(name: str, data: bytes, content_type: str):
    """Allow only supported document formats with matching file signatures."""
    ext=Path(name).suffix.casefold()
    mime=(content_type or "").split(";",1)[0].strip().casefold()
    expected={".pdf":"application/pdf", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg"}
    if expected.get(ext)!=mime:
        raise ValueError("File extension and content type do not match a supported document.")
    if ext==".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a valid PDF document.")
    if ext==".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The uploaded file is not a valid PNG image.")
    if ext in {".jpg",".jpeg"} and not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("The uploaded file is not a valid JPEG image.")
    if ext==".docx":
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names=set(archive.namelist())
                total=sum(item.file_size for item in archive.infolist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names or total>100*1024*1024:
                    raise ValueError("The uploaded file is not a supported DOCX document.")
        except (zipfile.BadZipFile, OSError):
            raise ValueError("The uploaded file is not a valid DOCX document.") from None


def store_private_file(base, filename: str, data: bytes, content_type: str, *, user_id: str | None=None) -> Path:
    identity=user_id or current_user_id();name=_name(filename);blob=bytes(data)
    if not blob or len(blob)>MAX_OBJECT_BYTES:raise ValueError("Private file size is outside the supported range.")
    _validate_content(name,blob,content_type)
    cfg=_config()
    if cfg:
        key=_object_key(identity,name)
        response=_request("POST","object/"+quote(cfg["bucket"],safe="")+"/"+quote(key,safe="/"),config=cfg,
                          data=blob,headers={"Content-Type":content_type or "application/octet-stream","x-upsert":"true"})
        if response.status_code not in {200,201}:raise RuntimeError("Private file upload failed.")
    target=_cache_path(base,name)
    try:_write_private(target,blob)
    except Exception:
        if cfg:
            _request("DELETE","object/"+quote(cfg["bucket"],safe=""),config=cfg,json={"prefixes":[_object_key(identity,name)]})
        raise
    return target


def resolve_private_file(base, filename: str, *, user_id: str | None=None) -> Path:
    identity=user_id or current_user_id();name=_name(filename);target=_cache_path(base,name)
    cfg=_config()
    if cfg and not target.is_file():
        key=_object_key(identity,name)
        response=_request("GET","object/authenticated/"+quote(cfg["bucket"],safe="")+"/"+quote(key,safe="/"),config=cfg)
        if response.status_code!=200:raise FileNotFoundError("Private file is unavailable.")
        if len(response.content)>MAX_OBJECT_BYTES:raise RuntimeError("Private file exceeded the configured size limit.")
        mime={".pdf":"application/pdf", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg"}.get(Path(name).suffix.casefold(),"")
        _validate_content(name,response.content,mime)
        _write_private(target,response.content)
    if not target.is_file():raise FileNotFoundError("Private file is unavailable.")
    return target


def delete_private_file(base, filename: str, *, user_id: str | None=None) -> bool:
    identity=user_id or current_user_id();name=_name(filename);target=_cache_path(base,name);cfg=_config()
    if cfg:
        key=_object_key(identity,name)
        response=_request("DELETE","object/"+quote(cfg["bucket"],safe=""),config=cfg,json={"prefixes":[key]})
        if response.status_code not in {200,204}:raise RuntimeError("Private file deletion failed.")
    existed=target.is_file()
    target.unlink(missing_ok=True)
    return True if cfg else existed


def ensure_private_bucket():
    """Create the configured bucket as private, or fail if it is public."""
    cfg=_config()
    if cfg is None:raise RuntimeError("Set BABYSTEPS_STORAGE_BACKEND=supabase before bucket setup.")
    bucket=quote(cfg["bucket"],safe="")
    response=_request("GET","bucket/"+bucket,config=cfg)
    if response.status_code==404:
        response=_request("POST","bucket",config=cfg,json={"name":cfg["bucket"],"public":False,
                         "file_size_limit":MAX_OBJECT_BYTES,"allowed_mime_types":list(SUPPORTED_MIME_TYPES)})
        if response.status_code not in {200,201}:raise RuntimeError("Private bucket creation failed.")
    elif response.status_code!=200:
        raise RuntimeError("Private bucket verification failed.")
    try:details=response.json()
    except ValueError:details={}
    if details.get("public") is not False:
        raise RuntimeError("Configured bucket is public; refusing applicant file storage.")
    return {"bucket_private":True,"bucket_ready":True}


def verify_private_bucket():
    """Read-only verification of an existing private bucket and its limits."""
    cfg=_config()
    if cfg is None:raise RuntimeError("Set BABYSTEPS_STORAGE_BACKEND=supabase before bucket verification.")
    bucket=quote(cfg["bucket"],safe="")
    response=_request("GET","bucket/"+bucket,config=cfg)
    if response.status_code==404:
        raise RuntimeError("Configured private bucket does not exist; no bucket was created.")
    if response.status_code!=200:
        raise RuntimeError("Private bucket verification failed.")
    try:details=response.json()
    except ValueError:raise RuntimeError("Private bucket metadata could not be verified.") from None
    if details.get("public") is not False:
        raise RuntimeError("Configured bucket is public; refusing applicant file storage.")
    try:file_size_limit=int(details.get("file_size_limit"))
    except (TypeError,ValueError):
        raise RuntimeError("Private bucket has no verifiable file-size limit.") from None
    if not 0<file_size_limit<=MAX_OBJECT_BYTES:
        raise RuntimeError("Private bucket file-size limit exceeds the application limit.")
    allowed=details.get("allowed_mime_types")
    if not isinstance(allowed,list) or set(allowed)!=set(SUPPORTED_MIME_TYPES):
        raise RuntimeError("Private bucket MIME restrictions do not match supported document types.")
    return {"bucket_private":True,"bucket_ready":True,"file_size_limit":file_size_limit,
            "allowed_mime_types":sorted(allowed)}
