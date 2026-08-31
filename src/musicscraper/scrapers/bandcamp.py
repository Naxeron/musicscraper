"""
Bandcamp downloader engine supporting free lossless/MP3-320 downloads and stream fallback.
"""

import os
import re
import json
import time
import urllib.parse
from pathlib import Path
from datetime import datetime
from zipfile import ZipFile
from typing import List, Dict, Set, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from musicscraper.config import Config
from musicscraper.core.constants import BANDCAMP_SUPPORTED_FORMATS
from musicscraper.core.text import FilenameUtils
from musicscraper.core.audio import AudioMetadataHandler
from musicscraper.core.report import console
from musicscraper.clients.http import create_resilient_session


class BandcampEngine:
    """Core Bandcamp resolver, scraper, and downloader."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        audio_format: str = "mp3-320",
        fallback: bool = True,
        max_workers: int = 3,
        overwrite: bool = False,
        session: Optional[requests.Session] = None,
        email: Optional[str] = None,
        country: str = "US",
        postcode: str = "90210"
    ):
        self.output_dir = Path(output_dir or Config.DEFAULT_OUTPUT_DIR).resolve()
        self.audio_format = audio_format.lower()
        self.fallback = fallback
        self.max_workers = max_workers
        self.overwrite = overwrite
        self.email = email or Config.BANDCAMP_EMAIL
        self.country = country or "US"
        self.postcode = postcode or "90210"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_dir / "manifest.json"

        self.session = session or create_resilient_session(user_agent=Config.BANDCAMP_USER_AGENT)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Dict[str, Any]]:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_manifest(self) -> None:
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
                return []
        except Exception:
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

        # 3. Fallback scan
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
            except Exception:
                return None

        try:
            resp = self.session.get(release_url, timeout=15)
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        soup = BeautifulSoup(resp.content, "html.parser")
        tralbum_tag = soup.find("script", attrs={"data-tralbum": True})

        if not tralbum_tag:
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
        except Exception:
            return None

    def _process_tralbum_data(self, data: Dict[str, Any], soup: BeautifulSoup, release_url: str) -> Dict[str, Any]:
        artist = data.get("artist", "Unknown Artist")
        current = data.get("current", {})
        title = current.get("title", "Unknown Title")
        item_type = data.get("item_type") or current.get("type", "album")
        item_id = current.get("id") or data.get("id")

        album = title if item_type == "album" else ""
        if not album:
            from_album = soup.select_one("span.fromAlbum")
            if from_album:
                album = from_album.get_text(strip=True)
            elif "album_title" in data:
                album = data["album_title"]
            else:
                album = title

        rel_date = current.get("release_date") or current.get("publish_date") or ""
        year = ""
        if rel_date:
            year_match = re.search(r"\b(19\d\d|20\d\d)\b", rel_date)
            if year_match:
                year = year_match.group(1)

        art_url = None
        art_tag = soup.select_one("div#tralbumArt > a.popupImage") or soup.select_one("div#tralbumArt img")
        if art_tag:
            art_url = art_tag.get("href") or art_tag.get("src")

        download_pref = current.get("download_pref")
        free_dl_page = data.get("freeDownloadPage")
        minimum_price = current.get("minimum_price")
        set_price = current.get("set_price")
        is_set_price = current.get("is_set_price") == 1
        require_email = bool(current.get("require_email") or current.get("require_email_0"))

        is_free_direct = bool(free_dl_page) or (download_pref == 1)
        is_nyp = (download_pref == 2) and (not is_set_price) and (minimum_price == 0.0 or minimum_price == 0 or minimum_price is None)
        is_free = is_free_direct or is_nyp
        is_paid = (download_pref == 2) and not is_nyp

        if is_free_direct:
            price_desc = "Free"
        elif is_nyp:
            price_desc = "NYP (Email)" if require_email else "NYP (Free)"
        elif is_paid:
            price_val = set_price if set_price is not None else minimum_price
            price_desc = f"Paid ({price_val:.2f})" if isinstance(price_val, (int, float)) else "Paid"
        else:
            price_desc = "Stream"

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
        """Submits email to Bandcamp's /email_download endpoint for Name Your Price releases."""
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
                "User-Agent": Config.BANDCAMP_USER_AGENT,
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
        """Resolves direct CDN download link from a free download page."""
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

            chosen_fmt = None
            if self.audio_format in downloads:
                chosen_fmt = self.audio_format
            else:
                for fmt in BANDCAMP_SUPPORTED_FORMATS:
                    if fmt in downloads:
                        chosen_fmt = fmt
                        break

            if not chosen_fmt:
                return None

            fmt_info = downloads[chosen_fmt]
            stat_url = fmt_info.get("url", "")
            if not stat_url:
                return None

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
        except Exception:
            return None

    def download_release(self, meta: Dict[str, Any]) -> bool:
        """Downloads a release via free download ZIP, email request, or stream fallback."""
        artist = meta["artist"]
        album = meta["album"] or meta["title"]
        key = meta["key"]

        if not self.overwrite and key in self.manifest:
            console.print(f"[dim]Skipping already downloaded release: {artist} - {album}[/dim]")
            return True

        safe_artist = FilenameUtils.sanitize(artist)
        safe_album = FilenameUtils.sanitize(album)
        release_dir = self.output_dir / safe_artist / safe_album

        if not self.overwrite and release_dir.exists() and any(release_dir.iterdir()):
            console.print(f"[dim]Directory already exists: {release_dir.relative_to(self.output_dir)} (Skipping)[/dim]")
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

        # 1. Free Direct Download
        if meta.get("free_download_page"):
            resolved = self._resolve_free_download_url(meta["free_download_page"])
            if resolved:
                dl_url, fmt_name = resolved
                console.print(f"[green]Free [{fmt_name.upper()}] download found:[/green] {artist} - {album}")
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
                            console.print(f"[green]✔ Extracted [{fmt_name.upper()}] release to:[/green] {release_dir.name}")
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
                    console.print(f"[yellow]Free download failed for {meta['url']} ({e}), falling back...[/yellow]")

        # 2. Name Your Price email link
        if meta.get("is_nyp") and meta.get("require_email") and self.email:
            ok, direct_dl, msg = self.request_email_download(meta, self.email, self.country, self.postcode)
            if ok and direct_dl:
                resolved = self._resolve_free_download_url(direct_dl)
                if resolved:
                    dl_url, fmt_name = resolved
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
                    except Exception:
                        pass

        # 3. Stream fallback (MP3-128)
        if not self.fallback:
            return False

        tracks = meta.get("tracks", [])
        streamable_tracks = [t for t in tracks if t.get("stream_url")]
        if not streamable_tracks:
            return False

        console.print(f"[cyan]Downloading {len(streamable_tracks)} track(s) [MP3-128 stream]:[/cyan] {artist} - {album}")

        artwork_bytes = None
        if meta.get("art_url"):
            try:
                art_resp = self.session.get(meta["art_url"], timeout=10)
                if art_resp.status_code == 200:
                    artwork_bytes = art_resp.content
                    with open(release_dir / "cover.jpg", "wb") as f_art:
                        f_art.write(artwork_bytes)
            except Exception:
                pass

        success_tracks = 0
        for track in streamable_tracks:
            t_num = track["track_num"]
            t_title = track["title"]
            s_url = track["stream_url"]
            safe_title = FilenameUtils.sanitize(t_title)
            filename = f"{t_num:02d} - {safe_title}.mp3"
            track_file = release_dir / filename

            try:
                resp = self.session.get(s_url, timeout=20)
                if resp.status_code == 200:
                    with open(track_file, "wb") as f_out:
                        f_out.write(resp.content)
                    # Write tags
                    AudioMetadataHandler.write_tags(
                        file_path=track_file,
                        tags={
                            "title": t_title,
                            "artist": artist,
                            "album": album,
                            "track": t_num
                        }
                    )
                    success_tracks += 1
            except Exception:
                pass

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
