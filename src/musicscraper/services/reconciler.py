"""
Multi-tier discography reconciler comparing MusicBrainz tracks against local library audio files.
"""

from typing import Dict, List, Set, Tuple, Optional, Any
from unidecode import unidecode

from musicscraper.core.text import (
    normalize_text,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible
)
from musicscraper.clients.musicbrainz import ArtistCatalog


def deduplicate_candidate_tracks(tracks_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicates and merges candidate audio track items across Navidrome and Local Disk.
    Consolidates MusicBrainz ID tags and preserves local filesystem paths.
    """
    if not tracks_list:
        return []

    by_fingerprint: Dict[str, Dict[str, Any]] = {}
    merged: List[Dict[str, Any]] = []

    for t in tracks_list:
        norm_t = t.get("norm_title", "")
        norm_a = t.get("norm_album", "")
        trk = str(t.get("track_number", "")).strip()
        fn = t.get("filename", "")
        path = t.get("path", "")

        fp = None
        if norm_t and norm_a:
            fp = f"alb_title:{norm_a}::{norm_t}::{trk}"
        elif norm_t:
            fp = f"title:{norm_t}::{trk}::{fn}"

        matched_existing = None
        if fp and fp in by_fingerprint:
            matched_existing = by_fingerprint[fp]
        elif t.get("mb_rec_ids"):
            for ex in by_fingerprint.values():
                if ex.get("mb_rec_ids") and (ex["mb_rec_ids"] & t["mb_rec_ids"]):
                    matched_existing = ex
                    break

        if matched_existing is not None:
            matched_existing["mb_track_ids"].update(t.get("mb_track_ids", set()))
            matched_existing["mb_rec_ids"].update(t.get("mb_rec_ids", set()))
            matched_existing["mb_artist_ids"].update(t.get("mb_artist_ids", set()))
            matched_existing["mb_release_ids"].update(t.get("mb_release_ids", set()))
            if t.get("source") == "local" or (str(path).startswith("/") and not str(matched_existing.get("path", "")).startswith("/")):
                matched_existing["path"] = path
                matched_existing["filename"] = fn or matched_existing.get("filename", "")
                matched_existing["source"] = "local+navidrome"
            elif matched_existing.get("source") != "local":
                matched_existing["source"] = "local+navidrome"
        else:
            item_copy = dict(t)
            item_copy["mb_track_ids"] = set(item_copy.get("mb_track_ids", set()))
            item_copy["mb_rec_ids"] = set(item_copy.get("mb_rec_ids", set()))
            item_copy["mb_artist_ids"] = set(item_copy.get("mb_artist_ids", set()))
            item_copy["mb_release_ids"] = set(item_copy.get("mb_release_ids", set()))
            if fp:
                by_fingerprint[fp] = item_copy
            merged.append(item_copy)

    return merged


class DiscographyReconciler:
    """Matches MusicBrainz tracks against local library audio files across 4 distinct tiers."""

    def __init__(self, catalog: ArtistCatalog, local_tracks: List[Dict[str, Any]]):
        self.catalog = catalog
        self.local_tracks = deduplicate_candidate_tracks(local_tracks)
        self.matched: Dict[int, Tuple[Dict[str, Any], str]] = {}
        self.unmatched_local: List[Dict[str, Any]] = []

    def reconcile(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Matches MusicBrainz tracks against local library audio files.
        Returns (found_items, missing_items).
        """
        matched_mb_indices: Set[int] = set()
        matched_local_paths: Set[str] = set()
        mb_tracks = self.catalog.tracks
        artist_aliases = self.catalog.aliases

        mb_parsed = [parse_track_title_structure(mb["title"]) for mb in mb_tracks]
        local_parsed = [
            parse_track_title_structure(lt.get("title") or lt.get("filename") or "")
            for lt in self.local_tracks
        ]

        # -------------------------------------------------------------
        # TIER 1: Exact MusicBrainz Tag Matching (MBID Track/Recording)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue
            mb_rec_ids = mb.get("recording_ids", set())
            mb_track_ids = mb.get("track_ids", set())

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                if mb_rec_ids and any(rid in lt["mb_rec_ids"] for rid in mb_rec_ids):
                    self.matched[i] = (lt, "Exact MBID (Recording)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

                if mb_track_ids and any(tid in lt["mb_track_ids"] for tid in mb_track_ids):
                    self.matched[i] = (lt, "Exact MBID (Track ID)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

        # -------------------------------------------------------------
        # TIER 2: Exact Release + Compatible Track Title Match
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            p_mb = mb_parsed[i]
            mb_rel_norm = mb.get("norm_release", "")
            if not p_mb["base_norm"]:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                lt_album_norm = lt.get("norm_album", "")
                path_norm = normalize_text(str(lt.get("path", "")))

                base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                base_match = (p_mb["base_norm"] == p_lt["base_norm"]) or base_sim > 0.90
                ver_compat = are_versions_compatible(
                    p_mb["version_type"], p_mb["version_text"],
                    p_lt["version_type"], p_lt["version_text"]
                )

                if base_match and ver_compat:
                    rel_sim = calculate_similarity(mb_rel_norm, lt_album_norm)
                    path_has_rel = bool(mb_rel_norm and mb_rel_norm in path_norm)

                    if rel_sim > 0.7 or path_has_rel or mb.get("release_title") == "Standalone / Other":
                        self.matched[i] = (lt, "Exact Title & Album Match")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

        # -------------------------------------------------------------
        # TIER 3: Track Title + Artist Alias Match (Tags or Path)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            p_mb = mb_parsed[i]
            if not p_mb["base_norm"]:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                path_norm = normalize_text(str(lt.get("path", "")))

                has_artist_tag = any(
                    any(alias in str(a).lower() or alias in unidecode(str(a).lower()) for alias in artist_aliases)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(alias in path_norm for alias in artist_aliases)

                if has_artist_tag or has_artist_path or (mb.get("recording_ids") and any(rid in lt["mb_rec_ids"] for rid in mb["recording_ids"])):
                    base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                    base_match = (p_mb["base_norm"] == p_lt["base_norm"]) or base_sim > 0.90
                    ver_compat = are_versions_compatible(
                        p_mb["version_type"], p_mb["version_text"],
                        p_lt["version_type"], p_lt["version_text"]
                    )

                    if base_match and ver_compat:
                        self.matched[i] = (lt, "Title & Artist Alias Match")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

        # -------------------------------------------------------------
        # TIER 4: Strict Fuzzy Match (Transliterations, Minor Variances)
        # -------------------------------------------------------------
        for i, mb in enumerate(mb_tracks):
            if i in matched_mb_indices:
                continue

            p_mb = mb_parsed[i]
            mb_rel_norm = mb.get("norm_release", "")
            if not p_mb["base_norm"] or len(p_mb["base_norm"]) < 3:
                continue

            for j, lt in enumerate(self.local_tracks):
                if lt["path"] in matched_local_paths:
                    continue

                p_lt = local_parsed[j]
                path_norm = normalize_text(str(lt.get("path", "")))

                has_artist_tag = any(
                    any(alias in str(a).lower() or alias in unidecode(str(a).lower()) for alias in artist_aliases)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(alias in path_norm for alias in artist_aliases)
                has_rel_match = bool(mb_rel_norm and (mb_rel_norm in path_norm or mb_rel_norm == lt.get("norm_album", "")))

                if not (has_artist_tag or has_artist_path or has_rel_match or mb.get("release_title") == "Standalone / Other"):
                    continue

                ver_compat = are_versions_compatible(
                    p_mb["version_type"], p_mb["version_text"],
                    p_lt["version_type"], p_lt["version_text"]
                )
                if not ver_compat:
                    continue

                base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                full_sim = calculate_similarity(mb["norm_title"], lt.get("norm_title", ""))
                max_sim = max(base_sim, full_sim)

                if max_sim >= 0.85:
                    self.matched[i] = (lt, f"Fuzzy Match ({int(max_sim*100)}%)")
                    matched_mb_indices.add(i)
                    matched_local_paths.add(lt["path"])
                    break

        # Compile found and missing lists
        found_items = []
        missing_items = []

        for i, mb in enumerate(mb_tracks):
            if i in self.matched:
                lt, method = self.matched[i]
                found_items.append({
                    "mb_track": mb,
                    "local_track": lt,
                    "match_method": method
                })
            else:
                missing_items.append({
                    "mb_track": mb
                })

        return found_items, missing_items
