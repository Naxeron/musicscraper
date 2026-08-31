"""
Unified text processing, NLP, fuzzy matching, and Unicode normalization for MusicScraper.
"""

import re
import time
import urllib.parse
from functools import lru_cache
from difflib import SequenceMatcher
from typing import Dict, List, Set, Tuple, Optional, Any
from unidecode import unidecode

from musicscraper.core.constants import (
    POLISH_DIACRITICS_MAP,
    DIR_STOP_WORDS,
    GENERIC_OR_COMMON_WORDS,
)

# Japanese Zenkaku/Hankaku & Kana Transliteration Mappings
KATAKANA_TO_HIRAGANA: Dict[int, int] = {
    i: i - 0x60 for i in range(0x30A1, 0x30F7)
}

ZENKAKU_TO_HANKAKU: Dict[int, int] = {
    0x3000: 0x0020,  # Ideographic space -> ASCII space
    **{i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)},  # Fullwidth ASCII -> Halfwidth ASCII
}

KANJI_NUMERALS: Dict[str, str] = {
    "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    "十": "10", "百": "100", "千": "1000",
}


def kanji_to_arabic(text: str) -> str:
    """Converts simple Kanji and full-width Japanese numerals to Arabic digits."""
    if not text:
        return ""
    result = text
    if "十二" in result:
        result = result.replace("十二", "12")
    if "十一" in result:
        result = result.replace("十一", "11")
    if "十" in result:
        result = re.sub(r"([1-9])十([1-9])", r"\g<1>\g<2>", result)
        result = re.sub(r"([1-9])十", r"\g<1>0", result)
        result = re.sub(r"十([1-9])", r"1\g<1>", result)
        result = result.replace("十", "10")
    for kanji, digit in KANJI_NUMERALS.items():
        result = result.replace(kanji, digit)
    return result


@lru_cache(maxsize=65536)
def normalize_text(text: Optional[str]) -> str:
    """
    Applies comprehensive Unicode normalization, Katakana/Hiragana unification,
    Kanji numeral conversion, punctuation stripping, and diacritics removal.
    """
    if not text:
        return ""

    raw = str(text).strip()
    raw = raw.translate(ZENKAKU_TO_HANKAKU)
    raw = raw.translate(KATAKANA_TO_HIRAGANA)
    raw = raw.translate(POLISH_DIACRITICS_MAP)
    raw = kanji_to_arabic(raw)
    ascii_norm = unidecode(raw).lower()
    clean = re.sub(r"[^\w\s]", " ", ascii_norm)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if clean else ascii_norm.strip()


@lru_cache(maxsize=65536)
def clean_tokens(text: Optional[str]) -> str:
    """Compresses normalized text into contiguous alphanumeric characters."""
    norm = normalize_text(text)
    return "".join(norm.split())


@lru_cache(maxsize=65536)
def clean_search_phrase(query: str) -> str:
    """Cleans punctuation and symbols from a search query for maximum index recall, preserving non-Latin scripts."""
    if not query:
        return ""
    q = query.translate(POLISH_DIACRITICS_MAP)
    q = q.translate(ZENKAKU_TO_HANKAKU)
    q = re.sub(r"[^\w\s\-\.\']", " ", q, flags=re.UNICODE)
    q = re.sub(r"\s+", " ", q).strip()
    return q


@lru_cache(maxsize=65536)
def _tokenize_words_cached(text: Optional[str]) -> Tuple[str, ...]:
    """Tokenizes text into a tuple of individual alphanumeric word tokens."""
    norm = normalize_text(text)
    if not norm:
        return ()
    return tuple(re.findall(r"[a-z0-9]+", norm))


def tokenize_words(text: Optional[str]) -> List[str]:
    """Extracts lowercase alphanumeric words with unidecode transliteration."""
    return list(_tokenize_words_cached(text))


def is_sublist(sub: List[str], full: List[str]) -> bool:
    """Checks if sub is an exact contiguous sub-sequence of full word tokens."""
    if not sub or not full or len(sub) > len(full):
        return False
    sub_len = len(sub)
    for i in range(len(full) - sub_len + 1):
        if full[i:i + sub_len] == sub:
            return True
    return False


