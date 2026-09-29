"""cache.py — the local ROG Ally Life cache.

TravelReady must not hit the site on every start, and a traveller may be
offline exactly when they need the data. So retrieved posts are cached::

    <data>/cache/rogallylife/
        index.json                       what is known, when it was checked
        games/clair-obscur-expedition-33-rog-ally.json
        games/resident-evil-requiem-rog-ally-game.json

Each cached entry carries ``content_hash``, ``retrieved_at``, ``last_updated``
and ``parser_version``, which between them answer the three questions a refresh
needs: has the source changed, when did we last look, and was this extracted by
a parser we still trust.

Writes are atomic and confined to TravelReady's own data directory. Nothing in
here touches a game file — the cache is the source's data, not the user's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ...apppaths import data_subdir
from .model import SourceGame

CACHE_VERSION = 1
INDEX_FILE = "index.json"


def cache_root(root: Optional[Path] = None) -> Path:
    """The cache directory, created on demand."""
    if root is not None:
        path = Path(root)
        (path / "games").mkdir(parents=True, exist_ok=True)
        return path
    path = data_subdir("cache", "rogallylife")
    (path / "games").mkdir(parents=True, exist_ok=True)
    return path


def entry_key(game: SourceGame) -> str:
    """Filename stem for a cached game: slug, which already encodes the device."""
    slug = game.slug or re.sub(r"[^a-z0-9]+", "-", game.title.lower()).strip("-")
    return re.sub(r"[^a-z0-9._-]+", "-", slug.lower()).strip("-") or "entry"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class CacheIndex:
    """What the cache knows, without reading every game file."""

    cache_version: int = CACHE_VERSION
    parser_version: int = 0
    last_sync: str = ""
    source: str = "rogallylife"
    entries: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cache_version": self.cache_version,
            "parser_version": self.parser_version,
            "last_sync": self.last_sync,
            "source": self.source,
            "entries": self.entries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CacheIndex":
        if not isinstance(data, dict):
            return cls()
        entries = data.get("entries")
        return cls(
            cache_version=int(data.get("cache_version", 0) or 0),
            parser_version=int(data.get("parser_version", 0) or 0),
            last_sync=str(data.get("last_sync", "")),
            source=str(data.get("source", "rogallylife")),
            entries=entries if isinstance(entries, dict) else {},
        )


class ProfileCache:
    """Read/write access to the cached source data."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = cache_root(root)
        self.games_dir = self.root / "games"
        self.index = self._load_index()

    # -- index -------------------------------------------------------------

    def _index_path(self) -> Path:
        return self.root / INDEX_FILE

    def _load_index(self) -> CacheIndex:
        path = self._index_path()
        if not path.exists():
            return CacheIndex()
        try:
            return CacheIndex.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # A corrupt index is recoverable: the game files are the truth, so
            # move it aside and rebuild rather than losing the cache.
            try:
                shutil.move(str(path), str(path.with_suffix(".corrupt")))
            except OSError:
                pass
            return CacheIndex()

    def save_index(self) -> None:
        self.index.last_sync = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_write(self._index_path(), json.dumps(self.index.to_dict(), indent=2))

    # -- games -------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        return self.games_dir / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.path_for(key).exists()

    def get(self, key: str) -> Optional[SourceGame]:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            return SourceGame.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def put(self, game: SourceGame) -> str:
        """Cache one game and update the index. Returns its key."""
        key = entry_key(game)
        _atomic_write(self.path_for(key), json.dumps(game.to_dict(), indent=2))
        self.index.entries[key] = {
            "title": game.title,
            "source_url": game.source_url,
            "device_family": game.device_family,
            "content_hash": game.content_hash,
            "last_updated": game.last_updated,
            "retrieved_at": game.retrieved_at,
            "parser_version": game.parser_version,
            "profiles": len(game.profiles),
        }
        self.index.parser_version = max(self.index.parser_version, game.parser_version)
        return key

    def remove(self, key: str) -> bool:
        removed = False
        path = self.path_for(key)
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
        self.index.entries.pop(key, None)
        return removed

    def all_games(self) -> List[SourceGame]:
        """Every cached game. Unreadable files are skipped, not fatal."""
        out: List[SourceGame] = []
        if not self.games_dir.is_dir():
            return out
        for path in sorted(self.games_dir.glob("*.json")):
            try:
                out.append(SourceGame.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return out

    def games_for_device(self, device: str) -> List[SourceGame]:
        return [g for g in self.all_games() if g.covers_device(device)]

    # -- freshness ---------------------------------------------------------

    def is_stale(self, key: str, *, parser_version: int,
                 content_hash: str = "", last_updated: str = "") -> bool:
        """Does this entry need re-fetching?

        Stale when absent, when extracted by an older parser, or when the
        source reports a different hash or modification time.
        """
        record = self.index.entries.get(key)
        if not record or not self.has(key):
            return True
        if int(record.get("parser_version", 0) or 0) < parser_version:
            return True
        if content_hash and record.get("content_hash") != content_hash:
            return True
        if last_updated and record.get("last_updated") != last_updated:
            return True
        return False

    def needs_reparse(self, parser_version: int) -> List[str]:
        """Keys extracted by an older parser version."""
        return [key for key, record in self.index.entries.items()
                if int(record.get("parser_version", 0) or 0) < parser_version]

    def stats(self) -> dict:
        games = self.all_games()
        return {
            "entries": len(self.index.entries),
            "files": len(games),
            "profiles": sum(len(g.profiles) for g in games),
            "last_sync": self.index.last_sync,
            "parser_version": self.index.parser_version,
            "root": str(self.root),
        }
