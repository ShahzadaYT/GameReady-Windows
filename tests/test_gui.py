"""GUI smoke tests.

Skipped where tkinter or a display is unavailable. These do not test look and
feel; they assert that the window builds against the real shipped library and
that the launcher tabs, filtering, dashboard and settings panel work.
"""
from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURES

tk = pytest.importorskip("tkinter")


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


def test_window_builds_with_the_real_library(app):
    assert len(app.entries) == 162
    assert len(app.tree.get_children()) == 162


def test_launcher_tabs_show_live_counts(app):
    labels = [app.notebook.tab(i, "text") for i in range(len(app.notebook.tabs()))]
    assert "All (162)" in labels
    assert "Xbox (49)" in labels
    assert "EA (13)" in labels
    assert "Steam (44)" in labels


def test_switching_tab_filters_rows_without_losing_games(app):
    app.notebook.select(1)                       # Xbox
    app.root.update()
    assert app.current_tab == "Xbox"
    assert len(app.tree.get_children()) == 49
    assert len(app.entries) == 162, "filtering must not drop library entries"


def test_dashboard_summarises_readiness(app):
    text = app.dashboard_label.cget("text")
    assert "READY 43" in text
    assert "Needs attention" in text


def test_selecting_a_game_shows_whether_it_can_be_verified(app):
    app.notebook.select(2)                       # EA
    app.root.update()
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    detail = app.detail_text.get("1.0", "end")
    assert "Launcher" in detail and "Expected process" in detail
    assert ("cannot be verified automatically" in detail
            or "Can be verified automatically" in detail)


def test_settings_panel_reports_a_missing_profile_without_inventing_one(app):
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    app._on_review_settings()
    app.root.update()
    text = app.settings_text.get("1.0", "end")
    assert "NOT FOUND" in text
    assert "rogallylife.com" in text
    assert str(app.apply_button.cget("state")) == "disabled"


def test_apply_button_stays_disabled_without_a_plan(app):
    assert str(app.apply_button.cget("state")) == "disabled"


# -- ROG Ally Life panel -----------------------------------------------------

def test_settings_column_is_present_and_does_not_gate_launch(app):
    assert "settings" in app.tree["columns"]
    rows = app.tree.get_children()
    values = app.tree.item(rows[0], "values")
    # readiness and settings are separate columns; neither derives from the other
    assert len(values) == len(app.tree["columns"])
    assert values[3] in ("Ready", "Review", "Manual", "No profile", "Not checked")


def test_source_label_reports_an_empty_cache_honestly(app):
    text = app.source_label.cget("text")
    assert "ROG Ally Life" in text
    assert "0 games" in text and "never synced" in text


def test_operating_mode_defaults_to_balanced_and_persists(app):
    from travelready.optimiser.rogallylife.select import MODE_BALANCED, MODE_BATTERY

    assert app.mode_var.get() == MODE_BALANCED
    app.mode_var.set(MODE_BATTERY)
    app._on_mode_changed()
    assert app.settings.operating_mode == MODE_BATTERY
    assert app.resolver.mode == MODE_BATTERY


def test_view_source_is_disabled_until_a_profile_is_loaded(app):
    assert str(app.view_source_button.cget("state")) == "disabled"


def test_review_with_an_empty_cache_offers_no_profile_and_no_invention(app):
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    app._on_review_settings()
    app.root.update()
    text = app.settings_text.get("1.0", "end")
    assert "NOT FOUND" in text
    assert "Cached ROG Ally Life games: 0" in text
    assert str(app.apply_button.cget("state")) == "disabled"
