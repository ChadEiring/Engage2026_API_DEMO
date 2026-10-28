"""Tiny .env loader shared by the demo scripts (no third-party dependency).

Reads KEY=VALUE lines from a file named ".env" next to the scripts and puts them in
os.environ, so you don't have to export SMARTSHEET_ACCESS_TOKEN / SHEET_ID in every new
terminal. Variables already set in your shell win over the file, which lets you override
a value for a single run:  SHEET_ID=123 python erp_sync.py ...

Keep ".env" private: it holds your API token. It is listed in .gitignore, and
".env.example" shows the format to share instead.
"""
import os
from pathlib import Path


def load(path: Path | None = None) -> None:
    path = path or Path(__file__).parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue                                   # skip blanks, comments, junk
        key, _, value = line.partition("=")
        # strip optional "export " prefix and surrounding quotes
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)              # real environment variables win
