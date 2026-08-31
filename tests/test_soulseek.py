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
