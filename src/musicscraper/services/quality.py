"""
Automated audio quality scanner and Soulseek/slskd lossless/320k upgrader.
"""

import os
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich import box

from musicscraper.config import Config
from musicscraper.core.constants import AUDIO_EXTENSIONS
from musicscraper.core.text import normalize_text, calculate_similarity, parse_track_title_structure, are_versions_compatible
from musicscraper.core.audio import AudioMetadata, AudioQualityAnalyzer
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.report import console
from musicscraper.clients.slskd import SlskdClient


class QualityUpgradeCandidate:
    """Represents a local audio track eligible for quality upgrade."""

    def __init__(self, local_meta: AudioMetadata, target_quality: str = "flac"):
        self.meta = local_meta
        self.target_quality = target_quality.lower()
        self.current_score = local_meta.quality_score
        self.current_label = f"{local_meta.format.upper()} ({local_meta.bitrate}kbps)" if local_meta.bitrate else local_meta.format.upper()
        self.matched_remote: Optional[Dict[str, Any]] = None


class LocalLibraryQualityScanner:
    """Scans local music directory to find files below target quality."""

    def __init__(
        self,
        library_dir: Path,
        target_format: str = "flac",
        min_bitrate: int = 320,
        cache_manager: Optional[UnifiedCacheManager] = None,
        threads: int = 16
    ):
        self.library_dir = Path(library_dir).resolve()
        self.target_format = target_format.lower()
        self.min_bitrate = min_bitrate
        self.cache = cache_manager or UnifiedCacheManager()
        self.threads = threads

    def scan(self, artist_filter: Optional[str] = None) -> List[QualityUpgradeCandidate]:
        """Discovers audio files that qualify for upgrade."""
        if not self.library_dir.exists():
            return []

        audio_files: List[Path] = []
        for root, _, files in os.walk(self.library_dir):
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in AUDIO_EXTENSIONS:
                    if artist_filter and normalize_text(artist_filter) not in normalize_text(str(p)):
                        continue
                    audio_files.append(p)

        candidates: List[QualityUpgradeCandidate] = []

        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            for meta in pool.map(AudioQualityAnalyzer.analyze_file, audio_files):
                if not meta:
                    continue
                self.cache.store_audio_metadata(meta)

                # Qualification check
                needs_upgrade = False
                if self.target_format == "flac":
                    if not meta.is_lossless:
                        needs_upgrade = True
                elif self.target_format == "mp3-320":
                    if not meta.is_lossless and (meta.bitrate or 0) < self.min_bitrate:
                        needs_upgrade = True

                if needs_upgrade:
                    candidates.append(QualityUpgradeCandidate(meta, self.target_format))

        return candidates


class SoulseekQualityUpgrader:
    """Orchestrates Soulseek searching and replacement queueing for low-quality tracks."""

    def __init__(
        self,
        slskd_client: Optional[SlskdClient] = None,
        preferred_format: str = "flac",
        dry_run: bool = False,
        search_timeout: float = 25.0
    ):
        self.client = slskd_client or SlskdClient()
        self.preferred_format = preferred_format.lower()
        self.dry_run = dry_run
        self.search_timeout = search_timeout

    def upgrade_candidates(self, candidates: List[QualityUpgradeCandidate]) -> List[QualityUpgradeCandidate]:
        """Searches Soulseek for high-res replacements and queues them."""
        if not candidates:
            console.print("[green]All tracks are already at or above target quality![/green]")
            return []

        console.print(Panel(
            f"[bold cyan]Found {len(candidates)} tracks eligible for quality upgrade.[/bold cyan]\n"
            f"[dim]Target Quality: {self.preferred_format.upper()} | Dry-Run: {self.dry_run}[/dim]",
            title="[bold]Quality Upgrade Pipeline[/bold]",
            border_style="cyan",
            box=box.ROUNDED
        ))

        queries = []
        cand_by_query: Dict[str, QualityUpgradeCandidate] = {}
        for c in candidates:
            artist = c.meta.artist or c.meta.album_artist
            title = c.meta.title or c.meta.path.stem
            q = f"{artist} {title}".strip()
            queries.append(q)
            cand_by_query[q] = c

        search_results = self.client.batch_search(queries, timeout=self.search_timeout)
        upgraded: List[QualityUpgradeCandidate] = []

        for q, res in search_results.items():
            cand = cand_by_query.get(q)
            if not cand:
                continue

            responses = res.get("responses", [])
            matched_files: List[Dict[str, Any]] = []

            for resp in responses:
                user = resp.get("username")
                for f in resp.get("files", []):
                    fn = f.get("filename", "")
                    ext = Path(fn).suffix.lower()
                    if ext not in AUDIO_EXTENSIONS or f.get("isLocked"):
                        continue

                    label, score = AudioQualityAnalyzer.determine_stream_quality(f)
                    if score > cand.current_score:
                        # Verify track title similarity
                        cand_struct = parse_track_title_structure(cand.meta.title or cand.meta.path.stem)
                        rem_struct = parse_track_title_structure(Path(fn).name)
                        if are_versions_compatible(cand_struct["version_type"], cand_struct["version_text"], rem_struct["version_type"], rem_struct["version_text"]):
                            sim = calculate_similarity(cand_struct["base_norm"], rem_struct["base_norm"])
                            if sim >= 0.85:
                                f_copy = dict(f)
                                f_copy["user"] = user
                                f_copy["quality_label"] = label
                                f_copy["quality_score"] = score
                                matched_files.append(f_copy)

            if matched_files:
                best = max(matched_files, key=lambda x: x["quality_score"])
                cand.matched_remote = best
                upgraded.append(cand)
                if not self.dry_run:
                    try:
                        self.client.enqueue_download(best["user"], [best])
                    except Exception:
                        pass

        # Results table
        table = Table(title="Quality Upgrade Summary", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("Track", style="bold white", min_width=25)
        table.add_column("Current Quality", style="dim red")
        table.add_column("Upgraded Quality", style="bold green")
        table.add_column("Peer", style="cyan")
        table.add_column("Status", justify="center")

        for c in upgraded:
            rem = c.matched_remote
            status = "[green]✔ Enqueued[/green]" if not self.dry_run else "[yellow]Available[/yellow]"
            table.add_row(
                c.meta.path.name,
                c.current_label,
                rem["quality_label"],
                rem["user"],
                status
            )

        if upgraded:
            console.print(table)

        return upgraded
