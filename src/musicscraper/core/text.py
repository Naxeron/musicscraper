"""
Unified text processing, NLP, fuzzy matching, and Unicode normalization for MusicScraper.
"""

import re
import time
import unicodedata
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

ROMAN_NUMERALS_MAP: Dict[str, str] = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
    "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
}

UNAMBIGUOUS_ROMAN: Set[str] = {
    "ii", "iii", "iv", "vi", "vii", "viii", "ix",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx"
}


def normalize_roman_numerals(text: str) -> str:
    """
    Translates Roman numerals (I through XX) in track and movement indicators to Arabic digits.
    Guards against mangling common words and pronouns (e.g. 'I', 'V', 'X').
    """
    if not text:
        return ""

    # 1. Indicator + Roman numeral (I through XX, case-insensitive)
    pat_indicator = re.compile(
        r"(\b(?:part|pt|act|movement|mov|mvt|vol|volume|chapter|suite|no|nr|track|scene|phase|section|book|canto|opus|op)\b\.?\s*)([ivx]{1,6})\b",
        re.IGNORECASE
    )
    result = pat_indicator.sub(
        lambda m: f"{m.group(1)}{ROMAN_NUMERALS_MAP.get(m.group(2).lower(), m.group(2))}",
        text
    )

    # 2. Standalone unambiguous Roman numerals (II through XX, excluding single letters 'I', 'V', 'X')
    pat_unambiguous = re.compile(r"\b([ivx]{2,6})\b", re.IGNORECASE)
    result = pat_unambiguous.sub(
        lambda m: ROMAN_NUMERALS_MAP.get(m.group(1).lower(), m.group(1))
        if m.group(1).lower() in UNAMBIGUOUS_ROMAN else m.group(1),
        result
    )

    # 3. Bracketed Roman numerals: (I), [I], {I}, (V), [V], (X)
    pat_bracketed = re.compile(r"([\(\[\{])\s*([ivx]{1,6})\s*([\)\]\}])", re.IGNORECASE)
    result = pat_bracketed.sub(
        lambda m: f"{m.group(1)}{ROMAN_NUMERALS_MAP.get(m.group(2).lower(), m.group(2))}{m.group(3)}"
        if m.group(2).lower() in ROMAN_NUMERALS_MAP else m.group(0),
        result
    )

    return result


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
    Applies comprehensive Unicode NFC normalization, Katakana/Hiragana unification,
    Kanji numeral conversion, Roman numeral conversion, punctuation stripping, and diacritics removal.
    """
    if not text:
        return ""

    raw = unicodedata.normalize('NFC', str(text)).strip()
    raw = raw.translate(ZENKAKU_TO_HANKAKU)
    raw = raw.translate(KATAKANA_TO_HIRAGANA)
    raw = raw.translate(POLISH_DIACRITICS_MAP)
    raw = kanji_to_arabic(raw)
    raw = normalize_roman_numerals(raw)
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
    q = unicodedata.normalize('NFC', str(query)).strip()
    q = q.translate(POLISH_DIACRITICS_MAP)
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


VERSION_DESCRIPTOR_RE = re.compile(
    r"^(?:"
    r"(?:.*?\s+)?(?:remix|rmx|re-mix|flip|bootleg|rework|refix|mashup|mash-up)|"
    r"(?:.*?\s+)?vip(?:\s*mix)?|"
    r"instrumental|inst|off\s*vocal|karaoke|backing\s*track|"
    r"acapella|a\s*cappella|vocal\s*version|"
    r"acoustic(?:\s*ver(?:sion)?)?|unplugged(?:\s*ver(?:sion)?)?|piano\s*ver(?:sion)?|"
    r"live(?:\s+at\s+.*|\s+in\s+.*|\s+version|\s*\d{4})?|"
    r"speed\s*up|sped\s*up|slowed|nightcore|daycore|chopped\s*and\s*screwed|"
    r"demo|alternate\s*take|alt\s*take|alt\s*mix|rough\s*mix|"
    r"club\s*mix|extended\s*mix|extended\s*version|radio\s*edit|dub\s*mix|dub|original\s*mix|album\s*version|"
    r"remaster(?:ed)?|digital\s*remaster|anniversary\s*edition|deluxe\s*(?:edition|version)|bonus\s*track"
    r")$",
    re.IGNORECASE
)


@lru_cache(maxsize=65536)
def strip_track_number_and_artist(filename_no_ext: str) -> str:
    """
    Cleans a filename to extract the track title.
    e.g. '01 すてらべえ - Ultra Cutie Gangsta' -> 'Ultra Cutie Gangsta'
    e.g. '2-11 Stellabee - Enemy' -> 'Enemy'
    e.g. 'Aphex Twin - Syro - 03 - produk 29' -> 'produk 29'
    e.g. 'Aphex Twin (On)-[WAP 39CD]-[01]-On' -> 'On'
    """
    raw = unicodedata.normalize('NFC', str(filename_no_ext or "")).strip()
    for ext in ('.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav', '.aiff', '.ape', '.wv', '.wma'):
        if raw.lower().endswith(ext):
            raw = raw[:-len(ext)].strip()
            break
    cleaned = raw

    # Standardize typographic dashes to ASCII hyphen
    cleaned = re.sub(r'[\u2010-\u2015\u2212\uFF0D]', '-', cleaned)
    # Standardize tildes / wave dashes used as separators to standard ' - '
    cleaned = re.sub(r'\s*[~～〜]\s*', ' - ', cleaned)

    cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
    cleaned = re.sub(r'^(?:[a-zA-Z]?\d{1,4})\s+', '', cleaned)
    cleaned = re.sub(r'-\[\d+\]-', ' - ', cleaned)
    cleaned = re.sub(r'^\[\d+\]\s*[-._]*\s*', '', cleaned)

    delimiter = None
    if ' - ' in cleaned:
        delimiter = ' - '
    elif ' _ ' in cleaned:
        delimiter = ' _ '
    elif ']-[' in cleaned:
        delimiter = ']-['

    if delimiter:
        parts = [p.strip().rstrip(']') for p in cleaned.split(delimiter) if p.strip()]
        if len(parts) >= 2 and VERSION_DESCRIPTOR_RE.match(parts[-1]):
            version_desc = parts[-1]
            title_part = parts[-2]
            title_part = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', title_part)
            title_part = re.sub(r'^(?:[a-zA-Z]?\d{1,4})\s+', '', title_part)
            if title_part.strip() and not title_part.strip().isdigit():
                cleaned = f"{title_part} ({version_desc})"
            else:
                cleaned = parts[-1]
                cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
                cleaned = re.sub(r'^(?:[a-zA-Z]?\d{1,4})\s+', '', cleaned)
        elif parts:
            cleaned = parts[-1]
            cleaned = re.sub(r'^(?:\d{1,4}\s*[-._]+\s*|\d+[-._]\d+\s*[-._]*\s*)', '', cleaned)
            cleaned = re.sub(r'^(?:[a-zA-Z]?\d{1,4})\s+', '', cleaned)

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
    re.compile(r"(?:\bfeat\.?|\bft\.?|\bfeaturing\b)\s*([^,\-\(\[\{]+)", re.IGNORECASE),
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
    raw = unicodedata.normalize('NFC', str(title or "")).strip()
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
            f_text = m.group(1).strip().rstrip(")]}")
            if f_text:
                norm_f = normalize_text(f_text)
                if norm_f and norm_f not in features:
                    features.append(norm_f)

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
    Supports flexible matching for instrumental, acoustic, live, demo, and remix variations.
    """
    if v1_type is None and v2_type is None:
        return True
    if (v1_type is None) != (v2_type is None):
        return False
    if v1_type != v2_type:
        return False

    if not v1_text or not v2_text:
        return True

    # Generic types where variations represent equivalent musical content
    if v1_type in ("instrumental", "acapella"):
        return True

    if v1_type in ("remix", "mix_edit", "vip"):
        c1 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v1_text).strip()
        c2 = re.sub(r"\b(remix|rmx|re-mix|flip|edit|bootleg|version|mix)\b", "", v2_text).strip()
        if c1 and c2:
            sim = calculate_similarity(c1, c2)
            return (sim >= 0.75 or c1 in c2 or c2 in c1)
        sim = calculate_similarity(v1_text, v2_text)
        return (sim >= 0.75 or v1_text in v2_text or v2_text in v1_text)

    if v1_type in ("acoustic", "live", "demo"):
        c1 = re.sub(r"\b(version|take|mix|live|acoustic|demo|rough|alt|alternate|unplugged|piano)\b", "", v1_text).strip()
        c2 = re.sub(r"\b(version|take|mix|live|acoustic|demo|rough|alt|alternate|unplugged|piano)\b", "", v2_text).strip()
        if not c1 or not c2:
            return True
        if c1 == c2 or c1 in c2 or c2 in c1:
            return True
        return calculate_similarity(c1, c2) >= 0.75

    if v1_text == v2_text or v1_text in v2_text or v2_text in v1_text:
        return True
    return calculate_similarity(v1_text, v2_text) >= 0.75


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
