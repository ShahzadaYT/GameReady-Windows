"""library.py — game library data model and persistence.

Defines :class:`GameEntry` (the record for one game) and all persistence
helpers: versioned JSON load/save (atomic), CSV/TXT import & export, dedup and
migration from the original v1 library format.

Every entry carries a formal ``launcher`` enum field
(steam/epic/ea/ubisoft/xbox/gog/battlenet/other). The GUI filters tabs on this
field with live counts — never by hiding rows.

Fields added in v3 (all optional, older libraries load unchanged):

``install_dir``
    The game's install directory. Enables install-directory process detection,
    which is how a game whose process name differs from its launch executable
    is verified. Restored capability — see docs/ENGINEERING_ASSESSMENT.md §3.
``app_user_model_id``
    Xbox / Microsoft Store AppUserModelID, so launch (``shell:AppsFolder\\…``)
    and verification (``expected_process``) can be held on one entry.
``package_family_name``
    Xbox / MS Store PackageFamilyName, used to resolve the game executable from
    the package manifest (read-only).
``alt_processes``
    Additional image names that legitimately count as "the game is running".
``verification``
    How this entry can be verified: ``auto`` / ``hybrid`` / ``manual``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .textnorm import as_path, atomic_write, fold, squash

LIBRARY_VERSION = "3"
LIBRARY_FILE = "games.json"
SCAN_FOLDERS_FILE = "scan_folders.json"

LAUNCHERS = ["steam", "epic", "ea", "ubisoft", "xbox", "gog", "battlenet", "other"]
MODES = ["quick", "standard", "manual", "smoke"]

VERIFY_AUTO = "auto"
VERIFY_HYBRID = "hybrid"
VERIFY_MANUAL = "manual"
VERIFICATIONS = [VERIFY_AUTO, VERIFY_HYBRID, VERIFY_MANUAL]

CSV_FIELDS = [
    "id", "name", "launcher", "source", "exe_path", "launch_method",
    "launch_target", "expected_process", "working_dir", "mode",
    "launch_timeout", "validation_time", "notes", "origin",
    "last_result", "last_ready",
]

_DEFAULT_LAUNCH_TIMEOUT = 60
_DEFAULT_VALIDATION_TIME = 15

_LAUNCHER_ALIASES = {
    "steam": "steam",
    "epic": "epic", "epic games": "epic", "epicgames": "epic", "egs": "epic",
    "ea": "ea", "origin": "ea", "ea app": "ea", "ea desktop": "ea",
    "electronic arts": "ea", "eaapp": "ea",
    "ubisoft": "ubisoft", "uplay": "ubisoft", "ubisoft connect": "ubisoft",
    "xbox": "xbox", "ms store": "xbox", "msstore": "xbox",
    "microsoft store": "xbox", "microsoft": "xbox", "gamepass": "xbox",
    "game pass": "xbox", "store": "xbox", "windows store": "xbox",
    "gog": "gog", "gog galaxy": "gog",
    "battlenet": "battlenet", "battle.net": "battlenet", "blizzard": "battlenet",
}

# Trailing edition/trademark noise stripped when comparing two entries for the
# same game. Deliberately conservative: it must never merge two distinct games.
_IDENTITY_NOISE = re.compile(
    r"(?:\b(?:definitive|deluxe|ultimate|complete|remastered|remaster|goty|"
    r"game of the year|standard|limited|gold|premium|enhanced|anniversary|"
    r"director'?s\s+cut|trial|demo|beta|preview|edition|pc)\b|[™®])",
    re.IGNORECASE,
)


def normalize_launcher(value: Optional[str]) -> str:
    """Normalise an arbitrary source/launcher string to a valid enum value."""
    if not value:
        return "other"
    v = str(value).strip().lower()
    if v in LAUNCHERS:
        return v
    return _LAUNCHER_ALIASES.get(v, "other")


def identity_key(name: str) -> str:
    """A conservative comparison key for 'is this the same game?'.

    Strips trademark symbols, edition words and all non-alphanumerics. Used to
    join the two halves of an Xbox or EA game that discovery finds separately
    (one launchable, one verifiable) — see docs/ENGINEERING_ASSESSMENT.md.
    """
    return squash(_IDENTITY_NOISE.sub(" ", str(name or "")))


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class GameEntry:
    """One game in the library."""

    name: str = ""
    launcher: str = "other"
    source: str = ""
    exe_path: str = ""
    launch_method: str = ""
    launch_target: str = ""
    expected_process: str = ""
    working_dir: str = ""
    mode: str = "standard"
    launch_timeout: float = _DEFAULT_LAUNCH_TIMEOUT
    validation_time: float = _DEFAULT_VALIDATION_TIME
    notes: str = ""
    origin: str = "auto"
    last_result: str = ""
    last_ready: str = ""
    id: str = field(default_factory=_new_id)
    # --- v3 additions -----------------------------------------------------
    install_dir: str = ""
    app_user_model_id: str = ""
    package_family_name: str = ""
    alt_processes: List[str] = field(default_factory=list)
    verification: str = VERIFY_AUTO

    def __post_init__(self) -> None:
        self.launcher = normalize_launcher(self.launcher or self.source)
        if not self.source:
            self.source = self.launcher
        if self.mode not in MODES:
            self.mode = "standard"
        if self.verification not in VERIFICATIONS:
            self.verification = VERIFY_AUTO
        if not self.id:
            self.id = _new_id()
        try:
            self.launch_timeout = float(self.launch_timeout or _DEFAULT_LAUNCH_TIMEOUT)
        except (TypeError, ValueError):
            self.launch_timeout = float(_DEFAULT_LAUNCH_TIMEOUT)
        try:
            self.validation_time = float(self.validation_time or _DEFAULT_VALIDATION_TIME)
        except (TypeError, ValueError):
            self.validation_time = float(_DEFAULT_VALIDATION_TIME)
        if isinstance(self.alt_processes, str):
            self.alt_processes = [p for p in re.split(r"[;,]", self.alt_processes) if p.strip()]
        self.alt_processes = [str(p).strip() for p in (self.alt_processes or []) if str(p).strip()]

    # -- derived -----------------------------------------------------------

    @property
    def display_target(self) -> str:
        return self.launch_target or self.exe_path

    @property
    def identity(self) -> str:
        """Conservative identity key for merging duplicates."""
        return identity_key(self.name)

    def process_names(self) -> List[str]:
        """All image names that count as 'this game is running'."""
        names: List[str] = []
        for candidate in [self.expected_process] + list(self.alt_processes):
            c = str(candidate or "").strip()
            if not c:
                continue
            leaf = os.path.basename(c.replace("\\", "/"))
            if leaf and leaf.lower() not in {n.lower() for n in names}:
                names.append(leaf)
        if not names and self.exe_path and not is_pseudo_path(self.exe_path):
            leaf = os.path.basename(self.exe_path.replace("\\", "/"))
            if leaf.lower().endswith(".exe"):
                names.append(leaf)
        return names

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GameEntry":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known and v is not None}
        return cls(**clean)


def is_pseudo_path(value: str) -> bool:
    """True for launch targets that are URIs or shell routes, not real files."""
    v = str(value or "").strip().lower()
    return v.startswith("shell:") or "://" in v


def effective_launch_method(entry: GameEntry) -> str:
    """Infer a launch method when the entry does not specify one."""
    if entry.launch_method in ("exe", "uri", "shell", "shortcut"):
        return entry.launch_method
    target = (entry.launch_target or entry.exe_path or "").strip().lower()
    if target.startswith("shell:"):
        return "shell"
    if "://" in target:
        return "uri"
    if target.endswith(".lnk"):
        return "shortcut"
    return "exe"


def effective_launch_target(entry: GameEntry) -> str:
    """The string actually handed to the OS to start this game."""
    return entry.launch_target or entry.exe_path


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def save_library(entries: Sequence[GameEntry], path: Path) -> None:
    """Atomically write the library as versioned JSON."""
    payload = {
        "version": LIBRARY_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "games": [e.to_dict() for e in entries],
    }
    atomic_write(Path(path), json.dumps(payload, indent=2))


def load_library(path: Path) -> Tuple[List[GameEntry], str]:
    """Load games.json.

    Returns ``(entries, message)``. On corruption the bad file is backed up to
    ``<name>.corrupt-<timestamp>`` and an empty list is returned — the original
    is never destroyed. Handles migration from v1 (no ``launcher`` field, which
    is derived from ``source``) and v2 (no v3 fields).
    """
    path = Path(path)
    if not path.exists():
        return [], "No library file yet."
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + ".corrupt-" + stamp)
        try:
            shutil.copy2(path, backup)
            return [], f"Library corrupt ({exc}); backed up to {backup.name}."
        except OSError:
            return [], f"Library corrupt ({exc}); could not back up."

    if isinstance(raw, list):           # very old: a bare list of games
        version, games = "1", raw
    elif isinstance(raw, dict):
        version = str(raw.get("version") or "1")
        games = raw.get("games") or raw.get("entries") or []
    else:
        return [], "Library corrupt (unrecognised structure)."

    entries: List[GameEntry] = []
    for item in games:
        if not isinstance(item, dict):
            continue
        entry = GameEntry.from_dict(item)
        if not item.get("launcher"):
            entry.launcher = normalize_launcher(item.get("source"))
        entries.append(entry)

    if version != LIBRARY_VERSION:
        return entries, f"Loaded {len(entries)} games (migrated v{version} -> v{LIBRARY_VERSION})."
    return entries, f"Loaded {len(entries)} games."


def load_scan_folders(path: Path) -> List[str]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        return [str(x) for x in data.get("folders", [])]
    return []


def _rows_from_csv(path: Path) -> List[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def import_games(path: Path) -> Tuple[List[GameEntry], str]:
    """Import games from .json, .csv or a plain .txt list of executables."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        entries, msg = load_library(path)
        return entries, msg
    if suffix == ".csv":
        rows = _rows_from_csv(path)
        return [GameEntry.from_dict(r) for r in rows], f"Imported {len(rows)} rows."
    if suffix in (".txt", ""):
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(GameEntry(
                name=os.path.splitext(os.path.basename(line.replace("\\", "/")))[0],
                exe_path=line, launch_method="exe", origin="manual",
            ))
        return out, f"Imported {len(out)} paths."
    raise ValueError(f"Unsupported import format: {suffix}")


