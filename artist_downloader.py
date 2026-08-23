#!/usr/bin/env python3
"""
MusicBrainz Artist Downloader & Discography Auditor
===================================================
Automates the discovery, downloading, archive extraction, and auditing of an artist's
entire discography from MusicBrainz.

Workflow:
1. Resolves artist by Name, MBID, or MusicBrainz URL.
2. Queries MusicBrainz for full discography: primary releases, compilation/VA tracks,
   release groups, and direct recordings.
3. Harvests download endpoints across Bandcamp, MediaFire, Archive.org, and Netlabel sites.
4. Downloads all releases/tracks using the toolkit's native downloaders (FLAC/MP3-320/WAV/streams).
5. Safely extracts downloaded archives (.zip, .tar.gz, etc.) into structured album directories.
6. Reconciles local audio files against MusicBrainz to verify downloaded tracks.
7. Produces a comprehensive missing-track report and exports Markdown, JSON, TXT, and CSV reports.

SAFETY GUARANTEE:
- All file write/download/extraction operations occur exclusively in the project's output directory.
- Any external library directories (e.g. /mnt/music) are treated strictly as READ-ONLY for scanning.
"""

import os
import sys
import re
import csv
import json
import time
import logging
import zipfile
import tarfile
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Set, Tuple, Optional, Any

import requests
from bs4 import BeautifulSoup
from unidecode import unidecode
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.text import Text
from rich import box

# Silence noisy third-party loggers (e.g. musicbrainzngs schema parsing notices, urllib3)
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

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
from bandcamp_scraper import BandcampEngine, SUPPORTED_FORMATS, FilenameUtils
from music_scraper import (
    MediaFireResolver,
    ArchiveOrgResolver,
    UniversalLinkResolver,
    MusicDownloader,
    DEFAULT_USER_AGENT,
    IMAGE_EXTENSIONS,
    is_image_url_or_filename,
    is_audio_or_archive_url_or_filename,
)
from slskd_scraper import SlskdArtistScraper
from slskd_api import SlskdClient

# Initialize Rich Console
console = Console()

DEFAULT_OUTPUT_DIR = "/mnt/music/downloads" if os.path.exists("/mnt/music/downloads") else "./downloads"

# Domain normalizations (e.g. legacy/moved netlabels)
DOMAIN_REPLACEMENTS = [
    (r"http://dochakuso\.web\.fc2\.com/release/(dcks-\d+\.html)", r"https://dochakuso.net/release/\1"),
    (r"http://dochakuso\.web\.fc2\.com/?", r"https://dochakuso.net/release.html"),
    (r"https?://jw-records\.bandcamp\.com", r"https://jwrecords.bandcamp.com"),
]


class ArchiveExtractor:
    """Safely unpacks downloaded music archives into organized directories."""

    @staticmethod
    def extract_zip(zip_path: Path, target_dir: Path) -> List[Path]:
        """Extracts a zip file with UTF-8 / CP932 / Latin-1 filename sanitization."""
        extracted_files: List[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    # Handle encoding quirks (Japanese zip files on Windows/Linux)
                    filename = member.filename
                    try:
                        filename = filename.encode("cp437").decode("utf-8")
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        try:
                            filename = filename.encode("cp437").decode("cp932")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            pass

                    # Prevent directory traversal attacks
                    clean_name = os.path.normpath(filename).lstrip(os.sep + "/")
                    if ".." in clean_name.split(os.sep):
                        continue

                    dest_file = target_dir / clean_name
                    if member.is_dir():
                        dest_file.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest_file, "wb") as dst:
                            dst.write(src.read())
                        extracted_files.append(dest_file)
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to extract {zip_path.name}: {e}[/yellow]")
        return extracted_files

    @staticmethod
    def extract_tar(tar_path: Path, target_dir: Path) -> List[Path]:
        """Extracts tar/tar.gz files safely."""
        extracted_files: List[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tar_path, "r:*") as tf:
                for member in tf.getmembers():
                    clean_name = os.path.normpath(member.name).lstrip(os.sep + "/")
                    if ".." in clean_name.split(os.sep):
                        continue
                    dest_file = target_dir / clean_name
                    if member.isdir():
                        dest_file.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        f = tf.extractfile(member)
                        if f:
                            with open(dest_file, "wb") as dst:
                                dst.write(f.read())
                            extracted_files.append(dest_file)
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to extract {tar_path.name}: {e}[/yellow]")
        return extracted_files

    @classmethod
    def unpack_all_archives(cls, directory: Path) -> int:
        """Finds all archives in directory and extracts them into subdirectories."""
        extracted_count = 0
        for root, _, files in os.walk(directory):
            for f in files:
                f_path = Path(root) / f
                if f.endswith(".zip"):
                    sub_dir = f_path.parent / f_path.stem
                    cls.extract_zip(f_path, sub_dir)
                    extracted_count += 1
                elif f.endswith((".tar.gz", ".tgz", ".tar.bz2")):
                    stem = f_path.stem.replace(".tar", "")
                    sub_dir = f_path.parent / stem
                    cls.extract_tar(f_path, sub_dir)
                    extracted_count += 1
        return extracted_count


