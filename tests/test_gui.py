"""GUI workflow tests with a fake player; native playback is a manual check."""

import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog
from voiceclipper import gui
from voiceclipper.models import Project


class FakePlayer:
    def __init__(self, widget):
        self.time_ms = 0
        self.duration_ms = 60000
        self.playing = False
        self.failed = False
        self.track = 0
        self.path = ""

    def open(self, path):
        self.path = str(path)
        self.time_ms = 0
        self.playing = True

    def toggle_pause(self):
        self.playing = not self.playing

    def pause(self):
        self.playing = False

    def seek(self, ms):
        self.time_ms = int(ms)

    def set_volume(self, value):
        pass

    def set_rate(self, value):
        return 0

    def set_audio_track(self, ordinal):
        self.track = ordinal
        return 0

    def stop(self):
        self.playing = False

    def close(self):
        self.stop()


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    if sys.platform == "win32":
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "get_waveform", lambda *a, **kw: [0.1, 0.8, 0.3])
    monkeypatch.setattr(
        gui,
        "media_info",
        lambda *a, **kw: {
            "duration_ms": 60000,
            "audio_tracks": [
                {
                    "index": 1,
                    "ordinal": 0,
                    "language": "eng",
                    "title": "",
                    "codec": "aac",
                },
                {
                    "index": 2,
                    "ordinal": 1,
                    "language": "fra",
                    "title": "",
                    "codec": "aac",
                },
            ],
        },
    )
    w = gui.MainWindow(
        player_factory=FakePlayer, data_dir=tmp_path / "app", recover=False
    )
    w.settings["global_hotkeys"] = False
    w.source = str(tmp_path / "source.mp4")
    Path(w.source).write_bytes(b"synthetic test source")
    w.loaded_source = w.source
    w.project.media_path = w.source
    w.source_info = {"duration_ms": 60000}
    w.video_stack.setCurrentWidget(w.video)
    w.tracks.addItem("French", 1)
    w.show()
    app.processEvents()
    yield w
    w._force_close = True
    w.pool.waitForDone(5000)
    app.processEvents()
    w.close()
    app.processEvents()


def drain(w):
    w.pool.waitForDone(5000)
    QApplication.processEvents()


def test_marking_uses_media_time_and_preserves_selected_track(window):
    window.tracks.setCurrentIndex(1)
    window.player.time_ms = 2500
    window.mark_toggle()
    window.player.time_ms = 4800
    window.mark_toggle()
    (clip,) = window.project.clips
    assert (clip.start_ms, clip.end_ms, clip.audio_track) == (1000, 5300, 1)
    assert window.pending_start is None
    assert Path(clip.source).read_bytes() == b"synthetic test source"


def test_retrospective_and_source_end_clamp(window):
    window.player.time_ms = 3000
    window.save_previous()
    assert (window.project.clips[0].start_ms, window.project.clips[0].end_ms) == (
        0,
        3000,
    )
    window.player.time_ms = 59000
    window.mark_toggle()
    window.player.time_ms = 59900
    window.mark_toggle()
    assert window.project.clips[-1].end_ms == 60000


def test_backwards_mark_does_not_create_invalid_clip(window):
    window.player.time_ms = 5000
    window.mark_toggle()
    window.player.time_ms = 3000
    window.mark_toggle()
    assert not window.project.clips
    assert window.pending_start == 5000
    window.cancel_mark_or_fullscreen()
    assert window.pending_start is None


def test_reject_restore_and_metadata_are_non_destructive(window):
    window.player.time_ms = 4000
    window.save_previous()
    window.speaker_edit.setText("Speaker A")
    window.language_edit.setText("Arabic")
    window.transcript_edit.setPlainText("مرحبا")
    window.translation_edit.setPlainText("Hello")
    window._edit_metadata()
    clip = window.project.clips[0]
    window.discard_selected()
    assert clip.status == "rejected"
    assert Path(clip.source).exists()
    window.restore_discard()
    assert clip.status == "pending"
    assert (clip.speaker, clip.transcript, clip.translation) == (
        "Speaker A",
        "مرحبا",
        "Hello",
    )


def test_waveform_trim_roundtrip_and_invalid_bounds(window, tmp_path):
    window.player.time_ms = 6000
    window.save_previous()
    window._wave_trim(1500, 4800)
    clip = window.project.clips[0]
    assert (clip.start_ms, clip.end_ms) == (1500, 4800)
    window.start_edit.setValue(9)
    window.end_edit.setValue(8)
    window._edit_boundaries()
    assert (clip.start_ms, clip.end_ms) == (1500, 4800)
    window.project_path = tmp_path / "session.vclip"
    assert window.save_project()
    restored = Project.load(window.project_path)
    assert restored.clips[0].to_dict() == clip.to_dict()


