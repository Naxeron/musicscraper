# E2E Test Infrastructure: MusicScraper Audit & Soulseek Hardening

## 1. Overview & Test Philosophy
This document establishes the end-to-end (E2E) testing infrastructure, methodology, and verification criteria for the MusicScraper subsystem hardening project (`PROJECT.md`).

### Test Principles
1. **Opaque-Box & Requirement-Driven**: Tests assert observable external behavior, interface contracts, HTTP responses, SQLite cache performance, and reconciliation accuracy rather than internal implementation details.
2. **Deterministic & Isolated**: Each test provisions its own state (via `tmp_path`, synthetic catalogs, or in-memory caches), does not depend on test execution order, and tears down all temporary resources.
3. **Progressive Testability**: Tests are organized across 4 rigorous tiers from isolated unit feature tests to full real-world scenario simulations.
4. **Adversarial & Guardrail Verification**: Zero false positives and zero false negatives on missing tracks, including malicious or edge-case filenames, unicode divergences, and concurrent network disruptions.

---

## 2. Test Architecture & Runner Setup

### Test Execution Commands
- **Full Suite**:
  ```bash
  pytest -v
  ```
- **Targeted Subsystems**:
  ```bash
  # Discography Reconciliation & Matching Guardrails
  pytest -v tests/test_reconciler.py

  # Library Auditing, Metadata Extraction & Caching Benchmarks
  pytest -v tests/test_auditor.py

  # Soulseek Discovery, Candidate Indexing & Cache Bounds
  pytest -v tests/test_soulseek.py

  # Web API, Background Tasks & SSE Event Streaming
  pytest -v tests/test_web.py
  ```

### Directory Structure & Test Placement
```
tests/
├── __init__.py
├── test_audio.py        # Mutagen extraction, quality scoring
├── test_auditor.py      # Scanner filtering, caching benchmarks, multi-disc gap analysis
├── test_cleaner.py      # Folder cleaner heuristics
├── test_cli.py          # CLI command parser and dispatch
├── test_library.py      # LibraryReleaseService grouping, audits, downloads
├── test_reconciler.py   # Multi-tier reconciler, Roman numerals, diacritics, false-positive guardrails
├── test_resolvers.py    # URL and link resolution
├── test_soulseek.py     # Candidate deduplication, candidate indexing scalability, LRU cache bounding
├── test_text.py         # Text normalization, kanji, punctuation stripping
└── test_web.py          # Web API routes, task lifecycles, SSE streaming, in-flight cancellation
```

---

## 3. 4-Tier Test Methodology

### Tier 1: Feature Coverage (Isolated Happy-Path Tests)
Verifies that individual features, functions, endpoints, and data transformations work correctly under normal operating inputs.
- **Coverage**:
  - Exact MusicBrainz ID matching (Tier 1 reconciler).
  - Clean title and release matching (Tier 2 reconciler).
  - Basic Unicode text normalization and search phrase cleaning.
  - Audio metadata extraction from standard MP3 and FLAC tags.
  - Basic SQLite cache storage and retrieval.
  - Web API standard GET/POST routes and task submission.

### Tier 2: Boundary & Corner Cases
Verifies system behavior under extreme, degenerate, or tricky edge conditions.
- **Coverage**:
  - Unicode NFD vs NFC normalizations (e.g. Japanese Hiragana/Katakana, dakuten `ガ` vs `カ` + `゛`).
  - Polish, French, German, and Spanish diacritics (`Łódź`, `Café`, `Über`).
  - Roman numerals in titles, parts, and acts (`Part I` vs `Part 1`, `Act II` vs `Act 2`).
  - Numbering variations (leading zeros `01 Song`, no zero `10 Song`, track number prefixes `1-05`, pure digits `94`, `1999`).
  - Typographic dashes (en-dash `–`, em-dash `—`, tildes `~`, horizontal bars).
  - Hyphenated version descriptors (`Song - Instrumental`, `Song - Live`) preserving root title.
  - Restraint on `with`: unbracketed (`Stay with Me`) preserved vs bracketed (`(with Alice)`) extracted.
  - Audio metadata disc tags: ID3 `TPOS`, Vorbis `DISCNUMBER`, MP4 `\xa9disk`/`disk`.
  - Disc subfolder heuristics: `Disc 1`, `CD 02`, `Vinyl A` subdirectories.
  - Missing and untagged filenames (`Artist - Title.mp3` artist extraction).
  - False-positive prevention: differing track numbers (`Track 1` vs `Track 2`) must never match.
  - Incompatible version prevention: Original must never match Remix, Instrumental, or Acoustic.
  - Differing remixer protection: `Song (Alice Remix)` must never match `Song (Bob Remix)`.
  - Slskd LRU cache bound: verifying cache is capped at maxsize 500.

