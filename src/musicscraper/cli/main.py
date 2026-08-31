"""
Unified CLI Entrypoint and Subcommand Router for MusicScraper.
"""

import sys
import argparse
from pathlib import Path
from typing import List, Optional

from musicscraper.config import Config
from musicscraper.core.report import console
from musicscraper.services.auditor import AuditorService
from musicscraper.services.soulseek import SlskdArtistScraper
from musicscraper.services.artist import ArtistDownloadOrchestrator
from musicscraper.services.quality import LocalLibraryQualityScanner, SoulseekQualityUpgrader
from musicscraper.services.tagger import GenreTaggerService
from musicscraper.services.cleaner import FolderCleanerService
from musicscraper.scrapers.bandcamp import BandcampEngine
from musicscraper.scrapers.universal import UniversalScraper, MusicDownloader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicscraper",
        description="MusicScraper - Modern, Modular Music Archiving & Library Automation Suite"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 1. Audit Subcommand
    audit_p = subparsers.add_parser("audit", help="Audit local library against MusicBrainz discography")
    audit_p.add_argument("artist", help="Artist name or MusicBrainz artist ID (MBID)")
    audit_p.add_argument("--music-dir", "-d", default=str(Config.DEFAULT_LIBRARY_DIR), help="Path to local music library")
    audit_p.add_argument("--full-scan", action="store_true", help="Inspect tags across entire library without targeted discovery")
    audit_p.add_argument("--force-refresh", action="store_true", help="Bypass MusicBrainz cache")
    audit_p.add_argument("--missing-only", action="store_true", help="Display only missing tracks")
    audit_p.add_argument("--found-only", action="store_true", help="Display only found tracks")
    audit_p.add_argument("--json", dest="export_json", help="Export audit results to JSON file")
    audit_p.add_argument("--txt", dest="export_txt", help="Export missing tracks to plain text file")
    audit_p.add_argument("--csv", dest="export_csv", help="Export full audit table to CSV")
    audit_p.add_argument("--bandcamp-links", dest="export_bc", help="Export artist Bandcamp links to text file")

    # 2. Soulseek Subcommand
    slsk_p = subparsers.add_parser("soulseek", aliases=["slsk"], help="Search Soulseek via slskd and download missing discography items")
    slsk_p.add_argument("artist", help="Target artist name")
    slsk_p.add_argument("--music-dir", "-d", default=str(Config.DEFAULT_LIBRARY_DIR), help="Path to local library for pre-scan")
    slsk_p.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"], help="Preferred audio format (default: flac)")
    slsk_p.add_argument("--min-match", type=float, default=0.70, help="Minimum tracklist match ratio (default: 0.70)")
    slsk_p.add_argument("--dry-run", action="store_true", help="Scan and verify matches without queueing transfers")

    # 3. Artist Subcommand
    artist_p = subparsers.add_parser("artist", help="End-to-end multi-source artist downloader (MusicBrainz + Bandcamp + Soulseek)")
    artist_p.add_argument("artist", help="Artist name to download")
    artist_p.add_argument("-o", "--output-dir", default=str(Config.DEFAULT_OUTPUT_DIR), help="Output directory")
    artist_p.add_argument("-d", "--library-dir", default=str(Config.DEFAULT_LIBRARY_DIR), help="Local library path for pre-scan")
    artist_p.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"], help="Preferred audio format")
    artist_p.add_argument("--no-bandcamp", action="store_true", help="Disable Bandcamp downloading")
    artist_p.add_argument("--no-soulseek", action="store_true", help="Disable Soulseek queueing")
    artist_p.add_argument("--dry-run", action="store_true", help="Preview downloads without downloading")

    # 4. Quality Upgrade Subcommand
    upg_p = subparsers.add_parser("upgrade", aliases=["quality"], help="Scan local library and upgrade low-bitrate audio to FLAC/320k via Soulseek")
    upg_p.add_argument("-d", "--library-dir", default=str(Config.DEFAULT_LIBRARY_DIR), help="Path to music library")
    upg_p.add_argument("-a", "--artist", help="Filter quality scan to a specific artist")
    upg_p.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"], help="Target audio format")
    upg_p.add_argument("--dry-run", action="store_true", help="Identify upgrade candidates without queueing transfers")

    # 5. Genre Tagger Subcommand
    tag_p = subparsers.add_parser("tag", aliases=["genre"], help="Auto-tag music library with curated Last.fm genres")
    tag_p.add_argument("path", nargs="?", default=str(Config.DEFAULT_LIBRARY_DIR), help="File or folder path to tag")
    tag_p.add_argument("--strategy", choices=["cascade", "blend", "artist", "album", "track"], default="cascade", help="Tagging strategy (default: cascade)")
    tag_p.add_argument("--limit", type=int, default=3, help="Max genres to apply per track (default: 3)")
    tag_p.add_argument("--mode", choices=["overwrite", "skip_existing", "append"], default="overwrite", help="Write mode")
    tag_p.add_argument("--dry-run", action="store_true", help="Preview genre changes without modifying files")

    # 6. Bandcamp Subcommand
    bc_p = subparsers.add_parser("bandcamp", aliases=["bc"], help="Download Bandcamp artist discography, album, or track")
    bc_p.add_argument("targets", nargs="*", help="Bandcamp artist subdomain, URL, album, or track URL")
    bc_p.add_argument("-i", "--input", help="File containing Bandcamp URLs (one per line)")
    bc_p.add_argument("-o", "--output-dir", default=str(Config.DEFAULT_OUTPUT_DIR), help="Output directory")
    bc_p.add_argument("-f", "--format", default="mp3-320", help="Preferred audio format for free downloads")
    bc_p.add_argument("--no-fallback", action="store_true", help="Disable MP3-128 stream fallback")
    bc_p.add_argument("--overwrite", action="store_true", help="Overwrite existing files")

    # 7. Scrape Subcommand
    scrape_p = subparsers.add_parser("scrape", help="Crawl and download releases from web pages (Dochakuso, Otherman, Archive.org, MediaFire)")
    scrape_p.add_argument("url", help="Target URL to crawl (or release URL)")
    scrape_p.add_argument("-o", "--output-dir", default=str(Config.DEFAULT_OUTPUT_DIR), help="Output directory")
    scrape_p.add_argument("--max-workers", "-w", type=int, default=4, help="Download worker threads")
    scrape_p.add_argument("--overwrite", action="store_true", help="Overwrite existing files")

    # 8. Clean Subcommand
    clean_p = subparsers.add_parser("clean", help="Remove empty and non-music folders")
    clean_p.add_argument("path", nargs="?", default=str(Config.DEFAULT_OUTPUT_DIR), help="Directory to clean")
    clean_p.add_argument("--execute", "-y", action="store_true", help="Perform actual deletion (default is dry-run)")
    clean_p.add_argument("-v", "--verbose", action="store_true", help="Verbose log of retained folders")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Route subcommands
    if args.command == "audit":
        auditor = AuditorService()
        catalog, found, missing = auditor.audit_artist(
            artist_query=args.artist,
            music_dir=Path(args.music_dir),
            full_scan=args.full_scan,
            force_refresh=args.force_refresh
        )
        auditor.render_report(
            catalog, found, missing,
            only_missing=args.missing_only,
            only_found=args.found_only
        )
        auditor.export_reports(
            catalog, found, missing,
            json_path=Path(args.export_json) if args.export_json else None,
            txt_path=Path(args.export_txt) if args.export_txt else None,
            csv_path=Path(args.export_csv) if args.export_csv else None,
            bandcamp_path=Path(args.export_bc) if args.export_bc else None
        )
        return 0

    elif args.command in ("soulseek", "slsk"):
        scraper = SlskdArtistScraper(
            artist_query=args.artist,
            music_dir=Path(args.music_dir) if args.music_dir else None,
            preferred_format=args.format,
            min_match_ratio=args.min_match,
            dry_run=args.dry_run
        )
        scraper.run()
        return 0

    elif args.command == "artist":
        orchestrator = ArtistDownloadOrchestrator(
            artist_query=args.artist,
            output_dir=Path(args.output_dir),
            library_dir=Path(args.library_dir),
            preferred_format=args.format,
            dry_run=args.dry_run,
            use_bandcamp=not args.no_bandcamp,
            use_soulseek=not args.no_soulseek
        )
        orchestrator.run()
        return 0

    elif args.command in ("upgrade", "quality"):
        scanner = LocalLibraryQualityScanner(
            library_dir=Path(args.library_dir),
            target_format=args.format
        )
        candidates = scanner.scan(artist_filter=args.artist)
        upgrader = SoulseekQualityUpgrader(
            preferred_format=args.format,
            dry_run=args.dry_run
        )
        upgrader.upgrade_candidates(candidates)
        return 0

    elif args.command in ("tag", "genre"):
        tagger = GenreTaggerService(
            strategy=args.strategy,
            limit=args.limit,
            mode=args.mode,
            dry_run=args.dry_run
        )
        tagger.process_target(Path(args.path))
        return 0

    elif args.command in ("bandcamp", "bc"):
        engine = BandcampEngine(
            output_dir=Path(args.output_dir),
            audio_format=args.format,
            fallback=not args.no_fallback,
            overwrite=args.overwrite
        )
        targets = list(args.targets or [])
        if args.input and Path(args.input).exists():
            with open(args.input, "r", encoding="utf-8") as f:
                for line in f:
                    t = line.strip()
                    if t and not t.startswith("#"):
                        targets.append(t)

        if not targets:
            console.print("[yellow]No Bandcamp target URL or artist specified.[/yellow]")
            return 1

        for target in targets:
            norm_url, target_type = BandcampEngine.normalize_target(target)
            if target_type == "artist":
                rel_urls = engine.get_artist_release_urls(norm_url)
                console.print(f"[cyan]Found {len(rel_urls)} releases for {norm_url}[/cyan]")
                for r_url in rel_urls:
                    meta = engine.get_release_metadata(r_url)
                    if meta:
                        engine.download_release(meta)
            else:
                meta = engine.get_release_metadata(norm_url)
                if meta:
                    engine.download_release(meta)
        return 0

    elif args.command == "scrape":
        scraper = UniversalScraper(base_url=args.url)
        releases = scraper.crawl()
        downloader = MusicDownloader(output_dir=Path(args.output_dir), max_workers=args.max_workers, overwrite=args.overwrite)
        downloader.download_all(releases)
        return 0

    elif args.command == "clean":
        cleaner = FolderCleanerService()
        cleaner.clean(target_dir=Path(args.path), dry_run=not args.execute, verbose=args.verbose)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
