#!/usr/bin/env python3
"""
Last.fm Genre Tagger
====================
An intelligent, high-performance CLI tool and library for automatically
fetching, normalizing, and applying genre metadata to music collections
(artists, albums, and tracks) based on Last.fm community tags.

Features:
- Multi-level Tagging & Cascading: Track tags -> Album tags -> Artist tags (or blended weights)
- Smart Genre Normalization: Filters non-genre noise (ratings, playlist tags, formats, years, duplicates)
  and canonicalizes casing/aliases (IDM, J-Core, Breakcore, Drum and Bass, Lo-Fi, R&B, etc.)
- Multi-Format Mutagen Support: MP3 (ID3v2.3/v2.4 TCON), FLAC (Vorbis GENRE), M4A (©gen), OGG/Opus, WAV
- SQLite Caching & Fast Concurrency: Local database cache minimizes API calls and accelerates batch tagging
- Tagging Modes: Overwrite, Skip Existing, Append, and Dry-Run preview
- Rich Terminal Interface: Progress bars, before/after diff tables, and detailed summary statistics
"""

import os
import sys
import re
import time
import json
import sqlite3
import urllib.parse
import argparse
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import requests
import mutagen
from mutagen.id3 import ID3, TCON, ID3NoHeaderError
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mutagen.asf import ASF
from mutagen.apev2 import APEv2

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.text import Text
from rich import box

console = Console()

# Default fallback Last.fm API Key (Users can override via --api-key or LASTFM_API_KEY env var)
DEFAULT_API_KEY = "b25b959554ed76058ac220b7b2e0a026"
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Supported Audio File Extensions
SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav",
    ".aif", ".aiff", ".wma", ".ape", ".wv"
}

# ---------------------------------------------------------------------------
# Genre Normalization & Filtering
# ---------------------------------------------------------------------------

# Built-in Blacklist: Non-genre tags, subjective ratings, playback/collection info, years, noise
DEFAULT_BLACKLIST_TAGS = {
    # Subjective ratings / opinions
    "favorite", "favorites", "favourite", "favourites", "my favorites", "my favourites",
    "favorite songs", "favourite songs", "favourite tracks", "favorite tracks",
    "favorite albums", "favourite albums", "fav", "favs", "loved", "love", "awesome",
    "masterpiece", "guilty pleasure", "guilty pleasures", "classic", "good", "great",
    "cool", "underrated", "overrated", "best", "super", "perfection", "10/10",
    "heard on pandora", "under 2000 listeners", "under 1000 listeners", "radio",
    "charts", "top", "hit", "essential", "beautiful", "chill", "relaxing", "sad",
    "depressing", "happy", "melancholy", "nostalgic", "energetic", "dark", "heavy",
    "epic", "cute", "fun", "weird", "crazy", "funny", "masterpiece", "banger",
    
    # Collection / Playback / Streaming / Store tags
    "seen live", "seen live in concert", "albums i own", "tracks i own", "songs i own",
    "owned", "i own", "vinyl", "cd", "cds", "cassette", "tape", "mp3", "flac",
    "lossless", "spotify", "buy", "buy it", "check out", "listen to", "to listen",
    "need to buy", "wishlist", "recommend", "recommended", "download", "free download",
    "bandcamp", "soundcloud", "youtube", "myspace", "itunes", "audiotree", "kexp",
    
    # Formats & Release types
    "album", "albums", "ep", "eps", "single", "singles", "lp", "compilation",
    "compilations", "split", "split ep", "bootleg", "soundtrack", "ost", "score",
    "rip", "remix", "remixes", "mashup", "mash-up", "mashups", "cover", "covers",
    "instrumental", "acoustic", "live", "demo", "demos", "unreleased", "b-side",
    "bonus track", "various artists", "va", "promo", "white label", "netlabel",
    
    # Vocals / Misc non-genre classifiers (unless specifically desired)
    "female vocalist", "female vocalists", "male vocalist", "male vocalists",
    "female vocals", "male vocals", "female singer", "male singer",
    "vocal", "vocals", "singer-songwriter", "arranger", "composer", "producer",
    
    # Internet / Meme / Emoticons / Gibberish
    ":3", "^^", "^_^", "xd", ":d", "<3", "meme", "memes", "shitpost", "shitposting",
    "lol", "random", "misc", "wtf", "all", "other", "unknown", "none", "n/a",
    "tracks", "music", "songs", "sound", "sounds", "tunes", "good music"
}

# Regex to match years / decades / pure numbers
YEAR_DECADE_REGEX = re.compile(r"^(19|20)\d{2}s?$|^\d{2}s$|^\d+$")

