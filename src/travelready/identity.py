"""identity.py — one game, several installations.

The library stores a flat list of :class:`~travelready.library.GameEntry`, and
each entry conflates things that are not the same:

* the **game** — *Clair Obscur: Expedition 33*
* the **installation** — the Xbox copy in ``C:\\XboxGames``
* the **launch target** — ``shell:AppsFolder\\…``
* the **verification target** — ``SandFall-WinGDK-Shipping.exe``

That conflation has consequences. The supplied library holds the same game
twice — once launchable, once verifiable — and, before this, one game owned by
two launchers meant two unrelated rows looking up two unrelated
recommendations. A ROG Ally Life profile is about the *game*, not about which
store it came from.

So this module groups the flat list into :class:`GameIdentity` objects, each
owning one or more :class:`Installation` records. It is a **view**: the
``GameEntry`` list remains the stored form, nothing is rewritten on disk, and
every existing code path keeps working. What changes is that code which cares
about the game (recommendations, coverage, reporting) can ask about the game,
while code which cares about a copy on disk (launching, verifying) still asks
about the installation.

Launch capability and verification capability stay separate throughout — the
distinction the Xbox regression collapsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .library import GameEntry, effective_launch_method, identity_key
from .textnorm import fold


@dataclass
class Installation:
    """One copy of a game, from one launcher.

    Wraps the stored :class:`GameEntry` rather than replacing it, so nothing is
    lost and nothing needs migrating.
    """

    entry: GameEntry

    # -- identity of the copy, not of the game ----------------------------

    @property
    def launcher(self) -> str:
        return self.entry.launcher

    @property
    def install_dir(self) -> str:
        return self.entry.install_dir or self.entry.working_dir

    @property
    def executable(self) -> str:
        return self.entry.exe_path

    @property
    def launch_method(self) -> str:
        return effective_launch_method(self.entry)

    @property
    def launch_target(self) -> str:
        """What the OS is handed to start this copy."""
        return self.entry.launch_target or self.entry.exe_path

    @property
    def verification_targets(self) -> List[str]:
        """The process names that mean *this copy is running*.

        Deliberately distinct from :attr:`launch_target`: an Xbox game is
        launched through ``shell:AppsFolder\\…`` and verified by its
        executable. Collapsing the two is what made Xbox unverifiable.
        """
        return self.entry.process_names()

    @property
    def adapter(self):
        from .launchers import adapter_for

        return adapter_for(self.launcher)

    def verification_signal(self):
        return self.adapter.verification_signal(self.entry)

    @property
    def can_verify(self) -> bool:
        return self.verification_signal().usable

    def __repr__(self) -> str:                         # pragma: no cover
        return f"<Installation {self.entry.name!r} [{self.launcher}]>"


@dataclass
class GameIdentity:
    """One game, however many launchers it is installed from."""

    key: str
    canonical_title: str
    installations: List[Installation] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)

    # -- convenience -------------------------------------------------------

    @property
    def launchers(self) -> List[str]:
        seen, out = set(), []
        for installation in self.installations:
            if installation.launcher not in seen:
                seen.add(installation.launcher)
                out.append(installation.launcher)
        return out

    @property
    def entries(self) -> List[GameEntry]:
        return [i.entry for i in self.installations]

    @property
    def installed(self) -> bool:
        return bool(self.installations)

    def best_installation(self) -> Optional[Installation]:
        """The copy TravelReady should prefer.

        Verifiability first — a copy TravelReady can confirm started is worth
        more than one it can only launch — then a real launcher over a
        filesystem find, then a stable order so the choice is reproducible.
        """
        if not self.installations:
            return None
        order = {"steam": 0, "xbox": 1, "epic": 2, "ea": 3, "ubisoft": 4,
                 "gog": 5, "battlenet": 6, "other": 9}
        return sorted(
            self.installations,
            key=lambda i: (0 if i.can_verify else 1,
                           order.get(i.launcher, 8),
                           i.entry.name.lower()),
        )[0]

    def installation_for(self, launcher: str) -> Optional[Installation]:
        for installation in self.installations:
            if installation.launcher == launcher:
                return installation
        return None

    def add_alias(self, title: str) -> None:
        """Record another spelling this game is known by.

        Deduplicated on the *raw* string, not the folded one: ``Clair Obscur-
        Expedition 33`` and ``Clair Obscur: Expedition 33`` fold to the same
        key, which is why they group — but they are different strings a user
        might type or see, so both are kept for lookup and display.
        """
        text = str(title or "").strip()
        if not text or text.lower() == self.canonical_title.strip().lower():
            return
        if text.lower() in {a.strip().lower() for a in self.aliases}:
            return
        self.aliases.append(text)

    def __repr__(self) -> str:                         # pragma: no cover
        return (f"<GameIdentity {self.canonical_title!r} "
                f"x{len(self.installations)} {self.launchers}>")


# --------------------------------------------------------------------------
# building the view
# --------------------------------------------------------------------------

def _canonical_title(entries: Sequence[GameEntry]) -> str:
    """The most presentable title among several spellings of one game.

    Prefers the one with punctuation — ``Clair Obscur: Expedition 33`` over the
    Xbox folder's ``Clair Obscur- Expedition 33`` — then the longest, because a
    truncated slug is never the better name.
    """
    def score(entry: GameEntry) -> tuple:
        name = entry.name
        return (-(":" in name), -len(name), name.lower())

    return sorted(entries, key=score)[0].name


def build_identities(entries: Sequence[GameEntry],
                     *, include_infrastructure: bool = False) -> List[GameIdentity]:
    """Group a flat library into per-game identities.

    Grouping uses :func:`travelready.library.identity_key`, the conservative
    normaliser — the same rule that decides whether two entries are the same
    game for merging. It must not over-reach: joining two different games would
    make one of them silently inherit the other's recommendation.
    """
    from .classification import classify_entry

    excluded = set()
    if not include_infrastructure:
        excluded = {id(e) for e in entries if not classify_entry(e).is_game}

    grouped: Dict[str, List[GameEntry]] = {}
    order: List[str] = []
    for entry in entries:
        if id(entry) in excluded:
            continue
        key = entry.identity or fold(entry.name).replace(" ", "")
        if not key:
            key = f"\x00{id(entry)}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(entry)

    identities: List[GameIdentity] = []
    for key in order:
        group = grouped[key]
        canonical = _canonical_title(group)
        identity = GameIdentity(
            key=key,
            canonical_title=canonical,
            installations=[Installation(entry=e) for e in group],
        )
        for entry in group:
            identity.add_alias(entry.name)
        identities.append(identity)
    return identities


def index_by_key(identities: Sequence[GameIdentity]) -> Dict[str, GameIdentity]:
    return {identity.key: identity for identity in identities}


def find_identity(identities: Sequence[GameIdentity], name: str) -> Optional[GameIdentity]:
    """The identity matching ``name`` exactly or by identity key."""
    target = str(name or "").strip().lower()
    for identity in identities:
        if identity.canonical_title.strip().lower() == target:
            return identity
    key = identity_key(name)
    for identity in identities:
        if identity.key == key:
            return identity
    for identity in identities:
        if any(a.strip().lower() == target for a in identity.aliases):
            return identity
    return None


def summarise(identities: Sequence[GameIdentity]) -> dict:
    """Counts for the dashboard: games, installations, multi-launcher games."""
    multi = [i for i in identities if len(i.launchers) > 1]
    per_launcher: Dict[str, int] = {}
    for identity in identities:
        for launcher in identity.launchers:
            per_launcher[launcher] = per_launcher.get(launcher, 0) + 1
    return {
        "games": len(identities),
        "installations": sum(len(i.installations) for i in identities),
        "multi_launcher": len(multi),
        "verifiable": sum(1 for i in identities
                          if i.best_installation() and i.best_installation().can_verify),
        "per_launcher": dict(sorted(per_launcher.items())),
    }
