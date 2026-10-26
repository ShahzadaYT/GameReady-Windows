"""The built executable must stand alone.

`dist\\TravelReady.exe` is copied to other machines and run from folders that
have never seen this repository. Nothing may depend on the checkout being
present, on the working directory, or on a developer's home folder.

These tests check what can be checked without Windows. Actually running the
Windows binary is a hardware test; see HARDWARE_VALIDATION.md.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "travelready"
SPEC = ROOT / "travelready.spec"


def python_files():
    return sorted(SRC.rglob("*.py"))


# -- the spec ---------------------------------------------------------------

def test_the_spec_exists_and_names_the_real_entry_point():
    text = SPEC.read_text(encoding="utf-8")
    assert '"run.py"' in text
    assert (ROOT / "run.py").is_file()


def test_every_bundled_data_path_exists():
    """A datas entry naming a missing folder fails only at build time."""
    text = SPEC.read_text(encoding="utf-8")
    for source, _target in re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', text):
        if source.startswith("src/"):
            assert (ROOT / source).exists(), f"spec references missing {source}"


def test_tkinter_submodules_are_declared_as_hidden_imports():
    """PyInstaller does not always find these by static analysis."""
    text = SPEC.read_text(encoding="utf-8")
    for module in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                   "tkinter.messagebox"):
        assert f'"{module}"' in text


# -- no developer paths -----------------------------------------------------

DEV_PATH = re.compile(r"[A-Za-z]:\\Users\\(?!%USERNAME%)[A-Za-z0-9._-]+", re.I)


def test_no_module_hardcodes_a_user_profile_path():
    r"""The EXE must not need C:\Users\SHAHZ\GameReady-Windows to exist."""
    offenders = []
    for path in python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if DEV_PATH.search(line) and "%USERNAME%" not in line:
                # Docstrings may use one as an example of what gets redacted.
                stripped = line.strip()
                if stripped.startswith(("#", '"', "'", "*", "``")) or "``" in line:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {stripped}")
    assert not offenders, "developer paths baked into the build:\n" + "\n".join(offenders)


def test_no_module_reads_the_checkout_directory():
    """Nothing may resolve data relative to the repository root."""
    offenders = []
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        for needle in ("GameReady-Windows", "os.getcwd()", 'Path(".")', "Path('.')"):
            if needle in text:
                # The project URL in the User-Agent is a contact address, not
                # a filesystem path.
                if needle == "GameReady-Windows" and "github.com" in text:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert not offenders, offenders


# -- where data goes --------------------------------------------------------

def test_the_data_directory_is_per_user_not_next_to_the_executable(tmp_path, monkeypatch):
    from travelready import apppaths

    monkeypatch.delenv("TRAVELREADY_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    directory = apppaths.data_dir()
    assert directory.name == "TravelReady"
    assert str(ROOT) not in str(directory), "data must not live in the checkout"


def test_the_data_directory_can_be_overridden_for_tests(tmp_path, monkeypatch):
    from travelready import apppaths

    monkeypatch.setenv("TRAVELREADY_DATA_DIR", str(tmp_path))
    assert apppaths.data_dir() == tmp_path


def test_bundled_resources_resolve_through_meipass(monkeypatch, tmp_path):
    """Frozen, read-only data comes from the bundle, not from the source tree."""
    from travelready import apppaths

    bundle = tmp_path / "_MEI12345"
    (bundle / "travelready").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert apppaths.resource_dir() == bundle / "travelready"


def test_resource_dir_falls_back_to_the_bundle_root(monkeypatch, tmp_path):
    from travelready import apppaths

    bundle = tmp_path / "_MEI9"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert apppaths.resource_dir() == bundle


# -- nothing is left out of the bundle --------------------------------------

def imported_names(path: Path) -> set:
    """Module names imported by one file, relative ones resolved to leaves."""
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.rsplit(".", 1)[-1])
            names.update(a.name for a in node.names)
    return names


def test_every_package_module_is_reachable_from_the_entry_point():
    """A module nothing imports is left out of the EXE.

    Regression: tasks.py was written, tested and never wired in, so PyInstaller
    did not bundle it — the tests passed while the shipped application had no
    such module.
    """
    reachable, pending = set(), ["main"]
    by_name = {p.stem: p for p in python_files()}
    while pending:
        name = pending.pop()
        if name in reachable or name not in by_name:
            continue
        reachable.add(name)
        pending.extend(imported_names(by_name[name]))

    all_modules = {p.stem for p in python_files()
                   if p.stem not in ("__init__", "__main__")}
    orphans = sorted(all_modules - reachable)
    assert not orphans, (
        f"modules nothing imports, so the build omits them: {orphans}")


def test_the_gui_uses_the_shared_task_runner_and_state_model():
    """Both must be imported, or the EXE ships without them."""
    names = imported_names(SRC / "gui_app.py")
    assert "tasks" in names
    assert "appstate" in names
