"""Security tests for the ROG Ally Life integration.

Two attack surfaces come with an external source: the launch targets that a
fetched or imported record could carry, and the source data itself, which is
untrusted text that must not be able to widen what TravelReady will do.
"""
from __future__ import annotations

import json

import pytest

from travelready import launch_tester as lt
from travelready.library import GameEntry, import_games
from travelready.optimiser.model import BLOCKED, SAFE
from travelready.optimiser.rogallylife.bridge import SourceResolver, to_game_profile
from travelready.optimiser.rogallylife.cache import ProfileCache
from travelready.optimiser.rogallylife.capability import canonical_key
from travelready.optimiser.rogallylife.model import (
    SourceGame, SourceProfile, SourceSetting,
)
from travelready.optimiser.rogallylife.parser import parse_post
from travelready.optimiser.rogallylife.select import select_profile
from travelready.optimiser.safety import classify_path, classify_setting

ALLY_URL = "https://rogallylife.com/2026/02/27/x-rog-ally-game-settings/"


# -- launch targets ----------------------------------------------------------

@pytest.mark.parametrize("target", [
    "&calc.exe",
    "calc.exe",
    "not-a-uri",
    "steam://x\ncalc.exe",
    "steam://x\x00calc.exe",
    "   ",
    "",
    "\tsteam://x",
])
def test_malformed_or_bare_targets_are_refused(target):
    assert lt.validate_launch_target(lt.METHOD_URI, target) != ""


@pytest.mark.parametrize("target", [
    "steam://rungameid/1030840",
    "link2ea://launch/71067",
    "com.epicgames.launcher://apps/abc?action=launch&silent=true",
    "steam://rungameid/1?a=1&b=2",
    "uplay://launch/123/0",
])
def test_legitimate_launcher_uris_are_accepted(target):
    """'&' is legitimate in a launcher URI and must not be banned."""
    assert lt.validate_launch_target(lt.METHOD_URI, target) == ""


@pytest.mark.parametrize("target", [
    "shell:AppsFolder\\KeplerInteractive.Expedition33_ym!Game",
    "shell:AppsFolder\\Microsoft.ForteBaseGame_8wekyb3d8bbwe!Game",
])
def test_shell_routes_are_accepted(target):
    assert lt.validate_launch_target(lt.METHOD_SHELL, target) == ""


def test_a_quoted_or_spaced_target_still_never_reaches_a_shell():
    """Whatever the target, no subprocess may be spawned for a shell launch."""
    import os as os_mod
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    opened, spawned = [], []

    class ForbiddenPopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            raise AssertionError(f"must not spawn: {args}")

    for target in ['steam://a"b&c', "steam://a b&c", "steam://\u00e9\u4f60\u597d",
                   "steam://a'b|c>d"]:
        entry = GameEntry(name="G", launcher="steam", launch_method="uri",
                          launch_target=target)
        orig_start = getattr(os_mod, "startfile", None)
        orig_popen, orig_win = sp.Popen, lt_mod.IS_WINDOWS
        os_mod.startfile = lambda t: opened.append(t)
        sp.Popen, lt_mod.IS_WINDOWS = ForbiddenPopen, True
        try:
            lt_mod._start_process(entry, lt.build_command(entry))
        finally:
            sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
            if orig_start is None:
                del os_mod.startfile
            else:
                os_mod.startfile = orig_start
    assert spawned == []
    assert all("cmd" not in str(t) for t in opened)


def test_unicode_targets_round_trip_unchanged():
    import os as os_mod
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    target = "steam://rungameid/1\u00e9\u4f60"
    opened = []
    orig_start = getattr(os_mod, "startfile", None)
    orig_win = lt_mod.IS_WINDOWS
    os_mod.startfile = lambda t: opened.append(t)
    lt_mod.IS_WINDOWS = True
    try:
        entry = GameEntry(name="G", launcher="steam", launch_method="uri",
                          launch_target=target)
        lt_mod._start_process(entry, lt.build_command(entry))
    finally:
        lt_mod.IS_WINDOWS = orig_win
        if orig_start is None:
            del os_mod.startfile
        else:
            os_mod.startfile = orig_start
    assert opened == [target]


