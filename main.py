#!/usr/bin/env python3
"""
MusicScraper Toolkit - Unified CLI Entrypoint
==============================================
Convenient dispatcher for all MusicScraper tools:
1. audit    - Check library for missing tracks/albums using MusicBrainz (check_missing_tracks.py)
2. bandcamp - Download Bandcamp discographies, albums, and tracks (bandcamp_scraper.py)
3. scrape   - Crawl and download music from web releases (music_scraper.py)
4. clean    - Remove empty and non-music folders (clean_empty_folders.py)
"""

import sys
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

TOOL_SCRIPTS = {
    "audit": ROOT_DIR / "check_missing_tracks.py",
    "bandcamp": ROOT_DIR / "bandcamp_scraper.py",
    "scrape": ROOT_DIR / "music_scraper.py",
    "clean": ROOT_DIR / "clean_empty_folders.py",
}


def print_help():
    print("""
MusicScraper Toolkit - Complete Music Collection & Archival Suite
==================================================================

Usage:
  python3 main.py <command> [options]

Commands:
  audit     Audit your local library against MusicBrainz for missing releases & tracks
            Example: python3 main.py audit "Stellabee" -d /mnt/music

  bandcamp  Download albums, tracks, or full artist discographies from Bandcamp
            Example: python3 main.py bandcamp goreshit -f flac

  scrape    Scrape and download releases from websites (MediaFire, Archive.org, Direct)
            Example: python3 main.py scrape https://dochakuso.net/release.html

  clean     Clean empty and non-music directories from your library or downloads
            Example: python3 main.py clean ./downloads --force

Run 'python3 main.py <command> --help' for detailed options on any command.
""")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd not in TOOL_SCRIPTS:
        print(f"Unknown command: '{cmd}'")
        print_help()
        sys.exit(1)

    script_path = TOOL_SCRIPTS[cmd]
    args = [sys.executable, str(script_path)] + sys.argv[2:]

    try:
        proc = subprocess.run(args)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
