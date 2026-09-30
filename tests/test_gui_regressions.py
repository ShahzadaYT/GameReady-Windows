"""Regressions found by running dist\\TravelReady.exe on a real desktop.

Each test here reproduces a fault a user hit in the built application. They
drive the real window against the real shipped library, because every one of
these bugs was invisible to a test that only checked the startup state.
"""
from __future__ import annotations

import shutil
import threading

import pytest

from conftest import FIXTURES

tk = pytest.importorskip("tkinter")


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    """Record message boxes instead of showing them.

    Every one of these is modal: without this, a test that triggers one waits
    forever for a click that never comes.
    """
    import travelready.gui_app as gui_app

    shown = {"info": [], "warning": [], "error": []}
    for kind, name in (("info", "showinfo"), ("warning", "showwarning"),
                       ("error", "showerror")):
        monkeypatch.setattr(
            gui_app.messagebox, name,
            lambda title, message, _k=kind: shown[_k].append((title, message)))
    return shown


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path))
    shutil.copy(FIXTURES / "games_2026-09-26.json", tmp_path / "games.json")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    from travelready.gui_app import TravelReadyGUI

    gui = TravelReadyGUI(root)
    root.update()
    yield gui
    root.destroy()


def entry(name: str, launcher: str = "Steam"):
    from travelready.library import GameEntry
    return GameEntry(name=name, launcher=launcher)


# -- "scanning does not refresh the list" -----------------------------------

def titles(app) -> set:
    """Game titles as shown — the name lives in the tree column, not values."""
    return {app.tree.item(i, "text") for i in app.tree.get_children()}


def test_a_scan_puts_new_games_in_the_tree_without_a_restart(app):
    """Reported: scanned games appeared only after restarting the EXE."""
    before = len(app.tree.get_children())
    assert "Zzz Brand New Game" not in titles(app)

    app._on_scanned(list(app.entries) + [entry("Zzz Brand New Game")])
    app.root.update()

    assert "Zzz Brand New Game" in titles(app)
    assert len(app.tree.get_children()) == before + 1


def test_a_scan_updates_the_tab_counts_too(app):
    """Every view derives from the model, so none of them can lag behind."""
    from travelready import readiness

    steam = readiness.TAB_ORDER.index("Steam")

    def steam_tab() -> str:
        return app.notebook.tab(steam, "text")

    before = steam_tab()
    app._on_scanned(list(app.entries) + [entry("Zzz Brand New Game", "steam")])
    app.root.update()
    assert steam_tab() != before, "the tab count must move with the library"


def test_a_scan_that_finds_nothing_loses_no_game(app):
    """An empty scan may tidy duplicates, but must never drop a game."""
    before = titles(app)
    paths = {e.exe_path for e in app.entries if e.exe_path}

    app._on_scanned([])
    app.root.update()

    assert titles(app) == before, "no game may disappear because a scan found nothing"
    # Folding a duplicate pair moves the executable onto the surviving entry;
    # it must never drop off the library altogether.
    after = {e.exe_path for e in app.entries if e.exe_path}
    assert paths <= after, f"lost executable paths: {sorted(paths - after)}"


# -- "a successful sync emptied the game list" ------------------------------

def test_a_successful_sync_does_not_empty_the_game_list(app):
    """Regression: __init__ lines spliced into the sync handler cleared it.

    The handler contained `self.identities = []`, copied in by a careless
    search-and-replace, so finishing an update wiped every game from the view.
    """
    from travelready.optimiser.rogallylife.sync import SyncReport

    before = len(app.tree.get_children())
    assert before > 0

    app._on_source_synced(SyncReport(discovered=3, fetched=3))
    app.root.update()

    assert len(app.identities) > 0, "a sync must never empty the library"
    assert len(app.tree.get_children()) == before


def test_the_sync_handler_does_not_reassign_core_state(app):
    """The handler must touch what depends on the source, and nothing else."""
    import ast
    import inspect
    from travelready.gui_app import TravelReadyGUI

    import textwrap
    source = textwrap.dedent(inspect.getsource(TravelReadyGUI._on_source_synced))
    tree = ast.parse(source)
    assigned = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                assigned.add(target.attr)

    assert "identities" not in assigned, "the sync handler must not clear the games"
    assert "entries" not in assigned, "a sync must not replace the library"


def test_a_cancelled_sync_is_not_reported_as_success(app):
    from travelready.optimiser.rogallylife.sync import SyncReport

    report = SyncReport(discovered=10, fetched=2, skipped=8,
                        cancelled=True, duration=3.0)
    app._on_source_synced(report)
    app.root.update()
    status = app.status_label.cget("text")
    assert "cancelled" in status.lower()
    assert len(app.identities) > 0


def test_an_unreachable_source_never_reads_as_no_recommendations(app, dialogs):
    """The project's oldest rule: 'cannot determine' must not become 'no'."""
    from travelready.optimiser.rogallylife.sync import SyncReport

    app._on_source_synced(SyncReport(blocked=True, errors=["403"]))
    app.root.update()

    assert dialogs["warning"], "an unreachable source must be reported, not hidden"
    status = app.status_label.cget("text").lower()
    assert "unreachable" in status or "cached" in status
    assert "no recommendation" not in status


def test_a_failed_sync_shows_an_error_and_keeps_cached_data(app, dialogs):
    """Regression: the exception was logged and the status said 'Update failed'
    with no indication that cached recommendations still worked."""
    app._on_source_failed(OSError("connection reset by peer"))
    app.root.update()

    assert dialogs["error"], "a failure must be shown to the user"
    text = dialogs["error"][0][1].lower()
    assert "connection reset" in text, "the real error must be visible"
    assert "cached" in text, "the user must be told cached data still works"
    assert len(app.identities) > 0


# -- "Cancel does nothing" --------------------------------------------------

def test_the_update_worker_passes_the_cancel_event_to_sync(app, monkeypatch):
    """Regression: Cancel set an event that sync() never looked at."""
    import travelready.gui_app as gui_app

    captured = {}

    def fake_sync(client, cache, **kwargs):
        captured.update(kwargs)
        from travelready.optimiser.rogallylife.sync import SyncReport
        return SyncReport()

    monkeypatch.setattr(gui_app.ral_sync, "sync", fake_sync)
    app._on_update_source()
    if app.worker:
        app.worker.join(timeout=5)

    assert "stop_event" in captured, "sync must receive the GUI's cancel event"
    assert captured["stop_event"] is app.cancel_event


def test_cancel_sets_the_event_the_worker_watches(app):
    app.cancel_event.clear()
    app._on_cancel()
    assert app.cancel_event.is_set()


# -- the model behind the views ---------------------------------------------

def test_the_window_keeps_no_second_copy_of_the_library(app):
    """Views read through the model; a private copy is how state goes stale."""
    assert app.entries is not app.state._entries
    assert len(app.entries) == len(app.state.entries)
    app.state.set_entries([entry("Only One")])
    app.root.update()
    assert len(app.entries) == 1
    assert len(app.tree.get_children()) == 1


def test_changing_the_library_repaints_without_an_explicit_refresh_call(app):
    """Events drive repaints, so a new handler cannot forget to refresh."""
    app.state.set_entries([entry("Solo Game")])
    app.root.update()
    assert titles(app) == {"Solo Game"}
