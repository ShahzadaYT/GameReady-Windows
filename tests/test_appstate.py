"""Central state model: derived data must never outlive the data it came from.

The bug these pin: the game tree rendered from ``identities``, a value derived
from ``entries``, and a scan replaced ``entries`` without recomputing it. Newly
scanned games appeared only after restarting the application.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from travelready import preparation
from travelready.appstate import (
    FILTER_ALL, FILTER_READY, FILTER_WITH_SETTINGS, LIBRARY_CHANGED,
    READINESS_CHANGED, SCAN_COMPLETED, SELECTION_CHANGED, SORT_LAUNCHER,
    SORT_NAME, AppState, EventBus, ScanResult,
)
from travelready.environment import offline_environment
from travelready.library import GameEntry

FIXTURE = Path(__file__).parent / "fixtures" / "games_2026-09-26.json"


def entry(name: str, launcher: str = "Steam", **kw) -> GameEntry:
    return GameEntry(name=name, launcher=launcher, **kw)


@pytest.fixture
def state() -> AppState:
    return AppState(environment=offline_environment())


# -- the regression ---------------------------------------------------------

def test_scanning_makes_new_games_visible_without_a_restart(state):
    """Regression: a scan's games must appear immediately.

    Previously ``_on_scanned`` merged into ``entries`` and repainted from a
    stale ``identities`` list, so the new games were invisible until the
    application was restarted.
    """
    state.set_entries([entry("Hades")])
    assert [i.canonical_title for i in state.identities] == ["Hades"]

    state.scan_completed([entry("Hades"), entry("Celeste")],
                         ScanResult(found=2, added=1))

    titles = [i.canonical_title for i in state.identities]
    assert "Celeste" in titles, "a scanned game must be visible at once"
    assert len(state.visible()) == 2


def test_replacing_the_library_invalidates_derived_state(state):
    """Any path that changes entries drops identities, not just the scan path."""
    state.set_entries([entry("Hades")])
    state.identities                                   # populate the cache
    state.set_entries([entry("Bastion")])
    assert [i.canonical_title for i in state.identities] == ["Bastion"]


def test_derived_state_cannot_be_left_stale_by_a_new_code_path(state):
    """The model, not the caller, owns invalidation.

    A future handler that forgets to 'refresh' still cannot show stale data,
    because the only way to change entries goes through set_entries.
    """
    state.set_entries([entry("Hades")])
    state.identities
    # Simulate a careless caller: mutate through the public setter only.
    state.set_entries(state.entries + [entry("Pyre")])
    assert len(state.identities) == 2


def test_readiness_cache_is_dropped_when_the_library_changes(state):
    state.set_entries([entry("Hades")])
    first = state.report_for(state.identities[0])
    assert state.report_for(state.identities[0]) is first        # cached
    state.set_entries([entry("Hades")])
    assert state.report_for(state.identities[0]) is not first    # recomputed


def test_a_source_sync_invalidates_readiness_but_keeps_the_library(state):
    """Regression: a successful sync must not empty the game list.

    A bad search-and-replace once spliced ``self.identities = []`` from
    __init__ into the sync handler, so a successful update cleared the library.
    """
    state.set_entries([entry("Hades"), entry("Celeste")])
    report = state.report_for(state.identities[0])
    state.source_synced(report=None)
    assert len(state.identities) == 2, "a sync must never empty the library"
    assert state.report_for(state.identities[0]) is not report


# -- events -----------------------------------------------------------------

def test_scan_publishes_both_library_changed_and_scan_completed(state):
    seen = []
    state.bus.subscribe(LIBRARY_CHANGED, lambda p: seen.append("library"))
    state.bus.subscribe(SCAN_COMPLETED, lambda p: seen.append(("scan", p.added)))
    state.scan_completed([entry("Hades")], ScanResult(found=1, added=1))
    assert seen == ["library", ("scan", 1)]


def test_readiness_changed_fires_on_library_and_on_sync(state):
    count = []
    state.bus.subscribe(READINESS_CHANGED, lambda p: count.append(1))
    state.set_entries([entry("Hades")])
    state.source_synced()
    assert len(count) == 2


def test_a_failing_subscriber_does_not_stop_the_others():
    """One broken view must not freeze every other view."""
    bus = EventBus()
    seen = []
    bus.subscribe(LIBRARY_CHANGED, lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(LIBRARY_CHANGED, lambda p: seen.append("ran"))
    bus.publish(LIBRARY_CHANGED, None)
    assert seen == ["ran"]
    assert bus.errors and "boom" in bus.errors[0]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    seen = []
    off = bus.subscribe(LIBRARY_CHANGED, lambda p: seen.append(1))
    off()
    bus.publish(LIBRARY_CHANGED, None)
    assert seen == []


def test_unknown_events_are_rejected_rather_than_silently_ignored():
    with pytest.raises(ValueError):
        EventBus().subscribe("NoSuchEvent", lambda p: None)


# -- selection --------------------------------------------------------------

def test_select_all_respects_the_current_tab(state):
    state.set_entries([entry("Hades", "Steam"), entry("Forza", "Xbox")])
    state.set_view(tab="Steam")
    state.select_all_visible()
    assert state.selection_count == 1
    assert state.selected_identities()[0].canonical_title == "Hades"


def test_select_all_respects_an_active_search(state):
    state.set_entries([entry("Hades"), entry("Celeste"), entry("Hollow Knight")])
    state.set_view(search="hades")
    state.select_all_visible()
    assert state.selection_count == 1


def test_invert_selection_only_touches_visible_games(state):
    state.set_entries([entry("Hades", "Steam"), entry("Celeste", "Steam"),
                       entry("Forza", "Xbox")])
    state.set_view(tab="Steam")
    state.select_all_visible()
    state.set_view(tab="All")
    state.set_view(tab="Steam")
    state.invert_selection()
    assert state.selection_count == 0


def test_selection_survives_a_refresh_of_the_same_games(state):
    state.set_entries([entry("Hades"), entry("Celeste")])
    state.select_all_visible()
    state.set_entries([entry("Hades"), entry("Celeste"), entry("Pyre")])
    assert state.selection_count == 2, "a refresh must not clear the selection"


def test_selection_drops_games_that_no_longer_exist(state):
    state.set_entries([entry("Hades"), entry("Celeste")])
    state.select_all_visible()
    state.set_entries([entry("Hades")])
    assert state.selection == {i.key for i in state.identities}


def test_selection_changes_publish_a_count(state):
    state.set_entries([entry("Hades"), entry("Celeste")])
    seen = []
    state.bus.subscribe(SELECTION_CHANGED, lambda n: seen.append(n))
    state.select_all_visible()
    state.select_none()
    assert seen == [2, 0]


def test_selecting_the_same_set_twice_publishes_once(state):
    state.set_entries([entry("Hades")])
    seen = []
    state.bus.subscribe(SELECTION_CHANGED, lambda n: seen.append(n))
    state.select_all_visible()
    state.select_all_visible()
    assert seen == [1]


# -- filtering and sorting --------------------------------------------------

def test_search_matches_aliases_as_well_as_titles(state):
    state.set_entries([entry("Grand Theft Auto V"), entry("Hades")])
    state.set_view(search="grand theft")
    assert len(state.visible()) == 1


def test_filters_partition_the_library(state):
    entries, _ = _load_fixture()
    state.set_entries(entries)
    state.set_view(filter=FILTER_ALL)
    total = len(state.visible())
    counted = 0
    for verdict in preparation.READINESS_ORDER:
        counted += sum(1 for i in state.identities
                       if state.report_for(i).readiness == verdict)
    assert counted == total


def test_sorting_by_launcher_groups_launchers(state):
    state.set_entries([entry("Zed", "Steam"), entry("Alpha", "Xbox"),
                       entry("Beta", "Steam")])
    state.set_view(sort=SORT_LAUNCHER)
    # Launcher ids are normalised to lower case by the library.
    assert [i.launchers[0] for i in state.visible()] == ["steam", "steam", "xbox"]
    assert [i.canonical_title for i in state.visible()] == ["Beta", "Zed", "Alpha"]


def test_sorting_by_name_is_case_insensitive(state):
    state.set_entries([entry("zed"), entry("Alpha")])
    state.set_view(sort=SORT_NAME)
    assert [i.canonical_title for i in state.visible()] == ["Alpha", "zed"]


def test_changing_the_view_does_not_recompute_readiness(state):
    """Regression: tab switching once cost 149 fuzzy matches on the UI thread."""
    entries, _ = _load_fixture()
    state.set_entries(entries)
    state.set_view(filter=FILTER_READY)
    state.visible()
    calls = []
    original = preparation.assess

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    preparation.assess = counting
    try:
        for _ in range(5):
            state.set_view(tab="Steam")
            state.visible()
            state.set_view(tab="All")
            state.visible()
    finally:
        preparation.assess = original
    assert calls == [], "readiness must come from the cache, not be recomputed"


# -- real data --------------------------------------------------------------

def _load_fixture():
    from travelready.library import load_library
    return load_library(str(FIXTURE))


def test_the_real_library_loads_into_the_model():
    entries, _ = _load_fixture()
    state = AppState(environment=offline_environment())
    state.set_entries(entries)
    assert len(state.identities) > 100
    assert state.non_games, "launcher software must still be separated out"
    assert state.summary()
