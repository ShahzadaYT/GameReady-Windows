# TravelReady — Engineering Assessment

Date: 2026-09-29

## 0. Starting position (important)

The Git repository `ShahzadaYT/GameReady-Windows` contained **only `README.md`**
on both `main` and the development branch. There was no application source, no
tests and no history beyond two README commits:

```
4807abe Improve GameReady README
b4f65f2 Initial commit
```

The application source was therefore recovered from the two supplied
**PyInstaller builds** of `GameLaunchTester.exe`, which embed the Python 3.12
modules of the app:

| Upload | Build | Identifying evidence |
|---|---|---|
| `10e9b1f7-GameLaunchTester.exe` | **previous** | GUI class `LaunchTesterApp`; `GameEntry` lives in `launch_tester`; has `find_pids_by_path_prefix`, `safe_install_dir`, `can_auto_close`, `_map_creation_error`; discovery has `_store_executables`, `_make_xbox_entry`, `xbox_app_is_game` |
| `ebbc5c19-GameLaunchTester.exe` | **current** | GUI class `GameLaunchTesterGUI`; `GameEntry` moved to `library`; `library.load_library` performs the v1→v2 migration (`migrated v1 -> v2`, derives `launcher` from `source`); `readiness.tab_counts`; EA staged timeouts |

Both were unpacked (PyInstaller CArchive + PYZ) and analysed by decompiling with
Decompyle++ and, where decompilation was lossy, by reading the exact CPython
3.12 code objects (`co_consts`, `co_names`, `dis`). Constants, names, docstrings
and control flow quoted below are taken from that bytecode, not inferred.

Because the repository was empty, this work necessarily **reconstructs** the
application into the repo rather than patching files in place. The
reconstruction is behaviour-faithful to the *current* build — every Phase-1
feature listed in the brief is carried over — and the regression fixes are then
applied on top.

## 1. Xbox regression — root cause

### How the previous build launched Xbox games

`discovery._make_xbox_entry` (previous build), verbatim docstring:

> Xbox / Microsoft Store / Game Pass game. The OFFICIAL launch mechanism is
> `shell:AppsFolder\<AppUserModelID>`. When the package manifest exposes the
> game executable, the entry becomes fully automatic (tracked and closed by
> process name); otherwise it stays MANUAL - no protection is ever bypassed.

The pipeline was:

1. `_get_start_apps()` — `Get-StartApps` → `(Name, AppUserModelID)` pairs.
   *"These AppIDs are the OFFICIAL launch targets usable via shell:AppsFolder\<AppID>."*
2. `_store_package_families()` — `Get-AppxPackage | Where-Object { $_.SignatureKind -eq 'Store' -and -not $_.IsFramework -and -not $_.IsResourcePackage }`.
3. `_store_executables(pfns)` — **`Get-AppxPackageManifest` → `$m.Package.Applications.Application.Executable`**, docstring:
   *"PackageFamilyName -> game executable leaf name, resolved from the package manifest (Get-AppxPackageManifest - read-only, no package files are touched). Lets Store games be tracked and auto-closed by process name instead of requiring manual verification."*
4. `xbox_app_is_game()` — heuristic filter against a large `_XBOX_SKIP` list.
5. `_make_xbox_entry()` — automatic entry when the manifest gave an executable, MANUAL entry otherwise.

So the previous build already implemented exactly *automated launch + honest
fallback*. Nothing unsafe was involved: three read-only PowerShell queries and
the documented shell launch route.

### What changed

In the current build:

* `_store_executables`, `_store_package_families`, `_get_start_apps`,
  `_make_xbox_entry` and `xbox_app_is_game` were **deleted**.
* `discover_xbox` now emits every Store app with `mode='manual'`,
  `expected_process=''` and a fixed note:
  `"Xbox Store apps run in protected containers and cannot be automatically verified. Test manually before travel."`
* `readiness.XBOX_MANUAL_NOTE` hard-codes the same claim.
* `gui_app._run_tests.work` forces `mode = MODE_MANUAL` whenever
  `entry.launcher == 'xbox'`.
