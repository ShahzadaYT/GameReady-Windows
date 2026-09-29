"""bridge.py — ROG Ally Life data -> TravelReady's optimiser model.

The optimiser already knows how to compare a :class:`GameProfile` against a
game's current configuration, classify each change and apply the safe ones
transactionally. This module is the only place that converts the source's own
model into that one, so the settings engine stays independent of where a
recommendation came from.

Three things are enforced on the way across:

* **Attribution survives.** Source name, URL, retrieval time, the source's own
  last-updated date and the parser version all land on the ``GameProfile``, so
  the UI can always show where a number came from.
* **Capability gates category.** A setting TravelReady cannot write is handed
  over as a device or informational recommendation, so the existing safety
  engine will classify it MANUAL. A setting is never presented as applicable
  just because the source stated it.
* **Nothing is invented.** Only settings actually published for the selected
  profile cross over. An unrecognised setting crosses too, marked so it is
  shown and not applied — it is not silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ...library import GameEntry
from ..model import (
    CATEGORY_DEVICE, CATEGORY_GAME, TARGET_DEVICE, GameProfile, Recommendation,
)
from .cache import ProfileCache
from .capability import SCOPE_DEVICE, SCOPE_GAME, SCOPE_INFO, capability_for
from .matcher import AUTO_THRESHOLD, MatchResult, match
from .model import FAMILY_COVERS, SourceGame, SourceProfile
from .select import MODE_BALANCED, Selection, select_profile

#: The device family whose posts apply to a given TravelReady target device.
_FAMILY_FOR_DEVICE = {
    device: family
    for family, devices in FAMILY_COVERS.items()
    for device in devices
}


def family_for_device(device: str) -> str:
    """Which ROG Ally Life post family covers ``device``."""
    return _FAMILY_FOR_DEVICE.get(str(device or ""), "")


def _recommendation_for(setting, profile: SourceProfile) -> Optional[Recommendation]:
    """Convert one source setting, or ``None`` when it carries no value."""
    value = str(setting.value or "").strip()
    if not value:
        return None

    capability = capability_for(setting.canonical) if setting.canonical else None
    if capability is None:
        # Unrecognised: shown with the source's own label so the user can act
        # on it manually. CATEGORY_DEVICE keeps it out of automatic application.
        return Recommendation(
            key=setting.canonical or "unknown_setting",
            value=value,
            category=CATEGORY_DEVICE,
            note=f"'{setting.label}' — TravelReady has no validated way to change "
                 f"this setting, so apply it in the game.",
            optional=True,
        )
    if capability.scope == SCOPE_INFO:
        return Recommendation(
            key=capability.key, value=value, category=CATEGORY_DEVICE,
            note=f"'{setting.label}' — the author's note, not a setting.",
            optional=True,
        )
    if capability.scope == SCOPE_DEVICE:
        return Recommendation(
            key=capability.key, value=value, category=CATEGORY_DEVICE,
            note=capability.note or f"'{setting.label}' is a device-level setting.",
            optional=True,
        )
    if not capability.automatable:
        return Recommendation(
            key=capability.key, value=value, category=CATEGORY_DEVICE,
            note=capability.note or f"'{setting.label}' cannot be applied automatically.",
            optional=True,
        )
    return Recommendation(key=capability.key, value=value, category=CATEGORY_GAME,
                          note=setting.label)


def to_game_profile(game: SourceGame, selection: Selection,
                    target_device: str = TARGET_DEVICE) -> Optional[GameProfile]:
    """Build the optimiser's :class:`GameProfile` from a selected source profile."""
    if selection.profile is None:
        return None

    recommendations: List[Recommendation] = []
    seen: set = set()
    for setting in selection.profile.settings:
        recommendation = _recommendation_for(setting, selection.profile)
        if recommendation is None:
            continue
        marker = (recommendation.key, recommendation.category)
        if marker in seen:
            continue
        seen.add(marker)
        recommendations.append(recommendation)

    # The profile's own headline numbers are recommendations in their own right,
    # and are device-scoped so they are reported but never applied.
    if selection.profile.max_tdp and "tdp_watts" not in {r.key for r in recommendations}:
        recommendations.append(Recommendation(
            key="tdp_watts", value=str(selection.profile.max_tdp),
            category=CATEGORY_DEVICE, optional=True,
            note="Set in Armoury Crate. TravelReady does not change TDP."))
    if selection.profile.vram_gb and "vram_allocation_gb" not in {r.key for r in recommendations}:
        recommendations.append(Recommendation(
            key="vram_allocation_gb", value=str(selection.profile.vram_gb),
            category=CATEGORY_DEVICE, optional=True,
            note="Armoury Crate > Performance > GPU Settings > Memory Assigned to GPU."))

    if not recommendations:
        return None

    notes = [f"Profile: {selection.profile.label}", f"Selected because: {selection.reason}"]
    if selection.alternatives:
        notes.append(f"Other published profiles: {', '.join(selection.alternatives)}")
    if selection.profile.notes:
        notes.append(selection.profile.notes)
    if game.performance_summary:
        notes.append(game.performance_summary)

    return GameProfile(
        game_name=game.title,
        device=target_device,
        source=game.source_name,
        source_url=game.source_url,
        recommendations=recommendations,
        source_date=game.last_updated or game.published_at,
        source_version=f"parser {game.parser_version}",
        profile_notes=" | ".join(n for n in notes if n),
        device_notes=(f"Published for {game.device_family.replace('_', ' ')}; "
                      f"retrieved {game.retrieved_at}."),
        hardware_assumptions=(f"Performance rating: {game.performance_rating} "
                              f"({game.rating_word})" if game.performance_rating else ""),
        config_hint="",
    )


