"""model.py — the ROG Ally Life data model, as the site actually publishes it.

Two things shape this module.

**A game has several profiles, not one.** The site publishes, for example,
*Resident Evil Requiem* with "900P and 1080P resolution profiles and 15/18W and
18/25/30W power modes", and the graphics table differs between them. So
:class:`SourceGame` holds a list of :class:`SourceProfile`.

**The setting vocabulary is open-ended.** *Hair Strands* appears on one game
and nowhere else; *Screen Space Reflections*, *FSR 4.02c* and *Frame
Generation* appear on some. A fixed schema would fit one game and lose data on
the next, so a profile stores settings as an ordered list of
:class:`SourceSetting`, each keeping the site's own label verbatim alongside a
canonical key where TravelReady recognises one. Unrecognised settings are
preserved and shown, never dropped and never guessed at.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

#: The device families the site publishes for. Each is a *separate post*: the
#: same game has one URL for the Ally family and another for the Xbox Ally
#: family, so the device is known from the URL before the page is parsed.
DEVICE_ROG_ALLY_FAMILY = "rog_ally_family"      # ROG Ally (Z1 Extreme) + ROG Ally X
DEVICE_ROG_XBOX_ALLY_FAMILY = "rog_xbox_ally_family"   # ROG Xbox Ally / X / X20

DEVICE_FAMILIES = (DEVICE_ROG_ALLY_FAMILY, DEVICE_ROG_XBOX_ALLY_FAMILY)

#: Which TravelReady target devices each family's advice covers. The site states
#: that editorial settings are tested on the original ROG Ally (Z1 Extreme) and
#: the ROG Ally X, so one post legitimately covers both.
FAMILY_COVERS = {
    DEVICE_ROG_ALLY_FAMILY: ("rog_ally", "rog_ally_x"),
    DEVICE_ROG_XBOX_ALLY_FAMILY: ("rog_xbox_ally", "rog_xbox_ally_x"),
}

#: URL slug suffixes seen in real post URLs, longest first so the most specific
#: match wins. Truncated slugs ("-xbox-all") occur and must be recognised.
_SLUG_DEVICE_SUFFIXES = (
    ("-rog-xbox-ally-x-game-settings", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-rog-xbox-ally-x20-game-settings", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-rog-xbox-ally-x-game", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-rog-xbox-ally-x", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-rog-xbox-ally", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-xbox-ally-x20", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-xbox-ally-x", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-xbox-ally", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-xbox-all", DEVICE_ROG_XBOX_ALLY_FAMILY),
    ("-rog-ally-x-game-settings", DEVICE_ROG_ALLY_FAMILY),
    ("-rog-ally-game-settings", DEVICE_ROG_ALLY_FAMILY),
    ("-rog-ally-game", DEVICE_ROG_ALLY_FAMILY),
    ("-rog-ally-x", DEVICE_ROG_ALLY_FAMILY),
    ("-rog-ally", DEVICE_ROG_ALLY_FAMILY),
)


def device_family_from_url(url: str) -> Optional[str]:
    """Which device family a post URL is for, or ``None`` if it is not a
    settings post.

    The Xbox suffixes are tested first because ``-rog-xbox-ally-x`` also ends
    with ``-ally-x``; ordering here is what keeps an Xbox Ally post from being
    read as ROG Ally advice.
    """
    slug = str(url or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if not slug:
        return None
    for suffix, family in _SLUG_DEVICE_SUFFIXES:
        if slug.endswith(suffix):
            return family
    return None


#: Trailing phrases the site appends to a post title. Stripped so the stored
#: title is the game's name, which is what the matcher compares against.
_TITLE_SUFFIX = re.compile(
    r"\s*[-–—|:]?\s*"
    r"(?:(?:on|for)\s+)?"
    r"(?:(?:rog\s*(?:xbox\s*)?ally(?:\s*x20|\s*x)?|community)\s*)?"
    r"(?:game\s*)?(?:settings|guide|optimi[sz]ed\s*settings)\s*$",
    re.IGNORECASE)

#: A trailing "on ROG Ally" with no "settings" word after it.
_TITLE_DEVICE_TAIL = re.compile(
    r"\s*[-–—|:]?\s*(?:on|for)?\s*"
    r"rog\s*(?:xbox\s*)?ally(?:\s*x20|\s*x)?\s*$", re.IGNORECASE)

_TITLE_PREFIX = re.compile(r"^\s*best\s+settings\s+for\s+", re.IGNORECASE)


def clean_game_title(title: str) -> str:
    """The game's name, with the site's own post-title decoration removed.

    ``"Resident Evil Requiem ROG Ally Game Settings"`` -> ``"Resident Evil
    Requiem"``. Leaving the suffix on would make every title look alike to the
    matcher, which compares whole normalised titles.
    """
    text = str(title or "").strip()
    text = _TITLE_PREFIX.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _TITLE_SUFFIX.sub("", text).strip()
        text = _TITLE_DEVICE_TAIL.sub("", text).strip()
    return text.strip(" -–—|:") or str(title or "").strip()


def title_from_slug(slug: str) -> str:
    """Best-effort game title from a post slug, for logging and fallback."""
    text = str(slug or "").rstrip("/").rsplit("/", 1)[-1].lower()
    for suffix, _ in _SLUG_DEVICE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.replace("-", " ").strip()


# --------------------------------------------------------------------------
# performance rating
# --------------------------------------------------------------------------

#: The site's own scale, quoted: 5 excellent, 4.5 great, 4 good, 3.5 average,
#: 3 playable, 2.5 and below unplayable.
RATING_MEANINGS = (
    (5.0, "excellent"),
    (4.5, "great"),
    (4.0, "good"),
    (3.5, "average"),
    (3.0, "playable"),
    (0.0, "unplayable"),
)


def rating_meaning(stars: Optional[float]) -> str:
    """The site's word for a star rating, or ``''`` when unrated."""
    if stars is None:
        return ""
    for threshold, word in RATING_MEANINGS:
        if stars >= threshold:
            return word
    return "unplayable"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

