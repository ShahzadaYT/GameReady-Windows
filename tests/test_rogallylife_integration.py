"""Bridge, profile selection, capability gating and coverage reporting."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready.library import GameEntry, load_library
from travelready.optimiser.diff import build_plan, plan_for_game, render_plan
from travelready.optimiser.inspector import Inspection, read_config_file
from travelready.optimiser.model import CATEGORY_DEVICE, CATEGORY_GAME, MANUAL, SAFE
from travelready.optimiser.rogallylife import capability as cap
from travelready.optimiser.rogallylife.bridge import (
    SourceResolver, family_for_device, to_game_profile,
)
from travelready.optimiser.rogallylife.cache import ProfileCache
from travelready.optimiser.rogallylife.coverage import build_report, title_only_report
from travelready.optimiser.rogallylife.model import SourceGame, SourceProfile, SourceSetting
from travelready.optimiser.rogallylife.parser import parse_post
from travelready.optimiser.rogallylife.select import (
    MODE_BALANCED, MODE_BATTERY, MODE_PERFORMANCE, select_profile,
)

RAL = FIXTURES / "rogallylife"
ALLY_URL = "https://rogallylife.com/2026/02/27/example-adventure-rog-ally-game-settings/"

INI = (
    "; keep me\r\n[ScalabilityGroups]\r\nsg.TextureQuality=0\r\nsg.ShadowQuality=0\r\n"
    "[/Script/Engine.GameUserSettings]\r\nResolutionSizeX=1280\r\nResolutionSizeY=720\r\n"
    "FrameRateLimit=45.000000\r\nbUseVSync=False\r\n"
)


@pytest.fixture
def cached(tmp_path):
    cache = ProfileCache(tmp_path / "cache")
    game = parse_post((RAL / "two_profiles_table.html").read_text(),
                      url=ALLY_URL, title="Example Adventure ROG Ally Game Settings",
                      last_updated="2026-02-27")
    cache.put(game)
    cache.save_index()
    return cache


@pytest.fixture
def config(tmp_path):
    d = tmp_path / "AppData" / "Local" / "G" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    path = d / "GameUserSettings.ini"
    path.write_bytes(INI.encode("utf-8"))
    return path


# -- capability matrix -------------------------------------------------------

@pytest.mark.parametrize("label,key", [
    ("Resolution", "resolution"), ("Texture Quality", "texture_quality"),
    ("Shadows", "shadow_quality"), ("AMD FSR", "fsr_mode"),
    ("Frame Generation", "frame_generation"), ("VSync", "vsync"),
    ("Frame Rate Limit", "frame_rate_limit"), ("TDP", "tdp_watts"),
    ("Memory Assigned to GPU", "vram_allocation_gb"),
    ("Hair Strands", "hair_strands"), ("Average FPS", "average_fps"),
])
def test_site_labels_map_to_canonical_keys(label, key):
    assert cap.canonical_key(label) == key


def test_an_unknown_label_maps_to_nothing():
    assert cap.canonical_key("Completely Unknown Knob") == ""
    assert cap.capability_for("") is None


def test_longer_alias_wins_over_a_shorter_one():
    assert cap.canonical_key("Texture Quality") == "texture_quality"
    assert cap.canonical_key("Shadow Quality") == "shadow_quality"


@pytest.mark.parametrize("key,automatable", [
    ("resolution", True), ("texture_quality", True), ("fsr_mode", True),
    ("vsync", True), ("frame_rate_limit", True),
    ("frame_generation", False), ("hair_strands", False),
    ("tdp_watts", False), ("vram_allocation_gb", False), ("average_fps", False),
])
def test_capability_decides_what_is_automatable(key, automatable):
    assert cap.capability_for(key).automatable is automatable


def test_matrix_lists_the_unknown_case():
    assert "unknown game setting" in cap.describe_matrix()


# -- profile selection -------------------------------------------------------

def _two_profile_game():
    return SourceGame(title="X", source_url=ALLY_URL, device_family="rog_ally_family",
                      profiles=[SourceProfile(name="900P 15/18W"),
                                SourceProfile(name="1080P 18/25/30W")])


def test_battery_mode_picks_the_lowest_wattage():
    selection = select_profile(_two_profile_game(), MODE_BATTERY)
    assert selection.profile.label == "15/18W • 900p"
    assert "lowest published wattage" in selection.reason


def test_performance_mode_picks_the_highest_wattage():
    selection = select_profile(_two_profile_game(), MODE_PERFORMANCE)
    assert selection.profile.label == "18/25/30W • 1080p"


def test_balanced_mode_targets_18w():
    selection = select_profile(_two_profile_game(), MODE_BALANCED)
    assert selection.profile.min_tdp == 18


def test_selection_lists_the_alternatives():
    selection = select_profile(_two_profile_game(), MODE_BATTERY)
    assert selection.alternatives == ("18/25/30W • 1080p",)


def test_no_low_power_profile_is_stated_not_invented():
    game = SourceGame(title="X", source_url=ALLY_URL, device_family="rog_ally_family",
                      profiles=[SourceProfile(name="1080P 25/30W")])
    selection = select_profile(game, MODE_BATTERY)
    assert selection.profile.min_tdp == 25
    assert not selection.exact
    assert "publishes nothing below" in selection.reason


def test_a_wattage_cap_with_no_match_says_so():
    game = SourceGame(title="X", source_url=ALLY_URL, device_family="rog_ally_family",
                      profiles=[SourceProfile(name="1080P 25/30W")])
    selection = select_profile(game, MODE_PERFORMANCE, max_watts=18)
    assert "publishes nothing at or below 18W" in selection.reason
    assert not selection.exact


def test_a_game_with_no_profiles_selects_nothing():
    game = SourceGame(title="X", source_url=ALLY_URL, device_family="rog_ally_family")
    assert select_profile(game).profile is None


# -- bridge ------------------------------------------------------------------

def test_device_family_mapping():
    assert family_for_device("rog_ally_x") == "rog_ally_family"
    assert family_for_device("rog_xbox_ally_x") == "rog_xbox_ally_family"
    assert family_for_device("steam_deck") == ""


def test_bridge_keeps_full_attribution(cached):
    resolver = SourceResolver(cached)
    resolution = resolver.resolve(GameEntry(name="Example Adventure", launcher="steam"))
    profile = resolution.profile
    assert profile.source == "ROG Ally Life"
    assert profile.source_url == ALLY_URL
    assert profile.source_date == "2026-02-27"
    assert "parser" in profile.source_version
    assert "rogallylife.com" in profile.attribution


def test_bridge_splits_automatic_from_manual(cached):
    resolver = SourceResolver(cached, mode=MODE_BATTERY)
    profile = resolver.resolve(GameEntry(name="Example Adventure")).profile
    by_key = {r.key: r for r in profile.recommendations}
    assert by_key["texture_quality"].category == CATEGORY_GAME
    assert by_key["fsr_mode"].category == CATEGORY_GAME
    # a WxH resolution becomes the two keys engines actually store
    assert by_key["resolution_width"].value == "1600"
    assert by_key["resolution_height"].value == "900"
    # capability says these cannot be written
    assert by_key["hair_strands"].category == CATEGORY_DEVICE
    assert by_key["vram_allocation_gb"].category == CATEGORY_DEVICE
    assert by_key["tdp_watts"].category == CATEGORY_DEVICE


def test_unrecognised_settings_are_carried_not_dropped(tmp_path):
    cache = ProfileCache(tmp_path)
    cache.put(parse_post((RAL / "single_profile_list.html").read_text(),
                         url="https://rogallylife.com/2026/03/01/example-racer-rog-ally-game-settings/"))
    cache.save_index()
    profile = SourceResolver(cache).resolve(GameEntry(name="Example Racer")).profile
    notes = " ".join(r.note for r in profile.recommendations)
    assert "Completely Unknown Knob" in notes


def test_resolution_of_an_unknown_game_finds_nothing(cached):
    resolution = SourceResolver(cached).resolve(GameEntry(name="Some Game Nobody Covers"))
    assert resolution.status == "no_profile"
    assert resolution.profile is None
    assert "NO PROFILE FOUND" in resolution.describe()


def test_a_below_threshold_match_needs_review_and_is_not_applied(cached):
    resolution = SourceResolver(cached).resolve(GameEntry(name="Example Adventurer Two"))
    assert resolution.profile is None
    assert resolution.status in ("review", "no_profile")


def test_xbox_family_post_is_not_used_for_the_ally(tmp_path):
    cache = ProfileCache(tmp_path)
    game = parse_post((RAL / "two_profiles_table.html").read_text(),
                      url="https://rogallylife.com/2026/02/27/example-adventure-rog-xbox-ally-x/")
    cache.put(game)
    cache.save_index()
    resolver = SourceResolver(cache, target_device="rog_ally_x")
    assert resolver.candidates() == []
    assert SourceResolver(cache).resolve(GameEntry(name="Example Adventure")).profile is None


# -- plan building -----------------------------------------------------------

def test_plan_uses_the_source_and_shows_its_reasoning(cached, config):
    entry = GameEntry(name="Example Adventure", launcher="steam",
                      expected_process="g.exe")
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(config))])
    resolution = SourceResolver(cached, mode=MODE_BATTERY).resolve(entry)
    plan = build_plan(entry, resolution.profile)
    plan.resolution = resolution
    text = render_plan(plan)
    assert "ROG Ally Life" in text
    assert ALLY_URL in text
    assert "Confidence:" in text
    assert "Chosen because:" in text


def test_capability_gating_keeps_a_recognised_setting_manual(cached, config):
    entry = GameEntry(name="Example Adventure", launcher="steam")
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(config))])
    # The two profiles genuinely name different settings: Hair Strands appears
    # only on the 900p one, Frame Generation only on the 1080p one. Check each
    # against the profile that actually contains it.
    battery = build_plan(entry,
                         SourceResolver(cached, mode=MODE_BATTERY).resolve(entry).profile,
                         inspection)
    assert "texture_quality" in {c.key for c in battery.safe}
    battery_manual = {c.key for c in battery.manual}
    assert "hair_strands" in battery_manual, "no validated mapping -> manual"
    assert "tdp_watts" in battery_manual
    assert "vram_allocation_gb" in battery_manual

    performance = build_plan(
        entry, SourceResolver(cached, mode=MODE_PERFORMANCE).resolve(entry).profile,
        inspection)
    assert "frame_generation" in {c.key for c in performance.manual}


def test_a_locally_imported_profile_wins_over_the_cache(cached, config, tmp_path):
    from travelready.optimiser.profiles import ProfileStore, import_profile

    import_profile({
        "game_name": "Example Adventure", "device": "rog_ally_x",
        "source": "My own testing", "source_url": "https://example.invalid/notes",
        "recommendations": [{"key": "texture_quality", "value": "Low"}],
    }, tmp_path / "profiles")
    store = ProfileStore.load([tmp_path / "profiles"])
    plan = plan_for_game(GameEntry(name="Example Adventure"), store,
                         resolver=SourceResolver(cached))
    assert plan.profile.source == "My own testing"


def test_no_profile_says_so_and_substitutes_nothing(cached):
    from travelready.optimiser.profiles import ProfileStore

    plan = plan_for_game(GameEntry(name="Totally Uncovered Game"),
                         ProfileStore([], []), resolver=SourceResolver(cached))
    assert plan.profile is None
    assert plan.changes == []
    assert any("NO PROFILE FOUND" in w for w in plan.warnings)


# -- coverage ----------------------------------------------------------------

def test_coverage_counts_the_real_library(cached):
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    report = build_report(entries, SourceResolver(cached))
    assert report.total == 162
    assert len(report.rows) + len(report.infrastructure) == 162
    assert report.by_launcher()["xbox"] > 0
    assert "ROG Ally Life coverage" in report.describe()


def test_title_only_report_is_labelled_as_a_lower_bound():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    report = title_only_report(entries)
    assert "lower bound" in report.note
    assert all(r.status in ("review", "no_profile") for r in report.rows), \
        "a title match is never a profile"


def test_title_only_report_finds_the_known_real_matches():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    report = title_only_report(entries)
    matched = {r.entry.name for r in report.review}
    assert "Clair Obscur: Expedition 33" in matched
    assert "Clair Obscur- Expedition 33" in matched
    assert "Forza Horizon 6" in matched


def test_coverage_never_reports_junk_as_a_game():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    report = title_only_report(entries)
    matched = {r.entry.name for r in report.rows if r.confidence >= 0.90}
    assert not ({"Calculator", "Notepad", "Windows Security"} & matched)
