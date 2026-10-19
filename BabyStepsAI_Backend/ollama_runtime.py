"""Process-wide serialization for local Ollama requests."""

from threading import Lock

OLLAMA_LOCK = Lock()
