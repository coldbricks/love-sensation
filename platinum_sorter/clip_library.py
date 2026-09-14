"""Read-only discovery and validation of local footage for timeline assembly."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .pmv_forge import prepare_candidate_clips
from .video_engine import is_video_path


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checked_file_stat(path: Path, root: Path) -> os.stat_result:
    # Recheck the parents as well: a directory can become a junction after discovery.
    for relative in reversed(path.relative_to(root).parents):
        directory = root / relative
        info = directory.lstat()
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("A containing folder is a link, reparse point, or no longer a directory")
    info = path.lstat()
    if _is_link_or_reparse(info):
        raise ValueError("Links and reparse points are skipped")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("The source is no longer a regular file")
    return info


def scan_clip_folder(folder: Path, recursive=True, cancel_event=None, progress=None) -> dict:
    """Return validated clips, skipped ``{path, reason}`` records, and cancellation state.

    This function performs blocking metadata probes and belongs in a worker thread.
    ``progress(done, total, path)`` runs there after each attempted video file; its
    path argument is a ``Path``. Links and Windows reparse points are never followed.
    Source files are only read, and candidate preparation does not run detection.
    """
    root = Path(os.path.abspath(os.fspath(folder)))
    try:
        info = root.lstat()
    except OSError as exc:
        raise ValueError("Choose an existing footage folder") from exc
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Choose a regular footage folder, not a link or reparse point")

    report = {"clips": [], "skipped": [], "cancelled": False}

    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            report["cancelled"] = True
            return True
        return False

    def skip(path, reason):
        report["skipped"].append({"path": str(path), "reason": str(reason)})

    pending = [root]
    candidates = []
    while pending:
        if cancelled():
            return report
        directory = pending.pop()
        try:
            directory_info = directory.lstat()
            if _is_link_or_reparse(directory_info) or not stat.S_ISDIR(directory_info.st_mode):
                skip(directory, "Links, reparse points, and unavailable folders are skipped")
                continue
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as exc:
            skip(directory, exc)
            continue
        subdirectories = []
        for entry in ordered:
            if cancelled():
                return report
            path = Path(entry.path)
            try:
                entry_info = entry.stat(follow_symlinks=False)
                if _is_link_or_reparse(entry_info):
                    skip(path, "Links and reparse points are skipped")
                elif stat.S_ISDIR(entry_info.st_mode):
                    if recursive:
                        subdirectories.append(path)
                elif stat.S_ISREG(entry_info.st_mode) and is_video_path(path):
                    candidates.append(path)
            except OSError as exc:
                skip(path, exc)
        pending.extend(reversed(subdirectories))

    candidates.sort(key=lambda path: (str(path.relative_to(root)).casefold(), str(path.relative_to(root))))
    total = len(candidates)
    for index, path in enumerate(candidates, 1):
        if cancelled():
            return report
        try:
            before = _checked_file_stat(path, root)
            prepared = prepare_candidate_clips([{"path": str(path)}])
            after = _checked_file_stat(path, root)
            if _identity(before) != _identity(after):
                raise ValueError("The source changed while its metadata was being read; reload the folder")
            report["clips"].extend(prepared)
        except (OSError, ValueError, RuntimeError) as exc:
            skip(path, exc)
        if progress is not None:
            progress(index, total, path)
    cancelled()
    return report
