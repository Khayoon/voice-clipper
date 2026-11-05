"""A focused VLC wrapper: watch, mark, review, export."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading

from PySide6.QtCore import (
    Qt,
    QTimer,
    Signal,
    QObject,
    QRunnable,
    QThreadPool,
    QStandardPaths,
)
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QFileDialog,
    QComboBox,
    QSlider,
    QSplitter,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QPlainTextEdit,
    QFormLayout,
    QDoubleSpinBox,
    QAbstractSpinBox,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QMessageBox,
    QSystemTrayIcon,
    QMenu,
    QProgressBar,
    QStackedWidget,
    QStyle,
    QProgressDialog,
)

from .models import Clip, Project
from .export import media_info, export_clips, get_waveform, ExportCancelled
from .player import VlcBackend

DEFAULT_SETTINGS = {
    "pre_roll": 1.5,
    "post_roll": 0.5,
    "last_seconds": 10.0,
    "history_seconds": 30.0,
    "max_clip": 600.0,
    "key_toggle": "b",
    "key_last": "ctrl+shift+b",
    "key_discard": "f7",
    "key_restore": "ctrl+shift+z",
    "global_hotkeys": True,
    "sample_rate": 24000,
    "output_dir": "",
    "device_id": None,
}

STYLE = """
QMainWindow,QDialog { background:#161a20; color:#e8ecf2; }
QWidget { color:#e8ecf2; font-family:'Segoe UI'; font-size:13px; }
QLabel#brand { font-size:25px; font-weight:700; }
QLabel#muted { color:#9da8b9; }
QLabel#section { color:#aab5c5; font-size:11px; font-weight:700; }
QFrame#panel { background:#20262f; border:1px solid #303946; border-radius:10px; }
QFrame#video { background:#090c10; border:1px solid #303946; border-radius:8px; }
QPushButton,QToolButton { background:#2b3440; border:1px solid #3d4859; border-radius:6px; padding:8px 12px; }
QPushButton:hover,QToolButton:hover { background:#394555; border-color:#697b91; }
QPushButton:disabled { color:#697586; background:#232a34; border-color:#303946; }
QPushButton#primary { color:#141820; background:#ffb15b; border-color:#ffb15b; font-weight:700; }
QPushButton#primary:hover { background:#ffc383; }
QPushButton#primary:disabled { color:#72604b; background:#70563b; border-color:#70563b; }
QPushButton#danger { color:#ffadad; }
QLineEdit,QPlainTextEdit,QSpinBox,QDoubleSpinBox,QComboBox { background:#141a22; border:1px solid #3b4655; border-radius:5px; padding:6px; selection-background-color:#bd7533; }
QLineEdit:focus,QPlainTextEdit:focus,QSpinBox:focus,QDoubleSpinBox:focus { border-color:#ffb15b; }
QComboBox QAbstractItemView { background:#222b36; selection-background-color:#465367; }
QListWidget { background:#171e27; border:1px solid #303946; border-radius:6px; outline:none; }
QListWidget::item { padding:10px; border-bottom:1px solid #2b3440; }
QListWidget::item:selected { background:#384756; color:#ffffff; border-left:3px solid #ffb15b; }
QListWidget::item:hover { background:#293542; }
QSlider::groove:horizontal { height:5px; background:#364353; border-radius:2px; }
QSlider::sub-page:horizontal { background:#ffb15b; border-radius:2px; }
QSlider::handle:horizontal { background:#ffd19b; width:12px; margin:-4px 0; border-radius:6px; }
QProgressBar { border:0; background:#10171f; border-radius:3px; text-align:center; }
QProgressBar::chunk { background:#6bcbb5; border-radius:3px; }
QSplitter::handle { background:#161a20; }
QScrollArea { background:transparent; border:0; }
QToolTip { background:#26313e; color:#e8ecf2; border:1px solid #5c6c80; padding:4px; }
QMenu { background:#202a35; border:1px solid #465568; }
QMenu::item:selected { background:#465568; }
"""


def time_text(ms):
    ms = max(0, int(ms))
    seconds, fraction = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}"


def app_data_dir():
    override = os.environ.get("VOICE_CLIPPER_DATA_DIR")
    return (
        Path(override)
        if override
        else Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
    )


class JobSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function(self.signals.progress.emit))
        except ExportCancelled:
            self.signals.error.emit("Export cancelled. Completed files have been kept.")
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class Waveform(QWidget):
    trimChanged = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(90)
        self.setMaximumHeight(120)
        self.setToolTip(
            "Drag either orange boundary to adjust the selected clip. Originals stay unchanged."
        )
        self.peaks = []
        self.window_start = self.window_end = self.start = self.end = 0
        self.dragging = None

    def set_data(self, peaks, window_start, window_end, start, end):
        self.peaks = peaks
        self.window_start, self.window_end = window_start, window_end
        self.start, self.end = start, end
        self.update()

    def _x(self, ms):
        return 12 + (self.width() - 24) * (ms - self.window_start) / max(
            1, self.window_end - self.window_start
        )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#111923"))
        if self.window_end <= self.window_start:
            p.setPen(QColor("#8e9caf"))
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Select a clip to review its waveform",
            )
            return
        left, right = self._x(self.start), self._x(self.end)
        p.fillRect(
            int(left),
            4,
            max(1, int(right - left)),
            self.height() - 8,
            QColor("#3e342b"),
        )
        center = self.height() / 2
        p.setPen(QColor("#879aa9"))
        if self.peaks:
            peak_max = max(max(self.peaks), 0.01)
            for i, value in enumerate(self.peaks):
                x = 12 + i * (self.width() - 24) / max(1, len(self.peaks) - 1)
                amp = min(1, abs(value) / peak_max) * (center - 16)
                p.drawLine(int(x), int(center - amp), int(x), int(center + amp))
        else:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Loading waveform…")
        p.setPen(QColor("#ffb15b"))
        for x in (left, right):
            p.drawLine(int(x), 4, int(x), self.height() - 4)
            p.fillRect(int(x) - 3, 5, 7, 15, QColor("#ffb15b"))

    def mousePressEvent(self, event):
        if self.window_end <= self.window_start:
            return
        x = event.position().x()
        self.dragging = (
            "start"
            if abs(x - self._x(self.start)) < abs(x - self._x(self.end))
            else "end"
        )
        self._move(x)

    def mouseMoveEvent(self, event):
        if self.dragging:
            self._move(event.position().x())

    def _move(self, x):
        ratio = max(0.0, min(1.0, (x - 12) / max(1, self.width() - 24)))
        value = round(self.window_start + ratio * (self.window_end - self.window_start))
        if self.dragging == "start":
            self.start = min(value, self.end - 1)
        else:
            self.end = max(value, self.start + 1)
        self.update()

    def mouseReleaseEvent(self, event):
        if self.dragging:
            self.dragging = None
            self.trimChanged.emit(self.start, self.end)


class VideoFrame(QFrame):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("Capture settings")
        self.resize(460, 450)
        form = QFormLayout(self)
        self.fields = {}
        for key, label, low, high in (
            ("pre_roll", "Pre-roll (seconds)", 0, 30),
            ("post_roll", "Post-roll (seconds)", 0, 10),
            ("last_seconds", "Save previous (seconds)", 0.1, 120),
            ("history_seconds", "Live history (seconds)", 1, 120),
            ("max_clip", "Maximum live clip (seconds)", 1, 3600),
        ):
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(1)
            box.setValue(settings[key])
            self.fields[key] = box
            form.addRow(label, box)
        for key, label in (
            ("key_toggle", "Mark / start-stop"),
            ("key_last", "Save previous"),
            ("key_discard", "Discard latest"),
            ("key_restore", "Restore last discard"),
        ):
            box = QLineEdit(settings[key])
            self.fields[key] = box
            form.addRow(label, box)
        self.global_box = QCheckBox("Global hotkeys while live capture is running")
        self.global_box.setChecked(settings["global_hotkeys"])
        form.addRow(self.global_box)
        directory = QHBoxLayout()
        self.output_edit = QLineEdit(settings["output_dir"])
        directory.addWidget(self.output_edit, 1)
        choose = QPushButton("Choose…")

        def browse():
            folder = QFileDialog.getExistingDirectory(
                self, "Live recordings folder", self.output_edit.text()
            )
            if folder:
                self.output_edit.setText(folder)

        choose.clicked.connect(browse)
        directory.addWidget(choose)
        form.addRow("Live recordings", directory)
        hint = QLabel(
            "VLC mode uses media time. Live mode captures the selected Windows output, including other apps."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        result = {
            key: box.value()
            if isinstance(box, QDoubleSpinBox)
            else box.text().strip().lower()
            for key, box in self.fields.items()
        }
        keys = [
            result[k] for k in ("key_toggle", "key_last", "key_discard", "key_restore")
        ]
        if any(not k for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("Choose four different, nonempty hotkeys.")
        if result["history_seconds"] < max(result["pre_roll"], result["last_seconds"]):
            raise ValueError(
                "Live history must cover both pre-roll and Save previous duration."
            )
        result["global_hotkeys"] = self.global_box.isChecked()
        if not self.output_edit.text().strip():
            raise ValueError("Choose a folder for live recordings.")
        result["output_dir"] = str(Path(self.output_edit.text()).expanduser().resolve())
        return result


class MainWindow(QMainWindow):
    captureEvent = Signal(dict)
    globalAction = Signal(str)

    def __init__(
        self, player_factory=VlcBackend, data_dir=None, recover=True, lazy_player=True
    ):
        super().__init__()
        self.setWindowTitle("Voice Clipper")
        self.resize(1320, 900)
        self.setMinimumSize(1080, 760)
        self.setAcceptDrops(True)
        self.data_dir = Path(data_dir) if data_dir else app_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            self.settings.update(
                json.loads((self.data_dir / "settings.json").read_text("utf-8"))
            )
        except (OSError, ValueError, TypeError):
            pass
        if not self.settings.get("output_dir"):
            self.settings["output_dir"] = str(self.data_dir / "recordings")
        self.project = Project()
        self.project_path = None
        self.dirty = False
        self.source = ""
        self.source_info = {}
        self.pending_start = None
        self.preview_end = None
        self.loaded_source = ""
        self._pending_replay = None
        self.capture = None
        self.capture_running = False
        self.capture_stopping = False
        self.global_hooks = []
        self.shortcuts = []
        self._jobs = {}
        self._next_job = 0
        self._wave_generation = 0
        self._probe_generation = 0
        self._loading_editor = False
        self._force_close = False
        self._player_error_shown = False
        self._discard_changes = False
        self._undo = []
        self._export_cancel = None
        self._export_dialog = None
        self._fullscreen = False
        self._normal_split = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self._build_ui()
        self.player = None
        self._player_factory = player_factory
        self._vlc_error = None
        if not lazy_player:
            self._ensure_player(show_error=False)
        self.captureEvent.connect(self._capture_event)
        self.globalAction.connect(self._global_action)
        self._install_shortcuts()
        self._create_tray()
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.timeout.connect(self._write_recovery)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_player)
        self.timer.start(100)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        if recover:
            self._recover()
        self._refresh_clips()
        self._update_mode()

    def _button(self, text, slot, primary=False):
        b = QPushButton(text)
        b.clicked.connect(slot)
        if primary:
            b.setObjectName("primary")
        return b

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(14)
        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        titlecol = QVBoxLayout()
        titlecol.setSpacing(2)
        title = QLabel("Voice Clipper")
        title.setObjectName("brand")
        titlecol.addWidget(title)
        sub = QLabel("Watch. Mark. Keep the useful speech.")
        sub.setObjectName("muted")
        titlecol.addWidget(sub)
        header.addLayout(titlecol)
        header.addStretch()
        self.listen_btn = self._button("Just Listen", self.just_listen, True)
        self.listen_btn.setMinimumWidth(126)
        self.listen_btn.setToolTip(
            "Capture any Windows audio using global hotkeys, even while minimized"
        )
        header.addWidget(self.listen_btn)
        self.open_btn = self._button("Open video…", self.open_media_dialog, True)
        header.addWidget(self.open_btn)
        self.open_btn.setMinimumWidth(126)
        header.addWidget(self._button("Open project…", self.open_project_dialog))
        header.addWidget(self._button("Save project", self.save_project))
        header.addWidget(self._button("Settings", self.configure))
        outer.addWidget(self.header)
        self.modebar = QWidget()
        mb = QHBoxLayout(self.modebar)
        mb.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        self.mode.addItems(["VLC · source clips", "Live · system audio"])
        self.mode.setMinimumWidth(205)
        self.mode.currentIndexChanged.connect(self._update_mode)
        mb.addWidget(self.mode)
        self.source_label = QLabel("Open a video to begin")
        self.source_label.setObjectName("muted")
        mb.addWidget(self.source_label, 1)
        self.count_label = QLabel("0 clips")
        mb.addWidget(self.count_label)
        outer.addWidget(self.modebar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(16)
        self.playback_panel = QWidget()
        left = QVBoxLayout(self.playback_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        self.video_stack = QStackedWidget()
        self.empty = QFrame()
        self.empty.setObjectName("video")
        el = QVBoxLayout(self.empty)
        el.addStretch()
        self.empty_title = QLabel("Your next collection starts here")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setStyleSheet("font-size:21px;font-weight:600;color:#d9e2ee;")
        el.addWidget(self.empty_title)
        self.empty_hint = QLabel(
            "Open a video, or choose Just Listen to capture audio from any app.\nMark useful lines with a hotkey and review them here."
        )
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setObjectName("muted")
        el.addWidget(self.empty_hint)
        el.addStretch()
        self.video_stack.addWidget(self.empty)
        self.video = VideoFrame()
        self.video.setObjectName("video")
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.doubleClicked.connect(self.toggle_fullscreen)
        self.video_stack.addWidget(self.video)
        self.video_stack.setMinimumHeight(240)
        left.addWidget(self.video_stack, 1)
        self.transport = QWidget()
        transport = QVBoxLayout(self.transport)
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(8)
        seekrow = QHBoxLayout()
        self.clock = QLabel("00:00:00.000")
        self.clock.setMinimumWidth(104)
        seekrow.addWidget(self.clock)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 100000)
        self.seek_slider.sliderReleased.connect(self._seek_from_slider)
        seekrow.addWidget(self.seek_slider, 1)
        self.duration_label = QLabel("00:00:00.000")
        seekrow.addWidget(self.duration_label)
        transport.addLayout(seekrow)
        controls = QHBoxLayout()
        self.play_button = self._button("Play / pause", self.play_pause)
        controls.addWidget(self.play_button)
        controls.addWidget(self._button("−5s", lambda: self.seek_relative(-5000)))
        controls.addWidget(self._button("+5s", lambda: self.seek_relative(5000)))
        self.speed = QComboBox()
        for value in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(self._set_speed)
        controls.addWidget(self.speed)
        self.tracks = QComboBox()
        self.tracks.setMinimumWidth(110)
        self.tracks.setMaximumWidth(190)
        self.tracks.addItem("Audio track 1", 0)
        self.tracks.currentIndexChanged.connect(self._set_track)
        controls.addWidget(self.tracks)
        controls.addStretch()
        volume = QLabel("Volume")
        volume.setObjectName("muted")
        controls.addWidget(volume)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(85)
        self.volume.valueChanged.connect(
            lambda n: self.player and self.player.set_volume(n)
        )
        controls.addWidget(self.volume)
        controls.addWidget(self._button("Fullscreen", self.toggle_fullscreen))
        transport.addLayout(controls)
        left.addWidget(self.transport)
        self.live_panel = QFrame()
        self.live_panel.setObjectName("panel")
        live = QVBoxLayout(self.live_panel)
        row = QHBoxLayout()
        self.devices = QComboBox()
        self.devices.setMinimumWidth(220)
        row.addWidget(self.devices, 1)
        row.addWidget(self._button("Refresh", self.refresh_devices))
        self.live_button = self._button("Start listening", self.toggle_listening)
        row.addWidget(self.live_button)
        row.addWidget(self._button("Minimize to tray", self.hide_to_tray))
        live.addLayout(row)
        row = QHBoxLayout()
        self.live_state = QLabel("Stopped")
        row.addWidget(self.live_state, 1)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setValue(0)
        self.level.setTextVisible(False)
        self.level.setMaximumHeight(8)
        row.addWidget(self.level, 1)
        live.addLayout(row)
        hint = QLabel(
            "Captures all sound through this output. Keep listening on while you mark clips."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        live.addWidget(hint)
        left.addWidget(self.live_panel)
        markpanel = QFrame()
        markpanel.setObjectName("panel")
        marking = QVBoxLayout(markpanel)
        markrow = QHBoxLayout()
        self.mark_button = self._button("Mark start · B", self.mark_toggle, True)
        markrow.addWidget(self.mark_button, 1)
        self.last_button = self._button("Save previous 10s", self.save_previous)
        markrow.addWidget(self.last_button, 1)
        marking.addLayout(markrow)
        self.mark_hint = QLabel(
            "Mark the beginning and end of a line. Pre/post-roll adds breathing room."
        )
        self.mark_hint.setObjectName("muted")
        self.mark_hint.setWordWrap(True)
        marking.addWidget(self.mark_hint)
        left.addWidget(markpanel)
        self.waveform = Waveform()
        self.waveform.trimChanged.connect(self._wave_trim)
        left.addWidget(self.waveform)
        self.wave_hint = QLabel(
            "Original media stays unchanged. Drag waveform boundaries to trim."
        )
        self.wave_hint.setObjectName("muted")
        left.addWidget(self.wave_hint)
        self.splitter.addWidget(self.playback_panel)
        self.review_panel = QFrame()
        self.review_panel.setObjectName("panel")
        review = QVBoxLayout(self.review_panel)
        review.setContentsMargins(14, 14, 14, 14)
        review.setSpacing(10)
        row = QHBoxLayout()
        section = QLabel("YOUR CLIPS")
        section.setObjectName("section")
        row.addWidget(section, 1)
        self.filter = QComboBox()
        self.filter.addItems(["All clips", "To review", "Accepted", "Rejected"])
        self.filter.currentIndexChanged.connect(lambda: self._refresh_clips())
        row.addWidget(self.filter)
        review.addLayout(row)
        self.clips_list = QListWidget()
        self.clips_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.clips_list.itemSelectionChanged.connect(self._selection_changed)
        self.clips_list.itemDoubleClicked.connect(lambda _: self.preview_clip())
        review.addWidget(self.clips_list, 1)
        actions = QHBoxLayout()
        actions.addWidget(self._button("Replay", self.preview_clip))
        actions.addWidget(
            self._button("Accept", lambda: self.set_review_status("accepted"))
        )
        actions.addWidget(self._button("Reject", self.discard_selected))
        actions.addWidget(self._button("Restore", self.restore_discard))
        review.addLayout(actions)
        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Optional clip name")
        form.addRow("Name", self.name_edit)
        boundaries = QHBoxLayout()
        self.start_edit = QDoubleSpinBox()
        self.end_edit = QDoubleSpinBox()
        for box in (self.start_edit, self.end_edit):
            box.setRange(0, 8640000)
            box.setDecimals(3)
            box.setSuffix(" s")
            box.setSingleStep(0.1)
            boundaries.addWidget(box)
        form.addRow("Start / end", boundaries)
        row = QHBoxLayout()
        self.speaker_edit = QLineEdit()
        self.speaker_edit.setPlaceholderText("Speaker")
        row.addWidget(self.speaker_edit)
        self.language_edit = QLineEdit()
        self.language_edit.setPlaceholderText("Language")
        row.addWidget(self.language_edit)
        form.addRow("Labels", row)
        self.transcript_edit = QPlainTextEdit()
        self.transcript_edit.setPlaceholderText("Edit or paste the spoken text…")
        self.transcript_edit.setMaximumHeight(72)
        form.addRow("Transcript", self.transcript_edit)
        self.translation_edit = QPlainTextEdit()
        self.translation_edit.setPlaceholderText("Optional translation for Anki…")
        self.translation_edit.setMaximumHeight(60)
        form.addRow("Translation", self.translation_edit)
        for box in (self.name_edit, self.speaker_edit, self.language_edit):
            box.editingFinished.connect(self._edit_metadata)
        for box in (self.start_edit, self.end_edit):
            box.editingFinished.connect(self._edit_boundaries)
        self.transcript_edit.textChanged.connect(self._edit_metadata)
        self.translation_edit.textChanged.connect(self._edit_metadata)
        review.addWidget(self.editor)
        self.editor.setEnabled(False)
        self.splitter.addWidget(self.review_panel)
        self.splitter.setSizes([800, 440])
        outer.addWidget(self.splitter, 1)
        self.footer = QWidget()
        footer = QHBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        self.notice = QLabel("Ready · local files export directly from the source")
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        footer.addWidget(self.notice, 1)
        footer.addWidget(QLabel("WAV"))
        self.sample_rate = QComboBox()
        for rate in (16000, 22050, 24000, 44100, 48000):
            self.sample_rate.addItem(f"{rate / 1000:g} kHz", rate)
        self.sample_rate.setCurrentIndex(
            max(0, self.sample_rate.findData(self.settings["sample_rate"]))
        )
        footer.addWidget(self.sample_rate)
        self.export_selected = self._button(
            "Export selected…", lambda: self.export(False)
        )
        footer.addWidget(self.export_selected)
        self.export_all = self._button("Export all…", lambda: self.export(True), True)
        footer.addWidget(self.export_all)
        outer.addWidget(self.footer)
        icon_path = (
            Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
            / "assets"
            / "voice-clipper-icon.png"
        )
        self.app_icon = (
            QIcon(str(icon_path))
            if icon_path.is_file()
            else self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        )
        self.setWindowIcon(self.app_icon)
        filemenu = self.menuBar().addMenu("File")
        for name, shortcut, slot in (
            ("New project", "Ctrl+N", self.new_project),
            ("Open video…", "Ctrl+O", self.open_media_dialog),
            ("Open project…", "Ctrl+Shift+O", self.open_project_dialog),
            ("Save project", "Ctrl+S", self.save_project),
            ("Save project as…", "Ctrl+Shift+S", lambda: self.save_project(True)),
            ("Quit", "Ctrl+Shift+Q", self.close),
        ):
            action = QAction(name, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            filemenu.addAction(action)
        view = self.menuBar().addMenu("View")
        action = QAction("Minimize to tray", self)
        action.triggered.connect(self.hide_to_tray)
        view.addAction(action)

    def run_job(self, function, success, error=None, progress=None, finished=None):
        job = Job(function)
        ident = self._next_job
        self._next_job += 1
        self._jobs[ident] = job
        job.signals.result.connect(success)
        job.signals.error.connect(error or self._show_error)
        if progress:
            job.signals.progress.connect(progress)
        if finished:
            job.signals.finished.connect(finished)
        # Keep the signal owner alive until every queued completion callback ran.
        job.signals.finished.connect(
            lambda: QTimer.singleShot(0, lambda: self._jobs.pop(ident, None))
        )
        self.pool.start(job)
        return job

    def _show_error(self, message):
        self.notice.setText(str(message))
        QMessageBox.warning(self, "Voice Clipper", str(message))

    def _changed(self):
        self.dirty = True
        self.setWindowTitle(
            "Voice Clipper · "
            + (self.project_path.stem if self.project_path else "Untitled project")
            + " *"
        )
        self.autosave.start(700)

    def _write_preferences(self):
        self.settings["sample_rate"] = self.sample_rate.currentData()
        target = self.data_dir / "settings.json"
        temp = target.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), "utf-8"
            )
            temp.replace(target)
        except OSError as exc:
            self.notice.setText("Could not save settings: " + str(exc))

    def _write_recovery(self):
        try:
            self.project.settings.update(
                {"capture": self.settings, "source_info": self.source_info}
            )
            self.project.save(self.data_dir / "autosave.vclip")
            self._write_preferences()
        except (OSError, ValueError) as exc:
            self.notice.setText("Could not save recovery copy: " + str(exc))

    def _recover(self):
        candidates = [self.data_dir / "autosave.vclip"]
        if self.settings.get("last_project"):
            candidates.append(Path(self.settings["last_project"]))
        for path in candidates:
            if not path.is_file():
                continue
            try:
                self.project = Project.load(path)
                if path.name != "autosave.vclip":
                    self.project_path = path
                self.source = self.project.media_path
                self.source_label.setText(
                    Path(self.source).name if self.source else "Open a video to begin"
                )
                self.notice.setText(
                    "Previous session restored · open or replay its source when ready"
                )
                return
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def _ask_save(self):
        self._discard_changes = False
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Save this project?",
            "Save your clips and edits before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        self._discard_changes = True
        return True

    def new_project(self):
        if self.capture:
            if self.capture.stop() is False:
                self.notice.setText(
                    "Stop the audio device before starting another project."
                )
                return
            QApplication.processEvents()
            self.capture_running = False
            self._remove_global_hooks()
            self.capture = None
        if not self._ask_save():
            return
        if self.player:
            self.player.stop()
        self.project = Project()
        self.project_path = None
        self.source = self.loaded_source = ""
        self.source_info = {}
        self.pending_start = self.preview_end = None
        self._undo = []
        self.dirty = False
        self.video_stack.setCurrentWidget(self.empty)
        self.mode.setCurrentIndex(0)
        self.source_label.setText("Open a video or choose Just Listen")
        self.setWindowTitle("Voice Clipper")
        self._refresh_clips()
        self._update_mode()
        self._write_recovery()

    def save_project(self, save_as=False):
        path = self.project_path
        if not path or save_as:
            name, _ = QFileDialog.getSaveFileName(
                self,
                "Save Voice Clipper project",
                str(path or self.data_dir / "My collection.vclip"),
                "Voice Clipper project (*.vclip)",
            )
            if not name:
                return False
            path = Path(name)
            if not path.suffix:
                path = path.with_suffix(".vclip")
        try:
            self.project.settings["capture"] = dict(self.settings)
            self.project.save(path)
            self.project_path = path
            self.dirty = False
            self.settings["last_project"] = str(path)
            self._write_preferences()
            self.setWindowTitle("Voice Clipper · " + path.stem)
            self.notice.setText("Project saved · " + str(path))
            return True
        except (OSError, ValueError) as exc:
            self._show_error(str(exc))
            return False

    def open_project_dialog(self):
        if not self._ask_save():
            return
        name, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            str(self.data_dir),
            "Voice Clipper project (*.vclip *.json)",
        )
        if not name:
            return
        try:
            project = Project.load(name)
            if self.capture_running:
                self._stop_capture()
            if self.player:
                self.player.stop()
            self.source = ""
            self.loaded_source = ""
            self.source_info = {}
            self.preview_end = None
            self.video_stack.setCurrentWidget(self.empty)
            self.project = project
            self.project_path = Path(name)
            self.pending_start = None
            self._undo = []
            self.dirty = False
            self._refresh_clips()
            self.setWindowTitle("Voice Clipper · " + Path(name).stem)
            if project.media_path and Path(project.media_path).is_file():
                self.load_media(project.media_path, mark_dirty=False)
            elif project.media_path:
                self.source = project.media_path
                self.notice.setText(
                    "Source moved or missing. Open its replacement to relink the project."
                )
            else:
                self.source_label.setText("Open a video or choose Just Listen")
            self._update_mark_label()
            self.settings["last_project"] = name
            self._write_preferences()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._show_error("Could not open project: " + str(exc))

    def open_media_dialog(self):
        name, _ = QFileDialog.getOpenFileName(
            self,
            "Open video or audio",
            self.settings.get("last_media_dir", ""),
            "Media (*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.mp3 *.wav *.flac *.m4a *.ogg);;All files (*)",
        )
        if name:
            if self.source and not Path(self.source).is_file():
                old = self.source
                for clip in self.project.clips:
                    if clip.source == old:
                        clip.source = str(Path(name).resolve())
            self.load_media(name)

    def _ensure_player(self, show_error=True):
        if self.player:
            return True
        try:
            self.notice.setText("Opening VLC playback…")
            self.player = self._player_factory(self.video)
            self._vlc_error = None
            return True
        except Exception as exc:
            self._vlc_error = str(exc)
            self.empty_title.setText("VLC needs a quick setup")
            self.empty_hint.setText(str(exc))
            self.empty_hint.setWordWrap(True)
            self.notice.setText(
                "VLC playback is unavailable. Just Listen and export remain available."
            )
            if show_error:
                self._show_error(str(exc))
            return False

    def load_media(self, path, mark_dirty=True, replay=None):
        path = str(Path(path).resolve())
        if not Path(path).is_file():
            self._show_error("Source file not found: " + path)
            return
        if not self._ensure_player():
            return
        try:
            self.player.open(path)
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.player.set_volume(self.volume.value())
        self.source = path
        self.loaded_source = path
        self.project.media_path = path
        self.source_info = {}
        self.pending_start = None
        self.preview_end = None
        self._pending_replay = replay
        self._player_error_shown = False
        self.mode.setCurrentIndex(0)
        self.video_stack.setCurrentWidget(self.video)
        self.source_label.setText(Path(path).name)
        self.source_label.setToolTip(path)
        self.settings["last_media_dir"] = str(Path(path).parent)
        self.notice.setText("Reading source tracks…")
        self._probe_generation += 1
        generation = self._probe_generation

        def ready(info):
            if generation != self._probe_generation:
                return
            self.source_info = info
            self.tracks.blockSignals(True)
            self.tracks.clear()
            for track in info.get("audio_tracks", []):
                label = f"{track['ordinal'] + 1} · " + (
                    track.get("language")
                    or track.get("title")
                    or track.get("codec")
                    or "Audio"
                )
                self.tracks.addItem(label, track["ordinal"])
            self.tracks.blockSignals(False)
            if self._pending_replay:
                index = self.tracks.findData(self._pending_replay.audio_track)
                if index >= 0:
                    self.tracks.setCurrentIndex(index)
            self._set_track()
            self.notice.setText(
                "Source ready · clips export directly from the selected audio track"
            )
            if self._pending_replay:
                clip = self._pending_replay
                self._pending_replay = None
                QTimer.singleShot(200, lambda: self._start_preview(clip))

        self.run_job(
            lambda _: media_info(path),
            ready,
            lambda message: self.notice.setText(
                "Playback ready. Export tools: " + message
            ),
        )
        if mark_dirty:
            self._changed()
        self._update_mark_label()

    def _poll_player(self):
        if not self.player or not self.source or self.loaded_source != self.source:
            return
        position = self.player.time_ms
        duration = self.player.duration_ms or self.source_info.get("duration_ms", 0)
        self.clock.setText(time_text(position))
        self.duration_label.setText(time_text(duration))
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(round(position / max(1, duration) * 100000))
        if self.preview_end is not None and position >= self.preview_end:
            self.player.pause()
            self.preview_end = None
        if self.player.failed and not self._player_error_shown:
            self._player_error_shown = True
            self.notice.setText(
                "VLC could not play this source. Check the file and codec support."
            )
        if self.pending_start is not None:
            self.mark_hint.setText(
                "Marking from "
                + time_text(self.pending_start)
                + " · press again at the end"
            )

    def play_pause(self):
        if self.source and self._ensure_player():
            if self.loaded_source != self.source:
                self.load_media(self.source, False)
            else:
                self.player.toggle_pause()

    def seek_relative(self, offset):
        if self.player and self.source:
            self.preview_end = None
            self.player.seek(self.player.time_ms + offset)

    def _seek_from_slider(self):
        if self.player:
            duration = self.player.duration_ms or self.source_info.get("duration_ms", 0)
            self.preview_end = None
            self.player.seek(duration * self.seek_slider.value() / 100000)

    def _set_speed(self):
        if self.player and self.player.set_rate(self.speed.currentData()) == -1:
            self.notice.setText("This source does not support that playback speed.")

    def _set_track(self):
        if self.player and self.tracks.currentData() is not None:
            ordinal = self.tracks.currentData()
            self.player.set_audio_track(ordinal)
            QTimer.singleShot(
                450,
                lambda: (
                    self.player
                    and self.tracks.currentData() == ordinal
                    and self.player.set_audio_track(ordinal)
                ),
            )

    def _update_mode(self):
        live = self.mode.currentIndex() == 1
        self.live_panel.setVisible(live)
        self.transport.setVisible(not live)
        if live:
            self.pending_start = None
            if self.player:
                self.player.pause()
            self.video_stack.setCurrentWidget(self.empty)
            self.empty_title.setText("Just Listen")
            self.empty_hint.setText(
                "Any app. Any audio playing through your selected output.\nUse the hotkeys while minimized, then return to review your clips."
            )
            self.source_label.setText("Windows system audio · no video file needed")
            if self.devices.count() == 0:
                self.refresh_devices()
        elif self.capture_running and not self.capture_stopping:
            self._stop_capture()
        if not live:
            if self.source and self.player:
                self.video_stack.setCurrentWidget(
                    self.video if self.loaded_source == self.source else self.empty
                )
                self.source_label.setText(Path(self.source).name)
            else:
                self.empty_title.setText(
                    "Your next collection starts here"
                    if not self._vlc_error
                    else "VLC needs a quick setup"
                )
                self.empty_hint.setText(
                    "Open a video, or choose Just Listen to capture audio from any app.\nMark useful lines with a hotkey and review them here."
                )
                self.source_label.setText("Open a video or choose Just Listen")
        self._update_mark_label()

    def _update_mark_label(self):
        live = self.mode.currentIndex() == 1
        recording = (
            bool(self.capture and self.capture.recording)
            if live
            else self.pending_start is not None
        )
        key = self.settings["key_toggle"].upper()
        self.mark_button.setText(
            ("Stop clip" if recording else ("Start clip" if live else "Mark start"))
            + " · "
            + key
        )
        self.last_button.setText(f"Save previous {self.settings['last_seconds']:g}s")
        self.mark_button.setEnabled(
            (self.capture_running and bool(self.capture and self.capture.ready))
            if live
            else bool(self.player and self.source and self.loaded_source == self.source)
        )
        self.last_button.setEnabled(self.mark_button.isEnabled())
        if not recording:
            self.mark_hint.setText(
                "Live capture includes pre/post-roll."
                if live
                else "Mark the beginning and end of a line. Pre/post-roll adds breathing room."
            )

    def mark_toggle(self):
        if self.mode.currentIndex() == 1:
            if self.capture and self.capture.ready:
                self.capture.toggle()
            self._update_mark_label()
            return
        if not self.player or not self.source:
            return
        now = self.player.time_ms
        if self.pending_start is None:
            self.pending_start = now
            self.pending_track = self.tracks.currentData() or 0
        else:
            if now <= self.pending_start:
                self.notice.setText(
                    "End must follow start. Seek forward, or press Escape to cancel the mark."
                )
                return
            start = max(0, self.pending_start - round(self.settings["pre_roll"] * 1000))
            end = now + round(self.settings["post_roll"] * 1000)
            duration = self.source_info.get("duration_ms") or self.player.duration_ms
            if duration:
                end = min(end, duration)
            clip = self.project.add_clip(
                self.source, start, end, audio_track=self.pending_track
            )
            self.pending_start = None
            self._changed()
            self._refresh_clips(clip.id)
            self.notice.setText("Marked " + time_text(start) + " → " + time_text(end))
        self._update_mark_label()

    def save_previous(self):
        seconds = self.settings["last_seconds"]
        if self.mode.currentIndex() == 1:
            if self.capture and self.capture.ready:
                self.capture.save_last(seconds)
            return
        if not self.player or not self.source:
            return
        end = self.player.time_ms
        if end <= 0:
            self.notice.setText(
                "Play some media first, then save the previous seconds."
            )
            return
        clip = self.project.add_clip(
            self.source,
            max(0, end - round(seconds * 1000)),
            end,
            audio_track=self.tracks.currentData() or 0,
        )
        self._changed()
        self._refresh_clips(clip.id)
        self.notice.setText(f"Saved previous {clip.duration_ms / 1000:.2f}s as a clip")

    def _selected_clips(self):
        ids = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.clips_list.selectedItems()
        }
        return [c for c in self.project.clips if c.id in ids]

    def selected_clip(self):
        item = self.clips_list.currentItem()
        return next(
            (
                c
                for c in self.project.clips
                if item and c.id == item.data(Qt.ItemDataRole.UserRole)
            ),
            None,
        )

    def _refresh_clips(self, select_id=None):
        selected = select_id or (
            self.selected_clip().id if self.selected_clip() else None
        )
        self.clips_list.blockSignals(True)
        self.clips_list.clear()
        wanted = {1: "pending", 2: "accepted", 3: "rejected"}.get(
            self.filter.currentIndex()
        )
        for i, clip in enumerate(self.project.clips, 1):
            if wanted and clip.status != wanted:
                continue
            item = QListWidgetItem(
                f"{i:03d}  {clip.display_name}\n{time_text(clip.start_ms)}  ·  {clip.duration_ms / 1000:.2f}s  ·  {clip.status}"
            )
            item.setData(Qt.ItemDataRole.UserRole, clip.id)
            item.setToolTip(clip.source)
            self.clips_list.addItem(item)
            if clip.status == "rejected":
                item.setForeground(QColor("#8290a3"))
            if clip.id == selected:
                self.clips_list.setCurrentItem(item)
        self.clips_list.blockSignals(False)
        accepted = sum(c.status == "accepted" for c in self.project.clips)
        self.count_label.setText(
            f"{len(self.project.clips)} clips · {accepted} accepted"
        )
        self.export_all.setEnabled(
            any(c.status != "rejected" for c in self.project.clips)
        )
        self._selection_changed()

    def _selection_changed(self):
        clip = self.selected_clip()
        self.editor.setEnabled(clip is not None)
        self.export_selected.setEnabled(bool(self._selected_clips()))
        self._loading_editor = True
        if clip:
            self.name_edit.setText(clip.name)
            self.start_edit.setValue(clip.start_ms / 1000)
            self.end_edit.setValue(clip.end_ms / 1000)
            self.speaker_edit.setText(clip.speaker)
            self.language_edit.setText(clip.language)
            self.transcript_edit.setPlainText(clip.transcript)
            self.translation_edit.setPlainText(clip.translation)
        else:
            for field in (self.name_edit, self.speaker_edit, self.language_edit):
                field.clear()
            self.transcript_edit.clear()
            self.translation_edit.clear()
            self.waveform.set_data([], 0, 0, 0, 0)
        self._loading_editor = False
        if clip:
            self._load_waveform(clip)

    def _load_waveform(self, clip):
        self._wave_generation += 1
        generation = self._wave_generation
        start = max(0, clip.start_ms - 2000)
        end = clip.end_ms + 2000
        if self.source == clip.source and self.source_info.get("duration_ms"):
            end = min(end, self.source_info["duration_ms"])
        self.waveform.set_data([], start, end, clip.start_ms, clip.end_ms)

        def ready(peaks):
            current = self.selected_clip()
            if (
                generation == self._wave_generation
                and current
                and current.id == clip.id
            ):
                self.waveform.set_data(
                    peaks, start, end, current.start_ms, current.end_ms
                )

        self.run_job(
            lambda _: get_waveform(clip.source, start, end, clip.audio_track, bins=450),
            ready,
            lambda message: (
                self.wave_hint.setText("Waveform unavailable: " + message)
                if generation == self._wave_generation
                else None
            ),
        )

    def _edit_metadata(self):
        if self._loading_editor:
            return
        clip = self.selected_clip()
        if not clip:
            return
        clip.name = self.name_edit.text()
        clip.speaker = self.speaker_edit.text()
        clip.language = self.language_edit.text()
        clip.transcript = self.transcript_edit.toPlainText()
        clip.translation = self.translation_edit.toPlainText()
        item = self.clips_list.currentItem()
        if item:
            index = self.project.clips.index(clip) + 1
            item.setText(
                f"{index:03d}  {clip.display_name}\n{time_text(clip.start_ms)}  ·  {clip.duration_ms / 1000:.2f}s  ·  {clip.status}"
            )
        self._changed()

    def _edit_boundaries(self):
        if self._loading_editor:
            return
        clip = self.selected_clip()
        if not clip:
            return
        start = round(self.start_edit.value() * 1000)
        end = round(self.end_edit.value() * 1000)
        duration = (
            self.source_info.get("duration_ms") if clip.source == self.source else None
        )
        if end <= start or (duration and end > duration):
            self.notice.setText("Choose a positive range within the source.")
            self._selection_changed()
            return
        clip.start_ms = start
        clip.end_ms = end
        self._changed()
        self._refresh_clips(clip.id)

    def _wave_trim(self, start, end):
        self.start_edit.setValue(start / 1000)
        self.end_edit.setValue(end / 1000)
        self._edit_boundaries()

    def preview_clip(self):
        clip = self.selected_clip()
        if not clip or not self._ensure_player():
            return
        if self.loaded_source != clip.source:
            self.load_media(clip.source, mark_dirty=False, replay=clip)
        else:
            self._start_preview(clip)
        self.mode.setCurrentIndex(0)
        self.notice.setText("Previewing " + clip.display_name)

    def _start_preview(self, clip):
        if not self.player or self.source != clip.source:
            return
        index = self.tracks.findData(clip.audio_track)
        if index >= 0:
            self.tracks.setCurrentIndex(index)
        self.player.set_audio_track(clip.audio_track)
        if not self.player.playing:
            self.player.toggle_pause()
        self.player.seek(clip.start_ms)
        self.preview_end = clip.end_ms

    def set_review_status(self, status):
        clips = self._selected_clips()
        for clip in clips:
            if clip.status != status:
                self._undo.append((clip.id, clip.status))
                clip.status = status
        if clips:
            self._changed()
            self._refresh_clips(clips[-1].id)

    def discard_selected(self):
        self.set_review_status("rejected")

    def discard_latest(self):
        if self.mode.currentIndex() == 1 and self.capture_running:
            self.capture.discard_latest()
            return
        clip = next(
            (c for c in reversed(self.project.clips) if c.status != "rejected"), None
        )
        if clip:
            self._undo.append((clip.id, clip.status))
            clip.status = "rejected"
            self._changed()
            self._refresh_clips(clip.id)

    def restore_discard(self):
        selected = self.selected_clip()
        if self.capture and selected and selected.id in self.capture.discarded_ids:
            self.capture.restore_clip(selected.id)
            return
        if (
            self.capture
            and self.mode.currentIndex() == 1
            and self.capture.has_discarded
        ):
            self.capture.restore_last()
            return
        if selected and selected.status == "rejected":
            selected.status = "pending"
            self._changed()
            self._refresh_clips(selected.id)
            return
        if self._undo:
            ident, status = self._undo.pop()
            clip = next((c for c in self.project.clips if c.id == ident), None)
            if clip:
                clip.status = status
                self._changed()
                self._refresh_clips(ident)
        elif self.mode.currentIndex() == 1 and self.capture:
            self.capture.restore_last()

    def refresh_devices(self):
        if self.capture_running:
            return
        self.devices.clear()
        self.devices.addItem("Default Windows output", None)

        def ready(devices):
            for device in devices:
                self.devices.addItem(device["name"], device["id"])
            idx = self.devices.findData(self.settings.get("device_id"))
            if idx >= 0:
                self.devices.setCurrentIndex(idx)

        from .capture import CaptureEngine

        self.run_job(
            lambda _: CaptureEngine.list_devices(),
            ready,
            lambda message: self.live_state.setText("Device discovery: " + message),
        )

    def just_listen(self):
        self.mode.setCurrentIndex(1)
        if not self.capture_running and not self.capture_stopping:
            self.toggle_listening()

    def _stop_capture(self):
        if not self.capture or self.capture_stopping:
            return
        self.capture_stopping = True
        self._remove_global_hooks()
        self.live_button.setEnabled(False)
        self.listen_btn.setEnabled(False)
        self.live_state.setText("Finishing pending clips…")
        engine = self.capture

        def finished(ok):
            self.capture_stopping = False
            self.live_button.setEnabled(True)
            self.listen_btn.setEnabled(True)
            self.capture_running = ok is False
            self.live_button.setText("Retry stop" if ok is False else "Start listening")
            self.devices.setEnabled(ok is not False)
            self.live_state.setText(
                "Could not stop recorder cleanly; retry Stop before restarting"
                if ok is False
                else "Stopped"
            )
            self._update_mark_label()
            self._focus_changed()

        def failed(message):
            self.capture_stopping = False
            self.capture_running = True
            self.live_button.setEnabled(True)
            self.listen_btn.setEnabled(True)
            self.live_button.setText("Retry stop")
            self.notice.setText(message)

        self.run_job(lambda _: engine.stop(), finished, failed)

    def toggle_listening(self):
        if self.capture_stopping:
            return
        if self.capture_running:
            self._stop_capture()
            return
        from .capture import CaptureEngine, CaptureConfig

        config = CaptureConfig(
            output_dir=self.settings["output_dir"],
            pre_roll=self.settings["pre_roll"],
            post_roll=self.settings["post_roll"],
            history_seconds=self.settings["history_seconds"],
            max_clip=self.settings["max_clip"],
        )
        self.capture = CaptureEngine(config, on_event=self.captureEvent.emit)
        self.capture_running = True
        self.settings["device_id"] = self.devices.currentData()
        self.devices.setEnabled(False)
        self.live_button.setText("Stop listening")
        self.live_state.setText("Opening audio device…")
        self.capture.start(self.devices.currentData())
        self._update_mark_label()

    def _capture_event(self, event):
        kind = event.get("type")
        if kind == "ready":
            self.live_state.setText(
                "Listening · " + str(event.get("device", "Windows output"))
            )
            self._install_global_hooks()
        elif kind == "level":
            self.level.setValue(min(100, round(float(event.get("peak", 0)) * 100)))
        elif kind == "state":
            state = event.get("state", "")
            label = {
                "idle": "Listening · ready",
                "recording": "Recording clip",
                "finishing": "Finishing clip · listening",
                "stopped": "Stopped",
            }.get(state, str(state).capitalize())
            self.live_state.setText(label)
            if self.tray:
                self.tray.setToolTip("Voice Clipper · " + label)
        elif kind in ("saved", "restored"):
            path = event.get("path")
            ident = event.get("clip_id")
            existing = next((c for c in self.project.clips if c.id == ident), None)
            if kind == "saved" and not existing and path:
                clip = self.project.add_clip(
                    path, 0, max(1, round(event["duration"] * 1000)), id=ident
                )
                clip.processing["capture"] = "wasapi_loopback"
                if event.get("warnings"):
                    clip.processing["session_stream_warnings"] = event["warnings"]
                self._changed()
                self._refresh_clips(clip.id)
                self.notice.setText("Saved live clip · " + Path(path).name)
            elif existing:
                existing.source = path or existing.source
                if kind == "restored":
                    existing.status = "pending"
                self._changed()
                self._refresh_clips(existing.id)
        elif kind == "discarded":
            clip = next(
                (c for c in self.project.clips if c.id == event.get("clip_id")), None
            )
            if clip:
                clip.status = "rejected"
                clip.source = event.get("path") or clip.source
                self._changed()
                self._refresh_clips(clip.id)
            elif event.get("path") and event.get("duration", 0) > 0:
                clip = self.project.add_clip(
                    event["path"],
                    0,
                    max(1, round(event["duration"] * 1000)),
                    id=event["clip_id"],
                    status="rejected",
                )
                clip.processing["capture"] = "wasapi_loopback"
                self._changed()
                self._refresh_clips(clip.id)
            self.notice.setText("Latest live take discarded · Restore can recover it")
        elif kind == "warning":
            self.notice.setText(
                event.get("message", "Review captured audio for stream artifacts.")
            )
        elif kind == "message":
            self.notice.setText(event.get("message", ""))
        elif kind == "error":
            message = event.get("message", "Audio capture failed")
            self.live_state.setText(message)
            self.notice.setText(message)
            if event.get("fatal"):
                self._remove_global_hooks()
                self._stop_capture()
            if self.tray:
                self.tray.showMessage(
                    "Voice Clipper · capture",
                    message,
                    QSystemTrayIcon.MessageIcon.Warning,
                )
        self._update_mark_label()
        self._focus_changed()

    def _install_shortcuts(self):
        for shortcut in self.shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.shortcuts = []
        self.shortcut_actions = {}
        mapping = {
            "toggle": self.mark_toggle,
            "last": self.save_previous,
            "discard": self.discard_latest,
            "restore": self.restore_discard,
        }
        for key, slot in mapping.items():
            shortcut = QShortcut(QKeySequence(self.settings["key_" + key]), self)
            shortcut.activated.connect(slot)
            shortcut.setAutoRepeat(False)
            self.shortcuts.append(shortcut)
            self.shortcut_actions[key] = shortcut
        for key, slot in (
            ("Space", self.play_pause),
            ("Left", lambda: self.seek_relative(-5000)),
            ("Right", lambda: self.seek_relative(5000)),
            ("F11", self.toggle_fullscreen),
            ("Escape", self.cancel_mark_or_fullscreen),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(slot)
            self.shortcuts.append(shortcut)
        self._focus_changed()

    def _focus_changed(self, *args):
        focused = QApplication.focusWidget()
        editing = isinstance(focused, (QLineEdit, QPlainTextEdit, QAbstractSpinBox))
        hooked = bool(self.global_hooks)
        for shortcut in self.shortcuts:
            is_hooked = hooked and shortcut in self.shortcut_actions.values()
            shortcut.setEnabled(not editing and not is_hooked)

    def _global_action(self, action):
        if action == "quit":
            if not self.isActiveWindow():
                self.show_from_tray()
                self.close()
            return
        if self.isActiveWindow() and isinstance(
            QApplication.focusWidget(), (QLineEdit, QPlainTextEdit, QAbstractSpinBox)
        ):
            return
        {
            "toggle": self.mark_toggle,
            "last": self.save_previous,
            "discard": self.discard_latest,
            "restore": self.restore_discard,
        }.get(action, lambda: None)()

    def _install_global_hooks(self):
        self._remove_global_hooks()
        if not self.settings["global_hotkeys"]:
            return
        try:
            import keyboard

            for action in ("toggle", "last", "discard", "restore"):
                handle = keyboard.add_hotkey(
                    self.settings["key_" + action],
                    lambda a=action: self.globalAction.emit(a),
                    trigger_on_release=True,
                )
                self.global_hooks.append(handle)
            self.global_hooks.append(
                keyboard.add_hotkey(
                    "ctrl+shift+q",
                    lambda: self.globalAction.emit("quit"),
                    trigger_on_release=True,
                )
            )
        except Exception as exc:
            self._remove_global_hooks()
            self.notice.setText(
                "Global hotkeys unavailable; use the app controls. " + str(exc)
            )
        self._focus_changed()

    def _remove_global_hooks(self):
        if self.global_hooks:
            import keyboard

            for handle in self.global_hooks:
                try:
                    keyboard.remove_hotkey(handle)
                except (KeyError, ValueError):
                    pass
        self.global_hooks = []

    def configure(self):
        if self.capture_running:
            self.notice.setText("Stop listening before changing capture settings.")
            return
        dialog = SettingsDialog(self.settings, self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                values = dialog.values()
                import keyboard

                for key in ("key_toggle", "key_last", "key_discard", "key_restore"):
                    keyboard.parse_hotkey(values[key])
                self.settings.update(values)
                self._write_preferences()
                self._install_shortcuts()
                self._update_mark_label()
                return
            except (ValueError, KeyError) as exc:
                QMessageBox.warning(dialog, "Check settings", str(exc))

    def export(self, all_clips):
        if self._export_cancel is not None:
            return
        clips = [
            c
            for c in (self.project.clips if all_clips else self._selected_clips())
            if c.status != "rejected"
        ]
        if not clips:
            self.notice.setText("Choose at least one clip that has not been rejected.")
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "Export WAV clips and metadata",
            self.settings.get("last_export_dir", str(self.data_dir)),
        )
        if not folder:
            return
        self.settings["last_export_dir"] = folder
        self._write_preferences()
        snapshot = [Clip(**c.to_dict()) for c in clips]
        cancelled = threading.Event()
        self._export_cancel = cancelled
        dialog = QProgressDialog("Preparing export…", "Cancel", 0, len(snapshot), self)
        dialog.setWindowTitle("Export clips")
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAutoClose(False)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(cancelled.set)
        self._export_dialog = dialog
        rate = self.sample_rate.currentData()
        self.export_all.setEnabled(False)
        self.export_selected.setEnabled(False)

        def done(paths):
            self.notice.setText(
                f"Exported {len(paths)} clip(s) · WAV + CSV + JSONL + Anki · {folder}"
            )
            if self.tray:
                self.tray.showMessage(
                    "Voice Clipper",
                    f"Exported {len(paths)} clips",
                    QSystemTrayIcon.MessageIcon.Information,
                )

        def progress(n, total, message):
            dialog.setMaximum(total)
            dialog.setValue(n)
            dialog.setLabelText(message)

        def error(message):
            self.notice.setText(message)

        def finish():
            dialog.close()
            dialog.deleteLater()
            self._export_dialog = None
            self._export_cancel = None
            self._refresh_clips()

        self.run_job(
            lambda notify: export_clips(
                snapshot,
                folder,
                sample_rate=rate,
                progress=notify,
                cancelled=cancelled.is_set,
            ),
            done,
            error,
            progress,
            finish,
        )

    def _create_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip("Voice Clipper")
        menu = QMenu(self)
        for label, slot in (
            ("Show Voice Clipper", self.show_from_tray),
            ("Mark / start-stop", self.mark_toggle),
            ("Save previous seconds", self.save_previous),
            ("Quit", self.close),
        ):
            action = menu.addAction(label)
            action.triggered.connect(slot)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.show_from_tray()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )
        self.tray.show()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        if self.tray:
            self.hide()
            self.tray.showMessage(
                "Voice Clipper", "Still running. Double-click the tray icon to return."
            )
        else:
            self.showMinimized()

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        for widget in (
            self.header,
            self.modebar,
            self.footer,
            self.review_panel,
            self.menuBar(),
        ):
            widget.setVisible(not self._fullscreen)
        if self._fullscreen:
            self.showFullScreen()
        else:
            self.showNormal()

    def cancel_mark_or_fullscreen(self):
        if self._fullscreen:
            self.toggle_fullscreen()
        elif self.pending_start is not None:
            self.pending_start = None
            self._update_mark_label()
            self.notice.setText("Unfinished mark cancelled")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.load_media(url.toLocalFile())
                event.acceptProposedAction()
                break

    def closeEvent(self, event):
        self._remove_global_hooks()
        if self.capture:
            stopped = self.capture.stop()
            QApplication.processEvents()
            if stopped is False and not self._force_close:
                self.notice.setText(
                    "Recorder has not stopped cleanly. Retry Stop before closing."
                )
                event.ignore()
                return
            self.capture_running = False
        if not self._force_close and not self._ask_save():
            event.ignore()
            return
        if self._export_cancel is not None:
            self._export_cancel.set()
        self.autosave.stop()
        if self._discard_changes and not self._force_close:
            try:
                (self.data_dir / "autosave.vclip").unlink(missing_ok=True)
            except OSError:
                pass
        else:
            self._write_recovery()
        self._write_preferences()
        self.timer.stop()
        if self.player:
            self.player.close()
        if self.tray:
            self.tray.hide()
        self.pool.waitForDone(10000)
        event.accept()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv[1:]
    if args and args[0] == "--diagnostics":
        if len(args) != 2:
            raise SystemExit("Usage: Voice Clipper --diagnostics report.json")
        from .diagnostics import write_report

        return write_report(args[1])
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Voice Clipper")
    app.setOrganizationName("Voice Clipper")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    if args:
        path = Path(args[0])
        if path.is_file():
            QTimer.singleShot(100, lambda: window.load_media(path))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
