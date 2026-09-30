"""sync.py — refresh the local cache from ROG Ally Life, and say what changed.

``travelready settings update`` runs :func:`sync`. It discovers the settings
posts, compares each against the cache by content hash and modification time,
fetches only what is new or changed, and reports a summary::

    ROG Ally Life update

    Games checked: 87
    Matches found: 61
    New profiles: 4
    Updated profiles: 7
    Unchanged: 50
    No recommendation: 26
    Needs review: 3

Two behaviours matter more than speed.

**Nothing is silently overwritten.** A changed post is stored alongside its
previous ``content_hash``, and :class:`SyncReport` names every entry that
changed, so a user who had applied the old recommendation can see that the
source moved.

**A removed post is not deleted on a whim.** Disappearing from one discovery
run is usually a listing quirk, not a retraction, so removals are reported and
only pruned when explicitly asked for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .budget import DEFAULT_BUDGET_SECONDS, Budget, Cancelled, StopEvent
from .cache import ProfileCache, entry_key
from .client import FetchError, RogAllyLifeClient
from .model import SourceGame, device_family_from_url
from .parser import PARSER_VERSION, parse_post

Progress = Optional[Callable[[str], None]]


@dataclass
class Discovered:
    """One settings post found by discovery, before it is fetched."""

    url: str
    title: str = ""
    device_family: str = ""
    last_updated: str = ""
    payload: Optional[dict] = None      # REST post object, when available

    def key(self) -> str:
        slug = self.url.rstrip("/").rsplit("/", 1)[-1]
        return slug.lower()


@dataclass
class SyncReport:
    """What a refresh did."""

    discovered: int = 0
    fetched: int = 0
    new: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    unparsed: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    blocked: bool = False
    cancelled: bool = False
    timed_out: bool = False
    not_modified: int = 0
    skipped: int = 0
    duration: float = 0.0
    route: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.errors and not self.cancelled

    @property
    def complete(self) -> bool:
        """Was every discovered post considered?"""
        return not (self.blocked or self.cancelled or self.timed_out)

    @property
    def changed(self) -> int:
        return len(self.new) + len(self.updated)

    def describe(self) -> str:
        lines = ["ROG Ally Life update", ""]
        if self.route:
            lines.append(f"Route:             {self.route}")
        lines += [
            f"Posts discovered:  {self.discovered}",
            f"Fetched:           {self.fetched}",
            f"Not modified:      {self.not_modified}",
            f"New profiles:      {len(self.new)}",
            f"Updated profiles:  {len(self.updated)}",
            f"Unchanged:         {len(self.unchanged)}",
        ]
        if self.skipped:
            lines.append(f"Not reached:       {self.skipped}")
        lines.append(f"Duration:          {self.duration:.1f}s")
        if self.unparsed:
            lines.append(f"No settings found: {len(self.unparsed)}")
        if self.missing:
            lines.append(f"Gone from source:  {len(self.missing)} (kept in cache)")
        if self.errors:
            lines.append(f"Errors:            {len(self.errors)}")
            lines += [f"  - {e}" for e in self.errors[:8]]
        if self.cancelled:
            lines += [
                "",
                "Cancelled. Everything fetched before the cancellation was saved,",
                "and the rest of the cache is untouched. Run the update again to",
                "continue — entries that are already current will be skipped.",
            ]
        elif self.timed_out:
            lines += [
                "",
                f"Stopped after {self.duration:.0f}s without finishing. What was",
                "fetched is saved; run the update again to carry on from there.",
                "This is a slow or unresponsive source, not a failure, and the",
                "cached profiles remain usable.",
            ]
        if self.blocked:
            lines += [
                "",
                "The source could not be reached from this machine. The cache is",
                "unchanged, and any profiles already cached remain usable offline.",
            ]
        return "\n".join(lines)


def discover(client: RogAllyLifeClient, device_family: str = "rog_ally_family",
             *, progress: Progress = None,
             budget: Optional[Budget] = None) -> Tuple[List[Discovered], str]:
    """Find settings posts. Returns ``(posts, route_used)``.

    Tries the WordPress REST API, then the sitemap, then the index pages, and
    reports which route actually worked so a sync is reproducible.

    Each route is checked against ``budget`` before it starts: discovery walks
    paginated listings, so without that check a slow source could spend the
    entire sync here and never fetch a single post.
    """
    budget = budget or Budget()

    def note(message: str) -> None:
        if progress:
            progress(message)

    # 1. REST API — structured, paginated, carries modification times.
    try:
        budget.check()
        if client.rest_available():
            note("Checking the WordPress REST API…")
            posts = client.list_posts(budget=budget)
            found = []
            for item in posts:
                link = item.get("link") or ""
                family = device_family_from_url(link)
                if family != device_family:
                    continue
                found.append(Discovered(
                    url=link,
                    title=((item.get("title") or {}).get("rendered") or ""),
                    device_family=family,
                    last_updated=(item.get("modified_gmt") or item.get("date_gmt") or ""),
                    payload=item,
                ))
            if found:
                note(f"REST API: {len(found)} posts for this device.")
                return found, "wp-json REST API"
    except FetchError as exc:
        note(f"REST API unavailable ({exc}).")

    # 2. Sitemap — URL discovery with lastmod.
    try:
        budget.check()
        note("Trying the sitemap…")
        entries = client.sitemap_urls(budget=budget)
        found = [Discovered(url=url, device_family=device_family_from_url(url) or "",
                            last_updated=lastmod)
                 for url, lastmod in entries
                 if device_family_from_url(url) == device_family]
        if found:
            note(f"Sitemap: {len(found)} posts for this device.")
            return found, "sitemap.xml"
    except FetchError as exc:
        note(f"Sitemap unavailable ({exc}).")

    # 3. Index and category archive HTML.
    budget.check()
    note("Falling back to the index pages…")
    found = [Discovered(url=url, title=title, device_family=device_family)
             for title, url in client.index_urls(device_family, budget=budget)]
    if not found:
        # Nothing worked. Distinguish "the site has no such posts" from "we
        # could not reach the site at all" — the second must not look like the
        # first, or a denied network would silently read as an empty catalogue.
        try:
            client.get("")
        except FetchError as exc:
            if exc.blocked:
                raise
    return found, "index pages"


def sync(client: RogAllyLifeClient, cache: ProfileCache,
         *, device_family: str = "rog_ally_family",
         titles: Optional[Sequence[str]] = None,
         force: bool = False, limit: Optional[int] = None,
         progress: Progress = None,
         stop_event: Optional[StopEvent] = None,
         budget_seconds: Optional[float] = DEFAULT_BUDGET_SECONDS,
         clock: Callable[[], float] = time.monotonic) -> SyncReport:
    """Refresh the cache. Read-only against the site; writes only the cache.

    Bounded three ways, because an unbounded sync is what made the GUI look
    hung: ``stop_event`` lets the caller cancel, ``budget_seconds`` caps the
    whole run, and cached HTTP validators turn an unchanged post into a 304
    rather than a full fetch and re-parse.

    Stopping early is not failure. Whatever was fetched is saved, the index is
    written, and the report distinguishes cancelled, timed out and blocked from
    one another — so "we could not finish" never reads as "there is nothing
    there".
    """
    report = SyncReport()
    budget = Budget(seconds=budget_seconds, stop_event=stop_event, clock=clock)

    def note(message: str) -> None:
        if progress:
            progress(message)

    def finish() -> SyncReport:
        report.duration = budget.elapsed
        cache.save_index()
        return report

    try:
        note("Discovering settings posts…")
        posts, route = discover(client, device_family,
                                progress=progress, budget=budget)
    except Cancelled:
        report.cancelled, report.timed_out = budget.cancelled, budget.expired
        note("Stopped during discovery; the cache is unchanged.")
        return finish()
    except FetchError as exc:
        report.blocked = bool(exc.blocked)
        report.errors.append(str(exc))
        return finish()

    report.route = route
    if titles:
        wanted = {t.strip().lower() for t in titles if t.strip()}
        posts = [p for p in posts
                 if any(w in (p.title or p.url).lower() for w in wanted)]
    if limit is not None:
        posts = posts[:limit]
    report.discovered = len(posts)
    note(f"{len(posts)} posts to check via {route}.")

    seen_keys = set()
    total = len(posts)
    for number, post in enumerate(posts, start=1):
        key = post.key()
        seen_keys.add(key)
        cached = cache.get(key)
        record = cache.index.entries.get(key, {})

        try:
            budget.check()
        except Cancelled:
            # Everything already fetched stays cached; the rest is simply not
            # reached. Report the count so the user knows the run is partial.
            report.cancelled, report.timed_out = budget.cancelled, budget.expired
            report.skipped = total - number + 1
            seen_keys.clear()          # an incomplete pass cannot judge removals
            note(f"Stopped after {number - 1} of {total}.")
            return finish()

        # Cheapest check first: the listing itself says when the post changed.
        if (not force and cached is not None
                and cached.parser_version >= PARSER_VERSION
                and post.last_updated
                and record.get("last_updated") == post.last_updated):
            report.unchanged.append(cached.title or key)
            continue

        html = ""
        try:
            if post.payload is not None:
                html = ((post.payload.get("content") or {}).get("rendered") or "")
            if not html:
                note(f"[{number}/{total}] Fetching {post.title or post.url}")
                etag, last_modified = ("", "") if force else \
                    cache.validators_for(key, PARSER_VERSION)
                response = client.fetch_post(post.url, etag=etag,
                                             last_modified=last_modified,
                                             budget=budget)
                if response.not_modified:
                    # The server confirmed our copy is current: no parse, no write.
                    report.not_modified += 1
                    report.unchanged.append(cached.title if cached else key)
                    continue
                html = response.body
                post_etag, post_modified = response.etag, response.last_modified
            else:
                post_etag = post_modified = ""
            report.fetched += 1
        except Cancelled:
            report.cancelled, report.timed_out = budget.cancelled, budget.expired
            report.skipped = total - number + 1
            seen_keys.clear()
            note(f"Stopped after {number - 1} of {total}.")
            return finish()
        except FetchError as exc:
            report.errors.append(f"{post.url}: {exc}")
            if exc.blocked:
                report.blocked = True
                seen_keys.clear()
                break
            continue

        game = parse_post(
            html, url=post.url, title=post.title,
            published_at=((post.payload or {}).get("date_gmt") or ""),
            last_updated=post.last_updated,
        )
        if not game.profiles:
            report.unparsed.append(game.title or key)
            # Still cached, so the title is searchable and the absence of
            # profiles is a fact about the source rather than a gap in the cache.
        if cached is None:
            cache.put(game, etag=post_etag, last_modified=post_modified)
            report.new.append(game.title or key)
        elif cached.content_hash != game.content_hash or force:
            cache.put(game, etag=post_etag, last_modified=post_modified)
            report.updated.append(game.title or key)
        else:
            cache.put(game, etag=post_etag, last_modified=post_modified)
            report.unchanged.append(game.title or key)

    # Only a complete pass may claim a post has gone from the source; a run
    # that stopped early has simply not looked at the rest.
    if seen_keys and report.complete:
        for key in sorted(set(cache.index.entries) - seen_keys):
            record = cache.index.entries.get(key, {})
            if record.get("device_family") == device_family:
                report.missing.append(record.get("title") or key)

    return finish()


def prune(cache: ProfileCache, keys: Sequence[str]) -> List[str]:
    """Delete named cache entries. Only ever called when the user asks."""
    removed = []
    for key in keys:
        if cache.remove(key):
            removed.append(key)
    cache.save_index()
    return removed
