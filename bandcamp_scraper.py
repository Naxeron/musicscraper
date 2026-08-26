#!/usr/bin/env python3
"""
BandcampScraper - Fast, Native Bandcamp Discography & Release Downloader
========================================================================
Downloads albums, tracks, and complete artist discographies from Bandcamp without
external venvs or heavy dependencies. Supports high-res free downloads (FLAC, MP3-320,
WAV), streaming fallback (MP3-128), embedded metadata/artwork tagging, and zero-duplicate
manifest tracking.
"""

import os
import sys
import re
import time
import json
import logging
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Set, Optional, Tuple, Any

import requests
from bs4 import BeautifulSoup
import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich import box

# Initialize Rich Console
console = Console()

# Bandcamp user agent that reliably circumvents challenges
BANDCAMP_USER_AGENT = "bandcamper/0.0.2"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Supported audio format priorities
SUPPORTED_FORMATS = [
    "flac", "mp3-320", "wav", "aac-hi", "aiff-lossless", "alac", "vorbis", "mp3-v0", "mp3-128"
]

DEFAULT_OUTPUT_DIR = "/mnt/music/downloads" if os.path.exists("/mnt/music/downloads") else "./downloads"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("bandcampscraper")
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


class FilenameUtils:
    """Sanitizes filenames and paths for filesystem safety."""

    @staticmethod
    def sanitize(name: str, max_bytes: int = 240) -> str:
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.strip('. \t\r\n')
        if not name:
            name = f"track_{int(time.time())}"
        # Truncate UTF-8 byte length if needed
        encoded = name.encode("utf-8")
        if len(encoded) > max_bytes:
            name = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return name


