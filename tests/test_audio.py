"""
Unit tests for audio metadata handling and quality scoring.
"""

from pathlib import Path
import pytest

from musicscraper.core.audio import (
    AudioMetadata,
    AudioQualityAnalyzer,
    AudioMetadataHandler
)


def test_quality_score_calculation():
    # 24-bit 96kHz FLAC
    flac_hi = AudioMetadata(
        path=Path("/tmp/song.flac"),
        file_type="flac",
        is_lossless=True,
        bit_depth=24,
        sample_rate=96000
    )
    score_hi = AudioQualityAnalyzer.calculate_quality_score(flac_hi)
    assert score_hi > 100

    # 16-bit 44.1kHz FLAC
    flac_cd = AudioMetadata(
        path=Path("/tmp/song.flac"),
        file_type="flac",
        is_lossless=True,
        bit_depth=16,
        sample_rate=44100
    )
    score_cd = AudioQualityAnalyzer.calculate_quality_score(flac_cd)
    assert score_cd == 100

    # MP3 320k
    mp3_320 = AudioMetadata(
        path=Path("/tmp/song.mp3"),
        file_type="mp3",
        is_lossless=False,
        bitrate_kbps=320
    )
    score_mp3_320 = AudioQualityAnalyzer.calculate_quality_score(mp3_320)
    assert score_mp3_320 == 80

    # MP3 128k
    mp3_128 = AudioMetadata(
        path=Path("/tmp/song.mp3"),
        file_type="mp3",
        is_lossless=False,
        bitrate_kbps=128
    )
    score_mp3_128 = AudioQualityAnalyzer.calculate_quality_score(mp3_128)
    assert score_mp3_128 < score_mp3_320


def test_stream_quality_determination():
    label, score = AudioQualityAnalyzer.determine_stream_quality({"filename": "track.flac", "bitRate": 1000})
    assert "FLAC" in label
    assert score >= 100

    label_320, score_320 = AudioQualityAnalyzer.determine_stream_quality({"filename": "track.mp3", "bitRate": 320})
    assert "320" in label_320
    assert score_320 == 80


def test_mbid_tag_mapping(monkeypatch):
    class DummyTags(dict):
        def items(self):
            return super().items()

    class DummyMutagenFile:
        def __init__(self, path):
            self.tags = DummyTags({
                "MUSICBRAINZ_TRACKID": ["rec-id-1234"],
                "MUSICBRAINZ_RECORDINGID": ["rec-id-5678"],
                "MUSICBRAINZ_RELEASETRACKID": ["trk-id-9999"],
                "MUSICBRAINZ_ALBUMID": ["rel-id-aaaa"],
                "MUSICBRAINZ_ARTISTID": ["art-id-bbbb"]
            })
            self.info = None

    import mutagen
    monkeypatch.setattr(mutagen, "File", lambda path: DummyMutagenFile(path))

    meta = AudioQualityAnalyzer.analyze_file(Path("/tmp/test_track.flac"))
    assert "rec-id-1234" in meta.mb_rec_ids
    assert "rec-id-5678" in meta.mb_rec_ids
    assert "trk-id-9999" in meta.mb_track_ids
    assert "rel-id-aaaa" in meta.mb_release_ids
    assert "art-id-bbbb" in meta.mb_artist_ids

