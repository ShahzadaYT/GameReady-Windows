"""history.py — persistent record of every test run."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

HISTORY_FILE = "history.json"
HISTORY_VERSION = "2"
MAX_RECORDS = 2000


def _atomic_write(path: Path, text: str) -> None:
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _to_dict(record) -> dict:
    if isinstance(record, dict):
        return dict(record)
    if is_dataclass(record):
        return asdict(record)
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return {"value": str(record)}


def load_history(path: Path) -> List[dict]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [r for r in raw.get("records", []) if isinstance(r, dict)]
    return []


def save_history(records: Sequence[dict], path: Path) -> None:
    payload = {
        "version": HISTORY_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "records": [_to_dict(r) for r in records][-MAX_RECORDS:],
    }
    _atomic_write(Path(path), json.dumps(payload, indent=2))


def append_results(results: Iterable, path: Path) -> List[dict]:
    """Append test results to the history file and return the new history."""
    records = load_history(path)
    for result in results:
        records.append(_to_dict(result))
    save_history(records, path)
    return records


def recent(path: Path, limit: int = 50) -> List[dict]:
    return load_history(path)[-limit:][::-1]


def for_game(path: Path, name: str, limit: int = 20) -> List[dict]:
    target = str(name or "").strip().lower()
    rows = [r for r in load_history(path) if str(r.get("name", "")).strip().lower() == target]
    return rows[-limit:][::-1]
