"""
Unit tests for AudioFileScanner directory filtering and DiscographyReconciler matching.
"""

from pathlib import Path
from musicscraper.services.auditor import AudioFileScanner
from musicscraper.clients.musicbrainz import ArtistCatalog


def test_audio_file_scanner_skips_trash(tmp_path):
    # Setup dummy directory structure
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    album_dir = music_dir / "Album"
    album_dir.mkdir()
    (album_dir / "01 track.mp3").write_text("dummy audio")

    trash_dir = music_dir / ".Trash-1000" / "files"
    trash_dir.mkdir(parents=True)
    (trash_dir / "02 deleted.mp3").write_text("dummy audio")

    git_dir = music_dir / ".git" / "objects"
    git_dir.mkdir(parents=True)
    (git_dir / "03 git.mp3").write_text("dummy audio")

    catalog = ArtistCatalog({
        "artist": {"id": "dummy-mbid", "name": "Artist"},
        "releases_artist": [],
        "releases_track_artist": [],
        "recordings": []
    })

    scanner = AudioFileScanner(music_dir=music_dir, catalog=catalog, full_scan=True)
    tracks = scanner.scan()

    paths = [t["path"] for t in tracks]
    assert any("Album" in p for p in paths)
    assert not any(".Trash" in p for p in paths)
    assert not any(".git" in p for p in paths)
