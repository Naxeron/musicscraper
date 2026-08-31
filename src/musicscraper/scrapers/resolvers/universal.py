"""
Universal routing link resolver dispatching to MediaFire, Archive.org, or Direct endpoints.
"""

import os
import urllib.parse
from typing import Optional, Dict
import requests

from musicscraper.core.constants import AUDIO_ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS
from musicscraper.core.text import FilenameUtils
from musicscraper.clients.http import create_resilient_session
from musicscraper.scrapers.resolvers.mediafire import MediaFireResolver
from musicscraper.scrapers.resolvers.archive_org import ArchiveOrgResolver


class UniversalLinkResolver:
    """Master resolver routing URLs to their corresponding handler."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or create_resilient_session()
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
        ext = os.path.splitext(urllib.parse.urlsplit(str(url)).path.lower())[1]
        if ext in IMAGE_EXTENSIONS:
            return None

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
            parsed = urllib.parse.urlparse(url)
            fname = os.path.basename(parsed.path)
            fext = os.path.splitext(fname.lower())[1]
            if not fname or fext in IMAGE_EXTENSIONS or fext not in AUDIO_ARCHIVE_EXTENSIONS:
                return None
            fname = FilenameUtils.decode(fname)
            fname = FilenameUtils.sanitize(fname)
            return {
                "direct_url": url,
                "filename": fname,
                "key": f"direct_{url}",
                "host": "direct",
                "original_url": url
            }
