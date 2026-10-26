"""Backup, dry-run, apply, verify and restore tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from travelready.library import GameEntry
from travelready.optimiser import transaction as T
from travelready.optimiser.configio import IniDocument
from travelready.optimiser.diff import build_plan
from travelready.optimiser.inspector import Inspection, read_config_file
from travelready.optimiser.model import (
    BLOCKED, MANUAL, SAFE, ChangePlan, GameProfile, ProposedChange, Recommendation,
    SettingValue,
)
from travelready.processes import FakeProcessTable, ProcInfo

INI = (
    "; TravelReady test config - this comment must survive\r\n"
    "[ScalabilityGroups]\r\n"
    "sg.ShadowQuality=0\r\n"
    "sg.TextureQuality=1\r\n"
    "ModAddedKey=leave me alone\r\n"
    "\r\n"
    "[/Script/Engine.GameUserSettings]\r\n"
    "ResolutionSizeX=1280 ; inline comment\r\n"
    "ResolutionSizeY=720\r\n"
    "FrameRateLimit=45.000000\r\n"
)


@pytest.fixture
def game_config(tmp_path):
    """A real GameUserSettings.ini in a path the safety engine accepts."""
    d = tmp_path / "AppData" / "Local" / "MyGame" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    path = d / "GameUserSettings.ini"
    path.write_bytes(INI.encode("utf-8"))
    return path


@pytest.fixture
def profile():
    return GameProfile(
        game_name="My Game", device="rog_ally_x", source="ROG Ally Life",
        source_url="https://rogallylife.com/my-game", source_date="2026-02-01",
        recommendations=[
            Recommendation("shadow_quality", "Medium"),
            Recommendation("frame_rate_limit", "60"),
            Recommendation("tdp_watts", "20", category="device"),
        ],
    )


def _plan(path, profile, entry):
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(path))])
    return build_plan(entry, profile, inspection)


def _entry():
    return GameEntry(name="My Game", launcher="steam", expected_process="mygame.exe")


def _approve(plan):
    changes = plan.applicable()
    for c in changes:
        c.approved = True
    return changes


# -- plan construction -------------------------------------------------------

def test_plan_separates_safe_from_device_settings(game_config, profile):
    plan = _plan(game_config, profile, _entry())
    assert {c.key for c in plan.safe} == {"shadow_quality", "frame_rate_limit"}
    assert {c.key for c in plan.manual} == {"tdp_watts"}
    assert plan.blocked == []


def test_plan_reads_current_values(game_config, profile):
    plan = _plan(game_config, profile, _entry())
    shadow = next(c for c in plan.changes if c.key == "shadow_quality")
    assert shadow.current.value == "Low"          # sg.ShadowQuality=0
    assert shadow.recommended == "Medium"


def test_settings_already_correct_are_not_proposed(game_config):
    profile = GameProfile(
        game_name="My Game", device="rog_ally_x", source="ROG Ally Life",
        source_url="https://rogallylife.com/x",
        recommendations=[Recommendation("texture_quality", "Medium")],  # already 1
    )
    plan = _plan(game_config, profile, _entry())
    assert plan.safe == []
    assert [c.key for c in plan.already_correct] == ["texture_quality"]


# -- dry run -----------------------------------------------------------------

def test_dry_run_shows_exactly_what_would_change_and_writes_nothing(game_config, profile):
    plan = _plan(game_config, profile, _entry())
    before = game_config.read_bytes()
    run = T.dry_run(plan, str(game_config))
    assert run.ok
    assert "sg.ShadowQuality" in run.diff and "FrameRateLimit" in run.diff
    assert game_config.read_bytes() == before, "dry run must not touch the file"


def test_dry_run_refuses_non_safe_changes(game_config, profile):
    plan = _plan(game_config, profile, _entry())
    change = plan.safe[0]
    change.safety = MANUAL
    run = T.dry_run(plan, str(game_config), [change])
    assert not run.ok
    assert "not SAFE" in " ".join(run.errors)


# -- backup ------------------------------------------------------------------

def test_backup_is_created_and_verified(game_config, tmp_path):
    backup = T.create_backup(str(game_config), "My Game", tmp_path / "backups")
    assert Path(backup.backup_path).is_file()
    assert backup.verify()
    assert Path(backup.backup_path).read_bytes() == game_config.read_bytes()


def test_backup_detects_corruption(game_config, tmp_path):
    backup = T.create_backup(str(game_config), "My Game", tmp_path / "backups")
    Path(backup.backup_path).write_text("tampered")
    assert not backup.verify()
    with pytest.raises(T.CriticalRestoreError):
        T.restore_backup(backup)


# -- apply -------------------------------------------------------------------

def test_apply_writes_only_approved_safe_changes(game_config, profile, tmp_path):
    entry, plan = _entry(), None
    plan = _plan(game_config, profile, entry)
    approved = _approve(plan)
    record = T.apply_plan(entry, plan, str(game_config), approved,
                          table=FakeProcessTable(),
                          backup_dir=tmp_path / "backups",
                          log_path=tmp_path / "log.json")
    assert record.succeeded
    assert set(record.applied) == {"shadow_quality", "frame_rate_limit"}
    text = game_config.read_text(encoding="utf-8")
    assert "sg.ShadowQuality=1" in text
    assert "FrameRateLimit=60" in text


def test_apply_preserves_comments_order_and_unknown_keys(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    T.apply_plan(entry, plan, str(game_config), _approve(plan),
                 table=FakeProcessTable(), backup_dir=tmp_path / "b",
                 log_path=tmp_path / "l.json")
    raw = game_config.read_bytes()          # bytes: read_text() would hide newline style
    text = raw.decode("utf-8")
    assert "; TravelReady test config - this comment must survive" in text
    assert "ModAddedKey=leave me alone" in text
    assert "; inline comment" in text
    assert b"\r\n" in raw, "CRLF line endings must be preserved"
    assert raw.count(b"\r\n") == INI.count("\r\n")
    assert not raw.startswith(b"\xef\xbb\xbf"), "a BOM must not be introduced"
    # untouched settings keep their values
    assert "sg.TextureQuality=1" in text
    assert "ResolutionSizeY=720" in text
    # ordering preserved
    assert text.index("[ScalabilityGroups]") < text.index("[/Script/Engine.GameUserSettings]")


def test_apply_requires_explicit_approval(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    unapproved = plan.applicable()          # deliberately not approved
    with pytest.raises(T.TransactionError, match="not approved"):
        T.apply_plan(entry, plan, str(game_config), unapproved,
                     table=FakeProcessTable(), backup_dir=tmp_path / "b",
                     log_path=tmp_path / "l.json")
    assert game_config.read_text(encoding="utf-8").count("sg.ShadowQuality=0") == 1


def test_apply_refuses_when_the_game_is_running(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    table = FakeProcessTable([ProcInfo(11, "mygame.exe", r"C:\g\mygame.exe", 1)])
    with pytest.raises(T.TransactionError, match="is running"):
        T.apply_plan(entry, plan, str(game_config), _approve(plan), table=table,
                     backup_dir=tmp_path / "b", log_path=tmp_path / "l.json")
    assert "sg.ShadowQuality=0" in game_config.read_text(encoding="utf-8")


def test_apply_refuses_when_a_config_rewriting_launcher_is_running(game_config, profile, tmp_path):
    entry = GameEntry(name="My Game", launcher="ea", expected_process="mygame.exe")
    plan = _plan(game_config, profile, entry)
    table = FakeProcessTable([ProcInfo(12, "EADesktop.exe", r"C:\ea\EADesktop.exe", 1)])
    with pytest.raises(T.TransactionError, match="launcher is running"):
        T.apply_plan(entry, plan, str(game_config), _approve(plan), table=table,
                     backup_dir=tmp_path / "b", log_path=tmp_path / "l.json")


def test_apply_refuses_a_non_safe_change_even_if_passed_in(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    change = plan.safe[0]
    change.approved = True
    change.safety = BLOCKED
    with pytest.raises(T.TransactionError, match="non-SAFE"):
        T.apply_plan(entry, plan, str(game_config), [change], table=FakeProcessTable(),
                     backup_dir=tmp_path / "b", log_path=tmp_path / "l.json")


def test_apply_aborts_if_the_file_changed_since_inspection(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    approved = _approve(plan)
    game_config.write_bytes(INI.replace("ResolutionSizeY=720", "ResolutionSizeY=1080").encode())
    # the dry run inside apply re-hashes, so this must abort rather than clobber
    record_before = game_config.read_bytes()
    try:
        T.apply_plan(entry, plan, str(game_config), approved, table=FakeProcessTable(),
                     backup_dir=tmp_path / "b", log_path=tmp_path / "l.json")
    except T.TransactionError:
        pass
    assert b"ResolutionSizeY=1080" in game_config.read_bytes()


def test_backup_exists_before_any_write(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    backup_dir = tmp_path / "backups"
    record = T.apply_plan(entry, plan, str(game_config), _approve(plan),
                          table=FakeProcessTable(), backup_dir=backup_dir,
                          log_path=tmp_path / "l.json")
    assert record.backup is not None
    saved = Path(record.backup["backup_path"])
    assert saved.is_file()
    assert saved.read_bytes() == INI.encode("utf-8"), "backup holds the ORIGINAL content"


def test_restore_puts_the_original_back(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    record = T.apply_plan(entry, plan, str(game_config), _approve(plan),
                          table=FakeProcessTable(), backup_dir=tmp_path / "b",
                          log_path=tmp_path / "l.json")
    assert game_config.read_bytes() != INI.encode("utf-8")
    T.restore_game_settings(T.Backup(**record.backup), log_path=tmp_path / "l.json")
    assert game_config.read_bytes() == INI.encode("utf-8")


def test_transaction_is_recorded(game_config, profile, tmp_path):
    entry = _entry()
    plan = _plan(game_config, profile, entry)
    log = tmp_path / "log.json"
    T.apply_plan(entry, plan, str(game_config), _approve(plan),
                 table=FakeProcessTable(), backup_dir=tmp_path / "b", log_path=log)
    rows = json.loads(log.read_text())["transactions"]
    assert rows and rows[-1]["succeeded"] is True
    assert rows[-1]["diff"]


# -- malformed input ---------------------------------------------------------

def test_malformed_config_cannot_trigger_a_destructive_rewrite(tmp_path, profile):
    d = tmp_path / "AppData" / "Local" / "MyGame" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    path = d / "GameUserSettings.ini"
    garbage = b"\x00\x01\x02 this is not a config \xff\xfe binary junk"
    path.write_bytes(garbage)
    entry = _entry()
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(path))])
    plan = build_plan(entry, profile, inspection)
    assert not plan.safe, "nothing may be SAFE in an unparseable file"
    assert path.read_bytes() == garbage


def test_empty_config_is_not_rewritten(tmp_path, profile):
    d = tmp_path / "AppData" / "Local" / "MyGame" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    path = d / "GameUserSettings.ini"
    path.write_bytes(b"")
    entry = _entry()
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(path))])
    plan = build_plan(entry, profile, inspection)
    assert not plan.safe
    assert path.read_bytes() == b""
