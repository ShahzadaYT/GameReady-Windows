"""Safety-classification tests.

These encode the absolute rule: anti-cheat, DRM, executables, DLLs, drivers,
protected processes and unknown targets can never be modified, and anything
uncertain fails closed.
"""
from __future__ import annotations

import pytest

from travelready.optimiser import safety as S
from travelready.optimiser.model import (
    BLOCKED, CAUTION, MANUAL, RESEARCH_REQUIRED, SAFE, is_auto_applicable, worst,
)

USER_CFG = r"C:\Users\S\AppData\Local\MyGame\Saved\Config\WindowsNoEditor\GameUserSettings.ini"


# -- anti-cheat, DRM and integrity -------------------------------------------

@pytest.mark.parametrize("path", [
    r"C:\Program Files\MyGame\EasyAntiCheat\settings.ini",
    r"C:\Program Files\MyGame\EasyAntiCheat_EOS\cfg.ini",
    r"C:\Program Files\MyGame\BattlEye\BEClient.cfg",
    r"C:\Program Files\Riot Vanguard\config.ini",
    r"C:\Users\S\AppData\Local\MyGame\denuvo\settings.ini",
    r"C:\Users\S\AppData\Local\MyGame\anticheat.ini",
    r"C:\Users\S\AppData\Local\MyGame\drm.cfg",
    r"C:\Users\S\AppData\Local\MyGame\license.ini",
    r"C:\Users\S\AppData\Local\MyGame\entitlement.json",
    r"C:\Users\S\AppData\Local\MyGame\authtoken.json",
    r"C:\Program Files\MyGame\steam_api.ini",
])
def test_anticheat_and_drm_paths_are_blocked(path):
    assert S.classify_path(path).safety == BLOCKED


@pytest.mark.parametrize("suffix", [
    ".exe", ".dll", ".sys", ".drv", ".msi", ".bat", ".cmd", ".ps1", ".efi",
    ".cat", ".sig", ".crt", ".pfx", ".key", ".reg", ".inf", ".msix", ".appx",
    ".jar", ".so", ".bin", ".dat", ".pak", ".zip",
])
def test_executables_libraries_drivers_and_binaries_are_blocked(suffix):
    path = rf"C:\Users\S\AppData\Local\MyGame\thing{suffix}"
    assert S.classify_path(path).safety == BLOCKED


@pytest.mark.parametrize("path", [
    r"C:\Windows\win.ini",
    r"C:\Windows\System32\config.ini",
    r"C:\Windows\System32\drivers\etc\hosts.cfg",
    r"C:\Program Files\WindowsApps\Pub.Game\config.ini",
    r"C:\Windows\SysWOW64\x.ini",
    r"C:\$Recycle.Bin\x.ini",
])
def test_protected_directories_are_blocked(path):
    assert S.classify_path(path).safety == BLOCKED


def test_path_traversal_is_rejected():
    for path in [r"C:\Users\S\AppData\Local\..\..\Windows\x.ini",
                 r"C:\Users\S\AppData\Local\Game\..\..\..\x.ini",
                 "C:/Users/S/AppData/Local/../x.ini"]:
        assert S.classify_path(path).safety == BLOCKED


def test_relative_and_empty_paths_are_rejected():
    for path in ["", "   ", "config.ini", r"..\config.ini", "\\x\x00y.ini"]:
        assert S.classify_path(path).safety == BLOCKED


# -- no false positives ------------------------------------------------------

def test_unreal_config_path_is_not_mistaken_for_the_windows_directory():
    """'WindowsNoEditor' is where GameUserSettings.ini actually lives."""
    assert S.classify_path(USER_CFG).safety == SAFE


@pytest.mark.parametrize("path", [
    r"C:\Users\S\Documents\My Games\Peaceful Nights\settings.ini",
    r"C:\Users\S\Documents\My Games\Breach\config.ini",
])
def test_innocent_titles_containing_marker_substrings_are_not_blocked(path):
    """'Peaceful' contains 'eac'; 'Breach' contains 'eac'. Neither is anti-cheat."""
    assert S.classify_path(path).safety != BLOCKED


# -- setting keys ------------------------------------------------------------

def test_unknown_setting_keys_fail_closed():
    assert S.classify_setting("mystery_knob", "1").safety == RESEARCH_REQUIRED
    assert not is_auto_applicable(S.classify_setting("mystery_knob", "1").safety)


