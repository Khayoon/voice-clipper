"""GUI entry point for source checkouts and the Windows application bundle."""

import os
import sys
from pathlib import Path

_dll_directory_handles = []


def configure_bundled_runtime():
    """Make packaged VLC discoverable before python-vlc imports it."""
    runtime = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    vlc_dir = runtime / "vlc"
    if (vlc_dir / "libvlc.dll").is_file():
        os.environ["PYTHON_VLC_LIB_PATH"] = str(vlc_dir / "libvlc.dll")
        os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc_dir)
        os.environ["VLC_PLUGIN_PATH"] = str(vlc_dir / "plugins")
        os.environ["PATH"] = str(vlc_dir) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _dll_directory_handles.append(os.add_dll_directory(str(vlc_dir)))
    tools_dir = runtime / "tools" / "bin"
    if tools_dir.is_dir():
        os.environ["PATH"] = str(tools_dir) + os.pathsep + os.environ.get("PATH", "")


if __name__ == "__main__":
    configure_bundled_runtime()
    from voiceclipper.gui import main

    raise SystemExit(main())
