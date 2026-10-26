# TravelReady — GUI usability, live refresh, sync and reliability

**Branch** `claude/travelready-development-9nebka` · **Head** `141b1f1`
**Tests** 834 passing, 10 hardware-marked and deselected, 0 failing
**Changed** 23 files, +4,270 / −148

No pull request has been opened, and no hardware testing has been started.

---

## 1. What you reported, and what each one actually was

### Scanning did not refresh the list

The tree rendered from `identities`, a value *derived* from `entries`. Three
code paths changed `entries`; each was expected to rebuild the derived data by
hand. Two did. `_on_scanned` did not, so scanned games existed in the library
and were invisible until restart.

The fix is not the missing line. `AppState` now owns the library and
everything derived from it, and the only way to change the library is
`set_entries`, which drops the derived caches and publishes an event. A
handler cannot forget to invalidate, because handlers no longer invalidate.

### The sync hung on "Updating…", and Cancel did nothing

You asked me not to just raise the timeout. The timeout was not the problem.

`sync()` took no stop event — the Cancel button set a flag nothing read. It
had no overall deadline. And it never sent the conditional-request headers the
HTTP client already supported, so every run re-fetched and re-parsed every
post at one request per second: 87 posts is 87 seconds before parsing,
discovery walking up to 40 REST pages, or any retry.

It now takes a `stop_event` and a `Budget` (deadline plus cancel flag), both
checked *inside* requests rather than only between posts; stores and sends
ETag/Last-Modified so an unchanged post costs a 304; reports per-item progress
with counts; and distinguishes four outcomes — complete, cancelled, timed out,
blocked — which the GUI words differently.

Writing the tests found two more: the `robots.txt` fetch, the first request of
every run, ignored the budget entirely, and politeness delays and retry
backoff slept in one uninterruptible block, so Cancel had to wait out a
30-second `Retry-After`.

### A successful sync emptied the game list

`_on_source_synced` contained lines spliced in from `__init__` by a bad
search-and-replace in my previous pass, including `self.identities = []`.
**This was my regression, and it shipped because the GUI tests asserted
startup state only — and, worse, never ran at all here.**

It now invalidates only what depends on the source. Because the prose version
of that rule is exactly what failed, an AST-based test asserts the handler
assigns neither `identities` nor `entries`.

---

## 2. Two problems the tests found that you had not reported

### Silent library data loss on every scan

Asserting "an empty scan leaves the library alone" failed: **162 entries
became 155.**

Duplicate stored entries — one from the launcher catalogue carrying the
display name, one from disk carrying the executable path — keyed alike, and
`merge_library_updates` *assigned* rather than merged, so the later silently
replaced the earlier and its fields were gone. Seven games in your library
were affected: Battlefield 3, Immortals of Aveum, STAR WARS Jedi: Fallen
Order, Clair Obscur: Expedition 33, DOOM: The Dark Ages, Kingdom Come:
Deliverance II, Dune: Awakening.

They now fold: the displayed name survives, the executable path is gained,
recorded test results and process names are unioned, and the operation is
idempotent. Its own docstring had promised entries are never dropped.

### A tested module that was not in the build

`tasks.py` was written, tested and never imported, so PyInstaller did not
bundle it — the tests passed while the shipped application contained no such
module. It is now what the GUI actually runs background work through, and
`test_every_package_module_is_reachable_from_the_entry_point` fails on any
module nothing imports. I verified that test fails on a deliberately orphaned
module rather than passing vacuously.

---

## 3. Why these reached your desktop

The GUI tests never executed in this environment. `tkinter` is absent from the
interpreter the suite uses, so the whole module was skipped — reported as one
quiet `1 skipped` line under a green summary.

They now run under `python3.12` with `xvfb`, and a run without them prints a
prominent warning naming the skipped modules and the command that fixes it. A
summary claiming "750 passed" when the interface was never exercised is not a
true account of the run.

```
PYTHONPATH=src xvfb-run -a python3.12 -m pytest tests/
```

---

## 4. The rest of the brief

**Bulk selection** — Select all / none / invert, "select not ready", a live
count, Ctrl+A that yields to text boxes. Everything operates on the *visible*
games: with the Steam tab and a search active, Select all takes those and no
others. Selection lives in the model, so it survives repaints and scans.
"Select not ready" deliberately excludes UNKNOWN.

**Search, filter, sort** — debounced at 180ms; filtering and sorting read
cached verdicts, so a tab switch performs no fuzzy matching. A test pins that.

