"""
API controllers and service orchestrator wrappers for MusicScraper Web GUI.
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

from musicscraper.config import Config
from musicscraper.core.report import console
from musicscraper.web.tasks import BackgroundTask, global_task_manager


def get_system_status() -> Dict[str, Any]:
    """Inspects and returns the status of connected services and local paths."""
    status = {
        "timestamp": time.time(),
        "paths": {
            "library_dir": str(Config.DEFAULT_LIBRARY_DIR),
            "library_exists": Config.DEFAULT_LIBRARY_DIR.exists(),
            "output_dir": str(Config.DEFAULT_OUTPUT_DIR),
            "output_exists": Config.DEFAULT_OUTPUT_DIR.exists(),
            "cache_dir": str(Config.CACHE_DIR),
            "cache_exists": Config.CACHE_DIR.exists(),
        },
        "services": {
            "slskd": {"configured": bool(Config.SLSKD_URL), "url": Config.SLSKD_URL, "connected": False, "username": None, "state": None},
            "navidrome": {"configured": bool(Config.NAVIDROME_URL), "url": Config.NAVIDROME_URL, "connected": False},
            "lastfm": {"configured": bool(Config.LASTFM_API_KEY), "connected": False},
            "musicbrainz": {"configured": True, "connected": True},
        }
    }

    # Check slskd
    try:
        from musicscraper.clients.slskd import SlskdClient
        client = SlskdClient()
        app_info = client.get_application()
        if app_info:
            status["services"]["slskd"]["connected"] = True
            status["services"]["slskd"]["username"] = app_info.get("user", {}).get("username")
            status["services"]["slskd"]["state"] = app_info.get("server", {}).get("state")
    except Exception as e:
        status["services"]["slskd"]["error"] = str(e)

    # Check Navidrome
    if Config.NAVIDROME_URL and Config.NAVIDROME_USER:
        try:
            from musicscraper.clients.navidrome import NavidromeScanner
            nav = NavidromeScanner(base_url=Config.NAVIDROME_URL, username=Config.NAVIDROME_USER, password=Config.NAVIDROME_TOKEN)
            status["services"]["navidrome"]["connected"] = nav.test_connection()
        except Exception as e:
            status["services"]["navidrome"]["error"] = str(e)

    # Check Last.fm
    if Config.LASTFM_API_KEY:
        try:
            from musicscraper.clients.lastfm import LastFMClient
            lfm = LastFMClient()
            # Quick lightweight test call
            tags = lfm.get_artist_tags("Radiohead")
            status["services"]["lastfm"]["connected"] = bool(tags is not None)
        except Exception as e:
            status["services"]["lastfm"]["error"] = str(e)

    return status


def get_system_config() -> Dict[str, Any]:
    """Returns current environment configurations."""
    return {
        "DEFAULT_LIBRARY_DIR": str(Config.DEFAULT_LIBRARY_DIR),
        "DEFAULT_OUTPUT_DIR": str(Config.DEFAULT_OUTPUT_DIR),
        "CACHE_DIR": str(Config.CACHE_DIR),
        "SLSKD_URL": Config.SLSKD_URL,
        "SLSKD_USERNAME": Config.SLSKD_USERNAME or "",
        "NAVIDROME_URL": Config.NAVIDROME_URL,
        "NAVIDROME_USER": Config.NAVIDROME_USER,
        "LASTFM_API_KEY": Config.LASTFM_API_KEY or "",
        "BANDCAMP_EMAIL": Config.BANDCAMP_EMAIL or "",
    }


def update_system_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates runtime configuration settings."""
    if "DEFAULT_LIBRARY_DIR" in updates:
        Config.DEFAULT_LIBRARY_DIR = Path(updates["DEFAULT_LIBRARY_DIR"]).resolve()
    if "DEFAULT_OUTPUT_DIR" in updates:
        Config.DEFAULT_OUTPUT_DIR = Path(updates["DEFAULT_OUTPUT_DIR"]).resolve()
    if "SLSKD_URL" in updates:
        Config.SLSKD_URL = updates["SLSKD_URL"].rstrip("/")
    if "SLSKD_USERNAME" in updates:
        Config.SLSKD_USERNAME = updates["SLSKD_USERNAME"]
    if "SLSKD_PASSWORD" in updates:
        Config.SLSKD_PASSWORD = updates["SLSKD_PASSWORD"]
    if "NAVIDROME_URL" in updates:
        Config.NAVIDROME_URL = updates["NAVIDROME_URL"].rstrip("/")
    if "NAVIDROME_USER" in updates:
        Config.NAVIDROME_USER = updates["NAVIDROME_USER"]
    if "NAVIDROME_TOKEN" in updates:
        Config.NAVIDROME_TOKEN = updates["NAVIDROME_TOKEN"]
    if "LASTFM_API_KEY" in updates:
        Config.LASTFM_API_KEY = updates["LASTFM_API_KEY"]
    if "BANDCAMP_EMAIL" in updates:
        Config.BANDCAMP_EMAIL = updates["BANDCAMP_EMAIL"]

    return get_system_config()


