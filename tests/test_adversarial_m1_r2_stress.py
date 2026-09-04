"""
Adversarial Stress Test Harness for Milestone 1 Hardening (Round 2 Verification):
- Stress testing Unicode diacritics (NFC/NFD), complex scripts, combining marks
- Stress testing Roman numerals, indicators, edge cases, pronoun preservation
- Stress testing typographic dashes, wave dashes, version descriptors
- Stress testing flat multi-disc structures and disc prefixes (1-01, 2-01, B1, Side B, Disc 2 - 01)
- Stress testing sequence gap detection and MusicBrainz reconciliation across multi-disc setups
"""

import unicodedata
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from musicscraper.core.text import (
    normalize_text,
    clean_tokens,
    clean_search_phrase,
    normalize_roman_numerals,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
)
from musicscraper.core.audio import (
    AudioMetadata,
    AudioQualityAnalyzer,
    DISC_DIR_PATTERN,
)
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.services.library import (
    LibraryReleaseService,
    parse_disc_and_track_number,
)
from musicscraper.services.reconciler import (
    DiscographyReconciler,
    is_purely_numeric_track,
    is_track_number_match,
    have_conflicting_numbers,
    have_conflicting_track_numbers,
)
from musicscraper.clients.musicbrainz import ArtistCatalog


# ==============================================================================
# SECTION 1: Adversarial Unicode Diacritics (NFC vs NFD, Combining Marks)
# ==============================================================================

