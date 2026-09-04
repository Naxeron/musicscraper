# Project: MusicScraper Soulseek & Local Library Audit Hardening

## Architecture
MusicScraper Core, Audio, Search, and Web Subsystems:
- `src/musicscraper/core/text.py`: Normalization, NFC unicode handling, Roman numerals, dash/separator cleaning, feature extraction, and string similarity matching.
- `src/musicscraper/core/audio.py`: Audio file metadata extraction via Mutagen, quality scoring, disc number (`TPOS`/`DISCNUMBER`) tag extraction, and folder hierarchy heuristics.
- `src/musicscraper/core/cache.py`: High-performance SQLite cache (`UnifiedCacheManager`) with WAL mode, thread-local connections, batch writes, and bulk file pre-fetching.
- `src/musicscraper/clients/musicbrainz.py`: MusicBrainz API client and `ArtistCatalog` discography catalog builder with robust multi-artist credit formatting.
- `src/musicscraper/clients/slskd.py`: Slskd REST API client with bounded LRU directory caching and batch search polling.
- `src/musicscraper/services/reconciler.py`: Multi-tier discography reconciler (`DiscographyReconciler`) matching catalog tracks against local/remote files with zero false positives.
- `src/musicscraper/services/library.py`: Local library scanner and release auditor (`LibraryReleaseService`), multi-disc album grouping, gap detection, and missing track detection.
- `src/musicscraper/services/auditor.py`: Missing release and track discovery pipeline (`AuditorService`, `AudioFileScanner`).
- `src/musicscraper/services/soulseek.py`: Soulseek candidate discovery, indexing (`PeerCandidateIndex`, `CandidateDir`, `CandidateFile`), and release/track matching.
- `src/musicscraper/web/server.py`: HTTP server, REST endpoints, and SSE event streaming.
- `src/musicscraper/web/api.py`: Web API controllers, task runner dispatchers (`TASK_DISPATCHER`).
- `src/musicscraper/web/tasks.py`: Background task execution system, state transitions, log isolation, and cooperative cancellation tokens.

