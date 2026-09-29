"""Library model, persistence and migration tests."""
from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from travelready import library as L


def test_loads_the_real_shipped_libraries():
    for name in ("games_2026-09-23.json", "games_2026-09-26.json"):
        entries, message = L.load_library(FIXTURES / name)
        assert entries
        assert all(e.id and e.name for e in entries)
        assert all(e.launcher in L.LAUNCHERS for e in entries)


def test_v2_library_migrates_and_keeps_every_game():
    entries, message = L.load_library(FIXTURES / "games_2026-09-26.json")
    assert len(entries) == 162
    assert "migrated v2 -> v3" in message


def test_v1_library_derives_launcher_from_source(tmp_path):
    path = tmp_path / "games.json"
    path.write_text(json.dumps({"version": "1", "games": [
        {"name": "A", "source": "Origin", "exe_path": "C:\\a\\a.exe"},
        {"name": "B", "source": "Microsoft Store", "exe_path": "C:\\b\\b.exe"},
    ]}))
    entries, message = L.load_library(path)
    assert [e.launcher for e in entries] == ["ea", "xbox"]
    assert "migrated v1 -> v3" in message


def test_bare_list_library_still_loads(tmp_path):
    path = tmp_path / "games.json"
    path.write_text(json.dumps([{"name": "A", "source": "steam"}]))
    entries, _ = L.load_library(path)
    assert entries[0].launcher == "steam"


def test_corrupt_library_is_backed_up_not_destroyed(tmp_path):
    path = tmp_path / "games.json"
    path.write_text("{not json")
    entries, message = L.load_library(path)
    assert entries == []
    assert "corrupt" in message.lower()
    assert path.read_text() == "{not json", "the original must be left alone"
    assert list(tmp_path.glob("games.json.corrupt-*"))


def test_save_is_atomic_and_round_trips(tmp_path):
    original, _ = L.load_library(FIXTURES / "games_2026-09-26.json")
    path = tmp_path / "out.json"
    L.save_library(original, path)
    reloaded, _ = L.load_library(path)
    assert [e.to_dict() for e in reloaded] == [e.to_dict() for e in original]
    assert not list(tmp_path.glob("*.tmp"))


def test_new_v3_fields_default_safely():
    entry = L.GameEntry(name="X", launcher="steam")
    assert entry.install_dir == "" and entry.alt_processes == []
    assert entry.verification == L.VERIFY_AUTO


def test_process_names_prefers_explicit_over_derived():
    entry = L.GameEntry(name="X", exe_path=r"C:\g\launch.exe",
                        expected_process="real.exe", alt_processes=["alt.exe", "real.exe"])
    assert entry.process_names() == ["real.exe", "alt.exe"]


def test_process_names_ignores_pseudo_paths():
    entry = L.GameEntry(name="X", exe_path="shell:AppsFolder\\Pub.Game_x!App")
    assert entry.process_names() == []


@pytest.mark.parametrize("value,expected", [
    ("Origin", "ea"), ("EA Desktop", "ea"), ("Microsoft Store", "xbox"),
    ("Game Pass", "xbox"), ("Uplay", "ubisoft"), ("Battle.net", "battlenet"),
    ("GOG Galaxy", "gog"), ("nonsense", "other"), ("", "other"), (None, "other"),
])
def test_launcher_normalisation(value, expected):
    assert L.normalize_launcher(value) == expected


def test_identity_key_is_conservative():
    same = [("Battlefield 3\u2122 Limited Edition", "Battlefield 3"),
            ("Clair Obscur: Expedition 33", "Clair Obscur- Expedition 33"),
            ("Mafia: Definitive Edition", "Mafia")]
    for a, b in same:
        assert L.identity_key(a) == L.identity_key(b), (a, b)
    different = [("DOOM + DOOM II", "DOOM: The Dark Ages"),
                 ("F1 2020", "F1 24"),
                 ("Need for Speed Unbound", "Need for Speed Heat")]
    for a, b in different:
        assert L.identity_key(a) != L.identity_key(b), (a, b)


# -- rescan merging ----------------------------------------------------------

def test_rescan_never_loses_user_test_results():
    existing = [L.GameEntry(name="Game", launcher="steam", last_result="PASS",
                            last_ready="2026-01-01T00:00:00+00:00",
                            launch_timeout=99, notes="my note")]
    found = [L.GameEntry(name="Game", launcher="steam", exe_path=r"C:\g\g.exe",
                         install_dir=r"C:\g", expected_process="g.exe")]
    merged, added, updated = L.merge_library_updates(existing, found)
    assert added == 0 and updated == 1 and len(merged) == 1
    entry = merged[0]
    assert entry.last_result == "PASS"
    assert entry.launch_timeout == 99
    assert entry.notes == "my note"
    assert entry.expected_process == "g.exe", "new verification data is adopted"


def test_rescan_never_overwrites_a_known_value():
    existing = [L.GameEntry(name="Game", launcher="steam", expected_process="user-set.exe")]
    found = [L.GameEntry(name="Game", launcher="steam", expected_process="scanner.exe")]
    merged, _, _ = L.merge_library_updates(existing, found)
    assert merged[0].expected_process == "user-set.exe"
    assert "scanner.exe" in merged[0].alt_processes


def test_rescan_keeps_games_the_scan_did_not_find():
    existing = [L.GameEntry(name="Offline Launcher Game", launcher="epic")]
    merged, added, _ = L.merge_library_updates(existing, [])
    assert len(merged) == 1 and added == 0


def test_rescan_upgrades_a_manual_entry_that_became_verifiable():
    existing = [L.GameEntry(name="G", launcher="xbox", mode="manual",
                            verification=L.VERIFY_MANUAL,
                            launch_target="shell:AppsFolder\\P.G_x!App")]
    found = [L.GameEntry(name="G", launcher="xbox", expected_process="g.exe")]
    merged, _, _ = L.merge_library_updates(existing, found)
    assert merged[0].mode == "standard"
    assert merged[0].verification == L.VERIFY_AUTO


def test_infrastructure_entries_are_flagged_not_deleted():
    entries, _ = L.load_library(FIXTURES / "games_2026-09-26.json")
    flagged = L.infrastructure_entries(entries)
    names = {e.name for e in flagged}
    assert "Electronic Arts" in names and "EA Games" in names
    assert len(entries) == 162, "flagging must not remove anything"


# -- import / export ---------------------------------------------------------

def test_csv_round_trip(tmp_path):
    entries, _ = L.load_library(FIXTURES / "games_2026-09-26.json")
    path = tmp_path / "out.csv"
    L.export_games(entries[:5], path)
    imported, _ = L.import_games(path)
    assert [e.name for e in imported] == [e.name for e in entries[:5]]


def test_txt_import_of_executable_paths(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("# comment\nC:\\Games\\Foo\\foo.exe\n\nC:\\Games\\Bar\\bar.exe\n")
    imported, _ = L.import_games(path)
    assert [e.name for e in imported] == ["foo", "bar"]
    assert imported[0].origin == "manual"
