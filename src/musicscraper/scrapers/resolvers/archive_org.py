"""
Archive.org link and item resolver for direct audio and archive downloads.
"""

import os
import urllib.parse
from typing import Optional, Dict, Tuple
import requests

from musicscraper.core.constants import AUDIO_ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS
from musicscraper.core.text import FilenameUtils
from musicscraper.clients.http import create_resilient_session


class ArchiveOrgResolver:
    """Resolves Archive.org item/download links to direct download URLs."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or create_resilient_session()

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
        """Extracts item ID and filename from Archive.org URL formats."""
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

    @staticmethod
    def extract_key(url: str) -> str:
        """Extracts unique persistence key for Archive.org URL."""
        item, fname = ArchiveOrgResolver.extract_item_and_file(url)
        if item and fname:
            return f"archive_{item}_{fname}"
        elif item:
            return f"archive_{item}"
        return f"archive_{url}"

    def resolve(self, archive_url: str) -> Optional[Dict[str, str]]:
        archive_url = self.normalize_url(archive_url)
        item_id, filename = self.extract_item_and_file(archive_url)

        # Case 1: Direct file download URL (/download/<item>/<file>)
        if item_id and filename:
            ext = os.path.splitext(filename.lower())[1]
            if ext in IMAGE_EXTENSIONS:
                return None
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
                    best_file = None
                    for f in files:
                        fname = f.get("name", "")
                        fmt = f.get("format", "").lower()
                        ext = os.path.splitext(fname.lower())[1]
                        if ext in IMAGE_EXTENSIONS:
                            continue
                        if fname.lower().endswith((".zip", ".tar", ".gz", ".7z", ".rar", ".tar.gz", ".tgz")) or "zip" in fmt:
                            best_file = fname
                            break
                        elif any(fname.lower().endswith(ae) for ae in AUDIO_ARCHIVE_EXTENSIONS):
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
            except Exception:
                return None

        # Fallback to direct URL if path contains a valid audio/archive filename
        parsed = urllib.parse.urlparse(archive_url)
        fname = os.path.basename(parsed.path)
        ext = os.path.splitext(fname.lower())[1]
        if fname and ext in AUDIO_ARCHIVE_EXTENSIONS and ext not in IMAGE_EXTENSIONS:
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
