# Architecture

```
run.py                      entry point (GUI by default, CLI with arguments)
src/travelready/
  apppaths.py               where TravelReady keeps its own data
  library.py                GameEntry, versioned persistence, migration, merging
  processes.py              read-only process inspection + guarded termination
  discovery.py              read-only discovery from every launcher
  launch_tester.py          launch + multi-signal verification state machine
  readiness.py              readiness states, launcher tabs, prepare targets
  history.py                test-result history
  cli.py                    full command-line interface
  gui_app.py                tkinter interface, sized for a 7" touchscreen
  main.py                   dispatch
  optimiser/
    model.py                value types, safety classes, device + category tables
    safety.py               classification and protected-target refusal
    profiles.py             ROG Ally Life store: schema, validation, import
    inspector.py            read-only config discovery via per-engine schemas
    configio.py             minimal, preservation-first config editing
    diff.py                 recommended vs current comparison, plan rendering
    transaction.py          the only module that writes game files
```

## Two rules shape the layout

### 1. Read-only and write are separated, provably

Every module except `optimiser/transaction.py` is read-only.
`tests/test_readonly.py` enforces it with a byte-level snapshot of the sandbox
and a guard that raises on any write-mode `open()` or `os.replace`, and with an
AST check that the read-only modules do not even reference write primitives.

### 2. Platform access sits behind a seam, so logic is testable

`processes.ProcessTable` is an abstract read-only view of the process table.
`WindowsProcessTable` implements it with `tasklist` and `Win32_Process`;
`FakeProcessTable` implements it for tests, with scheduled process appearances
and exits.

`run_test` takes `table`, `clock`, `sleep` and `starter` as parameters. That is
why the whole EA and Xbox detection state machine — hand-off, late start,
respawn, immediate exit, wrong-process adoption, cleanup — is unit-tested on
Linux in under a second, instead of being a thing that can only be tried on the
device and quietly regress.

`library.as_path()` parses paths the way Windows would regardless of host OS,
so the tests exercise the same path logic the device runs.

## The detection state machine

The previous build had two detection signals: an exact `expected_process` image
name, or the PID it started (for `exe` launches only). Detection is now
multi-signal, tried in confidence order:

1. `expected_process` / `alt_processes` image names, excluding PIDs that existed
   before launch, launcher infrastructure, and protected processes
2. descendants of the PID we started
3. any new process running from the game's **install directory** —
   `safe_install_dir()` vets the scope so it can never sweep in unrelated
   software
4. the PID we started, for direct `exe` launches

An entry with *no* signal is reported immediately as `MANUAL_REQUIRED` with
outcome `NO_DETECTION_SIGNAL_AVAILABLE`, rather than sleeping out the launch
timeout and reporting a misleading `TIMEOUT`.

Outcomes are distinguishable rather than collapsed: `LAUNCHER_NOT_STARTED`,
`LAUNCHER_STARTED_GAME_DID_NOT`, `NO_DETECTION_SIGNAL_AVAILABLE`,
`EXPECTED_PROCESS_NOT_SEEN_BUT_INSTALL_DIR_ACTIVE`,
`GAME_STARTED_AFTER_DETECTION_WINDOW`, `GAME_EXITED_IMMEDIATELY`,
`GAME_RESPAWNED`, `GAME_VERIFIED`.

## Launch vs. verification

These are separate capabilities, which is the heart of the Xbox fix. An entry
carries a `verification` mode (`auto` / `hybrid` / `manual`) independent of
whether TravelReady can launch it. Xbox games are always launched through
`shell:AppsFolder\<AppID>`; only the verification half may fall back to asking
the user.

## Entry merging

Discovery can find the same game twice — one entry launchable but unverifiable,
another verifiable but not launchable. `discovery.merge_entries()` joins them on
a conservative identity key scoped to the launcher, keeping the better launch
route and adopting the missing verification data. This is what gives Xbox
entries a process name and EA URI entries an install folder.

`library.merge_library_updates()` folds a rescan into the stored library:
existing entries keep their id, test history, timeouts and manual edits, and a
scan may only fill in fields that are empty.