* `gui_app._on_prepare_travel` **filters Xbox entries out of Prepare-for-Travel
  entirely** and reports them under `"Skipped (verify manually): "`.

The last point is the user-visible regression: in the main workflow Xbox games
are never launched at all.

### Why the removal was not necessary

The premise ("protected containers … cannot be automatically verified") is
wrong for the case that matters. Confirmed against the supplied `games.json`:

```
xbox | Clair Obscur: Expedition 33  | exe='' | expected='' | shell:AppsFolder\KeplerInteractive.Expedition33_…  | mode=manual
xbox | Clair Obscur- Expedition 33  | exe=C:\XboxGames\Clair Obscur- Expedition 33\Content\Sandfall\Binaries\…\SandFall-WinGDK-Shipping.exe | expected='SandFall-WinGDK-Shipping.exe' | mode=standard
```

The **same game is in the library twice**: once from `discover_xbox` (launchable
but unverifiable) and once from `discover_common_dirs` scanning `C:\XboxGames`
(verifiable but with no reliable launch route). Xbox/Game Pass PC titles install
to `C:\XboxGames\<Game>\Content\…`, which is ordinary, readable filesystem — not
`WindowsApps`. The two halves were simply never joined.

Same pattern for `DOOM- The Dark Ages`, `DSDC` (Death Stranding),
`Kingdom Come- Deliverance II`, `Dune- Awakening`.

### Fix adopted

Launch and verification are separated into distinct capabilities:

* **Launch** — always `shell:AppsFolder\<AppUserModelID>` (the documented route).
* **Verification** — `expected_process` resolved from, in order:
  1. the `C:\XboxGames\<title>\Content\` executable found by the filesystem scan
     (merged onto the `shell:` entry by identity matching), then
  2. `Get-AppxPackageManifest` (restored from the previous build), then
  3. install-directory process matching (see §2), then
  4. honest `MANUAL` verification.

A `VerificationMethod` is recorded on every result so the UI never implies a
level of confidence it does not have. Nothing injects, hooks, or modifies any
package. Regression tests pin all of this.

Additionally the current `_XBOX_SKIP` filter is far weaker than the previous
one: the supplied library contains Calculator, Notepad, Paint, Photos, Settings,
Terminal, Weather, Windows Security, Game Bar, Copilot, Recall, Snipping Tool,
Sticky Notes, Media Player, Clock, Dolby Access, Realtek Audio Console,
AMD Software, MyASUS, Armoury Crate SE and the Xbox app itself as "games". The
previous build's filter list is restored and extended.

## 2. EA regression — root cause

Traced through the current `run_test` bytecode (source lines 595–648). The
detection loop is:

```python
while time.time() < detect_deadline:
    ...
    if is_ea and not ea_launcher_noted:        # EA App infra detection
        ...
    if expected:                                # (A)
        current = set(find_pids_by_name(expected)) - pre_existing
        current = {p for p in current if not _pid_is_launcher_infra(p)}
        if current:
            game_pid = sorted(current)[0]; break
    elif result.launch_method == METHOD_EXE and launcher_pid and pid_alive(launcher_pid):   # (B)
        game_pid = launcher_pid; break
    time.sleep(POLL_INTERVAL)