@lru_cache(maxsize=65536)
def calculate_similarity(str1: str, str2: str) -> float:
    """Calculates SequenceMatcher ratio between two normalized strings."""
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0
    return SequenceMatcher(None, str1, str2).ratio()


@lru_cache(maxsize=65536)
def strip_track_number_and_artist(filename_no_ext: str) -> str:
    """
    Cleans a filename to extract the track title.
    e.g. '01 すてらべえ - Ultra Cutie Gangsta' -> 'Ultra Cutie Gangsta'
    e.g. '2-11 Stellabee - Enemy' -> 'Enemy'
    e.g. 'Aphex Twin - Syro - 03 - produk 29' -> 'produk 29'
    e.g. 'Aphex Twin (On)-[WAP 39CD]-[01]-On' -> 'On'
    """
    raw = filename_no_ext.strip()
    cleaned = raw
    cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
    cleaned = re.sub(r'^0\d\s+', '', cleaned)
    cleaned = re.sub(r'-\[\d+\]-', ' - ', cleaned)
    cleaned = re.sub(r'^\[\d+\]\s*[-._]*\s*', '', cleaned)

    if ' - ' in cleaned:
        parts = cleaned.split(' - ')
        cleaned = parts[-1].strip()
        cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
        cleaned = re.sub(r'^0\d\s+', '', cleaned)
    elif ' _ ' in cleaned:
        parts = cleaned.split(' _ ')
        cleaned = parts[-1].strip()
        cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
        cleaned = re.sub(r'^0\d\s+', '', cleaned)
    elif ']-[' in cleaned:
        parts = cleaned.split(']-[')
        cleaned = parts[-1].rstrip(']').strip()
        cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
        cleaned = re.sub(r'^0\d\s+', '', cleaned)

    return cleaned.strip() if cleaned.strip() else raw


# Version & Remaster Patterns
REMASTER_OR_NOISE_PATTERNS = [
    re.compile(r"[\(\[\{]?(?:20\d\d|19\d\d)?\s*digital\s*remaster(?:ed)?(?:\s*version|\s*\d{4})?[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:20\d\d|19\d\d)?\s*remaster(?:ed)?(?:\s*version|\s*\d{4})?[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?anniversary\s*edition[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?deluxe\s*(?:edition|version)[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?bonus\s*track[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:original\s*mix|original\s*version|album\s*version|main\s*version)[\)\]\}]?", re.IGNORECASE),
    re.compile(r"[\(\[\{]?(?:flac|mp3|320kbps|24bit|lossless|wav|vbr|cd|web|vinyl|rip|official\s*audio|official\s*video|mv|lyrics)[\)\]\}]?", re.IGNORECASE),
    re.compile(r"\[\s*[\d.]+\s*(?:bpm)?\s*\]", re.IGNORECASE),
]

FEATURE_PATTERNS = [
    re.compile(r"[\(\[\{]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]\}]+)[\)\]\}]", re.IGNORECASE),
    re.compile(r"(?:\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*([^,\-\(\[\{]+)", re.IGNORECASE),
]

VERSION_PATTERNS = [
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:remix|rmx|re-mix|flip|bootleg|rework|edit|refix|mashup|mash-up)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "remix"),
    (re.compile(r"(?:^|\s)\-\s*([^\-]+(?:remix|rmx|re-mix|flip|bootleg|rework|edit|refix|mashup|mash-up)[^\-]*)", re.IGNORECASE), "remix"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:vip\s*mix|vip)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "vip"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:instrumental|inst\b|off\s*vocal|karaoke|backing\s*track)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "instrumental"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:acapella|a\s*cappella|vocal\s*version)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "acapella"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:acoustic|unplugged|piano\s*ver(?:sion)?)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "acoustic"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:live(?:\s+at|\s+in|\s+version|\s*\d{4})?)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "live"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:speed\s*up|sped\s*up|slowed|nightcore|daycore|chopped\s*and\s*screwed)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "speed"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:demo|alternate\s*take|alt\s*take|alt\s*mix|rough\s*mix)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "demo"),
    (re.compile(r"[\(\[\{]([^\)\]\}]*(?:club\s*mix|extended\s*mix|extended\s*version|radio\s*edit|dub\s*mix|dub)[^\)\]\}]*)[\)\]\}]", re.IGNORECASE), "mix_edit"),
]


