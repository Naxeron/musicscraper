# MusicScraper

A fast, versatile Python toolkit for music collectors and archivers:
1. **Audit local music libraries** for missing tracks and albums against MusicBrainz discography data (supporting aliases, transliterations, VA compilations, and network/SSHFS libraries).
2. **Download Bandcamp releases and artist discographies** natively (supporting high-resolution free downloads in FLAC/MP3-320/WAV, streaming fallback in MP3-128, automatic ID3 & artwork tagging, and zero-duplicate manifest tracking).
3. **Scrape and download free music releases** from netlabels and websites without duplicates (supporting **Bandcamp**, **MediaFire**, **Archive.org**, and direct audio/archive links).
4. **Clean up empty & non-music folders** automatically.

### Dedicated Support Out-of-the-Box:
- **[MusicBrainz Discography Audit](https://musicbrainz.org)**: Complete track verification with alias & compilation matching + official Bandcamp relation discovery.
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
# Audit artist discography
python3 main.py audit "Stellabee" -d /mnt/music

# Download Bandcamp discography
python3 main.py bandcamp goreshit -f flac

# Scrape web netlabel releases
python3 main.py scrape https://dochakuso.net/release.html

# Clean empty/non-music directories
python3 main.py clean ./downloads --force
```

---

## 1. Bandcamp Scraper (`bandcamp_scraper.py`)

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

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `targets` | *(optional)* | Artist subdomains, artist URLs, album URLs, or track URLs |
| `-i`, `--input` | `None` | Text file containing Bandcamp URLs (one per line) |
| `-o`, `--output-dir` | `./downloads` | Destination directory for downloads |
| `-f`, `--format` | `mp3-320` | Preferred audio format for free downloads (`flac`, `mp3-320`, `wav`, etc.) |
| `--no-fallback` | `False` | Disable fallback to MP3-128 streams if direct free download is not offered |
| `-t`, `--threads` | `3` | Concurrent worker threads |
| `--dry-run` | `False` | Inspect metadata and list discovered releases without downloading |
| `--overwrite` | `False` | Force redownload even if files already exist on disk |
| `-v`, `--verbose` | `False` | Enable debug logs |

---

## 2. Missing Tracks Checker (`check_missing_tracks.py`)

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

## 3. Universal Music Scraper (`music_scraper.py`)

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
| `-o`, `--output-dir` | `./downloads` | Directory to save downloaded files |
| `-t`, `--threads` | `3` | Number of concurrent download workers |
| `-d`, `--depth` | `1` | Max crawl depth for generic websites |
| `-f`, `--format` | `mp3-320` | Preferred audio format for Bandcamp downloads (`flac`, `mp3-320`, `wav`, etc.) |
| `--no-fallback` | `False` | Disable fallback to streaming MP3-128 for Bandcamp releases |
| `--dry-run` | `False` | Discover and list links without downloading |
| `--overwrite` | `False` | Redownload even if files already exist locally |
| `--max-files` | `None` | Limit total number of files to download |
| `--export-links` | `None` | Export found links to a file (`.json` or `.txt`) |
| `--delay` | `0.05` | Polite delay (seconds) between web requests |
| `-v`, `--verbose` | `False` | Enable debug logs |

---

## 4. Folder Cleaner (`clean_empty_folders.py`)

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
