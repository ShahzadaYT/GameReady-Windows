"""Readiness state, tabs and Prepare-for-Travel selection tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FIXTURES
from travelready import readiness as R
from travelready.library import GameEntry, load_library

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _entry(**kw):
    return GameEntry(**{"name": "G", "launcher": "steam", **kw})


def test_untested_when_never_run():
    assert R.state_of(_entry(), now=NOW) == R.STATE_UNTESTED


def test_ready_when_recently_passed():
    entry = _entry(last_result="PASS",
                   last_ready=(NOW - timedelta(days=2)).isoformat())
    assert R.state_of(entry, now=NOW) == R.STATE_READY


def test_stale_when_the_pass_is_old():
    entry = _entry(last_result="PASS",
                   last_ready=(NOW - timedelta(days=30)).isoformat())
    assert R.state_of(entry, stale_days=7, now=NOW) == R.STATE_STALE


@pytest.mark.parametrize("status", ["FAIL", "TIMEOUT", "NOT_FOUND", "ACCESS_DENIED"])
def test_failures_need_attention(status):
    assert R.state_of(_entry(last_result=status), now=NOW) == R.STATE_ATTENTION


@pytest.mark.parametrize("status", ["MANUAL", "UNKNOWN", "MANUAL_REQUIRED"])
def test_manual_results_are_their_own_state(status):
    assert R.state_of(_entry(last_result=status), now=NOW) == R.STATE_MANUAL


@pytest.mark.parametrize("launcher,tab", [
    ("xbox", "Xbox"), ("ea", "EA"), ("ubisoft", "Ubisoft"), ("epic", "Epic"),
    ("steam", "Steam"), ("gog", "GOG"), ("battlenet", "Battle.net"),
    ("other", "Other"), ("", "Other"),
])
def test_tab_mapping(launcher, tab):
    assert R.tab_for_launcher(launcher) == tab


def test_tab_counts_against_the_real_library():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    counts = R.tab_counts(entries)
    assert counts["All"] == 162
    assert counts["Xbox"] == 49 and counts["EA"] == 13 and counts["Steam"] == 44
    assert sum(counts[t] for t in R.TAB_ORDER if t != "All") == counts["All"]


def test_prepare_skips_only_what_cannot_be_closed():
    closable = _entry(name="Closable", expected_process="g.exe")
    unclosable = _entry(name="Blind", launch_method="uri",
                        launch_target="link2ea://launch/1")
    targets, skipped = R.prepare_targets([closable, unclosable], now=NOW)
    assert closable in targets
    assert unclosable in skipped


def test_prepare_skips_games_that_are_already_ready():
    ready = _entry(name="Ready", expected_process="g.exe", last_result="PASS",
                   last_ready=(NOW - timedelta(days=1)).isoformat())
    targets, _ = R.prepare_targets([ready], now=NOW)
    assert targets == []
    targets, _ = R.prepare_targets([ready], reverify_ready=True, now=NOW)
    assert targets == [ready]


def test_prepare_does_not_exclude_by_launcher():
    """The regression: Xbox used to be filtered out of Prepare for Travel."""
    xbox = _entry(name="Xbox game", launcher="xbox", expected_process="g.exe",
                  launch_method="shell", launch_target="shell:AppsFolder\\P.G_x!App")
    targets, skipped = R.prepare_targets([xbox], now=NOW)
    assert xbox in targets and xbox not in skipped


def test_travel_ready_requires_no_attention_and_nothing_untested():
    ok = _entry(last_result="PASS", last_ready=NOW.isoformat())
    bad = _entry(name="B", last_result="FAIL")
    assert R.is_travel_ready([ok], now=NOW)
    assert not R.is_travel_ready([ok, bad], now=NOW)
    assert not R.is_travel_ready([ok, _entry(name="C")], now=NOW)


def test_xbox_manual_note_is_about_the_entry_not_the_platform():
    """The old blanket claim justified the regression; it must not return."""
    note = R.XBOX_MANUAL_NOTE.lower()
    assert "cannot be automatically process-verified" not in note
    assert "this store entry" in note or "this" in note


# -- settings state must not turn a network problem into a permanent answer ---

def test_settings_state_without_a_resolver_is_not_checked():
    assert R.settings_state_of(_entry()) == R.SETTINGS_UNKNOWN


def test_an_unsynced_cache_reports_not_checked_not_no_profile(tmp_path):
    from travelready.optimiser.rogallylife.bridge import SourceResolver
    from travelready.optimiser.rogallylife.cache import ProfileCache

    resolver = SourceResolver(ProfileCache(tmp_path))
    assert R.settings_state_of(_entry(), resolver) == R.SETTINGS_UNKNOWN
    assert R.settings_state_of(_entry(), resolver) != R.SETTINGS_NONE


def test_a_synced_cache_with_no_match_reports_no_profile(tmp_path):
    from travelready.optimiser.rogallylife.bridge import SourceResolver
    from travelready.optimiser.rogallylife.cache import ProfileCache
    from travelready.optimiser.rogallylife.model import (
        SourceGame, SourceProfile, SourceSetting,
    )

    cache = ProfileCache(tmp_path)
    cache.put(SourceGame(
        title="Some Other Game",
        source_url="https://rogallylife.com/2026/01/01/other-rog-ally/",
        device_family="rog_ally_family", slug="other-rog-ally",
        profiles=[SourceProfile(name="900P 18W", settings=[
            SourceSetting("Texture Quality", "Low", "texture_quality")])]))
    cache.save_index()
    resolver = SourceResolver(cache)
    assert R.settings_state_of(_entry(name="Nothing Like That"),
                               resolver) == R.SETTINGS_NONE