@dataclass
class Resolution:
    """The full result of looking one library game up in the source."""

    entry_name: str
    source_game: Optional[SourceGame] = None
    match: Optional[MatchResult] = None
    selection: Optional[Selection] = None
    profile: Optional[GameProfile] = None
    status: str = "no_profile"      # matched | review | no_profile | no_profiles_published

    @property
    def matched(self) -> bool:
        return self.status == "matched"

    @property
    def needs_review(self) -> bool:
        return self.status == "review"

    def describe(self) -> str:
        if self.match is None:
            return f"{self.entry_name}\nNO PROFILE FOUND"
        lines = [self.match.describe()]
        if self.selection is not None:
            lines.append("")
            lines.append(self.selection.describe())
        return "\n".join(lines)


class SourceResolver:
    """Looks a library game up in the cached source data.

    Read-only: it reads the cache and returns data. It never fetches (that is
    :mod:`sync`) and never writes.
    """

    def __init__(self, cache: Optional[ProfileCache] = None,
                 *, target_device: str = TARGET_DEVICE,
                 mode: str = MODE_BALANCED,
                 max_watts: Optional[int] = None) -> None:
        self.cache = cache if cache is not None else ProfileCache()
        self.target_device = target_device
        self.mode = mode
        self.max_watts = max_watts
        self._games: Optional[List[SourceGame]] = None

    @property
    def games(self) -> List[SourceGame]:
        if self._games is None:
            self._games = self.cache.all_games()
        return self._games

    def candidates(self) -> List[SourceGame]:
        """Cached posts that cover the target device."""
        return [g for g in self.games if g.covers_device(self.target_device)]

    def resolve(self, entry: GameEntry, *, mode: Optional[str] = None,
                accept_review: bool = False) -> Resolution:
        """Find the source profile for ``entry``.

        Returns a :class:`Resolution` in every case — a miss is reported as
        ``no_profile``, never filled in from somewhere else.
        """
        resolution = Resolution(entry_name=entry.name)
        pool = self.candidates()
        if not pool:
            return resolution

        found = match(entry.name, pool,
                      prefer_family=family_for_device(self.target_device))
        if found is None:
            return resolution
        resolution.match = found

        source_game = next(
            (g for g in pool
             if g.title == found.matched_title and g.source_url == found.source_url),
            None)
        if source_game is None:
            return resolution
        resolution.source_game = source_game

        if not found.automatic and not accept_review:
            resolution.status = "review" if found.needs_review else "no_profile"
            return resolution

        selection = select_profile(source_game, mode or self.mode,
                                   max_watts=self.max_watts)
        resolution.selection = selection
        if selection.profile is None:
            resolution.status = "no_profiles_published"
            return resolution

        resolution.profile = to_game_profile(source_game, selection, self.target_device)
        resolution.status = "matched" if resolution.profile else "no_profiles_published"
        return resolution

    def resolve_all(self, entries: Sequence[GameEntry],
                    **kwargs) -> List[Resolution]:
        return [self.resolve(e, **kwargs) for e in entries]

    def search(self, term: str, limit: int = 20) -> List[Tuple[float, SourceGame]]:
        """Cached games whose title is close to ``term``, best first."""
        from .matcher import score

        rows = []
        for game in self.games:
            confidence, _ = score(term, game.title)
            if confidence > 0:
                rows.append((confidence, game))
        rows.sort(key=lambda r: (-r[0], r[1].title))
        return rows[:limit]