## Feature Inventory
Every feature from the Survey phase must appear here with its assigned milestone. No feature may be left unassigned.
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Unicode NFC Normalization | Normalize text via `unicodedata.normalize('NFC', ...)` in `normalize_text`, `clean_search_phrase`, and title parsing | M1 | R1 Survey |
| 2 | Typographic Dash & Separator Cleaning | Support en-dash (`–`), em-dash (`—`), tildes (`~`) and horizontal bars in `strip_track_number_and_artist` | M1 | R1 Survey |
| 3 | Robust Track Number Stripping | Strip leading numbers without punctuation (`10 Song`, `1 Song`, `01 Song`) without mangling short titles | M1 | R1 Survey |
| 4 | Hyphenated Version Descriptors Preservation | Prevent `strip_track_number_and_artist` from replacing song titles with version descriptors | M1 | R1 Survey |
| 5 | Roman Numeral Normalization | Translate Roman numerals (`Part I` vs `Part 1`, `Act II` vs `Act 2`) in titles and parts to prevent false negatives | M1 | R1 Survey |
| 6 | Feature Pattern Restraint for `with` | Restrict `with` to bracketed/parenthetical contexts so titles like *"Stay with Me"* aren't truncated | M1 | R1 Survey |
| 7 | Version Descriptor Compatibility | Support flexible matching for live, acoustic, and instrumental versions without strict 0.80 cutoff | M1 | R1 Survey |
| 8 | AudioMetadata Disc Number Tags | Extract `disc_number` and `total_discs` from ID3 (`TPOS`), Vorbis (`DISCNUMBER`), MP4 (`disk`) | M1 | R1 Survey |
| 9 | Disc Subfolder Detection | Parse `Disc 1`, `CD 01`, `Vinyl` subdirectories, preserving true album and artist while setting `disc_number` | M1 | R1 Survey |
| 10 | Untagged Filename Artist Extraction | Extract artist from `Artist - Title` filenames when tags and parent directory heuristics are missing | M1 | R1 Survey |
| 11 | MusicBrainz Artist Credit Formatting | Fix `format_credit` in `ArtistCatalog` to preserve `joinphrase` (`" & "`, `" feat. "`, `" / "`) and `name` | M1 | R1 Survey |
| 12 | Purely Numeric Track Filename Matching | Allow `01.flac`, `02.flac` in confirmed album folders to match official track numbers without failing similarity | M1 | R1 Survey |
| 13 | Multi-Disc Sequence Gap Tracking | Track missing track gaps by `(disc_number, track_number)` tuple to prevent cross-disc collision | M1 | R1 Survey |
| 14 | Reconciler False-Positive Guardrails | Eliminate standalone bypass in Tier 2, enforce word boundaries on artist path matching, prevent numeric track mismatch | M1 | R1 Survey |
| 15 | SQLite WAL Mode & PRAGMA Tuning | Set `journal_mode = WAL`, `synchronous = NORMAL`, `temp_store = MEMORY` in `UnifiedCacheManager` | M2 | R2 Survey |
| 16 | Thread-Local SQLite Connections | Reuse thread-local connection objects in `_get_conn` instead of opening and closing per file | M2 | R2 Survey |
| 17 | Batch Audio Metadata Storage | Implement `store_audio_metadata_batch` using `executemany` for atomic batch disk writes | M2 | R2 Survey |
| 18 | Bulk Pre-fetch Cache Verification | Implement `get_cached_metadata_for_files` for single-query bulk cache verification on warm scans | M2 | R2 Survey |
| 19 | Syscall Elimination (`os.path.abspath`) | Replace `Path.resolve()` with `os.path.abspath` across cache and scanner, avoiding multi-level `readlink` syscalls | M2 | R2 Survey |
| 20 | High-Performance Directory Discovery | Traverse audio directories via `os.scandir` in `LibraryReleaseService`, capturing `stat` info upfront | M2 | R2 Survey |
| 21 | Mathematical Length Ratio Guard | Implement $2 \min(l_1, l_2) / (l_1 + l_2)$ upper-bound pruning in `calculate_similarity` and `is_track_title_match_fast` | M2 | R2 Survey |
| 22 | Postings List & Stop-Word Pruning | Filter generic common words from `CandidateFile.sig_words` and prune postings list queries in `PeerCandidateIndex` | M2 | R2 Survey |
| 23 | Slim `CandidateFile` Memory Optimization | Remove redundant word lists/sets and unneeded slskd JSON payload attributes from `CandidateFile` | M2 | R2 Survey |
| 24 | Candidate Directory Expansion Consistency | Fix `PeerCandidateIndex.update_directory` so browsed directory files are added to `all_audio_files` and word index | M2 | R2 Survey |
| 25 | Bounded LRU Directory Cache | Replace unbounded `dict` in `SlskdClient._directory_cache` with an `OrderedDict` capped at maxsize 500 | M2 | R2 Survey |
| 26 | Web GUI Task Type Registration | Register `"library_audit"` in `TASK_DISPATCHER` in `src/musicscraper/web/api.py` | M3 | R3 Survey |
| 27 | Async Release Audit Task Lifecycle | Update `POST /api/library/releases/<id>/audit` to support asynchronous task execution with full state transitions | M3 | R3 Survey |
| 28 | SSE Event Stream Serialization Safety | Add `default=str` to `json.dumps` on line 380 of `src/musicscraper/web/server.py` to prevent `TypeError` crashes | M3 | R3 Survey |
| 29 | Cooperative Task Cancellation | Integrate cancellation checks (`task.check_cancelled()` / `is_cancelled`) into long-running loops in library and soulseek | M3 | R3 Survey |
| 30 | Soulseek Download Task Validation | Ensure `soulseek_download` validates parameters and transitions states reliably without crashing worker threads | M3 | R3 Survey |
| 31 | E2E Test Suite & Adversarial Hardening | Complete 5-tier test suite verifying all acceptance criteria and publishing `TEST_READY.md` | E2E, M4 | Acceptance |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Missing Track Detection & Scan Accuracy | Features 1–14: Unicode NFC, dash/separators, track stripping, Roman numerals, bracketed `with`, disc tags, disc folders, multi-artist credits, numeric filenames, gap tracking, reconciler guardrails in `core/text.py`, `core/audio.py`, `clients/musicbrainz.py`, `services/reconciler.py`, `services/library.py` | none | PLANNED |
| M2 | Scan Throughput, Caching Engine & Soulseek Optimization | Features 15–25: SQLite WAL & tuning, thread-local connections, batch storage, bulk pre-fetch, `os.path.abspath`, `os.scandir`, similarity ratio guard, stop-word/postings pruning, slim CandidateFile, directory expansion fix, bounded LRU cache in `core/cache.py`, `core/text.py`, `services/library.py`, `services/soulseek.py`, `clients/slskd.py` | none | PLANNED |
| M3 | Web GUI Task Operations & Cancellation Reliability | Features 26–30: Register `library_audit`, async release audit option, SSE serialization safety, cooperative cancellation in audit/download loops, task validation in `web/server.py`, `web/api.py`, `services/library.py`, `services/soulseek.py` | M1, M2 | PLANNED |
| M4 | Final Milestone: 100% E2E Test Pass & Coverage Hardening | Feature 31: Pass 100% of E2E test suite (Tiers 1-4), followed by Tier 5 adversarial coverage hardening | M1, M2, M3, E2E | PLANNED |