class TestAdversarialUnicodeDiacriticsStress:
    """Stress tests Unicode normalization across global languages, decomposed marks, and edge cases."""

    @pytest.mark.parametrize("title_nfc", [
        # Vietnamese (nested diacritics)
        "Tiếng Việt",
        "Đường về quê mẹ",
        "Nguyễn Du",
        "Khúc Ca Mùa Thu",
        "Biển Nhớ",
        "Ở Trọ",
        "Hạ Trắng",
        # Nordic & Scandinavian
        "Þórsdrápa",
        "Árstíðir",
        "Sigur Rós",
        "Múm",
        "Blåbærtur",
        "Røyksopp",
        "Björk Guðmundsdóttir",
        # Baltic & Slavic
        "Žuvų Šokis",
        "Lietuvos Rytas",
        "Rīgas Vējš",
        "Zażółć gęślą jaźń",
        "Dvořák Antonín",
        "Leoš Janáček",
        "Bedřich Smetana",
        # Turkish
        "İstanbul Türküsü",
        "Diyarbakır",
        "Gümüşhane",
        "Aşık Veysel",
        # German / French / Spanish
        "Götterdämmerung",
        "Größenwahn",
        "Français Élégant",
        "España Cañí",
        "Corazón Espinado",
        # Greek
        "Ὀδύσσεια",
        "Μίκης Θεοδωράκης",
        # Cyrillic
        "Пётр Ильич Чайковский",
        "Модест Мусоргский",
    ])
    def test_nfd_vs_nfc_equivalence_across_languages(self, title_nfc: str):
        """Precomposed NFC and decomposed NFD representations must normalize identically."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"Mismatch for '{title_nfc}': NFC '{norm_nfc}' != NFD '{norm_nfd}'"

    def test_combining_diacritics_without_base_and_zero_width_chars(self):
        """Zero-width spaces, joiners, and combining characters do not crash normalize_text."""
        dirty = "Song\u200B \u200C\u200DTitle\uFEFF"
        clean = normalize_text(dirty)
        assert clean == "song title"

        # Trailing combining acute mark alone
        lone_combining = "Title\u0301"
        assert normalize_text(lone_combining) == "title"

    def test_fullwidth_and_ideographic_space_normalization(self):
        """Ideographic spaces (U+3000) and Fullwidth ASCII (U+FF01-U+FF5E) are handled."""
        fw_title = "Ｓｏｎｇ　Ｎａｍｅ　（Ｒｅｍｉｘ）"
        assert normalize_text(fw_title) == "song name remix"

    def test_clean_search_phrase_preserves_non_latin(self):
        """clean_search_phrase preserves Japanese/Cyrillic while stripping noise."""
        jp_query = "ステラベエ - 超カワイイ (Album)"
        cleaned = clean_search_phrase(jp_query)
        assert "ステラベエ" in cleaned
        assert "超カワイイ" in cleaned
        assert "Album" in cleaned

        cyrillic_query = "Чайковский - Лебединое озеро [FLAC]"
        cleaned_cyr = clean_search_phrase(cyrillic_query)
        assert "Чайковский" in cleaned_cyr
        assert "Лебединое озеро" in cleaned_cyr


# ==============================================================================
# SECTION 2: Adversarial Roman Numerals & Track Conflict Detection
# ==============================================================================

class TestAdversarialRomanNumeralsStress:
    """Stress tests Roman numeral normalization, case insensitivity, and conflict detection."""

    @pytest.mark.parametrize("roman_str,arabic_str", [
        ("Part I", "Part 1"),
        ("Part II", "Part 2"),
        ("Part III", "Part 3"),
        ("Part IV", "Part 4"),
        ("Part V", "Part 5"),
        ("Part VI", "Part 6"),
        ("Part VII", "Part 7"),
        ("Part VIII", "Part 8"),
        ("Part IX", "Part 9"),
        ("Part X", "Part 10"),
        ("Part XI", "Part 11"),
        ("Part XII", "Part 12"),
        ("Part XIII", "Part 13"),
        ("Part XIV", "Part 14"),
        ("Part XV", "Part 15"),
        ("Part XVI", "Part 16"),
        ("Part XVII", "Part 17"),
        ("Part XVIII", "Part 18"),
        ("Part XIX", "Part 19"),
        ("Part XX", "Part 20"),
        # Lowercase indicators & numerals
        ("part i", "part 1"),
        ("part iv", "part 4"),
        ("part ix", "part 9"),
        ("part xiv", "part 14"),
        ("part xx", "part 20"),
        # Other indicators
        ("Act I", "Act 1"),
        ("Act IX", "Act 9"),
        ("Movement III", "Movement 3"),
        ("Mov. IV", "Mov 4"),
        ("Mvt V", "Mvt 5"),
        ("Vol. II", "Vol 2"),
        ("Volume X", "Volume 10"),
        ("Suite No. IV", "Suite No 4"),
        ("Opus XII", "Opus 12"),
        ("Op. III", "Op 3"),
        ("Section VI", "Section 6"),
        ("Scene VII", "Scene 7"),
        ("Chapter VIII", "Chapter 8"),
    ])
    def test_roman_indicators_convert_to_arabic(self, roman_str: str, arabic_str: str):
        """Roman numerals with indicators convert to Arabic numbers identically."""
        norm_r = normalize_text(roman_str)
        norm_a = normalize_text(arabic_str)
        assert norm_r == norm_a, f"Mismatch: '{norm_r}' != '{norm_a}'"

    @pytest.mark.parametrize("bracketed,expected", [
        ("Song Title (I)", "Song Title (1)"),
        ("Song Title [II]", "Song Title [2]"),
        ("Song Title {III}", "Song Title {3}"),
        ("Song Title (IV)", "Song Title (4)"),
        ("Song Title [V]", "Song Title [5]"),
        ("Song Title (VI)", "Song Title (6)"),
        ("Song Title [VII]", "Song Title [7]"),
        ("Song Title (VIII)", "Song Title (8)"),
        ("Song Title [IX]", "Song Title [9]"),
        ("Song Title (X)", "Song Title (10)"),
        ("Song Title [XIV]", "Song Title [14]"),
        ("Song Title (XIX)", "Song Title (19)"),
        ("Song Title [XX]", "Song Title [20]"),
    ])
    def test_bracketed_roman_numerals_convert(self, bracketed: str, expected: str):
        """Bracketed Roman numerals convert accurately."""
        assert normalize_text(bracketed) == normalize_text(expected)

    @pytest.mark.parametrize("word", [
        "I",
        "I Am",
        "V",
        "V For Vendetta",
        "X",
        "Planet X",
        "Six",
        "Mix",
        "Fix",
        "Exit Music",
        "Vivid",
        "Livin' La Vida",
        "Matrix",
        "Civilization",
        "Maximum",
        "Taxation",
        "Oxidation",
    ])
    def test_common_words_and_pronouns_not_corrupted(self, word: str):
        """English words and pronouns containing Roman characters are never converted to digits."""
        converted = normalize_roman_numerals(word)
        assert not any(d in converted for d in "0123456789"), f"Word '{word}' was corrupted to '{converted}'"

    @pytest.mark.parametrize("title_a,title_b", [
        ("Part I", "Part II"),
        ("Part III", "Part IV"),
        ("Part VII", "Part VIII"),
        ("Part IX", "Part X"),
        ("Part XIV", "Part XV"),
        ("Movement 1", "Movement 2"),
        ("Movement I", "Movement 2"),
        ("Act 1", "Act 2"),
        ("Suite No. 1", "Suite No. 2"),
        ("Sonata No. 3", "Sonata No. 4"),
        ("Opus 1", "Opus 2"),
    ])
    def test_have_conflicting_numbers_flags_differences(self, title_a: str, title_b: str):
        """Differing Roman/Arabic numbered tracks are recognized as conflicting."""
        assert have_conflicting_numbers(title_a, title_b) is True
        assert have_conflicting_numbers(title_b, title_a) is True

    @pytest.mark.parametrize("title_a,title_b", [
        ("Part I", "Part 1"),
        ("Part IV", "Part 4"),
        ("Part IX", "Part 9"),
        ("Part XIV", "Part 14"),
        ("Part XX", "Part 20"),
        ("Movement II", "Movement 2"),
        ("Act III", "Act 3"),
        ("Suite No. V", "Suite No. 5"),
    ])
    def test_have_conflicting_numbers_recognizes_equivalents(self, title_a: str, title_b: str):
        """Equivalent Roman/Arabic numbered tracks are NOT flagged as conflicting."""
        assert have_conflicting_numbers(title_a, title_b) is False
        assert have_conflicting_numbers(title_b, title_a) is False


# ==============================================================================
# SECTION 3: Adversarial Typographic Dashes, Separators, and Version Descriptors
# ==============================================================================

class TestAdversarialDashesAndVersionsStress:
    """Stress tests typographic dashes, wave dashes, and version descriptor preservation."""

    @pytest.mark.parametrize("dash", [
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
    def test_all_dashes_stripped_cleanly_in_filename(self, dash: str):
        """All typographic dashes and wave dashes act as valid separators."""
        fn = f"01 {dash} Radiohead {dash} Paranoid Android.flac"
        extracted = strip_track_number_and_artist(fn)
        assert extracted == "Paranoid Android", f"Failed with delimiter '{dash}': extracted '{extracted}'"

    @pytest.mark.parametrize("fn,expected_title", [
        ("01 Track One - Instrumental.flac", "Track One (Instrumental)"),
        ("02 Track Two – Acoustic Version.mp3", "Track Two (Acoustic Version)"),
        ("03 Track Three — Live in Tokyo.flac", "Track Three (Live in Tokyo)"),
        ("04 Track Four - Radio Edit.flac", "Track Four (Radio Edit)"),
        ("05 Track Five - VIP Mix.flac", "Track Five (VIP Mix)"),
        ("06 Track Six - Remaster.flac", "Track Six (Remaster)"),
        ("07 Track Seven - Demo.flac", "Track Seven (Demo)"),
    ])
    def test_hyphenated_version_descriptors_preserve_core_title(self, fn: str, expected_title: str):
        """Version descriptors are preserved rather than replacing the song title."""
        cleaned = strip_track_number_and_artist(fn)
        assert cleaned == expected_title

    def test_short_song_titles_not_stripped(self):
        """Short numeric/alphanumeric song titles are never mangled."""
        assert strip_track_number_and_artist("01 - 1999.flac") == "1999"
        assert strip_track_number_and_artist("02 - 1984.mp3") == "1984"
        assert strip_track_number_and_artist("03 - 21.flac") == "21"
        assert strip_track_number_and_artist("04 - One.flac") == "One"
        assert strip_track_number_and_artist("05 - 3D.flac") == "3D"


# ==============================================================================
# SECTION 4: Adversarial Flat Multi-Disc Structures & Disc Prefixes
# ==============================================================================

class TestAdversarialFlatMultiDiscStress:
    """Stress tests flat multi-disc directory structures, disc prefixes, and gap detection."""

    @pytest.mark.parametrize("raw_track,filename,meta_disc,expected_disc,expected_trk", [
        # Standard tag digits
        ("1", "01 Song.flac", None, 1, 1),
        ("02", "02 Song.flac", 1, 1, 2),
        ("1/12", "01 Song.flac", 1, 1, 1),
        # Disc-track prefix in tag
        ("1-01", "01 Song.flac", 1, 1, 1),
        ("2-01", "01 Song.flac", 1, 2, 1),
        ("2.03", "03 Song.flac", 1, 2, 3),
        ("02-04", "04 Song.flac", 1, 2, 4),
        # Vinyl side notation in tag
        ("A1", "01 Song.flac", 1, 1, 1),
        ("B1", "01 Song.flac", 1, 2, 1),
        ("B2", "02 Song.flac", 1, 2, 2),
        ("C1", "01 Song.flac", 1, 3, 1),
        ("D2", "02 Song.flac", 1, 4, 2),
        ("Side A", "01 Song.flac", 1, 1, 1),
        ("Side B", "01 Song.flac", 1, 2, 1),
        ("Side B 02", "02 Song.flac", 1, 2, 2),
        ("Side 2 - 01", "01 Song.flac", 1, 2, 1),
        ("Disc 2 - 01", "01 Song.flac", 1, 2, 1),
        ("CD 2 - 03", "03 Song.flac", 1, 2, 3),
        # Disc-track prefix in filename (tag has standard digits or untagged)
        ("1", "1-01 Song.flac", 1, 1, 1),
        ("1", "2-01 Song.flac", 1, 2, 1),
        ("2", "2-02 Song.flac", 1, 2, 2),
        (None, "2.01 Song.flac", 1, 2, 1),
        (None, "02-03 Song.flac", 1, 2, 3),
        ("1", "B1 Song.flac", 1, 2, 1),
        ("2", "B02 Song.flac", 1, 2, 2),
        (None, "C1 Song.flac", 1, 3, 1),
        (None, "Side B 01 - Song.flac", 1, 2, 1),
        (None, "Side 2 - 01 Song.flac", 1, 2, 1),
        (None, "Disc 2 - 01 Song.flac", 1, 2, 1),
        (None, "CD 2 - 04 Song.flac", 1, 2, 4),
    ])
    def test_parse_disc_and_track_number_prefixes(
        self,
        raw_track,
        filename,
        meta_disc,
        expected_disc,
        expected_trk
    ):
        """parse_disc_and_track_number accurately extracts disc and track across all prefix variants."""
        d, t, _ = parse_disc_and_track_number(raw_track, filename=filename, meta_disc=meta_disc)
        assert d == expected_disc, f"Expected disc {expected_disc}, got {d} for ({raw_track}, {filename})"
        assert t == expected_trk, f"Expected track {expected_trk}, got {t} for ({raw_track}, {filename})"

    def test_flat_folder_4_vinyl_sides_scan_and_gap_detection(self, tmp_path):
        """
        A double vinyl album with 4 sides in a single flat folder:
        Side A: A1, A2
        Side B: B1 (missing B2)
        Side C: C1, C2, C3
        Side D: D1 (missing D2, D3)
        Must detect 4 distinct discs, correct tracks per disc, and exact missing tracks.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "The Clash" / "London Calling Flat"
        album_dir.mkdir(parents=True)

        # Side A (Disc 1): 1, 2
        (album_dir / "A1 London Calling.flac").write_text("audio")
        (album_dir / "A2 Brand New Cadillac.flac").write_text("audio")

        # Side B (Disc 2): 1 (missing 2)
        (album_dir / "B1 Spanish Bombs.flac").write_text("audio")
        # B2 Missing

        # Side C (Disc 3): 1, 2, 3
        (album_dir / "C1 Clampdown.flac").write_text("audio")
        (album_dir / "C2 The Guns of Brixton.flac").write_text("audio")
        (album_dir / "C3 Wrong 'Em Boyo.flac").write_text("audio")

        # Side D (Disc 4): 1, 3 (missing 2)
        (album_dir / "D1 Lover's Rock.flac").write_text("audio")
        (album_dir / "D3 Revolution Rock.flac").write_text("audio")

        cache = UnifiedCacheManager(db_path=tmp_path / "clash_flat.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["title"] == "London Calling Flat"
        assert rel["artist"] == "The Clash"

        tracks = rel["tracks"]
        found = [t for t in tracks if t["status"] == "found"]
        missing = [t for t in tracks if t["status"] == "missing"]

        assert len(found) == 8  # A1, A2, B1, C1, C2, C3, D1, D3 (8 found)

        # In Side D: 1, 3 found -> missing track 2 detected!
        # Check that Disc 4 missing track 2 was flagged:
        d4_missing = [t for t in missing if t["disc_number"] == 4 and t["track_num_int"] == 2]
        assert len(d4_missing) == 1, "Disc 4 Track 2 gap was not detected!"

        # Ensure discs 1, 2, 3, 4 are all represented
        discs_found = {t["disc_number"] for t in found}
        assert discs_found == {1, 2, 3, 4}

    def test_flat_folder_audit_release_against_musicbrainz_multidisc(self):
        """
        Auditing a flat multi-disc release with '1-01', '1-02', '2-01', '2-02' against MusicBrainz.
        Disc 1 has track 1 and 2.
        Disc 2 has track 1 and 2.
        MusicBrainz has Disc 1 (tracks 1, 2) and Disc 2 (tracks 1, 2, 3).
        Disc 2 Track 3 must be marked missing; all other tracks found with zero collisions.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-audit-stress",
            "title": "Flat Multi Album",
            "artist-credit": [{"name": "Rock Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Song 1", "recording": {"id": "d1-1"}},
                        {"number": "2", "position": 2, "title": "D1 Song 2", "recording": {"id": "d1-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Song 1", "recording": {"id": "d2-1"}},
                        {"number": "2", "position": 2, "title": "D2 Song 2", "recording": {"id": "d2-2"}},
                        {"number": "3", "position": 3, "title": "D2 Song 3", "recording": {"id": "d2-3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Rock Band",
            "title": "Flat Multi Album",
            "mb_release_id": "mb-flat-audit-stress",
            "tracks": [
                # Notice disc_number is 1 or None originally (simulating flat folder entry before normalization)
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "D1 Song 1", "filename": "1-01 D1 Song 1.flac", "path": "/music/1-01 D1 Song 1.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "D1 Song 2", "filename": "1-02 D1 Song 2.flac", "path": "/music/1-02 D1 Song 2.flac"},
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "D2 Song 1", "filename": "2-01 D2 Song 1.flac", "path": "/music/2-01 D2 Song 1.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "D2 Song 2", "filename": "2-02 D2 Song 2.flac", "path": "/music/2-02 D2 Song 2.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 1

        # Check Disc 2 Track 3 is the only missing track
        missing = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_number"] == "3"
        assert missing[0]["title"] == "D2 Song 3"

        # Check all 4 local files are matched as found with correct titles
        found = [t for t in audited["tracks"] if t["status"] == "found"]
        assert len(found) == 4
        found_d1 = [t for t in found if t["disc_number"] == 1]
        found_d2 = [t for t in found if t["disc_number"] == 2]
        assert len(found_d1) == 2
        assert len(found_d2) == 2

        # Check zero unmatched bonus tracks appended
        assert len(audited["tracks"]) == 5

    def test_flat_folder_purely_numeric_multidisc_audit(self):
        """
        Purely numeric files in flat structure:
        '1-01.flac', '1-02.flac', '2-01.flac', '2-02.flac'
        Must resolve to official song titles without collision or false negatives.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-num-stress",
            "title": "Numeric Multi Album",
            "artist-credit": [{"name": "Ambient Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "First Movement", "recording": {"id": "rec-1"}},
                        {"number": "2", "position": 2, "title": "Second Movement", "recording": {"id": "rec-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Third Movement", "recording": {"id": "rec-3"}},
                        {"number": "2", "position": 2, "title": "Fourth Movement", "recording": {"id": "rec-4"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Ambient Band",
            "title": "Numeric Multi Album",
            "mb_release_id": "mb-flat-num-stress",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "1-01.flac", "path": "/music/1-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "1-02.flac", "path": "/music/1-02.flac"},
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "2-01.flac", "path": "/music/2-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "2-02.flac", "path": "/music/2-02.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "complete"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 0
        assert len(audited["tracks"]) == 4

        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        d1_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "2")
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")
        d2_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "2")

        assert d1_t1["title"] == "First Movement"
        assert d1_t2["title"] == "Second Movement"
        assert d2_t1["title"] == "Third Movement"
        assert d2_t2["title"] == "Fourth Movement"
