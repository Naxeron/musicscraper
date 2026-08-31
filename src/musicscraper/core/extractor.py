"""
Safe archive extraction (.zip, .tar.gz, etc.) with traversal protection and Japanese encoding recovery.
"""

import os
import zipfile
import tarfile
from pathlib import Path
from typing import List, Optional


class ArchiveExtractor:
    """Safely unpacks downloaded music archives into organized directories."""

    @staticmethod
    def extract_zip(zip_path: Path, target_dir: Path) -> List[Path]:
        """Extracts a zip file with UTF-8 / CP932 / Latin-1 filename sanitization."""
        extracted_files: List[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    # Handle encoding quirks (Japanese zip files on Windows/Linux)
                    filename = member.filename
                    try:
                        filename = filename.encode("cp437").decode("utf-8")
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        try:
                            filename = filename.encode("cp437").decode("cp932")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            pass

                    # Prevent directory traversal attacks
                    clean_name = os.path.normpath(filename).lstrip(os.sep + "/")
                    if ".." in clean_name.split(os.sep):
                        continue

                    dest_file = target_dir / clean_name
                    if member.is_dir():
                        dest_file.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest_file, "wb") as dst:
                            dst.write(src.read())
                        extracted_files.append(dest_file)
        except Exception:
            pass
        return extracted_files

    @staticmethod
    def extract_tar(tar_path: Path, target_dir: Path) -> List[Path]:
        """Extracts tar/tar.gz files safely."""
        extracted_files: List[Path] = []
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tar_path, "r:*") as tf:
                for member in tf.getmembers():
                    clean_name = os.path.normpath(member.name).lstrip(os.sep + "/")
                    if ".." in clean_name.split(os.sep):
                        continue
                    dest_file = target_dir / clean_name
                    if member.isdir():
                        dest_file.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        f = tf.extractfile(member)
                        if f:
                            with open(dest_file, "wb") as dst:
                                dst.write(f.read())
                            extracted_files.append(dest_file)
        except Exception:
            pass
        return extracted_files

    @classmethod
    def unpack_all_archives(cls, directory: Path) -> int:
        """Finds all archives in directory and extracts them into subdirectories."""
        extracted_count = 0
        for root, _, files in os.walk(directory):
            for f in files:
                f_path = Path(root) / f
                if f.endswith(".zip"):
                    sub_dir = f_path.parent / f_path.stem
                    cls.extract_zip(f_path, sub_dir)
                    extracted_count += 1
                elif f.endswith((".tar.gz", ".tgz", ".tar.bz2")):
                    stem = f_path.stem.replace(".tar", "")
                    sub_dir = f_path.parent / stem
                    cls.extract_tar(f_path, sub_dir)
                    extracted_count += 1
        return extracted_count
