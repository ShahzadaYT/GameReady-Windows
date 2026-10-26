"""processes.py — read-only process inspection and safe termination.

Why this module exists
----------------------
The previous build detected a running game by **two** signals: an exact
``expected_process`` image name, or the PID it started (``exe`` launches only).
Both are unavailable for an EA URI launch with no known executable, which is
exactly the shape of every EA entry in the supplied library — so detection
could never succeed. See ``docs/ENGINEERING_ASSESSMENT.md`` §2.

Pulling process access behind :class:`ProcessTable` does three things:

1. restores install-directory detection, deleted from the current build
   (``find_pids_by_path_prefix``);
2. lets the detection state machine be unit-tested off Windows against
   :class:`FakeProcessTable`, so the regressions cannot silently return;
3. keeps every Windows-specific command in one auditable place.

SAFETY CONTRACT
---------------
* Everything here is **read-only** except :meth:`ProcessTable.terminate`.
* Nothing injects code, opens process memory, reads or writes another
  process's address space, hooks, or attaches a debugger.
* ``tasklist`` and ``Get-CimInstance Win32_Process`` are ordinary read-only
  queries — the same information Task Manager shows.
* :meth:`terminate` refuses to act on any PID in :data:`PROTECTED_PROCESSES`,
  which covers anti-cheat, DRM, launcher authentication and OS components, and
  is enforced regardless of caller. There is no override.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
_TIMEOUT = 30

#: Image names TravelReady will never terminate, under any circumstance.
#: Anti-cheat, anti-tamper, DRM and launcher authentication components.
PROTECTED_PROCESSES = frozenset({
    # anti-cheat
    "easyanticheat.exe", "easyanticheat_eos.exe", "eac_launcher.exe",
    "beservice.exe", "bedaisy.sys", "battleye.exe", "be_service.exe",
    "vgc.exe", "vgtray.exe", "vgk.sys", "vanguard.exe",
    "ricochet.exe", "xigncode.exe", "xhunter1.sys", "gameguard.des",
    "nprotect.exe", "ahnhids.exe", "mhyprot.exe", "sgguard.exe",
    "faceit.exe", "esea.exe", "punkbuster.exe", "pnkbstra.exe", "pnkbstrb.exe",
    # DRM / anti-tamper / licensing
    "denuvo.exe", "denuvoservice.exe", "steamservice.exe",
    "uplayinstallhelper.exe", "eabackgroundservice.exe",
    "gameoverlayui.exe", "securomgr.exe",
    # OS / security
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "system", "svchost.exe", "msmpeng.exe", "securityhealthservice.exe",
    "gamingservices.exe", "gamingservicesnet.exe",
})

#: Launcher infrastructure that must never be adopted as "the game".
LAUNCHER_PROCESSES = frozenset({
    "ealink.exe", "origin.exe", "eadesktop.exe", "easteamproxy.exe",
    "eabackgroundservice.exe", "eacrashreporter.exe", "originwebhelperservice.exe",
    "upc.exe", "uplay.exe", "ubisoftconnect.exe", "ubisoftgamelauncher.exe",
    "upcgamelauncher.exe", "uplaywebcore.exe",
    "steam.exe", "steamwebhelper.exe", "steamerrorreporter.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe", "eoshelper.exe",
    "galaxyclient.exe", "galaxyclienthelper.exe", "galaxycommunication.exe",
    "battle.net.exe", "blizzard.exe", "battle.net helper.exe", "agent.exe",
    "gamebar.exe", "gamebarftserver.exe", "xboxpcapp.exe", "xbox.exe",
    "gamelaunchhelper.exe",
    "explorer.exe", "cmd.exe", "conhost.exe", "powershell.exe", "rundll32.exe",
    "wmiprvse.exe", "backgroundtaskhost.exe", "applicationframehost.exe",
})


@dataclass(frozen=True)
class ProcInfo:
    """One row of the process table."""

    pid: int
    name: str = ""            # image name, e.g. "bf3.exe"
    exe_path: str = ""        # full path, may be "" when not readable
    ppid: int = 0

    @property
    def key(self) -> str:
        return self.name.lower()


from .textnorm import leaf as _leaf, norm_dir as _norm_dir  # noqa: E402


class ProcessTable:
    """Read-only view of running processes, plus guarded termination.

    Subclasses provide :meth:`snapshot`; everything else is derived, so the
    Windows implementation and the test double share identical semantics.
    """

    # -- to implement ------------------------------------------------------

    def snapshot(self) -> List[ProcInfo]:
        raise NotImplementedError

    def _kill(self, pid: int, force: bool) -> bool:
        raise NotImplementedError

    # -- derived, read-only ------------------------------------------------

    def pids_by_name(self, name: str) -> List[int]:
        """PIDs whose image name matches ``name`` (case-insensitive)."""
        leaf = _leaf(name).lower()
        if not leaf:
            return []
        if not leaf.endswith(".exe"):
            leaf += ".exe"
        return sorted(p.pid for p in self.snapshot() if p.key == leaf)

    def pids_by_names(self, names: Iterable[str]) -> List[int]:
        out: set[int] = set()
        for n in names:
            out.update(self.pids_by_name(n))
        return sorted(out)

    def pids_under(self, directory: str) -> List[int]:
        """PIDs whose executable lives under ``directory``.

        Catches game helper processes and shipping binaries whose image name
        differs from the launch executable — the capability the current build
        lost. Callers must pass a directory vetted by
        :func:`travelready.launch_tester.safe_install_dir`.
        """
        root = _norm_dir(directory)
        if not root:
            return []
        out = []
        for p in self.snapshot():
            if not p.exe_path:
                continue
            if p.exe_path.replace("/", "\\").lower().startswith(root):
                out.append(p.pid)
        return sorted(out)

    def image_name_of(self, pid: int) -> str:
        for p in self.snapshot():
            if p.pid == pid:
                return p.key
        return ""

    def exe_path_of(self, pid: int) -> str:
        for p in self.snapshot():
            if p.pid == pid:
                return p.exe_path
        return ""

    def alive(self, pid: int) -> bool:
        return any(p.pid == pid for p in self.snapshot())

    def children(self, pid: int, _depth: int = 0) -> List[int]:
        """Direct and descendant PIDs of ``pid`` (best effort, cycle-safe)."""
        if _depth > 6:
            return []
        rows = self.snapshot()
        direct = [p.pid for p in rows if p.ppid == pid and p.pid != pid]
        out = list(direct)
        for child in direct:
            out.extend(self.children(child, _depth + 1))
        # stable, de-duplicated
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def is_launcher_infra(self, pid: int) -> bool:
        """True if the PID is launcher infrastructure, never the game itself."""
        return self.image_name_of(pid) in LAUNCHER_PROCESSES

    def is_protected(self, pid: int) -> bool:
        """True if the PID is anti-cheat, DRM or an OS/security component."""
        return self.image_name_of(pid) in PROTECTED_PROCESSES

    # -- the one mutating operation ---------------------------------------

    def terminate(self, pid: int, force: bool = False) -> tuple[bool, str]:
        """Terminate ``pid``, refusing protected processes.

        The refusal is enforced here rather than at the call site so that no
        caller — present or future — can talk its way past it. There is no
        force-anyway parameter.
        """
        name = self.image_name_of(pid)
        if not name:
            return True, f"PID {pid} already gone"
        if name in PROTECTED_PROCESSES:
            return False, (
                f"REFUSED: PID {pid} ({name}) is a protected anti-cheat, DRM or "
                f"system process. TravelReady never terminates these."
            )
        ok = self._kill(pid, force)
        return ok, (f"PID {pid} ({name}) "
                    f"{'force-terminated' if force else 'asked to close'}"
                    if ok else f"PID {pid} ({name}) could not be terminated")

    def terminate_tree(self, pid: int, graceful_wait: float = 3.0,
                       force_wait: float = 3.0,
                       sleep=time.sleep) -> tuple[bool, List[str]]:
        """Close ``pid`` and its descendants: graceful first, force if needed.

        Only ever touches the tree it was given — a PID TravelReady started or
        positively identified as the game — never arbitrary processes.
        """
        log: List[str] = []
        if not self.alive(pid):
            return True, [f"PID {pid} was not running"]
        targets = [pid] + self.children(pid)
        protected = [p for p in targets if self.is_protected(p)]
        for p in protected:
            log.append(f"REFUSED to touch protected process {p} ({self.image_name_of(p)})")
        targets = [p for p in targets if p not in set(protected)]

        for p in reversed(targets):                      # children first
            ok, msg = self.terminate(p, force=False)
            log.append(msg)
        deadline = time.time() + graceful_wait
        while time.time() < deadline and any(self.alive(p) for p in targets):
            sleep(0.25)

        survivors = [p for p in targets if self.alive(p)]
        if survivors:
            for p in reversed(survivors):
                ok, msg = self.terminate(p, force=True)
                log.append(msg)
            deadline = time.time() + force_wait
            while time.time() < deadline and any(self.alive(p) for p in survivors):
                sleep(0.25)

        remaining = [p for p in targets if self.alive(p)]
        if remaining:
            log.append(f"Still running after cleanup: {sorted(remaining)}")
        return (not remaining), log


class WindowsProcessTable(ProcessTable):
    """Real process table, read via ``tasklist`` and ``Win32_Process``.

    The snapshot is cached briefly: detection polls once a second and a CIM
    query is not free, but a stale view would break respawn detection, so the
    TTL is deliberately shorter than the poll interval.
    """

    def __init__(self, cache_ttl: float = 0.75) -> None:
        self._cache_ttl = cache_ttl
        self._cache: List[ProcInfo] = []
        self._cache_at = 0.0

    # -- command helpers ---------------------------------------------------

    @staticmethod
    def _run(cmd: Sequence[str], timeout: int = _TIMEOUT) -> str:
        try:
            proc = subprocess.run(
                list(cmd), capture_output=True, text=True,
                timeout=timeout, creationflags=_NO_WINDOW,
            )
            return proc.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""

    @classmethod
    def _powershell(cls, script: str, timeout: int = _TIMEOUT) -> str:
        exe = os.environ.get("TRAVELREADY_POWERSHELL") or "powershell"
        return cls._run([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout)

    # -- snapshot ----------------------------------------------------------

    def invalidate(self) -> None:
        self._cache_at = 0.0

    def snapshot(self) -> List[ProcInfo]:
        now = time.time()
        if self._cache and (now - self._cache_at) < self._cache_ttl:
            return self._cache
        rows = self._snapshot_cim()
        if not rows:
            rows = self._snapshot_tasklist()
        self._cache, self._cache_at = rows, now
        return rows

    def _snapshot_cim(self) -> List[ProcInfo]:
        """Full table including executable paths and parent PIDs (read-only)."""
        if not IS_WINDOWS:
            return []
        out = self._powershell(
            "Get-CimInstance Win32_Process | ForEach-Object { "
            "\"$($_.ProcessId)|$($_.Name)|$($_.ParentProcessId)|$($_.ExecutablePath)\" }"
        )
        rows: List[ProcInfo] = []
        for line in out.splitlines():
            parts = line.strip().split("|", 3)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            rows.append(ProcInfo(
                pid=int(parts[0]),
                name=parts[1].strip(),
                ppid=int(parts[2]) if parts[2].strip().isdigit() else 0,
                exe_path=(parts[3].strip() if len(parts) > 3 else ""),
            ))
        return rows

    def _snapshot_tasklist(self) -> List[ProcInfo]:
        """Fallback when CIM is unavailable: names and PIDs only."""
        if not IS_WINDOWS:
            return []
        out = self._run(["tasklist", "/FO", "CSV", "/NH"])
        rows: List[ProcInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith('"'):
                continue
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) < 2 or not cols[1].strip().isdigit():
                continue
            rows.append(ProcInfo(pid=int(cols[1].strip()), name=cols[0].strip()))
        return rows

    def _kill(self, pid: int, force: bool) -> bool:
        if not IS_WINDOWS:
            return False
        cmd = ["taskkill", "/PID", str(pid)]
        if force:
            cmd.append("/F")
        self._run(cmd, timeout=15)
        self.invalidate()
        return not self.alive(pid)


class FakeProcessTable(ProcessTable):
    """Scriptable process table for tests.

    Supports scheduled appearances and exits so the detection state machine can
    be driven deterministically: EA hand-off, late spawn, respawn, immediate
    exit, wrong-process adoption and cleanup all become ordinary unit tests.
    """

    def __init__(self, rows: Optional[Sequence[ProcInfo]] = None) -> None:
        self._rows: Dict[int, ProcInfo] = {p.pid: p for p in (rows or [])}
        self.clock = 0.0
        self._schedule: List[tuple[float, str, ProcInfo]] = []
        self.killed: List[tuple[int, bool]] = []
        self.refused: List[int] = []

    # -- scripting ---------------------------------------------------------

    def add_at(self, when: float, proc: ProcInfo) -> "FakeProcessTable":
        self._schedule.append((when, "add", proc))
        return self

    def remove_at(self, when: float, pid: int) -> "FakeProcessTable":
        self._schedule.append((when, "del", ProcInfo(pid=pid)))
        return self

    def advance(self, seconds: float) -> None:
        self.clock += seconds
        self._apply()

    def _apply(self) -> None:
        for when, action, proc in sorted(self._schedule, key=lambda x: x[0]):
            if when > self.clock:
                continue
            if action == "add":
                self._rows.setdefault(proc.pid, proc)
            elif proc.pid in self._rows:
                del self._rows[proc.pid]
        self._schedule = [s for s in self._schedule if s[0] > self.clock]

    # -- ProcessTable ------------------------------------------------------

    def snapshot(self) -> List[ProcInfo]:
        return list(self._rows.values())

    def _kill(self, pid: int, force: bool) -> bool:
        self.killed.append((pid, force))
        self._rows.pop(pid, None)
        return True

    def terminate(self, pid: int, force: bool = False):
        if self.is_protected(pid):
            self.refused.append(pid)
        return super().terminate(pid, force)


def default_table() -> ProcessTable:
    """The process table for this platform."""
    return WindowsProcessTable()
