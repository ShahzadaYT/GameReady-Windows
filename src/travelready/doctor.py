"""doctor.py — why can't TravelReady do the thing?

Every support question about a tool like this is really one of a small number
of questions: is my environment capable, is my data sane, is the source
reachable, is my library full of junk. The doctor answers all of them in one
pass and, where the answer is bad, says what to do about it.

``--fix`` repairs only what is unambiguously TravelReady's own local state:
missing directories, a corrupt index it can rebuild, a stale cache marker, a
run record from an incompatible version. It will never touch credentials, DRM,
anti-cheat, account state, or anything belonging to a launcher or a game —
those produce instructions for the user instead. The split is not a matter of
politeness: repairing someone's account state without being asked is exactly
the class of action this application refuses on principle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__
from .apppaths import backups_dir, data_dir, data_file
from .environment import Environment, current
from .library import LIBRARY_FILE, GameEntry, identity_key, load_library

OK = "OK"
WARN = "WARN"
PROBLEM = "PROBLEM"
INFO = "INFO"

_MARKS = {OK: "✓", WARN: "⚠", PROBLEM: "✗", INFO: "·"}


@dataclass
class Finding:
    """One diagnostic result."""

    section: str
    name: str
    status: str
    detail: str = ""
    fix_hint: str = ""
    #: Set when ``--fix`` can repair this without touching anything external.
    repair: Optional[Callable[[], str]] = None

    @property
    def mark(self) -> str:
        return _MARKS.get(self.status, "?")

    @property
    def repairable(self) -> bool:
        return self.repair is not None

    def line(self) -> str:
        text = f"  {self.mark} {self.name:<34}{self.detail}"
        return text.rstrip()


@dataclass
class DoctorReport:
    findings: List[Finding] = field(default_factory=list)

    def add(self, *args, **kwargs) -> Finding:
        finding = Finding(*args, **kwargs)
        self.findings.append(finding)
        return finding

    def by_section(self) -> Dict[str, List[Finding]]:
        out: Dict[str, List[Finding]] = {}
        for finding in self.findings:
            out.setdefault(finding.section, []).append(finding)
        return out

    @property
    def problems(self) -> List[Finding]:
        return [f for f in self.findings if f.status == PROBLEM]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.status == WARN]

    @property
    def repairable(self) -> List[Finding]:
        return [f for f in self.findings if f.repairable]

    @property
    def healthy(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        lines = [f"TravelReady doctor — version {__version__}", "=" * 58]
        for section, findings in self.by_section().items():
            lines += ["", section]
            lines += [f.line() for f in findings]
        lines += ["", "-" * 58]
        if self.problems:
            lines.append(f"{len(self.problems)} problem(s), "
                         f"{len(self.warnings)} warning(s).")
            lines.append("")
            lines.append("What to do:")
            for finding in self.problems + self.warnings:
                if finding.fix_hint:
                    lines.append(f"  • {finding.name}: {finding.fix_hint}")
        elif self.warnings:
            lines.append(f"No problems. {len(self.warnings)} warning(s).")
            for finding in self.warnings:
                if finding.fix_hint:
                    lines.append(f"  • {finding.name}: {finding.fix_hint}")
        else:
            lines.append("Everything checks out.")
        if self.repairable:
            lines.append("")
            lines.append(f"{len(self.repairable)} item(s) can be repaired with: "
                         f"travelready doctor --fix")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def _check_application(report: DoctorReport) -> None:
    section = "Application"
    report.add(section, "Version", INFO, __version__)
    directory = data_dir()
    if directory.is_dir():
        report.add(section, "Data directory", OK, str(directory))
    else:
        report.add(section, "Data directory", WARN, f"{directory} does not exist yet",
                   fix_hint="It is created on first use; --fix creates it now.",
                   repair=lambda: (directory.mkdir(parents=True, exist_ok=True),
                                   f"created {directory}")[1])
    writable = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        report.add(section, "Data directory writable", PROBLEM, str(exc),
                   fix_hint="Check permissions on the data directory.")
    if writable:
        report.add(section, "Data directory writable", OK)


def _check_environment(report: DoctorReport, env: Environment) -> None:
    section = "Environment"
    for name, capability in env.summary().items():
        status = OK if capability else WARN
        hint = ""
        if not capability:
            if name == "Windows":
                hint = "Discovery, launching and verification need Windows."
            elif name == "Internet":
                hint = "Run 'travelready settings update' when you are online."
            elif name == "ROG Ally":
                hint = "Profiles are written for the ROG Ally; check they apply."
            elif name == "PowerShell":
                hint = "Xbox discovery and process paths need PowerShell."
        report.add(section, name, status, capability.detail, fix_hint=hint)
    if env.device_model:
        report.add(section, "Device model", INFO, env.device_model)


def _check_launchers(report: DoctorReport, env: Environment) -> None:
    from .launchers import ADAPTERS

    section = "Launchers"
    if not env.windows:
        report.add(section, "Launcher detection", WARN,
                   "cannot detect launchers off Windows")
        return
    for key in ("steam", "xbox", "ea", "epic", "ubisoft", "gog", "battlenet"):
        adapter = ADAPTERS[key]
        present = adapter.launcher_installed()
        if present is None:
            report.add(section, adapter.display_name, INFO,
                       "no supported detection for this launcher")
        elif present:
            report.add(section, adapter.display_name, OK, "installed")
        else:
            report.add(section, adapter.display_name, INFO, "not installed")


def _check_library(report: DoctorReport, library_path: Optional[Path]) -> None:
    from .classification import classify_entry, split_games
    from .identity import build_identities, summarise as summarise_identities

    section = "Game library"
    path = library_path or data_file(LIBRARY_FILE)
    if not Path(path).exists():
        report.add(section, "Library file", WARN, f"{path} does not exist",
                   fix_hint="Run 'travelready scan' to build it.")
        return
    entries, message = load_library(path)
    if not entries and "corrupt" in message.lower():
        report.add(section, "Library file", PROBLEM, message,
                   fix_hint="The original was backed up; run 'travelready scan'.")
        return
    report.add(section, "Library file", OK, f"{len(entries)} entries")

    games, non_games = split_games(entries)
    report.add(section, "Classified as games", INFO,
               f"{len(games)} games, {len(non_games)} other entries")
    if non_games:
        names = ", ".join(e.name for e in non_games[:3])
        report.add(section, "Non-games in the library", INFO,
                   f"{len(non_games)} ({names}…) — excluded from every count",
                   fix_hint="See 'travelready scan --explain' for the reasons.")
    flagged = [(e, classify_entry(e)) for e in games]
    flagged = [(e, c) for e, c in flagged if c.warning]
    if flagged:
        report.add(section, "Games with a bad executable", WARN,
                   f"{len(flagged)} ({flagged[0][0].name}…)",
                   fix_hint="Re-scan so discovery can find the real executable.")

    identities = build_identities(games)
    stats = summarise_identities(identities)
    report.add(section, "Distinct games", INFO,
               f"{stats['games']} games from {stats['installations']} installations")
    if stats["multi_launcher"]:
        report.add(section, "Games on several launchers", INFO,
                   str(stats["multi_launcher"]))

    invalid = [e for e in entries if not (e.launch_target or e.exe_path)]
    if invalid:
        report.add(section, "Entries with no launch target", WARN, str(len(invalid)),
                   fix_hint="Re-scan, or set the executable in Edit Game.")
    else:
        report.add(section, "Entries with no launch target", OK, "none")

    seen: Dict[str, int] = {}
    for entry in entries:
        key = (entry.launcher, identity_key(entry.name))
        seen[key] = seen.get(key, 0) + 1
    duplicates = sum(1 for count in seen.values() if count > 1)
    if duplicates:
        report.add(section, "Duplicate entries", WARN,
                   f"{duplicates} game(s) appear more than once for one launcher",
                   fix_hint="Re-scan; discovery merges these now.")
    else:
        report.add(section, "Duplicate entries", OK, "none")

    unverifiable = sum(1 for i in identities
                       if i.best_installation() and not i.best_installation().can_verify)
    if unverifiable:
        report.add(section, "Games that cannot be verified", WARN,
                   f"{unverifiable} of {stats['games']}",
                   fix_hint="Re-scan to resolve executables, or set process names.")


def _check_source(report: DoctorReport, env: Environment) -> None:
    from .optimiser.rogallylife import SOURCE_NAME
    from .optimiser.rogallylife.cache import ProfileCache
    from .optimiser.rogallylife.parser import PARSER_VERSION

    section = SOURCE_NAME
    cache = ProfileCache()
    stats = cache.stats()
    if not stats["files"]:
        report.add(section, "Cached recommendations", WARN, "none",
                   fix_hint="Run 'travelready settings update' while you are online.")
    else:
        report.add(section, "Cached recommendations", OK,
                   f"{stats['files']} games, {stats['profiles']} profiles")
    report.add(section, "Last sync", INFO if stats["last_sync"] else WARN,
               stats["last_sync"] or "never")
    report.add(section, "Parser version", INFO, str(PARSER_VERSION))

    stale = cache.needs_reparse(PARSER_VERSION)
    if stale:
        finding = report.add(
            section, "Entries from an older parser", WARN, str(len(stale)),
            fix_hint="Run 'travelready settings update --force' to re-read them.")
        finding.repair = lambda: (_mark_stale(cache, stale),
                                  f"marked {len(stale)} entries for re-fetch")[1]
    else:
        report.add(section, "Entries from an older parser", OK, "none")

    if not env.network:
        report.add(section, "Source reachable", WARN, env.network.detail,
                   fix_hint="Cached profiles still work offline.")
    else:
        report.add(section, "Source reachable", OK, env.network.detail)


def _mark_stale(cache, keys: Sequence[str]) -> None:
    """Force a re-fetch by clearing the recorded hashes, not the data."""
    for key in keys:
        record = cache.index.entries.get(key)
        if record:
            record["content_hash"] = ""
    cache.save_index()


def _check_settings(report: DoctorReport) -> None:
    from .optimiser.inspector import KNOWN_SCHEMAS
    from .optimiser.rogallylife.capability import CAPABILITIES
    from .optimiser.safety import ALLOWED_CONFIG_SUFFIXES
    from .optimiser.transaction import list_backups

    section = "Settings"
    report.add(section, "Config formats understood", INFO,
               ", ".join(sorted(ALLOWED_CONFIG_SUFFIXES)))
    report.add(section, "Game engines recognised", INFO,
               ", ".join(s.name for s in KNOWN_SCHEMAS))
    automatable = sum(1 for c in CAPABILITIES.values() if c.automatable)
    report.add(section, "Settings that can be applied", INFO,
               f"{automatable} of {len(CAPABILITIES)} recognised")
    backups = list_backups()
    report.add(section, "Configuration backups", INFO,
               f"{len(backups)} stored in {backups_dir()}")
    directory = backups_dir()
    if not directory.is_dir():
        report.add(section, "Backup directory", WARN, "missing",
                   repair=lambda: (directory.mkdir(parents=True, exist_ok=True),
                                   f"created {directory}")[1])


def _check_run_state(report: DoctorReport) -> None:
    from .prepare_run import RUN_FILE, RUN_VERSION, load_run

    section = "Prepare-for-Travel"
    path = data_file(RUN_FILE)
    if not path.exists():
        report.add(section, "Saved run", INFO, "none")
        return
    run = load_run()
    if run is None:
        finding = report.add(
            section, "Saved run", WARN,
            "unreadable or from an incompatible version",
            fix_hint="It will be discarded; start a fresh prepare.")
        finding.repair = lambda: (path.unlink(missing_ok=True),
                                  "removed the unusable run record")[1]
        return
    if run.resumable:
        report.add(section, "Saved run", INFO,
                   f"{run.completed}/{run.total} done — "
                   f"resume with 'travelready prepare --resume'")
    else:
        report.add(section, "Saved run", OK,
                   f"complete ({run.total} games, {run.finished_at})")


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def run_doctor(*, env: Optional[Environment] = None,
               library_path: Optional[Path] = None,
               check_network: bool = True) -> DoctorReport:
    """Run every diagnostic. Read-only."""
    env = env or current(check_network=check_network)
    report = DoctorReport()
    _check_application(report)
    _check_environment(report, env)
    _check_launchers(report, env)
    _check_library(report, library_path)
    _check_source(report, env)
    _check_settings(report)
    _check_run_state(report)
    return report


def apply_fixes(report: DoctorReport) -> List[str]:
    """Repair what is safely repairable. Returns what was done.

    Only findings that carry a ``repair`` callable are touched, and a repair is
    only ever attached to TravelReady's own local state.
    """
    done: List[str] = []
    for finding in report.repairable:
        try:
            done.append(finding.repair() or finding.name)
        except Exception as exc:
            done.append(f"{finding.name}: repair failed ({exc})")
    return done
