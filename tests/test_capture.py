"""Deterministic audio-state tests; no live device or GPU is required."""

from pathlib import Path
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch

import numpy as np
import soundfile as sf

from voiceclipper.capture import CaptureConfig, CaptureEngine


class ManualInputEngine(CaptureEngine):
    """A push-input adapter, using the real buffer and disk writer."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_ready = threading.Event()

    def _record_loop(self, device_id):
        with self._lock:
            self._ready = True
        self.input_ready.set()
        self._stop_event.wait()
        with self._lock:
            self._ready = False
            self._flush_pending()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        self.config = CaptureConfig(
            self.temp.name,
            sample_rate=1000,
            block_size=100,
            pre_roll=0.3,
            post_roll=0.2,
            history_seconds=2,
            max_clip=2,
        )
        self.engine = ManualInputEngine(self.config, self.events.append)
        self.engine.start()
        self.assertTrue(self.engine.input_ready.wait(2))

    def tearDown(self):
        self.engine.stop()
        self.temp.cleanup()

    def block(self, value, frames=100):
        self.engine.feed_block(np.full((frames, 2), value, dtype=np.float32))

    def read(self, clip_id):
        self.engine._jobs.join()
        audio, rate = sf.read(Path(self.temp.name) / f"clip_{clip_id}.wav")
        self.assertEqual(rate, 1000)
        self.assertEqual(audio.ndim, 1)
        return audio

    def test_back_to_back_marks_during_tail_keep_full_preroll(self):
        for value in (0.1, 0.2, 0.3):
            self.block(value)
        first = self.engine.toggle()
        self.block(0.4)
        self.engine.toggle()
        second = self.engine.toggle()  # The previous clip is still in post-roll.
        self.assertNotEqual(first, second)
        self.assertTrue(self.engine.recording)
        self.block(0.5)
        self.block(0.6)
        self.engine.toggle()
        self.block(0.7)
        self.block(0.8)
        first_audio, second_audio = self.read(first), self.read(second)
        self.assertEqual(len(first_audio), 600)
        self.assertEqual(len(second_audio), 700)
        np.testing.assert_allclose(
            first_audio[:300], np.repeat([0.1, 0.2, 0.3], 100), atol=4e-5
        )
        np.testing.assert_allclose(
            second_audio[:300], np.repeat([0.2, 0.3, 0.4], 100), atol=4e-5
        )

    def test_history_updates_during_capture_and_save(self):
        self.block(0.1)
        self.engine.toggle()
        self.block(0.2)
        self.engine.toggle()
        self.block(0.3)
        self.block(0.4)
        retrospective = self.engine.save_last(0.3)
        audio = self.read(retrospective)
        np.testing.assert_allclose(audio, np.repeat([0.2, 0.3, 0.4], 100), atol=4e-5)

    def test_undo_during_tail_targets_newest_take(self):
        self.block(0.1)
        previous = self.engine.save_last(0.1)
        self.read(previous)
        current = self.engine.toggle()
        self.block(0.5)
        self.engine.toggle()
        self.assertEqual(self.engine.discard_latest(), current)
        self.engine._jobs.join()
        self.assertTrue((Path(self.temp.name) / f"clip_{previous}.wav").exists())
        self.assertFalse((Path(self.temp.name) / f"clip_{current}.wav").exists())
        self.assertTrue(
            (Path(self.temp.name) / ".trash" / f"clip_{current}.wav").exists()
        )
        self.assertEqual(self.engine.restore_last(), current)
        self.assertEqual(len(self.read(current)), 200)

    def test_discard_and_restore_while_writer_is_busy(self):
        entered, resume = threading.Event(), threading.Event()
        original_write = sf.write

        def slow_write(*args, **kwargs):
            entered.set()
            if not resume.wait(3):
                raise TimeoutError("Test writer was not resumed")
            return original_write(*args, **kwargs)

        self.block(0.2)
        with patch("soundfile.write", side_effect=slow_write):
            clip_id = self.engine.save_last(0.1)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.engine.discard_latest(), clip_id)
                self.assertEqual(self.engine.restore_last(), clip_id)
            finally:
                resume.set()
            audio = self.read(clip_id)
        self.assertEqual(len(audio), 100)
        self.assertEqual(
            [event["clip_id"] for event in self.events if event["type"] == "saved"],
            [clip_id],
        )

    def test_stop_flushes_active_clip_and_joins_workers(self):
        self.block(0.1)
        clip_id = self.engine.toggle()
        self.block(0.2)
        self.assertTrue(self.engine.stop())
        self.assertFalse(self.engine.ready)
        self.assertFalse(self.engine._recorder_thread.is_alive())
        self.assertFalse(self.engine._writer_thread.is_alive())
        self.assertEqual(len(self.read(clip_id)), 200)

    def test_saved_clip_can_be_discarded_and_restored_after_stop(self):
        self.block(0.2)
        clip_id = self.engine.save_last(0.1)
        self.assertTrue(self.engine.stop())
        path = Path(self.temp.name) / f"clip_{clip_id}.wav"
        self.assertTrue(path.exists())
        self.assertEqual(self.engine.discard_latest(), clip_id)
        self.assertFalse(path.exists())
        self.assertEqual(self.engine.restore_last(), clip_id)
        self.assertTrue(path.exists())

    def test_recording_failure_clears_ready_and_reports_error(self):
        self.engine.stop()
        reported = threading.Event()
        events = []

        def failed_input(device, config):
            raise OSError("Device disconnected")

        def event(data):
            events.append(data)
            if data["type"] == "error":
                reported.set()

        engine = CaptureEngine(self.config, event, recorder_factory=failed_input)
        try:
            engine.start()
            self.assertTrue(reported.wait(2))
            self.assertFalse(engine.ready)
            self.assertIsNone(engine.toggle())
            self.assertTrue(
                any(e.get("fatal") and "disconnected" in e["message"] for e in events)
            )
        finally:
            self.assertTrue(engine.stop())

    def test_read_failure_flushes_marked_audio_from_real_worker_path(self):
        self.engine.stop()
        waiting, release, reported = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        events = []

        class DisconnectingRecorder:
            name = "Synthetic loopback"

            def __enter__(self):
                self.reads = 0
                return self

            def __exit__(self, *args):
                return False

            def record(self, numframes):
                self.reads += 1
                if self.reads == 1:
                    warnings.warn("data discontinuity in recording", RuntimeWarning)
                    return np.full((100, 2), 0.2, dtype=np.float32)
                waiting.set()
                if not release.wait(3):
                    raise TimeoutError("Test stream was not released")
                raise OSError("Playback device disconnected")

        def event(data):
            events.append(data)
            if data["type"] == "error":
                reported.set()

        engine = CaptureEngine(
            self.config,
            event,
            recorder_factory=lambda device, config: DisconnectingRecorder(),
        )
        try:
            engine.start()
            self.assertTrue(waiting.wait(2))  # The first block has been accepted.
            clip_id = engine.toggle()
            self.assertIsNotNone(clip_id)
            release.set()
            self.assertTrue(reported.wait(2))
            self.assertFalse(engine.ready)
            self.assertTrue(engine.stop())
            path = Path(self.temp.name) / f"clip_{clip_id}.wav"
            audio, rate = sf.read(path)
            self.assertEqual(len(audio), 100)
            self.assertTrue(
                any(
                    e["type"] == "warning" and "discontinuity" in e["message"]
                    for e in events
                )
            )
            saved_event = next(e for e in events if e["type"] == "saved")
            self.assertIn("data discontinuity in recording", saved_event["warnings"])
            self.assertTrue(
                any(e["type"] == "saved" and e["clip_id"] == clip_id for e in events)
            )
        finally:
            release.set()
            engine.stop()

    def test_failed_write_is_reported_without_hanging_shutdown(self):
        self.block(0.1)
        with patch("soundfile.write", side_effect=OSError("Disk full")):
            self.engine.save_last(0.1)
            self.engine._jobs.join()
        self.assertTrue(
            any(
                e["type"] == "error" and "Disk full" in e["message"]
                for e in self.events
            )
        )
        self.assertTrue(self.engine.stop())

    def test_buffer_limit_uses_exact_frame_counts(self):
        self.block(0.1, 1550)
        self.block(0.2, 700)
        clip_id = self.engine.save_last(2)
        audio = self.read(clip_id)
        self.assertEqual(len(audio), 2000)
        np.testing.assert_allclose(audio[:1300], 0.1, atol=4e-5)
        np.testing.assert_allclose(audio[1300:], 0.2, atol=4e-5)

    def test_empty_history_and_invalid_durations_do_not_create_clips(self):
        self.assertIsNone(self.engine.save_last(1))
        for seconds in (0, -1, 3, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.engine.save_last(seconds)
        self.assertFalse(list(Path(self.temp.name).glob("*.wav")))


if __name__ == "__main__":
    unittest.main()
