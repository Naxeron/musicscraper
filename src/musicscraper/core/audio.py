"""
Unified Mutagen audio inspection, metadata extraction, quality scoring, and tag writing.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional, Tuple, Any

import mutagen
import mutagen.mp3
import mutagen.aiff
from mutagen.id3 import ID3, TCON, TIT2, TPE1, TALB, TRCK, TDRC, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mutagen.asf import ASF
from mutagen.apev2 import APEv2

from musicscraper.core.constants import (
    LOSSLESS_EXTENSIONS,
    LOSSY_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VA_DIR_MARKERS,
)
from musicscraper.core.text import (
    strip_track_number_and_artist,
    normalize_text
)


@dataclass
class AudioMetadata:
    """Unified representation of audio file metadata, tags, and stream specs."""
    path: Path
    file_type: str = ""
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_number: str = ""
    disc_number: int = 1
    total_discs: int = 1
    year: str = ""
    genres: List[str] = field(default_factory=list)

    # MusicBrainz Embedded Tag IDs
    mb_track_ids: Set[str] = field(default_factory=set)
    mb_rec_ids: Set[str] = field(default_factory=set)
    mb_artist_ids: Set[str] = field(default_factory=set)
    mb_release_ids: Set[str] = field(default_factory=set)

    # Audio Stream Parameters
    bitrate_kbps: int = 0
    bit_depth: int = 0
    sample_rate: int = 0
    channels: int = 2
    duration: float = 0.0
    is_lossless: bool = False
    format_label: str = ""
    quality_score: int = 0

    @property
    def norm_title(self) -> str:
        return normalize_text(self.title)

    @property
    def norm_artist(self) -> str:
        return normalize_text(self.artist)

    @property
    def norm_album(self) -> str:
        return normalize_text(self.album)

    @property
    def format(self) -> str:
        return self.format_label or self.file_type or ""

    @property
    def bitrate(self) -> int:
        return self.bitrate_kbps


WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}
SIDE_MAP = {
    "a": 1, "b": 2, "c": 3, "d": 4,
    "e": 5, "f": 6, "g": 7, "h": 8,
}
DISC_DIR_PATTERN = re.compile(
    r"^(?:(?:vinyl|lp)\s+)?(?:disc|cd|disk|vinyl|lp|side)\s*[-_.]?\s*(?:(\d{1,2})|([a-hA-H])|(one|two|three|four|five|six|seven|eight|nine|ten))\b(?:\s*[-_.:(\[].*)?$",
    re.IGNORECASE
)


def extract_artist_title_from_filename(stem: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts artist and title from filename stem when untagged and directory heuristics fail.
    Guards against extracting track numbers or single-element titles as artists.
    """
    normalized = re.sub(r"[\u2010-\u2015\u2212~]", " - ", stem)
    parts = [p.strip() for p in normalized.split(" - ") if p.strip()]
    if len(parts) < 2:
        return None, None

    p0_clean = re.sub(r"^(?:[a-zA-Z]?\d{1,4}|\d+[-._]\d+)$", "", parts[0]).strip()
    if not p0_clean:
        if len(parts) >= 3:
            cand_artist = parts[1].strip()
            cand_title = " - ".join(parts[2:]).strip()
            if cand_artist and not cand_artist.isdigit():
                return cand_artist, cand_title
            return None, cand_title
        return None, parts[1].strip()
    else:
        cand_artist = re.sub(r"^(?:[a-zA-Z]?\d{1,4}|\d+[-._]\d+)[\s._\-]+", "", parts[0]).strip()
        cand_title = " - ".join(parts[1:]).strip()
        if cand_artist and not re.match(r"^\d+$", cand_artist):
            return cand_artist, cand_title
        return None, None


