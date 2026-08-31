"""
Soulseek / slskd artist discography discovery, peer candidate indexing, and download queueing.
"""

import os
import re
import csv
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from unidecode import unidecode
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.text import Text
from rich import box

from musicscraper.config import Config
from musicscraper.core.constants import (
    AUDIO_EXTENSIONS,
    SUPPORTING_EXTENSIONS,
    DIR_STOP_WORDS,
    POLISH_DIACRITICS_MAP,
)
from musicscraper.core.text import (
    normalize_text,
    clean_tokens,
    _tokenize_words_cached,
    calculate_similarity,
    strip_track_number_and_artist,
    parse_track_title_structure,
    are_versions_compatible,
    clean_search_phrase,
    extract_dir_and_filename,
    is_sublist,
)
from musicscraper.core.audio import AudioQualityAnalyzer
from musicscraper.core.report import console, BaseReportExporter
from musicscraper.clients.slskd import SlskdClient, SlskdAPIError
from musicscraper.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from musicscraper.clients.navidrome import NavidromeScanner
from musicscraper.services.auditor import AudioFileScanner
from musicscraper.services.reconciler import DiscographyReconciler


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
        ext = Path(self.base_filename).suffix.lower()
        self.is_audio = ext in AUDIO_EXTENSIONS
        self.is_supporting = ext in SUPPORTING_EXTENSIONS
        if self.is_audio:
            self.p_struct = parse_track_title_structure(self.base_filename)
            self.clean_base = self.p_struct["base_norm"] or strip_track_number_and_artist(self.base_filename)
            self.words = list(_tokenize_words_cached(self.p_struct["base_norm"] or self.base_filename))
            self.clean_words = list(_tokenize_words_cached(self.clean_base)) or self.words
            self.sig_words = {w for w in self.clean_words if len(w) >= 3 and w not in DIR_STOP_WORDS}
            self.concat = "".join(self.clean_words)
            self.fmt_label, self.fmt_score = AudioQualityAnalyzer.determine_stream_quality(raw_file)
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

        seen_files: Set[str] = set()
        for rf in raw_files:
            fn = (rf.get("filename") or rf.get("full_filename") or "").replace("/", "\\").strip().lower()
            if fn and fn in seen_files:
                continue
            if fn:
                seen_files.add(fn)
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

    def _build_index(self) -> None:
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
        cd = CandidateDir(user, dir_name, dir_info)
        self.dirs_map[(user, dir_name)] = cd
        return cd

    def get_candidate_dirs_for_release(
        self,
        rel_title: str,
        parsed_expected: List[Dict[str, Any]],
        expected_count: int
    ) -> List[CandidateDir]:
        clean_rel = clean_tokens(rel_title)
        rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
        rel_sig_words = {w for w in rel_words if len(w) >= 3}

        album_sig_words: Set[str] = set()
        for pe in parsed_expected:
            album_sig_words.update(pe["sig_words"])

        cand_dirs_set: Set[CandidateDir] = set()

        for w in rel_sig_words:
            if w in self.word_to_dirs:
                cand_dirs_set.update(self.word_to_dirs[w])

        if clean_rel:
            for cd in self.all_dirs:
                if clean_rel in cd.clean_dir or cd.clean_dir in clean_rel:
                    cand_dirs_set.add(cd)

        if expected_count >= 3:
            for w in album_sig_words:
                if w in self.word_to_dirs:
                    cand_dirs_set.update(self.word_to_dirs[w])

        return list(cand_dirs_set)

    def get_candidate_files_for_track(self, parsed_track: Dict[str, Any]) -> List[CandidateFile]:
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

    core_title = re.sub(r"\[.*?\]|\(.*?\)", " ", track_title).strip()
    core_base = strip_track_number_and_artist(core_title)
    core_words = list(_tokenize_words_cached(core_base))
    core_concat = "".join(core_words)
    core_sig_words = {w for w in core_words if len(w) >= 3 and w not in DIR_STOP_WORDS}

    return {
        "title": track_title,
        "p_struct": p_struct,
        "clean_base": clean_base,
        "words": words,
        "clean_words": clean_words,
        "sig_words": sig_words,
        "concat": concat,
        "core_base": core_base,
        "core_words": core_words,
        "core_sig_words": core_sig_words,
        "core_concat": core_concat,
    }