def test_cmd_start_is_not_reachable_from_any_launch_path():
    """The injectable construct must not reappear anywhere in the engine."""
    import ast
    import inspect

    import travelready.launch_tester as lt_mod

    tree = ast.parse(inspect.getsource(lt_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.lower() not in ("cmd", "cmd.exe"), \
                "launch_tester must not invoke cmd.exe"


# -- hostile source data -----------------------------------------------------

def _game_with(label: str, value: str) -> SourceGame:
    return SourceGame(
        title="Hostile", source_url=ALLY_URL, device_family="rog_ally_family",
        profiles=[SourceProfile(name="900P 18W",
                                settings=[SourceSetting(label, value,
                                                        canonical_key(label))])])


@pytest.mark.parametrize("label,value", [
    ("Anti-Cheat", "Disabled"),
    ("EasyAntiCheat", "Off"),
    ("DRM", "Bypass"),
    ("Denuvo", "Remove"),
    ("Config File", "C:\\Windows\\System32\\config.ini"),
    ("Run", "payload.exe"),
    ("Patch", "..\\..\\game.dll"),
])
def test_a_hostile_recommendation_cannot_become_applicable(label, value):
    """A profile is data. It cannot widen what TravelReady will change."""
    game = _game_with(label, value)
    selection = select_profile(game)
    profile = to_game_profile(game, selection)
    if profile is None:
        return
    for recommendation in profile.recommendations:
        verdict = classify_setting(recommendation.key, recommendation.value)
        assert verdict.safety != SAFE or recommendation.category != "game", (
            f"{label}={value} became applicable as {recommendation.key}")


def test_a_recognised_key_with_an_executable_value_is_blocked():
    assert classify_setting("graphics_quality", "run payload.exe").safety == BLOCKED
    assert classify_setting("texture_quality", "..\\..\\x").safety == BLOCKED


def test_source_data_cannot_redirect_a_write_outside_user_config():
    for path in ("C:\\Windows\\System32\\config.ini",
                 "C:\\Program Files\\Game\\EasyAntiCheat\\settings.ini",
                 "C:\\Users\\S\\AppData\\Local\\..\\..\\Windows\\x.ini"):
        assert classify_path(path).safety == BLOCKED


def test_html_in_source_data_is_not_executed_or_reflected():
    game = parse_post(
        "<article><h2>900P 18W</h2>"
        "<p>Texture Quality: <script>alert(1)</script>Medium</p></article>",
        url=ALLY_URL)
    values = [s.value for p in game.profiles for s in p.settings]
    assert not any("<script" in v for v in values)


def test_enormous_source_values_are_rejected():
    assert classify_setting("texture_quality", "x" * 500).safety != SAFE


def test_a_malicious_imported_library_entry_is_still_refused(tmp_path):
    """The pre-existing protection must survive the new source plumbing."""
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    path = tmp_path / "library.json"
    path.write_text(json.dumps({"version": "3", "games": [{
        "name": "Free", "launcher": "steam", "launch_method": "uri",
        "launch_target": "not-a-uri-at-all&C:\\payload.exe"}]}))
    entries, _ = import_games(path)

    spawned = []

    class ForbiddenPopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            raise AssertionError("must not spawn")

    orig_popen, orig_win = sp.Popen, lt_mod.IS_WINDOWS
    sp.Popen, lt_mod.IS_WINDOWS = ForbiddenPopen, True
    try:
        _, _, status, err = lt_mod._start_process(entries[0],
                                                  lt.build_command(entries[0]))
    finally:
        sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
    assert status == lt.STATUS_FAIL and "Refusing to launch" in err
    assert spawned == []


# -- the client does not evade access controls -------------------------------

def test_no_translation_or_mirror_host_is_used():
    """A blocked host must be reported, never routed around."""
    import inspect

    from travelready.optimiser.rogallylife import client, known_urls, sync

    for module in (client, sync, known_urls):
        source = inspect.getsource(module).lower()
        for host in ("translate.goog", "translate.google", "webcache",
                     "cachedview", "r.jina.ai", "corsproxy", "allorigins"):
            assert host not in source, f"{module.__name__} references {host}"


def test_client_has_no_authentication_or_header_spoofing():
    import inspect

    from travelready.optimiser.rogallylife import client

    source = inspect.getsource(client).lower()
    for token in ("authorization", "cookie", "x-forwarded-for", "basic auth",
                  "api_key", "password"):
        assert token not in source


# -- the client cannot be steered off its own host ---------------------------

def test_client_refuses_a_url_on_another_host():
    """Sitemap and index links are external input; the host is pinned."""
    from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient

    client = RogAllyLifeClient(opener=lambda *a, **k: None, delay=0,
                               sleep=lambda s: None, respect_robots=False)
    for url in ("https://evil.example/x-rog-ally-game-settings/",
                "http://127.0.0.1:8080/admin",
                "http://169.254.169.254/latest/meta-data/",
                "https://rogallylife.com.evil.example/x-rog-ally/"):
        with pytest.raises(FetchError, match="host is not"):
            client.get(url)


def test_client_accepts_its_own_host_and_www():
    from travelready.optimiser.rogallylife.client import RogAllyLifeClient

    client = RogAllyLifeClient(respect_robots=False)
    assert client._absolute("https://rogallylife.com/a/").startswith("https://rogallylife.com/")
    assert client._absolute("https://www.rogallylife.com/a/")
    assert client._absolute("/relative/").startswith("https://rogallylife.com/")


@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "..%2f..%2fx", "....//x", ".", "..", "", "a/b/c",
])
def test_cache_keys_cannot_escape_the_games_directory(slug, tmp_path):
    from travelready.optimiser.rogallylife.cache import ProfileCache, entry_key

    game = SourceGame(title="X", source_url=ALLY_URL,
                      device_family="rog_ally_family", slug=slug)
    key = entry_key(game)
    assert ".." not in key
    assert "/" not in key and "\\" not in key
    cache = ProfileCache(tmp_path)
    path = cache.path_for(key).resolve()
    assert path.parent == (tmp_path / "games").resolve()
