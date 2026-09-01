"""
Unit tests for LibraryReleaseService discovery, reconciliation, and missing track downloads.
"""

from pathlib import Path
from unittest.mock import MagicMock

from musicscraper.core.audio import AudioMetadata
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.services.library import LibraryReleaseService


def test_scan_library_releases_grouping(tmp_path):
    """Verifies that audio files in library are properly grouped into releases with stats."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # Create dummy album 1: Aphex Twin - Selected Ambient Works
    saw_dir = music_dir / "Aphex Twin - Selected Ambient Works"
    saw_dir.mkdir()
    (saw_dir / "01 Xtal.flac").write_text("dummy")
    (saw_dir / "02 Tha.flac").write_text("dummy")

    # Create dummy album 2: Boards of Canada - Music Has the Right to Children
    boc_dir = music_dir / "Boards of Canada" / "MHTRTC"
    boc_dir.mkdir(parents=True)
    (boc_dir / "01 Wildlife Analysis.mp3").write_text("dummy")

    # Pre-populate cache with audio metadata
    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=saw_dir / "01 Xtal.flac",
        title="Xtal",
        artist="Aphex Twin",
        album="Selected Ambient Works 85-92",
        track_number="1/13",
        year="1992",
        format_label="FLAC",
        is_lossless=True,
        bitrate_kbps=900,
        quality_score=90
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=saw_dir / "02 Tha.flac",
        title="Tha",
        artist="Aphex Twin",
        album="Selected Ambient Works 85-92",
        track_number="2/13",
        year="1992",
        format_label="FLAC",
        is_lossless=True,
        bitrate_kbps=850,
        quality_score=90
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=boc_dir / "01 Wildlife Analysis.mp3",
        title="Wildlife Analysis",
        artist="Boards of Canada",
        album="Music Has the Right to Children",
        track_number="1/18",
        year="1998",
        format_label="MP3",
        is_lossless=False,
        bitrate_kbps=320,
        quality_score=60
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 2

    # Check Aphex Twin release
    saw_rel = next(r for r in releases if "Aphex" in r["artist"])
    assert saw_rel["title"] == "Selected Ambient Works 85-92"
    assert saw_rel["year"] == "1992"
    assert saw_rel["found_count"] == 2
    assert saw_rel["total_tracks_expected"] == 13
    assert saw_rel["missing_count"] == 11
    assert saw_rel["status"] == "has_missing"
    assert "FLAC" in saw_rel["formats"]

    # Check BoC release
    boc_rel = next(r for r in releases if "Boards" in r["artist"])
    assert boc_rel["title"] == "Music Has the Right to Children"
    assert boc_rel["found_count"] == 1
    assert boc_rel["total_tracks_expected"] == 18
    assert boc_rel["missing_count"] == 17
    assert boc_rel["status"] == "has_missing"


def test_audit_release_reconciliation():
    """Tests auditing a release against MusicBrainz tracklist to classify found vs missing tracks."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Mock MusicBrainz release response
    mb_mock.get_release_by_id.return_value = {
        "id": "rel-mbid-1234",
        "title": "Selected Ambient Works 85-92",
        "medium-list": [{
            "position": 1,
            "track-list": [
                {"position": 1, "number": "1", "title": "Xtal", "recording": {"id": "rec-1", "title": "Xtal"}},
                {"position": 2, "number": "2", "title": "Tha", "recording": {"id": "rec-2", "title": "Tha"}},
                {"position": 3, "number": "3", "title": "Pulsewidth", "recording": {"id": "rec-3", "title": "Pulsewidth"}},
                {"position": 4, "number": "4", "title": "Ageispolis", "recording": {"id": "rec-4", "title": "Ageispolis"}},
            ]
        }]
    }

    local_release = {
        "id": "test-rel-id",
        "title": "Selected Ambient Works 85-92",
        "artist": "Aphex Twin",
        "mb_release_id": "rel-mbid-1234",
        "tracks": [
            {
                "filename": "01 Xtal.flac",
                "title": "Xtal",
                "track_number": "1",
                "format": "FLAC",
                "bitrate": 900,
                "is_lossless": True,
                "quality_score": 90,
                "status": "found"
            },
            {
                "filename": "02 Tha.flac",
                "title": "Tha",
                "track_number": "2",
                "format": "FLAC",
                "bitrate": 850,
                "is_lossless": True,
                "quality_score": 90,
                "status": "found"
            }
        ]
    }

    audited = service.audit_release(local_release)
    assert audited["total_tracks_expected"] == 4
    assert audited["found_count"] == 2
    assert audited["missing_count"] == 2
    assert audited["completion_pct"] == 50.0
    assert audited["status"] == "has_missing"

    tracks = audited["tracks"]
    assert len(tracks) == 4
    assert tracks[0]["title"] == "Xtal" and tracks[0]["status"] == "found"
    assert tracks[1]["title"] == "Tha" and tracks[1]["status"] == "found"
    assert tracks[2]["title"] == "Pulsewidth" and tracks[2]["status"] == "missing"
    assert tracks[3]["title"] == "Ageispolis" and tracks[3]["status"] == "missing"


