"""Whole-application security pass.

Covers the inputs the brief names explicitly, across every surface that takes
external data: launcher URIs, imported library metadata, filesystem and cache
paths, and the recommendation source's URLs.
"""
from __future__ import annotations

import json

import pytest

from travelready import launch_tester as lt
from travelready.library import GameEntry, import_games
from travelready.optimiser.model import BLOCKED, SAFE
from travelready.optimiser.rogallylife.cache import ProfileCache, entry_key
from travelready.optimiser.rogallylife.client import FetchError, RogAllyLifeClient
from travelready.optimiser.rogallylife.model import SourceGame
from travelready.optimiser.safety import classify_path, classify_setting

#: The exact characters and hosts the brief calls out.
HOSTILE = ["&", "|", ";", ">", "<", '"', "'", "%", "..\\", "../", "\\\\",
           "127.0.0.1", "localhost", "169.254.169.254", "file://"]


# -- launcher URIs -----------------------------------------------------------

@pytest.mark.parametrize("fragment", HOSTILE)
def test_hostile_fragments_never_reach_a_shell(fragment):
    """Whatever the target, no subprocess is spawned for a shell launch."""
    import os as os_mod
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    opened, spawned = [], []

    class ForbiddenPopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            raise AssertionError(f"must not spawn: {args}")

    entry = GameEntry(name="G", launcher="steam", launch_method="uri",
                      launch_target=f"steam://rungameid/1{fragment}payload.exe")
    orig_start = getattr(os_mod, "startfile", None)
    orig_popen, orig_win = sp.Popen, lt_mod.IS_WINDOWS
    os_mod.startfile = lambda t: opened.append(t)
    sp.Popen, lt_mod.IS_WINDOWS = ForbiddenPopen, True
    try:
        lt_mod._start_process(entry, lt.build_command(entry))
    except ValueError:
        pass                                   # refused before launching: also fine
    finally:
        sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
        if orig_start is None:
            del os_mod.startfile
        else:
            os_mod.startfile = orig_start
    assert spawned == [], "a shell launch must never spawn a process"
    assert all("cmd" not in str(t).lower() for t in opened)


@pytest.mark.parametrize("target", [
    "file:///C:/Windows/System32/calc.exe",
    "\\\\evil-host\\share\\payload.exe",
    "steam://a\x00b",
    "steam://a\nb",
])
def test_dangerous_target_shapes_are_refused(target):
    assert lt.validate_launch_target(lt.METHOD_URI, target) != ""


def test_epic_uri_with_an_ampersand_still_works():
    """Legitimate URI characters must not be banned to 'fix' injection."""
    assert lt.validate_launch_target(
        lt.METHOD_URI,
        "com.epicgames.launcher://apps/abc?action=launch&silent=true") == ""


def test_no_module_invokes_a_shell():
    """No shell=True, no cmd /c, anywhere in the application."""
    import ast
    import pathlib

    for path in pathlib.Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert not (isinstance(node.value, ast.Constant)
                            and node.value.value), f"{path} uses shell=True"
            # cmd.exe appears legitimately in the never-adopt-as-the-game
            # exclusion list, so check what is *invoked*, not every constant.
            if isinstance(node, ast.Call):
                for arg in node.args[:1]:
                    literals = ([arg] if isinstance(arg, ast.Constant)
                                else getattr(arg, "elts", [])[:1])
                    for item in literals:
                        if isinstance(item, ast.Constant) and \
                                isinstance(item.value, str):
                            assert item.value.lower() not in ("cmd", "cmd.exe"), \
                                f"{path}:{node.lineno} invokes cmd.exe"


# -- imported library metadata -----------------------------------------------

@pytest.mark.parametrize("fragment", HOSTILE)
def test_imported_metadata_cannot_execute_anything(fragment, tmp_path):
    import subprocess as sp

    import travelready.launch_tester as lt_mod

    path = tmp_path / "library.json"
    path.write_text(json.dumps({"version": "3", "games": [{
        "name": f"Evil{fragment}", "launcher": "steam", "launch_method": "uri",
        "launch_target": f"{fragment}C:\\payload.exe"}]}))
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
        assert status == lt.STATUS_FAIL
    except ValueError:
        pass
    finally:
        sp.Popen, lt_mod.IS_WINDOWS = orig_popen, orig_win
    assert spawned == []