# Canonical Genre Name Mappings & Capitalization Rules
CANONICAL_GENRES: Dict[str, str] = {
    # Electronic / Hardcore / Bass / Core
    "breakcore": "Breakcore",
    "happy breakcore": "Happy Breakcore",
    "drill n bass": "Drill 'n' Bass",
    "drill 'n' bass": "Drill 'n' Bass",
    "drill and bass": "Drill 'n' Bass",
    "idm": "IDM",
    "intelligent dance music": "IDM",
    "edm": "EDM",
    "electronic dance music": "EDM",
    "j-core": "J-Core",
    "jcore": "J-Core",
    "japanese hardcore": "Japanese Hardcore",
    "lolicore": "Lolicore",
    "speedcore": "Speedcore",
    "extratone": "Extratone",
    "splittercore": "Splittercore",
    "flashcore": "Flashcore",
    "suiscore": "Suiscore",
    "mashcore": "Mashcore",
    "frenchcore": "Frenchcore",
    "terrorcore": "Terrorcore",
    "dancecore": "Dancecore",
    "digital hardcore": "Digital Hardcore",
    "hardcore techno": "Hardcore Techno",
    "happy hardcore": "Happy Hardcore",
    "uk hardcore": "UK Hardcore",
    "gabber": "Gabber",
    "early hardcore": "Early Hardcore",
    "mainstream hardcore": "Mainstream Hardcore",
    "hardstyle": "Hardstyle",
    "rawstyle": "Rawstyle",
    "euphoric hardstyle": "Euphoric Hardstyle",
    "hands up": "Hands Up",
    "handsup": "Hands Up",
    "eurodance": "Eurodance",
    "eurobeat": "Eurobeat",
    "nightcore": "Nightcore",
    "daycore": "Daycore",
    
    # Drum & Bass / Jungle / Breakbeat
    "drum and bass": "Drum and Bass",
    "drum & bass": "Drum and Bass",
    "drum n bass": "Drum and Bass",
    "drum 'n' bass": "Drum and Bass",
    "dnb": "Drum and Bass",
    "d&b": "Drum and Bass",
    "jungle": "Jungle",
    "ragga jungle": "Ragga Jungle",
    "atmospheric jungle": "Atmospheric Jungle",
    "liquid funk": "Liquid Funk",
    "liquid drum and bass": "Liquid Drum and Bass",
    "liquid dnb": "Liquid Drum and Bass",
    "neurofunk": "Neurofunk",
    "techstep": "Techstep",
    "jump up": "Jump Up",
    "breakbeat": "Breakbeat",
    "breaks": "Breaks",
    "big beat": "Big Beat",
    "breakbeat hardcore": "Breakbeat Hardcore",
    
    # House / Techno / Trance
    "techno": "Techno",
    "acid techno": "Acid Techno",
    "hard techno": "Hard Techno",
    "industrial techno": "Industrial Techno",
    "minimal techno": "Minimal Techno",
    "dub techno": "Dub Techno",
    "house": "House",
    "acid house": "Acid House",
    "deep house": "Deep House",
    "tech house": "Tech House",
    "electro house": "Electro House",
    "progressive house": "Progressive House",
    "french house": "French House",
    "trance": "Trance",
    "psytrance": "Psytrance",
    "psychedelic trance": "Psytrance",
    "goa trance": "Goa Trance",
    "progressive trance": "Progressive Trance",
    "uplifting trance": "Uplifting Trance",
    "hard trance": "Hard Trance",
    "euro trance": "Euro Trance",
    
    # Bass / Dubstep / Garage
    "dubstep": "Dubstep",
    "deathstep": "Deathstep",
    "brostep": "Brostep",
    "riddim": "Riddim",
    "riddim dubstep": "Riddim",
    "uk garage": "UK Garage",
    "ukg": "UK Garage",
    "speed garage": "Speed Garage",
    "2-step": "2-Step",
    "future garage": "Future Garage",
    "grime": "Grime",
    "bass music": "Bass Music",
    "future bass": "Future Bass",
    "trap": "Trap",
    "electronic trap": "Trap",
    
    # Ambient / Experimental / Noise / Chiptune
    "ambient": "Ambient",
    "dark ambient": "Dark Ambient",
    "drone": "Drone",
    "noise": "Noise",
    "harsh noise": "Harsh Noise",
    "harsh noise wall": "Harsh Noise Wall",
    "power electronics": "Power Electronics",
    "experimental": "Experimental",
    "avant-garde": "Avant-Garde",
    "glitch": "Glitch",
    "chiptune": "Chiptune",
    "8-bit": "8-Bit",
    "8bit": "8-Bit",
    "16-bit": "16-Bit",
    "nintendocore": "Nintendocore",
    "synthwave": "Synthwave",
    "vaporwave": "Vaporwave",
    "future funk": "Future Funk",
    "chillwave": "Chillwave",
    "downtempo": "Downtempo",
    "trip hop": "Trip-Hop",
    "trip-hop": "Trip-Hop",
    "lo-fi": "Lo-Fi",
    "lofi": "Lo-Fi",
    "lo-fi hip hop": "Lo-Fi Hip-Hop",
    "lofi hip hop": "Lo-Fi Hip-Hop",
    "dungeon synth": "Dungeon Synth",
    
    # Rock / Metal / Punk
    "rock": "Rock",
    "alternative rock": "Alternative Rock",
    "alt-rock": "Alternative Rock",
    "indie rock": "Indie Rock",
    "post-rock": "Post-Rock",
    "math rock": "Math Rock",
    "shoegaze": "Shoegaze",
    "dream pop": "Dream Pop",
    "dreampop": "Dream Pop",
    "noise rock": "Noise Rock",
    "punk": "Punk",
    "punk rock": "Punk Rock",
    "hardcore punk": "Hardcore Punk",
    "post-punk": "Post-Punk",
    "pop punk": "Pop Punk",
    "emo": "Emo",
    "screamo": "Screamo",
    "metal": "Metal",
    "heavy metal": "Heavy Metal",
    "death metal": "Death Metal",
    "black metal": "Black Metal",
    "thrash metal": "Thrash Metal",
    "doom metal": "Doom Metal",
    "sludge metal": "Sludge Metal",
    "industrial metal": "Industrial Metal",
    "metalcore": "Metalcore",
    "deathcore": "Deathcore",
    "grindcore": "Grindcore",
    "cybergrind": "Cybergrind",
    "goregrind": "Goregrind",
    "pornogrind": "Pornogrind",
    
    # Pop / Urban / Regional
    "pop": "Pop",
    "synthpop": "Synthpop",
    "electropop": "Electropop",
    "indie pop": "Indie Pop",
    "hyperpop": "Hyperpop",
    "j-pop": "J-Pop",
    "jpop": "J-Pop",
    "k-pop": "K-Pop",
    "kpop": "K-Pop",
    "c-pop": "C-Pop",
    "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop",
    "rap": "Rap",
    "r&b": "R&B",
    "rnb": "R&B",
    "contemporary r&b": "Contemporary R&B",
    "soul": "Soul",
    "neo-soul": "Neo-Soul",
    "funk": "Funk",
    "disco": "Disco",
    "jazz": "Jazz",
    "fusion": "Jazz Fusion",
    "jazz fusion": "Jazz Fusion",
    "reggae": "Reggae",
    "dub": "Dub",
    "dancehall": "Dancehall",
    "ska": "Ska",
    "folk": "Folk",
    "indie folk": "Indie Folk",
    "acoustic": "Acoustic",
    "classical": "Classical",
    "soundtrack": "Soundtrack",
    "doujin": "Doujin",
    "doujin music": "Doujin Music",
    "doujin ongaku": "Doujin Ongaku",
    "touhou": "Touhou",
    "vocaloid": "Vocaloid"
}


