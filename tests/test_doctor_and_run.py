"""Doctor and resumable Prepare-for-Travel tests."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from conftest import FIXTURES
from travelready import doctor as doctor_mod
from travelready import prepare_run as pr
from travelready.classification import split_games
from travelready.environment import offline_environment
from travelready.identity import build_identities
from travelready.library import GameEntry, load_library


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture
def identities():
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    games, _non = split_games(entries)
    return build_identities(games)[:12]


# -- doctor ------------------------------------------------------------------

def test_doctor_runs_without_windows_or_network():
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    assert report.findings
    assert "TravelReady doctor" in report.describe()


def test_doctor_covers_every_section():
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    sections = set(report.by_section())
    assert {"Application", "Environment", "Launchers", "Game library",
            "ROG Ally Life", "Settings", "Prepare-for-Travel"} <= sections


def test_doctor_reports_the_library_it_was_given():
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    library = report.by_section()["Game library"]
    assert any("162 entries" in f.detail for f in library)
    assert any(f.name == "Classified as games" for f in library)


def test_doctor_flags_a_missing_library(tmp_path):
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=tmp_path / "nope.json",
                                   check_network=False)
    library = report.by_section()["Game library"]
    assert any("does not exist" in f.detail for f in library)
    assert any("scan" in (f.fix_hint or "") for f in library)


def test_doctor_flags_a_corrupt_library(tmp_path):
    bad = tmp_path / "games.json"
    bad.write_text("{not json")
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=bad, check_network=False)
    assert report.problems
    assert not report.healthy


def test_every_problem_and_warning_can_be_acted_on():
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    text = report.describe()
    for finding in report.problems:
        assert finding.fix_hint, f"{finding.name} is a problem with no guidance"
    assert "What to do" in text or "Everything checks out" in text \
        or "warning(s)" in text


def test_fix_only_touches_travelready_state(tmp_path):
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    for finding in report.repairable:
        assert finding.section in ("Application", "Settings", "ROG Ally Life",
                                   "Prepare-for-Travel"), \
            f"{finding.section} repairs are not TravelReady's own state"


def test_fix_never_offers_to_repair_credentials_or_drm():
    import inspect

    source = inspect.getsource(doctor_mod).lower()
    # the repair callables must not mention anything account- or DRM-shaped
    for token in ("password", "credential", "token=", "licence key", "drm",
                  "anti-cheat"):
        occurrences = [line for line in source.splitlines()
                       if token in line and "repair=" in line]
        assert not occurrences


def test_fix_creates_a_missing_backup_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path / "fresh"))
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    done = doctor_mod.apply_fixes(report)
    assert isinstance(done, list)


def test_doctor_discards_an_incompatible_run_record(tmp_path):
    from travelready.apppaths import data_file

    path = data_file(pr.RUN_FILE)
    path.write_text(json.dumps({"version": 999, "planned": [], "done": {}}))
    report = doctor_mod.run_doctor(env=offline_environment(),
                                   library_path=FIXTURES / "games_2026-09-26.json",
                                   check_network=False)
    run_section = report.by_section()["Prepare-for-Travel"]
    assert any("incompatible" in f.detail for f in run_section)
    assert any(f.repairable for f in run_section)
    doctor_mod.apply_fixes(report)
    assert not path.exists()


# -- prepare runs ------------------------------------------------------------

def _counting_launcher(calls):
    def launch(identity, options, note, stop_event):
        calls.append(identity.key)
    return launch


def test_a_run_records_every_game(identities, tmp_path):
    calls = []
    run = pr.run_preparation(
        identities, pr.PrepareOptions(reverify_ready=True),
        env=offline_environment(), launcher=_counting_launcher(calls),
        run_path=tmp_path / "run.json")
    assert run.status == pr.STATUS_COMPLETE
    assert len(run.done) == len(identities)
    assert len(calls) == len(identities)


def test_an_interrupted_run_is_persisted_and_resumable(identities, tmp_path):
    path = tmp_path / "run.json"
    stop = threading.Event()
    calls = []

    def launch(identity, options, note, stop_event):
        calls.append(identity.key)
        if len(calls) == 4:
            stop.set()

    first = pr.run_preparation(identities, pr.PrepareOptions(reverify_ready=True),
                               env=offline_environment(), launcher=launch,
                               stop_event=stop, run_path=path)
    assert first.status == pr.STATUS_INTERRUPTED
    assert len(first.done) == 4
    assert first.resumable
    assert json.loads(path.read_text())["status"] == pr.STATUS_INTERRUPTED

    completed_keys = list(first.done)
    second = pr.run_preparation(
        identities, pr.PrepareOptions(reverify_ready=True, resume=True),
        env=offline_environment(), launcher=launch,
        stop_event=threading.Event(), run_path=path)
    assert second.status == pr.STATUS_COMPLETE
    assert len(second.done) == len(identities)
    assert list(second.done)[:4] == completed_keys, "earlier work must be kept"
    assert len(calls) == len(identities), "no game may be launched twice"


def test_resume_without_a_saved_run_starts_fresh(identities, tmp_path):
    run = pr.run_preparation(identities[:3],
                             pr.PrepareOptions(reverify_ready=True, resume=True),
                             env=offline_environment(),
                             launcher=_counting_launcher([]),
                             run_path=tmp_path / "missing.json")
    assert run.status == pr.STATUS_COMPLETE


def test_a_run_from_another_version_is_not_resumed(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"version": 999, "planned": ["a"], "done": {},
                                "status": "interrupted"}))
    assert pr.load_run(path) is None


def test_one_failing_game_does_not_kill_the_run(identities, tmp_path):
    calls = []

    def launch(identity, options, note, stop_event):
        calls.append(identity.key)
        if len(calls) == 2:
            raise RuntimeError("simulated launcher explosion")

    run = pr.run_preparation(identities[:5], pr.PrepareOptions(reverify_ready=True),
                             env=offline_environment(), launcher=launch,
                             run_path=tmp_path / "run.json")
    assert run.status == pr.STATUS_COMPLETE
    assert len(run.done) == 5
    reports = run.reports()
    assert any("raised an error" in a for r in reports for a in r.actions)


def test_games_that_cannot_be_closed_are_skipped_with_a_reason(tmp_path):
    blind = GameEntry(name="Blind EA Game", launcher="ea", launch_method="uri",
                      launch_target="link2ea://launch/1")
    identities = build_identities([blind])
    run = pr.run_preparation(identities, pr.PrepareOptions(),
                             env=offline_environment(),
                             launcher=_counting_launcher([]),
                             run_path=tmp_path / "run.json")
    assert run.skipped
    assert "cannot be closed safely" in list(run.skipped.values())[0]


def test_no_launch_mode_assesses_without_launching(identities, tmp_path):
    calls = []
    run = pr.run_preparation(identities[:4],
                             pr.PrepareOptions(launch=False, reverify_ready=True),
                             env=offline_environment(),
                             launcher=_counting_launcher(calls),
                             run_path=tmp_path / "run.json")
    assert calls == []
    assert len(run.done) == 4


def test_the_report_numbers_come_from_recorded_state(identities, tmp_path):
    run = pr.run_preparation(identities, pr.PrepareOptions(reverify_ready=True),
                             env=offline_environment(),
                             launcher=_counting_launcher([]),
                             run_path=tmp_path / "run.json")
    text = pr.render_run_report(run, identities)
    assert f"Games checked: {len(run.done)}" in text
    assert "Launcher readiness:" in text
    assert "ROG Ally Life:" in text


def test_an_interrupted_report_says_how_to_continue(identities, tmp_path):
    stop = threading.Event()
    calls = []

    def launch(identity, options, note, stop_event):
        calls.append(identity.key)
        stop.set()

    run = pr.run_preparation(identities, pr.PrepareOptions(reverify_ready=True),
                             env=offline_environment(), launcher=launch,
                             stop_event=stop, run_path=tmp_path / "run.json")
    assert "--resume" in pr.render_run_report(run, identities)


def test_settings_are_never_applied_by_a_prepare_run():
    """Changing configuration is a separate, explicitly approved transaction.

    Checked on the syntax tree: a prepare run must not import or call the
    writing side of the settings engine, whatever its prose says.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pr))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = {"transaction", "apply_plan", "restore_backup", "create_backup",
              "dry_run", "optimiser.transaction",
              "travelready.optimiser.transaction"}
    assert not (imported & banned), f"prepare_run imports {imported & banned}"

    called = {node.func.attr if isinstance(node.func, ast.Attribute)
              else getattr(node.func, "id", "")
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not (called & {"apply_plan", "create_backup", "restore_backup"})
