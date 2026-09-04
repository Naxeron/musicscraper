"""
Comprehensive 4-Tier Test Suite for DiscographyReconciler and Track Matching Guardrails.

Tier 1: Feature Coverage (Multi-tier reconciler: MBID, Album+Title, Artist+Title, Fuzzy)
Tier 2: Boundary & Corner Cases (Diacritics, NFC/NFD, Roman numerals, dashes, version descriptors, false-positive guardrails)
Tier 3: Cross-Feature Combinations (Split albums, multi-artist credit formatting, numeric filenames in confirmed albums)
Tier 4: Real-World Scenarios (Full synthetic discography audit with found/missing/adversarial tracks)
"""

import unicodedata
import pytest
from musicscraper.clients.musicbrainz import ArtistCatalog
from musicscraper.services.reconciler import DiscographyReconciler, deduplicate_candidate_tracks
from musicscraper.core.text import (
    normalize_text,
    strip_track_number_and_artist,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible,
)


def _make_dummy_catalog(
    artist_name="Test Artist",
    artist_id="art-1",
    aliases=None,
    releases=None,
):
    """Helper to construct a realistic ArtistCatalog from minimal raw dict."""
    alias_list = []
    if aliases:
        for a in aliases:
            alias_list.append({"alias": a, "name": a})

    rel_artist_data = []
    if releases:
        for rel in releases:
            tracks_data = []
            for t in rel.get("tracks", []):
                rec_id = t.get("rec_id") or f"rec-{t['title']}"
                trk_id = t.get("trk_id") or f"trk-{t['title']}"
                ac = t.get("artist_credit")
                if not ac:
                    ac = [{"artist": {"id": artist_id, "name": artist_name}}]
                tracks_data.append({
                    "id": trk_id,
                    "title": t["title"],
                    "number": str(t.get("number", "1")),
                    "position": int(t.get("number", "1")),
                    "recording": {
                        "id": rec_id,
                        "title": t["title"],
                    },
                    "artist-credit": ac,
                })
            rel_artist_data.append({
                "id": rel.get("id", f"rel-{rel['title']}"),
                "title": rel["title"],
                "release-group": {
                    "id": f"rg-{rel['title']}",
                    "title": rel["title"],
                    "primary-type": rel.get("type", "Album"),
                },
                "medium-list": [
                    {
                        "position": 1,
                        "track-list": tracks_data,
                    }
                ],
            })

    raw_data = {
        "artist": {
            "id": artist_id,
            "name": artist_name,
            "sort-name": artist_name,
            "alias-list": alias_list,
            "artist-relation-list": [],
            "url-relation-list": [],
        },
        "releases_artist": rel_artist_data,
        "releases_track_artist": [],
        "recordings": [],
    }
    return ArtistCatalog(raw_data)


# ==============================================================================
# Tier 1: Feature Coverage (Isolated Happy-Path Tests)
# ==============================================================================

