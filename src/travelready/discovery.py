"""discovery.py — READ-ONLY game discovery from all major launchers.

Supported sources: Steam, Epic, EA (Origin / EA App), Ubisoft Connect,
Xbox / Microsoft Store, GOG Galaxy, Battle.net, Windows Start-menu shortcuts,
common install directories and user-supplied custom folders.

SAFETY CONTRACT
---------------
This module NEVER writes to game files, launcher files, Steam data, the
registry, or anywhere outside the application's own data directory. It only
*reads* the registry, manifest files and directory listings, and launches
PowerShell only to read data.

Xbox / Microsoft Store
----------------------
Restored from the older build, whose own docstring described the design:

    "PackageFamilyName -> game executable leaf name, resolved from the package
    manifest (Get-AppxPackageManifest - read-only, no package files are
    touched). Lets Store games be tracked and auto-closed by process name
    instead of requiring manual verification."

Three read-only queries — ``Get-StartApps``, ``Get-AppxPackage`` and
``Get-AppxPackageManifest`` — plus the documented ``shell:AppsFolder\\<AppID>``
launch route. Nothing is injected, patched or bypassed.

In addition, Xbox/Game Pass PC titles install to ``C:\\XboxGames\\<Title>\\Content``,
which is ordinary readable filesystem (not ``WindowsApps``). The supplied
library shows discovery finding the *same game twice* — once launchable but
unverifiable, once verifiable but not launchable — and never joining them.
:func:`merge_entries` joins them, which is what restores automatic Xbox
verification. See docs/ENGINEERING_ASSESSMENT.md §1.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .library import GameEntry, identity_key, normalize_launcher
from .textnorm import as_path, leaf

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
_SUBPROCESS_TIMEOUT = 30

Progress = Optional[Callable[[str], None]]

# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

_EXE_BLACKLIST = re.compile(
    r"(unins|uninstall|redist|vcredist|vc_redist|dxsetup|directx|setup|install|"
    r"crash|report|helper|updater|update|patcher|service|benchmark|server|editor|"
    r"config|tool|prereq|support|anti|eac|battleye|easyanticheat|launcher(?!.*game)|"
    r"overlay|cleanup|diag|touchup|activation|register|bootstrap|dotnet|oalinst|"
    r"physx|ue4prereq|ue5prereq|quickboot|cef|handler)",
    re.IGNORECASE,
)

_NAME_BLACKLIST = re.compile(
    r"(redistributable|^battle\.net$|^ubisoft connect$|^ubisoft game launcher$|"
    r"^ea desktop$|^ea app$|^ea games$|^origin$|^electronic arts$|"
    r"^epic games launcher$|^epic online services$|^gog galaxy$|"
    r"^rockstar games launcher$|^steam$|^steamvr$|^steamwebhelper$|^xbox$|"
    r"^microsoft store$|^game bar$|^game pass$)",
    re.IGNORECASE,
)

#: Store packages that are not games. Restored and extended from the older
#: build — the current build's filter is far weaker, which is why the supplied
#: library lists Calculator, Notepad, Paint, Settings, Terminal, Weather,
#: Windows Security, Copilot, Recall, Realtek Audio Console, AMD Software,
#: MyASUS and Armoury Crate SE as "games".
_XBOX_SKIP = (
    "netnative", "vclibs", "ui.xaml", "servicesstore", "storepurchaseapp",
    "windowsstore", "desktopappinstaller", "gethelp", "getstarted", "feedback",
    "calculator", "camera", "alarms", "maps", "soundrecorder", "photos",
    "notepad", "paint", "screensketch", "stickynotes", "terminal", "sechealth",
    "edge", "office", "onedrive", "teams", "outlook", "bing", "zune", "people",
    "messaging", "communicationsapps", "yourphone", "xboxidentityprovider",
    "xboxgamecallableui", "gamingservices", "gamingapp", "ecapp",
    "winappruntime", "storeexperiencehost", "brokerplugin", "accountscontrol",
    "lockapp", "parentalcontrols", "defender", "devhome", "webexperience",
    "webmediaextensions", "startmenuexperiencehost", "shellexperiencehost",
    "searchapp", "inputapp", "immersivecontrolpanel", "extension", "clipchamp",
    "xboxspeechtotextoverlay", "xboxgamingoverlay", "xbox.tcui",
    "applicationcompatibility", "widgetsplatformruntime", "languageexperiencepack",
    "powerautomatedesktop", "startexperiencesapp", "todos", "crossdevice",
    "winget", "officehub", "copilot", "openai", "chatgpt",
    "advancedmicrodevices", "amdsoftware", "nvidia", "realtek",
    "pythonsoftwarefoundation", "python", "maxon", "cinebench", "intellij",
    "jetbrains", "codeblocks", "notepad++", "7zip", "vlc", "spotify", "discord",
    "slack", "zoom", "obsproject", "obsstudio",
    # additionally observed as false positives in the supplied library
    "client.coreai", "client.cbs", "client.aix", "quickassist", "dolbylaboratories",
    "armourycrate", "asuspcassistant", "myasus", "windowsalarms", "windowsnotepad",
    "windowscalculator", "windowsterminal", "bingweather", "zunemusic",
    "zunevideo", "windows.photos", "microsoftstickynotes", "screensketch",
    "windowsbackup", "windowsclient", "mixedreality",
    "securityhealth", "windowscamera", "windowssoundrecorder",
)

_EA_INFRA_DIRS = ("ea desktop", "electronic arts\\ea desktop", "origin",
                  "eaappinstaller", "ea app")

_COMMON_DIRS = (
    r"C:\XboxGames",
    r"C:\Program Files\EA Games",
    r"C:\Program Files (x86)\Origin Games",
    r"C:\Program Files\Epic Games",
    r"C:\Program Files (x86)\Steam\steamapps\common",
    r"C:\Program Files\WindowsApps",   # listed only; never opened for writing
    r"C:\GOG Games",
    r"C:\Games",
)

#: Directories that are launcher infrastructure, never a game.
_INFRA_DIR_TOKENS = ("ea desktop", "eaappinstaller", "origin", "epic games launcher",
                     "launcher", "__installer", "installers", "directx", "redist",
                     "_commonredist", "vcredist", "dotnet", "support")


def is_blacklisted_exe(name: str) -> bool:
    """True when an executable name looks like tooling rather than a game."""
    return bool(_EXE_BLACKLIST.search(os.path.basename(str(name or ""))))


def is_blacklisted_name(name: str) -> bool:
    """True when a title looks like a launcher or utility rather than a game."""
    return bool(_NAME_BLACKLIST.search(str(name or "").strip()))


def xbox_app_is_game(name: str, app_id: str) -> bool:
    """Heuristic filter: is this Store-registered app plausibly a game?

    Filters Windows system apps and well-known non-game utilities. Restored
    from the older build, with its matching tightened: that build compared skip
    tokens against ``name + app_id`` as one squashed string, so ``"edge"``
    rejected **DREDGE**. Here the package family is matched by substring (where
    ``Microsoft.MicrosoftEdge`` genuinely contains ``edge``) while the display
    name is matched on whole words, so a game whose title merely contains a
    token survives.
    """
    if is_blacklisted_name(name):
        return False
    family = str(app_id or "").split("!", 1)[0].lower()
    words = [w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if w]
    squashed = "".join(words)
    for token in _XBOX_SKIP:
        if token and token in family:
            return False
        if token in words:
            return False
        # multi-word product names: "Armoury Crate SE" vs "armourycrate"
        if len(token) >= 8 and token in squashed:
            return False
    return True


def is_launcher_infrastructure(path: str) -> bool:
    """True when a path belongs to launcher plumbing, not a game.

    Prevents ``EADesktop.exe`` being catalogued as a game — which the supplied
    library shows happening (``ea | Electronic Arts | … EADesktop.exe``).
    """
    low = str(path or "").lower().replace("/", "\\")
    return any(token in low for token in _INFRA_DIR_TOKENS + _EA_INFRA_DIRS)


# --------------------------------------------------------------------------
# windows helpers (all read-only)
# --------------------------------------------------------------------------

def _run(cmd: Sequence[str], timeout: int = _SUBPROCESS_TIMEOUT) -> str:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True,
                              timeout=timeout, creationflags=_NO_WINDOW)
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _powershell(script: str, timeout: int = 60) -> str:
    if not IS_WINDOWS:
        return ""
    exe = os.environ.get("TRAVELREADY_POWERSHELL") or "powershell"
    return _run([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def _read_registry_value(root, subkey: str, name: str) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return ""


def _iter_registry_subkeys(root, subkey: str) -> List[str]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    out: List[str] = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            index = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        return []
    return out


def largest_game_exe(directory: str, *, lister=None) -> str:
    """The most plausible game executable in ``directory``.

    Largest non-blacklisted ``.exe``, searched a few levels deep. ``lister`` is
    injected by tests so the choice logic can be exercised without a filesystem.
    """
    candidates: List[Tuple[int, str]] = []
    if lister is not None:
        rows = list(lister(directory))
    else:
        rows = []
        root = Path(directory)
        if not root.is_dir():
            return ""
        try:
            for path in root.rglob("*.exe"):
                try:
                    depth = len(path.relative_to(root).parts)
                except ValueError:
                    continue
                if depth > 5:
                    continue
                try:
                    rows.append((str(path), path.stat().st_size))
                except OSError:
                    continue
        except OSError:
            return ""
    for path, size in rows:
        leaf = os.path.basename(path.replace("\\", "/"))
        if is_blacklisted_exe(leaf) or is_launcher_infrastructure(path):
            continue
        candidates.append((size, path))
    if not candidates:
        return ""
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][1]


def infer_launcher_from_path(path: str) -> str:
    """Best-guess launcher for a path found by a filesystem or shortcut scan."""
    low = str(path or "").lower().replace("/", "\\")
    if "steamapps" in low or "\\steam\\" in low:
        return "steam"
    if "xboxgames" in low or "windowsapps" in low:
        return "xbox"
    if "ea games" in low or "origin games" in low or "electronic arts" in low:
        return "ea"
    if "epic games" in low:
        return "epic"
    if "ubisoft" in low:
        return "ubisoft"
    if "gog galaxy" in low or "gog games" in low:
        return "gog"
    if "battle.net" in low or "blizzard" in low:
        return "battlenet"
    return "other"


# --------------------------------------------------------------------------
# Xbox / Microsoft Store
# --------------------------------------------------------------------------

XBOX_MANUAL_REASON = (
    "Launched through the official shell:AppsFolder route. The game process "
    "could not be identified, so completion is verified manually."
)
XBOX_AUTO_REASON = (
    "Launched through the official shell:AppsFolder route; the game process "
    "was resolved from the package manifest, so the launch is verified "
    "automatically."
)
XBOX_DIR_REASON = (
    "Launched through the official shell:AppsFolder route; the game process "
    "was resolved from its C:\\XboxGames install folder, so the launch is "
    "verified automatically."
)


def get_start_apps() -> List[Tuple[str, str]]:
    """``(name, AppUserModelID)`` pairs from Windows application registration.

    These AppIDs are the OFFICIAL launch targets usable via
    ``shell:AppsFolder\\<AppID>``. Read-only.
    """
    out = _powershell(
        "Get-StartApps | ForEach-Object { Write-Output ($_.Name + '|' + $_.AppID) }",
        timeout=60,
    )
    return parse_start_apps(out)


def parse_start_apps(output: str) -> List[Tuple[str, str]]:
    apps = []
    for line in (output or "").splitlines():
        if "|" not in line:
            continue
        name, app_id = line.rsplit("|", 1)
        name, app_id = name.strip(), app_id.strip()
        if name and app_id:
            apps.append((name, app_id))
    return apps


def store_package_families() -> List[str]:
    """PackageFamilyNames of Store-signed packages (read-only)."""
    out = _powershell(
        "Get-AppxPackage | Where-Object { $_.SignatureKind -eq 'Store' -and "
        "-not $_.IsFramework -and -not $_.IsResourcePackage } | "
        "Select-Object -ExpandProperty PackageFamilyName",
        timeout=90,
    )
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def store_executables(pfns: Sequence[str]) -> Dict[str, str]:
    """PackageFamilyName -> game executable leaf name, from the package manifest.

    Uses ``Get-AppxPackageManifest``, which reads the manifest the package
    already publishes. No package file is opened, modified or bypassed.
    Restored from the older build — its removal is why Xbox entries in the
    supplied library all carry ``expected_process: ''``.
    """
    if not pfns:
        return {}
    chunks = []
    for pfn in pfns:
        p = str(pfn).replace("'", "''")
        chunks.append(
            f"$pkg = Get-AppxPackage | Where-Object {{ $_.PackageFamilyName -eq '{p}' }} "
            f"| Select-Object -First 1; if ($pkg) {{ try {{ "
            f"$m = Get-AppxPackageManifest -Package $pkg.PackageFullName; "
            f"$exe = $m.Package.Applications.Application.Executable; "
            f"if ($exe -is [array]) {{ $exe = $exe[0] }}; "
            f"if ($exe) {{ Write-Output ('{p}|' + $exe) }} }} catch {{ }} }}"
        )
    return parse_store_executables(_powershell("; ".join(chunks), timeout=180))


def parse_store_executables(output: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in (output or "").splitlines():
        if "|" not in line:
            continue
        pfn, exe = line.rsplit("|", 1)
        exe = exe.strip().replace("/", "\\").split("\\")[-1]
        if not exe.lower().endswith(".exe"):
            continue
        result[pfn.strip()] = exe
    return result


def make_xbox_entry(name: str, app_id: str, expected_process: str = "",
                    package_family_name: str = "", install_dir: str = "",
                    reason: str = "") -> GameEntry:
    """Build an Xbox entry.

    Launch is always the official ``shell:AppsFolder\\<AppID>`` route. What
    varies is *verification*: automatic when the game process is known,
    honestly manual when it is not. Launch and verification are separate
    capabilities — that separation is the Xbox fix.
    """
    from .library import VERIFY_AUTO, VERIFY_MANUAL

    target = f"shell:AppsFolder\\{app_id}"
    automatic = bool(expected_process)
    return GameEntry(
        name=name,
        launcher="xbox",
        source="xbox",
        exe_path="",
        launch_method="shell",
        launch_target=target,
        expected_process=expected_process or "",
        app_user_model_id=app_id,
        package_family_name=package_family_name,
        install_dir=install_dir,
        mode="standard" if automatic else "manual",
        verification=VERIFY_AUTO if automatic else VERIFY_MANUAL,
        origin="auto",
        notes=reason or (XBOX_AUTO_REASON if automatic else XBOX_MANUAL_REASON),
    )


def scan_xbox_games_folder(root: str = r"C:\XboxGames", *, lister=None) -> Dict[str, dict]:
    """Map identity key -> ``{name, install_dir, exe_path}`` for ``C:\\XboxGames``.

    This folder is ordinary readable filesystem, not the protected
    ``WindowsApps`` store. Supplying the executable from here is what lets an
    Xbox game be launched via ``shell:AppsFolder`` *and* verified by process.
    """
    found: Dict[str, dict] = {}
    if lister is not None:
        rows = list(lister(root))
    else:
        base = Path(root)
        if not base.is_dir():
            return {}
        rows = []
        try:
            for child in base.iterdir():
                if child.is_dir():
                    rows.append((child.name, str(child)))
        except OSError:
            return {}
    for title, path in rows:
        content = str(as_path(path) / "Content")
        exe = largest_game_exe(content) or largest_game_exe(path)
        if not exe:
            continue
        found[identity_key(title)] = {
            "name": title,
            "install_dir": str(as_path(exe).parent),
            "exe_path": exe,
            "expected_process": os.path.basename(exe.replace("\\", "/")),
        }
    return found


def discover_xbox(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    """Discover Store / Xbox / Game Pass games.

    Cross-references Windows application registration (``Get-StartApps``) with
    Store-signed packages, resolves the real game executable from each package
    manifest, and falls back to the ``C:\\XboxGames`` install folder. Read-only;
    nothing is bypassed.
    """
    if not IS_WINDOWS:
        return []
    apps = get_start_apps()
    if not apps:
        return []
    families = set(store_package_families())
    wanted = {}
    for name, app_id in apps:
        if cancel_event is not None and cancel_event.is_set():
            return []
        if "!" not in app_id or not xbox_app_is_game(name, app_id):
            continue
        pfn = app_id.split("!", 1)[0]
        if families and pfn not in families:
            continue
        wanted[app_id] = (name, pfn)
    manifest_exes = store_executables(sorted({pfn for _, pfn in wanted.values()}))
    folder = scan_xbox_games_folder()

    entries: List[GameEntry] = []
    for app_id, (name, pfn) in sorted(wanted.items()):
        exe_name = manifest_exes.get(pfn, "")
        install_dir = ""
        reason = XBOX_AUTO_REASON if exe_name else ""
        hit = folder.get(identity_key(name))
        if hit:
            install_dir = hit["install_dir"]
            if not exe_name:
                exe_name = hit["expected_process"]
                reason = XBOX_DIR_REASON
        entries.append(make_xbox_entry(name, app_id, exe_name, pfn, install_dir, reason))
        if progress:
            progress(f"Xbox: {name}")
    return entries


# --------------------------------------------------------------------------
# merging complementary entries
# --------------------------------------------------------------------------

#: Fields a launchable entry may adopt from a verifiable duplicate.
_MERGE_FIELDS = ("expected_process", "install_dir", "exe_path", "working_dir",
                 "package_family_name", "app_user_model_id")


def _launch_quality(entry: GameEntry) -> int:
    """How good is this entry's launch route? Higher wins."""
    if entry.launcher == "xbox":
        return 3 if entry.launch_target.lower().startswith("shell:appsfolder") else 1
    if entry.launch_method == "uri":
        return 3
    if entry.launch_method == "exe":
        return 2
    return 1


