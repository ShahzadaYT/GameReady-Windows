"""Shared helpers: a deterministic virtual clock for the detection tests."""
from __future__ import annotations

from travelready import launch_tester as lt
from travelready.processes import FakeProcessTable, ProcInfo


def executables_exist(monkeypatch, *paths: str) -> None:
    """Treat these Windows executable paths as installed, on any host.

    ``build_command`` refuses a missing executable. Off Windows a
    ``C:\\...`` path can only be checked by suffix, so a fixture's game passes;
    on Windows it is looked up on the real disk, where that install does not
    exist. Naming the paths keeps every other path on the real check.
    """
    known = set(paths)
    real = lt.is_valid_executable
    monkeypatch.setattr(lt, "is_valid_executable",
                        lambda file_path: file_path in known or real(file_path))


class VirtualClock:
    """A clock/sleep pair that advances a FakeProcessTable in lockstep.

    Lets the detection state machine be driven through minutes of simulated
    launcher behaviour instantly and deterministically.
    """

    def __init__(self, table: FakeProcessTable, start: float = 1000.0) -> None:
        self.t = start
        self.table = table
        self.table.clock = 0.0

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.table.advance(seconds)


def launcher_started(pid: int = 900, name: str = "cmd.exe"):
    """A starter() double: pretends the launch command succeeded."""
    def _start(entry, command):
        return object(), pid, "", ""
    return _start


def failing_starter(status: str, message: str):
    def _start(entry, command):
        return None, None, status, message
    return _start