def test_download_missing_tracks_orchestration():
    """Tests Soulseek missing track downloading via folder matching and individual search."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Peer directory search result containing the missing tracks
    slsk_mock.search.return_value = {
        "responses": [
            {
                "username": "AmbientFan",
                "files": [
                    {"filename": "Music\\Aphex Twin - SAW\\03 Pulsewidth.flac", "size": 25000000},
                    {"filename": "Music\\Aphex Twin - SAW\\04 Ageispolis.flac", "size": 30000000},
                ]
            }
        ]
    }

    missing_tracks = [
        {"title": "Pulsewidth", "track_number": "3"},
        {"title": "Ageispolis", "track_number": "4"}
    ]

    res = service.download_missing_tracks(
        artist="Aphex Twin",
        release_title="Selected Ambient Works 85-92",
        missing_tracks=missing_tracks,
        preferred_format="flac",
        dry_run=False
    )

    assert res["total_missing"] == 2
    assert res["queued_count"] == 2
    assert res["resolved_count"] == 2
    slsk_mock.enqueue_download.assert_called_once()
    args = slsk_mock.enqueue_download.call_args[0]
    assert args[0] == "AmbientFan"
    assert len(args[1]) == 2


def test_scan_library_releases_gap_detection(tmp_path):
    """Tests automatic sequence gap detection when tags do not have total tracks."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    album_dir = music_dir / "Autechre - Tri Repetae"
    album_dir.mkdir()
    (album_dir / "01 Dael.flac").write_text("dummy")
    (album_dir / "02 Clipper.flac").write_text("dummy")
    (album_dir / "04 Leterel.flac").write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "01 Dael.flac",
        title="Dael",
        artist="Autechre",
        album="Tri Repetae",
        track_number="1",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "02 Clipper.flac",
        title="Clipper",
        artist="Autechre",
        album="Tri Repetae",
        track_number="2",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=album_dir / "04 Leterel.flac",
        title="Leterel",
        artist="Autechre",
        album="Tri Repetae",
        track_number="4",
        year="1995",
        format_label="FLAC",
        is_lossless=True,
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 1
    rel = releases[0]
    assert rel["title"] == "Tri Repetae"
    assert rel["found_count"] == 3
    assert rel["total_tracks_expected"] == 4
    assert rel["missing_count"] == 1
    assert rel["status"] == "has_missing"

    # Track 3 should be marked as missing
    t3 = next((t for t in rel["tracks"] if t.get("track_number") == "3"), None)
    assert t3 is not None
    assert t3["status"] == "missing"


