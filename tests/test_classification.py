"""Scan classification tests — including the real false positives it fixed."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready.classification import (
    APPLICATION, GAME, LAUNCHER, OVERRIDE_GAME, OVERRIDE_NOT_GAME,
    SYSTEM_COMPONENT, UNKNOWN, UTILITY, classify, classify_entry,
    classification_report, clear_evidence_cache, game_files_evidence,
    split_games,
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


def test_an_unrecognised_entry_is_not_counted_as_a_game():
    """Regression: anything unrecognised used to be a game.

    That rule ("fail open: excluding a real game is worse than listing an
    app") made every Start-menu shortcut on an ordinary PC a game to launch.
    A game now needs positive evidence; the user can mark one that has none.
    """
    result = classify("Some Indie Title", exe_path=r"D:\Indie\indie.exe")
    assert result.category == UNKNOWN
    assert not result.is_game
    assert result.rule == "unidentified"
    assert "no sign it is a game" in result.reason
    # a path that *is* a games folder is still recognised positively
    assert classify("Some Indie Title",
                    exe_path=r"D:\Games\Indie\indie.exe").rule == "game-directory"


# -- positive evidence ---------------------------------------------------------

@pytest.mark.parametrize("name,exe", [
    ("Command Prompt", r"C:\Windows\system32\cmd.exe"),
    ("Windows PowerShell", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    ("Task Manager", r"C:\Windows\system32\taskmgr.exe"),
    ("RecoveryDrive", r"C:\Windows\system32\RecoveryDrive.exe"),
    ("ODBC Data Sources (32-bit)", r"C:\Windows\syswow64\odbcad32.exe"),
])
def test_nothing_in_the_windows_folder_is_a_game(name, exe):
    result = classify(name, exe_path=exe, launcher="other")
    assert result.category == SYSTEM_COMPONENT
    assert result.rule == "windows-folder"


@pytest.mark.parametrize("name,exe", [
    # Start-menu shortcuts found on an ordinary Windows 11 PC, all of which
    # were "games" offered to Prepare for Travel before this change.
    ("Word", r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
    ("Excel", r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"),
    ("VLC media player - reset preferences and cache files",
     r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    ("DaVinci Resolve", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"),
    ("Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("Internet Download Manager",
     r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"),
    ("TreeSize Free (Administrator)",
     r"C:\Program Files\JAM Software\TreeSize Free\TreeSizeFree.exe"),
    ("Run DiskGenius", r"C:\Program Files\DiskGenius\DiskGenius.exe"),
])
def test_ordinary_start_menu_software_is_not_a_game(name, exe):
    result = classify(name, exe_path=exe, launcher="other")
    assert not result.is_game, result.reason


@pytest.mark.parametrize("launcher", ["steam", "epic", "ea", "ubisoft", "gog",
                                      "battlenet", "xbox"])
def test_a_launchers_own_library_is_evidence(launcher):
    result = classify("Metro 2033", exe_path="steam://rungameid/43110",
                      launcher=launcher)
    assert result.category == GAME
    assert result.rule == "launcher-library"


def test_game_files_next_to_the_executable_are_evidence():
    result = classify("Play Dispatch", exe_path=r"D:\Dispatch\Dispatch.exe",
                      launcher="other", evidence="Unreal Engine files (Engine\\Binaries)")
    assert result.category == GAME
    assert result.rule == "game-files"
    assert "Unreal Engine" in result.reason


def test_rockstar_games_is_a_games_folder():
    result = classify("Play Red Dead Redemption 2",
                      exe_path=r"C:\Program Files\Rockstar Games\Red Dead Redemption 2\RDR2.exe",
                      launcher="other")
    assert result.rule == "game-directory"


# -- the user's decision --------------------------------------------------------

def test_marking_an_entry_as_a_game_outranks_every_rule():
    result = classify("Command Prompt", exe_path=r"C:\Windows\system32\cmd.exe",
                      override=OVERRIDE_GAME)
    assert result.category == GAME
    assert result.rule == "user-override"


def test_marking_an_entry_as_not_a_game_outranks_a_launcher_library():
    """Netflix arrives through the Xbox catalogue and passes its filter."""
    result = classify("Netflix", launcher="xbox", override=OVERRIDE_NOT_GAME)
    assert not result.is_game
    assert result.rule == "user-override"


def test_classify_entry_reads_the_override():
    entry = GameEntry(name="Play Dispatch", launcher="other",
                      exe_path=r"D:\Dispatch\Dispatch.exe",
                      category_override=OVERRIDE_GAME)
    assert classify_entry(entry).is_game


# -- finding game files on disk --------------------------------------------------

def _install(root, files, exe="game.exe"):
    root.mkdir(parents=True, exist_ok=True)
    for name in files:
        path = root / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
    (root / exe).write_bytes(b"MZ")
    return str(root / exe)


@pytest.fixture(autouse=True)
def _fresh_evidence_cache():
    clear_evidence_cache()
    yield
    clear_evidence_cache()


@pytest.mark.parametrize("files,expected", [
    (["UnityPlayer.dll"], "Unity"),
    (["steam_api64.dll"], "Steam game API"),
    (["EOSSDK-Win64-Shipping.dll"], "Epic Online Services"),
    (["bink2w64.dll"], "Bink"),
    (["x64.rpf"], "Rockstar"),
    (["Runtime/chunk0.rpkg"], ""),           # only the exe folder and parents
    (["Engine/Binaries/"], "Unreal Engine"),
])
def test_game_files_are_found_next_to_the_executable(tmp_path, files, expected):
    exe = _install(tmp_path / "Games2" / "Some Game", files)
    found = game_files_evidence(exe)
    assert (expected in found) if expected else found == ""


def test_game_files_are_found_above_a_bin_folder(tmp_path):
    root = tmp_path / "Library" / "Far Cry Primal"
    _install(root, ["data.forge"], exe="readme.txt")
    exe = _install(root / "bin", [], exe="FCPrimal.exe")
    assert "Ubisoft" in game_files_evidence(exe)


def test_electron_and_chrome_resources_are_not_game_files(tmp_path):
    """Chrome and every Electron app ship resources.pak; that is not a game."""
    exe = _install(tmp_path / "Apps" / "Chat", ["resources.pak", "chrome_100_percent.pak",
                                                "ffmpeg.dll", "d3dcompiler_47.dll"])
    assert game_files_evidence(exe) == ""


def test_shared_roots_are_never_searched(tmp_path):
    """A marker in Program Files itself must not make every program a game."""
    shared = tmp_path / "Program Files"
    _install(shared, ["steam_api64.dll"], exe="unrelated.exe")
    exe = _install(shared / "VideoLAN" / "VLC", [], exe="vlc.exe")
    assert game_files_evidence(exe) == ""


def test_uris_and_store_routes_are_not_searched():
    assert game_files_evidence("steam://rungameid/1") == ""
    assert game_files_evidence(r"shell:AppsFolder\Pub.Game!App") == ""
    assert game_files_evidence("") == ""


def test_classify_entry_uses_game_files_for_unrecognised_entries(tmp_path):
    exe = _install(tmp_path / "Repacks" / "God of War Ragnarok", ["steam_api64.dll"],
                   exe="GoWR.exe")
    entry = GameEntry(name="Play God of War Ragnarok", launcher="other",
                      source="shortcuts", exe_path=exe)
    result = classify_entry(entry)
    assert result.is_game and result.rule == "game-files"


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


WINDOWS_TOOLS_IN_THE_REAL_LIBRARY = (
    "Command Prompt", "Windows PowerShell", "Windows PowerShell ISE",
    "Task Manager", "RecoveryDrive", "Steps Recorder", "iSCSI Initiator",
    "ODBC Data Sources (64-bit)", "Remote Desktop Connection", "Narrator",
    "Windows Defender", "Internet Explorer", "Common Files",
)


def test_real_library_windows_tools_are_not_games_but_its_games_are():
    """Regression on the supplied library: these were all counted as games."""
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, non_games = split_games(entries)
    game_names = {e.name for e in games}
    for name in WINDOWS_TOOLS_IN_THE_REAL_LIBRARY:
        assert name not in game_names, f"{name} must not be a game"
    # games that only a launcher's own library identifies stay games
    for name in ("Metro 2033", "GRID Legends", "Mass Effect™ Legendary Edition",
                 "Hogwarts Legacy", "Forza Horizon 6", "Far Cry Primal",
                 "Play Red Dead Redemption 2"):
        assert name in game_names, f"{name} should be a game"


def test_prepare_for_travel_never_plans_to_start_a_windows_tool():
    """The safety property behind D1: Prepare starts games, nothing else."""
    from travelready.identity import build_identities
    from travelready.prepare_run import PrepareOptions, plan_run

    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, _ = split_games(entries)
    targets, _ = plan_run(build_identities(games), PrepareOptions())
    planned = {i.canonical_title for i in targets}
    for name in WINDOWS_TOOLS_IN_THE_REAL_LIBRARY:
        assert name not in planned, f"Prepare for Travel would start {name}"


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
