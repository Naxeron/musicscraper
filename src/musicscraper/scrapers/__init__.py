"""
Web scrapers, release crawlers, and link resolvers.
"""

from musicscraper.scrapers.bandcamp import BandcampEngine
from musicscraper.scrapers.universal import UniversalScraper, MusicDownloader
from musicscraper.scrapers.resolvers.mediafire import MediaFireResolver
from musicscraper.scrapers.resolvers.archive_org import ArchiveOrgResolver
from musicscraper.scrapers.resolvers.universal import UniversalLinkResolver

__all__ = [
    "BandcampEngine",
    "UniversalScraper",
    "MusicDownloader",
    "MediaFireResolver",
    "ArchiveOrgResolver",
    "UniversalLinkResolver",
]
