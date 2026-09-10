"""Optional, bounded startup music from a user-selected local audio file.

No music is bundled, downloaded, recorded, or uploaded. Qt decodes local media
asynchronously; a wall-clock timer and media-position limit both stop the cue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import math
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal

from .engine import _assert_no_links, _atomic_json

_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".ogg"})


def one_bar_ms(bpm: float) -> int:
    """Four quarter-note beats, rounded down to Qt's millisecond resolution."""
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or not 40 <= bpm <= 240:
        raise ValueError("Tempo must be between 40 and 240 BPM.")
    return math.floor(240_000 / bpm)


@dataclass(frozen=True)
class AudioCueSettings:
    enabled: bool = False
    file_path: str = ""
    start_seconds: float = 0.0
    bpm: float = 120.0
    volume: float = 0.35
    muted: bool = False

    def validated(self, *, require_file: bool = False) -> "AudioCueSettings":
        if not isinstance(self.enabled, bool) or not isinstance(self.muted, bool):
            raise ValueError("Enabled and mute settings must be true or false.")
        one_bar_ms(self.bpm)
        for value, minimum, maximum, description in (
            (self.start_seconds, 0, 86_400, "Start time must be between 0 and 86,400 seconds."),
            (self.volume, 0, 1, "Volume must be between 0 and 100 percent."),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(description)
        if not isinstance(self.file_path, str):
            raise ValueError("Choose a local audio file.")
        if self.file_path:
            path = Path(self.file_path).expanduser()
            if (not path.is_absolute() or "://" in self.file_path or self.file_path.startswith(("\\\\", "//"))
                    or path.suffix.lower() not in _AUDIO_EXTENSIONS):
                raise ValueError("Choose a local WAV, MP3, FLAC, or OGG file.")
            if require_file:
                _assert_no_links(path)
                if not path.is_file():
                    raise ValueError("The selected audio file is unavailable. Choose it again.")
        elif require_file:
            raise ValueError("Choose an audio file first.")
        return self


class StartupAudioController(QObject):
    """Keep on the main window; startup playback is optional and never modal."""

    status_changed = Signal(str)
    playing_changed = Signal(bool)
    settings_changed = Signal(object)

    def __init__(self, parent=None, data_dir: Path | None = None, *, media_factory=None, timer_factory=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[1] / "data"
        self.settings_path = self.data_dir / "startup_audio.json"
        self.settings = self._load_settings()
        self.last_status = "Startup groove is off. Choose a local audio file to enable it."
        self._media_factory = media_factory
        self._player = None
        self._audio = None
        self._phase = "idle"
        self._startup_attempted = False
        self._dialog = None
        self._cue = None
        self._start_ms = 0
        self._end_ms = 0
        self._wall_started = False
        factory = timer_factory or (lambda parent: QTimer(parent))
        self._wall_timer = factory(self)
        self._load_timer = factory(self)
        for timer in (self._wall_timer, self._load_timer):
            timer.setSingleShot(True)
            timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._wall_timer.timeout.connect(lambda: self.stop("One bar finished."))
        self._load_timer.timeout.connect(lambda: self.stop("Audio took too long to load or seek. Try another start time or file."))

    def _load_settings(self) -> AudioCueSettings:
        try:
            _assert_no_links(self.settings_path)
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return AudioCueSettings(**payload).validated()
        except FileNotFoundError:
            return AudioCueSettings()
        except Exception:
            logging.warning("Startup audio settings could not be read; the cue remains disabled.")
            return AudioCueSettings()

    def save_settings(self, settings: AudioCueSettings) -> None:
        settings.validated(require_file=settings.enabled and not settings.muted)
        _atomic_json(self.settings_path, asdict(settings))
        self.settings = settings
        self.settings_changed.emit(settings)
        self._status("Startup groove saved." if settings.enabled and not settings.muted else "Startup groove is muted or disabled.")

    def _status(self, message: str) -> None:
        self.last_status = message
        self.status_changed.emit(message)

    def _ensure_media(self) -> None:
        if self._player is not None:
            return
        # Loading the app with the cue disabled never initializes an audio device.
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        if self._media_factory is None:
            self._player, self._audio = QMediaPlayer(self), QAudioOutput(self)
        else:
            self._player, self._audio = self._media_factory(self)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.errorOccurred.connect(self._on_error)

    def play_startup(self) -> bool:
        if self._startup_attempted:
            return False
        self._startup_attempted = True
        if not self.settings.enabled:
            return False
        return self.preview(self.settings)

    def preview(self, settings: AudioCueSettings | None = None) -> bool:
        """Play one local bar without persisting unsaved dialog settings."""
        self.stop()
        cue = settings or self.settings
        try:
            cue.validated()
            if cue.muted or cue.volume == 0:
                self._status("Startup groove is muted. Unmute it or raise the volume to preview.")
                return False
            cue.validated(require_file=True)
            self._ensure_media()
            self._cue = cue
            self._start_ms = round(cue.start_seconds * 1000)
            self._end_ms = self._start_ms + one_bar_ms(cue.bpm)
            self._wall_started = False
            self._phase = "loading"
            self._audio.setMuted(True)
            self._audio.setVolume(cue.volume)
            self._status("Loading your local audio cue…")
            self._load_timer.start(8000)
            self._player.setSource(QUrl.fromLocalFile(str(Path(cue.file_path).expanduser())))
            return True
        except Exception as error:
            self.stop(f"Could not play the startup groove: {error}")
            return False

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._phase == "idle":
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.stop("That audio file could not be opened. Try a WAV, MP3, FLAC, or OGG file.")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop("The audio file ended.")
        elif self._phase == "loading" and status in {
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
        }:
            if self._player.duration() <= 0:
                self.stop("This audio file has no readable duration.")
                return
            if self._end_ms > self._player.duration():
                self.stop("Choose an earlier start time with room for one full bar.")
                return
            if self._start_ms and not self._player.isSeekable():
                self.stop("This audio file cannot seek to the selected start time.")
                return
            self._phase = "seeking"
            self._player.setPosition(self._start_ms)
            if self._phase == "seeking" and abs(self._player.position() - self._start_ms) <= 5:
                self._begin_playback()

    def _begin_playback(self) -> None:
        if self._phase != "seeking":
            return
        self._phase = "playing"
        self._audio.setMuted(False)
        self._player.play()

    def _on_playback_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._phase != "playing":
            return
        if state == QMediaPlayer.PlaybackState.PlayingState and not self._wall_started:
            self._wall_started = True
            self._load_timer.stop()
            self._wall_timer.start(one_bar_ms(self._cue.bpm))
            self.playing_changed.emit(True)
            self._status(f"Playing one bar · {self._cue.bpm:g} BPM.")
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            self.stop("One bar finished.")

    def _on_position(self, position: int) -> None:
        if self._phase == "seeking" and abs(position - self._start_ms) <= 5:
            self._begin_playback()
        elif self._phase == "playing" and position >= self._end_ms:
            self.stop("One bar finished.")

    def _on_error(self, error, message="") -> None:
        if self._phase != "idle":
            self.stop(f"Audio playback stopped: {message or 'the file could not be decoded.'}")

    def stop(self, message: str | None = None) -> None:
        was_active = self._phase != "idle"
        self._phase = "idle"
        self._wall_timer.stop()
        self._load_timer.stop()
        if self._audio is not None:
            self._audio.setMuted(True)
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if was_active:
            self.playing_changed.emit(False)
        if message:
            self._status(message)

    def show_settings(self, parent=None) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._dialog = StartupAudioDialog(self, parent)
        self._dialog.finished.connect(self._close_dialog)
        self._dialog.open()

    def _close_dialog(self, result) -> None:
        self.stop()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.deleteLater()


from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
)


class StartupAudioDialog(QDialog):
    def __init__(self, controller: StartupAudioController, parent=None):
        super().__init__(parent)
        from .ui import STYLE

        self.controller = controller
        self.setStyleSheet(STYLE)
        self.setWindowTitle("Startup groove — Love Sensation")
        self.setMinimumWidth(620)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("Startup groove")
        title.setObjectName("title")
        layout.addWidget(title)
        description = QLabel("Play one bar from your own audio when Love Sensation opens.")
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(description)

        settings = controller.settings
        self.enabled_box = QCheckBox("Play at startup")
        self.enabled_box.setChecked(settings.enabled)
        layout.addWidget(self.enabled_box)
        file_row = QHBoxLayout()
        self.file_field = QLineEdit(settings.file_path)
        self.file_field.setReadOnly(True)
        self.file_field.setPlaceholderText("Choose your local audio file…")
        file_row.addWidget(self.file_field, 1)
        self.browse_button = QPushButton("Choose file…")
        self.browse_button.clicked.connect(self._choose_file)
        file_row.addWidget(self.browse_button)
        layout.addLayout(file_row)

        form = QFormLayout()
        self.start_field = QDoubleSpinBox()
        self.start_field.setRange(0, 86_400)
        self.start_field.setDecimals(2)
        self.start_field.setSuffix(" sec")
        self.start_field.setValue(settings.start_seconds)
        form.addRow("Start at", self.start_field)
        self.bpm_field = QDoubleSpinBox()
        self.bpm_field.setRange(40, 240)
        self.bpm_field.setDecimals(1)
        self.bpm_field.setSuffix(" BPM")
        self.bpm_field.setValue(settings.bpm)
        form.addRow("Tempo", self.bpm_field)
        self.volume_field = QSpinBox()
        self.volume_field.setRange(0, 100)
        self.volume_field.setSuffix("%")
        self.volume_field.setValue(round(settings.volume * 100))
        form.addRow("Volume", self.volume_field)
        layout.addLayout(form)
        self.muted_box = QCheckBox("Mute startup audio")
        self.muted_box.setChecked(settings.muted)
        layout.addWidget(self.muted_box)
        self.duration_label = QLabel()
        self.duration_label.setObjectName("muted")
        self.bpm_field.valueChanged.connect(self._update_duration)
        self._update_duration()
        layout.addWidget(self.duration_label)

        previews = QHBoxLayout()
        self.preview_button = QPushButton("Preview one bar")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(lambda: controller.preview(self._values()))
        previews.addWidget(self.preview_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(lambda: controller.stop("Preview stopped."))
        previews.addWidget(self.stop_button)
        previews.addStretch()
        layout.addLayout(previews)
        self.status_label = QLabel("Choose a local WAV, MP3, FLAC, or OGG file. Nothing is downloaded or uploaded.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        controller.status_changed.connect(self.status_label.setText)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(lambda result: controller.stop())

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose startup audio", self.file_field.text(),
            "Audio files (*.wav *.mp3 *.flac *.ogg)",
        )
        if path:
            self.file_field.setText(path)

    def _values(self) -> AudioCueSettings:
        return AudioCueSettings(
            enabled=self.enabled_box.isChecked(), file_path=self.file_field.text(),
            start_seconds=self.start_field.value(), bpm=self.bpm_field.value(),
            volume=self.volume_field.value() / 100, muted=self.muted_box.isChecked(),
        )

    def _update_duration(self):
        self.duration_label.setText(f"4 beats · {one_bar_ms(self.bpm_field.value()) / 1000:.2f} seconds")

    def _save(self):
        try:
            self.controller.save_settings(self._values())
        except Exception as error:
            self.status_label.setText(str(error))
            return
        self.accept()