class BandcampEngine:
    """Core Bandcamp resolver, scraper, and downloader."""

    def __init__(
        self,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        audio_format: str = "mp3-320",
        fallback: bool = True,
        max_workers: int = 3,
        overwrite: bool = False,
        session: Optional[requests.Session] = None,
        email: Optional[str] = None,
        country: str = "US",
        postcode: str = "90210"
    ):
        self.output_dir = Path(output_dir).resolve()
        self.audio_format = audio_format.lower()
        self.fallback = fallback
        self.max_workers = max_workers
        self.overwrite = overwrite
        self.email = email or os.environ.get("BANDCAMP_EMAIL")
        self.country = country or "US"
        self.postcode = postcode or "90210"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_dir / "manifest.json"

        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": BANDCAMP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        })
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Dict]:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load manifest: {e}")
        return {}

    def _save_manifest(self):
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save manifest: {e}")

    @staticmethod
    def normalize_target(target: str) -> Tuple[str, str]:
        """
        Normalizes artist name, subdomain, or URL into a full URL and target type.
        Returns (normalized_url, target_type): 'artist', 'album', 'track', or 'download'.
        """
        target = target.strip()
        if not target.startswith("http://") and not target.startswith("https://"):
            if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{1,}[a-zA-Z0-9]$", target):
                return f"https://{target.lower()}.bandcamp.com/music", "artist"
            target = "https://" + target

        parsed = urllib.parse.urlparse(target)
        path = parsed.path.rstrip("/")

        if "/download" in path:
            return target, "download"
        elif "/album/" in path:
            return target, "album"
        elif "/track/" in path:
            return target, "track"
        elif path.endswith("/music") or path == "":
            base = f"{parsed.scheme}://{parsed.netloc}/music"
            return base, "artist"
        return target, "artist"

    def get_artist_release_urls(self, artist_url: str) -> List[str]:
        """Crawls an artist's Bandcamp /music page to discover all album and track URLs."""
        if not artist_url.endswith("/music"):
            parsed = urllib.parse.urlparse(artist_url)
            artist_url = f"{parsed.scheme}://{parsed.netloc}/music"

        try:
            resp = self.session.get(artist_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Bandcamp returned status {resp.status_code} for {artist_url}")
                return []
        except Exception as e:
            logger.error(f"Failed to fetch artist page {artist_url}: {e}")
            return []

        soup = BeautifulSoup(resp.content, "html.parser")
        base_url = f"{urllib.parse.urlparse(artist_url).scheme}://{urllib.parse.urlparse(artist_url).netloc}"
        discovered_urls: List[str] = []

        # 1. Parse client-items JSON data attribute
        client_items = soup.find(attrs={"data-client-items": True})
        if client_items:
            try:
                items_data = json.loads(client_items["data-client-items"])
                for it in items_data:
                    p_url = it.get("page_url")
                    if p_url:
                        full = urllib.parse.urljoin(base_url, p_url)
                        if full not in discovered_urls:
                            discovered_urls.append(full)
            except Exception:
                pass

        # 2. Parse ol#music-grid
        music_grid = soup.find("ol", id="music-grid") or soup.find("ol", class_="music-grid")
        if music_grid:
            for a in music_grid.find_all("a", href=True):
                href = a["href"]
                if "/album/" in href or "/track/" in href:
                    full = urllib.parse.urljoin(base_url, href)
                    if full not in discovered_urls:
                        discovered_urls.append(full)

        # 3. Fallback: scan all links for /album/ or /track/
        if not discovered_urls:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/album/" in href or "/track/" in href:
                    full = urllib.parse.urljoin(base_url, href)
                    if full not in discovered_urls:
                        discovered_urls.append(full)

        return discovered_urls

    def get_release_metadata(self, release_url: str) -> Optional[Dict[str, Any]]:
        """Fetches release page and extracts complete TralbumData and tracklist."""
        # Check if URL is a direct Bandcamp download page
        if "/download" in release_url:
            try:
                resp = self.session.get(release_url, timeout=15)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.content, "html.parser")
                    pagedata_tag = soup.find("div", id="pagedata")
                    if pagedata_tag and "data-blob" in pagedata_tag.attrs:
                        blob = json.loads(pagedata_tag["data-blob"])
                        items = blob.get("download_items", [])
                        if items:
                            item = items[0]
                            artist = item.get("artist", "Unknown Artist")
                            title = item.get("title", "Unknown Title")
                            item_type = item.get("item_type", "album")
                            art_url = item.get("art_url")
                            return {
                                "url": release_url,
                                "artist": artist,
                                "title": title,
                                "album": title if item_type == "album" else "",
                                "year": "",
                                "item_type": item_type,
                                "item_id": item.get("id"),
                                "art_url": art_url,
                                "download_pref": 1,
                                "free_download_page": release_url,
                                "minimum_price": 0.0,
                                "set_price": 0.0,
                                "is_set_price": False,
                                "is_free": True,
                                "is_free_direct": True,
                                "is_nyp": False,
                                "require_email": False,
                                "price_desc": "Free (Download Link)",
                                "tracks": [],
                                "key": f"bc_{FilenameUtils.sanitize(artist)}_{FilenameUtils.sanitize(title)}"
                            }
            except Exception as e:
                logger.error(f"Failed to parse download page {release_url}: {e}")
                return None

        try:
            resp = self.session.get(release_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch {release_url} (HTTP {resp.status_code})")
                return None
        except Exception as e:
            logger.error(f"Error fetching release {release_url}: {e}")
            return None

        soup = BeautifulSoup(resp.content, "html.parser")
        tralbum_tag = soup.find("script", attrs={"data-tralbum": True})

        if not tralbum_tag:
            # Try finding inline TralbumData
            for script in soup.find_all("script"):
                if script.string and "TralbumData =" in script.string:
                    m = re.search(r"data-tralbum=[\"'](.*?)[\"']", resp.text)
                    if m:
                        try:
                            raw_json = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                            data = json.loads(raw_json)
                            return self._process_tralbum_data(data, soup, release_url)
                        except Exception:
                            pass
            return None

        try:
            data = json.loads(tralbum_tag["data-tralbum"])
            return self._process_tralbum_data(data, soup, release_url)
        except Exception as e:
            logger.error(f"Failed to parse release data for {release_url}: {e}")
            return None

    def _process_tralbum_data(self, data: Dict[str, Any], soup: BeautifulSoup, release_url: str) -> Dict[str, Any]:
        artist = data.get("artist", "Unknown Artist")
        current = data.get("current", {})
        title = current.get("title", "Unknown Title")
        item_type = data.get("item_type") or current.get("type", "album")
        item_id = current.get("id") or data.get("id")

        # Determine album title
        album = title if item_type == "album" else ""
        if not album:
            from_album = soup.select_one("span.fromAlbum")
            if from_album:
                album = from_album.get_text(strip=True)
            elif "album_title" in data:
                album = data["album_title"]
            else:
                album = title

        # Release year
        rel_date = current.get("release_date") or current.get("publish_date") or ""
        year = ""
        if rel_date:
            year_match = re.search(r"\b(19\d\d|20\d\d)\b", rel_date)
            if year_match:
                year = year_match.group(1)

        # Artwork URL
        art_url = None
        art_tag = soup.select_one("div#tralbumArt > a.popupImage") or soup.select_one("div#tralbumArt img")
        if art_tag:
            art_url = art_tag.get("href") or art_tag.get("src")

        # Free download page & pricing detection
        download_pref = current.get("download_pref")
        free_dl_page = data.get("freeDownloadPage")
        minimum_price = current.get("minimum_price")
        set_price = current.get("set_price")
        is_set_price = current.get("is_set_price") == 1
        require_email = bool(current.get("require_email") or current.get("require_email_0"))

        # Classification
        is_free_direct = bool(free_dl_page) or (download_pref == 1)
        is_nyp = (download_pref == 2) and (not is_set_price) and (minimum_price == 0.0 or minimum_price == 0 or minimum_price is None)
        is_free = is_free_direct or is_nyp
        is_paid = (download_pref == 2) and not is_nyp

        # Format human-readable price description
        if is_free_direct:
            price_desc = "Free"
        elif is_nyp:
            price_desc = "NYP (Email)" if require_email else "NYP (Free)"
        elif is_paid:
            price_val = set_price if set_price is not None else minimum_price
            price_desc = f"Paid ({price_val:.2f})" if isinstance(price_val, (int, float)) else "Paid"
        else:
            price_desc = "Stream"

        # Trackinfo
        tracks = []
        for t in data.get("trackinfo", []):
            num = t.get("track_num") or len(tracks) + 1
            t_title = t.get("title", f"Track {num}")
            stream_file = t.get("file", {})
            stream_url = stream_file.get("mp3-128") if stream_file else None
            duration = t.get("duration")
            tracks.append({
                "track_num": num,
                "title": t_title,
                "duration": duration,
                "stream_url": stream_url,
                "has_stream": bool(stream_url)
            })

        return {
            "url": release_url,
            "artist": artist,
            "title": title,
            "album": album,
            "year": year,
            "item_type": item_type,
            "item_id": item_id,
            "art_url": art_url,
            "download_pref": download_pref,
            "free_download_page": free_dl_page,
            "minimum_price": minimum_price,
            "set_price": set_price,
            "is_set_price": is_set_price,
            "is_free": is_free,
            "is_free_direct": is_free_direct,
            "is_nyp": is_nyp,
            "require_email": require_email,
            "price_desc": price_desc,
            "tracks": tracks,
            "key": f"bc_{FilenameUtils.sanitize(artist)}_{FilenameUtils.sanitize(title)}"
        }

    def request_email_download(
        self,
        meta: Dict[str, Any],
        email: str,
        country: str = "US",
        postcode: str = "90210"
    ) -> Tuple[bool, Optional[str], str]:
        """
        Submits email to Bandcamp's /email_download endpoint for Name Your Price releases.
        Returns (success, download_url_if_any, message).
        """
        release_url = meta.get("url", "")
        item_id = meta.get("item_id")
        item_type = meta.get("item_type", "album")
        if not item_id or not release_url:
            return False, None, "Missing item ID or release URL"

        parsed = urllib.parse.urlparse(release_url)
        email_endpoint = f"{parsed.scheme}://{parsed.netloc}/email_download"

        payload = {
            "encoding_name": "none",
            "item_id": str(item_id),
            "item_type": item_type,
            "address": email,
            "country": country,
            "postcode": postcode
        }

        try:
            headers = {
                "User-Agent": BANDCAMP_USER_AGENT,
                "Referer": release_url,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest"
            }
            resp = self.session.post(email_endpoint, data=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                try:
                    res_json = resp.json()
                    if res_json.get("ok"):
                        dl_url = res_json.get("download_url")
                        if dl_url:
                            return True, dl_url, "Direct download URL received"
                        return True, None, f"Download link emailed to {email}"
                    else:
                        err = res_json.get("error") or res_json.get("errors") or "Unknown error"
                        return False, None, f"Bandcamp error: {err}"
                except Exception:
                    return True, None, f"Download request submitted to {email}"
            else:
                return False, None, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, None, f"Network error: {e}"

    def _resolve_free_download_url(self, free_page_url: str) -> Optional[Tuple[str, str]]:
        """
        Fetches the free download page blob and resolves the direct CDN link for the preferred format.
        Returns (download_url, format_name) or None.
        """
        try:
            resp = self.session.get(free_page_url, timeout=15)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.content, "html.parser")
            pagedata_tag = soup.find("div", id="pagedata")
            if not pagedata_tag or "data-blob" not in pagedata_tag.attrs:
                return None

            blob = json.loads(pagedata_tag["data-blob"])
            download_items = blob.get("download_items", [])
            if not download_items:
                return None

            downloads = download_items[0].get("downloads", {})
            if not downloads:
                return None

            # Pick user requested format or best matching format
            chosen_fmt = None
            if self.audio_format in downloads:
                chosen_fmt = self.audio_format
            else:
                for fmt in SUPPORTED_FORMATS:
                    if fmt in downloads:
                        chosen_fmt = fmt
                        break

            if not chosen_fmt:
                return None

            fmt_info = downloads[chosen_fmt]
            stat_url = fmt_info.get("url", "")
            if not stat_url:
                return None

            # Rewrite /download/ to /statdownload/ for CDN link resolution
            parsed = urllib.parse.urlparse(stat_url)
            stat_path = parsed.path.replace("/download/", "/statdownload/")
            fwd_url = parsed._replace(path=stat_path).geturl()

            fwd_resp = self.session.get(
                fwd_url,
                params={".vrs": 1},
                headers={"Accept": "application/json"},
                timeout=15
            )
            if fwd_resp.status_code == 200:
                fwd_json = fwd_resp.json()
                if fwd_json.get("result", "").lower() == "ok":
                    return fwd_json.get("download_url"), chosen_fmt
                elif "retry_url" in fwd_json:
                    return fwd_json.get("retry_url"), chosen_fmt

            return stat_url, chosen_fmt
        except Exception as e:
            logger.debug(f"Free download resolution failed: {e}")
            return None

    def _tag_mp3(self, file_path: Path, artist: str, album: str, title: str, track_num: int, year: str, artwork_data: Optional[bytes] = None):
        """Writes ID3 tags and embedded artwork to an MP3 file using Mutagen."""
        try:
            try:
                audio = ID3(file_path)
            except ID3NoHeaderError:
                audio = ID3()

            audio.add(TIT2(encoding=3, text=title))
            audio.add(TPE1(encoding=3, text=artist))
            audio.add(TALB(encoding=3, text=album))
            audio.add(TRCK(encoding=3, text=str(track_num)))
            if year:
                audio.add(TDRC(encoding=3, text=year))

            if artwork_data:
                audio.add(APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Front cover
                    desc="Cover",
                    data=artwork_data
                ))

            audio.save(file_path)
        except Exception as e:
            logger.debug(f"Failed tagging {file_path.name}: {e}")

    def download_release(self, meta: Dict[str, Any], progress_callback=None) -> bool:
        """Downloads a release using direct free download, email request, or streaming fallback."""
        artist = meta["artist"]
        album = meta["album"] or meta["title"]
        key = meta["key"]

        # Check manifest
        if not self.overwrite and key in self.manifest:
            logger.info(f"Skipping already downloaded release: {artist} - {album}")
            return True

        # Check directory existence
        safe_artist = FilenameUtils.sanitize(artist)
        safe_album = FilenameUtils.sanitize(album)
        release_dir = self.output_dir / safe_artist / safe_album
        
        if not self.overwrite and release_dir.exists() and any(release_dir.iterdir()):
            logger.info(f"Directory already exists: {release_dir.relative_to(self.output_dir)} (Skipping)")
            self.manifest[key] = {
                "artist": artist,
                "album": album,
                "url": meta["url"],
                "path": str(release_dir),
                "downloaded_at": datetime.now().isoformat()
            }
            self._save_manifest()
            return True

        release_dir.mkdir(parents=True, exist_ok=True)

        # 1. Attempt official Free Download (ZIP / High-Res)
        if meta.get("free_download_page"):
            resolved = self._resolve_free_download_url(meta["free_download_page"])
            if resolved:
                dl_url, fmt_name = resolved
                logger.info(f"Free [{fmt_name.upper()}] download found: {artist} - {album}")
                temp_zip = release_dir / f"temp_{int(time.time())}.zip"
                try:
                    with self.session.get(dl_url, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        with open(temp_zip, "wb") as f:
                            for chunk in r.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)

                    if temp_zip.exists() and temp_zip.suffix == ".zip":
                        try:
                            with ZipFile(temp_zip, "r") as zf:
                                zf.extractall(release_dir)
                            temp_zip.unlink()
                            logger.info(f"Extracted [{fmt_name.upper()}] release to: {release_dir.name}")
                            self.manifest[key] = {
                                "artist": artist,
                                "album": album,
                                "format": fmt_name,
                                "url": meta["url"],
                                "path": str(release_dir),
                                "downloaded_at": datetime.now().isoformat()
                            }
                            self._save_manifest()
                            return True
                        except Exception:
                            # Not a zip file, keep as audio file
                            dest_audio = release_dir / f"{safe_artist} - {safe_album}.{fmt_name}"
                            temp_zip.rename(dest_audio)
                            self.manifest[key] = {
                                "artist": artist,
                                "album": album,
                                "format": fmt_name,
                                "url": meta["url"],
                                "path": str(dest_audio),
                                "downloaded_at": datetime.now().isoformat()
                            }
                            self._save_manifest()
                            return True
                except Exception as e:
                    logger.warning(f"Free download failed for {meta['url']} ({e}), falling back...")

        # 1b. If Name Your Price (no minimum) and requires email
        if meta.get("is_nyp") and meta.get("require_email"):
            if self.email:
                ok, direct_dl, msg = self.request_email_download(meta, self.email, self.country, self.postcode)
                if ok:
                    if direct_dl:
                        resolved = self._resolve_free_download_url(direct_dl)
                        if resolved:
                            dl_url, fmt_name = resolved
                            logger.info(f"Free [{fmt_name.upper()}] download found via NYP link: {artist} - {album}")
                            temp_zip = release_dir / f"temp_{int(time.time())}.zip"
                            try:
                                with self.session.get(dl_url, stream=True, timeout=30) as r:
                                    r.raise_for_status()
                                    with open(temp_zip, "wb") as f:
                                        for chunk in r.iter_content(chunk_size=64 * 1024):
                                            if chunk:
                                                f.write(chunk)
                                if temp_zip.exists() and temp_zip.suffix == ".zip":
                                    with ZipFile(temp_zip, "r") as zf:
                                        zf.extractall(release_dir)
                                    temp_zip.unlink()
                                    self.manifest[key] = {
                                        "artist": artist,
                                        "album": album,
                                        "format": fmt_name,
                                        "url": meta["url"],
                                        "path": str(release_dir),
                                        "downloaded_at": datetime.now().isoformat()
                                    }
                                    self._save_manifest()
                                    return True
                            except Exception as e:
                                logger.warning(f"ZIP extraction failed ({e}), falling back to streams...")
                    else:
                        logger.info(f"📧 High-res ZIP download requested for {artist} - {album} ({msg})")
                else:
                    logger.warning(f"Could not request email download for {artist} - {album}: {msg}")
            else:
                logger.info(
                    f"💡 [NYP] '{artist} - {album}' is Name Your Price ($0 min). High-res ZIP requires email "
                    f"(pass --email <addr> to request lossless/320k links by email)."
                )

        # 2. Fallback: Download individual stream tracks (MP3-128)
        if not self.fallback:
            logger.warning(f"No free download available for {artist} - {album} (Fallback disabled).")
            return False

        tracks = meta.get("tracks", [])
        streamable_tracks = [t for t in tracks if t.get("stream_url")]
        if not streamable_tracks:
            logger.warning(f"No streamable tracks found for {artist} - {album}")
            return False

        logger.info(f"Downloading {len(streamable_tracks)} track(s) [MP3-128 stream]: {artist} - {album}")

        # Fetch artwork once
        artwork_bytes = None
        if meta.get("art_url"):
            try:
                art_resp = self.session.get(meta["art_url"], timeout=10)
                if art_resp.status_code == 200:
                    artwork_bytes = art_resp.content
                    # Also save cover.jpg in album folder
                    with open(release_dir / "cover.jpg", "wb") as f_art:
                        f_art.write(artwork_bytes)
            except Exception:
                pass

        success_tracks = 0
        for track in streamable_tracks:
            t_num = track["track_num"]
            t_title = track["title"]
            s_url = track["stream_url"]
            # Check if any audio file for this track already exists in release_dir in any format
            existing_audio = None
            if not self.overwrite and release_dir.exists():
                clean_title = FilenameUtils.sanitize(t_title).lower()
                for f_exist in release_dir.iterdir():
                    if f_exist.is_file() and f_exist.suffix.lower() in {".flac", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aif", ".aiff"} and f_exist.stat().st_size > 0:
                        fn_low = f_exist.stem.lower()
                        if fn_low.startswith(f"{t_num:02d}") or fn_low.startswith(f"{t_num} ") or fn_low.startswith(f"{t_num}-") or clean_title in fn_low:
                            existing_audio = f_exist
                            break

            if existing_audio:
                success_tracks += 1
                continue

            try:
                resp = self.session.get(s_url, timeout=20)
                if resp.status_code == 200:
                    with open(track_file, "wb") as f_out:
                        f_out.write(resp.content)
                    # Write ID3 tags
                    self._tag_mp3(
                        file_path=track_file,
                        artist=artist,
                        album=album,
                        title=t_title,
                        track_num=t_num,
                        year=meta.get("year", ""),
                        artwork_data=artwork_bytes
                    )
                    success_tracks += 1
            except Exception as e:
                logger.error(f"Failed to download track {filename}: {e}")

        if success_tracks > 0:
            self.manifest[key] = {
                "artist": artist,
                "album": album,
                "format": "mp3-128",
                "tracks_downloaded": success_tracks,
                "url": meta["url"],
                "path": str(release_dir),
                "downloaded_at": datetime.now().isoformat()
            }
            self._save_manifest()
            return True

        return False


def main():
    parser = argparse.ArgumentParser(
        description="BandcampScraper - Fast, Native Bandcamp Discography & Release Downloader"
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Bandcamp artist subdomain, artist URL, album URL, or track URL"
    )
    parser.add_argument(
        "-i", "--input",
        help="Path to a text file containing Bandcamp URLs to download (one per line)"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Destination directory for downloads (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "-f", "--format",
        choices=SUPPORTED_FORMATS,
        default="mp3-320",
        help="Preferred audio format for free downloads (default: mp3-320)"
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable downloading fallback MP3-128 streams when free downloads are not available"
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=3,
        help="Concurrent download worker threads (default: 3)"
    )
    parser.add_argument(
        "--email",
        type=str,
        default=os.environ.get("BANDCAMP_EMAIL"),
        help="Email address to request high-res download links for Name Your Price releases"
    )
    parser.add_argument(
        "--country",
        type=str,
        default="US",
        help="Country code for email download requests (default: US)"
    )
    parser.add_argument(
        "--postcode",
        type=str,
        default="90210",
        help="Postal code for email download requests (default: 90210)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and list all found releases and metadata without downloading"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force redownload even if releases already exist on disk"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable detailed debug logs"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Collect target URLs
    targets: List[str] = list(args.targets)
    if args.input:
        if os.path.exists(args.input):
            with open(args.input, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        targets.append(line)
        else:
            console.print(f"[red]Error:[/red] Input file not found: {args.input}")
            sys.exit(1)

    if not targets:
        parser.print_help()
        sys.exit(1)

    email_info = f" | [dim]Email:[/dim] [cyan]{args.email}[/cyan]" if args.email else ""
    console.print(Panel.fit(
        "[bold cyan]BandcampScraper[/bold cyan] [bold white]- Native Bandcamp Downloader[/bold white]\n"
        f"[dim]Output Directory:[/dim] [green]{os.path.abspath(args.output_dir)}[/green] | "
        f"[dim]Preferred Format:[/dim] [yellow]{args.format}[/yellow] | "
        f"[dim]Fallback:[/dim] [yellow]{not args.no_fallback}[/yellow]{email_info}",
        border_style="cyan"
    ))

    engine = BandcampEngine(
        output_dir=args.output_dir,
        audio_format=args.format,
        fallback=not args.no_fallback,
        max_workers=args.threads,
        overwrite=args.overwrite,
        email=args.email,
        country=args.country,
        postcode=args.postcode
    )

    # 1. Resolve all release URLs
    all_release_urls: List[str] = []
    for tgt in targets:
        norm_url, tgt_type = BandcampEngine.normalize_target(tgt)
        if tgt_type == "artist":
            console.print(f"[cyan]Discovering releases for artist:[/cyan] {norm_url}...")
            discovered = engine.get_artist_release_urls(norm_url)
            console.print(f"  [green]✔ Found {len(discovered)} release(s)[/green]")
            all_release_urls.extend(discovered)
        else:
            all_release_urls.append(norm_url)

    # Deduplicate
    unique_urls = list(dict.fromkeys(all_release_urls))
    if not unique_urls:
        console.print("[yellow]No releases found to download.[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold]Inspecting metadata for {len(unique_urls)} release(s)...[/bold]")
    releases_meta: List[Dict[str, Any]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task("Fetching metadata...", total=len(unique_urls))
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_url = {executor.submit(engine.get_release_metadata, u): u for u in unique_urls}
            for fut in as_completed(future_to_url):
                meta = fut.result()
                if meta:
                    releases_meta.append(meta)
                progress.advance(task)

    # Display releases table
    table = Table(title=f"Discovered Releases ({len(releases_meta)})", box=box.ROUNDED, expand=True)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Artist", style="cyan", min_width=16, ratio=2)
    table.add_column("Release Title", style="bold white", min_width=22, ratio=3)
    table.add_column("Type", style="yellow", justify="center", width=7)
    table.add_column("Year", style="green", justify="center", width=6)
    table.add_column("Tracks", justify="right", width=6)
    table.add_column("Download / Status", justify="center", width=18)

    for idx, meta in enumerate(releases_meta, 1):
        if meta.get("is_free_direct"):
            status_str = "[bold green]Free (ZIP)[/bold green]"
        elif meta.get("is_nyp"):
            if meta.get("require_email"):
                status_str = "[bold cyan]Free (NYP Email)[/bold cyan]"
            else:
                status_str = "[bold cyan]Free (NYP)[/bold cyan]"
        elif any(t.get("has_stream") for t in meta.get("tracks", [])):
            status_str = f"[yellow]Stream ({meta.get('price_desc')})[/yellow]"
        else:
            status_str = f"[red]{meta.get('price_desc')}[/red]"

        table.add_row(
            str(idx),
            meta["artist"],
            meta["title"],
            meta["item_type"].upper(),
            meta.get("year", "-"),
            str(len(meta.get("tracks", []))),
            status_str
        )

    console.print(table)

    if args.dry_run:
        console.print("\n[yellow]Dry-run complete. Exiting without downloading.[/yellow]")
        sys.exit(0)

    # 2. Download all releases
    console.print(f"\n[bold green]Starting download of {len(releases_meta)} release(s)...[/bold green]")
    success_count = 0

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(engine.download_release, m): m for m in releases_meta}
        for f in as_completed(futures):
            meta = futures[f]
            try:
                if f.result():
                    success_count += 1
            except Exception as e:
                logger.error(f"Download failed for {meta['title']}: {e}")

    console.print("\n" + "=" * 65)
    console.print(f"[bold green]Summary:[/bold green] {success_count}/{len(releases_meta)} releases downloaded successfully.")
    console.print(f"[dim]Library location:[/dim] [green]{os.path.abspath(args.output_dir)}[/green]")
    console.print(f"[dim]Manifest logged at:[/dim] [green]{engine.manifest_path}[/green]")
    console.print("=" * 65)


if __name__ == "__main__":
    main()
