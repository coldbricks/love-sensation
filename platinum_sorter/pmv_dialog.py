"""The PMV Forge dialog: audio beat-grid analysis, Chaos Knob tuning, and Premiere XML export."""
from __future__ import annotations

import os
import random
import copy
import json
import threading
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QFileDialog, QSlider, QSpinBox, QMessageBox, QFrame, QProgressBar,
    QWidget, QScrollArea, QDoubleSpinBox, QComboBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

from .audio_grid import detect_tempo_and_grid, generate_waveform_svg_points
from .pmv_forge import assemble_pmv_timeline, export_fcp7_xml, prepare_candidate_clips
from .video_engine import is_video_path
from .flight_report import generate_flight_report
from .clip_library import scan_clip_folder


class EditWorker(QThread):
    progress = Signal(int, int, object)

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.cancel = threading.Event()
        self.result = self.error = None

    def run(self):
        try:
            self.result = self.job(self.cancel, self.progress.emit)
        except Exception as exc:
            self.error = str(exc)


class BeatWorker(QThread):
    def __init__(self, audio_path: str, requested_bpm: float | None = None):
        super().__init__()
        self.audio_path = audio_path
        self.requested_bpm = requested_bpm
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = detect_tempo_and_grid(self.audio_path, requested_bpm=self.requested_bpm)
        except Exception as exc:
            self.error = str(exc)


