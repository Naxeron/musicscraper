"""
Library auditor service checking local library and Navidrome against MusicBrainz discographies.
"""

import os
import re
import csv
import json
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor

import mutagen
from unidecode import unidecode
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich import box

from musicscraper.config import Config
from musicscraper.core.constants import AUDIO_EXTENSIONS, GENERIC_OR_COMMON_WORDS
from musicscraper.core.text import normalize_text, strip_track_number_and_artist
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.report import console, BaseReportExporter
from musicscraper.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from musicscraper.clients.navidrome import NavidromeScanner
from musicscraper.services.reconciler import DiscographyReconciler


def is_distinct_track_title(title_norm: str) -> bool:
    """Determines if a normalized track title is distinct enough to safely match standalone filenames."""
    if not title_norm or len(title_norm) < 3:
        return False
    if title_norm in GENERIC_OR_COMMON_WORDS:
        return False
    words = title_norm.split()
    if len(words) >= 2 and len(title_norm) >= 4:
        return True
    if len(words) == 1 and len(title_norm) >= 4 and title_norm not in GENERIC_OR_COMMON_WORDS:
        return True
    return False


class AudioFileScanner:
    """Scans local music directory with fast 2-stage discovery and persistent SQLite caching."""

    def __init__(
        self,
        music_dir: Path,
        catalog: ArtistCatalog,
        full_scan: bool = False,
        threads: int = 24,
        cache_manager: Optional[UnifiedCacheManager] = None
    ):
        self.music_dir = Path(music_dir).resolve()
        self.catalog = catalog
        self.full_scan = full_scan
        self.threads = threads
        self.cache = cache_manager or UnifiedCacheManager()

    def scan(self, progress: Optional[Progress] = None, task_id: Optional[Any] = None) -> List[Dict[str, Any]]:
        """Discovers and extracts metadata from candidate audio files."""
        if not self.music_dir.exists():
            return []

        if progress and task_id:
            progress.update(task_id, description="[cyan]Stage 1: Discovering candidate audio files on disk...")

        # 1. Compile Artist Alias Regex
        alias_norms = sorted({normalize_text(a) for a in self.catalog.aliases if normalize_text(a) and len(normalize_text(a)) >= 2}, key=len, reverse=True)
        alias_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(a) for a in alias_norms) + r")(?:\b|_)", re.IGNORECASE) if alias_norms else None

        # 2. Compile Release Title Regex
        rel_norms_set = set()
        for rel in self.catalog.releases:
            norm = normalize_text(rel.get("title", ""))
            if norm and len(norm) >= 2 and norm not in GENERIC_OR_COMMON_WORDS:
                rel_norms_set.add(norm)
        for trk in self.catalog.tracks:
            for rel_t in trk.get("all_releases", set()):
                norm = normalize_text(rel_t)
                if norm and len(norm) >= 2 and norm not in GENERIC_OR_COMMON_WORDS:
                    rel_norms_set.add(norm)
        rel_norms = sorted(rel_norms_set, key=len, reverse=True)
        rel_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(r) for r in rel_norms) + r")(?:\b|_)", re.IGNORECASE) if rel_norms else None

        # 3. Compile Distinct Track Title Regex
        trk_norms_set = {
            trk.get("norm_title", "")
            for trk in self.catalog.tracks
            if is_distinct_track_title(trk.get("norm_title", ""))
        }
        trk_norms = sorted(trk_norms_set, key=len, reverse=True)
        trk_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(t) for t in trk_norms) + r")(?:\b|_)", re.IGNORECASE) if trk_norms else None

        candidate_paths: List[str] = []

        # Stage 1: Walk directory
        for root, dirs, files in os.walk(self.music_dir):
            norm_root = normalize_text(root)
            dir_matches = bool((alias_regex and alias_regex.search(norm_root)) or (rel_regex and rel_regex.search(norm_root)))

            audio_in_dir = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS]
            if not audio_in_dir:
                continue

            if self.full_scan or dir_matches:
                for f in audio_in_dir:
                    candidate_paths.append(os.path.join(root, f))
            else:
                matching_files = [
                    f for f in audio_in_dir
                    if (alias_regex and alias_regex.search(normalize_text(f)))
                    or (trk_regex and trk_regex.search(normalize_text(f)))
                    or (rel_regex and rel_regex.search(normalize_text(f)))
                ]
                if len(matching_files) >= 2 or (matching_files and len(matching_files) == len(audio_in_dir)):
                    for f in audio_in_dir:
                        candidate_paths.append(os.path.join(root, f))
                else:
                    for f in matching_files:
                        candidate_paths.append(os.path.join(root, f))

        total_candidates = len(candidate_paths)

        if progress and task_id:
            progress.update(
                task_id,
                description=f"[cyan]Stage 2: Inspecting tags of {total_candidates} candidate audio files...",
                total=total_candidates,
                completed=0
            )

        # Stage 2: Cache Lookup + Extraction
        local_tracks: List[Dict[str, Any]] = []
        uncached_paths: List[Path] = []

        for p_str in candidate_paths:
            p = Path(p_str)
            cached_meta = self.cache.get_audio_metadata(p)
            if cached_meta:
                local_tracks.append({
                    "path": str(cached_meta.path),
                    "filename": cached_meta.path.name,
                    "title": cached_meta.title,
                    "norm_title": cached_meta.norm_title,
                    "album": cached_meta.album,
                    "norm_album": cached_meta.norm_album,
                    "track_number": cached_meta.track_number,
                    "artists": [cached_meta.artist] if cached_meta.artist else [],
                    "mb_track_ids": cached_meta.mb_track_ids,
                    "mb_rec_ids": cached_meta.mb_rec_ids,
                    "mb_artist_ids": cached_meta.mb_artist_ids,
                    "mb_release_ids": cached_meta.mb_release_ids,
                    "source": "local"
                })
                if progress and task_id:
                    progress.advance(task_id, 1)
            else:
                uncached_paths.append(p)

        if uncached_paths:
            from musicscraper.core.audio import AudioQualityAnalyzer
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                for meta in pool.map(AudioQualityAnalyzer.analyze_file, uncached_paths):
                    if meta:
                        self.cache.store_audio_metadata(meta)
                        local_tracks.append({
                            "path": str(meta.path),
                            "filename": meta.path.name,
                            "title": meta.title,
                            "norm_title": meta.norm_title,
                            "album": meta.album,
                            "norm_album": meta.norm_album,
                            "track_number": meta.track_number,
                            "artists": [meta.artist] if meta.artist else [],
                            "mb_track_ids": meta.mb_track_ids,
                            "mb_rec_ids": meta.mb_rec_ids,
                            "mb_artist_ids": meta.mb_artist_ids,
                            "mb_release_ids": meta.mb_release_ids,
                            "source": "local"
                        })
                    if progress and task_id:
                        progress.advance(task_id, 1)

        return local_tracks


