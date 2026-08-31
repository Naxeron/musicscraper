#!/usr/bin/env python3
"""
Convenience launcher for the MusicScraper Web GUI.
Usage:
    python web_gui.py [--host 127.0.0.1] [--port 8080] [--open]
"""

import sys
from pathlib import Path

# Ensure src/ is in sys.path when running from repository root
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.cli.main import main as cli_main


def main():
    args = sys.argv[1:]
    return cli_main(["web", *args])


if __name__ == "__main__":
    sys.exit(main())
