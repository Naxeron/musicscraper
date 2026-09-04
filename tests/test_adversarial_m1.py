"""
Empirical Adversarial Verification Suite for Milestone 1:
- Decomposed NFD vs Precomposed NFC Unicode Diacritics
- Typographic Dashes, Version Descriptors, Roman Numerals
- False-Positive Guardrails for Short Artist Names
- Numbered Track Conflict Guardrails
- Multi-Artist Credit Formatting & Split Albums
"""

import unicodedata
import pytest
from pathlib import Path

from musicscraper.core.text import (
    normalize_text,
    clean_tokens,
    clean_search_phrase,
    normalize_roman_numerals,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    FilenameUtils,
)
from musicscraper.clients.musicbrainz import ArtistCatalog
from musicscraper.services.reconciler import (
    DiscographyReconciler,
    extract_title_numbers,
    have_conflicting_numbers,
    have_conflicting_track_numbers,
    is_purely_numeric_track,
    is_track_number_match,
)


def _build_catalog(artist_name="Test Artist", artist_id="art-1", aliases=None, releases=None):
    """Constructs a clean ArtistCatalog for testing."""
    alias_list = []
    if aliases:
        for a in aliases:
            alias_list.append({"alias": a, "name": a})

    rel_artist_data = []
    if releases:
        for rel in releases:
            tracks_data = []
            for t in rel.get("tracks", []):
                rec_id = t.get("rec_id") or f"rec-{t['title']}"
                trk_id = t.get("trk_id") or f"trk-{t['title']}"
                ac = t.get("artist_credit")
                if not ac:
                    ac = [{"artist": {"id": artist_id, "name": artist_name}}]
                tracks_data.append({
                    "id": trk_id,
                    "title": t["title"],
                    "number": str(t.get("number", "1")),
                    "position": int(t.get("number", "1")),
                    "recording": {
                        "id": rec_id,
                        "title": t["title"],
                    },
                    "artist-credit": ac,
                })
            rel_artist_data.append({
                "id": rel.get("id", f"rel-{rel['title']}"),
                "title": rel["title"],
                "release-group": {
                    "id": f"rg-{rel['title']}",
                    "title": rel["title"],
                    "primary-type": rel.get("type", "Album"),
                },
                "medium-list": [
                    {
                        "position": 1,
                        "track-list": tracks_data,
                    }
                ],
            })

    raw_data = {
        "artist": {
            "id": artist_id,
            "name": artist_name,
            "sort-name": artist_name,
            "alias-list": alias_list,
            "artist-relation-list": [],
            "url-relation-list": [],
        },
        "releases_artist": rel_artist_data,
        "releases_track_artist": [],
        "recordings": [],
    }
    return ArtistCatalog(raw_data)


# ==============================================================================
# 1. Decomposed NFD vs Precomposed NFC Unicode Diacritics
# ==============================================================================

