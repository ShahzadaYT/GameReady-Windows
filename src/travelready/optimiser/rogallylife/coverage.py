"""coverage.py — how much of a library ROG Ally Life actually covers.

Answers the questions in plain numbers:

* which of my games have a recommendation?
* which do not?
* which can TravelReady configure automatically?
* which need manual changes?
* which matched ambiguously and need a human?

Read-only. It matches the library against whatever is cached and counts; it
fetches nothing and changes nothing. A game with no cached recommendation is
counted as *no recommendation* — never filled in from elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ...library import GameEntry
from ...readiness import tab_for_launcher
from ..model import CATEGORY_GAME
from .bridge import Resolution, SourceResolver
from .capability import capability_for
from .known_urls import seed_titles
from .matcher import AUTO_THRESHOLD, MIN_THRESHOLD, match


@dataclass
class GameCoverage:
    """One library entry's relationship to the source."""

    entry: GameEntry
    status: str                  # matched | review | no_profile | no_profiles_published
    matched_title: str = ""
    confidence: float = 0.0
    reason: str = ""
    source_url: str = ""
    profile_label: str = ""
    profiles_available: int = 0
    automatic_settings: List[str] = field(default_factory=list)
    manual_settings: List[str] = field(default_factory=list)
    ambiguous_with: Tuple[str, ...] = ()

    @property
    def is_match(self) -> bool:
        return self.status == "matched"


