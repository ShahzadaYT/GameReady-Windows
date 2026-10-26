"""parser.py — a rendered ROG Ally Life post -> :class:`SourceGame`.

The site is WordPress, so a post's settings live in the rendered HTML of
``content.rendered``: headings introduce each performance profile
("900P 18/25/30W"), and the settings under a heading appear as a table or a
list of ``Label: Value`` lines.

Two rules govern this module.

**Never invent.** A field the page does not state is left empty. There is no
defaulting, no carrying a value over from a sibling profile, and no inference
from the game's engine. An unparseable page yields a :class:`SourceGame` with
no profiles, which the rest of the system reports as "no recommendation".

**Never discard.** A settings row whose label TravelReady does not recognise is
kept with ``canonical=''``, so the user still sees what the source recommended
even though TravelReady will not apply it.

``PARSER_VERSION`` is stored on every cached entry. Raising it invalidates the
cache, so a parser fix re-reads pages rather than trusting an old extraction.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .capability import canonical_key
from .model import (
    SourceGame, SourceProfile, SourceSetting, clean_game_title,
    device_family_from_url, parse_resolution, parse_tdp_watts, parse_vram_gb,
    title_from_slug,
)

PARSER_VERSION = 1

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "section", "article"} | _HEADING_TAGS

#: A heading that introduces a performance profile. It must mention a wattage
#: or a resolution — otherwise it is just a prose heading.
_PROFILE_HEADING = re.compile(
    r"(\d{1,2}\s*(?:/\s*\d{1,2}\s*)*w\b)|(\b\d{3,4}\s*p\b)", re.IGNORECASE)

#: "Label: Value" or "Label - Value" on one line.
_KV_LINE = re.compile(r"^\s*([^:–—]{2,60}?)\s*[:–—]\s*(.{1,80})\s*$")

_STAR_RATING = re.compile(r"(\d(?:\.\d)?)\s*(?:/\s*5|stars?\b|out of 5)", re.IGNORECASE)
_STORE_LINK = re.compile(
    r"https?://(?:store\.steampowered\.com|www\.xbox\.com|store\.epicgames\.com|"
    r"www\.gog\.com|store\.playstation\.com)/[^\s\"'<>]+", re.IGNORECASE)


class _Extractor(HTMLParser):
    """Flattens a post into headings, table rows and text blocks.

    Uses the standard library only — TravelReady ships with no runtime
    dependencies so the packaged executable works on a clean Windows install.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Tuple[str, str]] = []      # (kind, text)
        self.rows: List[Tuple[int, List[str]]] = []  # (block index, cells)
        self.images: List[str] = []
        self.links: List[str] = []
        self._buffer: List[str] = []
        self._kind = "text"
        self._in_table = 0
        self._cells: List[str] = []
        self._cell: List[str] = []
        self._in_cell = False
        self._skip = 0

    # -- helpers -----------------------------------------------------------

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if text:
            self.blocks.append((self._kind, text))
        self._buffer = []
        self._kind = "text"

    # -- HTMLParser --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if tag == "img":
            src = attributes.get("src") or attributes.get("data-src") or ""
            if src:
                self.images.append(src)
            return
        if tag == "a":
            href = attributes.get("href") or ""
            if href:
                self.links.append(href)
        if tag == "table":
            self._flush()
            self._in_table += 1
            return
        if tag == "tr" and self._in_table:
            self._cells = []
            return
        if tag in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell = []
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self._kind = "heading"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "table" and self._in_table:
            self._in_table -= 1
            return
        if tag in ("td", "th") and self._in_cell:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = []
            self._in_cell = False
            return
        if tag == "tr" and self._in_table:
            if any(c for c in self._cells):
                self.rows.append((len(self.blocks), list(self._cells)))
            self._cells = []
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_cell:
            self._cell.append(data)
        else:
            self._buffer.append(data)

    def close(self) -> None:          # noqa: D102
        super().close()
        self._flush()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(str(text or ""))).strip()


def content_hash(text: str) -> str:
    """Stable hash of a post's content, for change detection."""
    return hashlib.sha256(re.sub(r"\s+", " ", str(text or "")).strip().encode("utf-8")).hexdigest()


def is_profile_heading(text: str) -> bool:
    """Does this heading introduce a performance profile?"""
    stripped = _clean(text)
    if not stripped or len(stripped) > 80:
        return False
    return bool(_PROFILE_HEADING.search(stripped))


