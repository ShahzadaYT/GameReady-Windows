# Pre-hardware application audit

Date: 2026-09-29 · Baseline: `b403585`, 9,871 lines of source, 513 tests passing.

Traced startup → scan → identity → launcher → prepare → verify → settings →
report, then classified every subsystem. This is the map the consolidation work
that follows is based on.

## 1. Findings by classification

### DEAD

17 symbols defined and never used anywhere, including tests:

`attach_ea_uri`, `run_batch`, `EA_LAUNCH_TIMEOUT`, `STANDARD_EXIT_WATCH`,
`legacy_data_files`, `discovery._exists`, `library.add_entry`,
`library.dedupe`, `library.find_by_name`, `library.save_scan_folders`,
`LAUNCH_METHODS`, `DEVICE_UNKNOWN`, `SOURCE_ID`, `MODE_DESCRIPTIONS`,
`summarise_profiles`, `HISTORY_FILE`, `APP_NAME`.

Two are actively misleading rather than merely unused:

* `attach_ea_uri` is a second, weaker implementation of what
  `discovery.merge_entries` already does. Two ways to join EA entries is one too
  many.
* `EA_LAUNCH_TIMEOUT = EA_GAME_PHASE_TIMEOUT` is a leftover alias from the
  build whose EA bug was *caused* by trusting timeouts. Keeping the name around
  invites its reintroduction.

### DUPLICATED

| What | Where | Problem |
|---|---|---|
| `_atomic_write` | `library.py`, `history.py`, `rogallylife/cache.py`, inline in `transaction.py` | four copies of the one operation that must never leave a truncated file |
| Title normalisation | `library.identity_key` (conservative, joins installs) vs `matcher.normalize_title` (rich, matches the source) | genuinely different jobs, but they can drift: a pair that merges under one rule may not match under the other |
| Path normalisation | `library.as_path`, `processes._norm_dir`, `safety._normalise` | three spellings of "compare Windows paths safely" |

The two title normalisers are **kept separate deliberately** — merging two
installs of one game and matching a library title to an article title are
different risk profiles — but they now share one `textnorm` primitive layer so
they cannot silently diverge.

### FRAGILE

* `readiness.settings_state_of` catches bare `Exception` and returns
  "Not checked", hiding real errors.
* GUI `_refresh` resolves **every** entry against the source cache on every
  repaint: 149 games × fuzzy match over the whole cache, on the UI thread, per
  tab switch.
* `library.merge_library_updates` merges on `identity_key`, which strips
  edition words. Two genuinely different SKUs sharing a base name would merge.
* `discovery` classification is boolean with no recorded reason, so a user
  cannot see *why* something was excluded.

### MISLEADING

`readiness.settings_state_of` returns `No profile` when the cache is simply
empty. "The source has no recommendation" and "we have not synced yet" are
different facts and must not share a label. Same class of error as the EA
timeout that hid a detection failure.

### INCOMPLETE — the significant gaps

Grepping for `authenticat|offline|cloud.?sync|first.?launch|dependenc` across
the application returns **no implementation**: only docstrings and one log
line. The mission question — *"which of my games are actually ready to play
offline"* — is therefore not answered by the current code at all. It reports
whether a game *launched recently*, which is a proxy, not the answer.

Also absent:

| Gap | Consequence |
|---|---|
| No game-vs-installation separation | one game on Steam and Xbox is two unrelated rows with two unrelated profiles |
| No launcher capability model | every launcher is treated as equally verifiable; the EA regression was exactly this mistake |
| No explicit preparation state machine | readiness is derived from `last_result` alone; there is no `AUTH_REQUIRED`, `OFFLINE_READY` or `NOT_INSTALLED` |
| No resumability | an interrupted 149-game prepare restarts from zero |
| No doctor | nothing tells the user why their environment cannot do something |
| No environment capability model | the app cannot say which checks it is able to perform here |

### UNTESTABLE / ENVIRONMENT-DEPENDENT

Already isolated well: `ProcessTable` (injectable), `RogAllyLifeClient`
(injectable opener), `run_test` (injectable clock/sleep/starter). Not isolated:
registry access and `_powershell` are called directly from `discovery`
functions, so no discoverer can be exercised off Windows.

## 2. What this pass changes

1. Consolidate the four `_atomic_write` copies and the path/text normalisers;
   delete the dead symbols.
2. Introduce `identity.py`: `GameIdentity` grouping `Installation`s, as a view
   over the existing `GameEntry` list — no schema rewrite, nothing lost.
3. Introduce `launchers.py`: one adapter per launcher with an explicit
   capability record, replacing the implicit assumption that every launcher
   behaves alike.
4. Introduce `preparation.py`: an explicit state machine with per-check
   evidence, answering the offline-readiness question honestly.
5. Introduce `environment.py`: what this machine can actually check.
6. Introduce `doctor.py` and `travelready doctor [--fix]`.
7. Make `prepare` orchestrate and resume.
8. Give the scanner recorded classification reasons.
9. Separate "source unavailable" from "no recommendation" everywhere.
10. Rework the GUI around Prepare-for-Travel as the primary workflow.

Nothing in the Xbox, EA, security or settings-transaction work is touched
except to route it through the consolidated helpers; the regression suites for
all four are the gate on every commit below.
