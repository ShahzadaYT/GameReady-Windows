"""CLI end-to-end tests against the real shipped library."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import FIXTURES, ROOT
from travelready.cli import main


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture
def library(tmp_path):
    path = tmp_path / "games.json"
    shutil.copy(FIXTURES / "games_2026-09-26.json", path)
    return str(path)


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_list_shows_the_whole_library(capsys, library):
    code, out, _ = run(capsys, "--library", library, "-q", "list")
    assert code == 0
    assert "Mafia: Definitive Edition" in out
    assert "All:162" in out


def test_list_filters_by_launcher(capsys, library):
    _, out, _ = run(capsys, "--library", library, "-q", "list", "--launcher", "ea")
    assert "EA:13" in out
    assert "Mafia: Definitive Edition" not in out


def test_list_json_is_machine_readable(capsys, library):
    _, out, _ = run(capsys, "--library", library, "-q", "list", "--json")
    rows = json.loads(out)
    assert len(rows) == 162
    assert {"id", "name", "launcher", "install_dir", "verification"} <= set(rows[0])


def test_status_reports_unverifiable_games_honestly(capsys, library):
    _, out, _ = run(capsys, "--library", library, "-q", "status")
    assert "cannot be verified automatically" in out
    assert "NOT READY" in out


def test_report_groups_by_readiness(capsys, library):
    _, out, _ = run(capsys, "--library", library, "-q", "report")
    assert "TRAVELREADY TRIP REPORT" in out
    assert "READY" in out and "UNTESTED" in out


def test_export_and_reimport(capsys, library, tmp_path):
    target = tmp_path / "exported.csv"
    run(capsys, "--library", library, "-q", "export", str(target))
    assert target.is_file()
    code, out, _ = run(capsys, "--library", library, "-q", "import", str(target))
    assert code == 0


def test_settings_show_says_when_no_profile_exists(capsys, library):
    code, out, _ = run(capsys, "--library", library, "-q",
                       "settings", "show", "Mafia: Definitive Edition")
    assert code == 0
    assert "NOT FOUND" in out
    assert "rogallylife.com" in out
    assert "Cached ROG Ally Life games: 0" in out
    assert "locally imported profiles: 0" in out
    assert "settings update" in out


def test_settings_show_reports_an_ambiguous_name(capsys, library):
    code, _, err = run(capsys, "--library", library, "-q", "settings", "show", "DOOM")
    assert code == 2
    assert "matched" in err


def test_profile_template_is_empty_and_attributed(capsys):
    code, out, _ = run(capsys, "profile-template", "Cyberpunk 2077")
    template = json.loads(out)
    assert code == 0
    assert template["recommendations"] == []
    assert template["source"] == "ROG Ally Life"
    assert template["device"] == "rog_ally_x"


def test_profile_import_rejects_an_unattributed_profile(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"game_name": "X", "device": "rog_ally_x",
                               "recommendations": [{"key": "resolution", "value": "1080p"}]}))
    code, _, err = run(capsys, "profile-import", str(bad))
    assert code == 1
    assert "source" in err


def test_profile_import_then_list(capsys, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "game_name": "Test Game", "device": "rog_ally_x", "source": "ROG Ally Life",
        "source_url": "https://rogallylife.com/test-game",
        "recommendations": [{"key": "frame_rate_limit", "value": "60"}]}))
    assert run(capsys, "profile-import", str(good))[0] == 0
    _, out, _ = run(capsys, "profile-list")
    assert "Test Game" in out and "rogallylife.com" in out


def test_settings_restore_with_no_backups(capsys, library):
    code, out, _ = run(capsys, "--library", library, "-q",
                       "settings", "restore", "--list")
    assert code == 2
    assert "No backups" in out


def test_version():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


# -- output redirected to a file -----------------------------------------------
#
# Regression: on Windows, redirected output is encoded with the ANSI code page
# (cp1252), which has no ✓ or →, so `doctor` and `scan` stopped with
# UnicodeEncodeError, shown by the packaged build as a blocking error dialog.
# A real process with stdout redirected to a file, forced to cp1252 so the
# same failure reproduces on every host.

def _run_redirected(tmp_path, *argv):
    out = tmp_path / "out.txt"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="cp1252",
               TRAVELREADY_DATA_DIR=str(tmp_path / "data"))
    with open(out, "wb") as stdout:
        proc = subprocess.run([sys.executable, "-m", "travelready", *argv],
                              stdout=stdout, stderr=subprocess.PIPE, env=env,
                              timeout=180)
    return (proc.returncode, out.read_bytes().decode("utf-8"),
            proc.stderr.decode("utf-8", "replace"))


def test_redirected_doctor_output_is_written_not_raised(tmp_path):
    code, out, err = _run_redirected(tmp_path, "doctor", "--offline")
    assert "UnicodeEncodeError" not in err
    assert code in (0, 1), err            # 1 means doctor found problems
    assert "✓" in out


def test_redirected_scan_output_is_written_not_raised(tmp_path):
    code, out, err = _run_redirected(tmp_path, "scan", "--sources", "epic")
    assert "UnicodeEncodeError" not in err
    assert code == 0, err
    assert "→" in out


def test_redirected_version_is_written(tmp_path):
    from travelready import __version__

    code, out, err = _run_redirected(tmp_path, "--version")
    assert code == 0, err
    assert out.strip() == f"TravelReady {__version__}"
