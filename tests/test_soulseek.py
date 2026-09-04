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


def test_candidate_indexing_scalability_and_speed():
    """
    SCALABILITY BENCHMARK:
    Verifies that PeerCandidateIndex scales to 5,000 candidate audio files across
    250 directories, indexing in <1s and pruning candidate searches in <50ms.
    """
    import time
    from musicscraper.services.soulseek import PeerCandidateIndex

    peer_dirs = {}
    for d in range(250):
        files = [
            {
                "filename": f"Music\\Artist\\Album_{d}\\{i:02d} unique_track_{d}_{i}.flac",
                "size": 25000000 + i * 1000,
                "bitrate": 950,
                "bitDepth": 16,
                "sampleRate": 44100,
            }
            for i in range(20)
        ]
        peer_dirs[(f"peer_{d}", f"Album_{d}")] = {
            "user": f"peer_{d}",
            "directory": f"Album_{d}",
            "matched_search_files": files,
            "full_directory_files": None,
            "speed": 1000000,
            "queue": 0,
            "has_slot": True,
        }

    t0_idx = time.perf_counter()
    index = PeerCandidateIndex(peer_dirs)
    idx_duration = time.perf_counter() - t0_idx

    assert len(index.all_audio_files) == 5000
    assert len(index.all_dirs) == 250
    assert idx_duration < 1.0, f"Indexing 5000 files took {idx_duration:.4f}s; expected < 1.0s"

    # Query candidate directories for a specific release
    t0_query = time.perf_counter()
    cand_dirs = index.get_candidate_dirs_for_release(
        rel_title="Album_42",
        parsed_expected=[{"sig_words": {"unique", "track", "42", "5"}}],
        expected_count=20
    )
    query_duration = time.perf_counter() - t0_query

    assert query_duration < 0.05, f"Directory query took {query_duration:.4f}s; expected < 0.05s"
    assert any(cd.dir_name == "Album_42" for cd in cand_dirs)
    assert len(cand_dirs) <= 10, f"Inverted index should prune 250 dirs down to candidate match; got {len(cand_dirs)}"


def test_candidate_matching_format_quality_scoring():
    """Verifies that candidate matching evaluates and scores audio formats (FLAC > MP3 320 > MP3 128)."""
    from musicscraper.services.soulseek import CandidateFile

    dir_info = {"speed": 1000, "queue": 0, "has_slot": True}

    flac_file = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.flac", "size": 30000000, "bitDepth": 16, "sampleRate": 44100},
        dir_info=dir_info
    )
    mp3_320 = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.mp3", "size": 8000000, "bitRate": 320},
        dir_info=dir_info
    )
    mp3_128 = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.mp3", "size": 3000000, "bitRate": 128},
        dir_info=dir_info
    )

    assert flac_file.is_audio is True
    assert flac_file.fmt_score > mp3_320.fmt_score > mp3_128.fmt_score
    assert "lossless" in flac_file.fmt_label.lower() or "flac" in flac_file.fmt_label.lower()


def test_partial_album_candidate_matching_selective_queue(monkeypatch):
    """
    Verifies that when a release is partially satisfied locally (e.g. tracks 1 and 2 present),
    the downloader enqueues ONLY the genuinely missing track (track 3) from the remote candidate directory.
    """
    from musicscraper.core.text import normalize_text
    from musicscraper.services.soulseek import SlskdArtistScraper, CandidateDir, pre_parse_expected_tracks
    from musicscraper.clients.musicbrainz import ArtistCatalog

    raw_data = {
        "artist": {"id": "art-partial", "name": "Target Artist"},
        "releases_artist": [
            {
                "id": "rel-partial-1",
                "title": "Three Track EP",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "Track One"},
                            {"id": "t2", "title": "Track Two"},
                            {"id": "t3", "title": "Track Three"},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }

    catalog = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Target Artist", dry_run=True)
    scraper.catalog = catalog
    scraper.all_artist_aliases = {"target artist"}

    # Tracks 1 and 2 already present locally
    scraper.local_found_map = {
        "track one": {"path": "/music/Target Artist/Three Track EP/01 Track One.flac"},
        "track two": {"path": "/music/Target Artist/Three Track EP/02 Track Two.flac"},
    }

    cand_dir_files = [
        {"filename": "Music\\Target Artist - Three Track EP\\01 Track One.flac", "size": 1000},
        {"filename": "Music\\Target Artist - Three Track EP\\02 Track Two.flac", "size": 2000},
        {"filename": "Music\\Target Artist - Three Track EP\\03 Track Three.flac", "size": 3000},
    ]

    cd = CandidateDir(
        user="peer_full",
        dir_name="Target Artist - Three Track EP",
        dir_info={
            "user": "peer_full",
            "directory": "Target Artist - Three Track EP",
            "matched_search_files": cand_dir_files,
            "full_directory_files": cand_dir_files,
            "speed": 5000,
            "queue": 0,
            "has_slot": True,
        }
    )

    expected_tracks = catalog.tracks
    parsed_expected = pre_parse_expected_tracks(expected_tracks)

    # Reconcile release with candidate directory
    res = scraper._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, "Three Track EP")
    assert res is not None
    assert len(res["matched_tracks"]) == 3

    # Missing track filtering: only track three is missing locally
    missing_to_queue = [
        m for m in res["matched_tracks"]
        if normalize_text(m["expected"]) not in scraper.local_found_map
    ]
    assert len(missing_to_queue) == 1
    assert "track three" in normalize_text(missing_to_queue[0]["expected"])