**Empty states** — four distinct messages naming the way out: empty library,
search matched nothing, filter excluded everything, launcher tab empty. The
table is hidden while the message shows.

**Readiness dashboard** — each category is a button that filters to it; empty
ones are disabled. "Cannot determine" is its own category, never counted as
needing attention, and when nothing else is outstanding the verdict says so
rather than withholding the all-clear. Counts follow the tab, not the active
filter — otherwise filtering to Ready would have made the dashboard read 100%.

**ROG Ally Life search** — a tab that searches the cached catalogue whether or
not a game is installed, shows profile counts, source dates and settings
verbatim with the URL attached. **It never adds anything to the library**; two
tests assert `games.json` is byte-identical after a search and a selection.

Searching had to be separated from matching. `matcher.match` decides whether a
game *is* a title, where a wrong answer silently applies another game's
settings, so it refuses partial titles — "Game" must never become "Game 2".
That strictness made the search box useless: typing "cyberpunk" found nothing.
Search now also accepts substring hits, ranked strictly below real matches,
and a test pins that the generosity does not leak back into automatic
association.

**Settings presentation** — the comparison opens with a count per outcome, and
a test asserts the categories account for every setting exactly once.

**Prepare for Travel** — lists the games it will start, with "… and N more",
and names each skipped game with its reason.

**Diagnostics** — Copy log, Save log, Open data folder, Clear, with an
environment header. On secrets: TravelReady holds no credentials or tokens; it
reads a public site anonymously and never authenticates, and a test asserts
the text contains none of those words rather than assuming it. What the log
*does* carry is paths, and on Windows those name the account —
`C:\Users\SHAHZ\…` identifies a person — so `redact_paths` replaces the
account with `%USERNAME%` on everything leaving the application.

**Background work** — one runner for every operation, three outcomes never
collapsed, exceptions delivered with their traceback. A worker returning
normally after a cancel is reported cancelled, not complete. The progress bar
sweeps while the total is unknown and becomes a real measure once counts
arrive.

---

## 5. What I did not do

**I did not split the GUI into a package of view modules.** The plan I
committed at the start of this pass said I would. The fault was never that the
views shared a file — it was that each kept its own copy of the state, which
is fixed, and fixed in a way a file split would not have achieved. Splitting a
working 1,676-line module in the same pass as the bug fixes is a large diff
across every handler where a mistake would be indistinguishable from one of
the bugs. If the file keeps growing it is worth doing on its own.

---

## 6. CODE VERIFIED vs REQUIRES REAL HARDWARE TEST

### Verified here

- 834 tests, including 66 GUI tests driving the real window under Xvfb
- Every reported fault reproduced by a test that fails against the old code
- Built from `travelready.spec`; all modules and data present in the bundle
- The frozen binary run from `/tmp/TravelReady-Test` with the repository off
  the path: `--help`, `doctor` and `status` work, the GUI starts clean, data
  lands in the per-user directory, and `games.json` is byte-identical after a
  session
- A blocked network ends the sync in 26s reporting "could not be reached" with
  the cache intact — not "no recommendations"
- No developer path baked into any module; nothing resolves data against the
  checkout

### Requires real hardware

**This was a Linux bundle built from the same spec.** It proves the packaging,
the imports, the data files and the repo-independence. It is not the Windows
binary, and these still need the device:

1. `pyinstaller travelready.spec` on Windows, then `dist\TravelReady.exe`
2. The same EXE copied to `%TEMP%\TravelReady-Test\` and run from there
3. A real ROG Ally Life sync over a live network — including pressing Cancel
   mid-sync, and confirming the second sync is fast because of the 304s
4. A real scan on the device, confirming new games appear without a restart
5. Xbox and EA launch-and-verify on actual installs
6. Any settings apply — every write path remains behind backup, preview,
   explicit approval and read-back verification

The 10 hardware-marked tests remain deselected by default and document what
each one needs.

---

## 7. Preserved from the earlier passes

Re-checked and holding: `os.startfile` / ShellExecuteW retained with no
`cmd /c start` anywhere; Epic's `?action=launch&silent=true` still accepted
(three tests); `file://` and UNC targets still blocked; `transaction.py` still
the only writer; read-only inspection still enforced against a filesystem
sentinel; anti-cheat, DRM, executables, DLLs, drivers and protected processes
never touched; no percentage safety scores; no "Force Apply Anyway"; the two
supplied `games.json` fixtures still pristine and still pinned by a test.
