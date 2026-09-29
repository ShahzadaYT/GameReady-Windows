"""Scan classification tests — including the real false positives it fixed."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready.classification import (
    APPLICATION, GAME, LAUNCHER, SYSTEM_COMPONENT, UTILITY, classify,
    classify_entry, classification_report, split_games,
)
from travelready.library import GameEntry, load_library

UBI = r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games"


# -- the bug this module was written for ------------------------------------

@pytest.mark.parametrize("name,exe", [
    ("The Crew 2", UBI + r"\The Crew 2\TheCrew2.exe"),
    ("Assassin's Creed Origins", UBI + r"\Assassin's Creed Origins\ACOrigins_plus.exe"),
    ("Assassin's Creed Odyssey", UBI + r"\Assassin's Creed Odyssey\ACOdyssey_plus.exe"),
    ("ForHonor", UBI + r"\ForHonor\forhonor.exe"),
    ("Star Wars Outlaws", UBI + r"\Star Wars Outlaws\Outlaws_Plus.exe"),
    ("AFOP", UBI + r"\AFOP\afop_plus.exe"),
])
def test_ubisoft_games_are_not_launcher_software(name, exe):
    """Ubisoft installs games under 'Ubisoft Game Launcher\\games\\'.

    The substring 'launcher' therefore appears in every legitimate Ubisoft
    game path, which the old filter read as launcher software.
    """
    result = classify(name, exe_path=exe)
    assert result.category == GAME, result.reason
    assert result.rule == "game-directory"


def test_a_game_whose_own_binary_is_called_a_launcher_is_still_a_game():
    """Cyberpunk 2077 starts through REDprelauncher.exe."""
    result = classify("Cyberpunk 2077",
                      exe_path=r"C:\Program Files\GOG Galaxy\Games\Cyberpunk 2077"
                               r"\REDprelauncher.exe")
    assert result.category == GAME


def test_a_game_containing_a_launcher_name_is_not_that_launcher():
    """'Assassin's Creed Origins' contains 'Origin', the EA launcher."""
    result = classify("Assassin's Creed Origins",
                      exe_path=UBI + r"\Assassin's Creed Origins\ACOrigins.exe")
    assert result.category == GAME


def test_a_game_with_a_redist_executable_stays_a_game_but_is_flagged():
    """The entry is wrong, not the game. A bad exe makes it unverifiable."""
    result = classify(
        "Batman: Arkham Asylum GOTY Edition",
        exe_path=r"D:\SteamLibrary\steamapps\common\Batman Arkham Asylum GOTY"
                 r"\redist\PhysX_9.08.14_SystemSoftware.exe")
    assert result.category == GAME
    assert result.warning
    assert "redistributable" in result.warning


# -- genuine non-games -------------------------------------------------------

@pytest.mark.parametrize("name,category", [
    ("Calculator", SYSTEM_COMPONENT), ("Notepad", SYSTEM_COMPONENT),
    ("Windows Security", SYSTEM_COMPONENT), ("Settings", SYSTEM_COMPONENT),
    ("Copilot", SYSTEM_COMPONENT), ("Recall (preview)", SYSTEM_COMPONENT),
    ("Microsoft Edge", SYSTEM_COMPONENT), ("OneDrive", SYSTEM_COMPONENT),
    ("AMD Software", APPLICATION), ("Armoury Crate SE", APPLICATION),
    ("MyASUS", APPLICATION), ("Realtek Audio Console", APPLICATION),
    ("dotnet", APPLICATION),
    ("Steam", LAUNCHER), ("EA Games", LAUNCHER), ("Electronic Arts", LAUNCHER),
    ("XBOX", LAUNCHER), ("Game Bar", LAUNCHER), ("Ubisoft Connect", LAUNCHER),
    ("Bitwarden", UTILITY), ("7-Zip", UTILITY), ("Discord", UTILITY),
])
def test_known_non_games(name, category):
    assert classify(name).category == category


def test_a_redistributable_outside_a_game_folder_is_a_utility():
    result = classify("PhysX", exe_path=r"C:\Program Files\NVIDIA\redist\physx.exe")
    assert result.category == UTILITY


def test_launcher_program_directory_is_launcher_software():
    result = classify("Something",
                      exe_path=r"C:\Program Files\Electronic Arts\EA Desktop"
                               r"\EADesktop.exe")
    assert result.category == LAUNCHER
    assert result.rule == "launcher-directory"


def test_an_unrecognised_entry_defaults_to_game():
    """Fail open here: excluding a real game is worse than listing an app."""
    result = classify("Some Indie Title", exe_path=r"D:\Indie\indie.exe")
    assert result.category == GAME
    assert result.rule == "default"
    # and a path that *is* a games folder is recognised positively
    assert classify("Some Indie Title",
                    exe_path=r"D:\Games\Indie\indie.exe").rule == "game-directory"


def test_every_classification_carries_a_reason():
    for name in ("Calculator", "The Crew 2", "Steam", "Some Indie Title"):
        assert classify(name).reason


# -- against the real library -------------------------------------------------

def test_real_library_classification():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, non_games = split_games(entries)
    assert len(games) + len(non_games) == 162

    game_names = {e.name for e in games}
    non_game_names = {e.name for e in non_games}

    # real games that the old filter wrongly excluded
    for name in ("The Crew 2", "Assassin's Creed Origins", "Assassin's Creed Odyssey",
                 "ForHonor", "Star Wars Outlaws", "AFOP", "Cyberpunk 2077"):
        assert name in game_names, f"{name} should be a game"

    # Windows apps the old filter wrongly included
    for name in ("Calculator", "Notepad", "Paint", "Settings", "Terminal",
                 "Windows Security", "Copilot", "AMD Software", "MyASUS",
                 "Armoury Crate SE", "Realtek Audio Console", "XBOX", "Game Bar"):
        assert name in non_game_names, f"{name} should not be a game"


def test_classification_report_explains_every_exclusion():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    text = classification_report(entries)
    assert "Scan classification" in text
    _games, non_games = split_games(entries)
    for entry in non_games:
        assert entry.name[:40] in text


def test_classify_entry_reads_the_store_package_id():
    entry = GameEntry(name="Calculator", launcher="xbox", launch_method="shell",
                      launch_target="shell:AppsFolder\\Microsoft.WindowsCalculator"
                                    "_8wekyb3d8bbwe!App")
    assert classify_entry(entry).category == SYSTEM_COMPONENT
