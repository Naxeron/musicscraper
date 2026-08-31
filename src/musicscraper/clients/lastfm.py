"""
Last.fm API client, rate limiting, and intelligent genre normalization.
"""

import re
import time
import json
import urllib.parse
from pathlib import Path
from typing import Dict, List, Set, Optional, Any

import requests

from musicscraper.config import Config
from musicscraper.core.report import console
from musicscraper.clients.http import create_resilient_session, RateLimiter

YEAR_DECADE_REGEX = re.compile(r"^(19|20)\d{2}s?$|^\d{2}s$|^\d+$")

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
    
    # Vocals / Misc non-genre classifiers
    "female vocalist", "female vocalists", "male vocalist", "male vocalists",
    "female vocals", "male vocals", "female singer", "male singer",
    "vocal", "vocals", "singer-songwriter", "arranger", "composer", "producer",
    
    # Internet / Meme / Emoticons / Gibberish
    ":3", "^^", "^_^", "xd", ":d", "<3", "meme", "memes", "shitpost", "shitposting",
    "lol", "random", "misc", "wtf", "all", "other", "unknown", "none", "n/a",
    "tracks", "music", "songs", "sound", "sounds", "tunes", "good music"
}

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
}


class GenreNormalizer:
    """Cleans, filters, and standardizes Last.fm tags into canonical genres."""

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
        t = re.sub(r"^[\s\-_'\"`\[\]\(\)]+|[\s\-_'\"`\[\]\(\)]+$", "", t)
        t = re.sub(r"\s+", " ", t)
        return t

    def is_valid_genre(self, tag: str, artist: str = "", album: str = "", track: str = "") -> bool:
        """Check whether a tag is a valid genre or noisy metadata."""
        if not tag or len(tag) < 2 or len(tag) > 40:
            return False

        t_clean = self.clean_tag_name(tag)

        if self.whitelist and t_clean in self.whitelist:
            return True

        if t_clean in self.blacklist:
            return False

        if YEAR_DECADE_REGEX.match(t_clean):
            return False

        if not self.allow_nationality and t_clean in self.nationality_tags:
            return False

        if not self.allow_vocals and ("vocal" in t_clean or "female" in t_clean or "male" in t_clean):
            if t_clean not in {"vocaloid", "vocal trance", "vocal house"}:
                return False

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

        if not re.search(r"[a-zA-Z0-9\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf]", t_clean):
            return False

        return True

    def canonicalize(self, tag: str) -> str:
        """Standardize genre capitalization and alias mapping."""
        t_clean = self.clean_tag_name(tag)
        if t_clean in CANONICAL_GENRES:
            return CANONICAL_GENRES[t_clean]

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
        """Filters raw Last.fm tag objects and returns canonicalized genre strings."""
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


class LastFMClient:
    """Handles requests to Last.fm API with local caching, rate limiting, and retries."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        use_cache: bool = True
    ):
        self.api_key = api_key or Config.LASTFM_API_KEY
        self.use_cache = use_cache
        self.rate_limiter = RateLimiter(min_interval=0.10)
        self.session = create_resilient_session(user_agent="MusicScraperGenreTagger/1.0 (https://github.com/naxeron/musicscraper)")

        from musicscraper.core.cache import UnifiedCacheManager
        self.cache = UnifiedCacheManager()

    def _make_cache_key(self, method: str, **params) -> str:
        sorted_items = sorted((k, str(v).lower().strip()) for k, v in params.items() if v)
        param_str = urllib.parse.urlencode(sorted_items)
        return f"{method}:{param_str}"

    def _api_request(self, method: str, **params) -> List[Dict[str, Any]]:
        """Executes a Last.fm API call with caching and retries."""
        cache_key = self._make_cache_key(method, **params)
        if self.use_cache:
            cached = self.cache.get_api_cache("lastfm", cache_key)
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
        success = False
        max_retries = 4

        for attempt in range(max_retries):
            self.rate_limiter.wait()
            try:
                resp = self.session.get(Config.LASTFM_API_URL, params=req_params, timeout=12.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" in data and "toptags" not in data:
                        success = True
                        break

                    raw_tags = data.get("toptags", {}).get("tag", [])
                    if isinstance(raw_tags, dict):
                        raw_tags = [raw_tags]

                    for t in raw_tags:
                        if isinstance(t, dict) and "name" in t:
                            tags.append({
                                "name": str(t.get("name", "")).strip(),
                                "count": t.get("count", 0)
                            })
                    success = True
                    break
                elif resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                elif resp.status_code == 404:
                    success = True
                    break
                else:
                    break
            except Exception:
                time.sleep(1.0 * (attempt + 1))

        if success and self.use_cache:
            self.cache.store_api_cache("lastfm", cache_key, tags, ttl_seconds=86400 * 30)

        return tags

    def get_artist_tags(self, artist: str) -> List[Dict[str, Any]]:
        """Fetch top tags for an artist, with collaboration fallback."""
        if not artist or not artist.strip():
            return []

        art_clean = artist.strip()
        tags = self._api_request("artist.gettoptags", artist=art_clean)
        if tags:
            return tags

        # Fallback for collaborations
        primary = re.split(r"\s+(?:feat\.?|ft\.?|vs\.?|x|&|with|\+|/|_)\s+", art_clean, flags=re.IGNORECASE)[0].strip()
        if primary and primary.lower() != art_clean.lower():
            primary_tags = self._api_request("artist.gettoptags", artist=primary)
            if primary_tags:
                return primary_tags

        return []

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
