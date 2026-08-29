#!/usr/bin/env python3
"""
Soulseek / slskd Music Quality Upgrader
=======================================
Scans local music collections for low-quality audio files (e.g. MP3 < 320kbps,
lossy files when lossless FLAC is desired, or low-bitrate streams), groups them
into albums/releases and standalone singles, queries Soulseek via slskd, strictly
verifies candidate releases using tracklist reconciliation and version compatibility,
and enqueues verified higher-quality releases for download.

Features:
1. Audio Quality Analyzer: Uses Mutagen to inspect audio codecs, bitrates, bit depths,
   sample rates, and channels across MP3, FLAC, M4A/AAC, OGG, OPUS, WAV, AIFF, APE, WV.
2. Smart Release & Track Grouping: Automatically identifies complete albums/EPs vs standalone
   singles/features from ID3 tags, folder structures, and optional MusicBrainz catalog enrichment.
3. Strict Quality Verification: Ensures discovered peer candidates have strictly higher audio
   quality (e.g., MP3 128/192kbps -> MP3 320kbps or FLAC; MP3 320kbps -> FLAC; FLAC 16-bit -> FLAC 24-bit).
4. Multi-Layer Tracklist Reconciliation: Verifies matching tracks using Katakana-Hiragana conversion,
   diacritics transliteration, token sequences, fuzzy similarity, and version/remix compatibility.
5. Peer Directory Expansion: Automatically browses remote peer folders to inspect full contents,
   bonus tracks, artwork, cue sheets, and logs.
6. Safe Queueing & Deduplication: Enqueues verified directories in slskd, skips existing transfers,
   and supports non-destructive --dry-run previews.
7. Rich Terminal Reports: Displays before/after quality comparison tables and exports Markdown,
   JSON, CSV, and TXT audit summaries.
"""

import os
import sys
import re
import csv
import json
import time
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import mutagen
from unidecode import unidecode
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.text import Text
from rich import box

from slskd_api import SlskdClient, SlskdAPIError
from check_missing_tracks import (
    MusicBrainzClient,
    normalize_text,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    DEFAULT_CACHE_DIR,
    AUDIO_EXTENSIONS,
)
from slskd_scraper import (
    katakana_to_hiragana,
    normalize_track_title,
    clean_tokens,
    tokenize_words,
    is_sublist,
    extract_catalog_codes,
    is_track_title_match,
    is_track_title_match_fast,
    is_dir_name_match,
    is_dir_name_match_fast,
    sanitize_remote_path,
    extract_dir_and_filename,
    is_audio_file,
    is_supporting_file,
    clean_search_phrase,
    determine_audio_quality,
    pre_parse_single_track,
    pre_parse_expected_tracks,
    CandidateFile,
    CandidateDir,
    PeerCandidateIndex,
    DIR_STOP_WORDS,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTING_EXTENSIONS,
    POLISH_DIACRITICS_MAP,
)

console = Console()

DEFAULT_AUDIO_CACHE_DB = os.path.expanduser("~/.cache/musicscraper/quality_upgrade_cache.db")


# ==============================================================================
# AUDIO QUALITY ANALYZER
# ==============================================================================