def test_known_game_settings_can_be_safe():
    assert S.classify_setting("shadow_quality", "Medium").safety == SAFE


def test_device_settings_are_manual_not_automatic():
    for key in ("tdp_watts", "fan_profile", "vram_allocation_gb",
                "armoury_crate_profile", "adrenalin_profile", "windows_power_plan"):
        assert S.classify_setting(key, "20").safety == MANUAL


def test_setting_values_referencing_executables_are_blocked():
    assert S.classify_setting("graphics_quality", "run me.exe").safety == BLOCKED
    assert S.classify_setting("graphics_quality", r"..\..\x").safety == BLOCKED


def test_anticheat_named_settings_are_blocked():
    assert S.classify_setting("disable_anticheat", "true").safety == BLOCKED
    assert S.classify_setting("drm_bypass", "1").safety == BLOCKED


# -- device targeting --------------------------------------------------------

def test_profile_for_another_device_is_never_automatic():
    for device in ("rog_ally", "rog_xbox_ally", "rog_xbox_ally_x",
                   "steam_deck", "legion_go"):
        assert S.classify_device(device, "rog_ally_x").safety == MANUAL


def test_unknown_device_requires_research():
    assert S.classify_device("mystery_handheld", "rog_ally_x").safety == RESEARCH_REQUIRED
    assert S.classify_device("", "rog_ally_x").safety == RESEARCH_REQUIRED


def test_matching_device_is_safe():
    assert S.classify_device("rog_ally_x", "rog_ally_x").safety == SAFE


# -- composition -------------------------------------------------------------

def test_safety_composes_by_failing_closed():
    assert worst(SAFE, BLOCKED) == BLOCKED
    assert worst(SAFE, CAUTION) == CAUTION
    assert worst(SAFE, MANUAL, RESEARCH_REQUIRED) == RESEARCH_REQUIRED
    assert worst() == RESEARCH_REQUIRED


def test_only_safe_is_auto_applicable():
    assert is_auto_applicable(SAFE)
    for cls in (CAUTION, MANUAL, RESEARCH_REQUIRED, BLOCKED):
        assert not is_auto_applicable(cls)


def test_combined_classification_reports_the_limiting_reason():
    verdict = S.classify_target("shadow_quality", "Medium",
                                r"C:\Program Files\Game\EasyAntiCheat\x.ini",
                                "rog_ally_x", "rog_ally_x")
    assert verdict.safety == BLOCKED
    assert "anti-cheat" in verdict.reason.lower()


def test_no_percentage_or_probability_language_anywhere():
    """The brief forbids '95% safe' / 'probably safe' style output."""
    import inspect

    source = inspect.getsource(S)
    lowered = source.lower()
    for phrase in ("% safe", "probably safe", "low risk", "mostly safe",
                   "confidence score", "safety score"):
        assert phrase not in lowered


# -- binary content ----------------------------------------------------------

def test_binary_content_is_blocked_even_with_an_ini_extension():
    verdict = S.classify_file_content("x.ini", reader=lambda p: b"MZ\x90\x00\x03\x00")
    assert verdict.safety == BLOCKED


def test_text_content_passes():
    verdict = S.classify_file_content("x.ini", reader=lambda p: b"[a]\nb=1\n")
    assert verdict.safety == SAFE


def test_assert_writable_refuses_non_safe_targets(tmp_path):
    target = tmp_path / "GameUserSettings.ini"
    target.write_text("[a]\nb=1\n")
    with pytest.raises(PermissionError):
        S.assert_writable(str(target), "mystery_knob", "1", "rog_ally_x", "rog_ally_x")


def test_there_is_no_force_apply_escape_hatch():
    """No function anywhere exposes a parameter that bypasses classification.

    Checked against the parsed syntax tree rather than the text, so prose that
    merely mentions the absence of such a switch does not satisfy the test.
    """
    import ast
    import inspect

    from travelready.optimiser import diff, inspector, transaction

    banned = {"force_apply", "force", "ignore_safety", "skip_safety",
              "allow_blocked", "override_safety", "unsafe", "bypass"}
    for module in (S, transaction, diff, inspector):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                names = {a.arg for a in
                         args.args + args.posonlyargs + args.kwonlyargs}
                if args.vararg:
                    names.add(args.vararg.arg)
                if args.kwarg:
                    names.add(args.kwarg.arg)
                leaked = names & banned
                assert not leaked, (
                    f"{module.__name__}.{node.name} exposes {leaked}")
