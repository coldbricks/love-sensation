"""Mask owned Qt dialogs without cancelling their running workers or modal loops."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import weakref
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

_HIDDEN_TITLE = "Love Sensation — Workspace hidden"


def belongs_to(widget, owner):
    current = widget
    try:
        while current is not None:
            if current is owner:
                return True
            current = current.parent()
    except RuntimeError:
        # A worker can complete and delete its dialog during event delivery.
        return False
    return False


@dataclass
class _Mask:
    window: weakref.ReferenceType
    title: str
    minimum_size: QSize
    size: QSize
    children: list = field(default_factory=list)
    overlay: QFrame | None = None

    def remember(self, child):
        if not any(reference() is child for reference in self.children):
            self.children.append(weakref.ref(child))


class DialogMasks:
    def __init__(self, owner):
        self.owner = owner
        self.records = []
        self._mutating = 0

    @contextmanager
    def _changes(self):
        self._mutating += 1
        try:
            yield
        finally:
            self._mutating -= 1

    def _record(self, window):
        return next((record for record in self.records if record.window() is window), None)

    def _hide_visible_children(self, window, record):
        for child in window.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if child is record.overlay or child.isWindow() or child.isHidden():
                continue
            record.remember(child)
            child.hide()

    def _mask_window(self, window):
        record = self._record(window)
        with self._changes():
            if record is None:
                record = _Mask(weakref.ref(window), window.windowTitle(), window.minimumSize(), window.size())
                # Register before showing anything: Qt Show events are synchronous.
                self.records.append(record)
                overlay = QFrame(window)
                record.overlay = overlay
                overlay.setObjectName("privacyMask")
                overlay.setStyleSheet("QFrame#privacyMask { background: #101014; }")
                layout = QVBoxLayout(overlay)
                layout.addStretch()
                label = QLabel("Workspace hidden", overlay)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
                resume = QPushButton("Resume workspace", overlay)
                resume.clicked.connect(lambda: self.owner._set_privacy(False))
                layout.addWidget(resume, 0, Qt.AlignmentFlag.AlignCenter)
                layout.addStretch()
                # Keep the resume control reachable even on a small message box.
                window.setMinimumSize(record.minimum_size.expandedTo(QSize(280, 160)))
            self._hide_visible_children(window, record)
            window.setWindowTitle(_HIDDEN_TITLE)
            record.overlay.setGeometry(window.rect())
            record.overlay.show()
            record.overlay.raise_()
        return record

    def on_show(self, watched):
        """Synchronously conceal an owned dialog or newly shown child.

        Call from the application Show event filter. Only the direct content
        containers are hidden, so descendants keep their own intended visibility.
        The main workspace is protected separately by its stacked privacy page.
        """
        if self._mutating or not isinstance(watched, QWidget) or not belongs_to(watched, self.owner):
            return
        try:
            window = watched.window()
            if window is self.owner:
                return
            if window.windowType() in {Qt.WindowType.ToolTip, Qt.WindowType.Popup}:
                with self._changes():
                    window.hide()
                return
            record = self._record(window)
            if record is not None and belongs_to(watched, record.overlay):
                return
            # Children may receive Show before the top-level becomes visible.
            record = self._mask_window(window)
            if watched is not window:
                child = watched
                while child.parentWidget() is not window:
                    child = child.parentWidget()
                    if child is None:
                        return
                if child is not record.overlay and (child is watched or not child.isHidden()):
                    record.remember(child)
                    with self._changes():
                        child.hide()
                        record.overlay.raise_()
        except RuntimeError:
            # A completed/cancelled dialog may have been deleted while masked.
            return

    def mask_visible(self):
        for window in QApplication.topLevelWidgets():
            if window is self.owner or not window.isVisible() or not belongs_to(window, self.owner):
                continue
            self.on_show(window)

    def resized(self, window):
        record = self._record(window)
        if record is not None and record.overlay is not None:
            try:
                record.overlay.setGeometry(window.rect())
            except RuntimeError:
                pass

    def title_changed(self, window):
        """Keep a worker's latest title for resume without displaying it."""
        if self._mutating:
            return
        record = self._record(window)
        if record is None:
            return
        try:
            title = window.windowTitle()
            if title != _HIDDEN_TITLE:
                record.title = title
                with self._changes():
                    window.setWindowTitle(_HIDDEN_TITLE)
        except RuntimeError:
            pass

    def restore(self):
        records, self.records = self.records, []
        with self._changes():
            for record in records:
                window = record.window()
                try:
                    if window is None:
                        continue
                    record.overlay.hide()
                    record.overlay.deleteLater()
                    window.setWindowTitle(record.title)
                    window.setMinimumSize(record.minimum_size)
                    for child_ref in record.children:
                        child = child_ref()
                        try:
                            if child is not None:
                                child.show()
                        except RuntimeError:
                            continue
                    window.resize(record.size)
                except RuntimeError:
                    continue