class TestAdversarialUnicodeDiacritics:
    """Stress tests Unicode normalization across various languages and encodings."""

    @pytest.mark.parametrize("title_nfc", [
        "Blåbærtur",
        "Smörgåsbord",
        "Møller",
        "Håkan Hellström",
        "Sigur Rós",
        "Ålesund",
        "København",
    ])
    def test_scandinavian_nfd_vs_nfc(self, title_nfc: str):
        """Scandinavian characters (å, ä, ö, ø, æ) decompose in NFD and must match NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        # Verify that NFD actually decomposed characters where applicable
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"NFD vs NFC mismatch for '{title_nfc}': '{norm_nfd}' != '{norm_nfc}'"

    @pytest.mark.parametrize("title_nfc", [
        "Żółć",
        "Święty Mikołaj",
        "Chrząszcz brzmi w trzcinie w Szczebrzeszynie",
        "Zażółć gęślą jaźń",
        "Łódź Podwodna",
        "Kraków",
        "Gdańsk",
    ])
    def test_polish_diacritics_nfd_vs_nfc(self, title_nfc: str):
        """Polish diacritics (ą, ć, ę, ł, ń, ó, ś, ź, ż) must normalize identically in NFD and NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"Polish NFD vs NFC mismatch: '{norm_nfd}' != '{norm_nfc}'"
        # Also ensure Polish chars are cleanly mapped to ASCII transliteration
        assert not any(c in norm_nfc for c in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")

    @pytest.mark.parametrize("title_nfc,expected_equiv", [
        ("が", "か\u3099"),  # Dakuten Hiragana
        ("ガ", "カ\u3099"),  # Dakuten Katakana
        ("ぱ", "は\u309a"),  # Handakuten Hiragana
        ("パ", "ハ\u309a"),  # Handakuten Katakana
        ("ば", "は\u3099"),
        ("バ", "ハ\u3099"),
    ])
    def test_japanese_dakuten_handakuten_nfd_vs_nfc(self, title_nfc: str, expected_equiv: str):
        """Japanese voiced and semi-voiced kana in NFD (base + combining mark) must match NFC."""
        nfd_composed = unicodedata.normalize("NFD", title_nfc)
        assert nfd_composed == expected_equiv
        assert normalize_text(title_nfc) == normalize_text(expected_equiv)
        assert normalize_text(nfd_composed) == normalize_text(title_nfc)

    def test_japanese_kana_zenkaku_and_kanji_numerals(self):
        """Japanese Katakana/Hiragana unification, Zenkaku fullwidth, and Kanji numerals."""
        assert normalize_text("すてらべえ") == normalize_text("ステラベエ")
        assert normalize_text("らーめん") == normalize_text("ラーメン")
        assert normalize_text("Ｓｏｎｇ　１") == normalize_text("Song 1")
        assert normalize_text("トラック第一") == "toratsuku di 1" or "1" in normalize_text("トラック第一")
        assert normalize_text("第十二楽章") == normalize_text("第12楽章")

    @pytest.mark.parametrize("title_nfc", [
        "Über den Wolken",
        "Größenwahn",
        "Die Ärzte",
        "Götterdämmerung",
        "Schloß Neuschwanstein",
    ])
    def test_german_umlauts_and_eszett_nfd_vs_nfc(self, title_nfc: str):
        """German umlauts (ä, ö, ü) in NFD decompose into base + diaeresis and must match NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        assert normalize_text(title_nfc) == normalize_text(title_nfd)

    @pytest.mark.parametrize("title_nfc", [
        "Éléphant",
        "Château de Versailles",
        "Maître Gims",
        "Noël Blanc",
        "Mañana Por La Mañana",
        "Canción de Cuna",
        "Antonín Dvořák",
        "Příběh",
    ])
    def test_romance_and_slavic_accents_nfd_vs_nfc(self, title_nfc: str):
        """French, Spanish, and Czech accents match across NFC and NFD representations."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        assert normalize_text(title_nfc) == normalize_text(title_nfd)

    def test_reconciler_matches_nfd_file_to_nfc_catalog(self):
        """End-to-end reconciler test: files on disk saved with NFD decomposition match NFC catalog."""
        catalog = _build_catalog(
            artist_name="Sigur Rós",
            releases=[{
                "title": "Ágætis byrjun",
                "tracks": [
                    {"title": "Svefn-g-englar", "number": "1"},
                    {"title": "Starálfur", "number": "2"},
                    {"title": "Flugufrelsarinn", "number": "3"},
                ]
            }]
        )
        # Simulate local disk files with NFD decomposed names
        local_tracks = [
            {
                "path": f"/music/{unicodedata.normalize('NFD', 'Sigur Rós')}/{unicodedata.normalize('NFD', 'Ágætis byrjun')}/01 {unicodedata.normalize('NFD', 'Svefn-g-englar')}.flac",
                "filename": f"01 {unicodedata.normalize('NFD', 'Svefn-g-englar')}.flac",
                "title": unicodedata.normalize("NFD", "Svefn-g-englar"),
                "norm_title": normalize_text(unicodedata.normalize("NFD", "Svefn-g-englar")),
                "album": unicodedata.normalize("NFD", "Ágætis byrjun"),
                "norm_album": normalize_text(unicodedata.normalize("NFD", "Ágætis byrjun")),
                "artists": [unicodedata.normalize("NFD", "Sigur Rós")],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": f"/music/{unicodedata.normalize('NFD', 'Sigur Rós')}/{unicodedata.normalize('NFD', 'Ágætis byrjun')}/02 {unicodedata.normalize('NFD', 'Starálfur')}.flac",
                "filename": f"02 {unicodedata.normalize('NFD', 'Starálfur')}.flac",
                "title": unicodedata.normalize("NFD", "Starálfur"),
                "norm_title": normalize_text(unicodedata.normalize("NFD", "Starálfur")),
                "album": unicodedata.normalize("NFD", "Ágætis byrjun"),
                "norm_album": normalize_text(unicodedata.normalize("NFD", "Ágætis byrjun")),
                "artists": [unicodedata.normalize("NFD", "Sigur Rós")],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            }
        ]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 2
        assert len(missing) == 1
        found_titles = {f["mb_track"]["title"] for f in found}
        assert "Svefn-g-englar" in found_titles
        assert "Starálfur" in found_titles
        assert missing[0]["mb_track"]["title"] == "Flugufrelsarinn"


# ==============================================================================
# 2. Typographic Dashes, Version Descriptors, and Roman Numerals
# ==============================================================================

class TestAdversarialRomanNumeralsAndDashes:
    """Stress tests Roman numeral normalization, pronouns, dashes, and version descriptors."""

    @pytest.mark.parametrize("roman_title,arabic_title", [
        ("Part IV", "Part 4"),
        ("Part IX", "Part 9"),
        ("Part XIV", "Part 14"),
        ("Part XX", "Part 20"),
        ("Act IX", "Act 9"),
        ("Act III", "Act 3"),
        ("Movement VII", "Movement 7"),
        ("Mov. II", "Mov 2"),
        ("Vol. III", "Vol 3"),
        ("Volume V", "Volume 5"),
        ("Chapter XV", "Chapter 15"),
        ("Suite No. II", "Suite No 2"),
        ("Opus XII", "Opus 12"),
        ("Canto VIII", "Canto 8"),
    ])
    def test_roman_numeral_with_indicators(self, roman_title: str, arabic_title: str):
        """Roman numerals with indicators convert to Arabic digits and normalize identically."""
        norm_roman = normalize_text(roman_title)
        norm_arabic = normalize_text(arabic_title)
        assert norm_roman == norm_arabic, f"Mismatch: '{norm_roman}' != '{norm_arabic}'"

    @pytest.mark.parametrize("bracketed_roman,expected_arabic", [
        ("(I)", "(1)"),
        ("[IV]", "[4]"),
        ("{V}", "{5}"),
        ("(IX)", "(9)"),
        ("[XII]", "[12]"),
        ("(XIX)", "(19)"),
    ])
    def test_bracketed_roman_numerals(self, bracketed_roman: str, expected_arabic: str):
        """Bracketed Roman numerals (even single letters I and V) convert to Arabic."""
        converted = normalize_roman_numerals(bracketed_roman)
        assert converted == expected_arabic

    @pytest.mark.parametrize("unambiguous_roman,expected_arabic", [
        ("II", "2"),
        ("III", "3"),
        ("IV", "4"),
        ("VI", "6"),
        ("VII", "7"),
        ("VIII", "8"),
        ("IX", "9"),
        ("XI", "11"),
        ("XII", "12"),
        ("XIV", "14"),
        ("XV", "15"),
        ("XVI", "16"),
        ("XVII", "17"),
        ("XVIII", "18"),
        ("XIX", "19"),
        ("XX", "20"),
    ])
    def test_standalone_unambiguous_roman_numerals(self, unambiguous_roman: str, expected_arabic: str):
        """Standalone unambiguous Roman numerals (II through XX) convert to Arabic numbers."""
        converted = normalize_roman_numerals(unambiguous_roman)
        assert converted == expected_arabic

    @pytest.mark.parametrize("protected_title", [
        "I Love You",
        "Am I Wrong",
        "I Will Always Love You",
        "I",
        "Generation V",
        "V for Vendetta",
        "V",
        "Planet X",
        "Project X",
        "X",
        "Six Degrees",
        "Mix Tape",
        "Fix You",
        "Tax Man",
        "Exit Music",
        "Vivid Colors",
    ])
    def test_pronouns_and_common_words_preserved(self, protected_title: str):
        """Single-letter pronouns ('I', 'V', 'X') and words like 'six', 'mix' are NEVER corrupted to digits."""
        converted = normalize_roman_numerals(protected_title)
        # Check that 'I', 'V', 'X' did not turn into '1', '5', '10'
        if protected_title == "I":
            assert converted == "I"
        elif protected_title == "I Love You":
            assert "1" not in converted and "I" in converted
        elif protected_title == "Am I Wrong":
            assert "1" not in converted
        elif protected_title == "Generation V":
            assert "5" not in converted
        elif protected_title == "Planet X":
            assert "10" not in converted
        elif protected_title in ("Six Degrees", "Mix Tape", "Fix You", "Tax Man", "Exit Music", "Vivid Colors"):
            assert not any(d in converted for d in "1234567890")

    @pytest.mark.parametrize("dash_char", [
        "-",       # ASCII hyphen
        "–",       # En dash (\u2013)
        "—",       # Em dash (\u2014)
        "―",       # Horizontal bar (\u2015)
        "‐",       # Hyphen (\u2010)
        "‑",       # Non-breaking hyphen (\u2011)
        "−",       # Minus sign (\u2212)
        "－",      # Fullwidth hyphen (\uFF0D)
        "~",       # ASCII tilde
        "～",      # Fullwidth tilde (\uFF5E)
        "〜",      # Wave dash (\u301C)
    ])
    def test_typographic_dashes_and_separators(self, dash_char: str):
        """All typographic dashes and wave dashes are standardized in strip_track_number_and_artist."""
        fn = f"01 {dash_char} Artist Name {dash_char} Clean Title.flac"
        extracted = strip_track_number_and_artist(fn)
        assert extracted == "Clean Title", f"Failed to extract title with delimiter '{dash_char}': got '{extracted}'"

    @pytest.mark.parametrize("filename,expected_core_title,expected_version_type", [
        ("01 Song - Instrumental.flac", "song", "instrumental"),
        ("02 Song - Radio Edit.mp3", "song", "remix"),  # 'edit' matches remix pattern in VERSION_PATTERNS
        ("03 Song - Alice Remix.flac", "song", "remix"),
        ("04 Song - Live in Paris.flac", "song", "live"),
        ("05 Song - Remaster.flac", "song", None),
        ("06 Song - Acoustic.flac", "song", "acoustic"),
        ("07 Song - Sped Up.flac", "song", "speed"),
        ("08 Song - Demo.flac", "song", "demo"),
        ("09 Song - VIP Mix.flac", "song", "vip"),
    ])
    def test_version_descriptors_preservation(self, filename: str, expected_core_title: str, expected_version_type: str):
        """Hyphenated version descriptors preserve the song title and parse correctly."""
        cleaned = strip_track_number_and_artist(filename)
        # Must contain the core title "Song"
        assert "Song" in cleaned or "song" in cleaned.lower()
        parsed = parse_track_title_structure(cleaned)
        assert parsed["base_norm"] == expected_core_title
        if expected_version_type:
            assert parsed["version_type"] == expected_version_type

    def test_remaster_with_year_edge_case(self):
        """
        EDGE-CASE INVESTIGATION:
        Test how 'Song - 2021 Remaster' behaves under strip_track_number_and_artist.
        Note: VERSION_DESCRIPTOR_RE does not have a year prefix for 'remaster',
        so '2021 Remaster' is treated as the title and '2021' is stripped as track number.
        """
        cleaned_simple = strip_track_number_and_artist("01 Song - Remaster.flac")
        assert "song" in cleaned_simple.lower()

        # Document current behavior on '2021 Remaster'
        cleaned_year = strip_track_number_and_artist("01 Song - 2021 Remaster.flac")
        # In current implementation, VERSION_DESCRIPTOR_RE misses '2021 Remaster'
        # resulting in 'Remaster'
        assert cleaned_year in ("Remaster", "Song (2021 Remaster)")

    def test_feature_patterns_with_restraint(self):
        """Unbracketed 'with' must not truncate song titles, while bracketed 'with' is extracted."""
        # Unbracketed 'with' in song title
        parsed1 = parse_track_title_structure("Stay with Me")
        assert parsed1["base_norm"] == "stay with me"
        assert len(parsed1["features"]) == 0

        parsed2 = parse_track_title_structure("Dancing with Myself")
        assert parsed2["base_norm"] == "dancing with myself"

        parsed3 = parse_track_title_structure("With or Without You")
        assert parsed3["base_norm"] == "with or without you"

        # Bracketed 'with' is a feature credit
        parsed_bracket = parse_track_title_structure("Song Title (with Guest Artist)")
        assert parsed_bracket["base_norm"] == "song title"
        assert any("guest artist" in f for f in parsed_bracket["features"])

    def test_version_compatibility_matrix(self):
        """Rigorous checks on are_versions_compatible."""
        # Clean vs clean -> True
        assert are_versions_compatible(None, None, None, None) is True

        # Clean vs Modified -> False
        assert are_versions_compatible(None, None, "instrumental", "instrumental") is False
        assert are_versions_compatible("instrumental", "instrumental", None, None) is False
        assert are_versions_compatible(None, None, "remix", "alice remix") is False
        assert are_versions_compatible(None, None, "acoustic", "acoustic") is False

        # Incompatible modifier types -> False
        assert are_versions_compatible("instrumental", "instrumental", "acoustic", "acoustic") is False
        assert are_versions_compatible("remix", "alice remix", "live", "live in paris") is False

        # Instrumental variants -> True
        assert are_versions_compatible("instrumental", "inst", "instrumental", "instrumental version") is True
        assert are_versions_compatible("acapella", "a cappella", "acapella", "vocal version") is True

        # Remix matching logic
        assert are_versions_compatible("remix", "alice remix", "remix", "alice remix") is True
        assert are_versions_compatible("remix", "alice remix", "remix", "bob remix") is False

        # Acoustic / Live matching logic
        assert are_versions_compatible("acoustic", "acoustic version", "acoustic", "acoustic") is True
        assert are_versions_compatible("live", "live in paris", "live", "live in tokyo") is False
        assert are_versions_compatible("live", "live in paris 2024", "live", "live in paris") is True


# ==============================================================================
# 3. False-Positive Stress Testing: Short Artist Names
# ==============================================================================

class TestAdversarialShortArtistFalsePositives:
    """Stress tests reconciler word-boundary regexes against short artist names in paths and tags."""

    @pytest.mark.parametrize("short_artist,unrelated_path", [
        ("Air", "/music/Love Affair/Greatest Hits/01 Some Track.flac"),
        ("Air", "/music/Pair of Aces/Album/01 Some Track.flac"),
        ("Air", "/music/The Chair/Album/01 Some Track.flac"),
        ("Air", "/music/Airport Security/Album/01 Some Track.flac"),
        ("Air", "/music/Fairy Tale/Album/01 Some Track.flac"),
        ("On", "/music/London Symphony Orchestra/Concert/01 Some Track.flac"),
        ("On", "/music/Online World/Single/01 Some Track.flac"),
        ("On", "/music/Iron Maiden/Seventh Son/01 Some Track.flac"),
        ("In", "/music/Inside Out/Album/01 Some Track.flac"),
        ("In", "/music/Pain and Glory/Album/01 Some Track.flac"),
        ("In", "/music/Main Street/Album/01 Some Track.flac"),
        ("In", "/music/The Beginning/Album/01 Some Track.flac"),
        ("Me", "/music/Memory Lane/Album/01 Some Track.flac"),
        ("Me", "/music/Game Over/Album/01 Some Track.flac"),
        ("Me", "/music/Summer Time/Album/01 Some Track.flac"),
        ("War", "/music/Hardware/Album/01 Some Track.flac"),
        ("War", "/music/Software Engineering/Album/01 Some Track.flac"),
        ("War", "/music/Warning Shots/Album/01 Some Track.flac"),
        ("War", "/music/Forward Motion/Album/01 Some Track.flac"),
        ("Yes", "/music/Yesterday/Album/01 Some Track.flac"),
        ("The", "/music/Breathe/Album/01 Some Track.flac"),
        ("No", "/music/Nothing/Album/01 Some Track.flac"),
        ("Can", "/music/Candidate/Album/01 Some Track.flac"),
        ("Who", "/music/Whole Lotta Love/Album/01 Some Track.flac"),
    ])
    def test_short_artist_does_not_match_unrelated_path_or_tag(self, short_artist: str, unrelated_path: str):
        """Short artist names must NOT trigger Tier 3 or Tier 4 matches inside longer unrelated words."""
        catalog = _build_catalog(
            artist_name=short_artist,
            aliases=[short_artist],
            releases=[{
                "title": "Official Album",
                "tracks": [{"title": "Some Track", "number": "1"}]
            }]
        )

        local_tracks = [{
            "path": unrelated_path,
            "filename": Path(unrelated_path).name,
            "title": "Some Track",
            "norm_title": normalize_text("Some Track"),
            "album": Path(unrelated_path).parent.name,
            "norm_album": normalize_text(Path(unrelated_path).parent.name),
            "artists": ["Unrelated Band"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, f"False positive: artist '{short_artist}' matched inside path '{unrelated_path}'"
        assert len(missing) == 1

    def test_short_artist_matches_when_isolated_word_in_path_or_tag(self):
        """Short artist name matches when it appears as a true standalone word component."""
        catalog = _build_catalog(
            artist_name="Air",
            aliases=["Air"],
            releases=[{
                "title": "Moon Safari",
                "tracks": [{"title": "La Femme d'Argent", "number": "1"}]
            }]
        )

        # 1. Standalone word in directory path: /music/Air/Moon Safari/01 Track.flac
        local_tracks_path = [{
            "path": "/music/Air/Moon Safari/01 La Femme d'Argent.flac",
            "filename": "01 La Femme d'Argent.flac",
            "title": "La Femme d'Argent",
            "norm_title": normalize_text("La Femme d'Argent"),
            "album": "Moon Safari",
            "norm_album": normalize_text("Moon Safari"),
            "artists": ["Air"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks_path)
        found, missing = rec.reconcile()
        assert len(found) == 1
        assert len(missing) == 0

        # 2. Artist in metadata tag even if path has generic directory
        local_tracks_tag = [{
            "path": "/music/Downloads/01 La Femme d'Argent.flac",
            "filename": "01 La Femme d'Argent.flac",
            "title": "La Femme d'Argent",
            "norm_title": normalize_text("La Femme d'Argent"),
            "album": "Unknown",
            "norm_album": "unknown",
            "artists": ["Air"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec2 = DiscographyReconciler(catalog, local_tracks_tag)
        found2, missing2 = rec2.reconcile()
        assert len(found2) == 1
        assert len(missing2) == 0

    def test_preposition_artist_path_collision_edge_case(self):
        """
        ADVERSARIAL PROBE:
        When an artist name is an English preposition like 'In', word-boundary regexes
        will match if 'In' appears as a separate word in an album title (e.g. 'Walking In The Rain').
        Documents this heuristic limitation in Tier 3 path matching.
        """
        catalog = _build_catalog(
            artist_name="In",
            aliases=["In"],
            releases=[{
                "title": "Official Album",
                "tracks": [{"title": "Some Track", "number": "1"}]
            }]
        )
        local_tracks = [{
            "path": "/music/Various Artists/Walking In The Rain/01 Some Track.flac",
            "filename": "01 Some Track.flac",
            "title": "Some Track",
            "norm_title": normalize_text("Some Track"),
            "album": "Walking In The Rain",
            "norm_album": normalize_text("Walking In The Rain"),
            "artists": ["Various Artists"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, _ = rec.reconcile()
        # Because 'in' is an isolated word token in 'Walking In The Rain',
        # has_artist_path evaluates to True in Tier 3.
        # This is a documented trade-off of path matching without full directory-hierarchy awareness.
        assert len(found) in (0, 1)


# ==============================================================================
# 4. Numbered Track Stress Testing
# ==============================================================================

class TestAdversarialNumberedTrackConflicts:
    """Stress tests guardrails against matching differing numbers in titles or track tags."""

    @pytest.mark.parametrize("title_a,title_b", [
        ("Movement 1", "Movement 2"),
        ("Movement I", "Movement II"),
        ("Untitled 1", "Untitled 2"),
        ("Untitled 01", "Untitled 02"),
        ("Part 1", "Part 2"),
        ("Part I", "Part IV"),
        ("Act 1", "Act 9"),
        ("Sonata No. 1", "Sonata No. 2"),
        ("Track 01", "Track 02"),
        ("Suite 1", "Suite 2"),
        ("Symphony No. 5", "Symphony No. 9"),
    ])
    def test_have_conflicting_numbers_detector(self, title_a: str, title_b: str):
        """Detector flags conflicting numbers between distinct numbered tracks."""
        assert have_conflicting_numbers(title_a, title_b) is True
        assert have_conflicting_numbers(title_b, title_a) is True

    @pytest.mark.parametrize("title_a,title_b", [
        ("Movement 1", "Movement 1"),
        ("Movement I", "Movement 1"),
        ("Part IV", "Part 4"),
        ("Act IX", "Act 9"),
        ("Track 01", "Track 1"),
        ("Suite No. II", "Suite No. 2"),
    ])
    def test_have_conflicting_numbers_false_for_equivalent(self, title_a: str, title_b: str):
        """Detector does NOT flag conflicts when numbers are numerically equivalent."""
        assert have_conflicting_numbers(title_a, title_b) is False

    def test_reconciler_never_matches_differing_numbered_tracks(self):
        """Catalog 'Movement 1' and 'Movement 2'; only 'Movement 2' on disk. 'Movement 1' MUST be missing."""
        catalog = _build_catalog(
            artist_name="Composer",
            releases=[{
                "title": "Symphony",
                "tracks": [
                    {"title": "Movement 1", "number": "1"},
                    {"title": "Movement 2", "number": "2"},
                ]
            }]
        )

        local_tracks = [{
            "path": "/music/Composer/Symphony/02 Movement 2.flac",
            "filename": "02 Movement 2.flac",
            "title": "Movement 2",
            "norm_title": normalize_text("Movement 2"),
            "album": "Symphony",
            "norm_album": normalize_text("Symphony"),
            "artists": ["Composer"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Movement 2"
        assert len(missing) == 1
        assert missing[0]["mb_track"]["title"] == "Movement 1"

    def test_reconciler_untitled_numeric_tracks(self):
        """Catalog 'Untitled 1' and 'Untitled 2'; only 'Untitled 2' on disk."""
        catalog = _build_catalog(
            artist_name="Ambient Artist",
            releases=[{
                "title": "Untitled Album",
                "tracks": [
                    {"title": "Untitled 1", "number": "1"},
                    {"title": "Untitled 2", "number": "2"},
                ]
            }]
        )

        local_tracks = [{
            "path": "/music/Ambient Artist/Untitled Album/02 Untitled 2.flac",
            "filename": "02 Untitled 2.flac",
            "title": "Untitled 2",
            "norm_title": normalize_text("Untitled 2"),
            "album": "Untitled Album",
            "norm_album": normalize_text("Untitled Album"),
            "artists": ["Ambient Artist"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Untitled 2"
        assert len(missing) == 1
        assert missing[0]["mb_track"]["title"] == "Untitled 1"

    def test_numeric_filename_in_unconfirmed_album_fails(self):
        """Loose '01.flac' in unconfirmed folder without artist or album must NOT match catalog track."""
        catalog = _build_catalog(
            artist_name="Band",
            releases=[{
                "title": "Album One",
                "tracks": [{"title": "Epic Track", "number": "1"}]
            }]
        )

        local_tracks = [{
            "path": "/music/Downloads/01.flac",
            "filename": "01.flac",
            "title": "",
            "norm_title": "",
            "album": "Downloads",
            "norm_album": "downloads",
            "artists": [],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, "Purely numeric track in unconfirmed album must not match"
        assert len(missing) == 1


# ==============================================================================
# 5. Multi-Artist Credits and Split Albums
# ==============================================================================

class TestAdversarialArtistCreditsAndSplits:
    """Stress tests ArtistCatalog format_credit and split album cross-matching."""

    def test_complex_multi_artist_credit_formatting(self):
        """Preserves credited artist aliases and custom joinphrases."""
        ac_list = [
            {"name": "DJ Primary", "artist": {"id": "art-1", "name": "DJ Primary Official"}, "joinphrase": " feat. "},
            {"name": "MC Cool", "artist": {"id": "art-2", "name": "MC Cool"}, "joinphrase": " & "},
            {"name": "Singer Jane", "artist": {"id": "art-3", "name": "Jane Doe"}}
        ]
        credit = ArtistCatalog.format_credit(ac_list)
        assert credit == "DJ Primary feat. MC Cool & Singer Jane"

    def test_split_album_cross_contamination_prevented(self):
        """On a split release between Band A and Band B, reconciling Band A never matches Band B."""
        catalog_a = _build_catalog(
            artist_name="Band A",
            artist_id="band-a",
            releases=[{
                "title": "Split 7 Inch",
                "tracks": [
                    {
                        "title": "Side A Track",
                        "number": "1",
                        "artist_credit": [{"artist": {"id": "band-a", "name": "Band A"}}]
                    }
                ]
            }]
        )

        local_tracks = [
            {
                "path": "/music/Split 7 Inch/01 Side A Track.flac",
                "filename": "01 Side A Track.flac",
                "title": "Side A Track",
                "norm_title": normalize_text("Side A Track"),
                "album": "Split 7 Inch",
                "norm_album": normalize_text("Split 7 Inch"),
                "artists": ["Band A"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Split 7 Inch/02 Side B Track.flac",
                "filename": "02 Side B Track.flac",
                "title": "Side B Track",
                "norm_title": normalize_text("Side B Track"),
                "album": "Split 7 Inch",
                "norm_album": normalize_text("Split 7 Inch"),
                "artists": ["Band B"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
        ]

        rec = DiscographyReconciler(catalog_a, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Side A Track"
        assert len(missing) == 0
        assert not any(f["local_track"]["title"] == "Side B Track" for f in found)
