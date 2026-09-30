"""appstate.py — one owner of the application's state, with events.

The stale-game-list bug was not a missing line. The tree rendered from a
*computed* model (`identities`) derived from a *stored* model (`entries`), and
one of the three code paths that mutate the stored model forgot to recompute
the derived one. Adding the missing call would have fixed that path and left
the next one to be written just as fragile.

So the model owns its own invalidation. :class:`AppState` holds the library and
everything derived from it; mutating the library through
:meth:`AppState.set_entries` (the only way in) drops the derived caches and
publishes an event. Views subscribe to events and never keep their own copy.

Events
------
``LIBRARY_CHANGED``      the entry list was replaced — identities and readiness
                         are gone and will be recomputed on next access
``SCAN_COMPLETED``       a discovery scan finished (carries the counts)
``SOURCE_SYNC_COMPLETED`` the recommendation cache changed
``READINESS_CHANGED``    one or more preparation verdicts changed
``SELECTION_CHANGED``    the set of selected games changed
``FILTER_CHANGED``       the visible subset changed (tab, search or filter)
``STATUS_CHANGED``       a human-readable status line for the UI

Nothing here imports tkinter. The whole model is exercised in tests without a
display, which is how the regressions below are pinned.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import classification, identity as identity_mod, preparation, readiness
from .environment import Environment
from .identity import GameIdentity
from .library import GameEntry

# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

LIBRARY_CHANGED = "LibraryChanged"
SCAN_COMPLETED = "ScanCompleted"
SOURCE_SYNC_COMPLETED = "SettingsSyncCompleted"
READINESS_CHANGED = "ReadinessChanged"
SELECTION_CHANGED = "GameSelectionChanged"
FILTER_CHANGED = "FilterChanged"
STATUS_CHANGED = "StatusChanged"

EVENTS = (LIBRARY_CHANGED, SCAN_COMPLETED, SOURCE_SYNC_COMPLETED,
          READINESS_CHANGED, SELECTION_CHANGED, FILTER_CHANGED, STATUS_CHANGED)


class EventBus:
    """A tiny synchronous publish/subscribe bus.

    Callbacks run on whichever thread publishes, so the GUI publishes only from
    its own thread — background work reports through the task queue instead.
    A failing subscriber is isolated: one broken view must not stop the others
    from updating, which is how a cosmetic error turns into a stale window.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable]] = {}
        self.errors: List[str] = []

    def subscribe(self, event: str, callback: Callable) -> Callable:
        """Register ``callback`` for ``event``; returns an unsubscribe callable."""
        if event not in EVENTS:
            raise ValueError(f"unknown event: {event}")
        self._subscribers.setdefault(event, []).append(callback)

        def unsubscribe() -> None:
            handlers = self._subscribers.get(event, [])
            if callback in handlers:
                handlers.remove(callback)

        return unsubscribe

    def publish(self, event: str, payload=None) -> None:
        for callback in list(self._subscribers.get(event, [])):
            try:
                callback(payload)
            except Exception as exc:                 # one bad view, not all of them
                self.errors.append(f"{event}: {type(exc).__name__}: {exc}")

    def subscriber_count(self, event: str) -> int:
        return len(self._subscribers.get(event, []))


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

FILTER_ALL = "All"
FILTER_READY = "Ready"
FILTER_WARNINGS = "Warnings"
FILTER_ACTION = "Action required"
FILTER_UNKNOWN = "Cannot determine"
FILTER_WITH_SETTINGS = "Settings available"
FILTER_WITHOUT_SETTINGS = "No settings"

FILTERS = (FILTER_ALL, FILTER_READY, FILTER_WARNINGS, FILTER_ACTION,
           FILTER_UNKNOWN, FILTER_WITH_SETTINGS, FILTER_WITHOUT_SETTINGS)

_FILTER_TO_READINESS = {
    FILTER_READY: preparation.READY,
    FILTER_WARNINGS: preparation.READY_WITH_WARNINGS,
    FILTER_ACTION: preparation.ACTION_REQUIRED,
    FILTER_UNKNOWN: preparation.READINESS_UNKNOWN,
}

SORT_NAME = "Name"
SORT_LAUNCHER = "Launcher"
SORT_READINESS = "Readiness"
SORT_SETTINGS = "Settings"
SORT_LAST_TESTED = "Last tested"

SORTS = (SORT_NAME, SORT_LAUNCHER, SORT_READINESS, SORT_SETTINGS, SORT_LAST_TESTED)


