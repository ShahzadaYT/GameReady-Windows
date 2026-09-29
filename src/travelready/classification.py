"""classification.py — is this entry a game, and why?

Discovery finds things. Not all of them are games: a scan turns up launchers,
Windows apps, redistributables, runtimes and utilities, and counting those as
games makes every number the application reports wrong.

The previous filter was a set of substring tests returning a bare boolean. That
failed on real data in both directions:

* **Ubisoft installs games into** ``C:\\Program Files (x86)\\Ubisoft\\Ubisoft
  Game Launcher\\games\\<Game>\\``. The substring ``launcher`` appears in the
  path of every legitimate Ubisoft game, so *The Crew 2*, *Assassin's Creed
  Origins*, *Assassin's Creed Odyssey*, *For Honor*, *Star Wars Outlaws* and
  *AFOP* were all classified as launcher software.
* ``Cyberpunk 2077`` launches through ``REDprelauncher.exe`` — the word
  "launcher" in an executable name does not make the entry a launcher.
* ``Assassin's Creed Origins`` contains the substring ``origin``, the EA
  launcher.

So classification is now ordered rules over path *components*, each recording
the rule that fired. A positive game signal — sitting in a launcher's ``games``
folder, or in ``steamapps\\common``, or in ``C:\\XboxGames`` — outranks the
launcher-directory rule, because that is exactly where games live.

Every classification carries its reason, so a user can see why something was
excluded instead of wondering where their game went.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .textnorm import fold, leaf, path_components

GAME = "GAME"
APPLICATION = "APPLICATION"
LAUNCHER = "LAUNCHER"
UTILITY = "UTILITY"
SYSTEM_COMPONENT = "SYSTEM_COMPONENT"
UNKNOWN = "UNKNOWN"

CATEGORIES = (GAME, APPLICATION, LAUNCHER, UTILITY, SYSTEM_COMPONENT, UNKNOWN)

#: Everything that is not a GAME is excluded from the library's game counts.
NON_GAME = (APPLICATION, LAUNCHER, UTILITY, SYSTEM_COMPONENT)

#: Path components that positively mean "a game lives here". Checked first,
#: because they sit *inside* launcher directories.
GAME_DIR_COMPONENTS = frozenset({
    "games", "steamapps", "common", "xboxgames", "gog games", "ea games",
    "origin games", "epic games", "wow_retail_", "call of duty",
})

#: Directory components belonging to a launcher's own program files.
LAUNCHER_DIR_COMPONENTS = frozenset({
    "ea desktop", "eaappinstaller", "ea app", "origin", "epic games launcher",
    "ubisoft game launcher", "gog galaxy", "battle.net", "steam",
    "rockstar games launcher", "electronic arts",
})

#: Names that are the launcher itself.
LAUNCHER_NAMES = re.compile(
    r"^(steam|steamvr|epic games launcher|epic online services|ea|ea app|"
    r"ea desktop|ea games|origin|electronic arts|ubisoft connect|"
    r"ubisoft game launcher|uplay|gog galaxy|battle\.net|blizzard|"
    r"rockstar games launcher|xbox|game bar|game pass|microsoft store)$",
    re.IGNORECASE)

#: Windows and vendor software that is never a game.
SYSTEM_NAMES = re.compile(
    r"^(calculator|notepad|paint|photos|settings|terminal|weather|clock|"
    r"media player|movies & tv|windows security|windows backup|windows back up|"
    r"snipping tool|sticky notes|quick assist|get started|copilot|recall.*|"
    r"click to do|cortana|maps|camera|voice recorder|sound recorder|"
    r"phone link|your phone|people|mail|calendar|to do|onedrive|edge|"
    r"microsoft edge|office|outlook|teams|xbox game bar)$",
    re.IGNORECASE)

VENDOR_NAMES = re.compile(
    r"^(amd software.*|amd radeon.*|nvidia.*|geforce.*|realtek.*|intel.*|"
    r"armoury crate.*|myasus|asus.*|dolby.*|dts.*|logitech.*|razer.*|"
    r"corsair.*|steelseries.*|dotnet|\.net.*|microsoft visual c\+\+.*|"
    r"directx.*|vulkan.*|java.*|python.*|node.*)$",
    re.IGNORECASE)

UTILITY_NAMES = re.compile(
    r"^(7-?zip|winrar|vlc|discord|spotify|obs studio|bitwarden|notepad\+\+|"
    r"git|visual studio.*|vscode|chrome|firefox|brave|opera|zoom|slack)$",
    re.IGNORECASE)

#: Executable names that are tooling rather than a game binary.
TOOLING_EXE = re.compile(
    r"(unins|uninstall|redist|vcredist|vc_redist|dxsetup|directx|dotnet|"
    r"oalinst|physx|ue[45]prereq|prereq|setup|install(er)?|crashreport|"
    r"crashhandler|updater|patcher|benchmark|dxwebsetup|touchup|"
    r"activation|cleanup|diag)",
    re.IGNORECASE)

#: Path components that are support directories, not the game itself.
SUPPORT_DIR_COMPONENTS = frozenset({
    "redist", "_commonredist", "redistributables", "directx", "vcredist",
    "__installer", "installers", "support", "prereq", "prerequisites",
    "dotnet", "_redist",
})


@dataclass(frozen=True)
class Classification:
    """What something is, and the rule that decided it."""

    category: str
    reason: str
    rule: str = ""
    #: Set when the entry is a game but something about it is wrong — most
    #: often that discovery stored the wrong executable. A bad executable
    #: makes a game unverifiable; it does not stop it being a game.
    warning: str = ""

    @property
    def is_game(self) -> bool:
        return self.category == GAME

    def describe(self, name: str = "") -> str:
        head = f"{name}\n" if name else ""
        return f"{head}Classification: {self.category}\nReason:\n{self.reason}"


def _has_component(components: Sequence[str], wanted) -> Optional[str]:
    for component in components:
        if component in wanted:
            return component
    return None


def classify(name: str, *, exe_path: str = "", install_dir: str = "",
             launcher: str = "", app_id: str = "") -> Classification:
    """Classify one discovered entry. Ordered rules; first match wins."""
    title = str(name or "").strip()
    path = exe_path or install_dir or ""
    components = path_components(path)
    exe_leaf = leaf(exe_path)

    # 1. The name says it outright.
    if LAUNCHER_NAMES.match(title):
        return Classification(LAUNCHER, f"'{title}' is a game launcher, not a game.",
                              "launcher-name")
    if SYSTEM_NAMES.match(title):
        return Classification(SYSTEM_COMPONENT,
                              f"'{title}' is a Windows system application.",
                              "system-name")
    if VENDOR_NAMES.match(title):
        return Classification(APPLICATION,
                              f"'{title}' is known system or vendor software.",
                              "vendor-name")
    if UTILITY_NAMES.match(title):
        return Classification(UTILITY, f"'{title}' is a desktop utility.",
                              "utility-name")

    # 2. A positive game signal outranks everything about the directory it is
    #    inside — Ubisoft games live under 'Ubisoft Game Launcher\games\'.
    game_component = _has_component(components, GAME_DIR_COMPONENTS)

    # 3. A support subdirectory. Where it sits relative to the game folder is
    #    what distinguishes a redistributable from a game whose stored
    #    executable happens to point into its own redist folder.
    support = _has_component(components, SUPPORT_DIR_COMPONENTS)
    inside_game = (game_component is not None
                   and support is not None
                   and components.index(game_component) < components.index(support))
    if support and not inside_game:
        return Classification(
            UTILITY,
            f"the executable sits in a '{support}' support folder, so it is a "
            f"redistributable or installer rather than a game.",
            "support-directory")

    tooling = bool(exe_leaf) and bool(TOOLING_EXE.search(exe_leaf)) \
        and not _is_prelauncher(exe_leaf)

    if game_component:
        warning = ""
        if inside_game or tooling:
            warning = (f"the recorded executable '{exe_leaf}' looks like a "
                       f"redistributable or installer, not the game. Re-scan so "
                       f"TravelReady can find the real one.")
        return Classification(
            GAME,
            f"installed under a '{game_component}' folder, where games live.",
            "game-directory", warning)

    if tooling:
        return Classification(
            UTILITY,
            f"'{exe_leaf}' is an installer, updater or redistributable.",
            "tooling-executable")

    # 4. Only now does a launcher's own directory mean launcher software.
    launcher_component = _has_component(components, LAUNCHER_DIR_COMPONENTS)
    if launcher_component:
        return Classification(
            LAUNCHER,
            f"sits directly in the '{launcher_component}' program folder rather "
            f"than in its games folder.",
            "launcher-directory")

    # 5. Store apps are filtered by their package identity.
    if app_id:
        from .discovery import xbox_app_is_game

        if not xbox_app_is_game(title, app_id):
            return Classification(
                SYSTEM_COMPONENT,
                f"the Store package '{app_id.split('!')[0]}' is a system or "
                f"utility app, not a game.",
                "store-package")

    if not title:
        return Classification(UNKNOWN, "no name was recorded.", "no-name")

    return Classification(GAME, "no rule identified it as anything else.",
                          "default")


def _is_prelauncher(exe_leaf: str) -> bool:
    """A game whose own start-up binary is called a launcher is still a game.

    ``REDprelauncher.exe`` is how Cyberpunk 2077 starts.
    """
    return bool(re.search(r"(prelaunch|launcher)", exe_leaf, re.IGNORECASE)) and \
        not re.search(r"(unins|setup|install|updater|patch)", exe_leaf, re.IGNORECASE)


def classify_entry(entry) -> Classification:
    """Classify a :class:`~travelready.library.GameEntry`."""
    app_id = ""
    target = getattr(entry, "launch_target", "") or ""
    if target.lower().startswith("shell:appsfolder\\"):
        app_id = target.split("\\", 1)[1]
    return classify(
        getattr(entry, "name", ""),
        exe_path=getattr(entry, "exe_path", "") or "",
        install_dir=getattr(entry, "install_dir", "") or "",
        launcher=getattr(entry, "launcher", "") or "",
        app_id=app_id or getattr(entry, "app_user_model_id", "") or "",
    )


def split_games(entries: Sequence) -> Tuple[List, List]:
    """``(games, non_games)``, classified."""
    games, others = [], []
    for entry in entries:
        (games if classify_entry(entry).is_game else others).append(entry)
    return games, others


def classification_report(entries: Sequence) -> str:
    """Per-category counts plus every non-game with its reason."""
    rows = [(e, classify_entry(e)) for e in entries]
    counts: dict = {}
    for _, result in rows:
        counts[result.category] = counts.get(result.category, 0) + 1
    lines = ["Scan classification", "=" * 58, ""]
    for category in CATEGORIES:
        if counts.get(category):
            lines.append(f"  {category:<20}{counts[category]:>4}")
    lines += ["", "Excluded from the game count:", "-" * 58]
    for entry, result in sorted(rows, key=lambda r: (r[1].category, r[0].name.lower())):
        if result.is_game:
            continue
        lines.append(f"  {entry.name[:40]:<42}{result.category}")
        lines.append(f"      {result.reason}")
    flagged = [(e, r) for e, r in rows if r.is_game and r.warning]
    if flagged:
        lines += ["", "Games with a problem recorded:", "-" * 58]
        for entry, result in sorted(flagged, key=lambda r: r[0].name.lower()):
            lines.append(f"  {entry.name[:40]:<42}")
            lines.append(f"      {result.warning}")
    return "\n".join(lines)
