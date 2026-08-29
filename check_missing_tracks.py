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
import logging
import argparse
import hashlib
import random
import string
import urllib.parse
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Dict, List, Set, Tuple, Optional, Any

import sqlite3
import requests
import musicbrainzngs
import mutagen
from unidecode import unidecode
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.text import Text
from rich.tree import Tree
from rich import box

# Load environment variables (.env)
load_dotenv()

# Silence noisy third-party loggers (e.g. musicbrainzngs schema parsing notices, urllib3)
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

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
    'various', 'various artists', 'compilation', 'compilations', 'split',
    'soundtrack', 'soundtracks', 'ost', 'sampler', 'anthology', 'tribute', 'tributes'
}

# Generic words / track markers / common words that should not trigger loose filename matching
GENERIC_OR_COMMON_WORDS = {
    # Common track descriptors & markers
    "intro", "outro", "interlude", "prelude", "untitled", "bonus", "bonus track",
    "track", "instrumental", "opening", "ending", "skit", "silence", "noise",
    "demo", "mix", "remix", "version", "edit", "side a", "side b", "vip", "theme",
    "audio", "original", "cover", "live", "sampler", "compilation", "single", "ep",
    "album", "lp", "ost", "soundtrack", "part", "vol", "volume", "chapter", "reissue",
    # Common single English words prone to false positives when matching loose filenames
    "shit", "dreams", "sleep", "everyday", "scream", "fly", "sin", "jump", "angels",
    "love", "love you", "alone", "night", "rain", "home", "time", "summer", "winter",
    "spring", "fall", "sky", "sun", "moon", "star", "fire", "water", "blue", "red",
    "black", "white", "run", "walk", "fall", "rise", "stay", "go", "come", "life",
    "death", "dark", "light", "space", "mind", "soul", "heart", "eyes", "girl", "boy",
    "friends", "forever", "today", "tomorrow", "yesterday", "world", "hope", "lost",
    # Electronic music genre keywords
    "breakcore", "lolicore", "speedcore", "hardcore", "frenchcore", "nightcore",
    "jcore", "j-core", "extratone", "splittercore", "terrorcore", "mashcore",
    "gabber", "gabba", "dancecore", "flashcore", "noise", "harsh noise", "ambient",
    "vaporwave", "chiptune", "electronic", "techno", "trance", "house", "dubstep",
    "dnb", "drum and bass", "jungle", "rave", "rave music", "acid"
}

# Default Cache Directory & Audio Cache Database
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/musicscraper/mb_cache")
DEFAULT_AUDIO_CACHE_DB = os.path.expanduser("~/.cache/musicscraper/audio_cache.db")


from functools import lru_cache

# ==============================================================================
# STRING NORMALIZATION & FUZZY MATCHING HELPERS
# ==============================================================================

def katakana_to_hiragana(text: str) -> str:
    """Converts Katakana characters to Hiragana for uniform Japanese phonetic matching."""
    res = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            res.append(chr(code - 0x60))
        else:
            res.append(ch)
    return "".join(res)


@lru_cache(maxsize=65536)
def normalize_text(text: Optional[str]) -> str:
    """
    Normalizes text for robust comparison:
    - Lowercases & unifies Katakana/Hiragana
    - Transliterates unicode (e.g. Japanese to ASCII Romaji approximations)
    - Separates number-letter boundaries (e.g. menson1mix -> menson 1 mix)
    - Replaces punctuation and special symbols with spaces
    - Collapses consecutive whitespace
    """
    if not text:
        return ""
    text = katakana_to_hiragana(text.lower())
    text = unidecode(text)
    # Separate letter-number boundaries
    text = re.sub(r'([a-zA-Z])([0-9])', r'\1 \2', text)
    text = re.sub(r'([0-9])([a-zA-Z])', r'\1 \2', text)
    # Remove punctuation and special symbols
    text = re.sub(r'[\(\)\[\]\{\}\-_,.\'\"!?:;~`+*#&/\\|><$%^@=]', ' ', text)
    # Collapse multiple whitespaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


@lru_cache(maxsize=65536)
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


@lru_cache(maxsize=65536)
def calculate_similarity(str1: str, str2: str) -> float:
    """Calculates SequenceMatcher ratio between two normalized strings."""
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0
    return SequenceMatcher(None, str1, str2).ratio()


# ==============================================================================
# TRACK TITLE STRUCTURE, REMIX / VERSION & FEATURE EXTRACTION
# ==============================================================================

REMASTER_OR_NOISE_PATTERNS = [
    re.compile(r"[\(\[\{]?(?:20\d\d|19\d\d)?\s*digital\s*remaster(?:ed)?(?:\s*version|\s*\d{4})?[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:20\d\d|19\d\d)?\s*remaster(?:ed)?(?:\s*version|\s*\d{4})?[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?anniversary\s*edition[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?deluxe\s*(?:edition|version)[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?bonus\s*track[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:original\s*mix|original\s*version|album\s*version|main\s*version)[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:flac|mp3|320kbps|24bit|lossless|wav|vbr|cd|web|vinyl|rip|official\s*audio|official\s*video|mv|lyrics)[\)\]\}]?", re.IGNORECASE),
]

FEATURE_PATTERNS = [
    re.compile(r"[\(\[\{]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]\}]+)[\)\]\}]", re.IGNORECASE),
    re.compile(r"(?:\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*([^,\-\(\[\{]+)", re.IGNORECASE),
]

VERSION_PATTERNS = [
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:remix|rmx|re-mix|flip|bootleg|rework|edit|refix|mashup|mash-up)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "remix"),
    (re.compile(r"(?:^|\s)\-\s*([^\-]+(?:remix|rmx|re-mix|flip|bootleg|rework|edit|refix|mashup|mash-up)[^\-]*)", re.IGNORECASE), "remix"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:vip\s*mix|vip)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "vip"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:instrumental|inst\b|off\s*vocal|karaoke|backing\s*track)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "instrumental"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:acapella|a\s*cappella|vocal\s*version)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "acapella"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:acoustic|unplugged|piano\s*ver(?:sion)?)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "acoustic"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:live(?:\s+at|\s+in|\s+version|\s*\d{4})?)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "live"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:speed\s*up|sped\s*up|slowed|nightcore|daycore|chopped\s*and\s*screwed)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "speed"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:demo|alternate\s*take|alt\s*take|alt\s*mix|rough\s*mix)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "demo"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:club\s*mix|extended\s*mix|extended\s*version|radio\s*edit|dub\s*mix|dub)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "mix_edit"),
]


