"""environment.py — what this machine can actually check.

TravelReady makes claims about a user's games. Every one of those claims rests
on some capability of the machine it is running on: reading the process table
needs Windows, reading a game's config needs the game installed, fetching a
recommendation needs the network. When a capability is missing, the honest
answer is "cannot check", and that is a different answer from "no" — conflating
the two is the mistake that produced both the EA timeout and the "no profile"
label on an unsynced cache.

So every check declares what it requires, and the application knows at runtime
which requirements this environment satisfies. A check whose requirement is
unmet is reported as `UNKNOWN` with the reason, never as a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

# --------------------------------------------------------------------------
# requirement vocabulary
# --------------------------------------------------------------------------

PURE_LOGIC = "PURE_LOGIC"
WINDOWS_REQUIRED = "WINDOWS_REQUIRED"
ROG_ALLY_REQUIRED = "ROG_ALLY_REQUIRED"
LIVE_NETWORK_REQUIRED = "LIVE_NETWORK_REQUIRED"
LIVE_GAME_REQUIRED = "LIVE_GAME_REQUIRED"
LAUNCHER_REQUIRED = "LAUNCHER_REQUIRED"

REQUIREMENTS = (PURE_LOGIC, WINDOWS_REQUIRED, ROG_ALLY_REQUIRED,
                LIVE_NETWORK_REQUIRED, LIVE_GAME_REQUIRED, LAUNCHER_REQUIRED)

REQUIREMENT_LABELS = {
    PURE_LOGIC: "always available",
    WINDOWS_REQUIRED: "needs Windows",
    ROG_ALLY_REQUIRED: "needs a ROG Ally",
    LIVE_NETWORK_REQUIRED: "needs internet",
    LIVE_GAME_REQUIRED: "needs the game installed",
    LAUNCHER_REQUIRED: "needs the launcher installed",
}

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0


@dataclass(frozen=True)
class Capability:
    """One environment capability, and whether it is present."""

    name: str
    available: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.available


@dataclass
class Environment:
    """What this machine can do. Probed once, cached, and injectable for tests."""

    windows: Capability
    powershell: Capability
    process_table: Capability
    shell_execute: Capability
    registry: Capability
    network: Capability
    rog_ally: Capability
    windows_version: str = ""
    device_model: str = ""
    notes: List[str] = field(default_factory=list)

    # -- requirement satisfaction -----------------------------------------

    def satisfies(self, requirement: str) -> bool:
        """Can a check with this requirement run here?"""
        if requirement == PURE_LOGIC:
            return True
        if requirement == WINDOWS_REQUIRED:
            return bool(self.windows)
        if requirement == ROG_ALLY_REQUIRED:
            return bool(self.rog_ally)
        if requirement == LIVE_NETWORK_REQUIRED:
            return bool(self.network)
        # LIVE_GAME_REQUIRED and LAUNCHER_REQUIRED are per-game, resolved by
        # the caller against that game's own state, not by the environment.
        return bool(self.windows)

    def why_not(self, requirement: str) -> str:
        """Why a requirement is unmet here, for the user-facing reason string."""
        if self.satisfies(requirement):
            return ""
        return {
            WINDOWS_REQUIRED: "this is not Windows",
            ROG_ALLY_REQUIRED: "this is not a ROG Ally",
            LIVE_NETWORK_REQUIRED: "no internet connection",
        }.get(requirement, REQUIREMENT_LABELS.get(requirement, requirement))

    def summary(self) -> Dict[str, Capability]:
        return {
            "Windows": self.windows,
            "PowerShell": self.powershell,
            "Process table": self.process_table,
            "ShellExecute": self.shell_execute,
            "Registry": self.registry,
            "Internet": self.network,
            "ROG Ally": self.rog_ally,
        }


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def _run(cmd: Sequence[str], timeout: int = 15) -> str:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True,
                              timeout=timeout, creationflags=_NO_WINDOW)
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _probe_windows() -> Capability:
    if not IS_WINDOWS:
        return Capability("windows", False,
                          f"running on {sys.platform}; Windows-only features are unavailable")
    return Capability("windows", True, f"{os.name} {sys.getwindowsversion().major}"
                      if hasattr(sys, "getwindowsversion") else "windows")


def _probe_powershell() -> Capability:
    if not IS_WINDOWS:
        return Capability("powershell", False, "not Windows")
    exe = os.environ.get("TRAVELREADY_POWERSHELL") or "powershell"
    if shutil.which(exe) is None:
        return Capability("powershell", False,
                          f"'{exe}' not found on PATH; Xbox discovery and process "
                          f"paths will be unavailable")
    return Capability("powershell", True, exe)


def _probe_process_table() -> Capability:
    if not IS_WINDOWS:
        return Capability("process_table", False, "not Windows")
    from .processes import WindowsProcessTable

    rows = WindowsProcessTable().snapshot()
    if not rows:
        return Capability("process_table", False,
                          "neither Win32_Process nor tasklist returned anything; "
                          "game verification will not work")
    return Capability("process_table", True, f"{len(rows)} processes visible")


def _probe_shell_execute() -> Capability:
    if not IS_WINDOWS:
        return Capability("shell_execute", False, "not Windows")
    return Capability("shell_execute", hasattr(os, "startfile"),
                      "os.startfile available" if hasattr(os, "startfile")
                      else "os.startfile missing; URI and shell launches cannot run")


def _probe_registry() -> Capability:
    if not IS_WINDOWS:
        return Capability("registry", False, "not Windows")
    try:
        import winreg  # noqa: F401
    except ImportError:
        return Capability("registry", False, "winreg unavailable")
    return Capability("registry", True, "winreg available")


def probe_network(host: str = "rogallylife.com", *, opener=None,
                  timeout: int = 8) -> Capability:
    """Is the recommendation source reachable?

    Deliberately probes the host TravelReady actually needs rather than a
    generic connectivity check: a captive portal or a policy that allows
    everything except this host both matter, and only this tells us.
    """
    url = f"https://{host}/robots.txt"
    try:
        if opener is None:
            from urllib.request import Request, urlopen

            request = Request(url, headers={"User-Agent": "TravelReady/0.4"})
            opener = lambda: urlopen(request, timeout=timeout)  # noqa: E731
            with opener():
                return Capability("network", True, f"{host} reachable")
        with opener(url, timeout=timeout):
            return Capability("network", True, f"{host} reachable")
    except Exception as exc:                 # any failure means "cannot check"
        return Capability("network", False, f"{host} unreachable: {exc}")


def _probe_rog_ally() -> Capability:
    """Is this a ROG Ally? Read-only WMI lookup of the system model."""
    if not IS_WINDOWS:
        return Capability("rog_ally", False, "not Windows")
    model = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                  "(Get-CimInstance Win32_ComputerSystem).Model"]).strip()
    if not model:
        return Capability("rog_ally", False, "could not read the system model")
    low = model.lower()
    known = ("rc71l", "rc72la", "rc73", "rog ally")
    if any(token in low for token in known):
        return Capability("rog_ally", True, model)
    return Capability("rog_ally", False,
                      f"system model is '{model}', not a ROG Ally; device-specific "
                      f"recommendations may not apply")


def detect(*, check_network: bool = True, network_opener=None) -> Environment:
    """Probe this machine. Cheap except for the optional network check."""
    windows = _probe_windows()
    env = Environment(
        windows=windows,
        powershell=_probe_powershell(),
        process_table=_probe_process_table(),
        shell_execute=_probe_shell_execute(),
        registry=_probe_registry(),
        network=(probe_network(opener=network_opener) if check_network
                 else Capability("network", False, "not checked")),
        rog_ally=_probe_rog_ally(),
    )
    if not windows:
        env.notes.append(
            "Running off Windows: discovery, launching and verification are "
            "unavailable. Pure-logic features (matching, profiles, planning, "
            "reporting) work normally.")
    return env


_CACHED: Optional[Environment] = None


def current(*, refresh: bool = False, **kwargs) -> Environment:
    """The probed environment, cached for the process lifetime."""
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = detect(**kwargs)
    return _CACHED


def offline_environment() -> Environment:
    """An environment with nothing available — the fixture for pure-logic tests."""
    unavailable = lambda name: Capability(name, False, "test environment")  # noqa: E731
    return Environment(
        windows=unavailable("windows"), powershell=unavailable("powershell"),
        process_table=unavailable("process_table"),
        shell_execute=unavailable("shell_execute"),
        registry=unavailable("registry"), network=unavailable("network"),
        rog_ally=unavailable("rog_ally"),
    )
