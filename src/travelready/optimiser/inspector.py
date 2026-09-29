"""inspector.py — read-only discovery of a game's current settings.

Everything here is read-only. Finding config files, parsing them and reading
current values performs no writes of any kind; ``tests/test_readonly.py``
enforces that against a filesystem sentinel.

Mapping a game's config keys to TravelReady's canonical setting names is done
through explicit, per-engine :class:`ConfigSchema` definitions. A file that
does not match a known schema yields no automatable settings — its values are
reported for display, but every one of them classifies as RESEARCH_REQUIRED, so
"it's an .ini file" never by itself makes anything safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..library import GameEntry, as_path
from .configio import ConfigError, IniDocument, JsonDocument, load_document
from .model import SettingValue
from .safety import classify_path

# --------------------------------------------------------------------------
# known configuration schemas
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyMapping:
    """One canonical setting mapped to a concrete config key."""

    setting: str
    config_key: str
    section: str = ""
    values: Dict[str, str] = field(default_factory=dict)      # canonical -> file value
    reverse: Dict[str, str] = field(default_factory=dict)     # file value -> canonical

    def to_file_value(self, canonical: str) -> str:
        return self.values.get(str(canonical).strip().lower(), str(canonical))

    def to_canonical(self, file_value: str) -> str:
        return self.reverse.get(str(file_value).strip().lower(), str(file_value).strip())


def _quality_scale() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Unreal's 0-3 scalability scale, both directions."""
    forward = {"low": "0", "medium": "1", "high": "2", "epic": "3", "ultra": "3",
               "0": "0", "1": "1", "2": "2", "3": "3"}
    reverse = {"0": "Low", "1": "Medium", "2": "High", "3": "Epic"}
    return forward, reverse


_Q, _QR = _quality_scale()


@dataclass(frozen=True)
class ConfigSchema:
    """A recognised configuration file format for a known engine."""

    name: str
    filename: str
    mappings: Tuple[KeyMapping, ...]
    relative_paths: Tuple[str, ...] = ()

    def mapping_for(self, setting: str) -> Optional[KeyMapping]:
        for m in self.mappings:
            if m.setting == setting:
                return m
        return None


UNREAL_GAME_USER_SETTINGS = ConfigSchema(
    name="Unreal Engine GameUserSettings",
    filename="GameUserSettings.ini",
    relative_paths=(
        r"Saved\Config\WindowsNoEditor",
        r"Saved\Config\Windows",
        r"Saved\Config\WinGDK",
    ),
    mappings=(
        KeyMapping("resolution_width", "ResolutionSizeX", "/Script/Engine.GameUserSettings"),
        KeyMapping("resolution_height", "ResolutionSizeY", "/Script/Engine.GameUserSettings"),
        KeyMapping("display_mode", "FullscreenMode", "/Script/Engine.GameUserSettings",
                   {"fullscreen": "0", "windowed fullscreen": "1", "borderless": "1",
                    "windowed": "2"},
                   {"0": "Fullscreen", "1": "Borderless", "2": "Windowed"}),
        KeyMapping("frame_rate_limit", "FrameRateLimit", "/Script/Engine.GameUserSettings"),
        KeyMapping("vsync", "bUseVSync", "/Script/Engine.GameUserSettings",
                   {"on": "True", "off": "False", "true": "True", "false": "False"},
                   {"true": "On", "false": "Off"}),
        KeyMapping("texture_quality", "sg.TextureQuality", "ScalabilityGroups", _Q, _QR),
        KeyMapping("shadow_quality", "sg.ShadowQuality", "ScalabilityGroups", _Q, _QR),
        KeyMapping("effects_quality", "sg.EffectsQuality", "ScalabilityGroups", _Q, _QR),
        KeyMapping("anti_aliasing", "sg.AntiAliasingQuality", "ScalabilityGroups", _Q, _QR),
        KeyMapping("view_distance", "sg.ViewDistanceQuality", "ScalabilityGroups", _Q, _QR),
        KeyMapping("post_processing", "sg.PostProcessQuality", "ScalabilityGroups", _Q, _QR),
    ),
)

KNOWN_SCHEMAS: Tuple[ConfigSchema, ...] = (UNREAL_GAME_USER_SETTINGS,)


def schema_for_file(path: str) -> Optional[ConfigSchema]:
    """The schema matching ``path`` by filename, or ``None``."""
    leaf = os.path.basename(str(path).replace("\\", "/")).lower()
    for schema in KNOWN_SCHEMAS:
        if schema.filename.lower() == leaf:
            return schema
    return None