class AuditorService:
    """Orchestrates discography retrieval, local/navidrome scanning, reconciliation, and reports."""

    def __init__(
        self,
        mb_client: Optional[MusicBrainzClient] = None,
        cache_manager: Optional[UnifiedCacheManager] = None
    ):
        self.mb_client = mb_client or MusicBrainzClient()
        self.cache = cache_manager or UnifiedCacheManager()

    def audit_artist(
        self,
        artist_query: str,
        music_dir: Optional[Path] = None,
        navidrome_url: Optional[str] = None,
        navidrome_user: Optional[str] = None,
        navidrome_password: Optional[str] = None,
        full_scan: bool = False,
        force_refresh: bool = False,
        threads: int = 24
    ) -> Tuple[ArtistCatalog, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Runs complete audit for an artist query against local disk and/or Navidrome."""
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(artist_query)
        discog_data = self.mb_client.fetch_full_discography(mbid, force_refresh=force_refresh)
        catalog = ArtistCatalog(discog_data)

        local_tracks: List[Dict[str, Any]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            scan_task = progress.add_task("[cyan]Scanning library...", total=None)

            # 1. Local Disk Scan
            scan_target = Path(music_dir or Config.DEFAULT_LIBRARY_DIR).resolve()
            if scan_target.exists():
                scanner = AudioFileScanner(
                    music_dir=scan_target,
                    catalog=catalog,
                    full_scan=full_scan,
                    threads=threads,
                    cache_manager=self.cache
                )
                local_tracks.extend(scanner.scan(progress=progress, task_id=scan_task))

            # 2. Navidrome Scan (if configured)
            nav_url = navidrome_url or Config.NAVIDROME_URL
            nav_user = navidrome_user or Config.NAVIDROME_USER
            nav_pass = navidrome_password or Config.NAVIDROME_TOKEN
            if nav_url and nav_user and nav_pass:
                nav_scanner = NavidromeScanner(
                    base_url=nav_url,
                    username=nav_user,
                    password=nav_pass,
                    catalog=catalog
                )
                if nav_scanner.test_connection():
                    local_tracks.extend(nav_scanner.scan(progress=progress, task_id=scan_task))

            progress.update(scan_task, description="[green]✔ Library scan completed", completed=1, total=1)

        # 3. Reconcile
        reconciler = DiscographyReconciler(catalog, local_tracks)
        found_items, missing_items = reconciler.reconcile()

        return catalog, found_items, missing_items

    @staticmethod
    def render_report(
        catalog: ArtistCatalog,
        found_items: List[Dict[str, Any]],
        missing_items: List[Dict[str, Any]],
        only_missing: bool = False,
        only_found: bool = False,
        verbose: bool = False
    ) -> None:
        """Renders rich terminal tables, release breakdowns, and completion scorecards."""
        total_tracks = len(catalog.tracks)
        found_count = len(found_items)
        missing_count = len(missing_items)
        completion_pct = (found_count / total_tracks * 100) if total_tracks > 0 else 100.0

        # Header
        header_text = Text()
        header_text.append(f"Artist: {catalog.name}\n", style="bold green")
        if catalog.sort_name and catalog.sort_name.lower() != catalog.name.lower():
            header_text.append(f"Sort Name: {catalog.sort_name}\n", style="dim")
        header_text.append(f"MBID: {catalog.mbid}\n", style="cyan")
        header_text.append(f"MusicBrainz URL: https://musicbrainz.org/artist/{catalog.mbid}\n", style="blue underline")

        alias_sample = list(catalog.aliases)[:8]
        alias_str = ", ".join(alias_sample)
        if len(catalog.aliases) > 8:
            alias_str += f" (+{len(catalog.aliases)-8} more)"
        header_text.append(f"Aliases & Spellings ({len(catalog.aliases)}): {alias_str}\n", style="italic magenta")

        if catalog.bandcamp_urls:
            header_text.append(f"Bandcamp: {' | '.join(catalog.bandcamp_urls)}", style="bold cyan")

        console.print(Panel(header_text, title="[bold]MusicBrainz Discography Audit[/bold]", border_style="green", box=box.ROUNDED))

        # Releases breakdown
        releases_map: Dict[str, Dict[str, Any]] = {}
        for item in found_items:
            mb = item["mb_track"]
            rel = mb["release_title"] or "Standalone / Other"
            if rel not in releases_map:
                releases_map[rel] = {"release_type": mb["release_type"], "date": mb["date"], "found": [], "missing": []}
            releases_map[rel]["found"].append(item)

        for item in missing_items:
            mb = item["mb_track"]
            rel = mb["release_title"] or "Standalone / Other"
            if rel not in releases_map:
                releases_map[rel] = {"release_type": mb["release_type"], "date": mb["date"], "found": [], "missing": []}
            releases_map[rel]["missing"].append(item)

        rel_table = Table(title="Discography Release Overview", box=box.ROUNDED, show_lines=False, header_style="bold cyan")
        rel_table.add_column("Release Title", style="bold white", min_width=30)
        rel_table.add_column("Type", style="yellow", justify="left")
        rel_table.add_column("Date", style="dim", justify="center")
        rel_table.add_column("Status", justify="center")
        rel_table.add_column("Found / Total", justify="right")

        for rel_title, rinfo in sorted(releases_map.items(), key=lambda x: (x[1]["date"] or "9999", x[0])):
            found_n = len(rinfo["found"])
            missing_n = len(rinfo["missing"])
            total_n = found_n + missing_n
            if missing_n == 0:
                status = "[green]✔ Complete[/green]"
            elif found_n == 0:
                status = "[red]✖ Missing[/red]"
            else:
                status = f"[yellow]⚠ Partial ({found_n}/{total_n})[/yellow]"

            rel_table.add_row(rel_title, rinfo["release_type"], rinfo["date"] or "-", status, f"{found_n} / {total_n}")

        console.print(rel_table)

        # Missing table
        if not only_found and missing_items:
            console.print("\n[bold red]── Missing Tracks Checklist ──────────────────────────────────────────[/bold red]")
            missing_table = Table(box=box.SIMPLE, show_lines=False, header_style="bold red")
            missing_table.add_column("#", style="dim", width=4, justify="right")
            missing_table.add_column("Track Title", style="bold red", min_width=25)
            missing_table.add_column("Artist Credit", style="magenta")
            missing_table.add_column("Release / Album", style="white")
            missing_table.add_column("Year", style="dim", justify="center")

            for item in missing_items:
                mb = item["mb_track"]
                missing_table.add_row(
                    mb["track_number"] or "-",
                    mb["title"],
                    mb["artist_credit"],
                    mb["release_title"] or "(Standalone)",
                    mb["date"][:4] if mb["date"] else "-"
                )
            console.print(missing_table)

        # Summary scorecard
        score_text = Text()
        score_text.append(f"Total Tracks in Discography: ", style="bold")
        score_text.append(f"{total_tracks}\n", style="bold cyan")
        score_text.append(f"Found in Local Library:       ", style="bold")
        score_text.append(f"{found_count} tracks\n", style="bold green")
        score_text.append(f"Missing from Local Library:   ", style="bold")
        score_text.append(f"{missing_count} tracks\n", style="bold red" if missing_count > 0 else "bold green")
        score_text.append(f"Library Completion Rate:      ", style="bold")
        score_color = "green" if completion_pct >= 90 else ("yellow" if completion_pct >= 60 else "red")
        score_text.append(f"{completion_pct:.1f}%\n", style=f"bold {score_color}")

        console.print(Panel(score_text, title="[bold]Summary Scorecard[/bold]", border_style="cyan", box=box.ROUNDED))

    @staticmethod
    def export_reports(
        catalog: ArtistCatalog,
        found_items: List[Dict[str, Any]],
        missing_items: List[Dict[str, Any]],
        json_path: Optional[Path] = None,
        txt_path: Optional[Path] = None,
        csv_path: Optional[Path] = None,
        bandcamp_path: Optional[Path] = None
    ) -> None:
        """Exports audit data to JSON, TXT, CSV, or Bandcamp links files."""
        if json_path:
            report_data = {
                "artist": {
                    "name": catalog.name,
                    "sort_name": catalog.sort_name,
                    "mbid": catalog.mbid,
                    "aliases": list(catalog.aliases),
                    "url": f"https://musicbrainz.org/artist/{catalog.mbid}"
                },
                "summary": {
                    "total_tracks": len(catalog.tracks),
                    "found_tracks": len(found_items),
                    "missing_tracks": len(missing_items),
                    "completion_percentage": round((len(found_items) / len(catalog.tracks) * 100) if catalog.tracks else 100.0, 2)
                },
                "missing": [
                    {
                        "title": item["mb_track"]["title"],
                        "artist_credit": item["mb_track"]["artist_credit"],
                        "release": item["mb_track"]["release_title"],
                        "track_number": item["mb_track"]["track_number"],
                        "date": item["mb_track"]["date"],
                        "recording_ids": list(item["mb_track"].get("recording_ids", []))
                    }
                    for item in missing_items
                ],
                "found": [
                    {
                        "title": item["mb_track"]["title"],
                        "release": item["mb_track"]["release_title"],
                        "local_path": item["local_track"]["path"],
                        "match_method": item["match_method"]
                    }
                    for item in found_items
                ]
            }
            BaseReportExporter.export_json(report_data, json_path)

        if txt_path:
            lines = []
            for item in missing_items:
                mb = item["mb_track"]
                tr = f"#{mb['track_number']} " if mb['track_number'] else ""
                rel = f" (Release: {mb['release_title']})" if mb['release_title'] else ""
                lines.append(f"{mb['artist_credit']} - {tr}{mb['title']}{rel}")
            BaseReportExporter.export_text(lines, txt_path, header_title=f"Missing Tracks for {catalog.name}")

        if csv_path:
            headers = ["Status", "Track Title", "Artist Credit", "Release", "Track Number", "Release Date", "Local Path", "Match Method"]
            rows = []
            for item in found_items:
                mb = item["mb_track"]
                lt = item["local_track"]
                rows.append(["FOUND", mb["title"], mb["artist_credit"], mb["release_title"], mb["track_number"], mb["date"], lt["path"], item["match_method"]])
            for item in missing_items:
                mb = item["mb_track"]
                rows.append(["MISSING", mb["title"], mb["artist_credit"], mb["release_title"], mb["track_number"], mb["date"], "", ""])
            BaseReportExporter.export_csv(headers, rows, csv_path)

        if bandcamp_path:
            urls = list(catalog.bandcamp_urls)
            BaseReportExporter.export_text(urls or ["# No official Bandcamp URLs linked in MusicBrainz"], bandcamp_path, header_title=f"Bandcamp Links for {catalog.name}")