### Tier 3: Cross-Feature Combinations
Tests interaction between multiple features operating simultaneously.
- **Coverage**:
  - Split albums combined with Unicode diacritics and multi-artist credits.
  - Multi-disc releases with missing tracks on Disc 1 while Disc 2 is complete, tracking gaps by `(disc_number, track_number)`.
  - Purely numeric filenames (`01.flac`, `02.flac`) in confirmed album folders matching catalog tracks.
  - Untagged files inside disc subdirectories (`Album/Disc 2/05.flac`).
  - Multi-artist credits with join phrases (`"Artist A & Artist B feat. Artist C"`) matching catalog tracks across compilations.
  - Soulseek candidate indexing with large candidate pools containing overlapping words and stop-words.
  - Selective download queueing for partial albums (downloading only missing tracks, preserving local files).

### Tier 4: Real-World Application Scenarios
Tests end-to-end workflows matching actual user production usage.
- **Coverage**:
  - Full synthetic discography audit: comparing multi-album discography against synthetic library directories with complete, partial, and missing releases.
  - Library scan caching benchmark: asserting warm second-pass scan executes significantly faster than cold initial scan.
  - Web GUI asynchronous task lifecycle: `POST /api/library/releases/<id>/audit` and `POST /api/tasks/run` transitioning `pending` -> `running` -> `completed` / `failed`.
  - Real-time SSE event streaming: verifying event generation and non-primitive JSON serialization safety (`default=str`).
  - Cooperative in-flight task cancellation: verifying workers stop promptly upon `POST /api/tasks/<id>/cancel` without deadlocks or state corruption.

---

## 4. Feature Inventory Mapping to Test Tiers

