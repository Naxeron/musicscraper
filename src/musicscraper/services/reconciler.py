"""
Multi-tier discography reconciler comparing MusicBrainz tracks against local library audio files.
"""

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from unidecode import unidecode

from musicscraper.core.text import (
    normalize_text,
    calculate_similarity,
    parse_track_title_structure,
    are_versions_compatible
)
from musicscraper.clients.musicbrainz import ArtistCatalog


ROMAN_TO_ARABIC = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20
}


def extract_title_numbers(title: str) -> List[int]:
    """Extracts numeric values from track titles, converting Roman numerals."""
    if not title:
        return []

    def _replace_roman(match):
        w = match.group(1).lower()
        return f" {ROMAN_TO_ARABIC[w]} " if w in ROMAN_TO_ARABIC else match.group(0)

    converted = re.sub(
        r'\b(?:part|pt|act|movement|vol|volume|chapter|suite|no|track)?\s*([ivx]+)\b',
        _replace_roman,
        title,
        flags=re.IGNORECASE
    )
    return [int(t) for t in re.findall(r'\b\d+\b', converted)]


def have_conflicting_numbers(t1: str, t2: str) -> bool:
    """Returns True if both titles contain numeric sequence tokens and they do not match."""
    nums1 = extract_title_numbers(t1)
    nums2 = extract_title_numbers(t2)
    return bool(nums1 and nums2 and nums1 != nums2)


def have_conflicting_track_numbers(trk1: Any, trk2: Any) -> bool:
    """Returns True if both track numbers are known integers and do not match."""
    if not trk1 or not trk2:
        return False
    d1 = re.sub(r"[^\d]", "", str(trk1).split("/")[-1].split("-")[-1])
    d2 = re.sub(r"[^\d]", "", str(trk2).split("/")[-1].split("-")[-1])
    if d1 and d2:
        try:
            return int(d1) != int(d2)
        except ValueError:
            pass
    return False


def is_purely_numeric_track(lt: Dict[str, Any]) -> bool:
    """Checks if a local track lacks title text and is identified only by track number."""
    raw_title = (lt.get("title") or "").strip()
    raw_fn = Path(lt.get("filename") or lt.get("path") or "").stem.strip()
    if raw_fn.isdigit() or re.match(r"^(?:track|trk)[\s._\-]*\d{1,3}$", raw_fn, re.IGNORECASE):
        if not raw_title or raw_title.isdigit() or re.match(r"^(?:track|trk)[\s._\-]*\d{1,3}$", raw_title, re.IGNORECASE):
            return True
    if raw_title.isdigit() and not re.search(r"[a-zA-Z]", raw_fn):
        return True
    return False


