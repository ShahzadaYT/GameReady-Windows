"""capability.py — what TravelReady can actually do with a given setting.

ROG Ally Life publishes whatever a game exposes. That includes things
TravelReady can write to a config file, things that live in Armoury Crate or
the driver, and things that are the author's opinion. Pretending the three are
the same would be the quickest way to mislead someone into thinking a profile
had been applied when it had not.

So every canonical setting is registered with three independent capabilities:

``detect``  can TravelReady read the game's current value?
``apply``   can it write the value safely?
``verify``  can it read the value back and confirm the write?

A setting the site names but that is not registered here is **informational**:
it is shown, attributed, and marked as needing manual action. It is never
silently dropped, and never applied.

Registering a setting here does not make it applicable on its own — the
capability says "TravelReady knows how", while
:mod:`travelready.optimiser.safety` independently decides "and is it allowed to
here, in this file, on this device". Both must agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..model import CATEGORY_DEVICE, CATEGORY_GAME

#: Applied by writing the game's own configuration file.
SCOPE_GAME = CATEGORY_GAME
#: Applied outside the game — Armoury Crate, the driver, Windows.
SCOPE_DEVICE = CATEGORY_DEVICE
#: Not a setting at all: commentary, FPS claims, playability opinions.
SCOPE_INFO = "informational"


@dataclass(frozen=True)
class Capability:
    """What TravelReady can do with one canonical setting."""

    key: str
    scope: str
    detect: bool
    apply: bool
    verify: bool
    note: str = ""

    @property
    def automatable(self) -> bool:
        return self.scope == SCOPE_GAME and self.detect and self.apply and self.verify

    @property
    def status(self) -> str:
        if self.automatable:
            return "automatic"
        if self.scope == SCOPE_DEVICE:
            return "manual (device)"
        if self.scope == SCOPE_INFO:
            return "informational"
        return "manual"


def _game(key: str, *, apply: bool = True, note: str = "") -> Capability:
    return Capability(key, SCOPE_GAME, detect=True, apply=apply, verify=apply, note=note)


def _device(key: str, note: str) -> Capability:
    return Capability(key, SCOPE_DEVICE, detect=False, apply=False, verify=False, note=note)


def _info(key: str, note: str) -> Capability:
    return Capability(key, SCOPE_INFO, detect=False, apply=False, verify=False, note=note)


#: The registry. ``apply=False`` on a game setting means TravelReady can read
#: and report it but has no validated way to write it.
CAPABILITIES: Dict[str, Capability] = {c.key: c for c in (
    # -- game settings TravelReady writes -------------------------------
    _game("resolution"),
    _game("resolution_width"),
    _game("resolution_height"),
    _game("display_mode"),
    _game("vsync"),
    _game("frame_rate_limit"),
    _game("graphics_quality"),
    _game("texture_quality"),
    _game("shadow_quality"),
    _game("effects_quality"),
    _game("view_distance"),
    _game("post_processing"),
    _game("anti_aliasing"),
    _game("ambient_occlusion"),
    _game("reflection_quality"),
    _game("motion_blur"),
    _game("upscaling_mode"),
    _game("fsr_mode"),
    _game("fsr_sharpness"),

    # -- game settings recognised but not yet writable -------------------
    _game("frame_generation", apply=False,
          note="Recognised, but the config key differs per engine and is not "
               "validated yet. Reported; set it in the game."),
    _game("ray_tracing", apply=False,
          note="Recognised but not written automatically — the key and its "
               "dependencies vary per engine."),
    _game("hair_strands", apply=False,
          note="Game-specific setting with no validated config mapping."),
    _game("screen_space_reflections", apply=False,
          note="Game-specific setting with no validated config mapping."),
    _game("volumetric_quality", apply=False, note="No validated config mapping."),
    _game("water_quality", apply=False, note="No validated config mapping."),
    _game("crowd_density", apply=False, note="No validated config mapping."),

    # -- device settings: shown, never changed ---------------------------
    _device("tdp_watts",
            "Set in Armoury Crate (Operating Mode). TravelReady does not change TDP."),
    _device("vram_allocation_gb",
            "Set in Armoury Crate under Performance > GPU Settings > Memory Assigned "
            "to GPU. TravelReady does not change VRAM allocation."),
    _device("cpu_boost", "BIOS / Armoury Crate setting. Not changed automatically."),
    _device("operating_mode", "Armoury Crate operating mode. Not changed automatically."),
    _device("fan_profile", "Armoury Crate fan curve. Not changed automatically."),
    _device("refresh_rate_hz", "Windows display setting. Not changed automatically."),
    _device("windows_power_plan", "Windows setting. Not changed automatically."),
    _device("adrenalin_profile", "AMD driver profile. Not changed automatically."),

    # -- informational ----------------------------------------------------
    _info("average_fps", "The author's measured frame rate. Not a setting."),
    _info("performance_note", "The author's commentary. Not a setting."),
    _info("playability", "The author's subjective verdict. Not a setting."),
)}


#: Site label -> canonical key. Ordered longest-first at match time so
#: "texture quality" wins over "quality". Labels are matched loosely
#: (case, punctuation and spacing are ignored).
LABEL_ALIASES: Dict[str, str] = {
    "resolution": "resolution",
    "display resolution": "resolution",
    "render resolution": "resolution",
    "resolution scale": "resolution",
    "display mode": "display_mode",
    "window mode": "display_mode",
    "fullscreen": "display_mode",
    "screen mode": "display_mode",
    "vsync": "vsync",
    "v sync": "vsync",
    "vertical sync": "vsync",
    "frame rate limit": "frame_rate_limit",
    "framerate limit": "frame_rate_limit",
    "frame limit": "frame_rate_limit",
    "fps limit": "frame_rate_limit",
    "max fps": "frame_rate_limit",
    "frame rate cap": "frame_rate_limit",
    "frame rate target": "frame_rate_limit",
    "graphics quality": "graphics_quality",
    "graphics preset": "graphics_quality",
    "overall quality": "graphics_quality",
    "quality preset": "graphics_quality",
    "preset": "graphics_quality",
    "texture quality": "texture_quality",
    "textures": "texture_quality",
    "texture detail": "texture_quality",
    "texture filtering": "texture_quality",
    "shadow quality": "shadow_quality",
    "shadows": "shadow_quality",
    "shadow detail": "shadow_quality",
    "effects quality": "effects_quality",
    "effects": "effects_quality",
    "particle quality": "effects_quality",
    "view distance": "view_distance",
    "draw distance": "view_distance",
    "level of detail": "view_distance",
    "post processing": "post_processing",
    "post process quality": "post_processing",
    "anti aliasing": "anti_aliasing",
    "antialiasing": "anti_aliasing",
    "aa": "anti_aliasing",
    "ambient occlusion": "ambient_occlusion",
    "ssao": "ambient_occlusion",
    "reflections": "reflection_quality",
    "reflection quality": "reflection_quality",
    "screen space reflections": "screen_space_reflections",
    "ssr": "screen_space_reflections",
    "motion blur": "motion_blur",
    "upscaling": "upscaling_mode",
    "upscaler": "upscaling_mode",
    "upscaling mode": "upscaling_mode",
    "super resolution": "upscaling_mode",
    "image scaling": "upscaling_mode",
    "fsr": "fsr_mode",
    "amd fsr": "fsr_mode",
    "fsr mode": "fsr_mode",
    "fsr quality": "fsr_mode",
    "fidelityfx super resolution": "fsr_mode",
    "amd fidelityfx super resolution": "fsr_mode",
    "fsr sharpness": "fsr_sharpness",
    "sharpness": "fsr_sharpness",
    "frame generation": "frame_generation",
    "frame gen": "frame_generation",
    "amd fluid motion frames": "frame_generation",
    "afmf": "frame_generation",
    "ray tracing": "ray_tracing",
    "rt": "ray_tracing",
    "hair strands": "hair_strands",
    "hair quality": "hair_strands",
    "strand hair": "hair_strands",
    "volumetric quality": "volumetric_quality",
    "volumetric fog": "volumetric_quality",
    "water quality": "water_quality",
    "crowd density": "crowd_density",
    # device
    "tdp": "tdp_watts",
    "power": "tdp_watts",
    "watts": "tdp_watts",
    "power mode": "tdp_watts",
    "tdp watts": "tdp_watts",
    "vram": "vram_allocation_gb",
    "vram allocation": "vram_allocation_gb",
    "gpu memory": "vram_allocation_gb",
    "memory assigned to gpu": "vram_allocation_gb",
    "shared memory": "vram_allocation_gb",
    "cpu boost": "cpu_boost",
    "operating mode": "operating_mode",
    "manual mode": "operating_mode",
    "turbo mode": "operating_mode",
    "fan profile": "fan_profile",
    "fan curve": "fan_profile",
    "refresh rate": "refresh_rate_hz",
    # informational
    "average fps": "average_fps",
    "avg fps": "average_fps",
    "fps": "average_fps",
    "performance": "performance_note",
    "playability": "playability",
}

_SORTED_ALIASES = sorted(LABEL_ALIASES.items(), key=lambda kv: -len(kv[0]))


def _flatten(label: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(label or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def canonical_key(label: str) -> str:
    """Canonical key for a site label, or ``''`` when unrecognised.

    An exact flattened match is tried first; only then a containment match,
    longest alias first, so ``"Texture Quality"`` never resolves through the
    shorter ``"quality"``.
    """
    flat = _flatten(label)
    if not flat:
        return ""
    if flat in LABEL_ALIASES:
        return LABEL_ALIASES[flat]
    for alias, key in _SORTED_ALIASES:
        if len(alias) < 3:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", flat):
            return key
    return ""


def capability_for(canonical: str) -> Optional[Capability]:
    return CAPABILITIES.get(str(canonical or ""))


def describe_matrix(keys: Optional[Sequence[str]] = None) -> str:
    """The capability matrix as a table, for ``travelready settings status``."""
    rows = [CAPABILITIES[k] for k in (keys or sorted(CAPABILITIES)) if k in CAPABILITIES]
    lines = [f"{'Setting':<32} {'Detect':<8} {'Apply':<8} {'Verify':<8} Scope",
             "-" * 78]
    for cap in rows:
        lines.append(f"{cap.key:<32} {'YES' if cap.detect else 'NO':<8} "
                     f"{'YES' if cap.apply else 'NO':<8} "
                     f"{'YES' if cap.verify else 'NO':<8} {cap.scope}")
    lines.append(f"{'unknown game setting':<32} {'NO':<8} {'NO':<8} {'NO':<8} informational")
    return "\n".join(lines)
