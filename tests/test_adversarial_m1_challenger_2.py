"""
Independent Empirical Challenger 2 Verification Suite for Milestone 1 Remediation.

Focus areas:
1. Edge cases around DISC_DIR_PATTERN:
   - Vinyl Side A/B, LP Side 1/2, Disc 1 (Remaster), bracketed suffixes, and word numbers.
   - Negative cases: folder names that must NOT be matched as disc folders.
   - Grandparent album inference when disc subfolders match DISC_DIR_PATTERN.
2. Edge cases around multi-disc gap tracking with missing tracks on higher discs:
   - 4-disc and 5-disc album structures with gaps on Discs 2, 3, and 4.
   - Completely missing intermediate discs (e.g. Discs 1, 2, 4 present, Disc 3 completely missing).
   - Flat multi-disc directory prefix tracking (e.g. 3-01, 3-03, Side C, Side D).
   - Purely numeric track items on higher discs (3-01.flac, 4-02.flac).
3. Edge cases with version descriptors:
   - Live in Tokyo, Acoustic Version, Instrumental, VIP Mix, Piano Version, etc.
   - Songs with version keywords embedded in the base title (e.g. "Live and Let Die - Live in Tokyo").
   - are_versions_compatible matrix behavior across acoustic, live, remix, and instrumental types.
"""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from musicscraper.core.audio import (
    AudioMetadata,
    AudioQualityAnalyzer,
    DISC_DIR_PATTERN,
    SIDE_MAP,
    WORD_NUMS,
)
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.text import (
    strip_track_number_and_artist,
    are_versions_compatible,
    parse_track_title_structure,
    VERSION_DESCRIPTOR_RE,
)
from musicscraper.services.library import (
    LibraryReleaseService,
    parse_disc_and_track_number,
    _is_numeric_track_item,
)


# ============================================================================
# Section 1: DISC_DIR_PATTERN Edge Cases & Directory Heuristics
# ============================================================================

class TestDiscDirPatternChallenger:
    """Stress tests DISC_DIR_PATTERN matching and disc folder album heuristics."""

    @pytest.mark.parametrize("folder_name,expected_disc", [
        ("Vinyl Side A", 1),
        ("Vinyl Side B", 2),
        ("vinyl side a", 1),
        ("VINYL SIDE B", 2),
        ("Vinyl Side C", 3),
        ("Vinyl Side D", 4),
        ("Vinyl Side E", 5),
        ("Vinyl Side F", 6),
        ("Vinyl Side G", 7),
        ("Vinyl Side H", 8),
        ("LP Side 1", 1),
        ("LP Side 2", 2),
        ("lp side 1", 1),
        ("LP Side 3", 3),
        ("LP Side 4", 4),
        ("LP Side A", 1),
        ("LP Side B", 2),
        ("Disc 1 (Remaster)", 1),
        ("Disc 1 (Remastered 2024)", 1),
        ("Disc 1 [Bonus Tracks]", 1),
        ("Disc 1 - Bonus Disc", 1),
        ("Disc 1: Collector's Edition", 1),
        ("Disc 1. The Early Years", 1),
        ("Disc 1 _ Special Edition", 1),
        ("Disc 2 (Deluxe Edition)", 2),
        ("CD 1 (Remaster)", 1),
        ("CD 02 [Bonus]", 2),
        ("Vinyl Side A-B", 1),
        ("Side A", 1),
        ("Side B", 2),
        ("Side C", 3),
        ("Side D", 4),
        ("Side 1", 1),
        ("Side 2", 2),
        ("LP 1", 1),
        ("LP 2", 2),
        ("Disc One", 1),
        ("Disc Two", 2),
        ("Disc Three", 3),
        ("Disc Four", 4),
        ("Disc Five", 5),
        ("Disc Six", 6),
        ("Disc Seven", 7),
        ("Disc Eight", 8),
        ("Disc Nine", 9),
        ("Disc Ten", 10),
        ("CD 01", 1),
        ("CD 02", 2),
        ("CD03", 3),
        ("Disk 4", 4),
    ])
    def test_disc_dir_pattern_positive_matches(self, folder_name: str, expected_disc: int):
        """Verify DISC_DIR_PATTERN matches all standard and edge-case disc folder names."""
        m = DISC_DIR_PATTERN.match(folder_name)
        assert m is not None, f"DISC_DIR_PATTERN failed to match valid disc folder: '{folder_name}'"
        if m.group(1):
            disc_num = int(m.group(1))
        elif m.group(2):
            disc_num = SIDE_MAP.get(m.group(2).lower(), 1)
        elif m.group(3):
            disc_num = WORD_NUMS.get(m.group(3).lower(), 1)
        else:
            disc_num = None
        assert disc_num == expected_disc, (
            f"Expected disc {expected_disc} for '{folder_name}', got {disc_num}"
        )

    @pytest.mark.parametrize("invalid_folder", [
        "Discotheque",
        "Discourse",
        "Side by Side",
        "The Other Side of Town",
        "Dark Side of the Moon",
        "CD Baby",
        "Side-Effects",
        "LP Vinyl Records",
    ])
    def test_disc_dir_pattern_negative_matches(self, invalid_folder: str):
        """Verify DISC_DIR_PATTERN does not falsely match unrelated folder names."""
        m = DISC_DIR_PATTERN.match(invalid_folder)
        assert m is None, f"DISC_DIR_PATTERN falsely matched non-disc folder: '{invalid_folder}'"

    def test_disc_subfolder_album_inference_in_analyzer(self, tmp_path):
        """
        Verify AudioQualityAnalyzer infers the true album name from grandparent
        and disc number from parent folder for Vinyl Side A/B and Disc 1 (Remaster).
        """
        base_dir = tmp_path / "music" / "Pink Floyd" / "The Wall"
        side_a = base_dir / "Vinyl Side A"
        side_b = base_dir / "Vinyl Side B"
        remaster_dir = base_dir / "Disc 1 (Remaster)"

        side_a.mkdir(parents=True)
        side_b.mkdir(parents=True)
        remaster_dir.mkdir(parents=True)

        f_a = side_a / "01 In The Flesh.mp3"
        f_b = side_b / "01 Hey You.mp3"
        f_remaster = remaster_dir / "01 Another Brick.mp3"

        f_a.write_text("dummy audio")
        f_b.write_text("dummy audio")
        f_remaster.write_text("dummy audio")

        meta_a = AudioQualityAnalyzer.analyze_file(f_a)
        assert meta_a.disc_number == 1
        assert meta_a.album == "The Wall"

        meta_b = AudioQualityAnalyzer.analyze_file(f_b)
        assert meta_b.disc_number == 2
        assert meta_b.album == "The Wall"

        meta_remaster = AudioQualityAnalyzer.analyze_file(f_remaster)
        assert meta_remaster.disc_number == 1
        assert meta_remaster.album == "The Wall"


