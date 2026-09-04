"""
Deep Adversarial Stress and Verification Suite for Milestone 1 Remediation (R3).
Author: m1_challenger_1_r3

Focus areas:
1. Disc prefix parsing across edge cases, conflicting metadata, and malformed inputs.
2. Flat folder multi-disc grouping, deduplication, and multi-gap detection.
3. Hyphenated version descriptor preservation vs false positive prevention.
4. Compound numeric track filenames in audit_release with missing tracks and cross-disc guards.
"""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from musicscraper.core.text import (
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    VERSION_DESCRIPTOR_RE,
)
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.services.library import (
    LibraryReleaseService,
    parse_disc_and_track_number,
    _is_numeric_track_item,
)
from musicscraper.services.reconciler import (
    is_purely_numeric_track,
    is_track_number_match,
    have_conflicting_numbers,
    have_conflicting_track_numbers,
)


# ==============================================================================
# Suite 1: Disc Prefix Parsing Deep Stress
# ==============================================================================

class TestDiscPrefixParsingDeepStress:
    """Rigorous stress testing for parse_disc_and_track_number."""

    @pytest.mark.parametrize("raw_track,filename,meta_disc,exp_disc,exp_trk,exp_total", [
        # Standard track numbering
        ("1", "01 Song.flac", None, 1, 1, None),
        ("01", "01 Song.flac", None, 1, 1, None),
        ("1/12", "01 Song.flac", None, 1, 1, 12),
        ("05/20", "05 Song.flac", 1, 1, 5, 20),

        # Multi-disc tag formats
        ("1-01", "Song.flac", None, 1, 1, None),
        ("2-04", "Song.flac", None, 2, 4, None),
        ("03-01", "Song.flac", None, 3, 1, None),
        ("1.01", "Song.flac", None, 1, 1, None),
        ("2.05", "Song.flac", None, 2, 5, None),

        # Vinyl side notation in tag
        ("A1", "Song.flac", None, 1, 1, None),
        ("a1", "Song.flac", None, 1, 1, None),
        ("B1", "Song.flac", None, 2, 1, None),
        ("B02", "Song.flac", None, 2, 2, None),
        ("C3", "Song.flac", None, 3, 3, None),
        ("D12", "Song.flac", None, 4, 12, None),
        ("Side A", "Song.flac", None, 1, 1, None),
        ("Side B", "Song.flac", None, 2, 1, None),
        ("Side B 03", "Song.flac", None, 2, 3, None),
        ("Vinyl Side C", "Song.flac", None, 3, 1, None),
        ("Side 2 - 01", "Song.flac", None, 2, 1, None),
        ("Disc 2 - 01", "Song.flac", None, 2, 1, None),
        ("CD 3 - 04", "Song.flac", None, 3, 4, None),

        # Filename prefix when tag has plain digits or is None
        ("1", "1-01 Track.flac", None, 1, 1, None),
        ("1", "2-01 Track.flac", None, 2, 1, None),
        ("2", "2-02 Track.flac", 1, 2, 2, None),
        (None, "1-01 Track.flac", None, 1, 1, None),
        (None, "2-03 Track.flac", None, 2, 3, None),
        (None, "03-05 Track.flac", None, 3, 5, None),
        (None, "1.01 Track.flac", None, 1, 1, None),
        (None, "2.04 Track.flac", None, 2, 4, None),
        (None, "A1 Track.flac", None, 1, 1, None),
        (None, "B2 Track.flac", None, 2, 2, None),
        (None, "C03 Track.flac", None, 3, 3, None),
        (None, "Side A 01 - Track.flac", None, 1, 1, None),
        (None, "Side B 02 - Track.flac", None, 2, 2, None),
        (None, "Side 2 - 01 Track.flac", None, 2, 1, None),
        (None, "Disc 2 - 03 Track.flac", None, 2, 3, None),
        (None, "CD 2 - 01 Track.flac", None, 2, 1, None),
        (None, "CD 03 - 04 Track.flac", None, 3, 4, None),

        # Purely numeric files
        (None, "01.flac", None, 1, 1, None),
        (None, "02.mp3", None, 1, 2, None),
        (None, "1-01.flac", None, 1, 1, None),
        (None, "2-01.flac", None, 2, 1, None),
        (None, "2-02.flac", None, 2, 2, None),
        (None, "A1.flac", None, 1, 1, None),
        (None, "B1.flac", None, 2, 1, None),

        # Meta disc precedence when not default 1
        ("1", "01 Song.flac", 2, 2, 1, None),
        ("2", "02 Song.flac", 3, 3, 2, None),
    ])
    def test_parse_disc_and_track_matrix(
        self, raw_track, filename, meta_disc, exp_disc, exp_trk, exp_total
    ):
        d, t, tot = parse_disc_and_track_number(raw_track, filename=filename, meta_disc=meta_disc)
        assert d == exp_disc, f"Disc mismatch for ({raw_track}, {filename}, {meta_disc}): expected {exp_disc}, got {d}"
        assert t == exp_trk, f"Track mismatch for ({raw_track}, {filename}, {meta_disc}): expected {exp_trk}, got {t}"
        if exp_total is not None:
            assert tot == exp_total, f"Total mismatch: expected {exp_total}, got {tot}"

    def test_malformed_and_boundary_disc_inputs(self):
        """Check behavior on malformed and boundary inputs."""
        # Empty string
        d, t, _ = parse_disc_and_track_number("", filename="")
        assert d == 1
        assert t is None

        # None inputs
        d, t, _ = parse_disc_and_track_number(None, filename=None)
        assert d == 1
        assert t is None

        # Disc number outside 1..20 boundary returns None or 1, and effective_disc resolves to 1
        d, t, _ = parse_disc_and_track_number("99-01", filename=None)
        assert d is None or d == 1
        assert t == 1


