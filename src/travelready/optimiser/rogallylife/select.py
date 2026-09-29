"""select.py — choosing which published profile suits the user.

A game commonly has several profiles — 15/18W at 900p, 18/25/30W at 1080p —
and the highest-power one is not automatically the right one. Someone packing
for a flight wants the profile that lasts, not the one that looks best.

Selection is by **operating mode**, and it is transparent: every choice returns
the reason it was made and the alternatives it passed over, so the UI can show
why a particular profile is being proposed.

The selector only ever picks from what the source published. If a game has no
low-power profile, the answer is the nearest published one *plus a note saying
so* — never an invented profile interpolated between two real ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .model import SourceGame, SourceProfile

MODE_BATTERY = "battery"
MODE_BALANCED = "balanced"
MODE_PERFORMANCE = "performance"
OPERATING_MODES = (MODE_BATTERY, MODE_BALANCED, MODE_PERFORMANCE)

#: The wattage each mode aims at, from the site's own guidance: 7W best battery
#: life on lighter titles, 18W the balance point for most, 25-30W for demanding
#: AAA at 1080p.
MODE_TARGET_WATTS = {
    MODE_BATTERY: 13,
    MODE_BALANCED: 18,
    MODE_PERFORMANCE: 30,
}



@dataclass(frozen=True)
class Selection:
    """The chosen profile, why, and what else was available."""

    profile: Optional[SourceProfile]
    mode: str
    reason: str
    alternatives: Tuple[str, ...] = ()
    exact: bool = True

    @property
    def found(self) -> bool:
        return self.profile is not None

    def describe(self) -> str:
        if self.profile is None:
            return f"No profile available for {self.mode} mode: {self.reason}"
        lines = [f"{self.profile.label}", f"mode: {self.mode}", f"reason: {self.reason}"]
        if self.alternatives:
            lines.append(f"other profiles: {', '.join(self.alternatives)}")
        return "\n".join(lines)


def _sort_key(profile: SourceProfile) -> Tuple[int, int, str]:
    """Order profiles by wattage, then by resolution, deterministically."""
    watts = profile.min_tdp if profile.min_tdp is not None else 999
    try:
        pixels = int(str(profile.resolution).rstrip("pP") or 0)
    except ValueError:
        pixels = 0
    return (watts, pixels, profile.label)


def select_profile(game: SourceGame, mode: str = MODE_BALANCED,
                   *, max_watts: Optional[int] = None) -> Selection:
    """Pick the published profile that best suits ``mode``.

    ``max_watts`` caps the choice — useful when the user's device or their own
    Armoury Crate limit rules a profile out.
    """
    mode = str(mode or MODE_BALANCED).lower()
    if mode not in OPERATING_MODES:
        mode = MODE_BALANCED

    profiles = [p for p in (game.profiles or []) if p is not None]
    if not profiles:
        return Selection(None, mode, "the source publishes no profiles for this game")

    ordered = sorted(profiles, key=_sort_key)
    eligible = ordered
    capped = False
    nothing_within_cap = False
    if max_watts is not None:
        within = [p for p in ordered if (p.min_tdp or 0) <= max_watts]
        if within:
            eligible, capped = within, len(within) != len(ordered)
        else:
            # Nothing the source published fits the cap. Offer the lowest it
            # does publish and say plainly that it exceeds the limit, rather
            # than inventing a profile that would fit.
            eligible = ordered[:1]
            nothing_within_cap = True

    labels = tuple(p.label for p in ordered)

    def others(chosen: SourceProfile) -> Tuple[str, ...]:
        return tuple(label for label in labels if label != chosen.label)

    unrated = [p for p in eligible if p.min_tdp is None]
    rated = [p for p in eligible if p.min_tdp is not None]

    if not rated:
        chosen = eligible[0]
        return Selection(chosen, mode,
                         "the source does not state wattages, so the first published "
                         "profile is used", others(chosen), exact=False)

    if mode == MODE_BATTERY:
        chosen = rated[0]
        reason = f"lowest published wattage ({chosen.min_tdp}W)"
    elif mode == MODE_PERFORMANCE:
        chosen = rated[-1]
        reason = f"highest published wattage ({chosen.max_tdp}W)"
    else:
        target = MODE_TARGET_WATTS[mode]
        chosen = min(rated, key=lambda p: (abs((p.min_tdp or 0) - target), p.min_tdp or 0))
        distance = abs((chosen.min_tdp or 0) - target)
        reason = (f"closest published wattage to {target}W ({chosen.min_tdp}W)"
                  if distance else f"published wattage matches the {target}W target")

    if nothing_within_cap and max_watts is not None:
        reason = (f"the source publishes nothing at or below {max_watts}W for this "
                  f"game; this is its lowest profile at {chosen.min_tdp}W")
        exact_override = False
    elif capped and max_watts is not None:
        reason += f"; limited to {max_watts}W or below"
        exact_override = None
    else:
        exact_override = None

    exact = True if exact_override is None else exact_override
    if (not nothing_within_cap and mode == MODE_BATTERY
            and (chosen.min_tdp or 0) > MODE_TARGET_WATTS[MODE_BATTERY]):
        reason += (f". The source publishes nothing below {chosen.min_tdp}W for this "
                   f"game, so this is the lowest available rather than a battery profile")
        exact = False
    if unrated:
        reason += f"; {len(unrated)} profile(s) state no wattage and were not ranked"

    return Selection(chosen, mode, reason, others(chosen), exact=exact)
