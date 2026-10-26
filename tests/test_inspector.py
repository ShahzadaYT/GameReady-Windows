"""Read-only settings inspection tests."""
from __future__ import annotations

import pytest

from travelready.library import GameEntry
from travelready.optimiser.inspector import (
    UNREAL_GAME_USER_SETTINGS, candidate_config_paths, inspect_game,
    read_config_file, schema_for_file,
)

INI = (
    "[ScalabilityGroups]\r\nsg.ShadowQuality=0\r\nsg.TextureQuality=3\r\n"
    "[/Script/Engine.GameUserSettings]\r\nResolutionSizeX=1280\r\n"
    "ResolutionSizeY=720\r\nFrameRateLimit=45.000000\r\nbUseVSync=False\r\n"
)


@pytest.fixture
def config(tmp_path):
    d = tmp_path / "AppData" / "Local" / "G" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    path = d / "GameUserSettings.ini"
    path.write_bytes(INI.encode("utf-8"))
    return path


def test_candidate_paths_split_windows_relative_components():
    """Regression: 'Saved\\Config\\WindowsNoEditor' must not become one filename."""
    entry = GameEntry(name="G", install_dir=r"C:\Games\G")
    paths = candidate_config_paths(entry)
    assert any(p.endswith(r"C:\Games\G\Saved\Config\WindowsNoEditor\GameUserSettings.ini")
               for p in paths)
    # No component may still contain an unsplit separator. On a POSIX host the
    # unfixed code produced ".../G/Saved\Config\WindowsNoEditor/…", which never
    # matches a real file.
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        assert all("\\" not in part for part in parts), path


def test_candidate_paths_cover_the_localappdata_location():
    entry = GameEntry(name="My Game™")
    paths = candidate_config_paths(entry, home=r"C:\Users\S\AppData\Local")
    assert any("My Game" in p and p.endswith("GameUserSettings.ini") for p in paths)
    assert all("™" not in p for p in paths)


def test_schema_is_matched_by_filename():
    assert schema_for_file(r"C:\x\GameUserSettings.ini") is UNREAL_GAME_USER_SETTINGS
    assert schema_for_file(r"C:\x\Engine.ini") is None


def test_values_are_read_and_translated_to_canonical_names(config):
    inspected = read_config_file(str(config))
    assert inspected.recognised and inspected.readable
    assert inspected.values["shadow_quality"].value == "Low"       # 0
    assert inspected.values["texture_quality"].value == "Epic"     # 3
    assert inspected.values["vsync"].value == "Off"                # False
    assert inspected.values["resolution"].value == "1280x720"


def test_reading_reports_where_each_value_came_from(config):
    inspected = read_config_file(str(config))
    location = inspected.values["shadow_quality"].location
    assert "GameUserSettings.ini" in location and "sg.ShadowQuality" in location


def test_unrecognised_file_yields_no_automatable_settings(tmp_path):
    path = tmp_path / "AppData" / "Local" / "G"
    path.mkdir(parents=True)
    other = path / "Engine.ini"
    other.write_text("[Core]\nSomething=1\n")
    inspected = read_config_file(str(other))
    assert inspected.readable
    assert not inspected.recognised
    assert inspected.values == {}


def test_missing_config_is_reported_not_guessed():
    entry = GameEntry(name="Nothing Installed", install_dir=r"C:\nope")
    inspection = inspect_game(entry, paths=[r"C:\nope\GameUserSettings.ini"],
                              exists=lambda p: False)
    assert inspection.files == []
    assert any("No recognised configuration file" in w for w in inspection.warnings)
    assert inspection.current("shadow_quality").present is False


def test_protected_file_is_not_inspected():
    inspected = read_config_file(r"C:\Windows\System32\config.ini")
    assert not inspected.readable
    assert "protected" in inspected.note.lower()
