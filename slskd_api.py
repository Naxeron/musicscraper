#!/usr/bin/env python3
"""
Backward-compatibility shim for slskd_api.py.
Delegates to musicscraper.clients.slskd.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from musicscraper.clients.slskd import SlskdClient, SlskdAPIError

__all__ = ["SlskdClient", "SlskdAPIError"]

if __name__ == "__main__":
    client = SlskdClient()
    try:
        app_info = client.get_application()
        print(f"Connected to slskd v{app_info.get('version', 'Unknown')}")
    except Exception as e:
        print(f"Error connecting to slskd: {e}")
