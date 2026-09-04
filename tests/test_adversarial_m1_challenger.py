"""
Adversarial and Empirical Verification Suite for Milestone 1:
Multi-Disc Hierarchies, Tag Parsing, and Missing Track Detection.

Focus areas:
1. Multi-disc structures: nested Disc 1/Disc 2, CD01/CD02, Vinyl Side A/Side B,
   flat folder with 1-01/2-01, and mixed tag vs folder numbering.
2. Multi-disc sequence gap detection: ensure missing tracks on Disc 1 are NEVER
   obscured by existing tracks with the same track number on Disc 2 (zero false negatives).
3. Purely numeric filenames in confirmed album folders: ensure 01.flac, 02.flac
   correctly resolve without failing similarity or creating duplicate bonus tracks.
"""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

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
    is_purely_numeric_track,
    is_track_number_match,
    have_conflicting_numbers,
    have_conflicting_track_numbers,
)


# ============================================================================
# Section 1: Multi-Disc Structures & Folder Heuristics (Verified Working)
# ============================================================================

class TestMultiDiscStructuresWorking:
    """Verifies multi-disc folder hierarchies that function correctly."""

    def test_disc_dir_pattern_baseline_coverage(self):
        """Verify DISC_DIR_PATTERN matches standard disc folder notations."""
        valid_cases = [
            ("Disc 1", 1),
            ("Disc 2", 2),
            ("disc 01", 1),
            ("DISC 02", 2),
            ("Disc-1", 1),
            ("Disc_2", 2),
            ("Disc.1", 1),
            ("CD 1", 1),
            ("CD 2", 2),
            ("CD01", 1),
            ("CD02", 2),
            ("cd-01", 1),
            ("cd_02", 2),
            ("Disk 1", 1),
            ("Disk 2", 2),
            ("disk01", 1),
            ("Vinyl 1", 1),
            ("Vinyl 2", 2),
            ("Side A", 1),
            ("Side B", 2),
            ("side 1", 1),
            ("side 2", 2),
            ("LP 1", 1),
            ("LP 2", 2),
            ("Disc One", 1),
            ("Disc Two", 2),
            ("Disc 1 - Bonus Tracks", 1),
            ("CD 1: The Early Years", 1),
        ]
        for folder_name, expected_disc in valid_cases:
            m = DISC_DIR_PATTERN.match(folder_name)
            assert m is not None, f"DISC_DIR_PATTERN failed to match valid folder: '{folder_name}'"

    def test_nested_disc1_disc2_grouping(self, tmp_path):
        """Nested Disc 1 / Disc 2 folders group into a single release with correct disc numbers."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Radiohead" / "OK Computer OKNOTOK 1997 2017"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        (d1 / "01 Airbag.flac").write_text("audio1")
        (d1 / "02 Paranoid Android.flac").write_text("audio2")
        (d2 / "01 I Promise.flac").write_text("audio3")
        (d2 / "02 Man of War.flac").write_text("audio4")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_nested_disc.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1, f"Expected 1 grouped release, got {len(releases)}"

        rel = releases[0]
        assert rel["title"] == "OK Computer OKNOTOK 1997 2017"
        assert rel["artist"] == "Radiohead"

        tracks = rel["tracks"]
        assert len(tracks) == 4

        d1_tracks = [t for t in tracks if t["disc_number"] == 1]
        d2_tracks = [t for t in tracks if t["disc_number"] == 2]
        assert len(d1_tracks) == 2
        assert len(d2_tracks) == 2
        assert [t["track_num_int"] for t in d1_tracks] == [1, 2]
        assert [t["track_num_int"] for t in d2_tracks] == [1, 2]

    def test_nested_cd01_cd02_grouping(self, tmp_path):
        """Zero-padded CD01 / CD02 subfolders group correctly into single release."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Daft Punk" / "Alive 2007"
        cd1 = album_dir / "CD01"
        cd2 = album_dir / "CD02"
        cd1.mkdir(parents=True)
        cd2.mkdir(parents=True)

        (cd1 / "01 Robot Rock.flac").write_text("audio1")
        (cd2 / "01 One More Time.flac").write_text("audio2")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_cd_pad.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]
        assert len(rel["tracks"]) == 2

        t1 = next(t for t in rel["tracks"] if t["filename"] == "01 Robot Rock.flac")
        t2 = next(t for t in rel["tracks"] if t["filename"] == "01 One More Time.flac")
        assert t1["disc_number"] == 1
        assert t2["disc_number"] == 2

    def test_side_a_side_b_grouping(self, tmp_path):
        """'Side A' and 'Side B' subfolders map to disc 1 and disc 2."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Pink Floyd" / "The Dark Side of the Moon"
        side_a = album_dir / "Side A"
        side_b = album_dir / "Side B"
        side_a.mkdir(parents=True)
        side_b.mkdir(parents=True)

        (side_a / "01 Speak to Me.flac").write_text("audio1")
        (side_b / "01 Money.flac").write_text("audio2")

        cache = UnifiedCacheManager(db_path=tmp_path / "test_vinyl_sides.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        t_a = next(t for t in rel["tracks"] if "Speak to Me" in t["title"])
        t_b = next(t for t in rel["tracks"] if "Money" in t["title"])
        assert t_a["disc_number"] == 1
        assert t_b["disc_number"] == 2

    def test_mixed_tag_vs_folder_precedence(self, tmp_path):
        """
        When file has no disc tag, folder disc number (Disc 2) takes precedence over default disc_number=1.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Album"
        d2 = album_dir / "Disc 2"
        d2.mkdir(parents=True)

        f_untagged = d2 / "01 Untagged.flac"
        f_untagged.write_text("data")

        meta = AudioQualityAnalyzer.analyze_file(f_untagged)
        assert meta.disc_number == 2
        assert meta.album == "Album"
        assert meta.artist == "Artist"


