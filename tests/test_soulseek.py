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


def test_slskd_batch_search_waits_for_completion(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    call_count = {"count": 0}
    progress_updates = []

    def mock_request(method, path, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if method == "GET" and path == "/api/v0/searches":
            resp.json.return_value = []
            return resp
        elif method == "POST" and path == "/api/v0/searches":
            resp.json.return_value = {"id": "search-123", "searchText": kwargs["json"]["searchText"]}
            return resp
        elif method == "GET" and "/api/v0/searches/search-123" in path:
            call_count["count"] += 1
            if call_count["count"] < 3:
                # Simulating in-progress search where slskd has responseCount > 0 but empty responses
                resp.json.return_value = {
                    "id": "search-123",
                    "searchText": "test artist",
                    "isComplete": False,
                    "state": "InProgress",
                    "responseCount": 50,
                    "fileCount": 200,
                    "responses": []
                }
            else:
                # Search finishes on 3rd poll
                resp.json.return_value = {
                    "id": "search-123",
                    "searchText": "test artist",
                    "isComplete": True,
                    "state": "Completed, TimedOut",
                    "responseCount": 50,
                    "fileCount": 200,
                    "responses": [{"username": "peer1", "files": [{"filename": "song.flac", "size": 1000}]}]
                }
            return resp
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    def on_prog(done, total, q):
        progress_updates.append((done, total, q))

    results = client.batch_search(["test artist"], timeout=10.0, poll_interval=0.01, on_progress=on_prog)

    assert "test artist" in results
    assert len(results["test artist"]["responses"]) == 1
    assert results["test artist"]["responses"][0]["username"] == "peer1"
    assert call_count["count"] >= 3
    assert len(progress_updates) == 1
    assert progress_updates[0] == (1, 1, "test artist")


def test_slskd_batch_search_uses_in_progress_existing_search(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    post_calls = []

    def mock_request(method, path, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if method == "GET" and path == "/api/v0/searches":
            # Search is already running in slskd
            resp.json.return_value = [
                {
                    "id": "existing-sid",
                    "searchText": "existing query",
                    "isComplete": False,
                    "state": "InProgress",
                    "fileCount": 10,
                    "responseCount": 2
                }
            ]
            return resp
        elif method == "POST" and path == "/api/v0/searches":
            post_calls.append(kwargs["json"])
            resp.json.return_value = {"id": "new-sid"}
            return resp
        elif method == "GET" and "/api/v0/searches/existing-sid" in path:
            resp.json.return_value = {
                "id": "existing-sid",
                "searchText": "existing query",
                "isComplete": True,
                "state": "Completed",
                "responses": [{"username": "peer2", "files": []}]
            }
            return resp
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    results = client.batch_search(["existing query"], timeout=5.0, poll_interval=0.01)

    assert len(post_calls) == 0  # Did not dispatch duplicate POST
    assert "existing query" in results
    assert len(results["existing query"]["responses"]) == 1


def test_query_generation_includes_user_query_and_canonical():
    from musicscraper.services.soulseek import SlskdArtistScraper
    from musicscraper.clients.musicbrainz import ArtistCatalog

    raw_data = {
        "artist": {"id": "art-jp", "name": "すてらべえ"},
        "releases_artist": [
            {
                "id": "rel-1",
                "title": "Breakcore Forever",
                "medium-list": [{"track-list": [{"id": "t1", "title": "Track 1"}]}]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }
    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Stellabee", dry_run=True)
    scraper.catalog = cat

    queries = scraper._generate_all_search_queries()
    # Must include user query, canonical name, and release queries
    assert "Stellabee" in queries
    assert "すてらべえ" in queries
    assert "Stellabee Breakcore Forever" in queries
    assert "すてらべえ Breakcore Forever" in queries
    # Must NOT produce mangled unidecode "suterabee Breakcore Forever"
    assert "suterabee Breakcore Forever" not in queries


def test_candidate_dir_remote_expansion():
    from musicscraper.services.soulseek import SlskdArtistScraper, CandidateDir, PeerCandidateIndex
    from musicscraper.clients.musicbrainz import ArtistCatalog

    raw_data = {
        "artist": {"id": "art-1", "name": "Artist"},
        "releases_artist": [
            {
                "id": "rel-1",
                "title": "Awesome Album",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "First Song"},
                            {"id": "t2", "title": "Second Song"},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }
    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Artist", dry_run=True)
    scraper.catalog = cat
    scraper.all_artist_aliases = {"artist"}

    # Simulate a peer directory where search results only returned track 1
    mock_client = MagicMock()
    mock_client.browse_directories_batch.return_value = {
        ("peer1", "Music\\Artist - Awesome Album"): [
            {"filename": "Music\\Artist - Awesome Album\\01 First Song.flac", "size": 1000},
            {"filename": "Music\\Artist - Awesome Album\\02 Second Song.flac", "size": 2000},
        ]
    }
    scraper.client = mock_client

    scraper.peer_directories = {
        ("peer1", "Music\\Artist - Awesome Album"): {
            "user": "peer1",
            "directory": "Music\\Artist - Awesome Album",
            "matched_search_files": [
                {"filename": "Music\\Artist - Awesome Album\\01 First Song.flac", "size": 1000}
            ],
            "full_directory_files": None,
            "speed": 1000,
            "queue": 0,
            "has_slot": True,
        }
    }
    scraper.candidate_index = PeerCandidateIndex(scraper.peer_directories)

    scraper._reconcile_primary_releases()

    # browse_directories_batch should have been called to expand the album from 1 track to 2 tracks
    mock_client.browse_directories_batch.assert_called_once()
    assert len(scraper.verified_releases) == 1
    assert scraper.verified_releases[0]["matched_count"] == 2
    assert scraper.verified_releases[0]["total_count"] == 2


