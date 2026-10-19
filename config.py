from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent

def read_json(name, default=None):
    path = ROOT / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

def write_json(name, data):
    (ROOT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