# ============================================================================
# Section 2: Multi-Disc Sequence Gap Detection (Verified Working)
# ============================================================================

class TestMultiDiscSequenceGapDetectionWorking:
    """Verifies sequence gap detection where missing tracks on Disc 1 are NOT obscured by Disc 2."""

    def test_disc1_missing_track_never_obscured_by_disc2_track(self, tmp_path):
        """
        Disc 1 is missing track 2 (has tracks 1, 3).
        Disc 2 HAS track 2 (has tracks 1, 2, 3).
        Ensure Disc 1 Track 2 is strictly identified as missing and NOT obscured.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Nine Inch Nails" / "The Fragile"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        # Disc 1: tracks 1, 3
        (d1 / "01 Track One.flac").write_text("data1")
        (d1 / "03 Track Three.flac").write_text("data3")

        # Disc 2: tracks 1, 2, 3
        (d2 / "01 Track One.flac").write_text("data4")
        (d2 / "02 Track Two.flac").write_text("data5")
        (d2 / "03 Track Three.flac").write_text("data6")

        cache = UnifiedCacheManager(db_path=tmp_path / "gap_d1_d2.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["missing_count"] == 1
        assert rel["found_count"] == 5

        # Inspect missing tracks specifically
        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        m_trk = missing[0]
        assert m_trk["disc_number"] == 1
        assert m_trk["track_num_int"] == 2
        assert "Disc 1 Track 02 (Missing)" in m_trk["title"]

        # Ensure Disc 2 Track 2 is present and found
        d2_t2 = next(t for t in rel["tracks"] if t.get("disc_number") == 2 and t.get("track_num_int") == 2)
        assert d2_t2["status"] == "found"

    def test_disc2_missing_track_never_obscured_by_disc1_track(self, tmp_path):
        """
        Disc 1 has tracks 1, 2, 3 (complete).
        Disc 2 is missing track 2 (has tracks 1, 3).
        Ensure Disc 2 Track 2 is strictly identified as missing.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Two Disc Album"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        (d1 / "01 Song 1.flac").write_text("data1")
        (d1 / "02 Song 2.flac").write_text("data2")
        (d1 / "03 Song 3.flac").write_text("data3")

        (d2 / "01 Song 4.flac").write_text("data4")
        (d2 / "03 Song 6.flac").write_text("data6")

        cache = UnifiedCacheManager(db_path=tmp_path / "gap_d2_missing.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_num_int"] == 2
        assert "Disc 2 Track 02 (Missing)" in missing[0]["title"]

    def test_3disc_simultaneous_independent_gaps(self, tmp_path):
        """
        3-disc set with different gaps on each disc:
        Disc 1 missing track 2
        Disc 2 missing track 1
        Disc 3 missing track 3
        All 3 gaps must be independently and accurately identified.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Triple Box Set"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d3 = album_dir / "Disc 3"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        d3.mkdir(parents=True)

        # Disc 1: 1, 3 (missing 2)
        (d1 / "01 D1T1.flac").write_text("1")
        (d1 / "03 D1T3.flac").write_text("3")

        # Disc 2: 2, 3 (missing 1)
        (d2 / "02 D2T2.flac").write_text("2")
        (d2 / "03 D2T3.flac").write_text("3")

        # Disc 3: 1, 2, 4 (missing 3)
        (d3 / "01 D3T1.flac").write_text("1")
        (d3 / "02 D3T2.flac").write_text("2")
        (d3 / "04 D3T4.flac").write_text("4")

        cache = UnifiedCacheManager(db_path=tmp_path / "triple_box.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        missing = [t for t in rel["tracks"] if t["status"] == "missing"]
        assert len(missing) == 3

        missing_tuples = [(t["disc_number"], t["track_num_int"]) for t in missing]
        assert (1, 2) in missing_tuples
        assert (2, 1) in missing_tuples
        assert (3, 3) in missing_tuples

    def test_audit_release_multidisc_reconciliation_zero_false_negatives(self):
        """
        Auditing a multi-disc release against MusicBrainz data:
        Disc 1 is missing track 2; Disc 2 has track 2.
        Ensure MusicBrainz reconciler marks Disc 1 Track 2 as missing and Disc 2 Track 2 as found.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-multi-123",
            "title": "Double Album",
            "artist-credit": [{"name": "The Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Track 1.1", "recording": {"id": "rec-1-1"}},
                        {"number": "2", "position": 2, "title": "Track 1.2", "recording": {"id": "rec-1-2"}},
                        {"number": "3", "position": 3, "title": "Track 1.3", "recording": {"id": "rec-1-3"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Track 2.1", "recording": {"id": "rec-2-1"}},
                        {"number": "2", "position": 2, "title": "Track 2.2", "recording": {"id": "rec-2-2"}},
                        {"number": "3", "position": 3, "title": "Track 2.3", "recording": {"id": "rec-2-3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "The Band",
            "title": "Double Album",
            "mb_release_id": "mb-multi-123",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "Track 1.1", "filename": "01.flac", "path": "/music/1/01.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "Track 1.3", "filename": "03.flac", "path": "/music/1/03.flac"},
                {"disc_number": 2, "track_number": "1", "track_num_int": 1, "title": "Track 2.1", "filename": "01.flac", "path": "/music/2/01.flac"},
                {"disc_number": 2, "track_number": "2", "track_num_int": 2, "title": "Track 2.2", "filename": "02.flac", "path": "/music/2/02.flac"},
                {"disc_number": 2, "track_number": "3", "track_num_int": 3, "title": "Track 2.3", "filename": "03.flac", "path": "/music/2/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 5
        assert audited["missing_count"] == 1

        missing_tracks = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing_tracks) == 1
        m = missing_tracks[0]
        assert m["disc_number"] == 1
        assert m["track_number"] == "2"
        assert m["title"] == "Track 1.2"

        # Disc 2 track 2 must be found
        found_d2_t2 = [t for t in audited["tracks"] if t["status"] == "found" and t["disc_number"] == 2 and t["track_number"] == "2"]
        assert len(found_d2_t2) == 1
        assert found_d2_t2[0]["title"] == "Track 2.2"


# ============================================================================
# Section 3: Purely Numeric Filenames in Confirmed Folders (Verified Working)
# ============================================================================

class TestPurelyNumericFilenamesWorking:
    """Verifies purely numeric track matching in confirmed album folders."""

    def test_is_purely_numeric_track_helper(self):
        """Verify helper correctly classifies purely numeric files without false positives."""
        assert is_purely_numeric_track({"title": "", "filename": "01.flac"}) is True
        assert is_purely_numeric_track({"title": "01", "filename": "01.flac"}) is True
        assert is_purely_numeric_track({"title": "Track 01", "filename": "track 01.mp3"}) is True
        assert is_purely_numeric_track({"title": "Trk_02", "filename": "trk_02.flac"}) is True

        # False cases (must NOT be treated as purely numeric)
        assert is_purely_numeric_track({"title": "1999", "filename": "Prince - 1999.flac"}) is False
        assert is_purely_numeric_track({"title": "Song Title", "filename": "01 Song Title.flac"}) is False
        assert is_purely_numeric_track({"title": "One", "filename": "01 One.flac"}) is False

    def test_single_disc_numeric_filenames_audit(self):
        """
        Purely numeric filenames (01.flac, 02.flac, 03.flac) in a confirmed album folder
        match catalog tracks by track number without failing similarity or duplicating.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-album-num",
            "title": "Morning Glory",
            "artist-credit": [{"name": "Oasis"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Hello", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Roll With It", "recording": {"id": "r2"}},
                        {"number": "3", "position": 3, "title": "Wonderwall", "recording": {"id": "r3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Oasis",
            "title": "Morning Glory",
            "mb_release_id": "mb-album-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Oasis/01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Oasis/02.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "03", "filename": "03.flac", "path": "/music/Oasis/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "complete"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 0
        assert audited["completion_pct"] == 100.0

        # Zero duplicate bonus tracks
        assert len(audited["tracks"]) == 3
        titles = [t["title"] for t in audited["tracks"]]
        assert titles == ["Hello", "Roll With It", "Wonderwall"]

    def test_numeric_filenames_with_sequence_gap_in_audit(self):
        """
        Files are 01.flac and 03.flac (missing 02.flac).
        01.flac matches Track 1.
        03.flac matches Track 3.
        Track 2 is strictly identified as missing.
        03.flac MUST NOT match Track 2.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-gap-num",
            "title": "Album With Gap",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "First Song", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Second Song", "recording": {"id": "r2"}},
                        {"number": "3", "position": 3, "title": "Third Song", "recording": {"id": "r3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Album With Gap",
            "mb_release_id": "mb-gap-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "01.flac", "path": "/music/01.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "", "filename": "03.flac", "path": "/music/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 2
        assert audited["missing_count"] == 1

        t1 = next(t for t in audited["tracks"] if t["track_number"] == "1")
        t2 = next(t for t in audited["tracks"] if t["track_number"] == "2")
        t3 = next(t for t in audited["tracks"] if t["track_number"] == "3")

        assert t1["status"] == "found"
        assert t1["title"] == "First Song"
        assert t1["filename"] == "01.flac"

        assert t2["status"] == "missing"
        assert t2["title"] == "Second Song"
        assert t2["filename"] is None

        assert t3["status"] == "found"
        assert t3["title"] == "Third Song"
        assert t3["filename"] == "03.flac"

        # No duplicate bonus tracks
        assert len(audited["tracks"]) == 3

    def test_multi_disc_numeric_filenames_audit(self):
        """
        Multi-disc folder where both discs use numeric filenames:
        Disc 1 has 01.flac, 02.flac
        Disc 2 has 01.flac, 02.flac
        Ensure each disc matches only its own tracks without cross-disc collision.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-multi-num",
            "title": "Double Numeric Album",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Disc 1 Song 1", "recording": {"id": "d1r1"}},
                        {"number": "2", "position": 2, "title": "Disc 1 Song 2", "recording": {"id": "d1r2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Disc 2 Song 1", "recording": {"id": "d2r1"}},
                        {"number": "2", "position": 2, "title": "Disc 2 Song 2", "recording": {"id": "d2r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Double Numeric Album",
            "mb_release_id": "mb-multi-num",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 1/01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Disc 1/02.flac"},
                {"disc_number": 2, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 2/01.flac"},
                {"disc_number": 2, "track_number": "2", "track_num_int": 2, "title": "02", "filename": "02.flac", "path": "/music/Disc 2/02.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "complete"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 0
        assert len(audited["tracks"]) == 4

        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")

        assert d1_t1["title"] == "Disc 1 Song 1"
        assert "/Disc 1/01.flac" in d1_t1["path"]

        assert d2_t1["title"] == "Disc 2 Song 1"
        assert "/Disc 2/01.flac" in d2_t1["path"]

    def test_multi_disc_numeric_cross_disc_rejection(self):
        """
        If Disc 1 has 01.flac, but Disc 2 has NO files, Disc 2 Track 1 must NOT match Disc 1 01.flac.
        Disc 2 Track 1 must remain missing.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-cross-reject",
            "title": "Cross Disc Test",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Track 1", "recording": {"id": "r1"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Track 1", "recording": {"id": "r2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Artist",
            "title": "Cross Disc Test",
            "mb_release_id": "mb-cross-reject",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "01", "filename": "01.flac", "path": "/music/Disc 1/01.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 1
        assert audited["missing_count"] == 1

        d1 = next(t for t in audited["tracks"] if t["disc_number"] == 1)
        d2 = next(t for t in audited["tracks"] if t["disc_number"] == 2)

        assert d1["status"] == "found"
        assert d1["title"] == "D1 Track 1"
        assert d2["status"] == "missing"
        assert d2["title"] == "D2 Track 1"


# ============================================================================
# Section 4: EMPIRICAL BUG PROBES (Failing Test Cases Proving Subsystem Flaws)
# ============================================================================

class TestEmpiricalBugProbes:
    """
    Adversarial probes proving exact defects in:
    1. parse_disc_and_track_number disc suppression when meta_disc defaults to 1.
    2. Flat folder multi-disc tracking (1-01, 2-01) causing false sequence gaps and missed discs.
    3. Flat folder multi-disc MusicBrainz audit creating false negatives and bogus bonus tracks.
    4. DISC_DIR_PATTERN blindspots for 'Vinyl Side A/B' and bracketed disc folder names.
    """

    def test_bug_probe_1_parse_disc_suppression_by_meta_disc_default(self):
        """
        BUG PROBE 1: In LibraryReleaseService.scan_library_releases:
        `meta_disc = getattr(meta, 'disc_number', None)` is ALWAYS 1 because AudioMetadata defaults disc_number=1.
        Inside parse_disc_and_track_number:
        - Line 87: `if not disc_num and 1 <= d_val <= 20: disc_num = d_val`
        - Line 115: `if not disc_num and 1 <= d_val <= 20: disc_num = d_val`
        Because disc_num was initialized to 1, `not disc_num` evaluates to False!
        This permanently blocks disc number extraction from '2-01' or 'B1' tags and filenames.
        """
        # When meta_disc is None, it extracts disc 2:
        d_clean, t_clean, _ = parse_disc_and_track_number("2-01", meta_disc=None)
        assert d_clean == 2 and t_clean == 1, "Expected disc=2, track=1 when meta_disc is None"

        # EMPIRICAL PROBE of current behavior when meta_disc=1 (as passed from scan_library_releases):
        d_actual, t_actual, _ = parse_disc_and_track_number("2-01", meta_disc=1)
        # In a correct implementation, '2-01' should yield disc=2 even if meta_disc was default 1:
        assert d_actual == 2, f"CRITICAL BUG: Expected disc=2 from '2-01', but got {d_actual} because meta_disc=1 suppressed it!"

    def test_bug_probe_2_flat_folder_multidisc_grouping_and_gap_corruption(self, tmp_path):
        """
        BUG PROBE 2: In a flat album directory with disc-track prefixes (1-01, 1-02, 2-01, 2-02):
        Because disc 2 is suppressed to disc 1:
        - Disc 2 tracks are assigned disc_number=1.
        - Two tracks have track_num=1 and two tracks have track_num=2 on Disc 1.
        - Sequence gap detection sees 4 tracks on Disc 1 with max track number 2,
          and erroneously generates missing track placeholders 'Track 03 (Missing)' and 'Track 04 (Missing)'!
        - Total tracks reported: 6 (4 found + 2 false missing).
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Flat Double Album"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Song A.flac").write_text("a")
        (album_dir / "1-02 Song B.flac").write_text("b")
        (album_dir / "2-01 Song C.flac").write_text("c")
        (album_dir / "2-02 Song D.flac").write_text("d")

        cache = UnifiedCacheManager(db_path=tmp_path / "flat_probe.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        d1 = [t for t in rel["tracks"] if t.get("disc_number") == 1 and t.get("status") == "found"]
        d2 = [t for t in rel["tracks"] if t.get("disc_number") == 2 and t.get("status") == "found"]

        # Expected: 2 tracks on Disc 1, 2 tracks on Disc 2, 0 missing tracks
        assert len(d2) == 2, f"CRITICAL BUG: Expected 2 tracks on Disc 2, but got {len(d2)}. Disc 2 was collapsed into Disc 1!"
        assert len(d1) == 2, f"CRITICAL BUG: Expected 2 tracks on Disc 1, but got {len(d1)}"
        assert rel["missing_count"] == 0, f"CRITICAL BUG: Expected 0 missing tracks, but gap analysis generated {rel['missing_count']} false missing tracks!"

    def test_bug_probe_3_flat_folder_multidisc_audit_causes_false_negatives(self, tmp_path):
        """
        BUG PROBE 3: In a flat album directory with disc-track prefixes (1-01, 1-02, 2-01, 2-02):
        When audited against MusicBrainz:
        - MusicBrainz Medium 2 tracks (disc 2) cannot match local files because local files have disc_number=1.
        - Reconciler Pass 2 skips matching due to `lt_disc != off_disc`.
        - Result: MusicBrainz Disc 2 tracks are falsely marked MISSING (false negatives!).
        - Local Disc 2 files are dumped as UNMATCHED BONUS TRACKS at the bottom.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Flat Audit Double"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Song 1.flac").write_text("1")
        (album_dir / "1-02 Song 2.flac").write_text("2")
        (album_dir / "2-01 Song 3.flac").write_text("3")
        (album_dir / "2-02 Song 4.flac").write_text("4")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-probe-3",
            "title": "Flat Audit Double",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 1", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Song 2", "recording": {"id": "r2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 3", "recording": {"id": "r3"}},
                        {"number": "2", "position": 2, "title": "Song 4", "recording": {"id": "r4"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "flat_probe_3.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        audited = service.audit_release(rel)
        assert audited["missing_count"] == 0, (
            f"CRITICAL BUG: Reconciler reported {audited['missing_count']} missing tracks! "
            f"Disc 2 tracks were marked missing despite existing on disk as '2-01' and '2-02'."
        )

    def test_bug_probe_4_disc_dir_pattern_fails_on_vinyl_side_and_bracketed_names(self):
        """
        BUG PROBE 4: DISC_DIR_PATTERN blindspots:
        1. 'Vinyl Side A', 'Vinyl Side B': Regex expects digits/letters directly after 'vinyl', not 'Side A'.
        2. 'Disc 1 (Remaster)', 'Disc 1 [Bonus]': Regex suffix requires '[-_.:]', rejecting parentheses and brackets.
        """
        folders = [
            "Vinyl Side A",
            "Vinyl Side B",
            "Disc 1 (Remaster)",
            "Disc 1 [Bonus Tracks]",
        ]
        failed_folders = [f for f in folders if not DISC_DIR_PATTERN.match(f)]
        assert not failed_folders, (
            f"CRITICAL BUG: DISC_DIR_PATTERN failed to match standard folder names: {failed_folders}"
        )
