"""
MediaFire link resolver for direct CDN download URLs.
"""

import os
import re
import urllib.parse
from typing import Optional, Dict
import requests
from bs4 import BeautifulSoup

from musicscraper.core.constants import IMAGE_EXTENSIONS
from musicscraper.core.text import FilenameUtils
from musicscraper.clients.http import create_resilient_session


class MediaFireResolver:
    """Resolves MediaFire share links to direct CDN download URLs."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or create_resilient_session()

    @staticmethod
    def extract_key(url: str) -> Optional[str]:
        patterns = [
            r"mediafire\.com/(?:file(?:_premium)?|download|view)/([a-zA-Z0-9_-]{4,40})",
            r"mediafire\.com/\?([a-zA-Z0-9_-]{4,40})",
            r"mediafire\.com/\?d=([a-zA-Z0-9_-]{4,40})",
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

            ext = os.path.splitext(filename.lower())[1]
            if ext in IMAGE_EXTENSIONS:
                return None

            return {
                "direct_url": direct_url,
                "filename": filename,
                "key": f"mf_{file_key or mediafire_url}",
                "host": "mediafire",
                "original_url": mediafire_url
            }
        except Exception:
            return None
