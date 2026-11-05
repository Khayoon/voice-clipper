"""A quiet runtime check for portable builds; never captures audio."""

import json
import faulthandler
from pathlib import Path
import subprocess
import sys

from PySide6.QtWidgets import QApplication, QFrame
from .export import find_tool
from .player import VlcBackend
from .capture import CaptureEngine


def write_report(path):
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": False,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    player = None

    def checkpoint(stage):
        report["stage"] = stage
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")

    stack_file = target.with_suffix(".stack.txt").open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(15, file=stack_file)
    try:
        checkpoint("Loading Qt")
        _app = QApplication.instance() or QApplication([])
        frame = QFrame()
        checkpoint("Loading libVLC")
        player = VlcBackend(frame)
        report["vlc_version"] = player.vlc.libvlc_get_version().decode(
            "utf-8", errors="replace"
        )
        for tool in ("ffmpeg", "ffprobe"):
            checkpoint("Checking " + tool)
            binary = find_tool(tool)
            options = (
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if sys.platform == "win32"
                else {}
            )
            result = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                text=True,
                timeout=15,
                **options,
            )
            if result.returncode:
                raise RuntimeError(f"{tool} failed to start")
            report[tool] = {"path": binary, "version": result.stdout.splitlines()[0]}
        if sys.platform == "win32":
            checkpoint("Discovering loopback devices")
            report["loopback_devices"] = len(CaptureEngine.list_devices())
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if player:
            checkpoint("Releasing libVLC")
            player.close()
        faulthandler.cancel_dump_traceback_later()
        stack_file.close()
    checkpoint("Complete")
    return 0 if report["ok"] else 1
