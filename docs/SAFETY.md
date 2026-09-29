# TravelReady safety model

## The absolute rule

TravelReady never modifies, disables, patches, bypasses, stops, replaces,
renames, deletes, injects into or hooks:

anti-cheat · DRM · anti-tamper · game executables · game DLLs · signed game
components · anti-cheat services · anti-cheat drivers · DRM services · launcher
authentication components · protected processes · kernel drivers · BIOS ·
firmware · Secure Boot · TPM · Windows security · Memory Integrity · any
game-integrity system.

This includes Easy Anti-Cheat, BattlEye, Vanguard, RICOCHET and every other
anti-cheat system.

The design goal is that targeting such a component is **difficult or
impossible**, not merely discouraged. The rules below are enforced in code and
pinned by `tests/test_safety.py`.

## How it is enforced

### Whitelist first

`optimiser/safety.py` only permits editing files whose extension is in
`ALLOWED_CONFIG_SUFFIXES`: `.ini .cfg .conf .json .xml .yaml .yml`. Every
executable, library, driver, archive, signature, certificate, licence, save and
binary format is absent, and an extension that is merely *unrecognised* is
refused too. There is no path by which a `.exe`, `.dll` or `.sys` becomes
editable.

### Unconditional blacklist on top

A path is BLOCKED if it contains an anti-cheat / DRM / anti-tamper marker, or
sits in a protected directory. Matching is deliberate about precision:

* distinctive markers (`easyanticheat`, `battleye`, `denuvo`, `vmprotect`, …)
  match anywhere in the path;
* short or ambiguous ones (`eac`, `vgk`, `drm`, `token`, `license`) match only
  as whole words, so a game called *Peaceful Nights* or *Breach* is not caught;
* protected directories match whole path components, so `C:\Windows` is blocked
  while Unreal Engine's `…\Config\WindowsNoEditor\` — the real home of
  `GameUserSettings.ini` — is not.

### Content check

`classify_file_content` reads the first bytes and refuses anything that is not
plain text. An executable renamed to `.ini` gets no further.

### Known settings only

A setting key must appear in `KNOWN_GAME_SETTINGS`. An unknown key is
RESEARCH_REQUIRED, never SAFE. "It's an .ini file" never by itself makes
anything safe: the file must also match a `ConfigSchema` TravelReady
understands.

### Device-wide settings are manual

TDP, power mode, fan profile, VRAM allocation, Armoury Crate, AMD Adrenalin and
Windows power plans are classified MANUAL. TravelReady shows them so you can
apply them yourself; it does not change them.

### Process safety

`processes.py` refuses to terminate any PID in `PROTECTED_PROCESSES` — anti-cheat,
DRM, launcher authentication and OS components — and the refusal lives inside
`ProcessTable.terminate`, so no caller can route around it. There is no
force parameter. Cleanup only ever touches the tree TravelReady started or
positively identified as the game, and it never kills a process to make a
settings write possible: if the game or launcher is running, you close it.

## Classification

Every proposed change gets exactly one of:

| Class | Meaning |
|---|---|
| `SAFE` | Understood, user-level, reversible. May be applied after approval. |
| `CAUTION` | Plausible but outside the usual locations. Needs review. |
| `MANUAL` | Real, but TravelReady will not do it (device settings, other device's profile). |
| `RESEARCH_REQUIRED` | Not understood well enough. Default for anything unrecognised. |
| `BLOCKED` | Protected target. Refused, permanently. |

There are **no percentages**, no "95% safe", no "probably safe", no risk scores.
Classifications compose by failing closed: the least permissive verdict wins,
and `worst()` with no input returns RESEARCH_REQUIRED.

Only `SAFE` is ever auto-applicable. **There is no force-apply.** A test parses
the syntax tree of every safety-relevant module and fails if any function
exposes a parameter such as `force`, `ignore_safety` or `allow_blocked`.

## Read-only by construction

These operations perform **zero writes**: game discovery, profile discovery,
ROG Ally Life lookup, settings discovery, configuration parsing, current-value
inspection, recommendation matching, safety classification, diff generation and
dry-run.

`optimiser/transaction.py` is the only module permitted to write.
`tests/test_readonly.py` proves the separation two ways at once: a byte-level
snapshot of the whole sandbox before and after, and a guard that raises if
anything opens a file in a write mode or calls `os.replace`.

## Writing, when it happens

1. verify the game is not running;
2. verify a config-rewriting launcher is not running;
3. re-resolve the target path;
4. verify it is the same file that was inspected (hash);
5. re-classify the target — a stale plan cannot write somewhere new;
6. create a backup and verify it by hash;
7. build the edit in memory;
8. produce the dry-run diff;
9. require explicit per-change approval;
10. write atomically;
11. re-read and verify;
12. record the transaction;
13. on any failure, restore from backup.

If restoration itself fails, a `CriticalRestoreError` is raised and the backup
is deliberately preserved. Your original configuration is never silently
destroyed.

## Configuration preservation

Config files are edited line-by-line: only the value portion of the one line
holding the key is replaced. Preserved byte-for-byte: comments, inline
comments, blank lines, ordering, indentation, separator spacing, duplicate-key
behaviour, unknown keys and sections, encoding, BOM presence, and newline style.
Keys are never created — a setting the game does not already write is one whose
placement and semantics TravelReady cannot be sure of.

If a file cannot be parsed with confidence, no edit is offered at all.