def test_download_single_missing_track():
    """Tests downloading a single missing track from Soulseek."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [
            {
                "username": "IDM_Collector",
                "files": [
                    {"filename": "\\Shared\\Aphex Twin - Pulsewidth.flac", "size": 24000000, "bitRate": 1411}
                ]
            }
        ]
    }

    res = service.download_single_missing_track(
        artist="Aphex Twin",
        release_title="Selected Ambient Works 85-92",
        track_title="Pulsewidth",
        dry_run=False
    )

    assert res["success"] is True
    assert res["user"] == "IDM_Collector"
    assert "Pulsewidth" in res["filename"]
    slsk_mock.enqueue_download.assert_called_once()


def test_scan_library_various_artists_compilation(tmp_path):
    """Verifies that compilation releases are properly identified as Various Artists even with 1 track."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # 1. Folder with VA directory marker: "VA - Amen Destroyer" with 1 track by Exnoiz
    va_dir = music_dir / "VA - Amen Destroyer"
    va_dir.mkdir()
    (va_dir / "01 - Exnoiz - Destruction.mp3").write_text("dummy")

    # 2. Folder with multiple tracks by different artists
    comp_dir = music_dir / "Breakcore Sampler"
    comp_dir.mkdir()
    (comp_dir / "01 - Bong-Ra - Jungle.flac").write_text("dummy")
    (comp_dir / "02 - Venetian Snares - Hajnal.flac").write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    cache.store_audio_metadata(AudioMetadata(
        path=va_dir / "01 - Exnoiz - Destruction.mp3",
        title="Destruction",
        artist="Exnoiz",
        album_artist="Various Artists",
        album="Amen Destroyer",
        track_number="1/10",
        year="2004",
        format_label="MP3",
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=comp_dir / "01 - Bong-Ra - Jungle.flac",
        title="Jungle",
        artist="Bong-Ra",
        album="Breakcore Sampler",
        track_number="1/2",
        format_label="FLAC",
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=comp_dir / "02 - Venetian Snares - Hajnal.flac",
        title="Hajnal",
        artist="Venetian Snares",
        album="Breakcore Sampler",
        track_number="2/2",
        format_label="FLAC",
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    assert len(releases) == 2
    amen_rel = next(r for r in releases if r["title"] == "Amen Destroyer")
    assert amen_rel["artist"] == "Various Artists"
    assert amen_rel["is_va"] is True
    assert amen_rel["tracks"][0]["artist"] == "Exnoiz"

    sampler_rel = next(r for r in releases if r["title"] == "Breakcore Sampler")
    assert sampler_rel["artist"] == "Various Artists"
    assert sampler_rel["is_va"] is True
    assert len(sampler_rel["tracks"]) == 2


def test_audit_release_various_artists_reconciliation():
    """Tests auditing a release where local had 1 track by Exnoiz and MusicBrainz returns Various Artists with distinct track artists."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Mock MusicBrainz response for Amen Destroyer (Various Artists compilation)
    mb_mock.search_release.return_value = [
        {"id": "amen-mbid-9999", "title": "Amen Destroyer", "artist-credit": [{"name": "Various Artists"}]}
    ]
    mb_mock.get_release_by_id.return_value = {
        "id": "amen-mbid-9999",
        "title": "Amen Destroyer",
        "artist-credit": [{"name": "Various Artists"}],
        "release-group": {"primary-type": "Compilation"},
        "medium-list": [{
            "position": 1,
            "track-list": [
                {
                    "position": 1, "number": "1", "title": "Destruction",
                    "artist-credit": [{"name": "Exnoiz"}],
                    "recording": {"id": "rec-exnoiz", "title": "Destruction"}
                },
                {
                    "position": 2, "number": "2", "title": "Amen Terror",
                    "artist-credit": [{"name": "Venetian Snares"}],
                    "recording": {"id": "rec-snares", "title": "Amen Terror"}
                },
                {
                    "position": 3, "number": "3", "title": "Mashup Core",
                    "artist-credit": [{"name": "Bong-Ra"}],
                    "recording": {"id": "rec-bongra", "title": "Mashup Core"}
                },
            ]
        }]
    }

    # Local release was mistakenly identified with artist="Exnoiz" because only 1 track was downloaded
    local_release = {
        "id": "local-amen-id",
        "title": "Amen Destroyer",
        "artist": "Exnoiz",
        "tracks": [
            {
                "filename": "01 - Exnoiz - Destruction.mp3",
                "title": "Destruction",
                "artist": "Exnoiz",
                "track_number": "1",
                "format": "MP3",
                "status": "found"
            }
        ]
    }

    audited = service.audit_release(local_release)
    assert audited["artist"] == "Various Artists"
    assert audited["is_va"] is True
    assert audited["found_count"] == 1
    assert audited["missing_count"] == 2
    assert audited["status"] == "has_missing"

    # Verify track artists: Track 1 is Exnoiz, Missing Track 2 is Venetian Snares, Missing Track 3 is Bong-Ra
    tracks = audited["tracks"]
    assert len(tracks) == 3
    assert tracks[0]["title"] == "Destruction" and tracks[0]["artist"] == "Exnoiz" and tracks[0]["status"] == "found"
    assert tracks[1]["title"] == "Amen Terror" and tracks[1]["artist"] == "Venetian Snares" and tracks[1]["status"] == "missing"
    assert tracks[2]["title"] == "Mashup Core" and tracks[2]["artist"] == "Bong-Ra" and tracks[2]["status"] == "missing"


def test_download_missing_tracks_various_artists_orchestration():
    """Verifies that downloading missing tracks for a compilation searches for each track's actual artist rather than the release artist."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Soulseek batch search responses for individual missing tracks
    slsk_mock.search.return_value = {"responses": []}  # No peer directory match, so goes to Stage 2
    slsk_mock.batch_search.return_value = {
        "Venetian Snares - Amen Terror": {
            "responses": [{
                "username": "BreakcoreHead",
                "files": [{"filename": "Music\\Venetian Snares - Amen Terror.flac", "size": 30000000}]
            }]
        },
        "Bong-Ra - Mashup Core": {
            "responses": [{
                "username": "JungleJunkie",
                "files": [{"filename": "Music\\Bong-Ra - Mashup Core.flac", "size": 28000000}]
            }]
        }
    }

    missing_tracks = [
        {"title": "Amen Terror", "artist": "Venetian Snares", "track_number": "2"},
        {"title": "Mashup Core", "artist": "Bong-Ra", "track_number": "3"},
    ]

    res = service.download_missing_tracks(
        artist="Various Artists",
        release_title="Amen Destroyer",
        missing_tracks=missing_tracks,
        preferred_format="flac",
        dry_run=False
    )

    assert res["total_missing"] == 2
    assert res["queued_count"] == 2
    assert res["resolved_count"] == 2

    # Verify that Soulseek batch_search was called with the individual track artists, NOT "Various Artists" or "Exnoiz"
    slsk_mock.batch_search.assert_called_once()
    queries = slsk_mock.batch_search.call_args[0][0]
    assert "Venetian Snares - Amen Terror" in queries
    assert "Bong-Ra - Mashup Core" in queries
    assert not any("Exnoiz" in q for q in queries)
    assert not any("Various Artists" in q for q in queries)


def test_download_single_missing_track_with_track_artist():
    """Verifies that downloading a single track on a VA release searches for the track artist rather than 'Various Artists'."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "BreakcoreHead",
            "files": [{"filename": "Music\\Venetian Snares - Amen Terror.flac", "size": 30000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="Various Artists",
        release_title="Amen Destroyer",
        track_title="Amen Terror",
        track_artist="Venetian Snares",
        dry_run=False
    )

    assert res["success"] is True
    assert res["artist"] == "Venetian Snares"
    # Verify search query was for "Venetian Snares Amen Terror", NOT "Various Artists Amen Terror"
    slsk_mock.search.assert_called_once()
    query = slsk_mock.search.call_args[1]["query"]
    assert query == "Venetian Snares Amen Terror"


def test_download_single_missing_track_va_without_track_artist():
    """Verifies that downloading a single track on a VA release without a track artist queries 'Various Artists <Track Title>'."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "CompilationHoarder",
            "files": [{"filename": "Music\\VA - Amen Destroyer\\05 - Hard Track.flac", "size": 30000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="Various Artists",
        release_title="Amen Destroyer",
        track_title="Hard Track",
        track_artist=None,
        dry_run=False
    )

    assert res["success"] is True
    assert res["artist"] == "Various Artists"
    slsk_mock.search.assert_called_once()
    query = slsk_mock.search.call_args[1]["query"]
    assert query == "Various Artists Hard Track"


def test_scan_library_unifies_mbid_tagged_track_with_untagged_downloads(tmp_path):
    """Verifies that an album with 1 MBID-tagged track in Library/ and untagged tracks in downloads/ unifies into one release."""
    music_dir = tmp_path / "music"
    lib_dir = music_dir / "Library" / "Various Artists" / "新しいフォルダー (10)"
    dl_dir = music_dir / "downloads" / "Various Artitsts - 新しいフォルダー (10)"
    lib_dir.mkdir(parents=True)
    dl_dir.mkdir(parents=True)

    f1 = lib_dir / "30 exnoiz - re_Control.flac"
    f2 = dl_dir / "01 DJ - Track 1.flac"
    f3 = dl_dir / "02 Producer - Track 2.flac"
    f1.write_text("dummy")
    f2.write_text("dummy")
    f3.write_text("dummy")

    cache_db = tmp_path / "cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    # Track in Library/ has MBID tag
    cache.store_audio_metadata(AudioMetadata(
        path=f1,
        title="re_Control",
        artist="exnoiz",
        album_artist="Various Artists",
        album="新しいフォルダー (10)",
        track_number="30/30",
        format_label="FLAC",
        mb_release_ids={"062bbccc-346a-49af-b4c8-3037db346d56"}
    ))
    # Tracks in downloads/ do NOT have MBID tag and have "Various Artitsts" typo in album artist
    cache.store_audio_metadata(AudioMetadata(
        path=f2,
        title="Track 1",
        artist="DJ",
        album_artist="Various Artitsts",
        album="新しいフォルダー (10)",
        track_number="1/30",
        format_label="FLAC"
    ))
    cache.store_audio_metadata(AudioMetadata(
        path=f3,
        title="Track 2",
        artist="Producer",
        album_artist="Various Artitsts",
        album="新しいフォルダー (10)",
        track_number="2/30",
        format_label="FLAC"
    ))

    service = LibraryReleaseService(cache_manager=cache)
    releases = service.scan_library_releases(library_dir=music_dir, force_rescan=False)

    # Must be unified into exactly ONE release, NOT split into two
    matching = [r for r in releases if r["title"] == "新しいフォルダー (10)"]
    assert len(matching) == 1
    rel = matching[0]
    assert rel["artist"] == "Various Artists"
    assert rel["found_count"] == 3
    assert rel["mb_release_id"] == "062bbccc-346a-49af-b4c8-3037db346d56"
    assert "Library" in rel["folder_path"]


def test_download_single_missing_track_resolves_placeholder_title():
    """Verifies that downloading a placeholder 'Track 01 (Missing)' resolves the official title via MusicBrainz."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # MusicBrainz search returns release
    mb_mock.search_release.return_value = [{"id": "mb-rel-1", "title": "propa bo! EP"}]
    mb_mock.get_release_by_id.return_value = {
        "id": "mb-rel-1",
        "title": "propa bo! EP",
        "artist-credit": [{"name": "goreshit"}],
        "medium-list": [{
            "position": 1,
            "track-list": [
                {"number": "1", "recording": {"title": "take you"}},
                {"number": "2", "recording": {"title": "propa bo!"}}
            ]
        }]
    }

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "peer1",
            "files": [{"filename": "goreshit\\propa bo! EP\\01 take you.flac", "size": 20000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="goreshit",
        release_title="propa bo! EP",
        track_title="Track 01 (Missing)",
        dry_run=True
    )

    assert res["success"] is True
    assert res["track"] == "take you"
    slsk_mock.search.assert_called_once()
    assert slsk_mock.search.call_args[1]["query"] == "goreshit take you"