@lru_cache(maxsize=65536)
def parse_track_title_structure(title: str) -> Dict[str, Any]:
    """
    Parses a track title into structured components:
    - Base normalized title (with noise, remasters, format tags, and features stripped)
    - Version type and normalized descriptor text (e.g. remix, instrumental, live)
    - Featured artist list
    """
    raw = (title or "").strip()
    if not raw:
        return {
            "raw": "",
            "base_norm": "",
            "version_type": None,
            "version_text": None,
            "features": []
        }

    # Clean leading track numbers & artist prefixes (e.g. '01 - Artist - Title' or 'Artist: Title')
    cleaned = strip_track_number_and_artist(raw)
    if "：" in cleaned:
        cleaned = cleaned.split("：", 1)[1].strip()

    # 1. Extract features
    features = []
    for pat in FEATURE_PATTERNS:
        for m in pat.finditer(cleaned):
            f_text = m.group(1).strip()
            if f_text:
                features.append(normalize_text(f_text))

    # 2. Extract version / remix
    v_type = None
    v_text = None
    for pat, vtype in VERSION_PATTERNS:
        m = pat.search(cleaned)
        if m:
            v_type = vtype
            v_text = normalize_text(m.group(1))
            break

    # 3. Clean base title
    base_str = cleaned
    for pat, _ in VERSION_PATTERNS:
        base_str = pat.sub(" ", base_str)
    for pat in FEATURE_PATTERNS:
        base_str = pat.sub(" ", base_str)
    for pat in REMASTER_OR_NOISE_PATTERNS:
        base_str = pat.sub(" ", base_str)

    base_norm = normalize_text(base_str)

    return {
        "raw": raw,
        "base_norm": base_norm,
        "version_type": v_type,
        "version_text": v_text,
        "features": features
    }


def are_versions_compatible(
    v1_type: Optional[str],
    v1_text: Optional[str],
    v2_type: Optional[str],
    v2_text: Optional[str]
) -> bool:
    """
    Determines if two track version modifiers are musically compatible:
    - Both original (None) -> Compatible
    - Original vs Remix/Instrumental/Live -> INCOMPATIBLE
    - Different version types (e.g. remix vs live) -> INCOMPATIBLE
    - Both remixes/edits -> Compatible only if remix descriptors match/overlap
    """
    if v1_type is None and v2_type is None:
        return True
    if (v1_type is None) != (v2_type is None):
        return False
    if v1_type != v2_type:
        return False
    if v1_type in ("remix", "mix_edit", "vip"):
        if not v1_text or not v2_text:
            return True
        sim = calculate_similarity(v1_text, v2_text)
        if sim > 0.65 or v1_text in v2_text or v2_text in v1_text:
            return True
        return False
    return True


