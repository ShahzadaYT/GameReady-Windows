"""EA launch/detection regression tests.

These pin the root cause found in the shipped build: every EA entry in the
supplied library is a ``link2ea://`` URI with an empty ``exe_path`` and an
empty ``expected_process``, so the old detector had no signal at all and always
ran to a 90 s TIMEOUT. See docs/ENGINEERING_ASSESSMENT.md §2.
"""
from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from helpers import VirtualClock, launcher_started
from travelready import launch_tester as lt
from travelready.library import GameEntry, load_library
from travelready.processes import FakeProcessTable, ProcInfo

EA_DIR = r"C:\Program Files\EA Games\Battlefield 3"


def _entry(**kw):
    base = dict(name="Battlefield 3", launcher="ea", launch_method="uri",
                launch_target="link2ea://launch/71067")
    base.update(kw)
    return GameEntry(**base)


def _run(entry, table, **kw):
    clock = VirtualClock(table)
    kw.setdefault("starter", launcher_started())
    return lt.run_test(entry, table=table, clock=clock.time, sleep=clock.sleep, **kw)


# -- the actual shipped data -------------------------------------------------

def test_real_library_ea_entries_have_no_detection_signal():
    """Documents the defect using the user's own games.json."""
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    ea = [e for e in entries if e.launcher == "ea" and e.launch_method == "uri"]
    assert ea, "fixture should contain EA URI entries"
    blind = [e for e in ea if not lt.build_detection_plan(e).has_any_signal]
    assert len(blind) == len(ea), "all shipped EA URI entries are undetectable as stored"


def test_no_signal_is_reported_immediately_not_as_timeout():
    """The engine must say *why* rather than sleep 90s and claim TIMEOUT."""
    table = FakeProcessTable()
    clock = VirtualClock(table)
    result = lt.run_test(_entry(), table=table, clock=clock.time, sleep=clock.sleep,
                         starter=launcher_started())
    assert result.status == lt.STATUS_MANUAL_REQUIRED
    assert result.outcome == lt.OUTCOME_NO_DETECTION_SIGNAL
    assert result.verification_possible is False
    # and it must not have burned the launch timeout
    assert clock.t - 1000.0 < lt.EA_GAME_PHASE_TIMEOUT
    assert "no expected process" in result.failure_hint.lower() or \
           "install folder" in result.failure_hint.lower()


# -- the fix: install-directory detection ------------------------------------

