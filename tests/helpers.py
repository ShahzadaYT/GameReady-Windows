"""Shared helpers: a deterministic virtual clock for the detection tests."""
from __future__ import annotations

from travelready.processes import FakeProcessTable, ProcInfo


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
