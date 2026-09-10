"""Capture public documentation images from an isolated, synthetic demo.

No personal media, live settings, saved runs, or processing backends are read.
The portrait illustration is drawn with Qt paths specifically for this demo.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from platinum_sorter import ui
from platinum_sorter.contracts import ImageResult, ScanReport, SortOptions
from platinum_sorter.crop_dialog import FaceCropDialog


def portrait_png() -> bytes:
    """Draw a wholly invented, geometric face using vector primitives."""
    pixmap = QPixmap(480, 480)
    pixmap.fill(QColor("#dbcaad"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, 480, 480)
    gradient.setColorAt(0, QColor("#736271"))
    gradient.setColorAt(1, QColor("#d8b477"))
    painter.fillRect(pixmap.rect(), gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#e3d0b0"))
    painter.drawEllipse(QRectF(32, 20, 420, 420))
    painter.setBrush(QColor("#25222c"))
    painter.drawRoundedRect(QRectF(112, 69, 256, 327), 115, 115)
    painter.setBrush(QColor("#e9a788"))
    painter.drawEllipse(QRectF(101, 211, 42, 77))
    painter.drawEllipse(QRectF(337, 211, 42, 77))
    painter.setBrush(QColor("#f5c4a5"))
    painter.drawRoundedRect(QRectF(127, 113, 226, 290), 105, 105)
    hair = QPainterPath()
    hair.moveTo(119, 227)
    hair.cubicTo(84, 145, 118, 63, 203, 57)
    hair.cubicTo(284, 27, 370, 98, 362, 216)
    hair.cubicTo(331, 204, 307, 168, 295, 139)
    hair.cubicTo(244, 189, 187, 182, 147, 163)
    hair.cubicTo(139, 192, 131, 213, 119, 227)
    painter.setBrush(QColor("#29252f"))
    painter.drawPath(hair)
    highlight = QPainterPath()
    highlight.moveTo(142, 118)
    highlight.cubicTo(181, 79, 248, 68, 291, 95)
    painter.setPen(QPen(QColor("#514754"), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawPath(highlight)
    painter.setPen(QPen(QColor("#39323e"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPointF(164, 223), QPointF(194, 219))
    painter.drawLine(QPointF(280, 219), QPointF(310, 223))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#24212b"))
    painter.drawEllipse(QRectF(174, 244, 13, 18))
    painter.drawEllipse(QRectF(293, 244, 13, 18))
    nose = QPainterPath()
    nose.moveTo(240, 248)
    nose.lineTo(230, 292)
    nose.quadTo(238, 299, 250, 291)
    painter.setPen(QPen(QColor("#d9957d"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.drawPath(nose)
    smile = QPainterPath()
    smile.moveTo(203, 328)
    smile.quadTo(240, 351, 277, 328)
    painter.setPen(QPen(QColor("#aa685d"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawPath(smile)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert pixmap.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def demo_report() -> ScanReport:
    options = SortOptions(
        source=r"D:\Photos\Portrait collection",
        destination=r"D:\Photos\Organized",
        threshold=0.62,
        mode="all",
    )
    names = (
        "city_walk_01.jpg", "golden_hour_02.jpg", "group_portrait_03.jpg",
        "outdoor_portrait_04.jpg", "studio_portrait_05.jpg",
        "weekend_walk_06.jpg", "window_light_07.jpg",
    )
    rows = []
    for index, name in enumerate(names):
        category = "FACE_FEMALE" if index % 2 == 0 else "FACE_MALE"
        detections = [{"class": category, "score": 0.97 - index * 0.02, "box": [60, 45, 320, 345]}]
        if index == 2:
            detections.append({"class": "FACE_MALE", "score": 0.92, "box": [440, 52, 300, 335]})
        rows.append(ImageResult(
            source=str(Path(options.source) / name), relative_path=name,
            size=2_500_000, mtime_ns=1, detections=detections,
            categories=list(dict.fromkeys(item["class"] for item in detections)),
            status="ready", sha256="synthetic-documentation-snapshot",
        ))
    return ScanReport("documentation-demo", options, rows, analysis_complete=True)


class DemoFaceService:
    def __init__(self, report, png):
        self.png = png
        self.rows = []
        for row_index, row in enumerate(report.results):
            for face_index, detection in enumerate(row.detections, start=1):
                self.rows.append(dict(
                    id=f"demo-{row_index}-{face_index}", source=row.source,
                    relative_path=row.relative_path, face_index=face_index,
                    score=detection["score"], box=detection["box"],
                ))

    def candidates(self):
        return self.rows

    def preview(self, candidate, padding=0.10):
        return dict(png=self.png, width=480, height=480, info={"device": "synthetic demo"})


def write_icon():
    """Package the independently drawn icon sizes without resizing images."""
    icon = ui._brand_icon()
    frames = []
    for size in (16, 32, 48, 64, 128, 256):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert icon.pixmap(size, size).save(buffer, "PNG")
        buffer.close()
        frames.append((size, bytes(data)))
    entries = []
    offset = 6 + len(frames) * 16
    for size, data in frames:
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (ROOT / "love-sensation.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(data for _, data in frames)
    )


def main():
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    write_icon()
    report = demo_report()
    with tempfile.TemporaryDirectory(prefix="platinum-docs-") as temporary:
        # Prevent the desktop class from reading or changing real preferences.
        ui.DATA_DIR = Path(temporary)
        ui.SETTINGS_PATH = Path(temporary) / "settings.json"
        window = ui.create_window()
        window.resize(1440, 960)
        window._restore_options(report.options)
        window._phase = "analyze"
        window._on_report(report)
        window._refresh_actions()
        window.device_label.setText("●  Local workspace")
        window.status_label.setText("Demo collection · 7 files ready to review. Apply sorting when you are ready.")
        window.progress_label.setText("DEMO")
        window.show()
        app.processEvents()
        window.table.selectRow(4)
        window.selection_detail.setText("Demo files shown · your original images are preserved when copying.")
        app.processEvents()
        assert window.grab().save(str(output / "organizer.png"))

        dialog = FaceCropDialog(report)
        dialog.resize(1180, 800)
        dialog._service = DemoFaceService(report, portrait_png())
        dialog.show()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.processEvents()
            if dialog._worker is None and not dialog._preview_pixmap.isNull():
                break
            time.sleep(0.01)
        assert dialog._worker is None and not dialog._preview_pixmap.isNull()
        dialog.device_label.setText("●  Local export")
        dialog.status_label.setText("Illustrated demo preview · export saves all 8 face crops as separate PNG files.")
        app.processEvents()
        assert dialog.grab().save(str(output / "face-crops.png"))
        dialog.reject()
        window.close()
        app.processEvents()
    print(f"Created {output / 'organizer.png'}")
    print(f"Created {output / 'face-crops.png'}")


if __name__ == "__main__":
    main()
