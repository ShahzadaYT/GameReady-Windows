"""model.py — value types for the settings system.

A ROG Ally Life recommendation is **data**, never an instruction. Nothing in
this module can execute, and nothing downstream treats a profile field as a
command: a profile can only ever name a setting and a desired value, which
TravelReady then independently decides whether it is allowed to apply.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

# --------------------------------------------------------------------------
# safety classification
# --------------------------------------------------------------------------

SAFE = "SAFE"
CAUTION = "CAUTION"
MANUAL = "MANUAL"
RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
BLOCKED = "BLOCKED"

#: Ordered most- to least-permissive. There are deliberately no numeric scores
#: and no "probably safe": a change is either understood well enough to apply
#: or it is not.
SAFETY_CLASSES = (SAFE, CAUTION, MANUAL, RESEARCH_REQUIRED, BLOCKED)

#: Only these may ever be applied automatically.
AUTO_APPLICABLE = (SAFE,)

_SEVERITY = {cls: i for i, cls in enumerate(SAFETY_CLASSES)}


def worst(*classes: str) -> str:
    """The least permissive of ``classes`` — safety composes by failing closed."""
    known = [c for c in classes if c in _SEVERITY]
    if not known:
        return RESEARCH_REQUIRED
    return max(known, key=lambda c: _SEVERITY[c])


def is_auto_applicable(safety: str) -> bool:
    return safety in AUTO_APPLICABLE


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

DEVICE_ROG_ALLY_X = "rog_ally_x"
DEVICE_ROG_ALLY = "rog_ally"
DEVICE_ROG_XBOX_ALLY = "rog_xbox_ally"
DEVICE_ROG_XBOX_ALLY_X = "rog_xbox_ally_x"
DEVICE_STEAM_DECK = "steam_deck"
DEVICE_LEGION_GO = "legion_go"
DEVICE_UNKNOWN = "unknown"

KNOWN_DEVICES = (
    DEVICE_ROG_ALLY_X, DEVICE_ROG_ALLY, DEVICE_ROG_XBOX_ALLY,
    DEVICE_ROG_XBOX_ALLY_X, DEVICE_STEAM_DECK, DEVICE_LEGION_GO,
)

#: The device this build targets. A profile for any other device is never
#: applied automatically, however similar the hardware looks.
TARGET_DEVICE = DEVICE_ROG_ALLY_X


# --------------------------------------------------------------------------
# setting categories
# --------------------------------------------------------------------------

CATEGORY_GAME = "game"
CATEGORY_DEVICE = "device"

#: Game settings TravelReady understands well enough to consider automating.
#: A setting not named here can never be SAFE, whatever file it lives in.
KNOWN_GAME_SETTINGS = (
    "resolution", "resolution_width", "resolution_height", "display_mode",
    "graphics_quality", "texture_quality", "shadow_quality", "effects_quality",
    "anti_aliasing", "upscaling_mode", "fsr_mode", "fsr_sharpness",
    "frame_rate_limit", "vsync", "view_distance", "post_processing",
    "motion_blur", "ambient_occlusion", "reflection_quality",
)

#: Device-wide settings. MANUAL in this release — TravelReady does not change
#: TDP, fan curves, VRAM allocation, Armoury Crate, Adrenalin or Windows power
#: settings. They are surfaced so the user can apply them, never automated.
KNOWN_DEVICE_SETTINGS = (
    "tdp_watts", "power_mode", "fan_profile", "vram_allocation_gb",
    "armoury_crate_profile", "adrenalin_profile", "windows_power_plan",
    "refresh_rate_hz", "resolution_scale_device",
)


def category_of(key: str) -> str:
    return CATEGORY_DEVICE if key in KNOWN_DEVICE_SETTINGS else CATEGORY_GAME


# --------------------------------------------------------------------------
# value types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SettingValue:
    """One setting's value, as read from a config file or a profile."""

    key: str
    value: Optional[str] = None
    raw: Optional[str] = None
    location: str = ""          # e.g. "GameUserSettings.ini [ScalabilityGroups] sg.ShadowQuality"
    present: bool = True

    def display(self) -> str:
        if not self.present or self.value is None:
            return "not set"
        return str(self.value)


