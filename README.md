# MusicScraper

A fast, versatile Python toolkit for music collectors and archivers:
1. **Apply Intelligent Last.fm Genre Tags**: Queries Last.fm community tags for tracks, albums, and artists, filters out non-genre noise, canonicalizes formatting/casing (`IDM`, `J-Core`, `Breakcore`, `Lo-Fi`, etc.), and tags audio files (`MP3`, `FLAC`, `M4A`, `OGG`, `WAV`) with SQLite caching and fast multithreading.
2. **Download & Audit Artist Discographies from MusicBrainz**: Automatically queries MusicBrainz for an artist's full catalog (primary releases, VA compilations, splits, and standalone recordings), discovers downloadable releases across Bandcamp, MediaFire, Archive.org, and netlabels, downloads & unpacks them into project folders, cross-references downloaded audio against MusicBrainz, and generates comprehensive missing-track audit reports.
3. **Audit local music libraries** for missing tracks and albums against MusicBrainz discography data (supporting aliases, transliterations, VA compilations, and network/SSHFS libraries).
4. **Download Bandcamp releases and artist discographies** natively (supporting high-resolution free downloads in FLAC/MP3-320/WAV, streaming fallback in MP3-128, automatic ID3 & artwork tagging, and zero-duplicate manifest tracking).
5. **Scrape and download free music releases** from netlabels and websites without duplicates (supporting **Bandcamp**, **MediaFire**, **Archive.org**, and direct audio/archive links).
6. **Clean up empty & non-music folders** automatically.

