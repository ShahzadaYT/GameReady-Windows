# Architecture

```
run.py                      entry point (GUI by default, CLI with arguments)
src/travelready/
  textnorm.py               shared text/path primitives and the atomic write
  apppaths.py               where TravelReady keeps its own data
  environment.py            what this machine can actually check
  library.py                GameEntry, versioned persistence, migration, merging
  classification.py         GAME / APPLICATION / LAUNCHER / UTILITY / SYSTEM
  identity.py               GameIdentity owning Installations
  launchers.py              one adapter per launcher, with explicit capabilities
  processes.py              read-only process inspection + guarded termination
  discovery.py              read-only discovery from every launcher
  launch_tester.py          launch + multi-signal verification state machine
  preparation.py            per-game checks, states and readiness verdicts
  prepare_run.py            Prepare-for-Travel as a persisted, resumable run
  readiness.py              tabs, staleness, settings state
  doctor.py                 diagnostics, and safe repair of local state only
  history.py                test-result history
  cli.py                    full command-line interface
  gui_app.py                tkinter interface, sized for a 7" touchscreen
  optimiser/                the settings engine (read-only except transaction)
    rogallylife/            the recommendation source adapter
```

## Seven rules shape the layout

### 1. "Cannot determine" is not "no"

`environment.py` gives every check a requirement (`PURE_LOGIC`,
`WINDOWS_REQUIRED`, `ROG_ALLY_REQUIRED`, `LIVE_NETWORK_REQUIRED`,
`LIVE_GAME_REQUIRED`, `LAUNCHER_REQUIRED`). A check whose requirement this
machine cannot satisfy returns `UNKNOWN` with the reason — never `FAIL`.

This is not fastidiousness. Both regressions this project has fixed were
exactly this error: a 90-second EA timeout reporting `TIMEOUT` when the truth
was "there was never anything to look for", and an unsynced recommendation
cache reporting "No profile" when the truth was "we have not looked".

### 2. Launch capability is not verification capability

An Xbox game is launched through `shell:AppsFolder\<AppID>` and verified by its
executable. Collapsing the two is what made Xbox look unlaunchable. So
`Installation` exposes `launch_target` and `verification_targets` as separate
properties, `launchers.py` reports `launch` and `verify_game` as separate
capabilities, and `preparation.py` checks them as separate questions. A game
TravelReady can start but not verify is still preparable — it just needs the
user to confirm.

### 3. Capabilities are levels, not booleans

`FULL` / `PARTIAL` / `NONE` / `UNKNOWN`, and a `PARTIAL` must say what it
depends on. Steam names the game's executable in its manifest; EA hands the
request to EA App and exits. Treating those as the same is the assumption the
EA regression rested on.

### 4. The game is not the installation

`identity.py` groups the flat `GameEntry` list into `GameIdentity` objects
owning `Installation`s. It is a **view** — the stored form is unchanged and
nothing migrates. A ROG Ally Life recommendation is about the game, so one game
installed from Steam and Xbox looks up one recommendation, not two.

### 5. Platform access sits behind a seam

`ProcessTable`, `RogAllyLifeClient` and `run_test` all take their platform
dependencies as parameters, so the detection state machine, the sync and the
preparation orchestrator are unit-tested off Windows. `textnorm.as_path` parses
Windows paths on any host, so the tests exercise the logic the device runs.

### 6. Derived state owns its own invalidation

The window renders games (`identities`), which are derived from stored entries
(`entries`). For a while, three code paths mutated the entries and each was
expected to rebuild the derived data by hand. Two did. The third — the scan
handler — did not, so scanned games appeared only after a restart.

The fix is not the missing call. `AppState` owns the library and everything
derived from it, and the only way to change the library is `set_entries`,
which drops the derived caches and publishes an event. A handler cannot forget
to invalidate, because handlers do not invalidate.

Views subscribe to `LibraryChanged`, `ScanCompleted`, `SettingsSyncCompleted`,
`ReadinessChanged`, `GameSelectionChanged`, `FilterChanged` and
`StatusChanged`. None of them keeps a copy of anything. Readiness is still
computed once per game and cached — a repaint that re-assessed 149 games on
the UI thread is what made tab switching slow — but the cache belongs to the
model that knows when it is stale.

`AppState` imports no tkinter, so the whole model, including filtering,
sorting and bulk selection, is tested without a display.

### 7. Long work is cancellable and bounded, and says which of three things happened

Every background operation runs through one `TaskRunner`: scanning, launch
testing, preparation, source refresh. Each ends as exactly one of `SUCCEEDED`,
`CANCELLED` or `FAILED`, and those are never collapsed. A worker that returns
normally after a cancel is reported cancelled, not complete — reporting
success would claim a full run over partial data.

Exceptions are caught at the thread boundary and delivered to a callback with
their traceback. Nothing logs-and-continues, because an exception escaping a
worker thread vanishes into stderr and leaves the window spinning.

Network work additionally carries a `Budget`: a deadline plus the cancel flag,
checked *inside* requests rather than only between them, and clipping both the
socket timeout and any retry backoff. A run that stops early saves what it has
and reports a partial result. That is rule 1 again in a different costume: "we
could not finish" must never be shown as "there is nothing there".

## The preparation model

```
Check   one question · a requirement · PASS/FAIL/WARN/UNKNOWN/NOT_APPLICABLE
        · a reason · evidence · how to fix it
State   NOT_INSTALLED · DISCOVERED · LAUNCHER_MISSING · AUTH_REQUIRED
        · READY_TO_LAUNCH · LAUNCHING · RUNNING · VERIFIED · PREPARED
        · OFFLINE_READY · ACTION_REQUIRED · UNSUPPORTED · FAILED
Verdict READY · READY_WITH_WARNINGS · ACTION_REQUIRED · NOT_READY
        · UNSUPPORTED · UNKNOWN
```

Checks: installed, launcher present, launch target valid, verification signal,
launcher sign-in, first successful online launch, offline support, and
recommendation availability. The settings check never blocks a verdict — a game
with no published profile is still ready to travel.

No check reads credentials, tokens or licence state. Where a launcher exposes
no supported read-only way to tell whether it is signed in, the answer is
`UNKNOWN` and the user is asked. Offline preparation is described as
instructions; TravelReady does not perform it, because signing a launcher in is
the user's account activity.

## Prepare-for-Travel

A run is a persisted object. Each game's outcome is written as it completes, so
`travelready prepare --resume` continues from where it stopped. A run from an
incompatible version is discarded rather than misinterpreted, and one game
raising an error does not end the run.

Settings are deliberately not applied by a run. Changing a game's configuration
is a separate, explicitly approved transaction; burying it in a 149-game batch
would be the silent modification the settings engine exists to prevent. A test
asserts this on the syntax tree.

## The settings engine

Unchanged by this pass except to route through the shared primitives.
`optimiser/transaction.py` remains the only component that writes, and
`tests/test_readonly.py` still proves the read-only half writes nothing, by
byte-level snapshot and by a guard that fails any write-mode `open()`.
