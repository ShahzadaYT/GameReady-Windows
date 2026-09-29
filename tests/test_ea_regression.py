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
