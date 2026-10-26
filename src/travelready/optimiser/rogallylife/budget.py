"""budget.py — the stop conditions for a long network operation.

A sync that cannot be cancelled and has no deadline is the reason the GUI
appeared to hang: the work was not stuck, it was simply unbounded, and nothing
could tell it to stop. :class:`Budget` makes both limits explicit and checkable,
and both the HTTP client and the sync loop consult the same object, so a cancel
takes effect inside a request rather than only between posts.

Nothing here performs I/O or imports anything from the package, so it can be
used by the client and by the sync loop without a circular import.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

#: How long a whole sync may take before it gives up and reports what it has.
#: A sync that cannot finish inside this is not "slow" — it is a sync whose
#: result the user will never see. A partial cache plus an honest report beats
#: a spinner that never stops.
DEFAULT_BUDGET_SECONDS = 180.0

#: The longest single uninterruptible sleep. Throttling and retry backoff are
#: split into slices no longer than this so Cancel is felt promptly instead of
#: after the current wait happens to end.
SLEEP_SLICE = 0.25


class StopEvent(Protocol):
    """Anything with :meth:`is_set` — ``threading.Event`` in the GUI."""

    def is_set(self) -> bool: ...          # pragma: no cover - structural


class Cancelled(Exception):
    """The caller asked us to stop, or the deadline passed.

    Raised at the nearest checkpoint and caught by the sync loop, which saves
    what it has and reports a partial run. It is not an error: it never becomes
    a "no recommendations" result.
    """


@dataclass
class Budget:
    """A deadline and a cancel flag, checked at every network boundary.

    ``seconds=None`` means no deadline, for the CLI, where a long-running
    update in a terminal the user can Ctrl-C is perfectly reasonable.
    """

    seconds: Optional[float] = DEFAULT_BUDGET_SECONDS
    stop_event: Optional[StopEvent] = None
    clock: Callable[[], float] = time.monotonic
    started: float = 0.0

    def __post_init__(self) -> None:
        self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def remaining(self) -> Optional[float]:
        return None if self.seconds is None else self.seconds - self.elapsed

    @property
    def cancelled(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    @property
    def expired(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def check(self) -> None:
        """Raise :class:`Cancelled` if the caller stopped us or time ran out."""
        if self.cancelled or self.expired:
            raise Cancelled()

    def sleep(self, seconds: float, sleeper: Callable[[float], None] = time.sleep) -> None:
        """Wait, in slices, giving up early if cancelled or out of time.

        Politeness delays and retry backoff must not outlive the operation
        they belong to: waiting 30 seconds to retry inside a run that has 5
        seconds left, or that the user cancelled a moment ago, spends time
        nobody is still waiting for.
        """
        self.check()
        remaining = self.remaining
        if remaining is not None:
            seconds = min(seconds, max(0.0, remaining))
        left = seconds
        while left > 0:
            sleeper(min(SLEEP_SLICE, left))
            left -= SLEEP_SLICE
            self.check()


#: A budget that never stops anything, for callers that want no limits.
UNLIMITED = Budget(seconds=None)


def resolve(budget: Optional[Budget]) -> Budget:
    """``budget`` or an unlimited one — never ``None`` at a call site."""
    return budget if budget is not None else Budget(seconds=None)
