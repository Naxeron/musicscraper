"""
Unit tests for directory cleaning service.
"""

import tempfile
from pathlib import Path
import pytest

from musicscraper.services.cleaner import FolderCleanerService


def test_folder_cleaner_service():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)

        # 1. Folder with music
        music_folder = base / "Artist" / "Album1"
        music_folder.mkdir(parents=True, exist_ok=True)
        (music_folder / "01.mp3").write_bytes(b"dummy audio content")

        # 2. Empty folder
        empty_folder = base / "EmptyArtist" / "EmptyAlbum"
        empty_folder.mkdir(parents=True, exist_ok=True)

        # 3. Folder with only junk files
        junk_folder = base / "JunkArtist" / "JunkAlbum"
        junk_folder.mkdir(parents=True, exist_ok=True)
        (junk_folder / ".DS_Store").write_bytes(b"junk")
        (junk_folder / "Thumbs.db").write_bytes(b"junk")

        cleaner = FolderCleanerService()

        # Check has_music_files
        assert cleaner.has_music_files(music_folder) is True
        assert cleaner.has_music_files(empty_folder) is False
        assert cleaner.has_music_files(junk_folder) is False

        # Test dry-run
        deleted_dry, scanned = cleaner.clean(base, dry_run=True)
        assert len(deleted_dry) >= 2
        assert empty_folder.exists() is True
        assert junk_folder.exists() is True

        # Test actual deletion
        deleted_real, _ = cleaner.clean(base, dry_run=False)
        assert empty_folder.exists() is False
        assert junk_folder.exists() is False
        assert music_folder.exists() is True
        assert (music_folder / "01.mp3").exists() is True
