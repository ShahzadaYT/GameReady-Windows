"""Xbox launch regression tests.

Pins the regression found in the shipped builds: the previous build launched
Xbox games automatically and verified them by a process name resolved from the
package manifest; the current build deleted that resolution, forced every Xbox
entry to ``mode='manual'``, and filters Xbox out of Prepare-for-Travel
entirely. See docs/ENGINEERING_ASSESSMENT.md §1.

The rule these tests enforce: **launch and verification are separate
capabilities.** Xbox games are always launched through the official
``shell:AppsFolder\\<AppID>`` route; only verification may fall back to manual.
"""
from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from helpers import VirtualClock, launcher_started
from travelready import discovery as d
from travelready import launch_tester as lt
from travelready.library import (
    VERIFY_AUTO, VERIFY_MANUAL, GameEntry, identity_key, load_library,
)
from travelready.processes import FakeProcessTable, ProcInfo

APPID = "KeplerInteractive.Expedition33_ymj30pw6xe604!Game"
XBOX_EXE = r"C:\XboxGames\Clair Obscur- Expedition 33\Content\Sandfall\Binaries\WinGDK\SandFall-WinGDK-Shipping.exe"
XBOX_DIR = r"C:\XboxGames\Clair Obscur- Expedition 33\Content\Sandfall\Binaries\WinGDK"


# -- the defect, stated against the shipped library --------------------------

def test_shipped_library_xbox_entries_are_all_unverifiable_and_manual():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    shell_entries = [e for e in entries
                     if e.launcher == "xbox" and e.launch_target.lower().startswith("shell:")]
    assert shell_entries
    assert all(not e.expected_process for e in shell_entries)
    assert all(e.mode == "manual" for e in shell_entries)
    assert all(e.last_result == "" for e in shell_entries), "none was ever tested"


def test_shipped_library_contains_both_halves_of_the_same_xbox_game():
    """The launchable half and the verifiable half exist but are never joined."""
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    xbox = [e for e in entries if e.launcher == "xbox"]
    by_identity = {}
    for e in xbox:
        by_identity.setdefault(e.identity, []).append(e)
    split = {k: v for k, v in by_identity.items() if len(v) > 1}
    assert split, "expected duplicate Xbox entries in the shipped library"
    # at least one pair is 'launchable but blind' + 'verifiable but unlaunchable'
    joinable = [
        v for v in split.values()
        if any(e.launch_target.lower().startswith("shell:") and not e.expected_process for e in v)
        and any(e.expected_process and not e.launch_target for e in v)
    ]
    assert joinable, "expected a shell-launch entry paired with an XboxGames exe entry"


# -- the fix -----------------------------------------------------------------

def test_merge_joins_launch_route_with_verification_data():
    blind = d.make_xbox_entry("Clair Obscur: Expedition 33", APPID)
    verifiable = GameEntry(
        name="Clair Obscur- Expedition 33", launcher="xbox", source="folder",
        exe_path=XBOX_EXE, install_dir=XBOX_DIR, launch_method="exe",
        expected_process="SandFall-WinGDK-Shipping.exe",
    )
    merged = d.merge_entries([blind, verifiable])
    assert len(merged) == 1
    entry = merged[0]
    assert entry.launch_target == f"shell:AppsFolder\\{APPID}", "official launch route kept"
    assert entry.expected_process == "SandFall-WinGDK-Shipping.exe"
    assert entry.install_dir == XBOX_DIR
    assert entry.verification == VERIFY_AUTO
    assert entry.mode == "standard", "no longer forced to manual"


def test_merge_never_joins_two_different_games():
    a = d.make_xbox_entry("DOOM + DOOM II", "BethesdaSoftworks.Osiris2.0_3275kfvn8vcwc!Game")
    b = d.make_xbox_entry("DOOM: The Dark Ages", "BethesdaSoftworks.ProjectTitan_3275kfvn8vcwc!Game")
    assert len(d.merge_entries([a, b])) == 2


def test_manifest_resolution_makes_an_entry_automatic():
    """Restores store_executables(), deleted from the current build."""
    exes = d.parse_store_executables(
        "KeplerInteractive.Expedition33_ymj30pw6xe604|Sandfall\\Binaries\\WinGDK\\SandFall-WinGDK-Shipping.exe\n"
        "Microsoft.WindowsCalculator_8wekyb3d8bbwe|Calculator.exe\n"
        "Broken.Package_xxx|not-an-exe\n"
    )
    assert exes["KeplerInteractive.Expedition33_ymj30pw6xe604"] == "SandFall-WinGDK-Shipping.exe"
    assert "Broken.Package_xxx" not in exes
    entry = d.make_xbox_entry("Clair Obscur: Expedition 33", APPID,
                              exes["KeplerInteractive.Expedition33_ymj30pw6xe604"])
    assert entry.verification == VERIFY_AUTO
    assert entry.mode == "standard"


def test_entry_without_a_resolvable_process_stays_honestly_manual():
    entry = d.make_xbox_entry("Some Store Game", "Pub.Game_abc!App")
    assert entry.verification == VERIFY_MANUAL
    assert entry.mode == "manual"
    # but it is still launchable through the official route
    assert entry.launch_target == "shell:AppsFolder\\Pub.Game_abc!App"
    assert lt.build_command(entry) == ["shell:AppsFolder\\Pub.Game_abc!App"]


