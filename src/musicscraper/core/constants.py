"""
Universal constants, file extensions, stop words, and mappings for MusicScraper.
"""

from typing import Dict, Set

# Audio Extensions
AUDIO_EXTENSIONS: Set[str] = {
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg",
    ".opus", ".alac", ".aiff", ".aif", ".wma", ".ape",
    ".wv", ".dsf", ".dff", ".mka", ".mid", ".midi",
    ".mod", ".xm", ".it", ".s3m"
}

LOSSLESS_EXTENSIONS: Set[str] = {
    ".flac", ".wav", ".alac", ".aiff", ".aif",
    ".ape", ".wv", ".dsf", ".dff"
}

LOSSY_EXTENSIONS: Set[str] = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"
}

# Archive / Release Container Extensions
ARCHIVE_EXTENSIONS: Set[str] = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tar.gz", ".tgz"
}

AUDIO_AND_ARCHIVE_EXTENSIONS: Set[str] = AUDIO_EXTENSIONS | ARCHIVE_EXTENSIONS
AUDIO_ARCHIVE_EXTENSIONS: Set[str] = AUDIO_AND_ARCHIVE_EXTENSIONS

# Image Extensions (Album artwork, scans, etc.)
IMAGE_EXTENSIONS: Set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".svg", ".ico", ".tiff", ".tif", ".avif", ".heic",
    ".heif", ".psd", ".raw", ".cr2", ".nef", ".jfif"
}

# Supporting Release Metadata Extensions
SUPPORTING_EXTENSIONS: Set[str] = {
    ".cue", ".log", ".nfo", ".txt", ".m3u", ".m3u8",
    ".sfv", ".md5", ".pdf", ".accurip"
}

# Non-audio junk files to clean or ignore
COMMON_JUNK_FILES: Set[str] = {
    ".ds_store", "thumbs.db", "desktop.ini", ".gitkeep",
    ".directory", ".trash", "ehthumbs.db", "folder.htt"
}

# Compilation / Various Artists Directory Markers
VA_DIR_MARKERS: Set[str] = {
    "various", "various artists", "compilation", "compilations",
    "split", "soundtrack", "soundtracks", "ost", "sampler",
    "anthology", "tribute", "tributes", "v.a.", "va"
}

# Generic words / common words that shouldn't trigger loose title matching
GENERIC_OR_COMMON_WORDS: Set[str] = {
    "intro", "outro", "interlude", "prelude", "untitled", "bonus", "bonus track",
    "track", "instrumental", "opening", "ending", "skit", "silence", "noise",
    "demo", "mix", "remix", "version", "edit", "side a", "side b", "vip", "theme",
    "audio", "original", "cover", "live", "sampler", "compilation", "single", "ep",
    "album", "lp", "ost", "soundtrack", "part", "vol", "volume", "chapter", "reissue",
    "shit", "dreams", "sleep", "everyday", "scream", "fly", "sin", "jump", "angels",
    "love", "love you", "alone", "night", "rain", "home", "time", "summer", "winter",
    "spring", "fall", "sky", "sun", "moon", "star", "fire", "water", "blue", "red",
    "black", "white", "run", "walk", "stay", "go", "come", "life",
    "death", "dark", "light", "space", "mind", "soul", "heart", "eyes", "girl", "boy",
    "friends", "forever", "today", "tomorrow", "yesterday", "world", "hope", "lost",
    "breakcore", "lolicore", "speedcore", "hardcore", "frenchcore", "nightcore",
    "jcore", "j-core", "extratone", "splittercore", "terrorcore", "mashcore",
    "gabber", "gabba", "dancecore", "flashcore", "ambient",
    "vaporwave", "chiptune", "electronic", "techno", "trance", "house", "dubstep",
    "dnb", "drum and bass", "jungle", "rave", "rave music", "acid"
}

# Directory noise words for Soulseek directory filtering
DIR_STOP_WORDS: Set[str] = {
    "flac", "mp3", "320", "v0", "v2", "lossless", "cd", "vinyl", "web",
    "rip", "24bit", "16bit", "44.1khz", "96khz", "192khz", "cue", "log",
    "eac", "edition", "reissue", "remaster", "remastered", "deluxe",
    "ep", "lp", "single", "album", "disc", "disk", "ost", "soundtrack",
    "discography", "complete", "boxset", "vol", "volume"
}

# Special diacritics translation mappings (e.g. Polish letters that unidecode transforms into approximations)
POLISH_DIACRITICS_MAP: Dict[str, str] = {
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ź": "z", "ż": "z",
    "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N",
    "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z"
}

# Supported Bandcamp formats in order of download preference
BANDCAMP_SUPPORTED_FORMATS = [
    "flac", "mp3-320", "wav", "aac-hi", "aiff-lossless", "alac", "vorbis", "mp3-v0", "mp3-128"
]

import os
from pathlib import Path
DEFAULT_CACHE_DIR: Path = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "musicscraper"
DEFAULT_OUTPUT_DIR: Path = Path("./downloads")
DEFAULT_LIBRARY_DIR: Path = Path("./music")
