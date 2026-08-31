"""
Universal music release crawler and multi-threaded downloader.
"""

import os
import re
import time
import json
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn

from musicscraper.config import Config
from musicscraper.core.constants import AUDIO_ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS
from musicscraper.core.text import FilenameUtils
from musicscraper.core.report import console
from musicscraper.clients.http import create_resilient_session
from musicscraper.scrapers.resolvers.universal import UniversalLinkResolver
from musicscraper.scrapers.bandcamp import BandcampEngine


class UniversalScraper:
    """Scrapes release websites (Dochakuso, Otherman, Bandcamp, or generic sites) for music links."""

    def __init__(self, base_url: str, session: Optional[requests.Session] = None, delay: float = 0.05, crawl_workers: int = 16):
        self.base_url = base_url
        self.parsed_base = urllib.parse.urlparse(base_url)
        self.session = session or create_resilient_session()
        self.delay = delay
        self.crawl_workers = crawl_workers
        self.visited_urls: Set[str] = set()

    def fetch_text(self, url: str) -> Optional[str]:
        if self.delay > 0:
            time.sleep(self.delay)
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.content.decode("utf-8", errors="ignore")
            return None
        except Exception:
            return None

    def fetch_json(self, url: str) -> Optional[Dict[str, Any]]:
        if self.delay > 0:
            time.sleep(self.delay)
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def find_all_music_links_in_text(self, text: str) -> List[str]:
        """Extracts MediaFire, Archive.org, Bandcamp, and direct audio/zip URLs from HTML/text."""
        found: List[str] = []

        # 1. MediaFire
        mf_matches = re.findall(r"https?://(?:www\.)?mediafire\.com/[^\s\"\'<>]+", text)
        for m in mf_matches:
            c = m.rstrip(".,;)>'\"]")
            ext = os.path.splitext(urllib.parse.urlsplit(c).path.lower())[1]
            if ext not in IMAGE_EXTENSIONS and c not in found:
                found.append(c)

        # 2. Archive.org
        arch_matches = re.findall(r"(?:https?:)?//(?:www\.)?archive\.org/[^\s\"\'<>]+", text)
        for a in arch_matches:
            if a.startswith("//"):
                a = "https:" + a
            c = a.rstrip(".,;)>'\"]")
            ext = os.path.splitext(urllib.parse.urlsplit(c).path.lower())[1]
            if ext not in IMAGE_EXTENSIONS and c not in found:
                found.append(c)

        # 3. Bandcamp
        bc_matches = re.findall(r"https?://[a-zA-Z0-9_-]+\.bandcamp\.com/(?:album|track)/[^\s\"\'<>]+", text)
        for b in bc_matches:
            c = b.rstrip(".,;)>'\"]")
            if c not in found:
                found.append(c)

        # 4. Direct audio/archive links
        ext_pattern = "|".join(re.escape(ext.lstrip(".")) for ext in AUDIO_ARCHIVE_EXTENSIONS)
        direct_matches = re.findall(rf"https?://[^\s\"\'<>]+\.(?:{ext_pattern})", text, re.IGNORECASE)
        for d in direct_matches:
            c = d.rstrip(".,;)>'\"]")
            ext = os.path.splitext(urllib.parse.urlsplit(c).path.lower())[1]
            if ext not in IMAGE_EXTENSIONS and c not in found:
                found.append(c)

        return found

    def crawl_dochakuso(self) -> List[Dict[str, str]]:
        """Crawls dochakuso.net release pages."""
        html = self.fetch_text(self.base_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        release_items: List[Dict[str, str]] = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "release/" in href and "bosyuu" not in href:
                sub_url = urllib.parse.urljoin(self.base_url, href)
                title = a_tag.get_text(strip=True)
                if not any(it["subpage_url"] == sub_url for it in release_items):
                    release_items.append({"title": title, "subpage_url": sub_url})

        results: List[Dict[str, str]] = []

        def _scrape_page(item):
            sub_url = item["subpage_url"]
            title = item["title"]
            page_html = self.fetch_text(sub_url)
            if not page_html:
                return []
            links = self.find_all_music_links_in_text(page_html)
            return [{
                "title": title,
                "page_url": sub_url,
                "download_url": lnk,
                "host": "mediafire" if "mediafire" in lnk else ("archive.org" if "archive.org" in lnk else ("bandcamp" if "bandcamp.com" in lnk else "direct"))
            } for lnk in links]

        with ThreadPoolExecutor(max_workers=self.crawl_workers) as executor:
            futures = [executor.submit(_scrape_page, it) for it in release_items]
            for f in as_completed(futures):
                try:
                    results.extend(f.result())
                except Exception:
                    pass

        return results

    def crawl_otherman(self) -> List[Dict[str, str]]:
        """Crawls otherman-records.com via sitemap and release API."""
        sitemap_url = f"{self.parsed_base.scheme}://{self.parsed_base.netloc}/sitemap.xml"
        sitemap_xml = self.fetch_text(sitemap_url)

        if not sitemap_xml:
            return self.crawl_generic()

        locs = re.findall(r"<loc>(https?://[^<]*/releases/([^<]+))</loc>", sitemap_xml)
        results: List[Dict[str, str]] = []
        base_api = f"{self.parsed_base.scheme}://{self.parsed_base.netloc}/index.php//api/releases/id"

        def _fetch_release(url_tuple):
            page_url, rid = url_tuple
            api_url = f"{base_api}/{rid}"
            data = self.fetch_json(api_url)
            if not data:
                return None

            archive_url = data.get("archive")
            if not archive_url:
                text = data.get("text", "")
                found = self.find_all_music_links_in_text(text)
                archive_url = found[0] if found else None

            if not archive_url:
                return None

            if archive_url.startswith("//"):
                archive_url = "https:" + archive_url
            elif not archive_url.startswith("http"):
                archive_url = "https://" + archive_url

            rid_val = data.get("id", rid)
            artist = data.get("artist_name", "")
            title = data.get("title", "")
            full_title = f"[{rid_val}] {artist} - {title}" if artist else f"[{rid_val}] {title}"

            return {
                "title": full_title,
                "page_url": page_url,
                "download_url": archive_url,
                "host": "archive.org" if "archive.org" in archive_url else "direct"
            }

        with ThreadPoolExecutor(max_workers=self.crawl_workers) as executor:
            futures = [executor.submit(_fetch_release, loc) for loc in locs]
            for f in as_completed(futures):
                try:
                    res = f.result()
                    if res:
                        results.append(res)
                except Exception:
                    pass

        return results

    def crawl_generic(self, max_depth: int = 1) -> List[Dict[str, str]]:
        """Generic crawler for any website up to max_depth."""
        queue = [(self.base_url, 0)]
        self.visited_urls.add(self.base_url)
        results: List[Dict[str, str]] = []

        while queue:
            current_url, depth = queue.pop(0)
            html = self.fetch_text(current_url)
            if not html:
                continue

            links = self.find_all_music_links_in_text(html)
            for lnk in links:
                if not any(r["download_url"] == lnk for r in results):
                    results.append({
                        "title": current_url,
                        "page_url": current_url,
                        "download_url": lnk,
                        "host": "mediafire" if "mediafire" in lnk else ("archive.org" if "archive.org" in lnk else ("bandcamp" if "bandcamp.com" in lnk else "direct"))
                    })

            if depth < max_depth:
                soup = BeautifulSoup(html, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                        continue
                    full_link = urllib.parse.urljoin(current_url, href)
                    parsed_link = urllib.parse.urlparse(full_link)
                    if parsed_link.netloc == self.parsed_base.netloc and full_link not in self.visited_urls:
                        self.visited_urls.add(full_link)
                        queue.append((full_link, depth + 1))

        return results

    def crawl(self, max_depth: int = 1) -> List[Dict[str, str]]:
        """Auto-detects site flavor and crawls for releases."""
        netloc = self.parsed_base.netloc.lower()
        if "dochakuso.net" in netloc or "dochakuso" in self.base_url:
            return self.crawl_dochakuso()
        elif "otherman-records.com" in netloc or "otherman" in self.base_url:
            return self.crawl_otherman()
        else:
            return self.crawl_generic(max_depth=max_depth)


class MusicDownloader:
    """Coordinates link deduplication, direct link resolution, and multithreaded downloading."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        max_workers: int = 3,
        overwrite: bool = False,
        bandcamp_format: str = "mp3-320",
        bandcamp_fallback: bool = True
    ):
        self.output_dir = Path(output_dir or Config.DEFAULT_OUTPUT_DIR).resolve()
        self.max_workers = max_workers
        self.overwrite = overwrite
        self.bandcamp_format = bandcamp_format
        self.bandcamp_fallback = bandcamp_fallback
        self.manifest_path = self.output_dir / "manifest.json"
        self.session = create_resilient_session()
        self.resolver = UniversalLinkResolver(session=self.session)
        self.bc_engine = BandcampEngine(
            output_dir=self.output_dir,
            audio_format=self.bandcamp_format,
            fallback=self.bandcamp_fallback,
            max_workers=self.max_workers,
            overwrite=self.overwrite
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
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

    def deduplicate(self, items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Deduplicates release items by unique host key or URL."""
        seen: Set[str] = set()
        unique: List[Dict[str, str]] = []
        for item in items:
            raw_url = item["download_url"]
            key = self.resolver.extract_key(raw_url)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def download_item(self, item: Dict[str, str]) -> bool:
        """Resolves and downloads a single release item."""
        url = item["download_url"]
        key = self.resolver.extract_key(url)

        # 1. Bandcamp
        if item.get("host") == "bandcamp" or "bandcamp.com" in url:
            meta = self.bc_engine.get_release_metadata(url)
            if not meta:
                return False
            return self.bc_engine.download_release(meta)

        # 2. Check manifest
        if not self.overwrite and key in self.manifest:
            entry = self.manifest[key]
            existing_file = self.output_dir / entry.get("filename", "")
            if existing_file.exists() and existing_file.stat().st_size > 0:
                return True

        # 3. Resolve direct link
        resolved = self.resolver.resolve(url)
        if not resolved:
            return False

        direct_url = resolved["direct_url"]
        filename = resolved["filename"]
        target_path = self.output_dir / filename
        part_path = self.output_dir / f"{filename}.part"

        if not self.overwrite and target_path.exists() and target_path.stat().st_size > 0:
            self.manifest[key] = {
                "filename": filename,
                "url": url,
                "direct_url": direct_url,
                "host": resolved.get("host", "direct"),
                "size_bytes": target_path.stat().st_size,
                "downloaded_at": datetime.now().isoformat()
            }
            self._save_manifest()
            return True

        # Download with part file
        try:
            with self.session.get(direct_url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)

            if part_path.exists() and part_path.stat().st_size > 0:
                part_path.rename(target_path)
                self.manifest[key] = {
                    "filename": filename,
                    "url": url,
                    "direct_url": direct_url,
                    "host": resolved.get("host", "direct"),
                    "size_bytes": target_path.stat().st_size,
                    "downloaded_at": datetime.now().isoformat()
                }
                self._save_manifest()
                return True
        except Exception:
            if part_path.exists():
                try:
                    part_path.unlink()
                except Exception:
                    pass
            return False

        return False

    def download_all(self, items: List[Dict[str, str]]) -> Tuple[int, int]:
        """Downloads a collection of release items concurrently."""
        unique_items = self.deduplicate(items)
        if not unique_items:
            return 0, 0

        console.print(f"[cyan]Downloading {len(unique_items)} releases to:[/cyan] {self.output_dir}")
        success_count = 0
        failed_count = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_item = {executor.submit(self.download_item, it): it for it in unique_items}
            for future in as_completed(future_to_item):
                it = future_to_item[future]
                try:
                    ok = future.result()
                    if ok:
                        success_count += 1
                        console.print(f"[green]✔ Downloaded:[/green] {it.get('title', it['download_url'])}")
                    else:
                        failed_count += 1
                        console.print(f"[red]✖ Failed:[/red] {it.get('title', it['download_url'])}")
                except Exception as e:
                    failed_count += 1
                    console.print(f"[red]✖ Error downloading {it['download_url']}: {e}[/red]")

        return success_count, failed_count
