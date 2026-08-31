"""
Unified artist download orchestrator combining MusicBrainz auditing, Bandcamp, and Soulseek.
"""

from pathlib import Path
from typing import Dict, List, Optional, Any

from rich.panel import Panel
from rich.text import Text
from rich import box

from musicscraper.config import Config
from musicscraper.core.report import console
from musicscraper.clients.slskd import SlskdClient
from musicscraper.clients.musicbrainz import MusicBrainzClient
from musicscraper.scrapers.bandcamp import BandcampEngine
from musicscraper.services.auditor import AuditorService
from musicscraper.services.soulseek import SlskdArtistScraper


class ArtistDownloadOrchestrator:
    """End-to-end multi-source artist discography downloader."""

    def __init__(
        self,
        artist_query: str,
        output_dir: Optional[Path] = None,
        library_dir: Optional[Path] = None,
        preferred_format: str = "flac",
        dry_run: bool = False,
        use_bandcamp: bool = True,
        use_soulseek: bool = True,
        email: Optional[str] = None,
        search_timeout: float = 25.0
    ):
        self.artist_query = artist_query.strip()
        self.output_dir = Path(output_dir or Config.DEFAULT_OUTPUT_DIR).resolve()
        self.library_dir = Path(library_dir or Config.DEFAULT_LIBRARY_DIR).resolve()
        self.preferred_format = preferred_format.lower()
        self.dry_run = dry_run
        self.use_bandcamp = use_bandcamp
        self.use_soulseek = use_soulseek
        self.email = email or Config.BANDCAMP_EMAIL
        self.search_timeout = search_timeout

        self.auditor = AuditorService()
        self.bc_engine = BandcampEngine(
            output_dir=self.output_dir,
            audio_format="mp3-320" if self.preferred_format == "mp3-320" else "flac",
            email=self.email
        )

    def run(self) -> Dict[str, Any]:
        """Runs the multi-source workflow: Audit -> Bandcamp -> Soulseek."""
        console.print(Panel(
            Text.from_markup(
                f"[bold cyan]End-to-End Artist Discography Downloader[/bold cyan]\n"
                f"[dim]Artist:[/dim] [bold]{self.artist_query}[/bold]\n"
                f"[dim]Output Directory:[/dim] {self.output_dir}\n"
                f"[dim]Preferred Format:[/dim] {self.preferred_format.upper()}\n"
                f"[dim]Sources Enabled:[/dim] Bandcamp: {'Yes' if self.use_bandcamp else 'No'} | Soulseek: {'Yes' if self.use_soulseek else 'No'}\n"
                f"[dim]Dry-Run:[/dim] {'Yes' if self.dry_run else 'No'}"
            ),
            title="[bold]Artist Download Orchestrator[/bold]",
            border_style="cyan",
            box=box.ROUNDED
        ))

        # Step 1: Audit artist
        catalog, found_items, missing_items = self.auditor.audit_artist(
            artist_query=self.artist_query,
            music_dir=self.library_dir
        )

        self.auditor.render_report(catalog, found_items, missing_items)

        results = {
            "artist": catalog.name,
            "mbid": catalog.mbid,
            "total_tracks": len(catalog.tracks),
            "initially_found": len(found_items),
            "initially_missing": len(missing_items),
            "bandcamp_downloads": 0,
            "soulseek_queued": 0
        }

        if not missing_items:
            console.print(f"[bold green]✔ Discography for {catalog.name} is already 100% complete![/bold green]")
            return results

        # Step 2: Bandcamp downloads
        if self.use_bandcamp and catalog.bandcamp_urls:
            console.print(f"\n[bold cyan]Step 2: Checking official Bandcamp releases...[/bold cyan]")
            for bc_url in catalog.bandcamp_urls:
                try:
                    rel_urls = self.bc_engine.get_artist_release_urls(bc_url)
                    for r_url in rel_urls:
                        meta = self.bc_engine.get_release_metadata(r_url)
                        if meta and (meta.get("is_free") or meta.get("is_nyp")):
                            if not self.dry_run:
                                ok = self.bc_engine.download_release(meta)
                                if ok:
                                    results["bandcamp_downloads"] += 1
                except Exception as e:
                    console.print(f"[yellow]Bandcamp crawl error for {bc_url}: {e}[/yellow]")

        # Step 3: Soulseek discovery and queueing
        if self.use_soulseek:
            console.print(f"\n[bold cyan]Step 3: Searching Soulseek for missing discography items...[/bold cyan]")
            try:
                slsk_scraper = SlskdArtistScraper(
                    artist_query=catalog.name,
                    music_dir=self.library_dir,
                    preferred_format=self.preferred_format,
                    search_timeout=self.search_timeout,
                    dry_run=self.dry_run
                )
                slsk_res = slsk_scraper.run()
                results["soulseek_queued"] = len(slsk_res.get("queued_directories", []))
            except Exception as e:
                console.print(f"[yellow]Soulseek scraper error: {e}[/yellow]")

        return results
