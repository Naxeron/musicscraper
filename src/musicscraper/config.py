"""
Centralized configuration and environment settings for MusicScraper.
"""

import os
from pathlib import Path
from typing import Optional

ENV_FILE_PATH: Optional[Path] = None

try:
    from dotenv import load_dotenv, find_dotenv
    env_file = find_dotenv(usecwd=True)
    if not env_file:
        project_env = Path(__file__).resolve().parent.parent.parent / ".env"
        if project_env.exists():
            env_file = str(project_env)
    if env_file:
        load_dotenv(dotenv_path=env_file)
        ENV_FILE_PATH = Path(env_file).resolve()
    else:
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

    # Navidrome / Subsonic Settings
    NAVIDROME_URL = (
        os.environ.get("NAVIDROME_URL")
        or os.environ.get("SUBSONIC_URL")
        or ""
    ).rstrip("/")
    NAVIDROME_USER = (
        os.environ.get("NAVIDROME_USERNAME")
        or os.environ.get("NAVIDROME_USER")
        or os.environ.get("SUBSONIC_USERNAME")
        or os.environ.get("SUBSONIC_USER")
        or ""
    )
    NAVIDROME_USERNAME = NAVIDROME_USER
    NAVIDROME_TOKEN = (
        os.environ.get("NAVIDROME_PASSWORD")
        or os.environ.get("NAVIDROME_TOKEN")
        or os.environ.get("NAVIDROME_PASS")
        or os.environ.get("SUBSONIC_PASSWORD")
        or os.environ.get("SUBSONIC_TOKEN")
        or ""
    )
    NAVIDROME_PASSWORD = NAVIDROME_TOKEN
    NAVIDROME_SALT = (
        os.environ.get("NAVIDROME_SALT")
        or os.environ.get("SUBSONIC_SALT")
        or ""
    )

    # Bandcamp Account (optional)
    BANDCAMP_EMAIL = os.environ.get("BANDCAMP_EMAIL")

    @classmethod
    def save_to_env(cls) -> bool:
        """Persists current configuration to the active .env file if available."""
        if not ENV_FILE_PATH or not ENV_FILE_PATH.exists():
            return False
        try:
            from dotenv import set_key
            env_str = str(ENV_FILE_PATH)
            if cls.SLSKD_URL:
                set_key(env_str, "SLSKD_URL", cls.SLSKD_URL)
            if cls.SLSKD_USERNAME:
                set_key(env_str, "SLSKD_USERNAME", cls.SLSKD_USERNAME)
            if cls.SLSKD_PASSWORD:
                set_key(env_str, "SLSKD_PASSWORD", cls.SLSKD_PASSWORD)
            if cls.NAVIDROME_URL:
                set_key(env_str, "NAVIDROME_URL", cls.NAVIDROME_URL)
            if cls.NAVIDROME_USER:
                set_key(env_str, "NAVIDROME_USERNAME", cls.NAVIDROME_USER)
            if cls.NAVIDROME_TOKEN:
                set_key(env_str, "NAVIDROME_PASSWORD", cls.NAVIDROME_TOKEN)
            if cls.LASTFM_API_KEY:
                set_key(env_str, "LASTFM_API_KEY", cls.LASTFM_API_KEY)
            return True
        except Exception:
            return False

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensures that configured cache and output directories exist."""
        cls.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cls.MB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cls.DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
