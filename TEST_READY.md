# TEST_READY: MusicScraper Hardening E2E Test Suite

## 1. Test Suite Status: READY
The comprehensive 4-Tier E2E test suite for MusicScraper subsystem hardening has been designed, authored, and verified against the current codebase.

- **Status**: Complete & Active (E2E Testing Track)
- **Total Test Cases**: 133
- **Baseline Results**: 124 PASSED, 9 FAILED (expected pending features in M1, M2, M3)
- **Execution Runtime**: ~9.86s
- **Test Infrastructure Specification**: `/home/naxeron/Projects/musicscraper/TEST_INFRA.md`

---

## 2. Test Files Created and Modified

| Test File | Scope / Coverage | Test Tiers | Tests Count | Status |
|---|---|---|---|---|
| `tests/test_reconciler.py` | Dedicated discography reconciler, Roman numerals, diacritics, NFC/NFD, false-positive guardrails, credit formatting, numeric filenames | Tiers 1–4 | 20 | 16 PASS, 4 FAIL (M1 features) |
| `tests/test_auditor.py` | AudioFileScanner filtering, second-pass caching benchmarks, disc metadata tag parsing, multi-disc gap tracking | Tiers 1–4 | 6 | 4 PASS, 2 FAIL (M1 features) |
| `tests/test_soulseek.py` | Candidate deduplication, batch search polling, 5,000-file index scalability benchmark, format quality scoring, selective partial-album queueing, bounded LRU cache | Tiers 1–4 | 11 | 10 PASS, 1 FAIL (M2 feature) |
| `tests/test_web.py` | Web API endpoints, async release audit lifecycle, library_audit and soulseek_download task dispatches, SSE non-primitive serialization safety, cooperative in-flight cancellation | Tiers 1–4 | 65 | 63 PASS, 2 FAIL (M3 features) |
| `tests/test_audio.py` | Core audio tag reading, quality scoring | Tier 1 | 3 | 3 PASS |
| `tests/test_cleaner.py` | Directory cleaner heuristics | Tier 1 | 1 | 1 PASS |
| `tests/test_cli.py` | CLI argument parser | Tier 1 | 1 | 1 PASS |
| `tests/test_library.py` | LibraryReleaseService grouping, audits | Tiers 1–3 | 12 | 12 PASS |
| `tests/test_resolvers.py` | External link resolvers | Tier 1 | 4 | 4 PASS |
| `tests/test_text.py` | Text normalization, kanji, punctuation | Tiers 1–2 | 10 | 10 PASS |

---

## 3. How to Run the Tests

### Full Test Suite
```bash
pytest
```

### By Target Subsystem
```bash
# 1. Discography Reconciler & Guardrails
pytest -v tests/test_reconciler.py

# 2. Scanner, Caching Engine & Multi-Disc Audit
pytest -v tests/test_auditor.py

# 3. Soulseek Scalability, Candidate Matching & LRU Cache
pytest -v tests/test_soulseek.py

# 4. Web API, Background Tasks, SSE & Cancellation
pytest -v tests/test_web.py
```

---

## 4. Pending Implementation Feature Escalation (9 Expected Failures)

The 9 test failures accurately pinpoint the exact architectural improvements scheduled in Milestones M1, M2, and M3:

### Milestone 1: Missing Track Detection & Scan Accuracy
1. `tests/test_reconciler.py::test_unicode_nfc_vs_nfd_normalization`
   - **Feature**: #1 Unicode NFC Normalization
   - **Target**: `src/musicscraper/core/text.py`
   - **Failure**: Decomposed NFD character `\u30ab\u3099` fails to match precomposed NFC `\u30ac` in `normalize_text`.
2. `tests/test_reconciler.py::test_hyphenated_version_descriptors_preservation`
   - **Feature**: #4 Hyphenated Version Descriptors Preservation
   - **Target**: `src/musicscraper/core/text.py:146`
   - **Failure**: `strip_track_number_and_artist("01 Song - Instrumental")` extracts `"Instrumental"` rather than preserving `"Song"`.
3. `tests/test_reconciler.py::test_unbracketed_with_restraint`
   - **Feature**: #6 Feature Pattern Restraint for `with`
   - **Target**: `src/musicscraper/core/text.py:178`
   - **Failure**: Unbracketed `"with"` in `"Stay with Me"` strips `"with Me"` as a feature artist.
4. `tests/test_reconciler.py::test_purely_numeric_track_filename_in_confirmed_album`
   - **Feature**: #12 Purely Numeric Track Filename Matching
   - **Target**: `src/musicscraper/services/reconciler.py`
   - **Failure**: Numeric filenames (`01.flac`, `02.flac`) in confirmed album directory fail to match catalog tracks without title tags.
5. `tests/test_auditor.py::test_audio_metadata_disc_parsing_tags`
   - **Feature**: #8 AudioMetadata Disc Number Tags
   - **Target**: `src/musicscraper/core/audio.py:35`
   - **Failure**: `AudioMetadata` lacks `disc_number: int = 1` and `total_discs: int = 1` dataclass fields.
6. `tests/test_auditor.py::test_disc_subfolder_heuristic_detection`
   - **Feature**: #9 Disc Subfolder Detection
   - **Target**: `src/musicscraper/core/audio.py:270`
   - **Failure**: `AudioQualityAnalyzer.analyze_file` sets album name to `"Disc 1"` when reading files from disc subdirectories.

### Milestone 2: Scan Throughput, Caching Engine & Soulseek Optimization
7. `tests/test_soulseek.py::test_slskd_lru_directory_cache_bounding`
   - **Feature**: #25 Bounded LRU Directory Cache
   - **Target**: `src/musicscraper/clients/slskd.py:47`
   - **Failure**: `SlskdClient._directory_cache` is an unbounded `dict`, storing 600 entries when maxsize is 500.

### Milestone 3: Web GUI Task Operations & Cancellation Reliability
8. `tests/test_web.py::test_tasks_run_library_audit`
   - **Feature**: #26 Web GUI Task Type Registration
   - **Target**: `src/musicscraper/web/api.py:912`
   - **Failure**: `TASK_DISPATCHER` lacks registration for `"library_audit"`, returning HTTP 400.
9. `tests/test_web.py::test_sse_non_primitive_serialization_safety`
   - **Feature**: #28 SSE Event Stream Serialization Safety
   - **Target**: `src/musicscraper/web/server.py:380`
   - **Failure**: `json.dumps` on line 380 lacks `default=str`, raising `TypeError: Object of type PosixPath is not JSON serializable`.

---

## 5. Transition to Implementation Track
With the E2E Test Suite published and verified:
1. `m1_worker` can proceed with implementation of Features 1–14 to resolve the 6 M1 test failures.
2. `m2_worker` can proceed with Features 15–25 to resolve the M2 test failure.
3. `m3_worker` can proceed with Features 26–30 to resolve the 2 M3 test failures.
4. Final Milestone (M4) will verify 100% test pass rate across all 133 tests.
