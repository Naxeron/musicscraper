#!/usr/bin/env python3
"""
Soulseek / slskd Artist Discography Scraper & Tracklist Reconciler
=================================================================
Automates the search, tracklist verification, and directory download queueing
for an artist's full discography on Soulseek via slskd.

Workflow:
1. Queries MusicBrainz to build the complete artist catalog (primary releases, EPs,
   split releases, compilations, and individual recordings).
2. Pre-scans the local/server music library (Read-Only) to detect existing tracks.
3. Performs intelligent Soulseek searches across the Soulseek network via slskd.
4. Aggregates results by peer and remote directory, browsing full directory contents.
5. Reconciles and verifies candidate directories against MusicBrainz tracklists
   (checking track names, numbering, audio formats, and durations).
6. Prioritizes lossless (FLAC) and high-bitrate (MP3-320) releases with low peer queues.
7. Automatically queues complete verified directories (audio + artwork/cue/log) in slskd.
8. Generates rich terminal reports and exports audit summaries.
"""

import os
import sys
import re
import csv
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from unidecode import unidecode
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.text import Text
from rich import box

from slskd_api import SlskdClient, SlskdAPIError
from check_missing_tracks import (
    MusicBrainzClient,
    ArtistCatalog,
    AudioFileScanner,
    DiscographyReconciler,
    ReportGenerator,
    normalize_text,
    strip_track_number_and_artist,
    DEFAULT_CACHE_DIR,
    AUDIO_EXTENSIONS,
)

console = Console()

SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav",
    ".aif", ".aiff", ".wma", ".ape", ".wv"
}

SUPPORTING_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".cue", ".log", ".nfo"
}


def katakana_to_hiragana(text: str) -> str:
    """Converts Katakana characters to Hiragana for uniform Japanese phonetic matching."""
    res = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            res.append(chr(code - 0x60))
        else:
            res.append(ch)
    return "".join(res)


def clean_tokens(text: str) -> str:
    """Strips all punctuation and whitespace for fuzzy token comparison."""
    if not text:
        return ""
    norm = katakana_to_hiragana(text.lower())
    norm = unidecode(norm)
    return re.sub(r"[^a-z0-9]", "", norm)


def tokenize_words(text: str) -> List[str]:
    """Extracts lowercase alphanumeric words with unidecode transliteration and number splitting."""
    if not text:
        return []
    norm = katakana_to_hiragana(text.lower())
    norm = unidecode(norm)
    norm = re.sub(r"([a-zA-Z])([0-9])", r"\1 \2", norm)
    norm = re.sub(r"([0-9])([a-zA-Z])", r"\1 \2", norm)
    return re.findall(r"[a-z0-9]+", norm)


def is_sublist(sub: List[str], full: List[str]) -> bool:
    """Checks if sub is an exact contiguous sub-sequence of full word tokens."""
    if not sub or not full or len(sub) > len(full):
        return False
    sub_len = len(sub)
    for i in range(len(full) - sub_len + 1):
        if full[i:i + sub_len] == sub:
            return True
    return False


def is_track_title_match(
    exp_title: str,
    candidate_filename: str,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = ""
) -> bool:
    """
    Strictly verifies if candidate_filename matches expected track title:
    - Uses exact whole-word token sub-sequence matching to avoid substring false positives.
    - For short or generic titles (<= 4 chars or <= 2 words), enforces contextual validation
      (requires artist name or release title in the file/directory path).
    """
    exp_words = tokenize_words(exp_title)
    if not exp_words:
        return False

    clean_exp_title = strip_track_number_and_artist(exp_title)
    clean_exp_words = tokenize_words(clean_exp_title) or exp_words

    file_words = tokenize_words(candidate_filename)
    clean_file_words = tokenize_words(strip_track_number_and_artist(candidate_filename)) or file_words

    # Check for direct token sequence match
    exact_match = (
        is_sublist(exp_words, file_words) or
        is_sublist(clean_exp_words, clean_file_words) or
        is_sublist(clean_exp_words, file_words)
    )

    # Check concatenated tokens (e.g. FBD vs F>B>D, SSSS vs $S$S$, mugenuingu vs むげん☆ういんぐ)
    exp_concat = "".join(exp_words)
    file_concat = "".join(file_words)
    if not exact_match and len(exp_concat) >= 3:
        if exp_concat in file_concat or file_concat in exp_concat:
            exact_match = True

    if not exact_match:
        return False

    # For short/generic titles, require context
    if len(clean_exp_words) <= 2 or len(exp_concat) <= 4:
        dir_norm = clean_tokens(dir_path)
        file_norm = clean_tokens(candidate_filename)
        has_artist_context = any(clean_tokens(alias) in dir_norm or clean_tokens(alias) in file_norm for alias in artist_aliases if len(alias) >= 3)
        has_rel_context = False
        if rel_title:
            rel_words = [w for w in tokenize_words(rel_title) if w not in ("various", "artists", "va", "compilation", "album", "ep", "vol")]
            if rel_words:
                has_rel_context = (
                    is_sublist(rel_words[:3], tokenize_words(dir_path)) or
                    any(w in dir_norm for w in rel_words if len(w) >= 4)
                )

        if not has_artist_context and not has_rel_context:
            return False

    return True


