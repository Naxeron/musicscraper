# Media Library Scanning, Tag Extraction & Reconciliation Rules

Guidelines and invariants for audio file discovery, metadata tag parsing, persistent caching, and MusicBrainz discography reconciliation in the `musicscraper` codebase.

## 1. Fast Candidate Discovery & Substring Avoidance
- **Never use naive substring matching** (`needle in string` or `any(x in string for x in list)`) on disk paths or filenames without word boundaries.
- **Word Boundaries**: Always enforce word boundaries (`(?:\b|_)` or tokenization) when matching artist aliases, release names, or track titles.
- **Generic Token & Common Word Exclusion**:
  - Filter out generic track markers (`intro`, `outro`, `interlude`, `untitled`, `bonus`, `track`, `demo`, `mix`, `edit`, `vip`, `theme`, pure digits, single letters).
  - Filter out common single English dictionary words and electronic genre keywords (`breakcore`, `lolicore`, `speedcore`, `hardcore`, `ambient`, `vaporwave`, `rave`) when matching loose standalone filenames.
  - Require standalone track titles to be distinct (e.g. multi-word or length $\ge 8$) unless accompanied by an artist alias or album container match.
  - **Unified Regex Matching**: In high-latency/SSHFS filesystems, compile combined unified regexes for aliases, release titles, and track titles rather than sequentially evaluating hundreds of regexes per file.
- **Compilation Folder Filtering**: Do not blindly classify every audio file in a compilation/VA folder as a candidate for a single queried artist. Only match files in compilation directories if the album title matches a known catalog release or the track filename contains the artist's name.
- **Folder-Level Candidate Propagation**: If a non-canonical folder (such as inside a downloads directory) contains $\ge 2$ tracks matching catalog items, scan the entire folder as a cohesive album candidate.

## 2. Persistent Audio Tag Caching
- **SQLite Metadata Caching**: Always check `AudioMetadataCache` (`~/.cache/musicscraper/audio_cache.db`) before invoking `mutagen` file reads over disk.
- **Cache Key Invariants**: Cache records must be keyed by `(path, mtime, size)`. Unmodified files must resolve from cache in microseconds to prevent multi-minute blocking disk I/O.
- **Batch Operations**: Perform SQLite cache lookups and writes in chunks/batches.

## 3. Safe Audio Tag Extraction (ID3, Vorbis, MP4)
- **Exact ID3 Frame Matching**:
  - Never use loose substring checks like `'ALBUM' in k_str` or `'TITLE' in k_str` on ID3 tag frames.
  - Match exact frame names (`TALB`, `TIT2`, `TRCK`, `TPE1`, `TPE2`, `UFID`, `TXXX:MusicBrainz...`).
  - Prevent ReplayGain peak/gain frames (`TXXX:replaygain_album_peak = '1.000000'`) from overwriting legitimate album or title tags.
- **Vorbis & MP4 Handling**: Standardize case-insensitive vorbis keys (`TITLE`, `ALBUM`, `ARTIST`, `TRACKNUMBER`) and MP4 atoms (`\xa9nam`, `\xa9alb`, `\xa9ART`, `aART`, `trkn`).

## 4. Multi-Tier Reconciliation & False-Positive Prevention
- **Tier 1 (Exact MBID)**: Highest priority match on Track ID, Release Track ID, or Recording UFID.
- **Tier 2 (Exact Release + Title)**: Matches track title and album title/path with $\ge 88\%$ base title similarity and strict version compatibility.
- **Tier 3 (Artist Alias + Title)**: Matches track title ($\ge 90\%$ base title similarity) and verifies that artist alias exists in tags or file path, with strict version compatibility.
- **Tier 4 (Fuzzy Match Guardrails)**:
  - Fuzzy matching must **never** attribute tracks to an artist without container or tag verification.
  - Require artist alias confirmation (`has_artist_tag` / `has_artist_path`) or release album confirmation (`has_rel_match`) before accepting a fuzzy match.
  - Standalone tracks without artist or album confirmation require very high direct similarity ($\ge 92\%$) and non-generic title status.
  - **Strict Version & Remix Incompatibility**:
    - An original track (`version_type is None`) must **never** match a remix, instrumental, acoustic, live, or speed-up version.
    - Remixes with differing remixers/descriptors (e.g. `WilliamDavi's remix` vs `Vaenus remix`) must **never** match each other.
    - Never bypass version compatibility with unvalidated substring checks (`title_in_tag` / `title_in_path`).
    - Distinguish featured artists (`feat.`, `ft.`) from version modifiers; featured artists do not alter the version type.
    - Treat non-musical sound engineering tags (`[2021 Remaster]`, `[FLAC]`, `[320]`, `(Original Mix)`) as non-destructive tags.

