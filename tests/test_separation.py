"""Cache integrity tests with fake Demucs output, not a GPU/model run."""

from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import soundfile as sf

import isolate_vocals as separation


class SeparationCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.input = Path(self.temp.name) / "originals"
        self.output = Path(self.temp.name) / "estimates"
        self.input.mkdir()
        self.source = self.input / "clip_0001.wav"
        sf.write(self.source, np.full(480, 0.2), 48000)
        self.invocations = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, command):
        self.invocations.append(command)
        root = Path(command[command.index("-o") + 1])
        model = command[command.index("-n") + 1]
        for source in command[command.index("-o") + 2 :]:
            source = Path(source)
            target = root / model / source.stem / "vocals.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return SimpleNamespace(returncode=0)

    def run_separation(self, **kwargs):
        return separation.separate(
            self.input, self.output, device="cpu", runner=self.runner, **kwargs
        )

    def test_unchanged_audio_is_reused_but_changed_source_is_not(self):
        original = separation.file_hash(self.source)
        self.assertEqual(self.run_separation(), 1)
        self.assertEqual(self.run_separation(), 0)
        self.assertEqual(separation.file_hash(self.source), original)
        sf.write(self.source, np.full(480, 0.7), 48000)
        self.assertEqual(self.run_separation(), 1)
        audio, rate = sf.read(self.output / self.source.name)
        np.testing.assert_allclose(audio, 0.7, atol=4e-5)
        self.assertEqual(rate, 48000)
        self.assertEqual(len(self.invocations), 2)

    def test_existing_untracked_or_corrupt_output_is_reprocessed(self):
        self.output.mkdir()
        (self.output / self.source.name).write_bytes(b"partial file")
        self.assertEqual(self.run_separation(), 1)
        (self.output / self.source.name).write_bytes(b"corrupted after processing")
        self.assertEqual(self.run_separation(), 1)
        self.assertEqual(len(self.invocations), 2)

    def test_model_change_invalidates_cache(self):
        self.assertEqual(self.run_separation(model="htdemucs"), 1)
        self.assertEqual(self.run_separation(model="htdemucs_ft"), 1)
        self.assertEqual(self.run_separation(model="htdemucs_ft"), 0)

    def test_corrupt_manifest_is_not_trusted(self):
        self.run_separation()
        (self.output / separation.MANIFEST_NAME).write_text(
            "{truncated", encoding="utf-8"
        )
        self.assertEqual(self.run_separation(), 1)

    def test_input_folder_cannot_be_used_as_output(self):
        with self.assertRaises(ValueError):
            separation.separate(
                self.input, self.input, device="cpu", runner=self.runner
            )

    def test_failed_demucs_does_not_write_cache_entry(self):
        with self.assertRaises(RuntimeError):
            separation.separate(
                self.input,
                self.output,
                device="cpu",
                runner=lambda command: SimpleNamespace(returncode=1),
            )
        self.assertFalse((self.output / separation.MANIFEST_NAME).exists())
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