class GenreNormalizer:
    """Intelligently cleans, filters, and standardizes Last.fm tags into high-quality genres."""
    
    def __init__(
        self,
        min_count: int = 5,
        custom_blacklist: Optional[Set[str]] = None,
        custom_whitelist: Optional[Set[str]] = None,
        allow_nationality: bool = False,
        allow_vocals: bool = False
    ):
        self.min_count = min_count
        self.blacklist = set(DEFAULT_BLACKLIST_TAGS)
        if custom_blacklist:
            self.blacklist.update(custom_blacklist)
        self.whitelist = custom_whitelist or set()
        self.allow_nationality = allow_nationality
        self.allow_vocals = allow_vocals
        
        # Nationality / country tags to filter unless explicitly allowed
        self.nationality_tags = {
            "japanese", "japan", "british", "uk", "american", "usa", "us", "english",
            "german", "germany", "french", "france", "belgian", "belgium", "dutch",
            "netherlands", "canadian", "canada", "australian", "australia", "swedish",
            "sweden", "finnish", "finland", "norwegian", "norway", "russian", "russia",
            "polish", "poland", "spanish", "spain", "italian", "italy", "korean",
            "korea", "south korea", "chinese", "china", "brazilian", "brazil",
            "mexican", "mexico", "chilean", "chile", "tokyo", "london"
        }
        
    def clean_tag_name(self, raw_tag: str) -> str:
        """Strip extraneous symbols and whitespace."""
        t = raw_tag.strip().lower()
        # Remove surrounding quotes, brackets, hyphens
        t = re.sub(r"^[\s\-_'\"`\[\]\(\)]+|[\s\-_'\"`\[\]\(\)]+$", "", t)
        # Normalize multiple spaces or weird punctuation
        t = re.sub(r"\s+", " ", t)
        return t

    def is_valid_genre(self, tag: str, artist: str = "", album: str = "", track: str = "") -> bool:
        """Check whether a tag is a valid genre or noisy metadata."""
        if not tag or len(tag) < 2 or len(tag) > 40:
            return False
            
        t_clean = self.clean_tag_name(tag)
        
        if self.whitelist and t_clean in self.whitelist:
            return True
            
        # Check blacklist
        if t_clean in self.blacklist:
            return False
            
        # Check year / decade pattern
        if YEAR_DECADE_REGEX.match(t_clean):
            return False
            
        # Check nationality tags
        if not self.allow_nationality and t_clean in self.nationality_tags:
            return False
            
        # Check vocal tags
        if not self.allow_vocals and ("vocal" in t_clean or "female" in t_clean or "male" in t_clean):
            if t_clean not in {"vocaloid", "vocal trance", "vocal house"}:
                return False
                
        # Check if tag is essentially the artist name, album name, or track title
        if artist:
            art_clean = self.clean_tag_name(artist)
            if t_clean == art_clean or (len(art_clean) > 3 and art_clean in t_clean):
                return False
                
        if album:
            alb_clean = self.clean_tag_name(album)
            if t_clean == alb_clean or (len(alb_clean) > 3 and alb_clean in t_clean):
                return False
                
        if track:
            trk_clean = self.clean_tag_name(track)
            if t_clean == trk_clean or (len(trk_clean) > 3 and trk_clean in t_clean):
                return False

        # Filter pure punctuation/emoticons
        if not re.search(r"[a-zA-Z0-9\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf]", t_clean):
            return False

        return True

    def canonicalize(self, tag: str) -> str:
        """Standardize genre capitalization and alias mapping."""
        t_clean = self.clean_tag_name(tag)
        if t_clean in CANONICAL_GENRES:
            return CANONICAL_GENRES[t_clean]
            
        # Custom Title Casing with preservation of special short words
        words = t_clean.split(" ")
        title_words = []
        for i, word in enumerate(words):
            if word in {"and", "&", "of", "the", "in", "on", "vs", "feat"} and i > 0:
                title_words.append(word)
            elif word.upper() in {"IDM", "EDM", "EP", "LP", "DJ", "OST", "UK", "US", "J-CORE", "R&B", "DNB"}:
                title_words.append(word.upper())
            else:
                title_words.append(word.capitalize())
        return " ".join(title_words)

    def filter_and_format(
        self,
        raw_tags: List[Dict[str, Any]],
        artist: str = "",
        album: str = "",
        track: str = "",
        limit: int = 3
    ) -> List[str]:
        """
        Filters raw Last.fm tag objects [{'name': '...', 'count': 100}, ...]
        Returns sorted, canonicalized genre strings up to limit.
        """
        seen: Set[str] = set()
        result: List[str] = []

        for tag_obj in raw_tags:
            name = tag_obj.get("name", "")
            count = tag_obj.get("count", 0)
            try:
                count = int(count)
            except (ValueError, TypeError):
                count = 0

            if self.min_count > 0 and count < self.min_count:
                continue

            if not self.is_valid_genre(name, artist=artist, album=album, track=track):
                continue

            canon = self.canonicalize(name)
            canon_lower = canon.lower()

            if canon_lower not in seen:
                seen.add(canon_lower)
                result.append(canon)
                if len(result) >= limit:
                    break

        return result


# ---------------------------------------------------------------------------
# Last.fm API Client with SQLite Caching
# ---------------------------------------------------------------------------

