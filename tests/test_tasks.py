"""Background task runner: three outcomes, and none of them silent."""
from __future__ import annotations

import threading
import time

import pytest

from travelready.tasks import (
    CANCELLED, FAILED, RUNNING, SUCCEEDED, Cancelled, Progress, TaskRunner,
)


@pytest.fixture
def runner() -> TaskRunner:
    return TaskRunner()


def run_now(runner: TaskRunner, work, **callbacks):
    """Run synchronously and deliver the events — no threads, no timing."""
    task = runner.start("Test", work, thread=False, **callbacks)
    runner.pump()
    return task


# -- the three states -------------------------------------------------------

def test_success_carries_the_result(runner):
    seen = []
    task = run_now(runner, lambda ctx: 42, on_success=lambda t: seen.append(t.result))
    assert task.state == SUCCEEDED and task.result == 42
    assert seen == [42]


def test_cancellation_is_not_success(runner):
    """Stopping at a checkpoint reports CANCELLED, never SUCCEEDED or FAILED."""
    reached_end = []

    def work(ctx):
        ctx.stop_event.set()          # stands in for the user pressing Cancel
        ctx.check()                   # the worker's next checkpoint
        reached_end.append(True)
        return "all done"

    task = run_now(runner, work)
    assert task.state == CANCELLED
    assert not reached_end, "work must stop at the checkpoint"
    assert task.error is None, "cancelling is not an error"


def test_a_worker_that_returns_after_being_cancelled_is_still_cancelled(runner):
    """Returning normally must not launder a cancelled run into a complete one."""
    def work(ctx):
        ctx.stop_event.set()          # user hit Cancel mid-flight
        return "partial"

    task = run_now(runner, work)
    assert task.state == CANCELLED
    assert task.result == "partial", "partial work is still reported"


def test_failure_carries_the_exception_and_traceback(runner):
    def work(ctx):
        raise ValueError("source unreachable")

    seen = []
    task = run_now(runner, work, on_failure=lambda t: seen.append(t))
    assert task.state == FAILED
    assert isinstance(task.error, ValueError)
    assert "source unreachable" in task.error_summary
    assert "ValueError" in task.traceback_text
    assert seen == [task]


def test_an_exception_is_never_swallowed(runner):
    """A worker raising must reach a callback, not vanish into the thread."""
    calls = {"success": 0, "failure": 0}
    run_now(runner, lambda ctx: 1 / 0,
            on_success=lambda t: calls.__setitem__("success", 1),
            on_failure=lambda t: calls.__setitem__("failure", 1))
    assert calls == {"success": 0, "failure": 1}


def test_failure_is_distinguishable_from_an_empty_result(runner):
    """'Could not fetch' and 'nothing found' must never look the same."""
    empty = run_now(runner, lambda ctx: [])
    runner.current = None
    broken = run_now(runner, lambda ctx: (_ for _ in ()).throw(OSError("no route")))
    assert empty.succeeded and empty.result == []
    assert broken.failed and not broken.succeeded
    assert empty.state != broken.state


# -- always finishing -------------------------------------------------------

def test_on_finished_runs_for_every_outcome(runner):
    for work in (lambda ctx: 1,
                 lambda ctx: (_ for _ in ()).throw(RuntimeError("x"))):
        runner.current = None
        seen = []
        run_now(runner, work, on_finished=lambda t: seen.append(t.state))
        assert len(seen) == 1

    runner.current = None
    seen = []

    def cancelled(ctx):
        ctx.stop_event.set()
        return None

    run_now(runner, cancelled, on_finished=lambda t: seen.append(t.state))
    assert seen == [CANCELLED]


def test_on_finished_runs_even_when_the_success_callback_raises(runner):
    """A broken view must not leave the toolbar disabled forever."""
    seen = []
    run_now(runner, lambda ctx: 1,
            on_success=lambda t: (_ for _ in ()).throw(RuntimeError("bad view")),
            on_finished=lambda t: seen.append("finished"))
    assert seen == ["finished"]


def test_the_runner_is_free_again_after_a_failure(runner):
    run_now(runner, lambda ctx: (_ for _ in ()).throw(RuntimeError("x")))
    assert not runner.busy
    assert run_now(runner, lambda ctx: "second").result == "second"


# -- one at a time ----------------------------------------------------------

def test_a_second_task_is_refused_while_one_runs(runner):
    started = threading.Event()
    release = threading.Event()

    def work(ctx):
        started.set()
        release.wait(5)
        return "done"

    first = runner.start("First", work)
    assert started.wait(5)
    assert runner.start("Second", lambda ctx: None) is None, "must refuse, not queue"
    release.set()
    runner.wait()
    assert first.state == SUCCEEDED


# -- progress ---------------------------------------------------------------

def test_progress_reports_reach_the_callback_with_counts(runner):
    def work(ctx):
        ctx.progress("Fetching A", 1, 3)
        ctx.progress("Fetching B", 2, 3)
        return None

    seen = []
    run_now(runner, work, on_progress=lambda t, p: seen.append(p.describe()))
    assert seen == ["[1/3] Fetching A", "[2/3] Fetching B"]


def test_progress_without_a_total_has_no_fraction():
    assert Progress("Working").fraction is None
    assert Progress("Working", 1, 4).fraction == 0.25


def test_log_lines_are_kept_on_the_task(runner):
    def work(ctx):
        ctx.log("line one")
        ctx.log("line two")
        return None

    task = run_now(runner, work)
    assert task.messages == ["line one", "line two"]


# -- real threading ---------------------------------------------------------

def test_cancellation_stops_a_running_thread_promptly(runner):
    started = threading.Event()

    def work(ctx):
        started.set()
        for _ in range(2000):
            ctx.check()
            time.sleep(0.001)
        return "finished all"

    task = runner.start("Long", work)
    assert started.wait(5)
    runner.cancel()
    runner.wait(timeout=5)
    assert task.state == CANCELLED
    assert task.duration < 5


def test_describe_distinguishes_the_outcomes(runner):
    ok = run_now(runner, lambda ctx: None)
    runner.current = None
    bad = run_now(runner, lambda ctx: (_ for _ in ()).throw(OSError("denied")))
    assert "finished" in ok.describe()
    assert "failed" in bad.describe() and "denied" in bad.describe()


# -- adopting the caller's cancel flag --------------------------------------

def test_a_task_can_adopt_an_existing_stop_event(runner):
    """A worker reading a long-lived flag must see the one Cancel sets.

    Assigning the task's own event to that attribute after start() returns is
    a race: the thread may already have read the old one, and cancelling then
    sets a flag nobody is watching.
    """
    shared = threading.Event()
    task = runner.start("Test", lambda ctx: None, stop_event=shared, thread=False)
    runner.pump()
    assert task.stop_event is shared


def test_adopting_a_stop_event_clears_it_first(runner):
    """A flag left set by the previous run must not cancel this one."""
    shared = threading.Event()
    shared.set()
    seen = {}

    def work(ctx):
        seen["cancelled"] = ctx.cancelled
        return "ran"

    task = runner.start("Test", work, stop_event=shared, thread=False)
    runner.pump()
    assert seen["cancelled"] is False
    assert task.succeeded


def test_cancelling_through_the_adopted_event_is_seen_by_the_worker(runner):
    shared = threading.Event()
    started = threading.Event()

    def work(ctx):
        started.set()
        for _ in range(2000):
            ctx.check()
            time.sleep(0.001)
        return "finished all"

    task = runner.start("Long", work, stop_event=shared)
    assert started.wait(5)
    shared.set()                       # the GUI's Stop button
    runner.wait(timeout=5)
    assert task.state == CANCELLED
