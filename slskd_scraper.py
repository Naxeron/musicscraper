#!/usr/bin/env python3
"""
Soulseek / slskd Artist Discography Scraper & Tracklist Reconciler
=================================================================
Automates the search, tracklist verification, and directory download queueing
for an artist's full discography on Soulseek via slskd.

Features:
1. Queries MusicBrainz to build the complete artist catalog (primary releases, EPs,
   split releases, compilations, alter-egos, and standalone recordings).
2. Pre-scans the local/server music library (Read-Only) to detect existing tracks.
3. Multi-tier parallel batch searching across the Soulseek network via slskd REST API.
4. Fast in-memory candidate reconciliation across all discovered peer files.
5. Selective remote peer directory expansion (discovers complete album contents,
   bonus tracks, artwork, cue sheets, and logs for top candidates).
6. Comprehensive tracklist reconciler with diacritics normalization (Polish/Unicode),
   alter-ego matching, multi-artist split handling, and subtitle variants.
7. Prioritizes lossless (FLAC 24-bit/16-bit) and MP3-320 with low peer queues.
8. Automatically queues complete verified directories in slskd.
9. Generates rich terminal reports and exports audit summaries.
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
    calculate_similarity,
    DEFAULT_CACHE_DIR,
    AUDIO_EXTENSIONS,
)

console = Console()

SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav",
    ".aif", ".aiff", ".wma", ".ape", ".wv", ".dsf", ".dff"
}

SUPPORTING_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".cue", ".log", ".nfo", ".txt", ".m3u", ".m3u8"
}

POLISH_DIACRITICS_MAP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


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


def normalize_track_title(text: Optional[str]) -> str:
    """
    Comprehensive string normalization:
    - Katakana to Hiragana conversion
    - Polish & European diacritic translation before unidecode
    - Transliterates unicode to ASCII
    - Splits number-letter boundaries
    - Strips punctuation and symbols
    """
    if not text:
        return ""
    norm = katakana_to_hiragana(text.lower())
    norm = norm.translate(POLISH_DIACRITICS_MAP)
    norm = unidecode(norm)
    norm = re.sub(r"([a-zA-Z])([0-9])", r"\1 \2", norm)
    norm = re.sub(r"([0-9])([a-zA-Z])", r"\1 \2", norm)
    norm = re.sub(r"[\-_:\(\)\[\]\{\}\.\,\+\?\!\~\"\'\/\\\|\@\#\$\%\^\&\*\=\`\<\>\;]", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def clean_tokens(text: str) -> str:
    """Strips all punctuation and whitespace for compressed token comparison."""
    if not text:
        return ""
    norm = normalize_track_title(text)
    return re.sub(r"[^a-z0-9]", "", norm)


def tokenize_words(text: str) -> List[str]:
    """Extracts lowercase alphanumeric words with unidecode transliteration and number splitting."""
    if not text:
        return []
    norm = normalize_track_title(text)
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


def extract_catalog_codes(text: str) -> List[str]:
    """Extracts netlabel release / catalog codes (e.g. SKRD-074, MISO-001, TANOCD-0013, OTMN070)."""
    if not text:
        return []
    patterns = [
        r"\b([A-Za-z0-9!]{2,10}[-_]\d{2,6})\b",
        r"\b([A-Za-z]{2,6}\d{2,5})\b",
        r"\[([A-Za-z0-9!#\-_ ]{3,15})\]",
    ]
    codes = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            code = m.group(1).strip()
            if re.match(r"^(mp3|flac|wav|cda|web|vol|cd\d|disc\d|\d{4})", code, re.IGNORECASE):
                continue
            if len(code) >= 4:
                codes.append(code)
    return list(dict.fromkeys(codes))


def is_track_title_match(
    exp_title: str,
    candidate_filename: str,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = ""
) -> bool:
    """
    Robustly verifies if candidate_filename matches expected track title:
    - Uses whole-word token sub-sequence matching.
    - Tests compressed tokens (handles symbols like F>B>D, $S$S$, むげん☆ういんぐ).
    - Checks fuzzy similarity for minor tagging variances.
    - Handles subtitle / remix differences.
    - For short titles (<= 3 chars or 1 common word), validates directory or artist context.
    """
    exp_words = tokenize_words(exp_title)
    if not exp_words:
        return False

    clean_exp_title = strip_track_number_and_artist(exp_title)
    clean_exp_words = tokenize_words(clean_exp_title) or exp_words

    file_words = tokenize_words(candidate_filename)
    clean_file_words = tokenize_words(strip_track_number_and_artist(candidate_filename)) or file_words

    # 1. Direct token sequence match
    exact_match = (
        is_sublist(exp_words, file_words) or
        is_sublist(clean_exp_words, clean_file_words) or
        is_sublist(clean_exp_words, file_words) or
        is_sublist(exp_words, clean_file_words)
    )

    # 2. Check concatenated tokens
    exp_concat = "".join(clean_exp_words)
    file_concat = "".join(clean_file_words)
    if not exact_match and len(exp_concat) >= 3:
        if exp_concat in file_concat or file_concat in exp_concat:
            exact_match = True

    # 3. Fuzzy similarity fallback
    if not exact_match and len(exp_concat) >= 4:
        norm_exp = normalize_track_title(clean_exp_title)
        norm_file = normalize_track_title(strip_track_number_and_artist(candidate_filename))
        sim = calculate_similarity(norm_exp, norm_file)
        if sim >= 0.82:
            exact_match = True

    if not exact_match:
        return False

    # 4. Contextual validation for very short titles
    if len(clean_exp_words) <= 1 or len(exp_concat) <= 3:
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


def sanitize_remote_path(path_str: str) -> str:
    """Normalizes slashes for Soulseek paths."""
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
    Evaluates audio format, bit depth, sample rate, and bitrate from slskd file attributes.
    Returns (format_label, quality_score).
    """
    fn = file_item.get("filename", "") or file_item.get("base_filename", "")
    ext = Path(fn).suffix.lower()
    bit_rate = file_item.get("bitRate", 0)
    bit_depth = file_item.get("bitDepth", 0)
    sample_rate = file_item.get("sampleRate", 0)

    if ext in (".flac", ".wav", ".aif", ".aiff", ".ape", ".wv", ".dsf", ".dff"):
        if bit_depth and bit_depth > 16:
            return f"FLAC {bit_depth}-bit/{sample_rate or 44100}Hz", 115
        return "FLAC (Lossless)", 100

    if ext in (".mp3", ".m4a", ".ogg", ".opus", ".mp4", ".wma"):
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


