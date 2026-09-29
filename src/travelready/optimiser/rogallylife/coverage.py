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

from ...identity import GameIdentity, build_identities
from ...library import GameEntry
from ...readiness import tab_for_launcher
from ..model import CATEGORY_GAME
from .bridge import Resolution, SourceResolver
from .capability import capability_for
from .known_urls import seed_titles
from .matcher import AUTO_THRESHOLD, MIN_THRESHOLD, match


#: Every game lands in exactly one of these. ``source_unavailable`` exists
#: because "we could not reach the source" is a different fact from "the source
#: has no recommendation", and reporting the first as the second is how a
#: network problem becomes a permanent-looking answer.
STATUS_MATCHED = "matched"
STATUS_REVIEW = "review"
STATUS_NO_PROFILE = "no_profile"
STATUS_NO_DATA = "no_profiles_published"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"

ALL_STATUSES = (STATUS_MATCHED, STATUS_REVIEW, STATUS_NO_DATA,
                STATUS_SOURCE_UNAVAILABLE, STATUS_NO_PROFILE)

STATUS_LABELS = {
    STATUS_MATCHED: "Recommendation found",
    STATUS_REVIEW: "Possible match, needs review",
    STATUS_NO_DATA: "Page found, no settings readable",
    STATUS_SOURCE_UNAVAILABLE: "Not checked (source not synced)",
    STATUS_NO_PROFILE: "No recommendation published",
}


@dataclass
class GameCoverage:
    """One game's relationship to the source."""

    entry: GameEntry
    status: str
    matched_title: str = ""
    confidence: float = 0.0
    reason: str = ""
    source_url: str = ""
    profile_label: str = ""
    profiles_available: int = 0
    automatic_settings: List[str] = field(default_factory=list)
    manual_settings: List[str] = field(default_factory=list)
    ambiguous_with: Tuple[str, ...] = ()

    identity: Optional[GameIdentity] = None

    @property
    def is_match(self) -> bool:
        return self.status == STATUS_MATCHED

    @property
    def game(self) -> str:
        return (self.identity.canonical_title if self.identity
                else getattr(self.entry, "name", ""))


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
        return self.with_status(STATUS_MATCHED)

    @property
    def review(self) -> List[GameCoverage]:
        return self.with_status(STATUS_REVIEW)

    @property
    def unmatched(self) -> List[GameCoverage]:
        return self.with_status(STATUS_NO_PROFILE)

    @property
    def not_checked(self) -> List[GameCoverage]:
        return self.with_status(STATUS_SOURCE_UNAVAILABLE)

    def categorised(self) -> Dict[str, List[GameCoverage]]:
        """Every game, in exactly one category."""
        out = {status: self.with_status(status) for status in ALL_STATUSES}
        assigned = sum(len(rows) for rows in out.values())
        assert assigned == len(self.rows), (
            f"{len(self.rows) - assigned} game(s) fell outside every category")
        return out

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
        categorised = self.categorised()
        lines += ["", f"Source games cached        {self.source_games}", "",
                  "Every game appears in exactly one row below."]
        for status in ALL_STATUSES:
            lines.append(f"  {STATUS_LABELS[status]:<36}{len(categorised[status]):>4}")
        lines.append(f"  {'':<36}{'----':>4}")
        lines.append(f"  {'Total games':<36}{len(self.rows):>4}")
        if self.ambiguous:
            lines.append(f"\n  Of those, ambiguous matches: {len(self.ambiguous)}")
        lines += [
            "",
            f"Settings profiles available       {sum(r.profiles_available for r in self.matched)}",
            f"Games automatically configurable  {len(self.automatically_configurable)}",
            f"Games needing manual changes      {len(self.manual_only)}",
        ]
        if self.note:
            lines += ["", self.note]
        if detail:
            if self.matched:
                lines += ["", "MATCHED", "-" * 58]
                for row in sorted(self.matched, key=lambda r: r.game.lower()):
                    lines.append(f"  {row.game}")
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
                    lines.append(f"  {row.game}")
                    lines.append(f"      -> {row.matched_title} "
                                 f"({row.confidence:.2f}, {row.reason})")
                    if row.ambiguous_with:
                        lines.append(f"      also matched: {', '.join(row.ambiguous_with)}")
            if self.unmatched:
                lines += ["", "NO RECOMMENDATION", "-" * 58]
                for row in sorted(self.unmatched, key=lambda r: r.game.lower()):
                    lines.append(f"  {row.game} [{row.entry.launcher}]")
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
    """Match every game against the cached source data.

    Works over game identities rather than raw entries, so one game installed
    from two launchers is counted once and looks up one recommendation.
    """
    from ...classification import classify_entry

    non_games = [e for e in entries if not classify_entry(e).is_game]
    identities = build_identities(entries)
    synced = bool(getattr(getattr(resolver, "cache", None), "index", None)
                  and resolver.cache.index.entries)

    report = CoverageReport(infrastructure=list(non_games),
                            source_games=len(resolver.candidates()))
    if not synced:
        report.note = (
            "The recommendation cache has never been synced, so no game has been\n"
            "checked against the source. This is not the same as those games\n"
            "having no recommendation. Run 'travelready settings update'.")
    for identity in identities:
        installation = identity.best_installation()
        entry = installation.entry if installation else None
        if entry is None:
            continue
        if not synced:
            report.rows.append(GameCoverage(
                entry=entry, status=STATUS_SOURCE_UNAVAILABLE,
                reason="the recommendation cache has not been synced",
                identity=identity))
            continue
        resolution = resolver.resolve(entry, mode=mode, accept_review=False)
        found = resolution.match
        automatic, manual = _settings_split(resolution)
        report.rows.append(GameCoverage(
            entry=entry,
            identity=identity,
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
    from ...classification import classify_entry

    catalogue = [
        type("SeedEntry", (), {"title": title, "source_url": url,
                               "device_family": device_family})()
        for title, url in seed_titles(device_family)
    ]
    non_games = [e for e in entries if not classify_entry(e).is_game]
    identities = build_identities(entries)

    report = CoverageReport(infrastructure=list(non_games),
                            source_games=len(catalogue))
    report.note = (
        "Matched against the observed post-title list only. A title match means a\n"
        "page exists, not that its settings are known — run 'travelready settings\n"
        "update' on a machine that can reach rogallylife.com to read them. The\n"
        "observed list is partial, so this is a lower bound.")
    for identity in identities:
        installation = identity.best_installation()
        if installation is None:
            continue
        entry = installation.entry
        found = match(identity.canonical_title, catalogue)
        report.rows.append(GameCoverage(
            entry=entry,
            identity=identity,
            status=(STATUS_REVIEW if found and found.confidence >= MIN_THRESHOLD
                    else STATUS_NO_PROFILE),
            matched_title=(found.matched_title if found else ""),
            confidence=(found.confidence if found else 0.0),
            reason=(found.reason if found else "no candidate above the threshold"),
            source_url=(found.source_url if found else ""),
            ambiguous_with=(found.ambiguous_with if found else ()),
        ))
    return report