def clean_search_query(artist: str, title: str) -> str:
    """Prepares a clean, punctuation-free search query for Soulseek."""
    raw = f"{artist} {title}"
    clean = re.sub(r"[\-_:\(\)\[\]\{\}\.\,\+\?\!\~\"\'\/\\\|\@\#\$\%\^\&\*]+", " ", raw)
    words = [w for w in clean.split() if w.lower() not in ("va", "various", "artists", "feat", "ft")]
    return " ".join(words[:6])


def clean_compilation_queries(title: str) -> List[str]:
    """Generates clean, targeted search queries for a compilation album."""
    queries = []
    # 1. Main prefix before subtitle separator (e.g. 'GACHIHOMO HARDCORE' from 'GACHIHOMO HARDCORE -IS MR. XYSTRAN A GAY?-')
    prefix = re.split(r"[-~:]", title)[0].strip()
    prefix_clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", prefix)
    prefix_clean = re.sub(r"[^a-zA-Z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\s]", " ", prefix_clean)
    prefix_clean = re.sub(r"\s+", " ", prefix_clean).strip()
    if prefix_clean and len(prefix_clean) > 3:
        queries.append(prefix_clean)

    # If title starts with Re: (e.g. Re:厨弐病), also query without Re:
    if title.lower().startswith("re:"):
        no_re = re.sub(r"^re:\s*", "", title, flags=re.IGNORECASE).strip()
        no_re_clean = re.sub(r"[^a-zA-Z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\s]", " ", no_re).strip()
        if no_re_clean and no_re_clean not in queries:
            queries.append(no_re_clean)

    # 2. Full title stripped of symbols
    full_clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", title)
    full_clean = re.sub(r"[^a-zA-Z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\s]", " ", full_clean)
    full_clean = re.sub(r"\s+", " ", full_clean).strip()
    words = [w for w in full_clean.split() if w.lower() not in ("va", "various", "artists", "compilation")]
    if words:
        clean_s = " ".join(words[:6])
        if clean_s and clean_s not in queries:
            queries.append(clean_s)

    return list(dict.fromkeys(queries))


def clean_track_queries(title: str, artist_credit: str = "", primary_artist: str = "") -> List[str]:
    """Generates targeted search queries for a standalone or featured track."""
    queries = []

    # Clean title
    clean_t = strip_track_number_and_artist(title)
    clean_t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", clean_t)
    clean_t = re.sub(r"[^a-zA-Z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\s]", " ", clean_t)
    clean_t = re.sub(r"\s+", " ", clean_t).strip()

    # Extract collaborator/lead artist if credit contains 'feat', 'vs', '×', '&', '-'
    collabs = []
    if artist_credit:
        m = re.split(r"(?:feat\.?|featuring|vs\.?|×|&|-|\+)", artist_credit, flags=re.IGNORECASE)
        for part in m:
            clean_part = part.strip()
            if clean_part and clean_part.lower() != primary_artist.lower():
                cp = re.sub(r"[^a-zA-Z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\s]", " ", clean_part).strip()
                if cp:
                    collabs.append(cp)

    for collab in collabs:
        if clean_t:
            queries.append(f"{collab} {clean_t}".strip())

    if primary_artist and clean_t and len(clean_t) > 3:
        queries.append(f"{primary_artist} {clean_t}".strip())

    if clean_t and len(clean_t) >= 4:
        queries.append(clean_t)

    # Sub-phrase for complex titles (e.g. TheAmenZa from 珍宝的-TheAmenZa-menson1MIX-)
    sub_words = re.findall(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]{4,}", title)
    for sw in sub_words:
        if sw.lower() not in (primary_artist.lower(), "remix", "mix", "edit", "ver", "feat"):
            queries.append(sw)

    return list(dict.fromkeys(queries))


def sanitize_remote_path(path_str: str) -> str:
    """Normalizes backslashes/slashes for Soulseek paths."""
    return path_str.replace("/", "\\")


