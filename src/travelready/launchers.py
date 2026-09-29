"""launchers.py — one adapter per launcher, with explicit capabilities.

The EA regression came from treating every launcher as equally verifiable. It
is not: Steam hands you a process named after the game, EA hands the request to
EA App and walks away, and an Xbox title is verifiable only if its executable
can be found. Pretending otherwise produced a 90-second wait that reported
TIMEOUT when the truth was "there was never anything to look for".

So each launcher declares what it can actually do, and every capability is one
of three states — not a boolean:

``FULL``     the adapter does this reliably
``PARTIAL``  it works when a precondition holds, and the adapter says which
``NONE``     it cannot, and the UI must not imply otherwise
``UNKNOWN``  not established yet; treated as NONE but reported differently

Adapters wrap the existing :mod:`travelready.discovery` functions rather than
replacing them. Discovery keeps doing the finding; adapters answer *what can be
done with what was found*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .environment import (
    LAUNCHER_REQUIRED, LIVE_GAME_REQUIRED, PURE_LOGIC, WINDOWS_REQUIRED,
)
from .library import GameEntry, effective_launch_method

FULL = "FULL"
PARTIAL = "PARTIAL"
NONE = "NONE"
UNKNOWN = "UNKNOWN"
LEVELS = (FULL, PARTIAL, NONE, UNKNOWN)


@dataclass(frozen=True)
class CapabilityLevel:
    """One capability at one level, with the reason it is not higher."""

    level: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.level in (FULL, PARTIAL)

    def __str__(self) -> str:
        return f"{self.level}{f' ({self.detail})' if self.detail else ''}"


def _full(detail: str = "") -> CapabilityLevel:
    return CapabilityLevel(FULL, detail)


def _partial(detail: str) -> CapabilityLevel:
    return CapabilityLevel(PARTIAL, detail)


def _none(detail: str) -> CapabilityLevel:
    return CapabilityLevel(NONE, detail)


def _unknown(detail: str) -> CapabilityLevel:
    return CapabilityLevel(UNKNOWN, detail)


@dataclass
class LauncherCapabilities:
    """What one launcher supports. Every field is deliberately explicit."""

    launch: CapabilityLevel
    detect_process: CapabilityLevel
    verify_game: CapabilityLevel
    can_close: CapabilityLevel
    is_authenticated: CapabilityLevel
    prepare_for_offline: CapabilityLevel
    get_installation: CapabilityLevel

    def as_rows(self) -> List[tuple]:
        return [
            ("launch", self.launch),
            ("process detection", self.detect_process),
            ("game verification", self.verify_game),
            ("safe close", self.can_close),
            ("authentication check", self.is_authenticated),
            ("offline preparation", self.prepare_for_offline),
            ("installation lookup", self.get_installation),
        ]


class LauncherAdapter:
    """Base adapter. Subclasses override only what differs."""

    launcher = "other"
    display_name = "Other"
    #: Processes that indicate the launcher itself is running.
    launcher_processes: tuple = ()
    #: Processes that are launcher infrastructure and are never the game.
    infrastructure_processes: tuple = ()

    # -- capabilities ------------------------------------------------------

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full(),
            detect_process=_partial("only when the game's process name or install "
                                    "folder is known"),
            verify_game=_partial("depends on process detection"),
            can_close=_partial("only what TravelReady started or identified"),
            is_authenticated=_unknown("no supported way to read sign-in state"),
            prepare_for_offline=_unknown("no supported offline-preparation step"),
            get_installation=_partial("from the filesystem scan"),
        )

    # -- behaviour ---------------------------------------------------------

    def discover(self, cancel_event=None, progress=None) -> List[GameEntry]:
        """Delegate to the existing discoverer for this launcher."""
        from . import discovery

        fn = discovery.DISCOVERERS.get(self.launcher)
        return fn(cancel_event, progress) if fn else []

    def launcher_running(self, table) -> bool:
        return any(table.pids_by_name(name) for name in self.launcher_processes)

    def launcher_installed(self, table=None) -> Optional[bool]:
        """``True``/``False``, or ``None`` when it cannot be determined here."""
        from .environment import current

        if not current(check_network=False).windows:
            return None
        return self._detect_installed()

    def _detect_installed(self) -> Optional[bool]:
        return None

    def is_authenticated(self, entry: GameEntry) -> Optional[bool]:
        """``None`` means *cannot determine*, which is not the same as ``False``.

        No adapter reads credentials, tokens or account files. Where a launcher
        exposes no supported, read-only way to tell, the honest answer is
        ``None`` and the user is asked.
        """
        return None

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        """Instructions for the user, or ``None`` when nothing is needed.

        TravelReady never performs this itself: making a launcher offline-ready
        means signing in and letting it cache entitlements, which is the user's
        account activity, not ours to automate.
        """
        return None

    def verification_signal(self, entry: GameEntry) -> CapabilityLevel:
        """Can *this specific game* be verified, given what is known about it?"""
        from .launch_tester import build_detection_plan

        plan = build_detection_plan(entry)
        if plan.process_names:
            return _full("known process name")
        if plan.install_dir:
            return _full("install folder is trackable")
        if plan.track_launched_pid:
            return _partial("the launched process can be tracked")
        return _none("no process name, install folder or trackable PID")


# --------------------------------------------------------------------------
# per-launcher adapters
# --------------------------------------------------------------------------

class SteamAdapter(LauncherAdapter):
    launcher = "steam"
    display_name = "Steam"
    launcher_processes = ("steam.exe",)
    infrastructure_processes = ("steam.exe", "steamwebhelper.exe", "steamerrorreporter.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("steam://rungameid/<id>"),
            detect_process=_full("the app manifest names the executable"),
            verify_game=_full(),
            can_close=_full(),
            is_authenticated=_partial("Steam's own offline mode is a user setting "
                                      "TravelReady reads nothing about"),
            prepare_for_offline=_partial("Steam supports offline mode once it has "
                                         "been started online at least once"),
            get_installation=_full("appmanifest_*.acf plus libraryfolders.vdf"),
        )

    def _detect_installed(self) -> Optional[bool]:
        from .discovery import _steam_install_path

        return bool(_steam_install_path())

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Start Steam online at least once so it caches your licences, "
                "then Steam > Go Offline before you travel.")


class XboxAdapter(LauncherAdapter):
    launcher = "xbox"
    display_name = "Xbox / Microsoft Store"
    launcher_processes = ("xboxpcapp.exe", "gamingservices.exe")
    infrastructure_processes = ("xboxpcapp.exe", "gamingservices.exe",
                                "gamelaunchhelper.exe", "gamebar.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("shell:AppsFolder\\<AppUserModelID>, the documented route"),
            detect_process=_partial("when the executable is resolvable from the "
                                    "package manifest or C:\\XboxGames"),
            verify_game=_partial("depends on process detection; otherwise manual"),
            can_close=_partial("only when the game process is known"),
            is_authenticated=_unknown("no supported read-only sign-in check"),
            prepare_for_offline=_partial("Game Pass titles need a licence refresh "
                                         "while online"),
            get_installation=_partial("C:\\XboxGames is readable; WindowsApps is not"),
        )

    def _detect_installed(self) -> Optional[bool]:
        from .discovery import get_start_apps

        return bool(get_start_apps())

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Launch the game once while online so Xbox refreshes its licence. "
                "Game Pass titles stop working offline once the licence lapses.")


class EAAdapter(LauncherAdapter):
    launcher = "ea"
    display_name = "EA"
    launcher_processes = ("eadesktop.exe", "ealink.exe", "origin.exe")
    infrastructure_processes = ("eadesktop.exe", "ealink.exe", "origin.exe",
                                "easteamproxy.exe", "eabackgroundservice.exe",
                                "eacrashreporter.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("link2ea://launch/<offerId>"),
            detect_process=_partial("EA App hands off and exits, so detection needs "
                                    "the game's own process name or install folder"),
            verify_game=_partial("PARTIAL by nature — see detect_process"),
            can_close=_full("only what TravelReady started or identified"),
            is_authenticated=_unknown("no supported read-only sign-in check"),
            prepare_for_offline=_partial("EA App must have been signed in online"),
            get_installation=_partial("from the EA/Origin registry keys"),
        )

    def _detect_installed(self) -> Optional[bool]:
        from .discovery import ea_install_dirs

        return bool(ea_install_dirs())

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Open EA App and sign in while online. EA App must have cached "
                "your entitlement before it will start a game offline.")


class EpicAdapter(LauncherAdapter):
    launcher = "epic"
    display_name = "Epic Games"
    launcher_processes = ("epicgameslauncher.exe",)
    infrastructure_processes = ("epicgameslauncher.exe", "epicwebhelper.exe",
                                "eoshelper.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("com.epicgames.launcher://apps/<id>?action=launch"),
            detect_process=_full("the manifest names LaunchExecutable"),
            verify_game=_full(),
            can_close=_full(),
            is_authenticated=_unknown("no supported read-only sign-in check"),
            prepare_for_offline=_partial("Epic requires a sign-in before offline use"),
            get_installation=_full("from the .item manifests"),
        )

    def _detect_installed(self) -> Optional[bool]:
        from pathlib import Path

        return Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests").is_dir()

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Sign in to the Epic launcher while online. Epic needs a recent "
                "sign-in before it will start games offline.")


class UbisoftAdapter(LauncherAdapter):
    launcher = "ubisoft"
    display_name = "Ubisoft Connect"
    launcher_processes = ("upc.exe", "ubisoftconnect.exe", "uplay.exe")
    infrastructure_processes = ("upc.exe", "ubisoftconnect.exe", "uplay.exe",
                                "uplaywebcore.exe", "ubisoftgamelauncher.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("the installed executable"),
            detect_process=_partial("from the install folder"),
            verify_game=_partial("depends on process detection"),
            can_close=_full(),
            is_authenticated=_unknown("no supported read-only sign-in check"),
            prepare_for_offline=_partial("Ubisoft Connect has an explicit offline mode"),
            get_installation=_partial("from the uninstall registry keys"),
        )

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Open Ubisoft Connect online, then switch it to offline mode "
                "before you travel.")


class GOGAdapter(LauncherAdapter):
    launcher = "gog"
    display_name = "GOG"
    launcher_processes = ("galaxyclient.exe",)
    infrastructure_processes = ("galaxyclient.exe", "galaxyclienthelper.exe",
                                "galaxycommunication.exe")

    def capabilities(self) -> LauncherCapabilities:
        return LauncherCapabilities(
            launch=_full("the installed executable"),
            detect_process=_full("GOG games are DRM-free and run directly"),
            verify_game=_full(),
            can_close=_full(),
            is_authenticated=_full("GOG games are DRM-free — no sign-in is needed "
                                   "to play offline"),
            prepare_for_offline=_full("nothing to do: GOG titles run offline"),
            get_installation=_partial("from the uninstall registry keys"),
        )

    def is_authenticated(self, entry: GameEntry) -> Optional[bool]:
        return True         # DRM-free: playing offline needs no account state


class BattleNetAdapter(LauncherAdapter):
    launcher = "battlenet"
    display_name = "Battle.net"
    launcher_processes = ("battle.net.exe", "blizzard.exe")
    infrastructure_processes = ("battle.net.exe", "blizzard.exe", "agent.exe")

    def capabilities(self) -> LauncherCapabilities:
        caps = super().capabilities()
        caps.prepare_for_offline = _none(
            "most Battle.net titles require a live connection and cannot be "
            "prepared for offline play")
        return caps

    def prepare_for_offline(self, entry: GameEntry) -> Optional[str]:
        return ("Most Battle.net games require an internet connection to play at "
                "all. Check the individual title before relying on it offline.")


ADAPTERS: Dict[str, LauncherAdapter] = {
    a.launcher: a for a in (
        SteamAdapter(), XboxAdapter(), EAAdapter(), EpicAdapter(),
        UbisoftAdapter(), GOGAdapter(), BattleNetAdapter(), LauncherAdapter(),
    )
}


def adapter_for(launcher: str) -> LauncherAdapter:
    """The adapter for ``launcher``, falling back to the generic one."""
    return ADAPTERS.get(str(launcher or "").strip().lower(), ADAPTERS["other"])


def adapter_for_entry(entry: GameEntry) -> LauncherAdapter:
    return adapter_for(entry.launcher)


def all_infrastructure_processes() -> frozenset:
    """Every launcher process that must never be adopted as a game."""
    out: set = set()
    for adapter in ADAPTERS.values():
        out.update(adapter.infrastructure_processes)
    return frozenset(out)


def capability_matrix() -> str:
    """The launcher capability table, for ``travelready doctor`` and the docs."""
    lines = [f"{'Launcher':<22}{'Launch':<10}{'Detect':<10}{'Verify':<10}"
             f"{'Close':<10}{'Auth':<10}{'Offline':<10}",
             "-" * 82]
    for key in ("steam", "xbox", "ea", "epic", "ubisoft", "gog", "battlenet"):
        adapter = ADAPTERS[key]
        caps = adapter.capabilities()
        lines.append(
            f"{adapter.display_name[:21]:<22}{caps.launch.level:<10}"
            f"{caps.detect_process.level:<10}{caps.verify_game.level:<10}"
            f"{caps.can_close.level:<10}{caps.is_authenticated.level:<10}"
            f"{caps.prepare_for_offline.level:<10}")
    return "\n".join(lines)
