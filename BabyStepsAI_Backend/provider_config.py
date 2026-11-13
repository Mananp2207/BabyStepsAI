"""Server-only configuration for optional hosted AI providers.

Only the Groq settings are read from the ignored project-root .env file. Values
are never logged, returned to clients, or copied into application databases.
"""
import ast
import os
from pathlib import Path


PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"
GROQ_FREE_MODELS = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
)
_LOCAL_KEYS = frozenset({"GROQ_API_KEY", "GROQ_MODEL"})


def _local_env_value(name):
    if name not in _LOCAL_KEYS:
        return ""
    try:
        # A local credentials file must be private to its owner. Fail closed
        # instead of silently using a broadly readable API key.
        if PROJECT_ENV.stat().st_mode & 0o077:
            return ""
        for raw_line in PROJECT_ENV.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, raw_value = line.partition("=")
            if not separator or key.strip() != name:
                continue
            value = raw_value.strip()
            if value[:1] in {"'", '"'}:
                try:
                    decoded = ast.literal_eval(value)
                except (SyntaxError, ValueError):
                    return ""
                return decoded.strip() if isinstance(decoded, str) else ""
            return value.split("#", 1)[0].strip()
    except (OSError, UnicodeError):
        return ""
    return ""


def groq_api_key():
    if "GROQ_API_KEY" in os.environ:
        return os.environ.get("GROQ_API_KEY", "").strip()
    return _local_env_value("GROQ_API_KEY")


def configured_groq_model():
    selected = (os.environ.get("GROQ_MODEL", "").strip() if "GROQ_MODEL" in os.environ
                else _local_env_value("GROQ_MODEL"))
    return selected if selected in GROQ_FREE_MODELS else GROQ_FREE_MODELS[0]


def groq_model_allowed(model):
    return isinstance(model, str) and model in GROQ_FREE_MODELS