def merge_entries(entries: Sequence[GameEntry]) -> List[GameEntry]:
    """Join entries that are the same game discovered twice.

    The supplied library contains, for several games, one entry that can be
    launched but not verified and another that can be verified but not
    launched — for example ``Clair Obscur: Expedition 33``
    (``shell:AppsFolder\\…``, no process name) alongside
    ``Clair Obscur- Expedition 33`` (``C:\\XboxGames\\…\\SandFall-WinGDK-Shipping.exe``).
    Merging keeps the better launch route and adopts the missing verification
    data. Entries are only merged when they share a launcher *and* a
    conservative identity key, so distinct games are never combined.
    """
    from .library import VERIFY_AUTO

    buckets: Dict[Tuple[str, str], List[GameEntry]] = {}
    order: List[Tuple[str, str]] = []
    for entry in entries:
        key = (entry.launcher, entry.identity)
        if not entry.identity:
            key = (entry.launcher, f"\x00{id(entry)}")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(entry)

    merged: List[GameEntry] = []
    for key in order:
        group = buckets[key]
        if len(group) == 1:
            merged.append(group[0])
            continue
        group = sorted(group, key=lambda e: (-_launch_quality(e), e.name))
        primary = group[0]
        donors = group[1:]
        adopted: List[str] = []
        for donor in donors:
            for fieldname in _MERGE_FIELDS:
                if getattr(primary, fieldname, ""):
                    continue
                value = getattr(donor, fieldname, "")
                if value:
                    setattr(primary, fieldname, value)
                    adopted.append(fieldname)
            for alt in donor.process_names():
                known = {n.lower() for n in primary.process_names()}
                if alt.lower() not in known:
                    primary.alt_processes.append(alt)
        if primary.expected_process and primary.launcher == "xbox":
            primary.verification = VERIFY_AUTO
            if primary.mode == "manual":
                primary.mode = "standard"
            primary.notes = XBOX_DIR_REASON
        if adopted:
            primary.notes = (primary.notes + " ").strip() + (
                f" Merged with a duplicate entry to recover: "
                f"{', '.join(sorted(set(adopted)))}."
            )
        merged.append(primary)
    return merged


