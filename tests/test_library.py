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

