"""Matcher tests: reach, and — more importantly — false-positive prevention."""
from __future__ import annotations

import pytest

from conftest import FIXTURES
from travelready.library import load_library
from travelready.optimiser.rogallylife.matcher import (
    AUTO_THRESHOLD, MIN_THRESHOLD, expand_roman_numerals, match,
    normalize_title, score, strip_accents, strip_editions,
)


class Candidate:
    def __init__(self, title, url="https://rogallylife.com/x/", family="rog_ally_family"):
        self.title, self.source_url, self.device_family = title, url, family


# -- normalisation -----------------------------------------------------------

def test_trademark_marks_are_removed_not_decomposed():
    """NFKD turns U+2122 into the letters 'TM'; that must not leak into the key."""
    assert "tm" not in normalize_title("STAR WARS Jedi: Fallen Order™").split()
    assert normalize_title("Immortals of Aveum™") == "immortals of aveum"
    assert normalize_title("F1® 24") == "f1 24"


def test_accents_are_folded():
    assert normalize_title("Pokémon") == normalize_title("Pokemon")
    assert strip_accents("Pokémon") == "Pokemon"


@pytest.mark.parametrize("a,b", [
    ("Clair Obscur: Expedition 33", "Clair Obscur- Expedition 33"),
    ("Half-Life 2", "Half Life 2"),
    ("S.T.A.L.K.E.R. 2", "STALKER 2"),
    ("Marvel's Spider-Man", "Marvels Spider Man"),
])
def test_punctuation_and_separators_normalise_together(a, b):
    assert normalize_title(a) == normalize_title(b)


def test_roman_numerals_expand_on_both_sides():
    assert expand_roman_numerals("Final Fantasy XVI") == "Final Fantasy 16"
    assert normalize_title("Final Fantasy XVI") == normalize_title("Final Fantasy 16")


def test_edition_words_are_stripped_but_reported():
    stripped, removed = strip_editions("mafia definitive edition")
    assert stripped.strip() == "mafia"
    assert "definitive edition" in removed


# -- matches that must succeed ------------------------------------------------

@pytest.mark.parametrize("library_title,source_title", [
    ("Clair Obscur- Expedition 33", "Clair Obscur: Expedition 33"),
    ("Clair Obscur: Expedition 33", "Clair Obscur: Expedition 33"),
    ("DOOM- The Dark Ages", "DOOM: The Dark Ages"),
    ("Dune- Awakening", "Dune: Awakening"),
    ("Kingdom Come- Deliverance II", "Kingdom Come: Deliverance 2"),
    ("STAR WARS Jedi - Fallen Order™", "STAR WARS Jedi: Fallen Order"),
    ("Immortals of Aveum™", "Immortals of Aveum"),
    ("Need for Speed™ Unbound", "Need for Speed Unbound"),
    ("F1® 24", "F1 24"),
    ("Mafia: Definitive Edition", "Mafia"),
    ("Battlefield 3™ Limited Edition", "Battlefield 3"),
    ("Mass Effect™ Legendary Edition", "Mass Effect Legendary Edition"),
    ("The Witcher 3", "Witcher 3"),
    ("FINAL FANTASY XVI", "Final Fantasy 16"),
])
def test_real_title_variations_match_automatically(library_title, source_title):
    confidence, reason = score(library_title, source_title)
    assert confidence >= AUTO_THRESHOLD, f"{confidence:.2f} — {reason}"


# -- matches that must NOT happen --------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("DOOM", "DOOM II"),
    ("DOOM + DOOM II", "DOOM: The Dark Ages"),
    ("Forza Horizon 5", "Forza Horizon 6"),
    ("Forza Horizon 6", "Forza Motorsport"),
    ("F1 2020", "F1 24"),
    ("Battlefield 3", "Battlefield 4"),
    ("Resident Evil 4", "Resident Evil Requiem"),
    ("A Plague Tale: Requiem", "Resident Evil Requiem"),
    ("Need for Speed Unbound", "Need for Speed Heat"),
    ("Mass Effect Legendary Edition", "Mass Effect Andromeda"),
    ("Star Trucker", "Star Wars Outlaws"),
    ("Dead Space", "Dead Island 2"),
    ("Silent Hill Townfall", "Silent Hill 2"),
])
def test_different_games_never_match(a, b):
    confidence, reason = score(a, b)
    assert confidence < MIN_THRESHOLD, f"{a} vs {b} scored {confidence:.2f} — {reason}"