def test_detects_game_by_install_dir_when_process_name_unknown():
    """The capability deleted from the current build, restored."""
    table = FakeProcessTable()
    table.add_at(6, ProcInfo(4100, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    entry = _entry(install_dir=EA_DIR)
    result = _run(entry, table, cleanup=False)
    assert result.status == lt.STATUS_PASS
    assert result.verified_by == lt.VERIFIED_BY_INSTALL_DIR
    assert result.pid == 4100


def test_game_running_under_a_different_executable_is_still_found():
    """'Game started under another executable' — a named EA failure state."""
    table = FakeProcessTable()
    # expected_process is wrong; the real binary has a different name
    table.add_at(5, ProcInfo(4200, "bf3_x64.exe", EA_DIR + r"\bf3_x64.exe", 3000))
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe")
    result = _run(entry, table)
    assert result.status == lt.STATUS_PASS
    assert result.outcome == lt.OUTCOME_WRONG_PROCESS_NAME
    assert result.corrected_process == "bf3_x64.exe"


# -- EA infrastructure must never be adopted as the game ---------------------

def test_ea_app_is_never_mistaken_for_the_game():
    table = FakeProcessTable()
    table.add_at(2, ProcInfo(3000, "EADesktop.exe",
                             r"C:\Program Files\Electronic Arts\EA Desktop\EADesktop.exe", 1))
    table.add_at(3, ProcInfo(3001, "EABackgroundService.exe",
                             r"C:\Program Files\Electronic Arts\EA Desktop\EABackgroundService.exe", 1))
    entry = _entry(install_dir=EA_DIR)
    result = _run(entry, table)
    assert result.launcher_started is True, "EA App presence should be noted"
    assert result.status == lt.STATUS_TIMEOUT
    assert result.outcome == lt.OUTCOME_LAUNCHER_ONLY
    assert result.pid is None


def test_ea_app_never_starting_is_distinct_from_game_never_starting():
    table = FakeProcessTable()
    entry = _entry(install_dir=EA_DIR)
    result = _run(entry, table)
    assert result.outcome == lt.OUTCOME_LAUNCHER_NOT_STARTED
    assert "EA App did not start" in result.failure_hint


# -- other named failure states ---------------------------------------------

def test_game_as_child_process_is_detected():
    table = FakeProcessTable([ProcInfo(900, "cmd.exe", r"C:\Windows\cmd.exe", 1)])
    table.add_at(4, ProcInfo(4300, "bf3.exe", EA_DIR + r"\bf3.exe", 900))
    entry = _entry(launch_method="exe", exe_path=EA_DIR + r"\bf3.exe")
    result = _run(entry, table)
    assert result.status == lt.STATUS_PASS
    assert result.pid == 4300


def test_game_respawn_is_followed():
    table = FakeProcessTable()
    table.add_at(3, ProcInfo(4400, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    table.remove_at(9, 4400)
    table.add_at(10, ProcInfo(4401, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe", validation_time=20)
    result = _run(entry, table)
    assert result.status == lt.STATUS_PASS
    assert result.respawns == 1
    assert result.pid == 4401
    assert result.outcome == lt.OUTCOME_RESPAWNED


def test_game_that_exits_immediately_fails_distinctly():
    table = FakeProcessTable()
    table.add_at(3, ProcInfo(4500, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    table.remove_at(6, 4500)
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe", validation_time=15)
    result = _run(entry, table)
    assert result.status == lt.STATUS_FAIL
    assert result.outcome == lt.OUTCOME_IMMEDIATE_EXIT
    assert "exited immediately" in result.failure_hint


def test_game_starting_after_the_window_is_reported_as_late_not_absent():
    table = FakeProcessTable()
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe")
    table.add_at(26, ProcInfo(4600, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    # explicit override: EA entries otherwise get the 90s EA game-phase window
    result = _run(entry, table, launch_timeout=20)
    assert result.outcome == lt.OUTCOME_LATE_START
    assert result.game_process_detected is True
    assert "increase the launch timeout" in result.failure_hint.lower()


def test_already_running_copy_is_not_mistaken_for_this_launch():
    table = FakeProcessTable([ProcInfo(4700, "bf3.exe", EA_DIR + r"\bf3.exe", 1)])
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe")
    result = _run(entry, table, launch_timeout=10)
    assert result.pid != 4700
    assert result.status == lt.STATUS_TIMEOUT


def test_cleanup_kills_only_what_the_test_started():
    table = FakeProcessTable([ProcInfo(4800, "bf3.exe", EA_DIR + r"\bf3.exe", 1)])
    table.add_at(2, ProcInfo(4801, "bf3.exe", EA_DIR + r"\bf3.exe", 3000))
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe", validation_time=5)
    result = _run(entry, table, cleanup=True)
    assert result.status == lt.STATUS_PASS
    killed = {pid for pid, _ in table.killed}
    assert 4801 in killed, "the launched game should be closed"
    assert 4800 not in killed, "the pre-existing copy must be left alone"


def test_launch_error_is_mapped_not_swallowed():
    from helpers import failing_starter
    table = FakeProcessTable()
    entry = _entry(install_dir=EA_DIR, expected_process="bf3.exe")
    clock = VirtualClock(table)
    result = lt.run_test(entry, table=table, clock=clock.time, sleep=clock.sleep,
                         starter=failing_starter(lt.STATUS_ACCESS_DENIED, "access denied"))
    assert result.status == lt.STATUS_ACCESS_DENIED
    assert "administrator" in result.failure_hint


def test_shortcut_and_uri_launches_never_go_through_cmd():
    """Regression: routing targets via 'cmd /c start' was command-injectable.

    subprocess.list2cmdline only quotes arguments containing whitespace, so a
    target like 'steam://x&payload.exe' reached cmd.exe unquoted and ran a
    second command. These must use ShellExecuteW (os.startfile) instead.
    """
    import os as os_mod
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    opened, spawned = [], []

    class ForbiddenPopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            raise AssertionError(f"a shell launch must not spawn a process: {args}")

    for method, target in [
        ("shortcut", r"C:\Users\S\Desktop\Battlefield 3.lnk"),
        ("uri", "link2ea://launch/71067"),
        ("shell", "shell:AppsFolder\\Pub.Game_abc!App"),
    ]:
        entry = GameEntry(name="G", launcher="ea", launch_method=method,
                          launch_target=target)
        orig_start, orig_popen, orig_win = (
            getattr(os_mod, "startfile", None), sp.Popen, lt_mod.IS_WINDOWS)
        os_mod.startfile = lambda t: opened.append(t)
        sp.Popen, lt_mod.IS_WINDOWS = ForbiddenPopen, True
        try:
            proc, pid, status, err = lt_mod._start_process(entry, lt.build_command(entry))
        finally:
            sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
            if orig_start is None:
                del os_mod.startfile
            else:
                os_mod.startfile = orig_start
        assert status == "", err
        assert pid is None, "a shell activation has no game PID to report"
    assert opened == [r"C:\Users\S\Desktop\Battlefield 3.lnk",
                      "link2ea://launch/71067",
                      "shell:AppsFolder\\Pub.Game_abc!App"]
    assert spawned == []


@pytest.mark.parametrize("target", [
    "&calc.exe",
    "calc.exe",
    "not-a-uri-at-all",
    "steam://x\ncalc.exe",
    "steam://x\x00calc.exe",
    "",
    "   ",
])
def test_structurally_invalid_or_control_bearing_targets_are_refused(target):
    assert lt.validate_launch_target(lt.METHOD_URI, target) != ""


def test_a_shell_method_target_must_be_a_shell_route():
    assert lt.validate_launch_target(lt.METHOD_SHELL, "calc.exe") != ""
    assert lt.validate_launch_target(lt.METHOD_SHELL,
                                     "shell:AppsFolder\\P.G_x!App") == ""


def test_a_shortcut_target_must_be_a_lnk():
    assert lt.validate_launch_target(lt.METHOD_SHORTCUT, r"C:\x\payload.exe") != ""


@pytest.mark.parametrize("method,target", [
    (lt.METHOD_URI, "steam://rungameid/1030840"),
    (lt.METHOD_URI, "link2ea://launch/71067"),
    (lt.METHOD_URI, "com.epicgames.launcher://apps/abc?action=launch&silent=true"),
    (lt.METHOD_SHELL, "shell:AppsFolder\\KeplerInteractive.Expedition33_ym!Game"),
    (lt.METHOD_SHORTCUT, r"C:\Users\S\Desktop\Game.lnk"),
])
def test_real_launch_targets_are_accepted(method, target):
    """Including Epic's, which legitimately contains '&'.

    Safety here comes from not using a command-line parser, not from banning
    characters that a real launcher needs.
    """
    assert lt.validate_launch_target(method, target) == ""


def test_a_malicious_imported_library_cannot_execute_a_command(tmp_path):
    """End to end: import a poisoned library, try to launch it, nothing runs."""
    import subprocess as sp

    import travelready.launch_tester as lt_mod
    from travelready.library import import_games

    poisoned = tmp_path / "library.json"
    poisoned.write_text(json.dumps({"version": "3", "games": [{
        "name": "Free Game", "launcher": "steam", "launch_method": "uri",
        "launch_target": "steam://rungameid/1&C:\\Users\\Public\\payload.exe",
        "expected_process": "game.exe", "install_dir": EA_DIR}]}))
    entries, _ = import_games(poisoned)

    spawned = []

    class ForbiddenPopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            raise AssertionError("nothing should be spawned")

    import os as os_mod

    opened = []
    orig_popen, orig_win = sp.Popen, lt_mod.IS_WINDOWS
    orig_start = getattr(os_mod, "startfile", None)
    os_mod.startfile = lambda t: opened.append(t)
    sp.Popen, lt_mod.IS_WINDOWS = ForbiddenPopen, True
    try:
        proc, pid, status, err = lt_mod._start_process(
            entries[0], lt.build_command(entries[0]))
    finally:
        sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
        if orig_start is None:
            del os_mod.startfile
        else:
            os_mod.startfile = orig_start
    # The payload is handed to ShellExecuteW as one opaque string, where it is
    # simply an unresolvable target — it never becomes a second command.
    assert spawned == [], "no process may be spawned for a URI launch"
    assert opened == ["steam://rungameid/1&C:\\Users\\Public\\payload.exe"]
    assert "cmd" not in str(opened)
