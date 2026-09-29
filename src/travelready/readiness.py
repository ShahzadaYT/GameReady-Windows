"""readiness.py — travel-readiness state management.

Maps a :class:`travelready.library.GameEntry` (plus its most recent test
result) to a human-facing readiness state and to the GUI tab it belongs to.

States
------
READY      — last test PASSed recently (within ``stale_days``).
STALE      — last test PASSed but the pass is older than ``stale_days``.
ATTENTION  — last test FAILed / TIMED OUT / etc. Needs the user's attention.
MANUAL     — verified by the user rather than by process detection.
UNTESTED   — never tested, or last result is empty/unknown.
PREPARING  — transient state used by the GUI while a test is in flight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

STATE_READY = "READY"
STATE_STALE = "STALE"
STATE_ATTENTION = "ATTENTION"
STATE_MANUAL = "MANUAL"
STATE_UNTESTED = "UNTESTED"
STATE_PREPARING = "PREPARING"
STATES = [STATE_READY, STATE_STALE, STATE_ATTENTION, STATE_MANUAL,
          STATE_UNTESTED, STATE_PREPARING]

FAIL_STATUSES = {"FAIL", "TIMEOUT", "NOT_FOUND", "ACCESS_DENIED"}
PASS_STATUSES = {"PASS"}
MANUAL_STATUSES = {"MANUAL", "UNKNOWN", "MANUAL_REQUIRED"}

DEFAULT_STALE_DAYS = 7

TAB_ORDER = ["All", "Xbox", "EA", "Ubisoft", "Epic", "Steam", "GOG", "Battle.net", "Other"]

_LAUNCHER_TO_TAB = {
    "xbox": "Xbox", "ea": "EA", "ubisoft": "Ubisoft", "epic": "Epic",
    "steam": "Steam", "gog": "GOG", "battlenet": "Battle.net",
}

STATE_COLORS = {
    STATE_READY: "#1e7d32",
    STATE_STALE: "#b26a00",
    STATE_ATTENTION: "#c62828",
    STATE_MANUAL: "#1565c0",
    STATE_UNTESTED: "#546e7a",
    STATE_PREPARING: "#6a1b9a",
}

#: Shown for an Xbox entry whose game process could not be resolved. Note the
#: wording: it is about *this entry*, not about Xbox as a platform. The older
#: blanket claim ("Xbox Store games cannot be automatically process-verified")
#: was the justification for the regression and is not true in general — see
#: docs/ENGINEERING_ASSESSMENT.md §1.
XBOX_MANUAL_NOTE = (
    "The game process for this Store entry could not be identified, so "
    "TravelReady will launch it for you but cannot confirm it started. "
    "Confirm manually while you are still online."
)


def tab_for_launcher(launcher: str) -> str:
    """Map a launcher enum string to its GUI tab name."""
    return _LAUNCHER_TO_TAB.get(str(launcher or "").strip().lower(), "Other")


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def readiness_age_days(entry, now: Optional[datetime] = None) -> Optional[float]:
    """Days since this entry last passed, or ``None`` if it never has."""
    ts = _parse_iso(getattr(entry, "last_ready", ""))
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def age_text(entry, now: Optional[datetime] = None) -> str:
    """'3 days ago' style text for the dashboard."""
    days = readiness_age_days(entry, now)
    if days is None:
        return "never"
    if days < 1 / 24:
        return "just now"
    if days < 1:
        return f"{int(days * 24)}h ago"
    if days < 2:
        return "yesterday"
    return f"{int(days)} days ago"


def state_of(entry, stale_days: int = DEFAULT_STALE_DAYS,
             now: Optional[datetime] = None) -> str:
    """Compute the readiness state for a game entry."""
    status = str(getattr(entry, "last_result", "") or "").strip().upper()
    if not status:
        return STATE_UNTESTED
    if status in FAIL_STATUSES:
        return STATE_ATTENTION
    if status in MANUAL_STATUSES:
        return STATE_MANUAL
    if status in PASS_STATUSES:
        days = readiness_age_days(entry, now)
        if days is None:
            return STATE_READY
        return STATE_READY if days <= stale_days else STATE_STALE
    return STATE_UNTESTED


def failure_hint(entry) -> str:
    """A per-entry note explaining what the user still has to do."""
    from .launch_tester import can_auto_close

    if getattr(entry, "verification", "auto") == "manual":
        if entry.launcher == "xbox":
            return XBOX_MANUAL_NOTE
        return "This entry is verified manually."
    ok, reason = can_auto_close(entry)
    if not ok:
        return f"Cannot be prepared automatically: {reason}."
    return ""


def prepare_targets(entries: Sequence, stale_days: int = DEFAULT_STALE_DAYS,
                    reverify_ready: bool = False,
                    now: Optional[datetime] = None) -> Tuple[List, List]:
    """Split entries into ``(will_prepare, skipped)`` for Prepare-for-Travel.

    Restored from the older build, with the Xbox exclusion removed. An entry is
    skipped only when TravelReady genuinely cannot close what it would start —
    never because of which launcher it belongs to. The older build's rule
    applies: *"Prepare-for-travel must NEVER launch what it cannot close,
    otherwise games pile up and stay running."*
    """
    from .launch_tester import can_auto_close

    targets, skipped = [], []
    for entry in entries:
        ok, _ = can_auto_close(entry)
        if not ok:
            skipped.append(entry)
            continue
        if not reverify_ready and state_of(entry, stale_days, now) == STATE_READY:
            continue
        targets.append(entry)
    return targets, skipped


def tab_counts(entries: Sequence, stale_days: int = DEFAULT_STALE_DAYS,
               now: Optional[datetime] = None) -> Dict[str, int]:
    """Live per-tab counts for the launcher tab bar."""
    counts = {tab: 0 for tab in TAB_ORDER}
    for entry in entries:
        counts["All"] += 1
        counts[tab_for_launcher(getattr(entry, "launcher", ""))] += 1
    return counts


def summarize(entries: Sequence, stale_days: int = DEFAULT_STALE_DAYS,
              now: Optional[datetime] = None) -> Dict[str, int]:
    """Counts by readiness state, for the travel dashboard."""
    out = {state: 0 for state in STATES}
    for entry in entries:
        out[state_of(entry, stale_days, now)] += 1
    out["total"] = len(entries)
    return out


# --------------------------------------------------------------------------
# settings readiness — reported alongside launch readiness, never gating it
# --------------------------------------------------------------------------

SETTINGS_READY = "Ready"
SETTINGS_REVIEW = "Review"
SETTINGS_NONE = "No profile"
SETTINGS_MANUAL = "Manual"
SETTINGS_UNKNOWN = "Not checked"

_SETTINGS_FROM_STATUS = {
    "matched": SETTINGS_READY,
    "review": SETTINGS_REVIEW,
    "no_profiles_published": SETTINGS_REVIEW,
    "no_profile": SETTINGS_NONE,
}


def settings_state_of(entry, resolver=None) -> str:
    """Whether a ROG Ally Life profile is available for ``entry``.

    Reported *beside* launch readiness and never affecting it: a game with no
    published recommendation is still perfectly launchable, and TravelReady
    must not hold up Prepare-for-Travel because an optional optimisation is
    unavailable.

    ``Not checked`` and ``No profile`` are deliberately different answers. An
    unsynced cache means we have not looked; only a synced cache that returned
    nothing means the source has no recommendation. Reporting the first as the
    second turns a temporary network problem into a permanent-looking fact.
    """
    if resolver is None:
        return SETTINGS_UNKNOWN
    cache = getattr(resolver, "cache", None)
    index = getattr(cache, "index", None)
    if not getattr(index, "entries", None):
        return SETTINGS_UNKNOWN
    resolution = resolver.resolve(entry)
    state = _SETTINGS_FROM_STATUS.get(resolution.status, SETTINGS_UNKNOWN)
    if state == SETTINGS_READY and resolution.profile is not None:
        applicable = [r for r in resolution.profile.recommendations
                      if r.category == "game"]
        if not applicable:
            return SETTINGS_MANUAL
    return state


def is_travel_ready(entries: Sequence, stale_days: int = DEFAULT_STALE_DAYS,
                    now: Optional[datetime] = None) -> bool:
    """True when nothing in the selection still needs attention."""
    summary = summarize(entries, stale_days, now)
    return summary[STATE_ATTENTION] == 0 and summary[STATE_UNTESTED] == 0
