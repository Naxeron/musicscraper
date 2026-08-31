"""
Unified SQLite caching layer for audio metadata, quality analysis, and external API responses.
"""

import json
import time
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

from musicscraper.config import Config
from musicscraper.core.audio import AudioMetadata


class UnifiedCacheManager:
    """Consolidated SQLite cache manager with thread-safe connection handling."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or Config.AUDIO_CACHE_DB).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initializes tables for audio files, quality inspections, and API response caches."""
        with self._get_conn() as conn:
            # 1. Audio metadata & quality cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audio_cache (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    size_bytes INTEGER,
                    title TEXT,
                    artist TEXT,
                    album_artist TEXT,
                    album TEXT,
                    track_number TEXT,
                    year TEXT,
                    genres TEXT,
                    mb_track_ids TEXT,
                    mb_rec_ids TEXT,
                    mb_artist_ids TEXT,
                    mb_release_ids TEXT,
                    bitrate_kbps INTEGER,
                    bit_depth INTEGER,
                    sample_rate INTEGER,
                    channels INTEGER,
                    duration REAL,
                    is_lossless INTEGER,
                    format_label TEXT,
                    quality_score INTEGER,
                    cached_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_mtime ON audio_cache (path, mtime)")

            # 2. Generic Key-Value / API Cache with TTL
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    namespace TEXT,
                    cache_key TEXT,
                    data_json TEXT,
                    expires_at REAL,
                    created_at REAL,
                    PRIMARY KEY (namespace, cache_key)
                )
            """)
            conn.commit()

    # --- Audio Metadata & Quality Cache Methods ---

    def get_audio_metadata(self, file_path: Path) -> Optional[AudioMetadata]:
        """Retrieves cached AudioMetadata if file mtime and size match."""
        try:
            st = file_path.stat()
            path_str = str(file_path.resolve())
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT * FROM audio_cache WHERE path = ? AND mtime = ? AND size_bytes = ?",
                    (path_str, st.st_mtime, st.st_size)
                )
                row = cur.fetchone()
                if not row:
                    return None

                return AudioMetadata(
                    path=file_path,
                    file_type=file_path.suffix.lower(),
                    title=row["title"] or "",
                    artist=row["artist"] or "",
                    album_artist=row["album_artist"] or "",
                    album=row["album"] or "",
                    track_number=row["track_number"] or "",
                    year=row["year"] or "",
                    genres=json.loads(row["genres"]) if row["genres"] else [],
                    mb_track_ids=set(json.loads(row["mb_track_ids"])) if row["mb_track_ids"] else set(),
                    mb_rec_ids=set(json.loads(row["mb_rec_ids"])) if row["mb_rec_ids"] else set(),
                    mb_artist_ids=set(json.loads(row["mb_artist_ids"])) if row["mb_artist_ids"] else set(),
                    mb_release_ids=set(json.loads(row["mb_release_ids"])) if row["mb_release_ids"] else set(),
                    bitrate_kbps=row["bitrate_kbps"] or 0,
                    bit_depth=row["bit_depth"] or 0,
                    sample_rate=row["sample_rate"] or 0,
                    channels=row["channels"] or 2,
                    duration=row["duration"] or 0.0,
                    is_lossless=bool(row["is_lossless"]),
                    format_label=row["format_label"] or "",
                    quality_score=row["quality_score"] or 0
                )
        except Exception:
            return None

    def store_audio_metadata(self, meta: AudioMetadata) -> None:
        """Stores AudioMetadata into the cache."""
        try:
            st = meta.path.stat()
            path_str = str(meta.path.resolve())
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO audio_cache (
                        path, mtime, size_bytes, title, artist, album_artist, album,
                        track_number, year, genres, mb_track_ids, mb_rec_ids, mb_artist_ids,
                        mb_release_ids, bitrate_kbps, bit_depth, sample_rate, channels,
                        duration, is_lossless, format_label, quality_score, cached_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    path_str,
                    st.st_mtime,
                    st.st_size,
                    meta.title,
                    meta.artist,
                    meta.album_artist,
                    meta.album,
                    meta.track_number,
                    meta.year,
                    json.dumps(meta.genres),
                    json.dumps(list(meta.mb_track_ids)),
                    json.dumps(list(meta.mb_rec_ids)),
                    json.dumps(list(meta.mb_artist_ids)),
                    json.dumps(list(meta.mb_release_ids)),
                    meta.bitrate_kbps,
                    meta.bit_depth,
                    meta.sample_rate,
                    meta.channels,
                    meta.duration,
                    1 if meta.is_lossless else 0,
                    meta.format_label,
                    meta.quality_score,
                    time.time()
                ))
                conn.commit()
        except Exception:
            pass

    # --- Generic API / Key-Value Cache Methods ---

    def get_api_cache(self, namespace: str, key: str) -> Optional[Any]:
        """Retrieves cached JSON data for namespace:key if not expired."""
        try:
            now = time.time()
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT data_json, expires_at FROM api_cache WHERE namespace = ? AND cache_key = ?",
                    (namespace, key)
                )
                row = cur.fetchone()
                if not row:
                    return None
                if row["expires_at"] and now > row["expires_at"]:
                    return None
                return json.loads(row["data_json"])
        except Exception:
            return None

    def store_api_cache(self, namespace: str, key: str, data: Any, ttl_seconds: Optional[float] = None) -> None:
        """Stores arbitrary JSON data into the cache with optional TTL."""
        try:
            now = time.time()
            expires_at = (now + ttl_seconds) if ttl_seconds else None
            data_json = json.dumps(data)
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO api_cache (namespace, cache_key, data_json, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (namespace, key, data_json, expires_at, now))
                conn.commit()
        except Exception:
            pass

    def clear_expired(self) -> int:
        """Removes expired entries from the API cache."""
        try:
            now = time.time()
            with self._get_conn() as conn:
                cur = conn.execute("DELETE FROM api_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
                deleted = cur.rowcount
                conn.commit()
                return deleted
        except Exception:
            return 0
