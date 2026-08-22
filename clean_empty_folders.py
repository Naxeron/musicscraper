#!/usr/bin/env python3
"""
Clean Empty & Non-Music Folders
===============================
Recursively scans a target directory and removes any folders that do not contain
any music files (e.g., empty directories, folders with only leftover artwork, text files, or OS junk).

Features:
- Bottom-up traversal to cleanly remove nested empty directory trees.
- Comprehensive list of audio and music archive extensions (.mp3, .flac, .wav, .zip, etc.).
- Safe dry-run mode (--dry-run) to preview deletions before executing.
- Summary report of cleaned folders and freed paths.
"""

import os
import sys
import shutil
import argparse
import logging
from typing import Set, List, Tuple

# Default music / audio file extensions
DEFAULT_MUSIC_EXTENSIONS = {
    # Audio formats
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus",
    ".alac", ".aiff", ".aif", ".wma", ".mid", ".midi", ".mod",
    # Music release archives
    ".zip", ".rar", ".7z", ".tar", ".gz"
}

# Common junk / metadata files that should not prevent a folder from being considered "empty of music"
COMMON_JUNK_FILES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".gitkeep", ".directory"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("clean_folders")


def has_music_files(dir_path: str, valid_extensions: Set[str]) -> bool:
    """
    Checks if a directory (or any of its subdirectories) contains at least one music file.
    """
    for root, _, files in os.walk(dir_path):
        for f in files:
            _, ext = os.path.splitext(f.lower())
            if ext in valid_extensions:
                return True
    return False


def clean_folders(
    target_dir: str,
    valid_extensions: Set[str],
    dry_run: bool = True,
    verbose: bool = False
) -> Tuple[List[str], int]:
    """
    Traverses target_dir bottom-up and deletes any folder that contains no music files.
    Returns (deleted_folders_list, total_folders_scanned).
    """
    target_dir = os.path.abspath(target_dir)
    if not os.path.isdir(target_dir):
        logger.error(f"Target directory does not exist: {target_dir}")
        return [], 0

    deleted_folders = []
    total_scanned = 0

    # Bottom-up walk so subdirectories are processed before their parents
    for root, dirs, _ in os.walk(target_dir, topdown=False):
        for d in dirs:
            dir_path = os.path.join(root, d)
            total_scanned += 1

            if not os.path.exists(dir_path):
                # May have been deleted as part of a parent/child operation
                continue

            if not has_music_files(dir_path, valid_extensions):
                deleted_folders.append(dir_path)
                if dry_run:
                    logger.info(f"[DRY RUN] Would delete: {dir_path}")
                else:
                    try:
                        shutil.rmtree(dir_path)
                        logger.info(f"[DELETED] Removed: {dir_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete {dir_path}: {e}")
            elif verbose:
                logger.debug(f"[KEEP] Contains music: {dir_path}")

    return deleted_folders, total_scanned


def main():
    default_dir = "./downloads" if os.path.exists("./downloads") else "."

    parser = argparse.ArgumentParser(
        description="Clean and delete folders that contain no music files."
    )
    parser.add_argument(
        "dir",
        nargs="?",
        default=default_dir,
        help=f"Target directory to clean (default: {default_dir})"
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Perform actual deletion (without this flag, runs in safe dry-run mode)"
    )
    parser.add_argument(
        "-e", "--extensions",
        help="Comma-separated list of additional or custom extensions (e.g. 'mp3,flac,wav,zip')"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed scanning logs"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Prepare extension set
    if args.extensions:
        exts = {f".{ext.strip().lstrip('.').lower()}" for ext in args.extensions.split(",") if ext.strip()}
    else:
        exts = DEFAULT_MUSIC_EXTENSIONS

    target_dir = os.path.abspath(args.dir)
    is_dry_run = not args.force

    print("=" * 65)
    print("        Empty & Non-Music Folder Cleaner")
    print("=" * 65)
    print(f"Target Directory: {target_dir}")
    print(f"Mode:             {'[DRY RUN - Safe Preview]' if is_dry_run else '[LIVE - Deleting Folders]'}")
    print(f"Music Extensions: {', '.join(sorted(exts))}")
    if is_dry_run:
        print("Note: Run with '-f' or '--force' to execute actual deletion.")
    print("=" * 65)

    deleted, total_scanned = clean_folders(
        target_dir=target_dir,
        valid_extensions=exts,
        dry_run=is_dry_run,
        verbose=args.verbose
    )

    print("\n" + "=" * 65)
    action_word = "Would delete" if is_dry_run else "Deleted"
    print(f"Summary: {action_word} {len(deleted)} of {total_scanned} scanned folder(s).")
    if is_dry_run and deleted:
        print("To delete these folders, re-run with: python3 clean_empty_folders.py --force")
    print("=" * 65)


if __name__ == "__main__":
    main()
