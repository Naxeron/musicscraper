"""
Unit tests for link resolvers (MediaFire, Archive.org, Universal router).
"""

import pytest
from unittest.mock import MagicMock

from musicscraper.scrapers.resolvers.mediafire import MediaFireResolver
from musicscraper.scrapers.resolvers.archive_org import ArchiveOrgResolver
from musicscraper.scrapers.resolvers.universal import UniversalLinkResolver


def test_mediafire_extract_key():
    assert MediaFireResolver.extract_key("https://www.mediafire.com/file/abc123xyz/sample.zip/file") == "abc123xyz"
    assert MediaFireResolver.extract_key("https://mediafire.com/?xyz987") == "xyz987"


def test_archive_org_extract_key():
    assert ArchiveOrgResolver.extract_key("https://archive.org/download/item-id-123/track.flac") == "archive_item-id-123_track.flac"
    assert ArchiveOrgResolver.extract_key("https://archive.org/details/my_album") == "archive_my_album"


def test_universal_resolver_routing():
    resolver = UniversalLinkResolver()

    # Routing key extraction
    assert resolver.extract_key("https://www.mediafire.com/file/test12345/album.zip").startswith("mf_")
    assert resolver.extract_key("https://archive.org/download/cool_ep/01.flac").startswith("archive_")
    assert resolver.extract_key("https://example.com/audio/test.mp3").startswith("https://")


def test_mediafire_resolve_mock():
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'<html><a class="input popsok" aria-label="Download file" href="https://download.mediafire.com/1234/test.zip">Download</a></html>'
    mock_session.get.return_value = mock_resp

    resolver = MediaFireResolver(session=mock_session)
    res = resolver.resolve("https://www.mediafire.com/file/1234/test.zip/file")
    assert res is not None
    assert res["direct_url"] == "https://download.mediafire.com/1234/test.zip"
    assert res["filename"] == "test.zip"
    assert res["host"] == "mediafire"
