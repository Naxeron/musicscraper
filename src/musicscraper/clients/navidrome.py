"""
Navidrome / Subsonic REST API scanner for remote music libraries.
"""

import os
import json
import random
import string
import hashlib
import urllib.parse
import urllib.request
from typing import Dict, List, Set, Optional, Any

from rich.progress import Progress

from musicscraper.config import Config
from musicscraper.core.constants import GENERIC_OR_COMMON_WORDS
from musicscraper.core.text import normalize_text
from musicscraper.clients.musicbrainz import ArtistCatalog


class NavidromeScanner:
    """Queries Navidrome/Subsonic API for artist discography and indexed audio tracks."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        catalog: Optional[ArtistCatalog] = None,
        timeout: int = 15
    ):
        self.base_url = (base_url or Config.NAVIDROME_URL).rstrip("/")
        self.username = username or Config.NAVIDROME_USER
        self.password = password or Config.NAVIDROME_TOKEN
        self.catalog = catalog
        self.timeout = timeout

    def test_connection(self) -> bool:
        """Pings the Navidrome/Subsonic server to verify connectivity and credentials."""
        if not self.base_url or not self.username:
            return False
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
        """Queries Navidrome Subsonic API for tracks matching artist aliases and releases."""
        if not self.catalog or not self.base_url:
            return []

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

        # 2. Check for exact MBID match
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

        # 3. Search primary releases
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
                "mb_track_ids": set(),
                "mb_rec_ids": mb_rec_ids,
                "mb_artist_ids": set(),
                "mb_release_ids": set(),
                "source": "navidrome"
            })

        return nav_tracks