@dataclass
class SourceSetting:
    """One line of a settings table, exactly as the site publishes it.

    ``label`` and ``value`` are verbatim. ``canonical`` is TravelReady's own
    key when the label is recognised, and ``''`` when it is not — an
    unrecognised setting is still carried and still shown to the user, it just
    cannot be applied automatically.
    """

    label: str
    value: str
    canonical: str = ""
    section: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSetting":
        return cls(
            label=str(data.get("label", "")),
            value=str(data.get("value", "")),
            canonical=str(data.get("canonical", "")),
            section=str(data.get("section", "")),
        )


_TDP_RE = re.compile(r"(\d{1,2})\s*(?:/\s*(\d{1,2})\s*)*w\b", re.IGNORECASE)
_RES_RE = re.compile(r"\b(\d{3,4})\s*p\b", re.IGNORECASE)
_VRAM_RE = re.compile(r"(\d{1,2})\s*gb\b", re.IGNORECASE)


def parse_tdp_watts(text: str) -> List[int]:
    """Every wattage in a profile label. ``'15/18W'`` -> ``[15, 18]``."""
    out: List[int] = []
    for chunk in re.findall(r"((?:\d{1,2}\s*/\s*)*\d{1,2})\s*w\b", str(text or ""), re.IGNORECASE):
        for part in re.split(r"\s*/\s*", chunk):
            if part.strip().isdigit():
                value = int(part)
                if 3 <= value <= 60 and value not in out:
                    out.append(value)
    return sorted(out)


def parse_resolution(text: str) -> str:
    """``'900p'`` from a profile label, or ``''``."""
    m = _RES_RE.search(str(text or ""))
    return f"{m.group(1)}p" if m else ""


def parse_vram_gb(text: str) -> Optional[int]:
    """``8`` from ``'8GB VRAM'``, or ``None``."""
    m = _VRAM_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


