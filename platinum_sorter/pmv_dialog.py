"""The PMV Forge dialog: audio beat-grid analysis, Chaos Knob tuning, and Premiere XML export."""
from __future__ import annotations

import os
import random
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QFileDialog, QSlider, QSpinBox, QMessageBox, QFrame, QProgressBar,
    QWidget, QScrollArea, QDoubleSpinBox, QComboBox,
)

from .audio_grid import detect_tempo_and_grid, generate_waveform_svg_points
from .pmv_forge import assemble_pmv_timeline, export_fcp7_xml, prepare_candidate_clips
from .video_engine import is_video_path
from .flight_report import generate_flight_report


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
    def __init__(self, parent=None, candidate_clips: list[dict] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Love Sensation — PMV Forge & Beat-Lock Engine")
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
            QSlider::groove:horizontal { height: 6px; background: #242128; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #cda96e; border-radius: 3px; }
            QSlider::handle:horizontal { background: #f3eee4; width: 16px; margin: -5px 0; border-radius: 8px; }
            QProgressBar { background: #242128; border: none; border-radius: 3px; max-height: 6px; }
            QProgressBar::chunk { background: #d8b477; border-radius: 3px; }
        """)
        self.candidate_clips = candidate_clips or []
        self.audio_grid: dict | None = None
        self._worker: BeatWorker | None = None
        self._pending_identity = None
        self._analyzed_identity = None
        self._close_when_finished = False
        self._build_ui()
        self.audio_edit.textChanged.connect(self._invalidate_audio_grid)
        self.bpm_spin.valueChanged.connect(self._invalidate_audio_grid)

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
        self.assemble_btn.setEnabled(False)
        self.grid_summary.setText("Track or tempo changed · Analyze Track to refresh the grid")

    def closeEvent(self, event):
        if self._worker is not None:
            self._close_when_finished = True
            event.ignore()
            self.grid_summary.setText("Closing when audio analysis finishes…")
        else:
            super().closeEvent(event)

    def reject(self):
        if self._worker is not None:
            self._close_when_finished = True
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
        if self._close_when_finished:
            super().reject()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.addWidget(QLabel("THE PMV FORGE", objectName="eyebrow"))
        title_lbl = QLabel("Beat-Locked Timeline Assembly")
        title_lbl.setStyleSheet("font-size: 20px; font-weight: 700; color: #f3eee4;")
        title_box.addWidget(title_lbl)
        header.addLayout(title_box)
        header.addStretch()
        self.status_pill = QLabel("IDLE")
        self.status_pill.setStyleSheet("background: #262329; border: 1px solid #655b4e; color: #d8b477; border-radius: 12px; padding: 4px 12px; font-size: 10px; font-weight: 700;")
        header.addWidget(self.status_pill)
        layout.addLayout(header)

        # Card 1: Music Track
        music_card = QFrame(objectName="card")
        music_layout = QVBoxLayout(music_card)
        music_layout.setSpacing(10)
        music_layout.addWidget(QLabel("1 / SOUNDTRACK INGEST & TEMPO LOCK", objectName="eyebrow"))
        
        row = QHBoxLayout()
        self.audio_edit = QLineEdit()
        self.audio_edit.setPlaceholderText("Select song (WAV, MP3, FLAC, OGG)...")
        row.addWidget(self.audio_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_audio)
        row.addWidget(browse_btn)
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
        chaos_layout.addWidget(QLabel("2 / EDITING GRAMMAR & SEQUENCE CONTROLS", objectName="eyebrow"))

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
        self.clip_count_lbl = QLabel(f"{len(self.candidate_clips)} candidate clips ready")
        self.clip_count_lbl.setStyleSheet("color: #aaa6aa;")
        ctrl_row.addWidget(self.clip_count_lbl)
        chaos_layout.addLayout(ctrl_row)
        layout.addWidget(chaos_card)

        # Footer Actions
        footer = QHBoxLayout()
        footer.addStretch()
        self.assemble_btn = QPushButton("Assemble Timeline & Export Premiere XML…")
        self.assemble_btn.setObjectName("primary")
        self.assemble_btn.setEnabled(False)
        self.assemble_btn.clicked.connect(self._assemble_and_export)
        footer.addWidget(self.assemble_btn)
        layout.addLayout(footer)

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
        self.assemble_btn.setEnabled(True)

    def _on_beat_failed(self, error: str):
        self._invalidate_audio_grid()
        self.progress_bar.hide()
        self.analyze_beat_btn.setEnabled(True)
        self.status_pill.setText("ERROR")
        QMessageBox.critical(self, "Beat Detection Failed", f"Could not analyze music grid:\n{error}")

    def _assemble_and_export(self):
        if not self.audio_grid:
            return
        if self._analyzed_identity != self._audio_identity():
            self._invalidate_audio_grid()
            QMessageBox.warning(self, "Track Changed", "Analyze the current soundtrack before exporting.")
            return
        if not self.candidate_clips:
            # Fallback: browse for clips folder if none passed
            folder = QFileDialog.getExistingDirectory(self, "Select Folder of Video Clips for Assembly",
                options=QFileDialog.Option.DontUseNativeDialog)
            if not folder:
                return
            clips = [{"path": str(p)} for p in sorted(Path(folder).iterdir()) if p.is_file() and is_video_path(p)]
            if not clips:
                QMessageBox.warning(self, "No Clips Found", "No video clips found in the selected folder.")
                return
            self.candidate_clips = clips
        try:
            self.candidate_clips = prepare_candidate_clips(self.candidate_clips)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Media Unavailable", str(exc))
            return

        out_xml, _ = QFileDialog.getSaveFileName(
            self, "Save Premiere Pro XML Sequence", "pmv_assembly.xml", "Final Cut Pro XML (*.xml)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not out_xml:
            return

        fps_val = self.fps_combo.currentData() or 30
        chaos = self.chaos_slider.value() / 100.0
        seed = self.seed_spin.value()
        audio_path = self.audio_edit.text().strip()
        try:
            cuts = assemble_pmv_timeline(self.audio_grid, self.candidate_clips, chaos=chaos, seed=seed, fps=fps_val)
            export_fcp7_xml(cuts, audio_path, out_xml, fps=fps_val)
        except (ValueError, OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Export Not Completed", str(exc))
            return

        # Also generate matching flight_report.html
        report_html = Path(out_xml).with_suffix(".html")
        generate_flight_report(
            "PMV Assembly Cockpit",
            report_html,
            self.candidate_clips,
            audio_grid=self.audio_grid,
            cuts=[{"clip_name": c.clip_name, "timeline_start_s": c.timeline_start_s, "timeline_end_s": c.timeline_end_s, "tag": c.tag} for c in cuts],
            waveform_svg=self.audio_grid.get("waveform_svg"),
        )

        QMessageBox.information(
            self, "Assembly Complete",
            f"Successfully exported Premiere Pro sequence:\n{out_xml}\n\nInteractive Flight Report saved to:\n{report_html}"
        )
        QDesktopServices.openUrl(report_html.as_uri())
        self.accept()