### Dedicated Support Out-of-the-Box:
- **[Last.fm Genre Tagger](https://www.last.fm)**: Multi-level genre resolution (track -> album -> artist cascading), noise blacklist filtering, canonical casing, and multi-format audio tagging.
- **[MusicBrainz Artist Downloader & Auditor](https://musicbrainz.org)**: Complete discography discovery, multi-provider downloading (Bandcamp, MediaFire, Archive.org, Netlabels), archive unpacking, and missing track audit reporting.
- **[Bandcamp](https://bandcamp.com)**: Full artist discographies, albums, and tracks with embedded metadata.
- **[Dochakuso Records](https://dochakuso.net/release.html)** (MediaFire releases)
- **[Otherman Records](https://www.otherman-records.com/releases)** (Archive.org releases)
- **Any Custom Website** (Bandcamp, MediaFire, Archive.org, direct `.zip`, `.flac`, `.mp3` links)

---

## Installation

Ensure Python 3.8+ is installed, then install dependencies:

```bash
pip install -r requirements.txt
```

*(Zero external virtual environments or heavy dependencies required. All tools run on standard Python with `requests`, `beautifulsoup4`, `mutagen`, `musicbrainzngs`, `unidecode`, `rich`, and `tqdm`.)*

---

## Unified Dispatcher (`main.py`)

Run any tool in the suite through a single clean command:

```bash
# Tag music files with Last.fm genre tags (preview with dry-run)
python3 main.py tag "/mnt/music/Library/goreshit" --dry-run
python3 main.py tag "/mnt/music/Library" --skip-existing

# Download and audit full artist discography using MusicBrainz
python3 main.py artist "96-glass"
python3 main.py artist "https://musicbrainz.org/artist/2a7276cf-e768-4e7e-bf71-be7468d3604f"

# Audit local/server library against MusicBrainz
python3 main.py audit "Stellabee" -d /mnt/music

# Download Bandcamp discography
python3 main.py bandcamp goreshit -f flac

# Scrape web netlabel releases
python3 main.py scrape https://dochakuso.net/release.html

# Clean empty/non-music directories
python3 main.py clean ./downloads --force
```

---

## 1. Last.fm Intelligent Genre Tagger (`lastfm_genre_tagger.py`)

Automatically queries Last.fm community metadata to accurately tag artists, albums, and tracks with clean, canonical genre tags.

### Key Capabilities:
- **Hierarchical Cascading**: Queries `Track Tags` -> `Album Tags` -> `Artist Tags`. If a track has specific tags, they take priority; otherwise, it smoothly falls back or supplements with album and artist genres.
- **Tag Blending**: Supports `--strategy blend` to combine weighted scores from track, album, and artist tags.
- **Smart Noise Filtering & Normalization**: Automatically strips subjective ratings (`favourite`, `guilty pleasure`), formats (`vinyl`, `flac`, `cd`), dates/years (`90s`, `2020`), emoticons (`:3`), and artist/title repeats.
- **Canonical Formatting & Aliases**: Standardizes casing and naming for tricky genres (`IDM`, `EDM`, `J-Core`, `Breakcore`, `Speedcore`, `Happy Hardcore`, `Drum and Bass`, `Lo-Fi`, `R&B`, `Hip-Hop`, `Lolicore`, `Mashcore`, `Extratone`, `Synthwave`, `Vaporwave`, etc.).
- **Multi-Format Audio Support**: MP3 (ID3v2 `TCON`), FLAC (Vorbis `GENRE`), M4A (`©gen`), OGG / Opus (`GENRE`), WAV / AIFF (`TCON`).
- **High Performance**: Built-in persistent SQLite query caching (`~/.cache/musicscraper/lastfm_tags_cache.sqlite`) and multithreaded scanning.
- **Safe Modes**: `--dry-run` preview, `--skip-existing` (only tag empty files), `--append` (combine with existing tags), or `--overwrite`.

### Usage Examples

#### Preview Genre Tags on an Artist Folder (Dry-Run)
```bash
python3 main.py tag "/mnt/music/Library/goreshit" --dry-run
```

#### Apply Genre Tags to Untagged Files Only
```bash
python3 main.py tag "/mnt/music/Library" --skip-existing
```

#### Append Last.fm Genres to Existing File Tags
```bash
python3 main.py tag "/mnt/music/Library/Wan Bushi" --append
```

#### Tag with Blended Weights and Custom Separator
```bash
python3 main.py tag "/mnt/music/Library/Nizikawa" --strategy blend --limit 4 --separator " / "
```

#### Direct Query Mode (Inspect Last.fm Tags Without Modifying Files)
```bash
python3 main.py tag --artist "goreshit" --album "dancefloor degrader" --track "all alone"
```

### Command-Line Options:
| Flag | Default | Description |
|---|---|---|
| `path` | `None` | Path to audio file, album folder, artist folder, or library root |
| `--dry-run`, `-d` | `False` | Preview proposed genre tag changes without modifying files |
| `--skip-existing` | `False` | Only tag files that currently have no genre metadata |
| `--append` | `False` | Preserve existing genres and append newly discovered Last.fm genres |
| `--overwrite` | `True` | Overwrite existing genre tags with fresh Last.fm genres |
| `--strategy`, `-s` | `cascade` | Tag resolution strategy: `cascade` (Track->Album->Artist), `blend`, `artist`, `album`, or `track` |
| `--limit`, `-n` | `3` | Maximum number of genre tags to write per track |
| `--min-count`, `-m` | `5` | Minimum Last.fm tag count/score to accept (1-100) |
| `--separator` | `; ` | Separator string used when joining multiple genres |
| `--multi-value` | `False` | Write multi-value genre tags instead of a joined string for FLAC/Vorbis/ID3 |
| `--allow-nationality`| `False` | Allow nationality/country tags (e.g. Japanese, British, Belgian) |
| `--allow-vocals` | `False` | Allow vocal classifiers (e.g. Female Vocalists, Male Vocalists) |
| `--threads`, `-t` | `8` | Number of concurrent worker threads |
| `--api-key` | `None` | Custom Last.fm API key (or set `LASTFM_API_KEY` env var) |
| `--no-cache` | `False` | Bypass SQLite query cache |
| `--clear-cache` | `False` | Clear local SQLite query cache |

---

## 2. Artist Downloader & Auditor (`artist_downloader.py`)

Automatically downloads as many songs as possible for a given artist using MusicBrainz catalog data and generates a comprehensive missing track report:

```bash
# Download artist discography and audit missing tracks
python3 main.py artist "96-glass"

# Download with preferred audio format (for free Bandcamp downloads: flac, mp3-320, wav, etc.)
python3 main.py artist "Stellabee" -f flac

# Cross-reference with existing music library (READ-ONLY) to verify missing tracks
python3 main.py artist "96-glass" -d /mnt/music

# Dry-run to inspect catalog and discovered download sources without downloading
python3 main.py artist "96-glass" --dry-run
```

### Generated Reports:
- `<artist>_audit_report.md`: Markdown document detailing downloaded tracks and full missing tracks checklist.
- `<artist>_missing_tracks.txt`: Plain text list of missing tracks formatted for quick checking.
- `<artist>_audit.json`: Structured JSON audit data.
- `<artist>_audit.csv`: CSV spreadsheet of discography coverage.

---

## 3. Bandcamp Scraper (`bandcamp_scraper.py`)

Fast, native Bandcamp release and discography downloader. Automatically extracts high-resolution downloads (FLAC, MP3-320, WAV, etc.) when free downloads are available, falls back to streaming audio (MP3-128) when enabled, fetches album artwork, writes ID3 metadata with `mutagen`, and tracks downloads in `manifest.json`.

### Usage Examples

#### Download Full Artist Discography by Subdomain or URL
```bash
python3 bandcamp_scraper.py goreshit
python3 bandcamp_scraper.py https://goreshit.bandcamp.com
```

#### Download a Specific Album or Track
```bash
python3 bandcamp_scraper.py https://goreshit.bandcamp.com/album/defective-beats-rough-cuts
python3 bandcamp_scraper.py https://goreshit.bandcamp.com/track/daddy-ft-shred-wilson
```

#### Batch Download from a List of URLs in a File
```bash
python3 bandcamp_scraper.py -i urls.txt -o ./music -t 4
```

#### Choose Preferred Audio Format for Free Downloads
```bash
# Formats: flac, mp3-320, wav, aac-hi, aiff-lossless, alac, vorbis, mp3-v0, mp3-128
python3 bandcamp_scraper.py goreshit -f flac
```

#### Preview Releases Without Downloading (Dry Run)
```bash
python3 bandcamp_scraper.py goreshit --dry-run
```

#### Download Name Your Price Releases via Email or Free Streaming Fallback
```bash
# Request high-res (FLAC/MP3-320) ZIP links to your email for Name Your Price releases:
python3 main.py bandcamp https://jwrecords.bandcamp.com/ --email myemail@example.com

# Direct download with MP3-128 stream fallback (default):
python3 main.py bandcamp https://jwrecords.bandcamp.com/
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `targets` | *(optional)* | Artist subdomains, artist URLs, album URLs, track URLs, or download URLs |
| `-i`, `--input` | `None` | Text file containing Bandcamp URLs (one per line) |
| `-o`, `--output-dir` | `./downloads` | Destination directory for downloads |
| `-f`, `--format` | `mp3-320` | Preferred audio format for free downloads (`flac`, `mp3-320`, `wav`, etc.) |
| `--email` | `BANDCAMP_EMAIL` | Email address to request high-res links for Name Your Price releases |
| `--country` | `US` | Country code for email download requests |
| `--postcode` | `90210` | Postal code for email download requests |
| `--no-fallback` | `False` | Disable fallback to MP3-128 streams if direct free download is not offered |
| `-t`, `--threads` | `3` | Concurrent worker threads |
| `--dry-run` | `False` | Inspect metadata and list discovered releases without downloading |
| `--overwrite` | `False` | Force redownload even if files already exist on disk |
| `-v`, `--verbose` | `False` | Enable debug logs |

---

## 4. Missing Tracks Checker (`check_missing_tracks.py`)

Cross-references your local music library (e.g. `/mnt/music`) against MusicBrainz discography data to detect missing tracks, albums, compilations, and standalone recordings for any artist. Discovers official Bandcamp pages and allows exporting URLs.

### Usage Examples

#### Audit by Japanese Artist Name or Alias
```bash
python3 check_missing_tracks.py "すてらべえ"
python3 check_missing_tracks.py "Stellabee" -d /mnt/music
```

#### Audit by MusicBrainz URL or MBID
```bash
python3 check_missing_tracks.py "https://musicbrainz.org/artist/2dbd3954-9bb7-4165-9445-98f66c3861bf"
```

#### Show Only Missing Tracks
```bash
python3 check_missing_tracks.py "Stellabee" --only-missing
```

#### Export Bandcamp Links for Missing Releases
```bash
python3 check_missing_tracks.py "goreshit" --export-bandcamp-links goreshit_bc.txt
# Then download directly with:
python3 bandcamp_scraper.py -i goreshit_bc.txt
```

#### Export Structured Results
```bash
python3 check_missing_tracks.py "Stellabee" \
  --export-json stellabee_audit.json \
  --export-csv stellabee_audit.csv \
  --export-txt stellabee_missing.txt
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `artist` | *(required)* | Artist Name, MBID UUID, or MusicBrainz Artist URL |
| `-d`, `--dir`, `--music-dir` | `/mnt/music` | Path to local or mounted music library directory |
| `--only-missing` | `False` | Display only missing tracks and incomplete releases in the output |
| `--only-found` | `False` | Display only found tracks in the output |
| `--export-bandcamp-links` | `None` | Export artist Bandcamp URLs to a text file (feedable into `bandcamp_scraper.py -i`) |
| `--export-json` | `None` | Export full structured audit results to a JSON file |
| `--export-txt` | `None` | Export a clean text list of missing tracks to a file |
| `--export-csv` | `None` | Export audit results to a CSV spreadsheet |
| `--full-scan` | `False` | Deep-scan every audio file in the library instead of fast path pre-filtering |
| `-t`, `--threads` | `24` | Number of parallel worker threads for reading metadata tags |
| `--cache-dir` | `~/.cache/musicscraper/mb_cache` | Directory to store MusicBrainz cache files |
| `--refresh-cache` | `False` | Force refresh MusicBrainz API cache for this artist |
| `--no-cache` | `False` | Disable caching of MusicBrainz data |
| `-v`, `--verbose` | `False` | Show detailed match logs and all local matches |

---

## 5. Universal Music Scraper (`music_scraper.py`)

Universal crawler and downloader supporting **Bandcamp**, **MediaFire**, **Archive.org**, and direct music release links.

### Usage Examples

#### Download Dochakuso Records Releases (Default)
```bash
python3 music_scraper.py https://dochakuso.net/release.html
```

#### Download Otherman Records Releases (Archive.org)
```bash
python3 music_scraper.py https://www.otherman-records.com/releases
```

#### Download Bandcamp Artist or Album Directly
```bash
python3 music_scraper.py https://goreshit.bandcamp.com
python3 music_scraper.py https://goreshit.bandcamp.com/album/bleak -f flac
```

#### Target Any Other Website
Crawl subpages up to depth 2 to find all MediaFire, Archive.org, Bandcamp, or audio archive links:
```bash
python3 music_scraper.py https://example.com/releases --depth 2
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `url` | `https://dochakuso.net/release.html` | Target website URL to scrape |
| `-o`, `--output-dir` | `./downloads` | Destination directory for downloaded music |
| `-f`, `--format` | `flac` | Preferred audio format for free Bandcamp downloads |
| `-d`, `--depth` | `1` | Maximum link traversal depth |
| `-t`, `--threads` | `4` | Number of concurrent download worker threads |
| `--dry-run` | `False` | Discover links and releases without downloading files |
| `--overwrite` | `False` | Redownload even if files already exist locally |
| `--max-files` | `None` | Limit total number of files to download |
| `--export-links` | `None` | Export found links to a file (`.json` or `.txt`) |
| `--delay` | `0.05` | Polite delay (seconds) between web requests |
| `-v`, `--verbose` | `False` | Enable debug logs |

---

## 6. Folder Cleaner (`clean_empty_folders.py`)

Deletes any folders that contain no music files (e.g. empty directories, or folders containing only leftover `.txt`, `.url`, `.DS_Store`, or image files with no music).

### Recognized Music Extensions
`.mp3`, `.flac`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.alac`, `.aiff`, `.wma`, `.mid`, `.midi`, `.zip`, `.rar`, `.7z`, `.tar`, `.gz`

### Usage Examples

#### Safe Dry-Run (Preview deletions)
```bash
python3 clean_empty_folders.py ./downloads
```

#### Perform Deletion
```bash
python3 clean_empty_folders.py ./downloads --force
```

#### Custom Music Extensions
```bash
python3 clean_empty_folders.py ./downloads --extensions mp3,flac,wav --force
```

---

## Deduplication & Manifest

1. **Host-Specific Key Normalization**:
   - Bandcamp: Normalizes `bc_<artist>_<album>` keys and verifies existing disk folders.
   - MediaFire: Normalizes unique file IDs (e.g. `fid2e24t027ldti`).
   - Archive.org: Normalizes item ID + target filename.
2. **Session Manifest**: Automatically maintains `<output-dir>/manifest.json` recording all successfully downloaded releases, formats, and timestamps across runs.