class ArtistDownloadOrchestrator:
    """
    Coordinates MusicBrainz metadata fetching, endpoint discovery, parallel downloading,
    archive extraction, and library reconciliation.
    """

    def __init__(
        self,
        artist_query: str,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        music_dir: Optional[str] = None,
        audio_format: str = "mp3-320",
        fallback: bool = True,
        threads: int = 4,
        overwrite: bool = False,
        dry_run: bool = False,
        cache_dir: str = DEFAULT_CACHE_DIR,
        bandcamp_email: Optional[str] = None,
        slskd: bool = False,
        slskd_format: str = "flac",
        slskd_singles_only: bool = False,
    ):
        self.artist_query = artist_query.strip()
        self.output_dir = Path(output_dir).resolve()
        
        # Auto-detect server music library if not explicitly provided
        if music_dir:
            self.music_dir = Path(music_dir).resolve()
        else:
            self.music_dir = None
            for cand in [Path("/mnt/music/Library"), Path("/mnt/music"), Path("/mnt/library"), Path.home() / "Music"]:
                if cand.exists() and any(cand.iterdir()):
                    self.music_dir = cand
                    break

        self.audio_format = audio_format.lower()
        self.fallback = fallback
        self.threads = threads
        self.overwrite = overwrite
        self.dry_run = dry_run
        self.cache_dir = cache_dir
        self.bandcamp_email = bandcamp_email or os.environ.get("BANDCAMP_EMAIL")
        self.slskd = slskd
        self.slskd_format = slskd_format
        self.slskd_singles_only = slskd_singles_only
        self.slskd_results: Optional[Dict[str, Any]] = None
        self.slskd_found_releases: Set[str] = set()
        self.slskd_found_tracks: Dict[str, Dict[str, Any]] = {}

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

        self.mb_client = MusicBrainzClient(cache_dir=self.cache_dir)
        self.catalog: Optional[ArtistCatalog] = None
        self.artist_output_dir: Optional[Path] = None

        # Discovered endpoints
        self.bandcamp_release_urls: Set[str] = set()
        self.bandcamp_artist_urls: Set[str] = set()
        self.mediafire_urls: Set[str] = set()
        self.archive_urls: Set[str] = set()
        self.direct_urls: Set[str] = set()
        self.web_release_urls: Set[str] = set()

        # Server library state (Read-Only pre-scan)
        self.server_tracks: List[Dict[str, Any]] = []
        self.server_found_map: Dict[str, Dict[str, Any]] = {} # norm_title -> local_track
        self.server_found_rec_ids: Set[str] = set()
        self.server_found_releases: Set[str] = set() # norm_release
        self.server_dcks_codes: Set[str] = set()

        # Structured download status tracking
        self.successful_downloads: List[Dict[str, Any]] = []
        self.skipped_downloads: List[Dict[str, Any]] = []
        self.failed_downloads: List[Dict[str, Any]] = []
        self.unresolved_promo_urls: List[Dict[str, Any]] = []
        self.download_results: List[Dict[str, Any]] = []

    def run(self) -> Dict[str, Any]:
        """Executes the full discovery -> download -> unpack -> reconcile pipeline."""
        console.print(Panel.fit(
            f"[bold cyan]MusicBrainz Artist Downloader & Auditor[/bold cyan]\n"
            f"[dim]Target: {self.artist_query}[/dim]",
            border_style="cyan"
        ))

        # 1. Resolve Artist & Fetch Catalog
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(self.artist_query)
        raw_mb_data = self.mb_client.fetch_full_discography(mbid)
        self.catalog = ArtistCatalog(raw_mb_data)

        safe_artist = FilenameUtils.sanitize(self.catalog.name)
        self.artist_output_dir = self.output_dir / safe_artist
        if not self.dry_run:
            self.artist_output_dir.mkdir(parents=True, exist_ok=True)

        console.print(f"[green]✔ Canonical Name:[/green] [bold]{self.catalog.name}[/bold] (MBID: {self.catalog.mbid})")
        console.print(f"[dim]Total Catalog Tracks/Recordings: {len(self.catalog.tracks)} | Primary Releases: {len(raw_mb_data.get('releases_artist', []))} | Compilations/VA: {len(raw_mb_data.get('releases_track_artist', []))}[/dim]")

        # 2. Pre-Scan Server Library (Read-Only) to identify existing tracks/releases
        self._prescan_server_library()

        # 3. PRIORITY SOURCE: Soulseek (slskd) Search & Verification (Runs FIRST)
        if self.slskd:
            self._execute_slskd_discovery()

        # 4. FALLBACK SOURCES: Discover Bandcamp & Web Endpoints (only for remaining missing items)
        self._discover_endpoints()

        # 5. Download Discovered Fallback Releases (Skipping items satisfied on Server / Soulseek)
        if not self.dry_run:
            self._execute_downloads()
            # 6. Unpack archives inside project output dir
            self._unpack_archives()
        else:
            console.print("\n[yellow]--dry-run enabled: Skipping downloads and archive unpacking.[/yellow]")

        # 7. Audit Library vs MusicBrainz
        audit_results = self._audit_library()

        # 8. Print Report & Export
        self._generate_reports(audit_results)

        return {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "output_dir": str(self.artist_output_dir),
            "audit_results": audit_results,
            "download_results": self.download_results,
            "successful_downloads": self.successful_downloads,
            "skipped_downloads": self.skipped_downloads,
            "failed_downloads": self.failed_downloads,
            "unresolved_promo_urls": self.unresolved_promo_urls,
        }

    def _execute_slskd_discovery(self):
        """
        Executes Soulseek searches via slskd as the FIRST and highest-priority download source.
        Verified matching directories are queued in slskd, and their releases/tracks are registered
        so Bandcamp and web mirrors only process remaining unresolved items.
        """
        console.print(f"\n[bold cyan]Stage 1: Searching Soulseek (slskd) for discography (Priority Source)...[/bold cyan]")
        try:
            slskd_scraper = SlskdArtistScraper(
                artist_query=self.artist_query,
                music_dir=str(self.music_dir) if self.music_dir else None,
                preferred_format=self.slskd_format,
                dry_run=self.dry_run,
                singles_only=self.slskd_singles_only,
                cache_dir=self.cache_dir,
                threads=self.threads,
            )
            self.slskd_results = slskd_scraper.run()

            # Register verified primary releases
            for r in self.slskd_results.get("verified_releases", []):
                rel_title = r.get("release_title", "")
                norm_rel = normalize_text(rel_title)
                self.slskd_found_releases.add(norm_rel)
                bm = r.get("best_match", {})
                for mt in bm.get("matched_tracks", []):
                    exp_t = mt.get("expected", "")
                    norm_t = normalize_text(exp_t)
                    self.slskd_found_tracks[norm_t] = {
                        "source": "Soulseek (slskd)",
                        "peer": bm.get("user"),
                        "directory": bm.get("directory"),
                        "format": bm.get("format_label")
                    }

            # Register verified compilation tracks
            for c in self.slskd_results.get("verified_compilation_tracks", []):
                rel_title = c.get("release_title", "")
                track_title = c.get("track_title", "")
                norm_rel = normalize_text(rel_title)
                norm_t = normalize_text(track_title)
                self.slskd_found_releases.add(norm_rel)
                bm = c.get("best_match", {})
                self.slskd_found_tracks[norm_t] = {
                    "source": "Soulseek (slskd)",
                    "peer": bm.get("user"),
                    "directory": bm.get("directory"),
                    "format": bm.get("format_label")
                }

            # Register verified standalone tracks
            for s in self.slskd_results.get("verified_standalone_tracks", []):
                track_title = s.get("track_title", "")
                norm_t = normalize_text(track_title)
                bm = s.get("best_match", {})
                self.slskd_found_tracks[norm_t] = {
                    "source": "Soulseek (slskd)",
                    "peer": bm.get("user"),
                    "directory": bm.get("directory"),
                    "format": bm.get("format_label")
                }

            queued_count = len(self.slskd_results.get("queued_directories", []))
            verified_count = (
                len(self.slskd_results.get("verified_releases", [])) +
                len(self.slskd_results.get("verified_compilation_tracks", [])) +
                len(self.slskd_results.get("verified_standalone_tracks", []))
            )
            console.print(f"[green]✔ Soulseek Priority Complete:[/green] Verified [bold]{verified_count}[/bold] release(s)/track(s) on Soulseek. (Queued: {queued_count})")
            if verified_count > 0:
                console.print(f"[dim]Bandcamp and web scrapers will now only target remaining missing releases.[/dim]")

        except Exception as e:
            console.print(f"[bold red]slskd priority discovery error:[/bold red] {e}")

    def _prescan_server_library(self):
        """Scans the existing server library (Read-Only) to detect tracks and releases already present."""
        if not self.music_dir or not self.music_dir.exists():
            return

        console.print(f"\n[cyan]Pre-scanning server music library at {self.music_dir} (Read-Only)...[/cyan]")
        scanner = AudioFileScanner(
            music_dir=str(self.music_dir),
            catalog=self.catalog,
            full_scan=False,
            threads=self.threads
        )
        self.server_tracks = scanner.scan()

        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=self.server_tracks)
        found_items, _ = reconciler.reconcile()

        for item in found_items:
            mb = item["mb_track"]
            lt = item["local_track"]
            norm_t = mb.get("norm_title", "")
            if norm_t:
                self.server_found_map[norm_t] = lt
            for rid in mb.get("recording_ids", []):
                self.server_found_rec_ids.add(rid)

        # Check for DCKS codes in server directory structure
        for root, dirs, _ in os.walk(self.music_dir):
            for d in dirs:
                m = re.search(r"dcks-\d+", d, re.IGNORECASE)
                if m:
                    self.server_dcks_codes.add(m.group(0).lower())

        # Check which releases are already fully satisfied on server
        for rel in self.catalog.releases:
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            # Find all catalog tracks belonging to this release
            rel_tracks = [t for t in self.catalog.tracks if t.get("norm_release") == norm_rel or rel_title in t.get("all_releases", set())]
            if rel_tracks and all(t.get("norm_title") in self.server_found_map or any(r in self.server_found_rec_ids for r in t.get("recording_ids", [])) for t in rel_tracks):
                self.server_found_releases.add(norm_rel)

        console.print(f"[green]✔ Server Library Status:[/green] [bold]{len(found_items)}[/bold] artist tracks / [bold]{len(self.server_found_releases)}[/bold] releases already present on server.")

    def _should_skip_bandcamp_release(self, meta: Dict[str, Any], bc_url: str) -> Tuple[bool, str]:
        """Determines if a Bandcamp release can be skipped because all its artist tracks are on server or queued in slskd."""
        if self.overwrite:
            return False, ""

        title = meta.get("title", "")
        album = meta.get("album", "")
        norm_album = normalize_text(album or title)
        artist = meta.get("artist", "").lower()
        tracks = meta.get("tracks", [])

        # Check if entire release was satisfied on server or Soulseek
        if norm_album in self.server_found_releases:
            return True, f"Release '{album or title}' is already fully present on server library"
        if norm_album in self.slskd_found_releases:
            return True, f"Release '{album or title}' is already verified & queued via Soulseek (slskd)"

        # Check tracks by target artist
        is_target_artist_rel = any(a in artist or a in unidecode(artist) for a in self.catalog.aliases)
        artist_tracks = []
        for t in tracks:
            t_title = t.get("title", "")
            t_artist = t.get("artist", "").lower()
            if is_target_artist_rel or any(a in t_artist or a in t_title.lower() for a in self.catalog.aliases):
                artist_tracks.append(t)

        if not artist_tracks:
            # If no track explicitly matched artist name but this release was linked to artist
            artist_tracks = tracks

        if artist_tracks:
            all_present = True
            for t in artist_tracks:
                norm_t = normalize_text(t.get("title", ""))
                clean_t = strip_track_number_and_artist(t.get("title", ""))
                norm_clean = normalize_text(clean_t)

                track_on_server = (
                    norm_t in self.server_found_map or
                    norm_clean in self.server_found_map or
                    any(norm_clean == k or norm_clean in k for k in self.server_found_map)
                )
                track_on_slsk = (
                    norm_t in self.slskd_found_tracks or
                    norm_clean in self.slskd_found_tracks or
                    any(norm_clean == k or norm_clean in k for k in self.slskd_found_tracks)
                )
                if not (track_on_server or track_on_slsk):
                    all_present = False
                    break

            if all_present:
                return True, f"All {len(artist_tracks)} artist track(s) from '{title}' are already satisfied (Server / Soulseek)"

        return False, ""

    def _should_skip_archive_item(self, item: Dict[str, str]) -> Tuple[bool, str]:
        """Determines if an archive or direct download item can be skipped."""
        if self.overwrite:
            return False, ""

        url = item.get("download_url", "")
        title = item.get("title", "")
        combined = f"{url} {title}".lower()

        # Check DCKS catalog codes (e.g. DCKS-0054, DCKS-0064)
        m = re.search(r"dcks-\d+", combined)
        if m:
            code = m.group(0).lower()
            if code in self.server_dcks_codes:
                rel_tracks = [t for t in self.catalog.tracks if code in t.get("norm_release", "") or code in normalize_text(t.get("release_title", ""))]
                if not rel_tracks or all(t.get("norm_title") in self.server_found_map for t in rel_tracks):
                    return True, f"Catalog code [{code.upper()}] is already present on server library"

        # Check if matching release tracks are already satisfied on server or Soulseek
        for rel in self.catalog.releases:
            norm_rel = normalize_text(rel.get("title", ""))
            if norm_rel and norm_rel in normalize_text(combined):
                if norm_rel in self.server_found_releases:
                    return True, f"Release '{rel.get('title')}' is already on server library"
                if norm_rel in self.slskd_found_releases:
                    return True, f"Release '{rel.get('title')}' is already verified & queued via Soulseek (slskd)"

        return False, ""

    def _discover_endpoints(self):
        """Scans MusicBrainz URL relations and web release pages for downloadable links."""
        console.print("\n[cyan]Stage 1: Discovering release downloads across providers...[/cyan]")

        # Collect URLs from artist relations
        for bc in self.catalog.bandcamp_urls:
            norm_url, target_type = BandcampEngine.normalize_target(bc)
            if target_type == "artist":
                self.bandcamp_artist_urls.add(norm_url)
            else:
                self.bandcamp_release_urls.add(norm_url)

        # Collect URLs from releases & recordings
        for u in self.catalog.all_external_urls:
            self._categorize_url(u)

        # Crawl Bandcamp artist pages to find all albums & tracks
        bc_engine = BandcampEngine(session=self.session)
        for art_url in list(self.bandcamp_artist_urls):
            console.print(f"[dim]Crawling Bandcamp artist discography: {art_url}[/dim]")
            discovered = bc_engine.get_artist_release_urls(art_url)
            for d in discovered:
                self.bandcamp_release_urls.add(d)

        # Crawl web release / netlabel pages (e.g. dochakuso, tumblr, fc2)
        for web_url in list(self.web_release_urls):
            # Apply domain replacements (e.g. dochakuso fc2 -> dochakuso.net)
            fixed_url = web_url
            for pat, repl in DOMAIN_REPLACEMENTS:
                fixed_url = re.sub(pat, repl, fixed_url)

            console.print(f"[dim]Inspecting web release page: {fixed_url}[/dim]")
            self._scrape_web_page(fixed_url)

        # Summary of discovered endpoints
        table = Table(title="Discovered Download Sources", box=box.ROUNDED)
        table.add_column("Provider / Host", style="cyan")
        table.add_column("Count", justify="right", style="green")
        table.add_column("Details", style="dim")

        table.add_row("Bandcamp Albums & Tracks", str(len(self.bandcamp_release_urls)), "FLAC / MP3-320 / Stream Fallback")
        table.add_row("MediaFire Archives", str(len(self.mediafire_urls)), "Direct Zip / Release Archives")
        table.add_row("Archive.org Releases", str(len(self.archive_urls)), "Internet Archive Audio Items")
        table.add_row("Direct Audio / Zip Links", str(len(self.direct_urls)), "Direct CDN / Web Links")
        console.print(table)

    def _categorize_url(self, raw_url: str):
        """Categorizes raw URL into provider sets."""
        if not raw_url or not raw_url.startswith("http"):
            return

        if is_image_url_or_filename(raw_url):
            return

        # Check for Bandcamp (including known custom domains like suckpuck.com)
        if "bandcamp.com" in raw_url or "suckpuck.com" in raw_url:
            norm_url, target_type = BandcampEngine.normalize_target(raw_url)
            if target_type == "artist":
                self.bandcamp_artist_urls.add(norm_url)
            else:
                self.bandcamp_release_urls.add(norm_url)
        elif "mediafire.com" in raw_url:
            if not is_image_url_or_filename(raw_url):
                self.mediafire_urls.add(raw_url)
        elif "archive.org" in raw_url:
            if not is_image_url_or_filename(raw_url):
                self.archive_urls.add(raw_url)
        elif any(raw_url.lower().endswith(ext) for ext in AUDIO_EXTENSIONS | {".zip", ".rar", ".7z", ".tar.gz", ".tgz"}):
            self.direct_urls.add(raw_url)
        elif "soundcloud.com" in raw_url:
            self.unresolved_promo_urls.append({
                "title": f"SoundCloud Track ({raw_url.split('/')[-1]})",
                "url": raw_url,
                "provider": "SoundCloud",
                "type": "Audio Stream",
                "reason": "Standalone SoundCloud track (stream-only platform, no direct file download link)",
                "suggestion": "Stream on SoundCloud or download via soundcloud-dl / yt-dlp"
            })
        elif not any(ign in raw_url for ign in ("discogs.com", "rateyourmusic.com", "wikidata.org", "imdb.com", "twitter.com", "instagram.com", "mixcloud.com", "creativecommons.org")):
            self.web_release_urls.add(raw_url)

    def _scrape_web_page(self, url: str):
        """Scrapes an external web page to find download links or Bandcamp players."""
        prev_mf = len(self.mediafire_urls)
        prev_bc = len(self.bandcamp_release_urls)
        prev_ar = len(self.archive_urls)
        prev_dir = len(self.direct_urls)

        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code != 200:
                self.failed_downloads.append({
                    "title": f"Web Page ({url})",
                    "url": url,
                    "provider": "Web / Netlabel",
                    "error_type": f"HTTP {resp.status_code}",
                    "reason": f"Web page returned status code {resp.status_code}",
                    "suggestion": "Check if page moved or check Wayback Machine archive"
                })
                return

            html = resp.content.decode("utf-8", errors="ignore")

            # Check if this page is a custom-domain Bandcamp page
            if "bandcamp.com" in html or "TralbumData" in html or "data-tralbum" in html:
                norm_url, target_type = BandcampEngine.normalize_target(url)
                self.bandcamp_release_urls.add(norm_url)

            # Search for MediaFire, Archive.org, Bandcamp, and direct links
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                full_link = urllib.parse.urljoin(url, href)
                self._categorize_url(full_link)

            # Check for MediaFire links embedded in script / text
            mf_matches = re.findall(r"https?://(?:www\.)?mediafire\.com/[^\s\"\'<>]+", html)
            for mf in mf_matches:
                clean_mf = mf.rstrip(".,;)>'\"]")
                if not is_image_url_or_filename(clean_mf):
                    self.mediafire_urls.add(clean_mf)

            # Check for Archive.org links embedded
            arch_matches = re.findall(r"https?://(?:www\.)?archive\.org/[^\s\"\'<>]+", html)
            for ar in arch_matches:
                clean_ar = ar.rstrip(".,;)>'\"]")
                if not is_image_url_or_filename(clean_ar):
                    self.archive_urls.add(clean_ar)

            # Check if this was a promo page with no downloadable links found
            new_found = (len(self.mediafire_urls) - prev_mf) + (len(self.bandcamp_release_urls) - prev_bc) + (len(self.archive_urls) - prev_ar) + (len(self.direct_urls) - prev_dir)
            if new_found == 0:
                if "tumblr.com" in url or "fc2.com" in url or "wixsite.com" in url:
                    self.unresolved_promo_urls.append({
                        "title": f"Event / Promo Page ({url})",
                        "url": url,
                        "provider": "Web / Netlabel",
                        "type": "Physical CD / Event Promo",
                        "reason": "Event announcement / crossfade promo page with no public direct downloads",
                        "suggestion": "Physical CD sold at M3 / Touhou event; check CD rips or secondary markets"
                    })

        except Exception as e:
            console.print(f"[dim yellow]Could not scrape {url}: {e}[/dim yellow]")
            self.failed_downloads.append({
                "title": f"Web Page ({url})",
                "url": url,
                "provider": "Web / Netlabel",
                "error_type": "Connection / DNS Failure",
                "reason": str(e),
                "suggestion": "Check Internet Archive Wayback Machine or Discogs listing"
            })

    def _execute_downloads(self):
        """Executes downloads across all discovered providers."""
        console.print("\n[cyan]Stage 2: Downloading releases...[/cyan]")

        # 1. Bandcamp Releases
        if self.bandcamp_release_urls:
            console.print(f"[bold cyan]• Processing {len(self.bandcamp_release_urls)} Bandcamp release(s)...[/bold cyan]")
            bc_engine = BandcampEngine(
                output_dir=str(self.artist_output_dir),
                audio_format=self.audio_format,
                fallback=self.fallback,
                max_workers=self.threads,
                overwrite=self.overwrite,
                session=self.session,
                email=self.bandcamp_email
            )

            for bc_url in sorted(self.bandcamp_release_urls):
                try:
                    meta = bc_engine.get_release_metadata(bc_url)
                    if meta:
                        should_skip, reason = self._should_skip_bandcamp_release(meta, bc_url)
                        if should_skip:
                            console.print(f"[dim yellow]⏩ Skipping Bandcamp release (already on server):[/dim yellow] {meta['artist']} - {meta['title']} [dim]({reason})[/dim]")
                            self.skipped_downloads.append({
                                "title": f"{meta['artist']} - {meta['title']}",
                                "url": bc_url,
                                "provider": "Bandcamp",
                                "reason": reason
                            })
                            self.download_results.append({
                                "title": f"{meta['artist']} - {meta['title']}",
                                "provider": "Bandcamp",
                                "url": bc_url,
                                "success": True,
                                "status": "Skipped (Already on server)"
                            })
                            continue

                        console.print(f"[cyan]Downloading Bandcamp release:[/cyan] {meta['artist']} - {meta['title']}")
                        success = bc_engine.download_release(meta)
                        if success:
                            self.successful_downloads.append({
                                "title": f"{meta['artist']} - {meta['title']}",
                                "url": bc_url,
                                "provider": "Bandcamp",
                                "tracks": len(meta.get("tracks", []))
                            })
                        else:
                            self.failed_downloads.append({
                                "title": f"{meta['artist']} - {meta['title']}",
                                "url": bc_url,
                                "provider": "Bandcamp",
                                "error_type": "Download Failed",
                                "reason": "Free download unavailable and stream fallback failed or was disabled",
                                "suggestion": "Purchase release on Bandcamp or check artist mirrors"
                            })
                        self.download_results.append({
                            "title": f"{meta['artist']} - {meta['title']}",
                            "provider": "Bandcamp",
                            "url": bc_url,
                            "success": success
                        })
                    else:
                        console.print(f"[yellow]Warning: Could not fetch Bandcamp metadata for {bc_url}[/yellow]")
                        self.failed_downloads.append({
                            "title": f"Bandcamp Release ({bc_url})",
                            "url": bc_url,
                            "provider": "Bandcamp",
                            "error_type": "Metadata Unavailable / Bot Challenge",
                            "reason": "Bandcamp Cloudflare client challenge, 404 deleted, or private album",
                            "suggestion": "Open URL in a web browser to verify or download directly"
                        })
                        self.download_results.append({
                            "title": bc_url,
                            "provider": "Bandcamp",
                            "url": bc_url,
                            "success": False
                        })
                except Exception as e:
                    console.print(f"[red]Error downloading {bc_url}: {e}[/red]")
                    self.failed_downloads.append({
                        "title": f"Bandcamp Release ({bc_url})",
                        "url": bc_url,
                        "provider": "Bandcamp",
                        "error_type": "Exception",
                        "reason": str(e),
                        "suggestion": "Check network connection and retry"
                    })
                    self.download_results.append({
                        "title": bc_url,
                        "provider": "Bandcamp",
                        "url": bc_url,
                        "success": False,
                        "error": str(e)
                    })

        # 2. MediaFire, Archive.org, and Direct Downloads
        non_bc_items = []
        for mf in self.mediafire_urls:
            non_bc_items.append({"title": f"MediaFire Archive ({mf})", "download_url": mf, "host": "mediafire"})
        for ar in self.archive_urls:
            non_bc_items.append({"title": f"Archive.org Release ({ar})", "download_url": ar, "host": "archive.org"})
        for d in self.direct_urls:
            non_bc_items.append({"title": f"Direct Audio/Zip ({d})", "download_url": d, "host": "direct"})

        if non_bc_items:
            console.print(f"[bold cyan]• Processing {len(non_bc_items)} archive & direct download item(s)...[/bold cyan]")
            archives_dir = self.artist_output_dir / "archives"
            archives_dir.mkdir(parents=True, exist_ok=True)

            downloader = MusicDownloader(
                output_dir=str(archives_dir),
                max_workers=self.threads,
                overwrite=self.overwrite
            )

            unique_items = downloader.deduplicate(non_bc_items)
            for item in unique_items:
                should_skip, reason = self._should_skip_archive_item(item)
                if should_skip:
                    console.print(f"[dim yellow]⏩ Skipping archive download (already on server):[/dim yellow] {item['download_url']} [dim]({reason})[/dim]")
                    self.skipped_downloads.append({
                        "title": item["title"],
                        "url": item["download_url"],
                        "provider": item["host"].capitalize(),
                        "reason": reason
                    })
                    self.download_results.append({
                        "title": item["title"],
                        "provider": item["host"].capitalize(),
                        "url": item["download_url"],
                        "success": True,
                        "status": "Skipped (Already on server)"
                    })
                    continue

                console.print(f"[cyan]Downloading archive:[/cyan] {item['download_url']}")
                try:
                    success = downloader.download_item(item)
                    if success:
                        self.successful_downloads.append({
                            "title": item["title"],
                            "url": item["download_url"],
                            "provider": item["host"].capitalize()
                        })
                    else:
                        self.failed_downloads.append({
                            "title": item["title"],
                            "url": item["download_url"],
                            "provider": item["host"].capitalize(),
                            "error_type": "Archive Download Failed",
                            "reason": "Link expired, captcha required, or file was removed",
                            "suggestion": "Open direct link in browser or search Archive.org"
                        })
                    self.download_results.append({
                        "title": item["title"],
                        "provider": item["host"].capitalize(),
                        "url": item["download_url"],
                        "success": success
                    })
                except Exception as e:
                    self.failed_downloads.append({
                        "title": item["title"],
                        "url": item["download_url"],
                        "provider": item["host"].capitalize(),
                        "error_type": "Exception",
                        "reason": str(e),
                        "suggestion": "Check connection"
                    })

    def _unpack_archives(self):
        """Unpacks all downloaded archives into structured album folders."""
        console.print("\n[cyan]Stage 3: Unpacking downloaded archives into library folders...[/cyan]")
        count = ArchiveExtractor.unpack_all_archives(self.artist_output_dir)
        console.print(f"[green]✔ Unpacked {count} archive(s) into {self.artist_output_dir.name}[/green]")

    def _audit_library(self) -> Dict[str, Any]:
        """Scans the downloaded directory and audits found vs missing tracks."""
        console.print("\n[cyan]Stage 4: Auditing downloaded tracks against MusicBrainz discography...[/cyan]")

        local_tracks = []
        if self.artist_output_dir and self.artist_output_dir.exists():
            scanner = AudioFileScanner(
                music_dir=str(self.artist_output_dir),
                catalog=self.catalog,
                full_scan=True,
                threads=self.threads
            )
            local_tracks.extend(scanner.scan())

        # If user also provided an existing music directory, scan it (READ-ONLY) to combine coverage
        if self.music_dir and self.music_dir.exists() and self.music_dir != self.artist_output_dir:
            console.print(f"[dim]Cross-referencing with existing music library at {self.music_dir} (Read-Only)...[/dim]")
            ext_scanner = AudioFileScanner(
                music_dir=str(self.music_dir),
                catalog=self.catalog,
                full_scan=False,
                threads=self.threads
            )
            local_tracks.extend(ext_scanner.scan())

        # Reconcile tracks
        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=local_tracks)
        found_items, missing_items = reconciler.reconcile()

        total_tracks = len(self.catalog.tracks)
        found_count = len(found_items)
        missing_count = len(missing_items)
        completion_pct = (found_count / total_tracks * 100) if total_tracks > 0 else 100.0

        return {
            "found_items": found_items,
            "missing_items": missing_items,
            "total_tracks": total_tracks,
            "found_count": found_count,
            "missing_count": missing_count,
            "completion_pct": completion_pct,
        }

    def _generate_reports(self, audit_results: Dict[str, Any]):
        """Renders rich terminal tables and exports Markdown, JSON, CSV, and TXT reports."""
        found_items = audit_results["found_items"]
        missing_items = audit_results["missing_items"]

        # Delegate terminal display to ReportGenerator
        report_gen = ReportGenerator(
            catalog=self.catalog,
            found_items=found_items,
            missing_items=missing_items
        )
        report_gen.render_terminal_report(verbose=False)

        # Print Failed & Unresolved Downloads Table if any exist
        if self.failed_downloads or self.unresolved_promo_urls:
            console.print("\n")
            table = Table(
                title=f"Failed & Unresolved Downloads Investigation ({len(self.failed_downloads) + len(self.unresolved_promo_urls)} items)",
                box=box.ROUNDED,
                header_style="bold red"
            )
            table.add_column("Release / Target", style="cyan", max_width=32)
            table.add_column("Provider", style="magenta", width=12)
            table.add_column("Issue / Status", style="yellow", max_width=38)
            table.add_column("Investigation & Suggested Action", style="green", max_width=42)

            for f in self.failed_downloads:
                table.add_row(
                    f.get("title", "Unknown"),
                    f.get("provider", "Web"),
                    f"[red]{f.get('error_type', 'Failed')}[/red]: {f.get('reason', '')}",
                    f.get("suggestion", "Check link manually")
                )
            for u in self.unresolved_promo_urls:
                table.add_row(
                    u.get("title", "Unknown"),
                    u.get("provider", "Web"),
                    f"[dim yellow]{u.get('type', 'Promo')}[/dim yellow]: {u.get('reason', '')}",
                    u.get("suggestion", "Check secondary market")
                )
            console.print(table)

        # Export structured reports
        if not self.dry_run and self.artist_output_dir and self.artist_output_dir.exists():
            self._export_files(audit_results, report_gen)

    def _export_files(self, audit_results: Dict[str, Any], report_gen: ReportGenerator):
        """Saves Markdown, JSON, CSV, and TXT reports to the artist download directory."""
        safe_name = FilenameUtils.sanitize(self.catalog.name)
        found_items = audit_results["found_items"]
        missing_items = audit_results["missing_items"]
        completion_pct = audit_results["completion_pct"]

        # 1. Markdown Report
        md_path = self.artist_output_dir / f"{safe_name}_audit_report.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Discography & Download Audit: {self.catalog.name}\n\n")
            f.write(f"- **MusicBrainz ID:** `{self.catalog.mbid}`\n")
            f.write(f"- **Audit Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- **Total Catalog Tracks:** {audit_results['total_tracks']}\n")
            f.write(f"- **Downloaded / Found:** {audit_results['found_count']} ({completion_pct:.1f}%)\n")
            f.write(f"- **Missing Tracks:** {audit_results['missing_count']}\n")
            f.write(f"- **Skipped (Already on Server):** {len(self.skipped_downloads)}\n")
            f.write(f"- **Failed / Unresolved Downloads:** {len(self.failed_downloads) + len(self.unresolved_promo_urls)}\n\n")

            f.write("## Downloaded / Found Tracks\n\n")
            f.write("| # | Track Title | Release | Matched File Path |\n")
            f.write("|---|---|---|---|\n")
            for i, m in enumerate(found_items, 1):
                tr = m["mb_track"]
                loc = m.get("local_track", {})
                loc_path = loc.get("path", "")
                if self.artist_output_dir and str(self.artist_output_dir) in loc_path:
                    rel_path = os.path.relpath(loc_path, str(self.artist_output_dir))
                else:
                    rel_path = loc_path
                f.write(f"| {i} | {tr.get('title')} | {tr.get('release_title')} | `{rel_path}` |\n")

            if self.skipped_downloads:
                f.write("\n## Skipped Releases (Already on Server)\n\n")
                f.write("| # | Release Title | Provider | URL | Reason |\n")
                f.write("|---|---|---|---|---|\n")
                for i, s in enumerate(self.skipped_downloads, 1):
                    f.write(f"| {i} | {s.get('title')} | {s.get('provider')} | {s.get('url')} | {s.get('reason')} |\n")

            if self.failed_downloads or self.unresolved_promo_urls:
                f.write("\n## Failed & Unresolved Downloads (Investigation & Remedies)\n\n")
                f.write("| # | Release / Target | Provider | Issue / Reason | Suggested Action | URL |\n")
                f.write("|---|---|---|---|---|---|\n")
                idx = 1
                for fld in self.failed_downloads:
                    f.write(f"| {idx} | {fld.get('title')} | {fld.get('provider')} | **{fld.get('error_type', 'Failed')}**: {fld.get('reason')} | {fld.get('suggestion')} | {fld.get('url')} |\n")
                    idx += 1
                for u in self.unresolved_promo_urls:
                    f.write(f"| {idx} | {u.get('title')} | {u.get('provider')} | **{u.get('type', 'Promo')}**: {u.get('reason')} | {u.get('suggestion')} | {u.get('url')} |\n")
                    idx += 1

            f.write("\n## Missing Tracks\n\n")
            f.write("| # | Track Title | Release | Release Type | Date | MusicBrainz / Discovered URLs |\n")
            f.write("|---|---|---|---|---|---|\n")
            for i, item in enumerate(missing_items, 1):
                tr = item["mb_track"]
                urls = ", ".join(tr.get("urls", [])) or "None"
                f.write(f"| {i} | {tr.get('title')} | {tr.get('release_title')} | {tr.get('release_type')} | {tr.get('date', 'N/A')} | {urls} |\n")

        # 2. Plain Text Missing Tracks List via report_gen
        txt_path = self.artist_output_dir / f"{safe_name}_missing_tracks.txt"
        report_gen.export_txt(str(txt_path))

        # 3. JSON Audit
        json_path = self.artist_output_dir / f"{safe_name}_audit.json"
        audit_data = {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "audit_date": datetime.now().isoformat(),
            "total_tracks": audit_results["total_tracks"],
            "found_count": audit_results["found_count"],
            "missing_count": audit_results["missing_count"],
            "completion_percentage": audit_results["completion_pct"],
            "successful_downloads": self.successful_downloads,
            "skipped_downloads": self.skipped_downloads,
            "failed_downloads": self.failed_downloads,
            "unresolved_promo_urls": self.unresolved_promo_urls,
            "found_tracks": [
                {
                    "title": item["mb_track"].get("title"),
                    "release": item["mb_track"].get("release_title"),
                    "path": item.get("local_track", {}).get("path"),
                    "match_tier": item.get("match_tier")
                }
                for item in found_items
            ],
            "missing_tracks": [
                {
                    "title": item["mb_track"].get("title"),
                    "release": item["mb_track"].get("release_title"),
                    "release_type": item["mb_track"].get("release_type"),
                    "date": item["mb_track"].get("date"),
                    "urls": item["mb_track"].get("urls", [])
                }
                for item in missing_items
            ]
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(audit_data, f, indent=2, ensure_ascii=False)

        # 4. CSV Spreadsheet
        csv_path = self.artist_output_dir / f"{safe_name}_audit.csv"
        report_gen.export_csv(str(csv_path))

        console.print(f"\n[dim]Exported audit reports into {self.artist_output_dir.name}:[/dim]")
        console.print(f"  • Markdown: [cyan]{md_path.name}[/cyan]")
        console.print(f"  • Text List: [cyan]{txt_path.name}[/cyan]")
        console.print(f"  • JSON Data: [cyan]{json_path.name}[/cyan]")
        console.print(f"  • CSV Spreadsheet: [cyan]{csv_path.name}[/cyan]")


def main():
    parser = argparse.ArgumentParser(
        description="MusicBrainz Artist Downloader & Discography Auditor - Automated discography download & missing track analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 artist_downloader.py "96-glass"
  python3 artist_downloader.py "https://musicbrainz.org/artist/2a7276cf-e768-4e7e-bf71-be7468d3604f"
  python3 artist_downloader.py "Stellabee" -o ./downloads -f flac
  python3 artist_downloader.py "goreshit" --dry-run
        """
    )
    parser.add_argument(
        "artist",
        help="Artist Name, MBID UUID, or MusicBrainz Artist URL (e.g. 96-glass or https://musicbrainz.org/artist/...)"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Project output directory for downloads and reports (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "-d", "--music-dir",
        default=None,
        help="Optional external music library directory to scan (READ-ONLY) for checking missing tracks"
    )
    parser.add_argument(
        "-f", "--format",
        default="mp3-320",
        choices=SUPPORTED_FORMATS,
        help="Preferred audio format for free Bandcamp downloads (default: mp3-320)"
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable streaming MP3-128 fallback on Bandcamp when free downloads are not offered"
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=4,
        help="Concurrent worker threads for downloading & tag reading (default: 4)"
    )
    parser.add_argument(
        "--bandcamp-email",
        type=str,
        default=os.environ.get("BANDCAMP_EMAIL"),
        help="Email address for requesting high-res downloads on Name Your Price Bandcamp releases"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force re-downloading files even if they already exist on disk"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only discover and audit releases without downloading audio files"
    )
    parser.add_argument(
        "--slskd",
        action="store_true",
        help="Search Soulseek via slskd for missing releases/tracks, verify tracklists, and queue directories for download"
    )
    parser.add_argument(
        "--slskd-format",
        default="flac",
        choices=["flac", "mp3-320", "any"],
        help="Preferred audio format for Soulseek downloads (default: flac)"
    )
    parser.add_argument(
        "--slskd-singles-only",
        action="store_true",
        default=False,
        help="Only download single matching tracks for compilations/features via Soulseek instead of full releases"
    )

    args = parser.parse_args()

    orchestrator = ArtistDownloadOrchestrator(
        artist_query=args.artist,
        output_dir=args.output_dir,
        music_dir=args.music_dir,
        audio_format=args.format,
        fallback=not args.no_fallback,
        threads=args.threads,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        bandcamp_email=args.bandcamp_email,
        slskd=args.slskd,
        slskd_format=args.slskd_format,
        slskd_singles_only=args.slskd_singles_only
    )

    try:
        orchestrator.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation aborted by user.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