def test_slskd_lru_directory_cache_bounding(monkeypatch):
    """
    Verifies that SlskdClient._directory_cache is bounded to maxsize 500
    when browsing hundreds of remote directories, evicting oldest entries via LRU.
    """
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"filename": "track.flac", "size": 1000}]

    monkeypatch.setattr(client, "_request", lambda method, path, **kwargs: mock_resp)

    # Browse 600 directories
    for i in range(600):
        client.browse_directory(username=f"peer_{i % 50}", directory=f"Music\\Album_{i}")

    assert len(client._directory_cache) <= 500, \
        f"Directory cache exceeded bound of 500: currently {len(client._directory_cache)}"


def test_slskd_lru_directory_cache_eviction_order(monkeypatch):
    """
    Verifies that cache hits refresh LRU order (move_to_end) and
    least recently used entries are evicted first.
    """
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    client.max_directory_cache_size = 3

    request_calls = []

    def mock_request(method, path, **kwargs):
        req_dir = kwargs.get("json", {}).get("directory", "default")
        request_calls.append(req_dir)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {"filename": f"{req_dir}.flac", "size": 1000}
        ]
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    # Insert dirs 1, 2, 3
    client.browse_directory("user1", "dir1")
    client.browse_directory("user1", "dir2")
    client.browse_directory("user1", "dir3")

    expected_keys = {"user1:dir1", "user1:dir2", "user1:dir3"}
    assert set(client._directory_cache.keys()) == expected_keys

    # Access dir1 (cache hit, moves to end/MRU)
    res_dir1 = client.browse_directory("user1", "dir1")
    assert res_dir1[0]["filename"] == "dir1.flac"
    # No new HTTP request should have been made for dir1
    assert request_calls.count("dir1") == 1

    # Insert dir4 -> dir2 was least recently used, so dir2 should be evicted!
    client.browse_directory("user1", "dir4")
    assert len(client._directory_cache) == 3
    assert "user1:dir2" not in client._directory_cache
    assert "user1:dir1" in client._directory_cache
    assert "user1:dir3" in client._directory_cache
    assert "user1:dir4" in client._directory_cache

    # Verify LRU order: dir3 is now oldest, dir1 is next, dir4 is newest
    keys = list(client._directory_cache.keys())
    assert keys == ["user1:dir3", "user1:dir1", "user1:dir4"]


def test_slskd_lru_directory_cache_batch_and_concurrency(monkeypatch):
    """
    Verifies that browse_directories_batch bounds LRU cache properly.
    """
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    client.max_directory_cache_size = 5

    def mock_request(method, path, **kwargs):
        req_dir = kwargs.get("json", {}).get("directory", "default")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"filename": f"{req_dir}.flac", "size": 500}]
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    # Batch browse 8 directories
    reqs = [(f"peer_{i}", f"Dir_{i}") for i in range(8)]
    results = client.browse_directories_batch(reqs)

    assert len(results) == 8
    assert len(client._directory_cache) <= 5

    # Access a cached directory via batch browse again
    cached_key = list(client._directory_cache.keys())[0]
    cached_user, cached_dir = cached_key.split(":")
    batch_res2 = client.browse_directories_batch([(cached_user, cached_dir)])
    assert (cached_user, cached_dir) in batch_res2
    # Verify cached item was moved to end (MRU)
    assert list(client._directory_cache.keys())[-1] == cached_key