class PmvForgeDialog(QDialog):
    work_finished = Signal()

    def __init__(self, parent=None, candidate_clips: list[dict] | None = None,
                 *, embedded=False, settings_path=None, default_source=""):
        super().__init__(parent)
        self._embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.settings_path = Path(settings_path) if settings_path else None
        self.setWindowTitle("Love Sensation — Create music video")
        self.resize(900, 720)
        self.setStyleSheet("""
            QDialog { background: #101014; color: #f3eee4; font-family: 'Segoe UI'; font-size: 13px; }
            QFrame#card { background: #19191e; border: 1px solid #3e3b40; border-radius: 8px; padding: 18px; }
            QLabel#eyebrow { color: #c5b18f; font-size: 10px; font-weight: 650; letter-spacing: 1.3px; }
            QLabel#section { font-size: 15px; font-weight: 600; color: #e8c58a; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #15151a; border: 1px solid #57515a; border-radius: 6px; padding: 7px 10px; color: #f3eee4; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #d8b477; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #202025; color: #f3eee4; selection-background-color: #4c3b47; }
            QPushButton { background: #302c33; border: 1px solid #655c66; border-radius: 6px; padding: 9px 16px; font-weight: 600; color: #f3eee4; }
            QPushButton:hover { background: #403741; border-color: #b09b88; }
            QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ebcd99,stop:1 #c49a5e); border-color: #e8c58a; color: #211913; }
            QPushButton#primary:hover { background: #f2d6a3; }
            QPushButton:disabled, QPushButton#primary:disabled { background: #202025; color: #79747e; border-color: #39343e; }
            QSlider::groove:horizontal { height: 6px; background: #242128; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #cda96e; border-radius: 3px; }
            QSlider::handle:horizontal { background: #f3eee4; width: 16px; margin: -5px 0; border-radius: 8px; }
            QProgressBar { background: #242128; border: none; border-radius: 3px; max-height: 6px; }
            QProgressBar::chunk { background: #d8b477; border-radius: 3px; }
        """)
        self.candidate_clips = []
        self._review_candidates = copy.deepcopy(candidate_clips or [])
        self.cuts = []
        self._footage_generation = 0
        self._built_identity = None
        self._preview_signatures = {}
        self._export_directory = ""
        self.audio_grid: dict | None = None
        self._worker: BeatWorker | None = None
        self._pending_identity = None
        self._analyzed_identity = None
        self._close_when_finished = False
        self._build_ui()
        self.audio_edit.textChanged.connect(self._invalidate_audio_grid)
        self.bpm_spin.valueChanged.connect(self._invalidate_audio_grid)
        self.footage_edit.textChanged.connect(self._invalidate_footage)
        self.recursive_check.toggled.connect(self._invalidate_footage)
        self.chaos_slider.valueChanged.connect(self._invalidate_timeline)
        self.seed_spin.valueChanged.connect(self._invalidate_timeline)
        self.fps_combo.currentIndexChanged.connect(self._invalidate_timeline)
        self._load_preferences(default_source)
        self.set_review_candidates(self._review_candidates)
        self._refresh_ready()

    def is_busy(self):
        return self._worker is not None

    def request_stop(self):
        if self._worker is not None and hasattr(self._worker, "cancel"):
            self._worker.cancel.set()
        self.job_status.setText("Finishing the current operation safely…")

    def _timeline_identity(self):
        return (self._footage_generation, self._audio_identity(), self.chaos_slider.value(),
                self.seed_spin.value(), self.fps_combo.currentData())

    def _invalidate_timeline(self, *_):
        self.cuts = []
        self._built_identity = None
        self.timeline_table.setRowCount(0)
        self.timeline_summary.setText("Build a timeline to review its cuts here.")
        self._refresh_ready()

    def _invalidate_footage(self, *_):
        self._footage_generation += 1
        self.candidate_clips = []
        self.clip_count_lbl.setText("Choose a footage folder, then Load clips.")
        self._invalidate_timeline()

    def _refresh_ready(self):
        busy = self.is_busy()
        has_grid = bool(self.audio_grid and self._analyzed_identity == self._audio_identity())
        self.assemble_btn.setEnabled(not busy and bool(self.candidate_clips) and has_grid)
        self.export_btn.setEnabled(not busy and bool(self.cuts) and self._built_identity == self._timeline_identity())
        self.analyze_beat_btn.setEnabled(not busy)
        self.load_clips_btn.setEnabled(not busy and bool(self.footage_edit.text().strip()))
        self.review_clips_btn.setEnabled(not busy and bool(self._review_candidates))
        self.stop_btn.setEnabled(busy)
        for control in (self.footage_browse, self.audio_browse, self.recursive_check,
                        self.footage_edit, self.audio_edit, self.bpm_spin,
                        self.chaos_slider, self.seed_spin, self.fps_combo):
            control.setEnabled(not busy)

    def set_review_candidates(self, clips):
        self._review_candidates = copy.deepcopy(clips)
        self.review_clips_btn.setText(f"Use {len(clips)} clips from library review")
        self.review_clips_btn.setVisible(bool(clips))
        self._refresh_ready()

    def _browse_footage(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose footage folder", self.footage_edit.text(),
                                                  options=QFileDialog.Option.DontUseNativeDialog)
        if folder:
            self.footage_edit.setText(folder)
            self._start_footage_load()

    def _start_footage_load(self):
        if self.is_busy():
            return
        folder = self.footage_edit.text().strip().strip('"')
        recursive = self.recursive_check.isChecked()
        generation = self._footage_generation
        self.candidate_clips = []
        self._invalidate_timeline()
        self._start_job("footage", lambda cancel, progress: scan_clip_folder(
            Path(folder), recursive=recursive, cancel_event=cancel, progress=progress), generation)

    def _use_review_clips(self):
        if self.is_busy():
            return
        self._footage_generation += 1
        self.candidate_clips = []
        self._invalidate_timeline()
        candidates = copy.deepcopy(self._review_candidates)
        def job(cancel, progress):
            clips, skipped = [], []
            for i, candidate in enumerate(candidates):
                if cancel.is_set():
                    break
                try:
                    clips.extend(prepare_candidate_clips([candidate]))
                except (ValueError, OSError) as error:
                    skipped.append({"path": candidate.get("path", ""), "error": str(error)})
                progress(i + 1, len(candidates), Path(candidate.get("path", "")).name)
            return {"clips": clips, "skipped": skipped, "cancelled": cancel.is_set()}
        self._start_job("footage", job, self._footage_generation)

    def _start_job(self, kind, job, identity):
        self._job_kind, self._job_identity = kind, identity
        self._worker = EditWorker(job)
        self._worker.progress.connect(lambda done, total, path: self.job_status.setText(
            f"Checking clips {done:,}/{total:,} · {Path(path).name}"))
        self._worker.finished.connect(self._finish_edit_worker)
        self.job_status.setText({"footage": "Loading footage…", "timeline": "Building timeline…", "export": "Saving editing sequence…"}[kind])
        self.progress_bar.show()
        self._refresh_ready()
        self._worker.start()

    @staticmethod
    def _signatures(clips):
        result = {}
        for clip in clips:
            path = Path(clip["path"])
            stat = path.stat()
            result[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return result

    def _finish_edit_worker(self):
        worker, kind, identity = self._worker, self._job_kind, self._job_identity
        worker.wait()
        self._worker = None
        self.progress_bar.hide()
        result = worker.result
        if worker.error:
            self.job_status.setText(worker.error)
        elif result is not None and not self._close_when_finished:
            if kind == "footage" and identity == self._footage_generation:
                if result.get("cancelled"):
                    self.job_status.setText("Loading stopped. Load clips again when ready.")
                else:
                    self.candidate_clips = result["clips"]
                    seconds = sum(c.get("duration_s", 0) for c in self.candidate_clips)
                    skipped = result.get("skipped", [])
                    self.clip_count_lbl.setText(f"{len(self.candidate_clips):,} clips ready · {seconds / 60:.1f} minutes · {len(skipped):,} skipped")
                    self.clip_count_lbl.setToolTip("\n".join(f"{Path(s['path']).name}: {s.get('reason', s.get('error', 'Unavailable'))}" for s in skipped[:20]))
                    self.job_status.setText("Footage ready. Choose your music." if self.candidate_clips else "No usable clips found. Choose another folder.")
            elif kind == "timeline" and identity == self._timeline_identity():
                self.candidate_clips, self.cuts, self._preview_signatures = result
                self._built_identity = identity
                self._show_timeline()
                self.job_status.setText("Timeline ready. Review the cuts, then export.")
            elif kind == "export":
                self.job_status.setText(f"Saved {result['xml']}" + (f" · {result['warning']}" if result.get('warning') else ""))
            else:
                self.job_status.setText("Inputs changed. Load or build again to use the current choices.")
        self._save_preferences()
        self._refresh_ready()
        worker.deleteLater()
        self.work_finished.emit()
        if self._close_when_finished and not self._embedded:
            super().reject()

    def _show_timeline(self):
        self.timeline_table.setRowCount(len(self.cuts))
        for row, cut in enumerate(self.cuts):
            for col, value in enumerate((str(row + 1), f"{cut.timeline_start_s:.2f}–{cut.timeline_end_s:.2f}s",
                                         cut.clip_name, f"{cut.clip_in_s:.2f}s", f"{cut.duration_s:.2f}s")):
                self.timeline_table.setItem(row, col, QTableWidgetItem(value))
        duration = self.cuts[-1].timeline_end_s if self.cuts else 0
        self.timeline_summary.setText(f"{len(self.cuts)} cuts · {duration:.1f}s · {self.fps_combo.currentText()} · editing sequence")

    def _load_preferences(self, default_source):
        saved = {}
        if self.settings_path:
            try:
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if not isinstance(saved, dict):
            saved = {}
        self.footage_edit.setText(str(saved.get("footage_folder", default_source)))
        self.audio_edit.setText(str(saved.get("audio_path", "")))
        self.recursive_check.setChecked(bool(saved.get("recursive", True)))
        self._export_directory = str(saved.get("export_directory", ""))
        for control, key, default, convert in ((self.bpm_spin, "bpm", 0, float),
                (self.seed_spin, "seed", 7, int), (self.chaos_slider, "chaos", 50, int)):
            try:
                control.setValue(convert(saved.get(key, default)))
            except (TypeError, ValueError, OverflowError):
                control.setValue(default)
        self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(saved.get("fps", 30))))

    def _save_preferences(self):
        if not self.settings_path:
            return
        saved = {"footage_folder": self.footage_edit.text(), "audio_path": self.audio_edit.text(),
                 "recursive": self.recursive_check.isChecked(), "bpm": self.bpm_spin.value(),
                 "seed": self.seed_spin.value(), "chaos": self.chaos_slider.value(),
                 "fps": self.fps_combo.currentData(), "export_directory": self._export_directory}
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            temporary.replace(self.settings_path)
        except OSError as error:
            self.job_status.setText(f"Could not save editor settings: {error}")

    def _audio_identity(self):
        try:
            path = Path(self.audio_edit.text().strip()).resolve()
            stat = path.stat()
            return (str(path), stat.st_size, stat.st_mtime_ns, self.bpm_spin.value()) if path.is_file() else None
        except OSError:
            return None

    def _invalidate_audio_grid(self, *_):
        self.audio_grid = None
        self._analyzed_identity = None
        self._invalidate_timeline()
        self.grid_summary.setText("Track or tempo changed · Analyze Track to refresh the grid")

    def closeEvent(self, event):
        self._save_preferences()
        if self._worker is not None:
            self._close_when_finished = True
            self.request_stop()
            event.ignore()
            self.grid_summary.setText("Closing when audio analysis finishes…")
        else:
            super().closeEvent(event)

    def reject(self):
        self._save_preferences()
        if self._worker is not None:
            self._close_when_finished = True
            self.request_stop()
            self.grid_summary.setText("Closing when audio analysis finishes…")
            return
        super().reject()

    def _finish_beat_worker(self):
        worker = self._worker
        if worker is None:
            return
        worker.wait()
        self._worker = None
        self.progress_bar.hide()
        self.analyze_beat_btn.setEnabled(True)
        if not self._close_when_finished:
            if worker.error is not None:
                self._on_beat_failed(worker.error)
            elif worker.result is not None:
                self._on_beat_finished(worker.result)
        worker.deleteLater()
        self._save_preferences()
        self._refresh_ready()
        self.work_finished.emit()
        if self._close_when_finished:
            if not self._embedded:
                super().reject()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.addWidget(QLabel("FOOTAGE  →  MUSIC  →  EDIT  →  EXPORT", objectName="eyebrow"))
        title_lbl = QLabel("Create a music video")
        title_lbl.setStyleSheet("font-size: 20px; font-weight: 700; color: #f3eee4;")
        title_box.addWidget(title_lbl)
        header.addLayout(title_box)
        header.addStretch()
        self.status_pill = QLabel("IDLE")
        self.status_pill.setStyleSheet("background: #262329; border: 1px solid #655b4e; color: #d8b477; border-radius: 12px; padding: 4px 12px; font-size: 10px; font-weight: 700;")
        header.addWidget(self.status_pill)
        layout.addLayout(header)

        footage_card = QFrame(objectName="card")
        footage_layout = QVBoxLayout(footage_card)
        footage_layout.addWidget(QLabel("1 / YOUR FOOTAGE", objectName="eyebrow"))
        hint = QLabel("Choose the folder of video clips you want to edit. Originals stay in place.")
        hint.setWordWrap(True)
        footage_layout.addWidget(hint)
        footage_row = QHBoxLayout()
        self.footage_edit = QLineEdit()
        self.footage_edit.setPlaceholderText("Folder containing your video clips")
        self.footage_edit.setAccessibleName("Music video footage folder")
        footage_row.addWidget(self.footage_edit, 1)
        self.footage_browse = QPushButton("Choose footage folder…")
        self.footage_browse.setObjectName("primary")
        self.footage_browse.clicked.connect(self._browse_footage)
        footage_row.addWidget(self.footage_browse)
        self.load_clips_btn = QPushButton("Load clips")
        self.load_clips_btn.clicked.connect(self._start_footage_load)
        footage_row.addWidget(self.load_clips_btn)
        footage_layout.addLayout(footage_row)
        footage_options = QHBoxLayout()
        self.recursive_check = QCheckBox("Include subfolders")
        self.recursive_check.setChecked(True)
        footage_options.addWidget(self.recursive_check)
        self.review_clips_btn = QPushButton("Use clips from library review")
        self.review_clips_btn.clicked.connect(self._use_review_clips)
        footage_options.addWidget(self.review_clips_btn)
        footage_options.addStretch()
        footage_layout.addLayout(footage_options)
        self.clip_count_lbl = QLabel("Choose a footage folder, then Load clips.")
        self.clip_count_lbl.setWordWrap(True)
        footage_layout.addWidget(self.clip_count_lbl)
        layout.addWidget(footage_card)

        # Card 1: Music Track
        music_card = QFrame(objectName="card")
        music_layout = QVBoxLayout(music_card)
        music_layout.setSpacing(10)
        music_layout.addWidget(QLabel("2 / YOUR MUSIC", objectName="eyebrow"))
        
        row = QHBoxLayout()
        self.audio_edit = QLineEdit()
        self.audio_edit.setPlaceholderText("Select song (WAV, MP3, FLAC, OGG)...")
        row.addWidget(self.audio_edit, 1)
        self.audio_browse = QPushButton("Choose music…")
        self.audio_browse.clicked.connect(self._browse_audio)
        row.addWidget(self.audio_browse)
        self.analyze_beat_btn = QPushButton("Analyze Track")
        self.analyze_beat_btn.setObjectName("primary")
        self.analyze_beat_btn.clicked.connect(self._start_beat_analysis)
        row.addWidget(self.analyze_beat_btn)
        music_layout.addLayout(row)

        bpm_row = QHBoxLayout()
        bpm_row.addWidget(QLabel("BPM Override (0 = Auto):"))
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(0.0, 240.0)
        self.bpm_spin.setValue(0.0)
        self.bpm_spin.setSingleStep(1.0)
        self.bpm_spin.setDecimals(2)
        self.bpm_spin.setFixedWidth(100)
        bpm_row.addWidget(self.bpm_spin)
        bpm_row.addSpacing(20)
        self.grid_summary = QLabel("No track analyzed yet · Select a music file and click Analyze Track")
        self.grid_summary.setStyleSheet("color: #aaa6aa; font-size: 11px;")
        bpm_row.addWidget(self.grid_summary, 1)
        music_layout.addLayout(bpm_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        music_layout.addWidget(self.progress_bar)
        layout.addWidget(music_card)

        # Card 2: Chaos Knob & Sequence Settings
        chaos_card = QFrame(objectName="card")
        chaos_layout = QVBoxLayout(chaos_card)
        chaos_layout.setSpacing(14)
        chaos_layout.addWidget(QLabel("3 / EDIT SETTINGS", objectName="eyebrow"))

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Chaos:"))
        self.chaos_slider = QSlider(Qt.Orientation.Horizontal)
        self.chaos_slider.setRange(0, 100)
        self.chaos_slider.setValue(50)
        self.chaos_slider.valueChanged.connect(self._update_chaos_label)
        slider_row.addWidget(self.chaos_slider, 1)
        self.chaos_val_lbl = QLabel("0.50 (Balanced groove)")
        self.chaos_val_lbl.setFixedWidth(170)
        self.chaos_val_lbl.setStyleSheet("font-weight: 600; color: #e8c58a;")
        slider_row.addWidget(self.chaos_val_lbl)
        chaos_layout.addLayout(slider_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Edit Seed:"))
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(1, 999999)
        self.seed_spin.setValue(7)
        self.seed_spin.setFixedWidth(100)
        ctrl_row.addWidget(self.seed_spin)
        dice_btn = QPushButton("🎲 Reroll Seed")
        dice_btn.clicked.connect(lambda: self.seed_spin.setValue(random.randint(1, 999999)))
        ctrl_row.addWidget(dice_btn)
        
        ctrl_row.addSpacing(20)
        ctrl_row.addWidget(QLabel("Timeline Rate:"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItem("30 FPS (Standard)", 30)
        self.fps_combo.addItem("24 FPS (Cinematic)", 24)
        self.fps_combo.addItem("60 FPS (High Speed)", 60)
        self.fps_combo.addItem("29.97 FPS (NTSC)", 29.97)
        self.fps_combo.setFixedWidth(160)
        ctrl_row.addWidget(self.fps_combo)

        ctrl_row.addStretch()
        chaos_layout.addLayout(ctrl_row)
        layout.addWidget(chaos_card)

        preview_card = QFrame(objectName="card")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.addWidget(QLabel("4 / TIMELINE PREVIEW", objectName="eyebrow"))
        self.timeline_summary = QLabel("Build a timeline to review its cuts here.")
        self.timeline_summary.setWordWrap(True)
        preview_layout.addWidget(self.timeline_summary)
        self.timeline_table = QTableWidget(0, 5)
        self.timeline_table.setHorizontalHeaderLabels(["#", "Timeline", "Source clip", "Source in", "Length"])
        self.timeline_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.timeline_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.timeline_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.timeline_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.timeline_table.verticalHeader().hide()
        self.timeline_table.setMinimumHeight(180)
        preview_layout.addWidget(self.timeline_table)
        note = QLabel("Export creates an editable XML sequence for Premiere Pro or a compatible editor. Movie rendering happens in your editor.")
        note.setWordWrap(True)
        preview_layout.addWidget(note)
        layout.addWidget(preview_card)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # Footer Actions
        footer = QHBoxLayout()
        footer.setContentsMargins(24, 10, 24, 16)
        self.job_status = QLabel("Choose footage and music to get started.")
        self.job_status.setWordWrap(True)
        footer.addWidget(self.job_status, 1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.request_stop)
        footer.addWidget(self.stop_btn)
        self.assemble_btn = QPushButton("Build timeline")
        self.assemble_btn.setObjectName("primary")
        self.assemble_btn.setEnabled(False)
        self.assemble_btn.clicked.connect(self._assemble_and_export)
        footer.addWidget(self.assemble_btn)
        self.export_btn = QPushButton("Export XML…")
        self.export_btn.clicked.connect(self._export_timeline)
        footer.addWidget(self.export_btn)
        outer.addLayout(footer)

    def _update_chaos_label(self, val: int):
        f = val / 100.0
        desc = "Square 1-bar" if f < 0.25 else ("Balanced groove" if f < 0.75 else "Rapid syncopation")
        self.chaos_val_lbl.setText(f"{f:.2f} ({desc})")

    def _browse_audio(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Choose Audio Track", "",
            "Audio Files (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;All Files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if file_path:
            self.audio_edit.setText(file_path)

    def _start_beat_analysis(self):
        if self._worker is not None:
            return
        path = self.audio_edit.text().strip()
        if not path or not Path(path).is_file():
            QMessageBox.warning(self, "Invalid Audio", "Please choose an existing audio track.")
            return
        self.progress_bar.show()
        self._invalidate_audio_grid()
        self._pending_identity = self._audio_identity()
        self.status_pill.setText("ANALYZING BEATS")
        self.analyze_beat_btn.setEnabled(False)
        req_bpm = self.bpm_spin.value() if self.bpm_spin.value() > 0 else None
        self._worker = BeatWorker(path, requested_bpm=req_bpm)
        self._worker.finished.connect(self._finish_beat_worker)
        self.job_status.setText("Analyzing music…")
        self._refresh_ready()
        self._worker.start()

    def _on_beat_finished(self, grid: dict):
        self.progress_bar.hide()
        self.analyze_beat_btn.setEnabled(True)
        if self._pending_identity != self._audio_identity():
            self._invalidate_audio_grid()
            self.status_pill.setText("TRACK CHANGED")
            return
        if grid.get("reliable") is False or not grid.get("bars"):
            self._invalidate_audio_grid()
            self.status_pill.setText("NO GRID")
            self.grid_summary.setText(grid.get("reason") or "No reliable rhythmic grid was found")
            return
        self.audio_grid = grid
        self._analyzed_identity = self._pending_identity
        bpm = grid.get("bpm", 0.0)
        bars = len(grid.get("bars", []))
        drops = len(grid.get("drop_bars", []))
        dur = grid.get("total_duration_s", 0.0)
        self.status_pill.setText(f"{bpm} BPM")
        self.grid_summary.setText(f"Estimated grid: {bpm} BPM · {bars} bars ({dur:.1f}s) · 4/4 assumed")
        self.grid_summary.setStyleSheet("color: #e8c58a; font-weight: 600;")
        self.job_status.setText("Music ready. Build your timeline." if self.candidate_clips else "Music ready. Load your footage to continue.")
        self._refresh_ready()

    def _on_beat_failed(self, error: str):
        self._invalidate_audio_grid()
        self.progress_bar.hide()
        self.analyze_beat_btn.setEnabled(True)
        self.status_pill.setText("ERROR")
        QMessageBox.critical(self, "Beat Detection Failed", f"Could not analyze music grid:\n{error}")

    def _assemble_and_export(self):
        """Build a reviewable timeline; exporting is a separate deliberate step."""
        if self.is_busy() or not self.candidate_clips or not self.audio_grid:
            return
        if self._analyzed_identity != self._audio_identity():
            self._invalidate_audio_grid()
            return
        identity = self._timeline_identity()
        clips, grid = copy.deepcopy(self.candidate_clips), copy.deepcopy(self.audio_grid)
        fps, chaos, seed = self.fps_combo.currentData(), self.chaos_slider.value() / 100.0, self.seed_spin.value()
        self._invalidate_timeline()
        def build(cancel, progress):
            before = self._signatures(clips)
            prepared = []
            for index, clip in enumerate(clips, 1):
                if cancel.is_set():
                    raise InterruptedError("Timeline build stopped.")
                prepared.extend(prepare_candidate_clips([clip]))
                progress(index, len(clips), clip["path"])
            if cancel.is_set():
                raise InterruptedError("Timeline build stopped.")
            cuts = assemble_pmv_timeline(grid, prepared, chaos=chaos, seed=seed, fps=fps)
            if not cuts:
                raise ValueError("No cuts could be assembled from this footage and music grid.")
            if self._signatures(prepared) != before:
                raise ValueError("Footage changed while building. Load clips again.")
            return prepared, cuts, before
        self._start_job("timeline", build, identity)

    def _export_timeline(self):
        if self.is_busy() or not self.cuts or self._built_identity != self._timeline_identity():
            return
        if self._analyzed_identity != self._audio_identity():
            self._invalidate_audio_grid()
            return
        initial = str(Path(self._export_directory or '.') / 'music_video.xml')
        out_xml, _ = QFileDialog.getSaveFileName(
            self, "Export editable music-video sequence", initial, "Final Cut Pro XML (*.xml)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not out_xml:
            return
        target = Path(out_xml).with_suffix('.xml').resolve()
        self._export_directory = str(target.parent)
        cuts, clips, grid = copy.deepcopy(self.cuts), copy.deepcopy(self.candidate_clips), copy.deepcopy(self.audio_grid)
        signatures = dict(self._preview_signatures)
        audio, fps = self.audio_edit.text().strip(), self.fps_combo.currentData()
        audio_identity = self._audio_identity()
        identity = self._built_identity
        def export(cancel, progress):
            if cancel.is_set():
                raise InterruptedError("Export stopped.")
            if self._signatures(clips) != signatures:
                raise ValueError("Footage changed since the preview. Load clips and rebuild before exporting.")
            stat = Path(audio).stat()
            if (stat.st_size, stat.st_mtime_ns) != audio_identity[1:3]:
                raise ValueError("Music changed since analysis. Analyze the track again.")
            export_fcp7_xml(cuts, audio, target, fps=fps, cancel_event=cancel)
            warning = ''
            try:
                generate_flight_report(
                    "Music video timeline", target.with_suffix('.html'), clips, audio_grid=grid,
                    cuts=[{"clip_name": c.clip_name, "timeline_start_s": c.timeline_start_s,
                           "timeline_end_s": c.timeline_end_s, "tag": c.tag} for c in cuts],
                    waveform_svg=grid.get('waveform_svg'),
                )
            except (OSError, ValueError) as error:
                warning = f"XML saved; companion report failed: {error}"
            return {"xml": str(target), "warning": warning}
        self._start_job("export", export, identity)