# ============================================================================
# Section 2: Multi-Disc Gap Tracking on Higher Discs
# ============================================================================

class TestMultiDiscGapTrackingChallenger:
    """Stress tests sequence gap tracking and audit reconciliation on higher discs."""

    def test_4disc_nested_gap_tracking_in_scan(self, tmp_path):
        """
        Verify 4-disc album folder hierarchy correctly tracks gaps independently on each disc:
        - Disc 1: tracks 1, 2, 3 (complete)
        - Disc 2: tracks 1, 3 (track 2 missing)
        - Disc 3: tracks 2, 4 (tracks 1 and 3 missing)
        - Disc 4: tracks 2, 3 (track 1 missing)
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "The Clash" / "Sandinista"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d3 = album_dir / "Disc 3"
        d4 = album_dir / "Disc 4"
        for d in (d1, d2, d3, d4):
            d.mkdir(parents=True)

        (d1 / "01 Track 1-1.flac").write_text("1")
        (d1 / "02 Track 1-2.flac").write_text("2")
        (d1 / "03 Track 1-3.flac").write_text("3")

        (d2 / "01 Track 2-1.flac").write_text("4")
        (d2 / "03 Track 2-3.flac").write_text("5")

        (d3 / "02 Track 3-2.flac").write_text("6")
        (d3 / "04 Track 3-4.flac").write_text("7")

        (d4 / "02 Track 4-2.flac").write_text("8")
        (d4 / "03 Track 4-3.flac").write_text("9")

        cache = UnifiedCacheManager(db_path=tmp_path / "clash.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["status"] == "has_missing"
        assert rel["found_count"] == 9
        assert rel["missing_count"] == 4  # D2T2, D3T1, D3T3, D4T1

        # Check Disc 1
        d1_tracks = [t for t in rel["tracks"] if t["disc_number"] == 1]
        assert len(d1_tracks) == 3
        assert all(t["status"] == "found" for t in d1_tracks)

        # Check Disc 2
        d2_missing = [t for t in rel["tracks"] if t["disc_number"] == 2 and t["status"] == "missing"]
        assert len(d2_missing) == 1
        assert d2_missing[0]["track_number"] == "2"
        assert d2_missing[0]["title"] == "Disc 2 Track 02 (Missing)"

        # Check Disc 3
        d3_missing = [t for t in rel["tracks"] if t["disc_number"] == 3 and t["status"] == "missing"]
        assert len(d3_missing) == 2
        assert {t["track_number"] for t in d3_missing} == {"1", "3"}
        assert {t["title"] for t in d3_missing} == {"Disc 3 Track 01 (Missing)", "Disc 3 Track 03 (Missing)"}

        # Check Disc 4
        d4_missing = [t for t in rel["tracks"] if t["disc_number"] == 4 and t["status"] == "missing"]
        assert len(d4_missing) == 1
        assert d4_missing[0]["track_number"] == "1"
        assert d4_missing[0]["title"] == "Disc 4 Track 01 (Missing)"

    def test_flat_folder_higher_disc_prefixes(self):
        """Verify parse_disc_and_track_number correctly handles higher discs (3 through 8)."""
        cases = [
            ("3-01 Song.flac", 3, 1),
            ("03-02 Song.flac", 3, 2),
            ("3.04 Song.flac", 3, 4),
            ("4-01 Song.flac", 4, 1),
            ("04-05 Song.flac", 4, 5),
            ("Side C 01 Song.flac", 3, 1),
            ("Side D 02 Song.flac", 4, 2),
            ("Side E 03 Song.flac", 5, 3),
            ("Side F 01 Song.flac", 6, 1),
            ("Side G 02 Song.flac", 7, 2),
            ("Side H 01 Song.flac", 8, 1),
            ("C1 Song.flac", 3, 1),
            ("D2 Song.flac", 4, 2),
            ("E03 Song.flac", 5, 3),
            ("Disc 3 - 01 Song.flac", 3, 1),
            ("CD 4 - 02 Song.flac", 4, 2),
        ]
        for fn, exp_d, exp_t in cases:
            # Without meta_disc
            d, t, _ = parse_disc_and_track_number(None, filename=fn, meta_disc=None)
            assert d == exp_d and t == exp_t, f"{fn} -> disc={d}, trk={t}, expected disc={exp_d}, trk={exp_t}"
            # With default meta_disc=1 (as passed from scan_library_releases)
            d1, t1, _ = parse_disc_and_track_number(None, filename=fn, meta_disc=1)
            assert d1 == exp_d and t1 == exp_t, f"{fn} with meta_disc=1 -> disc={d1}, trk={t1}, expected disc={exp_d}, trk={exp_t}"

    def test_audit_release_multidisc_completely_missing_disc(self, tmp_path):
        """
        Verify audit_release when an official release has 4 discs,
        and local files exist for Discs 1, 2, and 4, but Disc 3 is completely missing.
        All tracks of Disc 3 must be marked missing, with zero cross-disc pollution.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Four Disc Epic"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d4 = album_dir / "Disc 4"
        for d in (d1, d2, d4):
            d.mkdir(parents=True)

        (d1 / "01 Song 1.flac").write_text("1")
        (d1 / "02 Song 2.flac").write_text("2")

        (d2 / "01 Song 3.flac").write_text("3")
        (d2 / "03 Song 4.flac").write_text("4")

        (d4 / "02 Song 7.flac").write_text("7")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-4disc-complete-missing",
            "title": "Four Disc Epic",
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
                        {"number": "2", "position": 2, "title": "Song 3-Missing", "recording": {"id": "r3m"}},
                        {"number": "3", "position": 3, "title": "Song 4", "recording": {"id": "r4"}},
                    ]
                },
                {
                    "position": 3,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 5", "recording": {"id": "r5"}},
                        {"number": "2", "position": 2, "title": "Song 6", "recording": {"id": "r6"}},
                    ]
                },
                {
                    "position": 4,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 7-Missing", "recording": {"id": "r7m"}},
                        {"number": "2", "position": 2, "title": "Song 7", "recording": {"id": "r7"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "audit_4disc.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        rel = releases[0]
        rel["mb_release_id"] = "mb-4disc-complete-missing"

        audited = service.audit_release(rel)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 5
        assert audited["missing_count"] == 4  # Disc 2 trk 2, Disc 3 trk 1 & 2, Disc 4 trk 1

        # Verify Disc 3 has 0 found, 2 missing
        d3_tracks = [t for t in audited["tracks"] if t["disc_number"] == 3]
        assert len(d3_tracks) == 2
        assert all(t["status"] == "missing" for t in d3_tracks)
        assert [t["title"] for t in d3_tracks] == ["Song 5", "Song 6"]

        # Verify Disc 4 has 1 found (Song 7) and 1 missing (Song 7-Missing)
        d4_tracks = [t for t in audited["tracks"] if t["disc_number"] == 4]
        assert len(d4_tracks) == 2
        found_d4 = next(t for t in d4_tracks if t["status"] == "found")
        missing_d4 = next(t for t in d4_tracks if t["status"] == "missing")
        assert found_d4["title"] == "Song 7"
        assert found_d4["track_number"] == "2"
        assert missing_d4["title"] == "Song 7-Missing"
        assert missing_d4["track_number"] == "1"

    def test_purely_numeric_multidisc_higher_discs_audit(self, tmp_path):
        """
        Verify flat folder purely numeric files on higher discs (3-01.flac, 3-02.flac, 4-01.flac)
        reconcile cleanly in audit_release without similarity penalty.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Electronic Artist" / "Catalog Numbers"
        album_dir.mkdir(parents=True)

        (album_dir / "3-01.flac").write_text("1")
        (album_dir / "3-03.flac").write_text("3")
        (album_dir / "4-01.flac").write_text("4")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-num-higher-discs",
            "title": "Catalog Numbers",
            "artist-credit": [{"name": "Electronic Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 3,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Modular Pulse", "recording": {"id": "r31"}},
                        {"number": "2", "position": 2, "title": "Resonant Filter", "recording": {"id": "r32"}},
                        {"number": "3", "position": 3, "title": "Sine Wave", "recording": {"id": "r33"}},
                    ]
                },
                {
                    "position": 4,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Sub Bass", "recording": {"id": "r41"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "num_higher.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        rel = releases[0]
        rel["mb_release_id"] = "mb-num-higher-discs"

        audited = service.audit_release(rel)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 3
        assert audited["missing_count"] == 1  # Disc 3 Track 2 is missing

        # Verify matched tracks
        d3_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "1")
        d3_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "2")
        d3_t3 = next(t for t in audited["tracks"] if t["disc_number"] == 3 and t["track_number"] == "3")
        d4_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 4 and t["track_number"] == "1")

        assert d3_t1["status"] == "found" and d3_t1["title"] == "Modular Pulse"
        assert d3_t2["status"] == "missing" and d3_t2["title"] == "Resonant Filter"
        assert d3_t3["status"] == "found" and d3_t3["title"] == "Sine Wave"
        assert d4_t1["status"] == "found" and d4_t1["title"] == "Sub Bass"


# ============================================================================
# Section 3: Version Descriptors & Title Structure Stress Tests
# ============================================================================

class TestVersionDescriptorsChallenger:
    """Stress tests version descriptor preservation and compatibility."""

    @pytest.mark.parametrize("filename,expected_title", [
        ("01 Song - Live in Tokyo.mp3", "Song (Live in Tokyo)"),
        ("02 Song - Live at Wembley.flac", "Song (Live at Wembley)"),
        ("03 Song - Live 1999.mp3", "Song (Live 1999)"),
        ("04 Song - Live Version.flac", "Song (Live Version)"),
        ("05 Song – Acoustic Version.mp3", "Song (Acoustic Version)"),
        ("06 Song - Acoustic.flac", "Song (Acoustic)"),
        ("07 Song — Instrumental.flac", "Song (Instrumental)"),
        ("08 Song - Unplugged.mp3", "Song (Unplugged)"),
        ("09 Song - Unplugged Version.flac", "Song (Unplugged Version)"),
        ("10 Song - Piano Version.mp3", "Song (Piano Version)"),
        ("11 Song - VIP Mix.flac", "Song (VIP Mix)"),
        ("12 Song - VIP.mp3", "Song (VIP)"),
        ("13 Song - Radio Edit.flac", "Song (Radio Edit)"),
        ("14 Song - Extended Mix.mp3", "Song (Extended Mix)"),
        ("15 Song - Remaster.flac", "Song (Remaster)"),
        ("16 Song - Remastered.mp3", "Song (Remastered)"),
        ("17 Song - Digital Remaster.flac", "Song (Digital Remaster)"),
        ("18 Song - Anniversary Edition.mp3", "Song (Anniversary Edition)"),
        ("19 Song - Deluxe Edition.flac", "Song (Deluxe Edition)"),
        ("20 Song - Deluxe Version.mp3", "Song (Deluxe Version)"),
        ("21 Song - Bonus Track.flac", "Song (Bonus Track)"),
        ("22 Song - Demo.mp3", "Song (Demo)"),
        ("23 Song - Alt Take.flac", "Song (Alt Take)"),
        ("24 Song - Alternate Take.mp3", "Song (Alternate Take)"),
        ("25 Song - Alt Mix.flac", "Song (Alt Mix)"),
        ("26 Song - Rough Mix.mp3", "Song (Rough Mix)"),
        ("27 Song - Club Mix.flac", "Song (Club Mix)"),
        ("28 Song - Dub Mix.mp3", "Song (Dub Mix)"),
        ("29 Song - Acapella.flac", "Song (Acapella)"),
        ("30 Song - Vocal Version.mp3", "Song (Vocal Version)"),
        ("31 Song - Off Vocal.flac", "Song (Off Vocal)"),
        ("32 Song - Karaoke.mp3", "Song (Karaoke)"),
        ("33 Song - Backing Track.flac", "Song (Backing Track)"),
        ("34 Song - Original Mix.mp3", "Song (Original Mix)"),
        ("35 Song - Album Version.flac", "Song (Album Version)"),
        ("36 Song - Sped Up.mp3", "Song (Sped Up)"),
        ("37 Song - Slowed.flac", "Song (Slowed)"),
        ("38 Song - Nightcore.mp3", "Song (Nightcore)"),
    ])
    def test_strip_track_number_and_artist_version_descriptors(self, filename: str, expected_title: str):
        """Verify strip_track_number_and_artist correctly formats version descriptors with song titles."""
        result = strip_track_number_and_artist(filename)
        assert result == expected_title, f"For '{filename}', expected '{expected_title}', got '{result}'"

    def test_embedded_version_keywords_in_song_title(self):
        """
        Verify song titles containing words like 'Live' or 'Acoustic' are not mangled
        when followed by a version descriptor separator.
        """
        cases = [
            ("01 Artist - Live and Let Die - Live in Tokyo.mp3", "Live and Let Die (Live in Tokyo)"),
            ("02 Artist - Acoustic Guitar Solos - Acoustic Version.flac", "Acoustic Guitar Solos (Acoustic Version)"),
            ("03 Artist - Stay with Me - Piano Version.mp3", "Stay with Me (Piano Version)"),
            ("04 Artist - Born to Be Alive - Instrumental.flac", "Born to Be Alive (Instrumental)"),
            ("05 Artist - Demo Tape Blues - Demo.mp3", "Demo Tape Blues (Demo)"),
        ]
        for fn, exp in cases:
            res = strip_track_number_and_artist(fn)
            assert res == exp, f"For '{fn}', expected '{exp}', got '{res}'"

    def test_version_compatibility_matrix(self):
        """
        Verify are_versions_compatible accurately enforces compatibility rules:
        - Same version type with equivalent modifiers -> True
        - Same version type with conflicting modifiers -> False
        - Cross-version type mismatches (Live vs Acoustic, Instrumental vs Studio) -> False
        """
        # Compatible cases
        assert are_versions_compatible("acoustic", "acoustic", "acoustic", "acoustic version") is True
        assert are_versions_compatible("acoustic", "acoustic", "acoustic", "unplugged") is True
        assert are_versions_compatible("live", "live in tokyo", "live", "live in tokyo") is True
        assert are_versions_compatible("live", "live", "live", "live in tokyo") is True
        assert are_versions_compatible("instrumental", "instrumental", "instrumental", "instrumental") is True
        assert are_versions_compatible("remix", "vip mix", "remix", "vip") is True
        assert are_versions_compatible(None, None, None, None) is True

        # Incompatible cases
        # Live in different locations
        assert are_versions_compatible("live", "live in tokyo", "live", "live at wembley") is False
        # Acoustic vs Live
        assert are_versions_compatible("acoustic", "acoustic", "live", "live") is False
        # Instrumental vs Studio (None)
        assert are_versions_compatible("instrumental", "instrumental", None, None) is False
        # Studio (None) vs Live
        assert are_versions_compatible(None, None, "live", "live") is False
        # Remix vs Acoustic
        assert are_versions_compatible("remix", "vip mix", "acoustic", "acoustic") is False
        # Different remixes
        assert are_versions_compatible("remix", "club mix", "remix", "radio edit") is False