## Interface Contracts
### `core/text.py` ↔ `services/reconciler.py` & `services/library.py`
- `normalize_text(text: str) -> str`: Normalizes string to Unicode NFC, strips non-printable characters, performs lowercase / casefolding.
- `normalize_roman_numerals(text: str) -> str`: Converts Roman numerals (`I`-`XX`) in part/movement indicators to Arabic digits.
- `strip_track_number_and_artist(text: str) -> str`: Handles standard ASCII hyphens, typographic dashes, and leading track numbers without stripping version descriptors.
- `calculate_similarity(str1: str, str2: str) -> float`: Returns `difflib.SequenceMatcher.ratio()` with fast length ratio upper-bound check $(2 \min(|s_1|, |s_2|)) / (|s_1| + |s_2|) \ge 0.60$.

### `core/audio.py` ↔ `services/library.py` & `core/cache.py`
- `AudioMetadata`: Dataclass containing `disc_number: int = 1`, `total_discs: int = 1`, and standard tag fields.
- `AudioQualityAnalyzer.analyze_file(file_path: Path) -> AudioMetadata`: Reads audio tags (`TPOS`, `DISCNUMBER`), infers disc/album/artist from folder heuristics, extracts artist from filename if untagged.

### `core/cache.py` ↔ `services/library.py` & `services/auditor.py`
- `UnifiedCacheManager.get_audio_metadata(file_path: Path) -> Optional[AudioMetadata]`: Uses `os.path.abspath` and thread-local connection.
- `UnifiedCacheManager.store_audio_metadata(meta: AudioMetadata) -> None`: Inserts metadata using thread-local connection in WAL mode.
- `UnifiedCacheManager.store_audio_metadata_batch(metas: List[AudioMetadata]) -> None`: Inserts metadata list in a single atomic transaction.
- `UnifiedCacheManager.get_cached_metadata_for_files(file_paths: List[Path]) -> Dict[str, AudioMetadata]`: Bulk retrieves cached records for given files in batches of 500.

### `web/tasks.py` & `web/api.py` ↔ `services/library.py` & `services/soulseek.py`
- `TASK_DISPATCHER["library_audit"]`: Dispatches library audit task.
- `BackgroundTask.check_cancelled()`: Raises `TaskCancelledException` or sets state if `task.is_cancelled` is True. Long-running service loops call `task.check_cancelled()` or check cancellation token.

## Code Layout
- `src/musicscraper/core/text.py`: String normalization, title parsing, similarity
- `src/musicscraper/core/audio.py`: Metadata extraction, audio analysis, disc number tags
- `src/musicscraper/core/cache.py`: SQLite cache engine, WAL, batch storage, pre-fetch
- `src/musicscraper/clients/musicbrainz.py`: MusicBrainz client, catalog builder, credit formatter
- `src/musicscraper/clients/slskd.py`: Slskd client, bounded LRU directory cache
- `src/musicscraper/services/reconciler.py`: Discography reconciler, candidate matching
- `src/musicscraper/services/library.py`: Local library scanner, release auditor, gap detection
- `src/musicscraper/services/auditor.py`: Missing track/release scanner
- `src/musicscraper/services/soulseek.py`: Candidate indexing, candidate pruning, search reconciler
- `src/musicscraper/web/server.py`: HTTP server, endpoint handling, SSE streaming
- `src/musicscraper/web/api.py`: Controllers, task dispatcher, release audit handler
- `src/musicscraper/web/tasks.py`: Task manager, background tasks, cancellation tokens
- `tests/test_auditor.py`: Library audit, scanning, caching benchmark tests
- `tests/test_soulseek.py`: Soulseek candidate indexing, LRU cache, candidate matching tests
- `tests/test_web.py`: Web API, task execution, SSE streaming, cancellation tests
- `tests/test_reconciler.py`: Edge-case catalog reconciliation tests (diacritics, splits, multi-disc, numbering)
