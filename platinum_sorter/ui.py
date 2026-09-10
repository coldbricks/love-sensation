"""Desktop interface for the local image organizer.

Model construction and all filesystem processing run on the worker thread.
The interface only displays filenames and detection metadata, never images.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QPointF, QRectF, QSortFilterProxyModel, Qt, QThread,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QLayout, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from .contracts import ImageResult, ScanReport, SortOptions
from . import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"

STYLE = """
QWidget { background: #101014; color: #f3eee4; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog { background: #101014; }
QFrame#sidebar { background: #141418; border-right: 1px solid #49464a; }
QFrame#card { background: #202025; border: 1px solid #49464a; border-radius: 10px; }
QFrame#metric { background: #19191e; border: 1px solid #3e3b40; border-radius: 8px; }
QFrame#card QLabel, QFrame#card QCheckBox, QFrame#card QWidget#transparent,
QFrame#metric QLabel, QFrame#sidebar QLabel { background: transparent; }
QLabel#muted { color: #aaa6aa; }
QLabel#section { font-size: 15px; font-weight: 600; }
QLabel#title { font-family: 'Century Gothic'; font-size: 29px; font-weight: 700; letter-spacing: -0.8px; }
QLabel#metricValue { color: #f3eee4; font-family: 'Century Gothic'; font-size: 29px; font-weight: 700; }
QLabel#eyebrow { color: #c5b18f; font-size: 10px; font-weight: 650; letter-spacing: 1.3px; }
QLabel#device { color: #d8b477; background: #262329; border: 1px solid #655b4e;
    border-radius: 14px; padding: 6px 12px; font-size: 11px; }