def extract_dir_and_filename(full_path: str) -> Tuple[str, str]:
    """Splits a Soulseek full remote path into (directory, filename)."""
    norm = sanitize_remote_path(full_path)
    parts = norm.rsplit("\\", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def is_audio_file(filename: str) -> bool:
    """Checks if filename has a supported audio extension."""
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTED_AUDIO_EXTENSIONS


def is_supporting_file(filename: str) -> bool:
    """Checks if filename is artwork, log, cue, or info."""
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTING_EXTENSIONS


def determine_audio_quality(file_item: Dict[str, Any]) -> Tuple[str, int]:
    """
    Evaluates audio format and bitrate/bitdepth from slskd file attributes.
    Returns (format_label, quality_score).
    """
    fn = file_item.get("filename", "") or file_item.get("base_filename", "")
    ext = Path(fn).suffix.lower()
    bit_rate = file_item.get("bitRate", 0)
    bit_depth = file_item.get("bitDepth", 0)
    sample_rate = file_item.get("sampleRate", 0)

    if ext in (".flac", ".wav", ".aif", ".aiff", ".ape", ".wv"):
        if bit_depth and bit_depth > 16:
            return f"FLAC {bit_depth}-bit/{sample_rate or 44100}Hz", 110
        return "FLAC (Lossless)", 100

    if ext in (".mp3", ".m4a", ".ogg", ".opus"):
        if bit_rate >= 320:
            return "MP3 320kbps", 80
        elif bit_rate >= 240:
            return f"MP3 ~{bit_rate}kbps (V0)", 70
        elif bit_rate >= 192:
            return f"MP3 {bit_rate}kbps", 50
        elif bit_rate > 0:
            return f"MP3 {bit_rate}kbps", 30
        return ext.upper().lstrip("."), 40

    return "Audio File", 40


class SlskdArtistScraper:
    """Orchestrates Soulseek searches, directory verification, and download queueing."""

    def __init__(
        self,
        artist_query: str,
        slskd_client: Optional[SlskdClient] = None,
        music_dir: Optional[str] = None,
        preferred_format: str = "flac",
        min_match_ratio: float = 0.70,
        search_timeout: float = 8.0,
        dry_run: bool = False,
        singles_only: bool = False,
        cache_dir: str = DEFAULT_CACHE_DIR,
        threads: int = 4,
    ):
        self.artist_query = artist_query.strip()
        self.client = slskd_client or SlskdClient()
        self.preferred_format = preferred_format.lower()
        self.min_match_ratio = min_match_ratio
        self.search_timeout = search_timeout
        self.dry_run = dry_run
        self.singles_only = singles_only
        self.cache_dir = cache_dir
        self.threads = threads

        # Auto-detect server music library
        if music_dir:
            self.music_dir = Path(music_dir).resolve()
        else:
            self.music_dir = None
            for cand in [Path("/mnt/music/Library"), Path("/mnt/music"), Path("/mnt/library"), Path.home() / "Music"]:
                if cand.exists() and any(cand.iterdir()):
                    self.music_dir = cand
                    break

        self.mb_client = MusicBrainzClient(cache_dir=self.cache_dir)
        self.catalog: Optional[ArtistCatalog] = None
        self.raw_mb_data: Dict[str, Any] = {}

        # Local library scan state
        self.local_found_map: Dict[str, Dict[str, Any]] = {}
        self.local_found_releases: Set[str] = set()

        # Discovered peers & directory cache
        self.peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.searches_performed: Set[str] = set()

        # Audit & download state
        self.queued_directories: List[Dict[str, Any]] = []
        self.already_downloading_files: Set[str] = set()
        self.verified_releases: List[Dict[str, Any]] = []
        self.unresolved_releases: List[Dict[str, Any]] = []
        self.verified_compilation_tracks: List[Dict[str, Any]] = []
        self.unresolved_compilation_tracks: List[Dict[str, Any]] = []
        self.verified_standalone_tracks: List[Dict[str, Any]] = []
        self.unresolved_standalone_tracks: List[Dict[str, Any]] = []

    def run(self) -> Dict[str, Any]:
        """Runs the complete Soulseek discography discovery and queueing pipeline."""
        console.print(Panel.fit(
            f"[bold cyan]Soulseek / slskd Artist Scraper & Reconciler[/bold cyan]\n"
            f"[dim]Target Artist: {self.artist_query}[/dim]",
            border_style="cyan"
        ))

        # 1. Check slskd Connection
        app_info = self.client.get_application()
        slsk_user = app_info.get("user", {}).get("username", "Unknown")
        server_state = app_info.get("server", {}).get("state", "Unknown")
        console.print(f"[green]✔ Connected to slskd[/green] (Soulseek User: [bold]{slsk_user}[/bold] | Server: [dim]{server_state}[/dim])")

        # Refresh currently active/queued downloads in slskd
        self.already_downloading_files = self.client.get_queued_filenames()
        if self.already_downloading_files:
            console.print(f"[dim]Active/queued in slskd: {len(self.already_downloading_files)} files[/dim]")

        # 2. Resolve Artist & Fetch Catalog from MusicBrainz
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(self.artist_query)
        self.raw_mb_data = self.mb_client.fetch_full_discography(mbid)
        self.catalog = ArtistCatalog(self.raw_mb_data)

        console.print(f"[green]✔ Canonical Name:[/green] [bold]{self.catalog.name}[/bold] (MBID: {self.catalog.mbid})")
        primary_rels = self.catalog.releases
        comp_tracks = self.raw_mb_data.get("releases_track_artist", [])
        console.print(f"[dim]Catalog: {len(self.catalog.tracks)} total tracks | {len(primary_rels)} primary releases | {len(comp_tracks)} compilation/VA releases[/dim]")

        # 3. Pre-Scan Local Music Library (Read-Only)
        self._prescan_library()

        # 4. Search Soulseek & Aggregate Candidate Directories
        self._discover_soulseek_candidates()

        # 5. Verify & Reconcile Primary Releases
        self._reconcile_primary_releases()

        # 6. Verify & Reconcile Compilation / VA Tracks (with targeted compilation searches)
        self._reconcile_compilation_tracks()

        # 7. Verify & Reconcile Standalone & Non-Album Tracks (with collaborator searches)
        self._reconcile_standalone_tracks()

        # 8. Queue Verified Directories & Tracks in slskd
        if not self.dry_run:
            self._queue_downloads()
        else:
            console.print("\n[yellow]--dry-run enabled: Showing matched directories without enqueuing transfers.[/yellow]")

        # 9. Print Summary & Reports
        self._print_summary()

        return {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "queued_directories": self.queued_directories,
            "verified_releases": self.verified_releases,
            "unresolved_releases": self.unresolved_releases,
            "verified_compilation_tracks": self.verified_compilation_tracks,
            "unresolved_compilation_tracks": self.unresolved_compilation_tracks,
            "verified_standalone_tracks": self.verified_standalone_tracks,
            "unresolved_standalone_tracks": self.unresolved_standalone_tracks,
        }

    def _prescan_library(self):
        """Scans local music library to avoid downloading releases already in collection."""
        if not self.music_dir or not self.music_dir.exists():
            return

        console.print(f"\n[cyan]Pre-scanning local music library at {self.music_dir} (Read-Only)...[/cyan]")
        scanner = AudioFileScanner(
            music_dir=str(self.music_dir),
            catalog=self.catalog,
            full_scan=False,
            threads=self.threads
        )
        local_tracks = scanner.scan()

        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=local_tracks)
        found_items, _ = reconciler.reconcile()

        for item in found_items:
            mb = item["mb_track"]
            lt = item["local_track"]
            norm_t = mb.get("norm_title", "")
            if norm_t:
                self.local_found_map[norm_t] = lt

        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            rel_tracks = [t for t in self.catalog.tracks if t.get("norm_release") == norm_rel or rel_title in t.get("all_releases", set())]
            if rel_tracks and all(t.get("norm_title") in self.local_found_map for t in rel_tracks):
                self.local_found_releases.add(norm_rel)

        console.print(f"[green]✔ Local Library Status:[/green] [bold]{len(found_items)}[/bold] artist tracks / [bold]{len(self.local_found_releases)}[/bold] releases already in library.")

    def _execute_search(self, query: str) -> List[Dict[str, Any]]:
        """Executes a Soulseek search and registers candidate peer directories."""
        clean_q = query.strip()
        if not clean_q or clean_q.lower() in self.searches_performed:
            return []

        self.searches_performed.add(clean_q.lower())
        try:
            search_res = self.client.search(clean_q, timeout=self.search_timeout, min_responses=5)
            responses = search_res.get("responses", [])
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
            return responses
        except Exception as e:
            return []

    def _discover_soulseek_candidates(self):
        """Runs broad and targeted searches across Soulseek."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Searching Soulseek for {self.catalog.name}...", total=None)

            # 1. Broad Artist Name Search
            self._execute_search(self.catalog.name)

            # Also check artist aliases if any
            for alias in list(self.catalog.aliases)[:2]:
                if alias.lower() != self.catalog.name.lower() and len(alias) > 3:
                    self._execute_search(alias)

            progress.update(task, description=f"Discovered {len(self.peer_directories)} peer directories from artist search.")

        console.print(f"[green]✔ Soulseek Discovery:[/green] Found [bold]{len(self.peer_directories)}[/bold] candidate directories across peers.")

    def _get_or_fetch_full_directory(self, user: str, dir_name: str) -> List[Dict[str, Any]]:
        """Retrieves and caches the complete file list of a remote directory."""
        key = (user, dir_name)
        cached_info = self.peer_directories.get(key)
        if cached_info and cached_info.get("full_directory_files") is not None:
            return cached_info["full_directory_files"]

        try:
            nodes = self.client.browse_directory(user, dir_name)
            files = []
            for node in nodes:
                node_name = node.get("name", dir_name)
                for f in node.get("files", []):
                    fn = f.get("filename", "")
                    if "\\" in fn or "/" in fn:
                        full_fn = fn
                    else:
                        full_fn = f"{node_name}\\{fn}"
                    f_copy = dict(f)
                    f_copy["full_filename"] = full_fn
                    f_copy["base_filename"] = fn
                    files.append(f_copy)

            if cached_info:
                cached_info["full_directory_files"] = files
            return files
        except Exception:
            return []

    def _reconcile_primary_releases(self):
        """Verifies candidate directories against all primary releases from MusicBrainz."""
        console.print(f"\n[bold cyan]Verifying Primary Releases ({len(self.catalog.releases)} releases)...[/bold cyan]")

        # Index peer directories by clean path tokens for instant lookup
        dir_token_map: Dict[Tuple[str, str], str] = {
            (user, d): clean_tokens(d)
            for (user, d) in self.peer_directories.keys()
        }

        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            token_rel = clean_tokens(rel_title)

            # Check if already present on server/local library
            if norm_rel in self.local_found_releases:
                continue

            expected_tracks = [
                t for t in self.catalog.tracks
                if t.get("norm_release") == norm_rel or rel_title in t.get("all_releases", set())
            ]
            if not expected_tracks:
                continue

            candidate_matches: List[Dict[str, Any]] = []

            # 1. Match against discovered directories
            for (user, dir_name), dir_tokens in dir_token_map.items():
                if token_rel and (token_rel in dir_tokens or any(len(w) >= 4 and w in dir_tokens for w in token_rel.split())):
                    dir_info = self.peer_directories.get((user, dir_name), {})
                    match_data = self._verify_directory_against_release(user, dir_name, dir_info, expected_tracks, rel_title)
                    if match_data:
                        candidate_matches.append(match_data)

            # 2. If no candidate found from broad search, run a clean targeted search
            if not candidate_matches and len(self.searches_performed) < 25:
                q = clean_search_query(self.catalog.name, rel_title)
                self._execute_search(q)
                # Check new entries
                for (user, dir_name), dir_info in list(self.peer_directories.items()):
                    if (user, dir_name) not in dir_token_map:
                        dir_token_map[(user, dir_name)] = clean_tokens(dir_name)
                    dir_tokens = dir_token_map[(user, dir_name)]
                    if token_rel and (token_rel in dir_tokens or any(len(w) >= 4 and w in dir_tokens for w in token_rel.split())):
                        match_data = self._verify_directory_against_release(user, dir_name, dir_info, expected_tracks, rel_title)
                        if match_data:
                            candidate_matches.append(match_data)

            if candidate_matches:
                candidate_matches.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = candidate_matches[0]
                self.verified_releases.append({
                    "release_title": rel_title,
                    "release_type": rel.get("type", "Release"),
                    "date": rel.get("date", "N/A"),
                    "expected_track_count": len(expected_tracks),
                    "best_match": best_match,
                    "all_candidates_count": len(candidate_matches)
                })
            else:
                self.unresolved_releases.append({
                    "release_title": rel_title,
                    "release_type": rel.get("type", "Release"),
                    "date": rel.get("date", "N/A"),
                    "expected_track_count": len(expected_tracks),
                    "expected_tracks": [t.get("title") for t in expected_tracks]
                })

    def _verify_directory_against_release(
        self,
        user: str,
        dir_name: str,
        dir_info: Dict[str, Any],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
        """Compares directory contents against expected release tracklist."""
        # Check files from search responses first
        search_files = dir_info.get("matched_search_files", [])
        if not search_files:
            return None

        # Build tracklist
        matched_tracks: List[Dict[str, Any]] = []
        unmatched_expected: List[Dict[str, Any]] = []

        for exp in expected_tracks:
            exp_title = exp.get("title", "")

            matched_file = None
            for sf in search_files:
                fn = sf.get("filename", "")
                base = extract_dir_and_filename(fn)[1]
                if not is_audio_file(base):
                    continue

                if is_track_title_match(exp_title, base, set(self.catalog.aliases), rel_title, dir_name):
                    matched_file = sf
                    break

            if matched_file:
                matched_tracks.append({
                    "expected": exp_title,
                    "matched_file": extract_dir_and_filename(matched_file.get("filename", ""))[1],
                    "size": matched_file.get("size")
                })
            else:
                unmatched_expected.append(exp)

        match_ratio = len(matched_tracks) / len(expected_tracks) if expected_tracks else 0.0

        if match_ratio < self.min_match_ratio and len(matched_tracks) == 0:
            return None

        # Evaluate quality
        primary_audio = search_files[0]
        format_label, format_score = determine_audio_quality(primary_audio)

        queue = dir_info.get("queue", 0)
        speed = dir_info.get("speed", 0)
        has_slot = dir_info.get("has_slot", True)

        queue_penalty = min(queue, 50)
        speed_bonus = min(speed / 500_000, 20)
        slot_bonus = 20 if has_slot else 0

        total_score = (match_ratio * 100) + format_score + slot_bonus + speed_bonus - queue_penalty

        return {
            "user": user,
            "directory": dir_name,
            "queue": queue,
            "speed": speed,
            "has_slot": has_slot,
            "format_label": format_label,
            "format_score": format_score,
            "match_ratio": match_ratio,
            "matched_tracks": matched_tracks,
            "unmatched_expected": [u.get("title") for u in unmatched_expected],
            "total_score": total_score,
            "dir_info": dir_info
        }

    def _reconcile_compilation_tracks(self):
        """Verifies candidate tracks on compilation / VA releases with multi-tier targeted queries."""
        comp_releases = self.raw_mb_data.get("releases_track_artist", [])
        console.print(f"\n[bold cyan]Verifying Compilation & Split Tracks ({len(comp_releases)} releases)...[/bold cyan]")

        # Index all audio files across candidate directories for fast lookups
        all_discovered_audio_files: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        for (user, dir_name), dir_info in self.peer_directories.items():
            for sf in dir_info.get("matched_search_files", []):
                fn = sf.get("filename", "")
                base = extract_dir_and_filename(fn)[1]
                if is_audio_file(base):
                    all_discovered_audio_files.append((user, dir_name, sf, dir_info))

        for rel in comp_releases:
            rel_title = rel.get("title", "")
            artist_credit = rel.get("artist-credit-phrase", "Various Artists")
            norm_rel = normalize_text(rel_title)

            # Target tracks on this release attributed to artist
            target_tracks = []
            for m in rel.get("medium-list", []):
                for t in m.get("track-list", []):
                    rec = t.get("recording", {})
                    rec_artists = [ac.get("artist", {}).get("name", "").lower() for ac in rec.get("artist-credit", []) if isinstance(ac, dict)]
                    t_title = rec.get("title", t.get("title", ""))
                    if any(a in " ".join(rec_artists) or a in t_title.lower() for a in self.catalog.aliases):
                        target_tracks.append({
                            "title": t_title,
                            "norm_title": normalize_text(t_title),
                            "artist_credit": " ".join(rec_artists),
                        })

            if not target_tracks:
                target_tracks = [
                    {"title": t.get("title"), "norm_title": t.get("norm_title"), "artist_credit": t.get("artist_credit", "")}
                    for t in self.catalog.tracks if t.get("norm_release") == norm_rel
                ]

            missing_tracks = [tt for tt in target_tracks if tt["norm_title"] not in self.local_found_map]
            if not missing_tracks:
                continue

            # Check if any missing track can already be matched from peer directories
            has_candidate = any(
                any(is_track_title_match(tt["title"], extract_dir_and_filename(sf.get("filename", ""))[1], set(self.catalog.aliases), rel_title, d)
                    for _, d, sf, _ in all_discovered_audio_files)
                for tt in missing_tracks
            )

            # If not found, execute targeted search for compilation
            if not has_candidate and len(self.searches_performed) < 45:
                comp_queries = clean_compilation_queries(rel_title)
                for q in comp_queries[:2]:
                    new_res = self._execute_search(q)
                    if new_res:
                        for (u, d), d_info in self.peer_directories.items():
                            for sf in d_info.get("matched_search_files", []):
                                fn = sf.get("filename", "")
                                base = extract_dir_and_filename(fn)[1]
                                if is_audio_file(base) and (u, d, sf, d_info) not in all_discovered_audio_files:
                                    all_discovered_audio_files.append((u, d, sf, d_info))

            for tt in missing_tracks:
                t_title = tt.get("title", "")
                norm_t = tt.get("norm_title", "")
                candidate_matches: List[Dict[str, Any]] = []

                for user, dir_name, sf, dir_info in all_discovered_audio_files:
                    fn = sf.get("filename", "")
                    base = extract_dir_and_filename(fn)[1]

                    if is_track_title_match(t_title, base, set(self.catalog.aliases), rel_title, dir_name):
                        fmt_label, fmt_score = determine_audio_quality(sf)
                        queue = dir_info.get("queue", 0)
                        speed = dir_info.get("speed", 0)
                        has_slot = dir_info.get("has_slot", True)

                        candidate_matches.append({
                            "user": user,
                            "directory": dir_name,
                            "matched_file": base,
                            "full_filename": fn,
                            "size": sf.get("size", 0),
                            "format_label": fmt_label,
                            "format_score": fmt_score,
                            "queue": queue,
                            "speed": speed,
                            "has_slot": has_slot,
                            "total_score": fmt_score + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue, 50),
                            "dir_info": dir_info
                        })

                if candidate_matches:
                    candidate_matches.sort(key=lambda x: x["total_score"], reverse=True)
                    best_match = candidate_matches[0]
                    self.verified_compilation_tracks.append({
                        "track_title": t_title,
                        "release_title": rel_title,
                        "release_artist": artist_credit,
                        "best_match": best_match
                    })
                else:
                    self.unresolved_compilation_tracks.append({
                        "track_title": t_title,
                        "release_title": rel_title,
                        "release_artist": artist_credit
                    })

    def _reconcile_standalone_tracks(self):
        """Discovers and verifies standalone singles, remixes, collaborations, and non-album tracks."""
        console.print(f"\n[bold cyan]Verifying Standalone & Non-Album Tracks...[/bold cyan]")

        # Identify all tracks in catalog not on verified primary or compilation releases
        all_covered_titles = set(self.local_found_map.keys())
        for vr in self.verified_releases:
            for mt in vr["best_match"].get("matched_tracks", []):
                all_covered_titles.add(normalize_text(mt.get("expected", "")))
        for vc in self.verified_compilation_tracks:
            all_covered_titles.add(normalize_text(vc.get("track_title", "")))

        standalone_candidates = [
            t for t in self.catalog.tracks
            if t.get("norm_title") not in all_covered_titles
        ]

        if not standalone_candidates:
            return

        all_discovered_audio_files: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        for (user, dir_name), dir_info in self.peer_directories.items():
            for sf in dir_info.get("matched_search_files", []):
                fn = sf.get("filename", "")
                base = extract_dir_and_filename(fn)[1]
                if is_audio_file(base):
                    all_discovered_audio_files.append((user, dir_name, sf, dir_info))

        for t in standalone_candidates:
            t_title = t.get("title", "")
            norm_t = t.get("norm_title", "")
            artist_credit = t.get("artist_credit", "")

            if norm_t in all_covered_titles:
                continue

            matched_candidates: List[Dict[str, Any]] = []

            for user, dir_name, sf, dir_info in all_discovered_audio_files:
                fn = sf.get("filename", "")
                base = extract_dir_and_filename(fn)[1]
                if is_track_title_match(t_title, base, set(self.catalog.aliases), t.get("release_title", ""), dir_name):
                    fmt_label, fmt_score = determine_audio_quality(sf)
                    queue = dir_info.get("queue", 0)
                    speed = dir_info.get("speed", 0)
                    has_slot = dir_info.get("has_slot", True)
                    matched_candidates.append({
                        "user": user,
                        "directory": dir_name,
                        "matched_file": base,
                        "full_filename": fn,
                        "size": sf.get("size", 0),
                        "format_label": fmt_label,
                        "format_score": fmt_score,
                        "queue": queue,
                        "speed": speed,
                        "has_slot": has_slot,
                        "total_score": fmt_score + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue, 50),
                        "dir_info": dir_info
                    })

            # If no candidate found, execute targeted track searches!
            if not matched_candidates and len(self.searches_performed) < 60:
                track_queries = clean_track_queries(t_title, artist_credit, self.catalog.name)
                for q in track_queries[:2]:
                    new_res = self._execute_search(q)
                    if new_res:
                        for (u, d), d_info in self.peer_directories.items():
                            for sf in d_info.get("matched_search_files", []):
                                fn = sf.get("filename", "")
                                base = extract_dir_and_filename(fn)[1]
                                if is_audio_file(base):
                                    if (u, d, sf, d_info) not in all_discovered_audio_files:
                                        all_discovered_audio_files.append((u, d, sf, d_info))
                                    if is_track_title_match(t_title, base, set(self.catalog.aliases), t.get("release_title", ""), d):
                                        fmt_label, fmt_score = determine_audio_quality(sf)
                                        queue = d_info.get("queue", 0)
                                        speed = d_info.get("speed", 0)
                                        has_slot = d_info.get("has_slot", True)
                                        matched_candidates.append({
                                            "user": u,
                                            "directory": d,
                                            "matched_file": base,
                                            "full_filename": fn,
                                            "size": sf.get("size", 0),
                                            "format_label": fmt_label,
                                            "format_score": fmt_score,
                                            "queue": queue,
                                            "speed": speed,
                                            "has_slot": has_slot,
                                            "total_score": fmt_score + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue, 50),
                                            "dir_info": d_info
                                        })

            if matched_candidates:
                matched_candidates.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = matched_candidates[0]
                self.verified_standalone_tracks.append({
                    "track_title": t_title,
                    "artist_credit": artist_credit or self.catalog.name,
                    "best_match": best_match
                })
                all_covered_titles.add(norm_t)
            else:
                self.unresolved_standalone_tracks.append({
                    "track_title": t_title,
                    "artist_credit": artist_credit or self.catalog.name
                })

    def _queue_downloads(self):
        """Enqueues verified primary release directories, compilation tracks, and standalone tracks into slskd."""
        queued_dirs_set = set()
        queued_files_set = set(self.already_downloading_files)

        # 1. Queue Primary Release Directories
        for item in self.verified_releases:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            # Fetch complete folder from peer
            dir_files = self._get_or_fetch_full_directory(user, dir_name)
            if not dir_files:
                dir_files = match.get("dir_info", {}).get("matched_search_files", [])

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
                    self.queued_directories.append({
                        "type": "Primary Release",
                        "title": item["release_title"],
                        "user": user,
                        "directory": dir_name,
                        "format": match["format_label"],
                        "files_count": len(files_to_enqueue),
                        "total_size": sum(f["size"] for f in files_to_enqueue),
                        "status": "Enqueued"
                    })
                except Exception as e:
                    console.print(f"[red]Failed to enqueue directory '{dir_name}' from {user}: {e}[/red]")

    def _queue_downloads(self):
        """Enqueues verified primary release directories, compilation tracks, and standalone tracks into slskd."""
        queued_dirs_set = set()
        queued_files_set = set(self.already_downloading_files)

        def is_loose_dump(d_path: str) -> bool:
            parts = [p.strip().lower() for p in sanitize_remote_path(d_path).split("\\") if p.strip()]
            last = parts[-1] if parts else ""
            return any(k in last for k in ("soundcloud singles", "loose tracks", "random singles", "singles", "various singles"))

        # 1. Queue Primary Release Directories
        for item in self.verified_releases:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            dir_files = self._get_or_fetch_full_directory(user, dir_name)
            if not dir_files:
                dir_files = match.get("dir_info", {}).get("matched_search_files", [])

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
                    self.queued_directories.append({
                        "type": "Primary Release",
                        "title": item["release_title"],
                        "user": user,
                        "directory": dir_name,
                        "format": match["format_label"],
                        "files_count": len(files_to_enqueue),
                        "total_size": sum(f["size"] for f in files_to_enqueue),
                        "status": "Enqueued"
                    })
                except Exception as e:
                    console.print(f"[red]Failed to enqueue directory '{dir_name}' from {user}: {e}[/red]")

        # 2. Queue Compilation / Split Release Directories
        for item in self.verified_compilation_tracks:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            # By default, download full compilation folder unless --singles-only is set or folder is a loose dump
            download_full_dir = not self.singles_only and not is_loose_dump(dir_name)
            dir_files = self._get_or_fetch_full_directory(user, dir_name) if download_full_dir else []

            # Safety check: avoid directories with > 100 files if it's an accidental root share
            if dir_files and len(dir_files) > 100:
                dir_files = []

            if not dir_files:
                full_fn = match.get("full_filename")
                files_to_enqueue = [{"filename": full_fn, "size": match.get("size", 0)}] if full_fn and full_fn not in queued_files_set else []
            else:
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
                    self.queued_directories.append({
                        "type": "Compilation Album" if len(files_to_enqueue) > 1 else "Compilation Track",
                        "title": f"{item['release_title']} (feat. {item['track_title']})",
                        "user": user,
                        "directory": dir_name,
                        "format": match["format_label"],
                        "files_count": len(files_to_enqueue),
                        "total_size": sum(f["size"] for f in files_to_enqueue),
                        "status": "Enqueued"
                    })
                except Exception as e:
                    console.print(f"[red]Failed to enqueue compilation '{dir_name}' from {user}: {e}[/red]")

        # 3. Queue Standalone / Guest Track Releases
        for item in self.verified_standalone_tracks:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            # By default, download full release folder for guest features / collaborative albums
            download_full_dir = not self.singles_only and not is_loose_dump(dir_name)
            dir_files = self._get_or_fetch_full_directory(user, dir_name) if download_full_dir else []

            # Safety check: avoid directories with > 80 files
            if dir_files and len(dir_files) > 80:
                dir_files = []

            if not dir_files:
                full_fn = match.get("full_filename")
                files_to_enqueue = [{"filename": full_fn, "size": match.get("size", 0)}] if full_fn and full_fn not in queued_files_set else []
            else:
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
                    self.queued_directories.append({
                        "type": "Featured / Collab Album" if len(files_to_enqueue) > 1 else "Standalone Track",
                        "title": item["track_title"],
                        "user": user,
                        "directory": match["directory"],
                        "format": match["format_label"],
                        "files_count": len(files_to_enqueue),
                        "total_size": sum(f["size"] for f in files_to_enqueue),
                        "status": "Enqueued"
                    })
                except Exception as e:
                    console.print(f"[red]Failed to enqueue standalone item '{item['track_title']}' from {user}: {e}[/red]")

    def _print_summary(self):
        """Displays rich formatted summary tables of all verified releases and queued downloads."""
        if self.verified_releases or self.verified_compilation_tracks or self.verified_standalone_tracks:
            table = Table(
                title=f"Soulseek / slskd Verified Releases & Tracks for {self.catalog.name}",
                box=box.ROUNDED,
                header_style="bold cyan"
            )
            table.add_column("#", style="dim", justify="right", width=4)
            table.add_column("Type", style="bold magenta", width=14)
            table.add_column("Release / Track Title", style="bold white")
            table.add_column("Format", style="green", width=18)
            table.add_column("Peer", style="cyan", width=14)
            table.add_column("Match Score", justify="center", width=12)
            table.add_column("Remote Directory", style="dim")

            idx = 1
            for r in self.verified_releases:
                bm = r["best_match"]
                match_pct = f"{int(bm['match_ratio'] * 100)}%"
                table.add_row(
                    str(idx),
                    r["release_type"],
                    r["release_title"],
                    bm["format_label"],
                    bm["user"],
                    f"[green]{match_pct}[/green]",
                    bm["directory"]
                )
                idx += 1

            for c in self.verified_compilation_tracks:
                bm = c["best_match"]
                table.add_row(
                    str(idx),
                    "Compilation",
                    f"{c['release_title']}\n[dim]↳ Track: {c['track_title']}[/dim]",
                    bm["format_label"],
                    bm["user"],
                    "[green]100%[/green]",
                    bm["directory"]
                )
                idx += 1

            for s in self.verified_standalone_tracks:
                bm = s["best_match"]
                table.add_row(
                    str(idx),
                    "Standalone",
                    f"{s['track_title']}\n[dim]↳ Credit: {s['artist_credit']}[/dim]",
                    bm["format_label"],
                    bm["user"],
                    "[green]100%[/green]",
                    bm["directory"]
                )
                idx += 1

            console.print("\n", table)

        if self.queued_directories:
            q_table = Table(
                title="slskd Download Queue Summary",
                box=box.ROUNDED,
                header_style="bold green"
            )
            q_table.add_column("#", style="dim", justify="right", width=4)
            q_table.add_column("Category", style="magenta", width=22)
            q_table.add_column("Release Title", style="white")
            q_table.add_column("Peer", style="cyan", width=14)
            q_table.add_column("Files", justify="right", width=8)
            q_table.add_column("Size", justify="right", width=12)
            q_table.add_column("Status", style="bold green", width=10)

            for i, q in enumerate(self.queued_directories, 1):
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

        tot_verified = len(self.verified_releases) + len(self.verified_compilation_tracks) + len(self.verified_standalone_tracks)
        tot_unresolved = len(self.unresolved_releases) + len(self.unresolved_compilation_tracks) + len(self.unresolved_standalone_tracks)
        console.print(Panel.fit(
            f"[bold green]✔ Discography Reconciliation Complete[/bold green]\n"
            f"• Verified on Soulseek: [bold cyan]{tot_verified}[/bold cyan] releases/tracks\n"
            f"• Queued in slskd: [bold green]{len(self.queued_directories)}[/bold green] album directories / files\n"
            f"• Unresolved on Soulseek: [yellow]{tot_unresolved}[/yellow] items",
            border_style="green"
        ))


def main():
    parser = argparse.ArgumentParser(
        description="Soulseek / slskd Artist Discography Scraper & Tracklist Reconciler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 slskd_scraper.py "kyou1110"
  python3 slskd_scraper.py "kyou1110" --dry-run
  python3 slskd_scraper.py "kyou1110" --singles-only
  python3 slskd_scraper.py "Stellabee" --format flac --min-match 0.8
        """
    )
    parser.add_argument("artist", help="Artist Name, MBID UUID, or MusicBrainz URL")
    parser.add_argument("-d", "--music-dir", default=None, help="Local music library directory to scan (READ-ONLY)")
    parser.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320", "any"], help="Preferred audio format")
    parser.add_argument("--min-match", type=float, default=0.70, help="Minimum track match ratio for albums (default: 0.70)")
    parser.add_argument("--timeout", type=float, default=8.0, help="Soulseek search timeout in seconds (default: 8)")
    parser.add_argument("--dry-run", action="store_true", help="Discover and verify matches without enqueuing downloads")
    parser.add_argument("--singles-only", action="store_true", default=False, help="Only download single matching tracks for compilations/features instead of full releases")
    parser.add_argument("-t", "--threads", type=int, default=4, help="Worker threads for local scanning")

    args = parser.parse_args()

    scraper = SlskdArtistScraper(
        artist_query=args.artist,
        music_dir=args.music_dir,
        preferred_format=args.format,
        min_match_ratio=args.min_match,
        search_timeout=args.timeout,
        dry_run=args.dry_run,
        singles_only=args.singles_only,
        threads=args.threads
    )

    try:
        scraper.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation aborted by user.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
