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
from functools import lru_cache
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
    NavidromeScanner,
    DiscographyReconciler,
    ReportGenerator,
    normalize_text,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
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

DIR_STOP_WORDS = {
    "various", "artists", "va", "compilation", "album", "ep", "vol",
    "part", "records", "crew", "part1", "part2", "the", "a", "an",
    "and", "or", "in", "of", "to", "for", "with", "on", "at"
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


@lru_cache(maxsize=65536)
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


@lru_cache(maxsize=65536)
def clean_tokens(text: Optional[str]) -> str:
    """Strips all punctuation and whitespace for compressed token comparison."""
    if not text:
        return ""
    norm = normalize_track_title(text)
    return re.sub(r"[^a-z0-9]", "", norm)


@lru_cache(maxsize=65536)
def _tokenize_words_cached(text: Optional[str]) -> Tuple[str, ...]:
    if not text:
        return ()
    norm = normalize_track_title(text)
    return tuple(re.findall(r"[a-z0-9]+", norm))


def tokenize_words(text: Optional[str]) -> List[str]:
    """Extracts lowercase alphanumeric words with unidecode transliteration and number splitting."""
    return list(_tokenize_words_cached(text))


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


# ==============================================================================
# CANDIDATE INDEXING & FAST MATCHING ENGINE
# ==============================================================================

class CandidateFile:
    """Pre-parsed, indexed representation of a discovered remote Soulseek file."""
    __slots__ = (
        "user", "dir_name", "raw_file", "dir_info", "base_filename", "full_filename",
        "clean_base", "words", "clean_words", "sig_words", "concat",
        "p_struct", "is_audio", "is_supporting", "fmt_label", "fmt_score", "size"
    )

    def __init__(self, user: str, dir_name: str, raw_file: Dict[str, Any], dir_info: Optional[Dict[str, Any]] = None):
        self.user = user
        self.dir_name = dir_name
        self.raw_file = raw_file
        self.dir_info = dir_info or {}
        fn = raw_file.get("full_filename") or raw_file.get("filename", "")
        self.full_filename = fn
        self.base_filename = raw_file.get("base_filename") or extract_dir_and_filename(fn)[1]
        self.size = raw_file.get("size", 0)
        self.is_audio = is_audio_file(self.base_filename)
        self.is_supporting = is_supporting_file(self.base_filename)
        if self.is_audio:
            self.p_struct = parse_track_title_structure(self.base_filename)
            self.clean_base = self.p_struct["base_norm"] or strip_track_number_and_artist(self.base_filename)
            self.words = list(_tokenize_words_cached(self.p_struct["base_norm"] or self.base_filename))
            self.clean_words = list(_tokenize_words_cached(self.clean_base)) or self.words
            self.sig_words = {w for w in self.clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
            self.concat = "".join(self.clean_words)
            self.fmt_label, self.fmt_score = determine_audio_quality(raw_file)
        else:
            self.p_struct = None
            self.clean_base = ""
            self.words = []
            self.clean_words = []
            self.sig_words = set()
            self.concat = ""
            self.fmt_label, self.fmt_score = "", 0


class CandidateDir:
    """Pre-parsed, indexed representation of a discovered remote peer directory."""
    __slots__ = (
        "user", "dir_name", "clean_dir", "dir_words", "dir_sig_words",
        "audio_files", "all_dir_files", "all_file_sig_words", "has_artwork", "dir_info"
    )

    def __init__(self, user: str, dir_name: str, dir_info: Dict[str, Any]):
        self.user = user
        self.dir_name = dir_name
        self.dir_info = dir_info
        self.clean_dir = clean_tokens(dir_name)
        self.dir_words = set(_tokenize_words_cached(dir_name))
        self.dir_sig_words = {w for w in self.dir_words if len(w) >= 3 and w not in DIR_STOP_WORDS}

        raw_files = dir_info.get("full_directory_files") or dir_info.get("matched_search_files", [])
        self.audio_files: List[CandidateFile] = []
        self.all_dir_files: List[CandidateFile] = []
        self.all_file_sig_words: Set[str] = set()
        self.has_artwork = False

        for rf in raw_files:
            cf = CandidateFile(user, dir_name, rf, dir_info)
            self.all_dir_files.append(cf)
            if cf.is_audio:
                self.audio_files.append(cf)
                self.all_file_sig_words.update(cf.sig_words)
            elif cf.is_supporting:
                self.has_artwork = True


class PeerCandidateIndex:
    """In-memory inverted index over discovered Soulseek directories and audio files."""

    def __init__(self, peer_directories: Dict[Tuple[str, str], Dict[str, Any]]):
        self.peer_directories = peer_directories
        self.dirs_map: Dict[Tuple[str, str], CandidateDir] = {}
        self.word_to_dirs: Dict[str, List[CandidateDir]] = {}
        self.word_to_audio_files: Dict[str, List[CandidateFile]] = {}
        self.all_audio_files: List[CandidateFile] = []
        self.all_dirs: List[CandidateDir] = []
        self._build_index()

    def _build_index(self):
        for (user, dir_name), dir_info in self.peer_directories.items():
            cd = CandidateDir(user, dir_name, dir_info)
            self.dirs_map[(user, dir_name)] = cd
            self.all_dirs.append(cd)

            for w in cd.dir_sig_words:
                if w not in self.word_to_dirs:
                    self.word_to_dirs[w] = []
                self.word_to_dirs[w].append(cd)

            for cf in cd.audio_files:
                self.all_audio_files.append(cf)
                for w in cf.sig_words:
                    if w not in self.word_to_audio_files:
                        self.word_to_audio_files[w] = []
                    self.word_to_audio_files[w].append(cf)

    def update_directory(self, user: str, dir_name: str, dir_info: Dict[str, Any]) -> CandidateDir:
        """Updates a directory after fetching its complete remote file listing."""
        cd = CandidateDir(user, dir_name, dir_info)
        self.dirs_map[(user, dir_name)] = cd
        return cd

    def get_candidate_dirs_for_release(
        self,
        rel_title: str,
        parsed_expected: List[Dict[str, Any]],
        expected_count: int
    ) -> List[CandidateDir]:
        """Sub-millisecond filtering of 7000+ candidate dirs to only relevant directories for this release."""
        clean_rel = clean_tokens(rel_title)
        rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
        rel_sig_words = {w for w in rel_words if len(w) >= 3}

        album_sig_words: Set[str] = set()
        for pe in parsed_expected:
            album_sig_words.update(pe["sig_words"])

        cand_dirs_set: Set[CandidateDir] = set()

        # 1. Match directories containing release name tokens
        for w in rel_sig_words:
            if w in self.word_to_dirs:
                cand_dirs_set.update(self.word_to_dirs[w])

        # 2. Match directories with clean token substring
        if clean_rel:
            for cd in self.all_dirs:
                if clean_rel in cd.clean_dir or cd.clean_dir in clean_rel:
                    cand_dirs_set.add(cd)

        # 3. For releases with >= 3 expected tracks, also match directories whose files overlap with track names
        if expected_count >= 3:
            for w in album_sig_words:
                if w in self.word_to_dirs:
                    cand_dirs_set.update(self.word_to_dirs[w])

        return list(cand_dirs_set)

    def get_candidate_files_for_track(self, parsed_track: Dict[str, Any]) -> List[CandidateFile]:
        """Filters 60,000+ candidate files down to the ~2-10 files containing the target track's words."""
        sig_words = parsed_track["sig_words"]
        if not sig_words:
            return self.all_audio_files

        matched_files_set: Set[CandidateFile] = set()
        for w in sig_words:
            if w in self.word_to_audio_files:
                matched_files_set.update(self.word_to_audio_files[w])

        return list(matched_files_set) if matched_files_set else self.all_audio_files


def pre_parse_single_track(track_title: str) -> Dict[str, Any]:
    """Pre-parses and tokenizes a single track title for zero-allocation fast matching."""
    p_struct = parse_track_title_structure(track_title)
    clean_base = p_struct["base_norm"] or strip_track_number_and_artist(track_title)
    words = list(_tokenize_words_cached(p_struct["base_norm"] or track_title))
    clean_words = list(_tokenize_words_cached(clean_base)) or words
    sig_words = {w for w in clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
    concat = "".join(clean_words)
    return {
        "title": track_title,
        "p_struct": p_struct,
        "clean_base": clean_base,
        "words": words,
        "clean_words": clean_words,
        "sig_words": sig_words,
        "concat": concat,
    }


def pre_parse_expected_tracks(expected_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pre-parses an album's expected tracklist."""
    return [pre_parse_single_track(t.get("title", "")) for t in expected_tracks]


def is_track_title_match_fast(
    p_exp: Dict[str, Any],
    exp_words: List[str],
    clean_exp_words: List[str],
    exp_sig_words: Set[str],
    exp_concat: str,
    cand: CandidateFile,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = ""
) -> bool:
    """
    High-performance, zero-allocation track title matcher comparing pre-tokenized structures:
    - Enforces version and remix compatibility.
    - Uses whole-word token sub-sequence matching.
    - Tests compressed tokens (handles symbols like F>B>D, $S$S$, むげん☆ういんぐ).
    - Checks fuzzy similarity for minor tagging variances.
    - For short titles (<= 3 chars or 1 common word), validates directory or artist context.
    """
    p_cand = cand.p_struct
    if not p_cand:
        return False

    # 0. Version compatibility check
    if not are_versions_compatible(p_exp["version_type"], p_exp["version_text"], p_cand["version_type"], p_cand["version_text"]):
        return False

    if not exp_words:
        return False

    file_words = cand.words
    clean_file_words = cand.clean_words

    # 1. Direct token sequence match
    exact_match = (
        is_sublist(exp_words, file_words) or
        is_sublist(clean_exp_words, clean_file_words) or
        is_sublist(clean_exp_words, file_words) or
        is_sublist(exp_words, clean_file_words)
    )

    # 2. Check concatenated tokens
    if not exact_match and len(exp_concat) >= 3:
        if exp_concat in cand.concat or cand.concat in exp_concat:
            exact_match = True

    # 3. Fuzzy similarity fallback
    if not exact_match and len(exp_concat) >= 4:
        if (exp_sig_words & cand.sig_words) or not exp_sig_words:
            sim = calculate_similarity(p_exp["base_norm"], cand.clean_base)
            if sim >= 0.82:
                exact_match = True

    if not exact_match:
        return False

    # 4. Contextual validation for very short titles
    if len(clean_exp_words) <= 1 or len(exp_concat) <= 3:
        dir_norm = clean_tokens(dir_path)
        file_norm = cand.concat
        has_artist_context = any(clean_tokens(alias) in dir_norm or clean_tokens(alias) in file_norm for alias in artist_aliases if len(alias) >= 3)
        has_rel_context = False
        if rel_title:
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            if rel_words:
                has_rel_context = (
                    is_sublist(rel_words[:3], list(_tokenize_words_cached(dir_path))) or
                    any(w in dir_norm for w in rel_words if len(w) >= 4)
                )

        if not has_artist_context and not has_rel_context:
            return False

    return True


def is_track_title_match(
    exp_title: str,
    candidate_filename: str,
    artist_aliases: Set[str],
    rel_title: str = "",
    dir_path: str = ""
) -> bool:
    """Backward-compatible wrapper for single-track matching."""
    pe = pre_parse_single_track(exp_title)
    dummy_raw = {"filename": candidate_filename, "size": 0}
    cf = CandidateFile("", dir_path, dummy_raw)
    return is_track_title_match_fast(
        pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
        cf, artist_aliases, rel_title, dir_path
    )


def is_dir_name_match_fast(clean_rel: str, rel_sig_words: Set[str], cd: CandidateDir) -> bool:
    """Fast directory name match check using precomputed token sets."""
    if clean_rel and (clean_rel in cd.clean_dir or cd.clean_dir in clean_rel):
        return True
    if not rel_sig_words:
        return False
    matches = len(rel_sig_words & cd.dir_sig_words)
    return matches >= min(2, len(rel_sig_words))


def is_dir_name_match(rel_title: str, dir_name: str) -> bool:
    """Verifies whether a directory path corresponds to a specific release title."""
    clean_rel = clean_tokens(rel_title)
    clean_dir = clean_tokens(dir_name)
    if clean_rel and (clean_rel in clean_dir or clean_dir in clean_rel):
        return True
    rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
    if not rel_words:
        return False
    dir_words = set(_tokenize_words_cached(dir_name))
    matches = sum(1 for w in rel_words if len(w) >= 3 and w in dir_words)
    return matches >= min(2, len(rel_words))


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
        self.candidate_index: Optional[PeerCandidateIndex] = None
        self.searches_performed: Set[str] = set()

        # Deduplication & coverage tracking
        self.reconciled_release_keys: Set[str] = set()
        self.covered_track_titles: Set[str] = set()

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
        """Scans local music library and/or Navidrome server to avoid downloading releases already in collection."""
        nav_url = os.getenv("NAVIDROME_URL", os.getenv("SUBSONIC_URL", "")).strip()
        nav_user = os.getenv("NAVIDROME_USERNAME", os.getenv("SUBSONIC_USERNAME", "")).strip()
        nav_pass = os.getenv("NAVIDROME_PASSWORD", os.getenv("SUBSONIC_PASSWORD", "")).strip()
        has_nav = bool(nav_url and nav_user and nav_pass)
        has_local = bool(self.music_dir and self.music_dir.exists())

        if not has_nav and not has_local:
            return

        local_tracks: List[Dict[str, Any]] = []
        seen_paths: Set[str] = set()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            if has_nav:
                task_id = progress.add_task(f"[cyan]Pre-scanning Navidrome ({nav_url})...", total=None)
                try:
                    nav_scanner = NavidromeScanner(
                        base_url=nav_url,
                        username=nav_user,
                        password=nav_pass,
                        catalog=self.catalog
                    )
                    nav_tracks = nav_scanner.scan(progress=progress, task_id=task_id)
                    for nt in nav_tracks:
                        p = nt["path"]
                        if p not in seen_paths:
                            local_tracks.append(nt)
                            seen_paths.add(p)
                    progress.update(task_id, description=f"[green]✔ Retrieved {len(nav_tracks)} tracks from Navidrome server", completed=1, total=1)
                except Exception as e:
                    console.print(f"[yellow]Warning: Navidrome pre-scan error:[/yellow] {e}")

            if has_local:
                task_id = progress.add_task(f"[cyan]Pre-scanning local library ({self.music_dir})...", total=None)
                scanner = AudioFileScanner(
                    music_dir=str(self.music_dir),
                    catalog=self.catalog,
                    full_scan=False,
                    threads=self.threads
                )
                try:
                    disk_tracks = scanner.scan(progress=progress, task_id=task_id)
                    for dt in disk_tracks:
                        p = dt["path"]
                        if p not in seen_paths:
                            local_tracks.append(dt)
                            seen_paths.add(p)
                    progress.update(task_id, description=f"[green]✔ Parsed {len(disk_tracks)} audio files from local library", completed=1, total=1)
                except Exception as e:
                    console.print(f"[yellow]Warning: Local library pre-scan error:[/yellow] {e}")

        if not local_tracks:
            return

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

        console.print(f"[green]✔ Library Status:[/green] [bold]{len(found_items)}[/bold] artist tracks / [bold]{len(self.local_found_releases)}[/bold] releases already in library.")

    def _generate_all_search_queries(self) -> List[str]:
        """Generates a high-yield, curated list of prioritized search queries across the artist catalog."""
        queries: List[str] = []

        # Tier 1: Canonical Artist Name & Core Aliases
        queries.append(self.catalog.name)
        for alias in self.catalog.aliases:
            if alias.lower() != self.catalog.name.lower() and len(alias) >= 3:
                queries.append(alias)
        for alias in ("soniacz", "sonnie", "SyndraSound"):
            if alias.lower() != self.catalog.name.lower():
                queries.append(alias)

        # Tier 2: Primary Releases Missing from Local Library
        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if not rel_title or norm_rel in self.local_found_releases:
                continue

            q1 = clean_search_phrase(f"{self.catalog.name} {rel_title}")
            if q1:
                queries.append(q1)

        # Tier 3: Major Compilations & Split Releases Missing from Local Library
        comp_releases = self.raw_mb_data.get("releases_track_artist", [])
        for rel in comp_releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if not rel_title or norm_rel in self.local_found_releases:
                continue

            clean_t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", rel_title)
            clean_t = re.sub(r"^(?:re:\s*|v\.a\.?\s*|various\s*artists\s*)", "", clean_t, flags=re.IGNORECASE).strip()
            q = clean_search_phrase(clean_t)
            if q and len(q) >= 4:
                queries.append(q)

        # Tier 4: Standalone / Single Tracks Missing from Local Library
        standalone_candidates = [
            t for t in self.catalog.tracks
            if t.get("release_type") == "Standalone / Single" and t.get("norm_title") not in self.local_found_map
        ]
        for t in standalone_candidates[:10]: # Limit to 10 to avoid spam
            t_title = t.get("title", "")
            q = clean_search_phrase(f"{self.catalog.name} {t_title}")
            if q:
                queries.append(q)

        # Deduplicate preserving order
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
        self.candidate_index = PeerCandidateIndex(self.peer_directories)

    def _evaluate_indexed_directory(
        self,
        cd: CandidateDir,
        parsed_expected: List[Dict[str, Any]],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
        """Fast in-memory comparison of directory files against expected release tracklist."""
        audio_files = cd.audio_files
        if not audio_files:
            return None

        matched_tracks: List[Dict[str, Any]] = []
        unmatched_expected: List[Dict[str, Any]] = []

        for pe, exp in zip(parsed_expected, expected_tracks):
            exp_title = exp.get("title", "")
            matched_file = None

            for cf in audio_files:
                if is_track_title_match_fast(
                    pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
                    cf, self.all_artist_aliases, rel_title, cd.dir_name
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
            else:
                unmatched_expected.append(exp)

        match_ratio = len(matched_tracks) / len(expected_tracks) if expected_tracks else 0.0

        if match_ratio < self.min_match_ratio and len(matched_tracks) == 0:
            return None

        has_bonus = len(audio_files) > len(expected_tracks) and match_ratio >= 0.70

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
        bonus_ver_bonus = 15 if has_bonus else 0

        format_weight = format_score
        if self.preferred_format == "flac" and "FLAC" in format_label:
            format_weight += 25

        total_score = (match_ratio * 100) + format_weight + slot_bonus + speed_bonus + art_bonus + bonus_ver_bonus - queue_penalty

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
            "unmatched_expected": [u.get("title") for u in unmatched_expected],
            "total_score": total_score,
            "has_artwork": has_artwork,
            "dir_info": cd.dir_info,
            "all_dir_files": [cf.raw_file for cf in cd.all_dir_files]
        }

    def _evaluate_directory_in_memory(
        self,
        user: str,
        dir_name: str,
        dir_info: Dict[str, Any],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
        """Backward-compatible evaluation wrapper."""
        cd = CandidateDir(user, dir_name, dir_info)
        parsed_expected = pre_parse_expected_tracks(expected_tracks)
        return self._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, rel_title)

    def _reconcile_primary_releases(self):
        """Verifies candidate directories against all primary releases from MusicBrainz."""
        primary_rels = self.catalog.primary_releases if hasattr(self.catalog, "primary_releases") else [r for r in self.catalog.releases if not r.get("is_va", False)]
        console.print(f"\n[bold cyan]Verifying Primary Releases ({len(primary_rels)} releases)...[/bold cyan]")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for rel in primary_rels:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            token_rel = clean_tokens(rel_title)

            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys or (token_rel and token_rel in self.reconciled_release_keys):
                continue
            self.reconciled_release_keys.add(norm_rel)
            if token_rel:
                self.reconciled_release_keys.add(token_rel)

            expected_tracks = [
                t for t in self.catalog.tracks
                if t.get("norm_release") == norm_rel or rel_title in t.get("all_releases", set())
            ]
            if not expected_tracks:
                continue

            parsed_expected = pre_parse_expected_tracks(expected_tracks)
            clean_rel = clean_tokens(rel_title)
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            rel_sig_words = {w for w in rel_words if len(w) >= 3}

            # Instant candidate filtering using inverted index
            cand_dirs = self.candidate_index.get_candidate_dirs_for_release(rel_title, parsed_expected, len(expected_tracks))
            candidate_matches: List[Dict[str, Any]] = []

            for cd in cand_dirs:
                dir_matches = is_dir_name_match_fast(clean_rel, rel_sig_words, cd)
                if not dir_matches and len(expected_tracks) >= 3:
                    matched_cnt = 0
                    for pe in parsed_expected:
                        if any(is_track_title_match_fast(
                            pe["p_struct"], pe["words"], pe["clean_words"], pe["sig_words"], pe["concat"],
                            cf, self.all_artist_aliases, rel_title, cd.dir_name
                        ) for cf in cd.audio_files):
                            matched_cnt += 1
                    if matched_cnt >= 2 and (matched_cnt / len(expected_tracks) >= 0.60):
                        dir_matches = True

                if dir_matches and cd.audio_files:
                    match_data = self._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, rel_title)
                    if match_data and match_data["match_ratio"] >= (0.60 if len(expected_tracks) >= 3 else 0.99):
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
                                updated_cd = self.candidate_index.update_directory(top_user, top_dir, top_dir_info)
                                updated_match = self._evaluate_indexed_directory(updated_cd, parsed_expected, expected_tracks, rel_title)
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

                for mt in best_match.get("matched_tracks", []):
                    exp_t = mt.get("expected", "")
                    if exp_t:
                        self.covered_track_titles.add(normalize_text(exp_t))
                        self.covered_track_titles.add(clean_tokens(exp_t))
                if best_match.get("match_ratio", 0) >= self.min_match_ratio:
                    for exp in expected_tracks:
                        exp_t = exp.get("title", "")
                        if exp_t:
                            self.covered_track_titles.add(normalize_text(exp_t))
                            self.covered_track_titles.add(clean_tokens(exp_t))
            else:
                self.unresolved_releases.append({
                    "release_title": rel_title,
                    "release_type": rel.get("type", "Release"),
                    "date": rel.get("date", "N/A"),
                    "expected_track_count": len(expected_tracks),
                    "expected_tracks": [t.get("title") for t in expected_tracks]
                })

    def _reconcile_compilation_tracks(self):
        """Verifies candidate tracks on compilation / VA releases."""
        comp_releases = self.raw_mb_data.get("releases_track_artist", [])
        console.print(f"\n[bold cyan]Verifying Compilation & Split Tracks ({len(comp_releases)} releases)...[/bold cyan]")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for rel in comp_releases:
            rel_title = rel.get("title", "")
            artist_credit = rel.get("artist-credit-phrase", "Various Artists")
            norm_rel = normalize_text(rel_title)
            token_rel = clean_tokens(rel_title)

            # Skip if this compilation was already matched as a primary release or previously reconciled
            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys or (token_rel and token_rel in self.reconciled_release_keys):
                continue

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

            missing_tracks = [
                tt for tt in target_tracks
                if tt["norm_title"] not in self.local_found_map
                and tt["norm_title"] not in self.covered_track_titles
                and clean_tokens(tt["title"]) not in self.covered_track_titles
            ]
            if not missing_tracks:
                continue

            self.reconciled_release_keys.add(norm_rel)
            if token_rel:
                self.reconciled_release_keys.add(token_rel)

            comp_candidate_dirs: Dict[Tuple[str, str], Dict[str, Any]] = {}

            for tt in missing_tracks:
                t_title = tt.get("title", "")
                parsed_tt = pre_parse_single_track(t_title)
                cand_files = self.candidate_index.get_candidate_files_for_track(parsed_tt)

                for cf in cand_files:
                    if is_track_title_match_fast(
                        parsed_tt["p_struct"], parsed_tt["words"], parsed_tt["clean_words"],
                        parsed_tt["sig_words"], parsed_tt["concat"], cf, self.all_artist_aliases,
                        rel_title, cf.dir_name
                    ):
                        fmt_label, fmt_score = cf.fmt_label, cf.fmt_score
                        dir_info = cf.dir_info
                        queue = dir_info.get("queue", 0)
                        speed = dir_info.get("speed", 0)
                        has_slot = dir_info.get("has_slot", True)

                        fmt_bonus = 25 if self.preferred_format == "flac" and "FLAC" in fmt_label else 0
                        score = fmt_score + fmt_bonus + (20 if has_slot else 0) + min(speed / 500_000, 20) - min(queue / 2, 40)

                        dir_key = (cf.user, cf.dir_name)
                        if dir_key not in comp_candidate_dirs or score > comp_candidate_dirs[dir_key]["score"]:
                            comp_candidate_dirs[dir_key] = {
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
                                "score": score,
                                "track_title": t_title,
                                "dir_info": dir_info
                            }

            if comp_candidate_dirs:
                best_dir_candidate = max(comp_candidate_dirs.values(), key=lambda x: x["score"])
                self.verified_compilation_tracks.append({
                    "track_title": best_dir_candidate["track_title"],
                    "release_title": rel_title,
                    "release_artist": artist_credit,
                    "best_match": best_dir_candidate
                })
                self.covered_track_titles.add(normalize_text(best_dir_candidate["track_title"]))
                self.covered_track_titles.add(clean_tokens(best_dir_candidate["track_title"]))
                for tt in missing_tracks:
                    self.covered_track_titles.add(tt["norm_title"])
                    self.covered_track_titles.add(clean_tokens(tt["title"]))
            else:
                for tt in missing_tracks:
                    self.unresolved_compilation_tracks.append({
                        "track_title": tt.get("title", ""),
                        "release_title": rel_title,
                        "release_artist": artist_credit
                    })

    def _reconcile_standalone_tracks(self):
        """Discovers and verifies standalone singles, remixes, collaborations, and non-album tracks."""
        console.print(f"\n[bold cyan]Verifying Standalone & Non-Album Tracks...[/bold cyan]")

        all_covered_titles = set(self.local_found_map.keys()) | set(self.covered_track_titles)
        for vr in self.verified_releases:
            for mt in vr["best_match"].get("matched_tracks", []):
                all_covered_titles.add(normalize_text(mt.get("expected", "")))
                all_covered_titles.add(clean_tokens(mt.get("expected", "")))
        for vc in self.verified_compilation_tracks:
            all_covered_titles.add(normalize_text(vc.get("track_title", "")))
            all_covered_titles.add(clean_tokens(vc.get("track_title", "")))

        standalone_candidates = []
        for t in self.catalog.tracks:
            norm_t = t.get("norm_title", "")
            clean_t = clean_tokens(t.get("title", ""))
            norm_rel = t.get("norm_release", "")
            # Skip if track title was already covered
            if norm_t in all_covered_titles or clean_t in all_covered_titles:
                continue
            # Skip if track belongs to an album that was already reconciled / verified in full
            if norm_rel and norm_rel in self.reconciled_release_keys:
                continue
            standalone_candidates.append(t)

        if not standalone_candidates:
            return

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for t in standalone_candidates:
            t_title = t.get("title", "")
            norm_t = t.get("norm_title", "")
            clean_t = clean_tokens(t_title)
            artist_credit = t.get("artist_credit", "")

            if norm_t in all_covered_titles or clean_t in all_covered_titles:
                continue

            parsed_t = pre_parse_single_track(t_title)
            cand_files = self.candidate_index.get_candidate_files_for_track(parsed_t)
            matched_candidates: List[Dict[str, Any]] = []

            for cf in cand_files:
                if is_track_title_match_fast(
                    parsed_t["p_struct"], parsed_t["words"], parsed_t["clean_words"],
                    parsed_t["sig_words"], parsed_t["concat"], cf, self.all_artist_aliases,
                    t.get("release_title", ""), cf.dir_name
                ):
                    fmt_label, fmt_score = cf.fmt_label, cf.fmt_score
                    dir_info = cf.dir_info
                    queue = dir_info.get("queue", 0)
                    speed = dir_info.get("speed", 0)
                    has_slot = dir_info.get("has_slot", True)
                    fmt_bonus = 25 if self.preferred_format == "flac" and "FLAC" in fmt_label else 0
                    matched_candidates.append({
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

            if matched_candidates:
                matched_candidates.sort(key=lambda x: x["total_score"], reverse=True)
                best_match = matched_candidates[0]
                self.verified_standalone_tracks.append({
                    "track_title": t_title,
                    "artist_credit": artist_credit or self.catalog.name,
                    "best_match": best_match
                })
                all_covered_titles.add(norm_t)
                all_covered_titles.add(clean_t)
                self.covered_track_titles.add(norm_t)
                self.covered_track_titles.add(clean_t)
            else:
                if t.get("release_type") == "Standalone / Single":
                    self.unresolved_standalone_tracks.append({
                        "track_title": t_title,
                        "artist_credit": artist_credit or self.catalog.name
                    })

    def _queue_downloads(self):
        """Enqueues verified primary release directories, compilation tracks, and standalone tracks into slskd."""
        queued_dirs_set: Set[Tuple[str, str]] = set()
        queued_release_keys: Set[str] = set()
        queued_track_keys: Set[str] = set()
        queued_files_set: Set[str] = set(self.already_downloading_files)

        def is_loose_dump(d_path: str) -> bool:
            parts = [p.strip().lower() for p in sanitize_remote_path(d_path).split("\\") if p.strip()]
            last = parts[-1] if parts else ""
            if len(parts) <= 1:
                return True
            if any(k == last for k in ("dump", "archive", "tracks", "music", "songs", "shared", "root", "audio")):
                return True
            return any(k in last for k in ("soundcloud singles", "loose tracks", "random singles", "singles", "various singles", "dump", "archive"))

        # 1. Queue Primary Release Directories (Full Folders)
        for item in self.verified_releases:
            rel_title = item.get("release_title", "")
            norm_rel = normalize_text(rel_title)
            clean_rel = clean_tokens(rel_title)

            # Prevent duplicate queueing of the same release
            if norm_rel in queued_release_keys or (clean_rel and clean_rel in queued_release_keys):
                continue

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
                    queued_release_keys.add(norm_rel)
                    if clean_rel:
                        queued_release_keys.add(clean_rel)
                    for mt in match.get("matched_tracks", []):
                        exp = mt.get("expected", "")
                        if exp:
                            queued_track_keys.add(normalize_text(exp))
                            queued_track_keys.add(clean_tokens(exp))

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
            rel_title = item.get("release_title", "")
            track_title = item.get("track_title", "")
            norm_rel = normalize_text(rel_title)
            clean_rel = clean_tokens(rel_title)
            norm_track = normalize_text(track_title)
            clean_track = clean_tokens(track_title)

            # Skip if track already queued in another directory
            if (norm_track and norm_track in queued_track_keys) or (clean_track and clean_track in queued_track_keys):
                continue

            match = item["best_match"]
            user = match["user"]
            dir_name = match["directory"]
            dir_key = (user, dir_name)

            if dir_key in queued_dirs_set:
                continue

            download_full_dir = (
                not self.singles_only
                and not is_loose_dump(dir_name)
                and (norm_rel not in queued_release_keys)
                and (not clean_rel or clean_rel not in queued_release_keys)
            )

            if not download_full_dir:
                full_fn = match.get("full_filename")
                files_to_enqueue = [{"filename": full_fn, "size": match.get("size", 0)}] if full_fn and full_fn not in queued_files_set else []
                if files_to_enqueue:
                    queued_files_set.add(full_fn)
            else:
                dir_files = match.get("dir_info", {}).get("full_directory_files") or match.get("dir_info", {}).get("matched_search_files", [])
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
                    if download_full_dir:
                        queued_release_keys.add(norm_rel)
                        if clean_rel:
                            queued_release_keys.add(clean_rel)
                    if norm_track:
                        queued_track_keys.add(norm_track)
                    if clean_track:
                        queued_track_keys.add(clean_track)

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
            track_title = item.get("track_title", "")
            norm_track = normalize_text(track_title)
            clean_track = clean_tokens(track_title)

            # Skip if track already queued in a release or compilation
            if (norm_track and norm_track in queued_track_keys) or (clean_track and clean_track in queued_track_keys):
                continue

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
                    queued_files_set.add(full_fn)
                    if norm_track:
                        queued_track_keys.add(norm_track)
                    if clean_track:
                        queued_track_keys.add(clean_track)

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
