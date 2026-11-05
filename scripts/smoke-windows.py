r"""Manual hardware check: open loopback briefly; never mark or save audio.

Run from the repository: .venv\Scripts\python scripts/smoke-windows.py
"""

from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voiceclipper.capture import CaptureConfig, CaptureEngine


def main():
    ready = threading.Event()
    events = []

    def on_event(event):
        if event["type"] != "level":
            events.append(event)
        if event["type"] == "ready":
            ready.set()

    with tempfile.TemporaryDirectory(
        prefix="voice-clipper-smoke-", dir=Path(__file__).resolve().parent.parent
    ) as folder:
        engine = CaptureEngine(CaptureConfig(folder), on_event)
        try:
            engine.start()
            if not ready.wait(5):
                raise RuntimeError(f"Loopback did not become ready: {events}")
            deadline = time.monotonic() + 3
            while engine.frames_received < 1024 and time.monotonic() < deadline:
                time.sleep(0.02)
            frames = engine.frames_received
            if not frames:
                raise RuntimeError(f"Loopback opened but received no frames: {events}")
            if not engine.stop():
                raise RuntimeError("Loopback failed to stop cleanly")
            if list(Path(folder).rglob("*.wav")):
                raise AssertionError("Hardware check unexpectedly saved audio")
            print(
                f"PASS: real Windows loopback opened, received {frames} frames, stopped cleanly; no audio files saved."
            )
        finally:
            engine.stop()


if __name__ == "__main__":
    main()