@dataclass
class Recommendation:
    """A single recommended value from a profile source.

    ``source`` and ``source_url`` are mandatory at load time: a recommendation
    with no attribution is rejected by the schema, so TravelReady can never
    present an invented value as ROG Ally Life's.
    """

    key: str
    value: str
    category: str = CATEGORY_GAME
    note: str = ""
    optional: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GameProfile:
    """A recommended settings profile for one game on one device.

    This is inert data. TravelReady reads it, compares it with the game's
    current configuration, and independently decides what it is allowed to
    change. A profile cannot request an action.
    """

    game_name: str
    device: str
    source: str
    source_url: str
    recommendations: List[Recommendation] = field(default_factory=list)
    launcher: str = ""
    game_identity: str = ""
    source_date: str = ""
    source_version: str = ""
    profile_notes: str = ""
    device_notes: str = ""
    hardware_assumptions: str = ""
    exceptions: List[str] = field(default_factory=list)
    config_hint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recommendations"] = [r.to_dict() for r in self.recommendations]
        return d

    def game_settings(self) -> List[Recommendation]:
        return [r for r in self.recommendations if r.category == CATEGORY_GAME]

    def device_settings(self) -> List[Recommendation]:
        return [r for r in self.recommendations if r.category == CATEGORY_DEVICE]

    @property
    def attribution(self) -> str:
        """Always shown next to a recommendation in the UI."""
        parts = [self.source]
        if self.source_date:
            parts.append(self.source_date)
        return f"{' — '.join(parts)} ({self.source_url})"


@dataclass
class ProposedChange:
    """One proposed edit, with its safety classification and reason.

    ``safety`` is assigned by :mod:`travelready.optimiser.safety` and is never
    taken from the profile. ``applied`` and ``approved`` are set only by the
    transaction engine.
    """

    key: str
    current: SettingValue
    recommended: str
    safety: str = RESEARCH_REQUIRED
    reason: str = ""
    category: str = CATEGORY_GAME
    file_path: str = ""
    section: str = ""
    config_key: str = ""
    approved: bool = False
    applied: bool = False
    note: str = ""

    @property
    def is_change(self) -> bool:
        """False when the game is already set to the recommended value."""
        return (self.current.value or "").strip().lower() != str(self.recommended).strip().lower()

    @property
    def auto_applicable(self) -> bool:
        return is_auto_applicable(self.safety) and self.is_change

    def describe(self) -> str:
        return f"{self.key}: {self.current.display()} → {self.recommended}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["current"] = asdict(self.current)
        return d


@dataclass
class ChangePlan:
    """Everything proposed for one game, grouped by what may be done with it."""

    game_name: str
    profile: Optional[GameProfile] = None
    changes: List[ProposedChange] = field(default_factory=list)
    config_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: How the source profile was found, when one came from an external source.
    #: Carries the match confidence and the profile-selection reasoning so the
    #: UI can show both. ``None`` for a locally imported profile.
    resolution: object = None
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def by_safety(self, safety: str) -> List[ProposedChange]:
        return [c for c in self.changes if c.safety == safety]

    @property
    def safe(self) -> List[ProposedChange]:
        return [c for c in self.by_safety(SAFE) if c.is_change]

    @property
    def caution(self) -> List[ProposedChange]:
        return [c for c in self.by_safety(CAUTION) if c.is_change]

    @property
    def manual(self) -> List[ProposedChange]:
        return [c for c in self.by_safety(MANUAL) if c.is_change]

    @property
    def research(self) -> List[ProposedChange]:
        return [c for c in self.by_safety(RESEARCH_REQUIRED) if c.is_change]

    @property
    def blocked(self) -> List[ProposedChange]:
        return self.by_safety(BLOCKED)

    @property
    def already_correct(self) -> List[ProposedChange]:
        return [c for c in self.changes if not c.is_change]

    def applicable(self) -> List[ProposedChange]:
        """Only SAFE, changed settings — the set an Apply may ever touch."""
        return [c for c in self.changes if c.auto_applicable]

    def to_dict(self) -> dict:
        return {
            "game_name": self.game_name,
            "generated_at": self.generated_at,
            "profile": self.profile.to_dict() if self.profile else None,
            "changes": [c.to_dict() for c in self.changes],
            "config_files": list(self.config_files),
            "warnings": list(self.warnings),
            "match": (
                {
                    "matched_title": self.resolution.match.matched_title,
                    "confidence": self.resolution.match.confidence,
                    "reason": self.resolution.match.reason,
                    "source_url": self.resolution.match.source_url,
                }
                if getattr(self.resolution, "match", None) else None),
            "profile_selection": (
                {
                    "profile": self.resolution.selection.profile.label,
                    "mode": self.resolution.selection.mode,
                    "reason": self.resolution.selection.reason,
                    "alternatives": list(self.resolution.selection.alternatives),
                }
                if getattr(getattr(self.resolution, "selection", None), "profile", None)
                else None),
        }
