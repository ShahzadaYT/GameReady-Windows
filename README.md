# TravelReady

**Prepare your ROG Ally X for playing offline.**

Some PC games will not start without an internet connection unless their
launcher has authenticated recently. You find out on the plane. TravelReady
answers two questions before you leave:

1. **Will my games actually work when I'm offline?**
2. **Can I safely configure them using the best available ROG Ally recommendations?**

It discovers your installed games, launches each one the way its launcher
would, confirms the game process really started and survived, closes it again,
and records the result. Then it can compare your current graphics settings
against a ROG Ally Life profile and apply the changes it is certain are safe —
after showing you the exact diff and taking a verified backup.

Designed for the **ASUS ROG Ally X**; also useful on other Windows handhelds.

---

## What it does

```
DISCOVER → PREPARE → LAUNCH → VERIFY → CLEAN UP → OPTIMISE → VERIFY → READY FOR TRAVEL
```

- Discovers games from **Steam, Epic, EA, Ubisoft, Xbox / Game Pass, GOG,
  Battle.net**, Start-menu shortcuts and folders you choose
- Organises them into launcher tabs with live counts
- Launches and **verifies** each game actually started — `CreateProcess`
  succeeding is never reported as a pass
- Closes what it started, gracefully first, and reports anything it could not
- Tracks readiness: READY / STALE / ATTENTION / MANUAL / UNTESTED
- Produces a trip report and keeps a history
- Compares current vs. recommended game settings and applies only the safe ones

## Install

Download `TravelReady.exe` from the [Releases](../../releases) page. No install,
no dependencies.

From source:

```
git clone https://github.com/ShahzadaYT/GameReady-Windows
cd GameReady-Windows
python run.py              # GUI
python run.py --help       # CLI
```

## Quick start

```
travelready scan                 # find installed games
travelready status               # what is ready, what is not
travelready prepare              # launch, verify and close everything not ready
travelready report               # the trip report
```

Settings:

```
travelready settings show    "Cyberpunk 2077"   # read-only comparison
travelready settings dry-run "Cyberpunk 2077"   # the exact diff a write would make
travelready settings apply   "Cyberpunk 2077"   # asks before changing anything
travelready settings restore "Cyberpunk 2077"   # put the backup back
```

## Launchers are not all the same

Each launcher is treated according to how it actually behaves.

| Launcher | Launch route | How a start is verified |
|---|---|---|
| Steam | `steam://rungameid/<id>` | process name from the app manifest |
| Epic | `com.epicgames.launcher://apps/<id>` | executable from the Epic manifest |
| EA | `link2ea://launch/<id>` | process name and install folder resolved from the EA registry |
| Ubisoft / GOG / Battle.net | installed executable | process name, install folder |
| **Xbox / Game Pass** | `shell:AppsFolder\<AppUserModelID>` | process name from the package manifest or `C:\XboxGames`, else honest manual confirmation |

**Xbox games are launched automatically.** Launch and verification are separate
capabilities: a game can be started for you while being honestly reported as
manually verified, rather than refusing to start it at all.

## Game settings

ROG Ally Life (<https://rogallylife.com/>) is the **primary** source of
recommended per-game settings. TravelReady stores those recommendations as
inert, attributed data and independently decides what it is allowed to change.

> ROG Ally Life is a community source. It is **not** ASUS, and TravelReady
> never presents it as official ASUS guidance.

**This build ships no profiles.** The site was not reachable from the
environment this version was built in, and inventing plausible settings would
defeat the point. Add one:

```
travelready profile-template "Cyberpunk 2077" > cyberpunk.json
# fill it in from the ROG Ally Life page, then:
travelready profile-import cyberpunk.json
```

The importer rejects a profile with no `source_url`, or one naming a setting
TravelReady does not recognise.

### What gets changed

Every proposed change is classified `SAFE`, `CAUTION`, `MANUAL`,
`RESEARCH_REQUIRED` or `BLOCKED`. No percentages, no "probably safe". Only
`SAFE` can be applied, only after you approve it, and only after a verified
backup. Device-wide settings — TDP, fan profile, VRAM, Armoury Crate, Adrenalin,
Windows power plans — are `MANUAL`: shown, never changed.

Your config files keep their comments, ordering, duplicate keys, unknown keys,
encoding and line endings, byte for byte. Only the value you approved changes.

## Safety

TravelReady does **not** bypass DRM, launcher authentication or anti-cheat. It
uses the normal launch paths and watches for the game's process.

It never modifies, disables, stops, patches, renames, deletes, injects into or
hooks anti-cheat, DRM, anti-tamper, executables, DLLs, drivers, services,
protected processes, firmware or Windows security — and it is built so that
targeting those is difficult rather than merely discouraged. There is no
"force apply".

Full detail: **[docs/SAFETY.md](docs/SAFETY.md)**.

## Documentation

| | |
|---|---|
| [docs/SAFETY.md](docs/SAFETY.md) | What TravelReady will and will not touch, and how that is enforced |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module layout and the read-only / write split |
| [docs/ENGINEERING_ASSESSMENT.md](docs/ENGINEERING_ASSESSMENT.md) | Audit of the previous builds and the root causes of the Xbox and EA regressions |
| [docs/HARDWARE_VALIDATION.md](docs/HARDWARE_VALIDATION.md) | What is code-verified vs. what still needs the device |

## Development

```
pip install -e ".[dev]"
python -m pytest              # 272 tests
pytest -m hardware            # the cases that need a real device
```

Building the Windows executable:

```
pip install pyinstaller
pyinstaller travelready.spec  # -> dist/TravelReady.exe
```

---

**Version** 0.2.0 · **Platform** Windows · **Target** ASUS ROG Ally X
