"""Writable data follows the portable executable; source runs use AppData."""
from pathlib import Path
import os
import sys


def data_directory() -> Path:
    if getattr(sys, "frozen", False):
        # __file__ points into PyInstaller's temporary extraction directory.
        # sys.executable points at the portable EXE the user actually launched.
        return Path(sys.executable).resolve().parent / "NC300Control-data"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NC300Control"