# ==============================================================================
# Suite 2: Flat Folder Multi-Disc Grouping & Gap Detection
# ==============================================================================

class TestFlatFolderMultiDiscStress:
    """Stress tests flat folder scanning with multiple discs and simultaneous gaps."""

    def test_3_disc_flat_folder_multi_gap_detection(self, tmp_path):
        """
        3-disc flat album in a single folder:
        Disc 1: 1-01, 1-02 (complete)
        Disc 2: 2-01, 2-03 (track 2 missing!)
        Disc 3: 3-02, 3-03 (track 1 missing!)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Prog Artist" / "Triple Odyssey"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Act I Part 1.flac").write_text("a")
        (album_dir / "1-02 Act I Part 2.flac").write_text("b")
        (album_dir / "2-01 Act II Part 1.flac").write_text("c")
        # 2-02 is MISSING
        (album_dir / "2-03 Act II Part 3.flac").write_text("d")
        # 3-01 is MISSING
        (album_dir / "3-02 Act III Part 2.flac").write_text("e")
        (album_dir / "3-03 Act III Part 3.flac").write_text("f")

        cache = UnifiedCacheManager(db_path=tmp_path / "triple.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["title"] == "Triple Odyssey"
        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 6
        assert rel["missing_count"] == 2
        assert rel["total_tracks_expected"] == 8

        missing_tracks = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing_tracks) == 2

        d2_m = [t for t in missing_tracks if t["disc_number"] == 2]
        d3_m = [t for t in missing_tracks if t["disc_number"] == 3]

        assert len(d2_m) == 1
        assert d2_m[0]["track_num_int"] == 2
        assert d2_m[0]["title"] == "Disc 2 Track 02 (Missing)"

        assert len(d3_m) == 1
        assert d3_m[0]["track_num_int"] == 1
        assert d3_m[0]["title"] == "Disc 3 Track 01 (Missing)"

        # Verify ordering: sorted by (disc_number, track_num_int)
        seq = [(t["disc_number"], t["track_num_int"], t["status"]) for t in rel["tracks"]]
        assert seq == [
            (1, 1, "found"),
            (1, 2, "found"),
            (2, 1, "found"),
            (2, 2, "missing"),
            (2, 3, "found"),
            (3, 1, "missing"),
            (3, 2, "found"),
            (3, 3, "found"),
        ]

    def test_vinyl_4_sides_flat_folder_alternating_gaps(self, tmp_path):
        """
        4 vinyl sides in flat folder with gaps on alternating sides:
        Side A (D1): A1, A3 (A2 missing)
        Side B (D2): B2 (B1 missing)
        Side C (D3): C1, C2 (complete)
        Side D (D4): D1 (complete single track side)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Electronic" / "Quad Vinyl EP"
        album_dir.mkdir(parents=True)

        (album_dir / "A1 Synth Wave.flac").write_text("1")
        (album_dir / "A3 Neon Drive.flac").write_text("3")
        (album_dir / "B2 Cyber City.flac").write_text("4")
        (album_dir / "C1 Retro Sunset.flac").write_text("5")
        (album_dir / "C2 Night Grid.flac").write_text("6")
        (album_dir / "D1 Outro Horizon.flac").write_text("7")

        cache = UnifiedCacheManager(db_path=tmp_path / "quad_vinyl.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 6
        assert rel["missing_count"] == 2
        assert rel["total_tracks_expected"] == 8

        d1_missing = [t for t in rel["tracks"] if t["disc_number"] == 1 and t["status"] == "missing"]
        assert len(d1_missing) == 1
        assert d1_missing[0]["track_num_int"] == 2

        d2_missing = [t for t in rel["tracks"] if t["disc_number"] == 2 and t["status"] == "missing"]
        assert len(d2_missing) == 1
        assert d2_missing[0]["track_num_int"] == 1

        d3_missing = [t for t in rel["tracks"] if t["disc_number"] == 3 and t["status"] == "missing"]
        assert len(d3_missing) == 0

        d4_missing = [t for t in rel["tracks"] if t["disc_number"] == 4 and t["status"] == "missing"]
        assert len(d4_missing) == 0


# ==============================================================================
# Suite 3: Hyphenated Version Descriptors Preservation
# ==============================================================================

class TestHyphenatedVersionDescriptorsStress:
    """Stress tests version descriptor preservation in strip_track_number_and_artist."""

    @pytest.mark.parametrize("fn,expected", [
        # Acoustic variants
        ("01 Song - Acoustic.flac", "Song (Acoustic)"),
        ("01 Song - Acoustic Version.flac", "Song (Acoustic Version)"),
        ("01 Song – Acoustic Ver.mp3", "Song (Acoustic Ver)"),
        ("01 Song - Unplugged.flac", "Song (Unplugged)"),
        ("01 Song - Unplugged Version.flac", "Song (Unplugged Version)"),
        ("01 Song - Piano Version.flac", "Song (Piano Version)"),
        ("01 Song - Piano Ver.flac", "Song (Piano Ver)"),

        # Instrumental variants
        ("02 Title - Instrumental.flac", "Title (Instrumental)"),
        ("02 Title - Inst.flac", "Title (Inst)"),
        ("02 Title - Off Vocal.flac", "Title (Off Vocal)"),
        ("02 Title - Karaoke.flac", "Title (Karaoke)"),
        ("02 Title - Backing Track.flac", "Title (Backing Track)"),

        # Acapella variants
        ("03 Harmony - Acapella.flac", "Harmony (Acapella)"),
        ("03 Harmony - A Cappella.flac", "Harmony (A Cappella)"),
        ("03 Harmony - Vocal Version.flac", "Harmony (Vocal Version)"),

        # Live variants
        ("04 Anthem - Live.flac", "Anthem (Live)"),
        ("04 Anthem - Live in Berlin.flac", "Anthem (Live in Berlin)"),
        ("04 Anthem - Live at Wembley.flac", "Anthem (Live at Wembley)"),
        ("04 Anthem - Live 1998.flac", "Anthem (Live 1998)"),
        ("04 Anthem - Live Version.flac", "Anthem (Live Version)"),

        # Mix / VIP / Radio Edit variants
        ("05 Beats - Radio Edit.flac", "Beats (Radio Edit)"),
        ("05 Beats - Club Mix.flac", "Beats (Club Mix)"),
        ("05 Beats - Extended Mix.flac", "Beats (Extended Mix)"),
        ("05 Beats - Extended Version.flac", "Beats (Extended Version)"),
        ("05 Beats - VIP.flac", "Beats (VIP)"),
        ("05 Beats - VIP Mix.flac", "Beats (VIP Mix)"),
        ("05 Beats - Dub Mix.flac", "Beats (Dub Mix)"),
        ("05 Beats - Original Mix.flac", "Beats (Original Mix)"),

        # Remaster / Edition variants
        ("06 Classic - Remaster.flac", "Classic (Remaster)"),
        ("06 Classic - Remastered.flac", "Classic (Remastered)"),
        ("06 Classic - Digital Remaster.flac", "Classic (Digital Remaster)"),
        ("06 Classic - Deluxe Edition.flac", "Classic (Deluxe Edition)"),
        ("06 Classic - Anniversary Edition.flac", "Classic (Anniversary Edition)"),
        ("06 Classic - Bonus Track.flac", "Classic (Bonus Track)"),

        # Remix variants
        ("07 Track - Remix.flac", "Track (Remix)"),
        ("07 Track - Skrillex Remix.flac", "Track (Skrillex Remix)"),
        ("07 Track - Deadmau5 Rework.flac", "Track (Deadmau5 Rework)"),
        ("07 Track - Tiesto Bootleg.flac", "Track (Tiesto Bootleg)"),
        ("07 Track - Club Flip.flac", "Track (Club Flip)"),

        # Non-version multi-hyphen titles (must NOT treat normal titles as version)
        ("08 Artist - Love - Hate.flac", "Hate"),
        ("09 Artist - Sun - Moon.flac", "Moon"),
    ])
    def test_version_descriptor_matrix(self, fn: str, expected: str):
        result = strip_track_number_and_artist(fn)
        assert result == expected, f"Mismatch for '{fn}': expected '{expected}', got '{result}'"


# ==============================================================================
# Suite 4: Compound Numeric Track Filenames & Audit Reconciliation
# ==============================================================================

class TestCompoundNumericAuditStress:
    """Stress tests compound numeric filenames in audit_release."""

    def test_is_numeric_track_item_helper(self):
        """_is_numeric_track_item accurately differentiates numeric vs real titles."""
        # Pure numeric
        assert _is_numeric_track_item({"title": "", "filename": "01.flac"}) is True
        assert _is_numeric_track_item({"title": "01", "filename": "01.flac"}) is True
        assert _is_numeric_track_item({"title": "Track 01", "filename": "01.flac"}) is True

        # Compound numeric
        assert _is_numeric_track_item({"title": "", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "1-01", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "Track 01", "filename": "1-01.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "2-03.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "02.04.flac"}) is True
        assert _is_numeric_track_item({"title": "", "filename": "1_05.flac"}) is True

        # Non-numeric (has real title)
        assert _is_numeric_track_item({"title": "Paranoid Android", "filename": "1-01.flac"}) is False
        assert _is_numeric_track_item({"title": "Real Song", "filename": "01.flac"}) is False
        assert _is_numeric_track_item({"title": "1-01 Song Title", "filename": "1-01 Song Title.flac"}) is False

    def test_audit_release_compound_numeric_with_disc2_missing_track(self):
        """
        Audit a multi-disc release with compound numeric filenames:
        Official release:
          Disc 1: Track 1 ("Intro"), Track 2 ("Outro")
          Disc 2: Track 1 ("Part A"), Track 2 ("Part B")
        Local files:
          "1-01.flac" (D1 T1)
          "1-02.flac" (D1 T2)
          "2-02.flac" (D2 T2) - note 2-01 is MISSING!
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-comp-audit-gap",
            "title": "Dual Concept",
            "artist-credit": [{"name": "Electronic Project"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Intro", "recording": {"id": "rec-d1-1"}},
                        {"number": "2", "position": 2, "title": "Outro", "recording": {"id": "rec-d1-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Part A", "recording": {"id": "rec-d2-1"}},
                        {"number": "2", "position": 2, "title": "Part B", "recording": {"id": "rec-d2-2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Electronic Project",
            "title": "Dual Concept",
            "mb_release_id": "mb-comp-audit-gap",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "1-01.flac", "path": "/music/1-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "1-02.flac", "path": "/music/1-02.flac"},
                # Disc 2 track 2 only; track 1 is absent
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "2-02.flac", "path": "/music/2-02.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 1
        assert len(audited["tracks"]) == 4

        # Verify Disc 2 Track 1 is flagged missing
        missing = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_number"] == "1"
        assert missing[0]["title"] == "Part A"

        # Verify Disc 2 Track 2 is matched to 2-02.flac
        d2_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "2")
        assert d2_t2["status"] == "found"
        assert d2_t2["filename"] == "2-02.flac"
        assert d2_t2["title"] == "Part B"

        # Verify Disc 1 tracks
        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        assert d1_t1["status"] == "found"
        assert d1_t1["filename"] == "1-01.flac"
        assert d1_t1["title"] == "Intro"

        d1_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "2")
        assert d1_t2["status"] == "found"
        assert d1_t2["filename"] == "1-02.flac"
        assert d1_t2["title"] == "Outro"

    def test_audit_release_cross_disc_collision_numeric_prevention(self):
        """
        Ensure 2-01.flac NEVER falsely matches Disc 1 Track 1 even when track_number is '1'.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-collision-guard",
            "title": "Cross Guard",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Song 1", "recording": {"id": "r1"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Song 1", "recording": {"id": "r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        # Only provide 2-01.flac (Disc 2 Track 1)
        release_data = {
            "artist": "Artist",
            "title": "Cross Guard",
            "mb_release_id": "mb-collision-guard",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "2-01.flac", "path": "/music/2-01.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        # Disc 1 Track 1 must be MISSING
        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        assert d1_t1["status"] == "missing", "2-01.flac erroneously matched Disc 1 Track 1!"

        # Disc 2 Track 1 must be FOUND
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")
        assert d2_t1["status"] == "found"
        assert d2_t1["filename"] == "2-01.flac"
        assert d2_t1["title"] == "D2 Song 1"

        assert audited["missing_count"] == 1
        assert audited["found_count"] == 1
        assert len(audited["tracks"]) == 2
