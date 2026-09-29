"""launch_tester.py — core launch + process-monitoring engine.

Launches a game and verifies that it genuinely starts: the process must
actually appear AND survive the configured validation period. CreateProcess
succeeding alone is never reported as PASS.

What changed and why
--------------------
The previous engine had exactly two detection signals — an exact
``expected_process`` image name, or the PID it started (``exe`` launches only).
Every EA entry in the supplied library is a ``link2ea://`` URI with an empty
``exe_path`` and an empty ``expected_process``, so neither signal could ever
fire and the loop always ran to timeout. Raising the timeouts to 45 s / 90 s
made each guaranteed failure slower, not likelier to succeed. See
``docs/ENGINEERING_ASSESSMENT.md`` §2.

Detection is now multi-signal, in confidence order:

1. ``expected_process`` / ``alt_processes`` image names (new PIDs only);
2. descendants of the PID we started;
3. any new process running from the game's **install directory**
   (restores ``find_pids_by_path_prefix`` from the older build, which existed
   precisely to "catch game helper processes whose names differ from the
   expected process name");
4. the PID we started, for direct ``exe`` launches.

Launch and verification are also separated. A game can be launched
automatically while being honestly reported as manually-verified — the model
Xbox needs, and the one the older build already used.

SAFETY CONTRACT
---------------
* Never bypasses DRM or anti-cheat. It starts a game the way the launcher
  would and watches for its process.
* Cleanup only ever touches the tree it started or positively identified, and
  :mod:`travelready.processes` refuses protected processes outright.
* Anything that cannot be verified safely is reported MANUAL / UNKNOWN — never
  optimistically passed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, List, Optional, Sequence

from .library import (
    GameEntry, as_path, effective_launch_method, effective_launch_target,
    is_pseudo_path,
)
from .processes import IS_WINDOWS, ProcessTable, default_table

# --------------------------------------------------------------------------
# constants (values preserved from the shipped build unless noted)
# --------------------------------------------------------------------------

DEFAULT_LAUNCH_TIMEOUT = 60
DEFAULT_VALIDATION_TIME = 15
QUICK_VALIDATION_TIME = 5
STANDARD_EXIT_WATCH = 10
SMOKE_DEFAULT_DURATION = 10
SMOKE_PRESETS = [5, 10, 15, 30]
MAX_RESPAWNS = 5

EA_LAUNCHER_PHASE_TIMEOUT = 45
EA_GAME_PHASE_TIMEOUT = 90
EA_LAUNCH_TIMEOUT = EA_GAME_PHASE_TIMEOUT
URI_LAUNCH_TIMEOUT = 90
XBOX_LAUNCH_TIMEOUT = 90

POLL_INTERVAL = 1
GRACEFUL_WAIT = 3
FORCE_WAIT = 3

#: Extra window after the launch timeout in which a late-starting game is still
#: recognised — reported distinctly instead of as a plain timeout.
LATE_START_GRACE = 15

#: How long to wait for a replacement process after the tracked one exits.
#: Real games re-exec routinely (32-bit stub handing off to the 64-bit binary,
#: a launcher shim, an anti-cheat bootstrap starting the real executable), and
#: there is always a gap. Treating the first disappearance as "the game
#: exited" is what makes a healthy launch look like an immediate crash.
RESPAWN_GRACE = 5

_EA_LAUNCHER_PROCS = frozenset({
    "ealink.exe", "origin.exe", "eadesktop.exe",
    "easteamproxy.exe", "eabackgroundservice.exe",
})

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_MANUAL = "MANUAL"
STATUS_MANUAL_REQUIRED = "MANUAL_REQUIRED"
STATUS_ACCESS_DENIED = "ACCESS_DENIED"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_STOPPED = "STOPPED"
ALL_STATUSES = [
    STATUS_PASS, STATUS_FAIL, STATUS_TIMEOUT, STATUS_UNKNOWN, STATUS_MANUAL,
    STATUS_MANUAL_REQUIRED, STATUS_ACCESS_DENIED, STATUS_NOT_FOUND, STATUS_STOPPED,
]

STAGE_LAUNCHER_STARTED = "LAUNCHER_STARTED"
STAGE_GAME_PROCESS_STARTED = "GAME_PROCESS_STARTED"
STAGE_GAME_SURVIVED = "GAME_SURVIVED_10_SECONDS"
STAGE_GAME_FAILED = "GAME_FAILED_TO_START"
STAGE_TIMEOUT = "STAGE_TIMEOUT"
STAGE_MANUAL_REQUIRED = "STAGE_MANUAL_REQUIRED"

MODE_QUICK = "quick"
MODE_STANDARD = "standard"
MODE_MANUAL = "manual"
MODE_SMOKE = "smoke"
ALL_MODES = [MODE_QUICK, MODE_STANDARD, MODE_MANUAL, MODE_SMOKE]

METHOD_EXE = "exe"
METHOD_URI = "uri"
METHOD_SHELL = "shell"
METHOD_SHORTCUT = "shortcut"

VALID_EXE_SUFFIXES = (".exe", ".bat", ".cmd", ".ps1", ".com", ".lnk")

# How the game process was identified, recorded on every result so the UI can
# be honest about confidence instead of implying verification it did not do.
VERIFIED_BY_NAME = "expected_process"
VERIFIED_BY_CHILD = "child_process"
VERIFIED_BY_INSTALL_DIR = "install_dir"
VERIFIED_BY_LAUNCHED_PID = "launched_pid"
VERIFIED_MANUAL = "manual_confirmation"
VERIFIED_NONE = ""

#: Distinguishable outcomes. The brief requires these EA failure states to be
#: told apart rather than collapsed into a single TIMEOUT.
OUTCOME_OK = "GAME_VERIFIED"
OUTCOME_LAUNCHER_NOT_STARTED = "LAUNCHER_NOT_STARTED"
OUTCOME_LAUNCHER_ONLY = "LAUNCHER_STARTED_GAME_DID_NOT"
OUTCOME_NO_DETECTION_SIGNAL = "NO_DETECTION_SIGNAL_AVAILABLE"
OUTCOME_WRONG_PROCESS_NAME = "EXPECTED_PROCESS_NOT_SEEN_BUT_INSTALL_DIR_ACTIVE"
OUTCOME_LATE_START = "GAME_STARTED_AFTER_DETECTION_WINDOW"
OUTCOME_IMMEDIATE_EXIT = "GAME_EXITED_IMMEDIATELY"
OUTCOME_RESPAWNED = "GAME_RESPAWNED"
OUTCOME_MANUAL = "MANUAL_VERIFICATION"
OUTCOME_STOPPED = "STOPPED_BY_USER"
OUTCOME_LAUNCH_ERROR = "LAUNCH_ERROR"

_GENERIC_DIRS = {
    "program files", "program files (x86)", "desktop", "windows",
    "pictures", "documents", "downloads", "users", "public", "temp",
    "programdata", "appdata", "local", "roaming", "system32", "xboxgames",
    "steamapps", "common", "games", "c:", "",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TestResult:
    """The outcome of launching and verifying one game."""

    name: str = ""
    launcher: str = ""
    exe_path: str = ""
    launch_method: str = ""
    launch_target: str = ""
    status: str = STATUS_UNKNOWN
    outcome: str = ""
    launch_stage: str = ""
    verified_by: str = VERIFIED_NONE
    verification_possible: bool = True
    corrected_process: str = ""
    launcher_started: bool = False
    game_process_detected: bool = False
    game_survived: bool = False
    process_created: bool = False
    pid: Optional[int] = None
    launcher_pid: Optional[int] = None
    child_pids: List[int] = field(default_factory=list)
    respawns: int = 0
    time_to_launch: float = 0.0
    validation_duration: float = 0.0
    error_message: str = ""
    failure_hint: str = ""
    cleanup_performed: bool = False
    cleanup_ok: bool = True
    survivors: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# helpers preserved / restored from the older build
# --------------------------------------------------------------------------

def is_valid_executable(file_path: str) -> bool:
    """True when the file exists and looks launchable on Windows.

    On a non-Windows host a Windows-style path cannot be checked for
    existence, so only the suffix is validated. Launching is refused on
    non-Windows anyway (see :func:`_start_process`), so this cannot turn into
    an unchecked launch on a real device.
    """
    if not file_path or is_pseudo_path(file_path):
        return False
    wp = as_path(file_path)
    if not IS_WINDOWS and isinstance(wp, PureWindowsPath):
        return wp.suffix.lower() in VALID_EXE_SUFFIXES
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return False
    if p.suffix.lower() in VALID_EXE_SUFFIXES:
        return True
    try:
        with open(p, "rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


def safe_install_dir(entry_or_path) -> str:
    """The game's install directory, or ``''`` when it is not a safe scope.

    Restored from the older build. A directory that is too generic
    (``C:\\Program Files``, a drive root, ``C:\\XboxGames``) is rejected, so
    install-directory process matching can never sweep in unrelated software.
    """
    if isinstance(entry_or_path, GameEntry):
        candidate = entry_or_path.install_dir or entry_or_path.working_dir
        if not candidate and entry_or_path.exe_path and not is_pseudo_path(entry_or_path.exe_path):
            candidate = str(as_path(entry_or_path.exe_path).parent)
    else:
        candidate = str(entry_or_path or "")
        if candidate and not is_pseudo_path(candidate) and as_path(candidate).suffix:
            candidate = str(as_path(candidate).parent)
    if not candidate or is_pseudo_path(candidate):
        return ""
    p = as_path(candidate)
    if p.name.lower() in _GENERIC_DIRS or p == p.parent:
        return ""
    # Reject a shared root such as C:\, C:\Program Files or C:\XboxGames:
    # a game must sit at least one level inside one, or install-directory
    # matching could sweep in unrelated software.
    parts = [x for x in p.parts if x not in ("\\", "/")]
    if len(parts) <= 2:
        return ""
    return str(p)


def can_auto_close(entry: GameEntry) -> tuple[bool, str]:
    """Can a test reliably close what this entry launches?

    Preserved from the older build: *"Prepare-for-travel must NEVER launch what
    it cannot close, otherwise games pile up and stay running."* Closable means
    we create the process ourselves, know its process name, or can track new
    processes inside its install directory.
    """
    if effective_launch_method(entry) == METHOD_EXE and is_valid_executable(entry.exe_path):
        return True, "launched directly"
    if entry.process_names():
        return True, "known process name"
    if safe_install_dir(entry):
        return True, "trackable install directory"
    return False, "no way to identify or close the launched game"


def _map_creation_error(exc: BaseException) -> tuple[str, str]:
    """Translate a failed CreateProcess into a clear status (restored)."""
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, FileNotFoundError) or winerror == 2:
        return STATUS_NOT_FOUND, f"executable not found at launch time ({exc})"
    if isinstance(exc, PermissionError) or winerror == 5:
        return STATUS_ACCESS_DENIED, f"access denied starting process ({exc})"
    if winerror == 740:
        return STATUS_ACCESS_DENIED, "operation requires elevation (run as administrator)"
    if winerror == 193:
        return STATUS_FAIL, "not a valid Win32 application"
    return STATUS_UNKNOWN, f"process creation failed: {type(exc).__name__}: {exc}"


def build_command(entry: GameEntry):
    """Build the command/target for launching. Raises ValueError if invalid."""
    method = effective_launch_method(entry)
    target = effective_launch_target(entry)
    if method == METHOD_EXE:
        if not is_valid_executable(entry.exe_path):
            raise ValueError(f"Executable not found: {entry.exe_path}")
        return [entry.exe_path]
    if method == METHOD_SHORTCUT:
        if not target:
            raise ValueError("No shortcut path provided")
        return [target]
    if method in (METHOD_URI, METHOD_SHELL):
        if not target:
            raise ValueError("No launch target/URI provided")
        return [target]
    raise ValueError(f"Unknown or empty launch method for '{entry.name}'")


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------

def _start_process(entry: GameEntry, command) -> tuple[Optional[object], Optional[int], str, str]:
    """Start the launch command.

    Returns ``(popen_or_None, launcher_pid, status_or_empty, error_text)``.

    URI and ``shell:`` targets go through ``cmd /c start``, which is the
    documented way to hand a target to the shell. For ``shell:AppsFolder\\…``
    this is the supported Microsoft Store launch route: it asks the shell to
    activate the registered application. Nothing about the package is read,
    modified or bypassed.
    """
    method = effective_launch_method(entry)
    cwd = entry.working_dir if entry.working_dir and os.path.isdir(entry.working_dir) else None
    if not IS_WINDOWS:
        return None, None, STATUS_UNKNOWN, "Launching is only supported on Windows."
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if method in (METHOD_URI, METHOD_SHELL):
            proc = subprocess.Popen(
                ["cmd", "/c", "start", "", command[0]],
                cwd=cwd, creationflags=no_window,
            )
        else:
            proc = subprocess.Popen(command, cwd=cwd)
        return proc, proc.pid, "", ""
    except (PermissionError, FileNotFoundError, OSError) as exc:
        status, text = _map_creation_error(exc)
        return None, None, status, text


def _launch_timeout_for(entry: GameEntry, override: Optional[float] = None) -> float:
    """Resolve the launch timeout, applying launcher-specific extensions."""
    if override:
        return float(override)
    base = float(entry.launch_timeout or DEFAULT_LAUNCH_TIMEOUT)
    method = effective_launch_method(entry)
    if entry.launcher == "ea":
        return max(base, float(EA_GAME_PHASE_TIMEOUT))
    if entry.launcher == "xbox":
        return max(base, float(XBOX_LAUNCH_TIMEOUT))
    if method in (METHOD_URI, METHOD_SHELL):
        return max(base, float(URI_LAUNCH_TIMEOUT))
    return base


def _validation_time_for(entry: GameEntry, mode: str, smoke_duration: Optional[float],
                         override: Optional[float] = None) -> float:
    if override:
        return float(override)
    if mode == MODE_SMOKE:
        return float(smoke_duration or SMOKE_DEFAULT_DURATION)
    if mode == MODE_QUICK:
        return float(QUICK_VALIDATION_TIME)
    return float(entry.validation_time or DEFAULT_VALIDATION_TIME)


def _build_failure_hint(entry: GameEntry, result: TestResult) -> str:
    """Plain-English next step, chosen from the specific outcome."""
    if result.status == STATUS_ACCESS_DENIED:
        return "Access denied — running TravelReady as administrator may be required."
    if result.status == STATUS_NOT_FOUND:
        return "Executable not found at launch time. Re-scan or fix the path."
    if result.outcome == OUTCOME_NO_DETECTION_SIGNAL:
        if entry.launcher == "ea":
            return ("This EA entry has no known game executable, so the game process "
                    "cannot be identified. Re-scan so TravelReady can resolve the "
                    "install folder from EA, or set the game's process name in Edit Game.")
        return ("No expected process, install folder or launched PID is available for "
                "this entry, so nothing can be verified. Re-scan or set a process name.")
    if result.outcome == OUTCOME_LAUNCHER_NOT_STARTED and entry.launcher == "ea":
        return ("EA App did not start. Open EA App manually once and sign in, then retry.")
    if result.outcome == OUTCOME_LAUNCHER_ONLY:
        if entry.launcher == "ea":
            return ("EA App started but the game process never appeared. Check EA App for "
                    "a sign-in prompt, an update, or a EULA dialog waiting for input.")
        return ("The launcher started but the game did not — check for a login prompt, "
                "update or EULA in the launcher window.")
    if result.outcome == OUTCOME_WRONG_PROCESS_NAME:
        return ("A process started from the game's install folder, but not under the "
                "expected process name. The stored expected_process is probably wrong; "
                "TravelReady has recorded the name it actually saw.")
    if result.outcome == OUTCOME_LATE_START:
        return ("The game started, but only after the detection window closed. Increase "
                "the launch timeout for this game.")
    if result.outcome == OUTCOME_IMMEDIATE_EXIT:
        return ("The game process appeared but exited immediately. It may need an update, "
                "a sign-in, or a missing redistributable.")
    if result.status == STATUS_TIMEOUT:
        return "Timed out waiting for the game to start. Try increasing the launch timeout."
    if result.status == STATUS_MANUAL:
        return "Manual verification required."
    return ""


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

@dataclass
class DetectionPlan:
    """Everything the detector may watch for, resolved once before launch."""

    process_names: List[str] = field(default_factory=list)
    install_dir: str = ""
    track_launched_pid: bool = False
    is_ea: bool = False

    @property
    def has_any_signal(self) -> bool:
        return bool(self.process_names or self.install_dir or self.track_launched_pid)


def build_detection_plan(entry: GameEntry) -> DetectionPlan:
    """Resolve which detection signals are available for this entry.

    Deliberately computed before launching so a "nothing can be detected" entry
    is reported immediately and distinctly, instead of sleeping for 90 s and
    reporting a misleading TIMEOUT.
    """
    return DetectionPlan(
        process_names=entry.process_names(),
        install_dir=safe_install_dir(entry),
        track_launched_pid=effective_launch_method(entry) in (METHOD_EXE, METHOD_SHORTCUT),
        is_ea=(entry.launcher == "ea"),
    )


def _candidate_pids(table: ProcessTable, plan: DetectionPlan, pre_existing: set,
                    launcher_pid: Optional[int]) -> tuple[List[int], str]:
    """New PIDs that plausibly are the game, plus how they were identified."""
    def _clean(pids):
        return [p for p in pids
                if p not in pre_existing
                and p != launcher_pid
                and not table.is_launcher_infra(p)
                and not table.is_protected(p)]

    if plan.process_names:
        hits = _clean(table.pids_by_names(plan.process_names))
        if hits:
            return sorted(hits), VERIFIED_BY_NAME
    if launcher_pid is not None:
        hits = _clean(table.children(launcher_pid))
        if hits:
            return sorted(hits), VERIFIED_BY_CHILD
    if plan.install_dir:
        hits = _clean(table.pids_under(plan.install_dir))
        if hits:
            return sorted(hits), VERIFIED_BY_INSTALL_DIR
    if plan.track_launched_pid and launcher_pid is not None and table.alive(launcher_pid):
        if not table.is_launcher_infra(launcher_pid):
            return [launcher_pid], VERIFIED_BY_LAUNCHED_PID
    return [], VERIFIED_NONE


# --------------------------------------------------------------------------
# the test
# --------------------------------------------------------------------------

def run_test(
    entry: GameEntry,
    mode: str = MODE_STANDARD,
    stop_event=None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    manual_confirm: Optional[Callable[[GameEntry], bool]] = None,
    cleanup: bool = False,
    smoke_duration: Optional[float] = None,
    launch_timeout: Optional[float] = None,
    validation_time: Optional[float] = None,
    table: Optional[ProcessTable] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    starter: Optional[Callable] = None,
) -> TestResult:
    """Launch a game, monitor its process, and return a :class:`TestResult`.

    ``table``, ``clock``, ``sleep`` and ``starter`` are injected so the whole
    state machine is unit-testable off Windows.
    """
    table = table or default_table()
    starter = starter or _start_process
    mode = (mode or entry.mode or MODE_STANDARD).lower()
    if mode not in ALL_MODES:
        mode = MODE_STANDARD

    result = TestResult(
        name=entry.name, launcher=entry.launcher, exe_path=entry.exe_path,
        launch_method=effective_launch_method(entry),
        launch_target=effective_launch_target(entry),
    )
    notes = result.notes

    def log(msg: str) -> None:
        notes.append(msg)
        if on_progress:
            on_progress(msg)

    def stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    def finish(status: str, outcome: str, stage: str = "") -> TestResult:
        result.status = status
        result.outcome = outcome
        if stage:
            result.launch_stage = stage
        result.failure_hint = _build_failure_hint(entry, result)
        result.finished_at = _now_iso()
        return result

    plan = build_detection_plan(entry)
    lt = _launch_timeout_for(entry, launch_timeout)
    vt = _validation_time_for(entry, mode, smoke_duration, validation_time)
    result.validation_duration = vt

    log(f"Testing '{entry.name}' [{entry.launcher}] mode={mode} "
        f"method={result.launch_method} launch_timeout={lt:.0f}s validate={vt:.0f}s")

    # -- explicit manual mode -------------------------------------------
    if mode == MODE_MANUAL:
        result.launch_stage = STAGE_MANUAL_REQUIRED
        try:
            command = build_command(entry)
        except ValueError as exc:
            result.error_message = str(exc)
            return finish(STATUS_FAIL, OUTCOME_LAUNCH_ERROR, STAGE_GAME_FAILED)
        proc, pid, status, err = starter(entry, command)
        if status:
            result.error_message = err
            return finish(status, OUTCOME_LAUNCH_ERROR, STAGE_GAME_FAILED)
        result.process_created = True
        result.launcher_pid = pid
        result.launcher_started = True
        log(f"Opened '{entry.name}' (pid {pid}) for manual check.")
        result.verified_by = VERIFIED_MANUAL
        if manual_confirm is not None:
            confirmed = bool(manual_confirm(entry))
            result.game_process_detected = confirmed
            result.game_survived = confirmed
            return finish(STATUS_PASS if confirmed else STATUS_FAIL,
                          OUTCOME_MANUAL if confirmed else OUTCOME_LAUNCHER_ONLY)
        return finish(STATUS_MANUAL, OUTCOME_MANUAL)

    # -- no detection signal: say so now, do not burn the timeout --------
    if not plan.has_any_signal:
        log(f"No detection signal available for '{entry.name}': no expected process, "
            f"no install folder and the launch method does not yield a trackable PID.")
        result.verification_possible = False
        return finish(STATUS_MANUAL_REQUIRED, OUTCOME_NO_DETECTION_SIGNAL, STAGE_MANUAL_REQUIRED)

    if plan.process_names:
        log(f"Watching for game process: {', '.join(plan.process_names)}")
    if plan.install_dir:
        log(f"Watching install folder: {plan.install_dir}")

    # -- pre-existing snapshot ------------------------------------------
    pre_existing: set = set()
    if plan.process_names:
        pre_existing |= set(table.pids_by_names(plan.process_names))
    if plan.install_dir:
        pre_existing |= set(table.pids_under(plan.install_dir))
    if pre_existing:
        log(f"Ignoring {len(pre_existing)} pre-existing process(es) so an already-running "
            f"copy is never mistaken for this launch.")

    # -- launch ----------------------------------------------------------
    try:
        command = build_command(entry)
    except ValueError as exc:
        result.error_message = str(exc)
        status = STATUS_NOT_FOUND if "not found" in str(exc).lower() else STATUS_FAIL
        return finish(status, OUTCOME_LAUNCH_ERROR, STAGE_GAME_FAILED)

    t_start = clock()
    proc, launcher_pid, status, err = starter(entry, command)
    if status:
        result.error_message = err
        return finish(status, OUTCOME_LAUNCH_ERROR, STAGE_GAME_FAILED)
    result.process_created = True
    result.launcher_pid = launcher_pid
    result.launch_stage = STAGE_LAUNCHER_STARTED
    log(f"LAUNCHER_STARTED (pid {launcher_pid}).")
    if effective_launch_method(entry) == METHOD_EXE:
        result.launcher_started = True

    # -- detection loop ---------------------------------------------------
    deadline = t_start + lt
    ea_infra_seen = False
    ea_warned = False
    game_pid: Optional[int] = None
    verified_by = VERIFIED_NONE

    while clock() < deadline:
        if stopped():
            _cleanup(result, table, game_pid, launcher_pid, cleanup, log, sleep)
            return finish(STATUS_STOPPED, OUTCOME_STOPPED)

        if plan.is_ea and not ea_infra_seen:
            for lp in sorted(_EA_LAUNCHER_PROCS):
                pids = table.pids_by_name(lp)
                if pids:
                    ea_infra_seen = True
                    result.launcher_started = True
                    log(f"EA App process detected ({lp}, PID {pids[0]}) — "
                        f"waiting for the game process to spawn.")
                    break
            if not ea_infra_seen and not ea_warned and (clock() - t_start) >= EA_LAUNCHER_PHASE_TIMEOUT:
                ea_warned = True
                log(f"EA App infrastructure not detected within "
                    f"{EA_LAUNCHER_PHASE_TIMEOUT:.0f}s — it may still be starting.")

        pids, how = _candidate_pids(table, plan, pre_existing, launcher_pid)
        if pids:
            game_pid, verified_by = pids[0], how
            break
        sleep(POLL_INTERVAL)

    # -- late start grace --------------------------------------------------
    if game_pid is None:
        late_deadline = clock() + LATE_START_GRACE
        while clock() < late_deadline:
            if stopped():
                _cleanup(result, table, None, launcher_pid, cleanup, log, sleep)
                return finish(STATUS_STOPPED, OUTCOME_STOPPED)
            pids, how = _candidate_pids(table, plan, pre_existing, launcher_pid)
            if pids:
                game_pid, verified_by = pids[0], how
                log(f"Game process appeared after the detection window closed.")
                result.game_process_detected = True
                result.pid = game_pid
                result.verified_by = how
                result.time_to_launch = round(clock() - t_start, 2)
                _cleanup(result, table, game_pid, launcher_pid, cleanup, log, sleep)
                return finish(STATUS_TIMEOUT, OUTCOME_LATE_START, STAGE_TIMEOUT)
            sleep(POLL_INTERVAL)

    # -- nothing detected --------------------------------------------------
    if game_pid is None:
        result.game_process_detected = False
        if plan.is_ea and not ea_infra_seen:
            outcome = OUTCOME_LAUNCHER_NOT_STARTED
            log("EA App never appeared — the launch request did not reach EA App.")
        elif result.launcher_started:
            outcome = OUTCOME_LAUNCHER_ONLY
            log("Launcher started but no game process was detected.")
        else:
            outcome = OUTCOME_LAUNCHER_ONLY
            log(f"STAGE_TIMEOUT — game process "
                f"'{', '.join(plan.process_names) or '?'}' never appeared within {lt:.0f}s.")
        _cleanup(result, table, None, launcher_pid, cleanup, log, sleep)
        return finish(STATUS_TIMEOUT, outcome, STAGE_TIMEOUT)

    # -- game detected -----------------------------------------------------
    result.game_process_detected = True
    result.pid = game_pid
    result.verified_by = verified_by
    result.time_to_launch = round(clock() - t_start, 2)
    result.launch_stage = STAGE_GAME_PROCESS_STARTED
    actual_name = table.image_name_of(game_pid)
    log(f"GAME_PROCESS_STARTED — '{actual_name or '?'}' pid {game_pid} "
        f"(after {result.time_to_launch:.1f}s, identified by {verified_by}).")
    if on_pid:
        on_pid(game_pid)

    wrong_name = False
    if verified_by == VERIFIED_BY_INSTALL_DIR and plan.process_names and actual_name:
        if actual_name.lower() not in {n.lower() for n in plan.process_names}:
            wrong_name = True
            log(f"NOTE: expected {plan.process_names} but the game is running as "
                f"'{actual_name}'. The stored expected_process is wrong.")
            result.corrected_process = actual_name

    # -- validation window -------------------------------------------------
    validate_until = clock() + vt
    respawns = 0
    while clock() < validate_until:
        if stopped():
            _cleanup(result, table, game_pid, launcher_pid, cleanup, log, sleep)
            return finish(STATUS_STOPPED, OUTCOME_STOPPED)
        if not table.alive(game_pid):
            replacement = None
            if respawns < MAX_RESPAWNS:
                # Give the game a moment to come back before calling it dead.
                grace_until = min(clock() + RESPAWN_GRACE, validate_until + RESPAWN_GRACE)
                while clock() <= grace_until:
                    pids, how = _candidate_pids(table, plan, pre_existing, launcher_pid)
                    live = [p for p in pids if p != game_pid]
                    if live:
                        replacement = (live[0], how)
                        break
                    if stopped():
                        break
                    sleep(POLL_INTERVAL)
            if replacement is not None:
                respawns += 1
                game_pid, verified_by = replacement
                result.pid = game_pid
                result.respawns = respawns
                result.verified_by = verified_by
                actual_name = table.image_name_of(game_pid)
                log(f"Respawn detected (#{respawns}) — following new pid {game_pid} "
                    f"('{actual_name or '?'}').")
                validate_until = max(validate_until, clock() + vt)
                continue
            break
        sleep(POLL_INTERVAL)

    result.child_pids = table.children(game_pid) if table.alive(game_pid) else []

    if table.alive(game_pid):
        result.game_survived = True
        log(f"GAME_SURVIVED — '{actual_name or game_pid}' still running after {vt:.0f}s. PASS.")
        _cleanup(result, table, game_pid, launcher_pid, cleanup, log, sleep)
        outcome = OUTCOME_RESPAWNED if respawns else (
            OUTCOME_WRONG_PROCESS_NAME if wrong_name else OUTCOME_OK)
        return finish(STATUS_PASS, outcome, STAGE_GAME_SURVIVED)

    result.game_survived = False
    log("Game process exited before the validation window elapsed. FAIL.")
    _cleanup(result, table, None, launcher_pid, cleanup, log, sleep)
    return finish(STATUS_FAIL, OUTCOME_IMMEDIATE_EXIT, STAGE_GAME_FAILED)


def _cleanup(result: TestResult, table: ProcessTable, game_pid: Optional[int],
             launcher_pid: Optional[int], cleanup: bool,
             log: Callable[[str], None], sleep) -> None:
    """Close what this test started — and only that.

    Terminates the detected game tree and the PID we created. Protected
    processes are refused inside :mod:`travelready.processes`; survivors are
    reported rather than escalated against.
    """
    if not cleanup:
        return
    result.cleanup_performed = True
    ok_all = True
    survivors: List[int] = []
    for pid in [p for p in (game_pid, launcher_pid) if p is not None]:
        if not table.alive(pid):
            continue
        ok, lines = table.terminate_tree(pid, GRACEFUL_WAIT, FORCE_WAIT, sleep=sleep)
        for line in lines:
            log(line)
        if not ok:
            ok_all = False
            survivors.append(pid)
    result.cleanup_ok = ok_all
    result.survivors = survivors


def run_batch(entries: Sequence[GameEntry], **kwargs) -> List[TestResult]:
    """Run :func:`run_test` over several entries, in order."""
    on_result = kwargs.pop("on_result", None)
    stop_event = kwargs.get("stop_event")
    results = []
    for entry in entries:
        if stop_event is not None and stop_event.is_set():
            break
        r = run_test(entry, **kwargs)
        results.append(r)
        if on_result:
            on_result(r)
    return results


def summarize(results: Sequence[TestResult]) -> dict:
    """Counts by status, for the dashboard and the trip report."""
    out = {s: 0 for s in ALL_STATUSES}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    out["total"] = len(results)
    return out
