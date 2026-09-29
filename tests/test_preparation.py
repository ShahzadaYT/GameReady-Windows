"""Preparation state machine, launcher adapters and game identity tests."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready import preparation as prep
from travelready.classification import split_games
from travelready.environment import offline_environment
from travelready.identity import (
    Installation, build_identities, find_identity, summarise,
)
from travelready.launchers import (
    FULL, NONE, PARTIAL, UNKNOWN, ADAPTERS, adapter_for, capability_matrix,
)
from travelready.library import GameEntry, load_library


def _entry(**kw):
    base = dict(name="Test Game", launcher="steam", expected_process="test.exe",
                exe_path=r"C:\Games\Test\test.exe", install_dir=r"C:\Games\Test")
    base.update(kw)
    return GameEntry(**base)


def _identity(entry):
    return build_identities([entry])[0]


# -- launcher adapters -------------------------------------------------------

def test_every_launcher_has_an_adapter():
    for launcher in ("steam", "xbox", "ea", "epic", "ubisoft", "gog",
                     "battlenet", "other"):
        assert adapter_for(launcher).launcher in (launcher, "other")


def test_capabilities_are_levels_not_booleans():
    caps = adapter_for("ea").capabilities()
    assert caps.launch.level == FULL
    assert caps.detect_process.level == PARTIAL
    assert caps.detect_process.detail, "a PARTIAL must say what it depends on"


def test_ea_process_detection_is_partial_not_full():
    """The EA regression came from assuming otherwise."""
    assert adapter_for("ea").capabilities().detect_process.level == PARTIAL


def test_gog_needs_no_sign_in_because_it_is_drm_free():
    assert adapter_for("gog").is_authenticated(_entry()) is True
    assert adapter_for("gog").capabilities().prepare_for_offline.level == FULL


def test_launchers_with_no_supported_check_return_none_not_false():
    """None means 'cannot determine'. False would be a claim we cannot make."""
    for launcher in ("ea", "xbox", "epic", "ubisoft"):
        assert adapter_for(launcher).is_authenticated(_entry()) is None


def test_battlenet_offline_is_reported_as_unsupported():
    assert adapter_for("battlenet").capabilities().prepare_for_offline.level == NONE


def test_verification_signal_reflects_what_is_known():
    ea = adapter_for("ea")
    blind = _entry(launcher="ea", expected_process="", exe_path="", install_dir="",
                   launch_method="uri", launch_target="link2ea://launch/1")
    assert ea.verification_signal(blind).level == NONE
    assert ea.verification_signal(_entry(launcher="ea")).level == FULL


def test_no_adapter_claims_to_read_credentials():
    import inspect

    from travelready import launchers

    source = inspect.getsource(launchers).lower()
    for token in ("password", "token file", "credential", "cookie", "oauth"):
        assert token not in source.replace("no adapter reads credentials", "")


def test_capability_matrix_renders():
    text = capability_matrix()
    assert "Steam" in text and "Battle.net" in text
    assert FULL in text and PARTIAL in text


# -- identity ----------------------------------------------------------------

def test_one_game_on_two_launchers_is_one_identity():
    entries = [_entry(name="Hogwarts Legacy", launcher="steam"),
               _entry(name="Hogwarts Legacy", launcher="xbox",
                      exe_path="", install_dir="", launch_method="shell",
                      launch_target="shell:AppsFolder\\WB.PHX_x!Game",
                      expected_process="")]
    identities = build_identities(entries)
    assert len(identities) == 1
    assert identities[0].launchers == ["steam", "xbox"]
    assert len(identities[0].installations) == 2


def test_launch_target_and_verification_target_stay_separate():
    """The distinction the Xbox regression collapsed."""
    entry = _entry(name="Xbox Game", launcher="xbox", exe_path="",
                   launch_method="shell",
                   launch_target="shell:AppsFolder\\Pub.Game_x!Game",
                   expected_process="game.exe")
    installation = Installation(entry=entry)
    assert installation.launch_target == "shell:AppsFolder\\Pub.Game_x!Game"
    assert installation.verification_targets == ["game.exe"]


def test_best_installation_prefers_a_verifiable_copy():
    verifiable = _entry(name="Game", launcher="xbox", expected_process="g.exe")
    blind = _entry(name="Game", launcher="ea", expected_process="", exe_path="",
                   install_dir="", launch_method="uri",
                   launch_target="link2ea://launch/1")
    identity = build_identities([blind, verifiable])[0]
    assert identity.best_installation().launcher == "xbox"


def test_canonical_title_prefers_the_punctuated_spelling():
    entries = [_entry(name="Clair Obscur- Expedition 33", launcher="xbox"),
               _entry(name="Clair Obscur: Expedition 33", launcher="steam")]
    identity = build_identities(entries)[0]
    assert identity.canonical_title == "Clair Obscur: Expedition 33"
    assert "Clair Obscur- Expedition 33" in identity.aliases


def test_identity_lookup_by_alias():
    entries = [_entry(name="Clair Obscur- Expedition 33", launcher="xbox"),
               _entry(name="Clair Obscur: Expedition 33", launcher="steam")]
    identities = build_identities(entries)
    assert find_identity(identities, "Clair Obscur- Expedition 33") is not None


def test_non_games_are_excluded_from_identities():
    entries = [_entry(name="Real Game"), _entry(name="Calculator", launcher="xbox")]
    assert [i.canonical_title for i in build_identities(entries)] == ["Real Game"]


def test_identities_over_the_real_library():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, _non = split_games(entries)
    identities = build_identities(games)
    stats = summarise(identities)
    assert stats["games"] == len(identities)
    assert stats["installations"] >= stats["games"]
    assert stats["verifiable"] > 0


# -- checks ------------------------------------------------------------------

def test_off_windows_checks_are_unknown_not_failed():
    """'Cannot determine' must never be reported as 'broken'."""
    report = prep.assess(_identity(_entry()), env=offline_environment())
    installed = report.check("installed")
    assert installed.outcome == prep.UNKNOWN
    assert "off Windows" in installed.reason


def test_a_game_never_launched_needs_action():
    report = prep.assess(_identity(_entry()), env=offline_environment())
    first = report.check("first_launch")
    assert first.outcome == prep.FAIL
    assert "never been verified" in first.reason
    assert first.fix


def test_a_recently_verified_game_passes():
    from datetime import datetime, timezone

    entry = _entry(last_result="PASS",
                   last_ready=datetime.now(timezone.utc).isoformat())
    report = prep.assess(_identity(entry), env=offline_environment())
    assert report.check("first_launch").outcome == prep.PASS


def test_an_old_pass_warns_about_lapsed_licences():
    from datetime import datetime, timedelta, timezone

    entry = _entry(last_result="PASS",
                   last_ready=(datetime.now(timezone.utc)
                               - timedelta(days=90)).isoformat())
    check = prep.assess(_identity(entry), env=offline_environment()).check("first_launch")
    assert check.outcome == prep.WARN
    assert "lapse" in check.reason


def test_an_unverifiable_game_warns_but_does_not_fail():
    """A game TravelReady can start but not verify is still preparable."""
    entry = _entry(launcher="ea", expected_process="", exe_path="", install_dir="",
                   launch_method="uri", launch_target="link2ea://launch/1")
    check = prep.assess(_identity(entry), env=offline_environment()).check("verification")
    assert check.outcome == prep.WARN
    assert check.fix


def test_sign_in_is_unknown_not_assumed():
    check = prep.assess(_identity(_entry(launcher="ea")),
                        env=offline_environment()).check("authentication")
    assert check.outcome == prep.UNKNOWN
    assert "signed in" in check.fix.lower()


def test_battlenet_offline_support_fails_and_says_so():
    entry = _entry(launcher="battlenet")
    report = prep.assess(_identity(entry), env=offline_environment())
    assert report.check("offline_support").outcome == prep.FAIL
    assert report.readiness == prep.UNSUPPORTED
    assert report.state == prep.UNSUPPORTED_STATE


def test_a_bad_launch_target_fails_with_the_reason():
    entry = _entry(launch_method="uri", launch_target="not-a-uri", exe_path="")
    check = prep.assess(_identity(entry), env=offline_environment()).check("launch_target")
    assert check.outcome == prep.FAIL
    assert check.fix


def test_settings_unknown_when_the_cache_was_never_synced():
    """'Not synced' and 'no recommendation' are different facts."""
    check = prep.assess(_identity(_entry()), env=offline_environment()).check("settings")
    assert check.outcome == prep.UNKNOWN
    assert "settings update" in check.fix


def test_settings_check_never_blocks_readiness():
    from datetime import datetime, timezone

    entry = _entry(last_result="PASS",
                   last_ready=datetime.now(timezone.utc).isoformat())
    report = prep.assess(_identity(entry), env=offline_environment())
    assert report.check("settings").outcome != prep.FAIL
    assert report.readiness != prep.ACTION_REQUIRED


def test_every_bad_check_offers_a_fix():
    entry = _entry(launcher="ea", expected_process="", exe_path="", install_dir="",
                   launch_method="uri", launch_target="link2ea://launch/1")
    report = prep.assess(_identity(entry), env=offline_environment())
    for check in report.failures:
        assert check.fix, f"{check.id} fails with no way to fix it"
    assert report.actions


def test_report_round_trips_through_json():
    report = prep.assess(_identity(_entry()), env=offline_environment())
    restored = prep.PreparationReport.from_dict(report.to_dict())
    assert restored.game == report.game
    assert restored.readiness == report.readiness
    assert len(restored.checks) == len(report.checks)


def test_describe_lists_the_checks_and_the_actions():
    entry = _entry(launcher="ea", expected_process="", exe_path="", install_dir="",
                   launch_method="uri", launch_target="link2ea://launch/1")
    text = prep.assess(_identity(entry), env=offline_environment()).describe()
    assert "ACTION REQUIRED" in text
    assert "What to do:" in text


def test_summaries_add_up():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, _non = split_games(entries)
    identities = build_identities(games)
    reports = prep.assess_all(identities, env=offline_environment())
    counts = prep.summarise(reports)
    assert counts["total"] == len(identities)
    assert sum(counts[v] for v in prep.READINESS_ORDER) == counts["total"]
    per_launcher = prep.summarise_by_launcher(reports)
    assert sum(r["total"] for r in per_launcher.values()) == len(reports)
