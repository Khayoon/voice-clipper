"""Optional Demucs vocal estimates with content-verified, resumable caching.

Captured originals are never changed. A previous output is reused only when its
source hash, output hash, model and processing settings still match.
"""

from __future__ import annotations
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
IN_DIR = HERE / "voice_clips"
OUT_DIR = HERE / "voice_clips_clean"
MODEL = "htdemucs"
BATCH = 16
TARGET_SR = 48000
MANIFEST_NAME = "separation_manifest.json"


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def processing_settings(model=MODEL, target_sr=TARGET_SR):
    try:
        version = metadata.version("demucs")
    except metadata.PackageNotFoundError:
        version = "not-installed"
    return {
        "model": model,
        "target_sr": target_sr,
        "demucs_version": version,
        "stems": "vocals",
        "channels": "mono-average",
        "subtype": "PCM_16",
    }


def load_manifest(output_dir):
    try:
        data = json.loads(
            (Path(output_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        if data.get("version") == 1 and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": 1, "files": {}}


def save_manifest(output_dir, manifest):
    path = Path(output_dir) / MANIFEST_NAME
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cache_current(source, output, entry, settings):
    if not isinstance(entry, dict) or entry.get("settings") != settings:
        return False
    if not Path(output).is_file():
        return False
    try:
        return entry.get("source_sha256") == file_hash(source) and entry.get(
            "output_sha256"
        ) == file_hash(output)
    except OSError:
        return False


def pick_device():
    try:
        import torch

        if torch.cuda.is_available():
            print("Using GPU: %s" % torch.cuda.get_device_name(0))
            return "cuda"
    except ImportError:
        pass
    print("Using CPU for vocal separation.")
    return "cpu"


def to_mono_at(path, target_sr):
    import soundfile as sf

    audio, sr = sf.read(str(path), always_2d=True)
    mono = audio.mean(axis=1)
    if target_sr and sr != target_sr:
        try:
            import soxr
        except ImportError as exc:
            raise RuntimeError(
                "Install soxr to produce the requested sample rate: pip install soxr"
            ) from exc
        mono = soxr.resample(mono, sr, target_sr)
        sr = target_sr
    return mono, sr


def separate(
    input_dir=IN_DIR,
    output_dir=OUT_DIR,
    *,
    model=MODEL,
    batch=BATCH,
    target_sr=TARGET_SR,
    device=None,
    runner=None,
):
    import soundfile as sf

    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if input_dir == output_dir:
        raise ValueError("Choose a separate output folder so originals are preserved.")
    if not input_dir.is_dir():
        raise ValueError(f"No input folder: {input_dir}")
    if batch < 1 or (target_sr is not None and target_sr < 100):
        raise ValueError("Batch size and sample rate must be positive.")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(output_dir)
    settings = processing_settings(model, target_sr)
    todo = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_file()
        and path.suffix.lower() == ".wav"
        and not cache_current(
            path, output_dir / path.name, manifest["files"].get(path.name), settings
        )
    ]
    if not todo:
        print("Nothing new or changed to process.")
        return 0
    device = device or pick_device()
    runner = runner or subprocess.run
    processed = 0
    print(f"Separating {len(todo)} new/changed clip(s).")
    for start in range(0, len(todo), batch):
        items = todo[start : start + batch]
        source_hashes = {path.name: file_hash(path) for path in items}
        with tempfile.TemporaryDirectory(prefix="voice_clipper_demucs_") as temp_dir:
            command = [
                sys.executable,
                "-m",
                "demucs",
                "--two-stems=vocals",
                "-n",
                model,
                "-d",
                device,
                "-o",
                temp_dir,
                *map(str, items),
            ]
            result = runner(command)
            if result.returncode:
                raise RuntimeError(
                    f"Demucs failed (exit {result.returncode}). Install requirements-vocals.txt and check its output."
                )
            for source in items:
                vocals = Path(temp_dir) / model / source.stem / "vocals.wav"
                if not vocals.is_file():
                    raise RuntimeError(
                        f"Demucs did not create a vocal estimate for {source.name}."
                    )
                mono, sr = to_mono_at(vocals, target_sr)
                if file_hash(source) != source_hashes[source.name]:
                    raise RuntimeError(
                        f"{source.name} changed during separation. Run again to process the new source."
                    )
                destination = output_dir / source.name
                temporary = output_dir / f".{source.stem}.{uuid.uuid4().hex}.tmp.wav"
                try:
                    sf.write(str(temporary), mono, sr, subtype="PCM_16")
                    output_hash = file_hash(temporary)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                manifest["files"][source.name] = {
                    "source_sha256": source_hashes[source.name],
                    "output_sha256": output_hash,
                    "settings": settings,
                    "actual_sample_rate": sr,
                }
                save_manifest(output_dir, manifest)
                processed += 1
                print(f"  Wrote vocal estimate: {source.name}")
    print(f"{processed} clip(s) written to {output_dir}")
    return processed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=IN_DIR)
    parser.add_argument("--output", type=Path, default=OUT_DIR)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=TARGET_SR,
        help="Output Hz; use 0 to keep the model's native rate.",
    )
    parser.add_argument("--device", choices=("cpu", "cuda"))
    args = parser.parse_args(argv)
    try:
        separate(
            args.input,
            args.output,
            model=args.model,
            batch=args.batch,
            target_sr=args.sample_rate or None,
            device=args.device,
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