## 5. Multi-Source Candidate Deduplication
- **Cross-Source Merging (Navidrome + Local Disk)**:
  - When scanning both remote API sources (Navidrome / Subsonic) and local filesystem paths, candidate tracks must be deduplicated via `deduplicate_candidate_tracks()` prior to reconciliation.
  - Fingerprint candidate tracks by `(norm_album, norm_title, track_number)`, shared MusicBrainz Recording/Track IDs, and path tails.
  - Merge ID tag sets and retain the physical local filesystem path. Prevent duplicate candidate instances of the same physical file from being left over to falsely satisfy other releases in fuzzy passes.

## 6. Network Requests & MusicBrainz Resolution
- **Search Query Caching**: Cache resolved artist queries in `artist_search_cache.json` and check existing `artist_*.json` files before making remote network calls.
- **Explicit Timeouts & IPv4/HTTP Fast Fallbacks**: Use `requests` with explicit timeouts when querying MusicBrainz endpoints to avoid indefinite `urllib` blocking.

## 7. Soulseek & slskd P2P Discography Discovery Invariants
- **Non-Latin Unicode Script Preservation**:
  - `clean_search_phrase()` must **never** run `unidecode()` on non-Latin scripts (Japanese Kanji/Kana, Cyrillic, Chinese, Korean, Greek, etc.). P2P networks (Soulseek/slskd) preserve native Unicode filenames and tags.
  - Retain Unicode word characters (`re.UNICODE`) and strip only noise punctuation.
- **Dual-Query Generation (User Query + Canonical Name)**:
  - Generate release queries for both the user's input query (`<UserArtist> <Release>`) and the canonical catalog name (`<CanonicalArtist> <Release>`), plus distinctive release titles.
  - Release queries and primary artist names must be prioritized at the head of the search queue. Do not allow obscure aliases to delay release searches.
- **Curated High-Yield Query Generation**:
  - Never flood slskd with dozens of granular track searches. Limit artist search generation to curated queries (canonical artist name, user input query, missing primary releases, and key compilations).
  - Dispatch searches in controlled chunks (e.g. up to 8 concurrent) to balance speed and prevent slskd internal queue starvation (`state: Queued`).
- **slskd Search Polling & Completion Invariants**:
  - In slskd REST API (v0), `GET /api/v0/searches/{id}?includeResponses=true` returns an empty `responses: []` array while `isComplete` is `False` (`state: InProgress`), regardless of `responseCount` or `fileCount`.
  - Always query `GET /api/v0/searches/{id}/responses` as a direct fallback whenever `includeResponses=true` yields an empty responses array. The `/responses` endpoint streams live incoming peer responses in real-time even before search completion.
  - Never terminate search polling loops prematurely based on intermediate response counts or timers before `isComplete` is `True` (or `"Completed" in state`).
  - Newly dispatched (cold) searches must be polled across an adaptive observation window (minimum 5–6s) before evaluating completion, ensuring peer responses on the distributed P2P network are not prematurely truncated.
  - Default search timeouts must be set to $\ge 28\text{s}$ (e.g. 28–30s) for both single-track and batch searches to allow Soulseek distributed network responses to fully arrive and complete.
  - In-progress searches identified during `use_existing` checks must be attached by their existing ID and polled to completion rather than queried immediately for responses or re-dispatched as duplicates.
- **Search Result State & Stale Search Purging**:
  - Clean up stale 0-file/timed-out searches from slskd history before initiating fresh batches.
  - In-progress searches must remain tracked in polling pools.
  - Never overwrite existing search results containing file payloads with empty/queued polling snapshots.
- **Strict Release Directory Matching (`is_dir_name_match`)**:
  - Compilations, EPs, and single-track features must verify that the remote peer directory name matches the release title or catalog code.
  - Prevent unrelated album directories with single bonus remix tracks from being falsely assigned as the release folder.
- **Generic Dump Folder Detection (`is_loose_dump`)**:
  - Detect generic unsorted folders (`dump`, `archive`, `tracks`, `music`, `songs`, `shared`, `root`).
  - Only enqueue the individual matching track file from dump directories, never the entire folder.
- **Safe Enqueueing & Peer Username Encoding**:
  - Always clean and URL-encode peer usernames (`urllib.parse.quote(clean_user, safe="")`) to support special symbols, brackets, and IP suffixes.
  - Chunk download enqueue requests into batches of $\le 50$ files with $\ge 30$s timeout to prevent HTTP timeout errors.
  - Ensure all standard library modules (`re`, `urllib.parse`) are explicitly imported at the top of client modules.