def deduplicate_candidate_tracks(tracks_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicates and merges candidate audio track items across Navidrome and Local Disk.
    Consolidates MusicBrainz ID tags and preserves local filesystem paths.
    """
    if not tracks_list:
        return []

    by_fingerprint: Dict[str, Dict[str, Any]] = {}
    merged: List[Dict[str, Any]] = []

    for t in tracks_list:
        norm_t = t.get("norm_title", "")
        norm_a = t.get("norm_album", "")
        trk = str(t.get("track_number", "")).strip()
        fn = t.get("filename", "")
        path = t.get("path", "")

        # Try to find a matching existing fingerprint
        fp = None
        if norm_t and norm_a:
            fp = f"alb_title:{norm_a}::{norm_t}::{trk}"
        elif norm_t:
            fp = f"title:{norm_t}::{trk}::{fn}"

        # Also check if any existing item shares the exact same recording ID
        matched_existing = None
        if fp and fp in by_fingerprint:
            matched_existing = by_fingerprint[fp]
        elif t.get("mb_rec_ids"):
            for ex in by_fingerprint.values():
                if ex.get("mb_rec_ids") and (ex["mb_rec_ids"] & t["mb_rec_ids"]):
                    matched_existing = ex
                    break

        if matched_existing is not None:
            # Merge ID tags and metadata
            matched_existing["mb_track_ids"].update(t.get("mb_track_ids", set()))
            matched_existing["mb_rec_ids"].update(t.get("mb_rec_ids", set()))
            matched_existing["mb_artist_ids"].update(t.get("mb_artist_ids", set()))
            matched_existing["mb_release_ids"].update(t.get("mb_release_ids", set()))
            # Prefer local filesystem path if available
            if t.get("source") == "local" or (path.startswith("/") and not matched_existing.get("path", "").startswith("/")):
                matched_existing["path"] = path
                matched_existing["filename"] = fn or matched_existing.get("filename", "")
                matched_existing["source"] = "local+navidrome"
            elif matched_existing.get("source") != "local":
                matched_existing["source"] = "local+navidrome"
        else:
            item_copy = dict(t)
            item_copy["mb_track_ids"] = set(item_copy.get("mb_track_ids", set()))
            item_copy["mb_rec_ids"] = set(item_copy.get("mb_rec_ids", set()))
            item_copy["mb_artist_ids"] = set(item_copy.get("mb_artist_ids", set()))
            item_copy["mb_release_ids"] = set(item_copy.get("mb_release_ids", set()))
            if fp:
                by_fingerprint[fp] = item_copy
            merged.append(item_copy)

    return merged


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
        query_strip = query.strip()
        search_cache_file = self.cache_dir / "artist_search_cache.json"

        # 1. Check if query is a MusicBrainz URL
        url_match = re.search(r'musicbrainz\.org/artist/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', query_strip, re.I)
        if url_match:
            mbid = url_match.group(1)
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 2. Check if query is already an MBID UUID
        uuid_match = re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', query_strip, re.I)
        if uuid_match:
            mbid = query_strip
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 3. Check persistent search query cache
        search_cache = {}
        if self.use_cache and search_cache_file.exists():
            try:
                with open(search_cache_file, "r", encoding="utf-8") as f:
                    search_cache = json.load(f)
                cached = search_cache.get(query_strip.lower())
                if cached:
                    mbid, name, disambiguation, country = cached
                    console.print(f"[green]✔ Matched Artist (from cache):[/green] [bold]{name}[/bold]{disambiguation}{country} (MBID: {mbid})")
                    return mbid, name
            except Exception:
                pass

        # 4. Check cached artist files in cache_dir
        if self.use_cache and self.cache_dir.exists():
            try:
                for json_file in self.cache_dir.glob("artist_*.json"):
                    with open(json_file, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                    c_artist = cached_data.get("artist", {})
                    c_name = c_artist.get("name", "")
                    c_mbid = c_artist.get("id", "")
                    c_aliases = [a.get("alias", "") if isinstance(a, dict) else str(a) for a in c_artist.get("aliases", [])]
                    if c_name.lower() == query_strip.lower() or any(a.lower() == query_strip.lower() for a in c_aliases):
                        console.print(f"[green]✔ Matched Artist (from cache):[/green] [bold]{c_name}[/bold] (MBID: {c_mbid})")
                        if self.use_cache:
                            search_cache[query_strip.lower()] = [c_mbid, c_name, "", ""]
                            try:
                                with open(search_cache_file, "w", encoding="utf-8") as f:
                                    json.dump(search_cache, f, indent=2)
                            except Exception:
                                pass
                        return c_mbid, c_name
            except Exception:
                pass

        # 5. Search MusicBrainz API via fast requests JSON
        console.print(f"[cyan]Searching MusicBrainz for artist:[/cyan] [bold]{query_strip}[/bold]...")
        
        artist_list = []
        headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION} ({APP_CONTACT})"}
        try:
            r = requests.get(
                "https://musicbrainz.org/ws/2/artist",
                params={"query": query_strip, "fmt": "json", "limit": 10},
                headers=headers,
                timeout=12
            )
            if r.status_code == 200:
                artist_list = r.json().get("artists", [])
        except Exception:
            pass

        if not artist_list:
            try:
                res = musicbrainzngs.search_artists(query=query_strip, limit=10)
                artist_list = res.get('artist-list', [])
            except Exception:
                pass

        if not artist_list:
            raise ValueError(f"No artist found on MusicBrainz matching query '{query_strip}'.")

        # Pick top match
        top_match = artist_list[0]
        mbid = top_match['id']
        name = top_match.get('name', query_strip)
        disambiguation = f" ({top_match.get('disambiguation')})" if top_match.get('disambiguation') else ""
        country = f" [{top_match.get('country')}]" if top_match.get('country') else ""
        console.print(f"[green]✔ Matched Artist:[/green] [bold]{name}[/bold]{disambiguation}{country} (MBID: {mbid})")

        if self.use_cache:
            search_cache[query_strip.lower()] = [mbid, name, disambiguation, country]
            try:
                with open(search_cache_file, "w", encoding="utf-8") as f:
                    json.dump(search_cache, f, indent=2)
            except Exception:
                pass

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
                includes=['recordings', 'release-groups', 'artist-credits', 'media', 'url-rels']
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
                includes=['recordings', 'release-groups', 'artist-credits', 'media', 'url-rels']
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
                includes=['artist-credits', 'work-rels', 'url-rels']
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
                includes=['artist-credits', 'url-rels']
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

        self.releases: List[Dict[str, Any]] = []
        self.all_external_urls: Set[str] = set(self.bandcamp_urls)
        self.release_urls: Dict[str, List[str]] = {}
        self.recording_urls: Dict[str, List[str]] = {}
        self.mediafire_urls: List[str] = []
        self.archive_urls: List[str] = []
        self.web_urls: List[str] = []

        # 1. Primary Artist Releases
        for rel in self.raw_data.get('releases_artist', []):
            rel_title = rel.get('title', 'Unknown Release')
            rel_id = rel.get('id')
            rg_data = rel.get('release-group', {})
            rg_id = rg_data.get('id')
            rg_type = rg_data.get('primary-type') or 'Album'
            rel_date = rel.get('date', '')

            # Extract URLs
            rel_urls = [u.get('target', '') for u in rel.get('url-relation-list', []) if u.get('target')]
            if rel_id and rel_urls:
                self.release_urls[rel_id] = rel_urls
            for u in rel_urls:
                self.all_external_urls.add(u)
                if 'bandcamp.com' in u or 'suckpuck.com' in u:
                    if u not in self.bandcamp_urls: self.bandcamp_urls.append(u)
                elif 'mediafire.com' in u:
                    if u not in self.mediafire_urls: self.mediafire_urls.append(u)
                elif 'archive.org' in u:
                    if u not in self.archive_urls: self.archive_urls.append(u)
                elif u.startswith('http') and not any(ign in u for ign in ('discogs.com', 'rateyourmusic.com', 'wikidata.org', 'imdb.com', 'twitter.com', 'instagram.com')):
                    if u not in self.web_urls: self.web_urls.append(u)

            rel_dict = {
                "id": rel_id,
                "release_group_id": rg_id,
                "title": rel_title,
                "norm_title": normalize_text(rel_title),
                "type": rg_type,
                "date": rel_date,
                "is_va": False,
                "urls": rel_urls
            }
            # Deduplicate primary release entries by normalized title / release-group
            if not any(not r["is_va"] and (r["norm_title"] == rel_dict["norm_title"] or (rg_id and r.get("release_group_id") == rg_id)) for r in self.releases):
                self.releases.append(rel_dict)

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

                    rec_urls = [u.get('target', '') for u in rec.get('url-relation-list', []) if u.get('target')]
                    if rec_id and rec_urls:
                        self.recording_urls[rec_id] = rec_urls
                        for ru in rec_urls:
                            self.all_external_urls.add(ru)

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
                            "all_releases": {rel_title},
                            "urls": list(set(rel_urls + rec_urls))
                        }
                        if norm_t:
                            title_to_key[norm_t] = rec_id
                    else:
                        if not rec_id.startswith("virtual_"):
                            rec_map[rec_id]["recording_ids"].add(rec_id)
                        if track_id:
                            rec_map[rec_id]["track_ids"].add(track_id)
                        rec_map[rec_id]["all_releases"].add(rel_title)
                        for u in rel_urls + rec_urls:
                            if u not in rec_map[rec_id]["urls"]:
                                rec_map[rec_id]["urls"].append(u)

        # 2. Track Artist Releases (Compilations, Splits, Features, VA)
        for rel in self.raw_data.get('releases_track_artist', []):
            rel_title = rel.get('title', 'Unknown Release')
            rel_id = rel.get('id')
            rg_data = rel.get('release-group', {})
            rg_id = rg_data.get('id')
            rg_type = rg_data.get('primary-type') or 'Compilation'
            rel_date = rel.get('date', '')

            rel_urls = [u.get('target', '') for u in rel.get('url-relation-list', []) if u.get('target')]
            if rel_id and rel_urls:
                self.release_urls[rel_id] = rel_urls
            for u in rel_urls:
                self.all_external_urls.add(u)
                if 'bandcamp.com' in u or 'suckpuck.com' in u:
                    if u not in self.bandcamp_urls: self.bandcamp_urls.append(u)
                elif 'mediafire.com' in u:
                    if u not in self.mediafire_urls: self.mediafire_urls.append(u)
                elif 'archive.org' in u:
                    if u not in self.archive_urls: self.archive_urls.append(u)
                elif u.startswith('http') and not any(ign in u for ign in ('discogs.com', 'rateyourmusic.com', 'wikidata.org', 'imdb.com', 'twitter.com', 'instagram.com')):
                    if u not in self.web_urls: self.web_urls.append(u)

            comp_dict = {
                "id": rel_id,
                "release_group_id": rg_id,
                "title": rel_title,
                "norm_title": normalize_text(rel_title),
                "type": f"Compilation ({rg_type})",
                "date": rel_date,
                "is_va": True,
                "urls": rel_urls
            }
            # Deduplicate compilation entries by normalized title / release-group
            if not any(r["is_va"] and (r["norm_title"] == comp_dict["norm_title"] or (rg_id and r.get("release_group_id") == rg_id)) for r in self.releases):
                self.releases.append(comp_dict)

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

                        rec_urls = [u.get('target', '') for u in rec.get('url-relation-list', []) if u.get('target')]
                        if rec_id and rec_urls:
                            self.recording_urls[rec_id] = rec_urls
                            for ru in rec_urls:
                                self.all_external_urls.add(ru)

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
                                "all_releases": {rel_title},
                                "urls": list(set(rel_urls + rec_urls))
                            }
                            if norm_t:
                                title_to_key[norm_t] = rec_id
                        else:
                            if not rec_id.startswith("virtual_"):
                                rec_map[rec_id]["recording_ids"].add(rec_id)
                            if track_id:
                                rec_map[rec_id]["track_ids"].add(track_id)
                            rec_map[rec_id]["all_releases"].add(rel_title)
                            for u in rel_urls + rec_urls:
                                if u not in rec_map[rec_id]["urls"]:
                                    rec_map[rec_id]["urls"].append(u)

        # 3. Direct Recordings (Catch standalone / unreleased tracks and deduplicate by recording ID / title)
        for rec in self.raw_data.get('recordings', []):
            rec_id = rec.get('id')
            title = rec.get('title')
            norm_t = normalize_text(title)
            rec_urls = [u.get('target', '') for u in rec.get('url-relation-list', []) if u.get('target')]
            if rec_id and rec_urls:
                self.recording_urls[rec_id] = rec_urls
                for ru in rec_urls:
                    self.all_external_urls.add(ru)
                    if 'bandcamp.com' in ru or 'suckpuck.com' in ru:
                        if ru not in self.bandcamp_urls: self.bandcamp_urls.append(ru)
                    elif 'mediafire.com' in ru:
                        if ru not in self.mediafire_urls: self.mediafire_urls.append(ru)
                    elif 'archive.org' in ru:
                        if ru not in self.archive_urls: self.archive_urls.append(ru)

            # If this recording ID or title is already associated with an existing track, merge recording ID
            if rec_id and rec_id in rec_map:
                rec_map[rec_id]["recording_ids"].add(rec_id)
                for u in rec_urls:
                    if u not in rec_map[rec_id]["urls"]:
                        rec_map[rec_id]["urls"].append(u)
            elif norm_t and norm_t in title_to_key:
                existing_key = title_to_key[norm_t]
                if rec_id:
                    rec_map[existing_key]["recording_ids"].add(rec_id)
                for u in rec_urls:
                    if u not in rec_map[existing_key]["urls"]:
                        rec_map[existing_key]["urls"].append(u)
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
                    "all_releases": set(),
                    "urls": list(set(rec_urls))
                }
                if norm_t:
                    title_to_key[norm_t] = rec_id

        self.tracks = list(rec_map.values())

    @property
    def primary_releases(self) -> List[Dict[str, Any]]:
        """Returns only primary releases by the artist (excluding compilations/VA)."""
        return [r for r in self.releases if not r.get("is_va", False)]

    @property
    def compilation_releases(self) -> List[Dict[str, Any]]:
        """Returns only compilation/split releases where the artist is a track artist."""
        return [r for r in self.releases if r.get("is_va", False)]


# ==============================================================================
# AUDIO METADATA PERSISTENT CACHE
# ==============================================================================

class AudioMetadataCache:
    """Lightweight persistent SQLite database cache for parsed audio file tags."""
    def __init__(self, db_path: str = DEFAULT_AUDIO_CACHE_DB):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audio_metadata (
                        path TEXT PRIMARY KEY,
                        mtime REAL,
                        size INTEGER,
                        data_json TEXT
                    )
                """)
                conn.commit()
        except Exception:
            pass

    def get_batch(self, file_infos: List[Tuple[str, float, int]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """
        Takes list of (path, mtime, size).
        Returns (cached_map, uncached_paths).
        """
        cached_map: Dict[str, Dict[str, Any]] = {}
        uncached_paths: List[str] = []
        if not file_infos:
            return cached_map, uncached_paths

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for i in range(0, len(file_infos), 500):
                    chunk = file_infos[i:i+500]
                    placeholders = ",".join(["?"] * len(chunk))
                    paths = [p for p, _, _ in chunk]
                    info_map = {p: (mt, sz) for p, mt, sz in chunk}
                    cursor.execute(
                        f"SELECT path, mtime, size, data_json FROM audio_metadata WHERE path IN ({placeholders})",
                        paths
                    )
                    rows = cursor.fetchall()
                    for p, mt, sz, dj in rows:
                        expected_mt, expected_sz = info_map.get(p, (None, None))
                        if expected_mt is not None and abs(mt - expected_mt) < 0.001 and sz == expected_sz:
                            try:
                                d = json.loads(dj)
                                d["mb_track_ids"] = set(d.get("mb_track_ids", []))
                                d["mb_rec_ids"] = set(d.get("mb_rec_ids", []))
                                d["mb_artist_ids"] = set(d.get("mb_artist_ids", []))
                                d["mb_release_ids"] = set(d.get("mb_release_ids", []))
                                cached_map[p] = d
                            except Exception:
                                pass
        except Exception:
            pass

        for p, _, _ in file_infos:
            if p not in cached_map:
                uncached_paths.append(p)

        return cached_map, uncached_paths

    def set_batch(self, items: List[Tuple[str, float, int, Dict[str, Any]]]):
        """Saves parsed audio tag dictionaries to SQLite database."""
        if not items:
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                records = []
                for p, mt, sz, data in items:
                    data_copy = dict(data)
                    data_copy["mb_track_ids"] = list(data.get("mb_track_ids", []))
                    data_copy["mb_rec_ids"] = list(data.get("mb_rec_ids", []))
                    data_copy["mb_artist_ids"] = list(data.get("mb_artist_ids", []))
                    data_copy["mb_release_ids"] = list(data.get("mb_release_ids", []))
                    records.append((p, mt, sz, json.dumps(data_copy)))
                conn.executemany(
                    "INSERT OR REPLACE INTO audio_metadata (path, mtime, size, data_json) VALUES (?, ?, ?, ?)",
                    records
                )
                conn.commit()
        except Exception:
            pass


def is_distinct_track_title(title_norm: str) -> bool:
    """Determines if a normalized track title is distinct enough to safely match standalone filenames."""
    if not title_norm or len(title_norm) < 4:
        return False
    if title_norm in GENERIC_OR_COMMON_WORDS:
        return False
    words = title_norm.split()
    if len(words) >= 2 and len(title_norm) >= 6:
        return True
    if len(words) == 1 and len(title_norm) >= 8 and title_norm not in GENERIC_OR_COMMON_WORDS:
        return True
    return False


# ==============================================================================
# AUDIO LIBRARY SCANNER (OPTIMIZED FOR SSHFS / NETWORK / LOCAL)
# ==============================================================================

class AudioFileScanner:
    def __init__(self, music_dir: str, catalog: ArtistCatalog, full_scan: bool = False, threads: int = 24):
        self.music_dir = Path(music_dir)
        self.catalog = catalog
        self.full_scan = full_scan
        self.threads = threads
        self.cache = AudioMetadataCache()

    def scan(self, progress: Optional[Progress] = None, task_id: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Scans the music directory using a 2-stage fast discovery + parallel metadata extraction
        with persistent SQLite caching.
        """
        if not self.music_dir.exists():
            return []

        if progress and task_id:
            progress.update(task_id, description="[cyan]Stage 1: Discovering candidate audio files on disk...")

        # 1. Compile Artist Alias Patterns (Word boundary)
        alias_pats = []
        for a in self.catalog.aliases:
            norm = normalize_text(a)
            if norm and len(norm) >= 2:
                alias_pats.append(re.compile(r"(?:\b|_)" + re.escape(norm) + r"(?:\b|_)", re.IGNORECASE))

        # 2. Compile Release Title Patterns
        rel_pats = []
        for rel in self.catalog.releases:
            norm = normalize_text(rel.get("title", ""))
            if norm and len(norm) >= 4 and norm not in GENERIC_OR_COMMON_WORDS:
                rel_pats.append(re.compile(r"(?:\b|_)" + re.escape(norm) + r"(?:\b|_)", re.IGNORECASE))

        for trk in self.catalog.tracks:
            for rel_t in trk.get("all_releases", set()):
                norm = normalize_text(rel_t)
                if norm and len(norm) >= 4 and norm not in GENERIC_OR_COMMON_WORDS:
                    rel_pats.append(re.compile(r"(?:\b|_)" + re.escape(norm) + r"(?:\b|_)", re.IGNORECASE))

        # 3. Compile Distinct Track Title Patterns
        trk_pats = []
        for trk in self.catalog.tracks:
            norm = trk.get("norm_title", "")
            if is_distinct_track_title(norm):
                trk_pats.append(re.compile(r"(?:\b|_)" + re.escape(norm) + r"(?:\b|_)", re.IGNORECASE))

        candidate_paths: List[str] = []

        # Stage 1: Walk directory structure (Fast targeted discovery)
        for root, dirs, files in os.walk(self.music_dir):
            norm_root = normalize_text(root)
            dir_matches = any(p.search(norm_root) for p in alias_pats) or any(p.search(norm_root) for p in rel_pats)

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                full_path = os.path.join(root, f)

                if self.full_scan or dir_matches:
                    candidate_paths.append(full_path)
                else:
                    norm_f = normalize_text(f)
                    if any(p.search(norm_f) for p in alias_pats) or any(p.search(norm_f) for p in trk_pats):
                        candidate_paths.append(full_path)

        total_candidates = len(candidate_paths)

        if progress and task_id:
            progress.update(
                task_id,
                description=f"[cyan]Stage 2: Inspecting tags of {total_candidates} candidate audio files...",
                total=total_candidates,
                completed=0
            )

        # Stage 2: Fast Cache Lookup + Parallel Metadata Extraction
        file_infos: List[Tuple[str, float, int]] = []
        for p in candidate_paths:
            try:
                st = os.stat(p)
                file_infos.append((p, st.st_mtime, st.st_size))
            except Exception:
                pass

        cached_map, uncached_paths = self.cache.get_batch(file_infos)
        local_tracks: List[Dict[str, Any]] = list(cached_map.values())

        if progress and task_id and cached_map:
            progress.advance(task_id, len(cached_map))

        # Parse any uncached or modified candidate files
        if uncached_paths:
            new_records: List[Tuple[str, float, int, Dict[str, Any]]] = []
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                for item in pool.map(self._read_audio_metadata, uncached_paths):
                    if item:
                        local_tracks.append(item)
                        try:
                            st = os.stat(item["path"])
                            new_records.append((item["path"], st.st_mtime, st.st_size, item))
                        except Exception:
                            pass
                    if progress and task_id:
                        progress.advance(task_id, 1)

            if new_records:
                self.cache.set_batch(new_records)

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
            if mf and hasattr(mf, "tags") and mf.tags is not None:
                tags = mf.tags
                if hasattr(tags, "items"):
                    for k, v in tags.items():
                        k_str = str(k).upper().strip()
                        v_list = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]

                        # MusicBrainz Tag IDs
                        if k_str in (
                            "MUSICBRAINZ_TRACKID", "MUSICBRAINZ TRACK ID",
                            "MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ RELEASE TRACK ID",
                            "TXXX:MUSICBRAINZ TRACK ID", "TXXX:MUSICBRAINZ RELEASE TRACK ID",
                            "TXXX:MUSICBRAINZ/TRACK ID"
                        ):
                            for x in v_list:
                                if x.strip():
                                    mb_track_ids.add(x.strip())
                        elif k_str in (
                            "MUSICBRAINZ_RECORDINGID", "MUSICBRAINZ RECORDING ID",
                            "TXXX:MUSICBRAINZ RECORDING ID", "TXXX:MUSICBRAINZ/RECORDING ID"
                        ) or k_str.startswith("UFID"):
                            for x in v_list:
                                clean_ufid = re.sub(r"^[^\w\-]+", "", x).replace("http://musicbrainz.org=", "").replace("b'", "").replace("'", "").strip()
                                if clean_ufid:
                                    mb_rec_ids.add(clean_ufid)
                        elif k_str in (
                            "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ ARTIST ID",
                            "MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ ALBUM ARTIST ID",
                            "TXXX:MUSICBRAINZ ARTIST ID", "TXXX:MUSICBRAINZ ALBUM ARTIST ID"
                        ):
                            for x in v_list:
                                if x.strip():
                                    mb_artist_ids.add(x.strip())
                        elif k_str in (
                            "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ ALBUM ID",
                            "TXXX:MUSICBRAINZ ALBUM ID"
                        ):
                            for x in v_list:
                                if x.strip():
                                    mb_release_ids.add(x.strip())

                        # Core Metadata (Exact ID3 / Vorbis matching to avoid ReplayGain overwrite)
                        if k_str in ("TIT2", "TITLE", "\xa9NAM", "TXXX:TITLE") and not title:
                            title = v_list[0].strip() if v_list else ""
                        elif k_str in ("TALB", "ALBUM", "\xa9ALB", "TXXX:ALBUM") and not album:
                            album = v_list[0].strip() if v_list else ""
                        elif k_str in ("TRCK", "TRACKNUMBER", "TXXX:TRACKNUMBER") and not track_number:
                            raw_trck = v_list[0].strip() if v_list else ""
                            track_number = raw_trck.split("/")[0].strip()
                        elif k_str in (
                            "TPE1", "TPE2", "TOPE", "TEXT", "TCOM", "ARTIST",
                            "ALBUMARTIST", "COMPOSER", "PERFORMER", "ARTISTS", "\xa9ART", "AART"
                        ):
                            for x in v_list:
                                x_clean = x.strip()
                                if x_clean and x_clean not in artists:
                                    artists.append(x_clean)
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
            "mb_release_ids": mb_release_ids,
            "source": "local"
        }


# ==============================================================================
# NAVIDROME / SUBSONIC REMOTE LIBRARY SCANNER
# ==============================================================================

class NavidromeScanner:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        catalog: ArtistCatalog,
        timeout: int = 15
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.catalog = catalog
        self.timeout = timeout

    def test_connection(self) -> bool:
        """Pings the Navidrome/Subsonic server to verify connectivity and credentials."""
        res = self._api_request("ping", {})
        return res is not None

    def _api_request(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        salt = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()

        req_params = {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "musicscraper",
            "f": "json"
        }
        req_params.update(params)

        url = f"{self.base_url}/rest/{endpoint}.view?{urllib.parse.urlencode(req_params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "musicscraper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sub_resp = data.get("subsonic-response", {})
                if sub_resp.get("status") == "ok":
                    return sub_resp
        except Exception:
            pass
        return None

    def scan(self, progress: Optional[Progress] = None, task_id: Optional[Any] = None) -> List[Dict[str, Any]]:
        """
        Queries Navidrome Subsonic API for:
        1. Artist aliases & transliterations via search3.view
        2. Exact MusicBrainz Artist ID (if indexed) via getArtists.view / getArtist.view
        3. Primary release titles & distinct track titles
        """
        if progress and task_id:
            progress.update(task_id, description=f"[cyan]Connecting to Navidrome server ({self.base_url})...")

        found_songs: Dict[str, Dict[str, Any]] = {}
        processed_queries: Set[str] = set()

        # 1. Search by artist aliases
        for a in self.catalog.aliases:
            clean_a = a.strip()
            if clean_a and len(clean_a) >= 2 and clean_a.lower() not in processed_queries:
                processed_queries.add(clean_a.lower())
                if progress and task_id:
                    progress.update(task_id, description=f"[cyan]Navidrome: Searching for artist '{clean_a}'...")
                res = self._api_request("search3", {
                    "query": clean_a,
                    "artistCount": 20,
                    "albumCount": 50,
                    "songCount": 500
                })
                if res:
                    for s in res.get("searchResult3", {}).get("song", []):
                        sid = s.get("id") or s.get("path")
                        if sid:
                            found_songs[sid] = s

        # 2. Check for exact MBID match in Navidrome artist catalog
        if self.catalog.mbid:
            try:
                artists_res = self._api_request("getArtists", {})
                if artists_res:
                    for idx in artists_res.get("artists", {}).get("index", []):
                        for artist in idx.get("artist", []):
                            if artist.get("musicBrainzId") == self.catalog.mbid:
                                artist_id = artist.get("id")
                                if artist_id:
                                    artist_detail = self._api_request("getArtist", {"id": artist_id})
                                    if artist_detail:
                                        for alb in artist_detail.get("artist", {}).get("album", []):
                                            alb_id = alb.get("id")
                                            if alb_id:
                                                alb_detail = self._api_request("getAlbum", {"id": alb_id})
                                                if alb_detail:
                                                    for s in alb_detail.get("album", {}).get("song", []):
                                                        sid = s.get("id") or s.get("path")
                                                        if sid:
                                                            found_songs[sid] = s
            except Exception:
                pass

        # 3. Search for primary release titles (e.g. non-compilation albums)
        for rel in self.catalog.releases:
            rel_title = rel.get("title", "").strip()
            norm_rel = normalize_text(rel_title)
            if norm_rel and len(norm_rel) >= 5 and norm_rel not in GENERIC_OR_COMMON_WORDS and norm_rel not in processed_queries:
                processed_queries.add(norm_rel)
                res = self._api_request("search3", {
                    "query": rel_title,
                    "artistCount": 5,
                    "albumCount": 20,
                    "songCount": 200
                })
                if res:
                    for s in res.get("searchResult3", {}).get("song", []):
                        sid = s.get("id") or s.get("path")
                        if sid:
                            found_songs[sid] = s

        # Convert to local_tracks schema
        nav_tracks: List[Dict[str, Any]] = []
        for s in found_songs.values():
            title = s.get("title", "")
            album = s.get("album", "")
            track_num = str(s.get("track", ""))
            path = s.get("path", "")
            artists = [s.get("artist", "")]
            for a in s.get("artists", []):
                name = a.get("name", "")
                if name and name not in artists:
                    artists.append(name)

            mb_rec_ids: Set[str] = set()
            mb_trk_ids: Set[str] = set()
            mbid_s = s.get("musicBrainzId", "")
            if mbid_s:
                mb_rec_ids.add(mbid_s)

            nav_tracks.append({
                "path": path,
                "filename": os.path.basename(path),
                "title": title or "",
                "norm_title": normalize_text(title),
                "album": album or "",
                "norm_album": normalize_text(album),
                "track_number": track_num or "",
                "artists": artists,
                "mb_track_ids": mb_trk_ids,
                "mb_rec_ids": mb_rec_ids,
                "mb_artist_ids": set(),
                "mb_release_ids": set(),
                "source": "navidrome"
            })

        return nav_tracks


# ==============================================================================
# MULTI-TIER RECONCILIATION & MATCHING ENGINE
# ==============================================================================

class DiscographyReconciler:
    def __init__(self, catalog: ArtistCatalog, local_tracks: List[Dict[str, Any]]):
        self.catalog = catalog
        self.local_tracks = deduplicate_candidate_tracks(local_tracks)
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

        mb_parsed = [parse_track_title_structure(mb["title"]) for mb in mb_tracks]
        local_parsed = [
            parse_track_title_structure(lt.get("title") or lt.get("filename") or "")
            for lt in self.local_tracks
        ]

        # -------------------------------------------------------------
        # TIER 1: Exact MusicBrainz Tag Matching (MBID Track/Recording)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue
            mb_rec_ids = mb.get("recording_ids", set())
            mb_track_ids = mb.get("track_ids", set())

            for j, lt in enumerate(self.local_tracks):
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
        # TIER 2: Exact Release + Compatible Track Title Match
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            p_mb = mb_parsed[i]
            mb_rel_norm = mb.get("norm_release", "")
            if not p_mb["base_norm"]:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                lt_album_norm = lt.get("norm_album", "")
                path_norm = normalize_text(lt.get("path", ""))

                base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                base_match = (p_mb["base_norm"] == p_lt["base_norm"]) or base_sim > 0.90
                ver_compat = are_versions_compatible(
                    p_mb["version_type"], p_mb["version_text"],
                    p_lt["version_type"], p_lt["version_text"]
                )

                if base_match and ver_compat:
                    rel_sim = calculate_similarity(mb_rel_norm, lt_album_norm)
                    path_has_rel = bool(mb_rel_norm and mb_rel_norm in path_norm)

                    if rel_sim > 0.7 or path_has_rel or mb.get("release_title") == "Standalone / Other":
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

            p_mb = mb_parsed[i]
            if not p_mb["base_norm"]:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                path_norm = normalize_text(lt.get("path", ""))

                has_artist_tag = any(
                    any(alias in a.lower() or alias in unidecode(a.lower()) for alias in artist_aliases)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(alias in path_norm for alias in artist_aliases)

                if has_artist_tag or has_artist_path or (mb.get("recording_ids") and any(rid in lt["mb_rec_ids"] for rid in mb["recording_ids"])):
                    base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                    base_match = (p_mb["base_norm"] == p_lt["base_norm"]) or base_sim > 0.90
                    ver_compat = are_versions_compatible(
                        p_mb["version_type"], p_mb["version_text"],
                        p_lt["version_type"], p_lt["version_text"]
                    )

                    if base_match and ver_compat:
                        self.matched[i] = (lt, "Title & Artist Alias Match")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

        # -------------------------------------------------------------
        # TIER 4: Strict Fuzzy Match (Transliterations, Minor Variances)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            p_mb = mb_parsed[i]
            mb_rel_norm = mb.get("norm_release", "")
            if not p_mb["base_norm"] or len(p_mb["base_norm"]) < 3:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                path_norm = normalize_text(lt.get("path", ""))

                # Verify artist or release association to avoid matching other artists in compilation folders
                has_artist_tag = any(
                    any(alias in a.lower() or alias in unidecode(a.lower()) for alias in artist_aliases)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(alias in path_norm for alias in artist_aliases)
                has_rel_match = bool(mb_rel_norm and (mb_rel_norm in path_norm or mb_rel_norm == lt.get("norm_album", "")))

                # Require artist alias or release album verification for fuzzy match
                if not (has_artist_tag or has_artist_path or has_rel_match or mb.get("release_title") == "Standalone / Other"):
                    continue

                # Version compatibility is strictly required
                ver_compat = are_versions_compatible(
                    p_mb["version_type"], p_mb["version_text"],
                    p_lt["version_type"], p_lt["version_text"]
                )
                if not ver_compat:
                    continue

                base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                full_sim = calculate_similarity(mb["norm_title"], lt.get("norm_title", ""))
                max_sim = max(base_sim, full_sim)

                if max_sim >= 0.85:
                    self.matched[i] = (lt, f"Fuzzy Match ({int(max_sim*100)}%)")
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
  python3 check_missing_tracks.py "Glidelas" --source navidrome
  python3 check_missing_tracks.py "https://musicbrainz.org/artist/2dbd3954-9bb7-4165-9445-98f66c3861bf"
  python3 check_missing_tracks.py "Aphex Twin" --only-missing --export-txt missing.txt
  python3 check_missing_tracks.py "goreshit" --export-bandcamp-links goreshit_bc.txt
        """
    )
    parser.add_argument("artist", help="Artist Name, MusicBrainz Artist ID (MBID), or MusicBrainz Artist URL")
    parser.add_argument("-d", "--dir", "--music-dir", dest="music_dir", default="/mnt/music", help="Path to local music library directory (default: /mnt/music)")
    parser.add_argument("--source", choices=["auto", "local", "navidrome", "both"], default="auto", help="Library source to scan: 'auto' (detects Navidrome / local), 'navidrome', 'local', or 'both' (default: auto)")
    parser.add_argument("--navidrome", "--subsonic", action="store_true", help="Force scanning Navidrome/Subsonic server")
    parser.add_argument("--navidrome-url", type=str, default="", help="Navidrome/Subsonic server URL (default: from NAVIDROME_URL in .env)")
    parser.add_argument("--navidrome-user", "--navidrome-username", dest="navidrome_username", type=str, default="", help="Navidrome/Subsonic username (default: from NAVIDROME_USERNAME in .env)")
    parser.add_argument("--navidrome-pass", "--navidrome-password", dest="navidrome_password", type=str, default="", help="Navidrome/Subsonic password (default: from NAVIDROME_PASSWORD in .env)")
    parser.add_argument("--full-scan", action="store_true", help="Perform a full deep-scan of every audio file in the local music directory instead of fast path pre-filtering")
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

    # Step 4: Scan Music Library (Navidrome / Subsonic Server and/or Local Filesystem)
    nav_url = (args.navidrome_url or os.getenv("NAVIDROME_URL", os.getenv("SUBSONIC_URL", ""))).strip()
    nav_user = (args.navidrome_username or os.getenv("NAVIDROME_USERNAME", os.getenv("SUBSONIC_USERNAME", ""))).strip()
    nav_pass = (args.navidrome_password or os.getenv("NAVIDROME_PASSWORD", os.getenv("SUBSONIC_PASSWORD", ""))).strip()

    has_nav_config = bool(nav_url and nav_user and nav_pass)

    if args.navidrome:
        scan_nav = True
        scan_local = (args.source == "both")
    elif args.source == "navidrome":
        scan_nav = True
        scan_local = False
    elif args.source == "local":
        scan_nav = False
        scan_local = True
    elif args.source == "both":
        scan_nav = has_nav_config
        scan_local = True
    else:  # auto
        scan_nav = has_nav_config
        scan_local = True

    if (args.navidrome or args.source == "navidrome") and not has_nav_config:
        console.print("[red]Error: Navidrome credentials missing.[/red] Please set NAVIDROME_URL, NAVIDROME_USERNAME, and NAVIDROME_PASSWORD in .env or pass --navidrome-url, --navidrome-user, --navidrome-pass")
        sys.exit(1)

    local_tracks: List[Dict[str, Any]] = []
    seen_paths: Set[str] = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        # Scan Navidrome
        if scan_nav and has_nav_config:
            task_id = progress.add_task(f"[cyan]Connecting to Navidrome ({nav_url})...", total=None)
            nav_scanner = NavidromeScanner(
                base_url=nav_url,
                username=nav_user,
                password=nav_pass,
                catalog=catalog
            )
            try:
                nav_tracks = nav_scanner.scan(progress=progress, task_id=task_id)
                for nt in nav_tracks:
                    p = nt["path"]
                    if p not in seen_paths:
                        local_tracks.append(nt)
                        seen_paths.add(p)
                progress.update(task_id, description=f"[green]✔ Retrieved {len(nav_tracks)} tracks from Navidrome server ({nav_url})", completed=1, total=1)
            except Exception as e:
                console.print(f"[yellow]Warning: Navidrome scan error ({nav_url}):[/yellow] {e}")

        # Scan Local Filesystem
        disk_tracks_count = 0
        local_dir_scanned = False
        if scan_local:
            local_dir_path = Path(args.music_dir)
            if not local_dir_path.exists():
                console.print(f"[bold yellow]⚠ Warning:[/bold yellow] Local music directory '[bold cyan]{args.music_dir}[/bold cyan]' does not exist or is unmounted.")
            else:
                local_dir_scanned = True
                task_id = progress.add_task(f"[cyan]Scanning local library ({args.music_dir})...", total=None)
                scanner = AudioFileScanner(
                    music_dir=args.music_dir,
                    catalog=catalog,
                    full_scan=args.full_scan,
                    threads=args.threads
                )
                try:
                    disk_tracks = scanner.scan(progress=progress, task_id=task_id)
                    disk_tracks_count = len(disk_tracks)
                    added_disk = 0
                    for dt in disk_tracks:
                        p = dt["path"]
                        if p not in seen_paths:
                            local_tracks.append(dt)
                            seen_paths.add(p)
                            added_disk += 1
                    if disk_tracks_count == 0:
                        progress.update(task_id, description=f"[yellow]⚠ Local library ({args.music_dir}) contains 0 audio files (unmounted after reboot?)", completed=1, total=1)
                    else:
                        progress.update(task_id, description=f"[green]✔ Parsed {disk_tracks_count} audio files from local library", completed=1, total=1)
                except Exception as e:
                    console.print(f"[red]Error scanning music directory '{args.music_dir}':[/red] {e}")

    # Deduplicate candidate tracks across sources (Navidrome + Local Filesystem)
    raw_candidate_count = len(local_tracks)
    local_tracks = deduplicate_candidate_tracks(local_tracks)

    # Visible Warning & Status Notifications
    if local_dir_scanned and disk_tracks_count == 0:
        if len(local_tracks) > 0:
            console.print(f"[bold yellow]⚠ Notice:[/bold yellow] Local directory '[bold cyan]{args.music_dir}[/bold cyan]' contains 0 audio files (unmounted after reboot?). Using [bold green]{len(local_tracks)}[/bold green] tracks from Navidrome server.")
        else:
            warning_panel = Panel(
                f"[bold yellow]⚠ 0 audio files found in '{args.music_dir}' or Navidrome server.[/bold yellow]\n\n"
                f"• If [bold cyan]{args.music_dir}[/bold cyan] is an SSHFS, NFS, or network mount, make sure it is mounted after rebooting!\n"
                f"• Check mount status: [dim]`ls {args.music_dir}`[/dim]\n"
                f"• All [bold]{len(catalog.tracks)}[/bold] tracks in the MusicBrainz catalog will be marked as missing.",
                title="[bold yellow]Unmounted / Empty Library Warning[/bold yellow]",
                border_style="yellow",
                box=box.ROUNDED
            )
            console.print(warning_panel)
    elif len(local_tracks) == 0:
        console.print(Panel(
            f"[bold yellow]⚠ 0 candidate tracks found for '{catalog.name}' in local library or server.[/bold yellow]\n"
            f"[dim]All {len(catalog.tracks)} tracks in the MusicBrainz catalog will be marked as missing.[/dim]",
            title="[bold yellow]Library Scan Notice[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED
        ))
    else:
        if raw_candidate_count != len(local_tracks):
            console.print(f"[dim]Total {len(local_tracks)} unique candidate tracks loaded for reconciliation (deduplicated from {raw_candidate_count} scanned items).[/dim]")
        else:
            console.print(f"[dim]Total {len(local_tracks)} candidate tracks loaded for reconciliation.[/dim]")

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