class AudioQualityAnalyzer:
    """Inspects audio stream parameters and computes standardized quality scores."""

    @staticmethod
    def analyze_file(file_path: Path) -> Dict[str, Any]:
        """
        Inspects an audio file via Mutagen and extracts bitrate, bit depth, sample rate,
        lossless status, and formatted quality label.
        """
        path_str = str(file_path)
        ext = file_path.suffix.lower()

        title = ""
        album = ""
        artist = ""
        track_number = ""
        year = ""
        mb_album_id = ""
        mb_track_id = ""
        mb_artist_id = ""

        try:
            mf = mutagen.File(path_str)
        except Exception as e:
            mf = None

        type_name = type(mf).__name__ if mf else "Unknown"
        mime = getattr(mf, "mime", [""])[0] if (mf and hasattr(mf, "mime")) else ""

        # Extract Tags
        if mf and hasattr(mf, "tags") and mf.tags is not None:
            tags = mf.tags
            if hasattr(tags, "items"):
                for k, v in tags.items():
                    k_str = str(k).upper().strip()
                    v_list = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]

                    if k_str in ("TIT2", "TITLE", "\xa9NAM", "TXXX:TITLE") and not title:
                        title = v_list[0].strip() if v_list else ""
                    elif k_str in ("TALB", "ALBUM", "\xa9ALB", "TXXX:ALBUM") and not album:
                        album = v_list[0].strip() if v_list else ""
                    elif k_str in ("TRCK", "TRACKNUMBER", "TXXX:TRACKNUMBER") and not track_number:
                        raw_trck = v_list[0].strip() if v_list else ""
                        track_number = raw_trck.split("/")[0].strip()
                    elif k_str in ("TDRC", "DATE", "YEAR", "\xa9DAY") and not year:
                        year = v_list[0].strip() if v_list else ""
                    elif k_str in (
                        "TPE1", "TPE2", "TOPE", "ARTIST", "ALBUMARTIST",
                        "PERFORMER", "\xa9ART", "AART"
                    ) and not artist:
                        artist = v_list[0].strip() if v_list else ""

                    # MusicBrainz Tag IDs
                    if k_str in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ ALBUM ID", "TXXX:MUSICBRAINZ ALBUM ID") and not mb_album_id:
                        mb_album_id = v_list[0].strip() if v_list else ""
                    elif k_str in ("MUSICBRAINZ_TRACKID", "MUSICBRAINZ TRACK ID", "TXXX:MUSICBRAINZ TRACK ID") and not mb_track_id:
                        mb_track_id = v_list[0].strip() if v_list else ""
                    elif k_str in ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ ARTIST ID", "TXXX:MUSICBRAINZ ARTIST ID") and not mb_artist_id:
                        mb_artist_id = v_list[0].strip() if v_list else ""

        # Fallback metadata from filename / path hierarchy
        filename_no_ext = file_path.stem
        if not title:
            title = strip_track_number_and_artist(filename_no_ext)
        if not album:
            parent_name = file_path.parent.name
            if parent_name and parent_name.lower() not in ("music", "downloads", "library", "tracks", "singles"):
                album = parent_name
        if not artist:
            # Check grandparent or parent name for Artist - Album pattern
            parent_name = file_path.parent.name
            grandparent_name = file_path.parent.parent.name if file_path.parent != file_path.parent.parent else ""
            if " - " in parent_name:
                parts = parent_name.split(" - ", 1)
                artist = parts[0].strip()
                if not album or album == parent_name:
                    album = parts[1].strip()
            elif grandparent_name and grandparent_name.lower() not in ("music", "downloads", "library"):
                artist = grandparent_name

        # Extract Audio Stream Specs
        bitrate_kbps = 0
        bit_depth = 0
        sample_rate = 0
        channels = 2
        is_lossless = False
        duration = 0.0

        if mf and hasattr(mf, "info") and mf.info is not None:
            info = mf.info
            duration = getattr(info, "length", 0.0)
            sample_rate = getattr(info, "sample_rate", 0)
            channels = getattr(info, "channels", 2)
            bit_depth = getattr(info, "bits_per_sample", 0)
            raw_bitrate = getattr(info, "bitrate", 0)
            if raw_bitrate:
                bitrate_kbps = int(raw_bitrate / 1000)

        # Quality scoring & format categorization
        if ext in (".flac", ".wav", ".aif", ".aiff", ".ape", ".wv", ".dsf", ".dff") or type_name in ("FLAC", "WAVE", "AIFF", "MonkeysAudio", "WavPack") or "flac" in mime or "wav" in mime:
            is_lossless = True
            bit_depth = bit_depth or 16
            sample_rate = sample_rate or 44100
            if bit_depth > 16:
                format_label = f"FLAC {bit_depth}-bit/{sample_rate}Hz"
                quality_score = 115
            else:
                format_label = "FLAC (Lossless 16-bit)"
                quality_score = 100
        elif ext in (".mp4", ".m4a") or type_name == "MP4" or "mp4" in mime or "m4a" in mime:
            codec = getattr(mf.info, "codec", "") if (mf and hasattr(mf, "info")) else ""
            if codec == "alac" or bit_depth > 0:
                is_lossless = True
                format_label = f"ALAC {bit_depth or 16}-bit"
                quality_score = 100
            else:
                is_lossless = False
                bitrate_label = f"{bitrate_kbps}kbps" if bitrate_kbps else "AAC"
                format_label = f"AAC {bitrate_label}"
                quality_score = min(75, int(bitrate_kbps / 4)) if bitrate_kbps else 40
        elif ext == ".mp3" or type_name == "MP3" or "mp3" in mime or "mpeg" in mime:
            is_lossless = False
            if bitrate_kbps >= 320:
                format_label = "MP3 320kbps"
                quality_score = 80
            elif bitrate_kbps >= 240:
                format_label = f"MP3 ~{bitrate_kbps}kbps (V0)"
                quality_score = 70
            elif bitrate_kbps >= 192:
                format_label = f"MP3 {bitrate_kbps}kbps"
                quality_score = 50
            elif bitrate_kbps > 0:
                format_label = f"MP3 {bitrate_kbps}kbps"
                quality_score = 30
            else:
                format_label = "MP3"
                quality_score = 35
        elif ext in (".ogg", ".opus") or type_name in ("OggVorbis", "OggOpus"):
            is_lossless = False
            codec_name = "Opus" if (ext == ".opus" or type_name == "OggOpus") else "Vorbis"
            if bitrate_kbps >= 256:
                format_label = f"{codec_name} ~{bitrate_kbps}kbps"
                quality_score = 75
            elif bitrate_kbps >= 160:
                format_label = f"{codec_name} {bitrate_kbps}kbps"
                quality_score = 60
            elif bitrate_kbps > 0:
                format_label = f"{codec_name} {bitrate_kbps}kbps"
                quality_score = 40
            else:
                format_label = codec_name
                quality_score = 45
        else:
            is_lossless = False
            format_label = ext.upper().lstrip(".") or "Audio"
            quality_score = 30

        return {
            "path": path_str,
            "filename": file_path.name,
            "ext": ext,
            "title": title or file_path.stem,
            "norm_title": normalize_text(title or file_path.stem),
            "album": album,
            "norm_album": normalize_text(album),
            "artist": artist,
            "norm_artist": normalize_text(artist),
            "track_number": track_number,
            "year": year,
            "mb_album_id": mb_album_id,
            "mb_track_id": mb_track_id,
            "mb_artist_id": mb_artist_id,
            "is_lossless": is_lossless,
            "bitrate_kbps": bitrate_kbps,
            "bit_depth": bit_depth,
            "sample_rate": sample_rate,
            "channels": channels,
            "duration": duration,
            "format_label": format_label,
            "quality_score": quality_score,
            "file_size": file_path.stat().st_size if file_path.exists() else 0
        }


# ==============================================================================
# LOCAL METADATA CACHE (SQLITE)
# ==============================================================================

