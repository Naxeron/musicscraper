#!/usr/bin/env python3
"""
MusicScraper - Universal Music Release Scraper & Downloader
===========================================================
Crawls music websites, finds MediaFire, Archive.org, and direct music release links,
and downloads all releases without duplicates.

Supported Targets & Hosts:
- Dochakuso Records (https://dochakuso.net/release.html) -> MediaFire
- Otherman Records (https://www.otherman-records.com/releases) -> Archive.org
- Any custom website -> MediaFire, Archive.org, and direct audio/archive links (.zip, .flac, .mp3, etc.)

Features:
- Fast parallel discovery of releases and subpages.
- Direct CDN link resolution for MediaFire and Archive.org.
- Zero duplicate downloads via host key tracking, disk verification, and manifest state.
- Resumable downloads with .part temporary files.
- Full UTF-8 and Japanese filename decoding.
"""

import os
import sys
import re
import time
import json
import logging
import argparse
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Set, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from bandcamp_scraper import BandcampEngine, SUPPORTED_FORMATS

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# Default targets
DEFAULT_TARGET_URL = "https://dochakuso.net/release.html"
DEFAULT_OUTPUT_DIR = "./downloads"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Audio and archive extensions recognized for direct links
AUDIO_ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tar.gz",
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus",
    ".alac", ".aiff", ".wma", ".mid", ".midi"
}

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("musicscraper")


class FilenameUtils:
    """Utilities for decoding and sanitizing filenames."""

    @staticmethod
    def sanitize(filename: str) -> str:
        """Sanitizes filename for cross-platform filesystem safety while preserving Unicode."""
        filename = re.sub(r'[\\/*?:"<>|]', '_', filename)
        filename = filename.strip('. \t\r\n')
        if not filename:
            filename = f"download_{int(time.time())}.bin"
        return filename

    @staticmethod
    def decode(raw_name: str) -> str:
        """Decodes raw or double percent-encoded strings and fixes Latin-1 mojibake."""
        decoded = urllib.parse.unquote(urllib.parse.unquote(raw_name))
        try:
            decoded = decoded.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return decoded


