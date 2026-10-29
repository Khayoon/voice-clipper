"""
isolate_vocals.py - strip score, foley and explosions out of the clips,
leaving just the voice. Runs Demucs on your RTX 3060.

This is what makes the whole workflow practical: instead of hunting for lines
that happen to have no music under them, grab any line you want and separate
it afterwards.

    pip install demucs soxr
    (Demucs pulls in torch. For GPU, install the CUDA build of torch first:
     https://pytorch.org - otherwise this falls back to CPU and is ~20x slower.)

    python isolate_vocals.py

Reads  voice_clips/*.wav
Writes voice_clips_clean/*.wav   (mono, same filenames)

Already-processed files are skipped, so you can rerun it after each movie.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE       = os.path.dirname(os.path.abspath(__file__))
IN_DIR     = os.path.join(HERE, "voice_clips")
OUT_DIR    = os.path.join(HERE, "voice_clips_clean")
MODEL      = "htdemucs"
BATCH      = 16          # files per demucs invocation
TARGET_SR  = 48000       # set to None to keep Demucs' native 44100


def pick_device():
    try:
        import torch
        if torch.cuda.is_available():
            print("using GPU: %s" % torch.cuda.get_device_name(0))
            return "cuda"
        print("no CUDA build of torch found - falling back to CPU (slow).")
    except ImportError:
        print("torch not importable - is demucs installed?")
    return "cpu"


def to_mono_at(path, target_sr):
    audio, sr = sf.read(path, always_2d=True)
    mono = audio.mean(axis=1)
    if target_sr and sr != target_sr:
        try:
            import soxr
            mono = soxr.resample(mono, sr, target_sr)
            sr = target_sr
        except ImportError:
            pass          # keep native rate; `pip install soxr` to normalise
    return mono, sr


def main():
    if not os.path.isdir(IN_DIR):
        sys.exit("No %s folder yet - record some clips first." % IN_DIR)

    os.makedirs(OUT_DIR, exist_ok=True)

    todo = sorted(
        name for name in os.listdir(IN_DIR)
        if name.lower().endswith(".wav")
        and not os.path.exists(os.path.join(OUT_DIR, name))
    )
    if not todo:
        print("Nothing new to process.")
        return

    device = pick_device()
    print("%d clip(s) to separate\n" % len(todo))

    done = 0
    warned_rate = False

    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        tmp = tempfile.mkdtemp(prefix="demucs_")
        try:
            cmd = [
                sys.executable, "-m", "demucs",
                "--two-stems=vocals",
                "-n", MODEL,
                "-d", device,
                "-o", tmp,
            ] + [os.path.join(IN_DIR, name) for name in batch]

            result = subprocess.run(cmd)
            if result.returncode != 0:
                sys.exit("demucs failed (exit %d). Is it installed? pip install demucs"
                         % result.returncode)

            for name in batch:
                stem = os.path.splitext(name)[0]
                vocals = os.path.join(tmp, MODEL, stem, "vocals.wav")
                if not os.path.exists(vocals):
                    print("  !! no vocals stem for %s" % name)
                    continue
                mono, sr = to_mono_at(vocals, TARGET_SR)
                sf.write(os.path.join(OUT_DIR, name), mono, sr, subtype="PCM_16")
                if TARGET_SR and sr != TARGET_SR and not warned_rate:
                    print("  note: output is %d Hz. `pip install soxr` to get %d Hz."
                          % (sr, TARGET_SR))
                    warned_rate = True
                done += 1
                print("  cleaned %s" % name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d clip(s) written to %s" % (done, OUT_DIR))


if __name__ == "__main__":
    main()