def test_xbox_game_is_launched_and_verified_automatically():
    """The regression itself: an Xbox game must actually start and be verified."""
    entry = d.make_xbox_entry("Clair Obscur: Expedition 33", APPID,
                              "SandFall-WinGDK-Shipping.exe", install_dir=XBOX_DIR)
    table = FakeProcessTable()
    table.add_at(8, ProcInfo(7100, "SandFall-WinGDK-Shipping.exe", XBOX_EXE, 4))
    clock = VirtualClock(table)
    result = lt.run_test(entry, table=table, clock=clock.time, sleep=clock.sleep,
                         starter=launcher_started())
    assert result.process_created is True, "the game must be launched, not skipped"
    assert result.status == lt.STATUS_PASS
    assert result.verified_by == lt.VERIFIED_BY_NAME
    assert result.pid == 7100


def test_xbox_launch_uses_only_the_supported_shell_route():
    """No package is read, patched or side-loaded — only the documented route."""
    entry = d.make_xbox_entry("Game", APPID, "game.exe")
    command = lt.build_command(entry)
    assert command == [f"shell:AppsFolder\\{APPID}"]
    assert entry.launch_method == "shell"


def test_manual_xbox_entry_is_still_launched_before_manual_confirmation():
    """Manual verification must not mean 'do not launch'."""
    entry = d.make_xbox_entry("Unresolvable Game", "Pub.Game_abc!App")
    table = FakeProcessTable()
    clock = VirtualClock(table)
    asked = []
    result = lt.run_test(entry, mode=lt.MODE_MANUAL, table=table,
                         clock=clock.time, sleep=clock.sleep,
                         starter=launcher_started(),
                         manual_confirm=lambda e: asked.append(e) or True)
    assert result.process_created is True
    assert asked, "the user should be asked to confirm, after the game was launched"
    assert result.status == lt.STATUS_PASS
    assert result.verified_by == lt.VERIFIED_MANUAL


def test_prepare_for_travel_does_not_skip_verifiable_xbox_games():
    from travelready import readiness
    verifiable = d.make_xbox_entry("Verifiable", APPID, "game.exe", install_dir=XBOX_DIR)
    blind = d.make_xbox_entry("Blind", "Pub.Game_abc!App")
    targets, skipped = readiness.prepare_targets([verifiable, blind])
    assert verifiable in targets, "an Xbox game with a known process must be prepared"
    assert blind in skipped


# -- discovery quality --------------------------------------------------------

@pytest.mark.parametrize("name,app_id", [
    ("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    ("Notepad", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    ("Windows Security", "Microsoft.SecHealthUI_8wekyb3d8bbwe!SecHealthUI"),
    ("Game Bar", "Microsoft.XboxGamingOverlay_8wekyb3d8bbwe!App"),
    ("Armoury Crate SE", "B9ECED6F.ArmouryCrateSE_qmba6cd70vzyy!App"),
    ("AMD Software", "AdvancedMicroDevicesInc-2.AMDRadeonSoftware_0a9344xs7nr4m!App"),
    ("Copilot", "Microsoft.Copilot_8wekyb3d8bbwe!App"),
    ("MyASUS", "B9ECED6F.ASUSPCAssistant_qmba6cd70vzyy!App"),
    ("Realtek Audio Console", "RealtekSemiconductorCorp.RealtekAudioControl_dt26b99r8h8gj!App"),
])
def test_known_non_games_are_filtered_out(name, app_id):
    assert not d.xbox_app_is_game(name, app_id)


@pytest.mark.parametrize("name,app_id", [
    ("DOOM: The Dark Ages", "BethesdaSoftworks.ProjectTitan_3275kfvn8vcwc!Game"),
    ("Hogwarts Legacy", "WarnerBros.Interactive.PHX_ktmk1x7cxx7trd!Game"),
    ("Forza Horizon 6", "Microsoft.ForteBaseGame_8wekyb3d8bbwe!Game"),
    ("DREDGE", "Team17DigitalLimited.ProjectShoal_j5wdc1q1zdtqa!Game"),
])
def test_real_games_survive_the_filter(name, app_id):
    assert d.xbox_app_is_game(name, app_id)


def test_filter_removes_most_junk_from_the_shipped_library():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    xbox = [e for e in entries
            if e.launcher == "xbox" and e.launch_target.lower().startswith("shell:")]
    app_ids = [e.launch_target.split("\\", 1)[1] for e in xbox]
    kept = [n for n, a in zip([e.name for e in xbox], app_ids) if d.xbox_app_is_game(n, a)]
    junk = {"Calculator", "Notepad", "Paint", "Photos", "Settings", "Terminal",
            "Weather", "Windows Security", "Game Bar", "Copilot", "Clock",
            "Sticky Notes", "Snipping Tool", "Media Player", "Quick Assist",
            "Dolby Access", "Realtek Audio Console", "AMD Software", "MyASUS",
            "Armoury Crate SE", "XBOX", "Get Started", "Recall (preview)",
            "Click to Do", "Windows Back up"}
    assert not (junk & set(kept)), f"junk survived the filter: {junk & set(kept)}"
    assert "DOOM: The Dark Ages" in kept and "Hogwarts Legacy" in kept