@dataclass
class ScanResult:
    """What a scan changed, for the event payload."""

    found: int = 0
    added: int = 0
    updated: int = 0
    total_entries: int = 0
    games: int = 0
    non_games: int = 0


# --------------------------------------------------------------------------
# the state
# --------------------------------------------------------------------------

class AppState:
    """The library, everything derived from it, and the current view.

    Derived values (`identities`, readiness reports) are computed lazily and
    cached, and the cache is dropped automatically whenever the library
    changes. A caller cannot forget to invalidate, because callers do not
    invalidate — the setter does.
    """

    def __init__(self, *, environment: Optional[Environment] = None,
                 resolver=None, stale_days: int = readiness.DEFAULT_STALE_DAYS) -> None:
        self.bus = EventBus()
        self.environment = environment
        self.resolver = resolver
        self.stale_days = stale_days

        self._entries: List[GameEntry] = []
        self._identities: Optional[List[GameIdentity]] = None
        self._non_games: Optional[List[GameEntry]] = None
        self._reports: Dict[str, preparation.PreparationReport] = {}
        self._lock = threading.RLock()

        self.tab: str = "All"
        self.search: str = ""
        self.filter: str = FILTER_ALL
        self.sort: str = SORT_NAME
        self._selection: Set[str] = set()

    # -- the library: the only way in -------------------------------------

    @property
    def entries(self) -> List[GameEntry]:
        return list(self._entries)

    def set_entries(self, entries: Sequence[GameEntry], *,
                    event: str = LIBRARY_CHANGED, payload=None) -> None:
        """Replace the library and invalidate everything derived from it."""
        with self._lock:
            self._entries = list(entries)
            self._invalidate()
        self.bus.publish(LIBRARY_CHANGED, payload)
        if event != LIBRARY_CHANGED:
            self.bus.publish(event, payload)
        self.bus.publish(READINESS_CHANGED, None)
        self._prune_selection()

    def _invalidate(self) -> None:
        self._identities = None
        self._non_games = None
        self._reports.clear()

    def invalidate_readiness(self, keys: Optional[Iterable[str]] = None) -> None:
        """Drop cached verdicts — all of them, or just the ones named."""
        with self._lock:
            if keys is None:
                self._reports.clear()
            else:
                for key in keys:
                    self._reports.pop(key, None)
        self.bus.publish(READINESS_CHANGED, None)

    def source_synced(self, report=None) -> None:
        """The recommendation cache changed: readiness depends on it."""
        with self._lock:
            self._reports.clear()
        self.bus.publish(SOURCE_SYNC_COMPLETED, report)
        self.bus.publish(READINESS_CHANGED, None)

    def scan_completed(self, entries: Sequence[GameEntry], result: ScanResult) -> None:
        """Apply a scan's results and announce it in one step.

        Exists so a caller cannot merge the entries and forget the event, which
        is the shape the stale-list bug took.
        """
        self.set_entries(entries, event=SCAN_COMPLETED, payload=result)

    # -- derived ----------------------------------------------------------

    @property
    def identities(self) -> List[GameIdentity]:
        with self._lock:
            if self._identities is None:
                games, non_games = classification.split_games(self._entries)
                self._identities = identity_mod.build_identities(games)
                self._non_games = non_games
            return list(self._identities)

    @property
    def non_games(self) -> List[GameEntry]:
        self.identities                      # forces the split
        return list(self._non_games or [])

    def report_for(self, identity: GameIdentity) -> preparation.PreparationReport:
        """The readiness verdict for one game, computed once and cached."""
        with self._lock:
            cached = self._reports.get(identity.key)
            if cached is not None:
                return cached
        report = preparation.assess(identity, env=self.environment,
                                    resolver=self.resolver)
        with self._lock:
            self._reports[identity.key] = report
        return report

    def reports(self, identities: Optional[Sequence[GameIdentity]] = None):
        return [self.report_for(i) for i in (identities if identities is not None
                                             else self.identities)]

    def summary(self, identities: Optional[Sequence[GameIdentity]] = None) -> Dict[str, int]:
        return preparation.summarise(self.reports(identities))

    # -- the current view --------------------------------------------------

    def set_view(self, *, tab: Optional[str] = None, search: Optional[str] = None,
                 filter: Optional[str] = None, sort: Optional[str] = None) -> None:
        """Change tab/search/filter/sort and publish once."""
        changed = False
        if tab is not None and tab != self.tab:
            self.tab, changed = tab, True
        if search is not None and search != self.search:
            self.search, changed = search, True
        if filter is not None and filter != self.filter:
            self.filter, changed = filter, True
        if sort is not None and sort != self.sort:
            self.sort, changed = sort, True
        if changed:
            self.bus.publish(FILTER_CHANGED, None)
            self._prune_selection()

    def visible(self) -> List[GameIdentity]:
        """The games the current tab, search and filter admit, sorted."""
        rows = self.identities
        if self.tab != "All":
            rows = [i for i in rows
                    if any(readiness.tab_for_launcher(l) == self.tab
                           for l in i.launchers)]
        needle = self.search.strip().lower()
        if needle:
            rows = [i for i in rows
                    if needle in i.canonical_title.lower()
                    or any(needle in a.lower() for a in i.aliases)]
        rows = [i for i in rows if self._passes_filter(i)]
        return self._sorted(rows)

    def _passes_filter(self, identity: GameIdentity) -> bool:
        if self.filter == FILTER_ALL:
            return True
        report = self.report_for(identity)
        wanted = _FILTER_TO_READINESS.get(self.filter)
        if wanted is not None:
            return report.readiness == wanted
        if self.filter == FILTER_WITH_SETTINGS:
            return report.settings_state == preparation.PASS
        if self.filter == FILTER_WITHOUT_SETTINGS:
            return report.settings_state != preparation.PASS
        return True

    def _sorted(self, rows: Sequence[GameIdentity]) -> List[GameIdentity]:
        if self.sort == SORT_LAUNCHER:
            return sorted(rows, key=lambda i: (i.launchers[0] if i.launchers else "",
                                               i.canonical_title.lower()))
        if self.sort == SORT_READINESS:
            order = {v: n for n, v in enumerate(preparation.READINESS_ORDER)}
            return sorted(rows, key=lambda i: (order.get(self.report_for(i).readiness, 9),
                                               i.canonical_title.lower()))
        if self.sort == SORT_SETTINGS:
            rank = {preparation.PASS: 0, preparation.WARN: 1, preparation.UNKNOWN: 2}
            return sorted(rows, key=lambda i: (rank.get(self.report_for(i).settings_state, 3),
                                               i.canonical_title.lower()))
        if self.sort == SORT_LAST_TESTED:
            def age(i: GameIdentity):
                installation = i.best_installation()
                days = (readiness.readiness_age_days(installation.entry)
                        if installation else None)
                return (days is None, days or 0.0, i.canonical_title.lower())
            return sorted(rows, key=age)
        return sorted(rows, key=lambda i: i.canonical_title.lower())

    # -- selection ---------------------------------------------------------

    @property
    def selection(self) -> Set[str]:
        return set(self._selection)

    @property
    def selection_count(self) -> int:
        return len(self._selection)

    def selected_identities(self) -> List[GameIdentity]:
        index = {i.key: i for i in self.identities}
        return [index[k] for k in self._selection if k in index]

    def set_selection(self, keys: Iterable[str]) -> None:
        known = {i.key for i in self.identities}
        new = {k for k in keys if k in known}
        if new != self._selection:
            self._selection = new
            self.bus.publish(SELECTION_CHANGED, self.selection_count)

    def select_all_visible(self) -> None:
        self.set_selection({i.key for i in self.visible()})

    def select_none(self) -> None:
        self.set_selection(set())

    def invert_selection(self) -> None:
        visible = {i.key for i in self.visible()}
        self.set_selection((self._selection ^ visible) & visible)

    def select_where(self, predicate: Callable[[GameIdentity], bool]) -> None:
        """Select the visible games matching ``predicate`` — respects the tab."""
        self.set_selection({i.key for i in self.visible() if predicate(i)})

    def select_by_readiness(self, verdict: str) -> None:
        self.select_where(lambda i: self.report_for(i).readiness == verdict)

    def select_with_settings(self, present: bool = True) -> None:
        self.select_where(
            lambda i: (self.report_for(i).settings_state == preparation.PASS) is present)

    def _prune_selection(self) -> None:
        """Drop selected games that no longer exist, quietly and safely."""
        known = {i.key for i in self.identities}
        pruned = self._selection & known
        if pruned != self._selection:
            self._selection = pruned
            self.bus.publish(SELECTION_CHANGED, self.selection_count)

    # -- status ------------------------------------------------------------

    def status(self, message: str) -> None:
        self.bus.publish(STATUS_CHANGED, message)
