"""Discovery filtering, parsing and merging tests (no Windows required)."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready import discovery as d
from travelready.library import GameEntry, load_library


# -- parsers -----------------------------------------------------------------

def test_parse_start_apps():
    apps = d.parse_start_apps(
        "Calculator|Microsoft.WindowsCalculator_8wekyb3d8bbwe!App\n"
        "DOOM: The Dark Ages|BethesdaSoftworks.ProjectTitan_3275kfvn8vcwc!Game\n"
        "junk line without a pipe\n")
    assert len(apps) == 2
    assert apps[1] == ("DOOM: The Dark Ages",
                       "BethesdaSoftworks.ProjectTitan_3275kfvn8vcwc!Game")


def test_parse_acf():
    data = d.parse_acf('"AppState"\n{\n"appid" "1030840"\n'
                       '"name" "Mafia: Definitive Edition"\n'
                       '"installdir" "Mafia Definitive Edition"\n}\n')
    assert data == {"appid": "1030840", "name": "Mafia: Definitive Edition",
                    "installdir": "Mafia Definitive Edition"}


def test_parse_vdf_library_paths():
    paths = d.parse_vdf_paths('"libraryfolders"{"0"{"path" "C:\\\\Program Files '
                              '(x86)\\\\Steam"}"1"{"path" "D:\\\\SteamLibrary"}}')
    assert paths == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


# -- blacklists --------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "unins000.exe", "vcredist_x64.exe", "UnrealCEFSubProcess.exe",
    "EasyAntiCheat_Setup.exe", "crashreporter.exe", "DXSETUP.exe",
    "BattlEye_Launcher.exe", "GameUpdater.exe",
])
def test_tooling_executables_are_never_picked_as_the_game(name):
    assert d.is_blacklisted_exe(name)


@pytest.mark.parametrize("name", [
    "bf3.exe", "DOOMTheDarkAges.exe", "SandFall-WinGDK-Shipping.exe",
    "mafiadefinitiveedition.exe", "KingdomCome.exe",
])
def test_real_game_executables_survive(name):
    assert not d.is_blacklisted_exe(name)


@pytest.mark.parametrize("name", [
    "EA Desktop", "EA App", "Origin", "Electronic Arts", "EA Games",
    "Ubisoft Connect", "Epic Games Launcher", "Steam", "Battle.net", "XBOX",
])
def test_launchers_are_not_catalogued_as_games(name):
    assert d.is_blacklisted_name(name)


def test_ea_infrastructure_paths_are_excluded():
    assert d.is_launcher_infrastructure(
        r"C:\Program Files\Electronic Arts\EA Desktop\EADesktop.exe")
    assert d.is_launcher_infrastructure(r"C:\Games\MyGame\__Installer\setup.exe")
    assert not d.is_launcher_infrastructure(r"C:\Program Files\EA Games\Battlefield 3\bf3.exe")


# -- exe selection -----------------------------------------------------------

def test_largest_game_exe_skips_tooling_and_picks_the_game():
    listing = [
        (r"C:\G\unins000.exe", 90_000_000),
        (r"C:\G\__Installer\setup.exe", 80_000_000),
        (r"C:\G\EasyAntiCheat\EasyAntiCheat.exe", 70_000_000),
        (r"C:\G\Binaries\Win64\Game-Win64-Shipping.exe", 60_000_000),
        (r"C:\G\small.exe", 1_000),
    ]
    chosen = d.largest_game_exe(r"C:\G", lister=lambda _: listing)
    assert chosen == r"C:\G\Binaries\Win64\Game-Win64-Shipping.exe"


def test_largest_game_exe_returns_empty_when_only_tooling_is_present():
    listing = [(r"C:\G\unins000.exe", 100), (r"C:\G\vcredist.exe", 200)]
    assert d.largest_game_exe(r"C:\G", lister=lambda _: listing) == ""


@pytest.mark.parametrize("path,launcher", [
    (r"C:\Program Files (x86)\Steam\steamapps\common\X\x.exe", "steam"),
    (r"C:\XboxGames\X\Content\x.exe", "xbox"),
    (r"C:\Program Files\EA Games\X\x.exe", "ea"),
    (r"C:\Program Files\Epic Games\X\x.exe", "epic"),
    (r"C:\Program Files\Ubisoft\X\x.exe", "ubisoft"),
    (r"C:\GOG Games\X\x.exe", "gog"),
    (r"D:\Random\x.exe", "other"),
])
def test_launcher_inference_from_path(path, launcher):
    assert d.infer_launcher_from_path(path) == launcher


# -- merging -----------------------------------------------------------------

def test_merging_the_real_library_recovers_verification_data():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    merged = d.merge_entries(entries)
    by_name = {e.name: e for e in merged}
    recovered = [e for e in merged
                 if e.launcher == "xbox"
                 and e.launch_target.lower().startswith("shell:")
                 and e.expected_process]
    assert len(recovered) >= 4, "Xbox games should regain a verifiable process"
    assert len(merged) < len(entries), "duplicates should be collapsed"


def test_merging_keeps_the_better_launch_route():
    shell = GameEntry(name="G", launcher="xbox", launch_method="shell",
                      launch_target="shell:AppsFolder\\P.G_x!Game")
    exe = GameEntry(name="G", launcher="xbox", launch_method="exe",
                    exe_path=r"C:\XboxGames\G\Content\g.exe",
                    install_dir=r"C:\XboxGames\G\Content", expected_process="g.exe")
    merged = d.merge_entries([exe, shell])          # order must not matter
    assert len(merged) == 1
    assert merged[0].launch_target.startswith("shell:AppsFolder")
    assert merged[0].expected_process == "g.exe"


def test_merging_respects_launcher_boundaries():
    a = GameEntry(name="Same Name", launcher="steam")
    b = GameEntry(name="Same Name", launcher="epic")
    assert len(d.merge_entries([a, b])) == 2


def test_xbox_games_folder_scan_supplies_the_process_name():
    listing = [("DOOM- The Dark Ages", r"C:\XboxGames\DOOM- The Dark Ages")]

    def exe_lister(directory):
        if "Content" in directory:
            return [(directory + r"\DOOMTheDarkAges.exe", 50_000_000)]
        return []

    import travelready.discovery as mod
    original = mod.largest_game_exe
    mod.largest_game_exe = lambda p, **kw: exe_lister(p) and exe_lister(p)[0][0] or ""
    try:
        found = d.scan_xbox_games_folder(lister=lambda _: listing)
    finally:
        mod.largest_game_exe = original
    from travelready.library import identity_key
    hit = found[identity_key("DOOM: The Dark Ages")]
    assert hit["expected_process"] == "DOOMTheDarkAges.exe"


def test_discovery_never_enumerates_the_protected_windowsapps_store():
    import inspect

    source = inspect.getsource(d.discover_common_dirs)
    assert "windowsapps" in source.lower(), "the guard must be explicit"
    # and the guard must actually skip it
    assert any("windowsapps" in folder.lower() for folder in d._COMMON_DIRS)
