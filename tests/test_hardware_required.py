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
