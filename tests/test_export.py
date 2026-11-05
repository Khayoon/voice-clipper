import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
import wave

from voiceclipper.export import (
    ExportCancelled,
    ExportError,
    export_clips,
    find_tool,
    get_waveform,
    media_info,
)
from voiceclipper.models import Clip


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required for media integration tests",
)
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "Original 日本語.wav"
        frames = bytearray()
        for position in range(48000 * 2):
            value = round(math.sin(2 * math.pi * 440 * position / 48000) * 20000)
            frames.extend(struct.pack("<hh", value, value))
        with wave.open(str(self.source), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(frames)
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.output = self.directory / "export"

    def test_real_extraction_format_duration_and_all_metadata(self):
        accepted = Clip(
            str(self.source),
            250,
            1250,
            transcript="Hello\nこんにちは",
            translation="<translation>",
            speaker="Speaker A",
            language="ja",
            status="accepted",
            processing={"review": "human"},
        )
        pending = Clip(str(self.source), 1250, 1750, name="Second")
        rejected = Clip(str(self.source), 0, 250, status="rejected")
        events = []
        paths = export_clips(
            [accepted, pending, rejected],
            self.output,
            sample_rate=24000,
            progress=lambda *event: events.append(event),
        )
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0].name, f"clip_{accepted.id}.wav")
        with wave.open(str(paths[0]), "rb") as audio:
            self.assertEqual(
                (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()),
                (1, 24000, 2),
            )
            self.assertEqual(audio.getnframes(), 24000)
        with wave.open(str(paths[1]), "rb") as audio:
            self.assertEqual(audio.getnframes(), 12000)
        with (self.output / "clips.csv").open(encoding="utf-8", newline="") as stream:
            records = list(csv.DictReader(stream))
        self.assertEqual(records[0]["transcript"], accepted.transcript)
        self.assertEqual(records[0]["translation"], accepted.translation)
        records_jsonl = [
            json.loads(line)
            for line in (self.output / "clips.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            [record["id"] for record in records_jsonl], [accepted.id, pending.id]
        )
        self.assertEqual(records_jsonl[0]["processing"], {"review": "human"})
        self.assertIn(
            f"[sound:{paths[0].name}]",
            (self.output / "anki.tsv").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "&lt;translation&gt;",
            (self.output / "anki.tsv").read_text(encoding="utf-8"),
        )
        self.assertEqual(events[-1][:2], (2, 2))
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash
        )

    def test_media_inspection_and_bounded_waveform(self):
        info = media_info(self.source)
        self.assertEqual(info["duration_ms"], 2000)
        self.assertEqual(info["audio_tracks"][0]["ordinal"], 0)
        self.assertEqual(info["audio_tracks"][0]["channels"], 2)
        self.assertFalse(info["has_video"])
        values = get_waveform(self.source, 100, 1100, bins=80)
        self.assertEqual(len(values), 80)
        self.assertTrue(all(math.isfinite(value) and value > 0.1 for value in values))

    def test_cancel_before_export_and_after_a_completed_clip(self):
        clip = Clip(str(self.source), 0, 500)
        with self.assertRaises(ExportCancelled):
            export_clips([clip], self.output, cancelled=lambda: True)
        self.assertEqual(list(self.output.glob("*.wav")), [])
        state = {"cancel": False}

        def progress(done, total, message):
            if done == 1:
                state["cancel"] = True

        with self.assertRaises(ExportCancelled):
            export_clips(
                [clip, Clip(str(self.source), 500, 1000)],
                self.output,
                progress=progress,
                cancelled=lambda: state["cancel"],
            )
        self.assertTrue((self.output / f"clip_{clip.id}.wav").is_file())
        self.assertEqual(len(list(self.output.glob("*.wav"))), 1)
        self.assertEqual(list(self.output.glob("*.tmp.wav")), [])

    def test_bad_track_and_out_of_range_fail_without_final_or_partial_files(self):
        for clip in (
            Clip(str(self.source), 0, 500, audio_track=9),
            Clip(str(self.source), 1800, 2800),
        ):
            with self.subTest(clip=clip), self.assertRaises(ExportError):
                export_clips([clip], self.output)
            self.assertFalse((self.output / f"clip_{clip.id}.wav").exists())
            self.assertEqual(list(self.output.glob("*.tmp.wav")), [])

    def test_export_cannot_replace_source(self):
        source = self.directory / "clip_original.wav"
        source.write_bytes(self.source.read_bytes())
        clip = Clip(str(source), 0, 500, id="original")
        with self.assertRaisesRegex(ExportError, "original source"):
            export_clips([clip], self.directory)
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(), self.original_hash
        )

    def test_export_reuses_stable_ids_and_updates_trim(self):
        clip = Clip(str(self.source), 0, 500)
        first = export_clips([clip], self.output, sample_rate=48000)
        clip.end_ms = 750
        second = export_clips([clip], self.output, sample_rate=48000)
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.output.glob("*.wav"))), 1)
        with wave.open(str(second[0]), "rb") as audio:
            self.assertEqual(audio.getnframes(), 36000)

    def test_missing_sources_empty_review_and_invalid_rate_are_clear(self):
        with self.assertRaisesRegex(ExportError, "no non-rejected"):
            export_clips(
                [Clip(str(self.source), 0, 500, status="rejected")], self.output
            )
        with self.assertRaisesRegex(ExportError, "missing"):
            export_clips(
                [Clip(str(self.directory / "missing.wav"), 0, 500)], self.output
            )
        for rate in (True, 4000, 200000):
            with self.assertRaises(ValueError):
                export_clips(
                    [Clip(str(self.source), 0, 500)], self.output, sample_rate=rate
                )
        with self.assertRaisesRegex(ExportError, "Cannot find"):
            find_tool("ffmpeg", self.directory / "missing.exe")


if __name__ == "__main__":
    unittest.main()
