"""preparation.py — is this game actually ready to play offline?

This module exists because the application could not previously answer its own
question. It reported whether a game *launched recently*, which is a proxy for
offline readiness rather than an answer to it, and it had no representation at
all of authentication, first launch, licence state or offline support.

The model here is deliberately explicit:

* a **check** asks one question, declares what it requires to be answerable,
  and returns PASS / FAIL / WARN / UNKNOWN / NOT_APPLICABLE with a reason and,
  where the answer is bad, how to fix it;
* a **state** is where the game has got to in preparation;
* a **readiness** verdict is the one-word summary, derived from the checks.

Three rules govern the whole thing.

**UNKNOWN is a first-class answer.** A check whose requirement this machine
cannot satisfy returns UNKNOWN, never FAIL. "We could not determine this" and
"this is broken" are different facts, and merging them is precisely the mistake
that produced the EA 90-second timeout and the "no profile" label on a cache
that had simply never been synced.

**Nothing here bypasses anything.** No check reads credentials, tokens,
licence files or DRM state. Where a launcher exposes no supported read-only way
to tell whether it is signed in, the answer is UNKNOWN and the user is asked.
Offline preparation is described to the user as instructions; TravelReady does
not perform it, because signing a launcher in is the user's account activity.

**Evidence, not assertion.** Every check records what it actually observed, so
a verdict can be argued with.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from .environment import (
    LAUNCHER_REQUIRED, LIVE_GAME_REQUIRED, LIVE_NETWORK_REQUIRED, PURE_LOGIC,
    WINDOWS_REQUIRED, Environment,
)
from .identity import GameIdentity, Installation
from .launchers import NONE as CAP_NONE, PARTIAL, UNKNOWN as CAP_UNKNOWN, adapter_for
from .library import GameEntry

# --------------------------------------------------------------------------
# check outcomes
# --------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

OUTCOME_MARKS = {PASS: "✓", FAIL: "✗", WARN: "⚠",
                 UNKNOWN: "?", NOT_APPLICABLE: "-"}

# --------------------------------------------------------------------------
# preparation states
# --------------------------------------------------------------------------

NOT_INSTALLED = "NOT_INSTALLED"
DISCOVERED = "DISCOVERED"
LAUNCHER_MISSING = "LAUNCHER_MISSING"
AUTH_REQUIRED = "AUTH_REQUIRED"
READY_TO_LAUNCH = "READY_TO_LAUNCH"
LAUNCHING = "LAUNCHING"
RUNNING = "RUNNING"
VERIFIED = "VERIFIED"
PREPARED = "PREPARED"
OFFLINE_READY = "OFFLINE_READY"
ACTION_REQUIRED_STATE = "ACTION_REQUIRED"
UNSUPPORTED_STATE = "UNSUPPORTED"
FAILED = "FAILED"

STATES = (NOT_INSTALLED, DISCOVERED, LAUNCHER_MISSING, AUTH_REQUIRED,
          READY_TO_LAUNCH, LAUNCHING, RUNNING, VERIFIED, PREPARED,
          OFFLINE_READY, ACTION_REQUIRED_STATE, UNSUPPORTED_STATE, FAILED)

# --------------------------------------------------------------------------
# readiness verdicts
# --------------------------------------------------------------------------

READY = "READY"
READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
ACTION_REQUIRED = "ACTION_REQUIRED"
NOT_READY = "NOT_READY"
UNSUPPORTED = "UNSUPPORTED"
READINESS_UNKNOWN = "UNKNOWN"

READINESS_ORDER = (READY, READY_WITH_WARNINGS, ACTION_REQUIRED, NOT_READY,
                   UNSUPPORTED, READINESS_UNKNOWN)

READINESS_COLORS = {
    READY: "#1e7d32",
    READY_WITH_WARNINGS: "#b26a00",
    ACTION_REQUIRED: "#c62828",
    NOT_READY: "#c62828",
    UNSUPPORTED: "#546e7a",
    READINESS_UNKNOWN: "#546e7a",
}


@dataclass
class Check:
    """One question, its answer, and what the answer rests on."""

    id: str
    label: str
    outcome: str
    reason: str = ""
    requirement: str = PURE_LOGIC
    evidence: str = ""
    fix: str = ""

    @property
    def mark(self) -> str:
        return OUTCOME_MARKS.get(self.outcome, "?")

    @property
    def blocking(self) -> bool:
        return self.outcome == FAIL

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        text = f"{self.mark} {self.label}"
        if self.reason:
            text += f" — {self.reason}"
        return text


@dataclass
class PreparationReport:
    """Everything known about one game's readiness to travel."""

    game: str
    key: str = ""
    launcher: str = ""
    state: str = DISCOVERED
    readiness: str = READINESS_UNKNOWN
    checks: List[Check] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    settings_state: str = ""
    profile_label: str = ""
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # -- views -------------------------------------------------------------

    def with_outcome(self, outcome: str) -> List[Check]:
        return [c for c in self.checks if c.outcome == outcome]

    @property
    def failures(self) -> List[Check]:
        return self.with_outcome(FAIL)

    @property
    def warnings(self) -> List[Check]:
        return self.with_outcome(WARN)

    @property
    def unknowns(self) -> List[Check]:
        return self.with_outcome(UNKNOWN)

    def check(self, check_id: str) -> Optional[Check]:
        for c in self.checks:
            if c.id == check_id:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "game": self.game, "key": self.key, "launcher": self.launcher,
            "state": self.state, "readiness": self.readiness,
            "settings_state": self.settings_state,
            "profile_label": self.profile_label,
            "updated_at": self.updated_at,
            "actions": list(self.actions),
            "checks": [c.to_dict() for c in self.checks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PreparationReport":
        report = cls(
            game=str(data.get("game", "")), key=str(data.get("key", "")),
            launcher=str(data.get("launcher", "")),
            state=str(data.get("state", DISCOVERED)),
            readiness=str(data.get("readiness", READINESS_UNKNOWN)),
            settings_state=str(data.get("settings_state", "")),
            profile_label=str(data.get("profile_label", "")),
            updated_at=str(data.get("updated_at", "")),
            actions=[str(a) for a in data.get("actions", [])],
        )
        for raw in data.get("checks", []):
            report.checks.append(Check(
                id=str(raw.get("id", "")), label=str(raw.get("label", "")),
                outcome=str(raw.get("outcome", UNKNOWN)),
                reason=str(raw.get("reason", "")),
                requirement=str(raw.get("requirement", PURE_LOGIC)),
                evidence=str(raw.get("evidence", "")), fix=str(raw.get("fix", "")),
            ))
        return report

    def describe(self) -> str:
        lines = [self.game.upper(), "", self.readiness.replace("_", " "), ""]
        for check in self.checks:
            if check.outcome == NOT_APPLICABLE:
                continue
            lines.append(check.line())
        if self.actions:
            lines += ["", "What to do:"]
            lines += [f"  {i}. {a}" for i, a in enumerate(self.actions, 1)]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def _installed_check(installation: Optional[Installation], env: Environment) -> Check:
    if installation is None:
        return Check("installed", "Installed", FAIL,
                     "no installation of this game was found",
                     LIVE_GAME_REQUIRED,
                     fix="Install the game, then re-scan.")
    if not env.windows:
        return Check("installed", "Installed", UNKNOWN,
                     "cannot confirm off Windows", WINDOWS_REQUIRED,
                     evidence=f"library entry for {installation.launcher}")
    return Check("installed", "Installed", PASS, "",
                 LIVE_GAME_REQUIRED,
                 evidence=installation.install_dir or installation.launch_target)


def _launcher_check(installation: Installation, env: Environment) -> Check:
    adapter = installation.adapter
    if not env.windows:
        return Check("launcher", f"{adapter.display_name} available", UNKNOWN,
                     "cannot check off Windows", WINDOWS_REQUIRED)
    present = adapter.launcher_installed()
    if present is None:
        return Check("launcher", f"{adapter.display_name} available", UNKNOWN,
                     "no supported way to detect this launcher",
                     LAUNCHER_REQUIRED)
    if not present:
        return Check("launcher", f"{adapter.display_name} available", FAIL,
                     "the launcher does not appear to be installed",
                     LAUNCHER_REQUIRED,
                     fix=f"Install {adapter.display_name}, then re-scan.")
    return Check("launcher", f"{adapter.display_name} available", PASS,
                 "", LAUNCHER_REQUIRED)


def _launch_target_check(installation: Installation) -> Check:
    from .launch_tester import build_command, validate_launch_target

    target = installation.launch_target
    if not target:
        return Check("launch_target", "Launch target discovered", FAIL,
                     "no launch target is recorded for this installation",
                     PURE_LOGIC, fix="Re-scan, or set the executable in Edit Game.")
    problem = validate_launch_target(installation.launch_method, target)
    if problem:
        return Check("launch_target", "Launch target discovered", FAIL,
                     problem, PURE_LOGIC,
                     evidence=target[:120],
                     fix="Re-scan to rediscover a valid launch target.")
    try:
        build_command(installation.entry)
    except ValueError as exc:
        return Check("launch_target", "Launch target discovered", FAIL,
                     str(exc), PURE_LOGIC, evidence=target[:120],
                     fix="Re-scan, or correct the path in Edit Game.")
    return Check("launch_target", "Launch target discovered", PASS, "",
                 PURE_LOGIC, evidence=target[:120])


def _verification_check(installation: Installation) -> Check:
    """Can TravelReady confirm this game started?

    Reported separately from launching on purpose. A game TravelReady can start
    but cannot verify is still preparable — it just needs the user to confirm.
    Collapsing the two is what made Xbox look unlaunchable.
    """
    signal = installation.verification_signal()
    if signal.level == CAP_NONE:
        return Check("verification", "Launch can be verified", WARN,
                     signal.detail, PURE_LOGIC,
                     fix="Confirm manually when TravelReady launches it, or set "
                         "the game's process name in Edit Game.")
    return Check("verification", "Launch can be verified", PASS,
                 signal.detail, PURE_LOGIC)


def _authentication_check(installation: Installation, env: Environment) -> Check:
    """Is the launcher signed in?

    No adapter reads credentials or tokens. Most launchers expose no supported
    read-only way to tell, so the honest answer is UNKNOWN and the user is told
    what to confirm — never a guess presented as a fact.
    """
    adapter = installation.adapter
    caps = adapter.capabilities()
    state = adapter.is_authenticated(installation.entry)
    if state is True:
        return Check("authentication", "Launcher sign-in", PASS,
                     caps.is_authenticated.detail, LAUNCHER_REQUIRED)
    if state is False:
        return Check("authentication", "Launcher sign-in", FAIL,
                     "the launcher is not signed in", LAUNCHER_REQUIRED,
                     fix=f"Sign in to {adapter.display_name} while you are online.")
    return Check("authentication", "Launcher sign-in", UNKNOWN,
                 caps.is_authenticated.detail or
                 "TravelReady does not read sign-in state", LAUNCHER_REQUIRED,
                 fix=f"Confirm you are signed in to {adapter.display_name} "
                     f"before you travel.")


def _first_launch_check(installation: Installation) -> Check:
    """Has this game been started successfully while online?

    This is the check that most nearly answers the real question: a launcher
    caches entitlements at first launch, and a title that has never been run
    online is the one most likely to fail on a plane.
    """
    from .launch_tester import STATUS_PASS
    from .readiness import readiness_age_days

    entry = installation.entry
    result = (entry.last_result or "").strip().upper()
    if not result:
        return Check("first_launch", "Launched successfully while online", FAIL,
                     "this game has never been verified by TravelReady",
                     LIVE_GAME_REQUIRED,
                     fix="Run Prepare for Travel so TravelReady can start it once "
                         "while you are online.")
    if result != STATUS_PASS:
        if result in ("MANUAL", "MANUAL_REQUIRED", "UNKNOWN"):
            return Check("first_launch", "Launched successfully while online", WARN,
                         f"last result was {result}: you confirmed it rather than "
                         f"TravelReady verifying it", LIVE_GAME_REQUIRED)
        return Check("first_launch", "Launched successfully while online", FAIL,
                     f"last attempt ended in {result}", LIVE_GAME_REQUIRED,
                     evidence=entry.last_result,
                     fix="Run Prepare for Travel again and read the diagnostics.")
    days = readiness_age_days(entry)
    if days is not None and days > 30:
        return Check("first_launch", "Launched successfully while online", WARN,
                     f"last verified {int(days)} days ago; launcher licences can "
                     f"lapse", LIVE_GAME_REQUIRED,
                     fix="Re-verify before you travel.")
    return Check("first_launch", "Launched successfully while online", PASS,
                 "", LIVE_GAME_REQUIRED, evidence=entry.last_ready)


def _offline_support_check(installation: Installation) -> Check:
    """Does this launcher support playing offline at all?"""
    adapter = installation.adapter
    caps = adapter.capabilities()
    level = caps.prepare_for_offline
    if level.level == CAP_NONE:
        return Check("offline_support", "Offline play supported", FAIL,
                     level.detail, LAUNCHER_REQUIRED,
                     fix="Do not rely on this game offline.")
    if level.level == CAP_UNKNOWN:
        return Check("offline_support", "Offline play supported", UNKNOWN,
                     level.detail, LAUNCHER_REQUIRED)
    instruction = adapter.prepare_for_offline(installation.entry)
    if level.level == PARTIAL:
        return Check("offline_support", "Offline play supported", WARN,
                     level.detail, LAUNCHER_REQUIRED, fix=instruction or "")
    return Check("offline_support", "Offline play supported", PASS,
                 level.detail, LAUNCHER_REQUIRED)


def _settings_check(identity: GameIdentity, resolver, env: Environment) -> Check:
    """Is a ROG Ally Life recommendation available?

    Never blocking: settings are an optimisation, and a game with no published
    profile is still perfectly ready to travel. Note the three distinct
    outcomes — a profile, no profile, and *we have not looked* — which the
    previous implementation collapsed into "No profile".
    """
    if resolver is None:
        return Check("settings", "ROG Ally Life profile", UNKNOWN,
                     "the recommendation cache has not been consulted",
                     LIVE_NETWORK_REQUIRED,
                     fix="Run 'travelready settings update' while you are online.")
    cached = len(getattr(resolver.cache, "index", None).entries) if resolver.cache else 0
    if not cached:
        return Check("settings", "ROG Ally Life profile", UNKNOWN,
                     "no recommendations have been downloaded yet",
                     LIVE_NETWORK_REQUIRED,
                     fix="Run 'travelready settings update' while you are online.")
    entry = identity.best_installation().entry if identity.installed else None
    if entry is None:
        return Check("settings", "ROG Ally Life profile", NOT_APPLICABLE,
                     "no installation to match", PURE_LOGIC)
    resolution = resolver.resolve(entry)
    if resolution.matched:
        label = (resolution.selection.profile.label
                 if resolution.selection and resolution.selection.profile else "")
        return Check("settings", "ROG Ally Life profile", PASS, label,
                     PURE_LOGIC, evidence=resolution.match.source_url)
    if resolution.needs_review:
        return Check("settings", "ROG Ally Life profile", WARN,
                     f"a possible match needs review: "
                     f"'{resolution.match.matched_title}' "
                     f"({resolution.match.confidence:.2f})", PURE_LOGIC,
                     fix="Check the suggested match before using it.")
    return Check("settings", "ROG Ally Life profile", WARN,
                 "no recommendation published for this game", PURE_LOGIC)


# --------------------------------------------------------------------------
# assessment
# --------------------------------------------------------------------------

def _derive_state(report: PreparationReport, installation: Optional[Installation]) -> str:
    if installation is None:
        return NOT_INSTALLED
    def outcome(check_id: str) -> str:
        check = report.check(check_id)
        return check.outcome if check else UNKNOWN

    if outcome("installed") == FAIL:
        return NOT_INSTALLED
    if outcome("launcher") == FAIL:
        return LAUNCHER_MISSING
    if outcome("offline_support") == FAIL:
        return UNSUPPORTED_STATE
    if outcome("authentication") == FAIL:
        return AUTH_REQUIRED
    if outcome("launch_target") == FAIL:
        return FAILED
    if outcome("first_launch") == FAIL:
        return READY_TO_LAUNCH
    if report.failures:
        return ACTION_REQUIRED_STATE
    if outcome("first_launch") == PASS and not report.warnings:
        return OFFLINE_READY
    if outcome("first_launch") in (PASS, WARN):
        return PREPARED
    return DISCOVERED


def _derive_readiness(report: PreparationReport) -> str:
    if report.state == UNSUPPORTED_STATE:
        return UNSUPPORTED
    if report.state == NOT_INSTALLED:
        return NOT_READY
    if report.failures:
        return ACTION_REQUIRED
    blocking_unknowns = [c for c in report.unknowns
                         if c.id in ("installed", "launch_target")]
    if blocking_unknowns:
        return READINESS_UNKNOWN
    if report.warnings or report.unknowns:
        return READY_WITH_WARNINGS
    return READY


def assess(identity: GameIdentity, *, env: Optional[Environment] = None,
           resolver=None, installation: Optional[Installation] = None) -> PreparationReport:
    """Run every check for one game and derive its state and readiness.

    Read-only: nothing is launched, written or fetched. Running the checks is
    cheap enough to do for a whole library on every refresh.
    """
    from .environment import current

    env = env or current(check_network=False)
    installation = installation or identity.best_installation()
    report = PreparationReport(
        game=identity.canonical_title, key=identity.key,
        launcher=(installation.launcher if installation else ""),
    )

    report.checks.append(_installed_check(installation, env))
    if installation is not None:
        report.checks.append(_launcher_check(installation, env))
        report.checks.append(_launch_target_check(installation))
        report.checks.append(_verification_check(installation))
        report.checks.append(_authentication_check(installation, env))
        report.checks.append(_first_launch_check(installation))
        report.checks.append(_offline_support_check(installation))
    report.checks.append(_settings_check(identity, resolver, env))

    settings_check = report.check("settings")
    if settings_check is not None:
        report.settings_state = settings_check.outcome
        if settings_check.outcome == PASS:
            report.profile_label = settings_check.reason

    report.state = _derive_state(report, installation)
    report.readiness = _derive_readiness(report)

    seen = set()
    for check in report.checks:
        if check.fix and check.outcome in (FAIL, WARN, UNKNOWN) and check.fix not in seen:
            seen.add(check.fix)
            report.actions.append(check.fix)
    return report


def assess_all(identities: Sequence[GameIdentity], **kwargs) -> List[PreparationReport]:
    return [assess(identity, **kwargs) for identity in identities]


def summarise(reports: Sequence[PreparationReport]) -> Dict[str, int]:
    """Counts by readiness verdict, plus the total."""
    counts = {verdict: 0 for verdict in READINESS_ORDER}
    for report in reports:
        counts[report.readiness] = counts.get(report.readiness, 0) + 1
    counts["total"] = len(reports)
    return counts


def summarise_by_launcher(reports: Sequence[PreparationReport]) -> Dict[str, Dict[str, int]]:
    """``launcher -> {ready, total}`` for the per-launcher readiness table."""
    out: Dict[str, Dict[str, int]] = {}
    for report in reports:
        row = out.setdefault(report.launcher or "other", {"ready": 0, "total": 0})
        row["total"] += 1
        if report.readiness in (READY, READY_WITH_WARNINGS):
            row["ready"] += 1
    return dict(sorted(out.items()))