# --------------------------------------------------------------------------
# locating config files
# --------------------------------------------------------------------------

def candidate_config_paths(entry: GameEntry, *, home: Optional[str] = None) -> List[str]:
    """Paths that *might* hold this game's configuration. Read-only guesswork.

    Returns candidates only; :func:`inspect_game` checks which exist and which
    are allowed to be touched.
    """
    out: List[str] = []
    base = entry.install_dir or entry.working_dir
    if base:
        for schema in KNOWN_SCHEMAS:
            for rel in schema.relative_paths:
                out.append(str(as_path(base) / rel / schema.filename))
    local = home or os.environ.get("LOCALAPPDATA") or ""
    if local and entry.name:
        safe_name = entry.name.replace(":", "").replace("™", "").replace("®", "").strip()
        for schema in KNOWN_SCHEMAS:
            for rel in schema.relative_paths:
                out.append(str(as_path(local) / safe_name / rel / schema.filename))
    seen, unique = set(), []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique.append(p)
    return unique


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

@dataclass
class InspectedFile:
    """One config file that was found and read. Nothing was written."""

    path: str
    schema: Optional[ConfigSchema]
    readable: bool
    values: Dict[str, SettingValue] = field(default_factory=dict)
    note: str = ""

    @property
    def recognised(self) -> bool:
        return self.schema is not None


@dataclass
class Inspection:
    """Everything read about one game's configuration. Read-only."""

    game_name: str
    files: List[InspectedFile] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def current(self, setting: str) -> SettingValue:
        for f in self.files:
            if setting in f.values:
                return f.values[setting]
        return SettingValue(key=setting, value=None, present=False)

    def file_for(self, setting: str) -> Optional[InspectedFile]:
        for f in self.files:
            if setting in f.values:
                return f
        return None

    @property
    def recognised_files(self) -> List[InspectedFile]:
        return [f for f in self.files if f.recognised]


def read_config_file(path: str) -> InspectedFile:
    """Parse one config file and extract every canonical setting it holds."""
    schema = schema_for_file(path)
    verdict = classify_path(path, must_exist=True)
    if verdict.blocked:
        return InspectedFile(path, schema, readable=False,
                             note=f"Not inspected: {verdict.reason}")
    try:
        document = load_document(path)
    except (ConfigError, OSError) as exc:
        return InspectedFile(path, schema, readable=False, note=str(exc))

    if isinstance(document, IniDocument) and not document.parse_ok:
        return InspectedFile(path, schema, readable=False, note=document.parse_note)
    if isinstance(document, JsonDocument) and not document.parse_ok:
        return InspectedFile(path, schema, readable=False, note=document.parse_note)

    values: Dict[str, SettingValue] = {}
    if schema is not None and isinstance(document, IniDocument):
        for mapping in schema.mappings:
            raw = document.get(mapping.config_key, mapping.section or None)
            if raw is None:
                raw = document.get(mapping.config_key)
            if raw is None:
                continue
            values[mapping.setting] = SettingValue(
                key=mapping.setting,
                value=mapping.to_canonical(raw),
                raw=raw,
                location=f"{os.path.basename(path)} [{mapping.section}] {mapping.config_key}",
            )
        # Unreal stores resolution as two keys; present a combined view too.
        w, h = values.get("resolution_width"), values.get("resolution_height")
        if w and h and w.value and h.value:
            values["resolution"] = SettingValue(
                key="resolution", value=f"{w.value}x{h.value}",
                raw=f"{w.raw}x{h.raw}",
                location=f"{os.path.basename(path)} ResolutionSizeX/Y")
    return InspectedFile(path, schema, readable=True, values=values,
                         note="" if schema else
                         "File format not recognised; values shown for reference only.")


def inspect_game(entry: GameEntry, *, paths: Optional[Sequence[str]] = None,
                 exists=os.path.isfile) -> Inspection:
    """Find and read this game's configuration. Performs **no** writes."""
    inspection = Inspection(game_name=entry.name)
    candidates = list(paths) if paths is not None else candidate_config_paths(entry)
    found = [p for p in candidates if exists(p)]
    if not found:
        inspection.warnings.append(
            "No recognised configuration file was found for this game. "
            "TravelReady can show recommendations but cannot read or change "
            "current settings.")
        return inspection
    for path in found:
        inspection.files.append(read_config_file(path))
    if not inspection.recognised_files:
        inspection.warnings.append(
            "Configuration files were found but none matches a format TravelReady "
            "understands, so no setting can be changed automatically.")
    return inspection
