import json
from pathlib import Path
import shutil
import tempfile
import unittest

from voiceclipper.models import Clip, Project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_portable_roundtrip_preserves_metadata_and_rejected_clips(self):
        bundle = self.directory / "bundle"
        bundle.mkdir()
        media = bundle / "日本語.wav"
        media.write_bytes(b"original media")
        project = Project(media_path=str(media), settings={"post_roll": 0.5})
        clip = project.add_clip(
            str(media),
            1234,
            4567,
            audio_track=1,
            transcript="Hello\nこんにちは",
            translation="A translation",
            language="ja",
            speaker="Speaker A",
            status="rejected",
            processing={"reviewed": True},
        )
        path = bundle / "session.voiceclipper.json"
        project.save(path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(Path(saved["media_path"]).is_absolute())
        self.assertFalse(Path(saved["clips"][0]["source"]).is_absolute())
        moved = self.directory / "moved"
        shutil.copytree(bundle, moved)
        restored = Project.load(moved / path.name)
        self.assertEqual(restored.media_path, str((moved / media.name).resolve()))
        self.assertEqual(restored.clips[0].source, restored.media_path)
        self.assertEqual(restored.clips[0].id, clip.id)
        self.assertEqual(restored.clips[0].transcript, clip.transcript)
        self.assertEqual(restored.clips[0].duration_ms, 3333)
        self.assertEqual(restored.clips[0].status, "rejected")
        self.assertEqual(restored.clips[0].audio_track, 1)
        self.assertEqual(media.read_bytes(), b"original media")

    def test_invalid_ranges_status_and_ids_are_rejected(self):
        for fields in (
            {"start_ms": -1},
            {"start_ms": True},
            {"end_ms": 0},
            {"end_ms": 1},
            {"audio_track": -1},
            {"audio_track": False},
            {"status": "deleted"},
            {"status": []},
            {"id": "../escape"},
            {"source": ""},
        ):
            values = {"source": "source.wav", "start_ms": 1, "end_ms": 100, **fields}
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Clip(**values)

    def test_corrupt_and_unsupported_projects_have_clear_errors(self):
        path = self.directory / "broken.json"
        for text in (
            "{",
            "[]",
            '{"schema_version":2}',
            '{"schema_version":true}',
            '{"schema_version":1,"clips":{}}',
            '{"schema_version":1,"clips":[{}]}',
            '{"schema_version":1,"settings":[]}',
            '{"schema_version":1,"unexpected":true}',
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Project.load(path)

    def test_duplicate_ids_rejected_on_add_and_load(self):
        project = Project()
        first = project.add_clip("source.wav", 0, 100)
        with self.assertRaises(ValueError):
            project.add_clip("source.wav", 100, 200, id=first.id)
        path = self.directory / "duplicates.json"
        path.write_text(
            json.dumps(
                {"schema_version": 1, "clips": [first.to_dict(), first.to_dict()]}
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate clip ID"):
            Project.load(path)

    def test_failed_atomic_save_preserves_previous_project(self):
        path = self.directory / "session.json"
        project = Project()
        project.save(path)
        before = path.read_bytes()
        project.settings["invalid"] = float("nan")
        with self.assertRaises(ValueError):
            project.save(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_project_cannot_replace_source(self):
        path = self.directory / "original.wav"
        path.write_bytes(b"keep me")
        project = Project(media_path=str(path))
        with self.assertRaisesRegex(ValueError, "source media"):
            project.save(path)
        self.assertEqual(path.read_bytes(), b"keep me")


if __name__ == "__main__":
    unittest.main()
