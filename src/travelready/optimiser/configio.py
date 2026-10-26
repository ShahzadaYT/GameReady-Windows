"""configio.py — reading and minimally editing game configuration files.

Game configuration files belong to the user. They carry comments, deliberate
ordering, duplicate keys with meaning, unknown sections written by mods, and a
specific encoding and line ending. Parsing such a file into a dictionary and
writing it back out destroys all of that, so this module never does.

Instead the file is held as a list of lines and an edit replaces **only the
value portion of the one line that holds the key**. Everything else — every
byte of every other line, the BOM, the newline style, the trailing newline —
is preserved exactly.

If a file cannot be parsed with confidence, :class:`IniDocument` reports that
and no edit is offered. Refusing to change a file is always available; guessing
is not.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: ``key = value``, keeping the exact surrounding whitespace, and any inline
#: trailing comment, so replacing a value rewrites nothing but the value. An
#: inline comment is only recognised after whitespace, which is the usual INI
#: convention and avoids truncating values that legitimately contain ``;``.
_KV = re.compile(r"^(?P<indent>\s*)(?P<key>[^=:;#\[\]\s][^=:]*?)(?P<sep>\s*[=:]\s*)"
                 r"(?P<value>.*?)(?P<comment>\s+[;#].*?)?(?P<trail>\s*)$")
_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]*)\]\s*$")
_COMMENT = re.compile(r"^\s*[;#]")


class ConfigError(Exception):
    """Raised when a configuration file cannot be handled safely."""


@dataclass(frozen=True)
class Entry:
    """One ``key = value`` occurrence, located by line number."""

    section: str
    key: str
    value: str
    line_no: int

    @property
    def location(self) -> str:
        return f"[{self.section}] {self.key}" if self.section else self.key


def detect_newline(text: str) -> str:
    """The dominant newline style, so it can be reproduced exactly."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def read_text_preserving(path: str) -> Tuple[str, str, str]:
    """Read a config file, returning ``(text, encoding, newline)``.

    Tries UTF-8 with and without BOM, then UTF-16, then latin-1 as a
    last resort that cannot fail. The encoding is returned so the same one is
    used when writing back.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # 'utf-8-sig' decodes files with *and* without a BOM, but encoding with
        # it always *adds* one. Report plain utf-8 unless a BOM was really
        # there, so writing back cannot introduce a byte the game never wrote.
        if encoding == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8"
        return text, encoding, detect_newline(text)
    raise ConfigError(f"Could not decode {path} with any supported encoding.")


class IniDocument:
    """An INI-style file held as lines, edited in place, never rewritten.

    ``parse_ok`` is False when the file does not look like INI at all; callers
    must not attempt edits in that case.
    """

    def __init__(self, text: str, encoding: str = "utf-8", newline: str = "\n",
                 path: str = "") -> None:
        self.path = path
        self.encoding = encoding
        self.newline = newline
        self.had_bom = encoding == "utf-8-sig"
        self.ends_with_newline = text.endswith(("\n", "\r"))
        # splitlines() loses the newline style, which is why it is kept above.
        self._lines: List[str] = text.splitlines()
        self._entries: List[Entry] = []
        self.parse_ok = True
        self.parse_note = ""
        self._parse()

    # -- parsing -----------------------------------------------------------

    def _parse(self) -> None:
        section = ""
        kv_lines = 0
        for index, line in enumerate(self._lines):
            if _COMMENT.match(line) or not line.strip():
                continue
            m = _SECTION.match(line)
            if m:
                section = m.group("name").strip()
                continue
            m = _KV.match(line)
            if m:
                kv_lines += 1
                self._entries.append(Entry(section, m.group("key").strip(),
                                           m.group("value"), index))
        content_lines = [l for l in self._lines
                         if l.strip() and not _COMMENT.match(l)]
        if content_lines and kv_lines == 0:
            self.parse_ok = False
            self.parse_note = "No 'key = value' lines were found; this may not be an INI file."
        elif content_lines and kv_lines / len(content_lines) < 0.5:
            self.parse_ok = False
            self.parse_note = ("Fewer than half the content lines look like INI "
                               "settings; the format is not understood well enough to edit.")

    # -- reading -----------------------------------------------------------

    @property
    def entries(self) -> List[Entry]:
        return list(self._entries)

    def sections(self) -> List[str]:
        seen, out = set(), []
        for e in self._entries:
            if e.section not in seen:
                seen.add(e.section)
                out.append(e.section)
        return out

    def find(self, key: str, section: Optional[str] = None) -> List[Entry]:
        """Every occurrence of ``key``. Duplicates are reported, never collapsed."""
        k = key.strip().lower()
        return [e for e in self._entries
                if e.key.strip().lower() == k
                and (section is None or e.section.strip().lower() == section.strip().lower())]

    def get(self, key: str, section: Optional[str] = None) -> Optional[str]:
        """The effective value of ``key`` — the last occurrence, as INI readers do."""
        hits = self.find(key, section)
        return hits[-1].value.strip() if hits else None

    # -- editing -----------------------------------------------------------

    def set_value(self, key: str, value: str, section: Optional[str] = None) -> Entry:
        """Replace the value of an existing key, touching nothing else.

        Only an existing key is edited. Creating keys or sections is not
        supported: a setting the game does not already write is one whose
        semantics and placement TravelReady cannot be sure of.

        When a key occurs more than once, the last occurrence is edited — the
        one an INI reader would use — and the others are left untouched so the
        file's duplicate-key behaviour is unchanged.
        """
        if not self.parse_ok:
            raise ConfigError(f"Refusing to edit {self.path}: {self.parse_note}")
        hits = self.find(key, section)
        if not hits:
            raise ConfigError(
                f"Key '{key}' does not exist in {self.path or 'this file'}. "
                f"TravelReady only changes settings the game already writes.")
        target = hits[-1]
        line = self._lines[target.line_no]
        m = _KV.match(line)
        if not m:                                   # cannot happen, but fail closed
            raise ConfigError(f"Line {target.line_no + 1} could not be re-parsed; no edit made.")
        rebuilt = (m.group("indent") + m.group("key") + m.group("sep")
                   + str(value) + (m.group("comment") or "") + m.group("trail"))
        self._lines[target.line_no] = rebuilt
        updated = Entry(target.section, target.key, str(value), target.line_no)
        self._entries = [updated if e is target else e for e in self._entries]
        return updated

    # -- serialising -------------------------------------------------------

    def to_text(self) -> str:
        """Reassemble the file with its original newline style and trailing newline."""
        text = self.newline.join(self._lines)
        if self.ends_with_newline:
            text += self.newline
        return text

    def to_bytes(self) -> bytes:
        encoding = self.encoding
        return self.to_text().encode(encoding)

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "IniDocument":
        text, encoding, newline = read_text_preserving(path)
        return cls(text, encoding, newline, path)


class JsonDocument:
    """A JSON config edited with the same minimal-change discipline.

    Only an existing scalar at an existing path is replaced, and the file is
    re-serialised with its original indentation where that can be detected.
    """

    def __init__(self, text: str, encoding: str = "utf-8", newline: str = "\n",
                 path: str = "") -> None:
        self.path = path
        self.encoding = encoding
        self.newline = newline
        self.ends_with_newline = text.endswith(("\n", "\r"))
        self.parse_ok = True
        self.parse_note = ""
        self.indent = 2
        m = re.search(r"\{\s*\r?\n(\s+)\S", text)
        if m:
            self.indent = len(m.group(1).expandtabs(4))
        try:
            self.data = json.loads(text)
        except ValueError as exc:
            self.data = None
            self.parse_ok = False
            self.parse_note = f"File is not valid JSON ({exc})."

    def get(self, dotted_key: str):
        node = self.data
        for part in str(dotted_key).split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set_value(self, dotted_key: str, value) -> None:
        if not self.parse_ok:
            raise ConfigError(f"Refusing to edit {self.path}: {self.parse_note}")
        parts = str(dotted_key).split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"Key path '{dotted_key}' does not exist in {self.path}.")
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            raise ConfigError(f"Key '{dotted_key}' does not exist in {self.path}. "
                              f"TravelReady only changes settings the game already writes.")
        existing = node[parts[-1]]
        if isinstance(existing, (dict, list)):
            raise ConfigError(f"'{dotted_key}' is a structure, not a single value; not edited.")
        node[parts[-1]] = _coerce_like(existing, value)

    def to_text(self) -> str:
        text = json.dumps(self.data, indent=self.indent, ensure_ascii=False)
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        return text + (self.newline if self.ends_with_newline else "")

    def to_bytes(self) -> bytes:
        return self.to_text().encode(self.encoding)

    @classmethod
    def load(cls, path: str) -> "JsonDocument":
        text, encoding, newline = read_text_preserving(path)
        return cls(text, encoding, newline, path)


def _coerce_like(existing, value):
    """Keep the JSON type the game already used for this key."""
    if isinstance(existing, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(existing, int) and not isinstance(existing, bool):
        try:
            return int(str(value).strip())
        except ValueError:
            return existing
    if isinstance(existing, float):
        try:
            return float(str(value).strip())
        except ValueError:
            return existing
    return str(value)


def load_document(path: str):
    """Load ``path`` as the right document type, or raise :class:`ConfigError`."""
    from ..library import as_path

    suffix = as_path(path).suffix.lower()
    if suffix in (".ini", ".cfg", ".conf"):
        return IniDocument.load(path)
    if suffix == ".json":
        return JsonDocument.load(path)
    raise ConfigError(f"No safe editor for '{suffix}' files.")


def unified_diff(before: str, after: str, path: str = "") -> str:
    """A minimal diff of exactly what a write would change."""
    import difflib

    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"{path} (current)", tofile=f"{path} (proposed)", lineterm="", n=2))
    return "\n".join(lines)