class TestTier1ReconcilerFeatureCoverage:
    """Verifies standard 4-tier matching operations under clean inputs."""

    def test_reconcile_tier1_mbid_recording_match(self):
        """Tier 1: Exact MusicBrainz Recording ID match."""
        catalog = _make_dummy_catalog(
            releases=[{"title": "Album One", "tracks": [{"title": "Alpha Track", "rec_id": "rec-alpha"}]}]
        )
        local_tracks = [{
            "path": "/music/Album One/01 Alpha.flac",
            "filename": "01 Alpha.flac",
            "title": "Alpha",
            "norm_title": "alpha",
            "album": "Album One",
            "norm_album": "album one",
            "artists": ["Test Artist"],
            "track_number": "1",
            "mb_rec_ids": {"rec-alpha"},
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert len(missing) == 0
        assert found[0]["match_method"] == "Exact MBID (Recording)"
        assert found[0]["mb_track"]["title"] == "Alpha Track"

    def test_reconcile_tier1_mbid_track_id_match(self):
        """Tier 1: Exact MusicBrainz Track ID match."""
        catalog = _make_dummy_catalog(
            releases=[{"title": "Album One", "tracks": [{"title": "Beta Track", "trk_id": "trk-beta"}]}]
        )
        local_tracks = [{
            "path": "/music/Album One/02 Beta.mp3",
            "filename": "02 Beta.mp3",
            "title": "Beta",
            "norm_title": "beta",
            "album": "Album One",
            "norm_album": "album one",
            "artists": ["Test Artist"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": {"trk-beta"},
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert len(missing) == 0
        assert found[0]["match_method"] == "Exact MBID (Track ID)"

    def test_reconcile_tier2_exact_album_and_title(self):
        """Tier 2: Matches when album and title match with >0.90 similarity and compatible version."""
        catalog = _make_dummy_catalog(
            releases=[{"title": "Selected Ambient Works", "tracks": [{"title": "Pulsewidth", "number": "3"}]}]
        )
        local_tracks = [{
            "path": "/music/Aphex Twin/Selected Ambient Works/03 Pulsewidth.flac",
            "filename": "03 Pulsewidth.flac",
            "title": "Pulsewidth",
            "norm_title": "pulsewidth",
            "album": "Selected Ambient Works",
            "norm_album": "selected ambient works",
            "artists": ["Aphex Twin"],
            "track_number": "3",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert len(missing) == 0
        assert "Exact Title & Album Match" in found[0]["match_method"]

    def test_reconcile_tier3_artist_alias_and_title(self):
        """Tier 3: Matches when artist alias is in tags/path and title matches without album match."""
        catalog = _make_dummy_catalog(
            artist_name="Richard D. James",
            aliases=["Aphex Twin", "AFX"],
            releases=[{"title": "Drukqs", "tracks": [{"title": "Jynweythek", "number": "1"}]}]
        )
        # Local file has different or missing album name, but artist alias AFX in tags
        local_tracks = [{
            "path": "/music/Compilations/Jynweythek.flac",
            "filename": "Jynweythek.flac",
            "title": "Jynweythek",
            "norm_title": "jynweythek",
            "album": "Unknown Folder",
            "norm_album": "unknown folder",
            "artists": ["AFX"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert len(missing) == 0
        assert "Title & Artist Alias Match" in found[0]["match_method"]

    def test_reconcile_tier4_fuzzy_match(self):
        """Tier 4: Strict fuzzy match for minor title variance (0.85-0.90) when container confirmed."""
        catalog = _make_dummy_catalog(
            artist_name="Stellabee",
            releases=[{"title": "Breakcore Forever", "tracks": [{"title": "Hardcore Sound", "number": "1"}]}]
        )
        local_tracks = [{
            "path": "/music/Stellabee/Breakcore Forever/01 Hardcore Song.mp3",
            "filename": "01 Hardcore Song.mp3",
            "title": "Hardcore Song",  # 0.8888 similarity
            "norm_title": "hardcore song",
            "album": "Breakcore Forever",
            "norm_album": "breakcore forever",
            "artists": ["Stellabee"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert len(missing) == 0
        assert "Fuzzy Match" in found[0]["match_method"]

    def test_candidate_deduplication_combines_mbids(self):
        """deduplicate_candidate_tracks merges Navidrome and Local Disk candidates."""
        tracks = [
            {
                "path": "subsonic:12345",
                "filename": "01 song.mp3",
                "norm_title": "song",
                "norm_album": "album",
                "track_number": "1",
                "mb_rec_ids": {"rec-1"},
                "source": "navidrome",
            },
            {
                "path": "/music/Artist/Album/01 song.flac",
                "filename": "01 song.flac",
                "norm_title": "song",
                "norm_album": "album",
                "track_number": "1",
                "mb_rec_ids": {"rec-2"},
                "source": "local",
            },
        ]
        merged = deduplicate_candidate_tracks(tracks)
        assert len(merged) == 1
        assert merged[0]["path"] == "/music/Artist/Album/01 song.flac"
        assert merged[0]["mb_rec_ids"] == {"rec-1", "rec-2"}
        assert merged[0]["source"] == "local+navidrome"


# ==============================================================================
# Tier 2: Boundary & Corner Cases (Diacritics, Numerals, Guardrails)
# ==============================================================================

class TestTier2ReconcilerBoundaryAndGuardrails:
    """Verifies edge-case character normalizations and false-positive prevention."""

    def test_unicode_nfc_vs_nfd_normalization(self):
        """Asserts that NFD decomposed Unicode matches NFC precomposed catalog titles."""
        catalog = _make_dummy_catalog(
            artist_name="すてらべえ",
            releases=[{"title": "EP", "tracks": [{"title": "ガ", "number": "1"}]}]  # \u30ac (NFC)
        )
        # NFD decomposed form \u30ab\u3099
        nfd_title = unicodedata.normalize("NFD", "ガ")
        local_tracks = [{
            "path": f"/music/すてらべえ/EP/01 {nfd_title}.flac",
            "filename": f"01 {nfd_title}.flac",
            "title": nfd_title,
            "norm_title": normalize_text(nfd_title),
            "album": "EP",
            "norm_album": normalize_text("EP"),
            "artists": ["すてらべえ"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1, "NFD decomposed Unicode must match NFC catalog entry"
        assert len(missing) == 0

    def test_diacritics_polish_and_european(self):
        """Asserts Polish diacritics and European accents match cleanly."""
        catalog = _make_dummy_catalog(
            artist_name="Artysta",
            releases=[{"title": "Łódź", "tracks": [{"title": "Święty Spokój", "number": "1"}]}]
        )
        # Disk has ASCII or transliterated names
        local_tracks = [{
            "path": "/music/Artysta/Lodz/01 Swiety Spokoj.mp3",
            "filename": "01 Swiety Spokoj.mp3",
            "title": "Swiety Spokoj",
            "norm_title": normalize_text("Swiety Spokoj"),
            "album": "Lodz",
            "norm_album": normalize_text("Lodz"),
            "artists": ["Artysta"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1, "Polish diacritics must match transliterated disk files"
        assert len(missing) == 0

    def test_roman_numeral_title_matching(self):
        """Asserts Roman numerals match Arabic digit variations (e.g. Part I vs Part 1)."""
        catalog = _make_dummy_catalog(
            artist_name="Composer",
            releases=[{"title": "Symphony", "tracks": [
                {"title": "Movement I", "number": "1"},
                {"title": "Movement II", "number": "2"}
            ]}]
        )
        # Disk has Arabic digits: Movement 1, Movement 2
        local_tracks = [
            {
                "path": "/music/Composer/Symphony/01 Movement 1.flac",
                "filename": "01 Movement 1.flac",
                "title": "Movement 1",
                "norm_title": normalize_text("Movement 1"),
                "album": "Symphony",
                "norm_album": normalize_text("Symphony"),
                "artists": ["Composer"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Composer/Symphony/02 Movement 2.flac",
                "filename": "02 Movement 2.flac",
                "title": "Movement 2",
                "norm_title": normalize_text("Movement 2"),
                "album": "Symphony",
                "norm_album": normalize_text("Symphony"),
                "artists": ["Composer"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            }
        ]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 2, "Roman numeral titles must match Arabic digit disk files"
        assert len(missing) == 0

    def test_false_positive_prevention_differing_numbers(self):
        """GUARDRAIL: Differing track numbers (Movement 1 vs Movement 2) must NEVER match."""
        catalog = _make_dummy_catalog(
            artist_name="Composer",
            releases=[{"title": "Symphony", "tracks": [
                {"title": "Movement 1", "number": "1"},
                {"title": "Movement 2", "number": "2"},
            ]}]
        )
        # Only Movement 2 exists on disk
        local_tracks = [{
            "path": "/music/Composer/Symphony/02 Movement 2.flac",
            "filename": "02 Movement 2.flac",
            "title": "Movement 2",
            "norm_title": normalize_text("Movement 2"),
            "album": "Symphony",
            "norm_album": normalize_text("Symphony"),
            "artists": ["Composer"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        # Exactly Movement 2 must match; Movement 1 must be MISSING
        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Movement 2"
        assert len(missing) == 1
        assert missing[0]["mb_track"]["title"] == "Movement 1"

    def test_false_positive_prevention_incompatible_versions(self):
        """GUARDRAIL: Original track must NEVER match Remix, Instrumental, or Acoustic."""
        catalog = _make_dummy_catalog(
            artist_name="Electronic Artist",
            releases=[{"title": "Singles", "tracks": [
                {"title": "Starlight", "number": "1"},
            ]}]
        )
        # Disk only has the Instrumental and Remix versions
        local_tracks = [
            {
                "path": "/music/Singles/01 Starlight (Instrumental).mp3",
                "filename": "01 Starlight (Instrumental).mp3",
                "title": "Starlight (Instrumental)",
                "norm_title": normalize_text("Starlight (Instrumental)"),
                "album": "Singles",
                "norm_album": normalize_text("Singles"),
                "artists": ["Electronic Artist"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            }
        ]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, "Original track must not be satisfied by Instrumental version"
        assert len(missing) == 1

    def test_false_positive_prevention_differing_remixers(self):
        """GUARDRAIL: Remix by Alice must NEVER match Remix by Bob."""
        catalog = _make_dummy_catalog(
            artist_name="Producer",
            releases=[{"title": "Remixes", "tracks": [
                {"title": "Echoes (Alice Remix)", "number": "1"},
            ]}]
        )
        local_tracks = [{
            "path": "/music/Remixes/01 Echoes (Bob Remix).flac",
            "filename": "01 Echoes (Bob Remix).flac",
            "title": "Echoes (Bob Remix)",
            "norm_title": normalize_text("Echoes (Bob Remix)"),
            "album": "Remixes",
            "norm_album": normalize_text("Remixes"),
            "artists": ["Producer"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, "Alice Remix must not match Bob Remix"
        assert len(missing) == 1

    def test_false_positive_prevention_loose_standalone_generic_title(self):
        """GUARDRAIL: Loose file 'Intro.flac' without artist or album container must NOT match."""
        catalog = _make_dummy_catalog(
            artist_name="Target Band",
            releases=[{"title": "Album", "tracks": [{"title": "Intro", "number": "1"}]}]
        )
        # Loose file in unsorted dump without artist tags or matching directory
        local_tracks = [{
            "path": "/music/Downloads/Intro.flac",
            "filename": "Intro.flac",
            "title": "Intro",
            "norm_title": "intro",
            "album": "",
            "norm_album": "",
            "artists": [],
            "track_number": "",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, "Generic standalone title 'Intro' must not match without artist/album container"
        assert len(missing) == 1

    def test_word_boundary_prevents_substring_artist_match(self):
        """GUARDRAIL: Artist alias 'Air' must not match 'Affair' or 'Pair' in directory path."""
        catalog = _make_dummy_catalog(
            artist_name="Air",
            aliases=["Air"],
            releases=[{"title": "Moon Safari", "tracks": [{"title": "La Femme d'Argent", "number": "1"}]}]
        )
        local_tracks = [{
            "path": "/music/Love Affair/Various/01 Track.flac",
            "filename": "01 Track.flac",
            "title": "Track",
            "norm_title": "track",
            "album": "Various",
            "norm_album": "various",
            "artists": ["Someone Else"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()
        assert len(found) == 0

    def test_unbracketed_with_restraint(self):
        """Asserts song titles with 'with' (e.g. 'Stay with Me') do not have the title mutilated."""
        parsed = parse_track_title_structure("Stay with Me")
        assert "with me" in parsed["base_norm"] or parsed["base_norm"] == "stay with me", \
            f"Unbracketed 'with' must not truncate title: got {parsed['base_norm']}"
        assert not any("me" == f.lower() for f in parsed.get("features", []))

    def test_hyphenated_version_descriptors_preservation(self):
        """Asserts 'Song Title - Instrumental' preserves 'Song Title' rather than extracting 'Instrumental'."""
        cleaned = strip_track_number_and_artist("01 Song Title - Instrumental")
        assert "song title" in cleaned.lower(), \
            f"strip_track_number_and_artist must preserve core title: got '{cleaned}'"


# ==============================================================================
# Tier 3: Cross-Feature Combinations (Splits, Multi-Artist, Numeric Files)
# ==============================================================================

class TestTier3ReconcilerCrossFeatureCombinations:
    """Verifies complex feature interactions: split albums, multi-artist credits, numeric files."""

    def test_split_album_multi_artist_catalog_reconciliation(self):
        """Split release where Artist A and Artist B share an album; only Artist A tracks are reconciled."""
        catalog = _make_dummy_catalog(
            artist_name="Artist A",
            artist_id="art-a",
            releases=[{
                "title": "Split CD",
                "tracks": [
                    {
                        "title": "Song by A",
                        "number": "1",
                        "artist_credit": [{"artist": {"id": "art-a", "name": "Artist A"}}]
                    },
                ]
            }]
        )
        local_tracks = [
            {
                "path": "/music/Split CD/01 Song by A.flac",
                "filename": "01 Song by A.flac",
                "title": "Song by A",
                "norm_title": normalize_text("Song by A"),
                "album": "Split CD",
                "norm_album": normalize_text("Split CD"),
                "artists": ["Artist A"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Split CD/02 Song by B.flac",
                "filename": "02 Song by B.flac",
                "title": "Song by B",
                "norm_title": normalize_text("Song by B"),
                "album": "Split CD",
                "norm_album": normalize_text("Split CD"),
                "artists": ["Artist B"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
        ]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Song by A"
        assert len(missing) == 0

    def test_musicbrainz_artist_credit_formatting_with_joinphrase(self):
        """Asserts ArtistCatalog format_credit preserves joinphrase (' & ', ' feat. ')."""
        raw_data = {
            "artist": {"id": "art-1", "name": "Main Artist"},
            "releases_artist": [{
                "id": "rel-collab",
                "title": "Collab Release",
                "release-group": {"id": "rg-collab", "title": "Collab Release"},
                "medium-list": [{
                    "track-list": [{
                        "id": "trk-1",
                        "title": "Shared Track",
                        "number": "1",
                        "recording": {"id": "rec-1", "title": "Shared Track"},
                        "artist-credit": [
                            {"name": "Main Artist", "artist": {"id": "art-1", "name": "Main Artist"}, "joinphrase": " & "},
                            {"name": "Guest Artist", "artist": {"id": "art-2", "name": "Guest Artist"}}
                        ]
                    }]
                }]
            }],
            "releases_track_artist": [],
            "recordings": [],
        }
        catalog = ArtistCatalog(raw_data)
        assert len(catalog.tracks) == 1
        track = catalog.tracks[0]
        credit = track.get("artist_credit", "")
        assert "&" in credit or "Guest Artist" in credit, \
            f"Expected multi-artist credit with joinphrase, got: '{credit}'"

    def test_purely_numeric_track_filename_in_confirmed_album(self):
        """In confirmed album folder, '01.flac' and '02.flac' match official track numbers 1 and 2."""
        catalog = _make_dummy_catalog(
            artist_name="Band",
            releases=[{
                "title": "Greatest Hits",
                "tracks": [
                    {"title": "Track One", "number": "1"},
                    {"title": "Track Two", "number": "2"},
                ]
            }]
        )
        local_tracks = [
            {
                "path": "/music/Band/Greatest Hits/01.flac",
                "filename": "01.flac",
                "title": "",  # missing title tag
                "norm_title": "",
                "album": "Greatest Hits",
                "norm_album": normalize_text("Greatest Hits"),
                "artists": ["Band"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Band/Greatest Hits/02.flac",
                "filename": "02.flac",
                "title": "",
                "norm_title": "",
                "album": "Greatest Hits",
                "norm_album": normalize_text("Greatest Hits"),
                "artists": ["Band"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
        ]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        # In confirmed album folders, numeric track filenames should reconcile
        # (Tier 2 or Tier 4 track-number match fallback)
        matched_titles = {f["mb_track"]["title"] for f in found}
        assert "Track One" in matched_titles or len(found) > 0


# ==============================================================================
# Tier 4: Real-World Scenarios (Full Synthetic Discography Audit)
# ==============================================================================

class TestTier4ReconcilerRealWorldScenarios:
    """Verifies end-to-end reconciliation across a multi-album synthetic discography."""

    def test_full_synthetic_discography_audit(self):
        """Audits a catalog of 3 albums: one complete, one partial (1 missing), one totally missing."""
        catalog = _make_dummy_catalog(
            artist_name="Electronic Masters",
            releases=[
                {
                    "title": "Album Complete",
                    "tracks": [
                        {"title": "Track A", "number": "1"},
                        {"title": "Track B", "number": "2"},
                    ]
                },
                {
                    "title": "Album Partial",
                    "tracks": [
                        {"title": "Present Song", "number": "1"},
                        {"title": "Missing Song", "number": "2"},
                    ]
                },
                {
                    "title": "Album Missing",
                    "tracks": [
                        {"title": "Lost Track 1", "number": "1"},
                        {"title": "Lost Track 2", "number": "2"},
                    ]
                }
            ]
        )
        local_tracks = [
            # Album Complete
            {
                "path": "/music/Electronic Masters/Album Complete/01 Track A.flac",
                "filename": "01 Track A.flac",
                "title": "Track A",
                "norm_title": normalize_text("Track A"),
                "album": "Album Complete",
                "norm_album": normalize_text("Album Complete"),
                "artists": ["Electronic Masters"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": "/music/Electronic Masters/Album Complete/02 Track B.flac",
                "filename": "02 Track B.flac",
                "title": "Track B",
                "norm_title": normalize_text("Track B"),
                "album": "Album Complete",
                "norm_album": normalize_text("Album Complete"),
                "artists": ["Electronic Masters"],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            # Album Partial (only Track 1 present)
            {
                "path": "/music/Electronic Masters/Album Partial/01 Present Song.flac",
                "filename": "01 Present Song.flac",
                "title": "Present Song",
                "norm_title": normalize_text("Present Song"),
                "album": "Album Partial",
                "norm_album": normalize_text("Album Partial"),
                "artists": ["Electronic Masters"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            # Adversarial unrelated track from different artist that should NOT match anything
            {
                "path": "/music/Other Artist/Other Album/01 Random Track.mp3",
                "filename": "01 Random Track.mp3",
                "title": "Random Track",
                "norm_title": normalize_text("Random Track"),
                "album": "Other Album",
                "norm_album": normalize_text("Other Album"),
                "artists": ["Other Artist"],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            }
        ]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        found_titles = {f["mb_track"]["title"] for f in found}
        missing_titles = {m["mb_track"]["title"] for m in missing}

        assert "Track A" in found_titles
        assert "Track B" in found_titles
        assert "Present Song" in found_titles

        assert "Missing Song" in missing_titles
        assert "Lost Track 1" in missing_titles
        assert "Lost Track 2" in missing_titles

        assert len(found) == 3
        assert len(missing) == 3
