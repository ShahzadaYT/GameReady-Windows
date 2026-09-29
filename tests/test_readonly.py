"""Proof that the read-only half of the settings system writes nothing.

The brief requires that discovery, profile lookup, settings inspection, config
parsing, current-value reading, recommendation matching, safety classification,
diff generation and dry-run perform ZERO writes, and that the separation be
testable. This module enforces it two ways at once:

* a byte-level snapshot of the whole sandbox before and after, and
* a guard that makes any attempt to open a file for writing raise.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import os
from pathlib import Path

import pytest

from travelready.library import GameEntry
from travelready.optimiser import safety, transaction
from travelready.optimiser.diff import build_plan, render_plan
from travelready.optimiser.inspector import (
    Inspection, candidate_config_paths, inspect_game, read_config_file,
)
from travelready.optimiser.model import GameProfile, Recommendation
from travelready.optimiser.profiles import ProfileStore, validate_profile_dict

INI = (
    "; comment\r\n[ScalabilityGroups]\r\nsg.ShadowQuality=0\r\nsg.TextureQuality=1\r\n"
    "\r\n[/Script/Engine.GameUserSettings]\r\nResolutionSizeX=1280\r\n"
    "ResolutionSizeY=720\r\nFrameRateLimit=45.000000\r\n"
)

_WRITE_MODES = set("wax+")


def snapshot(root: Path) -> dict:
    """Every file under ``root`` as ``path -> (size, mtime_ns, sha256)``."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            stat = path.stat()
            out[str(path)] = (stat.st_size, stat.st_mtime_ns,
                              hashlib.sha256(data).hexdigest())
    return out


class WriteGuard:
    """Context manager that makes any write-mode ``open()`` fail loudly."""

    def __init__(self) -> None:
        self.violations = []

    def __enter__(self):
        self._real_open = builtins.open
        self._real_replace = os.replace
        guard = self

        def guarded_open(file, mode="r", *args, **kwargs):
            if _WRITE_MODES & set(str(mode)):
                guard.violations.append(f"open({file!r}, {mode!r})")
                raise AssertionError(f"read-only phase attempted a write: {file!r} ({mode})")
            return guard._real_open(file, mode, *args, **kwargs)

        def guarded_replace(src, dst, **kwargs):
            guard.violations.append(f"os.replace({src!r}, {dst!r})")
            raise AssertionError(f"read-only phase attempted os.replace: {src!r}")

        builtins.open = guarded_open
        os.replace = guarded_replace
        return self

    def __exit__(self, *exc):
        builtins.open = self._real_open
        os.replace = self._real_replace
        return False


@pytest.fixture
def sandbox(tmp_path):
    d = tmp_path / "AppData" / "Local" / "MyGame" / "Saved" / "Config" / "WindowsNoEditor"
    d.mkdir(parents=True)
    (d / "GameUserSettings.ini").write_bytes(INI.encode("utf-8"))
    (d / "Engine.ini").write_bytes(b"[Core]\nKey=1\n")
    (tmp_path / "profiles").mkdir()
    return tmp_path


@pytest.fixture
def profile():
    return GameProfile(
        game_name="My Game", device="rog_ally_x", source="ROG Ally Life",
        source_url="https://rogallylife.com/my-game",
        recommendations=[
            Recommendation("shadow_quality", "Medium"),
            Recommendation("frame_rate_limit", "60"),
            Recommendation("tdp_watts", "20", category="device"),
        ],
    )


def _read_only_pipeline(sandbox, profile):
    """Every operation the brief lists as read-only, end to end."""
    config = (sandbox / "AppData" / "Local" / "MyGame" / "Saved" / "Config"
              / "WindowsNoEditor" / "GameUserSettings.ini")
    entry = GameEntry(name="My Game", launcher="steam",
                      install_dir=str(config.parent), expected_process="mygame.exe")

    candidate_config_paths(entry)
    ProfileStore.load([sandbox / "profiles"])
    validate_profile_dict(profile.to_dict() | {"recommendations":
                                               [r.to_dict() for r in profile.recommendations]})
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(config))])
    inspect_game(entry, paths=[str(config)])
    safety.classify_path(str(config))
    safety.classify_setting("shadow_quality", "Medium")
    safety.classify_target("shadow_quality", "Medium", str(config),
                           "rog_ally_x", "rog_ally_x", must_exist=True, check_content=True)
    plan = build_plan(entry, profile, inspection)
    render_plan(plan)
    transaction.dry_run(plan, str(config))
    return plan


def test_read_only_pipeline_changes_nothing_on_disk(sandbox, profile):
    before = snapshot(sandbox)
    plan = _read_only_pipeline(sandbox, profile)
    after = snapshot(sandbox)
    assert before == after, "the read-only pipeline modified the filesystem"
    assert plan.safe, "sanity: the pipeline should still have produced a plan"


def test_read_only_pipeline_never_opens_a_file_for_writing(sandbox, profile):
    guard = WriteGuard()
    with guard:
        plan = _read_only_pipeline(sandbox, profile)
    assert guard.violations == []
    assert plan.safe


def test_dry_run_is_write_free_even_when_it_produces_a_full_diff(sandbox, profile):
    config = (sandbox / "AppData" / "Local" / "MyGame" / "Saved" / "Config"
              / "WindowsNoEditor" / "GameUserSettings.ini")
    entry = GameEntry(name="My Game", launcher="steam", expected_process="mygame.exe")
    inspection = Inspection(game_name=entry.name, files=[read_config_file(str(config))])
    plan = build_plan(entry, profile, inspection)
    before = snapshot(sandbox)
    guard = WriteGuard()
    with guard:
        run = transaction.dry_run(plan, str(config))
    assert run.ok and run.diff
    assert guard.violations == []
    assert snapshot(sandbox) == before


def test_only_the_transaction_module_writes():
    """No read-only module may call a filesystem write primitive.

    Checked structurally on the syntax tree. Names that are ambiguous with
    ordinary string/list methods (``replace``, ``remove``, ``rename``) are only
    flagged when called on ``os`` or ``shutil``; the rest are unambiguous.
    """
    import ast
    import inspect as pyinspect

    from travelready.optimiser import diff
    from travelready.optimiser import inspector as inspector_mod
    from travelready.optimiser import model
    from travelready.optimiser import safety as safety_mod

    unambiguous = {"write_text", "write_bytes", "unlink", "rmtree", "copy2",
                   "copyfile", "makedirs", "mkdir", "chmod", "truncate",
                   "copytree", "move"}
    module_qualified = {"replace", "remove", "rename", "copy", "open"}

    for module in (model, safety_mod, inspector_mod, diff):
        tree = ast.parse(pyinspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in unambiguous:
                pytest.fail(f"{module.__name__} calls .{node.attr}")
            if node.attr in module_qualified and isinstance(node.value, ast.Name) \
                    and node.value.id in ("os", "shutil"):
                pytest.fail(f"{module.__name__} calls {node.value.id}.{node.attr}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "open" and len(node.args) > 1:
                mode = node.args[1]
                if isinstance(mode, ast.Constant) and _WRITE_MODES & set(str(mode.value)):
                    pytest.fail(f"{module.__name__} opens a file for writing")


def test_configio_only_writes_via_callers():
    """configio builds bytes; it never puts them on disk itself."""
    import ast
    import inspect as pyinspect

    from travelready.optimiser import configio

    tree = ast.parse(pyinspect.getsource(configio))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open" and len(node.args) > 1:
            mode = node.args[1]
            if isinstance(mode, ast.Constant) and _WRITE_MODES & set(str(mode.value)):
                pytest.fail("configio opens a file for writing")