| # | Feature | Description | Milestone | Primary Test Tier | Test Location |
|---|---------|-------------|-----------|-------------------|---------------|
| 1 | Unicode NFC Normalization | Normalize text via `unicodedata.normalize('NFC', ...)` | M1 | Tier 2 | `tests/test_reconciler.py`, `tests/test_text.py` |
| 2 | Typographic Dash Cleaning | Support en-dash (`–`), em-dash (`—`), tildes (`~`) | M1 | Tier 2 | `tests/test_reconciler.py`, `tests/test_text.py` |
| 3 | Track Number Stripping | Strip `10 Song`, `01 Song`, `1 Song` without mangling | M1 | Tier 2 | `tests/test_reconciler.py`, `tests/test_text.py` |
| 4 | Hyphenated Descriptors | Prevent version descriptors from replacing titles | M1 | Tier 2 | `tests/test_reconciler.py`, `tests/test_text.py` |
| 5 | Roman Numeral Normalization | Translate Roman numerals (`I`-`XX`) in titles and parts | M1 | Tier 2 | `tests/test_reconciler.py` |
| 6 | Bracketed `with` Restraint | Restrict `with` to bracketed contexts (`Stay with Me`) | M1 | Tier 2, Tier 3 | `tests/test_reconciler.py` |
| 7 | Version Descriptors | Support flexible matching for live, acoustic versions | M1 | Tier 2, Tier 3 | `tests/test_reconciler.py` |
| 8 | AudioMetadata Disc Tags | Extract `disc_number`, `total_discs` from ID3/Vorbis/MP4 | M1 | Tier 2 | `tests/test_auditor.py`, `tests/test_audio.py` |
| 9 | Disc Subfolder Detection | Parse `Disc 1`, `CD 01`, preserving album/artist | M1 | Tier 2, Tier 3 | `tests/test_auditor.py`, `tests/test_library.py` |
| 10 | Untagged Filename Artist | Extract artist from `Artist - Title` filenames | M1 | Tier 2 | `tests/test_reconciler.py`, `tests/test_audio.py` |
| 11 | MB Artist Credit Formatting | Fix `format_credit` to preserve `joinphrase` and `name` | M1 | Tier 1, Tier 3 | `tests/test_reconciler.py` |
| 12 | Numeric Track Filename Match | Match `01.flac`, `02.flac` in confirmed album folders | M1 | Tier 2, Tier 3 | `tests/test_reconciler.py` |
| 13 | Multi-Disc Gap Tracking | Track gaps by `(disc_number, track_number)` tuple | M1 | Tier 2, Tier 3 | `tests/test_auditor.py`, `tests/test_library.py` |
| 14 | Reconciler Guardrails | Prevent numeric mismatch, version mismatch, loose leaks | M1 | Tier 2, Tier 3 | `tests/test_reconciler.py` |
| 15 | SQLite WAL Mode & Tuning | Enable WAL mode, NORMAL synchronous, MEMORY temp_store | M2 | Tier 1, Tier 4 | `tests/test_auditor.py` |
| 16 | Thread-Local SQLite Conn | Reuse thread-local connections in cache manager | M2 | Tier 1, Tier 4 | `tests/test_auditor.py` |
| 17 | Batch Audio Metadata Storage| Implement `store_audio_metadata_batch` atomic writes | M2 | Tier 1, Tier 4 | `tests/test_auditor.py` |
| 18 | Bulk Pre-fetch Cache Verif | Implement `get_cached_metadata_for_files` pre-fetch | M2 | Tier 1, Tier 4 | `tests/test_auditor.py` |
| 19 | Syscall Elimination | Replace `Path.resolve()` with `os.path.abspath` | M2 | Tier 4 | `tests/test_auditor.py` |
| 20 | High-Perf Directory Discovery| Traverse directories via `os.scandir` | M2 | Tier 4 | `tests/test_auditor.py` |
| 21 | Length Ratio Similarity Guard| Mathematical $2 \min(l_1, l_2) / (l_1 + l_2)$ pruning | M2 | Tier 2, Tier 4 | `tests/test_soulseek.py` |
| 22 | Stop-Word Pruning | Filter generic words and prune postings queries | M2 | Tier 2, Tier 4 | `tests/test_soulseek.py` |
| 23 | Slim CandidateFile Memory | Prune redundant word collections from `CandidateFile` | M2 | Tier 4 | `tests/test_soulseek.py` |
| 24 | Candidate Dir Expansion | Update `all_audio_files` on directory expansion | M2 | Tier 3 | `tests/test_soulseek.py` |
| 25 | Bounded LRU Directory Cache | Cap `SlskdClient._directory_cache` at 500 items | M2 | Tier 2, Tier 4 | `tests/test_soulseek.py` |
| 26 | Web Task Registration | Register `"library_audit"` in `TASK_DISPATCHER` | M3 | Tier 1, Tier 4 | `tests/test_web.py` |
| 27 | Async Release Audit Task | Update release audit route for async task lifecycle | M3 | Tier 1, Tier 4 | `tests/test_web.py` |
| 28 | SSE Serialization Safety | Add `default=str` to `json.dumps` in SSE events | M3 | Tier 2, Tier 4 | `tests/test_web.py` |
| 29 | Cooperative Cancellation | Check cancellation in library and soulseek loops | M3 | Tier 3, Tier 4 | `tests/test_web.py` |
| 30 | Soulseek Download Validation | Validate download task parameters cleanly | M3 | Tier 2, Tier 4 | `tests/test_web.py` |
| 31 | E2E Hardening & Acceptance | 100% pass across all test tiers and benchmarks | M4 | All Tiers | All Test Suites |

---

## 5. Verification Invariants & Pass Criteria

1. **Reconciler Guardrail Invariants**:
   - Zero False Negatives: Missing tracks must not be skipped due to Unicode diacritics, Roman numerals, or dash styling.
   - Zero False Positives: `Track 1` must never match `Track 2`; Original tracks must never match Remixes or Instrumentals; standalone generic tracks (`Intro`) without artist/album tags must never match catalog items.
2. **Scanning Benchmark Invariants**:
   - The warm cache scan (second-pass) on an audio directory must be significantly faster than the cold initial scan, bypassing Mutagen audio tag parsing.
3. **Memory & Scalability Invariants**:
   - `SlskdClient._directory_cache` must never exceed 500 entries.
   - `PeerCandidateIndex` must process and query 5,000+ files without unbounded memory consumption or quadratic latency.
4. **Web API & Task Invariants**:
   - API endpoints reject invalid inputs with HTTP 400 and consistent JSON payload (`{"error": str, "success": false}`).
   - SSE stream never crashes with `TypeError` when serializing non-primitive objects.
   - In-flight cancellation cleanly transitions task to `cancelled` and terminates worker loops.