## 8. Strict Release, Container & Audio Format Deduplication Invariants
- **Catalog Release Group & Normalized Title Deduplication**:
  - MusicBrainz catalogs must deduplicate release entries by `(is_va, normalize_text(title), release_group_id)`. Multiple editions (e.g. CD vs Bandcamp Web vs Remasters) must not produce duplicate release objects or evaluation loops.
  - Primary releases (`is_va=False`) and compilation releases (`is_va=True`) must be strictly segregated. Primary reconcilers must only evaluate primary releases; compilation reconcilers must evaluate compilations once without re-evaluating primary albums.
- **Cross-Stage Track Coverage & Spillover Prevention**:
  - When a primary release or compilation folder is matched with `match_ratio >= min_match_ratio`, mark all of its constituent track titles as covered in `covered_track_titles`.
  - Standalone reconcilers must never search for leftover track variations of already matched/queued releases to prevent secondary single downloads in alternative formats.
- **Release & Track-Level Queue Deduplication**:
  - Download queue engines must maintain `queued_release_keys` and `queued_track_keys` in addition to directory keys `(user, dir_name)`.
  - Never queue multiple peer directories for the same release across different peers or formats (e.g., FLAC directory vs MP3 directory). Prioritize preferred audio formats (e.g. lossless FLAC) and queue exactly one candidate directory per release.
  - **Multi-Fingerprint P2P Queue Deduplication**: Remote paths in slskd vary across peers. In addition to exact remote paths, track active and completed transfers by lowercase base filename (e.g., `01 - track.flac`) and normalized title tokens to prevent re-queueing identical files from different peers.
  - **Selective Partial-Album Queueing**: When a release is partially present locally (e.g., 10 of 11 tracks present), download engines must filter candidate directory files to enqueue ONLY the genuinely missing tracks, preventing all-or-nothing full album re-downloads.
  - **Local Library Pre-Seed Coverage Invariant**: Releases identified during local library pre-scan must immediately pre-seed `reconciled_release_keys` and `covered_track_titles` to prevent downstream compilation and standalone passes from re-evaluating and downloading tracks already satisfied locally.
- **Fuzzy Platform Title Skipping**:
  - Bandcamp, MediaFire, Archive.org, and web scrapers must use fuzzy normalized title matching (stripping `[EP]`, `[LP]`, `Single`, `Remaster`, `VIP`, etc.) against server and queued releases, skipping downloads when $\ge 80\%$ of artist tracks are already present or queued.
- **Intra-Directory Audio Container Conflict Safety**:
  - Stream fallback downloaders (e.g. Bandcamp MP3-128 stream fallback) must check if ANY supported audio file (`.flac`, `.wav`, `.m4a`, `.mp3`, etc.) already exists for that track index/title before writing fallback MP3 stream files.

## 9. P2P Candidate Indexing & High-Performance Verification Invariants
- **Inverted Candidate Indexing (`PeerCandidateIndex`)**:
  - Never evaluate releases or tracks via nested $O(N \times M)$ linear scans across thousands of candidate peer directories and files.
  - Construct an inverted dictionary index (`word_to_dirs` mapping tokens $\ge 3$ characters to candidate directory objects, and `word_to_files` mapping tokens to candidate file objects) immediately following discovery.
  - Release and track verification must query the inverted index to prune candidate search spaces from thousands down to $\le 20$ candidates in $<1\text{ms}$.
- **Two-Phase Release Evaluation & Batch Directory Browsing**:
  - **Phase 1**: Perform instant in-memory evaluation on candidate directories populated with search result files.
  - **Phase 2**: If no candidate directory reaches `min_match_ratio`, concurrently batch-browse the top matching candidate directories via `browse_directories_batch()` and re-evaluate.
  - Never invoke synchronous, sequential `browse_directory` calls inside loops across dozens of remote peers.
- **Pre-Parsed Track & Directory Data Structures**:
  - Pre-parse audio format scores, filename normalization, tokens, and structural title parts (base title, version modifiers, remaster descriptors) **once** during index creation (`CandidateFile` / `CandidateDir`).
  - Pre-parse expected catalog tracklists once per release (`pre_parse_expected_tracks()`) rather than re-normalizing per comparison.
- **LRU Caching & Pre-Compiled Regexes for Normalization**:
  - Decorate frequently called string normalization and distance functions (`normalize_text`, `strip_track_number_and_artist`, `parse_track_title_structure`, `calculate_similarity`) with `@lru_cache(maxsize=65536)`.
  - Ensure all arguments to `@lru_cache` functions are strictly hashable (`str`, `tuple`, `frozenset`). Never pass mutable `set` or `dict` objects.
  - Pre-compile all static regular expressions (`re.compile`) at module level.
  - Fast-path identical strings (`if str1 == str2: return 1.0`) to avoid matrix distance calculations.