QLabel#brandMark, QLabel#mastheadArt { background: transparent; }
QLabel#activeNav { background: #343036; border: 1px solid #6a5a49; border-radius: 6px; color: #f3dfbb; padding: 12px; font-weight: 600; }
QLineEdit, QComboBox, QDoubleSpinBox { background: #15151a; border: 1px solid #57515a;
    border-radius: 6px; min-height: 24px; padding: 6px 9px; selection-background-color: #564656; }
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 1px solid #d8b477; }
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled { color: #77717c; border-color: #39343e; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #262329; selection-background-color: #564656; }
QPushButton { background: #302c33; border: 1px solid #655c66; border-radius: 6px;
    padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #403741; border-color: #b09b88; }
QPushButton:pressed { background: #242128; }
QPushButton:disabled { color: #77717c; background: #232027; border-color: #39343e; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #e9e6eb,stop:1 #b8b5c0); border-color: #eeebf0; color: #17151b; }
QPushButton#primary:hover { background: #f3eff5; }
QPushButton#primary:disabled { background: #39343e; border-color: #514a56; color: #88818e; }
QPushButton#apply { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ebcd99,stop:1 #c49a5e); border-color: #e8c58a; color: #211913; }
QPushButton#apply:hover { background: #f2d6a3; }
QPushButton#apply:disabled { background: #393027; border-color: #544737; color: #9b8568; }
QToolButton { border: none; background: transparent; color: #c5b9c8; padding: 4px 0px; }
QToolButton:hover { color: #f3dfbb; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #a7947b; border-radius: 3px; background: #15151a; }
QCheckBox::indicator:checked { background: #cda96e; border-color: #f0d6a3; }
QTableView { background: #1b1b20; alternate-background-color: #202025; border: none;
    selection-background-color: #4c3b47; selection-color: #fff5e8; gridline-color: #373139; }
QTableView::item { padding: 9px 10px; border-bottom: 1px solid #312d34; }
QHeaderView::section { background: #262329; color: #c5b18f; border: none;
    border-bottom: 1px solid #655444; padding: 10px; font-size: 11px; font-weight: 600; }
QProgressBar { background: #242128; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { border-radius: 3px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #d3d2d7,stop:0.55 #d8b477,stop:1 #b96b8b); }
QScrollBar:vertical { background: #1b1b20; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #635664; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollArea { border: none; background: transparent; }
QToolTip { background: #302932; color: #f3eee4; border: 1px solid #a78b69; padding: 7px; }
"""


def _label(text: str, name: str | None = None) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def _has_error(result: ImageResult) -> bool:
    return bool(result.error) or result.status in {"error", "failed", "partial", "partial_error"}


def _actionable(result: ImageResult) -> bool:
    return bool(result.categories and getattr(result, "sha256", "")) and result.status in {"ready", "error", "cancelled", "copying", "removing_source"}


def _matched(result: ImageResult) -> bool:
    return any(category != "_Unmatched" for category in result.categories)


def _has_face_detection(report: ScanReport | None) -> bool:
    if report is None:
        return False
    for result in report.results:
        if not result.sha256:
            continue
        for detection in result.detections:
            if detection.get("class") not in {"FACE_FEMALE", "FACE_MALE"}:
                continue
            try:
                box = [float(value) for value in detection.get("box", [])]
                score = float(detection.get("score", 0))
                if (report.options.threshold <= score <= 1
                        and len(box) == 4 and all(math.isfinite(value) for value in box)
                        and box[2] > 0 and box[3] > 0):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _readable(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _moon_spoon_pixmap(width: int = 176, height: int = 72, *, compact: bool = False) -> QPixmap:
    """Original chrome moon profile and spoon, drawn as crisp vector paths."""
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(width / (72 if compact else 176), height / 72)
    # A quiet reflection behind the moon, discovered on a second look.
    painter.save()
    painter.setOpacity(0.25)
    if compact:
        painter.translate(-2, 7)
        painter.scale(0.48, 0.82)
    spoon = QPainterPath()
    spoon.moveTo(48, 51)
    spoon.cubicTo(86, 56, 114, 60, 154, 65)
    spoon.quadTo(164, 68, 158, 71)
    spoon.cubicTo(119, 65, 84, 60, 48, 55)
    spoon.closeSubpath()
    metal = QLinearGradient(0, 45, 0, 71)
    for stop, color in ((0, "#a9a6af"), (0.28, "#d3d2d7"), (0.55, "#77717f"), (0.72, "#d6d2db"), (1, "#66616f")):
        metal.setColorAt(stop, QColor(color))
    painter.setPen(QPen(QColor("#cbc5cf"), 0.6))
    painter.setBrush(metal)
    painter.drawPath(spoon)
    painter.drawEllipse(QRectF(26, 45, 29, 13))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#d6d0da"), 0.8))
    painter.drawArc(QRectF(29, 47, 24, 8), 15 * 16, 145 * 16)
    painter.restore()
    moon = QPainterPath()
    moon.moveTo(44, 7)
    moon.cubicTo(17, -2, -2, 24, 8, 46)
    moon.cubicTo(17, 66, 43, 73, 57, 53)
    moon.cubicTo(48, 60, 36, 56, 36, 50)
    moon.cubicTo(36, 46, 41, 46, 45, 43)
    moon.cubicTo(41, 42, 39, 39, 40, 36)
    moon.lineTo(52, 32)
    moon.cubicTo(44, 27, 39, 26, 39, 20)
    moon.cubicTo(39, 14, 41, 10, 44, 7)
    moon.closeSubpath()
    chrome = QLinearGradient(6, 5, 49, 65)
    for stop, color in ((0, "#f4f1ef"), (0.32, "#aaa8b3"), (0.47, "#ece7e4"), (0.62, "#8d8999"), (1, "#d8b477")):
        chrome.setColorAt(stop, QColor(color))
    painter.setPen(QPen(QColor("#e3d5bd"), 0.65))
    painter.setBrush(chrome)
    painter.drawPath(moon)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    eye = QPainterPath()
    eye.moveTo(22, 28)
    eye.quadTo(29, 24, 35, 28)
    painter.setPen(QPen(QColor("#34313d"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawPath(eye)
    painter.drawLine(QPointF(32, 27), QPointF(30, 32))
    rim = QPainterPath()
    rim.moveTo(19, 12)
    rim.cubicTo(4, 27, 8, 47, 23, 58)
    painter.setPen(QPen(QColor("#f7eee0"), 0.8))
    painter.drawPath(rim)
    painter.end()
    return pixmap


def _brand_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#72614d"), max(size / 100, 0.6)))
        painter.setBrush(QColor("#141418"))
        painter.drawRoundedRect(QRectF(0.8, 0.8, size - 1.6, size - 1.6), size * 0.20, size * 0.20)
        margin = max(2, round(size * 0.05))
        painter.drawPixmap(margin, margin, _moon_spoon_pixmap(size - margin * 2, size - margin * 2, compact=True))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class ResultsModel(QAbstractTableModel):
    HEADERS = ("FILE", "CATEGORIES", "CONFIDENCE", "STATUS")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results: list[ImageResult] = []
        self._by_source: dict[str, int] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.results)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        result = self.results[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return result
        if role == Qt.ItemDataRole.UserRole + 1:
            if index.column() == 2:
                return max((float(item.get("score", 0)) for item in result.detections), default=-1)
            return str(self.data(index, Qt.ItemDataRole.DisplayRole)).casefold()
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [result.source]
            if result.error:
                lines.append(result.error)
            lines.extend(result.destinations)
            return "\n".join(lines)
        if role == Qt.ItemDataRole.ForegroundRole:
            if _has_error(result):
                return QColor("#e69b9c")
            if index.column() == 3:
                return QColor("#e0c58f" if result.destinations else "#c1b8c5")
        if role == Qt.ItemDataRole.DisplayRole:
            scores = [float(item.get("score", 0)) for item in result.detections]
            values = (
                result.relative_path or Path(result.source).name,
                ", ".join(_readable(category) for category in result.categories) or ("—" if _has_error(result) else "Unmatched"),
                f"{max(scores):.0%}" if scores else "—",
                _readable(result.status) + (" · cached" if result.cached else ""),
            )
            return values[index.column()]
        return None

    def replace(self, results: list[ImageResult]):
        self.beginResetModel()
        self.results = list(results)
        self._by_source = {result.source: index for index, result in enumerate(self.results)}
        self.endResetModel()

    def upsert(self, result: ImageResult):
        existing = self._by_source.get(result.source)
        if existing is not None:
            self.results[existing] = result
            self.dataChanged.emit(self.index(existing, 0), self.index(existing, 3))
            return
        row = len(self.results)
        self.beginInsertRows(QModelIndex(), row, row)
        self._by_source[result.source] = row
        self.results.append(result)
        self.endInsertRows()


class ResultsFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.kind = "all"
        self.setDynamicSortFilter(True)
        self.setSortRole(Qt.ItemDataRole.UserRole + 1)

    def filterAcceptsRow(self, row, parent):
        result = self.sourceModel().results[row]
        if self.kind == "issues" and not _has_error(result):
            return False
        if self.kind == "matched" and not _matched(result):
            return False
        if self.kind == "unmatched" and (_matched(result) or _has_error(result)):
            return False
        haystack = " ".join((result.relative_path, result.source, " ".join(result.categories), result.status, result.error)).casefold()
        return self.query in haystack

    def update_filter(self, query: str, kind: str):
        self.query = query.strip().casefold()
        self.kind = kind
        self.invalidateFilter()


class TaskWorker(QThread):
    report_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, phase: str, options: SortOptions, report: ScanReport | None, emit, engine=None, parent=None, report_path: Path | None = None):
        super().__init__(parent)
        self.phase = phase
        self.options = options
        self.report = report
        self.emit_event = emit
        self.engine = engine
        self.report_path = report_path
        self.cancel = threading.Event()

    def run(self):
        try:
            if self.phase == "load":
                from .engine import load_report
                self.report_ready.emit(load_report(self.report_path))
                return
            if self.engine is None:
                from .detector import GpuDetector
                from .engine import SorterEngine
                self.engine = SorterEngine(
                    data_dir=DATA_DIR,
                    detector_factory=lambda: GpuDetector(PROJECT_ROOT / "models", emit=self.emit_event),
                )
            if self.phase == "analyze":
                report = self.engine.analyze(self.options, self.emit_event, self.cancel)
            else:
                report = self.engine.execute(self.report, self.emit_event, self.cancel)
            self.report_ready.emit(report)
        except Exception as exc:
            logging.exception("%s worker failed", self.phase)
            self.failed.emit(str(exc) or type(exc).__name__)


class MainWindow(QMainWindow):
    engine_event = Signal(dict)

    def __init__(self):
        super().__init__()
        # The Windows offscreen Qt plugin omits the system font database.
        # Register installed fonts only for that case, including screenshot QA.
        if not QFontDatabase.families():
            font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for filename in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf", "GOTHIC.TTF", "GOTHICB.TTF"):
                font_path = font_dir / filename
                if font_path.is_file():
                    QFontDatabase.addApplicationFont(str(font_path))
        self._brand = _brand_icon()
        self.setWindowIcon(self._brand)
        self.setWindowTitle(f"Love Sensation {__version__} — Private image organizer")
        self.resize(1300, 840)
        self.setMinimumSize(1080, 720)
        self.setStyleSheet(STYLE)
        self.report: ScanReport | None = None
        self._worker: TaskWorker | None = None
        self._engine = None
        self._busy = False
        self._close_requested = False
        self._phase = ""
        self._task_failed = False
        self._task_started_at = 0.0
        self._plan_valid = False
        self._face_crops_valid = False
        self._face_crop_dialog = None
        self._categories: dict[str, QCheckBox] = {}
        self._settings_timer = QTimer(self)
        self._settings_timer.setSingleShot(True)
        self._settings_timer.timeout.connect(self._save_settings)
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(150)
        self._metrics_timer.timeout.connect(self._update_metrics)
        self._build_ui()
        from .startup_audio import StartupAudioController
        self._startup_audio = StartupAudioController(parent=self, data_dir=DATA_DIR)
        self.engine_event.connect(self._on_event)
        self._load_settings()
        self._connect_options()
        self._refresh_actions()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 28, 20, 23)
        mark = _label("", "brandMark")
        mark.setPixmap(self._brand.pixmap(48, 48))
        mark.setFixedSize(48, 48)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(mark)
        side.addSpacing(12)
        brand = _label("LOVE\nSENSATION")
        brand.setStyleSheet("font-family: 'Century Gothic'; font-size: 24px; font-weight: 700; letter-spacing: 0.5px; color: #e8e2dd; background: transparent;")
        side.addWidget(brand)
        side.addWidget(_label("PRIVATE IMAGE ORGANIZER", "eyebrow"))
        side.addSpacing(34)
        side.addWidget(_label("WORKSPACE", "eyebrow"))
        side.addSpacing(8)
        active_nav = _label("Image organizer", "activeNav")
        active_nav.setStyleSheet("background: #343036; border: 1px solid #6a5a49; border-radius: 6px; color: #f3dfbb; padding: 12px; font-weight: 600;")
        side.addWidget(active_nav)
        side.addSpacing(28)
        side.addWidget(_label("YOUR WORKFLOW", "eyebrow"))
        side.addSpacing(12)
        self.step_labels = []
        for text in ("01   Analyze your folder", "02   Review the results", "03   Apply sorting"):
            step = _label(text, "muted")
            step.setContentsMargins(0, 7, 0, 7)
            self.step_labels.append(step)
            side.addWidget(step)
        side.addSpacing(24)
        side.addWidget(_label("ATMOSPHERE", "eyebrow"))
        side.addSpacing(6)
        self.startup_audio_button = QPushButton("Startup music…")
        self.startup_audio_button.setToolTip("Choose an optional opening bar from a local music file.")
        self.startup_audio_button.clicked.connect(self._show_startup_music)
        side.addWidget(self.startup_audio_button)
        side.addStretch()
        privacy = _label("LOCAL & PRIVATE", "eyebrow")
        side.addWidget(privacy)
        note = _label("Your files stay on this device.\nReview every run before sorting.", "muted")
        note.setWordWrap(True)
        note.setStyleSheet("line-height: 1.4; color: #aaa6aa; font-size: 12px; background: transparent;")
        side.addWidget(note)
        shell.addWidget(sidebar)

        body = QWidget()
        content = QVBoxLayout(body)
        content.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        content.setContentsMargins(26, 23, 26, 12)
        content.setSpacing(12)
        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(_label("Image organizer", "title"))
        titles.addWidget(_label("Find what belongs together. Keep every original accounted for.", "muted"))
        heading.addLayout(titles)
        heading.addStretch()
        self.masthead_art = _label("", "mastheadArt")
        self.masthead_art.setPixmap(_moon_spoon_pixmap(150, 62))
        self.masthead_art.setFixedSize(150, 62)
        self.masthead_art.setAccessibleName("Silver moon profile and spoon")
        heading.addWidget(self.masthead_art, 0, Qt.AlignmentFlag.AlignVCenter)
        self.device_label = _label("●  GPU checked on analyze", "device")
        heading.addWidget(self.device_label, 0, Qt.AlignmentFlag.AlignTop)
        content.addLayout(heading)

        self.options_card = QFrame()
        self.options_card.setObjectName("card")
        config = QVBoxLayout(self.options_card)
        config.setContentsMargins(18, 14, 18, 12)
        config.setSpacing(10)
        config_header = QHBoxLayout()
        config_header.addWidget(_label("Folders & sorting", "section"))
        config_header.addStretch()
        config_header.addWidget(_label("1  /  SET UP", "eyebrow"))
        config.addLayout(config_header)
        paths = QGridLayout()
        paths.setHorizontalSpacing(10)
        paths.setVerticalSpacing(8)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Choose a folder of images to organize")
        self.source_edit.setAccessibleName("Source folder")
        self.source_edit.setMinimumHeight(38)
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("Choose where sorted images will go")
        self.destination_edit.setAccessibleName("Output folder")
        self.destination_edit.setMinimumHeight(38)
        for row, (name, edit) in enumerate((("Source folder", self.source_edit), ("Output folder", self.destination_edit))):
            paths.addWidget(_label(name, "muted"), row, 0)
            paths.addWidget(edit, row, 1)
            browse = QPushButton("Browse…")
            browse.setMinimumHeight(38)
            browse.clicked.connect(lambda checked=False, target=edit, title=name: self._browse(target, title))
            paths.addWidget(browse, row, 2)
        paths.setColumnStretch(1, 1)
        config.addLayout(paths)
        choices = QHBoxLayout()
        choices.setSpacing(14)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Best category", "best")
        self.mode_combo.addItem("Top 3 categories", "top3")
        self.mode_combo.addItem("All matching categories", "all")
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.setAccessibleName("Category selection mode")
        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(0.25, 0.95)
        self.confidence_spin.setSingleStep(0.01)
        self.confidence_spin.setDecimals(2)
        self.confidence_spin.setValue(0.62)
        self.confidence_spin.setAccessibleName("Minimum confidence")
        self.confidence_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.operation_combo = QComboBox()
        self.operation_combo.addItem("Copy originals", "copy")
        self.operation_combo.addItem("Move originals", "move")
        self.operation_combo.setAccessibleName("File operation")
        for title, control in (("MATCHES", self.mode_combo), ("MIN. CONFIDENCE", self.confidence_spin), ("FILE OPERATION", self.operation_combo)):
            group = QVBoxLayout()
            group.setSpacing(5)
            group.addWidget(_label(title, "eyebrow"))
            group.addWidget(control)
            choices.addLayout(group, 2 if control is self.mode_combo else 1)
        self.unmatched_check = QCheckBox("Include unmatched")
        self.unmatched_check.setChecked(True)
        self.unmatched_check.setToolTip("Place images with no selected matches in an Unmatched folder.")
        choices.addWidget(self.unmatched_check, 0, Qt.AlignmentFlag.AlignBottom)
        config.addLayout(choices)
        self.category_toggle = QToolButton()
        self.category_toggle.setText("Choose categories  ·  all included")
        self.category_toggle.setCheckable(True)
        self.category_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.category_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.category_toggle.toggled.connect(self._toggle_categories)
        config.addWidget(self.category_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.category_area = QScrollArea()
        self.category_area.setWidgetResizable(True)
        self.category_area.setMaximumHeight(140)
        self.category_area.setMinimumHeight(100)
        category_widget = QWidget()
        category_widget.setObjectName("transparent")
        category_layout = QVBoxLayout(category_widget)
        category_layout.setContentsMargins(0, 0, 0, 0)
        category_actions = QHBoxLayout()
        category_actions.addStretch()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_categories(True))
        self.deselect_all_button = QPushButton("Deselect all")
        self.deselect_all_button.clicked.connect(lambda: self._set_all_categories(False))
        for button in (self.select_all_button, self.deselect_all_button):
            button.setStyleSheet("padding: 5px 10px; font-size: 11px;")
            category_actions.addWidget(button)
        category_layout.addLayout(category_actions)
        category_grid = QGridLayout()
        category_grid.setContentsMargins(0, 2, 0, 5)
        category_layout.addLayout(category_grid)
        try:
            from .detector import LABELS
            labels = list(LABELS.values()) if isinstance(LABELS, dict) else list(LABELS)
        except ImportError:
            labels = []
        for i, label in enumerate(labels):
            checkbox = QCheckBox(_readable(str(label)))
            checkbox.setChecked(True)
            self._categories[str(label)] = checkbox
            category_grid.addWidget(checkbox, i // 3, i % 3)
        if not labels:
            category_grid.addWidget(_label("Category filters will be available after the detector is installed.", "muted"), 0, 0)
        self.category_area.setWidget(category_widget)
        self.category_area.hide()
        config.addWidget(self.category_area)
        content.addWidget(self.options_card)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_values = {}
        for key, title, detail in (("files", "FILES ANALYZED", "Images in this run"), ("matched", "MATCHED", "At least one category"), ("ready", "READY TO SORT", "Review before applying"), ("issues", "ISSUES", "Need your attention")):
            card = QFrame()
            card.setObjectName("metric")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 11, 15, 11)
            card_layout.setSpacing(2)
            title_label = _label(title, "eyebrow")
            card_layout.addWidget(title_label)
            value = _label("0", "metricValue")
            if key == "issues":
                value.setStyleSheet("color: #aaa6aa; font-size: 29px; font-weight: 600;")
            card_layout.addWidget(value)
            detail_label = _label(detail, "muted")
            detail_label.setStyleSheet("font-size: 11px; color: #aaa6aa; background: transparent;")
            card_layout.addWidget(detail_label)
            self.metric_values[key] = (value, title_label, detail_label)
            metrics.addWidget(card, 1)
        content.addLayout(metrics)

        results_card = QFrame()
        results_card.setObjectName("card")
        results_layout = QVBoxLayout(results_card)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)
        tools_row = QHBoxLayout()
        tools_row.setSpacing(10)
        tools_row.setContentsMargins(16, 11, 16, 11)
        tools_row.addWidget(_label("Results", "section"))
        self.results_count = _label("0 files", "muted")
        tools_row.addWidget(self.results_count)
        tools_row.addStretch()
        self.face_crops_button = QPushButton("Face crops…")
        self.face_crops_button.setToolTip("Preview and export detected faces from a completed analysis.")
        self.face_crops_button.clicked.connect(self._open_face_crops)
        tools_row.addWidget(self.face_crops_button)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search files or categories…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(270)
        self.search_edit.setAccessibleName("Search results")
        self.filter_combo = QComboBox()
        for label, value in (("All files", "all"), ("Matched", "matched"), ("Unmatched", "unmatched"), ("Issues", "issues")):
            self.filter_combo.addItem(label, value)
        self.filter_combo.setMinimumWidth(120)
        tools_row.addWidget(self.search_edit)
        tools_row.addWidget(self.filter_combo)
        results_layout.addLayout(tools_row)
        self.model = ResultsModel(self)
        self.proxy = ResultsFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(41)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 140)
        self.table.setMinimumHeight(120)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().selectionChanged.connect(self._show_selection)
        results_layout.addWidget(self.table, 1)
        self.empty_hint = _label("Choose your folders, then analyze to preview the sorting plan.", "muted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setContentsMargins(12, 8, 12, 8)
        results_layout.addWidget(self.empty_hint)
        self.selection_detail = _label("Filenames and detection details only · no image previews", "muted")
        self.selection_detail.setContentsMargins(16, 9, 16, 10)
        self.selection_detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.selection_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_detail.setMaximumHeight(55)
        results_layout.addWidget(self.selection_detail)
        content.addWidget(results_card, 1)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(26, 0, 26, 20)
        footer_layout.setSpacing(12)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        footer_layout.addWidget(self.progress)
        status_row = QHBoxLayout()
        self.status_label = _label("Ready when you are", "muted")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_label = _label("", "muted")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.progress_label)
        footer_layout.addLayout(status_row)
        actions = QHBoxLayout()
        self.open_button = QPushButton("Open output")
        self.open_button.clicked.connect(self._open_output)
        self.export_button = QPushButton("Export report")
        self.export_button.clicked.connect(self._export_report)
        self.open_run_button = QPushButton("Open run")
        self.open_run_button.clicked.connect(self._open_run)
        self.open_run_button.setToolTip("Open a saved run to review it or resume incomplete sorting.")
        actions.addWidget(self.open_run_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.export_button)
        actions.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        self.analyze_button = QPushButton("Analyze images")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self._analyze)
        self.apply_button = QPushButton("Apply sorting")
        self.apply_button.setObjectName("apply")
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.analyze_button)
        actions.addWidget(self.apply_button)
        footer_layout.addLayout(actions)
        body_scroll = QScrollArea()
        body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_scroll.setWidgetResizable(True)
        body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body_scroll.setWidget(body)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(body_scroll, 1)
        right_layout.addWidget(footer)
        shell.addWidget(right, 1)
        self.search_edit.textChanged.connect(self._filter_results)
        self.filter_combo.currentIndexChanged.connect(self._filter_results)

    def _connect_options(self):
        self.source_edit.textChanged.connect(self._options_changed)
        self.destination_edit.textChanged.connect(self._options_changed)
        self.mode_combo.currentIndexChanged.connect(self._options_changed)
        self.operation_combo.currentIndexChanged.connect(self._options_changed)
        self.confidence_spin.valueChanged.connect(self._options_changed)
        self.unmatched_check.toggled.connect(self._options_changed)
        for checkbox in self._categories.values():
            checkbox.toggled.connect(self._options_changed)

    def _options(self) -> SortOptions:
        selected = [name for name, checkbox in self._categories.items() if checkbox.isChecked()]
        return SortOptions(
            source=self.source_edit.text().strip().strip('"'),
            destination=self.destination_edit.text().strip().strip('"'),
            threshold=self.confidence_spin.value(),
            mode=self.mode_combo.currentData(),
            operation=self.operation_combo.currentData(),
            include_unmatched=self.unmatched_check.isChecked(),
            selected_classes=[] if len(selected) == len(self._categories) else selected,
        )

    def _browse(self, edit: QLineEdit, title: str):
        directory = QFileDialog.getExistingDirectory(self, title, edit.text().strip('"') or str(Path.home()))
        if directory:
            edit.setText(directory)

    def _toggle_categories(self, checked: bool):
        self.category_area.setVisible(checked)
        self.category_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _set_all_categories(self, checked: bool):
        for checkbox in self._categories.values():
            blocked = checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(blocked)
        self._options_changed()

    def _options_changed(self, *_):
        self._plan_valid = False
        self._face_crops_valid = False
        selected = sum(checkbox.isChecked() for checkbox in self._categories.values())
        suffix = "all included" if selected == len(self._categories) else f"{selected} selected"
        self.category_toggle.setText(f"Choose categories  ·  {suffix}")
        if self.report is not None:
            self.status_label.setText("Settings changed. Analyze again to build an updated sorting plan.")
        self._settings_timer.start(450)
        self._refresh_actions()

    def _refresh_actions(self):
        self.options_card.setEnabled(not self._busy)
        paths_set = bool(self.source_edit.text().strip() and self.destination_edit.text().strip())
        categories_selected = not self._categories or any(checkbox.isChecked() for checkbox in self._categories.values())
        self.analyze_button.setEnabled(not self._busy and paths_set and categories_selected)
        self.apply_button.setEnabled(not self._busy and self._plan_valid and self.report is not None and any(_actionable(r) for r in self.report.results))
        self.apply_button.setText("Resume sorting" if self._phase == "execute" and self._plan_valid else "Apply sorting")
        self.cancel_button.setEnabled(self._busy and self._worker is not None and not self._worker.cancel.is_set())
        self.export_button.setEnabled(not self._busy and self.report is not None)
        self.open_run_button.setEnabled(not self._busy)
        self.open_button.setEnabled(not self._busy and bool(self.destination_edit.text().strip()))
        self.face_crops_button.setEnabled(not self._busy and self._face_crops_valid and _has_face_detection(self.report))

    def _analyze(self):
        options = self._options()
        if not Path(options.source).is_dir():
            QMessageBox.warning(self, "Choose a source folder", "The source folder does not exist. Choose an existing folder to analyze.")
            return
        if self._categories and not any(checkbox.isChecked() for checkbox in self._categories.values()):
            QMessageBox.warning(self, "Select a category", "Select at least one category, or select every category to include all matches.")
            return
        self.report = None
        self._plan_valid = False
        self._face_crops_valid = False
        self.model.replace([])
        self.selection_detail.setText("Analyzing filenames and detection metadata…")
        self._start_task("analyze", options)

    def _apply(self):
        if self.report is None or not self._plan_valid:
            return
        self._plan_valid = False
        self._start_task("execute", self.report.options)

    def _start_task(self, phase: str, options: SortOptions, report_path: Path | None = None):
        self._save_settings()
        self._phase = phase
        self._task_failed = False
        self._task_started_at = time.perf_counter() if phase == "analyze" else 0.0
        self._busy = True
        self.status_label.setText("Opening saved run…" if phase == "load" else "Preparing analysis…" if phase == "analyze" else "Applying your sorting plan…")
        self.progress.setRange(0, 0)
        self.progress_label.setText("")
        self._worker = TaskWorker(phase, options, self.report, self.engine_event.emit, self._engine, self, report_path=report_path)
        self._worker.report_ready.connect(self._on_report)
        self._worker.failed.connect(self._on_failure)
        self._worker.finished.connect(self._on_finished)
        self._metrics_timer.start()
        self._refresh_actions()
        self._worker.start()

    def _on_event(self, event: dict):
        kind = event.get("type")
        if kind == "status":
            self.status_label.setText(str(event.get("message", "")))
        elif kind == "device":
            self._show_device(event.get("info", {}))
        elif kind == "progress":
            completed = int(event.get("completed", 0))
            total = int(event.get("total", 0))
            if total > 0:
                self.progress.setRange(0, total)
                self.progress.setValue(min(completed, total))
                self.progress_label.setText(f"{completed:,} / {total:,}")
                if self._phase == "analyze" and self._task_started_at and completed > 0:
                    elapsed = max(time.perf_counter() - self._task_started_at, 0.001)
                    rate = completed / elapsed
                    remaining = max(0, round((total - completed) / rate))
                    hours, remainder = divmod(remaining, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    estimate = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
                    self.progress_label.setText(f"{completed:,} / {total:,} · {rate:.2f} images/s · ~{estimate} left")
                    self.progress_label.setToolTip("Average since analysis started, including file reads and model preparation. Time remaining is an estimate.")
            else:
                self.progress.setRange(0, 0)
        elif kind == "result":
            result = event.get("result")
            if isinstance(result, dict):
                result = ImageResult(**result)
            if isinstance(result, ImageResult):
                self.model.upsert(result)

    def _show_device(self, info: dict):
        if not info:
            return
        name = info.get("device_name") or info.get("name") or info.get("device") or "Inference device"
        provider = info.get("provider") or info.get("execution_provider") or ""
        providers = info.get("providers", [])
        is_gpu = info.get("cuda_verified") is True or str(info.get("device", "")).lower().startswith("cuda")
        label = "GPU active" if is_gpu else "Device verified"
        if "cpu" in str((name, provider)).lower() and not is_gpu:
            label = "CPU fallback"
        self.device_label.setText(f"●  {label}")
        self.device_label.setToolTip(json.dumps(info, indent=2, default=str))

    def _on_report(self, report: ScanReport):
        loaded = self._phase == "load"
        if loaded:
            self._restore_options(report.options)
            self._phase = "execute" if getattr(report, "phase", "analyze") == "execute" else "analyze"
        self.report = report
        self._face_crops_valid = bool(getattr(report, "analysis_complete", False))
        self.model.replace(report.results)
        self._show_device(report.device)
        self._plan_valid = (self._phase == "analyze" and not report.cancelled and getattr(report, "analysis_complete", False)) or (self._phase == "execute" and any(_actionable(result) for result in report.results))
        issues = sum(_has_error(result) for result in report.results)
        total = len(report.results)
        if report.cancelled:
            next_action = "Resume sorting to finish the remaining files." if self._phase == "execute" and self._plan_valid else "Analyze again before applying."
            self.status_label.setText(f"Cancelled · {total:,} files recorded · {issues:,} issues. {next_action}")
            self.progress_label.setText("Cancelled")
        elif self._phase == "analyze":
            self.status_label.setText(f"Analysis complete · {total:,} files · {issues:,} issues. Review the results, then apply sorting.")
            self.progress_label.setText(f"{report.elapsed_seconds:.1f}s")
        else:
            self.status_label.setText(f"Sorting finished with {issues:,} issues. Review the Issues filter; resume after resolving them." if issues else f"Sorting complete · {total:,} files reviewed.")
            self.progress_label.setText(f"{report.elapsed_seconds:.1f}s")
        if loaded:
            if self._plan_valid and any(_actionable(result) for result in report.results):
                next_action = "Resume sorting to finish remaining files." if self._phase == "execute" else "Review the results, then apply sorting."
            elif report.cancelled or (self._phase == "analyze" and not getattr(report, "analysis_complete", False)):
                next_action = "Analysis was incomplete. Analyze again before sorting."
            else:
                next_action = "There are no remaining files ready to sort."
            self.status_label.setText(f"Opened saved run · {total:,} files · {issues:,} issues. {next_action}")
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if report.cancelled else 100)
        self.selection_detail.setText("Select a row for its source path, destinations, or error details.")
        self._update_metrics()

    def _on_failure(self, message: str):
        self._task_failed = True
        self._plan_valid = self._phase == "execute" and self.report is not None and any(_actionable(result) for result in self.report.results)
        self.status_label.setText(f"Run stopped: {message}")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_label.setText("Error")
        if not self._close_requested:
            QMessageBox.critical(self, "The run could not finish", message)

    def _on_finished(self):
        worker = self._worker
        if worker is not None:
            self._engine = worker.engine
            worker.deleteLater()
        self._worker = None
        self._busy = False
        self._metrics_timer.stop()
        self._update_metrics()
        self._refresh_actions()
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel.set()
            self._plan_valid = False
            self.status_label.setText("Cancelling safely after the current operation…")
            self._refresh_actions()

    def _update_metrics(self):
        results = self.model.results
        issues = sum(_has_error(result) for result in results)
        matched = sum(_matched(result) for result in results)
        ready = sum(result.status == "ready" and not _has_error(result) for result in results)
        applied = sum(result.status in {"copied", "moved"} and not _has_error(result) for result in results)
        showing_applied = self._phase == "execute"
        values = {"files": len(results), "matched": matched, "ready": applied if showing_applied else ready, "issues": issues}
        for key, value in values.items():
            self.metric_values[key][0].setText(f"{value:,}")
        self.metric_values["ready"][1].setText("SORTED" if showing_applied else "READY TO SORT")
        self.metric_values["ready"][2].setText("Files with saved destinations" if showing_applied else "Review before applying")
        self.metric_values["issues"][0].setStyleSheet(f"color: {'#e69b9c' if issues else '#aaa6aa'}; font-size: 29px; font-weight: 600;")
        visible = self.proxy.rowCount()
        self.results_count.setText(f"{visible:,} of {len(results):,} files" if visible != len(results) else f"{len(results):,} files")
        self.empty_hint.setVisible(not results)
        self.empty_hint.setText("Analysis is running. Results will appear here." if self._busy else "Choose your folders, then analyze to preview the sorting plan.")
        for index, label in enumerate(self.step_labels):
            active = (0 if self._busy and self._phase == "analyze" else 2 if self._phase == "execute" else 1 if results else 0)
            label.setStyleSheet(f"color: {'#e0c58f' if index == active else '#aaa6aa'}; background: transparent;")

    def _filter_results(self, *_):
        self.proxy.update_filter(self.search_edit.text(), self.filter_combo.currentData())
        self._update_metrics()

    def _show_selection(self, *_):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        result = self.proxy.data(rows[0], Qt.ItemDataRole.UserRole)
        text = result.source
        if result.error:
            text += f"  |  {result.error}"
        elif result.destinations:
            text += "  →  " + "; ".join(result.destinations)
        self.selection_detail.setText(text)
        self.selection_detail.setToolTip(text)

    def _open_output(self):
        destination = Path(self.destination_edit.text().strip().strip('"'))
        if not destination.is_dir():
            QMessageBox.information(self, "Output folder", "The output folder has not been created yet. It will be created when sorting is applied.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination)))

    def _show_startup_music(self):
        self._startup_audio.show_settings(parent=self)

    def _open_run(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open a saved run", str(DATA_DIR / "runs"), "Saved run (*.json)")
        if path:
            self._plan_valid = False
            self._face_crops_valid = False
            self._start_task("load", self._options(), Path(path))

    def _open_face_crops(self):
        if self.report is None or not self.face_crops_button.isEnabled():
            return
        from .crop_dialog import FaceCropDialog
        if self._face_crop_dialog is not None:
            self._face_crop_dialog.deleteLater()
        self._face_crop_dialog = FaceCropDialog(self.report, parent=self, data_dir=DATA_DIR)
        self._face_crop_dialog.exec()

    def _restore_options(self, options: SortOptions):
        controls = [self.source_edit, self.destination_edit, self.confidence_spin, self.mode_combo, self.operation_combo, self.unmatched_check, *self._categories.values()]
        previous_blocks = [control.blockSignals(True) for control in controls]
        try:
            self.source_edit.setText(options.source)
            self.destination_edit.setText(options.destination)
            self.confidence_spin.setValue(options.threshold)
            for combo, value in ((self.mode_combo, options.mode), (self.operation_combo, options.operation)):
                combo.setCurrentIndex(max(combo.findData(value), 0))
            self.unmatched_check.setChecked(options.include_unmatched)
            for name, checkbox in self._categories.items():
                checkbox.setChecked(not options.selected_classes or name in options.selected_classes)
            count = sum(checkbox.isChecked() for checkbox in self._categories.values())
            self.category_toggle.setText("Choose categories  ·  " + ("all included" if count == len(self._categories) else f"{count} selected"))
        finally:
            for control, previous in zip(controls, previous_blocks):
                control.blockSignals(previous)
        self._save_settings()

    def _export_report(self):
        if self.report is None:
            return
        path, selected_filter = QFileDialog.getSaveFileName(self, "Export run report", str(DATA_DIR / f"report-{self.report.run_id}.json"), "JSON report (*.json);;CSV report (*.csv)")
        if not path:
            return
        wanted_suffix = ".csv" if selected_filter.startswith("CSV") else ".json"
        if Path(path).suffix.lower() != wanted_suffix:
            path = str(Path(path).with_suffix(wanted_suffix))
        try:
            from .engine import export_report
            export_report(self.report, Path(path))
            self.status_label.setText(f"Report exported to {path}")
        except Exception as exc:
            QMessageBox.warning(self, "Report export failed", str(exc))

    def _load_settings(self):
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.source_edit.setText(str(settings.get("source", "")))
            self.destination_edit.setText(str(settings.get("destination", "")))
            self.confidence_spin.setValue(float(settings.get("threshold", 0.62)))
            for combo, key, default in ((self.mode_combo, "mode", "top3"), (self.operation_combo, "operation", "copy")):
                index = combo.findData(settings.get(key, default))
                combo.setCurrentIndex(max(index, 0))
            self.unmatched_check.setChecked(bool(settings.get("include_unmatched", True)))
            selected = settings.get("selected_classes", [])
            deselected = bool(settings.get("categories_deselected", False))
            for name, checkbox in self._categories.items():
                checkbox.setChecked(not deselected and (not selected or name in selected))
            count = sum(checkbox.isChecked() for checkbox in self._categories.values())
            self.category_toggle.setText("Choose categories  ·  " + ("all included" if count == len(self._categories) else f"{count} selected"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_settings(self):
        try:
            from dataclasses import asdict
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = SETTINGS_PATH.with_suffix(".json.tmp")
            settings = asdict(self._options())
            settings["categories_deselected"] = bool(self._categories) and not any(checkbox.isChecked() for checkbox in self._categories.values())
            temp_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            temp_path.replace(SETTINGS_PATH)
        except OSError as exc:
            self.status_label.setText(f"Could not save preferences: {exc}")

    def closeEvent(self, event):
        self._startup_audio.stop()
        self._save_settings()
        if self._worker is not None and self._worker.isRunning():
            self._close_requested = True
            self._cancel()
            self.status_label.setText("Finishing the current operation safely, then closing…")
            event.ignore()
            return
        event.accept()


def create_window() -> MainWindow:
    """Construct the window; caller owns the QApplication (also useful for QA)."""
    return MainWindow()


def launch_ui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Love Sensation")
    app.setOrganizationName("Love Sensation")
    app.setFont(QFont("Segoe UI", 10))
    window = create_window()
    window.show()
    QTimer.singleShot(0, window._startup_audio.play_startup)
    return app.exec()
