"""Parser tests against ROG Ally Life page structure.

Fixtures in ``tests/fixtures/rogallylife/`` are synthetic: they reproduce the
site's markup, but their values are test data, not recommendations.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FIXTURES
from travelready.optimiser.rogallylife.model import (
    SourceGame, SourceProfile, clean_game_title, device_family_from_url,
    parse_resolution, parse_tdp_watts, parse_vram_gb, rating_meaning,
)
from travelready.optimiser.rogallylife.parser import (
    PARSER_VERSION, content_hash, is_profile_heading, parse_index, parse_post,
)

RAL = FIXTURES / "rogallylife"
ALLY_URL = "https://rogallylife.com/2026/02/27/example-adventure-rog-ally-game-settings/"


def fixture(name: str) -> str:
    return (RAL / name).read_text(encoding="utf-8")


# -- device family from the URL ---------------------------------------------

@pytest.mark.parametrize("url,family", [
    ("https://rogallylife.com/2025/04/24/clair-obscur-expedition-33-rog-ally/",
     "rog_ally_family"),
    ("https://rogallylife.com/2026/02/27/resident-evil-requiem-rog-ally-game/",
     "rog_ally_family"),
    ("https://rogallylife.com/2026/05/15/forza-horizon-6-rog-ally-game-settings/",
     "rog_ally_family"),
    ("https://rogallylife.com/2025/10/18/clair-obscur-expedition-33-xbox-ally-x/",
     "rog_xbox_ally_family"),
    ("https://rogallylife.com/2026/02/27/resident-evil-requiem-rog-xbox-ally-x/",
     "rog_xbox_ally_family"),
    ("https://rogallylife.com/2026/08/26/resonance-a-plague-tale-legacy-xbox-all/",
     "rog_xbox_ally_family"),
    ("https://rogallylife.com/community-game-settings/", None),
    ("https://rogallylife.com/about/", None),
])
def test_device_family_is_read_from_the_url(url, family):
    """The site publishes a separate post per device; the URL says which."""
    assert device_family_from_url(url) == family


def test_xbox_ally_suffix_is_not_read_as_rog_ally():
    """'-rog-xbox-ally-x' also ends with '-ally-x'; ordering must not confuse them."""
    assert device_family_from_url(
        "https://rogallylife.com/2026/01/01/game-rog-xbox-ally-x/") == "rog_xbox_ally_family"


@pytest.mark.parametrize("title,expected", [
    ("Resident Evil Requiem ROG Ally Game Settings", "Resident Evil Requiem"),
    ("Clair Obscur: Expedition 33 ROG Xbox Ally X Game Settings",
     "Clair Obscur: Expedition 33"),
    ("Best Settings For Clair Obscur Expedition 33 On ROG Ally",
     "Clair Obscur Expedition 33"),
    ("FINAL FANTASY XVI - Community Game Settings", "FINAL FANTASY XVI"),
    ("MXGP 26 - The Official Game ROG Ally Game Settings", "MXGP 26 - The Official Game"),
    ("Star Wars Outlaws", "Star Wars Outlaws"),
])
def test_post_title_decoration_is_stripped(title, expected):
    assert clean_game_title(title) == expected


# -- profile label parsing ---------------------------------------------------

@pytest.mark.parametrize("text,watts", [
    ("900P 15/18W", [15, 18]),
    ("1080P 18/25/30W", [18, 25, 30]),
    ("25W Turbo", [25]),
    ("no wattage here", []),
])
def test_tdp_parsing(text, watts):
    assert parse_tdp_watts(text) == watts


def test_resolution_and_vram_parsing():
    assert parse_resolution("900P 15/18W") == "900p"
    assert parse_resolution("1080P") == "1080p"
    assert parse_vram_gb("8GB VRAM") == 8
    assert parse_vram_gb("no vram") is None


def test_profile_label_is_stable():
    profile = SourceProfile(name="900P 15/18W 8GB VRAM")
    assert profile.label == "15/18W • 900p • 8GB VRAM"
    assert profile.min_tdp == 15 and profile.max_tdp == 18


@pytest.mark.parametrize("heading,is_profile", [
    ("900P 15/18W", True), ("1080P 18/25/30W", True), ("25W", True),
    ("1080P", True), ("Conclusion", False), ("About the game", False),
])
def test_profile_heading_detection(heading, is_profile):
    assert is_profile_heading(heading) is is_profile


# -- whole pages -------------------------------------------------------------

def test_multiple_profiles_are_parsed_separately():
    game = parse_post(fixture("two_profiles_table.html"), url=ALLY_URL)
    assert game.title == "Example Adventure"
    assert game.device_family == "rog_ally_family"
    assert [p.label for p in game.profiles] == [
        "15/18W • 900p", "18/25/30W • 1080p"]
    low, high = game.profiles
    assert low.setting("texture_quality").value == "Medium"
    assert high.setting("texture_quality").value == "High"
    assert low.setting("fsr_mode").value == "Performance"
    assert high.setting("fsr_mode").value == "Quality"


def test_settings_stated_once_apply_to_every_profile():
    """VRAM appears before any profile heading and belongs to both."""
    game = parse_post(fixture("two_profiles_table.html"), url=ALLY_URL)
    assert all(p.setting("vram_allocation_gb") is not None for p in game.profiles)


def test_unknown_settings_are_kept_not_dropped():
    game = parse_post(fixture("single_profile_list.html"), url=ALLY_URL)
    labels = {s.label: s for s in game.profiles[0].settings}
    assert "Completely Unknown Knob" in labels
    assert labels["Completely Unknown Knob"].canonical == ""
    assert labels["Completely Unknown Knob"].value == "Maximum"


def test_a_page_with_no_settings_yields_no_profiles():
    """Never invent a profile for a page that has none."""
    game = parse_post(fixture("no_profiles.html"), url=ALLY_URL)
    assert game.profiles == []


def test_settings_without_headings_become_one_profile():
    game = parse_post(fixture("implicit_profile.html"), url=ALLY_URL)
    assert len(game.profiles) == 1
    assert game.profiles[0].setting("vsync").value == "On"


def test_malformed_markup_does_not_raise():
    game = parse_post(fixture("malformed.html"), url=ALLY_URL)
    assert isinstance(game, SourceGame)
    assert game.parser_version == PARSER_VERSION


def test_missing_fields_stay_empty_rather_than_guessed():
    game = parse_post("<article><h1>X ROG Ally Game Settings</h1></article>", url=ALLY_URL)
    assert game.performance_rating is None
    assert game.release_date == ""
    assert game.store_links == []
    assert game.profiles == []


def test_rating_and_store_link_are_extracted():
    game = parse_post(fixture("two_profiles_table.html"), url=ALLY_URL)
    assert game.performance_rating == 4.5
    assert game.rating_word == "great"
    assert any("steampowered" in link for link in game.store_links)
    assert game.image_url.endswith("example.jpg")


def test_prose_is_not_captured_as_a_setting():
    game = parse_post(
        "<article><h2>900P 18W</h2><p>Performance: this runs fine. We saw 40 FPS "
        "in most areas.</p><p>Texture Quality: Low</p></article>", url=ALLY_URL)
    labels = {s.label for s in game.profiles[0].settings}
    assert "Texture Quality" in labels
    assert "Performance" not in labels


@pytest.mark.parametrize("stars,word", [
    (5.0, "excellent"), (4.5, "great"), (4.0, "good"),
    (3.5, "average"), (3.0, "playable"), (2.5, "unplayable"), (1.0, "unplayable"),
])
def test_rating_scale_matches_the_sites_own(stars, word):
    assert rating_meaning(stars) == word


# -- change detection --------------------------------------------------------

def test_content_hash_is_stable_across_whitespace():
    assert content_hash("a  b\n c") == content_hash("a b c")


def test_content_hash_changes_when_a_value_changes():
    base = fixture("two_profiles_table.html")
    changed = base.replace("<td>Medium</td>", "<td>High</td>")
    assert (parse_post(base, url=ALLY_URL).content_hash
            != parse_post(changed, url=ALLY_URL).content_hash)


def test_reparsing_the_same_page_gives_the_same_hash():
    body = fixture("two_profiles_table.html")
    assert parse_post(body, url=ALLY_URL).content_hash == \
           parse_post(body, url=ALLY_URL).content_hash


# -- index pages -------------------------------------------------------------

def test_index_lists_only_settings_posts():
    urls = [u for _, u in parse_index(fixture("index_page.html"),
                                      base_url="https://rogallylife.com")]
    assert all(device_family_from_url(u) for u in urls)
    assert not any("/about/" in u for u in urls)
    assert not any(u.endswith("/community-game-settings/") for u in urls)


def test_index_covers_both_device_families():
    urls = [u for _, u in parse_index(fixture("index_page.html"),
                                      base_url="https://rogallylife.com")]
    families = {device_family_from_url(u) for u in urls}
    assert families == {"rog_ally_family", "rog_xbox_ally_family"}


def test_index_relative_urls_are_absolutised():
    urls = [u for _, u in parse_index(fixture("index_page.html"),
                                      base_url="https://rogallylife.com")]
    assert all(u.startswith("https://rogallylife.com/") for u in urls)