```

Detection has exactly **two** signals: (A) an exact `expected_process` image
name, or (B) the PID we started, and (B) only for `launch_method == 'exe'`.

Now the supplied `games.json` — every EA entry discovered from EA itself:

```
ea | GRID Legends                     | exe_path='' | expected_process='' | uri link2ea://launch/16274527 | last_result=''
ea | F1® 24                           | exe_path='' | expected_process='' | uri link2ea://launch/16425782 | last_result=''
ea | STAR WARS Jedi: Fallen Order™    | exe_path='' | expected_process='' | uri link2ea://launch/196485   | last_result=''
ea | Mass Effect™ Legendary Edition   | exe_path='' | expected_process='' | uri link2ea://launch/198196   | last_result=''
… 13 EA entries, all identical in shape
```

`_expected_process_for(entry)` is `entry.expected_process or os.path.basename(entry.exe_path)` — with both empty it returns `''`.

So for **every** EA entry:

* branch (A) is skipped — `expected` is falsy;
* branch (B) is skipped — `launch_method` is `uri`, not `exe`;
* the loop sleeps for the full `EA_GAME_PHASE_TIMEOUT` (90 s) and exits;
* `game_pid is None` → `STATUS_TIMEOUT`, *"game process '?' never appeared within 90s"*.

**The EA failure is deterministic and has nothing to do with timing.** No
timeout value can fix it: the code has no signal to detect with. Raising
45 s/90 s only made each guaranteed failure slower. `last_result` is empty for
all 13 EA entries, consistent with never having passed.

The `_pid_is_launcher_infra` guard is real and correct, but it can only filter a
candidate set that is always empty here.

Secondary EA defects confirmed in the same data:

* **EA infrastructure catalogued as a game** —
  `ea | Electronic Arts | exe=C:\Program Files\Electronic Arts\EA Desktop\13.791.0.6304\EADesktop.exe | expected_process='EADesktop.exe'`.
  Testing this entry "passes" by detecting the EA App itself.
* **Folder names used as game names** —
  `ea | EA Games | exe=…\Need for Speed Unbound\NeedForSpeedUnboundTrial.exe`.
* **Duplicate, complementary entries never merged** — `Battlefield 3` exists as
  a URI entry with no exe *and* as a shortcut entry with `bf3.exe`; likewise
  `Immortals of Aveum` and `STAR WARS Jedi: Fallen Order`.

### Fix adopted

1. `discover_ea` resolves each title's **install directory and executable** from
   the EA/Origin registry and `__Installer` manifests, so URI entries carry a
   real `expected_process` and `install_dir`.
2. **Install-directory process detection restored.** The previous build had
   `find_pids_by_path_prefix()` — *"Catches game helper processes whose names
   differ from the expected process name"* — plus `safe_install_dir()` guarded by
   `_GENERIC_DIRS`, and `can_auto_close()` — *"Prepare-for-travel must NEVER
   launch what it cannot close"*. All three were deleted in the current build
   and are restored.
3. Detection is now multi-signal and each outcome is reported as a distinct
   `LaunchOutcome`, so the failure states named in the brief are
   distinguishable rather than collapsing into `TIMEOUT`.
4. Discovery-level EA infrastructure exclusion, and identity-based merging of
   complementary entries.

## 3. Third regression found: deleted process-detection capability

For completeness, these previous-build functions were removed and are restored
because both fixes depend on them:

| Function | Previous-build docstring |
|---|---|
| `find_pids_by_path_prefix` | "PIDs whose executable lives under dir_path (read-only CIM query). Catches game helper processes whose names differ from the expected process name…" |
| `safe_install_dir` | "The game's install directory, or '' when exe_path is not a real file or lives somewhere too generic to be a safe tracking scope." |
| `can_auto_close` | "Prepare-for-travel must NEVER launch what it cannot close, otherwise games pile up and stay running." |
| `_map_creation_error` | Maps winerror 5 / 740 / 193 to `ACCESS_DENIED` / elevation / invalid-image statuses. |

## 4. Verified vs requires real hardware

**Code-verified here** (Linux, unit tests): library/migration, readiness,
discovery parsing and filtering against the real `games.json` fixtures,
detection state machine against a simulated process table, safety
classification, config parsing/preservation, diff, backup/restore, dry-run,
and every regression test.

**Requires real ROG Ally X / Windows validation**: actual `shell:AppsFolder`
launches, `Get-AppxPackageManifest` output, EA App hand-off timing, real
`tasklist`/CIM output, and applying settings to real game config files. These
are marked `REQUIRES_HARDWARE` in the test suite and are not claimed as fixed.

## 5. ROG Ally Life

`rogallylife.com` is **blocked by this environment's network policy**
(`connect_rejected` on `rogallylife.com:443`), so no profile could be fetched.
Per the brief, no settings have been invented. The profile subsystem ships as a
complete, schema-validated, source-attributed store with an importer and a
fetcher, and **zero fabricated profiles**. Test fixtures are explicitly marked
as fixtures and are not presented as ROG Ally Life recommendations.
