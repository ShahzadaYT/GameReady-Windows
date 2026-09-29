# What is verified, and what still needs the device

An honest split. Nothing below is described as "fixed" without saying how it
was established.

## CODE VERIFIED

Established by the automated suite (272 tests) on Linux, including tests driven
by the two real `games.json` libraries supplied with this work.

| Area | Evidence |
|---|---|
| Library model, v1→v2→v3 migration, corrupt-file handling | `test_library.py` |
| Rescan never loses test history or manual edits | `test_library.py` |
| Readiness states, launcher tabs, live counts | `test_readiness.py` |
| Prepare-for-Travel no longer excludes Xbox | `test_readiness.py`, `test_xbox_regression.py` |
| EA detection state machine, all named failure states | `test_ea_regression.py` |
| Install-directory detection, respawn, late start, immediate exit | `test_ea_regression.py` |
| EA infrastructure never adopted as the game | `test_ea_regression.py` |
| Xbox launch/verification separation, manifest resolution, entry merging | `test_xbox_regression.py` |
| Store junk filtering (25 non-games removed from the real library) | `test_xbox_regression.py` |
| Discovery parsers (Get-StartApps, ACF, VDF), blacklists, exe selection | `test_discovery.py` |
| Cleanup closes only what the test started | `test_ea_regression.py` |
| Anti-cheat / DRM / executable / driver / traversal blocking | `test_safety.py` |
| Fail-closed composition; no force-apply anywhere | `test_safety.py` |
| Config preservation: comments, order, duplicates, encoding, BOM, newlines | `test_configio.py` |
| Profile schema, mandatory attribution, no invented values | `test_profiles.py` |
| Backup, verify, dry-run, approval, atomic write, restore, rollback | `test_transaction.py` |
| The read-only half writes nothing | `test_readonly.py` |
| CLI behaviour end to end | `test_cli.py` |
| GUI builds, tabs filter, dashboard, settings panel | `test_gui.py` |

## REQUIRES REAL HARDWARE TEST

Collected and skipped by `tests/test_hardware_required.py`, so the boundary is
visible in the test report rather than buried here. Run on the device with:

```
pytest -m hardware
```

| What | Why it cannot be verified here |
|---|---|
| `shell:AppsFolder\<AppID>` actually starting a Game Pass title | Needs Windows shell activation and an installed title |
| `Get-AppxPackageManifest` output shape and the executables it returns | Needs real MSIX packages |
| `C:\XboxGames\…\Content` layout across publishers | Needs installed Xbox titles |
| EA App startup timing and the launcher→game hand-off | Needs EA App installed and signed in |
| `link2ea://` URI behaviour per title | Needs real EA entitlements |
| `tasklist` / `Win32_Process` output shape | Needs Windows |
| Registry discovery for EA, Ubisoft, GOG, Battle.net | Needs those launchers installed |
| Applying settings to a real game's `GameUserSettings.ini` | Needs an installed Unreal title |
| Touch, scaling and readability on the 7" panel | Needs the ROG Ally X |
| PyInstaller build and launch of `TravelReady.exe` | Needs Windows |

## Suggested first run on the device

```
travelready scan -v                 # rebuild the library with the new discovery
travelready list --launcher xbox    # Xbox entries should now show a process name
travelready status                  # how many can be verified automatically
travelready test --launcher xbox --name "DOOM" --cleanup -v
travelready test --launcher ea --cleanup -v
travelready prepare --launcher xbox
```

Compare `travelready status` before and after the rescan: the count of games
that "cannot be verified automatically" is the headline number for whether the
Xbox and EA fixes took effect on your hardware.

## A note on the two regressions

Both root causes were established from the shipped bytecode and from your own
`games.json`, not inferred:

* **EA** — every EA entry has an empty `exe_path` *and* an empty
  `expected_process`, while the old detector had only those two signals. The
  failure was deterministic, and no timeout value could have fixed it.
* **Xbox** — the same games appear twice in your library, once launchable and
  once verifiable, never joined; and the manifest resolution that previously
  produced the process name had been deleted.

The fixes are code-verified against that data. Whether they make *your* games
launch on *your* device is the hardware test above.

---

## ROG Ally Life integration

### CODE VERIFIED

| Area | Evidence |
|---|---|
| Device family read from the post URL, including truncated slugs | `test_rogallylife_parser.py` |
| Post-title decoration stripped to the game's name | `test_rogallylife_parser.py` |
| Multiple profiles per game, each with its own settings table | `test_rogallylife_parser.py` |
| Unrecognised settings kept, never dropped or guessed | `test_rogallylife_parser.py` |
| A page with no settings yields no profiles | `test_rogallylife_parser.py` |
| Malformed markup does not raise | `test_rogallylife_parser.py` |
| Content hash covers the settings tables | `test_rogallylife_parser.py` |
| Matching: trademark marks, apostrophes, dotted acronyms, colon/dash, editions, roman numerals, articles | `test_rogallylife_matcher.py` |
| No automatic match between any two distinct titles in the real `games.json` | `test_rogallylife_matcher.py` |
| Sequel and sibling guards (`Forza Horizon 5`≠`6`, `Unbound`≠`Heat`) | `test_rogallylife_matcher.py` |
| Cache lifecycle, change detection, parser-version migration, corruption recovery | `test_rogallylife_cache.py` |
| robots.txt, Crawl-delay, conditional requests, 304, retry/backoff, blocked reporting | `test_rogallylife_cache.py` |
| Sync via REST API, sitemap and index fallbacks; unchanged/updated/missing | `test_rogallylife_cache.py` |
| A blocked source leaves the cache intact and says so | `test_rogallylife_cache.py` |
| Capability matrix gates what is applicable | `test_rogallylife_integration.py` |
| Profile selection per operating mode, with reasons and alternatives | `test_rogallylife_integration.py` |
| Attribution survives into the plan; local profiles win over synced data | `test_rogallylife_integration.py` |
| Hostile source data cannot become applicable | `test_rogallylife_security.py` |
| Client pinned to its own host; cache keys cannot escape `games/` | `test_rogallylife_security.py` |

### REQUIRES A REACHABLE rogallylife.com

`rogallylife.com` is blocked by this environment's egress proxy, so nothing
below has run against the real site. Run on a machine with access:

```
pytest -m hardware
travelready settings update -v
travelready settings coverage --detail
```

| Test | What it would prove |
|---|---|
| `test_rogallylife_is_reachable` | the host answers |
| `test_rogallylife_rest_api_is_available` | `/wp-json/wp/v2/posts` exists and paginates — the preferred route |
| `test_rogallylife_robots_is_honoured_against_the_real_file` | the real robots.txt permits what we fetch |
| `test_a_real_post_parses_into_profiles` | **the important one** — the parser against real markup, and the one that would catch a layout change |
| `test_full_sync_against_the_real_site` | discovery, fetch, parse and cache end to end |

If `test_a_real_post_parses_into_profiles` fails, the page structure differs
from what was inferred: update `parser.py`, raise `PARSER_VERSION`, and the
cache re-reads everything on the next sync.
