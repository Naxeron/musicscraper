# Media Library Scanning, Tag Extraction & Reconciliation Rules

Guidelines and invariants for audio file discovery, metadata tag parsing, persistent caching, and MusicBrainz discography reconciliation in the `musicscraper` codebase.

## 1. Fast Candidate Discovery & Substring Avoidance
- **Never use naive substring matching** (`needle in string` or `any(x in string for x in list)`) on disk paths or filenames without word boundaries.
- **Word Boundaries**: Always enforce word boundaries (`(?:\b|_)` or tokenization) when matching artist aliases, release names, or track titles.
- **Generic Token & Common Word Exclusion**:
  - Filter out generic track markers (`intro`, `outro`, `interlude`, `untitled`, `bonus`, `track`, `demo`, `mix`, `edit`, `vip`, `theme`, pure digits, single letters).
  - Filter out common single English dictionary words and electronic genre keywords (`breakcore`, `lolicore`, `speedcore`, `hardcore`, `ambient`, `vaporwave`, `rave`) when matching loose standalone filenames.
  - Require standalone track titles to be distinct (e.g. multi-word or length $\ge 8$) unless accompanied by an artist alias or album container match.
- **Compilation Folder Filtering**: Do not blindly classify every audio file in a compilation/VA folder as a candidate for a single queried artist. Only match files in compilation directories if the album title matches a known catalog release or the track filename contains the artist's name.

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
- **Tier 2 (Exact Release + Title)**: Matches track title and album title/path with $\ge 95\%$ confidence.
- **Tier 3 (Artist Alias + Title)**: Matches track title and verifies that artist alias exists in tags or file path.
- **Tier 4 (Fuzzy Match Guardrails)**:
  - Fuzzy matching must **never** attribute tracks to an artist without container or tag verification.
  - Require artist alias confirmation (`has_artist_tag` / `has_artist_path`) or release album confirmation (`has_rel_match`) before accepting a fuzzy match.
  - Standalone tracks without artist or album confirmation require very high direct similarity ($\ge 92\%$) and non-generic title status.

## 5. Network Requests & MusicBrainz Resolution
- **Search Query Caching**: Cache resolved artist queries in `artist_search_cache.json` and check existing `artist_*.json` files before making remote network calls.
- **Explicit Timeouts & IPv4/HTTP Fast Fallbacks**: Use `requests` with explicit timeouts when querying MusicBrainz endpoints to avoid indefinite `urllib` blocking.

## 6. Soulseek & slskd P2P Discography Discovery Invariants
- **Curated High-Yield Query Generation**:
  - Never flood slskd with dozens of granular track searches. Limit artist search generation to 10–15 curated queries (canonical artist name, aliases/alter-egos, missing primary releases, and key compilations).
  - Dispatch searches in controlled chunks (max 4 concurrent) to prevent slskd internal queue starvation (`state: Queued`).
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

## 7. Strict Release, Container & Audio Format Deduplication Invariants
- **Catalog Release Group & Normalized Title Deduplication**:
  - MusicBrainz catalogs must deduplicate release entries by `(is_va, normalize_text(title), release_group_id)`. Multiple editions (e.g. CD vs Bandcamp Web vs Remasters) must not produce duplicate release objects or evaluation loops.
  - Primary releases (`is_va=False`) and compilation releases (`is_va=True`) must be strictly segregated. Primary reconcilers must only evaluate primary releases; compilation reconcilers must evaluate compilations once without re-evaluating primary albums.
- **Cross-Stage Track Coverage & Spillover Prevention**:
  - When a primary release or compilation folder is matched with `match_ratio >= min_match_ratio`, mark all of its constituent track titles as covered in `covered_track_titles`.
  - Standalone reconcilers must never search for leftover track variations of already matched/queued releases to prevent secondary single downloads in alternative formats.
- **Release & Track-Level Queue Deduplication**:
  - Download queue engines must maintain `queued_release_keys` and `queued_track_keys` in addition to directory keys `(user, dir_name)`.
  - Never queue multiple peer directories for the same release across different peers or formats (e.g., FLAC directory vs MP3 directory). Prioritize preferred audio formats (e.g. lossless FLAC) and queue exactly one candidate directory per release.
- **Fuzzy Platform Title Skipping**:
  - Bandcamp, MediaFire, Archive.org, and web scrapers must use fuzzy normalized title matching (stripping `[EP]`, `[LP]`, `Single`, `Remaster`, `VIP`, etc.) against server and queued releases, skipping downloads when $\ge 80\%$ of artist tracks are already present or queued.
- **Intra-Directory Audio Container Conflict Safety**:
  - Stream fallback downloaders (e.g. Bandcamp MP3-128 stream fallback) must check if ANY supported audio file (`.flac`, `.wav`, `.m4a`, `.mp3`, etc.) already exists for that track index/title before writing fallback MP3 stream files.