@dataclass
class SourceProfile:
    """One performance configuration for a game.

    A game commonly has several, differing in TDP, resolution and the graphics
    table itself.
    """

    name: str = ""
    tdp_watts: List[int] = field(default_factory=list)
    resolution: str = ""
    vram_gb: Optional[int] = None
    settings: List[SourceSetting] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.tdp_watts:
            self.tdp_watts = parse_tdp_watts(self.name)
        if not self.resolution:
            self.resolution = parse_resolution(self.name)
        if self.vram_gb is None:
            self.vram_gb = parse_vram_gb(self.name)
        self.tdp_watts = sorted({int(w) for w in self.tdp_watts})

    @property
    def label(self) -> str:
        """A stable display label: ``15/18W • 900p • 8GB VRAM``."""
        parts = []
        if self.tdp_watts:
            parts.append("/".join(str(w) for w in self.tdp_watts) + "W")
        if self.resolution:
            parts.append(self.resolution)
        if self.vram_gb:
            parts.append(f"{self.vram_gb}GB VRAM")
        return " • ".join(parts) or (self.name or "profile")

    @property
    def min_tdp(self) -> Optional[int]:
        return self.tdp_watts[0] if self.tdp_watts else None

    @property
    def max_tdp(self) -> Optional[int]:
        return self.tdp_watts[-1] if self.tdp_watts else None

    def setting(self, canonical: str) -> Optional[SourceSetting]:
        for item in self.settings:
            if item.canonical and item.canonical == canonical:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tdp_watts": list(self.tdp_watts),
            "resolution": self.resolution,
            "vram_gb": self.vram_gb,
            "notes": self.notes,
            "settings": [s.to_dict() for s in self.settings],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceProfile":
        return cls(
            name=str(data.get("name", "")),
            tdp_watts=[int(w) for w in data.get("tdp_watts", [])],
            resolution=str(data.get("resolution", "")),
            vram_gb=(int(data["vram_gb"]) if data.get("vram_gb") is not None else None),
            notes=str(data.get("notes", "")),
            settings=[SourceSetting.from_dict(s) for s in data.get("settings", [])],
        )


@dataclass
class SourceGame:
    """Everything ROG Ally Life publishes about one game on one device family."""

    title: str
    source_url: str
    device_family: str
    profiles: List[SourceProfile] = field(default_factory=list)
    slug: str = ""
    published_at: str = ""
    last_updated: str = ""
    retrieved_at: str = ""
    performance_rating: Optional[float] = None
    performance_summary: str = ""
    release_date: str = ""
    store_links: List[str] = field(default_factory=list)
    image_url: str = ""
    notes: str = ""
    content_hash: str = ""
    parser_version: int = 0
    source: str = "rogallylife"
    source_name: str = "ROG Ally Life"

    @property
    def rating_word(self) -> str:
        return rating_meaning(self.performance_rating)

    def covers_device(self, device: str) -> bool:
        """Is this post's advice written for ``device``?"""
        return device in FAMILY_COVERS.get(self.device_family, ())

    def profile_labels(self) -> List[str]:
        return [p.label for p in self.profiles]

    def attribution(self) -> str:
        bits = [self.source_name]
        if self.last_updated:
            bits.append(f"updated {self.last_updated}")
        return f"{' — '.join(bits)} ({self.source_url})"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "slug": self.slug,
            "title": self.title,
            "device_family": self.device_family,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "last_updated": self.last_updated,
            "release_date": self.release_date,
            "performance_rating": self.performance_rating,
            "performance_summary": self.performance_summary,
            "store_links": list(self.store_links),
            "image_url": self.image_url,
            "notes": self.notes,
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "profiles": [p.to_dict() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceGame":
        rating = data.get("performance_rating")
        return cls(
            title=str(data.get("title", "")),
            source_url=str(data.get("source_url", "")),
            device_family=str(data.get("device_family", "")),
            profiles=[SourceProfile.from_dict(p) for p in data.get("profiles", [])],
            slug=str(data.get("slug", "")),
            published_at=str(data.get("published_at", "")),
            last_updated=str(data.get("last_updated", "")),
            retrieved_at=str(data.get("retrieved_at", "")),
            performance_rating=(float(rating) if rating is not None else None),
            performance_summary=str(data.get("performance_summary", "")),
            release_date=str(data.get("release_date", "")),
            store_links=[str(x) for x in data.get("store_links", [])],
            image_url=str(data.get("image_url", "")),
            notes=str(data.get("notes", "")),
            content_hash=str(data.get("content_hash", "")),
            parser_version=int(data.get("parser_version", 0) or 0),
            source=str(data.get("source", "rogallylife")),
            source_name=str(data.get("source_name", "ROG Ally Life")),
        )
