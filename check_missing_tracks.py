#!/usr/bin/env python3
"""
MusicBrainz Missing Tracks Checker
==================================
A fast, comprehensive CLI tool that cross-references your local music library
against MusicBrainz discography data to detect missing tracks, albums,
compilations, and standalone recordings for any artist.

Accounts for:
- Official MusicBrainz aliases and sort names
- Transliterations and romanizations (e.g. Japanese Kanji/Kana <-> Romaji <-> English)
- Alter-egos and related artist personas
- Features, collaborations, splits, remixes, and Various Artists compilations
- Embedded MusicBrainz tag IDs (Track ID, Recording ID, UFID)
- Fuzzy title and album matching
"""

import os
import sys
import re
import csv
import json
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Dict, List, Set, Tuple, Optional, Any

import requests
import musicbrainzngs
import mutagen
from unidecode import unidecode
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.text import Text
from rich.tree import Tree
from rich import box

# Initialize Rich Console
console = Console()

# Configure MusicBrainz User-Agent
APP_NAME = "MusicScraperMissingChecker"
APP_VERSION = "1.0"
APP_CONTACT = "https://github.com/naxeron/musicscraper"
musicbrainzngs.set_useragent(APP_NAME, APP_VERSION, APP_CONTACT)

# Supported Audio File Extensions
AUDIO_EXTENSIONS = {
    '.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg',
    '.opus', '.alac', '.aiff', '.wma', '.ape', '.wv',
    '.dsf', '.dff'
}

# Compilation / VA Directory Indicators
VA_DIR_MARKERS = {
    'various', 'va', 'compilation', 'compilations', 'split',
    'soundtrack', 'soundtracks', 'ost', 'various artists', 'sampler',
    'anthology', 'tribute', 'tributes'
}

# Default Cache Directory
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/musicscraper/mb_cache")


# ==============================================================================
# STRING NORMALIZATION & FUZZY MATCHING HELPERS
# ==============================================================================

def normalize_text(text: Optional[str]) -> str:
    """
    Normalizes text for robust comparison:
    - Lowercases
    - Transliterates unicode (e.g. Japanese to ASCII Romaji approximations)
    - Replaces punctuation and special symbols with spaces
    - Collapses consecutive whitespace
    """
    if not text:
        return ""
    text = text.lower()
    text = unidecode(text)
    # Remove punctuation and special symbols
    text = re.sub(r'[\(\)\[\]\{\}\-_,.\'\"!?:;~`+*#&/\\|]', ' ', text)
    # Collapse multiple whitespaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def strip_track_number_and_artist(filename_no_ext: str) -> str:
    """
    Cleans a filename to extract the track title:
    e.g. '01 すてらべえ - Ultra Cutie Gangsta' -> 'Ultra Cutie Gangsta'
    e.g. '2-11 Stellabee - Enemy' -> 'Enemy'
    """
    cleaned = filename_no_ext.strip()
    # Strip leading track numbers (e.g. '01 - ', '1-02. ', '12 ')
    cleaned = re.sub(r'^(\d+[\-_.]|\d+[\-_.]\d+|\d+)\s*[-_.]*\s*', '', cleaned)
    # If there is an 'Artist - Title' format, take the title
    if ' - ' in cleaned:
        parts = cleaned.split(' - ', 1)
        cleaned = parts[1]
    elif ' _ ' in cleaned:
        parts = cleaned.split(' _ ', 1)
        cleaned = parts[1]
    return cleaned.strip()


def calculate_similarity(str1: str, str2: str) -> float:
    """Calculates SequenceMatcher ratio between two normalized strings."""
    if not str1 or not str2:
        return 0.0
    return SequenceMatcher(None, str1, str2).ratio()


# ==============================================================================
# MUSICBRAINZ RESOLVER & DISCOGRAPHY FETCHER
# ==============================================================================