def test_replay_reinstates_clip_audio_track(window):
    clip = window.project.add_clip(window.source, 2000, 4000, audio_track=1)
    window._refresh_clips(clip.id)
    window.tracks.setCurrentIndex(0)
    window.preview_clip()
    assert window.player.track == 1
    assert window.player.time_ms == 2000
    window.player.time_ms = 4001
    window._poll_player()
    assert not window.player.playing


def test_text_focus_disables_capture_shortcuts_and_repeat(window, app):
    clip = window.project.add_clip(window.source, 0, 1000)
    window._refresh_clips(clip.id)
    window.name_edit.setFocus()
    app.processEvents()
    window._focus_changed()
    assert not window.shortcut_actions["toggle"].isEnabled()
    window.clips_list.setFocus()
    app.processEvents()
    window._focus_changed()
    assert window.shortcut_actions["toggle"].isEnabled()
    assert not window.shortcut_actions["toggle"].autoRepeat()


def test_just_listen_is_primary_left_of_open_and_starts_without_media(
    window, monkeypatch
):
    from voiceclipper import capture

    class FakeCapture:
        ready = True
        recording = False
        discarded_ids = set()
        has_discarded = False

        def __init__(self, config, on_event):
            self.callback = on_event
            self.started = False
            self.stopped = False

        @staticmethod
        def list_devices():
            return []

        def start(self, device=None):
            self.started = True
            self.callback({"type": "ready", "device": "Test output"})

        def stop(self):
            self.stopped = True
            return True

    monkeypatch.setattr(capture, "CaptureEngine", FakeCapture)
    window.source = ""
    window.loaded_source = ""
    assert window.listen_btn.objectName() == window.open_btn.objectName() == "primary"
    assert window.listen_btn.x() < window.open_btn.x()
    window.listen_btn.click()
    assert window.mode.currentIndex() == 1
    assert window.capture.started
    assert window.capture_running
    assert window.mark_button.isEnabled()
    assert window.live_panel.isVisible()
    assert "no video" in window.source_label.text()


def test_discarded_pending_live_clip_can_restore_actual_file(window, tmp_path):
    clip_id = "pending_live_clip"
    trash = tmp_path / ".trash" / f"clip_{clip_id}.wav"
    restored = tmp_path / f"clip_{clip_id}.wav"

    class FakeCapture:
        discarded_ids = {clip_id}
        has_discarded = True
        recording = False
        ready = True

        def restore_clip(self, ident):
            assert ident == clip_id
            self.discarded_ids.clear()
            self.has_discarded = False
            window._capture_event(
                {
                    "type": "restored",
                    "clip_id": ident,
                    "path": str(restored),
                    "duration": 2,
                }
            )

        def stop(self):
            return True

    window.capture = FakeCapture()
    window._capture_event(
        {"type": "discarded", "clip_id": clip_id, "path": str(trash), "duration": 2}
    )
    (clip,) = window.project.clips
    assert clip.status == "rejected" and clip.source == str(trash)
    window.restore_discard()
    assert clip.status == "pending" and clip.source == str(restored)


def test_open_empty_project_clears_previous_playback(window, tmp_path, monkeypatch):
    path = tmp_path / "empty.vclip"
    Project().save(path)
    window.dirty = False
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **kw: (str(path), "")
    )
    window.open_project_dialog()
    assert window.source == window.loaded_source == ""
    assert not window.player.playing
    assert window.project.clips == []


def test_recovered_source_is_loaded_when_play_is_pressed(window):
    window.loaded_source = ""
    window.play_pause()
    assert window.player.path == window.source
    assert window.loaded_source == window.source
    drain(window)


def test_export_uses_snapshot_and_filters_rejected(window, tmp_path, monkeypatch):
    a = window.project.add_clip(window.source, 0, 1000)
    window.project.add_clip(window.source, 1000, 2000, status="rejected")
    window._refresh_clips(a.id)
    seen = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **kw: str(tmp_path / "exports")
    )

    def export(clips, folder, **kw):
        seen.extend(clips)
        assert clips[0] is not a
        return [Path(folder) / "a.wav"]

    monkeypatch.setattr(gui, "export_clips", export)
    window.export(True)
    drain(window)
    assert [c.id for c in seen] == [a.id]
    assert window._export_cancel is None
