"""
MusicBrainz API client, rate-limited request handler, and ArtistCatalog builder.
"""

import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any

import requests
import musicbrainzngs
from unidecode import unidecode

from musicscraper.config import Config
from musicscraper.core.text import normalize_text
from musicscraper.core.report import console

# Silence noisy third-party loggers
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Initialize musicbrainzngs
musicbrainzngs.set_useragent(
    Config.MB_APP_NAME,
    Config.MB_APP_VERSION,
    Config.MB_APP_CONTACT
)


class ArtistCatalog:
    """Organized, release-grouped, deduplicated discography representation for an artist."""

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
        self.releases: List[Dict[str, Any]] = []
        self.all_external_urls: Set[str] = set(self.bandcamp_urls)
        self.release_urls: Dict[str, List[str]] = {}
        self.recording_urls: Dict[str, List[str]] = {}
        self.mediafire_urls: List[str] = []
        self.archive_urls: List[str] = []
        self.web_urls: List[str] = []

        self._build_catalog()

    def _extract_aliases(self) -> None:
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

        # Related artist personas
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

        self.aliases.discard("")

    def _build_catalog(self) -> None:
        """Organizes all recordings and tracks into a deduplicated, release-grouped catalog."""
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

            # Extract URLs
            rel_urls = [u.get('target', '') for u in rel.get('url-relation-list', []) if u.get('target')]
            if rel_id and rel_urls:
                self.release_urls[rel_id] = rel_urls
            for u in rel_urls:
                self.all_external_urls.add(u)
                if 'bandcamp.com' in u or 'suckpuck.com' in u:
                    if u not in self.bandcamp_urls:
                        self.bandcamp_urls.append(u)
                elif 'mediafire.com' in u:
                    if u not in self.mediafire_urls:
                        self.mediafire_urls.append(u)
                elif 'archive.org' in u:
                    if u not in self.archive_urls:
                        self.archive_urls.append(u)
                elif u.startswith('http') and not any(ign in u for ign in ('discogs.com', 'rateyourmusic.com', 'wikidata.org', 'imdb.com', 'twitter.com', 'instagram.com')):
                    if u not in self.web_urls:
                        self.web_urls.append(u)

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
                    if u not in self.bandcamp_urls:
                        self.bandcamp_urls.append(u)
                elif 'mediafire.com' in u:
                    if u not in self.mediafire_urls:
                        self.mediafire_urls.append(u)
                elif 'archive.org' in u:
                    if u not in self.archive_urls:
                        self.archive_urls.append(u)
                elif u.startswith('http') and not any(ign in u for ign in ('discogs.com', 'rateyourmusic.com', 'wikidata.org', 'imdb.com', 'twitter.com', 'instagram.com')):
                    if u not in self.web_urls:
                        self.web_urls.append(u)

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

        # 3. Direct Recordings
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
                        if ru not in self.bandcamp_urls:
                            self.bandcamp_urls.append(ru)
                    elif 'mediafire.com' in ru:
                        if ru not in self.mediafire_urls:
                            self.mediafire_urls.append(u)
                    elif 'archive.org' in ru:
                        if ru not in self.archive_urls:
                            self.archive_urls.append(u)

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
        return [r for r in self.releases if not r.get("is_va", False)]

    @property
    def compilation_releases(self) -> List[Dict[str, Any]]:
        return [r for r in self.releases if r.get("is_va", False)]