def test_imported_metadata_cannot_redirect_a_settings_write(tmp_path):
    for path in (r"C:\Windows\System32\config.ini",
                 r"C:\Users\S\AppData\Local\..\..\Windows\x.ini",
                 r"\\evil-host\share\config.ini",
                 r"C:\Program Files\Game\EasyAntiCheat\settings.ini"):
        assert classify_path(path).safety == BLOCKED


def test_imported_metadata_cannot_widen_a_setting():
    for key, value in (("disable_anticheat", "1"), ("drm_bypass", "1"),
                       ("graphics_quality", "payload.exe"),
                       ("texture_quality", "..\\..\\x")):
        assert classify_setting(key, value).safety != SAFE


# -- the source client: no SSRF ----------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/x-rog-ally-game-settings/",
    "https://rogallylife.com.evil.example/x-rog-ally/",
    "https://evil.example/?x=https://rogallylife.com/",
    "file:///etc/passwd",
])
def test_the_source_client_refuses_every_other_host(url):
    client = RogAllyLifeClient(opener=lambda *a, **k: None, delay=0,
                               sleep=lambda s: None, respect_robots=False)
    with pytest.raises(FetchError, match="host is not"):
        client.get(url)


def test_a_hostile_sitemap_cannot_redirect_the_crawler():
    """Sitemap entries are external input and go through the same check."""
    from travelready.optimiser.rogallylife.client import _parse_sitemap

    entries = _parse_sitemap(
        "<urlset><url><loc>http://169.254.169.254/x-rog-ally/</loc></url>"
        "<url><loc>https://rogallylife.com/2026/01/01/a-rog-ally/</loc></url>"
        "</urlset>")
    client = RogAllyLifeClient(opener=lambda *a, **k: None, delay=0,
                               sleep=lambda s: None, respect_robots=False)
    with pytest.raises(FetchError):
        client.get(entries[0][0])
    assert client._absolute(entries[1][0]).startswith("https://rogallylife.com/")


# -- cache and filesystem paths ----------------------------------------------

@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "..\\..\\windows\\system32", "..%2f..%2fx",
    "....//x", ".", "..", "", "a/b/c", "\x00evil", "con", "nul",
])
def test_cache_keys_stay_inside_the_cache(slug, tmp_path):
    game = SourceGame(title="X", source_url="https://rogallylife.com/x/",
                      device_family="rog_ally_family", slug=slug)
    key = entry_key(game)
    assert ".." not in key and "/" not in key and "\\" not in key
    cache = ProfileCache(tmp_path)
    assert cache.path_for(key).resolve().parent == (tmp_path / "games").resolve()


def test_backup_paths_are_built_from_sanitised_names(tmp_path):
    from travelready.optimiser.transaction import create_backup

    config = tmp_path / "AppData" / "Local" / "G" / "Saved" / "Config" / "WindowsNoEditor"
    config.mkdir(parents=True)
    target = config / "GameUserSettings.ini"
    target.write_text("[a]\nb=1\n")
    backup = create_backup(str(target), "../../evil name", tmp_path / "backups")
    resolved = (tmp_path / "backups").resolve()
    assert str(resolved) in str(__import__("pathlib").Path(backup.backup_path).resolve())


# -- privilege and process safety --------------------------------------------

def test_nothing_requests_elevation():
    import pathlib

    for path in pathlib.Path("src").rglob("*.py"):
        source = path.read_text().lower()
        for token in ("shellexecute(none, 'runas'", "runas", "createprocessasuser",
                      "adjusttokenprivileges"):
            assert token not in source, f"{path} attempts privilege escalation"


def test_protected_processes_can_never_be_terminated():
    from travelready.processes import PROTECTED_PROCESSES, FakeProcessTable, ProcInfo

    for name in sorted(PROTECTED_PROCESSES)[:12]:
        table = FakeProcessTable([ProcInfo(99, name, f"C:\\x\\{name}", 1)])
        ok, message = table.terminate(99)
        assert not ok and "REFUSED" in message


def test_the_doctor_never_repairs_anything_external():
    from travelready import doctor as doctor_mod
    from travelready.environment import offline_environment

    report = doctor_mod.run_doctor(env=offline_environment(), check_network=False)
    for finding in report.repairable:
        assert finding.section in ("Application", "Settings", "ROG Ally Life",
                                   "Prepare-for-Travel")
