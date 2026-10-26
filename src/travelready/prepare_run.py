"""prepare_run.py — Prepare-for-Travel as an orchestrated, resumable run.

Preparing a 149-game library takes a long time, and the previous
implementation was a straight loop: interrupt it at game 17 and games 1–16 were
thrown away with it. That is the wrong shape for the one workflow a user
actually runs before a flight.

A run is therefore a persisted object. Each game's outcome is written as soon
as it is known, so ``travelready prepare --resume`` continues from the last
game that finished rather than from the start. The run records why it stopped,
and re-running with ``--resume`` after a crash, a cancel or a closed lid picks
up where it left off.

Orchestration order per game, skipping what is already known:

    assess → (launch + verify, if needed) → re-assess → record

Settings are deliberately *not* applied here. Prepare-for-Travel readies games
for offline play; changing a game's configuration is a separate, explicitly
approved transaction, and burying it inside a 149-game batch would be exactly
the kind of silent modification the settings engine exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .apppaths import data_file
from .identity import GameIdentity, build_identities
from .library import GameEntry
from .preparation import (
    ACTION_REQUIRED, PASS, READY, READY_WITH_WARNINGS, PreparationReport,
    assess, summarise, summarise_by_launcher,
)
from .textnorm import atomic_write

RUN_FILE = "prepare_run.json"
RUN_VERSION = 1

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_INTERRUPTED = "interrupted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PrepareRun:
    """One Prepare-for-Travel run, persisted as it goes."""

    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    finished_at: str = ""
    status: str = STATUS_RUNNING
    planned: List[str] = field(default_factory=list)
    done: Dict[str, dict] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    stop_reason: str = ""
    version: int = RUN_VERSION

    # -- progress ----------------------------------------------------------

    @property
    def remaining(self) -> List[str]:
        return [k for k in self.planned if k not in self.done and k not in self.skipped]

    @property
    def completed(self) -> int:
        return len(self.done) + len(self.skipped)

    @property
    def total(self) -> int:
        return len(self.planned)

    @property
    def resumable(self) -> bool:
        return self.status != STATUS_COMPLETE and bool(self.remaining)

    def reports(self) -> List[PreparationReport]:
        return [PreparationReport.from_dict(d) for d in self.done.values()]

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version, "status": self.status,
            "started_at": self.started_at, "updated_at": self.updated_at,
            "finished_at": self.finished_at, "stop_reason": self.stop_reason,
            "planned": list(self.planned), "done": self.done,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PrepareRun":
        if not isinstance(data, dict):
            return cls()
        return cls(
            started_at=str(data.get("started_at", "")) or _now(),
            updated_at=str(data.get("updated_at", "")) or _now(),
            finished_at=str(data.get("finished_at", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            planned=[str(k) for k in data.get("planned", [])],
            done={str(k): v for k, v in (data.get("done") or {}).items()
                  if isinstance(v, dict)},
            skipped={str(k): str(v) for k, v in (data.get("skipped") or {}).items()},
            stop_reason=str(data.get("stop_reason", "")),
            version=int(data.get("version", 0) or 0),
        )

    def save(self, path: Optional[Path] = None) -> None:
        self.updated_at = _now()
        atomic_write(Path(path) if path else data_file(RUN_FILE),
                     json.dumps(self.to_dict(), indent=2))


def load_run(path: Optional[Path] = None) -> Optional[PrepareRun]:
    """The last run, or ``None`` when there is none or it is unreadable."""
    target = Path(path) if path else data_file(RUN_FILE)
    if not target.exists():
        return None
    try:
        run = PrepareRun.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if run.version != RUN_VERSION:
        # A run recorded by a different version is not resumed: its checks may
        # mean something else now. It is discarded, not misinterpreted.
        return None
    return run


def clear_run(path: Optional[Path] = None) -> None:
    target = Path(path) if path else data_file(RUN_FILE)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------

@dataclass
class PrepareOptions:
    """What a run is allowed to do."""

    launch: bool = True
    cleanup: bool = True
    smoke_duration: Optional[float] = None
    reverify_ready: bool = False
    stale_days: int = 30
    resume: bool = False


def plan_run(identities: Sequence[GameIdentity], options: PrepareOptions,
             *, env=None, resolver=None) -> tuple:
    """Decide what this run will touch. Returns ``(targets, skipped)``.

    A game is skipped, with the reason recorded, when TravelReady cannot close
    what it would start — the older build's rule, which exists so that a
    prepare run does not leave a pile of games running — or when it is already
    ready and re-verification was not asked for.
    """
    from .launch_tester import can_auto_close

    targets: List[GameIdentity] = []
    skipped: Dict[str, str] = {}
    for identity in identities:
        installation = identity.best_installation()
        if installation is None:
            skipped[identity.key] = "no installation found"
            continue
        report = assess(identity, env=env, resolver=resolver, installation=installation)
        if report.readiness in (READY, READY_WITH_WARNINGS) and not options.reverify_ready:
            first = report.check("first_launch")
            if first is not None and first.outcome == PASS:
                skipped[identity.key] = "already verified recently"
                continue
        ok, reason = can_auto_close(installation.entry)
        if options.launch and not ok:
            skipped[identity.key] = f"cannot be closed safely: {reason}"
            continue
        targets.append(identity)
    return targets, skipped


def run_preparation(identities: Sequence[GameIdentity], options: PrepareOptions,
                    *, env=None, resolver=None,
                    launcher: Optional[Callable] = None,
                    on_progress: Optional[Callable[[str], None]] = None,
                    stop_event=None,
                    run_path: Optional[Path] = None,
                    save: bool = True) -> PrepareRun:
    """Prepare every game, recording each outcome as it completes.

    ``launcher`` is injected so the orchestration can be tested without
    Windows; in production it is :func:`_launch_and_verify`.
    """
    def note(message: str) -> None:
        if on_progress:
            on_progress(message)

    by_key = {identity.key: identity for identity in identities}
    run = load_run(run_path) if options.resume else None
    if run is not None and run.resumable:
        note(f"Resuming: {run.completed} of {run.total} already done.")
        run.status = STATUS_RUNNING
    else:
        targets, skipped = plan_run(identities, options, env=env, resolver=resolver)
        run = PrepareRun(planned=[i.key for i in targets], skipped=skipped)
        for key, reason in skipped.items():
            note(f"Skipping {by_key[key].canonical_title if key in by_key else key}: {reason}")
    if save:
        run.save(run_path)

    launcher = launcher or _launch_and_verify
    remaining = run.remaining
    for index, key in enumerate(remaining, 1):
        identity = by_key.get(key)
        if identity is None:
            run.skipped[key] = "no longer in the library"
            continue
        if stop_event is not None and stop_event.is_set():
            run.status = STATUS_INTERRUPTED
            run.stop_reason = "stopped by the user"
            note("Stopped. Run 'travelready prepare --resume' to continue.")
            if save:
                run.save(run_path)
            return run

        note(f"[{run.completed + 1}/{run.total}] {identity.canonical_title}")
        try:
            if options.launch:
                launcher(identity, options, note, stop_event)
            report = assess(identity, env=env, resolver=resolver)
        except Exception as exc:                      # one game must not kill a run
            report = assess(identity, env=env, resolver=resolver)
            report.actions.append(f"Preparation raised an error: {exc}")
            note(f"    error: {exc}")
        run.done[key] = report.to_dict()
        if save:
            run.save(run_path)

    run.status = STATUS_COMPLETE
    run.finished_at = _now()
    if save:
        run.save(run_path)
    return run


def _launch_and_verify(identity: GameIdentity, options: PrepareOptions,
                       note: Callable[[str], None], stop_event) -> None:
    """Launch the best installation and record the result on its entry."""
    from . import history
    from .apppaths import data_file as _data_file
    from .launch_tester import MODE_SMOKE, STATUS_PASS, run_test

    installation = identity.best_installation()
    if installation is None:
        return
    entry = installation.entry
    result = run_test(entry, mode=MODE_SMOKE, stop_event=stop_event,
                      on_progress=note, cleanup=options.cleanup,
                      smoke_duration=options.smoke_duration)
    entry.last_result = result.status
    if result.status == STATUS_PASS:
        entry.last_ready = result.finished_at
    if result.corrected_process and not entry.expected_process:
        entry.expected_process = result.corrected_process
        note(f"    learned the real process name: {result.corrected_process}")
    history.append_results([result], _data_file("history.json"))


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def render_run_report(run: PrepareRun, identities: Sequence[GameIdentity],
                      *, resolver=None) -> str:
    """The end-of-run summary. Every number comes from recorded state."""
    reports = run.reports()
    counts = summarise(reports)
    lines = ["TRAVEL PREPARATION " + ("COMPLETE" if run.status == STATUS_COMPLETE
                                      else run.status.upper()),
             "=" * 58, ""]
    lines.append(f"Games checked: {len(reports)}")
    if run.skipped:
        lines.append(f"Skipped:       {len(run.skipped)}")
    lines.append("")
    for verdict in ("READY", "READY_WITH_WARNINGS", "ACTION_REQUIRED",
                    "NOT_READY", "UNSUPPORTED", "UNKNOWN"):
        if counts.get(verdict):
            lines.append(f"  {verdict.replace('_', ' '):<22}{counts[verdict]:>4}")

    per_launcher = summarise_by_launcher(reports)
    if per_launcher:
        lines += ["", "Launcher readiness:"]
        for launcher, row in per_launcher.items():
            lines.append(f"  {launcher:<12}{row['ready']:>3}/{row['total']}")

    settings_counts: Dict[str, int] = {}
    for report in reports:
        settings_counts[report.settings_state or "UNKNOWN"] = \
            settings_counts.get(report.settings_state or "UNKNOWN", 0) + 1
    if settings_counts:
        lines += ["", "ROG Ally Life:"]
        labels = {"PASS": "profile found", "WARN": "no profile / needs review",
                  "UNKNOWN": "not checked", "NOT_APPLICABLE": "not applicable"}
        for state, count in sorted(settings_counts.items()):
            lines.append(f"  {labels.get(state, state):<28}{count:>4}")

    needing = [r for r in reports if r.readiness == ACTION_REQUIRED]
    if needing:
        lines += ["", f"Needs your attention ({len(needing)}):"]
        for report in sorted(needing, key=lambda r: r.game.lower())[:15]:
            first = report.failures[0] if report.failures else None
            lines.append(f"  {report.game[:40]:<42}{first.reason[:34] if first else ''}")
        if len(needing) > 15:
            lines.append(f"  … and {len(needing) - 15} more")

    if run.resumable:
        lines += ["", f"{len(run.remaining)} game(s) not yet processed. "
                      f"Continue with: travelready prepare --resume"]
    return "\n".join(lines)