class LastFMClient:
    """Handles requests to Last.fm API with local SQLite caching, rate limiting, and retries."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        use_cache: bool = True
    ):
        self.api_key = api_key or os.environ.get("LASTFM_API_KEY") or DEFAULT_API_KEY
        self.use_cache = use_cache
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MusicScraperGenreTagger/1.0 (https://github.com/naxeron/musicscraper)"
        })
        
        # SQLite caching setup
        self.cache_dir = cache_dir or (Path.home() / ".cache" / "musicscraper")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "lastfm_tags_cache.sqlite"
        self._local = threading.local()
        self._init_db()

    def _get_db(self) -> sqlite3.Connection:
        """Returns a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.cache_file), timeout=30.0)
        return self._local.conn

    def _init_db(self):
        """Initializes the cache SQLite table."""
        conn = sqlite3.connect(str(self.cache_file), timeout=30.0)
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tag_cache (
                    cache_key TEXT PRIMARY KEY,
                    data_json TEXT,
                    created_at REAL
                )
            """)
        conn.close()

    def _make_cache_key(self, method: str, **params) -> str:
        """Constructs a deterministic cache key."""
        sorted_items = sorted((k, str(v).lower().strip()) for k, v in params.items() if v)
        param_str = urllib.parse.urlencode(sorted_items)
        return f"{method}:{param_str}"

    def _get_from_cache(self, key: str) -> Optional[List[Dict[str, Any]]]:
        if not self.use_cache:
            return None
        try:
            conn = self._get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT data_json FROM tag_cache WHERE cache_key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def _save_to_cache(self, key: str, tags: List[Dict[str, Any]]):
        if not self.use_cache:
            return
        try:
            conn = self._get_db()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO tag_cache (cache_key, data_json, created_at) VALUES (?, ?, ?)",
                    (key, json.dumps(tags), time.time())
                )
        except Exception:
            pass

    def clear_cache(self):
        """Empties the SQLite cache."""
        try:
            conn = sqlite3.connect(str(self.cache_file))
            with conn:
                conn.execute("DELETE FROM tag_cache")
            conn.close()
        except Exception as e:
            console.print(f"[yellow]Warning: Could not clear cache: {e}[/yellow]")

    def _api_request(self, method: str, **params) -> List[Dict[str, Any]]:
        """Executes a Last.fm API call with caching and exponential backoff retry."""
        cache_key = self._make_cache_key(method, **params)
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        req_params = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            "autocorrect": 1
        }
        req_params.update(params)

        tags: List[Dict[str, Any]] = []
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.get(LASTFM_API_URL, params=req_params, timeout=12.0)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_tags = data.get("toptags", {}).get("tag", [])
                    if isinstance(raw_tags, dict):
                        raw_tags = [raw_tags]
                    
                    for t in raw_tags:
                        if isinstance(t, dict) and "name" in t:
                            tags.append({
                                "name": str(t.get("name", "")).strip(),
                                "count": t.get("count", 0)
                            })
                    break
                elif resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.0 * (attempt + 1))
                else:
                    # 404 or missing entry
                    break
            except Exception:
                time.sleep(0.8 * (attempt + 1))

        self._save_to_cache(cache_key, tags)
        return tags

    def get_artist_tags(self, artist: str) -> List[Dict[str, Any]]:
        """Fetch top tags for an artist."""
        if not artist or not artist.strip():
            return []
        return self._api_request("artist.gettoptags", artist=artist.strip())

    def get_album_tags(self, artist: str, album: str) -> List[Dict[str, Any]]:
        """Fetch top tags for an album."""
        if not artist or not album:
            return []
        return self._api_request("album.gettoptags", artist=artist.strip(), album=album.strip())

    def get_track_tags(self, artist: str, track: str) -> List[Dict[str, Any]]:
        """Fetch top tags for a track."""
        if not artist or not track:
            return []
        return self._api_request("track.gettoptags", artist=artist.strip(), track=track.strip())


# ---------------------------------------------------------------------------
# Audio Metadata Extraction & Writing via Mutagen
# ---------------------------------------------------------------------------

class AudioMetadata:
    """Represents normalized metadata of an audio file."""
    
    def __init__(
        self,
        path: Path,
        artist: str = "",
        album_artist: str = "",
        album: str = "",
        title: str = "",
        track_number: str = "",
        genres: Optional[List[str]] = None,
        file_type: str = ""
    ):
        self.path = path
        self.artist = artist
        self.album_artist = album_artist
        self.album = album
        self.title = title
        self.track_number = track_number
        self.genres = genres or []
        self.file_type = file_type

    @property
    def effective_artist(self) -> str:
        return self.artist or self.album_artist

    def __repr__(self) -> str:
        return f"<AudioMetadata {self.effective_artist} - {self.title} [{self.album}] ({self.genres})>"


class AudioMetadataHandler:
    """Reads and writes audio tags across various audio formats using Mutagen."""

    @staticmethod
    def _clean_str(val: Any) -> str:
        """Convert tag value to clean stripped string."""
        if val is None:
            return ""
        if isinstance(val, (list, tuple)):
            if not val:
                return ""
            val = val[0]
        return str(val).strip()

    @classmethod
    def _extract_genres_from_tag(cls, tag_val: Any) -> List[str]:
        """Splits multi-genre values separated by ;, /, or commas."""
        if not tag_val:
            return []
        raw_list = []
        if isinstance(tag_val, (list, tuple)):
            raw_list = [str(x) for x in tag_val]
        else:
            raw_list = [str(tag_val)]

        genres = []
        for item in raw_list:
            # Split on standard delimiters
            parts = re.split(r"[;/]|\s{2,}", item)
            for p in parts:
                p_clean = p.strip()
                if p_clean and p_clean not in genres:
                    genres.append(p_clean)
        return genres

    @classmethod
    def read_metadata(cls, file_path: Path) -> AudioMetadata:
        """Reads audio tags from a file, with directory-structure fallback."""
        meta = AudioMetadata(path=file_path, file_type=file_path.suffix.lower())
        
        try:
            audio = mutagen.File(file_path)
            if audio is not None:
                # 1. MP3 / ID3
                if isinstance(audio, mutagen.mp3.MP3) or (hasattr(audio, "tags") and isinstance(audio.tags, ID3)):
                    tags = audio.tags
                    if tags:
                        meta.title = cls._clean_str(tags.get("TIT2"))
                        meta.artist = cls._clean_str(tags.get("TPE1"))
                        meta.album_artist = cls._clean_str(tags.get("TPE2"))
                        meta.album = cls._clean_str(tags.get("TALB"))
                        meta.track_number = cls._clean_str(tags.get("TRCK"))
                        tcon = tags.get("TCON")
                        if tcon:
                            meta.genres = cls._extract_genres_from_tag(tcon.text if hasattr(tcon, "text") else tcon)

                # 2. FLAC / OGG / Vorbis
                elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
                    meta.title = cls._clean_str(audio.get("title") or audio.get("TITLE"))
                    meta.artist = cls._clean_str(audio.get("artist") or audio.get("ARTIST"))
                    meta.album_artist = cls._clean_str(audio.get("albumartist") or audio.get("ALBUMARTIST") or audio.get("album_artist"))
                    meta.album = cls._clean_str(audio.get("album") or audio.get("ALBUM"))
                    meta.track_number = cls._clean_str(audio.get("tracknumber") or audio.get("TRACKNUMBER"))
                    genre_val = audio.get("genre") or audio.get("GENRE")
                    if genre_val:
                        meta.genres = cls._extract_genres_from_tag(genre_val)

                # 3. MP4 / M4A / AAC
                elif isinstance(audio, MP4):
                    meta.title = cls._clean_str(audio.get("\xa9nam"))
                    meta.artist = cls._clean_str(audio.get("\xa9ART"))
                    meta.album_artist = cls._clean_str(audio.get("aART"))
                    meta.album = cls._clean_str(audio.get("\xa9alb"))
                    meta.track_number = cls._clean_str(audio.get("trkn"))
                    meta.genres = cls._extract_genres_from_tag(audio.get("\xa9gen"))

                # 4. WAVE / AIFF
                elif isinstance(audio, (WAVE, mutagen.aiff.AIFF)):
                    if hasattr(audio, "tags") and audio.tags:
                        meta.title = cls._clean_str(audio.tags.get("TIT2"))
                        meta.artist = cls._clean_str(audio.tags.get("TPE1"))
                        meta.album = cls._clean_str(audio.tags.get("TALB"))
                        tcon = audio.tags.get("TCON")
                        if tcon:
                            meta.genres = cls._extract_genres_from_tag(tcon.text if hasattr(tcon, "text") else tcon)

                # 5. Generic / Fallback Mutagen Dict
                else:
                    if hasattr(audio, "get"):
                        meta.title = cls._clean_str(audio.get("title") or audio.get("TIT2"))
                        meta.artist = cls._clean_str(audio.get("artist") or audio.get("TPE1"))
                        meta.album = cls._clean_str(audio.get("album") or audio.get("TALB"))
                        genre_val = audio.get("genre") or audio.get("GENRE") or audio.get("TCON")
                        if genre_val:
                            meta.genres = cls._extract_genres_from_tag(genre_val)
        except Exception:
            pass

        # Fallback: Extract from path structure if metadata is missing
        cls._apply_path_fallback(meta)
        return meta

    @classmethod
    def _apply_path_fallback(cls, meta: AudioMetadata):
        """Infers missing artist, album, or title from directory hierarchy and file names."""
        file_path = meta.path
        parts = file_path.parts
        
        # 1. Clean track title
        if not meta.title:
            stem = file_path.stem
            # Strip trailing site/track ids like [377219936] or (2128296399)
            cleaned_stem = re.sub(r"\s*[\[\(]\d{6,}[\]\)]\s*$", "", stem).strip()
            # Strip leading track numbers e.g. "01 - Title", "01. Title", "01 Title"
            cleaned_title = re.sub(r"^\d+[\s\.\-_]+", "", cleaned_stem).strip()
            
            # If filename looks like "Artist - Title", split it
            if " - " in cleaned_title:
                parts_split = cleaned_title.split(" - ", 1)
                if not meta.artist:
                    meta.artist = parts_split[0].strip()
                meta.title = parts_split[1].strip()
            else:
                meta.title = cleaned_title or cleaned_stem or stem

        # 2. Check path relative to known music library roots or workspace
        lib_idx = -1
        for i, marker in enumerate(parts):
            if marker.lower() in {"library", "music", "downloads", "audio"}:
                lib_idx = i
                
        if lib_idx != -1 and len(parts) > lib_idx + 1:
            rel_parts = parts[lib_idx + 1:]
            # If 2 parts: Artist / File.mp3
            if len(rel_parts) == 2:
                if not meta.artist:
                    meta.artist = rel_parts[0].strip()
            # If 3+ parts: Artist / Album / (Subdir) / File.mp3
            elif len(rel_parts) >= 3:
                if not meta.artist:
                    meta.artist = rel_parts[0].strip()
                if not meta.album:
                    meta.album = re.sub(r"^\[.*?\]\s*", "", rel_parts[1]).strip()
        else:
            # Fallback if outside recognized library root:
            parent = file_path.parent
            grandparent = parent.parent
            if not meta.album and parent.name:
                meta.album = re.sub(r"^\[.*?\]\s*", "", parent.name).strip()
            if not meta.artist:
                if grandparent.name:
                    meta.artist = grandparent.name.strip()
                elif parent.name:
                    meta.artist = parent.name.strip()

    @classmethod
    def write_genres(
        cls,
        file_path: Path,
        genres: List[str],
        mode: str = "overwrite",
        separator: str = "; ",
        multi_value: bool = False
    ) -> bool:
        """
        Writes genre tags to the audio file.
        mode: 'overwrite', 'skip_existing', or 'append'
        """
        if not genres:
            return False

        try:
            audio = mutagen.File(file_path)
            if audio is None:
                # Try creating MP3 ID3 header if none exists
                if file_path.suffix.lower() == ".mp3":
                    try:
                        audio = mutagen.mp3.MP3(file_path)
                        audio.add_tags()
                    except Exception:
                        return False
                else:
                    return False

            # Determine final genre list based on mode
            current_meta = cls.read_metadata(file_path)
            final_genres = []
            
            if mode == "skip_existing" and current_meta.genres:
                return False
            elif mode == "append":
                final_genres = list(current_meta.genres)
                for g in genres:
                    if g not in final_genres:
                        final_genres.append(g)
            else:  # overwrite
                final_genres = list(genres)

            if not final_genres:
                return False

            genre_string = separator.join(final_genres)

            # 1. MP3 / ID3
            if isinstance(audio, mutagen.mp3.MP3) or (hasattr(audio, "tags") and isinstance(audio.tags, ID3)):
                if audio.tags is None:
                    try:
                        audio.add_tags()
                    except Exception:
                        pass
                if audio.tags is not None:
                    # Write ID3v2 TCON frame
                    text_val = final_genres if multi_value else [genre_string]
                    audio.tags["TCON"] = TCON(encoding=3, text=text_val)
                    audio.tags.save(file_path)
                    return True

            # 2. FLAC
            elif isinstance(audio, FLAC):
                if multi_value:
                    audio["GENRE"] = final_genres
                else:
                    audio["GENRE"] = [genre_string]
                audio.save()
                return True

            # 3. M4A / MP4
            elif isinstance(audio, MP4):
                if multi_value:
                    audio["\xa9gen"] = final_genres
                else:
                    audio["\xa9gen"] = [genre_string]
                audio.save()
                return True

            # 4. OGG / Opus
            elif isinstance(audio, (OggVorbis, OggOpus)):
                if multi_value:
                    audio["GENRE"] = final_genres
                else:
                    audio["GENRE"] = [genre_string]
                audio.save()
                return True

            # 5. WAVE / AIFF
            elif isinstance(audio, (WAVE, mutagen.aiff.AIFF)):
                if audio.tags is None:
                    try:
                        audio.add_tags()
                    except Exception:
                        pass
                if audio.tags is not None:
                    text_val = final_genres if multi_value else [genre_string]
                    audio.tags["TCON"] = TCON(encoding=3, text=text_val)
                    audio.save()
                    return True

            # 6. Generic fallback
            else:
                if hasattr(audio, "tags") and audio.tags is not None:
                    audio.tags["GENRE"] = genre_string
                    audio.save()
                    return True
        except Exception as e:
            console.print(f"[red]Error writing tags to {file_path.name}: {e}[/red]")
            return False

        return False


# ---------------------------------------------------------------------------
# Tagging Engine & Cascading Strategy
# ---------------------------------------------------------------------------

class GenreTaggerEngine:
    """Orchestrates tag fetching, cascading, filtering, and writing for a music collection."""

    def __init__(
        self,
        client: LastFMClient,
        normalizer: GenreNormalizer,
        strategy: str = "cascade",
        limit: int = 3,
        mode: str = "overwrite",
        separator: str = "; ",
        multi_value: bool = False,
        dry_run: bool = False,
        threads: int = 8
    ):
        self.client = client
        self.normalizer = normalizer
        self.strategy = strategy  # cascade, blend, artist, album, track
        self.limit = limit
        self.mode = mode          # overwrite, skip_existing, append
        self.separator = separator
        self.multi_value = multi_value
        self.dry_run = dry_run
        self.threads = max(1, threads)

    def resolve_genres(self, meta: AudioMetadata) -> List[str]:
        """Fetches and cascades/blends Last.fm tags for an audio file."""
        artist = meta.effective_artist
        album = meta.album
        title = meta.title

        if not artist:
            return []

        # Strategy 1: Artist-Only
        if self.strategy == "artist":
            raw = self.client.get_artist_tags(artist)
            return self.normalizer.filter_and_format(raw, artist=artist, limit=self.limit)

        # Strategy 2: Album-Only (fallback to artist)
        if self.strategy == "album":
            raw_album = self.client.get_album_tags(artist, album) if album else []
            genres = self.normalizer.filter_and_format(raw_album, artist=artist, album=album, limit=self.limit)
            if not genres:
                raw_artist = self.client.get_artist_tags(artist)
                genres = self.normalizer.filter_and_format(raw_artist, artist=artist, limit=self.limit)
            return genres

        # Strategy 3: Track-Only
        if self.strategy == "track":
            raw_track = self.client.get_track_tags(artist, title) if title else []
            return self.normalizer.filter_and_format(raw_track, artist=artist, track=title, limit=self.limit)

        # Strategy 4: Blend (Weighted combination of track, album, and artist tags)
        if self.strategy == "blend":
            raw_track = self.client.get_track_tags(artist, title) if title else []
            raw_album = self.client.get_album_tags(artist, album) if album else []
            raw_artist = self.client.get_artist_tags(artist)

            weighted_scores: Dict[str, float] = {}

            # Weight multipliers
            for t in raw_track:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 1.5)

            for t in raw_album:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 1.0)

            for t in raw_artist:
                name = self.normalizer.clean_tag_name(t.get("name", ""))
                cnt = float(t.get("count", 100))
                weighted_scores[name] = weighted_scores.get(name, 0.0) + (cnt * 0.7)

            sorted_tags = sorted(
                [{"name": k, "count": int(v)} for k, v in weighted_scores.items()],
                key=lambda x: x["count"],
                reverse=True
            )
            return self.normalizer.filter_and_format(sorted_tags, artist=artist, album=album, track=title, limit=self.limit)

        # Strategy 5: Cascade (Default: Track -> Album -> Artist)
        # 1. Try track tags
        raw_track = self.client.get_track_tags(artist, title) if title else []
        track_genres = self.normalizer.filter_and_format(raw_track, artist=artist, track=title, limit=self.limit)
        
        if len(track_genres) >= self.limit:
            return track_genres

        # 2. Supplement or fallback to album tags
        raw_album = self.client.get_album_tags(artist, album) if album else []
        album_genres = self.normalizer.filter_and_format(raw_album, artist=artist, album=album, limit=self.limit)

        combined = list(track_genres)
        for g in album_genres:
            if g.lower() not in [x.lower() for x in combined]:
                combined.append(g)
            if len(combined) >= self.limit:
                return combined

        # 3. Supplement or fallback to artist tags
        raw_artist = self.client.get_artist_tags(artist)
        artist_genres = self.normalizer.filter_and_format(raw_artist, artist=artist, limit=self.limit)

        for g in artist_genres:
            if g.lower() not in [x.lower() for x in combined]:
                combined.append(g)
            if len(combined) >= self.limit:
                break

        return combined

    def process_file(self, file_path: Path) -> Dict[str, Any]:
        """Reads, resolves genres, and applies tags for a single audio file."""
        meta = AudioMetadataHandler.read_metadata(file_path)
        existing_genres = list(meta.genres)
        
        # Check skip_existing
        if self.mode == "skip_existing" and existing_genres:
            return {
                "path": file_path,
                "artist": meta.effective_artist,
                "album": meta.album,
                "title": meta.title,
                "old_genres": existing_genres,
                "new_genres": existing_genres,
                "status": "skipped_existing",
                "changed": False
            }

        resolved_genres = self.resolve_genres(meta)

        if not resolved_genres:
            return {
                "path": file_path,
                "artist": meta.effective_artist,
                "album": meta.album,
                "title": meta.title,
                "old_genres": existing_genres,
                "new_genres": existing_genres,
                "status": "no_tags_found",
                "changed": False
            }

        # Calculate final genres
        if self.mode == "append":
            final_genres = list(existing_genres)
            for g in resolved_genres:
                if g.lower() not in [x.lower() for x in final_genres]:
                    final_genres.append(g)
        else:
            final_genres = resolved_genres

        changed = (existing_genres != final_genres)
        success = True

        if changed and not self.dry_run:
            success = AudioMetadataHandler.write_genres(
                file_path=file_path,
                genres=final_genres,
                mode=self.mode,
                separator=self.separator,
                multi_value=self.multi_value
            )

        status = "dry_run" if self.dry_run else ("updated" if (changed and success) else "unchanged")
        if changed and not success and not self.dry_run:
            status = "write_failed"

        return {
            "path": file_path,
            "artist": meta.effective_artist,
            "album": meta.album,
            "title": meta.title,
            "old_genres": existing_genres,
            "new_genres": final_genres,
            "status": status,
            "changed": changed
        }

    def process_target(self, target_path: Path) -> List[Dict[str, Any]]:
        """Processes a single file, album directory, artist directory, or entire library."""
        target_path = target_path.resolve()
        
        if not target_path.exists():
            console.print(f"[red]Error: Target path does not exist: {target_path}[/red]")
            return []

        # Collect audio files
        audio_files: List[Path] = []
        if target_path.is_file():
            if target_path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                audio_files.append(target_path)
            else:
                console.print(f"[yellow]Warning: '{target_path.name}' is not a recognized audio file format.[/yellow]")
                return []
        else:
            for root, _, files in os.walk(target_path):
                for f in files:
                    ext = Path(f).suffix.lower()
                    if ext in SUPPORTED_AUDIO_EXTENSIONS:
                        audio_files.append(Path(root) / f)

        if not audio_files:
            console.print(f"[yellow]No audio files found in {target_path}[/yellow]")
            return []

        console.print(f"[cyan]Found {len(audio_files)} audio file(s) to process in:[/cyan] [bold]{target_path}[/bold]")
        if self.dry_run:
            console.print("[bold yellow]Running in DRY-RUN mode. No files will be modified.[/bold yellow]\n")

        results: List[Dict[str, Any]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            task = progress.add_task("[green]Tagging with Last.fm genres...", total=len(audio_files))

            if self.threads > 1 and len(audio_files) > 1:
                with ThreadPoolExecutor(max_workers=self.threads) as executor:
                    future_to_file = {executor.submit(self.process_file, f): f for f in audio_files}
                    for future in as_completed(future_to_file):
                        try:
                            res = future.result()
                            results.append(res)
                        except Exception as e:
                            f = future_to_file[future]
                            console.print(f"[red]Error processing {f.name}: {e}[/red]")
                        finally:
                            progress.advance(task)
            else:
                for f in audio_files:
                    try:
                        res = self.process_file(f)
                        results.append(res)
                    except Exception as e:
                        console.print(f"[red]Error processing {f.name}: {e}[/red]")
                    finally:
                        progress.advance(task)

        return results


# ---------------------------------------------------------------------------
# CLI Reporting & Statistics
# ---------------------------------------------------------------------------

def display_results_table(results: List[Dict[str, Any]], max_rows: int = 40):
    """Renders a clean Rich Table of tagging changes."""
    changed_results = [r for r in results if r["changed"] or r["status"] in ("updated", "dry_run")]
    
    if not changed_results:
        console.print("\n[dim]No changes were required (all files already have tags or match new tags).[/dim]")
        return

    table = Table(
        title=f"\n[bold]Tagging Results (Showing {min(len(changed_results), max_rows)} of {len(changed_results)} changed files)[/bold]",
        box=box.ROUNDED,
        header_style="bold cyan"
    )
    table.add_column("File / Title", style="white", no_wrap=False, max_width=35)
    table.add_column("Artist", style="yellow", max_width=20)
    table.add_column("Album", style="magenta", max_width=25)
    table.add_column("Before", style="dim", max_width=25)
    table.add_column("After (Last.fm)", style="bold green", max_width=30)
    table.add_column("Status", style="bold", justify="center")

    for res in changed_results[:max_rows]:
        title = res["title"] or res["path"].name
        artist = res["artist"] or "[dim]Unknown[/dim]"
        album = res["album"] or "[dim]None[/dim]"
        old_g = ", ".join(res["old_genres"]) if res["old_genres"] else "[dim]None[/dim]"
        new_g = ", ".join(res["new_genres"]) if res["new_genres"] else "[dim]None[/dim]"
        
        status = res["status"]
        if status == "updated":
            status_text = "[green]✓ Updated[/green]"
        elif status == "dry_run":
            status_text = "[yellow]✦ Preview[/yellow]"
        elif status == "write_failed":
            status_text = "[red]✗ Failed[/red]"
        elif status == "no_tags_found":
            status_text = "[dim]- No Tags[/dim]"
        else:
            status_text = f"[blue]{status}[/blue]"

        table.add_row(title, artist, album, old_g, new_g, status_text)

    console.print(table)


def display_summary_panel(results: List[Dict[str, Any]], dry_run: bool):
    """Renders high-level statistics and top applied genres."""
    total = len(results)
    updated = sum(1 for r in results if r["status"] in ("updated", "dry_run") and r["changed"])
    unchanged = sum(1 for r in results if r["status"] == "unchanged")
    skipped = sum(1 for r in results if r["status"] == "skipped_existing")
    no_tags = sum(1 for r in results if r["status"] == "no_tags_found")
    failed = sum(1 for r in results if r["status"] == "write_failed")

    genre_counts: Dict[str, int] = {}
    for r in results:
        if r["status"] in ("updated", "dry_run"):
            for g in r["new_genres"]:
                genre_counts[g] = genre_counts.get(g, 0) + 1

    top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    top_genres_str = ", ".join([f"[green]{g}[/green] ({c})" for g, c in top_genres]) or "[dim]None[/dim]"

    summary_text = (
        f"[bold]Total Files Scanned:[/bold] {total}\n"
        f"[bold]{'Previewed Changes' if dry_run else 'Successfully Tagged'}:[/bold] [green]{updated}[/green]\n"
        f"[bold]Unchanged (Matching):[/bold] {unchanged}\n"
        f"[bold]Skipped (Already Tagged):[/bold] [yellow]{skipped}[/yellow]\n"
        f"[bold]No Last.fm Tags Found:[/bold] [dim]{no_tags}[/dim]\n"
        f"[bold]Write Failures:[/bold] [red]{failed}[/red]\n\n"
        f"[bold]Top Genres Applied:[/bold] {top_genres_str}"
    )

    panel_title = "Tagging Summary (DRY RUN - No Files Modified)" if dry_run else "Tagging Summary"
    console.print(Panel(summary_text, title=f"[bold cyan]{panel_title}[/bold cyan]", border_style="cyan", box=box.ROUNDED))


# ---------------------------------------------------------------------------
# Direct Query / Testing Mode
# ---------------------------------------------------------------------------

def run_direct_query(
    client: LastFMClient,
    normalizer: GenreNormalizer,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    track: Optional[str] = None,
    limit: int = 5
):
    """Directly queries Last.fm API and prints formatted genre tags without touching files."""
    console.print(Panel(
        f"[bold]Direct Last.fm Query[/bold]\n"
        f"[cyan]Artist:[/cyan] {artist or '[dim]N/A[/dim]'}\n"
        f"[cyan]Album:[/cyan] {album or '[dim]N/A[/dim]'}\n"
        f"[cyan]Track:[/cyan] {track or '[dim]N/A[/dim]'}",
        border_style="magenta",
        box=box.ROUNDED
    ))

    # 1. Track tags
    if artist and track:
        raw_track = client.get_track_tags(artist, track)
        track_genres = normalizer.filter_and_format(raw_track, artist=artist, track=track, limit=limit)
        console.print(f"[bold green]Track Tags ({track}):[/bold green] {', '.join(track_genres) if track_genres else '[dim]No tags found[/dim]'}")

    # 2. Album tags
    if artist and album:
        raw_album = client.get_album_tags(artist, album)
        album_genres = normalizer.filter_and_format(raw_album, artist=artist, album=album, limit=limit)
        console.print(f"[bold green]Album Tags ({album}):[/bold green] {', '.join(album_genres) if album_genres else '[dim]No tags found[/dim]'}")

    # 3. Artist tags
    if artist:
        raw_artist = client.get_artist_tags(artist)
        artist_genres = normalizer.filter_and_format(raw_artist, artist=artist, limit=limit)
        console.print(f"[bold green]Artist Tags ({artist}):[/bold green] {', '.join(artist_genres) if artist_genres else '[dim]No tags found[/dim]'}")


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entrypoint
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Apply intelligent genre tags to artists, albums, and tracks using Last.fm community metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview genre tags for an entire artist or album folder (Dry-Run):
  python3 lastfm_genre_tagger.py "/mnt/music/Library/goreshit" --dry-run

  # Apply genre tags to an artist folder (Overwrite existing genres):
  python3 lastfm_genre_tagger.py "/mnt/music/Library/Nizikawa"

  # Only tag files that have missing/empty genre tags:
  python3 lastfm_genre_tagger.py "/mnt/music/Library" --skip-existing

  # Append Last.fm genres to existing genre tags:
  python3 lastfm_genre_tagger.py "/mnt/music/Library/Wan Bushi" --append

  # Tag with blended weights and save up to 4 genres separated by '/':
  python3 lastfm_genre_tagger.py "/mnt/music/Library" --strategy blend --limit 4 --separator " / "

  # Test Last.fm tags directly for an artist or track without modifying files:
  python3 lastfm_genre_tagger.py --artist "Audiotist" --album "Music Obsession"
        """
    )

    parser.add_argument("path", nargs="?", type=str, default=None,
                        help="Path to an audio file, album directory, artist directory, or music library.")
    
    # Direct Query Options
    query_group = parser.add_argument_group("Direct Query Options (No Files Required)")
    query_group.add_argument("--artist", "-a", type=str, default=None, help="Artist name to query on Last.fm")
    query_group.add_argument("--album", "-b", type=str, default=None, help="Album title to query on Last.fm")
    query_group.add_argument("--track", "-k", type=str, default=None, help="Track title to query on Last.fm")

    # Tagging Strategy & Rules
    tag_group = parser.add_argument_group("Tagging Options")
    tag_group.add_argument("--strategy", "-s", choices=["cascade", "blend", "artist", "album", "track"],
                           default="cascade",
                           help="Tag resolution hierarchy: 'cascade' (Track->Album->Artist, default), 'blend' (weighted merge), 'artist', 'album', or 'track'.")
    tag_group.add_argument("--limit", "-n", type=int, default=3,
                           help="Maximum number of genre tags to write (default: 3).")
    tag_group.add_argument("--min-count", "-m", type=int, default=5,
                           help="Minimum Last.fm tag count/score to accept (default: 5).")
    tag_group.add_argument("--separator", type=str, default="; ",
                           help="Separator delimiter for joined genres (default: '; ').")
    tag_group.add_argument("--multi-value", action="store_true",
                           help="Write multi-value genre tags instead of a joined string for FLAC/Vorbis/ID3.")

    # Write Modes
    mode_group = parser.add_argument_group("Write Modes")
    mode_group.add_argument("--dry-run", "-d", action="store_true",
                            help="Preview proposed genre tag changes without modifying any files.")
    mode_group.add_argument("--skip-existing", action="store_true",
                            help="Only apply genre tags to files that currently have no genre metadata.")
    mode_group.add_argument("--append", action="store_true",
                            help="Preserve existing genres and append newly discovered Last.fm genres.")
    mode_group.add_argument("--overwrite", action="store_true",
                            help="Overwrite existing genre tags with fresh Last.fm genres (default).")

    # Filtering & Customization
    filter_group = parser.add_argument_group("Genre Filtering & Normalization")
    filter_group.add_argument("--allow-nationality", action="store_true",
                              help="Allow nationality/country tags (e.g. Japanese, British, Belgian).")
    filter_group.add_argument("--allow-vocals", action="store_true",
                              help="Allow vocal classifiers (e.g. Female Vocalists, Male Vocalists).")
    filter_group.add_argument("--blacklist-tag", action="append", default=[],
                              help="Add a custom tag name to the blacklist.")
    filter_group.add_argument("--whitelist-tag", action="append", default=[],
                              help="Force whitelist a specific tag name.")

    # Performance & Cache
    perf_group = parser.add_argument_group("Performance & Cache")
    perf_group.add_argument("--threads", "-t", type=int, default=8,
                            help="Number of concurrent worker threads for file processing (default: 8).")
    perf_group.add_argument("--api-key", type=str, default=None,
                            help="Custom Last.fm API Key (or set LASTFM_API_KEY environment variable).")
    perf_group.add_argument("--no-cache", action="store_true",
                            help="Bypass the local SQLite query cache.")
    perf_group.add_argument("--clear-cache", action="store_true",
                            help="Clear the local Last.fm SQLite query cache and exit.")

    return parser.parse_args()


