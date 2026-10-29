"""
voice_clipper.py - hotkey dialogue clipper for Windows (WASAPI loopback)

    F8            start / stop a clip
    F7            delete the clip you just saved
    Ctrl+Shift+Q  quit

Records whatever Windows is playing (VLC, MPC-HC, browser, anything) straight
from the digital output - no microphone, no Stereo Mix driver hunting.

The important difference from the naive version: audio is captured
CONTINUOUSLY into a small rolling buffer, so when you press F8 the clip
already includes the PRE_ROLL seconds before the keypress. You never lose the
first syllable to reaction time or to the ~200ms it takes WASAPI to open a
stream. A short POST_ROLL is kept on the tail for the same reason.

    pip install soundcard soundfile keyboard numpy

Run it from a normal Command Prompt:  python voice_clipper.py
If your video player runs elevated (as administrator), run this elevated too,
otherwise the global hotkeys won't reach it.
"""

import os
import queue
import threading
import time
from collections import deque

import numpy as np
import soundcard as sc
import soundfile as sf
import keyboard

# ---------------------------------------------------------------- settings --

SAMPLE_RATE = 48000
BLOCK       = 1024          # frames per read; ~21ms at 48kHz
PRE_ROLL    = 1.5           # seconds kept from BEFORE you press F8
POST_ROLL   = 0.5           # seconds kept AFTER you press F8 again
MAX_CLIP    = 600.0          # safety auto-stop, seconds
DEBOUNCE    = 0.25          # ignore repeat hotkey fires within this window

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_clips")

KEY_TOGGLE = "b"
KEY_UNDO   = "f7"
KEY_QUIT   = "ctrl+shift+q"

# ------------------------------------------------------------------ state ---

IDLE, CAPTURE, TAIL = 0, 1, 2

_state       = IDLE
_clip        = []
_tail_frames = 0
_running     = True
_last_toggle = 0.0

_ring = deque(maxlen=max(1, int(PRE_ROLL * SAMPLE_RATE / BLOCK)))
_lock = threading.RLock()

_save_q     = queue.Queue()
_saved      = []                    # written paths, newest last
_saved_lock = threading.Lock()
_next_index = 1                     # only ever touched by the writer thread


def find_loopback():
    """Pick the loopback device that mirrors the current default speaker."""
    mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    if not mics:
        raise RuntimeError(
            "No WASAPI loopback device found. Make sure you are on Windows and "
            "that an output device is enabled."
        )
    try:
        speaker = sc.default_speaker().name.lower()
    except Exception:
        speaker = ""
    for m in mics:
        n = m.name.lower()
        if speaker and (speaker in n or n in speaker):
            return m
    print("Could not match your default speaker. Loopback devices available:")
    for m in mics:
        print("   -", m.name)
    print("Falling back to the first one. Edit find_loopback() if that is wrong.\n")
    return mics[0]


def scan_next_index():
    """Resume numbering after the highest existing clip so reruns never overwrite."""
    highest = 0
    for name in os.listdir(OUTPUT_DIR):
        if name.startswith("clip_") and name.endswith(".wav"):
            stem = name[5:-4]
            if stem.isdigit():
                highest = max(highest, int(stem))
    return highest + 1


def writer_thread():
    """Files are written here so that disk I/O never stalls the capture loop."""
    global _next_index
    while True:
        audio = _save_q.get()
        if audio is None:
            _save_q.task_done()
            return
        try:
            mono = audio.mean(axis=1) if audio.ndim > 1 else audio
            seconds = len(mono) / SAMPLE_RATE
            peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
            path = os.path.join(OUTPUT_DIR, "clip_%04d.wav" % _next_index)
            sf.write(path, mono, SAMPLE_RATE, subtype="PCM_16")
            _next_index += 1
            with _saved_lock:
                _saved.append(path)
            db = 20 * np.log10(peak) if peak > 0 else -99.0
            warn = "   <- near silence, wrong output device?" if db < -50 else ""
            print("    saved %s   %.2fs   peak %.1f dBFS%s"
                  % (os.path.basename(path), seconds, db, warn))
        except Exception as exc:
            print("    could not save clip:", exc)
        finally:
            _save_q.task_done()


def recorder_thread(mic):
    """One long-lived WASAPI stream. Always recording, into ring or clip."""
    global _state, _clip, _tail_frames

    tail_target = max(1, int(POST_ROLL * SAMPLE_RATE / BLOCK))
    max_frames  = max(1, int(MAX_CLIP * SAMPLE_RATE / BLOCK))

    with mic.recorder(samplerate=SAMPLE_RATE, channels=[0, 1], blocksize=BLOCK) as rec:
        # Two channels then averaged to mono afterwards: soundcard documents a
        # WASAPI problem with asking for a single channel directly.
        print("listening on: %s\n" % mic.name)
        print_banner()
        while _running:
            data = rec.record(numframes=BLOCK)
            with _lock:
                if _state == IDLE:
                    _ring.append(data)
                    continue

                _clip.append(data)

                if _state == CAPTURE:
                    if len(_clip) >= max_frames:
                        print("    auto-stopped at %.0fs" % MAX_CLIP)
                        _state = TAIL
                        _tail_frames = 0
                elif _state == TAIL:
                    _tail_frames += 1
                    if _tail_frames >= tail_target:
                        _save_q.put(np.concatenate(_clip))
                        _clip = []
                        _state = IDLE
                        _ring.clear()


def toggle():
    global _state, _clip, _tail_frames, _last_toggle

    now = time.time()
    if now - _last_toggle < DEBOUNCE:      # key auto-repeat / double fire
        return
    _last_toggle = now

    with _lock:
        if _state == IDLE:
            _clip = list(_ring)            # the pre-roll head
            _ring.clear()
            _state = CAPTURE
            print("[*] recording   (+%.1fs pre-roll)" % PRE_ROLL)
        elif _state == CAPTURE:
            _state = TAIL
            _tail_frames = 0
            print("[ ] stopped")
        # TAIL: already finishing, ignore


def undo():
    with _saved_lock:
        if not _saved:
            print("    nothing to undo")
            return
        path = _saved.pop()
    try:
        os.remove(path)
        print("    deleted %s" % os.path.basename(path))
    except OSError as exc:
        print("    could not delete %s: %s" % (os.path.basename(path), exc))


def quit_all():
    global _running, _clip, _state
    _running = False
    time.sleep(0.2)                        # let the capture loop fall out
    with _lock:
        if _clip:                          # don't throw away a clip in progress
            _save_q.put(np.concatenate(_clip))
            _clip = []
            _state = IDLE
    _save_q.put(None)
    _save_q.join()
    print("done.")
    os._exit(0)


def print_banner():
    print("  %-14s start / stop a clip" % KEY_TOGGLE.upper())
    print("  %-14s delete the clip you just saved" % KEY_UNDO.upper())
    print("  %-14s quit" % KEY_QUIT.upper())
    print("  writing to %s\n" % OUTPUT_DIR)


def main():
    global _next_index

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _next_index = scan_next_index()
    if _next_index > 1:
        print("resuming at clip_%04d.wav\n" % _next_index)

    mic = find_loopback()

    threading.Thread(target=writer_thread, daemon=True).start()
    threading.Thread(target=recorder_thread, args=(mic,), daemon=True).start()

    keyboard.add_hotkey(KEY_TOGGLE, toggle)
    keyboard.add_hotkey(KEY_UNDO, undo)
    keyboard.add_hotkey(KEY_QUIT, quit_all)
    keyboard.wait()


if __name__ == "__main__":
    main()