def get_slskd_transfers() -> Dict[str, Any]:
    """Fetches active downloads, uploads, and search states from slskd."""
    try:
        from musicscraper.clients.slskd import SlskdClient
        client = SlskdClient()
        downloads = client.get_all_downloads()
        return {
            "connected": True,
            "downloads": downloads or []
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
            "downloads": []
        }


def browse_library(subpath: str = "") -> Dict[str, Any]:
    """Lists folders and audio files in the music library."""
    from musicscraper.core.constants import AUDIO_EXTENSIONS
    base_dir = Config.DEFAULT_LIBRARY_DIR
    target = (base_dir / subpath.lstrip("/")).resolve()

    if not str(target).startswith(str(base_dir)):
        target = base_dir

    if not target.exists() or not target.is_dir():
        return {"current_path": str(target), "exists": False, "directories": [], "files": []}

    directories = []
    files = []

    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                directories.append({
                    "name": entry.name,
                    "rel_path": str(Path(entry.path).relative_to(base_dir))
                })
            elif entry.is_file():
                ext = Path(entry.name).suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size": stat.st_size,
                        "rel_path": str(Path(entry.path).relative_to(base_dir))
                    })
    except Exception as e:
        return {"current_path": str(target), "error": str(e), "directories": [], "files": []}

    return {
        "current_path": str(target),
        "rel_path": str(target.relative_to(base_dir)) if target != base_dir else "",
        "exists": True,
        "directories": directories,
        "files": files
    }


_library_releases_cache: Dict[str, Any] = {"timestamp": 0, "releases": []}


def get_library_releases(refresh: bool = False, search: str = "", filter_mode: str = "all") -> Dict[str, Any]:
    """Scans and retrieves all releases present in the music library."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    now = time.time()
    if refresh or not _library_releases_cache["releases"] or (now - _library_releases_cache["timestamp"] > 300):
        service = LibraryReleaseService()
        releases = service.scan_library_releases(force_rescan=refresh)
        _library_releases_cache = {"timestamp": now, "releases": releases}

    all_releases = _library_releases_cache["releases"]

    # Compute global summary metrics
    total_releases = len(all_releases)
    complete_releases = sum(1 for r in all_releases if r.get("status") == "complete")
    has_missing_releases = sum(1 for r in all_releases if r.get("status") == "has_missing")
    total_local_tracks = sum(r.get("found_count", len(r.get("tracks", []))) for r in all_releases)
    total_missing_tracks = sum(r.get("missing_count", 0) for r in all_releases)

    # Filter releases
    filtered = list(all_releases)
    if search:
        s_lower = search.lower().strip()
        filtered = [
            r for r in filtered
            if s_lower in r.get("artist", "").lower()
            or s_lower in r.get("title", "").lower()
            or s_lower in r.get("folder_path", "").lower()
        ]

    if filter_mode == "missing":
        filtered = [r for r in filtered if r.get("status") == "has_missing" or r.get("missing_count", 0) > 0]
    elif filter_mode == "complete":
        filtered = [r for r in filtered if r.get("status") == "complete" and r.get("missing_count", 0) == 0]

    return {
        "summary": {
            "total_releases": total_releases,
            "complete_releases": complete_releases,
            "has_missing_releases": has_missing_releases,
            "total_local_tracks": total_local_tracks,
            "total_missing_tracks": total_missing_tracks,
        },
        "count": len(filtered),
        "releases": filtered
    }


def get_library_release_details(release_id: str, audit: bool = True, force_refresh: bool = False) -> Dict[str, Any]:
    """Retrieves full tracklist and metadata for a specific release, with optional MusicBrainz audit."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    if not _library_releases_cache["releases"]:
        get_library_releases()

    matched_rel = None
    for idx, r in enumerate(_library_releases_cache["releases"]):
        if r.get("id") == release_id or r.get("mb_release_id") == release_id:
            matched_rel = r
            break

    if not matched_rel:
        # Fallback to fresh scan
        get_library_releases(refresh=True)
        for idx, r in enumerate(_library_releases_cache["releases"]):
            if r.get("id") == release_id or r.get("mb_release_id") == release_id:
                matched_rel = r
                break

    if not matched_rel:
        raise ValueError(f"Release ID '{release_id}' not found in library.")

    if audit:
        service = LibraryReleaseService()
        audited = service.audit_release(matched_rel, force_refresh=force_refresh)
        # Update cache in place
        for idx, r in enumerate(_library_releases_cache["releases"]):
            if r.get("id") == release_id:
                _library_releases_cache["releases"][idx] = audited
                break
        return audited

    return matched_rel



