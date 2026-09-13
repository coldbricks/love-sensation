"""General scene extraction with explicit fast and accurate cut modes."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QFileDialog, QSlider, QMessageBox, QFrame, QProgressBar,
)

from .video_engine import split_compilation, probe_media_file


class HarvestWorker(QThread):
    progress = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, video_path: str, output_dir: str, threshold: float, min_duration: float = 1.0, adaptive: bool = True, cut_mode: str = "copy"):
        super().__init__()
        self.video_path = Path(video_path)
        self.output_dir = Path(output_dir)
        self.threshold = threshold
        self.min_duration = min_duration
        self.adaptive = adaptive
        self.cut_mode = cut_mode
        import threading
        self.cancel_event = threading.Event()

    def run(self):
        try:
            def show_progress(event):
                if event.get("type") == "harvest_started":
                    self.progress.emit(f"Saving to {event['output_dir']}")
                elif event.get("take_index"):
                    self.progress.emit(f"Extracted clip {event['take_index']}: {Path(event.get('path', '')).name} ({event.get('duration', 0):.1f}s)")
            takes = split_compilation(
                self.video_path,
                self.output_dir,
                min_take_duration=self.min_duration,
                scene_threshold=self.threshold,
                adaptive_threshold=self.adaptive,
                emit=show_progress,
                cancel_event=self.cancel_event,
                cut_mode=self.cut_mode,
            )
            self.completed.emit([str(p) for p in takes])
        except InterruptedError:
            self.completed.emit([])
        except Exception as exc:
            self.failed.emit(str(exc))


class CompHarvesterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Love Sensation — Compilation Take Harvester")
        self.resize(780, 560)
        self.setStyleSheet("""
            QDialog { background: #101014; color: #f3eee4; font-family: 'Segoe UI'; font-size: 13px; }
            QFrame#card { background: #19191e; border: 1px solid #3e3b40; border-radius: 8px; padding: 18px; }
            QLabel#eyebrow { color: #c5b18f; font-size: 10px; font-weight: 650; letter-spacing: 1.3px; }
            QLineEdit, QDoubleSpinBox { background: #15151a; border: 1px solid #57515a; border-radius: 6px; padding: 8px 10px; color: #f3eee4; }
            QLineEdit:focus, QDoubleSpinBox:focus { border: 1px solid #d8b477; }
            QPushButton { background: #302c33; border: 1px solid #655c66; border-radius: 6px; padding: 9px 16px; font-weight: 600; color: #f3eee4; }
            QPushButton:hover { background: #403741; border-color: #b09b88; }
            QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ebcd99,stop:1 #c49a5e); border-color: #e8c58a; color: #211913; }
            QPushButton#primary:hover { background: #f2d6a3; }
            QSlider::groove:horizontal { height: 6px; background: #242128; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #cda96e; border-radius: 3px; }
            QSlider::handle:horizontal { background: #f3eee4; width: 16px; margin: -5px 0; border-radius: 8px; }
            QProgressBar { background: #242128; border: none; border-radius: 3px; max-height: 6px; }
            QProgressBar::chunk { background: #d8b477; border-radius: 3px; }
            QCheckBox { spacing: 7px; }
            QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #a7947b; border-radius: 3px; background: #15151a; }
            QCheckBox::indicator:checked { background: #cda96e; border-color: #f0d6a3; }
        """)
        self._worker: HarvestWorker | None = None
        self.harvested_takes = []
        self._pending_takes = None
        self._pending_error = None
        self._close_requested = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QVBoxLayout()
        header.addWidget(QLabel("COMPILATION HARVESTER", objectName="eyebrow"))
        title_lbl = QLabel("Extract scenes for your library")
        title_lbl.setStyleSheet("font-size: 20px; font-weight: 700; color: #f3eee4;")
        header.addWidget(title_lbl)
        layout.addLayout(header)

        card = QFrame(objectName="card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(12)

        # Video path
        card_layout.addWidget(QLabel("SOURCE COMPILATION VIDEO", objectName="eyebrow"))
        v_row = QHBoxLayout()
        self.video_edit = QLineEdit()
        self.video_edit.setPlaceholderText("Select video file (MP4, MKV, WebM, MOV)...")
        v_row.addWidget(self.video_edit, 1)
        v_browse = QPushButton("Browse…")
        v_browse.clicked.connect(self._browse_video)
        v_row.addWidget(v_browse)
        card_layout.addLayout(v_row)

        # Output folder
        card_layout.addWidget(QLabel("TAKES DESTINATION FOLDER", objectName="eyebrow"))
        o_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Select destination folder for harvested clips...")
        o_row.addWidget(self.output_edit, 1)
        o_browse = QPushButton("Browse…")
        o_browse.clicked.connect(self._browse_output)
        o_row.addWidget(o_browse)
        card_layout.addLayout(o_row)

        # Sensitivity slider & Min Duration row
        card_layout.addWidget(QLabel("SCENE CUT SENSITIVITY & DURATION FILTERS", objectName="eyebrow"))
        s_row = QHBoxLayout()
        self.sens_slider = QSlider(Qt.Orientation.Horizontal)
        self.sens_slider.setRange(15, 65)
        self.sens_slider.setValue(35)
        self.sens_slider.valueChanged.connect(self._update_sens_label)
        s_row.addWidget(self.sens_slider, 1)
        self.sens_val_lbl = QLabel("0.35 (Standard cuts)")
        self.sens_val_lbl.setFixedWidth(140)
        self.sens_val_lbl.setStyleSheet("color: #e8c58a; font-weight: 600;")
        s_row.addWidget(self.sens_val_lbl)
        card_layout.addLayout(s_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Min Take Duration (s):"))
        from PySide6.QtWidgets import QDoubleSpinBox, QCheckBox
        self.min_dur_spin = QDoubleSpinBox()
        self.min_dur_spin.setRange(0.2, 30.0)
        self.min_dur_spin.setValue(1.0)
        self.min_dur_spin.setSingleStep(0.5)
        self.min_dur_spin.setFixedWidth(80)
        filter_row.addWidget(self.min_dur_spin)
        filter_row.addSpacing(24)
        self.adaptive_check = QCheckBox("Adaptive Sensitivity (Multi-pass fallback)")
        self.adaptive_check.setChecked(True)
        filter_row.addWidget(self.adaptive_check)
        filter_row.addStretch()
        card_layout.addLayout(filter_row)
        card_layout.addWidget(QLabel("EXTRACTION MODE", objectName="eyebrow"))
        self.cut_mode_combo = QComboBox()
        self.cut_mode_combo.addItem("Fast copy — keyframe-aligned boundaries", "copy")
        self.cut_mode_combo.addItem("Accurate cuts — re-encode with GPU", "accurate")
        self.cut_mode_combo.setToolTip("Fast copy preserves compressed video but may include earlier frames. Accurate mode re-encodes at the requested boundaries.")
        card_layout.addWidget(self.cut_mode_combo)

        layout.addWidget(card)

        self.status_lbl = QLabel("Fast copy may include keyframe preroll. Actual output durations are recorded.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.status_lbl.setStyleSheet("color: #aaa6aa; font-size: 11px;")
        layout.addWidget(self.status_lbl)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        footer = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_harvest)
        footer.addWidget(self.cancel_btn)
        footer.addStretch()
        self.harvest_btn = QPushButton("Extract clips")
        self.harvest_btn.setObjectName("primary")
        self.harvest_btn.clicked.connect(self._start_harvest)
        footer.addWidget(self.harvest_btn)
        layout.addLayout(footer)

    def _update_sens_label(self, val: int):
        f = val / 100.0
        self.sens_val_lbl.setText(f"{f:.2f}")

    def _browse_video(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Compilation Video", "",
            "Video Files (*.mp4 *.mkv *.webm *.mov *.avi *.m4v);;All Files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if file_path:
            self.video_edit.setText(file_path)
            p = Path(file_path)
            self.output_edit.setText(str(p.parent / f"{p.stem}_takes"))

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder for Takes", options=QFileDialog.Option.DontUseNativeDialog)
        if folder:
            self.output_edit.setText(folder)

    def _start_harvest(self):
        if self.harvested_takes:
            self.accept()
            return
        v_path = self.video_edit.text().strip()
        o_path = self.output_edit.text().strip()
        if not v_path or not Path(v_path).is_file():
            QMessageBox.warning(self, "Invalid Video", "Please choose an existing video file.")
            return
        if not o_path:
            QMessageBox.warning(self, "Invalid Destination", "Please choose an output folder.")
            return

        thr = self.sens_slider.value() / 100.0
        min_dur = self.min_dur_spin.value()
        adaptive = self.adaptive_check.isChecked()

        self.progress_bar.show()
        self.harvest_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_lbl.setText("Extracting scenes…")
        self._pending_takes = self._pending_error = None

        self._worker = HarvestWorker(v_path, o_path, thr, min_duration=min_dur, adaptive=adaptive, cut_mode=self.cut_mode_combo.currentData())
        self._worker.progress.connect(lambda msg: self.status_lbl.setText(msg))
        self._worker.completed.connect(lambda takes: setattr(self, "_pending_takes", takes))
        self._worker.failed.connect(lambda error: setattr(self, "_pending_error", error))
        self._worker.finished.connect(self._worker_stopped)
        self._worker.start()

    def _cancel_harvest(self):
        if self._worker is not None:
            self._worker.cancel_event.set()
            self.status_lbl.setText("Cancelling harvest...")
            self.cancel_btn.setEnabled(False)

    def _on_finished(self, takes: list):
        self.progress_bar.hide()
        self.harvest_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.harvested_takes = takes
        self.status_lbl.setText(f"{len(takes)} clips saved. Open their folder in Library & Review to analyze them.")
        if takes:
            self.harvest_btn.setText("Open in Library & Review")

    def _on_failed(self, err: str):
        self.progress_bar.hide()
        self.harvest_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_lbl.setText("Harvest failed")
        QMessageBox.critical(self, "Harvest Error", f"Failed to split compilation:\n{err}")

    def _worker_stopped(self):
        worker, self._worker = self._worker, None
        cancelled = worker.cancel_event.is_set()
        worker.deleteLater()
        if self._close_requested:
            super().reject()
        elif self._pending_error:
            self._on_failed(self._pending_error)
        else:
            self._on_finished(self._pending_takes or [])
            if cancelled:
                self.status_lbl.setText(f"Cancelled safely. {len(self.harvested_takes)} completed clips are available.")

    def reject(self):
        if self._worker is not None and self._worker.isRunning():
            self._close_requested = True
            self._cancel_harvest()
            return
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._close_requested = True
            self._cancel_harvest()
            event.ignore()
        else:
            event.accept()
