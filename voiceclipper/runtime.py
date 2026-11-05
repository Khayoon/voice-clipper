"""Discover private bundled tools without changing system configuration."""

from __future__ import annotations

import os
from pathlib import Path
import sys

_dll_handles = []


def prepare_vlc() -> Path | None:
    roots = [
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "vlc"
    ]
    for key in ("VOICE_CLIPPER_VLC", "VLC_HOME"):
        if os.environ.get(key):
            roots.insert(0, Path(os.environ[key]))
    if sys.platform == "win32":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(key):
                roots.append(Path(os.environ[key]) / "VideoLAN" / "VLC")
    for root in roots:
        dll = root / ("libvlc.dll" if sys.platform == "win32" else "libvlc.so")
        if dll.is_file():
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                _dll_handles.append(os.add_dll_directory(str(root)))
            os.environ["PATH"] = str(root) + os.pathsep + os.environ.get("PATH", "")
            os.environ["PYTHON_VLC_LIB_PATH"] = str(dll)
            os.environ["PYTHON_VLC_MODULE_PATH"] = str(root)
            os.environ["VLC_PLUGIN_PATH"] = str(root / "plugins")
            return root
    return None
