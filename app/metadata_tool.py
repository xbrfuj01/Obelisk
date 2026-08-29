import json
import os
import subprocess

# Filesystem facts about the temp file we wrote on our own server (its
# name, path, timestamps from the moment it landed on disk) - not metadata
# the file actually carries, and showing them would just be misleading
# (e.g. "FileModifyDate: <today>" implies something false about the
# original file's history).
EXCLUDED_TAGS = {
    "SourceFile", "Directory", "FileName", "FileSize", "FilePermissions",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "ExifToolVersion", "Warning", "Error",
}


def read_metadata(path):
    """Returns a {"Group:Tag": value} dict of every metadata field ExifTool
    can find - EXIF, XMP, IPTC, ICC, maker notes, C2PA/JUMBF content
    credentials where the installed ExifTool version supports it, and so
    on - or None if the file can't be read at all."""
    try:
        result = subprocess.run(
            ["exiftool", "-j", "-G1", "-a", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not data:
        return None

    tags = data[0]
    cleaned = {}
    for key, value in tags.items():
        base_key = key.split(":", 1)[-1]
        if base_key in EXCLUDED_TAGS:
            continue
        cleaned[key] = value
    return cleaned


def strip_metadata(input_path, output_path):
    """Writes a copy of input_path to output_path with every metadata tag
    ExifTool knows how to remove stripped out. Returns (True, None) on
    success or (False, error_text) if this format isn't writable."""
    try:
        result = subprocess.run(
            ["exiftool", "-all=", "-o", output_path, input_path],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Перевищено час очікування"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if result.returncode != 0 or not os.path.exists(output_path):
        return False, (result.stderr or "").strip()[:500]
    return True, None
