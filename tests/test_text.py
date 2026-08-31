"""
Unit tests for core text processing, Japanese NLP, fuzzy matching, and filename utils.
"""

import pytest
from musicscraper.core.text import (
    normalize_text,
    clean_tokens,
    kanji_to_arabic,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    FilenameUtils,
    is_sublist,
    clean_search_phrase,
)


def test_clean_search_phrase_unicode():
    assert clean_search_phrase("すてらべえ Breakcore Forever") == "すてらべえ Breakcore Forever"
    assert clean_search_phrase("Stellabee - Breakcore Forever [2021]") == "Stellabee - Breakcore Forever 2021"
    assert clean_search_phrase("Кино - Группа крови (1988)") == "Кино - Группа крови 1988"


def test_normalize_text_basic():
    assert normalize_text("  The   Quick--Brown...Fox  ") == "the quick brown fox"
    assert normalize_text("Café") == "cafe"


def test_normalize_text_symbols():
    assert normalize_text("]") == "]"
    assert normalize_text("???") == "???"
    assert normalize_text("***") == "***"



def test_kanji_to_arabic():
    assert kanji_to_arabic("三") == "3"
    assert kanji_to_arabic("十二") == "12"
    assert kanji_to_arabic("第一") == "第1"


def test_strip_track_number_and_artist():
    assert strip_track_number_and_artist("01. Track Title") == "Track Title"
    assert strip_track_number_and_artist("02 - Artist - Song Name") == "Song Name"
    assert strip_track_number_and_artist("1-05 Some Song") == "Some Song"


def test_calculate_similarity():
    assert calculate_similarity("hello world", "hello world") == 1.0
    assert calculate_similarity("hello world", "hello world!") > 0.9
    assert calculate_similarity("apple", "orange") < 0.5


def test_parse_track_title_structure():
    parsed = parse_track_title_structure("Awesome Song (Instrumental Version)")
    assert parsed["base_norm"] == "awesome song"
    assert parsed["version_type"] == "instrumental"

    parsed_remix = parse_track_title_structure("Awesome Song (DJ Cool Remix)")
    assert parsed_remix["base_norm"] == "awesome song"
    assert parsed_remix["version_type"] == "remix"


def test_are_versions_compatible():
    assert are_versions_compatible("original", "", "original", "") is True
    assert are_versions_compatible("instrumental", "", "original", "") is False
    assert are_versions_compatible("remix", "dj cool remix", "remix", "dj cool remix") is True
    assert are_versions_compatible("remix", "dj cool remix", "remix", "dj bob remix") is False


def test_filename_utils():
    sanitized = FilenameUtils.sanitize('Artist: "Song" / 2026? <test>')
    assert sanitized == "Artist_ _Song_ _ 2026_ _test_"
    assert FilenameUtils.clean_spaces("Too   Many    Spaces") == "Too Many Spaces"


def test_is_sublist():
    assert is_sublist(["a", "b"], ["x", "a", "b", "y"]) is True
    assert is_sublist(["a", "c"], ["x", "a", "b", "y"]) is False