def main():
    args = parse_args()

    # Initialize Last.fm Client
    client = LastFMClient(
        api_key=args.api_key,
        use_cache=not args.no_cache
    )

    if args.clear_cache:
        client.clear_cache()
        console.print("[green]✓ Last.fm query cache cleared successfully.[/green]")
        sys.exit(0)

    # Initialize Genre Normalizer
    normalizer = GenreNormalizer(
        min_count=args.min_count,
        custom_blacklist=set(args.blacklist_tag),
        custom_whitelist=set(args.whitelist_tag),
        allow_nationality=args.allow_nationality,
        allow_vocals=args.allow_vocals
    )

    # Mode resolution
    mode = "overwrite"
    if args.skip_existing:
        mode = "skip_existing"
    elif args.append:
        mode = "append"

    # Direct Query Mode
    if args.path is None:
        if args.artist or args.album or args.track:
            run_direct_query(
                client=client,
                normalizer=normalizer,
                artist=args.artist,
                album=args.album,
                track=args.track,
                limit=args.limit
            )
            sys.exit(0)
        else:
            console.print("[yellow]Please specify a file or folder path, or use --artist / --album / --track to query Last.fm.[/yellow]")
            console.print("Run [bold]python3 lastfm_genre_tagger.py --help[/bold] for full options.")
            sys.exit(1)

    target_path = Path(args.path)
    engine = GenreTaggerEngine(
        client=client,
        normalizer=normalizer,
        strategy=args.strategy,
        limit=args.limit,
        mode=mode,
        separator=args.separator,
        multi_value=args.multi_value,
        dry_run=args.dry_run,
        threads=args.threads
    )

    results = engine.process_target(target_path)
    if results:
        display_results_table(results)
        display_summary_panel(results, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation aborted by user.[/yellow]")
        sys.exit(130)
