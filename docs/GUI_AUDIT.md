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
