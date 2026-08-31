"""
Unit tests for Soulseek candidate file deduplication and slskd enqueue payload deduplication.
"""

from unittest.mock import MagicMock
from musicscraper.services.soulseek import CandidateDir
from musicscraper.clients.slskd import SlskdClient


def test_candidate_dir_deduplication():
    dir_info = {
        "matched_search_files": [
            {"filename": "Music\\Artist\\Album\\01 track.flac", "size": 1000},
            {"filename": "Music\\Artist\\Album\\01 track.flac", "size": 1000},
            {"filename": "Music\\Artist\\Album\\02 track.flac", "size": 2000},
            {"filename": "music/artist/album/02 track.flac", "size": 2000},
        ]
    }
    cd = CandidateDir(user="test_peer", dir_name="Album", dir_info=dir_info)
    assert len(cd.all_dir_files) == 2
    assert len(cd.audio_files) == 2


def test_slskd_enqueue_deduplication(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"

    posted_payloads = []

    def mock_request(method, path, **kwargs):
        if "json" in kwargs:
            posted_payloads.append(kwargs["json"])
        return mock_resp

    monkeypatch.setattr(client, "_request", mock_request)

    files = [
        {"filename": "Album\\01 track.flac", "size": 1000},
        {"filename": "Album\\01 track.flac", "size": 1000},
        {"filename": "Album\\02 track.flac", "size": 2000},
    ]

    res = client.enqueue_download("peer_user", files)
    assert res["status"] == "enqueued"
    assert res["files_count"] == 2
    assert len(posted_payloads) == 1
    assert len(posted_payloads[0]) == 2
    assert posted_payloads[0][0]["filename"] == "Album\\01 track.flac"
    assert posted_payloads[0][1]["filename"] == "Album\\02 track.flac"


def test_prescan_split_completion(monkeypatch):
    from musicscraper.services.soulseek import SlskdArtistScraper
    from musicscraper.clients.musicbrainz import ArtistCatalog

    raw_data = {
        "artist": {"id": "art-1", "name": "Target Artist"},
        "releases_artist": [
            {
                "id": "rel-split-1",
                "title": "Split Album",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "Other Track", "artist-credit": [{"artist": {"id": "art-other", "name": "Other Artist"}}]},
                            {"id": "t2", "title": "Artist Track", "artist-credit": [{"artist": {"id": "art-1", "name": "Target Artist"}}]},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }

    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Target Artist", dry_run=True)
    scraper.catalog = cat
    scraper.all_artist_aliases = {"target artist"}

    # Mock local found map having only the target artist's track
    scraper.local_found_map = {"artist track": {"path": "/music/02 artist track.flac"}}

    # Run the prescan logic for releases
    for rel in scraper.catalog.releases:
        rel_title = rel.get("title", "")
        from musicscraper.core.text import normalize_text
        norm_rel = normalize_text(rel_title)
        rel_tracks = [
            t for t in scraper.catalog.tracks
            if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
            or t.get("norm_release") == norm_rel
        ]
        artist_tracks = [
            t for t in rel_tracks
            if any(alias.lower() in t.get("artist_credit", "").lower() for alias in scraper.all_artist_aliases)
            or t.get("artist_credit", "").lower() == scraper.catalog.name.lower()
        ]
        found_artist_tracks = [t for t in artist_tracks if t.get("norm_title") in scraper.local_found_map]
        if artist_tracks and len(found_artist_tracks) == len(artist_tracks):
            scraper.local_found_releases.add(norm_rel)

    assert "split album" in scraper.local_found_releases