@lru_cache(maxsize=65536)
def parse_track_title_structure(title: str) -> Dict[str, Any]:
    """
    Parses a track title into structured components:
    - Base normalized title (with noise, remasters, format tags, and features stripped)
    - Base display title
    - Version type and normalized descriptor text (e.g. remix, instrumental, live)
    - Featured artist list
    """
    raw = (title or "").strip()
    if not raw:
        return {
            "raw": "",
            "base_title": "",
            "base_norm": "",
            "version_type": None,
            "version_text": None,
            "features": []
        }

    cleaned = strip_track_number_and_artist(raw)
    if "：" in cleaned:
        cleaned = cleaned.split("：", 1)[1].strip()

    # 1. Extract features
    features = []
    for pat in FEATURE_PATTERNS:
        for m in pat.finditer(cleaned):
            f_text = m.group(1).strip()
            if f_text:
                features.append(normalize_text(f_text))

    # 2. Extract version / remix
    v_type = None
    v_text = None
    for pat, vtype in VERSION_PATTERNS:
        m = pat.search(cleaned)
        if m:
            v_type = vtype
            v_text = normalize_text(m.group(1))
            break

    # 3. Clean base title
    base_str = cleaned
    for pat, _ in VERSION_PATTERNS:
        base_str = pat.sub(" ", base_str)
    for pat in FEATURE_PATTERNS:
        base_str = pat.sub(" ", base_str)
    for pat in REMASTER_OR_NOISE_PATTERNS:
        base_str = pat.sub(" ", base_str)

    base_norm = normalize_text(base_str)

    return {
        "raw": raw,
        "base_title": base_str.strip(),
        "base_norm": base_norm,
        "version_type": v_type,
        "version_text": v_text,
        "features": features
    }


def are_versions_compatible(
    v1_type: Optional[str],
    v1_text: Optional[str],
    v2_type: Optional[str],
    v2_text: Optional[str]
) -> bool:
    """
    Determines if two track version modifiers are musically compatible.
    """
    if v1_type is None and v2_type is None:
        return True
    if (v1_type is None) != (v2_type is None):
        return False
    if v1_type != v2_type:
        return False
    if v1_type in ("remix", "mix_edit", "vip"):
        if not v1_text or not v2_text:
            return True
        # Strip generic keywords like 'remix', 'mix', 'edit' to compare the remixer name specifically
        c1 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v1_text).strip()
        c2 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v2_text).strip()
        if c1 and c2:
            sim = calculate_similarity(c1, c2)
            return (sim >= 0.75 or c1 in c2 or c2 in c1)
        sim = calculate_similarity(v1_text, v2_text)
        return (sim >= 0.75 or v1_text in v2_text or v2_text in v1_text)

    if v1_text and v2_text:
        return (v1_text == v2_text) or (calculate_similarity(v1_text, v2_text) > 0.8)
    return True


def extract_dir_and_filename(full_path: str) -> Tuple[str, str]:
    """Splits a Windows or POSIX path into (directory_path, filename)."""
    clean_p = full_path.replace("/", "\\")
    parts = clean_p.rsplit("\\", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


class FilenameUtils:
    """Utilities for sanitizing, decoding, and validating file paths."""

    @staticmethod
    def sanitize(name: str, max_bytes: int = 240, default_ext: str = "") -> str:
        """Sanitizes filename for cross-platform filesystem safety while preserving Unicode."""
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.strip('. \t\r\n')
        if not name:
            name = f"download_{int(time.time())}{default_ext}"
        encoded = name.encode("utf-8")
        if len(encoded) > max_bytes:
            name = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return name

    @staticmethod
    def clean_spaces(text: str) -> str:
        """Collapses consecutive spaces into a single space."""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def decode(raw_name: str) -> str:
        """Decodes raw or double percent-encoded strings and fixes Latin-1 mojibake."""
        decoded = urllib.parse.unquote(urllib.parse.unquote(raw_name))
        try:
            decoded = decoded.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return decoded