# ==============================================================================
# Task Execution Handlers
# ==============================================================================

def run_audit_task(task: BackgroundTask) -> Dict[str, Any]:
    """Executes artist discography audit against local library."""
    from musicscraper.services.auditor import AuditorService
    artist = task.params.get("artist", "").strip()
    music_dir = Path(task.params.get("music_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    full_scan = bool(task.params.get("full_scan", False))
    force_refresh = bool(task.params.get("force_refresh", False))

    if not artist:
        raise ValueError("Artist name or MBID is required for audit.")

    task.update_progress(10, f"Resolving MusicBrainz discography for '{artist}'...")
    auditor = AuditorService()
    catalog, found_items, missing_items = auditor.audit_artist(
        artist_query=artist,
        music_dir=music_dir,
        full_scan=full_scan,
        force_refresh=force_refresh
    )

    task.update_progress(80, "Aggregating audit metrics...")

    # Group releases with found/missing breakdowns
    release_groups: Dict[str, Dict[str, Any]] = {}
    for rel in catalog.releases:
        rid = rel.get("id") or rel.get("title")
        release_groups[rid] = {
            "id": rid,
            "title": rel.get("title", "Unknown"),
            "year": (rel.get("date") or "")[:4],
            "type": rel.get("type", "Album"),
            "total_tracks": 0,
            "found_count": 0,
            "missing_count": 0,
            "tracks": []
        }

    for trk in catalog.tracks:
        rid = trk.get("release_id") or trk.get("release_title")
        if rid in release_groups:
            release_groups[rid]["total_tracks"] += 1
        else:
            for rk, rg in release_groups.items():
                if rg["title"].lower() == (trk.get("release_title") or "").lower():
                    rg["total_tracks"] += 1
                    break

    # Populate found track details
    for f in found_items:
        mb = f.get("mb_track", {})
        rel_id = mb.get("release_id") or mb.get("release_title")
        matched_rg = release_groups.get(rel_id)
        if not matched_rg:
            for rk, rg in release_groups.items():
                if rg["title"].lower() == (mb.get("release_title") or "").lower():
                    matched_rg = rg
                    break
        if matched_rg:
            matched_rg["found_count"] += 1
            matched_rg["tracks"].append({
                "title": mb.get("title"),
                "status": "found",
                "local_file": f.get("local_track", {}).get("filename"),
                "format": f.get("local_track", {}).get("format", ""),
                "bitrate": f.get("local_track", {}).get("bitrate", "")
            })

    # Populate missing track details
    for m in missing_items:
        rel_id = m.get("release_id") or m.get("release_title")
        matched_rg = release_groups.get(rel_id)
        if not matched_rg:
            for rk, rg in release_groups.items():
                if rg["title"].lower() == (m.get("release_title") or "").lower():
                    matched_rg = rg
                    break
        if matched_rg:
            matched_rg["missing_count"] += 1
            matched_rg["tracks"].append({
                "title": m.get("title"),
                "status": "missing",
                "local_file": None
            })

    # Filter out empty release groups if any
    filtered_releases = [rg for rg in release_groups.values() if rg["total_tracks"] > 0 or rg["found_count"] > 0 or rg["missing_count"] > 0]

    total_tracks = len(catalog.tracks)
    found_count = len(found_items)
    missing_count = len(missing_items)
    completion_pct = (found_count / total_tracks * 100.0) if total_tracks > 0 else 100.0

    task.update_progress(100, f"Audit complete: {found_count}/{total_tracks} tracks ({completion_pct:.1f}%)")

    return {
        "artist": catalog.name,
        "sort_name": catalog.sort_name,
        "mbid": catalog.mbid,
        "type": catalog.artist_info.get("type", "Artist"),
        "country": catalog.artist_info.get("country", ""),
        "tags": [t.get("name") for t in catalog.artist_info.get("tag-list", []) if isinstance(t, dict)],
        "bandcamp_urls": catalog.bandcamp_urls,
        "total_tracks": total_tracks,
        "found_count": found_count,
        "missing_count": missing_count,
        "completion_pct": round(completion_pct, 1),
        "releases": filtered_releases,
        "missing_items": [{
            "title": m.get("title"),
            "release": m.get("release_title"),
            "year": m.get("release_year")
        } for m in missing_items],
    }


def run_soulseek_search_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek via slskd for artist/album/track query."""
    from musicscraper.clients.slskd import SlskdClient
    from musicscraper.core.audio import AudioQualityAnalyzer

    query = task.params.get("query", "").strip()
    timeout = float(task.params.get("timeout", 15.0))
    fmt = task.params.get("format", "flac").lower()

    if not query:
        raise ValueError("Search query is required.")

    task.update_progress(20, f"Submitting Soulseek search for '{query}'...")
    client = SlskdClient(timeout=timeout + 10)
    search_data = client.search(query=query, timeout=timeout)

    task.update_progress(85, "Parsing discovered peer directories...")
    res = search_data.get("responses", [])

    parsed_dirs: List[Dict[str, Any]] = []
    if res and isinstance(res, list):
        for resp in res:
            user = resp.get("username", "Unknown")
            files = resp.get("files", [])
            if not files:
                continue

            # Group files by folder
            folder_map: Dict[str, List[Dict[str, Any]]] = {}
            for f in files:
                full_fn = f.get("filename", "")
                parts = full_fn.replace("\\", "/").rsplit("/", 1)
                dir_name = parts[0] if len(parts) > 1 else "/"
                fn = parts[1] if len(parts) > 1 else full_fn
                f_copy = dict(f)
                f_copy["base_filename"] = fn
                f_copy["full_filename"] = full_fn
                fmt_label, fmt_score = AudioQualityAnalyzer.determine_stream_quality(f_copy)
                f_copy["fmt_label"] = fmt_label
                f_copy["fmt_score"] = fmt_score
                folder_map.setdefault(dir_name, []).append(f_copy)

            for d_name, d_files in folder_map.items():
                audio_files = [f for f in d_files if f.get("fmt_score", 0) > 0]
                if not audio_files:
                    continue
                parsed_dirs.append({
                    "user": user,
                    "dir_name": d_name,
                    "file_count": len(audio_files),
                    "total_size": sum(f.get("size", 0) for f in audio_files),
                    "files": audio_files
                })

    task.update_progress(100, f"Found {len(parsed_dirs)} peer folders.")
    return {
        "query": query,
        "directories_count": len(parsed_dirs),
        "directories": parsed_dirs[:100]
    }


def run_soulseek_queue_task(task: BackgroundTask) -> Dict[str, Any]:
    """Enqueues files or directory transfers to slskd."""
    from musicscraper.clients.slskd import SlskdClient
    username = task.params.get("username", "").strip()
    files = task.params.get("files", [])

    if not username or not files:
        raise ValueError("Username and files list are required to queue transfers.")

    task.update_progress(20, f"Queueing {len(files)} items from user '{username}'...")
    client = SlskdClient()
    client.enqueue_download(username, files)
    task.update_progress(100, "Transfers successfully queued.")
    return {"queued_count": len(files), "username": username}


def run_artist_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Executes multi-source artist discography downloader."""
    from musicscraper.services.artist import ArtistDownloadOrchestrator
    artist = task.params.get("artist", "").strip()
    output_dir = Path(task.params.get("output_dir", str(Config.DEFAULT_OUTPUT_DIR)))
    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    preferred_format = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    use_bandcamp = bool(task.params.get("use_bandcamp", True))
    use_soulseek = bool(task.params.get("use_soulseek", True))
    timeout = float(task.params.get("timeout", 25.0))

    if not artist:
        raise ValueError("Artist name is required.")

    task.update_progress(10, f"Initializing multi-source orchestrator for '{artist}'...")
    orchestrator = ArtistDownloadOrchestrator(
        artist_query=artist,
        output_dir=output_dir,
        library_dir=library_dir,
        preferred_format=preferred_format,
        dry_run=dry_run,
        use_bandcamp=use_bandcamp,
        use_soulseek=use_soulseek,
        search_timeout=timeout
    )
    res = orchestrator.run()
    task.update_progress(100, "Artist download orchestration completed.")
    return res


def run_quality_scan_task(task: BackgroundTask) -> Dict[str, Any]:
    """Scans local library to discover low-bitrate tracks eligible for upgrade."""
    from musicscraper.services.quality import LocalLibraryQualityScanner
    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    target_format = task.params.get("format", "flac")
    artist_filter = task.params.get("artist_filter", "").strip() or None

    task.update_progress(10, f"Scanning library at {library_dir} for low-bitrate audio...")
    scanner = LocalLibraryQualityScanner(
        library_dir=library_dir,
        target_format=target_format
    )
    candidates = scanner.scan(artist_filter=artist_filter)
    task.update_progress(90, f"Found {len(candidates)} candidate tracks.")

    candidate_list = []
    for c in candidates:
        candidate_list.append({
            "path": str(c.meta.path),
            "filename": c.meta.path.name,
            "artist": c.meta.artist or c.meta.album_artist or "",
            "album": c.meta.album or "",
            "title": c.meta.title or c.meta.path.stem,
            "format": (c.meta.format_label or c.meta.file_type or "AUDIO").upper(),
            "bitrate": c.meta.bitrate_kbps or 0,
            "target_quality": c.target_quality.upper(),
            "current_label": c.current_label
        })

    task.update_progress(100, f"Scan finished. {len(candidate_list)} tracks need upgrade.")
    return {
        "candidate_count": len(candidate_list),
        "candidates": candidate_list
    }


def run_quality_upgrade_task(task: BackgroundTask) -> Dict[str, Any]:
    """Scans and upgrades low-bitrate tracks via Soulseek."""
    from musicscraper.services.quality import LocalLibraryQualityScanner, SoulseekQualityUpgrader
    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    target_format = task.params.get("format", "flac")
    artist_filter = task.params.get("artist_filter", "").strip() or None
    dry_run = bool(task.params.get("dry_run", False))
    timeout = float(task.params.get("timeout", 25.0))

    task.update_progress(10, "Scanning library for upgrade candidates...")
    scanner = LocalLibraryQualityScanner(library_dir=library_dir, target_format=target_format)
    candidates = scanner.scan(artist_filter=artist_filter)

    if not candidates:
        task.update_progress(100, "No upgrade candidates found.")
        return {"candidate_count": 0, "upgraded_count": 0, "candidates": []}

    task.update_progress(30, f"Searching Soulseek for {len(candidates)} candidates...")
    upgrader = SoulseekQualityUpgrader(preferred_format=target_format, dry_run=dry_run, search_timeout=timeout)
    upgrader.upgrade_candidates(candidates)

    task.update_progress(100, f"Quality upgrade run completed ({'Dry run' if dry_run else 'Queued'}).")
    return {
        "candidate_count": len(candidates),
        "dry_run": dry_run
    }


def run_genre_tag_task(task: BackgroundTask) -> Dict[str, Any]:
    """Runs Last.fm genre tagger service."""
    from musicscraper.services.tagger import GenreTaggerService
    path = Path(task.params.get("path", str(Config.DEFAULT_LIBRARY_DIR)))
    strategy = task.params.get("strategy", "cascade")
    limit = int(task.params.get("limit", 3))
    mode = task.params.get("mode", "overwrite")
    dry_run = bool(task.params.get("dry_run", False))

    task.update_progress(15, f"Tagging files in {path} (strategy: {strategy}, dry-run: {dry_run})...")
    tagger = GenreTaggerService(
        strategy=strategy,
        limit=limit,
        mode=mode,
        dry_run=dry_run
    )
    tagger.process_target(path)
    task.update_progress(100, "Genre tagging completed.")
    return {"path": str(path), "strategy": strategy, "dry_run": dry_run}


def run_bandcamp_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Downloads releases/tracks from Bandcamp."""
    from musicscraper.scrapers.bandcamp import BandcampEngine
    targets = task.params.get("targets", [])
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.splitlines() if t.strip()]

    output_dir = Path(task.params.get("output_dir", str(Config.DEFAULT_OUTPUT_DIR)))
    audio_format = task.params.get("format", "mp3-320")
    fallback = bool(task.params.get("fallback", True))
    overwrite = bool(task.params.get("overwrite", False))

    if not targets:
        raise ValueError("At least one Bandcamp URL or artist name is required.")

    task.update_progress(10, f"Initializing Bandcamp engine for {len(targets)} targets...")
    engine = BandcampEngine(
        output_dir=output_dir,
        audio_format=audio_format,
        fallback=fallback,
        overwrite=overwrite
    )

    downloaded = []
    for idx, target in enumerate(targets):
        task.update_progress(20 + int((idx / len(targets)) * 75), f"Processing Bandcamp target: {target}")
        norm_url, target_type = BandcampEngine.normalize_target(target)
        if target_type == "artist":
            rel_urls = engine.get_artist_release_urls(norm_url)
            for r_url in rel_urls:
                meta = engine.get_release_metadata(r_url)
                if meta:
                    engine.download_release(meta)
                    downloaded.append(meta.get("title", r_url))
        else:
            meta = engine.get_release_metadata(norm_url)
            if meta:
                engine.download_release(meta)
                downloaded.append(meta.get("title", norm_url))

    task.update_progress(100, f"Bandcamp download complete. {len(downloaded)} releases processed.")
    return {"targets": targets, "downloaded_count": len(downloaded), "releases": downloaded}


def run_universal_scrape_task(task: BackgroundTask) -> Dict[str, Any]:
    """Crawls a music release website and batch downloads audio files."""
    from musicscraper.scrapers.universal import UniversalScraper, MusicDownloader
    url = task.params.get("url", "").strip()
    output_dir = Path(task.params.get("output_dir", str(Config.DEFAULT_OUTPUT_DIR)))
    max_workers = int(task.params.get("max_workers", 4))
    overwrite = bool(task.params.get("overwrite", False))

    if not url:
        raise ValueError("URL to scrape is required.")

    task.update_progress(15, f"Crawling release links from {url}...")
    scraper = UniversalScraper(base_url=url)
    releases = scraper.crawl()

    task.update_progress(50, f"Discovered {len(releases)} releases. Starting downloads...")
    downloader = MusicDownloader(output_dir=output_dir, max_workers=max_workers, overwrite=overwrite)
    downloader.download_all(releases)

    task.update_progress(100, f"Downloaded {len(releases)} releases from {url}.")
    return {"url": url, "releases_count": len(releases)}


def run_clean_folders_task(task: BackgroundTask) -> Dict[str, Any]:
    """Scans and cleans empty / non-music directories."""
    from musicscraper.services.cleaner import FolderCleanerService
    path = Path(task.params.get("path", str(Config.DEFAULT_OUTPUT_DIR)))
    execute = bool(task.params.get("execute", False))

    task.update_progress(20, f"Scanning folders in {path} (mode: {'Execute Deletion' if execute else 'Preview Dry-run'})...")
    cleaner = FolderCleanerService()
    deleted_paths, total_scanned = cleaner.clean(target_dir=path, dry_run=not execute)

    task.update_progress(100, f"Finished scan. {len(deleted_paths)} empty folders {'deleted' if execute else 'identified'}.")
    return {
        "path": str(path),
        "dry_run": not execute,
        "total_scanned": total_scanned,
        "deleted_count": len(deleted_paths),
        "deleted_folders": [str(p) for p in deleted_paths]
    }


def run_library_scan_task(task: BackgroundTask) -> Dict[str, Any]:
    """Scans and caches all releases present in the music library."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    force_rescan = bool(task.params.get("force_rescan", True))

    task.update_progress(10, f"Scanning audio files in library: {library_dir}...")
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        pct = 10 + int((done / max(1, total)) * 75)
        task.update_progress(pct, f"{msg} ({done}/{total})")

    releases = service.scan_library_releases(
        library_dir=library_dir,
        force_rescan=force_rescan,
        on_progress=on_prog
    )

    _library_releases_cache = {"timestamp": time.time(), "releases": releases}
    task.update_progress(100, f"Discovered and organized {len(releases)} releases in library.")
    return {
        "releases_count": len(releases),
        "complete_count": sum(1 for r in releases if r.get("status") == "complete"),
        "missing_count": sum(1 for r in releases if r.get("status") == "has_missing"),
    }


def run_release_missing_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek and downloads all missing tracks for a specific release."""
    from musicscraper.services.library import LibraryReleaseService

    artist = task.params.get("artist", "").strip()
    release_title = task.params.get("release_title", "").strip()
    missing_tracks = task.params.get("missing_tracks", [])
    format_pref = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    timeout = float(task.params.get("timeout", 25.0))

    if not artist or not release_title:
        raise ValueError("Artist and release title are required to download missing tracks.")

    if not missing_tracks:
        task.update_progress(100, "No missing tracks specified for download.")
        return {"status": "skipped", "queued_count": 0}

    task.update_progress(10, f"Searching Soulseek for {len(missing_tracks)} missing tracks of '{artist} - {release_title}'...")
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        task.update_progress(done, msg)

    res = service.download_missing_tracks(
        artist=artist,
        release_title=release_title,
        missing_tracks=missing_tracks,
        preferred_format=format_pref,
        search_timeout=timeout,
        dry_run=dry_run,
        on_progress=on_prog
    )

    task.update_progress(100, f"Queued {res.get('queued_count', 0)} of {len(missing_tracks)} missing tracks.")
    return res


def run_track_soulseek_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek and downloads a single missing track."""
    from musicscraper.services.library import LibraryReleaseService

    artist = task.params.get("artist", "").strip()
    release_title = task.params.get("release_title", "").strip()
    track_title = task.params.get("track_title", "").strip()
    track_artist = task.params.get("track_artist", "").strip()
    format_pref = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    timeout = float(task.params.get("timeout", 20.0))

    if not artist or not track_title:
        raise ValueError("Artist and track title are required.")

    display_name = f"{track_artist} - {track_title}" if track_artist else f"{artist} - {track_title}"
    task.update_progress(15, f"Searching Soulseek for single track '{display_name}'...")
    service = LibraryReleaseService()
    res = service.download_single_missing_track(
        artist=artist,
        release_title=release_title,
        track_title=track_title,
        track_artist=track_artist if track_artist else None,
        preferred_format=format_pref,
        search_timeout=timeout,
        dry_run=dry_run
    )

    if res.get("success"):
        task.update_progress(100, f"Queued '{track_title}' from peer '{res.get('user')}'.")
    else:
        task.update_progress(100, f"No peer candidates found for '{track_title}'.")

    return res


def run_library_audit_all_task(task: BackgroundTask) -> Dict[str, Any]:
    """Audits all library releases against MusicBrainz to find missing tracks across the entire library."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    force_refresh = bool(task.params.get("force_refresh", True))

    task.update_progress(10, "Starting MusicBrainz audit for all library releases...")
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        pct = 10 + int((done / max(1, total)) * 85)
        task.update_progress(pct, f"[{done}/{total}] {msg}")

    audited = service.audit_all_releases(
        library_dir=library_dir,
        force_refresh=force_refresh,
        on_progress=on_prog
    )

    _library_releases_cache = {"timestamp": time.time(), "releases": audited}

    has_missing = sum(1 for r in audited if r.get("status") == "has_missing")
    total_missing_trks = sum(r.get("missing_count", 0) for r in audited)

    task.update_progress(100, f"Library audit complete: {has_missing} releases have missing tracks ({total_missing_trks} missing tracks total).")
    return {
        "total_releases": len(audited),
        "has_missing_releases": has_missing,
        "total_missing_tracks": total_missing_trks,
    }


TASK_DISPATCHER = {
    "audit": run_audit_task,
    "soulseek_search": run_soulseek_search_task,
    "soulseek_download": run_soulseek_queue_task,
    "artist_download": run_artist_download_task,
    "quality_scan": run_quality_scan_task,
    "quality_upgrade": run_quality_upgrade_task,
    "genre_tag": run_genre_tag_task,
    "bandcamp_download": run_bandcamp_download_task,
    "universal_scrape": run_universal_scrape_task,
    "clean_folders": run_clean_folders_task,
    "library_scan": run_library_scan_task,
    "library_audit_all": run_library_audit_all_task,
    "release_missing_download": run_release_missing_download_task,
    "track_soulseek_download": run_track_soulseek_download_task,
}


def launch_task(task_type: str, params: Dict[str, Any], name: Optional[str] = None) -> BackgroundTask:
    """Dispatches and launches an asynchronous task."""
    fn = TASK_DISPATCHER.get(task_type)
    if not fn:
        raise ValueError(f"Unknown task type: {task_type}")

    friendly_name = name or f"{task_type.replace('_', ' ').title()}"
    return global_task_manager.submit(
        name=friendly_name,
        task_type=task_type,
        target_fn=fn,
        params=params
    )