class AudioQualityAnalyzer:
    """Inspects audio stream parameters and computes standardized quality scores."""

    @staticmethod
    def analyze_file(file_path: Path) -> AudioMetadata:
        """
        Inspects an audio file via Mutagen and extracts tags, bitrate, bit depth,
        sample rate, lossless status, and quality score.
        """
        path_str = str(file_path)
        ext = file_path.suffix.lower()
        meta = AudioMetadata(path=file_path, file_type=ext)

        try:
            mf = mutagen.File(path_str)
        except Exception:
            mf = None

        type_name = type(mf).__name__ if mf else "Unknown"
        mime = getattr(mf, "mime", [""])[0] if (mf and hasattr(mf, "mime")) else ""

        is_compilation = False

        # Extract Tags
        if mf and hasattr(mf, "tags") and mf.tags is not None:
            tags = mf.tags
            if hasattr(tags, "items"):
                for k, v in tags.items():
                    k_str = str(k).upper().strip()
                    v_list = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]

                    if k_str in ("TIT2", "TITLE", "\xa9NAM", "TXXX:TITLE") and not meta.title:
                        meta.title = v_list[0].strip() if v_list else ""
                    elif k_str in ("TALB", "ALBUM", "\xa9ALB", "TXXX:ALBUM") and not meta.album:
                        meta.album = v_list[0].strip() if v_list else ""
                    elif k_str in ("TRCK", "TRACKNUMBER", "TXXX:TRACKNUMBER") and not meta.track_number:
                        raw_trck = v_list[0].strip() if v_list else ""
                        meta.track_number = raw_trck.split("/")[0].strip()
                    elif k_str in (
                        "TPOS", "DISCNUMBER", "DISC", "TXXX:DISCNUMBER",
                        "TXXX:DISC", "TXXX:PART OF SET", "TXXX:PARTOFSET",
                        "DISK", "\xa9DISK", "WM/PARTOFSET"
                    ):
                        if isinstance(v, (list, tuple)) and v and isinstance(v[0], (tuple, list)):
                            try:
                                d = int(v[0][0])
                                if d > 0:
                                    meta.disc_number = d
                                if len(v[0]) > 1:
                                    t = int(v[0][1])
                                    if t > 0:
                                        meta.total_discs = t
                            except Exception:
                                pass
                        else:
                            raw_disc = str(v_list[0]).strip() if v_list else ""
                            if "/" in raw_disc:
                                parts = raw_disc.split("/", 1)
                                d_digits = re.sub(r"[^\d]", "", parts[0])
                                t_digits = re.sub(r"[^\d]", "", parts[1])
                                if d_digits and int(d_digits) > 0:
                                    meta.disc_number = int(d_digits)
                                if t_digits and int(t_digits) > 0:
                                    meta.total_discs = int(t_digits)
                            else:
                                d_digits = re.sub(r"[^\d]", "", raw_disc)
                                if d_digits and int(d_digits) > 0:
                                    meta.disc_number = int(d_digits)
                    elif k_str in ("DISCTOTAL", "TOTALDISCS", "TXXX:DISCTOTAL", "TXXX:TOTALDISCS"):
                        raw_tot = str(v_list[0]).strip() if v_list else ""
                        t_digits = re.sub(r"[^\d]", "", raw_tot)
                        if t_digits and int(t_digits) > 0:
                            meta.total_discs = int(t_digits)
                    elif k_str in ("TDRC", "DATE", "YEAR", "\xa9DAY") and not meta.year:
                        meta.year = v_list[0].strip() if v_list else ""
                    # Album Artist
                    elif k_str in (
                        "TPE2", "ALBUMARTIST", "ALBUM ARTIST", "AART",
                        "TXXX:ALBUMARTIST", "TXXX:ALBUM ARTIST"
                    ) and not meta.album_artist:
                        meta.album_artist = v_list[0].strip() if v_list else ""
                    # Track Artist
                    elif k_str in (
                        "TPE1", "TOPE", "ARTIST", "PERFORMER",
                        "\xa9ART", "TXXX:ARTIST"
                    ) and not meta.artist:
                        meta.artist = v_list[0].strip() if v_list else ""

                    # Compilation flag
                    if k_str in ("TCMP", "CPIL", "COMPILATION", "TXXX:COMPILATION"):
                        val_str = str(v_list[0]).strip().lower() if v_list else ""
                        if val_str in ("1", "true", "yes"):
                            is_compilation = True

                    # Genres
                    if k_str in ("TCON", "GENRE", "\xa9GEN") and not meta.genres:
                        for item in v_list:
                            parts = re.split(r"[;/]|\s{2,}", item)
                            for p in parts:
                                p_clean = p.strip()
                                if p_clean and p_clean not in meta.genres:
                                    meta.genres.append(p_clean)

                    # MusicBrainz Tag IDs
                    if k_str in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ ALBUM ID", "TXXX:MUSICBRAINZ ALBUM ID"):
                        meta.mb_release_ids.update(x.strip() for x in v_list if x.strip())
                    elif k_str in (
                        "MUSICBRAINZ_TRACKID", "MUSICBRAINZ TRACK ID", "TXXX:MUSICBRAINZ TRACK ID",
                        "MUSICBRAINZ_RECORDINGID", "MUSICBRAINZ RECORDING ID", "TXXX:MUSICBRAINZ RECORDING ID"
                    ):
                        meta.mb_rec_ids.update(x.strip() for x in v_list if x.strip())
                    elif k_str in ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ ARTIST ID", "TXXX:MUSICBRAINZ ARTIST ID"):
                        meta.mb_artist_ids.update(x.strip() for x in v_list if x.strip())
                    elif k_str in ("MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ RELEASE TRACK ID", "TXXX:MUSICBRAINZ RELEASE TRACK ID"):
                        meta.mb_track_ids.update(x.strip() for x in v_list if x.strip())
                    elif k_str.startswith("UFID:HTTP://MUSICBRAINZ.ORG"):
                        # Extract UFID
                        try:
                            ufid_val = bytes(v.data).decode('utf-8', errors='ignore') if hasattr(v, 'data') else str(v)
                            if ufid_val:
                                meta.mb_rec_ids.add(ufid_val.strip())
                        except Exception:
                            pass

        if meta.total_discs < meta.disc_number:
            meta.total_discs = meta.disc_number

        if is_compilation and not meta.album_artist:
            meta.album_artist = "Various Artists"

        if meta.album_artist:
            norm_aa = meta.album_artist.strip().lower()
            if (
                norm_aa in VA_DIR_MARKERS
                or norm_aa in ("various artists", "various", "va", "v.a.", "v/a", "compilation", "compilations")
                or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", norm_aa))
            ):
                meta.album_artist = "Various Artists"

        # Fallback metadata from filename / path hierarchy
        filename_no_ext = file_path.stem

        parent_name = file_path.parent.name
        grandparent_name = file_path.parent.parent.name if file_path.parent != file_path.parent.parent else ""
        greatgrandparent_name = (
            file_path.parent.parent.parent.name
            if file_path.parent.parent != file_path.parent.parent.parent
            else ""
        )

        is_disc_subfolder = False
        disc_from_folder = None
        m_disc = DISC_DIR_PATTERN.match(parent_name.strip())
        if m_disc:
            is_disc_subfolder = True
            if m_disc.group(1):
                disc_from_folder = int(m_disc.group(1))
            elif m_disc.group(2):
                disc_from_folder = SIDE_MAP.get(m_disc.group(2).lower(), 1)
            elif m_disc.group(3):
                disc_from_folder = WORD_NUMS.get(m_disc.group(3).lower(), 1)

        if is_disc_subfolder and disc_from_folder:
            if meta.disc_number == 1:
                meta.disc_number = disc_from_folder
            if meta.total_discs < meta.disc_number:
                meta.total_discs = meta.disc_number

        if is_disc_subfolder:
            eff_album_folder = grandparent_name
            eff_artist_folder = greatgrandparent_name
        else:
            eff_album_folder = parent_name
            eff_artist_folder = grandparent_name

        if not meta.title:
            meta.title = strip_track_number_and_artist(filename_no_ext)
        if not meta.album or (meta.album and DISC_DIR_PATTERN.match(meta.album.strip())):
            if eff_album_folder and eff_album_folder.lower() not in ("music", "downloads", "library", "tracks", "singles"):
                meta.album = eff_album_folder

        norm_parent = eff_album_folder.lower().strip()
        norm_grandparent = eff_artist_folder.lower().strip()
        is_va_dir = (
            norm_parent in VA_DIR_MARKERS
            or norm_grandparent in VA_DIR_MARKERS
            or bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", norm_parent))
            or bool(re.match(r"^(?:va|v\.a\.|various\s*arti[st]{2,4}s?)\b", norm_grandparent))
            or norm_parent.startswith("va - ")
            or norm_parent.startswith("va-")
            or norm_parent.startswith("va ")
            or norm_parent.startswith("v.a. - ")
            or norm_parent.startswith("various artists - ")
            or norm_parent.startswith("various - ")
            or "[va]" in norm_parent
            or "(va)" in norm_parent
            or norm_parent.endswith(" [va]")
            or norm_parent.endswith(" (va)")
        )
        if is_va_dir and not meta.album_artist:
            meta.album_artist = "Various Artists"

        if not meta.artist:
            if " - " in eff_album_folder:
                parts = eff_album_folder.split(" - ", 1)
                first_part = parts[0].strip()
                if (
                    first_part.lower() in ("va", "v.a.", "various", "various artists")
                    or first_part.lower() in VA_DIR_MARKERS
                    or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", first_part.lower()))
                ):
                    if not meta.album_artist:
                        meta.album_artist = "Various Artists"
                    if not meta.album or meta.album == eff_album_folder:
                        meta.album = parts[1].strip()
                else:
                    meta.artist = first_part
                    if not meta.album or meta.album == eff_album_folder:
                        meta.album = parts[1].strip()
            elif eff_artist_folder and eff_artist_folder.lower() not in ("music", "downloads", "library"):
                if (
                    eff_artist_folder.lower() in VA_DIR_MARKERS
                    or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", eff_artist_folder.lower()))
                ):
                    if not meta.album_artist:
                        meta.album_artist = "Various Artists"
                else:
                    meta.artist = eff_artist_folder

        if not meta.artist:
            cand_artist, cand_title = extract_artist_title_from_filename(filename_no_ext)
            if cand_artist:
                meta.artist = cand_artist
                if not meta.title or meta.title == filename_no_ext:
                    if cand_title:
                        meta.title = strip_track_number_and_artist(cand_title)

        if meta.artist and (
            meta.artist.strip().lower() in ("va", "v.a.", "various", "various artists")
            or bool(re.match(r"^v(?:arious|\.)?\s*arti[st]{2,4}s?$", meta.artist.strip().lower()))
        ):
            meta.artist = "Various Artists"
            if not meta.album_artist:
                meta.album_artist = "Various Artists"

        # Extract Audio Stream Specs
        if mf and hasattr(mf, "info") and mf.info is not None:
            info = mf.info
            meta.duration = getattr(info, "length", 0.0)
            meta.sample_rate = getattr(info, "sample_rate", 0)
            meta.channels = getattr(info, "channels", 2)
            meta.bit_depth = getattr(info, "bits_per_sample", 0)
            raw_bitrate = getattr(info, "bitrate", 0)
            if raw_bitrate:
                meta.bitrate_kbps = int(raw_bitrate / 1000)

        # Quality scoring & format categorization
        if ext in LOSSLESS_EXTENSIONS or type_name in ("FLAC", "WAVE", "AIFF", "MonkeysAudio", "WavPack") or "flac" in mime or "wav" in mime:
            meta.is_lossless = True
            meta.bit_depth = meta.bit_depth or 16
            meta.sample_rate = meta.sample_rate or 44100
            if meta.bit_depth > 16:
                meta.format_label = f"FLAC {meta.bit_depth}-bit/{meta.sample_rate}Hz"
                meta.quality_score = 115
            else:
                meta.format_label = "FLAC (Lossless 16-bit)"
                meta.quality_score = 100
        elif ext in (".mp4", ".m4a") or type_name == "MP4" or "mp4" in mime or "m4a" in mime:
            codec = getattr(mf.info, "codec", "") if (mf and hasattr(mf, "info")) else ""
            if codec == "alac" or meta.bit_depth > 0:
                meta.is_lossless = True
                meta.format_label = f"ALAC {meta.bit_depth or 16}-bit"
                meta.quality_score = 100
            else:
                meta.is_lossless = False
                bitrate_label = f"{meta.bitrate_kbps}kbps" if meta.bitrate_kbps else "AAC"
                meta.format_label = f"AAC {bitrate_label}"
                meta.quality_score = min(75, int(meta.bitrate_kbps / 4)) if meta.bitrate_kbps else 40
        elif ext == ".mp3" or type_name == "MP3" or "mp3" in mime or "mpeg" in mime:
            meta.is_lossless = False
            if meta.bitrate_kbps >= 320:
                meta.format_label = "MP3 320kbps"
                meta.quality_score = 80
            elif meta.bitrate_kbps >= 240:
                meta.format_label = f"MP3 ~{meta.bitrate_kbps}kbps (V0)"
                meta.quality_score = 70
            elif meta.bitrate_kbps >= 192:
                meta.format_label = f"MP3 {meta.bitrate_kbps}kbps"
                meta.quality_score = 50
            elif meta.bitrate_kbps > 0:
                meta.format_label = f"MP3 {meta.bitrate_kbps}kbps"
                meta.quality_score = 30
            else:
                meta.format_label = "MP3"
                meta.quality_score = 35
        elif ext in (".ogg", ".opus") or type_name in ("OggVorbis", "OggOpus"):
            meta.is_lossless = False
            codec_name = "Opus" if (ext == ".opus" or type_name == "OggOpus") else "Vorbis"
            if meta.bitrate_kbps >= 256:
                meta.format_label = f"{codec_name} ~{meta.bitrate_kbps}kbps"
                meta.quality_score = 75
            elif meta.bitrate_kbps >= 160:
                meta.format_label = f"{codec_name} {meta.bitrate_kbps}kbps"
                meta.quality_score = 60
            elif meta.bitrate_kbps > 0:
                meta.format_label = f"{codec_name} {meta.bitrate_kbps}kbps"
                meta.quality_score = 40
            else:
                meta.format_label = codec_name
                meta.quality_score = 45
        else:
            meta.format_label = ext.upper().lstrip(".") or "Audio"
            meta.quality_score = 40

        return meta

    @staticmethod
    def calculate_quality_score(meta: AudioMetadata) -> int:
        """Calculates quality score from an AudioMetadata object."""
        if meta.is_lossless:
            if meta.bit_depth and meta.bit_depth > 16:
                return 115
            return 100
        if meta.bitrate_kbps >= 320:
            return 80
        elif meta.bitrate_kbps >= 240:
            return 70
        elif meta.bitrate_kbps >= 192:
            return 50
        elif meta.bitrate_kbps > 0:
            return 30
        return 35

    @staticmethod
    def determine_stream_quality(file_item: Dict[str, Any]) -> Tuple[str, int]:
        """
        Evaluates audio format, bit depth, sample rate, and bitrate from slskd file attributes.
        Returns (format_label, quality_score).
        """
        fn = file_item.get("filename", "") or file_item.get("base_filename", "")
        ext = Path(fn).suffix.lower()
        bit_rate = file_item.get("bitRate", 0)
        bit_depth = file_item.get("bitDepth", 0)
        sample_rate = file_item.get("sampleRate", 0)

        if ext in LOSSLESS_EXTENSIONS:
            if bit_depth and bit_depth > 16:
                return f"FLAC {bit_depth}-bit/{sample_rate or 44100}Hz", 115
            return "FLAC (Lossless)", 100

        if ext in (".mp3", ".m4a", ".ogg", ".opus", ".mp4", ".wma"):
            if bit_rate >= 320:
                return "MP3 320kbps", 80
            elif bit_rate >= 240:
                return f"MP3 ~{bit_rate}kbps (V0)", 70
            elif bit_rate >= 192:
                return f"MP3 {bit_rate}kbps", 50
            elif bit_rate > 0:
                return f"MP3 {bit_rate}kbps", 30
            return ext.upper().lstrip("."), 40

        return "Audio File", 40