def _setting_from(label: str, value: str, section: str = "") -> Optional[SourceSetting]:
    label, value = _clean(label), _clean(value)
    if not label or not value or len(label) > 60:
        return None
    if label.lower() in ("setting", "settings", "option", "options", "value", "name"):
        return None
    # Prose, not a setting: a value that runs into a second sentence is the
    # author writing, not a recommended value.
    if re.search(r"[.!?]\s+[A-Z0-9]", value) or value.count(" ") > 8:
        return None
    return SourceSetting(label=label, value=value,
                         canonical=canonical_key(label), section=section)


def parse_post(html: str, *, url: str = "", title: str = "",
               published_at: str = "", last_updated: str = "",
               retrieved_at: str = "") -> SourceGame:
    """Parse one rendered post into a :class:`SourceGame`.

    Structure is recovered from headings: each profile heading opens a profile,
    and every ``Label: Value`` line or two-column table row after it belongs to
    that profile until the next profile heading. Settings that appear before
    any profile heading are attached to every profile, because the site states
    them once as applying throughout.
    """
    extractor = _Extractor()
    try:
        extractor.feed(str(html or ""))
        extractor.close()
    except Exception:                       # malformed markup must not crash a sync
        pass

    blocks = extractor.blocks
    rows_by_index: Dict[int, List[List[str]]] = {}
    for index, cells in extractor.rows:
        rows_by_index.setdefault(index, []).append(cells)

    family = device_family_from_url(url) or ""
    resolved_title = clean_game_title(_clean(title) or title_from_slug(url).title())

    profiles: List[SourceProfile] = []
    shared: List[SourceSetting] = []
    current: Optional[SourceProfile] = None
    prose: List[str] = []

    def absorb_rows(index: int, target: Optional[SourceProfile]) -> None:
        for cells in rows_by_index.get(index, []):
            if len(cells) < 2:
                continue
            setting = _setting_from(cells[0], cells[1],
                                    section=(target.name if target else ""))
            if setting is None:
                continue
            (target.settings if target else shared).append(setting)

    for index in range(len(blocks) + 1):
        absorb_rows(index, current)
        if index >= len(blocks):
            break
        kind, text = blocks[index]
        if kind == "heading" and is_profile_heading(text):
            current = SourceProfile(name=_clean(text))
            profiles.append(current)
            continue
        if kind == "heading":
            continue
        match = _KV_LINE.match(text)
        if match:
            setting = _setting_from(match.group(1), match.group(2),
                                    section=(current.name if current else ""))
            if setting is not None:
                (current.settings if current else shared).append(setting)
                continue
        if len(text) > 40:
            prose.append(text)

    # Settings stated before any profile heading apply to all of them.
    for profile in profiles:
        known = {s.label.lower() for s in profile.settings}
        for setting in shared:
            if setting.label.lower() not in known:
                profile.settings.insert(0, SourceSetting(
                    label=setting.label, value=setting.value,
                    canonical=setting.canonical, section=profile.name))

    # A page with settings but no profile headings still yields one profile,
    # rather than silently returning nothing.
    if not profiles and shared:
        profiles = [SourceProfile(name="Recommended", settings=list(shared))]

    # The hash must cover the settings tables, not just the prose: a table cell
    # is held in ``rows``, not ``blocks``, so hashing blocks alone would miss a
    # changed recommendation — precisely the change detection exists to catch.
    body = " ".join(
        [text for _, text in blocks]
        + [" ".join(cells) for _, cells in extractor.rows]
    )
    rating = None
    rating_match = _STAR_RATING.search(body)
    if rating_match:
        try:
            value = float(rating_match.group(1))
            rating = value if 0 <= value <= 5 else None
        except ValueError:
            rating = None

    return SourceGame(
        title=resolved_title,
        source_url=url,
        device_family=family,
        profiles=profiles,
        slug=str(url or "").rstrip("/").rsplit("/", 1)[-1],
        published_at=_clean(published_at),
        last_updated=_clean(last_updated) or _clean(published_at),
        retrieved_at=retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        performance_rating=rating,
        performance_summary=(prose[0][:400] if prose else ""),
        store_links=sorted({m.group(0) for m in _STORE_LINK.finditer(str(html or ""))}),
        image_url=(extractor.images[0] if extractor.images else ""),
        notes=" ".join(prose[1:3])[:600],
        content_hash=content_hash(body),
        parser_version=PARSER_VERSION,
    )


def parse_index(html: str, *, base_url: str = "") -> List[Tuple[str, str]]:
    """``(title, url)`` pairs of settings posts linked from an index page."""
    extractor = _Extractor()
    try:
        extractor.feed(str(html or ""))
        extractor.close()
    except Exception:
        pass
    seen, out = set(), []
    for href in extractor.links:
        url = html_module.unescape(href)
        if url.startswith("/") and base_url:
            url = base_url.rstrip("/") + url
        if device_family_from_url(url) is None:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((clean_game_title(title_from_slug(url).title()), url))
    return out
