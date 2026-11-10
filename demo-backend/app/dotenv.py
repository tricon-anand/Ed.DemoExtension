"""Reads KEY=VALUE lines from a .env file in the backend folder into the environment.
Variables that are already set in the terminal are never overwritten. Empty values and
placeholders such as <your key> are ignored."""
import os
from pathlib import Path


def parse(text: str) -> dict:
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
            value = value[1:value.index(value[0], 1)]           # quoted: keep everything inside the quotes
        else:
            value = value.split(" #", 1)[0].strip()             # unquoted: drop a trailing " # comment"
        if key and value and not value.startswith("<"):
            out[key] = value
    return out


SHADOWED: list[str] = []      # names that are in .env but a different value was already set in the terminal


def load(path: Path | None = None) -> list[str]:
    """Returns the names of the variables it set."""
    if os.getenv("APP_SKIP_DOTENV") == "1":
        return []
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return []
    set_now = []
    for k, v in parse(path.read_text(encoding="utf-8-sig")).items():
        if k not in os.environ:
            os.environ[k] = v
            set_now.append(k)
        elif os.environ[k] != v and k not in SHADOWED:
            SHADOWED.append(k)
    return set_now