class AudioMetadataHandler:
    """Reads and writes audio tags (ID3, Vorbis, MP4, WAVE) via Mutagen."""

    @classmethod
    def read_metadata(cls, file_path: Path) -> AudioMetadata:
        """Reads audio tags and metadata using AudioQualityAnalyzer."""
        return AudioQualityAnalyzer.analyze_file(file_path)

    @classmethod
    def write_genres(
        cls,
        file_path: Path,
        genres: List[str],
        mode: str = "overwrite",
        separator: str = "; ",
        multi_value: bool = False
    ) -> bool:
        """
        Writes genre tags to the audio file.
        mode: 'overwrite', 'skip_existing', or 'append'
        """
        if not genres:
            return False

        try:
            audio = mutagen.File(file_path)
            if audio is None:
                if file_path.suffix.lower() == ".mp3":
                    try:
                        audio = mutagen.mp3.MP3(file_path)
                        audio.add_tags()
                    except Exception:
                        return False
                else:
                    return False

            current_meta = cls.read_metadata(file_path)
            final_genres: List[str] = []

            if mode == "skip_existing" and current_meta.genres:
                return False
            elif mode == "append":
                final_genres = list(current_meta.genres)
                for g in genres:
                    if g not in final_genres:
                        final_genres.append(g)
            else:  # overwrite
                final_genres = list(genres)

            if not final_genres:
                return False

            genre_string = separator.join(final_genres)

            # 1. MP3 / ID3
            if isinstance(audio, mutagen.mp3.MP3) or (hasattr(audio, "tags") and isinstance(audio.tags, ID3)):
                if audio.tags is None:
                    try:
                        audio.add_tags()
                    except Exception:
                        pass
                if audio.tags is not None:
                    text_val = final_genres if multi_value else [genre_string]
                    audio.tags["TCON"] = TCON(encoding=3, text=text_val)
                    audio.tags.save(file_path)
                    return True

            # 2. FLAC
            elif isinstance(audio, FLAC):
                if multi_value:
                    audio["GENRE"] = final_genres
                else:
                    audio["GENRE"] = [genre_string]
                audio.save()
                return True

            # 3. M4A / MP4
            elif isinstance(audio, MP4):
                if multi_value:
                    audio["\xa9gen"] = final_genres
                else:
                    audio["\xa9gen"] = [genre_string]
                audio.save()
                return True

            # 4. OGG / Opus
            elif isinstance(audio, (OggVorbis, OggOpus)):
                if multi_value:
                    audio["GENRE"] = final_genres
                else:
                    audio["GENRE"] = [genre_string]
                audio.save()
                return True

            # 5. WAVE / AIFF
            elif isinstance(audio, (WAVE, mutagen.aiff.AIFF)):
                if audio.tags is None:
                    try:
                        audio.add_tags()
                    except Exception:
                        pass
                if audio.tags is not None:
                    text_val = final_genres if multi_value else [genre_string]
                    audio.tags["TCON"] = TCON(encoding=3, text=text_val)
                    audio.save()
                    return True

            # 6. Generic fallback
            else:
                if hasattr(audio, "tags") and audio.tags is not None:
                    audio.tags["GENRE"] = genre_string
                    audio.save()
                    return True
        except Exception:
            return False

        return False

    @classmethod
    def write_tags(cls, file_path: Path, tags: Dict[str, Any]) -> bool:
        """Writes standard artist/title/album/track tags to an audio file."""
        try:
            audio = mutagen.File(file_path)
            if audio is None:
                return False

            if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
                if "title" in tags:
                    audio["TITLE"] = tags["title"]
                if "artist" in tags:
                    audio["ARTIST"] = tags["artist"]
                if "album" in tags:
                    audio["ALBUM"] = tags["album"]
                if "track" in tags:
                    audio["TRACKNUMBER"] = str(tags["track"])
                audio.save()
                return True

            elif isinstance(audio, mutagen.mp3.MP3) or (hasattr(audio, "tags") and isinstance(audio.tags, ID3)):
                if audio.tags is None:
                    audio.add_tags()
                if "title" in tags:
                    audio.tags["TIT2"] = TIT2(encoding=3, text=tags["title"])
                if "artist" in tags:
                    audio.tags["TPE1"] = TPE1(encoding=3, text=tags["artist"])
                if "album" in tags:
                    audio.tags["TALB"] = TALB(encoding=3, text=tags["album"])
                if "track" in tags:
                    audio.tags["TRCK"] = TRCK(encoding=3, text=str(tags["track"]))
                audio.tags.save(file_path)
                return True

            return False
        except Exception:
            return False
