"""apppaths.py — where TravelReady keeps its own data.

TravelReady writes **only** inside its own data directory. No module in this
package writes to a game folder, a launcher folder, the registry, or anywhere
else on the system, with the single exception of the settings transaction
engine (:mod:`travelready.optimiser.transaction`), which writes only to a game
configuration file the user has explicitly approved.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "TravelReady"
_LEGACY_DIR_NAMES = ("GameLaunchTester", "GameReady")


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """The per-user directory holding the library, history and backups.

    Honours ``TRAVELREADY_DATA_DIR`` so tests never touch real user data.
    """
    override = os.environ.get("TRAVELREADY_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIR_NAME


def data_subdir(*parts: str) -> Path:
    """A subdirectory of :func:`data_dir`, created on demand."""
    p = data_dir().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_file(name: str) -> Path:
    """Absolute path to a file in the data directory (directory created)."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def legacy_data_files(name: str) -> list[Path]:
    """Candidate paths for ``name`` under older application directory names.

    Used to migrate a library written by an earlier build without ever
    deleting the original.
    """
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return [base / legacy / name for legacy in _LEGACY_DIR_NAMES]


def backups_dir() -> Path:
    """Directory holding game-configuration backups."""
    return data_subdir("backups")


def resource_dir() -> Path:
    """Directory holding bundled read-only data (profiles, schemas).

    In a PyInstaller bundle the spec file places package data under
    ``<_MEIPASS>/travelready/…``, mirroring the source layout, so the same
    relative paths work frozen and unfrozen.
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        bundled = base / "travelready"
        return bundled if bundled.is_dir() else base
    return Path(__file__).parent
