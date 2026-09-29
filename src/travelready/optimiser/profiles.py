"""profiles.py — the ROG Ally Life recommended-settings store.

ROG Ally Life (https://rogallylife.com/) is the **primary** source of
recommended per-game settings for the ROG Ally family. This module stores those
recommendations as inert, schema-validated, attributed data.

Three rules govern the store, and the loader enforces all three:

1. **Attribution is mandatory.** A profile without ``source`` and
   ``source_url`` is rejected. Nothing can be shown to the user as a ROG Ally
   Life recommendation unless it carries a link back to where it came from.
2. **Nothing is invented.** A profile may only contain values present in its
   source. There is no defaulting, no interpolation between profiles and no
   generated "sensible" fallback. A field the source did not state is absent,
   and absent means "not recommended", not "guess".
3. **ROG Ally Life is not ASUS.** ``source`` is recorded verbatim and the UI
   prints it next to every value. These are community recommendations and are
   never described as official ASUS guidance.

Profiles are data files, not code. :func:`load_profile` refuses any structure
it does not recognise, so a malformed or hostile profile cannot widen what
TravelReady will do — the safety engine classifies every value independently
of what the profile claims.

Shipped content
---------------
**This build ships no ROG Ally Life profiles.** ``rogallylife.com`` was not
reachable from the environment this code was written in, and inventing plausible
settings would violate rule 2 above. :func:`fetch_profile_source` is provided so
profiles can be captured on a machine with access, and :func:`import_profile`
ingests them. :func:`profile_count` reports honestly how many are installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..apppaths import data_subdir, resource_dir
from ..library import identity_key
from .model import (
    CATEGORY_DEVICE, CATEGORY_GAME, KNOWN_DEVICE_SETTINGS, KNOWN_GAME_SETTINGS,
    KNOWN_DEVICES, GameProfile, Recommendation,
)

PROFILE_SCHEMA_VERSION = "1"
SOURCE_ROG_ALLY_LIFE = "ROG Ally Life"
ROG_ALLY_LIFE_BASE = "https://rogallylife.com/"

#: Where bundled profiles live (read-only), and where imported ones go.
BUNDLED_PROFILE_DIR = resource_dir() / "optimiser" / "data" / "profiles"


class ProfileError(Exception):
    """Raised when a profile file is malformed or unattributed."""


def user_profile_dir() -> Path:
    """Writable directory for profiles the user imports."""
    return data_subdir("profiles")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_ALLOWED_TOP_LEVEL = {
    "schema_version", "game_name", "game_identity", "launcher", "device",
    "source", "source_url", "source_date", "source_version", "profile_notes",
    "device_notes", "hardware_assumptions", "exceptions", "config_hint",
    "recommendations",
}
_ALLOWED_REC_KEYS = {"key", "value", "category", "note", "optional"}
_URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def validate_profile_dict(data: dict) -> List[str]:
    """Return a list of problems with ``data``; empty means valid.

    Strict by design: unknown top-level keys and unknown setting keys are
    errors, not warnings. A profile that TravelReady does not fully understand
    is not loaded at all, rather than partially applied.
    """
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["Profile is not a JSON object."]

    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        problems.append(f"Unknown profile fields: {', '.join(sorted(unknown))}.")

    for required in ("game_name", "device", "source", "source_url"):
        if not str(data.get(required) or "").strip():
            problems.append(f"Missing required field '{required}'.")

    url = str(data.get("source_url") or "")
    if url and not _URL.match(url):
        problems.append("'source_url' must be an http(s) URL.")

    device = str(data.get("device") or "").strip().lower()
    if device and device not in KNOWN_DEVICES:
        problems.append(f"Unknown device '{device}'.")

    recs = data.get("recommendations")
    if not isinstance(recs, list) or not recs:
        problems.append("'recommendations' must be a non-empty list.")
        return problems

    for index, rec in enumerate(recs):
        where = f"recommendation {index + 1}"
        if not isinstance(rec, dict):
            problems.append(f"{where} is not an object.")
            continue
        unknown = set(rec) - _ALLOWED_REC_KEYS
        if unknown:
            problems.append(f"{where} has unknown fields: {', '.join(sorted(unknown))}.")
        key = str(rec.get("key") or "").strip().lower()
        if not key:
            problems.append(f"{where} has no 'key'.")
            continue
        if key not in KNOWN_GAME_SETTINGS and key not in KNOWN_DEVICE_SETTINGS:
            problems.append(
                f"{where}: '{key}' is not a setting TravelReady recognises.")
        if "value" not in rec or rec["value"] is None or str(rec["value"]).strip() == "":
            problems.append(f"{where} ('{key}') has no value. "
                            f"A recommendation with no value is never invented.")
        category = str(rec.get("category") or "").strip().lower()
        if category and category not in (CATEGORY_GAME, CATEGORY_DEVICE):
            problems.append(f"{where} has unknown category '{category}'.")
    return problems


def profile_from_dict(data: dict) -> GameProfile:
    """Build a :class:`GameProfile`, raising :class:`ProfileError` if invalid."""
    problems = validate_profile_dict(data)
    if problems:
        raise ProfileError("; ".join(problems))

    recs = []
    for rec in data["recommendations"]:
        key = str(rec["key"]).strip().lower()
        category = str(rec.get("category") or "").strip().lower()
        if not category:
            category = CATEGORY_DEVICE if key in KNOWN_DEVICE_SETTINGS else CATEGORY_GAME
        recs.append(Recommendation(
            key=key,
            value=str(rec["value"]).strip(),
            category=category,
            note=str(rec.get("note") or ""),
            optional=bool(rec.get("optional", False)),
        ))

    name = str(data["game_name"]).strip()
    return GameProfile(
        game_name=name,
        device=str(data["device"]).strip().lower(),
        source=str(data["source"]).strip(),
        source_url=str(data["source_url"]).strip(),
        recommendations=recs,
        launcher=str(data.get("launcher") or "").strip().lower(),
        game_identity=str(data.get("game_identity") or "").strip() or identity_key(name),
        source_date=str(data.get("source_date") or "").strip(),
        source_version=str(data.get("source_version") or "").strip(),
        profile_notes=str(data.get("profile_notes") or "").strip(),
        device_notes=str(data.get("device_notes") or "").strip(),
        hardware_assumptions=str(data.get("hardware_assumptions") or "").strip(),
        exceptions=[str(x) for x in (data.get("exceptions") or [])],
        config_hint=str(data.get("config_hint") or "").strip(),
    )


def load_profile(path: Path) -> GameProfile:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{Path(path).name}: {exc}") from exc
    return profile_from_dict(data)


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

@dataclass
class ProfileStore:
    """All installed profiles, indexed by game identity.

    Read-only: loading, listing and looking up profiles never writes. Only
    :meth:`import_profile` writes, and only into TravelReady's own directory.
    """

    profiles: List[GameProfile]
    errors: List[str]

    @classmethod
    def load(cls, directories: Optional[Sequence[Path]] = None) -> "ProfileStore":
        dirs = list(directories) if directories is not None else [
            BUNDLED_PROFILE_DIR, user_profile_dir()]
        profiles: List[GameProfile] = []
        errors: List[str] = []
        for directory in dirs:
            d = Path(directory)
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.json")):
                try:
                    profiles.append(load_profile(path))
                except ProfileError as exc:
                    errors.append(str(exc))
        return cls(profiles, errors)

    def __len__(self) -> int:
        return len(self.profiles)

    def for_device(self, device: str) -> List[GameProfile]:
        return [p for p in self.profiles if p.device == device]

    def find(self, game_name: str, device: Optional[str] = None,
             launcher: str = "") -> Optional[GameProfile]:
        """The profile for ``game_name``, preferring an exact device match.

        A profile for a *different* device is still returned so the UI can show
        it, but :func:`travelready.optimiser.safety.classify_device` will
        classify its recommendations MANUAL rather than applying them.
        """
        key = identity_key(game_name)
        candidates = [p for p in self.profiles
                      if (p.game_identity or identity_key(p.game_name)) == key]
        if launcher:
            preferred = [p for p in candidates if not p.launcher or p.launcher == launcher]
            candidates = preferred or candidates
        if not candidates:
            return None
        if device:
            exact = [p for p in candidates if p.device == device]
            if exact:
                return exact[0]
        return candidates[0]


def profile_count(store: Optional[ProfileStore] = None) -> int:
    return len(store or ProfileStore.load())


def import_profile(data: dict, directory: Optional[Path] = None) -> Path:
    """Validate and install a profile. The only write in this module."""
    profile = profile_from_dict(data)
    target_dir = Path(directory) if directory else user_profile_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", profile.game_name.lower()).strip("-") or "profile"
    path = target_dir / f"{slug}.{profile.device}.json"
    payload = dict(data)
    payload.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def import_profile_file(path: Path, directory: Optional[Path] = None) -> Path:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{Path(path).name}: {exc}") from exc
    return import_profile(data, directory)


# --------------------------------------------------------------------------
# capturing from the source
# --------------------------------------------------------------------------

def rog_ally_life_search_url(game_name: str) -> str:
    """The ROG Ally Life page to consult for ``game_name``.

    Shown in the UI so the user can read the source themselves. TravelReady
    presents the link; it does not scrape and reinterpret the page.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(game_name).lower()).strip("-")
    return f"{ROG_ALLY_LIFE_BASE}?s={slug}"


def fetch_profile_source(game_name: str, *, opener=None, timeout: int = 20) -> str:
    """Fetch the raw ROG Ally Life page for ``game_name``.

    Separated from parsing on purpose: the fetched HTML is a *source document*
    to be reviewed, never something TravelReady executes or trusts. Requires
    network access to rogallylife.com.
    """
    url = rog_ally_life_search_url(game_name)
    if opener is None:
        from urllib.request import urlopen
        opener = urlopen
    with opener(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def blank_profile_template(game_name: str, device: str = "rog_ally_x") -> dict:
    """A template for recording a profile by hand from the source page.

    Every value is left empty deliberately: the person filling it in copies the
    real numbers from ROG Ally Life. Nothing here is a suggested value.
    """
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "game_name": game_name,
        "device": device,
        "source": SOURCE_ROG_ALLY_LIFE,
        "source_url": rog_ally_life_search_url(game_name),
        "source_date": "",
        "source_version": "",
        "launcher": "",
        "profile_notes": "",
        "device_notes": "",
        "hardware_assumptions": "",
        "exceptions": [],
        "config_hint": "",
        "recommendations": [],
    }