class MediaFireResolver:
    """Resolves MediaFire share links to direct CDN download URLs."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        })

    @staticmethod
    def extract_key(url: str) -> Optional[str]:
        patterns = [
            r"mediafire\.com/(?:file(?:_premium)?|download|view)/([a-zA-Z0-9_-]{8,30})",
            r"mediafire\.com/\?([a-zA-Z0-9_-]{8,30})",
            r"mediafire\.com/\?d=([a-zA-Z0-9_-]{8,30})",
        ]
        for pattern in patterns:
            match = re.search(pattern, url, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def resolve(self, mediafire_url: str) -> Optional[Dict[str, str]]:
        file_key = self.extract_key(mediafire_url)
        try:
            resp = self.session.get(mediafire_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"MediaFire returned {resp.status_code} for {mediafire_url}")
                return None

            html = resp.content.decode("utf-8", errors="ignore")

            # Strategy 1: Direct CDN link regex
            direct_url = None
            direct_matches = re.findall(r"https?://download\d*\.mediafire\.com/[^\s\"\'<>]+", html)
            if direct_matches:
                direct_url = direct_matches[0]

            # Strategy 2: Download button href
            if not direct_url:
                btn_matches = (
                    re.findall(r'id=["\']downloadButton["\'][^>]*href=["\']([^"\'\s>]+)["\']', html) or
                    re.findall(r'href=["\']([^"\'\s>]+)["\'][^>]*id=["\']downloadButton["\']', html) or
                    re.findall(r'aria-label=["\']Download file["\'][^>]*href=["\']([^"\'\s>]+)["\']', html) or
                    re.findall(r'href=["\']([^"\'\s>]+)["\'][^>]*aria-label=["\']Download file["\']', html)
                )
                if btn_matches:
                    direct_url = btn_matches[0]

            # Strategy 3: JavaScript kNO assignment
            if not direct_url:
                kno_match = re.search(r'kNO\s*=\s*["\']([^"\'\s]+)["\']', html)
                if kno_match:
                    direct_url = kno_match.group(1)

            if not direct_url:
                logger.warning(f"Could not find direct download link on MediaFire page: {mediafire_url}")
                return None

            # Determine filename
            filename = None
            soup = BeautifulSoup(html, "html.parser")
            fname_tag = (
                soup.find("div", class_="filename") or
                soup.find("span", class_="filename") or
                soup.find("div", class_="dl-btn-label")
            )
            if fname_tag and fname_tag.get_text(strip=True):
                filename = fname_tag.get_text(strip=True)

            if not filename:
                url_path = urllib.parse.urlparse(direct_url).path
                candidate = os.path.basename(url_path)
                if candidate and candidate != "file":
                    filename = candidate
                else:
                    orig_path = urllib.parse.urlparse(mediafire_url).path
                    parts = [p for p in orig_path.split("/") if p and p != "file" and p != file_key]
                    if parts:
                        filename = parts[-1]

            if filename:
                filename = FilenameUtils.decode(filename)
                filename = FilenameUtils.sanitize(filename)
            else:
                filename = f"mediafire_{file_key or 'file'}.zip"

            return {
                "direct_url": direct_url,
                "filename": filename,
                "key": f"mf_{file_key or mediafire_url}",
                "host": "mediafire",
                "original_url": mediafire_url
            }
        except Exception as e:
            logger.error(f"Error resolving MediaFire URL {mediafire_url}: {e}")
            return None


class ArchiveOrgResolver:
    """Resolves Archive.org item/download links to direct download URLs."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    @staticmethod
    def normalize_url(url: str) -> str:
        """Normalizes protocol-relative or non-https archive URLs."""
        if url.startswith("//"):
            return "https:" + url
        elif not url.startswith("http"):
            return "https://" + url
        elif url.startswith("http://"):
            return "https://" + url[7:]
        return url

    @staticmethod
    def extract_item_and_file(url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extracts item ID and filename from Archive.org URL formats:
        - https://archive.org/download/<item_id>/<file.zip>
        - https://archive.org/details/<item_id>
        """
        norm = ArchiveOrgResolver.normalize_url(url)
        parsed = urllib.parse.urlparse(norm)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "download":
            item_id = parts[1]
            filename = "/".join(parts[2:]) if len(parts) > 2 else None
            return item_id, filename
        elif len(parts) >= 2 and parts[0] == "details":
            item_id = parts[1]
            return item_id, None
        return None, None

    def resolve(self, archive_url: str) -> Optional[Dict[str, str]]:
        archive_url = self.normalize_url(archive_url)
        item_id, filename = self.extract_item_and_file(archive_url)

        # Case 1: Direct file download URL (/download/<item>/<file>)
        if item_id and filename:
            decoded_filename = FilenameUtils.decode(filename)
            decoded_filename = FilenameUtils.sanitize(decoded_filename)
            return {
                "direct_url": archive_url,
                "filename": decoded_filename,
                "key": f"archive_{item_id}_{filename}",
                "host": "archive.org",
                "original_url": archive_url
            }

        # Case 2: Details page (/details/<item>), query metadata API for best audio archive
        if item_id and not filename:
            try:
                meta_url = f"https://archive.org/metadata/{item_id}"
                resp = self.session.get(meta_url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    files = data.get("files", [])
                    # Pick zip or flac or mp3 file
                    best_file = None
                    for f in files:
                        fname = f.get("name", "")
                        fmt = f.get("format", "").lower()
                        if fname.lower().endswith(".zip") or "zip" in fmt:
                            best_file = fname
                            break
                        elif fname.lower().endswith((".flac", ".mp3", ".ogg")):
                            if not best_file:
                                best_file = fname

                    if best_file:
                        direct = f"https://archive.org/download/{item_id}/{urllib.parse.quote(best_file)}"
                        decoded_fname = FilenameUtils.decode(best_file)
                        decoded_fname = FilenameUtils.sanitize(decoded_fname)
                        return {
                            "direct_url": direct,
                            "filename": decoded_fname,
                            "key": f"archive_{item_id}_{best_file}",
                            "host": "archive.org",
                            "original_url": archive_url
                        }
            except Exception as e:
                logger.error(f"Error querying Archive.org metadata for {item_id}: {e}")

        # Fallback to direct URL if path contains a filename
        parsed = urllib.parse.urlparse(archive_url)
        fname = os.path.basename(parsed.path)
        if fname:
            fname = FilenameUtils.decode(fname)
            fname = FilenameUtils.sanitize(fname)
            return {
                "direct_url": archive_url,
                "filename": fname,
                "key": f"archive_{archive_url}",
                "host": "archive.org",
                "original_url": archive_url
            }

        return None


class UniversalLinkResolver:
    """Master resolver routing URLs to their corresponding handler."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.mf_resolver = MediaFireResolver(session=self.session)
        self.archive_resolver = ArchiveOrgResolver(session=self.session)

    def extract_key(self, url: str) -> str:
        if "mediafire.com" in url:
            k = self.mf_resolver.extract_key(url)
            return f"mf_{k}" if k else url
        elif "archive.org" in url:
            item, fname = self.archive_resolver.extract_item_and_file(url)
            if item and fname:
                return f"archive_{item}_{fname}"
            elif item:
                return f"archive_{item}"
            return f"archive_{url}"
        elif "bandcamp.com" in url:
            return f"bc_{url}"
        return url

    def resolve(self, url: str) -> Optional[Dict[str, str]]:
        if "mediafire.com" in url:
            return self.mf_resolver.resolve(url)
        elif "archive.org" in url:
            return self.archive_resolver.resolve(url)
        elif "bandcamp.com" in url:
            return {
                "direct_url": url,
                "filename": url,
                "key": f"bc_{url}",
                "host": "bandcamp",
                "original_url": url
            }
        else:
            # Direct link
            parsed = urllib.parse.urlparse(url)
            fname = os.path.basename(parsed.path)
            if not fname:
                fname = f"download_{int(time.time())}.bin"
            fname = FilenameUtils.decode(fname)
            fname = FilenameUtils.sanitize(fname)
            return {
                "direct_url": url,
                "filename": fname,
                "key": f"direct_{url}",
                "host": "direct",
                "original_url": url
            }


class UniversalScraper:
    """Scrapes release websites (Dochakuso, Otherman, Bandcamp, or generic sites) for music links."""

    def __init__(self, base_url: str, session: Optional[requests.Session] = None, delay: float = 0.05, crawl_workers: int = 16):
        self.base_url = base_url
        self.parsed_base = urllib.parse.urlparse(base_url)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
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
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    def fetch_json(self, url: str) -> Optional[Dict]:
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
        found = []

        # 1. MediaFire
        mf_matches = re.findall(r"https?://(?:www\.)?mediafire\.com/[^\s\"\'<>]+", text)
        for m in mf_matches:
            c = m.rstrip(".,;)>'\"]")
            if c not in found:
                found.append(c)

        # 2. Archive.org
        arch_matches = re.findall(r"(?:https?:)?//(?:www\.)?archive\.org/[^\s\"\'<>]+", text)
        for a in arch_matches:
            if a.startswith("//"):
                a = "https:" + a
            c = a.rstrip(".,;)>'\"]")
            if c not in found:
                found.append(c)

        # 3. Bandcamp (album and track URLs)
        bc_matches = re.findall(r"https?://[a-zA-Z0-9_-]+\.bandcamp\.com/(?:album|track)/[^\s\"\'<>]+", text)
        for b in bc_matches:
            c = b.rstrip(".,;)>'\"]")
            if c not in found:
                found.append(c)

        # 4. Direct audio/archive links
        direct_matches = re.findall(r"href=[\"\'](https?://[^\s\"\'<>]+\.(?:zip|rar|7z|tar\.gz|mp3|flac|wav))[\"\']", text, re.IGNORECASE)
        for d in direct_matches:
            if d not in found:
                found.append(d)

        return found

    def crawl_dochakuso(self) -> List[Dict[str, str]]:
        """Crawls dochakuso.net release pages."""
        logger.info(f"Crawling Dochakuso Records index: {self.base_url}")
        html = self.fetch_text(self.base_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        release_items = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "release/" in href and not "bosyuu" in href:
                sub_url = urllib.parse.urljoin(self.base_url, href)
                title = a_tag.get_text(strip=True)
                if not any(it["subpage_url"] == sub_url for it in release_items):
                    release_items.append({"title": title, "subpage_url": sub_url})

        logger.info(f"Found {len(release_items)} release pages. Scraping links concurrently...")
        results = []

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
                "host": "mediafire" if "mediafire" in lnk else ("archive.org" if "archive.org" in lnk else "direct")
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
        logger.info(f"Crawling Otherman Records releases: {self.base_url}")
        sitemap_url = f"{self.parsed_base.scheme}://{self.parsed_base.netloc}/sitemap.xml"
        sitemap_xml = self.fetch_text(sitemap_url)

        if not sitemap_xml:
            logger.warning("Could not fetch sitemap.xml, falling back to generic crawl.")
            return self.crawl_generic()

        locs = re.findall(r"<loc>(https?://[^<]*/releases/([^<]+))</loc>", sitemap_xml)
        logger.info(f"Found {len(locs)} release URLs in sitemap.xml. Fetching API metadata in parallel...")

        results = []
        base_api = f"{self.parsed_base.scheme}://{self.parsed_base.netloc}/index.php//api/releases/id"

        def _fetch_release(url_tuple):
            page_url, rid = url_tuple
            api_url = f"{base_api}/{rid}"
            data = self.fetch_json(api_url)
            if not data:
                return None

            archive_url = data.get("archive")
            if not archive_url:
                # Check text for any other links
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
        results = []

        while queue:
            current_url, depth = queue.pop(0)
            logger.info(f"Crawling (depth {depth}): {current_url}")
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
                        "host": "mediafire" if "mediafire" in lnk else ("archive.org" if "archive.org" in lnk else "direct")
                    })

            if depth < max_depth:
                soup = BeautifulSoup(html, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                        continue
                    full_link = urllib.parse.urljoin(current_url, href)
                    parsed_link = urllib.parse.urlparse(full_link)

                    if parsed_link.netloc == self.parsed_base.netloc:
                        clean_link = urllib.parse.urlunparse(parsed_link._replace(fragment=""))
                        if clean_link not in self.visited_urls:
                            self.visited_urls.add(clean_link)
                            queue.append((clean_link, depth + 1))

        return results

    def crawl_bandcamp(self) -> List[Dict[str, str]]:
        """Crawls Bandcamp artist or release page."""
        logger.info(f"Crawling Bandcamp releases for: {self.base_url}")
        norm_url, tgt_type = BandcampEngine.normalize_target(self.base_url)
        engine = BandcampEngine(output_dir=DEFAULT_OUTPUT_DIR)
        
        if tgt_type == "artist":
            urls = engine.get_artist_release_urls(norm_url)
        else:
            urls = [norm_url]

        results = []
        for u in urls:
            results.append({
                "title": u,
                "page_url": u,
                "download_url": u,
                "host": "bandcamp"
            })
        return results

    def crawl(self, max_depth: int = 1) -> List[Dict[str, str]]:
        """Auto-routes to specialized or generic crawler based on target URL."""
        if "dochakuso.net" in self.base_url:
            return self.crawl_dochakuso()
        elif "otherman-records.com" in self.base_url:
            return self.crawl_otherman()
        elif "bandcamp.com" in self.base_url or (not self.base_url.startswith("http") and "/" not in self.base_url):
            return self.crawl_bandcamp()
        else:
            return self.crawl_generic(max_depth=max_depth)


class MusicDownloader:
    """Coordinates link deduplication, direct link resolution, and multithreaded downloading."""

    def __init__(
        self,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        max_workers: int = 3,
        overwrite: bool = False,
        bandcamp_format: str = "mp3-320",
        bandcamp_fallback: bool = True
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.max_workers = max_workers
        self.overwrite = overwrite
        self.bandcamp_format = bandcamp_format
        self.bandcamp_fallback = bandcamp_fallback
        self.manifest_path = os.path.join(self.output_dir, "manifest.json")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.resolver = UniversalLinkResolver(session=self.session)
        self.bc_engine = BandcampEngine(
            output_dir=self.output_dir,
            audio_format=self.bandcamp_format,
            fallback=self.bandcamp_fallback,
            max_workers=self.max_workers,
            overwrite=self.overwrite
        )
        os.makedirs(self.output_dir, exist_ok=True)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Dict]:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read manifest: {e}")
        return {}

    def _save_manifest(self):
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save manifest: {e}")

    def deduplicate(self, items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Deduplicates release items by unique host key or URL."""
        seen: Set[str] = set()
        unique = []
        for item in items:
            raw_url = item["download_url"]
            key = self.resolver.extract_key(raw_url)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def download_item(self, item: Dict[str, str], progress_position: int = 0) -> bool:
        url = item["download_url"]
        title = item.get("title", "")
        key = self.resolver.extract_key(url)

        # Handle Bandcamp items
        if item.get("host") == "bandcamp" or "bandcamp.com" in url:
            meta = self.bc_engine.get_release_metadata(url)
            if not meta:
                logger.error(f"Failed to fetch Bandcamp metadata for: {url}")
                return False
            return self.bc_engine.download_release(meta)

        # Check manifest
        if not self.overwrite and key in self.manifest:
            entry = self.manifest[key]
            existing_file = os.path.join(self.output_dir, entry.get("filename", ""))
            if os.path.exists(existing_file) and os.path.getsize(existing_file) > 0:
                logger.info(f"Skipping already downloaded file: {entry.get('filename')} (Key: {key})")
                return True

        # Resolve direct link
        resolved = self.resolver.resolve(url)
        if not resolved:
            logger.error(f"Could not resolve download link for: {url}")
            return False

        direct_url = resolved["direct_url"]
        filename = resolved["filename"]
        target_path = os.path.join(self.output_dir, filename)
        part_path = target_path + ".part"

        # Check existing file on disk
        if not self.overwrite and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
            logger.info(f"File already exists on disk: {filename} - skipping.")
            self.manifest[key] = {
                "filename": filename,
                "title": title,
                "download_url": url,
                "file_size": os.path.getsize(target_path),
                "downloaded_at": datetime.now().isoformat()
            }
            self._save_manifest()
            return True

        # Resume support
        existing_part_size = 0
        headers = {}
        if os.path.exists(part_path):
            existing_part_size = os.path.getsize(part_path)
            if existing_part_size > 0:
                headers["Range"] = f"bytes={existing_part_size}-"

        try:
            resp = self.session.get(direct_url, headers=headers, stream=True, timeout=25)

            # Check Content-Disposition for filename
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                cd_fname = cd.split("filename=")[-1].strip("\"' ")
                cd_fname = FilenameUtils.decode(cd_fname)
                cd_fname = FilenameUtils.sanitize(cd_fname)
                if cd_fname and cd_fname != filename:
                    filename = cd_fname
                    target_path = os.path.join(self.output_dir, filename)
                    part_path = target_path + ".part"

            is_resumed = resp.status_code == 206
            if is_resumed:
                total_size = existing_part_size + int(resp.headers.get("Content-Length", 0))
                file_mode = "ab"
            else:
                total_size = int(resp.headers.get("Content-Length", 0))
                existing_part_size = 0
                file_mode = "wb"

            if resp.status_code not in (200, 206):
                logger.error(f"Download failed with status {resp.status_code} for {direct_url}")
                return False

            desc = f"{filename[:35]}..." if len(filename) > 35 else filename
            chunk_size = 64 * 1024

            if HAS_TQDM:
                pbar = tqdm(
                    total=total_size,
                    initial=existing_part_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=desc,
                    position=progress_position,
                    leave=True
                )
            else:
                pbar = None
                logger.info(f"Downloading {filename} ({total_size / (1024*1024):.2f} MB)...")

            with open(part_path, file_mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        if pbar:
                            pbar.update(len(chunk))

            if pbar:
                pbar.close()

            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(part_path, target_path)

            final_size = os.path.getsize(target_path)
            logger.info(f"Successfully downloaded: {filename} ({final_size / (1024*1024):.2f} MB)")

            self.manifest[key] = {
                "filename": filename,
                "title": title,
                "download_url": url,
                "file_size": final_size,
                "downloaded_at": datetime.now().isoformat()
            }
            self._save_manifest()
            return True

        except Exception as e:
            logger.error(f"Failed downloading {filename}: {e}")
            return False

    def download_all(self, items: List[Dict[str, str]], max_files: Optional[int] = None) -> Tuple[int, int]:
        unique_items = self.deduplicate(items)
        if max_files and max_files > 0:
            unique_items = unique_items[:max_files]

        total = len(unique_items)
        logger.info(f"Starting download of {total} unique releases (Workers: {self.max_workers})")

        success_count = 0

        if self.max_workers <= 1:
            for i, item in enumerate(unique_items):
                if self.download_item(item, progress_position=0):
                    success_count += 1
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self.download_item, item, i % self.max_workers): item
                    for i, item in enumerate(unique_items)
                }
                for f in as_completed(futures):
                    item = futures[f]
                    try:
                        if f.result():
                            success_count += 1
                    except Exception as e:
                        logger.error(f"Worker exception on {item.get('download_url')}: {e}")

        logger.info(f"Finished: {success_count}/{total} files downloaded successfully.")
        return success_count, total


def main():
    parser = argparse.ArgumentParser(
        description="MusicScraper - Universal Music Release Scraper & Downloader (MediaFire, Archive.org, Direct Links)"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_TARGET_URL,
        help=f"Target website URL to scrape (default: {DEFAULT_TARGET_URL})"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory to save downloads (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=3,
        help="Number of concurrent download threads (default: 3)"
    )
    parser.add_argument(
        "-d", "--depth",
        type=int,
        default=1,
        help="Max crawl depth for generic websites (default: 1)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="Polite delay between page requests in seconds (default: 0.05)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and list all found releases without downloading"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force redownload even if files exist on disk"
    )
    parser.add_argument(
        "--format", "--bandcamp-format",
        dest="bandcamp_format",
        choices=SUPPORTED_FORMATS,
        default="mp3-320",
        help="Preferred audio format for Bandcamp free downloads (default: mp3-320)"
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable downloading fallback MP3-128 stream audio for Bandcamp releases"
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Limit number of files to download (useful for testing)"
    )
    parser.add_argument(
        "--export-links",
        metavar="FILE",
        help="Export discovered links and release info to a JSON or text file"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    print("=" * 65)
    print("      MusicScraper - Universal Release Downloader")
    print("=" * 65)
    print(f"Target URL:    {args.url}")
    print(f"Output Dir:    {os.path.abspath(args.output_dir)}")
    print(f"Workers:       {args.threads}")
    print(f"Crawl Depth:   {args.depth}")
    print(f"Bandcamp Fmt:  {args.bandcamp_format}")
    print(f"Fallback:      {not args.no_fallback}")
    print(f"Overwrite:     {args.overwrite}")
    if args.max_files:
        print(f"Max Files:     {args.max_files}")
    print("=" * 65)

    # 1. Scrape target
    scraper = UniversalScraper(base_url=args.url, delay=args.delay)
    found_items = scraper.crawl(max_depth=args.depth)

    if not found_items:
        logger.warning("No music download links were found on the target website.")
        sys.exit(0)

    # 2. Deduplicate
    downloader = MusicDownloader(
        output_dir=args.output_dir,
        max_workers=args.threads,
        overwrite=args.overwrite,
        bandcamp_format=args.bandcamp_format,
        bandcamp_fallback=not args.no_fallback
    )
    unique_items = downloader.deduplicate(found_items)

    print(f"\n[+] Total download links found: {len(found_items)}")
    print(f"[+] Unique releases:             {len(unique_items)}")

    # 3. Export if requested
    if args.export_links:
        export_path = args.export_links
        try:
            if export_path.endswith(".json"):
                with open(export_path, "w", encoding="utf-8") as f:
                    json.dump(unique_items, f, ensure_ascii=False, indent=2)
            else:
                with open(export_path, "w", encoding="utf-8") as f:
                    for it in unique_items:
                        f.write(f"{it.get('title', '')}\t{it.get('download_url', '')}\n")
            print(f"[+] Exported links to: {export_path}")
        except Exception as e:
            logger.error(f"Failed to export links: {e}")

    # 4. Dry run
    if args.dry_run:
        print("\n--- Dry Run: Discovered Releases ---")
        for i, it in enumerate(unique_items, 1):
            title = it.get("title", "Unknown")
            dl = it.get("download_url", "")
            host = it.get("host", "direct")
            print(f"{i:3d}. [{host.upper()}] {title} -> {dl}")
        print("\nDry-run complete. Exiting without downloading.")
        sys.exit(0)

    # 5. Execute downloads
    success, total = downloader.download_all(unique_items, max_files=args.max_files)
    print("\n" + "=" * 65)
    print(f"Summary: {success}/{total} releases downloaded successfully.")
    print(f"Files saved in: {os.path.abspath(args.output_dir)}")
    print(f"Download manifest: {downloader.manifest_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
