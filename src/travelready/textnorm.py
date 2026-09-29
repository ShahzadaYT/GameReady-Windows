"""textnorm.py — the shared text and path primitives.

Three parts of TravelReady compare strings for a living: joining two installs
of one game, matching a library title to a ROG Ally Life article, and deciding
whether a path is inside a protected directory. Each had its own spelling of
"fold this down to something comparable", and they could drift apart — a pair
that merged under one rule might fail to match under another.

The primitives live here. The *policies* stay where they belong, because they
carry different risk:

* :func:`travelready.library.identity_key` is deliberately conservative — it
  joins two installations of the same game and must never join two games;
* :func:`travelready.optimiser.rogallylife.matcher.normalize_title` is richer —
  it matches a title against an article and can afford to be, because every
  match carries a confidence and a reason.

Both now build on the same folding here, so a fix to one cannot leave the other
behind.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import PurePath, PurePosixPath, PureWindowsPath

#: Trademark and quotation noise. Removed *before* NFKD, because NFKD
#: decomposes U+2122 into the letters "TM" — which is how
#: ``STAR WARS Jedi: Fallen Order™`` once folded to ``…fallen ordertm``.
NOISE_CHARS = dict.fromkeys(map(ord, "\u2122\u00ae\u00a9'\u2019\u02bc`\u201c\u201d"), None)

#: Separators the launchers disagree about. Xbox install folders cannot contain
#: ``:``, so the same game is ``Clair Obscur- Expedition 33`` on disk and
#: ``Clair Obscur: Expedition 33`` everywhere else.
SEPARATORS = re.compile(r"[\u2013\u2014\-:\u2012\u2015_/|,]+")

#: A run of three or more single letters, as a dotted acronym produces:
#: ``S.T.A.L.K.E.R.`` folds to ``s t a l k e r``, which must become ``stalker``.
LETTER_RUN = re.compile(r"\b(?:[a-z] ){2,}[a-z]\b")

_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def strip_noise(text: str) -> str:
    """Remove trademark marks and apostrophes, then fold accents."""
    cleaned = str(text or "").translate(NOISE_CHARS)
    decomposed = unicodedata.normalize("NFKD", cleaned)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def fold(text: str, *, separators: bool = True, letter_runs: bool = True) -> str:
    """Lower-case alphanumerics and single spaces. Digits are always kept."""
    working = strip_noise(text)
    if separators:
        working = SEPARATORS.sub(" ", working)
    working = re.sub(r"[^a-z0-9 ]+", " ", working.lower())
    working = re.sub(r"\s+", " ", working).strip()
    if letter_runs:
        working = LETTER_RUN.sub(lambda m: m.group(0).replace(" ", ""), working)
    return re.sub(r"\s+", " ", working).strip()


def squash(text: str) -> str:
    """Fold, then remove the spaces too — a key, not a phrase."""
    return fold(text).replace(" ", "")


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def as_path(value: str) -> PurePath:
    """Parse ``value`` the way Windows would, whatever the host OS is.

    TravelReady runs on Windows but is developed and tested on Linux, where
    ``pathlib.Path`` reads ``C:\\Games\\x.exe`` as one long filename. Every
    path decision goes through here so the tests exercise the logic the device
    will run.
    """
    text = str(value or "")
    if _WINDOWS_PATH.match(text) or "\\" in text:
        return PureWindowsPath(text)
    return PurePosixPath(text)


def norm_dir(path: str) -> str:
    """A directory normalised for prefix comparison: lower-case, one trailing sep."""
    text = str(path or "").strip().replace("/", "\\")
    if not text:
        return ""
    return text.rstrip("\\").lower() + "\\"


def path_components(path: str) -> list:
    """Lower-cased path segments, separator-agnostic."""
    return [p.strip().lower().rstrip("\\")
            for p in str(path or "").replace("/", "\\").split("\\") if p.strip()]


def leaf(path: str) -> str:
    """The final component of a path, separator-agnostic."""
    return os.path.basename(str(path or "").replace("\\", "/"))


# --------------------------------------------------------------------------
# durable writes
# --------------------------------------------------------------------------

def atomic_write(path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` without ever leaving a truncated file.

    The single implementation. Everything that persists state — the library,
    the history, the source cache, the transaction log — writes through here,
    so the durability guarantee is made in one place and tested once.
    """
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, target)


def atomic_write_bytes(path, payload: bytes) -> None:
    """Byte-level counterpart of :func:`atomic_write`."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
