"""
Library release scanner, release tracklist reconciler, and missing track downloader.
"""

import os
import re
import json
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor

from unidecode import unidecode

from musicscraper.config import Config
from musicscraper.core.constants import (
    AUDIO_EXTENSIONS,
    IGNORED_SCAN_DIR_NAMES,
    IGNORED_SCAN_DIR_PREFIXES,
)
from musicscraper.core.text import (
    normalize_text,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    strip_track_number_and_artist,
)
from musicscraper.core.audio import AudioMetadata, AudioQualityAnalyzer
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.report import console
from musicscraper.clients.musicbrainz import MusicBrainzClient
from musicscraper.clients.slskd import SlskdClient, SlskdAPIError


class LibraryReleaseService:
    """
    Service managing library-wide release discovery, MusicBrainz release tracklist
    reconciliation, and Soulseek missing track downloads.
    """

    def __init__(
        self,
        mb_client: Optional[MusicBrainzClient] = None,
        slskd_client: Optional[SlskdClient] = None,
        cache_manager: Optional[UnifiedCacheManager] = None
    ):
        self.mb_client = mb_client or MusicBrainzClient()
        self.slskd_client = slskd_client or SlskdClient()
        self.cache = cache_manager or UnifiedCacheManager()

    def scan_library_releases(
        self,
        library_dir: Optional[Path] = None,
        force_rescan: bool = False,
        threads: int = 16,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Scans music directory and aggregates all local audio files into grouped releases.
        """
        lib_path = Path(library_dir or Config.DEFAULT_LIBRARY_DIR).resolve()
        if not lib_path.exists():
            return []

        # 1. Discover all candidate audio files
        audio_paths: List[Path] = []
        for root, dirs, files in os.walk(lib_path):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(IGNORED_SCAN_DIR_PREFIXES)
                and d.lower() not in IGNORED_SCAN_DIR_NAMES
            ]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    audio_paths.append(Path(root) / f)

        total_files = len(audio_paths)
        if total_files == 0:
            return []

        # 2. Inspect metadata (Cache or Mutagen)
        all_tracks: List[AudioMetadata] = []
        uncached_paths: List[Path] = []

        for p in audio_paths:
            if not force_rescan:
                cached = self.cache.get_audio_metadata(p)
                if cached:
                    all_tracks.append(cached)
                    continue
            uncached_paths.append(p)

        if uncached_paths:
            completed = len(all_tracks)
            with ThreadPoolExecutor(max_workers=threads) as pool:
                for meta in pool.map(AudioQualityAnalyzer.analyze_file, uncached_paths):
                    if meta:
                        self.cache.store_audio_metadata(meta)
                        all_tracks.append(meta)
                    completed += 1
                    if on_progress and completed % 25 == 0:
                        on_progress(completed, total_files, "Extracting audio metadata...")

        # 3. Group files into Releases
        releases_map: Dict[str, Dict[str, Any]] = {}

        for meta in all_tracks:
            artist = meta.album_artist or meta.artist or "Unknown Artist"
            album = meta.album
            folder = str(meta.path.parent)

            # Derive clean album name fallback from parent folder if not tagged
            if not album:
                folder_name = meta.path.parent.name
                album = folder_name if folder_name else "Unknown Album"

            norm_artist = normalize_text(artist)
            norm_album = normalize_text(album)

            # Check if MB release ID is present
            mb_rel_id = next(iter(meta.mb_release_ids)) if meta.mb_release_ids else None

            # Generate unique release key
            if mb_rel_id:
                rel_key = f"mb_{mb_rel_id}"
            elif norm_artist and norm_album:
                rel_key = f"art_alb_{norm_artist}::{norm_album}"
            else:
                rel_key = f"dir_{hashlib.md5(folder.encode()).hexdigest()[:12]}"

            if rel_key not in releases_map:
                rel_id = hashlib.md5(rel_key.encode()).hexdigest()[:16]
                try:
                    rel_folder = str(meta.path.parent.relative_to(lib_path))
                except Exception:
                    rel_folder = str(meta.path.parent)

                releases_map[rel_key] = {
                    "id": rel_id,
                    "rel_key": rel_key,
                    "title": album,
                    "artist": artist,
                    "album_artist": meta.album_artist or artist,
                    "year": meta.year or "",
                    "folder_path": rel_folder,
                    "full_path": str(meta.path.parent),
                    "mb_release_id": mb_rel_id,
                    "formats": set(),
                    "is_lossless_all": True,
                    "tracks": [],
                    "total_tracks_expected": 0,
                    "status": "unverified",
                    "found_count": 0,
                    "missing_count": 0,
                    "completion_pct": 100.0,
                }

            rel = releases_map[rel_key]
            fmt_label = meta.format_label or meta.file_type.lstrip(".").upper()
            if fmt_label:
                rel["formats"].add(fmt_label)
            if not meta.is_lossless:
                rel["is_lossless_all"] = False
            if meta.year and not rel["year"]:
                rel["year"] = meta.year

            # Track number parsing from tag or filename prefix
            trk_raw = meta.track_number
            trk_num = None
            total_in_tag = None
            if trk_raw:
                trk_str = str(trk_raw).strip()
                if "/" in trk_str:
                    parts = trk_str.split("/")
                    try:
                        trk_num = int(re.sub(r"[^\d]", "", parts[0]))
                    except Exception:
                        pass
                    try:
                        total_in_tag = int(re.sub(r"[^\d]", "", parts[1]))
                    except Exception:
                        pass
                else:
                    try:
                        trk_num = int(re.sub(r"[^\d]", "", trk_str))
                    except Exception:
                        pass

            # Fallback: extract leading track number from filename (e.g. "01 - Song.mp3", "02. Song.flac")
            if trk_num is None:
                fn_match = re.match(r"^(\d{1,3})[\s._\-]", meta.path.name)
                if fn_match:
                    try:
                        trk_num = int(fn_match.group(1))
                    except Exception:
                        pass

            if total_in_tag and total_in_tag > rel["total_tracks_expected"]:
                rel["total_tracks_expected"] = total_in_tag

            rel["tracks"].append({
                "path": str(meta.path),
                "filename": meta.path.name,
                "title": meta.title or meta.path.stem,
                "artist": meta.artist or artist,
                "track_number": str(trk_num) if trk_num is not None else None,
                "track_num_int": trk_num,
                "format": fmt_label,
                "bitrate": meta.bitrate_kbps,
                "is_lossless": meta.is_lossless,
                "quality_score": meta.quality_score,
                "duration": meta.duration,
                "mb_track_ids": list(meta.mb_track_ids),
                "mb_rec_ids": list(meta.mb_rec_ids),
                "status": "found"
            })

        # 4. Finalize release metrics, perform gap analysis & sort tracks
        release_list: List[Dict[str, Any]] = []
        for rel in releases_map.values():
            found_tracks = list(rel["tracks"])
            found_nums = {t["track_num_int"] for t in found_tracks if t["track_num_int"] is not None and t["track_num_int"] > 0}

            max_trk_num = max(found_nums) if found_nums else 0
            # If max track number is reasonable (e.g. <= 60), use it for expected count
            if 0 < max_trk_num <= 60 and max_trk_num > rel["total_tracks_expected"]:
                rel["total_tracks_expected"] = max_trk_num

            # Check if sequence gaps or total tag indicate missing tracks
            total_expected = max(rel["total_tracks_expected"], len(found_tracks))
            all_tracks_list: List[Dict[str, Any]] = list(found_tracks)

            if total_expected > len(found_tracks) and 0 < total_expected <= 60:
                # Add placeholder missing track objects for missing sequence numbers
                for k in range(1, total_expected + 1):
                    if k not in found_nums:
                        all_tracks_list.append({
                            "disc_number": 1,
                            "track_number": str(k),
                            "track_num_int": k,
                            "title": f"Track {k:02d} (Missing)",
                            "status": "missing",
                            "filename": None,
                            "path": None,
                            "format": None,
                            "bitrate": None,
                            "is_lossless": None,
                            "quality_score": 0,
                            "duration": None,
                            "mb_recording_id": None
                        })

            # Sort tracks by track number or filename
            def _sort_key(t: Dict[str, Any]) -> Tuple[int, str]:
                tn = t.get("track_num_int")
                if tn is not None and tn > 0:
                    return tn, t.get("title", "")
                return 9999, t.get("filename") or t.get("title", "")

            all_tracks_list.sort(key=_sort_key)
            rel["tracks"] = all_tracks_list
            rel["formats"] = sorted(list(rel["formats"]))

            found_count = sum(1 for t in all_tracks_list if t["status"] == "found")
            missing_count = sum(1 for t in all_tracks_list if t["status"] == "missing")
            total_count = len(all_tracks_list)

            rel["found_count"] = found_count
            rel["missing_count"] = missing_count
            rel["total_tracks_expected"] = total_count

            if missing_count > 0:
                rel["status"] = "has_missing"
                rel["completion_pct"] = round((found_count / total_count) * 100.0, 1) if total_count > 0 else 100.0
            else:
                rel["status"] = "complete"
                rel["completion_pct"] = 100.0

            release_list.append(rel)

        # Sort releases alphabetically by Artist, then Title
        release_list.sort(key=lambda r: (r["artist"].lower(), r["title"].lower()))
        return release_list

    def audit_all_releases(
        self,
        library_dir: Optional[Path] = None,
        force_refresh: bool = False,
        max_workers: int = 4,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Scans all library releases and audits each against MusicBrainz to find missing tracks.
        """
        releases = self.scan_library_releases(library_dir=library_dir, force_rescan=force_refresh)
        if not releases:
            return []

        audited_releases: List[Dict[str, Any]] = []
        total = len(releases)

        for idx, rel in enumerate(releases):
            if on_progress:
                on_progress(idx + 1, total, f"Auditing MusicBrainz for '{rel.get('artist')} - {rel.get('title')}'...")
            try:
                audited = self.audit_release(rel, force_refresh=force_refresh)
                audited_releases.append(audited)
            except Exception as e:
                console.print(f"[yellow]Warning: Error auditing '{rel.get('title')}': {e}[/yellow]")
                audited_releases.append(rel)

        return audited_releases


    def audit_release(
        self,
        release_data: Dict[str, Any],
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Reconciles a local release against MusicBrainz to identify full tracklist,
        found tracks, and missing tracks with real official song titles.
        """
        artist = release_data.get("artist", "")
        title = release_data.get("title", "")
        mb_rel_id = release_data.get("mb_release_id")
        
        # Only consider actual local audio files on disk (ignore any prior missing placeholders)
        local_tracks = [
            t for t in release_data.get("tracks", [])
            if t.get("filename") or t.get("path")
        ]

        mb_release = None

        # 1. Look up by MBID if available
        if mb_rel_id:
            mb_release = self.mb_client.get_release_by_id(mb_rel_id, force_refresh=force_refresh)

        # 2. Search MusicBrainz by release title + artist if not found by ID
        if not mb_release and title:
            search_results = self.mb_client.search_release(release_title=title, artist_name=artist, limit=5)
            if search_results:
                best_id = search_results[0].get("id")
                if best_id:
                    mb_release = self.mb_client.get_release_by_id(best_id, force_refresh=force_refresh)

        # 3. If MB release found, reconcile official tracks against local files
        if mb_release:
            official_tracks: List[Dict[str, Any]] = []
            media_list = mb_release.get("medium-list", [])

            for medium in media_list:
                disc_num = medium.get("position", 1)
                for trk in medium.get("track-list", []):
                    rec = trk.get("recording", {})
                    trk_num = trk.get("number") or str(trk.get("position", ""))
                    trk_title = rec.get("title") or trk.get("title") or "Unknown Track"
                    trk_len = trk.get("length") or rec.get("length")
                    duration_sec = round(int(trk_len) / 1000.0, 1) if trk_len else None

                    official_tracks.append({
                        "disc_number": disc_num,
                        "track_number": trk_num,
                        "title": trk_title,
                        "norm_title": normalize_text(trk_title),
                        "mb_recording_id": rec.get("id"),
                        "mb_track_id": trk.get("id"),
                        "duration": duration_sec,
                    })

            # Reconcile local tracks against official tracks
            matched_local_indices: Set[int] = set()
            reconciled_tracklist: List[Dict[str, Any]] = []

            for off_trk in official_tracks:
                matched_local = None
                p_off = parse_track_title_structure(off_trk["title"])

                # Pass 1: MBID match
                for idx, lt in enumerate(local_tracks):
                    if idx in matched_local_indices:
                        continue
                    if off_trk["mb_recording_id"] and off_trk["mb_recording_id"] in lt.get("mb_rec_ids", []):
                        matched_local = lt
                        matched_local_indices.add(idx)
                        break
                    if off_trk["mb_track_id"] and off_trk["mb_track_id"] in lt.get("mb_track_ids", []):
                        matched_local = lt
                        matched_local_indices.add(idx)
                        break

                # Pass 2: Track number + Title match
                if not matched_local:
                    for idx, lt in enumerate(local_tracks):
                        if idx in matched_local_indices:
                            continue
                        lt_num = lt.get("track_number")
                        if lt_num and str(lt_num).strip() == str(off_trk["track_number"]).strip():
                            p_lt = parse_track_title_structure(lt.get("title", ""))
                            sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                            if (p_off["base_norm"] == p_lt["base_norm"]) or sim >= 0.70:
                                matched_local = lt
                                matched_local_indices.add(idx)
                                break

                # Pass 3: High title similarity
                if not matched_local:
                    for idx, lt in enumerate(local_tracks):
                        if idx in matched_local_indices:
                            continue
                        p_lt = parse_track_title_structure(lt.get("title", ""))
                        sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                        ver_compat = are_versions_compatible(
                            p_off["version_type"], p_off["version_text"],
                            p_lt["version_type"], p_lt["version_text"]
                        )
                        if (p_off["base_norm"] == p_lt["base_norm"] or sim >= 0.85) and ver_compat:
                            matched_local = lt
                            matched_local_indices.add(idx)
                            break

                if matched_local:
                    reconciled_tracklist.append({
                        "disc_number": off_trk["disc_number"],
                        "track_number": off_trk["track_number"],
                        "title": off_trk["title"],
                        "status": "found",
                        "filename": matched_local.get("filename"),
                        "path": matched_local.get("path"),
                        "format": matched_local.get("format"),
                        "bitrate": matched_local.get("bitrate"),
                        "is_lossless": matched_local.get("is_lossless"),
                        "quality_score": matched_local.get("quality_score"),
                        "duration": matched_local.get("duration") or off_trk["duration"],
                        "mb_recording_id": off_trk["mb_recording_id"]
                    })
                else:
                    reconciled_tracklist.append({
                        "disc_number": off_trk["disc_number"],
                        "track_number": off_trk["track_number"],
                        "title": off_trk["title"],
                        "status": "missing",
                        "filename": None,
                        "path": None,
                        "format": None,
                        "bitrate": None,
                        "is_lossless": None,
                        "quality_score": 0,
                        "duration": off_trk["duration"],
                        "mb_recording_id": off_trk["mb_recording_id"]
                    })

            # Append any unmatched local tracks (e.g. bonus tracks or non-standard mixes)
            for idx, lt in enumerate(local_tracks):
                if idx not in matched_local_indices and (lt.get("filename") or lt.get("path")):
                    reconciled_tracklist.append({
                        "disc_number": 1,
                        "track_number": lt.get("track_number") or "-",
                        "title": lt.get("title") or lt.get("filename"),
                        "status": "found",
                        "filename": lt.get("filename"),
                        "path": lt.get("path"),
                        "format": lt.get("format"),
                        "bitrate": lt.get("bitrate"),
                        "is_lossless": lt.get("is_lossless"),
                        "quality_score": lt.get("quality_score"),
                        "duration": lt.get("duration"),
                        "mb_recording_id": None
                    })


            total_tracks = len(reconciled_tracklist)
            found_count = sum(1 for t in reconciled_tracklist if t["status"] == "found")
            missing_count = sum(1 for t in reconciled_tracklist if t["status"] == "missing")
            completion_pct = round((found_count / total_tracks * 100.0), 1) if total_tracks > 0 else 100.0

            result = dict(release_data)
            result["mb_release_id"] = mb_release.get("id")
            result["mb_release_title"] = mb_release.get("title")
            result["total_tracks_expected"] = total_tracks
            result["found_count"] = found_count
            result["missing_count"] = missing_count
            result["completion_pct"] = completion_pct
            result["status"] = "has_missing" if missing_count > 0 else "complete"
            result["tracks"] = reconciled_tracklist
            return result

        # Fallback if no MB release found: return existing release data with verified status
        result = dict(release_data)
        result["status"] = "complete" if result.get("missing_count", 0) == 0 else "has_missing"
        return result

    def download_missing_tracks(
        self,
        artist: str,
        release_title: str,
        missing_tracks: List[Dict[str, Any]],
        preferred_format: str = "flac",
        search_timeout: float = 25.0,
        dry_run: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> Dict[str, Any]:
        """
        Orchestrates Soulseek discovery and queueing for missing tracks of a release.
        Attempts smart whole-album peer matching first, followed by individual track searches.
        """
        if not missing_tracks:
            return {"status": "skipped", "message": "No missing tracks to download.", "queued_count": 0}

        console.print(f"[cyan]Initiating Soulseek download for {len(missing_tracks)} missing tracks of '{artist} - {release_title}'...[/cyan]")
        if on_progress:
            on_progress(10, 100, f"Searching Soulseek for album: {artist} {release_title}...")

        queued_files: List[Dict[str, Any]] = []
        resolved_missing: Set[str] = set()

        # -------------------------------------------------------------
        # STAGE 1: Peer Directory Album Match
        # -------------------------------------------------------------
        album_query = f"{artist} {release_title}".strip()
        search_res = self.slskd_client.search(query=album_query, timeout=search_timeout)
        responses = search_res.get("responses", [])

        if responses:
            if on_progress:
                on_progress(40, 100, "Evaluating peer directory candidates...")

            # Group files by user and folder
            for resp in responses:
                user = resp.get("username")
                files = resp.get("files", [])
                if not user or not files:
                    continue

                folder_files: Dict[str, List[Dict[str, Any]]] = {}
                for f in files:
                    fn = f.get("filename", "")
                    ext = Path(fn).suffix.lower()
                    if ext in AUDIO_EXTENSIONS and not f.get("isLocked"):
                        folder_name = os.path.dirname(fn.replace("\\", "/"))
                        folder_files.setdefault(folder_name, []).append(f)

                # Check if this folder has files matching the missing tracks
                for d_name, d_files in folder_files.items():
                    to_queue_for_peer: List[Dict[str, Any]] = []

                    for m_trk in missing_tracks:
                        m_title = m_trk.get("title", "")
                        if m_title in resolved_missing:
                            continue

                        p_missing = parse_track_title_structure(m_title)

                        for remote_f in d_files:
                            r_fn = remote_f.get("filename", "")
                            base_fn = os.path.basename(r_fn.replace("\\", "/"))
                            p_remote = parse_track_title_structure(base_fn)

                            sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
                            ver_compat = are_versions_compatible(
                                p_missing["version_type"], p_missing["version_text"],
                                p_remote["version_type"], p_remote["version_text"]
                            )

                            if (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.85) and ver_compat:
                                to_queue_for_peer.append({
                                    "filename": r_fn,
                                    "size": remote_f.get("size", 0),
                                    "title": m_title
                                })
                                resolved_missing.add(m_title)
                                break

                    if to_queue_for_peer:
                        if not dry_run:
                            try:
                                self.slskd_client.enqueue_download(user, to_queue_for_peer)
                            except Exception as e:
                                console.print(f"[yellow]Failed to queue from peer {user}: {e}[/yellow]")
                        queued_files.extend(to_queue_for_peer)

        # -------------------------------------------------------------
        # STAGE 2: Individual Missing Track Search
        # -------------------------------------------------------------
        remaining_missing = [m for m in missing_tracks if m.get("title") not in resolved_missing]
        if remaining_missing:
            if on_progress:
                on_progress(60, 100, f"Searching individual Soulseek tracks for {len(remaining_missing)} remaining items...")

            track_queries = [f"{artist} - {m.get('title')}".strip() for m in remaining_missing]
            batch_results = self.slskd_client.batch_search(track_queries, timeout=search_timeout)

            for m_trk in remaining_missing:
                m_title = m_trk.get("title", "")
                q_key = f"{artist} - {m_title}".strip()
                s_data = batch_results.get(q_key, {})
                s_responses = s_data.get("responses", [])

                best_candidate: Optional[Tuple[str, Dict[str, Any]]] = None
                best_score = -1

                for resp in s_responses:
                    user = resp.get("username")
                    for f in resp.get("files", []):
                        fn = f.get("filename", "")
                        ext = Path(fn).suffix.lower()
                        if ext not in AUDIO_EXTENSIONS or f.get("isLocked"):
                            continue

                        base_fn = os.path.basename(fn.replace("\\", "/"))
                        p_missing = parse_track_title_structure(m_title)
                        p_remote = parse_track_title_structure(base_fn)

                        sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
                        ver_compat = are_versions_compatible(
                            p_missing["version_type"], p_missing["version_text"],
                            p_remote["version_type"], p_remote["version_text"]
                        )

                        if (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.80) and ver_compat:
                            fmt_label, score = AudioQualityAnalyzer.determine_stream_quality(f)
                            if score > best_score:
                                best_score = score
                                best_candidate = (user, f)

                if best_candidate:
                    u, cand_f = best_candidate
                    item = {
                        "filename": cand_f.get("filename"),
                        "size": cand_f.get("size", 0),
                        "title": m_title,
                        "user": u
                    }
                    if not dry_run:
                        try:
                            self.slskd_client.enqueue_download(u, [item])
                        except Exception as e:
                            console.print(f"[yellow]Failed to queue track from peer {u}: {e}[/yellow]")
                    queued_files.append(item)
                    resolved_missing.add(m_title)

        if on_progress:
            on_progress(100, 100, f"Completed: Queued {len(queued_files)} missing track files.")

        return {
            "artist": artist,
            "release": release_title,
            "total_missing": len(missing_tracks),
            "queued_count": len(queued_files),
            "resolved_count": len(resolved_missing),
            "dry_run": dry_run,
            "queued_files": queued_files
        }

    def download_single_missing_track(
        self,
        artist: str,
        release_title: str,
        track_title: str,
        preferred_format: str = "flac",
        search_timeout: float = 20.0,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Searches Soulseek and downloads a single missing track.
        """
        query = f"{artist} {track_title}".strip()
        search_res = self.slskd_client.search(query=query, timeout=search_timeout)
        responses = search_res.get("responses", [])

        best_candidate: Optional[Tuple[str, Dict[str, Any]]] = None
        best_score = -1

        p_missing = parse_track_title_structure(track_title)

        for resp in responses:
            user = resp.get("username")
            for f in resp.get("files", []):
                fn = f.get("filename", "")
                ext = Path(fn).suffix.lower()
                if ext not in AUDIO_EXTENSIONS or f.get("isLocked"):
                    continue

                base_fn = os.path.basename(fn.replace("\\", "/"))
                p_remote = parse_track_title_structure(base_fn)

                sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
                ver_compat = are_versions_compatible(
                    p_missing["version_type"], p_missing["version_text"],
                    p_remote["version_type"], p_remote["version_text"]
                )

                if (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.75) and ver_compat:
                    fmt_label, score = AudioQualityAnalyzer.determine_stream_quality(f)
                    if score > best_score:
                        best_score = score
                        best_candidate = (user, f)

        if not best_candidate:
            return {
                "success": False,
                "message": f"No compatible peer matches found for '{artist} - {track_title}'.",
                "artist": artist,
                "track": track_title
            }

        user, file_obj = best_candidate
        payload = [{
            "filename": file_obj.get("filename"),
            "size": file_obj.get("size", 0)
        }]

        if not dry_run:
            self.slskd_client.enqueue_download(user, payload)

        return {
            "success": True,
            "artist": artist,
            "track": track_title,
            "user": user,
            "filename": file_obj.get("filename"),
            "size": file_obj.get("size", 0),
            "dry_run": dry_run
        }