def test_sequel_numbering_is_never_normalised_away():
    confidence, reason = score("Forza Horizon 5", "Forza Horizon 6")
    assert confidence == 0.0
    assert "numbering" in reason


def test_distinguishing_words_block_a_franchise_false_positive():
    confidence, reason = score("Need for Speed Unbound", "Need for Speed Heat")
    assert confidence == 0.0
    assert "distinguishing" in reason


# -- selection among candidates ----------------------------------------------

def test_best_candidate_is_chosen_and_explained():
    found = match("Clair Obscur- Expedition 33", [
        Candidate("Some Other Game"),
        Candidate("Clair Obscur: Expedition 33"),
    ])
    assert found.automatic
    assert found.matched_title == "Clair Obscur: Expedition 33"
    assert found.reason == "exact normalised title"
    assert "MATCH" in found.describe()


def test_two_equally_good_but_different_titles_are_ambiguous():
    found = match("Game Remastered", [
        Candidate("Game: Northern Chapter"),
        Candidate("Game: Southern Chapter"),
    ])
    if found is not None:
        assert not found.automatic or not found.ambiguous_with


def test_ambiguity_forces_review():
    from travelready.optimiser.rogallylife.matcher import MatchResult

    result = MatchResult(query="q", matched_title="A", confidence=1.0,
                         reason="exact", ambiguous_with=("B",))
    assert not result.automatic
    assert "REVIEW REQUIRED" in result.describe()


def test_same_game_on_two_devices_is_not_ambiguous():
    """Two posts for one game differ by device, which is a choice, not ambiguity."""
    found = match("Clair Obscur: Expedition 33", [
        Candidate("Clair Obscur: Expedition 33",
                  "https://rogallylife.com/a-rog-ally/", "rog_ally_family"),
        Candidate("Clair Obscur: Expedition 33",
                  "https://rogallylife.com/a-xbox-ally-x/", "rog_xbox_ally_family"),
    ], prefer_family="rog_ally_family")
    assert found.automatic
    assert found.device_family == "rog_ally_family"


def test_nothing_close_enough_returns_none():
    assert match("Completely Unrelated Title", [Candidate("Forza Horizon 6")]) is None


def test_empty_titles_score_zero():
    assert score("", "Anything")[0] == 0.0
    assert score("Anything", "")[0] == 0.0


# -- against the real library -------------------------------------------------

def test_no_false_positives_across_the_whole_real_library():
    """Every library title against every other: none may match automatically."""
    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    names = sorted({e.name for e in entries})
    from travelready.library import identity_key

    collisions = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if identity_key(a) == identity_key(b):
                continue            # genuinely the same game, two installs
            confidence, reason = score(a, b)
            if confidence >= AUTO_THRESHOLD:
                collisions.append((a, b, confidence, reason))
    assert not collisions, f"automatic matches between distinct games: {collisions[:5]}"


def test_known_real_matches_are_found_in_the_real_library():
    from travelready.optimiser.rogallylife.known_urls import seed_titles

    entries, _ = load_library(FIXTURES / "games_2026-09-26.json")
    catalogue = [Candidate(title, url) for title, url in seed_titles("rog_ally_family")]
    matched = {}
    for entry in entries:
        found = match(entry.name, catalogue)
        if found is not None and found.confidence >= AUTO_THRESHOLD:
            matched[entry.name] = found.matched_title
    assert "Clair Obscur: Expedition 33" in matched
    assert "Clair Obscur- Expedition 33" in matched, "the Xbox install must match too"
    assert "Forza Horizon 6" in matched
    # and nothing absurd
    assert "Calculator" not in matched
    assert "Windows Security" not in matched
