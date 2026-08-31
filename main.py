#!/usr/bin/env python3
"""
MusicScraper - Main Entrypoint & Command Dispatcher.
Delegates to the modern musicscraper package CLI.
"""

import sys
from pathlib import Path

# Ensure src/ is in sys.path when running from repository root
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
