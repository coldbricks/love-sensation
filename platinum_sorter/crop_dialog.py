"""Face-only export controls; image processing stays on a worker thread."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout,
)

from .contracts import ScanReport


class FaceCropWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    event = Signal(dict)

    def __init__(self, phase, report, service=None, *, candidate=None,
                 candidates=None, destination=None, padding=0.10, parent=None):
        super().__init__(parent)
        self.phase = phase
        self.report = report
        self.service = service
        self.candidate = candidate
        self.candidates = candidates
        self.destination = destination
        self.padding = padding
        self.cancel = threading.Event()

    def run(self):
        try:
            if self.service is None:
                from .face_crops import FaceCropService
                self.service = FaceCropService(self.report)
            if self.cancel.is_set():
                if self.phase == "export":
                    self.completed.emit({
                        "exported": 0, "errors": [], "outputs": [],
                        "cancelled": True, "manifest_path": "", "info": {},
                    })
                return
            if self.phase == "load":
                result = self.service.candidates()
            elif self.phase == "preview":
                result = self.service.preview(self.candidate, padding=self.padding)
            elif self.phase == "export":
                result = self.service.export(
                    self.candidates, self.destination, self.padding,
                    self.event.emit, self.cancel,
                )
            else:
                raise ValueError(f"Unknown face crop action: {self.phase}")
            self.completed.emit(result)
        except Exception as exc:
            logging.exception("Face crop %s failed", self.phase)
            self.failed.emit(str(exc) or type(exc).__name__)


class FaceCropDialog(QDialog):
    """Preview one detected face and export every eligible face in a run."""

    def __init__(self, report: ScanReport, parent=None, data_dir=None):
        super().__init__(parent)
        # Import only the existing Qt theme, never the processing backend here.
        from .ui import STYLE
        self.setStyleSheet(STYLE)
        self.setWindowTitle("Face crops — Love Sensation")
        self.setModal(True)
        self.resize(980, 700)
        self.setMinimumSize(880, 640)
        self.report = report
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.last_export: dict | None = None
        self.candidates: list[dict] = []
        self._service = None
        self._worker: FaceCropWorker | None = None
        self._pending_result = None
        self._pending_error = ""
        self._close_when_ready = False
        self._preview_pixmap = QPixmap()
        self._export_destination: Path | None = None
        self._build_ui()
        self._set_busy(False)
        QTimer.singleShot(0, self._load_candidates)

    @staticmethod
    def _label(text, name=None):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if name:
            label.setObjectName(name)
        return label

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(self._label("Face crops", "title"))
        titles.addWidget(self._label("Preview detected faces and save a separate crop for each one.", "muted"))
        header.addLayout(titles)
        header.addStretch()
        self.device_label = self._label("Local export", "device")
        header.addWidget(self.device_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(14)
        list_card = QFrame()
        list_card.setObjectName("card")
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(0, 13, 0, 0)
        self.count_label = self._label("Loading detected faces…", "section")
        self.count_label.setContentsMargins(14, 0, 14, 3)
        list_layout.addWidget(self.count_label)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("FILE", "FACE", "CONFIDENCE"))
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 65)
        self.table.setColumnWidth(2, 110)
        self.table.setAccessibleName("Detected faces available for export")
        self.table.itemSelectionChanged.connect(self._selection_changed)
        list_layout.addWidget(self.table, 1)
        self.list_hint = self._label("Export includes every face in this list.", "muted")
        self.list_hint.setContentsMargins(14, 5, 14, 12)
        self.list_hint.setWordWrap(True)
        list_layout.addWidget(self.list_hint)
        body.addWidget(list_card, 3)

        preview_card = QFrame()
        preview_card.setObjectName("card")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 13, 14, 13)
        preview_layout.addWidget(self._label("SELECTED FACE", "eyebrow"))
        self.preview_image = self._label("Select a face, then preview.", "muted")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(240, 180)
        self.preview_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview_image.setStyleSheet("background: #141418; border-radius: 8px; color: #aaa6aa;")
        preview_layout.addWidget(self.preview_image, 1)
        self.preview_caption = self._label("Only the face crop is shown here.", "muted")
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        preview_controls = QHBoxLayout()
        preview_controls.addWidget(self._label("Padding", "muted"))
        self.padding_spin = QDoubleSpinBox()
        self.padding_spin.setRange(0, 30)
        self.padding_spin.setDecimals(0)
        self.padding_spin.setSingleStep(1)
        self.padding_spin.setValue(10)
        self.padding_spin.setSuffix("%")
        self.padding_spin.setMaximumWidth(90)
        self.padding_spin.setAccessibleName("Face crop padding percent")
        self.padding_spin.valueChanged.connect(self._selection_changed)
        preview_controls.addWidget(self.padding_spin)
        preview_controls.addStretch()
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._preview_selected)
        preview_controls.addWidget(self.preview_button)
        preview_layout.addLayout(preview_controls)
        body.addWidget(preview_card, 2)
        layout.addLayout(body, 1)

        output_row = QHBoxLayout()
        output_row.addWidget(self._label("Save crops to", "muted"))
        self.destination_edit = QLineEdit(str(Path(self.report.options.destination) / "_FaceCrops"))
        self.destination_edit.setAccessibleName("Face crop output folder")
        self.destination_edit.textChanged.connect(self._refresh_export)
        output_row.addWidget(self.destination_edit, 1)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse_output)
        output_row.addWidget(self.browse_button)
        layout.addLayout(output_row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status_label = self._label("Reading the completed analysis…", "muted")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(30)
        layout.addWidget(self.status_label)
        self.error_details = QTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setAccessibleName("Face crop errors")
        self.error_details.setMaximumHeight(85)
        self.error_details.setStyleSheet("background: #202025; color: #e69b9c; border: 1px solid #655c66; border-radius: 6px; padding: 5px;")
        self.error_details.hide()
        layout.addWidget(self.error_details)
        footer = QHBoxLayout()
        self.open_button = QPushButton("Open output")
        self.open_button.clicked.connect(self._open_output)
        self.open_button.setEnabled(False)
        footer.addWidget(self.open_button)
        footer.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        self.export_button = QPushButton("Export face crops")
        self.export_button.setObjectName("apply")
        self.export_button.clicked.connect(self._export)
        footer.addWidget(self.export_button)
        layout.addLayout(footer)

    def _load_candidates(self):
        if not self._close_when_ready:
            self._start("load")

    def _start(self, phase, **kwargs):
        if self._worker is not None:
            return
        self._pending_result = None
        self._pending_error = ""
        self.error_details.hide()
        self._worker = FaceCropWorker(
            phase, self.report, self._service, parent=self, **kwargs,
        )
        self._worker.completed.connect(self._receive_result)
        self._worker.failed.connect(self._receive_error)
        self._worker.event.connect(self._on_event)
        self._worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        self.progress.setRange(0, 0)
        self.status_label.setText({
            "load": "Reading detected faces from this analysis…",
            "preview": "Preparing the selected face preview…",
            "export": f"Exporting all {len(self.candidates):,} face crops…",
        }[phase])
        self._worker.start()

    def _set_busy(self, busy):
        for widget in (self.table, self.destination_edit, self.browse_button, self.padding_spin):
            widget.setEnabled(not busy)
        self.preview_button.setEnabled(not busy and self.table.currentRow() >= 0)
        self.cancel_button.setEnabled(busy and self._worker is not None and not self._worker.cancel.is_set())
        self.open_button.setEnabled(not busy and self._export_destination is not None)
        self._refresh_export()

    def _refresh_export(self, *_):
        # textChanged can fire before the footer has been constructed.
        if hasattr(self, "export_button"):
            self.export_button.setEnabled(
                self._worker is None and bool(self.candidates)
                and bool(self.destination_edit.text().strip())
            )

    def _receive_result(self, result):
        self._pending_result = result

    def _receive_error(self, message):
        self._pending_error = message

    def _worker_finished(self):
        worker = self._worker
        if worker is None:
            return
        phase = worker.phase
        cancelled = worker.cancel.is_set()
        self._service = worker.service
        self._worker = None
        worker.deleteLater()
        result, error = self._pending_result, self._pending_error
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if error or cancelled else 100)
        if error:
            self.status_label.setText(f"Face {phase} could not finish: {error}")
            self.error_details.setPlainText(error)
            self.error_details.show()
            if phase == "preview":
                self._clear_preview("Preview unavailable.")
            elif phase == "load":
                self.count_label.setText("Faces unavailable")
        elif phase == "export" and result is not None:
            self._export_finished(result)
        elif cancelled:
            self.status_label.setText("Cancelled. Your originals are unchanged.")
        elif phase == "load" and result is not None:
            self._candidates_loaded(result)
        elif phase == "preview" and result is not None:
            self._preview_ready(result)
        if self._close_when_ready:
            super().reject()

    def _candidates_loaded(self, candidates):
        self.candidates = list(candidates)
        self.table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
            values = (
                str(candidate.get("relative_path") or Path(candidate["source"]).name),
                str(candidate["face_index"]), f"{float(candidate['score']):.0%}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(str(candidate["source"]))
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        count = len(self.candidates)
        self.count_label.setText(f"{count:,} face{'s' if count != 1 else ''} available")
        self.list_hint.setText(f"Export includes all {count:,} face crops. Select a row to preview.")
        self.status_label.setText(f"{count:,} faces meet this analysis's {self.report.options.threshold:.0%} confidence threshold." if count else "No eligible faces were found in this analysis.")
        self._set_busy(False)
        if count and not self._close_when_ready:
            self.table.selectRow(0)
            QTimer.singleShot(0, self._preview_selected)
        elif not count:
            self._clear_preview("No faces to preview.")

    def _selection_changed(self, *_):
        self._clear_preview("Press Preview to see this face crop.")
        self.preview_caption.setText("Padding applies to every exported face.")
        self.preview_button.setEnabled(self._worker is None and self.table.currentRow() >= 0)

    def _clear_preview(self, text):
        self._preview_pixmap = QPixmap()
        self.preview_image.clear()
        self.preview_image.setText(text)

    def _preview_selected(self):
        if self._worker is not None or self._close_when_ready:
            return
        row = self.table.currentRow()
        if 0 <= row < len(self.candidates):
            self._clear_preview("Preparing face preview…")
            self._start("preview", candidate=self.candidates[row], padding=self.padding_spin.value() / 100)

    def _preview_ready(self, result):
        pixmap = QPixmap()
        if not pixmap.loadFromData(result["png"], "PNG"):
            self._clear_preview("Preview could not be displayed.")
            self.status_label.setText("The face preview returned an unreadable image.")
            return
        self._preview_pixmap = pixmap
        self._scale_preview()
        self.preview_caption.setText(f"{result['width']:,} × {result['height']:,} px · {self.padding_spin.value():.0f}% padding")
        self._show_info(result.get("info", {}))
        self.status_label.setText(f"Face preview ready. Export saves all {len(self.candidates):,} face crops.")

    def _scale_preview(self):
        if not self._preview_pixmap.isNull():
            self.preview_image.setPixmap(self._preview_pixmap.scaled(
                self.preview_image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "preview_image"):
            self._scale_preview()

    def _show_info(self, info):
        self.device_label.setToolTip(json.dumps(info, indent=2, default=str))
        device = str(info.get("device", "")).lower()
        if info.get("cuda_verified") is True or device.startswith("cuda"):
            self.device_label.setText("●  GPU active")
        elif device.startswith("cpu"):
            self.device_label.setText("CPU fallback")

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Choose face crop output folder", self.destination_edit.text().strip().strip('"'))
        if path:
            self.destination_edit.setText(path)

    def _export(self):
        if self._worker is not None or not self.candidates:
            return
        text = self.destination_edit.text().strip().strip('"')
        if not text:
            self.status_label.setText("Choose a folder for the exported face crops.")
            return
        destination = Path(text)
        self._start("export", candidates=self.candidates, destination=destination,
                    padding=self.padding_spin.value() / 100)

    def _on_event(self, event):
        if self._worker is not None and self._worker.cancel.is_set():
            return
        if event.get("type") == "status":
            self.status_label.setText(str(event.get("message", "")))
        elif event.get("type") == "progress":
            completed, total = int(event.get("completed", 0)), int(event.get("total", 0))
            if total:
                self.progress.setRange(0, total)
                self.progress.setValue(min(completed, total))
                self.status_label.setText(f"Exporting face crops · {completed:,} / {total:,}")

    def _export_finished(self, result):
        self.last_export = result
        count = int(result.get("exported", 0))
        errors = result.get("errors", [])
        cancelled = bool(result.get("cancelled"))
        self._show_info(result.get("info", {}))
        state = "Export cancelled" if cancelled else "Export complete"
        self.status_label.setText(f"{state} · {count:,} face crops saved · {len(errors):,} issues.")
        if errors:
            self.error_details.setPlainText("\n".join(
                str(error) if not isinstance(error, dict) else ": ".join(str(error.get(key, "")) for key in ("source", "error") if error.get(key)) or json.dumps(error)
                for error in errors
            ))
            self.error_details.show()
        manifest = result.get("manifest_path")
        if manifest:
            self._export_destination = Path(manifest).parent
        elif count:
            self._export_destination = Path(self.destination_edit.text().strip().strip('"'))
        self._set_busy(False)

    def _open_output(self):
        if self._export_destination is not None and self._export_destination.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._export_destination)))
        else:
            QMessageBox.information(self, "Face crop output", "The face crop output folder is no longer available.")

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling safely after the current face…")

    def reject(self):
        if self._worker is not None:
            self._close_when_ready = True
            self._cancel()
            self.close_button.setEnabled(False)
            self.status_label.setText("Finishing the current face safely, then closing…")
            return
        self._close_when_ready = True
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self.reject()
            event.ignore()
        else:
            self._close_when_ready = True
            event.accept()
