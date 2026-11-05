"""FFmpeg-based source extraction, metadata exports, and waveform previews."""

from __future__ import annotations

from array import array
import csv
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable
import uuid
import wave

from .models import Clip, atomic_json


class ExportError(RuntimeError):
    """An actionable media/export failure for display in the GUI."""


class ExportCancelled(ExportError):
    """The user cancelled an export; completed files are retained."""


def find_tool(name: str, provided: str | Path | None = None) -> str:
    """Find FFmpeg tools in an explicit path, bundled tools, or PATH."""
    if provided:
        candidate = Path(provided).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(str(provided))
        if resolved:
            return resolved
        raise ExportError(f"Cannot find {name}: {provided}")
    suffix = ".exe" if os.name == "nt" else ""
    roots = [Path(sys.executable).parent, Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        for folder in (root, root / "tools" / "bin", root / "tools", root / "bin"):
            candidate = folder / (name + suffix)
            if candidate.is_file():
                return str(candidate)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise ExportError(
        f"{name} is required. Install FFmpeg and add its bin folder to PATH, or place {name + suffix} in tools/bin beside Voice Clipper."
    )


def _process_options() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _source_file(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ExportError(f"Source file is missing: {source}")
    return source


def media_info(path: str | Path, ffprobe_path: str | Path | None = None) -> dict:
    source = _source_file(path)
    command = [
        find_tool("ffprobe", ffprobe_path),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=30, **_process_options()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"Could not inspect {source.name}: {exc}") from exc
    if result.returncode:
        raise ExportError(
            f"Cannot read {source.name}: {result.stderr.decode('utf-8', errors='replace')[-3000:].strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ExportError(
            f"ffprobe returned invalid media information for {source.name}."
        ) from exc
    durations = [data.get("format", {}).get("duration")]
    durations.extend(stream.get("duration") for stream in data.get("streams", []))
    duration = 0.0
    for value in durations:
        try:
            candidate = float(value)
            if math.isfinite(candidate) and candidate >= 0:
                duration = max(duration, candidate)
        except (ValueError, TypeError):
            continue
    audio_tracks = []
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags", {})
        audio_tracks.append(
            {
                "index": int(stream["index"]),
                "ordinal": len(audio_tracks),
                "language": tags.get("language", "und"),
                "title": tags.get("title", ""),
                "codec": stream.get("codec_name", ""),
                "channels": stream.get("channels", 0),
                "sample_rate": stream.get("sample_rate", ""),
            }
        )
    return {
        "duration_ms": round(duration * 1000),
        "audio_tracks": audio_tracks,
        "has_video": any(
            stream.get("codec_type") == "video" for stream in data.get("streams", [])
        ),
    }


def _run_export(command: list[str], cancelled: Callable[[], bool] | None) -> None:
    # FFmpeg's stderr can exceed the size of a pipe when working with damaged
    # media. A temporary file avoids deadlocks and unbounded in-memory logs.
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                **_process_options(),
            )
        except OSError as exc:
            raise ExportError(f"Could not start FFmpeg: {exc}") from exc
        try:
            while process.poll() is None:
                if cancelled and cancelled():
                    raise ExportCancelled(
                        "Export cancelled. Completed clips have been kept."
                    )
                time.sleep(0.05)
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4000))
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise ExportError(f"FFmpeg could not export this clip: {detail}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _atomic_text(path: Path, callback: Callable) -> None:
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            callback(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def export_clips(
    clips: Iterable[Clip],
    output_dir: str | Path,
    sample_rate: int = 24000,
    ffmpeg_path: str | Path | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    """Export non-rejected selections as mono PCM16 WAV plus text manifests.

    Returned paths contain only the WAV files. Re-exporting a stable clip ID
    replaces that exported derivative atomically, never its original media.
    Anki's WAV files must be copied into its collection.media directory before
    importing anki.tsv; see Anki's text-import instructions.
    """
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not 8000 <= sample_rate <= 192000
    ):
        raise ValueError("The export sample rate must be between 8000 and 192000 Hz.")
    selected = [clip for clip in clips if clip.status != "rejected"]
    if not selected:
        raise ExportError("There are no non-rejected clips to export.")
    seen = set()
    for clip in selected:
        clip.validate()
        if clip.id in seen:
            raise ValueError(f"Duplicate clip ID: {clip.id}")
        seen.add(clip.id)
    ffmpeg = find_tool("ffmpeg", ffmpeg_path)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sources = {_source_file(clip.source) for clip in selected}
    outputs = [destination / f"clip_{clip.id}.wav" for clip in selected]
    metadata_names = ("clips.csv", "clips.jsonl", "clips.json", "anki.tsv")
    if any(
        path.resolve() in sources
        for path in outputs + [destination / name for name in metadata_names]
    ):
        raise ExportError(
            "The export would replace an original source. Choose a different export folder."
        )
    records = []
    for position, (clip, output) in enumerate(zip(selected, outputs)):
        if cancelled and cancelled():
            raise ExportCancelled("Export cancelled. Completed clips have been kept.")
        if progress:
            progress(position, len(selected), f"Exporting {clip.display_name}")
        temporary = destination / f".{output.stem}.{uuid.uuid4().hex}.tmp.wav"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{clip.start_ms / 1000:.3f}",
            "-i",
            str(Path(clip.source).resolve()),
            "-t",
            f"{clip.duration_ms / 1000:.3f}",
            "-map",
            f"0:a:{clip.audio_track}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
        try:
            _run_export(command, cancelled)
            if not temporary.is_file() or temporary.stat().st_size <= 44:
                raise ExportError(
                    f"{clip.display_name} produced no audio. Check its track and timestamps."
                )
            with wave.open(str(temporary), "rb") as audio:
                actual_frames = audio.getnframes()
            expected_frames = round(clip.duration_ms / 1000 * sample_rate)
            if abs(actual_frames - expected_frames) > sample_rate * 0.05:
                raise ExportError(
                    f"{clip.display_name} is shorter than its marked range. Check the source and end timestamp."
                )
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        record = clip.to_dict()
        record.update(
            {
                "file": output.name,
                "duration_ms": clip.duration_ms,
                "sample_rate": sample_rate,
                "channels": 1,
                "subtype": "PCM_16",
            }
        )
        records.append(record)
        if progress:
            progress(position + 1, len(selected), f"Saved {output.name}")
    if cancelled and cancelled():
        raise ExportCancelled("Export cancelled. Completed clips have been kept.")
    columns = [
        "id",
        "file",
        "name",
        "speaker",
        "language",
        "transcript",
        "translation",
        "status",
        "source",
        "start_ms",
        "end_ms",
        "audio_track",
        "duration_ms",
        "sample_rate",
        "channels",
        "subtype",
        "created_at",
        "processing",
    ]

    def write_csv(stream):
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in records:
            entry = dict(record)
            entry["processing"] = json.dumps(entry["processing"], ensure_ascii=False)
            writer.writerow(entry)

    def write_jsonl(stream):
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def write_anki(stream):
        stream.write(
            "#separator:tab\n#html:true\n#columns:Phrase\tTranslation\tAudio\tTags\n"
        )
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        for clip, record in zip(selected, records):
            tags = " ".join(
                re.sub(r"\s+", "_", value.strip())
                for value in (clip.language, clip.speaker)
                if value.strip()
            )
            writer.writerow(
                [
                    html.escape(clip.transcript or clip.display_name).replace(
                        "\n", "<br>"
                    ),
                    html.escape(clip.translation).replace("\n", "<br>"),
                    f"[sound:{record['file']}]",
                    tags,
                ]
            )

    _atomic_text(destination / "clips.csv", write_csv)
    _atomic_text(destination / "clips.jsonl", write_jsonl)
    atomic_json(destination / "clips.json", {"schema_version": 1, "clips": records})
    _atomic_text(destination / "anki.tsv", write_anki)
    return outputs


def get_waveform(
    source: str | Path,
    start_ms: int,
    end_ms: int,
    audio_track: int = 0,
    bins: int = 500,
) -> list[float]:
    """Return peak amplitudes using bounded memory even for long selections."""
    clip = Clip(
        source=str(source), start_ms=start_ms, end_ms=end_ms, audio_track=audio_track
    )
    if isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= 10000:
        raise ValueError("Waveform bins must be between 1 and 10000.")
    path = _source_file(source)
    sample_rate = 8000
    expected_samples = max(1, round(clip.duration_ms / 1000 * sample_rate))
    peaks = [0.0] * bins
    command = [
        find_tool("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(path),
        "-t",
        f"{clip.duration_ms / 1000:.3f}",
        "-map",
        f"0:a:{audio_track}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-c:a",
        "pcm_f32le",
        "pipe:1",
    ]
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                **_process_options(),
            )
        except OSError as exc:
            raise ExportError(f"Could not start waveform preview: {exc}") from exc
        count = 0
        carry = b""
        try:
            while True:
                chunk = process.stdout.read(32768)
                if not chunk:
                    break
                chunk = carry + chunk
                usable = len(chunk) // 4 * 4
                samples = array("f")
                samples.frombytes(chunk[:usable])
                if sys.byteorder != "little":
                    samples.byteswap()
                carry = chunk[usable:]
                for value in samples:
                    index = min(bins - 1, count * bins // expected_samples)
                    if math.isfinite(value):
                        peaks[index] = max(peaks[index], abs(value))
                    count += 1
            process.wait(timeout=10)
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 2000))
                raise ExportError(
                    f"Cannot preview waveform: {errors.read().decode('utf-8', errors='replace').strip()}"
                )
        finally:
            if process.stdout:
                process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    return peaks
