"""tasks.py — running long work off the UI thread, with a result you can trust.

One runner for every background operation: scanning, launch testing, preparing
for travel and refreshing the recommendation source. Each has the same shape,
so each behaves the same way when it is slow, cancelled or broken.

Three states, always distinguishable
------------------------------------
Work either ``SUCCEEDED``, was ``CANCELLED``, or ``FAILED``. These are never
collapsed. A failure carries the exception and its traceback; a cancellation
carries how far it got. The UI can then say "could not reach the source" rather
than the thing this project has banned since the first brief — turning "cannot
determine" into "no".

**An exception is never swallowed.** It is caught at the thread boundary
(because an exception escaping a thread would otherwise vanish into stderr and
leave the window spinning forever), recorded on the task, and delivered to the
caller's completion callback. Nothing here logs-and-continues.

No tkinter
----------
The runner communicates through a queue that the UI drains with ``after``.
That keeps every state transition testable without a display, which matters:
this project's GUI tests could not run at all in the development environment,
and that is precisely how a regression reached a user's desktop.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional

# -- states -----------------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
SUCCEEDED = "succeeded"
CANCELLED = "cancelled"
FAILED = "failed"

FINISHED = (SUCCEEDED, CANCELLED, FAILED)


@dataclass
class Progress:
    """One progress report: a message, and a position when it is known."""

    message: str
    done: int = 0
    total: int = 0

    @property
    def fraction(self) -> Optional[float]:
        """0..1, or ``None`` when the total is not yet known."""
        if self.total <= 0:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    def describe(self) -> str:
        if self.total > 0:
            return f"[{self.done}/{self.total}] {self.message}"
        return self.message


@dataclass
class Task:
    """A unit of background work and everything known about how it went."""

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = IDLE
    result: Any = None
    error: Optional[BaseException] = None
    traceback_text: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    last_progress: Optional[Progress] = None
    messages: List[str] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)

    # -- state ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    @property
    def cancelled(self) -> bool:
        return self.state == CANCELLED

    @property
    def failed(self) -> bool:
        return self.state == FAILED

    @property
    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    def cancel(self) -> None:
        """Ask the work to stop. It stops at its next checkpoint."""
        self.stop_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self.stop_event.is_set()

    def describe(self) -> str:
        """A line fit for a status bar."""
        if self.state == RUNNING:
            progress = self.last_progress.describe() if self.last_progress else "Working…"
            return f"{self.name}: {progress}"
        if self.state == SUCCEEDED:
            return f"{self.name} finished in {self.duration:.1f}s."
        if self.state == CANCELLED:
            return f"{self.name} cancelled after {self.duration:.1f}s."
        if self.state == FAILED:
            return f"{self.name} failed: {self.error_summary}"
        return self.name

    @property
    def error_summary(self) -> str:
        """The failure in one line, for a status bar or a dialog heading."""
        if self.error is None:
            return ""
        text = str(self.error).strip()
        return f"{type(self.error).__name__}: {text}" if text else type(self.error).__name__


class Cancelled(Exception):
    """Raised by :meth:`TaskContext.check` when the user asked to stop."""


class TaskContext:
    """What the worker function is given: progress out, cancellation in."""

    def __init__(self, task: Task, queue: "Queue[tuple]") -> None:
        self._task = task
        self._queue = queue
        self.total = 0

    @property
    def task(self) -> Task:
        return self._task

    @property
    def stop_event(self) -> threading.Event:
        return self._task.stop_event

    @property
    def cancelled(self) -> bool:
        return self._task.stop_event.is_set()

    def check(self) -> None:
        """Raise :class:`Cancelled` if the user asked to stop."""
        if self.cancelled:
            raise Cancelled()

    def progress(self, message: str, done: int = 0, total: int = 0) -> None:
        """Report progress. Safe to call from the worker thread."""
        self._queue.put(("progress", self._task.id,
                         Progress(message, done, total or self.total)))

    def log(self, message: str) -> None:
        """Append a line to the activity log."""
        self._queue.put(("log", self._task.id, message))


class TaskRunner:
    """Starts tasks on worker threads and delivers their events on the UI thread.

    The UI calls :meth:`pump` from a timer. Nothing here touches a widget, and
    every callback runs on whichever thread pumps — the UI thread, in the app.
    """

    def __init__(self, *, queue: Optional["Queue[tuple]"] = None) -> None:
        self.queue: "Queue[tuple]" = queue if queue is not None else Queue()
        self.current: Optional[Task] = None
        self.history: List[Task] = []
        self._tasks: Dict[str, Task] = {}
        self._callbacks: Dict[str, Dict[str, Optional[Callable]]] = {}
        self._threads: Dict[str, threading.Thread] = {}

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    # -- starting ----------------------------------------------------------

    def start(self, name: str, work: Callable[[TaskContext], Any], *,
              on_success: Optional[Callable[[Task], None]] = None,
              on_cancelled: Optional[Callable[[Task], None]] = None,
              on_failure: Optional[Callable[[Task], None]] = None,
              on_progress: Optional[Callable[[Task, Progress], None]] = None,
              on_log: Optional[Callable[[Task, str], None]] = None,
              on_finished: Optional[Callable[[Task], None]] = None,
              thread: bool = True) -> Optional[Task]:
        """Run ``work`` in the background. Returns the :class:`Task`, or ``None``
        if one is already running — a refusal the caller must handle, rather
        than a second thread quietly writing the same files.
        """
        if self.busy:
            return None
        task = Task(name=name)
        task.state = RUNNING
        task.started_at = time.monotonic()
        self.current = task
        self._tasks[task.id] = task
        self._callbacks[task.id] = {
            "success": on_success, "cancelled": on_cancelled,
            "failure": on_failure, "progress": on_progress,
            "log": on_log, "finished": on_finished,
        }
        context = TaskContext(task, self.queue)

        def run() -> None:
            try:
                result = work(context)
            except Cancelled:
                self.queue.put(("cancelled", task.id, None))
            except BaseException as exc:                 # noqa: BLE001 - reported, never hidden
                self.queue.put(("failed", task.id,
                                (exc, traceback.format_exc())))
            else:
                # A worker that returns normally after being asked to stop was
                # still cancelled: reporting success would claim a complete run.
                if task.stop_event.is_set():
                    self.queue.put(("cancelled", task.id, result))
                else:
                    self.queue.put(("succeeded", task.id, result))

        if not thread:                       # synchronous, for tests
            run()
            return task
        worker = threading.Thread(target=run, name=f"travelready-{task.id}",
                                  daemon=True)
        self._threads[task.id] = worker
        worker.start()
        return task

    def thread_for(self, task: Task) -> Optional[threading.Thread]:
        """The worker thread running ``task``, while it is running.

        Exposed for callers that need to join it — tests, and shutdown.
        """
        return self._threads.get(task.id)

    def cancel(self) -> bool:
        """Ask the running task to stop. Returns whether there was one."""
        if self.current is not None and self.current.running:
            self.current.cancel()
            return True
        return False

    # -- draining ----------------------------------------------------------

    def pump(self, limit: int = 64) -> int:
        """Deliver queued events to callbacks. Call this from the UI timer."""
        handled = 0
        for _ in range(limit):
            try:
                kind, task_id, payload = self.queue.get_nowait()
            except Empty:
                break
            self._dispatch(kind, task_id, payload)
            handled += 1
        return handled

    def _dispatch(self, kind: str, task_id: str, payload) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        callbacks = self._callbacks.get(task_id, {})

        if kind == "progress":
            task.last_progress = payload
            self._call(callbacks.get("progress"), task, payload)
            return
        if kind == "log":
            task.messages.append(str(payload))
            self._call(callbacks.get("log"), task, payload)
            return

        if kind == "succeeded":
            task.state, task.result = SUCCEEDED, payload
        elif kind == "cancelled":
            task.state, task.result = CANCELLED, payload
        elif kind == "failed":
            task.state = FAILED
            task.error, task.traceback_text = payload
        else:
            return

        task.finished_at = time.monotonic()
        if self.current is task:
            self.current = None
        self.history.append(task)
        self._threads.pop(task.id, None)

        # The terminal callback runs first so the UI updates, then the
        # always-run one re-enables controls. A failure in either must not
        # leave the buttons disabled, so 'finished' is in a finally.
        try:
            if task.state == SUCCEEDED:
                self._call(callbacks.get("success"), task)
            elif task.state == CANCELLED:
                self._call(callbacks.get("cancelled"), task)
            else:
                self._call(callbacks.get("failure"), task)
        finally:
            self._call(callbacks.get("finished"), task)
            self._callbacks.pop(task.id, None)

    @staticmethod
    def _call(callback: Optional[Callable], *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:                    # a broken view must not wedge the runner
            traceback.print_exc()

    # -- tests -------------------------------------------------------------

    def wait(self, timeout: float = 10.0, interval: float = 0.005) -> Optional[Task]:
        """Block until the current task finishes, pumping as it goes.

        For tests and the CLI only; the GUI never blocks its own thread.
        """
        task = self.current
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if task is None or task.finished:
                return task
            time.sleep(interval)
        self.pump()
        return task