class MusicBrainzClient:
    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, use_cache: bool = True):
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def resolve_artist_mbid(self, query: str) -> Tuple[str, str]:
        """
        Resolves an artist query (MBID, URL, or Name) to (mbid, canonical_name).
        """
        # 1. Check if query is a MusicBrainz URL
        url_match = re.search(r'musicbrainz\.org/artist/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', query, re.I)
        if url_match:
            mbid = url_match.group(1)
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 2. Check if query is already an MBID UUID
        uuid_match = re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', query.strip(), re.I)
        if uuid_match:
            mbid = query.strip()
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 3. Otherwise, search by query (Lucene full text search across names, aliases, and sortnames)
        console.print(f"[cyan]Searching MusicBrainz for artist:[/cyan] [bold]{query}[/bold]...")
        
        artist_list = []
        try:
            res = musicbrainzngs.search_artists(query=query, limit=10)
            artist_list = res.get('artist-list', [])
        except Exception:
            pass

        if not artist_list:
            try:
                res = musicbrainzngs.search_artists(artist=query, limit=10)
                artist_list = res.get('artist-list', [])
            except Exception:
                pass

        if not artist_list:
            raise ValueError(f"No artist found on MusicBrainz matching query '{query}'.")

        # Pick top match
        top_match = artist_list[0]
        mbid = top_match['id']
        name = top_match.get('name', query)
        disambiguation = f" ({top_match.get('disambiguation')})" if top_match.get('disambiguation') else ""
        country = f" [{top_match.get('country')}]" if top_match.get('country') else ""
        console.print(f"[green]✔ Matched Artist:[/green] [bold]{name}[/bold]{disambiguation}{country} (MBID: {mbid})")
        return mbid, name

    def fetch_full_discography(self, mbid: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Fetches full artist details, aliases, releases, track-releases, and recordings.
        Utilizes disk cache if available.
        """
        cache_file = self.cache_dir / f"artist_{mbid}.json"

        if self.use_cache and not force_refresh and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                console.print(f"[dim]Loaded MusicBrainz catalog from cache ({cache_file.name})[/dim]")
                return data
            except Exception:
                pass

        console.print("[cyan]Fetching artist discography from MusicBrainz API (this may take a few seconds)...[/cyan]")

        # 1. Artist details & aliases & relations
        artist_data = musicbrainzngs.get_artist_by_id(
            mbid,
            includes=['aliases', 'artist-rels', 'recording-rels', 'release-rels', 'release-group-rels', 'url-rels', 'tags']
        )['artist']

        # 2. Browse Releases as primary release artist
        releases_artist = []
        offset = 0
        limit = 100
        while True:
            res = musicbrainzngs.browse_releases(
                artist=mbid,
                limit=limit,
                offset=offset,
                includes=['recordings', 'release-groups', 'artist-credits', 'media']
            )
            rels = res.get('release-list', [])
            releases_artist.extend(rels)
            if len(rels) < limit or len(releases_artist) >= int(res.get('release-count', 0)):
                break
            offset += limit

        # 3. Browse Releases where artist is a track artist (Compilations, VA, Splits)
        releases_track_artist = []
        offset = 0
        while True:
            res = musicbrainzngs.browse_releases(
                track_artist=mbid,
                limit=limit,
                offset=offset,
                includes=['recordings', 'release-groups', 'artist-credits', 'media']
            )
            rels = res.get('release-list', [])
            releases_track_artist.extend(rels)
            if len(rels) < limit or len(releases_track_artist) >= int(res.get('release-count', 0)):
                break
            offset += limit

        # 4. Browse all recordings directly linked to artist
        recordings = []
        offset = 0
        while True:
            res = musicbrainzngs.browse_recordings(
                artist=mbid,
                limit=limit,
                offset=offset,
                includes=['artist-credits', 'work-rels']
            )
            recs = res.get('recording-list', [])
            recordings.extend(recs)
            if len(recs) < limit or len(recordings) >= int(res.get('recording-count', 0)):
                break
            offset += limit

        # 5. Browse release groups
        release_groups = []
        offset = 0
        while True:
            res = musicbrainzngs.browse_release_groups(
                artist=mbid,
                limit=limit,
                offset=offset,
                includes=['artist-credits']
            )
            rgs = res.get('release-group-list', [])
            release_groups.extend(rgs)
            if len(rgs) < limit or len(release_groups) >= int(res.get('release-group-count', 0)):
                break
            offset += limit

        full_data = {
            "artist": artist_data,
            "releases_artist": releases_artist,
            "releases_track_artist": releases_track_artist,
            "recordings": recordings,
            "release_groups": release_groups,
            "fetched_at": time.time()
        }

        if self.use_cache:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(full_data, f, ensure_ascii=False, indent=2)
                console.print(f"[dim]Cached MusicBrainz catalog to {cache_file.name}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Warning: Could not save cache: {e}[/yellow]")

        return full_data


# ==============================================================================
# CATALOG PARSER & ALIAS EXTRACTOR
# ==============================================================================

class ArtistCatalog:
    def __init__(self, raw_data: Dict[str, Any]):
        self.raw_data = raw_data
        self.artist_info = raw_data['artist']
        self.mbid = self.artist_info['id']
        self.name = self.artist_info.get('name', 'Unknown Artist')
        self.sort_name = self.artist_info.get('sort-name', '')

        self.aliases: Set[str] = set()
        self.alias_details: List[Dict[str, str]] = []
        self.bandcamp_urls: List[str] = []
        self._extract_aliases()

        # Catalog items
        self.tracks: List[Dict[str, Any]] = []
        self.release_groups: Dict[str, Dict[str, Any]] = {}
        self._build_catalog()

    def _extract_aliases(self):
        """Extracts all aliases, transliterations, sort names, related personas, and Bandcamp URLs."""
        # Bandcamp URLs from artist relationships
        for rel in self.artist_info.get('url-relation-list', []):
            if isinstance(rel, dict):
                target = rel.get('target', '')
                rtype = rel.get('type', '').lower()
                if rtype == 'bandcamp' or 'bandcamp.com' in target:
                    if target not in self.bandcamp_urls:
                        self.bandcamp_urls.append(target)

        # Canonical name & sort name
        for n in (self.name, self.sort_name):
            if n:
                self.aliases.add(n.strip().lower())
                self.aliases.add(unidecode(n).strip().lower())

        # MusicBrainz aliases
        for a in self.artist_info.get('alias-list', []):
            if isinstance(a, dict):
                alias_name = a.get('alias') or a.get('name')
                sort_name = a.get('sort-name')
                locale = a.get('locale', '')
                type_name = a.get('type', 'Alias')
                for val in (alias_name, sort_name):
                    if val:
                        self.aliases.add(val.strip().lower())
                        self.aliases.add(unidecode(val).strip().lower())
                if alias_name:
                    self.alias_details.append({
                        "alias": alias_name,
                        "type": type_name,
                        "locale": locale
                    })

        # Related artist personas (e.g. 'is person' relationships, sub-projects)
        for rel in self.artist_info.get('artist-relation-list', []):
            if isinstance(rel, dict):
                art = rel.get('artist', {})
                art_name = art.get('name')
                if art_name:
                    self.aliases.add(art_name.strip().lower())
                    self.aliases.add(unidecode(art_name).strip().lower())
                    self.alias_details.append({
                        "alias": art_name,
                        "type": f"Relation ({rel.get('type', 'persona')})",
                        "locale": ""
                    })

        # Remove empty strings
        self.aliases.discard("")

    def _build_catalog(self):
        """
        Organizes all recordings and tracks into a deduplicated, release-grouped catalog.
        """
        for rg in self.raw_data.get('release_groups', []):
            rg_id = rg.get('id')
            if rg_id:
                self.release_groups[rg_id] = {
                    "id": rg_id,
                    "title": rg.get('title', 'Unknown Release Group'),
                    "primary_type": rg.get('primary-type', 'Album'),
                    "secondary_types": rg.get('secondary-type-list', [])
                }

        rec_map: Dict[str, Dict[str, Any]] = {}
        title_to_key: Dict[str, str] = {}
        standalone_counter = 0

        def format_credit(ac_list):
            if not ac_list:
                return self.name
            parts = []
            for c in ac_list:
                if isinstance(c, dict):
                    parts.append(c.get('artist', {}).get('name', ''))
                else:
                    parts.append(str(c))
            return "".join(parts) or self.name

        def is_artist_in_credit(ac_list):
            if not ac_list:
                return False
            for c in ac_list:
                if isinstance(c, dict) and c.get('artist', {}).get('id') == self.mbid:
                    return True
            return False

        # 1. Primary Artist Releases
        for rel in self.raw_data.get('releases_artist', []):
            rel_title = rel.get('title', 'Unknown Release')
            rel_id = rel.get('id')
            rg_data = rel.get('release-group', {})
            rg_id = rg_data.get('id')
            rg_type = rg_data.get('primary-type') or 'Album'
            rel_date = rel.get('date', '')

            for m in rel.get('medium-list', []):
                for t in m.get('track-list', []):
                    rec = t.get('recording', {})
                    rec_id = rec.get('id')
                    track_id = t.get('id')
                    title = t.get('title') or rec.get('title')
                    track_num = str(t.get('number', ''))
                    ac_raw = t.get('artist-credit', []) or rec.get('artist-credit', [])
                    artist_credit = format_credit(ac_raw)
                    norm_t = normalize_text(title)

                    if not rec_id:
                        standalone_counter += 1
                        rec_id = f"virtual_{standalone_counter}"

                    if rec_id not in rec_map:
                        rec_map[rec_id] = {
                            "recording_ids": {rec_id} if not rec_id.startswith("virtual_") else set(),
                            "track_ids": {track_id} if track_id else set(),
                            "title": title,
                            "norm_title": norm_t,
                            "artist_credit": artist_credit,
                            "release_title": rel_title,
                            "norm_release": normalize_text(rel_title),
                            "release_id": rel_id,
                            "release_group_id": rg_id,
                            "release_type": rg_type,
                            "track_number": track_num,
                            "date": rel_date,
                            "all_releases": {rel_title}
                        }
                        if norm_t:
                            title_to_key[norm_t] = rec_id
                    else:
                        if not rec_id.startswith("virtual_"):
                            rec_map[rec_id]["recording_ids"].add(rec_id)
                        if track_id:
                            rec_map[rec_id]["track_ids"].add(track_id)
                        rec_map[rec_id]["all_releases"].add(rel_title)

        # 2. Track Artist Releases (Compilations, Splits, Features, VA)
        for rel in self.raw_data.get('releases_track_artist', []):
            rel_title = rel.get('title', 'Unknown Release')
            rel_id = rel.get('id')
            rg_data = rel.get('release-group', {})
            rg_id = rg_data.get('id')
            rg_type = rg_data.get('primary-type') or 'Compilation'
            rel_date = rel.get('date', '')

            for m in rel.get('medium-list', []):
                for t in m.get('track-list', []):
                    rec = t.get('recording', {})
                    ac_raw = t.get('artist-credit', []) or rec.get('artist-credit', [])

                    if is_artist_in_credit(ac_raw):
                        rec_id = rec.get('id')
                        track_id = t.get('id')
                        title = t.get('title') or rec.get('title')
                        track_num = str(t.get('number', ''))
                        artist_credit = format_credit(ac_raw)
                        norm_t = normalize_text(title)

                        if not rec_id:
                            standalone_counter += 1
                            rec_id = f"virtual_{standalone_counter}"

                        if rec_id not in rec_map:
                            rec_map[rec_id] = {
                                "recording_ids": {rec_id} if not rec_id.startswith("virtual_") else set(),
                                "track_ids": {track_id} if track_id else set(),
                                "title": title,
                                "norm_title": norm_t,
                                "artist_credit": artist_credit,
                                "release_title": rel_title,
                                "norm_release": normalize_text(rel_title),
                                "release_id": rel_id,
                                "release_group_id": rg_id,
                                "release_type": f"Compilation / Feature ({rg_type})",
                                "track_number": track_num,
                                "date": rel_date,
                                "all_releases": {rel_title}
                            }
                            if norm_t:
                                title_to_key[norm_t] = rec_id
                        else:
                            if not rec_id.startswith("virtual_"):
                                rec_map[rec_id]["recording_ids"].add(rec_id)
                            if track_id:
                                rec_map[rec_id]["track_ids"].add(track_id)
                            rec_map[rec_id]["all_releases"].add(rel_title)

        # 3. Direct Recordings (Catch standalone / unreleased tracks and deduplicate by recording ID / title)
        for rec in self.raw_data.get('recordings', []):
            rec_id = rec.get('id')
            title = rec.get('title')
            norm_t = normalize_text(title)

            # If this recording ID or title is already associated with an existing track, merge recording ID
            if rec_id and rec_id in rec_map:
                rec_map[rec_id]["recording_ids"].add(rec_id)
            elif norm_t and norm_t in title_to_key:
                existing_key = title_to_key[norm_t]
                if rec_id:
                    rec_map[existing_key]["recording_ids"].add(rec_id)
            elif rec_id:
                ac_raw = rec.get('artist-credit', [])
                artist_credit = format_credit(ac_raw)

                rec_map[rec_id] = {
                    "recording_ids": {rec_id},
                    "track_ids": set(),
                    "title": title,
                    "norm_title": norm_t,
                    "artist_credit": artist_credit,
                    "release_title": "Standalone / Other",
                    "norm_release": "",
                    "release_id": None,
                    "release_group_id": None,
                    "release_type": "Standalone / Single",
                    "track_number": "",
                    "date": "",
                    "all_releases": set()
                }
                if norm_t:
                    title_to_key[norm_t] = rec_id

        self.tracks = list(rec_map.values())


# ==============================================================================
# AUDIO LIBRARY SCANNER (OPTIMIZED FOR SSHFS / NETWORK / LOCAL)
# ==============================================================================

class AudioFileScanner:
    def __init__(self, music_dir: str, catalog: ArtistCatalog, full_scan: bool = False, threads: int = 24):
        self.music_dir = Path(music_dir)
        self.catalog = catalog
        self.full_scan = full_scan
        self.threads = threads

    def scan(self, progress: Optional[Progress] = None, task_id: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Scans the music directory using a 2-stage fast discovery + parallel metadata extraction.
        """
        if not self.music_dir.exists():
            raise FileNotFoundError(f"Music directory not found: {self.music_dir}")

        if progress and task_id:
            progress.update(task_id, description="[cyan]Stage 1: Discovering audio files on disk...")

        # Prepare matching lookups
        known_releases = {t["norm_release"] for t in self.catalog.tracks if t["norm_release"]}
        known_tracks = {t["norm_title"] for t in self.catalog.tracks if len(t["norm_title"]) >= 3}
        artist_aliases = self.catalog.aliases

        all_audio_paths = []
        candidate_paths = []

        # Stage 1: Walk directory structure
        for root, dirs, files in os.walk(self.music_dir):
            root_lower = root.lower()
            root_uni = unidecode(root_lower)

            dir_matches_artist = any(a in root_lower or a in root_uni for a in artist_aliases)
            dir_matches_release = any(r in root_lower or r in root_uni for r in known_releases)
            dir_is_va = any(vm in root_lower or vm in root_uni for vm in VA_DIR_MARKERS)

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                full_path = os.path.join(root, f)
                all_audio_paths.append(full_path)

                if self.full_scan:
                    candidate_paths.append(full_path)
                else:
                    f_lower = f.lower()
                    f_uni = unidecode(f_lower)
                    f_matches_artist = any(a in f_lower or a in f_uni for a in artist_aliases)
                    f_matches_track = any(t in f_lower or t in f_uni for t in known_tracks)

                    if dir_matches_artist or dir_matches_release or f_matches_artist or f_matches_track or (dir_is_va and (f_matches_artist or f_matches_track)):
                        candidate_paths.append(full_path)

        total_audio = len(all_audio_paths)
        total_candidates = len(candidate_paths)

        if progress and task_id:
            progress.update(
                task_id,
                description=f"[cyan]Stage 2: Inspecting tags of {total_candidates} candidate audio files...",
                total=total_candidates,
                completed=0
            )

        # Stage 2: Parallel metadata reading
        local_tracks = []
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            for item in pool.map(self._read_audio_metadata, candidate_paths):
                if item:
                    local_tracks.append(item)
                if progress and task_id:
                    progress.advance(task_id, 1)

        return local_tracks

    @staticmethod
    def _read_audio_metadata(path: str) -> Optional[Dict[str, Any]]:
        """Extracts tags, MusicBrainz IDs, and clean titles from an audio file."""
        title = ""
        album = ""
        track_number = ""
        artists: List[str] = []
        mb_track_ids: Set[str] = set()
        mb_rec_ids: Set[str] = set()
        mb_artist_ids: Set[str] = set()
        mb_release_ids: Set[str] = set()

        try:
            mf = mutagen.File(path)
            if mf:
                # ID3 Tags (MP3, AIFF, WAV)
                if hasattr(mf, 'tags') and mf.tags:
                    for k, v in mf.tags.items():
                        k_str = str(k).upper()
                        v_str = str(v)
                        if 'MUSICBRAINZ TRACK ID' in k_str or 'MUSICBRAINZ RELEASE TRACK ID' in k_str:
                            mb_track_ids.add(v_str.strip())
                        elif 'UFID' in k_str or 'MUSICBRAINZ RECORDING ID' in k_str:
                            clean_ufid = re.sub(r'^[^\w\-]+', '', v_str)
                            clean_ufid = clean_ufid.replace("http://musicbrainz.org=", "").replace("b'", "").replace("'", "")
                            mb_rec_ids.add(clean_ufid.strip())
                        elif 'MUSICBRAINZ ARTIST ID' in k_str or 'MUSICBRAINZ ALBUM ARTIST ID' in k_str:
                            mb_artist_ids.add(v_str.strip())
                        elif 'MUSICBRAINZ ALBUM ID' in k_str:
                            mb_release_ids.add(v_str.strip())

                        if k_str in ('TPE1', 'TPE2', 'TOPE', 'TEXT', 'TCOM', 'ARTIST', 'ALBUMARTIST', 'COMPOSER', 'PERFORMER', 'ARTISTS'):
                            artists.append(v_str)
                        elif 'TIT2' in k_str or 'TITLE' in k_str:
                            title = v_str
                        elif 'TALB' in k_str or 'ALBUM' in k_str:
                            album = v_str
                        elif 'TRCK' in k_str or 'TRACKNUMBER' in k_str:
                            track_number = v_str.split('/')[0].strip()

                # Vorbis / FLAC / Opus Tags
                if hasattr(mf, 'items'):
                    for k, v in mf.items():
                        k_str = str(k).upper()
                        v_str = " / ".join(str(x) for x in v) if isinstance(v, list) else str(v)

                        if 'MUSICBRAINZ_TRACKID' in k_str or 'MUSICBRAINZ_RELEASETRACKID' in k_str:
                            mb_track_ids.add(v_str.strip())
                        elif 'MUSICBRAINZ_RECORDINGID' in k_str:
                            mb_rec_ids.add(v_str.strip())
                        elif 'MUSICBRAINZ_ARTISTID' in k_str or 'MUSICBRAINZ_ALBUMARTISTID' in k_str:
                            mb_artist_ids.add(v_str.strip())
                        elif 'MUSICBRAINZ_ALBUMID' in k_str:
                            mb_release_ids.add(v_str.strip())

                        if k_str in ('ARTIST', 'ALBUMARTIST', 'COMPOSER', 'PERFORMER', 'ARTISTS'):
                            artists.append(v_str)
                        elif k_str == 'TITLE':
                            title = v_str
                        elif k_str == 'ALBUM':
                            album = v_str
                        elif k_str == 'TRACKNUMBER':
                            track_number = v_str.split('/')[0].strip()
        except Exception:
            pass

        filename_no_ext = os.path.splitext(os.path.basename(path))[0]
        if not title:
            title = strip_track_number_and_artist(filename_no_ext)

        return {
            "path": path,
            "filename": os.path.basename(path),
            "title": title or "",
            "norm_title": normalize_text(title),
            "album": album or "",
            "norm_album": normalize_text(album),
            "track_number": track_number or "",
            "artists": artists,
            "mb_track_ids": mb_track_ids,
            "mb_rec_ids": mb_rec_ids,
            "mb_artist_ids": mb_artist_ids,
            "mb_release_ids": mb_release_ids
        }


# ==============================================================================
# MULTI-TIER RECONCILIATION & MATCHING ENGINE
# ==============================================================================

class DiscographyReconciler:
    def __init__(self, catalog: ArtistCatalog, local_tracks: List[Dict[str, Any]]):
        self.catalog = catalog
        self.local_tracks = local_tracks
        self.matched: Dict[int, Tuple[Dict[str, Any], str]] = {}
        self.unmatched_local: List[Dict[str, Any]] = []

    def reconcile(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Matches MusicBrainz tracks against local library audio files.
        Returns (found_items, missing_items).
        """
        matched_mb_indices = set()
        matched_local_paths = set()
        mb_tracks = self.catalog.tracks
        artist_aliases = self.catalog.aliases

        # -------------------------------------------------------------
        # TIER 1: Exact MusicBrainz Tag Matching (MBID Track/Recording)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue
            mb_rec_ids = mb.get("recording_ids", set())
            mb_track_ids = mb.get("track_ids", set())

            for lt in self.local_tracks:
                if lt["path"] in matched_local_paths:
                    continue

                # Check Recording ID
                if mb_rec_ids and any(rid in lt["mb_rec_ids"] for rid in mb_rec_ids):
                    self.matched[i] = (lt, "Exact MBID (Recording)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

                # Check Track ID
                if mb_track_ids and any(tid in lt["mb_track_ids"] for tid in mb_track_ids):
                    self.matched[i] = (lt, "Exact MBID (Track ID)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

        # -------------------------------------------------------------
        # TIER 2: Exact Release + Exact Track Title Match
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            mb_title_norm = mb["norm_title"]
            mb_rel_norm = mb["norm_release"]
            if not mb_title_norm:
                continue

            for lt in self.local_tracks:
                if lt["path"] in matched_local_paths:
                    continue

                lt_title_norm = lt["norm_title"]
                lt_album_norm = lt["norm_album"]
                path_norm = normalize_text(lt["path"])

                if mb_title_norm == lt_title_norm or calculate_similarity(mb_title_norm, lt_title_norm) > 0.95:
                    rel_sim = calculate_similarity(mb_rel_norm, lt_album_norm)
                    path_has_rel = mb_rel_norm and mb_rel_norm in path_norm

                    if rel_sim > 0.7 or path_has_rel or mb["release_title"] == "Standalone / Other":
                        self.matched[i] = (lt, "Exact Title & Album Match")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

        # -------------------------------------------------------------
        # TIER 3: Track Title + Artist Alias Match (Tags or Path)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            mb_title_norm = mb["norm_title"]
            if not mb_title_norm:
                continue

            for lt in self.local_tracks:
                if lt["path"] in matched_local_paths:
                    continue

                lt_title_norm = lt["norm_title"]
                path_norm = normalize_text(lt["path"])

                has_artist_tag = any(
                    any(alias in a.lower() or alias in unidecode(a.lower()) for alias in artist_aliases)
                    for a in lt["artists"]
                )
                has_artist_path = any(alias in path_norm for alias in artist_aliases)

                if has_artist_tag or has_artist_path or (mb.get("recording_ids") and any(rid in lt["mb_rec_ids"] for rid in mb["recording_ids"])):
                    if mb_title_norm == lt_title_norm or calculate_similarity(mb_title_norm, lt_title_norm) > 0.88:
                        self.matched[i] = (lt, "Title & Artist Alias Match")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

        # -------------------------------------------------------------
        # TIER 4: Fuzzy Match (Substrings, Transliterations, Remixes)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            mb_title_norm = mb["norm_title"]
            if not mb_title_norm or len(mb_title_norm) < 3:
                continue

            for lt in self.local_tracks:
                if lt["path"] in matched_local_paths:
                    continue

                lt_title_norm = lt["norm_title"]
                path_norm = normalize_text(lt["path"])

                title_in_path = len(mb_title_norm) >= 5 and mb_title_norm in path_norm
                title_in_tag = len(mb_title_norm) >= 5 and mb_title_norm in lt_title_norm
                sim = calculate_similarity(mb_title_norm, lt_title_norm)

                if sim > 0.82 or title_in_path or title_in_tag:
                    self.matched[i] = (lt, f"Fuzzy Match ({int(sim*100)}%)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

        # Compile found and missing lists
        found_items = []
        missing_items = []

        for i, mb in enumerate(mb_tracks):
            if i in self.matched:
                lt, method = self.matched[i]
                found_items.append({
                    "mb_track": mb,
                    "local_track": lt,
                    "match_method": method
                })
            else:
                missing_items.append({
                    "mb_track": mb
                })

        return found_items, missing_items


# ==============================================================================
# REPORTING & CLI FORMATTING
# ==============================================================================

class ReportGenerator:
    def __init__(self, catalog: ArtistCatalog, found_items: List[Dict[str, Any]], missing_items: List[Dict[str, Any]]):
        self.catalog = catalog
        self.found_items = found_items
        self.missing_items = missing_items

    def render_terminal_report(self, only_missing: bool = False, only_found: bool = False, verbose: bool = False):
        """Renders rich terminal tables, release breakdowns, and completion scorecards."""
        total_tracks = len(self.catalog.tracks)
        found_count = len(self.found_items)
        missing_count = len(self.missing_items)
        completion_pct = (found_count / total_tracks * 100) if total_tracks > 0 else 100.0

        # 1. Header Banner
        header_text = Text()
        header_text.append(f"Artist: {self.catalog.name}\n", style="bold green")
        if self.catalog.sort_name and self.catalog.sort_name.lower() != self.catalog.name.lower():
            header_text.append(f"Sort Name: {self.catalog.sort_name}\n", style="dim")
        header_text.append(f"MBID: {self.catalog.mbid}\n", style="cyan")
        header_text.append(f"MusicBrainz URL: https://musicbrainz.org/artist/{self.catalog.mbid}\n", style="blue underline")

        alias_sample = list(self.catalog.aliases)[:8]
        alias_str = ", ".join(alias_sample)
        if len(self.catalog.aliases) > 8:
            alias_str += f" (+{len(self.catalog.aliases)-8} more)"
        header_text.append(f"Aliases & Spellings ({len(self.catalog.aliases)}): {alias_str}\n", style="italic magenta")

        if self.catalog.bandcamp_urls:
            bc_links = " | ".join(self.catalog.bandcamp_urls)
            header_text.append(f"Bandcamp: {bc_links}", style="bold cyan")

        console.print(Panel(header_text, title="[bold]MusicBrainz Discography Audit[/bold]", border_style="green", box=box.ROUNDED))

        # 2. Group Tracks by Release for Clean Overview
        releases_map: Dict[str, Dict[str, Any]] = {}

        for item in self.found_items:
            mb = item["mb_track"]
            rel = mb["release_title"] or "Standalone / Other"
            if rel not in releases_map:
                releases_map[rel] = {
                    "release_type": mb["release_type"],
                    "date": mb["date"],
                    "found": [],
                    "missing": []
                }
            releases_map[rel]["found"].append(item)

        for item in self.missing_items:
            mb = item["mb_track"]
            rel = mb["release_title"] or "Standalone / Other"
            if rel not in releases_map:
                releases_map[rel] = {
                    "release_type": mb["release_type"],
                    "date": mb["date"],
                    "found": [],
                    "missing": []
                }
            releases_map[rel]["missing"].append(item)

        # 3. Release Summary Table
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

            rel_table.add_row(
                rel_title,
                rinfo["release_type"],
                rinfo["date"] or "-",
                status,
                f"{found_n} / {total_n}"
            )

        console.print(rel_table)

        # 4. Detailed Missing Tracks Listing
        if not only_found and self.missing_items:
            console.print("\n[bold red]── Missing Tracks Checklist ──────────────────────────────────────────[/bold red]")
            missing_table = Table(box=box.SIMPLE, show_lines=False, header_style="bold red")
            missing_table.add_column("#", style="dim", width=4, justify="right")
            missing_table.add_column("Track Title", style="bold red", min_width=25)
            missing_table.add_column("Artist Credit", style="magenta")
            missing_table.add_column("Release / Album", style="white")
            missing_table.add_column("Year", style="dim", justify="center")

            for item in self.missing_items:
                mb = item["mb_track"]
                missing_table.add_row(
                    mb["track_number"] or "-",
                    mb["title"],
                    mb["artist_credit"],
                    mb["release_title"] or "(Standalone)",
                    mb["date"][:4] if mb["date"] else "-"
                )

            console.print(missing_table)

        # 5. Detailed Found Tracks Listing (if requested via flag or verbose)
        if (only_found or verbose) and self.found_items:
            console.print("\n[bold green]── Found Tracks in Library ──────────────────────────────────────────[/bold green]")
            found_table = Table(box=box.SIMPLE, show_lines=False, header_style="bold green")
            found_table.add_column("Track Title", style="bold green")
            found_table.add_column("Release", style="white")
            found_table.add_column("Matched File Path", style="dim cyan")
            found_table.add_column("Match Method", style="yellow")

            for item in self.found_items:
                mb = item["mb_track"]
                lt = item["local_track"]
                found_table.add_row(
                    mb["title"],
                    mb["release_title"] or "(Standalone)",
                    lt["path"],
                    item["match_method"]
                )

            console.print(found_table)

        # 6. Overall Statistics Scorecard
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

    def export_json(self, output_path: str):
        """Exports full reconciliation results to JSON."""
        report = {
            "artist": {
                "name": self.catalog.name,
                "sort_name": self.catalog.sort_name,
                "mbid": self.catalog.mbid,
                "aliases": list(self.catalog.aliases),
                "url": f"https://musicbrainz.org/artist/{self.catalog.mbid}"
            },
            "summary": {
                "total_tracks": len(self.catalog.tracks),
                "found_tracks": len(self.found_items),
                "missing_tracks": len(self.missing_items),
                "completion_percentage": round((len(self.found_items) / len(self.catalog.tracks) * 100) if self.catalog.tracks else 100.0, 2)
            },
            "missing": [
                {
                    "title": item["mb_track"]["title"],
                    "artist_credit": item["mb_track"]["artist_credit"],
                    "release": item["mb_track"]["release_title"],
                    "track_number": item["mb_track"]["track_number"],
                    "release_type": item["mb_track"]["release_type"],
                    "date": item["mb_track"]["date"],
                    "recording_ids": list(item["mb_track"].get("recording_ids", []))
                }
                for item in self.missing_items
            ],
            "found": [
                {
                    "title": item["mb_track"]["title"],
                    "release": item["mb_track"]["release_title"],
                    "local_path": item["local_track"]["path"],
                    "match_method": item["match_method"]
                }
                for item in self.found_items
            ]
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        console.print(f"[green]✔ Exported JSON report to:[/green] [bold]{output_path}[/bold]")

    def export_txt(self, output_path: str):
        """Exports a clean plain text list of missing tracks."""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Missing Tracks for {self.catalog.name}\n")
            f.write(f"# MusicBrainz ID: {self.catalog.mbid}\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for item in self.missing_items:
                mb = item["mb_track"]
                tr = f"#{mb['track_number']} " if mb['track_number'] else ""
                rel = f" (Release: {mb['release_title']})" if mb['release_title'] else ""
                f.write(f"{mb['artist_credit']} - {tr}{mb['title']}{rel}\n")
        console.print(f"[green]✔ Exported missing tracks text list to:[/green] [bold]{output_path}[/bold]")

    def export_csv(self, output_path: str):
        """Exports missing and found tracks to CSV."""
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Status", "Track Title", "Artist Credit", "Release", "Track Number", "Release Date", "Local Path", "Match Method"])
            for item in self.found_items:
                mb = item["mb_track"]
                lt = item["local_track"]
                writer.writerow(["FOUND", mb["title"], mb["artist_credit"], mb["release_title"], mb["track_number"], mb["date"], lt["path"], item["match_method"]])
            for item in self.missing_items:
                mb = item["mb_track"]
                writer.writerow(["MISSING", mb["title"], mb["artist_credit"], mb["release_title"], mb["track_number"], mb["date"], "", ""])
        console.print(f"[green]✔ Exported CSV report to:[/green] [bold]{output_path}[/bold]")

    def export_bandcamp_links(self, output_path: str):
        """Exports artist and missing release Bandcamp links to a text file."""
        urls = list(self.catalog.bandcamp_urls)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Bandcamp Links for {self.catalog.name}\n")
            f.write(f"# MusicBrainz ID: {self.catalog.mbid}\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            if urls:
                for u in urls:
                    f.write(f"{u}\n")
            else:
                f.write("# No official Bandcamp URLs linked in MusicBrainz\n")
        console.print(f"[green]✔ Exported Bandcamp links ({len(urls)}) to:[/green] [bold]{output_path}[/bold]")


# ==============================================================================
# MAIN CLI ENTRYPOINT
# ==============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Check for missing tracks in your music library for a given artist using MusicBrainz.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 check_missing_tracks.py "すてらべえ"
  python3 check_missing_tracks.py "Stellabee" -d /mnt/music
  python3 check_missing_tracks.py "https://musicbrainz.org/artist/2dbd3954-9bb7-4165-9445-98f66c3861bf"
  python3 check_missing_tracks.py "Aphex Twin" --only-missing --export-txt missing.txt
  python3 check_missing_tracks.py "goreshit" --export-bandcamp-links goreshit_bc.txt
        """
    )
    parser.add_argument("artist", help="Artist Name, MusicBrainz Artist ID (MBID), or MusicBrainz Artist URL")
    parser.add_argument("-d", "--dir", "--music-dir", dest="music_dir", default="/mnt/music", help="Path to music library directory (default: /mnt/music)")
    parser.add_argument("--full-scan", action="store_true", help="Perform a full deep-scan of every audio file in the music directory instead of fast path pre-filtering")
    parser.add_argument("-t", "--threads", type=int, default=24, help="Number of parallel worker threads for reading audio metadata tags (default: 24)")
    parser.add_argument("--only-missing", action="store_true", help="Display only missing tracks/releases in the output")
    parser.add_argument("--only-found", action="store_true", help="Display only found tracks in the output")
    parser.add_argument("--export-json", type=str, metavar="PATH", help="Export full structured audit results to a JSON file")
    parser.add_argument("--export-txt", type=str, metavar="PATH", help="Export a clean text list of missing tracks to a file")
    parser.add_argument("--export-csv", type=str, metavar="PATH", help="Export audit results to a CSV spreadsheet")
    parser.add_argument("--export-bandcamp-links", type=str, metavar="PATH", help="Export discovered Bandcamp URLs to a text file (feedable into bandcamp_scraper.py -i)")
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR, help=f"Directory to store MusicBrainz cache files (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refresh MusicBrainz API cache for this artist")
    parser.add_argument("--no-cache", action="store_true", help="Disable caching of MusicBrainz data")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed match logs and all local matches")
    return parser.parse_args()


