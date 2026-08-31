"""
Centralized configuration and environment settings for MusicScraper.
"""

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    """Central application configuration with environment fallbacks."""

    # Default paths
    CACHE_DIR = Path(os.path.expanduser(os.environ.get("MUSICSCRAPER_CACHE_DIR", "~/.cache/musicscraper"))).resolve()
    MB_CACHE_DIR = CACHE_DIR / "mb_cache"
    AUDIO_CACHE_DB = CACHE_DIR / "musicscraper_cache.db"

    # Default library and download paths
    DEFAULT_OUTPUT_DIR = Path(
        os.environ.get("MUSICSCRAPER_OUTPUT_DIR", "/mnt/music/downloads" if os.path.exists("/mnt/music/downloads") else "./downloads")
    ).resolve()
    DEFAULT_LIBRARY_DIR = Path(
        os.environ.get("MUSICSCRAPER_LIBRARY_DIR", "/mnt/music" if os.path.exists("/mnt/music") else "./music")
    ).resolve()

    # User Agent
    USER_AGENT = (
        os.environ.get(
            "MUSICSCRAPER_USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    )
    BANDCAMP_USER_AGENT = "bandcamper/0.0.2"

    # MusicBrainz Application Identity
    MB_APP_NAME = "MusicScraper"
    MB_APP_VERSION = "1.0"
    MB_APP_CONTACT = "https://github.com/naxeron/musicscraper"

    # Last.fm Settings
    LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "b25b959554ed76058ac220b7b2e0a026")
    LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET", "")
    LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

    # Soulseek / slskd Settings
    SLSKD_URL = os.environ.get("SLSKD_URL", "http://localhost:5030").rstrip("/")
    SLSKD_USERNAME = os.environ.get("SLSKD_USERNAME")
    SLSKD_PASSWORD = os.environ.get("SLSKD_PASSWORD")
    SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY")

    # Navidrome Settings
    NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
    NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
    NAVIDROME_TOKEN = os.environ.get("NAVIDROME_TOKEN", "")
    NAVIDROME_SALT = os.environ.get("NAVIDROME_SALT", "")

    # Bandcamp Account (optional)
    BANDCAMP_EMAIL = os.environ.get("BANDCAMP_EMAIL")

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensures that configured cache and output directories exist."""
        cls.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cls.MB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cls.DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