- **Index Synchronization on Remote Browse**:
  - When fetching complete folder listings from peers via API (`browse_directory`), update the active inverted index via `candidate_index.update_directory(user, dir_name, dir_info)` so that subsequent release and track reconciliation passes immediately benefit from the complete folder file list.

## 10. Multi-Lingual & Numeric Title Normalization
- **Kanji Numeral Parsing**: Catalog track titles with Japanese Kanji numbers (e.g. `九十四`, `一`, `十`) must be converted to Arabic digits (`94`) during normalization to ensure alignment with standard numeric file tags.
- **Purely Numeric Track Title Safety**: Track-number stripping routines (`strip_track_number_and_artist`) must never strip purely numeric titles (e.g., `"94"`, `"1999"`, `"007"`) down to empty strings. If stripping leaves an empty string, fall back to the original non-extension filename.

## 11. Compilation & Various Artists Release Invariants
- **Album Artist & Compilation Flag Extraction**:
  - `AudioQualityAnalyzer` must extract `album_artist` from exact frames (`TPE2`, `ALBUMARTIST`, `aART`) and compilation flags (`TCMP`, `cpil`, `COMPILATION`).
  - Standardize all compilation aliases (`va`, `v.a.`, `various`, `various artists`, `compilation`) to `"Various Artists"`.
  - Normalize typo variations on peer folders and tags (e.g. `"Various Artitsts"`, `"Various Artist"`, `"V. Artists"`) via regex `^v(?:arious|\.)?\s*arti[st]{2,4}s?$`.
- **Contributing Track Independence**:
  - Never allow a single contributing artist track to define the release-level artist on a compilation or multi-artist directory.
  - In compilation releases, individual tracks must always retain their true track-level artist credits (`artist-credit` from tags or MusicBrainz).
- **Soulseek Querying for Compilations**:
  - Prioritize `"Various Artists <Release Title>"` at the head of compilation album searches, followed by `"<Release Title>"`. Avoid prioritizing `"VA"` which frequently yields noisy or zero results on P2P networks.
  - When searching for missing compilation tracks without a known track artist, query `"Various Artists <Track Title>"`, with fallback to `"<Track Title>"`. Never prepend a single contributing artist's name to all missing tracks on a compilation.

## 12. Unified Multi-Folder & Partial-Download Release Grouping
- **Natural Release Key Invariant**:
  - Library scanning must group releases by natural key (`va_{norm_album}` for compilations, `art_alb_{norm_artist}::{norm_album}` for single artists), rather than splitting by directory hash or MBID.
  - Untagged downloads (from Soulseek or Bandcamp) and MBID-tagged local files for the same album must be unified into a single release.
  - If any track on the album contains a `musicbrainz_albumid` tag, propagate `rel["mb_release_id"]` to the unified release so that MusicBrainz reconciliation immediately utilizes it.
- **Folder Precedence & Deduplication**:
  - When audio tracks for an album exist across multiple folders (e.g. `Library/` and `downloads/`), the release's primary `folder_path` must prioritize canonical non-download library paths.
  - Deduplicate multiple file instances of the same track across folders, prioritizing files in `Library/` over `downloads/`.

## 13. Incremental Re-Audit & Client State Synchronization
- **Fast Incremental Rescan on Re-Audit**:
  - Triggering a release re-audit (`force_refresh=True`) must trigger a fast incremental library rescan (`force_disk_rescan=False`) before MusicBrainz reconciliation. This reuses cached SQLite metadata for existing files ($<1\text{s}$) while instantly picking up newly downloaded files on disk.
  - Never reuse stale in-memory cached release objects when the user explicitly requests a re-audit.
- **Frontend ID Resilience & Selection Sync**:
  - When a library rescan or audit completes, the frontend must re-bind `AppState.selectedReleaseId` and `AppState.selectedReleaseData` using fallback matching on `(title, artist)` in case release IDs unify or shift.
  - Re-audits must update both the master release list item and the active details pane without requiring a full page refresh.

## 14. Missing Track Downloader & Placeholder Resolution Invariants
- **Synthetic Gap Placeholder Resolution**:
  - Local library scanning generates synthetic placeholders (`Track XX (Missing)`) for missing sequence numbers. Download engines (`download_single_missing_track`, `download_missing_tracks`) must **never** query P2P networks with synthetic placeholder strings.
  - When a download request contains a placeholder track or track number, automatically invoke `audit_release` to resolve the official title and track-level artist from MusicBrainz before dispatching search queries.
  - If a placeholder track cannot be resolved via MusicBrainz, abort with a clear, actionable error rather than wasting network timeout budgets searching for generic strings like `"Track 01 (Missing)"`.