def main():
    args = parse_args()

    # Step 1: Resolve Artist
    mb_client = MusicBrainzClient(cache_dir=args.cache_dir, use_cache=not args.no_cache)
    try:
        mbid, canonical_name = mb_client.resolve_artist_mbid(args.artist)
    except Exception as e:
        console.print(f"[red]Error resolving artist:[/red] {e}")
        sys.exit(1)

    # Step 2: Fetch Discography Data
    try:
        raw_data = mb_client.fetch_full_discography(mbid, force_refresh=args.refresh_cache)
    except Exception as e:
        console.print(f"[red]Error fetching MusicBrainz discography:[/red] {e}")
        sys.exit(1)

    # Step 3: Parse Catalog & Aliases
    catalog = ArtistCatalog(raw_data)
    console.print(f"[cyan]Loaded [bold]{len(catalog.tracks)}[/bold] unique recordings/tracks across [bold]{len(raw_data.get('releases_artist', [])) + len(raw_data.get('releases_track_artist', []))}[/bold] releases.[/cyan]")

    # Step 4: Scan Music Library
    scanner = AudioFileScanner(
        music_dir=args.music_dir,
        catalog=catalog,
        full_scan=args.full_scan,
        threads=args.threads
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        task_id = progress.add_task("[cyan]Scanning library...", total=None)
        try:
            local_tracks = scanner.scan(progress=progress, task_id=task_id)
        except Exception as e:
            console.print(f"[red]Error scanning music directory '{args.music_dir}':[/red] {e}")
            sys.exit(1)

    console.print(f"[dim]Parsed metadata from {len(local_tracks)} audio files in library.[/dim]")

    # Step 5: Reconcile Tracks
    reconciler = DiscographyReconciler(catalog=catalog, local_tracks=local_tracks)
    found_items, missing_items = reconciler.reconcile()

    # Step 6: Generate Reports & Export
    reporter = ReportGenerator(catalog=catalog, found_items=found_items, missing_items=missing_items)
    reporter.render_terminal_report(
        only_missing=args.only_missing,
        only_found=args.only_found,
        verbose=args.verbose
    )

    if args.export_json:
        reporter.export_json(args.export_json)
    if args.export_txt:
        reporter.export_txt(args.export_txt)
    if args.export_csv:
        reporter.export_csv(args.export_csv)
    if args.export_bandcamp_links:
        reporter.export_bandcamp_links(args.export_bandcamp_links)


if __name__ == "__main__":
    main()