def export_games(entries: Sequence[GameEntry], path: Path) -> str:
    """Export the library to .json or .csv."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for e in entries:
                writer.writerow({k: e.to_dict().get(k, "") for k in CSV_FIELDS})
        return f"Exported {len(entries)} games to CSV."
    save_library(entries, path)
    return f"Exported {len(entries)} games to JSON."


#: Fields a rescan may refresh on an existing entry. Everything else — most
#: importantly the user's test history and any manual edits — is preserved, so
#: re-scanning never costs the user work.
_REFRESHABLE = ("exe_path", "working_dir", "install_dir", "expected_process",
                "launch_method", "launch_target", "app_user_model_id",
                "package_family_name", "verification", "notes")


def merge_library_updates(existing: Sequence[GameEntry],
                          discovered: Sequence[GameEntry]) -> Tuple[List[GameEntry], int, int]:
    """Fold a fresh scan into the stored library.

    Returns ``(entries, added, updated)``. An existing entry keeps its id,
    ``last_result``, ``last_ready``, timeouts and any field the user edited by
    hand; a discovered entry may only *fill in* fields that are empty, or
    correct a launch route it now knows better. Entries the scan did not find
    are kept — a game is not deleted because a launcher was offline.
    """
    by_key: Dict[Tuple[str, str], GameEntry] = {}
    for entry in existing:
        by_key[(entry.launcher, entry.identity or entry.name.lower())] = entry

    added = updated = 0
    for found in discovered:
        key = (found.launcher, found.identity or found.name.lower())
        current = by_key.get(key)
        if current is None:
            by_key[key] = found
            added += 1
            continue
        changed = False
        for fieldname in _REFRESHABLE:
            new_value = getattr(found, fieldname, "")
            if not new_value:
                continue
            old_value = getattr(current, fieldname, "")
            if old_value:
                continue                      # never overwrite what is already known
            setattr(current, fieldname, new_value)
            changed = True
        for alt in found.process_names():
            known = {n.lower() for n in current.process_names()}
            if alt.lower() not in known:
                current.alt_processes.append(alt)
                changed = True
        if current.mode == "manual" and current.process_names():
            # a previously unverifiable entry can now be verified
            current.mode = "standard"
            current.verification = VERIFY_AUTO
            changed = True
        if changed:
            updated += 1
    return list(by_key.values()), added, updated


def infrastructure_entries(entries: Sequence[GameEntry]) -> List[GameEntry]:
    """Entries that are not games — launchers, system apps, utilities.

    Delegates to :mod:`travelready.classification`, which is the single
    classifier and records *why* each entry was excluded. Surfaced for the user
    to review rather than deleted: TravelReady does not throw away library rows
    on its own.
    """
    from .classification import classify_entry

    return [e for e in entries if not classify_entry(e).is_game]