# --------------------------------------------------------------------------
# Steam
# --------------------------------------------------------------------------

def _steam_install_path() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
        value = _read_registry_value(root, key, "SteamPath") or \
                _read_registry_value(root, key, "InstallPath")
        if value:
            return value.replace("/", "\\")
    return ""


def parse_vdf_paths(text: str) -> List[str]:
    """Library folders from ``libraryfolders.vdf`` (read-only parse)."""
    return [m.replace("\\\\", "\\") for m in
            re.findall(r'"path"\s+"([^"]+)"', text or "")]


def parse_acf(text: str) -> dict:
    """The fields TravelReady needs from a Steam ``appmanifest_*.acf``."""
    out = {}
    for key in ("appid", "name", "installdir"):
        m = re.search(r'"%s"\s+"([^"]*)"' % key, text or "", re.IGNORECASE)
        if m:
            out[key] = m.group(1)
    return out


def discover_steam(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    root = _steam_install_path()
    if not root:
        return []
    libraries = [str(as_path(root) / "steamapps")]
    vdf = as_path(root) / "steamapps" / "libraryfolders.vdf"
    try:
        if Path(str(vdf)).exists():
            for p in parse_vdf_paths(Path(str(vdf)).read_text(encoding="utf-8", errors="ignore")):
                libraries.append(str(as_path(p) / "steamapps"))
    except OSError:
        pass

    entries: List[GameEntry] = []
    seen = set()
    for lib in libraries:
        lib_path = Path(lib)
        if not lib_path.is_dir():
            continue
        try:
            manifests = sorted(lib_path.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for manifest in manifests:
            if cancel_event is not None and cancel_event.is_set():
                return entries
            try:
                data = parse_acf(manifest.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            appid, name, installdir = data.get("appid"), data.get("name"), data.get("installdir")
            if not appid or not name or appid in seen or is_blacklisted_name(name):
                continue
            seen.add(appid)
            game_dir = str(as_path(lib) / "common" / (installdir or name))
            exe = largest_game_exe(game_dir)
            entries.append(GameEntry(
                name=name, launcher="steam", source="steam",
                exe_path=exe, working_dir=game_dir, install_dir=game_dir,
                launch_method="uri", launch_target=f"steam://rungameid/{appid}",
                expected_process=os.path.basename(exe.replace("\\", "/")) if exe else "",
                notes=f"Steam AppID {appid}", origin="auto",
            ))
            if progress:
                progress(f"Steam: {name}")
    return entries


# --------------------------------------------------------------------------
# EA
# --------------------------------------------------------------------------

EA_REGISTRY_ROOTS = (
    r"SOFTWARE\WOW6432Node\Electronic Arts\EA Games",
    r"SOFTWARE\Electronic Arts\EA Games",
    r"SOFTWARE\WOW6432Node\Origin Games",
    r"SOFTWARE\Origin Games",
)


def ea_install_dirs() -> Dict[str, str]:
    """``title -> install directory`` from the EA / Origin registry (read-only).

    The supplied library shows every EA entry with an empty ``exe_path``, which
    is why the detector had nothing to watch for. Resolving the install folder
    here is what gives those entries a detection signal.
    """
    if not IS_WINDOWS:
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    out: Dict[str, str] = {}
    for base in EA_REGISTRY_ROOTS:
        for sub in _iter_registry_subkeys(winreg.HKEY_LOCAL_MACHINE, base):
            key = base + "\\" + sub
            location = (_read_registry_value(winreg.HKEY_LOCAL_MACHINE, key, "Install Dir")
                        or _read_registry_value(winreg.HKEY_LOCAL_MACHINE, key, "InstallLocation")
                        or _read_registry_value(winreg.HKEY_LOCAL_MACHINE, key, "InstallDir"))
            title = _read_registry_value(winreg.HKEY_LOCAL_MACHINE, key, "DisplayName") or sub
            if location and not is_launcher_infrastructure(location):
                out[title] = location.rstrip("\\")
    return out


def discover_ea(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    """Discover EA games, resolving each title's install folder and executable.

    EA infrastructure (``EADesktop.exe``, ``EABackgroundService.exe`` and the
    EA Desktop / Origin folders) is excluded, so the EA App can never be
    catalogued as a game — which the supplied library shows happening.
    """
    entries: List[GameEntry] = []
    for title, install_dir in sorted(ea_install_dirs().items()):
        if cancel_event is not None and cancel_event.is_set():
            return entries
        if is_blacklisted_name(title) or is_launcher_infrastructure(install_dir):
            continue
        exe = largest_game_exe(install_dir)
        entries.append(GameEntry(
            name=title, launcher="ea", source="ea",
            exe_path=exe, install_dir=install_dir, working_dir=install_dir,
            expected_process=os.path.basename(exe.replace("\\", "/")) if exe else "",
            launch_method="exe" if exe else "",
            notes="EA / Origin registry entry.", origin="auto",
        ))
        if progress:
            progress(f"EA: {title}")
    return entries


# --------------------------------------------------------------------------
# Epic / GOG / Ubisoft / Battle.net
# --------------------------------------------------------------------------

def discover_epic(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    manifest_dir = Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
    if not manifest_dir.is_dir():
        return []
    import json as _json
    entries: List[GameEntry] = []
    try:
        files = sorted(manifest_dir.glob("*.item"))
    except OSError:
        return []
    for item in files:
        if cancel_event is not None and cancel_event.is_set():
            return entries
        try:
            data = _json.loads(item.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        name = data.get("DisplayName") or ""
        install_dir = (data.get("InstallLocation") or "").rstrip("\\")
        app_name = data.get("AppName") or ""
        exe = data.get("LaunchExecutable") or ""
        if not name or is_blacklisted_name(name) or not app_name:
            continue
        full_exe = str(as_path(install_dir) / exe) if install_dir and exe else ""
        entries.append(GameEntry(
            name=name, launcher="epic", source="epic",
            exe_path=full_exe, install_dir=install_dir, working_dir=install_dir,
            expected_process=os.path.basename(exe.replace("/", "\\")) if exe else "",
            launch_method="uri",
            launch_target=f"com.epicgames.launcher://apps/{app_name}?action=launch&silent=true",
            notes="Epic Games Launcher manifest.", origin="auto",
        ))
        if progress:
            progress(f"Epic: {name}")
    return entries


def _uninstall_entries(match: Callable[[str, str], bool]) -> List[Tuple[str, str]]:
    """``(name, install_location)`` from the Windows uninstall registry."""
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    out = []
    roots = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"))
    for root, base in roots:
        for sub in _iter_registry_subkeys(root, base):
            key = base + "\\" + sub
            name = _read_registry_value(root, key, "DisplayName")
            location = _read_registry_value(root, key, "InstallLocation")
            publisher = _read_registry_value(root, key, "Publisher")
            if not name or not location:
                continue
            if match(f"{sub} {publisher} {location}".lower(), name):
                out.append((name, location.rstrip("\\")))
    return out


def _registry_launcher_discoverer(launcher: str, token: str, note: str):
    def _discover(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
        entries: List[GameEntry] = []
        for name, install_dir in _uninstall_entries(lambda hay, n: token in hay):
            if cancel_event is not None and cancel_event.is_set():
                return entries
            if is_blacklisted_name(name) or is_launcher_infrastructure(install_dir):
                continue
            exe = largest_game_exe(install_dir)
            entries.append(GameEntry(
                name=name, launcher=launcher, source=launcher,
                exe_path=exe, install_dir=install_dir, working_dir=install_dir,
                expected_process=os.path.basename(exe.replace("\\", "/")) if exe else "",
                launch_method="exe" if exe else "",
                notes=note, origin="auto",
            ))
            if progress:
                progress(f"{launcher}: {name}")
        return entries
    return _discover


discover_ubisoft = _registry_launcher_discoverer("ubisoft", "ubisoft", "Ubisoft Connect entry.")
discover_gog = _registry_launcher_discoverer("gog", "gog", "GOG Galaxy entry.")
discover_battlenet = _registry_launcher_discoverer("battlenet", "battle.net", "Battle.net entry.")


# --------------------------------------------------------------------------
# filesystem / shortcuts
# --------------------------------------------------------------------------

def resolve_shortcut(path: str) -> str:
    """Target of a ``.lnk``, read via the Shell COM API (read-only)."""
    if not IS_WINDOWS:
        return ""
    out = _powershell(
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{str(path).replace(chr(39), chr(39) * 2)}'); "
        "Write-Output $l.TargetPath", timeout=20)
    return (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""


def discover_shortcuts(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path.home() / "Desktop",
    ]
    entries: List[GameEntry] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            links = sorted(root.rglob("*.lnk"))
        except OSError:
            continue
        for link in links:
            if cancel_event is not None and cancel_event.is_set():
                return entries
            name = link.stem
            if is_blacklisted_name(name) or is_blacklisted_exe(name):
                continue
            target = resolve_shortcut(str(link))
            if not target or is_launcher_infrastructure(target) or is_blacklisted_exe(target):
                continue
            install_dir = str(as_path(target).parent)
            entries.append(GameEntry(
                name=name, launcher=infer_launcher_from_path(target),
                source="shortcut", exe_path=target,
                install_dir=install_dir, working_dir=install_dir,
                expected_process=os.path.basename(target.replace("\\", "/")),
                launch_method="shortcut", launch_target=str(link),
                notes="Start menu / desktop shortcut.", origin="auto",
            ))
            if progress:
                progress(f"Shortcut: {name}")
    return entries


def scan_folder(folder: str, cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    """Treat each immediate subdirectory of ``folder`` as a candidate game."""
    base = Path(folder)
    if not base.is_dir():
        return []
    entries: List[GameEntry] = []
    try:
        children = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    for child in children:
        if cancel_event is not None and cancel_event.is_set():
            return entries
        if is_blacklisted_name(child.name) or is_launcher_infrastructure(str(child)):
            continue
        content = child / "Content"
        exe = largest_game_exe(str(content)) if content.is_dir() else ""
        exe = exe or largest_game_exe(str(child))
        if not exe:
            continue
        install_dir = str(as_path(exe).parent)
        entries.append(GameEntry(
            name=child.name, launcher=infer_launcher_from_path(str(child)),
            source="folder", exe_path=exe,
            install_dir=install_dir, working_dir=install_dir,
            expected_process=os.path.basename(exe.replace("\\", "/")),
            launch_method="exe", notes=f"Found by folder scan of {folder}.",
            origin="auto",
        ))
        if progress:
            progress(f"Folder: {child.name}")
    return entries


def discover_common_dirs(cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    entries: List[GameEntry] = []
    for folder in _COMMON_DIRS:
        if "windowsapps" in folder.lower():
            continue     # never enumerated: protected store location
        entries.extend(scan_folder(folder, cancel_event, progress))
    return entries


def discover_custom_folders(folders: Sequence[str], cancel_event=None,
                            progress: Progress = None) -> List[GameEntry]:
    entries: List[GameEntry] = []
    for folder in folders or []:
        entries.extend(scan_folder(folder, cancel_event, progress))
    return entries


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

DISCOVERERS: Dict[str, Callable] = {
    "steam": discover_steam,
    "epic": discover_epic,
    "ea": discover_ea,
    "ubisoft": discover_ubisoft,
    "xbox": discover_xbox,
    "gog": discover_gog,
    "battlenet": discover_battlenet,
    "shortcuts": discover_shortcuts,
    "common": discover_common_dirs,
}

AUTO_SCAN_SOURCES = tuple(DISCOVERERS)


def discover(source: str, cancel_event=None, progress: Progress = None) -> List[GameEntry]:
    fn = DISCOVERERS.get(source)
    return fn(cancel_event, progress) if fn else []


def auto_scan(sources: Optional[Sequence[str]] = None, cancel_event=None,
              progress: Progress = None,
              custom_folders: Sequence[str] = ()) -> List[GameEntry]:
    """Run every discoverer, then merge complementary duplicates.

    Merging is the step that gives Xbox entries a verifiable process and EA URI
    entries an install folder — see :func:`merge_entries`.
    """
    found: List[GameEntry] = []
    for source in (sources or AUTO_SCAN_SOURCES):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress:
            progress(f"Scanning {source}…")
        found.extend(discover(source, cancel_event, progress))
    if custom_folders:
        found.extend(discover_custom_folders(custom_folders, cancel_event, progress))
    return merge_entries(found)
