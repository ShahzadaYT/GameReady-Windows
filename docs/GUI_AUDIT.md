# GUI audit — the bugs found in the standalone EXE

Date: 2026-09-30 · Baseline `5219358`.

## 1. The game list does not refresh after a scan

`_on_scanned` merges the discovered entries into `self.entries`, saves, and
calls `_refresh()`. But `_refresh()` does not render from `self.entries` — it
renders from `self.identities`, which is built by `_rebuild_identities()`.

`_rebuild_identities()` is called from `_load_library()` at startup and after a
prepare run. **It is never called after a scan.** So the tree keeps rendering
the identity list built at startup, and the only way to see newly scanned games
is to restart — exactly the reported symptom.

The same omission leaves `self._reports` (the readiness cache) keyed on the old
identities, so the dashboard, tab counts and settings coverage are stale too.

**Root cause:** the view derives from a computed model, and one of the three
mutation paths forgot to recompute it. That is a structural problem, not a
missing line: any future mutation path will forget too. Fixed by making the
model own its invalidation (see `appstate.py`) rather than asking each handler
to remember.

## 2. "Updating…" never finishes

The sync *does* run on a worker thread, so this is not a frozen UI thread. Four
things combine to make a slow operation indistinguishable from a hung one:

* **Cancel does nothing.** `sync()` accepts no `stop_event`, and the GUI's
  Cancel button only sets `self.cancel_event`, which sync never reads.
* **No progress beyond a few stage lines.** `note()` fires on route selection
  and on each fetch, but there is no counter and no total, so the status bar
  says "Updating…" from the first second to the last.
* **No overall deadline.** Per-request timeouts are bounded (20 s, 3 retries),
  but the whole operation is not.
* **The work is genuinely long.** `DEFAULT_DELAY` is 1.0 s between requests.
  If the REST API is unavailable, discovery falls through to the sitemap (3
  candidates plus up to 20 nested files), then fetches every post
  individually — on a site with ~126 posts that is over two minutes of
  throttled requests before anything visible happens, and the index-page
  fallback adds 2 paths × 30 pages on top.

**Root cause:** a long operation with no cancellation, no progress and no
bound. The CLI reported the host reachable, so the network is not the problem;
the reporting is.

## 3. A successful sync empties the game list

`_on_source_synced` contains lines spliced in from `__init__` by a bad
search-and-replace during the previous pass — including
`self.identities = []`. So a sync that *succeeded* wiped the library view.

This is my own regression from the previous pass, introduced by a
`str.replace` whose anchor text appeared in two methods. It had no test
because the GUI tests asserted startup state, not post-sync state.

## What this pass changes

1. `appstate.py` — one owner of the library, identities, readiness and
   selection, with explicit events. Views subscribe; no view keeps its own copy.
2. `tasks.py` — one background-task runner with cancellation, progress, a
   bounded deadline and three explicit states.
3. `sync()` gains a `stop_event`, per-item progress with counts, a deadline,
   and conditional requests so a second sync is cheap.
4. The GUI becomes a package of focused views, all driven by `appstate` events.
5. Regression tests for every bug above.

---

# Outcome

All three faults are fixed, with regression tests that fail against the old
code. Two further problems surfaced while fixing them, both described below.

## 1. Scanning did not refresh the list — fixed

The window no longer holds its own copy of anything. `entries`, `identities`,
`current_tab`, `environment` and `resolver` are properties over
`AppState`, and repaints come from `LIBRARY_CHANGED` / `READINESS_CHANGED`
subscriptions.

The point is not that `_on_scanned` now rebuilds the derived data — it is that
no handler *can* forget to. Entries change only through `set_entries`, which
drops the derived caches and publishes. A future handler written by someone
who has never read this document inherits the behaviour.

## 2. The sync hung, and Cancel did nothing — fixed

Not a timeout problem. `sync()` accepted no stop event, had no overall
deadline, and never sent the conditional-request headers the client already
supported, so every run re-fetched and re-parsed every post at one request per
second: 87 posts is 87 seconds before any other cost.

Now: a `stop_event` and a `Budget` (deadline + cancel flag), both checked
inside requests rather than only between posts; stored ETag/Last-Modified so
an unchanged post costs a 304; per-item progress with counts; and four
distinct outcomes — complete, cancelled, timed out, blocked — that the GUI
words differently.

Two further gaps found by the new tests: the `robots.txt` fetch, the first
request of every run, ignored the budget entirely; and politeness delays and
retry backoff slept in one uninterruptible block, so Cancel had to wait out a
30-second `Retry-After`.

## 3. A successful sync emptied the game list — fixed

The spliced `__init__` lines are gone. `_on_source_synced` now invalidates
only what depends on the source. Because the prose version of this rule is
what failed, an AST-based test asserts the handler assigns neither
`identities` nor `entries`.

## 4. Silent library data loss — found while testing, fixed

Asserting that an empty scan leaves the library alone failed: 162 entries
became 155. Duplicate stored entries — one from the launcher catalogue with
the display name, one from disk with the executable path — were keyed alike,
and `merge_library_updates` *assigned* rather than merged, so the later
silently replaced the earlier. Seven games in the shipped library were
affected. They are now folded: the displayed name survives, the executable is
gained, test results and process names are unioned, and it is idempotent.

## 5. A tested module that was not in the build — found while packaging

`tasks.py` was written, tested, and never imported, so PyInstaller did not
bundle it. The tests passed while the shipped application had no such module.
The GUI now runs all background work through it, and
`test_every_package_module_is_reachable_from_the_entry_point` fails on any
module nothing imports. That test was itself verified by orphaning a module
deliberately.

## On splitting the GUI into a package

The plan above said the GUI would become a package of focused views. It has
not been, and that is a deliberate change of mind rather than an omission.

The fault was never that the views lived in one file; it was that they each
kept their own copy of the state. That is fixed, and it is fixed in a way
that a file split would not have achieved on its own. Splitting a working
1,676-line module afterwards is a large diff across every handler, with real
regression risk, in exchange for file boundaries — while the boundary that
mattered, between state and presentation, now exists and is tested without a
display. If the file keeps growing the split is worth doing; it is not worth
doing in the same pass as the bug fixes, where a mistake would be
indistinguishable from one of them.

## A note on why these reached a desktop

The GUI tests never ran in this environment. `tkinter` is absent from the
interpreter the suite uses, so the entire module was skipped — silently, as a
single "1 skipped" line. Every GUI regression here was therefore invisible to
a green test run.

They now run under `python3.12` with `xvfb`:

    PYTHONPATH=src xvfb-run -a python3.12 -m pytest tests/

That is how the 30 GUI tests, and the 36 added in this pass, actually execute.
A test that cannot run is not a test.
