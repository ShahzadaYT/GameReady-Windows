"""Configuration preservation tests: comments, ordering, encoding, newlines."""
from __future__ import annotations

import pytest

from travelready.optimiser.configio import (
    ConfigError, IniDocument, JsonDocument, detect_newline, read_text_preserving,
    unified_diff,
)

SAMPLE = (
    "; leading comment\n"
    "# hash comment\n"
    "[Alpha]\n"
    "  spaced   =   value   \n"
    "dup=1\n"
    "dup=2\n"
    "unknown_mod_key=keep\n"
    "\n"
    "[Beta]\n"
    "inline=7 ; trailing note\n"
    "path=C:\\a;b\\c\n"
    "empty=\n"
)


def _doc(text=SAMPLE, encoding="utf-8", newline="\n"):
    return IniDocument(text, encoding, newline, "test.ini")


def test_round_trip_is_byte_identical():
    for text in (SAMPLE, SAMPLE.replace("\n", "\r\n"), SAMPLE.rstrip("\n")):
        nl = detect_newline(text)
        assert _doc(text, newline=nl).to_text() == text


def test_duplicate_keys_are_reported_not_collapsed():
    doc = _doc()
    assert len(doc.find("dup")) == 2
    assert doc.get("dup") == "2", "the effective value is the last occurrence"


def test_editing_a_duplicate_touches_only_the_effective_one():
    doc = _doc()
    doc.set_value("dup", "9")
    text = doc.to_text()
    assert "dup=1" in text and "dup=9" in text and "dup=2" not in text


def test_whitespace_around_the_separator_is_preserved():
    doc = _doc()
    doc.set_value("spaced", "new")
    assert "  spaced   =   new   " in doc.to_text()


def test_comments_survive_editing():
    doc = _doc()
    doc.set_value("inline", "8")
    text = doc.to_text()
    assert "; leading comment" in text
    assert "# hash comment" in text
    assert "inline=8 ; trailing note" in text


def test_unknown_keys_and_sections_are_untouched():
    doc = _doc()
    doc.set_value("inline", "8")
    text = doc.to_text()
    assert "unknown_mod_key=keep" in text
    assert text.index("[Alpha]") < text.index("[Beta]")


def test_semicolons_inside_values_are_not_treated_as_comments():
    assert _doc().get("path") == "C:\\a;b\\c"


def test_empty_values_are_readable_and_writable():
    doc = _doc()
    assert doc.get("empty") == ""
    doc.set_value("empty", "now set")
    assert "empty=now set" in doc.to_text()


def test_creating_a_missing_key_is_refused():
    with pytest.raises(ConfigError, match="does not exist"):
        _doc().set_value("brand_new_key", "1")


def test_unparseable_file_refuses_all_edits():
    doc = IniDocument("this is\njust prose\nwith no settings at all\n", path="x.ini")
    assert not doc.parse_ok
    with pytest.raises(ConfigError):
        doc.set_value("anything", "1")


def test_mostly_prose_file_is_rejected():
    doc = IniDocument("a=1\n" + "prose line\n" * 10, path="x.ini")
    assert not doc.parse_ok


def test_section_scoped_lookup():
    doc = IniDocument("[A]\nk=1\n[B]\nk=2\n", path="x.ini")
    assert doc.get("k", "A") == "1"
    assert doc.get("k", "B") == "2"
    doc.set_value("k", "9", "A")
    assert doc.to_text() == "[A]\nk=9\n[B]\nk=2\n"


@pytest.mark.parametrize("raw,expected_encoding,bom", [
    (b"[a]\nb=1\n", "utf-8", False),
    (b"\xef\xbb\xbf[a]\nb=1\n", "utf-8-sig", True),
])
def test_bom_is_preserved_exactly_as_found(tmp_path, raw, expected_encoding, bom):
    path = tmp_path / "c.ini"
    path.write_bytes(raw)
    text, encoding, newline = read_text_preserving(str(path))
    assert encoding == expected_encoding
    doc = IniDocument(text, encoding, newline, str(path))
    assert doc.to_bytes() == raw
    assert doc.to_bytes().startswith(b"\xef\xbb\xbf") is bom


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_newline_style_is_preserved(newline):
    text = newline.join(["[a]", "b=1", "c=2"]) + newline
    doc = IniDocument(text, "utf-8", detect_newline(text), "x.ini")
    doc.set_value("b", "9")
    assert doc.to_text() == newline.join(["[a]", "b=9", "c=2"]) + newline


def test_missing_trailing_newline_is_not_added():
    doc = IniDocument("[a]\nb=1", "utf-8", "\n", "x.ini")
    doc.set_value("b", "2")
    assert doc.to_text() == "[a]\nb=2"


# -- JSON --------------------------------------------------------------------

def test_json_keeps_the_original_value_type():
    doc = JsonDocument('{\n    "a": 1,\n    "b": true,\n    "c": "text"\n}\n', path="x.json")
    doc.set_value("a", "5")
    doc.set_value("b", "false")
    assert doc.data["a"] == 5 and doc.data["b"] is False
    assert '    "a": 5' in doc.to_text(), "indentation is detected and reused"


def test_json_refuses_new_keys_and_structures():
    doc = JsonDocument('{"a": {"b": 1}}', path="x.json")
    with pytest.raises(ConfigError):
        doc.set_value("nope", "1")
    with pytest.raises(ConfigError, match="structure"):
        doc.set_value("a", "1")
    doc.set_value("a.b", "2")
    assert doc.data["a"]["b"] == 2


def test_invalid_json_refuses_edits():
    doc = JsonDocument("{not json", path="x.json")
    assert not doc.parse_ok
    with pytest.raises(ConfigError):
        doc.set_value("a", "1")


def test_diff_shows_only_the_changed_line():
    doc = _doc()
    before = doc.to_text()
    doc.set_value("dup", "9")
    diff = unified_diff(before, doc.to_text(), "test.ini")
    body = [l for l in diff.splitlines()
            if not l.startswith(("+++", "---", "@@"))]
    assert [l for l in body if l.startswith("+")] == ["+dup=9"]
    assert [l for l in body if l.startswith("-")] == ["-dup=2"]
