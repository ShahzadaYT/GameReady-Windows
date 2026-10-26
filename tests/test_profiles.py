"""ROG Ally Life profile store tests."""
from __future__ import annotations

import json

import pytest

from travelready.optimiser import profiles as P
from travelready.optimiser.model import TARGET_DEVICE


VALID = {
    "game_name": "Cyberpunk 2077",
    "device": "rog_ally_x",
    "source": "ROG Ally Life",
    "source_url": "https://rogallylife.com/cyberpunk-2077-rog-ally-settings",
    "source_date": "2026-03-04",
    "launcher": "gog",
    "recommendations": [
        {"key": "resolution", "value": "1920x1080"},
        {"key": "graphics_quality", "value": "Medium"},
        {"key": "fsr_mode", "value": "Quality"},
        {"key": "frame_rate_limit", "value": "60"},
        {"key": "tdp_watts", "value": "20", "category": "device"},
    ],
}


def test_no_profiles_are_shipped():
    """Nothing was invented: rogallylife.com was unreachable when this was built."""
    store = P.ProfileStore.load([P.BUNDLED_PROFILE_DIR])
    assert len(store) == 0
    assert store.errors == []


def test_valid_profile_loads_with_attribution():
    profile = P.profile_from_dict(VALID)
    assert profile.source == "ROG Ally Life"
    assert "rogallylife.com" in profile.attribution
    assert "2026-03-04" in profile.attribution


def test_attribution_is_mandatory():
    for missing in ("source", "source_url"):
        data = {k: v for k, v in VALID.items() if k != missing}
        with pytest.raises(P.ProfileError, match=missing):
            P.profile_from_dict(data)


def test_source_url_must_be_a_url():
    data = dict(VALID, source_url="not a url")
    assert any("http" in p for p in P.validate_profile_dict(data))


def test_unknown_settings_are_rejected():
    data = dict(VALID, recommendations=[{"key": "disable_eac", "value": "1"}])
    with pytest.raises(P.ProfileError, match="not a setting"):
        P.profile_from_dict(data)


def test_unknown_top_level_fields_are_rejected():
    data = dict(VALID, run_command="calc.exe")
    with pytest.raises(P.ProfileError, match="Unknown profile fields"):
        P.profile_from_dict(data)


def test_a_recommendation_with_no_value_is_never_invented():
    data = dict(VALID, recommendations=[{"key": "resolution", "value": ""}])
    with pytest.raises(P.ProfileError, match="never invented"):
        P.profile_from_dict(data)


def test_unknown_device_is_rejected():
    data = dict(VALID, device="my_handheld")
    with pytest.raises(P.ProfileError, match="Unknown device"):
        P.profile_from_dict(data)


def test_game_and_device_settings_are_separated():
    profile = P.profile_from_dict(VALID)
    assert {r.key for r in profile.device_settings()} == {"tdp_watts"}
    assert "resolution" in {r.key for r in profile.game_settings()}


def test_import_validates_before_installing(tmp_path):
    with pytest.raises(P.ProfileError):
        P.import_profile({"game_name": "X"}, tmp_path)
    assert list(tmp_path.glob("*.json")) == []
    path = P.import_profile(VALID, tmp_path)
    assert path.is_file()
    assert P.load_profile(path).game_name == "Cyberpunk 2077"


def test_store_finds_by_identity_and_prefers_the_target_device(tmp_path):
    P.import_profile(VALID, tmp_path)
    P.import_profile(dict(VALID, device="steam_deck"), tmp_path)
    store = P.ProfileStore.load([tmp_path])
    assert len(store) == 2
    found = store.find("Cyberpunk 2077", TARGET_DEVICE)
    assert found.device == TARGET_DEVICE
    # edition noise should still match
    assert store.find("Cyberpunk 2077 Ultimate Edition", TARGET_DEVICE) is not None


def test_store_reports_broken_profiles_instead_of_failing(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    P.import_profile(VALID, tmp_path)
    store = P.ProfileStore.load([tmp_path])
    assert len(store) == 1
    assert store.errors and "broken.json" in store.errors[0]


def test_blank_template_contains_no_invented_values():
    template = P.blank_profile_template("Some Game")
    assert template["recommendations"] == []
    assert template["source"] == P.SOURCE_ROG_ALLY_LIFE
    assert "rogallylife.com" in template["source_url"]
    assert all(v in ("", [], P.PROFILE_SCHEMA_VERSION, "Some Game",
                     P.SOURCE_ROG_ALLY_LIFE, "rog_ally_x", template["source_url"])
               for v in template.values())


def test_attribution_never_claims_asus_endorsement():
    """The source string is echoed verbatim; nothing adds an ASUS claim."""
    profile = P.profile_from_dict(VALID)
    assert profile.attribution.startswith("ROG Ally Life")
    assert "asus" not in profile.attribution.lower()
    # a profile from some other source is reported as that source, not rebranded
    other = P.profile_from_dict(dict(VALID, source="Some Forum Post"))
    assert other.attribution.startswith("Some Forum Post")
