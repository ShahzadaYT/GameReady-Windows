"""safety.py — what TravelReady is allowed to change, and what it refuses.

ABSOLUTE RULE
-------------
TravelReady never modifies, disables, patches, bypasses, stops, replaces,
renames, deletes, injects into or hooks anti-cheat, DRM, anti-tamper, game
executables, game DLLs, signed game components, anti-cheat services or drivers,
DRM services, launcher authentication components, protected processes, kernel
drivers, BIOS, firmware, Secure Boot, TPM, Windows security, Memory Integrity,
or any game-integrity system.

The design goal is that targeting such a component is difficult or impossible
rather than merely discouraged, so the checks here are structured as a
whitelist with an unconditional blacklist on top:

* a file extension must be in :data:`ALLOWED_CONFIG_SUFFIXES` — every
  executable, library, driver, archive and signature format is refused, and
  anything unrecognised is refused too;
* a path must not touch any protected directory or contain any anti-cheat /
  DRM marker;
* a setting key must be one TravelReady understands
  (:data:`~travelready.optimiser.model.KNOWN_GAME_SETTINGS`);
* a device-wide setting is always MANUAL in this release;
* anything left over is RESEARCH_REQUIRED, never SAFE.

Classification returns one of SAFE / CAUTION / MANUAL / RESEARCH_REQUIRED /
BLOCKED with a human-readable reason. There are no percentages, no confidence
scores, and no "force apply" path: :func:`classify_target` returning BLOCKED is
final, and :mod:`travelready.optimiser.transaction` re-checks it immediately
before writing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..textnorm import as_path, path_components
from .model import (
    BLOCKED, CATEGORY_DEVICE, CATEGORY_GAME, CAUTION, KNOWN_DEVICE_SETTINGS,
    KNOWN_GAME_SETTINGS, MANUAL, RESEARCH_REQUIRED, SAFE, category_of, worst,
)

# --------------------------------------------------------------------------
# what a configuration file may be
# --------------------------------------------------------------------------

#: The only file types TravelReady will ever edit. Text-based, user-level,
#: well-understood game configuration. Note what is absent: every executable
#: and library format, every driver and archive format, every signature and
#: certificate format, and every save-game/binary blob.
ALLOWED_CONFIG_SUFFIXES = frozenset({".ini", ".cfg", ".conf", ".json", ".xml", ".yaml", ".yml"})

#: Refused outright, listed explicitly so the intent is auditable even though
#: the whitelist above already excludes them.
FORBIDDEN_SUFFIXES = frozenset({
    ".exe", ".dll", ".sys", ".drv", ".ocx", ".cpl", ".scr", ".com", ".msi",
    ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jar", ".so", ".dylib", ".bin",
    ".efi", ".sig", ".cat", ".crt", ".cer", ".pfx", ".p12", ".key", ".pem",
    ".lic", ".licence", ".license", ".token", ".dat", ".pak", ".sav", ".save",
    ".db", ".sqlite", ".lnk", ".reg", ".inf", ".msix", ".appx", ".zip", ".7z",
})

#: Distinctive, unambiguous markers. A substring match anywhere in the path is
#: enough, because none of these occurs innocently.
PROTECTED_SUBSTRINGS = (
    "easyanticheat", "easy anti-cheat", "easy-anti-cheat", "battleye", "battleeye",
    "riot vanguard", "ricochet", "punkbuster", "xigncode", "gameguard",
    "nprotect", "mhyprot", "anticheat", "anti-cheat", "anti_cheat",
    "anti-tamper", "antitamper", "denuvo", "securom", "starforce", "safedisc",
    "vmprotect", "themida", "steam_api", "steamworks",
    "secure boot", "secureboot", "memory integrity", "code integrity",
)

#: Short or ambiguous markers, matched only as whole words so that ordinary
#: titles are not caught. Without this, "eac" would block a game whose folder
#: is "Peaceful Nights" and "drm" would block "Drmanhattan".
PROTECTED_WORDS = (
    "eac", "vgk", "vgc", "vanguard", "drm", "faceit", "esea", "sgguard",
    "hshield", "licence", "license", "licensing", "activation", "entitlement",
    "ownership", "token", "authtoken", "credentials", "signature", "codesign",
)

#: Path *components* that are protected directories. Matched as whole path
#: segments, so Unreal Engine's ``…\Config\WindowsNoEditor\`` — the canonical
#: home of GameUserSettings.ini — is not mistaken for ``C:\Windows``.
PROTECTED_COMPONENTS = frozenset({
    "windows", "system32", "syswow64", "sysnative", "drivers", "driverstore",
    "windowsapps", "systemapps", "winsxs", "boot", "efi", "recovery",
    "system volume information", "windows defender", "microsoft defender",
    "easyanticheat", "easy anti-cheat", "battleye", "anticheat", "punkbuster",
    "riot vanguard", "vanguard", "denuvo", "securom", "$recycle.bin",
    "program files\\windowsapps",
})

#: Where user-level game configuration legitimately lives. A file outside all
#: of these is not automatically safe, even if its extension is allowed.
USER_CONFIG_ROOTS = (
    "\\appdata\\local\\", "\\appdata\\locallow\\", "\\appdata\\roaming\\",
    "\\saved games\\", "\\documents\\my games\\", "\\my games\\",
    "\\documents\\",
)

#: Kept for callers that want the full marker list for display.
PROTECTED_MARKERS = PROTECTED_SUBSTRINGS + PROTECTED_WORDS

_TRAVERSAL = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


@dataclass(frozen=True)
class Verdict:
    """A classification result. ``safety`` is authoritative; ``reason`` explains."""

    safety: str
    reason: str

    @property
    def blocked(self) -> bool:
        return self.safety == BLOCKED

    @property
    def auto(self) -> bool:
        return self.safety == SAFE


def _normalise(path: str) -> str:
    return str(path or "").replace("/", "\\").lower()


# --------------------------------------------------------------------------
# path classification
# --------------------------------------------------------------------------

def protected_marker_in(low_path: str) -> str:
    """The protected marker matched by ``low_path``, or ``''``.

    Distinctive markers match anywhere; short ones only as whole words.
    """
    for marker in PROTECTED_SUBSTRINGS:
        if marker in low_path:
            return marker
    words = set(re.split(r"[^a-z0-9]+", low_path))
    for marker in PROTECTED_WORDS:
        if marker in words:
            return marker
    return ""


def protected_component_in(path: str) -> str:
    """The protected directory component of ``path``, or ``''``.

    Whole-segment matching: ``C:\\Windows`` is protected, a folder merely named
    ``WindowsNoEditor`` is not.
    """
    parts = path_components(path)
    for i, part in enumerate(parts):
        if part in PROTECTED_COMPONENTS:
            return part
        if i + 1 < len(parts) and f"{part}\\{parts[i + 1]}" in PROTECTED_COMPONENTS:
            return f"{part}\\{parts[i + 1]}"
    return ""


def classify_path(path: str, *, must_exist: bool = False) -> Verdict:
    """Decide whether ``path`` may be edited at all.

    Fails closed: an empty, relative, unrecognised or unreadable path is never
    SAFE.
    """
    raw = str(path or "").strip()
    if not raw:
        return Verdict(BLOCKED, "No file path was given.")

    if _TRAVERSAL.search(raw):
        return Verdict(BLOCKED, "Path contains a '..' traversal segment.")

    low = _normalise(raw)

    if "\x00" in raw:
        return Verdict(BLOCKED, "Path contains a null byte.")

    wp = as_path(raw)
    if not wp.is_absolute():
        return Verdict(BLOCKED, "Only absolute paths may be edited.")

    # A UNC path names another machine. Writing a game's configuration to a
    # remote share is never something TravelReady should do, and an imported
    # library could otherwise point a settings write at an attacker's host.
    if low.startswith("\\\\") or low.startswith("//"):
        return Verdict(BLOCKED,
                       "Path is on a network share. TravelReady only edits "
                       "configuration on this machine.")

    hit = protected_marker_in(low)
    if hit:
        return Verdict(BLOCKED,
                       f"Path refers to a protected component ('{hit}'). "
                       f"TravelReady never modifies anti-cheat, DRM or "
                       f"game-integrity files.")

    component = protected_component_in(raw)
    if component:
        return Verdict(BLOCKED,
                       f"Path is inside a protected system or anti-cheat "
                       f"directory ('{component}').")

    suffix = wp.suffix.lower()
    if not suffix:
        return Verdict(BLOCKED, "File has no extension, so its format is unknown.")
    if suffix in FORBIDDEN_SUFFIXES:
        return Verdict(BLOCKED,
                       f"'{suffix}' is an executable, driver, archive, signature "
                       f"or binary format. TravelReady only edits text configuration.")
    if suffix not in ALLOWED_CONFIG_SUFFIXES:
        return Verdict(RESEARCH_REQUIRED,
                       f"'{suffix}' is not a configuration format TravelReady "
                       f"understands.")

    if must_exist and not os.path.isfile(raw):
        return Verdict(RESEARCH_REQUIRED, "File does not exist, so it cannot be inspected.")

    if not any(root in low for root in USER_CONFIG_ROOTS):
        return Verdict(CAUTION,
                       "File is outside the usual user configuration folders, so "
                       "it needs review before it can be changed automatically.")

    return Verdict(SAFE, "User-level text configuration in a known location.")


def looks_binary(sample: bytes) -> bool:
    """True when a file's opening bytes are not plain text.

    A config file that turns out to be binary is never edited, whatever its
    extension claims — an executable renamed to ``.ini`` gets no further than
    here.
    """
    if not sample:
        return False
    if sample[:2] == b"MZ" or sample[:4] in (b"\x7fELF", b"PK\x03\x04"):
        return True
    if b"\x00" in sample:
        return True
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) < 0.85


def classify_file_content(path: str, *, reader=None) -> Verdict:
    """Read the first bytes of ``path`` and refuse anything that is not text."""
    try:
        if reader is not None:
            sample = reader(path)
        else:
            with open(path, "rb") as fh:
                sample = fh.read(4096)
    except OSError as exc:
        return Verdict(RESEARCH_REQUIRED, f"File could not be read ({exc}).")
    if looks_binary(sample):
        return Verdict(BLOCKED,
                       "File content is binary, not text. TravelReady only edits "
                       "plain-text configuration.")
    return Verdict(SAFE, "File content is plain text.")


# --------------------------------------------------------------------------
# setting classification
# --------------------------------------------------------------------------

def classify_setting(key: str, value: str = "") -> Verdict:
    """Decide whether a *setting* may be changed automatically."""
    k = str(key or "").strip().lower()
    if not k:
        return Verdict(BLOCKED, "No setting key was given.")

    if any(marker in k for marker in PROTECTED_MARKERS):
        return Verdict(BLOCKED,
                       "Setting name refers to an anti-cheat, DRM or "
                       "authentication component.")

    if k in KNOWN_DEVICE_SETTINGS:
        return Verdict(MANUAL,
                       "Device-wide setting. TravelReady shows it but does not "
                       "change TDP, fan, VRAM, Armoury Crate, Adrenalin or "
                       "Windows power settings.")

    if k not in KNOWN_GAME_SETTINGS:
        return Verdict(RESEARCH_REQUIRED,
                       f"'{key}' is not a game setting TravelReady understands "
                       f"well enough to change.")

    text = str(value or "")
    if _TRAVERSAL.search(text) or "\x00" in text:
        return Verdict(BLOCKED, "Recommended value contains a path traversal or null byte.")
    if re.search(r"(?i)\.(exe|dll|sys|bat|cmd|ps1|msi)\b", text):
        return Verdict(BLOCKED, "Recommended value references an executable file.")
    if len(text) > 200:
        return Verdict(RESEARCH_REQUIRED, "Recommended value is implausibly long.")

    return Verdict(SAFE, "Known user-level game setting with a plain value.")


def classify_device(profile_device: str, target_device: str) -> Verdict:
    """A recommendation only applies to the device it was written for."""
    from .model import KNOWN_DEVICES

    d = str(profile_device or "").strip().lower()
    if not d:
        return Verdict(RESEARCH_REQUIRED, "Profile does not say which device it is for.")
    if d == target_device:
        return Verdict(SAFE, f"Profile targets {target_device}.")
    if d in KNOWN_DEVICES:
        return Verdict(MANUAL,
                       f"Profile targets {d}, not {target_device}. Similar hardware "
                       f"is not the same hardware, so this is not applied automatically.")
    return Verdict(RESEARCH_REQUIRED, f"Profile targets an unrecognised device ('{profile_device}').")


# --------------------------------------------------------------------------
# combined
# --------------------------------------------------------------------------

def classify_target(key: str, value: str, file_path: str, profile_device: str,
                    target_device: str, *, must_exist: bool = False,
                    check_content: bool = False) -> Verdict:
    """Classify a proposed change across every dimension, failing closed.

    The result is the least permissive of the individual verdicts, and the
    reason names the check that limited it — so the UI can always say exactly
    why something is not being changed.
    """
    verdicts = [
        classify_setting(key, value),
        classify_path(file_path, must_exist=must_exist),
        classify_device(profile_device, target_device),
    ]
    if check_content and not any(v.blocked for v in verdicts):
        verdicts.append(classify_file_content(file_path))

    combined = worst(*[v.safety for v in verdicts])
    limiting = next((v for v in verdicts if v.safety == combined), verdicts[0])
    return Verdict(combined, limiting.reason)


def assert_writable(file_path: str, key: str, value: str, profile_device: str,
                    target_device: str) -> None:
    """Raise unless this exact target may be written right now.

    Called by the transaction engine immediately before every write, after the
    path has been re-resolved. Re-checking here means a plan built minutes ago
    against a different file cannot be used to write somewhere else.
    """
    verdict = classify_target(key, value, file_path, profile_device, target_device,
                              must_exist=True, check_content=True)
    if verdict.safety != SAFE:
        raise PermissionError(
            f"Refusing to write {key} to {file_path}: [{verdict.safety}] {verdict.reason}"
        )
