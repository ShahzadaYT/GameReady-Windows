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
    # rows are games, not raw entries: non-games are excluded and duplicate
    # installs collapse into one identity
    assert app.identities
    assert len(app.tree.get_children()) == len(app.identities)
    assert len(app.identities) < 162


def test_launcher_tabs_show_live_counts(app):
    """Counts are games, not raw entries: non-games and duplicates are gone."""
    labels = [app.notebook.tab(i, "text") for i in range(len(app.notebook.tabs()))]
    assert f"All ({len(app.identities)})" in labels
    per_tab = {label.split(" (")[0]: int(label.split("(")[1].rstrip(")"))
               for label in labels if "(" in label}
    assert per_tab["All"] == len(app.identities)
    assert sum(v for k, v in per_tab.items() if k != "All") == per_tab["All"]
    assert per_tab["Steam"] > 0 and per_tab["Xbox"] > 0


def test_switching_tab_filters_rows_without_losing_games(app):
    total = len(app.tree.get_children())
    app.notebook.select(1)                       # Xbox
    app.root.update()
    assert app.current_tab == "Xbox"
    assert 0 < len(app.tree.get_children()) < total
    assert len(app.entries) == 162, "filtering must not drop library entries"


def test_dashboard_summarises_readiness(app):
    assert "game(s)" in app.dashboard_label.cget("text")
    labels = [b.cget("text") for b in app.dashboard_bar.winfo_children()]
    for field in ("Ready:", "Warnings:", "Action required:", "Cannot determine:"):
        assert any(text.startswith(field) for text in labels), field
    assert app.verdict_label.cget("text")


def test_prepare_is_the_primary_action(app):
    assert "Prepare for Travel" in app.prepare_button.cget("text")
    assert str(app.prepare_button.cget("style")) == "Accent.TButton"


def test_readiness_column_shows_a_verdict_not_a_raw_status(app):
    rows = app.tree.get_children()
    verdicts = {app.tree.item(r, "values")[1] for r in rows}
    allowed = {v.replace("_", " ").title() for v in
               __import__("travelready.preparation", fromlist=["x"]).READINESS_ORDER}
    assert verdicts <= allowed, verdicts


def test_selecting_a_game_shows_every_check_and_what_to_do(app):
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    detail = app.detail_text.get("1.0", "end")
    assert "Travel readiness:" in detail
    assert "Installation" in detail
    assert "Launch target" in detail and "Verify by" in detail


def test_launch_and_verification_are_shown_separately(app):
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    detail = app.detail_text.get("1.0", "end")
    assert "Launch target" in detail
    assert "Verify by" in detail, "verification target must be its own line"


def test_readiness_is_cached_rather_than_recomputed_per_repaint(app):
    app.state.invalidate_readiness()
    app._refresh()
    identity = app.identities[0]
    marker = app._report_for(identity)
    app._refresh()
    assert app._report_for(identity) is marker, \
        "an unchanged library must not be re-assessed"


def test_resume_is_disabled_without_a_saved_run(app):
    assert str(app.resume_button.cget("state")) == "disabled"


def test_selecting_a_game_shows_whether_it_can_be_verified(app):
    app.notebook.select(2)                       # EA
    app.root.update()
    rows = app.tree.get_children()
    app.tree.selection_set(rows[0])
    app.root.update()
    detail = app.detail_text.get("1.0", "end")
    assert "Launch can be verified" in detail
    assert "Verify by" in detail


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
    assert values[3] in ("profile", "none", "not checked", "-")


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
