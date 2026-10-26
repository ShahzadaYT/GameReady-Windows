"""Behaviour that can only be validated on a real Windows / ROG Ally device.

These tests are collected and skipped so the boundary between what is
code-verified and what still needs hardware validation is explicit and visible
in the test report, rather than buried in a document.

Run them on the device with:  pytest -m hardware --run-hardware
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.hardware

WINDOWS = sys.platform.startswith("win")
requires_windows = pytest.mark.skipif(not WINDOWS, reason="requires real Windows")


@requires_windows
def test_shell_appsfolder_launch_starts_an_xbox_game():
    """REQUIRES HARDWARE: a real shell:AppsFolder activation."""
    pytest.skip("Run manually on the ROG Ally with a known Game Pass title installed.")


@requires_windows
def test_get_appx_package_manifest_returns_an_executable():
    """REQUIRES HARDWARE: Get-AppxPackageManifest output shape."""
    from travelready.discovery import store_executables, store_package_families

    families = store_package_families()
    assert families, "no Store-signed packages found"
    exes = store_executables(families[:5])
    assert isinstance(exes, dict)


@requires_windows
def test_process_table_reads_real_processes():
    """REQUIRES HARDWARE: tasklist / Win32_Process output shape."""
    from travelready.processes import WindowsProcessTable

    rows = WindowsProcessTable().snapshot()
    assert rows and any(r.name.lower() == "explorer.exe" for r in rows)


@requires_windows
def test_ea_app_handoff_timing():
    """REQUIRES HARDWARE: real EA App startup and game hand-off."""
    pytest.skip("Run manually with EA App installed and signed in.")


@requires_windows
def test_applying_settings_to_a_real_game_config():
    """REQUIRES HARDWARE: a real game's GameUserSettings.ini."""
    pytest.skip("Run manually against an installed Unreal Engine game.")


# -- ROG Ally Life, live network ---------------------------------------------

def test_rogallylife_is_reachable():
    """REQUIRES NETWORK: rogallylife.com is blocked by the dev environment.

    Run this on a machine that can reach the site to confirm the client works
    against the real host before trusting a sync.
    """
    from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient

    client = RogAllyLifeClient()
    try:
        response = client.get("")
    except FetchError as exc:
        pytest.skip(f"rogallylife.com unreachable from here: {exc}")
    assert response.status == 200
    assert "ally" in response.body.lower()


def test_rogallylife_rest_api_is_available():
    """REQUIRES NETWORK: confirms the WP REST API, the preferred route."""
    from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient

    client = RogAllyLifeClient()
    try:
        client.get("")           # prove the host is reachable first
    except FetchError as exc:
        pytest.skip(f"rogallylife.com unreachable from here: {exc}")
    if not client.rest_available():
        pytest.skip("REST API not exposed; sync will fall back to sitemap/HTML")
    posts = client.list_posts(per_page=5, max_pages=1)
    assert posts, "no settings posts returned"
    assert all("link" in p for p in posts)


def test_rogallylife_robots_is_honoured_against_the_real_file():
    """REQUIRES NETWORK: parse the site's real robots.txt."""
    from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient

    client = RogAllyLifeClient()
    policy = client.robots()
    if not policy.fetched:
        # robots() falls back to a permissive policy when the file cannot be
        # fetched. Asserting against that would be a test that always passes.
        pytest.skip("robots.txt could not be fetched from here")
    assert policy.allows("https://rogallylife.com/2026/02/27/a-rog-ally-game-settings/")


def test_a_real_post_parses_into_profiles():
    """REQUIRES NETWORK: the parser against real markup, not a fixture.

    This is the test that would catch the site changing its layout.
    """
    from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient
    from travelready.optimiser.rogallylife.known_urls import seed_urls
    from travelready.optimiser.rogallylife.parser import parse_post

    client = RogAllyLifeClient()
    url = seed_urls("rog_ally_family")[0]
    try:
        response = client.get(url)
    except FetchError as exc:
        pytest.skip(f"unreachable: {exc}")
    game = parse_post(response.body, url=url)
    assert game.title
    assert game.profiles, "the real page yielded no profiles - the parser needs updating"
    assert any(s.canonical for p in game.profiles for s in p.settings), \
        "no setting label was recognised - check capability.LABEL_ALIASES"


def test_full_sync_against_the_real_site(tmp_path):
    """REQUIRES NETWORK: a real end-to-end refresh into a throwaway cache."""
    from travelready.optimiser.rogallylife.cache import ProfileCache
    from travelready.optimiser.rogallylife.client import RogAllyLifeClient
    from travelready.optimiser.rogallylife import sync as ral_sync

    cache = ProfileCache(tmp_path)
    report = ral_sync.sync(RogAllyLifeClient(), cache, limit=5)
    if report.blocked:
        pytest.skip("rogallylife.com unreachable from here")
    assert report.discovered > 0
    assert not report.errors, report.errors
    assert cache.all_games()