class MusicBrainzClient:
    """MusicBrainz API client with caching and discography retrieval."""

    def __init__(self, cache_dir: Optional[Path] = None, use_cache: bool = True):
        self.cache_dir = Path(cache_dir or Config.MB_CACHE_DIR).resolve()
        self.use_cache = use_cache
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def resolve_artist_mbid(self, query: str) -> Tuple[str, str]:
        """Resolves an artist query (MBID, URL, or Name) to (mbid, canonical_name)."""
        query_strip = query.strip()
        search_cache_file = self.cache_dir / "artist_search_cache.json"

        # 1. Check URL
        url_match = re.search(r'musicbrainz\.org/artist/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', query_strip, re.I)
        if url_match:
            mbid = url_match.group(1)
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 2. Check UUID MBID
        uuid_match = re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', query_strip, re.I)
        if uuid_match:
            mbid = query_strip
            artist_info = musicbrainzngs.get_artist_by_id(mbid)
            return mbid, artist_info['artist'].get('name', 'Unknown Artist')

        # 3. Check persistent search cache
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

        # 4. Check cached artist files
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

        # 5. Search API
        console.print(f"[cyan]Searching MusicBrainz for artist:[/cyan] [bold]{query_strip}[/bold]...")
        artist_list = []
        headers = {"User-Agent": f"{Config.MB_APP_NAME}/{Config.MB_APP_VERSION} ({Config.MB_APP_CONTACT})"}
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
        """Fetches full artist details, aliases, releases, track-releases, and recordings."""
        cache_file = self.cache_dir / f"artist_{mbid}.json"

        if self.use_cache and not force_refresh and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                console.print(f"[dim]Loaded MusicBrainz catalog from cache ({cache_file.name})[/dim]")
                return data
            except Exception:
                pass

        console.print("[cyan]Fetching artist discography from MusicBrainz API...[/cyan]")

        # 1. Artist details & aliases
        artist_data = musicbrainzngs.get_artist_by_id(
            mbid,
            includes=['aliases', 'artist-rels', 'recording-rels', 'release-rels', 'release-group-rels', 'url-rels', 'tags']
        )['artist']

        # 2. Primary Releases
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
            if not rels:
                break
            releases_artist.extend(rels)
            total_count = int(res.get('release-count', 0))
            if len(releases_artist) >= total_count:
                break
            offset += len(rels)

        # 3. Compilations / VA
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
            if not rels:
                break
            releases_track_artist.extend(rels)
            total_count = int(res.get('release-count', 0))
            if len(releases_track_artist) >= total_count:
                break
            offset += len(rels)

        # 4. Standalone Recordings
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
            if not recs:
                break
            recordings.extend(recs)
            total_count = int(res.get('recording-count', 0))
            if len(recordings) >= total_count:
                break
            offset += len(recs)

        # 5. Release Groups
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
            if not rgs:
                break
            release_groups.extend(rgs)
            total_count = int(res.get('release-group-count', 0))
            if len(release_groups) >= total_count:
                break
            offset += len(rgs)

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
            except Exception:
                pass

        return full_data

    def get_release_by_id(self, release_id: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """Fetches detailed release information including medium tracklist, recordings, and artist credits."""
        cache_file = self.cache_dir / f"release_{release_id}.json"
        if self.use_cache and not force_refresh and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        try:
            res = musicbrainzngs.get_release_by_id(
                release_id,
                includes=['recordings', 'media', 'artist-credits', 'release-groups', 'url-rels']
            )
            rel_data = res.get('release', {})
            if self.use_cache and rel_data:
                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(rel_data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return rel_data
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch MusicBrainz release '{release_id}': {e}[/yellow]")
            return None

    def search_release(self, release_title: str, artist_name: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches MusicBrainz for releases matching a title and optional artist name."""
        query_parts = []
        if release_title:
            query_parts.append(f'release:"{release_title}"')
        if artist_name:
            query_parts.append(f'artist:"{artist_name}"')

        query_str = " AND ".join(query_parts) if query_parts else release_title
        try:
            res = musicbrainzngs.search_releases(query=query_str, limit=limit)
            return res.get('release-list', [])
        except Exception:
            try:
                res = musicbrainzngs.search_releases(release=release_title, artist=artist_name, limit=limit)
                return res.get('release-list', [])
            except Exception as e:
                console.print(f"[yellow]Warning: MusicBrainz release search error for '{query_str}': {e}[/yellow]")
                return []