class QualityAudioCache:
    """Fast SQLite caching for scanned local file audio attributes."""

    def __init__(self, db_path: str = DEFAULT_AUDIO_CACHE_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_quality_cache (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    data TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_cache_mtime ON file_quality_cache (path, mtime, size)")

    def get_batch(self, file_infos: List[Tuple[str, float, int]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        if not file_infos:
            return {}, []

        cached_map: Dict[str, Dict[str, Any]] = {}
        uncached_paths: List[str] = []

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            for path, mtime, size in file_infos:
                cursor.execute("SELECT data FROM file_quality_cache WHERE path = ? AND mtime = ? AND size = ?", (path, mtime, size))
                row = cursor.fetchone()
                if row:
                    try:
                        cached_map[path] = json.loads(row[0])
                    except Exception:
                        uncached_paths.append(path)
                else:
                    uncached_paths.append(path)

        return cached_map, uncached_paths

    def set_batch(self, records: List[Tuple[str, float, int, Dict[str, Any]]]):
        if not records:
            return
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_quality_cache (path, mtime, size, data) VALUES (?, ?, ?, ?)",
                [(r[0], r[1], r[2], json.dumps(r[3])) for r in records]
            )


# ==============================================================================
# LOCAL LIBRARY QUALITY SCANNER
# ==============================================================================

class LocalLibraryQualityScanner:
    """Discovers audio files and groups low-quality candidates into albums and singles."""

    def __init__(
        self,
        target_path: Path,
        target_format: str = "flac",
        max_bitrate: Optional[int] = 320,
        lossy_only: bool = True,
        threads: int = 12
    ):
        self.target_path = target_path.resolve()
        self.target_format = target_format.lower()
        self.max_bitrate = max_bitrate
        self.lossy_only = lossy_only
        self.threads = threads
        self.cache = QualityAudioCache()

    def is_file_low_quality(self, item: Dict[str, Any]) -> bool:
        """Determines if a local audio file qualifies for upgrade based on user criteria."""
        is_lossless = item.get("is_lossless", False)
        bitrate_kbps = item.get("bitrate_kbps", 0)
        quality_score = item.get("quality_score", 40)

        # 1. Target Format: FLAC / Lossless (Default)
        if self.target_format in ("flac", "lossless"):
            # Any lossy file is low quality compared to FLAC
            if not is_lossless:
                if self.max_bitrate is not None and bitrate_kbps > self.max_bitrate:
                    return False
                return True
            # If already lossless, check if user specifically wants 24-bit hi-res
            if item.get("bit_depth", 16) <= 16 and self.target_format == "flac-24":
                return True
            return False

        # 2. Target Format: MP3 320kbps
        elif self.target_format in ("320", "mp3-320"):
            if is_lossless:
                return False
            # Upgrade if bitrate is below 315 kbps (covers 128, 192, 256, V0, etc.)
            return bitrate_kbps < 315

        # 3. Target Format: Any Higher Quality
        elif self.target_format == "any-higher":
            if not is_lossless:
                return True
            return quality_score < 115

        # General bitrate threshold fallback
        if self.max_bitrate is not None and bitrate_kbps and bitrate_kbps <= self.max_bitrate:
            return True

        return False

    def scan(self, progress: Optional[Progress] = None, task_id: Optional[Any] = None) -> Dict[str, Any]:
        """
        Scans target path, evaluates audio quality with persistent caching, and groups
        candidates into Albums/Releases and Standalone Tracks.
        """
        if not self.target_path.exists():
            return {"all_files": [], "albums": [], "singles": [], "stats": {}}

        candidate_paths: List[Path] = []

        if self.target_path.is_file():
            if self.target_path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                candidate_paths.append(self.target_path)
        else:
            for root, _, files in os.walk(self.target_path):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in SUPPORTED_AUDIO_EXTENSIONS:
                        candidate_paths.append(Path(root) / f)

        total_files = len(candidate_paths)
        if total_files == 0:
            return {"all_files": [], "albums": [], "singles": [], "stats": {}}

        if progress and task_id:
            progress.update(task_id, description=f"[cyan]Scanning tags & audio streams of {total_files} files...", total=total_files, completed=0)

        # Stat files for caching
        file_infos: List[Tuple[str, float, int]] = []
        path_map: Dict[str, Path] = {}
        for p in candidate_paths:
            try:
                st = p.stat()
                file_infos.append((str(p), st.st_mtime, st.st_size))
                path_map[str(p)] = p
            except Exception:
                pass

        cached_map, uncached_paths = self.cache.get_batch(file_infos)
        all_track_items: List[Dict[str, Any]] = list(cached_map.values())

        if progress and task_id and cached_map:
            progress.advance(task_id, len(cached_map))

        if uncached_paths:
            new_records: List[Tuple[str, float, int, Dict[str, Any]]] = []
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                future_to_path = {pool.submit(AudioQualityAnalyzer.analyze_file, path_map[p]): p for p in uncached_paths}
                for fut in as_completed(future_to_path):
                    p_str = future_to_path[fut]
                    try:
                        item = fut.result()
                        all_track_items.append(item)
                        p_obj = path_map[p_str]
                        st = p_obj.stat()
                        new_records.append((p_str, st.st_mtime, st.st_size, item))
                    except Exception:
                        pass
                    if progress and task_id:
                        progress.advance(task_id, 1)

            if new_records:
                self.cache.set_batch(new_records)

        # Filter low-quality files
        low_quality_files = [t for t in all_track_items if self.is_file_low_quality(t)]

        # Grouping by Album / Directory
        all_album_groups: Dict[str, List[Dict[str, Any]]] = {}
        for t in all_track_items:
            art = t.get("norm_artist", "")
            alb = t.get("norm_album", "")
            parent_dir = str(Path(t["path"]).parent)

            if alb and len(alb) >= 2:
                key = f"{art}::{alb}"
            else:
                key = f"dir::{parent_dir}"

            if key not in all_album_groups:
                all_album_groups[key] = []
            all_album_groups[key].append(t)

        # Identify which album releases contain low quality files
        processed_keys: Set[str] = set()
        release_candidates: List[Dict[str, Any]] = []
        standalone_files: List[Dict[str, Any]] = []

        for t in low_quality_files:
            art = t.get("norm_artist", "")
            alb = t.get("norm_album", "")
            parent_dir = str(Path(t["path"]).parent)

            if alb and len(alb) >= 2:
                key = f"{art}::{alb}"
            else:
                key = f"dir::{parent_dir}"

            if key in processed_keys:
                continue
            processed_keys.add(key)

            full_album_tracks = all_album_groups.get(key, [t])
            lq_tracks = [x for x in full_album_tracks if self.is_file_low_quality(x)]

            # Check if this represents a multi-track album/EP release or a standalone single
            has_album_tag = bool(t.get("album") and len(t.get("album", "").strip()) >= 2)
            is_multi_track = len(full_album_tracks) >= 2 or has_album_tag

            parent_name = Path(t["path"]).parent.name.lower()
            if not has_album_tag and parent_name in ("singles", "tracks", "music", "downloads", "loose", "dump", "audio"):
                is_multi_track = False

            if is_multi_track:
                sample_t = full_album_tracks[0]
                disp_artist = sample_t.get("artist") or "Unknown Artist"
                disp_album = sample_t.get("album") or Path(sample_t["path"]).parent.name
                formats_present = list(dict.fromkeys(x.get("format_label", "Unknown") for x in full_album_tracks))
                min_score = min(x.get("quality_score", 40) for x in full_album_tracks)
                avg_score = sum(x.get("quality_score", 40) for x in full_album_tracks) / len(full_album_tracks)
                mb_id = next((x.get("mb_album_id") for x in full_album_tracks if x.get("mb_album_id")), "")

                release_candidates.append({
                    "type": "Album / Release",
                    "artist": disp_artist,
                    "norm_artist": normalize_text(disp_artist),
                    "album": disp_album,
                    "norm_album": normalize_text(disp_album),
                    "total_tracks": len(full_album_tracks),
                    "low_quality_tracks": len(lq_tracks),
                    "tracks": full_album_tracks,
                    "formats": formats_present,
                    "min_score": min_score,
                    "avg_score": avg_score,
                    "mb_album_id": mb_id,
                    "parent_dir": str(Path(sample_t["path"]).parent)
                })
            else:
                for lqt in lq_tracks:
                    standalone_files.append(lqt)

        return {
            "all_files": all_track_items,
            "low_quality_files": low_quality_files,
            "albums": release_candidates,
            "singles": standalone_files,
            "stats": {
                "total_scanned": len(all_track_items),
                "low_quality_count": len(low_quality_files),
                "album_count": len(release_candidates),
                "single_count": len(standalone_files),
                "lossless_count": sum(1 for x in all_track_items if x.get("is_lossless")),
                "lossy_count": sum(1 for x in all_track_items if not x.get("is_lossless")),
            }
        }


# ==============================================================================
# SOULSEEK QUALITY UPGRADER ENGINE
# ==============================================================================

class SoulseekQualityUpgrader:
    """Orchestrates candidate searching, strict quality and tracklist verification, and slskd queueing."""

    def __init__(
        self,
        target_path: str,
        slskd_client: Optional[SlskdClient] = None,
        target_format: str = "flac",
        max_bitrate: Optional[int] = 320,
        min_match_ratio: float = 0.70,
        search_timeout: float = 14.0,
        dry_run: bool = False,
        singles_only: bool = False,
        use_mb: bool = True,
        threads: int = 8,
        cache_dir: str = DEFAULT_CACHE_DIR
    ):
        self.target_path = Path(target_path).resolve()
        self.client = slskd_client or SlskdClient()
        self.target_format = target_format.lower()
        self.max_bitrate = max_bitrate
        self.min_match_ratio = min_match_ratio
        self.search_timeout = search_timeout
        self.dry_run = dry_run
        self.singles_only = singles_only
        self.use_mb = use_mb
        self.threads = threads
        self.cache_dir = cache_dir

        self.mb_client = MusicBrainzClient(cache_dir=self.cache_dir) if self.use_mb else None

        # Discovered peers & directory cache
        self.peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.candidate_index: Optional[PeerCandidateIndex] = None
        self.searches_performed: Set[str] = set()

        # Results state
        self.verified_upgrades: List[Dict[str, Any]] = []
        self.unresolved_items: List[Dict[str, Any]] = []
        self.queued_transfers: List[Dict[str, Any]] = []
        self.already_downloading_files: Set[str] = set()

    def run(self) -> Dict[str, Any]:
        """Executes complete library scanning, Soulseek discovery, verification, and upgrade queueing."""
        console.print(Panel.fit(
            f"[bold cyan]Soulseek / slskd Music Quality Upgrader[/bold cyan]\n"
            f"[dim]Target Path: {self.target_path}[/dim]\n"
            f"[dim]Upgrade Target: [bold green]{self.target_format.upper()}[/bold green] (Min Match: {int(self.min_match_ratio*100)}%)[/dim]",
            border_style="cyan"
        ))

        # 1. Connect to slskd
        try:
            app_info = self.client.get_application()
            slsk_user = app_info.get("user", {}).get("username", "Unknown")
            server_state = app_info.get("server", {}).get("state", "Unknown")
            console.print(f"[green]✔ Connected to slskd[/green] (User: [bold]{slsk_user}[/bold] | Server: [dim]{server_state}[/dim])")
            self.already_downloading_files = self.client.get_queued_filenames()
            if self.already_downloading_files:
                console.print(f"[dim]Active/queued in slskd: {len(self.already_downloading_files)} files[/dim]")
        except Exception as e:
            console.print(f"[bold red]slskd Connection Error:[/bold red] {e}")
            if not self.dry_run:
                raise

        # 2. Scan Local Library for Low-Quality Files
        scanner = LocalLibraryQualityScanner(
            target_path=self.target_path,
            target_format=self.target_format,
            max_bitrate=self.max_bitrate,
            threads=self.threads
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task_id = progress.add_task("[cyan]Inspecting local audio library quality...", total=None)
            scan_results = scanner.scan(progress=progress, task_id=task_id)

        stats = scan_results["stats"]
        console.print(f"[green]✔ Library Quality Scan:[/green] [bold]{stats['total_scanned']}[/bold] total audio files | "
                      f"[bold red]{stats['low_quality_count']}[/bold red] low-quality tracks found "
                      f"([bold cyan]{len(scan_results['albums'])}[/bold cyan] albums, [bold cyan]{len(scan_results['singles'])}[/bold cyan] standalone singles)")

        if stats["low_quality_count"] == 0:
            console.print("\n[bold green]✨ All scanned audio files already meet or exceed the target quality standard![/bold green]")
            return {
                "verified_upgrades": [],
                "unresolved_items": [],
                "queued_transfers": [],
                "stats": stats
            }

        # 3. Generate Search Queries for Soulseek
        queries = self._generate_search_queries(scan_results["albums"], scan_results["singles"])
        console.print(f"\n[cyan]Searching Soulseek P2P network ({len(queries)} targeted queries)...[/cyan]")

        # 4. Execute Soulseek Batch Searches
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Querying peers across Soulseek...", total=len(queries))
            batch_results = self.client.batch_search(
                queries,
                timeout=self.search_timeout,
                poll_interval=1.0,
                max_concurrent=8,
                use_existing=True
            )
            progress.update(task, completed=len(queries), description="Processing peer search results...")

            total_discovered_files = 0
            for query_str, s_data in batch_results.items():
                self.searches_performed.add(query_str.lower())
                responses = s_data.get("responses", [])
                for resp in responses:
                    user = resp.get("username")
                    speed = resp.get("uploadSpeed", 0)
                    queue = resp.get("queueLength", 0)
                    has_slot = resp.get("hasFreeUploadSlot", True)
                    for f in resp.get("files", []):
                        fn = f.get("filename", "")
                        if not fn or f.get("isLocked", False):
                            continue
                        dir_name, file_name = extract_dir_and_filename(fn)
                        if not dir_name:
                            continue
                        key = (user, dir_name)
                        if key not in self.peer_directories:
                            self.peer_directories[key] = {
                                "user": user,
                                "directory": dir_name,
                                "speed": speed,
                                "queue": queue,
                                "has_slot": has_slot,
                                "matched_search_files": [],
                                "full_directory_files": None
                            }
                        self.peer_directories[key]["matched_search_files"].append(f)
                        total_discovered_files += 1

        console.print(f"[green]✔ Soulseek Discovery:[/green] Found [bold]{len(self.peer_directories)}[/bold] candidate directories ([dim]{total_discovered_files} candidate files[/dim]) across peers.")

        # 5. Verify & Reconcile Album Upgrades
        self._reconcile_album_upgrades(scan_results["albums"])

        # 6. Verify & Reconcile Standalone Track Upgrades
        self._reconcile_single_upgrades(scan_results["singles"])

        # 7. Queue Verified Upgrades
        if not self.dry_run:
            self._queue_upgrades()
        else:
            console.print("\n[yellow]--dry-run enabled: Previewing verified upgrades without queuing downloads.[/yellow]")

        # 8. Render Rich Report Tables
        self._print_reports()

        return {
            "verified_upgrades": self.verified_upgrades,
            "unresolved_items": self.unresolved_items,
            "queued_transfers": self.queued_transfers,
            "stats": stats
        }

    def _generate_search_queries(self, albums: List[Dict[str, Any]], singles: List[Dict[str, Any]]) -> List[str]:
        """Builds high-yield queries for low quality albums and singles."""
        queries: List[str] = []

        # Album queries
        for alb in albums:
            artist = alb.get("artist", "").strip()
            title = alb.get("album", "").strip()

            if artist and title and artist.lower() not in ("unknown", "various artists", "va"):
                q = clean_search_phrase(f"{artist} {title}")
                if q and len(q) >= 4:
                    queries.append(q)
            elif title:
                q = clean_search_phrase(title)
                if q and len(q) >= 4:
                    queries.append(q)

            cat_codes = extract_catalog_codes(title) + extract_catalog_codes(alb.get("parent_dir", ""))
            for code in cat_codes:
                if len(code) >= 4:
                    queries.append(code)

        # Standalone singles queries
        for s in singles[:25]:
            artist = s.get("artist", "").strip()
            title = s.get("title", "").strip()
            if artist and title and artist.lower() not in ("unknown", "various artists", "va"):
                q = clean_search_phrase(f"{artist} {title}")
                if q and len(q) >= 4:
                    queries.append(q)
            elif title:
                q = clean_search_phrase(title)
                if q and len(q) >= 4:
                    queries.append(q)

        return list(dict.fromkeys(q for q in queries if q and len(q) >= 3))

    def _evaluate_indexed_directory_for_upgrade(
        self,
        cd: CandidateDir,
        parsed_expected: List[Dict[str, Any]],
        expected_tracks: List[Dict[str, Any]],
        album_title: str,
        artist_aliases: Set[str],
        local_score: int
    ) -> Optional[Dict[str, Any]]:
        """Evaluates directory tracklist match and audio quality against local album."""
        audio_files = cd.audio_files
        if not audio_files:
            return None

        matched_tracks: List[Dict[str, Any]] = []
        for pe, exp in zip(parsed_expected, expected_tracks):
            exp_title = exp.get("title", "")
            matched_file = None
            for cf in audio_files:
                if is_track_title_match_fast(
                    pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
                    cf, artist_aliases, album_title, cd.dir_name
                ):
                    matched_file = cf
                    break
            if matched_file:
                matched_tracks.append({
                    "expected": exp_title,
                    "matched_file": matched_file.base_filename,
                    "full_filename": matched_file.full_filename,
                    "size": matched_file.size
                })

        match_ratio = len(matched_tracks) / len(expected_tracks) if expected_tracks else 0.0
        if match_ratio < self.min_match_ratio and len(matched_tracks) == 0:
            return None

        primary_audio = audio_files[0]
        format_label, format_score = primary_audio.fmt_label, primary_audio.fmt_score

        queue = cd.dir_info.get("queue", 0)
        speed = cd.dir_info.get("speed", 0)
        has_slot = cd.dir_info.get("has_slot", True)
        has_artwork = cd.has_artwork

        queue_penalty = min(queue / 2, 40)
        speed_bonus = min(speed / 500_000, 20)
        slot_bonus = 20 if has_slot else 0
        art_bonus = 10 if has_artwork else 0

        format_weight = format_score
        if self.target_format in ("flac", "lossless") and "FLAC" in format_label:
            format_weight += 30

        total_score = (match_ratio * 100) + format_weight + slot_bonus + speed_bonus + art_bonus - queue_penalty

        return {
            "user": cd.user,
            "directory": cd.dir_name,
            "queue": queue,
            "speed": speed,
            "has_slot": has_slot,
            "format_label": format_label,
            "format_score": format_score,
            "match_ratio": match_ratio,
            "matched_tracks": matched_tracks,
            "total_score": total_score,
            "has_artwork": has_artwork,
            "dir_info": cd.dir_info,
            "all_dir_files": [cf.raw_file for cf in cd.all_dir_files]
        }

    def _evaluate_directory_for_upgrade(
        self,
        user: str,
        dir_name: str,
        dir_info: Dict[str, Any],
        expected_tracks: List[Dict[str, Any]],
        album_title: str,
        artist_aliases: Set[str],
        local_score: int
    ) -> Optional[Dict[str, Any]]:
        """Backward-compatible evaluation wrapper."""
        cd = CandidateDir(user, dir_name, dir_info)
        parsed_expected = pre_parse_expected_tracks(expected_tracks)
        return self._evaluate_indexed_directory_for_upgrade(cd, parsed_expected, expected_tracks, album_title, artist_aliases, local_score)

    def _reconcile_album_upgrades(self, albums: List[Dict[str, Any]]):
        """Verifies candidate peer directories for low-quality albums with strict quality checks."""
        console.print(f"\n[bold cyan]Verifying Higher-Quality Releases for {len(albums)} Albums...[/bold cyan]")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for alb in albums:
            album_title = alb.get("album", "")
            artist_name = alb.get("artist", "")
            local_tracks = alb.get("tracks", [])
            local_min_score = alb.get("min_score", 40)
            local_formats = ", ".join(alb.get("formats", ["Unknown"]))

            expected_tracks = [{"title": t.get("title", ""), "norm_title": t.get("norm_title", "")} for t in local_tracks if t.get("title")]
            if not expected_tracks:
                continue

            artist_aliases = {artist_name, unidecode(artist_name), normalize_text(artist_name)}
            parsed_expected = pre_parse_expected_tracks(expected_tracks)
            clean_alb = clean_tokens(album_title)
            alb_words = [w for w in _tokenize_words_cached(album_title) if w not in DIR_STOP_WORDS]
            alb_sig_words = {w for w in alb_words if len(w) >= 3}

            cand_dirs = self.candidate_index.get_candidate_dirs_for_release(album_title, parsed_expected, len(expected_tracks))
            candidate_matches: List[Dict[str, Any]] = []

            for cd in cand_dirs:
                dir_matches = is_dir_name_match_fast(clean_alb, alb_sig_words, cd)
                if not dir_matches and len(expected_tracks) >= 3:
                    matched_cnt = 0
                    for pe in parsed_expected:
                        if any(is_track_title_match_fast(
                            pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
                            cf, artist_aliases, album_title, cd.dir_name
                        ) for cf in cd.audio_files):
                            matched_cnt += 1
                    if matched_cnt >= 2 and (matched_cnt / len(expected_tracks) >= 0.60):
                        dir_matches = True

                if dir_matches and cd.audio_files:
                    match_data = self._evaluate_indexed_directory_for_upgrade(cd, parsed_expected, expected_tracks, album_title, artist_aliases, local_min_score)
                    if match_data and match_data["match_ratio"] >= (0.60 if len(expected_tracks) >= 3 else 0.95):
                        if match_data["format_score"] > local_min_score or (self.target_format in ("flac", "lossless") and "FLAC" in match_data["format_label"] and local_min_score < 100):
                            candidate_matches.append(match_data)

            if candidate_matches:
                candidate_matches.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = candidate_matches[0]

                top_user = best_match["user"]
                top_dir = best_match["directory"]
                top_dir_info = self.peer_directories.get((top_user, top_dir), {})
                if top_dir_info.get("full_directory_files") is None:
                    try:
                        nodes = self.client.browse_directory(top_user, top_dir)
                        if nodes:
                            files = []
                            for node in nodes:
                                node_name = node.get("name", top_dir)
                                for f in node.get("files", []):
                                    fn = f.get("filename", "")
                                    full_fn = fn if ("\\" in fn or "/" in fn) else f"{node_name}\\{fn}"
                                    f_copy = dict(f)
                                    f_copy["full_filename"] = full_fn
                                    f_copy["base_filename"] = fn
                                    files.append(f_copy)
                            top_dir_info["full_directory_files"] = files
                            updated_cd = self.candidate_index.update_directory(top_user, top_dir, top_dir_info)
                            updated_match = self._evaluate_indexed_directory_for_upgrade(updated_cd, parsed_expected, expected_tracks, album_title, artist_aliases, local_min_score)
                            if updated_match and updated_match["format_score"] > local_min_score:
                                best_match = updated_match
                    except Exception:
                        pass

                self.verified_upgrades.append({
                    "category": "Album / Release",
                    "title": f"{artist_name} - {album_title}" if artist_name else album_title,
                    "artist": artist_name,
                    "album": album_title,
                    "local_quality": local_formats,
                    "local_score": local_min_score,
                    "local_tracks_count": len(local_tracks),
                    "upgraded_quality": best_match["format_label"],
                    "upgraded_score": best_match["format_score"],
                    "match_ratio": best_match["match_ratio"],
                    "best_match": best_match
                })
            else:
                self.unresolved_items.append({
                    "category": "Album / Release",
                    "title": f"{artist_name} - {album_title}" if artist_name else album_title,
                    "artist": artist_name,
                    "album": album_title,
                    "local_quality": local_formats,
                    "local_tracks_count": len(local_tracks)
                })

    def _reconcile_single_upgrades(self, singles: List[Dict[str, Any]]):
        """Verifies standalone single files against discovered peer files with strict quality checks."""
        if not singles:
            return

        console.print(f"\n[bold cyan]Verifying Higher-Quality Releases for {len(singles)} Standalone Singles...[/bold cyan]")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for s in singles:
            track_title = s.get("title", "")
            artist_name = s.get("artist", "")
            local_score = s.get("quality_score", 40)
            local_fmt = s.get("format_label", "Unknown")
            artist_aliases = {artist_name, unidecode(artist_name), normalize_text(artist_name)} if artist_name else set()

            parsed_s = pre_parse_single_track(track_title)
            cand_files = self.candidate_index.get_candidate_files_for_track(parsed_s)
            candidate_matches: List[Dict[str, Any]] = []

            for cf in cand_files:
                if is_track_title_match_fast(
                    parsed_s["p_struct"], parsed_s["words"], parsed_s["clean_words"],
                    parsed_s["sig_words"], parsed_s["concat"], cf, artist_aliases,
                    s.get("album", ""), cf.dir_name
                ):
                    fmt_label, fmt_score = cf.fmt_label, cf.fmt_score

                    if fmt_score > local_score or (self.target_format in ("flac", "lossless") and "FLAC" in fmt_label and local_score < 100):
                        dir_info = cf.dir_info
                        queue = dir_info.get("queue", 0)
                        speed = dir_info.get("speed", 0)
                        has_slot = dir_info.get("has_slot", True)
                        fmt_bonus = 30 if (self.target_format in ("flac", "lossless") and "FLAC" in fmt_label) else 0

                        candidate_matches.append({
                            "user": cf.user,
                            "directory": cf.dir_name,
                            "matched_file": cf.base_filename,
                            "full_filename": cf.full_filename,
                            "size": cf.size,
                            "format_label": fmt_label,
                            "format_score": fmt_score,
                            "queue": queue,
                            "speed": speed,
                            "has_slot": has_slot,
                            "total_score": fmt_score + fmt_bonus + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue / 2, 40),
                            "dir_info": dir_info
                        })

            if candidate_matches:
                candidate_matches.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = candidate_matches[0]
                self.verified_upgrades.append({
                    "category": "Standalone Single",
                    "title": f"{artist_name} - {track_title}" if artist_name else track_title,
                    "artist": artist_name,
                    "album": s.get("album", ""),
                    "local_quality": local_fmt,
                    "local_score": local_score,
                    "local_tracks_count": 1,
                    "upgraded_quality": best_match["format_label"],
                    "upgraded_score": best_match["format_score"],
                    "match_ratio": 1.0,
                    "best_match": best_match
                })
            else:
                self.unresolved_items.append({
                    "category": "Standalone Single",
                    "title": f"{artist_name} - {track_title}" if artist_name else track_title,
                    "artist": artist_name,
                    "album": s.get("album", ""),
                    "local_quality": local_fmt,
                    "local_tracks_count": 1
                })

    def _queue_upgrades(self):
        """Enqueues verified higher quality releases and tracks in slskd."""
        queued_dirs_set: Set[Tuple[str, str]] = set()
        queued_files_set: Set[str] = set(self.already_downloading_files)

        for up in self.verified_upgrades:
            bm = up["best_match"]
            user = bm["user"]
            dir_name = bm["directory"]
            dir_key = (user, dir_name)

            if up["category"] == "Album / Release":
                if dir_key in queued_dirs_set:
                    continue

                dir_files = bm.get("all_dir_files") or bm.get("dir_info", {}).get("matched_search_files", [])
                files_to_enqueue = []
                for f in dir_files:
                    base = f.get("base_filename") or extract_dir_and_filename(f.get("filename", ""))[1]
                    if is_audio_file(base) or is_supporting_file(base):
                        full_fn = f.get("full_filename") or f.get("filename")
                        if full_fn and full_fn not in queued_files_set:
                            files_to_enqueue.append({"filename": full_fn, "size": f.get("size", 0)})
                            queued_files_set.add(full_fn)

                if files_to_enqueue:
                    try:
                        self.client.enqueue_download(user, files_to_enqueue)
                        queued_dirs_set.add(dir_key)
                        self.queued_transfers.append({
                            "type": "Album Directory",
                            "title": up["title"],
                            "user": user,
                            "directory": dir_name,
                            "format": bm["format_label"],
                            "files_count": len(files_to_enqueue),
                            "total_size": sum(f["size"] for f in files_to_enqueue),
                            "status": "Enqueued"
                        })
                    except Exception as e:
                        console.print(f"[red]Failed to enqueue album '{dir_name}' from {user}: {e}[/red]")
            else:
                full_fn = bm.get("full_filename")
                if full_fn and full_fn not in queued_files_set:
                    files_to_enqueue = [{"filename": full_fn, "size": bm.get("size", 0)}]
                    try:
                        self.client.enqueue_download(user, files_to_enqueue)
                        queued_files_set.add(full_fn)
                        self.queued_transfers.append({
                            "type": "Standalone File",
                            "title": up["title"],
                            "user": user,
                            "directory": dir_name,
                            "format": bm["format_label"],
                            "files_count": 1,
                            "total_size": bm.get("size", 0),
                            "status": "Enqueued"
                        })
                    except Exception as e:
                        console.print(f"[red]Failed to enqueue file '{bm.get('matched_file')}' from {user}: {e}[/red]")

    def _print_reports(self):
        """Displays rich formatted comparison tables."""
        if self.verified_upgrades:
            table = Table(
                title="Verified Higher-Quality Soulseek Upgrades",
                box=box.ROUNDED,
                header_style="bold cyan"
            )
            table.add_column("#", style="dim", justify="right", width=4)
            table.add_column("Type", style="magenta", width=16)
            table.add_column("Release / Track Title", style="bold white")
            table.add_column("Current Quality", style="red", width=18)
            table.add_column("Upgraded Quality", style="bold green", width=22)
            table.add_column("Peer", style="cyan", width=14)
            table.add_column("Match", justify="center", width=10)

            for i, up in enumerate(self.verified_upgrades, 1):
                match_pct = f"{int(up['match_ratio'] * 100)}%"
                table.add_row(
                    str(i),
                    up["category"],
                    up["title"],
                    up["local_quality"],
                    up["upgraded_quality"],
                    up["best_match"]["user"],
                    f"[green]{match_pct}[/green]"
                )
            console.print("\n", table)

        if self.queued_transfers:
            q_table = Table(
                title="slskd Download Queue Summary",
                box=box.ROUNDED,
                header_style="bold green"
            )
            q_table.add_column("#", style="dim", justify="right", width=4)
            q_table.add_column("Type", style="magenta", width=16)
            q_table.add_column("Release Title", style="white")
            q_table.add_column("Peer", style="cyan", width=14)
            q_table.add_column("Files", justify="right", width=8)
            q_table.add_column("Size", justify="right", width=12)
            q_table.add_column("Status", style="bold green", width=10)

            for i, q in enumerate(self.queued_transfers, 1):
                size_mb = f"{q['total_size'] / (1024 * 1024):.1f} MB"
                q_table.add_row(
                    str(i),
                    q["type"],
                    q["title"],
                    q["user"],
                    str(q["files_count"]),
                    size_mb,
                    q["status"]
                )
            console.print("\n", q_table)

        tot_upgrades = len(self.verified_upgrades)
        tot_unresolved = len(self.unresolved_items)
        console.print(Panel.fit(
            f"[bold green]✔ Quality Upgrade Audit Complete[/bold green]\n"
            f"• Higher-Quality Upgrades Verified on Soulseek: [bold cyan]{tot_upgrades}[/bold cyan] releases/tracks\n"
            f"• Transfers Queued in slskd: [bold green]{len(self.queued_transfers)}[/bold green]\n"
            f"• Unresolved / Already Highest Available: [yellow]{tot_unresolved}[/yellow]",
            border_style="green"
        ))


# ==============================================================================
# CLI ENTRYPOINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Soulseek / slskd Music Quality Upgrader - Find Higher-Quality Releases for Low-Bitrate Files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan your music library and upgrade any lossy files to FLAC:
  python3 quality_upgrader.py /mnt/music/Library

  # Preview upgrades with dry-run mode:
  python3 quality_upgrader.py /mnt/music/Library/goreshit --dry-run

  # Upgrade files with bitrate <= 192 kbps to MP3-320 or FLAC:
  python3 quality_upgrader.py /mnt/music/Library --max-bitrate 192 --format 320

  # Upgrade a specific album folder to FLAC:
  python3 quality_upgrader.py "/mnt/music/Library/Mekuso/First Album" --format flac
        """
    )
    parser.add_argument("path", nargs="?", default=None, help="Target music directory, artist folder, album folder, or single audio file")
    parser.add_argument("-f", "--format", "--target-format", default="flac", choices=["flac", "lossless", "320", "mp3-320", "any-higher"], help="Target upgrade format (default: flac)")
    parser.add_argument("--max-bitrate", type=int, default=320, help="Maximum bitrate (kbps) to target for upgrade (default: 320)")
    parser.add_argument("--min-match", type=float, default=0.70, help="Minimum track match ratio for release verification (default: 0.70)")
    parser.add_argument("--timeout", type=float, default=14.0, help="Soulseek search timeout in seconds (default: 14.0)")
    parser.add_argument("--dry-run", action="store_true", help="Discover and verify upgrades without enqueuing downloads")
    parser.add_argument("--singles-only", action="store_true", help="Only download individual single files instead of full album directories")
    parser.add_argument("-t", "--threads", type=int, default=8, help="Worker threads for local scanning (default: 8)")
    parser.add_argument("--no-mb", action="store_true", help="Disable MusicBrainz metadata lookup enrichment")
    parser.add_argument("--export-json", default=None, help="Export upgrade results to a JSON file")

    args = parser.parse_args()

    target_path = args.path
    if not target_path:
        for cand in [Path("/mnt/music/Library"), Path("/mnt/music"), Path("/mnt/library"), Path.home() / "Music", Path("./downloads"), Path(".")]:
            if cand.exists() and any(cand.iterdir() if cand.is_dir() else [cand]):
                target_path = str(cand)
                break
    if not target_path:
        target_path = "."

    upgrader = SoulseekQualityUpgrader(
        target_path=target_path,
        target_format=args.format,
        max_bitrate=args.max_bitrate,
        min_match_ratio=args.min_match,
        search_timeout=args.timeout,
        dry_run=args.dry_run,
        singles_only=args.singles_only,
        use_mb=not args.no_mb,
        threads=args.threads
    )

    try:
        results = upgrader.run()
        if args.export_json:
            with open(args.export_json, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            console.print(f"[green]✔ Exported upgrade audit to {args.export_json}[/green]")
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation aborted by user.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