@dataclass
class CoverageReport:
    """The whole library, counted."""

    rows: List[GameCoverage] = field(default_factory=list)
    infrastructure: List[GameEntry] = field(default_factory=list)
    source_games: int = 0
    note: str = ""

    # -- counts ------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.infrastructure)

    def by_launcher(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.rows:
            counts[row.entry.launcher] = counts.get(row.entry.launcher, 0) + 1
        return dict(sorted(counts.items()))

    def with_status(self, status: str) -> List[GameCoverage]:
        return [r for r in self.rows if r.status == status]

    @property
    def matched(self) -> List[GameCoverage]:
        return self.with_status("matched")

    @property
    def review(self) -> List[GameCoverage]:
        return self.with_status("review")

    @property
    def unmatched(self) -> List[GameCoverage]:
        return self.with_status("no_profile")

    @property
    def ambiguous(self) -> List[GameCoverage]:
        return [r for r in self.rows if r.ambiguous_with]

    @property
    def automatically_configurable(self) -> List[GameCoverage]:
        return [r for r in self.matched if r.automatic_settings]

    @property
    def manual_only(self) -> List[GameCoverage]:
        return [r for r in self.matched if not r.automatic_settings and r.manual_settings]

    def describe(self, *, detail: bool = False) -> str:
        launchers = self.by_launcher()
        lines = [
            "ROG Ally Life coverage",
            "=" * 58,
            "",
            f"Total library entries      {self.total}",
            f"  Games                    {len(self.rows)}",
            f"  Launcher software        {len(self.infrastructure)}",
            "",
        ]
        for launcher, count in launchers.items():
            lines.append(f"  {tab_for_launcher(launcher):<12} {count}")
        lines += [
            "",
            f"Source games cached        {self.source_games}",
            "",
            f"ROG Ally Life matches      {len(self.matched)}",
            f"ROG Ally Life unmatched    {len(self.unmatched)}",
            f"Ambiguous / needs review   {len(self.review) + len(self.ambiguous)}",
            f"Page found, no settings    {len(self.with_status('no_profiles_published'))}",
            "",
            f"Settings profiles available  {sum(r.profiles_available for r in self.matched)}",
            f"Games automatically configurable  {len(self.automatically_configurable)}",
            f"Games needing manual changes      {len(self.manual_only)}",
        ]
        if self.note:
            lines += ["", self.note]
        if detail:
            if self.matched:
                lines += ["", "MATCHED", "-" * 58]
                for row in sorted(self.matched, key=lambda r: r.entry.name.lower()):
                    lines.append(f"  {row.entry.name}")
                    lines.append(f"      -> {row.matched_title}  "
                                 f"({row.confidence:.2f}, {row.reason})")
                    if row.profile_label:
                        lines.append(f"      profile: {row.profile_label}")
                    if row.automatic_settings:
                        lines.append(f"      automatic: {', '.join(row.automatic_settings)}")
                    if row.manual_settings:
                        lines.append(f"      manual:    {', '.join(row.manual_settings)}")
            if self.review or self.ambiguous:
                lines += ["", "NEEDS REVIEW", "-" * 58]
                for row in {id(r): r for r in self.review + self.ambiguous}.values():
                    lines.append(f"  {row.entry.name}")
                    lines.append(f"      -> {row.matched_title} "
                                 f"({row.confidence:.2f}, {row.reason})")
                    if row.ambiguous_with:
                        lines.append(f"      also matched: {', '.join(row.ambiguous_with)}")
            if self.unmatched:
                lines += ["", "NO RECOMMENDATION", "-" * 58]
                for row in sorted(self.unmatched, key=lambda r: r.entry.name.lower()):
                    lines.append(f"  {row.entry.name} [{row.entry.launcher}]")
        return "\n".join(lines)


def _settings_split(resolution: Resolution) -> Tuple[List[str], List[str]]:
    """``(automatic, manual)`` canonical keys for a resolved profile."""
    automatic: List[str] = []
    manual: List[str] = []
    if resolution.profile is None:
        return automatic, manual
    for rec in resolution.profile.recommendations:
        capability = capability_for(rec.key)
        if rec.category == CATEGORY_GAME and capability is not None and capability.automatable:
            automatic.append(rec.key)
        else:
            manual.append(rec.key)
    return automatic, manual


def build_report(entries: Sequence[GameEntry], resolver: SourceResolver,
                 *, mode: Optional[str] = None) -> CoverageReport:
    """Match every library entry against the cached source data."""
    from ...library import infrastructure_entries

    infrastructure = infrastructure_entries(entries)
    infrastructure_ids = {id(e) for e in infrastructure}
    games = [e for e in entries if id(e) not in infrastructure_ids]

    report = CoverageReport(infrastructure=list(infrastructure),
                            source_games=len(resolver.candidates()))
    for entry in games:
        resolution = resolver.resolve(entry, mode=mode, accept_review=False)
        found = resolution.match
        automatic, manual = _settings_split(resolution)
        report.rows.append(GameCoverage(
            entry=entry,
            status=resolution.status,
            matched_title=(found.matched_title if found else ""),
            confidence=(found.confidence if found else 0.0),
            reason=(found.reason if found else "no candidate above the threshold"),
            source_url=(found.source_url if found else ""),
            profile_label=(resolution.selection.profile.label
                           if resolution.selection and resolution.selection.profile else ""),
            profiles_available=(len(resolution.source_game.profiles)
                                if resolution.source_game else 0),
            automatic_settings=automatic,
            manual_settings=manual,
            ambiguous_with=(found.ambiguous_with if found else ()),
        ))
    return report


def title_only_report(entries: Sequence[GameEntry],
                      device_family: str = "rog_ally_family") -> CoverageReport:
    """Match the library against the *observed post titles* only.

    Useful when the cache is empty: it shows which games ROG Ally Life is known
    to have a page for, without claiming to know what those pages recommend.
    Every row is therefore ``review`` at best — a title match is not a profile.
    """
    from ...library import infrastructure_entries

    catalogue = [
        type("SeedEntry", (), {"title": title, "source_url": url,
                               "device_family": device_family})()
        for title, url in seed_titles(device_family)
    ]
    infrastructure = infrastructure_entries(entries)
    infrastructure_ids = {id(e) for e in infrastructure}
    games = [e for e in entries if id(e) not in infrastructure_ids]

    report = CoverageReport(infrastructure=list(infrastructure),
                            source_games=len(catalogue))
    report.note = (
        "Matched against the observed post-title list only. A title match means a\n"
        "page exists, not that its settings are known — run 'travelready settings\n"
        "update' on a machine that can reach rogallylife.com to read them. The\n"
        "observed list is partial, so this is a lower bound.")
    for entry in games:
        found = match(entry.name, catalogue)
        report.rows.append(GameCoverage(
            entry=entry,
            status=("review" if found and found.confidence >= MIN_THRESHOLD else "no_profile"),
            matched_title=(found.matched_title if found else ""),
            confidence=(found.confidence if found else 0.0),
            reason=(found.reason if found else "no candidate above the threshold"),
            source_url=(found.source_url if found else ""),
            ambiguous_with=(found.ambiguous_with if found else ()),
        ))
    return report
