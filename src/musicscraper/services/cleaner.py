"""
Empty and non-music directory cleaner with safe dry-run preview and bottom-up traversal.
"""

import os
import shutil
from pathlib import Path
from typing import Set, List, Tuple, Optional

from musicscraper.core.constants import AUDIO_AND_ARCHIVE_EXTENSIONS, COMMON_JUNK_FILES
from musicscraper.core.report import console


class FolderCleanerService:
    """Recursively scans a target directory and removes folders lacking music files."""

    def __init__(
        self,
        valid_extensions: Optional[Set[str]] = None,
        junk_filenames: Optional[Set[str]] = None
    ):
        self.valid_extensions = valid_extensions or set(AUDIO_AND_ARCHIVE_EXTENSIONS)
        self.junk_filenames = junk_filenames or set(COMMON_JUNK_FILES)

    def has_music_files(self, dir_path: Path) -> bool:
        """Checks if a directory (or any child directory) contains at least one audio/archive file."""
        for root, _, files in os.walk(dir_path):
            for f in files:
                ext = os.path.splitext(f.lower())[1]
                if ext in self.valid_extensions:
                    return True
        return False

    def clean(
        self,
        target_dir: Path,
        dry_run: bool = True,
        verbose: bool = False
    ) -> Tuple[List[Path], int]:
        """
        Traverses target_dir bottom-up and deletes folders that contain no music files.
        Returns (deleted_paths, total_folders_scanned).
        """
        target_dir = Path(target_dir).resolve()
        if not target_dir.is_dir():
            console.print(f"[red]Error: Target directory does not exist: {target_dir}[/red]")
            return [], 0

        deleted_folders: List[Path] = []
        total_scanned = 0

        # Bottom-up walk so child directories are evaluated before parents
        for root, dirs, _ in os.walk(str(target_dir), topdown=False):
            for d in dirs:
                dir_path = Path(root) / d
                total_scanned += 1

                if not dir_path.exists():
                    continue

                if not self.has_music_files(dir_path):
                    deleted_folders.append(dir_path)
                    if dry_run:
                        console.print(f"[yellow][DRY RUN] Would delete:[/yellow] {dir_path}")
                    else:
                        try:
                            shutil.rmtree(dir_path)
                            console.print(f"[red][DELETED] Removed:[/red] {dir_path}")
                        except Exception as e:
                            console.print(f"[red]Failed to delete {dir_path}: {e}[/red]")
                elif verbose:
                    console.print(f"[dim][KEEP] Contains music: {dir_path}[/dim]")

        return deleted_folders, total_scanned