def clean_search_phrase(text: str, max_words: int = 6) -> str:
    """Cleans punctuation and prepares a search phrase."""
    raw = re.sub(r"[\-_:\(\)\[\]\{\}\.\,\+\?\!\~\"\'\/\\\|\@\#\$\%\^\&\*\=\`\<\>\;]+", " ", text)
    words = [w for w in raw.split() if w.lower() not in ("va", "various", "artists", "feat", "ft", "compilation")]
    return " ".join(words[:max_words]).strip()


class SlskdArtistScraper:
    """Orchestrates parallel Soulseek discovery, directory expansion, reconciliation, and download queueing."""

    def __init__(
        self,
        artist_query: str,
        slskd_client: Optional[SlskdClient] = None,
        music_dir: Optional[str] = None,
        preferred_format: str = "flac",
        min_match_ratio: float = 0.70,
        search_timeout: float = 14.0,
        dry_run: bool = False,
        singles_only: bool = False,
        cache_dir: str = DEFAULT_CACHE_DIR,
        threads: int = 6,
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

        # Auto-detect local/server music library
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
        self.all_artist_aliases: Set[str] = set()

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

        # Collect all artist aliases, sort names, alter-egos, and romanized variations
        self.all_artist_aliases = set(self.catalog.aliases)
        self.all_artist_aliases.add(self.catalog.name)
        for a in list(self.all_artist_aliases):
            self.all_artist_aliases.add(unidecode(a))
            self.all_artist_aliases.add(a.translate(POLISH_DIACRITICS_MAP))

        # Extract alter-ego / collaborator names from track artist credits (e.g. soniacz, sonnie, SyndraSound)
        for t in self.catalog.tracks:
            ac = t.get("artist_credit", "")
            if ac:
                for part in re.split(r"(?:feat\.?|featuring|vs\.?|×|&|-|\+)", ac, flags=re.IGNORECASE):
                    clean_p = part.strip()
                    if clean_p and len(clean_p) >= 3 and len(clean_p) <= 25:
                        self.all_artist_aliases.add(clean_p)

        console.print(f"[green]✔ Canonical Name:[/green] [bold]{self.catalog.name}[/bold] (MBID: {self.catalog.mbid})")
        primary_rels = self.catalog.releases
        comp_tracks = self.raw_mb_data.get("releases_track_artist", [])
        console.print(f"[dim]Catalog: {len(self.catalog.tracks)} total tracks | {len(primary_rels)} primary releases | {len(comp_tracks)} compilation/VA releases[/dim]")

        # 3. Pre-Scan Local Music Library (Read-Only)
        self._prescan_library()

        # 4. Multi-Tier Parallel Batch Search on Soulseek
        self._discover_soulseek_candidates()

        # 5. Verify & Reconcile Primary Releases
        self._reconcile_primary_releases()

        # 6. Verify & Reconcile Compilation / VA Tracks
        self._reconcile_compilation_tracks()

        # 7. Verify & Reconcile Standalone & Non-Album Tracks
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

    def _generate_all_search_queries(self) -> List[str]:
        """Generates comprehensive multi-tier search queries across the artist catalog."""
        queries: List[str] = []

        # Tier 1: Artist & Primary Aliases
        queries.append(self.catalog.name)
        for alias in self.catalog.aliases:
            if alias.lower() != self.catalog.name.lower() and len(alias) >= 3:
                queries.append(alias)

        # Tier 2: Primary Releases
        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            if not rel_title:
                continue

            # Artist + Release
            q1 = clean_search_phrase(f"{self.catalog.name} {rel_title}")
            if q1:
                queries.append(q1)

            # Standalone Release Title
            q2 = clean_search_phrase(rel_title)
            if q2 and len(q2) >= 4 and q2.lower() != self.catalog.name.lower():
                queries.append(q2)

            # Subtitle split (e.g. 'Polska Ja Wersyja' from 'Polska Ja Wersyja: Polcore To Stan Umysłu')
            prefix = re.split(r"[-~:]", rel_title)[0].strip()
            if prefix and prefix != rel_title and len(prefix) >= 4:
                queries.append(clean_search_phrase(f"{self.catalog.name} {prefix}"))
                queries.append(clean_search_phrase(prefix))

            # Diacritic variants (e.g. Dlonie vs Dłonie)
            unidecode_title = unidecode(rel_title)
            if unidecode_title != rel_title:
                queries.append(clean_search_phrase(f"{self.catalog.name} {unidecode_title}"))
                queries.append(clean_search_phrase(unidecode_title))

            # Extract catalog codes
            for code in extract_catalog_codes(rel_title):
                queries.append(code)

        # Tier 3: Compilations / Split Releases
        comp_releases = self.raw_mb_data.get("releases_track_artist", [])
        for rel in comp_releases:
            rel_title = rel.get("title", "")
            if not rel_title:
                continue

            clean_t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", rel_title)
            clean_t = re.sub(r"^(?:re:\s*|v\.a\.?\s*|various\s*artists\s*)", "", clean_t, flags=re.IGNORECASE).strip()
            q = clean_search_phrase(clean_t)
            if q and len(q) >= 4:
                queries.append(q)

            prefix = re.split(r"[-~:]", rel_title)[0].strip()
            if prefix and len(prefix) >= 4:
                queries.append(clean_search_phrase(prefix))

            queries.append(clean_search_phrase(f"{self.catalog.name} {clean_t}"))

            for code in extract_catalog_codes(rel_title):
                queries.append(code)

        # Tier 4: Specific Missing Tracks & Collaborations
        for t in self.catalog.tracks:
            t_title = t.get("title", "")
            norm_t = t.get("norm_title", "")
            if not t_title or norm_t in self.local_found_map:
                continue

            clean_t = strip_track_number_and_artist(t_title)
            clean_t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", clean_t).strip()
            if not clean_t:
                continue

            if len(clean_t) >= 4:
                queries.append(clean_search_phrase(f"{self.catalog.name} {clean_t}"))
                queries.append(clean_search_phrase(clean_t))

            ac = t.get("artist_credit", "")
            if ac:
                for part in re.split(r"(?:feat\.?|featuring|vs\.?|×|&|-|\+)", ac, flags=re.IGNORECASE):
                    cp = part.strip()
                    if cp and cp.lower() != self.catalog.name.lower() and len(cp) >= 3:
                        queries.append(clean_search_phrase(f"{cp} {clean_t}"))

        return list(dict.fromkeys(q for q in queries if q and len(q) >= 3))

    def _discover_soulseek_candidates(self):
        """Runs parallel batch searches across Soulseek for all catalog items."""
        all_queries = self._generate_all_search_queries()
        console.print(f"\n[cyan]Executing parallel Soulseek searches ({len(all_queries)} targeted queries)...[/cyan]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Searching Soulseek P2P network...", total=len(all_queries))

            batch_results = self.client.batch_search(
                all_queries,
                timeout=self.search_timeout,
                poll_interval=1.0,
                max_concurrent=8,
                use_existing=True
            )

            progress.update(task, completed=len(all_queries), description="Processing Soulseek peer responses...")

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

    def _reconcile_primary_releases(self):
        """Verifies candidate directories against all primary releases from MusicBrainz."""
        console.print(f"\n[bold cyan]Verifying Primary Releases ({len(self.catalog.releases)} releases)...[/bold cyan]")

        # Index peer directories by clean path tokens
        dir_token_map: Dict[Tuple[str, str], str] = {
            (user, d): clean_tokens(d)
            for (user, d) in self.peer_directories.keys()
        }

        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            token_rel = clean_tokens(rel_title)

            if norm_rel in self.local_found_releases:
                continue

            expected_tracks = [
                t for t in self.catalog.tracks
                if t.get("norm_release") == norm_rel or rel_title in t.get("all_releases", set())
            ]
            if not expected_tracks:
                continue

            candidate_matches: List[Dict[str, Any]] = []

            # 1. Fast in-memory evaluation against candidate directories
            for (user, dir_name), dir_tokens in dir_token_map.items():
                is_candidate = (
                    token_rel and (token_rel in dir_tokens or any(len(w) >= 4 and w in dir_tokens for w in token_rel.split()))
                )
                if not is_candidate:
                    dir_files = self.peer_directories[(user, dir_name)].get("matched_search_files", [])
                    matched_cnt = sum(
                        1 for exp in expected_tracks
                        if any(is_track_title_match(exp.get("title", ""), extract_dir_and_filename(sf.get("filename", ""))[1], self.all_artist_aliases, rel_title, dir_name) for sf in dir_files)
                    )
                    if matched_cnt >= 2 or (len(expected_tracks) <= 2 and matched_cnt >= 1):
                        is_candidate = True

                if is_candidate:
                    dir_info = self.peer_directories.get((user, dir_name), {})
                    match_data = self._evaluate_directory_in_memory(user, dir_name, dir_info, expected_tracks, rel_title)
                    if match_data:
                        candidate_matches.append(match_data)

            if candidate_matches:
                candidate_matches.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = candidate_matches[0]

                # If best candidate is missing full directory files, fetch full folder listing for top candidate
                if best_match.get("match_ratio", 0) >= 0.50:
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
                                # Re-evaluate with complete files
                                updated_match = self._evaluate_directory_in_memory(top_user, top_dir, top_dir_info, expected_tracks, rel_title)
                                if updated_match:
                                    best_match = updated_match
                        except Exception:
                            pass

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

    def _evaluate_directory_in_memory(
        self,
        user: str,
        dir_name: str,
        dir_info: Dict[str, Any],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
        """Fast in-memory comparison of directory files against expected release tracklist."""
        dir_files = dir_info.get("full_directory_files") or dir_info.get("matched_search_files", [])
        if not dir_files:
            return None

        audio_files = [f for f in dir_files if is_audio_file(f.get("base_filename") or extract_dir_and_filename(f.get("filename", ""))[1])]
        if not audio_files:
            return None

        matched_tracks: List[Dict[str, Any]] = []
        unmatched_expected: List[Dict[str, Any]] = []

        for exp in expected_tracks:
            exp_title = exp.get("title", "")
            matched_file = None

            for f in audio_files:
                base = f.get("base_filename") or extract_dir_and_filename(f.get("filename", ""))[1]
                if is_track_title_match(exp_title, base, self.all_artist_aliases, rel_title, dir_name):
                    matched_file = f
                    break

            if matched_file:
                matched_tracks.append({
                    "expected": exp_title,
                    "matched_file": matched_file.get("base_filename") or extract_dir_and_filename(matched_file.get("filename", ""))[1],
                    "full_filename": matched_file.get("full_filename") or matched_file.get("filename"),
                    "size": matched_file.get("size", 0)
                })
            else:
                unmatched_expected.append(exp)

        match_ratio = len(matched_tracks) / len(expected_tracks) if expected_tracks else 0.0

        if match_ratio < self.min_match_ratio and len(matched_tracks) == 0:
            return None

        has_bonus = len(audio_files) > len(expected_tracks) and match_ratio >= 0.70

        primary_audio = audio_files[0]
        format_label, format_score = determine_audio_quality(primary_audio)

        queue = dir_info.get("queue", 0)
        speed = dir_info.get("speed", 0)
        has_slot = dir_info.get("has_slot", True)
        has_artwork = any(is_supporting_file(f.get("base_filename") or extract_dir_and_filename(f.get("filename", ""))[1]) for f in dir_files)

        queue_penalty = min(queue / 2, 40)
        speed_bonus = min(speed / 500_000, 20)
        slot_bonus = 20 if has_slot else 0
        art_bonus = 10 if has_artwork else 0
        bonus_ver_bonus = 15 if has_bonus else 0

        format_weight = format_score
        if self.preferred_format == "flac" and "FLAC" in format_label:
            format_weight += 25

        total_score = (match_ratio * 100) + format_weight + slot_bonus + speed_bonus + art_bonus + bonus_ver_bonus - queue_penalty

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
            "has_artwork": has_artwork,
            "dir_info": dir_info,
            "all_dir_files": dir_files
        }

    def _reconcile_compilation_tracks(self):
        """Verifies candidate tracks on compilation / VA releases."""
        comp_releases = self.raw_mb_data.get("releases_track_artist", [])
        console.print(f"\n[bold cyan]Verifying Compilation & Split Tracks ({len(comp_releases)} releases)...[/bold cyan]")

        all_discovered_audio_files: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        for (user, dir_name), dir_info in self.peer_directories.items():
            dir_files = dir_info.get("full_directory_files") or dir_info.get("matched_search_files", [])
            for sf in dir_files:
                fn = sf.get("full_filename") or sf.get("filename", "")
                base = sf.get("base_filename") or extract_dir_and_filename(fn)[1]
                if is_audio_file(base):
                    all_discovered_audio_files.append((user, dir_name, sf, dir_info))

        for rel in comp_releases:
            rel_title = rel.get("title", "")
            artist_credit = rel.get("artist-credit-phrase", "Various Artists")
            norm_rel = normalize_text(rel_title)

            target_tracks = []
            for m in rel.get("medium-list", []):
                for t in m.get("track-list", []):
                    rec = t.get("recording", {})
                    rec_artists = [ac.get("artist", {}).get("name", "").lower() for ac in rec.get("artist-credit", []) if isinstance(ac, dict)]
                    t_title = rec.get("title", t.get("title", ""))
                    if any(a.lower() in " ".join(rec_artists) or a.lower() in t_title.lower() for a in self.all_artist_aliases):
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

            for tt in missing_tracks:
                t_title = tt.get("title", "")
                norm_t = tt.get("norm_title", "")
                candidate_matches: List[Dict[str, Any]] = []

                for user, dir_name, sf, dir_info in all_discovered_audio_files:
                    fn = sf.get("full_filename") or sf.get("filename", "")
                    base = sf.get("base_filename") or extract_dir_and_filename(fn)[1]

                    if is_track_title_match(t_title, base, self.all_artist_aliases, rel_title, dir_name):
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
                            "total_score": fmt_score + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue / 2, 40),
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
            dir_files = dir_info.get("full_directory_files") or dir_info.get("matched_search_files", [])
            for sf in dir_files:
                fn = sf.get("full_filename") or sf.get("filename", "")
                base = sf.get("base_filename") or extract_dir_and_filename(fn)[1]
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
                fn = sf.get("full_filename") or sf.get("filename", "")
                base = sf.get("base_filename") or extract_dir_and_filename(fn)[1]
                if is_track_title_match(t_title, base, self.all_artist_aliases, t.get("release_title", ""), dir_name):
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
                        "total_score": fmt_score + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue / 2, 40),
                        "dir_info": dir_info
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

        def is_loose_dump(d_path: str) -> bool:
            parts = [p.strip().lower() for p in sanitize_remote_path(d_path).split("\\") if p.strip()]
            last = parts[-1] if parts else ""
            return any(k in last for k in ("soundcloud singles", "loose tracks", "random singles", "singles", "various singles"))

        # 1. Queue Primary Release Directories (Full Folders)
        for item in self.verified_releases:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            dir_files = match.get("all_dir_files") or match.get("dir_info", {}).get("matched_search_files", [])

            # Expand if full folder listing was not fetched
            if not any(is_supporting_file(f.get("base_filename") or extract_dir_and_filename(f.get("filename", ""))[1]) for f in dir_files):
                try:
                    nodes = self.client.browse_directory(user, dir_name)
                    if nodes:
                        files = []
                        for node in nodes:
                            node_name = node.get("name", dir_name)
                            for f in node.get("files", []):
                                fn = f.get("filename", "")
                                full_fn = fn if ("\\" in fn or "/" in fn) else f"{node_name}\\{fn}"
                                f_copy = dict(f)
                                f_copy["full_filename"] = full_fn
                                f_copy["base_filename"] = fn
                                files.append(f_copy)
                        if files:
                            dir_files = files
                except Exception:
                    pass

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

            download_full_dir = not self.singles_only and not is_loose_dump(dir_name)
            dir_files = match.get("dir_info", {}).get("full_directory_files") or match.get("dir_info", {}).get("matched_search_files", []) if download_full_dir else []

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

        # 3. Queue Standalone / Guest Feature Track Releases
        for item in self.verified_standalone_tracks:
            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            full_fn = match.get("full_filename")
            files_to_enqueue = [{"filename": full_fn, "size": match.get("size", 0)}] if full_fn and full_fn not in queued_files_set else []

            if files_to_enqueue:
                try:
                    self.client.enqueue_download(user, files_to_enqueue)
                    queued_dirs_set.add(dir_key)
                    self.queued_directories.append({
                        "type": "Standalone Track",
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
  python3 slskd_scraper.py "Mekuso"
  python3 slskd_scraper.py "Mekuso" --dry-run
  python3 slskd_scraper.py "Mekuso" --singles-only
  python3 slskd_scraper.py "Stellabee" --format flac --min-match 0.8
        """
    )
    parser.add_argument("artist", help="Artist Name, MBID UUID, or MusicBrainz URL")
    parser.add_argument("-d", "--music-dir", default=None, help="Local music library directory to scan (READ-ONLY)")
    parser.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320", "any"], help="Preferred audio format")
    parser.add_argument("--min-match", type=float, default=0.70, help="Minimum track match ratio for albums (default: 0.70)")
    parser.add_argument("--timeout", type=float, default=14.0, help="Soulseek search timeout in seconds (default: 14)")
    parser.add_argument("--dry-run", action="store_true", help="Discover and verify matches without enqueuing downloads")
    parser.add_argument("--singles-only", action="store_true", default=False, help="Only download single matching tracks for compilations/features instead of full releases")
    parser.add_argument("-t", "--threads", type=int, default=6, help="Worker threads for local scanning and browsing")

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
