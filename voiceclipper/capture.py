"""Threaded WASAPI capture with continuous history and recoverable discard.

SoundCard is loaded only when devices are listed or capture starts. The buffer
logic accepts ordinary NumPy blocks, which also makes it testable without a
Windows audio device. No disk work runs in the recording thread.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import queue
import threading
import time
import warnings
from typing import Callable
import uuid

import numpy as np


@dataclass
class CaptureConfig:
    output_dir: Path | str = field(default_factory=lambda: Path.cwd() / "voice_clips")
    sample_rate: int = 48000
    block_size: int = 1024
    pre_roll: float = 1.5
    post_roll: float = 0.5
    history_seconds: float = 30.0
    max_clip: float = 600.0

    def __post_init__(self):
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if not isinstance(self.sample_rate, int) or self.sample_rate < 100:
            raise ValueError(
                "Sample rate must be a positive integer of at least 100 Hz."
            )
        if not isinstance(self.block_size, int) or self.block_size < 1:
            raise ValueError("Block size must be a positive integer.")
        for key in ("pre_roll", "post_roll", "history_seconds", "max_clip"):
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be a finite, non-negative number.")
        if self.history_seconds <= 0 or self.max_clip <= 0:
            raise ValueError("History and maximum clip duration must be positive.")
        if self.history_seconds < self.pre_roll:
            raise ValueError("History must be at least as long as pre-roll.")


@dataclass
class _Segment:
    clip_id: str
    blocks: list = field(default_factory=list)
    samples: int = 0
    tail_remaining: int = 0
    discarded: bool = False
    queued: bool = False
    written: bool = False
    failed: bool = False
    path: Path | None = None


class CaptureEngine:
    """One audio stream, a continuous ring buffer, and one serial disk writer.

    ``on_event`` is called on worker threads; GUI clients should forward events
    through their toolkit's queued signal mechanism. ``recorder_factory`` is an
    optional adapter for tests: ``factory(device_id, config)`` returns a recorder
    context manager with ``record(numframes=...)``.
    """

    def __init__(
        self,
        config: CaptureConfig,
        on_event: Callable = lambda event: None,
        *,
        recorder_factory=None,
    ):
        self.config = config
        self.on_event = on_event
        self._recorder_factory = recorder_factory
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready = False
        self._history = deque()
        self._history_frames = 0
        self._frames_received = 0
        self._current = None
        self._tails = []
        self._segments = []
        self._discarded = []
        self._jobs = queue.Queue()
        self._recorder_thread = None
        self._writer_thread = None
        self._last_level = 0.0
        self._last_warning = 0.0
        self._quality_warnings = set()

    def _emit(self, kind, **fields):
        try:
            self.on_event({"type": kind, **fields})
        except Exception:
            # A UI callback must never kill audio capture or the disk writer.
            pass

    @property
    def ready(self):
        with self._lock:
            return self._ready

    @property
    def recording(self):
        with self._lock:
            return self._current is not None

    @property
    def frames_received(self):
        with self._lock:
            return self._frames_received

    @property
    def discarded_ids(self):
        with self._lock:
            return {
                segment.clip_id for segment in self._discarded if not segment.failed
            }

    @property
    def has_discarded(self):
        return bool(self.discarded_ids)

    @staticmethod
    def list_devices():
        import soundcard as sc

        speaker = sc.default_speaker()
        default_id = str(speaker.id) if speaker else ""
        return [
            {"id": str(m.id), "name": m.name, "default": str(m.id) == default_id}
            for m in sc.all_microphones(include_loopback=True)
            if m.isloopback
        ]

    def _open_recorder(self, device_id):
        if self._recorder_factory is not None:
            rec = self._recorder_factory(device_id, self.config)
            return rec, getattr(rec, "name", "Audio input")
        import soundcard as sc

        devices = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        if not devices:
            raise RuntimeError(
                "No WASAPI loopback device found. Enable a Windows output device."
            )
        if device_id is not None:
            matches = [m for m in devices if str(m.id) == str(device_id)]
            if not matches:
                raise RuntimeError("The selected output device is no longer available.")
            mic = matches[0]
        else:
            speaker = sc.default_speaker()
            matches = [m for m in devices if speaker and str(m.id) == str(speaker.id)]
            if not matches and speaker:
                matches = [m for m in devices if m.name == speaker.name]
            if not matches and len(devices) == 1:
                matches = devices
            if not matches:
                raise RuntimeError(
                    "Cannot match the default output. Select a loopback device explicitly."
                )
            mic = matches[0]
        # SoundCard's WASAPI backend has a single-channel capture issue. For
        # genuinely mono outputs, duplicate channel zero before downmixing.
        channels = [0, 1] if getattr(mic, "channels", 2) >= 2 else [0, 0]
        return mic.recorder(
            samplerate=self.config.sample_rate,
            channels=channels,
            blocksize=self.config.block_size,
        ), mic.name

    def start(self, device_id=None):
        with self._lifecycle_lock:
            if self._recorder_thread and self._recorder_thread.is_alive():
                raise RuntimeError("Capture is already running.")
            if self._writer_thread and self._writer_thread.is_alive():
                raise RuntimeError(
                    "Stop the previous capture session before restarting."
                )
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            self._stop_event.clear()
            with self._lock:
                self._ready = False
                self._history.clear()
                self._history_frames = self._frames_received = 0
                self._current = None
                self._tails.clear()
                self._quality_warnings.clear()
                self._jobs = queue.Queue()
            self._writer_thread = threading.Thread(
                target=self._write_loop, name="voice-clipper-writer", daemon=True
            )
            self._recorder_thread = threading.Thread(
                target=self._record_loop,
                args=(device_id,),
                name="voice-clipper-audio",
                daemon=True,
            )
            self._writer_thread.start()
            self._recorder_thread.start()

    def stop(self):
        """Stop reading, flush marked audio, and join workers in that order.

        A five-second join limit reports a stuck device driver instead of
        silently claiming capture stopped. No writer sentinel is enqueued until
        the producer has exited, so a final marked segment cannot be lost.
        """
        with self._lifecycle_lock:
            self._stop_event.set()
            recorder = self._recorder_thread
            if recorder and recorder is not threading.current_thread():
                recorder.join(timeout=5)
                if recorder.is_alive():
                    self._emit(
                        "error",
                        message="Audio driver did not stop; capture is still shutting down.",
                        fatal=True,
                    )
                    return False
            with self._lock:
                self._ready = False
                self._flush_pending()
            writer = self._writer_thread
            if writer and writer.is_alive():
                self._jobs.put(None)
                if writer is not threading.current_thread():
                    writer.join(timeout=30)
                    if writer.is_alive():
                        self._emit(
                            "error",
                            message="Clip writer is still saving. Keep the application open.",
                            fatal=True,
                        )
                        return False
            self._emit("state", state="stopped")
            return True

    def _record_loop(self, device_id):
        try:
            recorder, name = self._open_recorder(device_id)
            with recorder as rec:
                if self._stop_event.is_set():
                    return
                with self._lock:
                    self._ready = True
                self._emit("ready", device=name)
                self._emit("state", state="idle")
                while not self._stop_event.is_set():
                    with warnings.catch_warnings(record=True) as notices:
                        warnings.simplefilter("always")
                        data = rec.record(numframes=self.config.block_size)
                    for notice in notices:
                        detail = str(notice.message)
                        with self._lock:
                            self._quality_warnings.add(detail)
                        now = time.monotonic()
                        if now - self._last_warning >= 5:
                            self._last_warning = now
                            self._emit(
                                "warning",
                                message="Audio stream warning: "
                                + detail
                                + ". Review captured audio.",
                            )
                    if not self._stop_event.is_set():
                        self.feed_block(data)
        except Exception as exc:
            with self._lock:
                self._ready = False
            self._stop_event.set()
            self._emit("error", message=f"Audio capture failed: {exc}", fatal=True)
        finally:
            with self._lock:
                self._ready = False
                self._flush_pending()
            self._emit("state", state="stopped")

    def _snapshot(self, frames):
        if not self._history or frames <= 0:
            return []
        needed = min(frames, self._history_frames)
        selected = []
        for block in reversed(self._history):
            if needed <= 0:
                break
            piece = block[-needed:] if len(block) > needed else block
            selected.append(piece)
            needed -= len(piece)
        return list(reversed(selected))

    def _new_segment(self, blocks):
        segment = _Segment(uuid.uuid4().hex, list(blocks), sum(map(len, blocks)))
        self._segments.append(segment)
        return segment

    def _queue_save(self, segment):
        if not segment.queued and segment.samples:
            segment.queued = True
            self._jobs.put(("save", segment))

    def _state_event(self):
        state = "recording" if self._current else "finishing" if self._tails else "idle"
        self._emit("state", state=state)

    def toggle(self):
        with self._lock:
            if not self._ready:
                self._emit(
                    "error",
                    message="Capture is not ready. Start listening and check the output device.",
                    fatal=False,
                )
                return None
            if self._current:
                segment, self._current = self._current, None
                segment.tail_remaining = int(
                    round(self.config.post_roll * self.config.sample_rate)
                )
                if segment.tail_remaining:
                    self._tails.append(segment)
                else:
                    self._queue_save(segment)
            else:
                # A prior clip may still be finishing. It remains a separate
                # segment while the next one starts with a fully warm buffer.
                blocks = self._snapshot(
                    int(round(self.config.pre_roll * self.config.sample_rate))
                )
                segment = self._new_segment(blocks)
                self._current = segment
            self._state_event()
            return segment.clip_id

    def save_last(self, seconds):
        if (
            not math.isfinite(seconds)
            or seconds <= 0
            or seconds > self.config.history_seconds
        ):
            raise ValueError(
                f"Duration must be between 0 and {self.config.history_seconds:g} seconds."
            )
        with self._lock:
            if not self._ready:
                self._emit("error", message="Capture is not ready.", fatal=False)
                return None
            blocks = self._snapshot(int(round(seconds * self.config.sample_rate)))
            if not blocks:
                self._emit(
                    "error",
                    message="The audio history is empty. Let playback run first.",
                    fatal=False,
                )
                return None
            segment = self._new_segment(blocks)
            self._queue_save(segment)
            return segment.clip_id

    def feed_block(self, data):
        """Process one immutable audio block (also usable by recorder adapters)."""
        block = np.asarray(data, dtype=np.float32)
        if block.ndim not in (1, 2):
            raise ValueError("Audio blocks must have one or two dimensions.")
        if not len(block):
            return
        if not np.isfinite(block).all():
            raise ValueError("Audio block contains non-finite samples.")
        block = block.copy()
        with self._lock:
            if not self._ready:
                return
            self._history.append(block)
            self._history_frames += len(block)
            self._frames_received += len(block)
            history_limit = int(
                round(self.config.history_seconds * self.config.sample_rate)
            )
            while self._history_frames > history_limit:
                extra = self._history_frames - history_limit
                first = self._history.popleft()
                if len(first) > extra:
                    self._history.appendleft(first[extra:])
                    self._history_frames -= extra
                else:
                    self._history_frames -= len(first)
            finished = False
            for segment in list(self._tails):
                piece = block[: segment.tail_remaining]
                segment.blocks.append(piece)
                segment.samples += len(piece)
                segment.tail_remaining -= len(piece)
                if segment.tail_remaining <= 0:
                    self._tails.remove(segment)
                    self._queue_save(segment)
                    finished = True
            if self._current:
                segment = self._current
                remaining = (
                    int(self.config.max_clip * self.config.sample_rate)
                    - segment.samples
                )
                piece = block[: max(0, remaining)]
                if len(piece):
                    segment.blocks.append(piece)
                    segment.samples += len(piece)
                if segment.samples >= int(
                    self.config.max_clip * self.config.sample_rate
                ):
                    self._current = None
                    self._queue_save(segment)
                    finished = True
                    self._emit(
                        "message",
                        message=f"Auto-stopped after {self.config.max_clip:g} seconds.",
                    )
            if finished:
                self._state_event()
        now = time.monotonic()
        if now - self._last_level >= 0.1:
            self._last_level = now
            self._emit(
                "level",
                peak=float(np.max(np.abs(block))),
                rms=float(np.sqrt(np.mean(block.astype(np.float64) ** 2))),
            )

    def _flush_pending(self):
        if self._current:
            self._queue_save(self._current)
            self._current = None
        for segment in self._tails:
            self._queue_save(segment)
        self._tails.clear()

    def _path_for(self, segment):
        folder = (
            self.config.output_dir / ".trash"
            if segment.discarded
            else self.config.output_dir
        )
        return folder / f"clip_{segment.clip_id}.wav"

    def _sync_segment(self, segment):
        target = self._path_for(segment)
        if segment.path != target and segment.path and segment.path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(segment.path, target)
            segment.path = target

    def _request_sync(self, segment):
        if self._writer_thread and self._writer_thread.is_alive():
            self._jobs.put(("sync", segment))
        else:
            # Review may continue after listening has stopped.
            try:
                self._sync_segment(segment)
            except OSError as exc:
                self._emit(
                    "error",
                    message=f"Could not move discarded/restored clip: {exc}",
                    fatal=False,
                )

    def discard_latest(self):
        with self._lock:
            segment = next(
                (
                    s
                    for s in reversed(self._segments)
                    if not s.discarded and not s.failed
                ),
                None,
            )
            if segment is None:
                return None
            segment.discarded = True
            self._discarded.append(segment)
            if segment is self._current:
                self._current = None
            if segment in self._tails:
                self._tails.remove(segment)
            if not segment.queued:
                self._queue_save(segment)
            if segment.written:
                self._request_sync(segment)
            self._emit(
                "discarded",
                clip_id=segment.clip_id,
                path=str(self._path_for(segment)),
                duration=segment.samples / self.config.sample_rate,
            )
            self._state_event()
            return segment.clip_id

    def restore_last(self):
        with self._lock:
            if not self._discarded:
                return None
            return self.restore_clip(self._discarded[-1].clip_id)

    def restore_clip(self, clip_id):
        """Restore a chosen rejected take, including a save still in flight."""
        with self._lock:
            segment = next(
                (item for item in self._discarded if item.clip_id == clip_id), None
            )
            if segment is None:
                return None
            self._discarded.remove(segment)
            segment.discarded = False
            if segment.written:
                self._request_sync(segment)
            self._emit(
                "restored",
                clip_id=segment.clip_id,
                path=str(self._path_for(segment)),
                duration=segment.samples / self.config.sample_rate,
            )
            return segment.clip_id

    def _write_loop(self):
        while True:
            job = self._jobs.get()
            temporary = None
            try:
                if job is None:
                    return
                action, segment = job
                if action == "save":
                    import soundfile as sf

                    with self._lock:
                        blocks = list(segment.blocks)
                    audio = np.concatenate(blocks)
                    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
                    peak = float(np.max(np.abs(mono)))
                    temporary = (
                        self.config.output_dir
                        / f".clip_{segment.clip_id}.{uuid.uuid4().hex}.tmp.wav"
                    )
                    sf.write(
                        str(temporary), mono, self.config.sample_rate, subtype="PCM_16"
                    )
                    with self._lock:
                        target = self._path_for(segment)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temporary, target)
                        temporary = None
                        segment.path, segment.written = target, True
                        segment.blocks.clear()
                        if not segment.discarded:
                            self._emit(
                                "saved",
                                clip_id=segment.clip_id,
                                path=str(target),
                                duration=segment.samples / self.config.sample_rate,
                                peak=peak,
                                near_silence=peak < 0.0032,
                                warnings=sorted(self._quality_warnings),
                            )
                else:
                    with self._lock:
                        self._sync_segment(segment)
            except Exception as exc:
                if job is not None:
                    with self._lock:
                        job[1].failed = True
                self._emit(
                    "error", message=f"Could not save/update clip: {exc}", fatal=False
                )
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                self._jobs.task_done()
