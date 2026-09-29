"""matcher.py — library game title -> ROG Ally Life title.

Titles in ``games.json`` do not match the site's titles. Real examples from the
supplied library: ``STAR WARS Jedi: Fallen Order™`` vs ``STAR WARS Jedi - Fallen
Order``, ``Clair Obscur- Expedition 33`` (the Xbox install, where the colon is
illegal in a folder name) vs ``Clair Obscur: Expedition 33``, and
``Battlefield 3™ Limited Edition`` vs ``Battlefield 3``.

Every match carries a **confidence** and a **reason**, and only a match at or
above :data:`AUTO_THRESHOLD` is used without asking. The rules are ordered and
each one states what it did, so a match is always explainable:

    Clair Obscur- Expedition 33
    -> Clair Obscur: Expedition 33
    confidence 1.00 — exact normalised title

Guarding against false positives matters more than reach. A sequel number or
roman numeral is *never* normalised away, so ``DOOM`` never matches
``DOOM II``, and ``Forza Horizon 5`` never matches ``Forza Horizon 6``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ...textnorm import fold, strip_noise

#: At or above this, a match may be used automatically.
AUTO_THRESHOLD = 0.90

#: Below this, the candidate is not even offered for review.
MIN_THRESHOLD = 0.72

#: Edition / packaging words. Stripped when comparing, because the site titles
#: the base game. Deliberately excludes anything that could distinguish two
#: products, so no sequel or numbered entry is ever collapsed.
_EDITION_WORDS = (
    "definitive edition", "deluxe edition", "ultimate edition", "complete edition",
    "gold edition", "premium edition", "standard edition", "limited edition",
    "collectors edition", "collector's edition", "anniversary edition",
    "enhanced edition", "special edition", "legacy edition", "royal edition",
    "director's cut", "directors cut", "game of the year edition",
    "game of the year", "goty edition", "goty", "remastered", "remaster",
    "definitive", "deluxe", "ultimate", "complete edition", "edition",
    "bundle", "pack", "trial", "demo", "beta", "preview", "early access",
    "pc edition", "windows edition", "for windows", "digital edition",
)

#: Trademark and formatting noise. Apostrophes are *removed* rather than turned
#: into separators, so "Marvel's Spider-Man" and "Marvels Spider Man" agree.
_NOISE_CHARS = dict.fromkeys(map(ord, "™®©'’ʼ`"), None)

#: A run of three or more single letters, as produced by a dotted acronym:
#: "S.T.A.L.K.E.R." becomes "s t a l k e r", which must collapse to "stalker".
_LETTER_RUN = re.compile(r"\b(?:[a-z] ){2,}[a-z]\b")

#: Separators the launchers mangle. Xbox installs cannot contain ':' so the
#: folder becomes '-'; Steam keeps the colon.
_SEPARATORS = re.compile(r"[–—\-:–—_/|,]+")

_ARTICLES = ("the ", "a ", "an ")

_ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}

#: Abbreviations users and launchers actually use.
_ABBREVIATIONS = {
    "cod": "call of duty",
    "gta": "grand theft auto",
    "re": "resident evil",
    "nfs": "need for speed",
    "ac": "assassins creed",
    "bf": "battlefield",
    "tlou": "the last of us",
    "sw": "star wars",
    "ff": "final fantasy",
    "mgs": "metal gear solid",
    "dmc": "devil may cry",
    "kcd": "kingdom come deliverance",
}


def strip_accents(text: str) -> str:
    """Fold accents and trademark marks. See :func:`textnorm.strip_noise`."""
    return strip_noise(text)


def expand_roman_numerals(text: str) -> str:
    """Turn standalone roman numerals into digits.

    Applied to *both* sides before comparison, so ``Final Fantasy XVI`` and
    ``Final Fantasy 16`` agree. Because it is a normalisation rather than a
    removal, ``DOOM II`` still differs from ``DOOM``.
    """
    def replace(match: re.Match) -> str:
        word = match.group(0).lower()
        return str(_ROMAN[word]) if word in _ROMAN else match.group(0)

    return re.sub(r"\b[ivxIVX]{1,6}\b", replace, str(text or ""))


def expand_abbreviations(text: str) -> str:
    """Expand a leading known abbreviation (``RE4`` -> ``resident evil 4``)."""
    lowered = str(text or "").strip().lower()
    m = re.match(r"^([a-z]{2,4})\s*(\d.*)?$", lowered)
    if m and m.group(1) in _ABBREVIATIONS:
        rest = (m.group(2) or "").strip()
        return f"{_ABBREVIATIONS[m.group(1)]} {rest}".strip()
    first, _, rest = lowered.partition(" ")
    if first in _ABBREVIATIONS:
        return f"{_ABBREVIATIONS[first]} {rest}".strip()
    return lowered


def strip_editions(text: str) -> Tuple[str, List[str]]:
    """Remove edition words. Returns ``(stripped, removed_words)``."""
    working = f" {str(text or '').lower()} "
    removed: List[str] = []
    for word in sorted(_EDITION_WORDS, key=len, reverse=True):
        pattern = r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"
        if re.search(pattern, working):
            working = re.sub(pattern, " ", working)
            removed.append(word)
    return working.strip(), removed


def normalize_title(text: str, *, drop_editions: bool = True) -> str:
    """The comparison form of a title.

    Folds accents and trademark marks, unifies the separators launchers
    disagree about, expands roman numerals and abbreviations, optionally drops
    edition words, and collapses to lowercase alphanumerics plus single spaces.
    Digits are always kept.
    """
    working = _SEPARATORS.sub(" ", strip_noise(text))
    working = expand_roman_numerals(working)
    working = expand_abbreviations(working)
    if drop_editions:
        working, _ = strip_editions(working)
    return fold(working, separators=False)


def _drop_article(text: str) -> str:
    for article in _ARTICLES:
        if text.startswith(article):
            return text[len(article):]
    return text


def _numbers_in(text: str) -> List[str]:
    return re.findall(r"\d+", text)


@dataclass(frozen=True)
class MatchResult:
    """One candidate, with why it was chosen and how far it can be trusted."""

    query: str
    matched_title: str
    confidence: float
    reason: str
    source_url: str = ""
    device_family: str = ""
    ambiguous_with: Tuple[str, ...] = ()

    @property
    def automatic(self) -> bool:
        return self.confidence >= AUTO_THRESHOLD and not self.ambiguous_with

    @property
    def needs_review(self) -> bool:
        return not self.automatic and self.confidence >= MIN_THRESHOLD

    def describe(self) -> str:
        head = "MATCH" if self.automatic else "REVIEW REQUIRED"
        lines = [head, self.query, f"-> {self.matched_title}",
                 f"confidence: {self.confidence:.2f}", f"reason: {self.reason}"]
        if self.ambiguous_with:
            lines.append(f"also matched: {', '.join(self.ambiguous_with)}")
        return "\n".join(lines)


def score(query: str, candidate: str) -> Tuple[float, str]:
    """Score ``query`` against ``candidate``; returns ``(confidence, reason)``.

    The ladder runs from certain to weakest, and stops at the first rule that
    fires, so the reason names the rule that actually decided the match.
    """
    raw_q, raw_c = str(query or "").strip(), str(candidate or "").strip()
    if not raw_q or not raw_c:
        return 0.0, "empty title"

    if raw_q.lower() == raw_c.lower():
        return 1.0, "exact title"

    nq = normalize_title(raw_q, drop_editions=False)
    nc = normalize_title(raw_c, drop_editions=False)
    if nq and nq == nc:
        return 1.0, "exact normalised title"

    # Sequel safety: differing numbers means different games, always.
    numbers_q, numbers_c = _numbers_in(nq), _numbers_in(nc)
    if numbers_q != numbers_c:
        eq, _ = strip_editions(nq)
        ec, _ = strip_editions(nc)
        if _numbers_in(eq) != _numbers_in(ec):
            return 0.0, (f"different numbering ({', '.join(numbers_q) or 'none'} vs "
                         f"{', '.join(numbers_c) or 'none'})")

    eq, removed_q = strip_editions(nq)
    ec, removed_c = strip_editions(nc)
    eq, ec = re.sub(r"\s+", " ", eq).strip(), re.sub(r"\s+", " ", ec).strip()
    if eq and eq == ec:
        removed = sorted(set(removed_q) | set(removed_c))
        return 0.94, f"normalised title + edition suffix ({', '.join(removed)})"

    aq, ac = _drop_article(eq), _drop_article(ec)
    if aq and aq == ac:
        return 0.92, "normalised title ignoring leading article"

    # One title being a clean prefix of the other: a subtitle the site omits.
    longer, shorter = (ec, eq) if len(ec) >= len(eq) else (eq, ec)
    if shorter and longer.startswith(shorter + " ") and len(shorter) >= 8:
        extra = longer[len(shorter):].strip()
        if not _numbers_in(extra):
            return 0.88, f"title prefix match (source adds '{extra}')"

    if not eq or not ec:
        return 0.0, "nothing left after normalisation"

    # Guard against a shared franchise prefix carrying two different games:
    # "Need for Speed Unbound" and "Need for Speed Heat" share 75% of their
    # characters but name different products. If each title keeps a
    # substantial word the other lacks, they are siblings, not the same game.
    tokens_q, tokens_c = set(eq.split()), set(ec.split())
    only_q = {t for t in tokens_q - tokens_c if len(t) >= 4}
    only_c = {t for t in tokens_c - tokens_q if len(t) >= 4}
    if only_q and only_c:
        return 0.0, (f"different distinguishing words "
                     f"({'/'.join(sorted(only_q))} vs {'/'.join(sorted(only_c))})")

    ratio = difflib.SequenceMatcher(None, eq, ec).ratio()
    if ratio >= 0.97:
        return 0.90, f"near-identical normalised title ({ratio:.2f})"
    if ratio >= 0.88:
        return round(0.72 + (ratio - 0.88) * 1.5, 2), f"similar normalised title ({ratio:.2f})"
    return round(ratio, 2), f"weak similarity ({ratio:.2f})"


def match(query: str, candidates: Sequence, *,
          title_of=lambda c: getattr(c, "title", str(c)),
          url_of=lambda c: getattr(c, "source_url", ""),
          family_of=lambda c: getattr(c, "device_family", ""),
          prefer_family: str = "") -> Optional[MatchResult]:
    """Best candidate for ``query``, or ``None`` when nothing is close enough.

    When two different titles score equally well the result is marked
    ambiguous, which forces review rather than guessing between them.
    ``prefer_family`` breaks a tie between the *same* game published for two
    devices — that is not ambiguity, it is the device choice.
    """
    scored: List[Tuple[float, str, object]] = []
    for candidate in candidates:
        confidence, reason = score(query, title_of(candidate))
        if confidence >= MIN_THRESHOLD:
            scored.append((confidence, reason, candidate))
    if not scored:
        return None

    scored.sort(key=lambda row: (
        -row[0],
        0 if (prefer_family and family_of(row[2]) == prefer_family) else 1,
        title_of(row[2]).lower(),
    ))
    best_confidence, best_reason, best = scored[0]

    rivals = tuple(sorted({
        title_of(c) for conf, _, c in scored
        if abs(conf - best_confidence) < 1e-9
        and normalize_title(title_of(c)) != normalize_title(title_of(best))
    }))
    return MatchResult(
        query=query,
        matched_title=title_of(best),
        confidence=best_confidence,
        reason=best_reason,
        source_url=url_of(best),
        device_family=family_of(best),
        ambiguous_with=rivals,
    )
