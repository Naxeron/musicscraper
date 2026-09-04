"""
Library release scanner, release tracklist reconciler, and missing track downloader.
"""

import os
import re
import json
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any, Callable, Union
from concurrent.futures import ThreadPoolExecutor

from unidecode import unidecode

from musicscraper.config import Config
from musicscraper.core.constants import (
    AUDIO_EXTENSIONS,
    IGNORED_SCAN_DIR_NAMES,
    IGNORED_SCAN_DIR_PREFIXES,
    VA_DIR_MARKERS,
)
from musicscraper.core.text import (
    normalize_text,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
    strip_track_number_and_artist,
)
from musicscraper.core.audio import AudioMetadata, AudioQualityAnalyzer, DISC_DIR_PATTERN
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.report import console
from musicscraper.clients.musicbrainz import MusicBrainzClient
from musicscraper.clients.slskd import SlskdClient, SlskdAPIError
from musicscraper.services.reconciler import (
    is_purely_numeric_track,
    is_track_number_match,
    have_conflicting_numbers,
    have_conflicting_track_numbers,
)


def parse_disc_and_track_number(
    raw_track: Any,
    filename: Optional[str] = None,
    meta_disc: Optional[int] = None
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Robustly parses (disc_number, track_number, total_tracks).
    Handles:
    - Standard: '1', '01', '1/12'
    - Disc-track prefix: '1-01', '1.01', '2-03', '01-02'
    - Vinyl side notation: 'A1', 'B1', 'A', 'B', 'Side A', 'Side B', 'Vinyl Side B'
    - Filename prefixes: '1-01 - Title.flac', '2.01 Song.flac', 'A1 Title.mp3', 'Side B 01.flac', '01.flac'
    - Disc notation in raw_track or filename overrides default disc_num == 1.
    """
    disc_num = meta_disc if (meta_disc is not None and meta_disc > 0) else None
    track_num = None
    total_tracks = None

    if raw_track is not None:
        raw_str = str(raw_track).strip()
        if "/" in raw_str:
            parts = raw_str.split("/", 1)
            raw_str = parts[0].strip()
            tot_str = re.sub(r"[^\d]", "", parts[1])
            if tot_str:
                try:
                    total_tracks = int(tot_str)
                except ValueError:
                    pass

        # Side notation with letter in tag: 'Side A', 'Side B', 'Side B 02', 'Vinyl Side B', 'Side B-01'
        m_side = re.match(r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*([a-zA-Z])(?:\s*[-_.]?\s*(\d{1,3}))?$", raw_str, re.IGNORECASE)
        if m_side:
            letter = m_side.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord('A') + 1
            num_part = m_side.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Side notation with number in tag: 'Side 1', 'Side 2', 'Side 2 - 01'
        m_side_num = re.match(r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.]?\s*(\d{1,3}))?$", raw_str, re.IGNORECASE)
        if m_side_num:
            d_val = int(m_side_num.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            num_part = m_side_num.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Disc/CD with number in tag: 'Disc 2 - 01', 'CD 2 - 01', 'Disc 2'
        m_disc = re.match(r"^(?:disc|cd|disk)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.:]\s*(\d{1,3})|\s+track\s+(\d{1,3}))?$", raw_str, re.IGNORECASE)
        if m_disc:
            d_val = int(m_disc.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            num_part = m_disc.group(2) or m_disc.group(3)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Vinyl side: A1, B2, A, B
        m_vinyl = re.match(r"^([A-Za-z])(\d{1,3})?$", raw_str)
        if m_vinyl:
            letter = m_vinyl.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord('A') + 1
            num_part = m_vinyl.group(2)
            track_num = int(num_part) if num_part else 1
            return disc_num, track_num, total_tracks

        # Disc-track pattern: "1-01", "1.01", "2-03", "01-02"
        m_dt = re.match(r"^(\d{1,2})[-_.](\d{1,3})$", raw_str)
        if m_dt:
            d_val = int(m_dt.group(1))
            t_val = int(m_dt.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            track_num = t_val
            return disc_num, track_num, total_tracks

        # Standard digits
        digits_only = re.sub(r"[^\d]", "", raw_str)
        if digits_only:
            try:
                track_num = int(digits_only)
            except ValueError:
                pass

    # Inspect filename for disc/track information
    if filename:
        fn = Path(filename).name

        # Filename side notation with letter: 'Side B 01 - Song.flac', 'Side B - 02.mp3', 'Side B.flac'
        m_fn_side = re.match(r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*([a-zA-Z])(?:\s*[-_.]?\s*(\d{1,3}))?(?:[\s._\-]|$)", fn, re.IGNORECASE)
        if m_fn_side:
            letter = m_fn_side.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord('A') + 1
            if track_num is None:
                track_num = int(m_fn_side.group(2)) if m_fn_side.group(2) else 1
            return disc_num, track_num, total_tracks

        # Filename side notation with number: 'Side 2 - 01 Song.flac'
        m_fn_side_num = re.match(r"^(?:(?:vinyl|lp)\s+)?(?:side)\s*[-_.]?\s*(\d{1,2})(?:\s*[-_.]?\s*(\d{1,3}))?(?:[\s._\-]|$)", fn, re.IGNORECASE)
        if m_fn_side_num:
            d_val = int(m_fn_side_num.group(1))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = int(m_fn_side_num.group(2)) if m_fn_side_num.group(2) else 1
            return disc_num, track_num, total_tracks

        # Filename Disc/CD prefix: 'Disc 2 - 01 Song.flac', 'CD 2 - 01 Song.flac'
        m_fn_disc = re.match(r"^(?:disc|cd|disk)\s*[-_.]?\s*(\d{1,2})\s*[-_.:\s]\s*(?:track\s+)?(\d{1,3})(?:[\s._\-]|$)", fn, re.IGNORECASE)
        if m_fn_disc:
            d_val = int(m_fn_disc.group(1))
            t_val = int(m_fn_disc.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Filename vinyl notation: 'B1 Song.flac', 'B01 - Song.flac', 'A1 - Title.mp3'
        m_fn_vinyl = re.match(r"^([A-Za-z])(\d{1,3})(?:[\s._\-]|$)", fn)
        if m_fn_vinyl:
            letter = m_fn_vinyl.group(1).upper()
            if disc_num is None or disc_num == 1:
                disc_num = ord(letter) - ord('A') + 1
            if track_num is None:
                track_num = int(m_fn_vinyl.group(2))
            return disc_num, track_num, total_tracks

        # Filename disc-track prefix: '2-01 Song.flac', '2.01 Song.flac', '02-01 Song.flac'
        m_fn_dt = re.match(r"^(\d{1,2})[-_.](\d{1,3})(?:[\s._\-]|$)", fn)
        if m_fn_dt:
            d_val = int(m_fn_dt.group(1))
            t_val = int(m_fn_dt.group(2))
            if (disc_num is None or disc_num == 1) and 1 <= d_val <= 20:
                disc_num = d_val
            if track_num is None:
                track_num = t_val
            return disc_num, track_num, total_tracks

        # Standard track number fallback from filename (only if track_num not already resolved)
        if track_num is None:
            m_fn = re.match(r"^(\d{1,3})(?:[\s._\-]|$)", fn)
            if m_fn:
                try:
                    track_num = int(m_fn.group(1))
                except ValueError:
                    pass

    if disc_num is None:
        disc_num = 1

    return disc_num, track_num, total_tracks


def _is_numeric_track_item(lt: Dict[str, Any]) -> bool:
    """Checks if track is purely numeric or compound disc-track number with no title."""
    if is_purely_numeric_track(lt):
        return True
    raw_title = (lt.get("title") or "").strip()
    raw_fn = Path(lt.get("filename") or lt.get("path") or "").stem.strip()
    return bool(
        re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_fn)
        and (
            not raw_title
            or raw_title.isdigit()
            or raw_title == raw_fn
            or re.match(r"^(?:track|trk)[\s._\-]*\d{1,3}$", raw_title, re.IGNORECASE)
            or re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_title)
        )
    )


def _format_artist_credit(ac_list: Any, default: str = "") -> str:
    """Formats a MusicBrainz artist-credit list into a clean display string."""
    if not ac_list:
        return default
    if isinstance(ac_list, str):
        return ac_list
    parts = []
    for c in ac_list:
        if isinstance(c, dict):
            name = c.get("name") or c.get("artist", {}).get("name", "")
            join = c.get("joinphrase", "")
            parts.append(name + join)
        else:
            parts.append(str(c))
    return "".join(parts).strip() or default


def _is_va_string(name: Optional[str]) -> bool:
    """Checks if an artist or tag string represents Various Artists."""
    if not name:
        return False
    norm = name.strip().lower()
    return (
        norm in VA_DIR_MARKERS
        or norm in ("various artists", "various", "va", "v.a.", "v/a", "compilation", "compilations")
        or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", norm))
    )


def _is_va_directory(path: Path) -> bool:
    """Checks if a folder name or parent directory path matches compilation / VA patterns."""
    p_name = path.name.lower().strip()
    gp_name = path.parent.name.lower().strip() if path.parent != path else ""
    for name in (p_name, gp_name):
        if name in VA_DIR_MARKERS:
            return True
        if bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", name)):
            return True
        if name.startswith(("va - ", "va-", "va ", "v.a. - ", "various artists - ", "various - ", "[va]", "(va)")):
            return True
        if "[va]" in name or "(va)" in name or name.endswith(" [va]") or name.endswith(" (va)"):
            return True
    return False


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

        # Pre-analyze directories to detect multi-artist compilation folders
        folder_artists: Dict[str, Set[str]] = {}
        for meta in all_tracks:
            folder_key = str(meta.path.parent)
            art = meta.artist.strip() if meta.artist else ""
            if art and not _is_va_string(art) and art.lower() != "unknown artist":
                folder_artists.setdefault(folder_key, set()).add(art.lower())

        for meta in all_tracks:
            folder = str(meta.path.parent)
            is_folder_multi_artist = len(folder_artists.get(folder, set())) > 1
            is_va = (
                _is_va_string(meta.album_artist)
                or _is_va_string(meta.artist)
                or is_folder_multi_artist
                or _is_va_directory(meta.path.parent)
            )

            if is_va:
                artist = "Various Artists"
                album_artist = "Various Artists"
            else:
                artist = meta.album_artist or meta.artist or "Unknown Artist"
                album_artist = meta.album_artist or artist

            album = meta.album
            # Derive clean album name fallback from parent folder if not tagged
            if not album:
                folder_name = meta.path.parent.name
                album = folder_name if folder_name else "Unknown Album"

            norm_artist = normalize_text(artist)
            norm_album = normalize_text(album)

            # Check if MB release ID is present
            mb_rel_id = next(iter(meta.mb_release_ids)) if meta.mb_release_ids else None

            # Detect if file is in a disc subfolder
            is_disc_folder = bool(DISC_DIR_PATTERN.match(meta.path.parent.name))
            effective_folder_path = meta.path.parent.parent if is_disc_folder else meta.path.parent
            effective_folder_str = str(effective_folder_path)

            # Generate unique release key
            if is_va:
                if norm_album and norm_album not in ("", "unknown album", "unknown", "untitled", "album"):
                    rel_key = f"va_{norm_album}"
                elif mb_rel_id:
                    rel_key = f"mb_{mb_rel_id}"
                else:
                    rel_key = f"dir_{hashlib.md5(effective_folder_str.encode()).hexdigest()[:12]}"
            elif norm_artist and norm_album:
                rel_key = f"art_alb_{norm_artist}::{norm_album}"
            elif mb_rel_id:
                rel_key = f"mb_{mb_rel_id}"
            else:
                rel_key = f"dir_{hashlib.md5(effective_folder_str.encode()).hexdigest()[:12]}"

            if rel_key not in releases_map:
                rel_id = hashlib.md5(rel_key.encode()).hexdigest()[:16]
                try:
                    rel_folder = str(effective_folder_path.relative_to(lib_path))
                except Exception:
                    rel_folder = effective_folder_str

                releases_map[rel_key] = {
                    "id": rel_id,
                    "rel_key": rel_key,
                    "title": album,
                    "artist": artist,
                    "album_artist": album_artist,
                    "is_va": is_va,
                    "year": meta.year or "",
                    "folder_path": rel_folder,
                    "full_path": effective_folder_str,
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
            else:
                rel = releases_map[rel_key]
                try:
                    curr_folder = str(effective_folder_path.relative_to(lib_path))
                except Exception:
                    curr_folder = effective_folder_str
                # If existing folder is in 'downloads' but this track is in a proper library folder, prefer library folder
                if "download" in rel["folder_path"].lower() and "download" not in curr_folder.lower():
                    rel["folder_path"] = curr_folder
                    rel["full_path"] = effective_folder_str
                if mb_rel_id and not rel.get("mb_release_id"):
                    rel["mb_release_id"] = mb_rel_id

            rel = releases_map[rel_key]
            if mb_rel_id and not rel.get("mb_release_id"):
                rel["mb_release_id"] = mb_rel_id
            fmt_label = meta.format_label or meta.file_type.lstrip(".").upper()
            if fmt_label:
                rel["formats"].add(fmt_label)
            if not meta.is_lossless:
                rel["is_lossless_all"] = False
            if meta.year and not rel["year"]:
                rel["year"] = meta.year

            # Track number and disc number parsing
            parsed_disc, trk_num, total_in_tag = parse_disc_and_track_number(
                meta.track_number,
                filename=meta.path.name,
                meta_disc=getattr(meta, "disc_number", None)
            )
            effective_disc = parsed_disc or getattr(meta, "disc_number", 1) or 1
            effective_total_discs = getattr(meta, "total_discs", 1) or 1
            if effective_total_discs < effective_disc:
                effective_total_discs = effective_disc

            if total_in_tag and total_in_tag > rel["total_tracks_expected"]:
                rel["total_tracks_expected"] = total_in_tag

            rel["tracks"].append({
                "path": str(meta.path),
                "filename": meta.path.name,
                "title": meta.title or meta.path.stem,
                "artist": meta.artist or artist,
                "disc_number": effective_disc,
                "total_discs": effective_total_discs,
                "track_number": str(trk_num) if trk_num is not None else None,
                "track_num_int": trk_num,
                "total_in_tag": total_in_tag,
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
            # Deduplicate multiple files for the same track (e.g. file in Library/ and file in downloads/)
            deduped_found: Dict[Any, Dict[str, Any]] = {}
            for t in rel["tracks"]:
                d_num = t.get("disc_number", 1) or 1
                d_key = (
                    d_num,
                    t["track_num_int"],
                    normalize_text(t["title"])
                ) if t.get("track_num_int") is not None else (
                    d_num,
                    normalize_text(t["title"])
                )
                if d_key not in deduped_found:
                    deduped_found[d_key] = t
                else:
                    existing = deduped_found[d_key]
                    existing_is_lib = "download" not in (existing.get("path") or "").lower()
                    new_is_lib = "download" not in (t.get("path") or "").lower()
                    if (new_is_lib and not existing_is_lib) or (
                        new_is_lib == existing_is_lib
                        and (t.get("quality_score", 0) > existing.get("quality_score", 0))
                    ):
                        deduped_found[d_key] = t
            found_tracks = list(deduped_found.values())

            # Group found tracks by disc
            discs: Dict[int, List[Dict[str, Any]]] = {}
            disc_expected_tags: Dict[int, int] = {}
            for t in found_tracks:
                d = t.get("disc_number", 1) or 1
                discs.setdefault(d, []).append(t)
                tot = t.get("total_in_tag")
                if tot and tot > disc_expected_tags.get(d, 0):
                    disc_expected_tags[d] = tot

            max_disc_num = max(
                max((t.get("disc_number", 1) or 1 for t in found_tracks), default=1),
                max((t.get("total_discs", 1) or 1 for t in found_tracks), default=1),
            )
            if max_disc_num > 1:
                for d in range(1, max_disc_num + 1):
                    discs.setdefault(d, [])

            all_tracks_list: List[Dict[str, Any]] = list(found_tracks)
            is_multi_disc = len(discs) > 1

            for d_num, d_tracks in sorted(discs.items()):
                found_nums = {
                    t["track_num_int"] for t in d_tracks
                    if t.get("track_num_int") is not None and t["track_num_int"] > 0
                }
                max_trk_num = max(found_nums) if found_nums else 0
                expected_from_tag = disc_expected_tags.get(d_num, 0)
                disc_expected = max(expected_from_tag, max_trk_num)

                if 0 < disc_expected <= 60:
                    for k in range(1, disc_expected + 1):
                        if k not in found_nums:
                            missing_title = (
                                f"Disc {d_num} Track {k:02d} (Missing)"
                                if is_multi_disc
                                else f"Track {k:02d} (Missing)"
                            )
                            all_tracks_list.append({
                                "disc_number": d_num,
                                "track_number": str(k),
                                "track_num_int": k,
                                "title": missing_title,
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

            # Sort tracks by disc_number, track_num_int, and title/filename
            def _sort_key(t: Dict[str, Any]) -> Tuple[int, int, str]:
                d = t.get("disc_number", 1) or 1
                tn = t.get("track_num_int")
                order = tn if (tn is not None and tn > 0) else 9999
                return d, order, t.get("filename") or t.get("title", "")

            all_tracks_list.sort(key=_sort_key)
            rel["tracks"] = all_tracks_list
            rel["formats"] = sorted(list(rel["formats"]))

            found_count = sum(1 for t in all_tracks_list if t["status"] == "found")
            missing_count = sum(1 for t in all_tracks_list if t["status"] == "missing")
            total_count = len(all_tracks_list)

            rel["found_count"] = found_count
            rel["missing_count"] = missing_count
            rel["total_tracks_expected"] = max(rel["total_tracks_expected"], total_count)

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

        # Defensive normalization: ensure disc_number and track_number are accurately parsed from filenames
        for lt in local_tracks:
            curr_disc = lt.get("disc_number")
            if not curr_disc or curr_disc == 1:
                p_disc, p_trk, _ = parse_disc_and_track_number(
                    lt.get("track_number"),
                    filename=lt.get("filename") or lt.get("path"),
                    meta_disc=curr_disc
                )
                if p_disc and p_disc > 1:
                    lt["disc_number"] = p_disc
                if p_trk is not None:
                    if not lt.get("track_number") or lt.get("track_number") == "-":
                        lt["track_number"] = str(p_trk)
                    if lt.get("track_num_int") is None:
                        lt["track_num_int"] = p_trk

        mb_release = None

        # 1. Look up by MBID if available
        if mb_rel_id:
            mb_release = self.mb_client.get_release_by_id(mb_rel_id, force_refresh=force_refresh)

        # 2. Search MusicBrainz by release title + artist if not found by ID
        if not mb_release and title:
            search_results = self.mb_client.search_release(release_title=title, artist_name=artist, limit=5)
            if search_results:
                best_cand = None
                norm_target_title = normalize_text(title)
                for cand in search_results:
                    cand_title = normalize_text(cand.get("title", ""))
                    if cand_title == norm_target_title or calculate_similarity(cand_title, norm_target_title) >= 0.70:
                        best_cand = cand
                        break
                if best_cand:
                    best_id = best_cand.get("id")
                    if best_id:
                        mb_release = self.mb_client.get_release_by_id(best_id, force_refresh=force_refresh)

        # 3. If MB release found, reconcile official tracks against local files
        if mb_release:
            mb_artist = _format_artist_credit(mb_release.get("artist-credit")) or mb_release.get("artist-credit-phrase", "")
            rg = mb_release.get("release-group", {})
            rg_type = (rg.get("primary-type") or "").lower()
            rg_sec_types = [t.lower() for t in rg.get("secondary-type-list", [])]
            is_mb_compilation = (
                rg_type == "compilation"
                or "compilation" in rg_sec_types
                or _is_va_string(mb_artist)
                or release_data.get("is_va", False)
            )
            rel_artist = "Various Artists" if is_mb_compilation else (mb_artist or artist)

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
                    trk_artist = _format_artist_credit(trk.get("artist-credit") or rec.get("artist-credit"))
                    if not trk_artist:
                        trk_artist = rel_artist

                    official_tracks.append({
                        "disc_number": disc_num,
                        "track_number": trk_num,
                        "title": trk_title,
                        "artist": trk_artist,
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

                # Pass 2: Track number + Title match (with numeric filename support)
                if not matched_local:
                    for idx, lt in enumerate(local_tracks):
                        if idx in matched_local_indices:
                            continue

                        # Check disc number alignment if both are specified
                        lt_disc = lt.get("disc_number")
                        off_disc = off_trk.get("disc_number")
                        if lt_disc is not None and off_disc is not None and int(lt_disc) != int(off_disc):
                            continue

                        lt_num = lt.get("track_number")
                        off_num = off_trk.get("track_number")

                        if is_track_number_match(lt_num, off_num):
                            p_lt = parse_track_title_structure(lt.get("title", ""))

                            # Fast-path: local file is purely numeric (e.g. 01.flac, 1-01.flac) in confirmed album
                            if _is_numeric_track_item(lt):
                                matched_local = lt
                                matched_local_indices.add(idx)
                                break

                            # Title match with numeric conflict guardrail
                            has_num_conflict = have_conflicting_numbers(p_off["base_norm"], p_lt["base_norm"])
                            if not has_num_conflict:
                                sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                                ver_compat = are_versions_compatible(
                                    p_off["version_type"], p_off["version_text"],
                                    p_lt["version_type"], p_lt["version_text"]
                                )
                                if ((p_off["base_norm"] == p_lt["base_norm"]) or sim >= 0.70) and ver_compat:
                                    matched_local = lt
                                    matched_local_indices.add(idx)
                                    break

                # Pass 3: High title similarity
                if not matched_local:
                    for idx, lt in enumerate(local_tracks):
                        if idx in matched_local_indices:
                            continue

                        if _is_numeric_track_item(lt):
                            continue

                        # Check disc number alignment if both are specified
                        lt_disc = lt.get("disc_number")
                        off_disc = off_trk.get("disc_number")
                        if lt_disc is not None and off_disc is not None:
                            try:
                                if int(lt_disc) != int(off_disc):
                                    continue
                            except (ValueError, TypeError):
                                pass

                        p_lt = parse_track_title_structure(lt.get("title", ""))

                        if have_conflicting_numbers(p_off["base_norm"], p_lt["base_norm"]):
                            continue

                        if (
                            p_off["base_norm"] != p_lt["base_norm"]
                            and have_conflicting_track_numbers(off_trk.get("track_number"), lt.get("track_number"))
                        ):
                            continue

                        sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                        ver_compat = are_versions_compatible(
                            p_off["version_type"], p_off["version_text"],
                            p_lt["version_type"], p_lt["version_text"]
                        )
                        if ((p_off["base_norm"] == p_lt["base_norm"] or sim >= 0.85) and ver_compat):
                            matched_local = lt
                            matched_local_indices.add(idx)
                            break

                eff_trk_artist = off_trk.get("artist") or (matched_local.get("artist") if matched_local else None) or rel_artist

                if matched_local:
                    reconciled_tracklist.append({
                        "disc_number": off_trk["disc_number"],
                        "track_number": off_trk["track_number"],
                        "title": off_trk["title"],
                        "artist": eff_trk_artist,
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
                        "artist": eff_trk_artist,
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
                        "disc_number": lt.get("disc_number", 1) or 1,
                        "track_number": lt.get("track_number") or "-",
                        "title": lt.get("title") or lt.get("filename"),
                        "artist": lt.get("artist") or rel_artist,
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

            # Sort tracks cleanly by (disc_number, track_number)
            def _audit_sort_key(t: Dict[str, Any]) -> Tuple[int, int, str]:
                dn = t.get("disc_number") or 1
                tn_val = t.get("track_number")
                try:
                    tn_int = int(re.sub(r"[^\d]", "", str(tn_val))) if tn_val else 9999
                except Exception:
                    tn_int = 9999
                return dn, tn_int, t.get("title", "")

            reconciled_tracklist.sort(key=_audit_sort_key)

            total_tracks = len(reconciled_tracklist)
            found_count = sum(1 for t in reconciled_tracklist if t["status"] == "found")
            missing_count = sum(1 for t in reconciled_tracklist if t["status"] == "missing")
            completion_pct = round((found_count / total_tracks * 100.0), 1) if total_tracks > 0 else 100.0

            result = dict(release_data)
            result["artist"] = rel_artist
            result["album_artist"] = rel_artist
            result["is_va"] = is_mb_compilation
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

        # Check if release is compilation / Various Artists
        is_va = _is_va_string(artist)
        track_artists = {
            m.get("artist").strip()
            for m in missing_tracks
            if m.get("artist") and not _is_va_string(m.get("artist")) and m.get("artist").strip().lower() != "unknown artist"
        }
        if len(track_artists) > 1 or (len(track_artists) == 1 and not is_va and next(iter(track_artists)).lower() != artist.strip().lower()):
            is_va = True

        # Check if any missing tracks have placeholder names like "Track 01 (Missing)" or "Disc 1 Track 02 (Missing)"
        has_generic_placeholders = any(
            re.match(r"^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$", m.get("title", ""), re.IGNORECASE)
            for m in missing_tracks
        )
        if has_generic_placeholders and release_title:
            try:
                stub_rel = {
                    "artist": artist,
                    "title": release_title,
                    "tracks": missing_tracks,
                    "total_tracks_expected": len(missing_tracks),
                }
                audited = self.audit_release(stub_rel)
                audited_missing = [t for t in audited.get("tracks", []) if t.get("status") == "missing"]
                if audited_missing and not any(re.match(r"^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$", t.get("title", ""), re.IGNORECASE) for t in audited_missing):
                    missing_tracks = audited_missing
            except Exception as e:
                console.print(f"[yellow]Could not auto-resolve placeholder track titles: {e}[/yellow]")

        effective_artist = "Various Artists" if is_va else artist
        console.print(f"[cyan]Initiating Soulseek download for {len(missing_tracks)} missing tracks of '{effective_artist} - {release_title}'...[/cyan]")
        if on_progress:
            on_progress(10, 100, f"Searching Soulseek for album: {effective_artist} {release_title}...")

        queued_files: List[Dict[str, Any]] = []
        resolved_missing: Set[str] = set()

        # -------------------------------------------------------------
        # STAGE 1: Peer Directory Album Match
        # -------------------------------------------------------------
        if is_va:
            album_queries = [
                f"Various Artists {release_title}".strip(),
                f"{release_title}".strip(),
                f"VA {release_title}".strip(),
            ]
        else:
            album_queries = [f"{artist} {release_title}".strip()]

        search_res = None
        for q in album_queries:
            search_res = self.slskd_client.search(query=q, timeout=search_timeout)
            if search_res.get("responses"):
                break

        # Fallback album query if specific query yielded 0 responses
        if (not search_res or not search_res.get("responses")) and not is_va and len(missing_tracks) >= 2 and release_title:
            for fallback_q in (f"Various Artists {release_title}".strip(), release_title.strip()):
                fallback_res = self.slskd_client.search(query=fallback_q, timeout=search_timeout)
                if fallback_res.get("responses"):
                    search_res = fallback_res
                    break

        responses = search_res.get("responses", []) if search_res else []

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

                        is_placeholder = bool(re.match(r"^Track\s+\d+\s*\(Missing\)$", m_title, re.IGNORECASE))
                        m_trk_num = m_trk.get("track_num_int")
                        p_missing = parse_track_title_structure(m_title)

                        for remote_f in d_files:
                            r_fn = remote_f.get("filename", "")
                            base_fn = os.path.basename(r_fn.replace("\\", "/"))
                            p_remote = parse_track_title_structure(base_fn)

                            # If placeholder, match by track number prefix
                            if is_placeholder and m_trk_num:
                                trk_prefix_match = re.match(r"^0*" + str(m_trk_num) + r"[\s._\-]", base_fn)
                                if trk_prefix_match:
                                    to_queue_for_peer.append({
                                        "filename": r_fn,
                                        "size": remote_f.get("size", 0),
                                        "title": m_title
                                    })
                                    resolved_missing.add(m_title)
                                    break
                                continue

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

            track_queries = []
            trk_query_map: Dict[str, Dict[str, Any]] = {}

            for m in remaining_missing:
                m_title = m.get("title", "")
                m_artist = (m.get("artist") or "").strip()
                if re.match(r"^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$", m_title, re.IGNORECASE):
                    # Cannot search Soulseek for generic "Track 02 (Missing)"
                    continue

                if m_artist and not _is_va_string(m_artist) and m_artist.lower() != "unknown artist":
                    q = f"{m_artist} - {m_title}".strip()
                elif is_va:
                    q = f"Various Artists {m_title}".strip()
                else:
                    q = f"{artist} - {m_title}".strip()

                track_queries.append(q)
                trk_query_map[q] = m

            batch_results = self.slskd_client.batch_search(track_queries, timeout=search_timeout) if track_queries else {}

            for q_key, m_trk in trk_query_map.items():
                m_title = m_trk.get("title", "")
                m_artist = (m_trk.get("artist") or "").strip()
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

                        artist_matched = True
                        if m_artist and not _is_va_string(m_artist) and m_artist.lower() != "unknown artist":
                            norm_ma = normalize_text(m_artist)
                            norm_fn = normalize_text(base_fn)
                            if norm_ma not in norm_fn and sim < 0.90:
                                artist_matched = False

                        if artist_matched and (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.80) and ver_compat:
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
            "artist": effective_artist,
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
        track_artist: Optional[str] = None,
        track_number: Optional[Union[int, str]] = None,
        preferred_format: str = "flac",
        search_timeout: float = 28.0,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Searches Soulseek and downloads a single missing track.
        """
        # Auto-resolve placeholder titles like "Track 01 (Missing)" or "Disc 1 Track 02 (Missing)" via MusicBrainz audit
        m_match = re.match(r"^(?:Disc\s+(\d+)\s+)?Track\s+(\d+)\s*\(Missing\)$", track_title, re.IGNORECASE)
        disc_from_m = int(m_match.group(1)) if (m_match and m_match.group(1)) else None
        track_from_m = int(m_match.group(2)) if (m_match and m_match.group(2)) else None
        t_num = track_number or track_from_m

        if (m_match or not track_title or "missing" in track_title.lower()) and release_title:
            try:
                stub_rel = {
                    "artist": artist,
                    "title": release_title,
                    "tracks": [],
                    "total_tracks_expected": 0,
                }
                audited = self.audit_release(stub_rel)
                for trk in audited.get("tracks", []):
                    trk_idx = trk.get("track_num_int") or trk.get("track_number")
                    trk_d = trk.get("disc_number", 1) or 1
                    disc_matches = (disc_from_m is None) or (trk_d == disc_from_m)

                    if t_num and str(trk_idx) == str(t_num) and disc_matches:
                        if trk.get("title") and not re.match(r"^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$", trk["title"], re.IGNORECASE):
                            resolved_title = trk["title"]
                            console.print(f"[green]Resolved placeholder '{track_title}' to '{resolved_title}' via MusicBrainz[/green]")
                            track_title = resolved_title
                            if not track_artist and trk.get("artist"):
                                track_artist = trk.get("artist")
                            break
            except Exception as e:
                console.print(f"[yellow]Could not auto-resolve placeholder track '{track_title}': {e}[/yellow]")

        if re.match(r"^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$", track_title, re.IGNORECASE):
            return {
                "success": False,
                "message": f"Cannot download generic placeholder '{track_title}'. Please audit the release with MusicBrainz to resolve track titles.",
                "artist": artist,
                "track": track_title
            }

        eff_artist = (track_artist or "").strip()
        is_va = _is_va_string(artist)

        if eff_artist and not _is_va_string(eff_artist) and eff_artist.lower() != "unknown artist":
            query = f"{eff_artist} {track_title}".strip()
            display_artist = eff_artist
        elif is_va:
            query = f"Various Artists {track_title}".strip()
            display_artist = "Various Artists"
        else:
            query = f"{artist} {track_title}".strip()
            display_artist = artist

        search_res = self.slskd_client.search(query=query, timeout=search_timeout)
        responses = search_res.get("responses", [])

        # Fallback if "Various Artists {track_title}" yielded 0 responses
        if not responses and is_va and not eff_artist:
            fallback_res = self.slskd_client.search(query=track_title.strip(), timeout=search_timeout)
            if fallback_res.get("responses"):
                responses = fallback_res.get("responses", [])

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
                "message": f"No compatible peer matches found for '{display_artist} - {track_title}'.",
                "artist": display_artist,
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
            "artist": display_artist,
            "track": track_title,
            "user": user,
            "filename": file_obj.get("filename"),
            "size": file_obj.get("size", 0),
            "dry_run": dry_run
        }