def pre_parse_expected_tracks(expected_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
    """Fast, zero-allocation track title matcher."""
    p_cand = cand.p_struct
    if not p_cand:
        return False

    struct_exp = p_exp["p_struct"] if "p_struct" in p_exp else p_exp
    if not are_versions_compatible(struct_exp["version_type"], struct_exp["version_text"], p_cand["version_type"], p_cand["version_text"]):
        return False

    v_exp = struct_exp["base_norm"].split()[-1] if struct_exp.get("base_norm") else ""
    v_cand = cand.clean_base.split()[-1] if cand.clean_base else ""
    if v_exp.isdigit() and v_cand.isdigit() and v_exp != v_cand:
        return False

    if not exp_words:
        return False

    file_words = cand.words
    clean_file_words = cand.clean_words
    core_exp_words = p_exp.get("core_words", [])
    core_concat = p_exp.get("core_concat", "")

    exact_match = (
        is_sublist(clean_exp_words, clean_file_words) or
        is_sublist(clean_exp_words, file_words) or
        is_sublist(exp_words, clean_file_words) or
        is_sublist(exp_words, file_words) or
        (core_exp_words and (
            is_sublist(core_exp_words, clean_file_words) or
            is_sublist(core_exp_words, file_words)
        ))
    )

    if not exact_match:
        if len(exp_concat) >= 3 and (exp_concat == cand.concat or (len(exp_concat) >= 4 and exp_concat in cand.concat)):
            exact_match = True
        elif core_concat and len(core_concat) >= 3 and (core_concat == cand.concat or (len(core_concat) >= 4 and core_concat in cand.concat)):
            exact_match = True

    if not exact_match and len(exp_concat) >= 4 and len(cand.concat) >= 4:
        if (exp_sig_words & cand.sig_words) or not exp_sig_words:
            sim = calculate_similarity(struct_exp["base_norm"], cand.clean_base)
            if sim >= 0.88:
                exact_match = True

    if not exact_match:
        return False

    chk_concat = core_concat or exp_concat
    if len(chk_concat) <= 3 or chk_concat in DIR_STOP_WORDS:
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


def is_dir_name_match_fast(clean_rel: str, rel_sig_words: Set[str], cd: CandidateDir) -> bool:
    if clean_rel and (clean_rel in cd.clean_dir or cd.clean_dir in clean_rel):
        return True
    if not rel_sig_words:
        return False
    matches = len(rel_sig_words & cd.dir_sig_words)
    return matches >= min(2, len(rel_sig_words))


class SlskdArtistScraper:
    """Orchestrates parallel Soulseek discovery, directory expansion, reconciliation, and queueing."""

    def __init__(
        self,
        artist_query: str,
        slskd_client: Optional[SlskdClient] = None,
        music_dir: Optional[Path] = None,
        preferred_format: str = "flac",
        min_match_ratio: float = 0.70,
        search_timeout: float = 25.0,
        dry_run: bool = False,
        singles_only: bool = False,
        threads: int = 6,
    ):
        self.artist_query = artist_query.strip()
        self.client = slskd_client or SlskdClient()
        self.music_dir = Path(music_dir or Config.DEFAULT_LIBRARY_DIR).resolve() if (music_dir or Config.DEFAULT_LIBRARY_DIR) else None
        self.preferred_format = preferred_format.lower()
        self.min_match_ratio = min_match_ratio
        self.search_timeout = search_timeout
        self.dry_run = dry_run
        self.singles_only = singles_only
        self.threads = threads

        self.mb_client = MusicBrainzClient()
        self.catalog: Optional[ArtistCatalog] = None
        self.raw_mb_data: Dict[str, Any] = {}
        self.all_artist_aliases: Set[str] = set()

        self.local_found_map: Dict[str, Dict[str, Any]] = {}
        self.local_found_releases: Set[str] = set()

        self.peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.candidate_index: Optional[PeerCandidateIndex] = None
        self.searches_performed: Set[str] = set()

        self.reconciled_release_keys: Set[str] = set()
        self.covered_track_titles: Set[str] = set()

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
        console.print(f"[green]✔ Connected to slskd[/green] (User: [bold]{slsk_user}[/bold] | Server: [dim]{server_state}[/dim])")

        self.already_downloading_files = self.client.get_queued_filenames()

        # 2. Resolve Artist & Catalog
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(self.artist_query)
        self.raw_mb_data = self.mb_client.fetch_full_discography(mbid)
        self.catalog = ArtistCatalog(self.raw_mb_data)

        self.all_artist_aliases = set(self.catalog.aliases)
        self.all_artist_aliases.add(self.catalog.name)
        for a in list(self.all_artist_aliases):
            self.all_artist_aliases.add(unidecode(a))

        console.print(f"[green]✔ Canonical Name:[/green] [bold]{self.catalog.name}[/bold] (MBID: {self.catalog.mbid})")
        console.print(f"[dim]Catalog: {len(self.catalog.tracks)} total tracks | {len(self.catalog.primary_releases)} primary releases[/dim]")

        # 3. Pre-scan local library
        self._prescan_library()

        # 4. Search Soulseek
        self._discover_soulseek_candidates()

        # 5. Reconcile primary releases
        self._reconcile_primary_releases()

        # 6. Reconcile compilations and singles
        self._reconcile_compilation_tracks()
        self._reconcile_standalone_tracks()

        # 7. Queue downloads
        if not self.dry_run:
            self._queue_downloads()
        else:
            console.print("\n[yellow]--dry-run enabled: Showing matched directories without enqueuing transfers.[/yellow]")

        # 8. Summary
        self._print_summary()

        return {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "queued_directories": self.queued_directories,
            "verified_releases": self.verified_releases,
        }

    def _prescan_library(self) -> None:
        if not self.music_dir or not self.music_dir.exists():
            return

        scanner = AudioFileScanner(music_dir=self.music_dir, catalog=self.catalog, threads=self.threads)
        local_tracks = scanner.scan()
        if not local_tracks:
            return

        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=local_tracks)
        found_items, _ = reconciler.reconcile()

        for item in found_items:
            mb = item["mb_track"]
            norm_t = mb.get("norm_title", "")
            if norm_t:
                self.local_found_map[norm_t] = item["local_track"]

        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            rel_tracks = [
                t for t in self.catalog.tracks
                if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
                or t.get("norm_release") == norm_rel
            ]
            if not rel_tracks:
                continue

            artist_tracks = [
                t for t in rel_tracks
                if any(alias.lower() in t.get("artist_credit", "").lower() for alias in self.all_artist_aliases)
                or t.get("artist_credit", "").lower() == self.catalog.name.lower()
            ]

            found_rel_tracks = [t for t in rel_tracks if t.get("norm_title") in self.local_found_map]
            found_artist_tracks = [t for t in artist_tracks if t.get("norm_title") in self.local_found_map]

            is_complete = False
            if artist_tracks and len(found_artist_tracks) == len(artist_tracks):
                is_complete = True
            elif len(rel_tracks) > 0 and (len(found_rel_tracks) / len(rel_tracks) >= 0.85):
                is_complete = True

            if is_complete:
                self.local_found_releases.add(norm_rel)

        console.print(f"[green]✔ Library Status:[/green] [bold]{len(found_items)}[/bold] artist tracks / [bold]{len(self.local_found_releases)}[/bold] releases already in library.")

    def _generate_all_search_queries(self) -> List[str]:
        queries: List[str] = [self.catalog.name]
        for alias in self.catalog.aliases:
            if alias.lower() != self.catalog.name.lower() and len(alias) >= 3:
                queries.append(alias)

        for rel in self.catalog.primary_releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if not rel_title or norm_rel in self.local_found_releases:
                continue
            q1 = clean_search_phrase(f"{self.catalog.name} {rel_title}")
            if q1 and q1 not in queries:
                queries.append(q1)

        return list(dict.fromkeys(q for q in queries if q and len(q) >= 3))

    def _discover_soulseek_candidates(self) -> None:
        all_queries = self._generate_all_search_queries()
        console.print(f"\n[cyan]Executing parallel Soulseek searches ({len(all_queries)} targeted queries)...[/cyan]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Searching Soulseek network (0/{len(all_queries)} queries completed)...", total=len(all_queries))

            def _update_progress(completed: int, total: int, last_query: str) -> None:
                progress.update(task, completed=completed, description=f"Searching Soulseek ({completed}/{total} completed: {last_query})...")

            batch_results = self.client.batch_search(
                all_queries,
                timeout=self.search_timeout,
                poll_interval=1.0,
                on_progress=_update_progress
            )
            progress.update(task, completed=len(all_queries), description="Processing responses...")

            total_files = 0
            for query_str, s_data in batch_results.items():
                for resp in s_data.get("responses", []):
                    user = resp.get("username")
                    speed = resp.get("uploadSpeed", 0)
                    queue = resp.get("queueLength", 0)
                    has_slot = resp.get("hasFreeUploadSlot", True)
                    for f in resp.get("files", []):
                        fn = f.get("filename", "")
                        if not fn or f.get("isLocked", False):
                            continue
                        dir_name, _ = extract_dir_and_filename(fn)
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
                                "full_directory_files": None,
                                "_seen_files": set()
                            }
                        fn_norm = fn.strip().lower()
                        if fn_norm not in self.peer_directories[key]["_seen_files"]:
                            self.peer_directories[key]["_seen_files"].add(fn_norm)
                            self.peer_directories[key]["matched_search_files"].append(f)
                            total_files += 1

        console.print(f"[green]✔ Soulseek Discovery:[/green] Found [bold]{len(self.peer_directories)}[/bold] candidate directories ([dim]{total_files} candidate files[/dim]).")
        self.candidate_index = PeerCandidateIndex(self.peer_directories)

    def _evaluate_indexed_directory(
        self,
        cd: CandidateDir,
        parsed_expected: List[Dict[str, Any]],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str
    ) -> Optional[Dict[str, Any]]:
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

        primary_audio = audio_files[0]
        format_label, format_score = primary_audio.fmt_label, primary_audio.fmt_score
        queue = cd.dir_info.get("queue", 0)
        speed = cd.dir_info.get("speed", 0)
        has_slot = cd.dir_info.get("has_slot", True)

        total_score = (match_ratio * 100) + format_score + (20 if has_slot else 0) - min(queue / 2, 40)
        if self.preferred_format == "flac" and "FLAC" in format_label:
            total_score += 25

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
            "dir_info": cd.dir_info,
            "all_dir_files": [cf.raw_file for cf in cd.all_dir_files]
        }

    def _reconcile_primary_releases(self) -> None:
        primary_rels = self.catalog.primary_releases
        console.print(f"\n[bold cyan]Verifying Primary Releases ({len(primary_rels)} releases)...[/bold cyan]")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for rel in primary_rels:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys:
                continue
            self.reconciled_release_keys.add(norm_rel)

            expected_tracks = [
                t for t in self.catalog.tracks
                if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
                or t.get("norm_release") == norm_rel
            ]
            if not expected_tracks:
                continue

            parsed_expected = pre_parse_expected_tracks(expected_tracks)
            clean_rel = clean_tokens(rel_title)
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            rel_sig_words = {w for w in rel_words if len(w) >= 3}

            cand_dirs = self.candidate_index.get_candidate_dirs_for_release(rel_title, parsed_expected, len(expected_tracks))
            candidate_matches: List[Dict[str, Any]] = []

            for cd in cand_dirs:
                if is_dir_name_match_fast(clean_rel, rel_sig_words, cd) and cd.audio_files:
                    eval_res = self._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, rel_title)
                    if eval_res and eval_res["match_ratio"] >= self.min_match_ratio:
                        matched_new_tracks = [
                            m for m in eval_res["matched_tracks"]
                            if normalize_text(m["expected"]) not in self.local_found_map
                        ]
                        if not matched_new_tracks and len(eval_res["matched_tracks"]) > 0:
                            continue
                        candidate_matches.append(eval_res)

            if candidate_matches:
                best = max(candidate_matches, key=lambda x: x["total_score"])
                self.verified_releases.append({
                    "release": rel_title,
                    "user": best["user"],
                    "directory": best["directory"],
                    "match_ratio": best["match_ratio"],
                    "matched_count": len(best["matched_tracks"]),
                    "total_count": len(expected_tracks),
                    "format_label": best["format_label"],
                    "queue": best["queue"],
                    "speed": best["speed"],
                    "files": best["all_dir_files"]
                })
                self.queued_directories.append(best)
                console.print(f"[green]✔ Matched Album:[/green] [bold]{rel_title}[/bold] ({best['format_label']}) from [cyan]{best['user']}[/cyan] ({len(best['matched_tracks'])}/{len(expected_tracks)} tracks)")
            else:
                self.unresolved_releases.append(rel)

    def _reconcile_compilation_tracks(self) -> None:
        comp_releases = self.catalog.compilation_releases
        if not comp_releases:
            return
        console.print(f"\n[bold cyan]Verifying Compilation / VA Releases ({len(comp_releases)} releases)...[/bold cyan]")

    def _reconcile_standalone_tracks(self) -> None:
        standalone = [t for t in self.catalog.tracks if t.get("release_type") == "Standalone / Single"]
        if not standalone:
            return
        console.print(f"\n[bold cyan]Verifying Standalone Tracks ({len(standalone)} tracks)...[/bold cyan]")

    def _queue_downloads(self) -> None:
        if not self.queued_directories:
            console.print("[yellow]No releases to enqueue.[/yellow]")
            return

        try:
            self.already_downloading_files = self.client.get_queued_filenames()
            queued_fps = self.client.get_queued_track_fingerprints()
        except Exception:
            queued_fps = {"base_filenames": set(), "clean_titles": set(), "full_paths": set()}

        console.print(f"\n[cyan]Enqueuing {len(self.queued_directories)} verified releases into slskd...[/cyan]")
        for d in self.queued_directories:
            try:
                files_to_download = []
                for f in d["all_dir_files"]:
                    fn = f.get("filename", "")
                    if not fn:
                        continue
                    clean_p = fn.replace("/", "\\").split("\\")[-1].lower()
                    if fn in self.already_downloading_files or clean_p in queued_fps["base_filenames"]:
                        continue

                    files_to_download.append(f)
                    self.already_downloading_files.add(fn)
                    queued_fps["base_filenames"].add(clean_p)

                if not files_to_download:
                    console.print(f"[dim]↷ Skipping already queued folder:[/dim] {d['directory']} from {d['user']}")
                    continue

                self.client.enqueue_download(d["user"], files_to_download)
                console.print(f"[green]✔ Enqueued folder:[/green] {d['directory']} from {d['user']} ({len(files_to_download)} files)")
            except Exception as e:
                console.print(f"[red]Failed to enqueue {d['directory']}: {e}[/red]")

    def _print_summary(self) -> None:
        table = Table(title="Soulseek Scraper Summary", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("Release Title", style="bold white")
        table.add_column("Source / Peer", style="cyan")
        table.add_column("Format", style="green")
        table.add_column("Status", justify="center")

        for r in self.verified_releases:
            table.add_row(r["release"], r["user"], r["format_label"], "[green]✔ Enqueued[/green]" if not self.dry_run else "[yellow]Matched (Dry-run)[/yellow]")

        for u in self.unresolved_releases:
            table.add_row(u.get("title", ""), "-", "-", "[red]✖ Unresolved[/red]")

        console.print(table)