def is_track_number_match(num1: Any, num2: Any) -> bool:
    """Compares track numbers handling zero-padding, vinyl notation (A1), and disc-track (1-01)."""
    if num1 is None or num2 is None:
        return False
    s1 = str(num1).strip().lower()
    s2 = str(num2).strip().lower()
    if not s1 or not s2:
        return False
    if s1 == s2:
        return True
    d1 = re.sub(r"[^\d]", "", s1.split("/")[-1].split("-")[-1])
    d2 = re.sub(r"[^\d]", "", s2.split("/")[-1].split("-")[-1])
    if not d1:
        d1 = re.sub(r"[^\d]", "", s1)
    if not d2:
        d2 = re.sub(r"[^\d]", "", s2)
    if d1 and d2:
        try:
            return int(d1) == int(d2)
        except ValueError:
            pass
    return False


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

        # Pre-compile word boundary patterns for artist aliases
        self.artist_alias_patterns = []
        for alias in self.catalog.aliases:
            norm_alias = normalize_text(alias)
            if norm_alias:
                pat = re.compile(rf"(?<![^\W_]){re.escape(norm_alias)}(?![^\W_])")
                self.artist_alias_patterns.append((norm_alias, pat))

    def reconcile(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Matches MusicBrainz tracks against local library audio files.
        Returns (found_items, missing_items).
        """
        matched_mb_indices: Set[int] = set()
        matched_local_paths: Set[str] = set()
        mb_tracks = self.catalog.tracks

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

                # Check release container match
                all_rel_norms = [normalize_text(r) for r in mb.get("all_releases", set())] if mb.get("all_releases") else [mb_rel_norm]
                if mb_rel_norm and mb_rel_norm not in all_rel_norms:
                    all_rel_norms.append(mb_rel_norm)

                rel_sim = max([calculate_similarity(r, lt_album_norm) for r in all_rel_norms], default=0.0)
                path_has_rel = any(r and r in path_norm for r in all_rel_norms)
                rel_confirmed = rel_sim > 0.7 or path_has_rel

                # Support purely numeric tracks (e.g. 01.flac) in confirmed albums
                is_num_lt = is_purely_numeric_track(lt)
                if is_num_lt and rel_confirmed:
                    lt_trk = lt.get("track_number") or Path(lt.get("filename", "")).stem
                    if is_track_number_match(lt_trk, mb.get("track_number")):
                        self.matched[i] = (lt, "Numeric Track in Confirmed Album")
                        matched_mb_indices.add(i)
                        matched_local_paths.add(lt["path"])
                        break

                # Guardrails against numbered track conflict
                has_num_conflict = have_conflicting_numbers(p_mb["base_norm"], p_lt["base_norm"])
                has_trk_conflict = (
                    p_mb["base_norm"] != p_lt["base_norm"]
                    and have_conflicting_track_numbers(mb.get("track_number"), lt.get("track_number"))
                )
                if has_num_conflict or has_trk_conflict:
                    continue

                base_sim = calculate_similarity(p_mb["base_norm"], p_lt["base_norm"])
                base_match = (p_mb["base_norm"] == p_lt["base_norm"]) or base_sim > 0.90
                ver_compat = are_versions_compatible(
                    p_mb["version_type"], p_mb["version_text"],
                    p_lt["version_type"], p_lt["version_text"]
                )

                if base_match and ver_compat and rel_confirmed:
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
                    any(norm_alias == normalize_text(str(a)) or bool(pat.search(normalize_text(str(a))))
                        for norm_alias, pat in self.artist_alias_patterns)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(
                    bool(pat.search(path_norm))
                    for _, pat in self.artist_alias_patterns
                )

                if has_artist_tag or has_artist_path or (mb.get("recording_ids") and any(rid in lt["mb_rec_ids"] for rid in mb["recording_ids"])):
                    has_num_conflict = have_conflicting_numbers(p_mb["base_norm"], p_lt["base_norm"])
                    has_trk_conflict = (
                        p_mb["base_norm"] != p_lt["base_norm"]
                        and have_conflicting_track_numbers(mb.get("track_number"), lt.get("track_number"))
                    )
                    if has_num_conflict or has_trk_conflict:
                        continue

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

                if is_purely_numeric_track(lt):
                    continue

                p_lt = local_parsed[j]
                path_norm = normalize_text(str(lt.get("path", "")))

                has_artist_tag = any(
                    any(norm_alias == normalize_text(str(a)) or bool(pat.search(normalize_text(str(a))))
                        for norm_alias, pat in self.artist_alias_patterns)
                    for a in lt.get("artists", [])
                )
                has_artist_path = any(
                    bool(pat.search(path_norm))
                    for _, pat in self.artist_alias_patterns
                )
                has_rel_match = bool(mb_rel_norm and (mb_rel_norm in path_norm or mb_rel_norm == lt.get("norm_album", "")))

                if not (has_artist_tag or has_artist_path or has_rel_match):
                    continue

                has_num_conflict = (
                    have_conflicting_numbers(p_mb["base_norm"], p_lt["base_norm"])
                    or have_conflicting_numbers(mb.get("norm_title", ""), lt.get("norm_title", ""))
                )
                has_trk_conflict = (
                    p_mb["base_norm"] != p_lt["base_norm"]
                    and have_conflicting_track_numbers(mb.get("track_number"), lt.get("track_number"))
                )
                if has_num_conflict or has_trk_conflict:
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
