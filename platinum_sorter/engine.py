"""Read-only analysis and journaled, collision-safe image filing.

The detector is injected so file safety can be tested without loading a model
or accessing a user's media. Originals are never edited by analysis.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .contracts import Detector, EventSink, ImageResult, ScanReport, SortOptions

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"})
_CHUNK_SIZE = 1024 * 1024
# One current and one prefetched group: at most 128 MiB of encoded images.
# Larger individual files retain the streaming signature/path adapter.
_SNAPSHOT_BATCH_BYTES = 64 * 1024 * 1024
_APPLIED = frozenset({"copied", "moved"})


class _Cancelled(Exception):
    pass


def _send(emit: EventSink, **event) -> None:
    emit(event)


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_no_links(path: Path, *, allow_missing: bool = False) -> None:
    """Reject symlinks, junctions, and other Windows reparse points."""
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            if _is_link(component):
                raise ValueError(f"Symbolic links and junctions are not supported: {component}")
        except FileNotFoundError:
            if not allow_missing:
                raise


def _within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _validate_options(options: SortOptions) -> tuple[Path, Path]:
    if not isinstance(options.source, str) or not options.source.strip():
        raise ValueError("Choose an existing source folder.")
    if not isinstance(options.destination, str) or not options.destination.strip():
        raise ValueError("Choose an output folder.")
    source = Path(os.path.abspath(Path(options.source).expanduser()))
    destination = Path(os.path.abspath(Path(options.destination).expanduser()))
    _assert_no_links(source)
    _assert_no_links(destination, allow_missing=True)
    if not source.is_dir():
        raise ValueError(f"Source is not an existing folder: {source}")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Output is not a folder: {destination}")
    source, destination = source.resolve(), destination.resolve()
    if _within(source, destination) or _within(destination, source):
        raise ValueError("Source and output must be separate folders; neither may contain the other.")
    if not isinstance(options.threshold, (int, float)) or not math.isfinite(options.threshold) or not 0 <= options.threshold <= 1:
        raise ValueError("Confidence threshold must be between 0 and 1.")
    if options.mode not in {"best", "top3", "all"}:
        raise ValueError("Category mode must be best, top3, or all.")
    if options.operation not in {"copy", "move"}:
        raise ValueError("Operation must be copy or move.")
    if not isinstance(options.batch_size, int) or not 1 <= options.batch_size <= 256:
        raise ValueError("Batch size must be between 1 and 256.")
    return source, destination


@contextmanager
def _open_regular(path: Path, *, delete_access: bool = False) -> Iterator[object]:
    """Open a regular file without following a final-component symlink.

    Windows needs OPEN_REPARSE_POINT as os.open has no O_NOFOLLOW there.
    Ancestor reparse points are checked separately before every source read.
    """
    _assert_no_links(path)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        # SHARE_READ only: another process cannot replace or modify this source
        # while the handle is open. OPEN_REPARSE_POINT avoids link traversal.
        handle = create_file(str(path), 0x80000000 | (0x10000 if delete_access else 0), 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        get_info.restype = wintypes.BOOL

        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        tags = AttributeTag()
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        if not get_info(handle, 9, ctypes.byref(tags), ctypes.sizeof(tags)):
            error = ctypes.get_last_error()
            close_handle(handle)
            raise ctypes.WinError(error)
        if tags.attributes & 0x400:
            close_handle(handle)
            raise ValueError(f"Refusing reparse-point file: {path}")
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close_handle(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Not a regular file: {path}")
        file = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with file:
        yield file


def _hash_stream(file, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    while True:
        if cancel is not None and cancel.is_set():
            raise _Cancelled("Cancelled while reading file.")
        chunk = file.read(_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _signature(path: Path, cancel: threading.Event | None = None) -> tuple[int, int, str]:
    with _open_regular(path) as file:
        before = os.fstat(file.fileno())
        digest = _hash_stream(file, cancel)
        after = os.fstat(file.fileno())
    current = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or (
        after.st_size, after.st_mtime_ns, after.st_ino
    ) != (current.st_size, current.st_mtime_ns, current.st_ino):
        raise ValueError(f"Source changed while reading: {path}")
    return before.st_size, before.st_mtime_ns, digest


def _read_snapshot(path: Path, cancel: threading.Event, max_bytes: int) -> tuple[tuple[int, int, str], bytes | None]:
    """Hash exactly the immutable encoded bytes supplied to the model.

    The existing locked regular-file open and before/after checks are retained.
    Oversized inputs use bounded streaming hashing and the legacy detector path.
    """
    with _open_regular(path) as file:
        before = os.fstat(file.fileno())
        content = None
        if before.st_size <= max_bytes:
            if cancel.is_set():
                raise _Cancelled("Cancelled while reading file.")
            # Allocate for this file, not the entire 64 MiB group budget.
            content = file.read(before.st_size + 1)
            if len(content) > max_bytes:
                raise ValueError(f"Source changed while reading: {path}")
            digest = hashlib.sha256(content).hexdigest()
            if cancel.is_set():
                raise _Cancelled("Cancelled while reading file.")
        else:
            digest = _hash_stream(file, cancel)
        after = os.fstat(file.fileno())
    current = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or (
        after.st_size, after.st_mtime_ns, after.st_ino
    ) != (current.st_size, current.st_mtime_ns, current.st_ino):
        raise ValueError(f"Source changed while reading: {path}")
    if content is not None and len(content) != before.st_size:
        raise ValueError(f"Source changed while reading: {path}")
    return (before.st_size, before.st_mtime_ns, digest), content


@contextmanager
def _prefetched_sources(paths: list[Path], batch_size: int, cancel: threading.Event):
    """Overlap a single bounded read/hash group with current GPU inference.

    The worker never accesses the cache, reports, Qt callbacks, or the detector.
    Closing on cancellation/error stops prefetch and joins the sole reader.
    """
    stop = threading.Event()

    class ReadCancellation:
        def is_set(self):
            return cancel.is_set() or stop.is_set()

    read_cancel = ReadCancellation()

    def read_group(start):
        group, retained = [], 0
        while start < len(paths) and len(group) < batch_size and not cancel.is_set() and not stop.is_set():
            path = paths[start]
            try:
                # End the group before another normal image would exceed its
                # byte budget. A single oversized file uses streaming instead.
                if group and retained + path.lstat().st_size > _SNAPSHOT_BATCH_BYTES:
                    break
                signature, content = _read_snapshot(path, read_cancel, _SNAPSHOT_BATCH_BYTES - retained)
                retained += len(content) if content is not None else 0
                group.append((path, signature, content))
            except _Cancelled:
                break
            except Exception as error:
                group.append((path, error, None))
            start += 1
        return group, start

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-reader") as reader:
        def groups():
            future = reader.submit(read_group, 0)
            try:
                while not cancel.is_set() and not stop.is_set():
                    group, next_index = future.result()
                    if not group:
                        break
                    future = reader.submit(read_group, next_index) if next_index < len(paths) else None
                    yield group
                    # Drop the previous encoded group before awaiting the next.
                    del group
                    if future is None:
                        break
            finally:
                stop.set()
                if future is not None:
                    future.cancel()
        iterator = groups()
        try:
            yield iterator
        finally:
            stop.set()
            iterator.close()


def _delete_verified_source(path: Path, expected: tuple[int, int, str], cancel: threading.Event) -> None:
    """On Windows, verify and delete the same locked file handle.

    Holding SHARE_READ without SHARE_WRITE or SHARE_DELETE closes the usual
    check-then-unlink race: the source cannot be edited or replaced in between.
    """
    with _open_regular(path, delete_access=True) as original:
        info = os.fstat(original.fileno())
        actual = (info.st_size, info.st_mtime_ns, _hash_stream(original, cancel))
        if actual != expected:
            raise ValueError("Source changed during copying; original was preserved.")
        if cancel.is_set():
            raise _Cancelled("Cancelled before source removal; original was preserved.")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Disposition(ctypes.Structure):
                _fields_ = [("delete_file", ctypes.c_ubyte)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            set_info = kernel32.SetFileInformationByHandle
            set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            set_info.restype = wintypes.BOOL
            disposition = Disposition(1)
            if not set_info(msvcrt.get_osfhandle(original.fileno()), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            current = path.lstat()
            if _is_link(path) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
            ):
                raise ValueError("Source was replaced during copying; original was preserved.")
            path.unlink()


def _clean_detections(detections) -> list[dict]:
    if not isinstance(detections, list):
        raise ValueError("Detector returned an invalid detection list.")
    clean = []
    for item in detections:
        if not isinstance(item, dict) or not isinstance(item.get("class"), str) or not item["class"]:
            raise ValueError("Detector returned a detection without a class label.")
        score = float(item["score"])
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Detector returned an invalid confidence score.")
        box = [float(number) for number in item.get("box", [])]
        if box and (len(box) != 4 or not all(math.isfinite(number) for number in box)):
            raise ValueError("Detector returned an invalid bounding box.")
        clean.append({"class": item["class"], "score": score, "box": box})
    return clean


def _select_categories(detections: list[dict], options: SortOptions) -> list[str]:
    allowed = set(options.selected_classes)
    best: dict[str, float] = {}
    for detection in detections:
        label, confidence = detection["class"], detection["score"]
        if confidence >= options.threshold and (not allowed or label in allowed):
            best[label] = max(confidence, best.get(label, 0.0))
    categories = sorted(best, key=lambda label: (-best[label], label))
    if options.mode == "best":
        categories = categories[:1]
    elif options.mode == "top3":
        categories = categories[:3]
    return categories or (["_Unmatched"] if options.include_unmatched else [])


def _safe_category(label: str) -> str:
    component = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" .") or "_Unknown"
    if component.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        component = "_" + component
    return component[:120]


def _make_safe_directory(directory: Path) -> None:
    for component in reversed((directory, *directory.parents)):
        try:
            component.mkdir(exist_ok=True)
        except FileExistsError:
            pass
        if _is_link(component) or not component.is_dir():
            raise ValueError(f"Output path contains a link or non-folder: {component}")


def _reserve_destination(desired: Path) -> tuple[Path, int]:
    _make_safe_directory(desired.parent)
    index = 1
    while True:
        destination = desired if index == 1 else desired.with_name(f"{desired.stem}__{index}{desired.suffix}")
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            return destination, descriptor
        except FileExistsError:
            index += 1


def _atomic_json(path: Path, value: dict) -> None:
    _make_safe_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and _is_link(path):
            raise ValueError(f"Refusing to replace a linked report: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_jsonl(path: Path, records: list[dict]) -> None:
    _make_safe_directory(path.parent)
    _assert_no_links(path, allow_missing=True)
    payload = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records).encode("utf-8")
    with path.open("a+b") as file:
        # An interrupted final append can leave an incomplete last line. Remove
        # only that fragment before appending; complete earlier entries remain.
        end = file.seek(0, os.SEEK_END)
        if end:
            file.seek(end - 1)
            if file.read(1) != b"\n":
                cursor = end
                while cursor:
                    start = max(0, cursor - _CHUNK_SIZE)
                    file.seek(start)
                    block = file.read(cursor - start)
                    newline = block.rfind(b"\n")
                    if newline >= 0:
                        file.truncate(start + newline + 1)
                        break
                    cursor = start
                else:
                    file.truncate(0)
        file.seek(0, os.SEEK_END)
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())


class SorterEngine:
    def __init__(self, data_dir: Path, detector_factory: Callable[[], Detector]):
        self.data_dir = Path(os.path.abspath(data_dir))
        self.detector_factory = detector_factory
        self._detector: Detector | None = None
        self._run_lock = threading.Lock()

    def _journal(self, report: ScanReport, changed: ImageResult | list[ImageResult] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]+", report.run_id):
            raise ValueError("Invalid report run identifier.")
        path = self.data_dir / "runs" / f"{report.run_id}.json"
        report.manifest_path = str(path)
        sequence = getattr(report, "_journal_sequence", 0)
        if changed is None:
            snapshot = report.as_dict()
            snapshot["journal_sequence"] = sequence
            _atomic_json(path, snapshot)
        else:
            entries = []
            for result in changed if isinstance(changed, list) else [changed]:
                sequence += 1
                entries.append({"sequence": sequence, "run_id": report.run_id, "result": asdict(result),
                                "cancelled": report.cancelled, "elapsed_seconds": report.elapsed_seconds})
            if entries:
                _append_jsonl(path.with_suffix(".wal.jsonl"), entries)
                report._journal_sequence = sequence

    def _files(self, source: Path, cancel: threading.Event, emit: EventSink) -> list[Path]:
        files = []
        metadata_sidecars = 0
        def walk_error(error: OSError) -> None:
            raise OSError(f"Cannot read source folder: {error}") from error

        for directory, folders, names in os.walk(source, followlinks=False, onerror=walk_error):
            if cancel.is_set():
                break
            # os.walk does not follow symbolic links but can traverse junctions
            # on some Python/Windows versions; explicitly filter all reparses.
            folders[:] = sorted((name for name in folders if not _is_link(Path(directory) / name)), key=str.casefold)
            for name in sorted(names, key=str.casefold):
                path = Path(directory) / name
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    try:
                        if not _is_link(path) and path.is_file():
                            # macOS AppleDouble resource metadata sometimes keeps
                            # its photo's extension. Confirm the magic; a real
                            # photograph merely named ._something must survive.
                            if name.startswith("._"):
                                with _open_regular(path) as file:
                                    if file.read(4) == b"\x00\x05\x16\x07":
                                        metadata_sidecars += 1
                                        continue
                            files.append(path)
                    except OSError as error:
                        _send(emit, type="status", message=f"Cannot inspect {path}: {error}")
        if metadata_sidecars:
            _send(emit, type="status", message=f"Ignored {metadata_sidecars:,} macOS metadata sidecar files (originals preserved).")
        return files

    def _batch(self, paths: list[Path], snapshots: list[bytes] | None = None) -> list[list[dict] | Exception]:
        assert self._detector is not None
        try:
            output = self._detector.detect_encoded_batch(snapshots) if snapshots is not None else self._detector.detect_batch(paths)
            if len(output) != len(paths):
                raise ValueError("Detector returned a different number of results than input images.")
            return output
        except Exception as error:
            if len(paths) == 1:
                return [error]
            # Isolate a corrupt image if an adapter raised for its whole batch.
            return [self._batch([path], [snapshots[index]] if snapshots is not None else None)[0]
                    for index, path in enumerate(paths)]

    def analyze(self, options: SortOptions, emit: EventSink, cancel: threading.Event) -> ScanReport:
        with self._run_lock:
            return self._analyze(options, emit, cancel)

    def _analyze(self, options: SortOptions, emit: EventSink, cancel: threading.Event) -> ScanReport:
        source, _ = _validate_options(options)
        started = time.perf_counter()
        # Detach options from controls which may be changed during a running job.
        options = SortOptions(**json.loads(json.dumps(options.__dict__)))
        report = ScanReport(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12],
            options=options,
        )
        _send(emit, type="status", message="Finding image files...")
        paths = self._files(source, cancel, emit)
        _send(emit, type="progress", completed=0, total=len(paths), phase="analyze")
        _make_safe_directory(self.data_dir)
        self._journal(report)
        if paths and not cancel.is_set():
            if self._detector is None:
                _send(emit, type="status", message="Loading detection model...")
                self._detector = self.detector_factory()
            report.device = dict(self._detector.info)
            _send(emit, type="device", info=report.device)
            cache_path = self.data_dir / "detections.sqlite3"
            _assert_no_links(cache_path, allow_missing=True)
            with closing(sqlite3.connect(cache_path, timeout=30)) as cache:
                cache.execute("PRAGMA journal_mode=WAL")
                cache.execute("CREATE TABLE IF NOT EXISTS detections (path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, fingerprint TEXT NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (path,size,mtime_ns,fingerprint))")
                pending: list[ImageResult] = []
                pending_snapshots: dict[str, bytes] = {}
                use_snapshots = callable(getattr(self._detector, "detect_encoded_batch", None))
                if use_snapshots:
                    _send(emit, type="status", message="Reading and hashing ahead of GPU batches (128 MiB buffer limit)...")
                journaled_count = 0

                def checkpoint() -> None:
                    nonlocal journaled_count
                    self._journal(report, report.results[journaled_count:])
                    journaled_count = len(report.results)

                def finish(result: ImageResult, raw=None, error: Exception | None = None) -> None:
                    if error is not None:
                        result.status, result.error = "error", f"Analysis failed: {type(error).__name__}: {error}"
                    else:
                        try:
                            result.detections = _clean_detections(raw)
                            result.categories = _select_categories(result.detections, options)
                            result.status = "ready" if result.categories else "skipped"
                            if not result.cached:
                                cache.execute("INSERT OR REPLACE INTO detections VALUES (?,?,?,?,?,?)", (result.source, result.size, result.mtime_ns, self._detector.fingerprint, result.sha256, json.dumps(result.detections)))
                        except Exception as detected_error:
                            result.status, result.error = "error", f"Analysis failed: {type(detected_error).__name__}: {detected_error}"
                    report.results.append(result)
                    _send(emit, type="result", result=result)
                    _send(emit, type="progress", completed=len(report.results), total=len(paths), phase="analyze")

                def flush() -> None:
                    if not pending:
                        return
                    batch = list(pending)
                    pending.clear()
                    encoded_indices = [index for index, result in enumerate(batch) if result.source in pending_snapshots]
                    legacy_indices = [index for index, result in enumerate(batch) if result.source not in pending_snapshots]
                    detections = [None] * len(batch)
                    if encoded_indices:
                        raw_encoded = self._batch(
                            [Path(batch[index].source) for index in encoded_indices],
                            [pending_snapshots[batch[index].source] for index in encoded_indices],
                        )
                        for index, raw in zip(encoded_indices, raw_encoded):
                            detections[index] = raw
                    if legacy_indices:
                        for index, raw in zip(legacy_indices, self._batch([Path(batch[index].source) for index in legacy_indices])):
                            detections[index] = raw
                    for result, raw in zip(batch, detections):
                        if isinstance(raw, Exception):
                            finish(result, error=raw)
                        else:
                            try:
                                if cancel.is_set():
                                    raise _Cancelled("Analysis cancelled before source verification.")
                                path = Path(result.source)
                                if result.source in pending_snapshots:
                                    # Hash and detection came from exactly the same
                                    # immutable bytes. No second full read is needed.
                                    # Apply and future cache hits still rehash source.
                                    _assert_no_links(path)
                                    current = path.lstat()
                                    if (current.st_size, current.st_mtime_ns) != (result.size, result.mtime_ns):
                                        raise ValueError("Source changed during analysis.")
                                elif _signature(path, cancel) != (result.size, result.mtime_ns, result.sha256):
                                    raise ValueError("Source changed during analysis.")
                                finish(result, raw=raw)
                            except _Cancelled:
                                result.status, result.error = "cancelled", "Analysis cancelled before source verification."
                                report.results.append(result)
                                _send(emit, type="result", result=result)
                            except Exception as error:
                                finish(result, error=error)
                    pending_snapshots.clear()
                    cache.commit()
                    report.elapsed_seconds = time.perf_counter() - started
                    checkpoint()

                def accept_source(path, signature, content=None):
                    result = ImageResult(str(path), str(path.relative_to(source)), 0, 0)
                    try:
                        if isinstance(signature, Exception):
                            raise signature
                        result.size, result.mtime_ns, result.sha256 = signature
                        row = cache.execute("SELECT sha256,payload FROM detections WHERE path=? AND size=? AND mtime_ns=? AND fingerprint=?", (str(path), result.size, result.mtime_ns, self._detector.fingerprint)).fetchone()
                        if row is not None and row[0] == result.sha256:
                            result.cached = True
                            try:
                                payload = json.loads(row[1])
                            except (TypeError, ValueError):
                                result.cached = False
                                pending.append(result)
                            else:
                                finish(result, raw=payload)
                        else:
                            pending.append(result)
                        if not result.cached and content is not None:
                            pending_snapshots[result.source] = content
                    except Exception as error:
                        finish(result, error=error)

                if use_snapshots:
                    with _prefetched_sources(paths, options.batch_size, cancel) as groups:
                        for group in groups:
                            for path, signature, content in group:
                                if cancel.is_set():
                                    break
                                accept_source(path, signature, content)
                            if not cancel.is_set():
                                flush()
                                if len(report.results) - journaled_count >= options.batch_size * 4:
                                    checkpoint()
                            # Release this group before requesting another. Only
                            # current + one prefetched encoded group are retained.
                            pending_snapshots.clear()
                            group.clear()
                            content = None
                            if cancel.is_set():
                                break
                else:
                    for path in paths:
                        if cancel.is_set():
                            break
                        try:
                            signature = _signature(path, cancel)
                        except _Cancelled:
                            break
                        except Exception as error:
                            signature = error
                        accept_source(path, signature)
                        if len(pending) >= options.batch_size:
                            flush()
                        elif len(report.results) - journaled_count >= options.batch_size * 4:
                            checkpoint()
                if not cancel.is_set():
                    flush()
                cache.commit()
        report.results.sort(key=lambda result: result.relative_path.casefold())
        if self._detector is not None:
            report.device = dict(self._detector.info)
            _send(emit, type="device", info=report.device)
        report.cancelled = cancel.is_set()
        report.analysis_complete = not report.cancelled
        report.elapsed_seconds = time.perf_counter() - started
        self._journal(report)
        _send(emit, type="status", message="Analysis cancelled; originals are unchanged." if report.cancelled else f"Analysis complete: {len(report.results)} images reviewed.")
        return report

    def execute(self, report: ScanReport, emit: EventSink, cancel: threading.Event) -> ScanReport:
        with self._run_lock:
            return self._execute(report, emit, cancel)

    def _execute(self, report: ScanReport, emit: EventSink, cancel: threading.Event) -> ScanReport:
        if not report.analysis_complete:
            raise ValueError("This analysis did not finish. Analyze the source folder again before sorting.")
        source_root, output_root = _validate_options(report.options)
        report.phase = "execute"
        started = time.perf_counter()
        report.cancelled = False
        total = len(report.results)
        self._journal(report)
        for index, result in enumerate(report.results):
            if cancel.is_set():
                report.cancelled = True
                break
            if result.status in _APPLIED or not result.categories or not result.sha256:
                _send(emit, type="progress", completed=index + 1, total=total, phase="apply")
                continue
            current_output: dict | None = None
            try:
                source = Path(os.path.abspath(result.source))
                relative = Path(result.relative_path)
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError("Review manifest contains an unsafe relative source path.")
                if source != source_root / relative or not _within(source, source_root):
                    raise ValueError("Review manifest source is outside the selected source folder.")
                expected = (result.size, result.mtime_ns, result.sha256)
                if result.status == "removing_source" and report.options.operation == "move" and not source.exists():
                    for category in dict.fromkeys(result.categories):
                        details = [detail for detail in result.output_details if detail.get("category") == category and detail.get("status") == "verified"]
                        if not details:
                            raise ValueError("Interrupted move has no verified output for a required category.")
                        destination = Path(details[-1]["path"])
                        if not _within(destination, output_root) or destination.parent != (output_root / _safe_category(category) / relative).parent:
                            raise ValueError("Interrupted move has an output outside its expected category folder.")
                        size, _, digest = _signature(destination, cancel)
                        if (size, digest) != (result.size, result.sha256):
                            raise ValueError("Interrupted move output failed verification; source is already absent.")
                    result.status, result.error = "moved", ""
                    self._journal(report, result)
                    _send(emit, type="result", result=result)
                    _send(emit, type="progress", completed=index + 1, total=total, phase="apply")
                    continue
                if _signature(source, cancel) != expected:
                    raise ValueError("Source changed since analysis; analyze again before copying or moving it.")
                result.status, result.error = "copying", ""
                self._journal(report, result)
                verified_outputs = []
                for category in dict.fromkeys(result.categories):
                    if cancel.is_set():
                        raise _Cancelled("Cancelled between category copies; original was preserved.")
                    desired = output_root / _safe_category(category) / relative
                    previous = next((detail for detail in result.output_details if detail.get("category") == category and detail.get("status") == "verified"), None)
                    if previous:
                        previous_path = Path(previous["path"])
                        if not _within(previous_path, output_root) or previous_path.parent != desired.parent:
                            raise ValueError("Review manifest contains an output outside its expected category folder.")
                        try:
                            size, _, digest = _signature(previous_path, cancel)
                            if (size, digest) != (result.size, result.sha256):
                                raise ValueError("Previously copied output changed.")
                        except _Cancelled:
                            raise
                        except Exception as error:
                            previous["status"], previous["error"] = "error", str(error)
                        else:
                            verified_outputs.append(previous_path)
                            continue
                    destination, descriptor = _reserve_destination(desired)
                    current_output = {"path": str(destination), "category": category, "status": "reserved", "error": "", "sha256": ""}
                    result.destinations.append(str(destination))
                    result.output_details.append(current_output)
                    try:
                        self._journal(report, result)
                        with os.fdopen(descriptor, "wb") as target:
                            descriptor = -1
                            self._copy_source(source, target, cancel)
                            target.flush()
                            os.fsync(target.fileno())
                        size, _, digest = _signature(destination, cancel)
                        if (size, digest) != (result.size, result.sha256):
                            raise ValueError("Copied output failed SHA256 verification.")
                        os.utime(destination, ns=(source.stat().st_atime_ns, result.mtime_ns))
                        current_output.update(status="verified", sha256=digest)
                        verified_outputs.append(destination)
                        self._journal(report, result)
                    finally:
                        if descriptor != -1:
                            os.close(descriptor)
                if cancel.is_set():
                    raise _Cancelled("Cancelled before completion; original was preserved.")
                # Verify all destinations again immediately before source removal.
                # A source is never deleted after a partial or failed category copy.
                if report.options.operation == "move":
                    with ExitStack() as locked_outputs:
                        for destination in verified_outputs:
                            copied_file = locked_outputs.enter_context(_open_regular(destination))
                            size = os.fstat(copied_file.fileno()).st_size
                            digest = _hash_stream(copied_file, cancel)
                            if (size, digest) != (result.size, result.sha256):
                                raise ValueError(f"Output changed before source removal: {destination}")
                        result.status = "removing_source"
                        self._journal(report, result)
                        _delete_verified_source(source, expected, cancel)
                    result.status = "moved"
                else:
                    # Surface concurrent source changes even though the originals
                    # are safe and each destination matched the analyzed content.
                    if _signature(source, cancel) != expected:
                        raise ValueError("Source changed during copying; verified output copies were retained.")
                    result.status = "copied"
                current_output = None
            except _Cancelled as error:
                result.status, result.error = "cancelled", str(error)
                report.cancelled = True
                if current_output and current_output["status"] != "verified":
                    current_output.update(status="partial", error=str(error))
            except Exception as error:
                result.status, result.error = "error", f"{type(error).__name__}: {error}"
                if current_output and current_output["status"] != "verified":
                    current_output.update(status="error", error=result.error)
            self._journal(report, result)
            _send(emit, type="result", result=result)
            _send(emit, type="progress", completed=index + 1, total=total, phase="apply")
            if report.cancelled:
                break
        report.cancelled = report.cancelled or cancel.is_set()
        report.elapsed_seconds += time.perf_counter() - started
        self._journal(report)
        _send(emit, type="status", message="Filing cancelled; progress was saved." if report.cancelled else "Filing complete; review each result for errors.")
        return report

    @staticmethod
    def _copy_source(source: Path, target, cancel: threading.Event) -> None:
        with _open_regular(source) as original:
            while True:
                if cancel.is_set():
                    raise _Cancelled("Cancelled during a copy; original was preserved and partial output was recorded.")
                chunk = original.read(_CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)


def load_report(path: Path) -> ScanReport:
    """Restore a JSON snapshot and replay any newer durable operation entries."""
    path = Path(os.path.abspath(path))
    with _open_regular(path) as file:
        document = json.load(file)
    sequence = int(document.pop("journal_sequence", 0))
    document["options"] = SortOptions(**document["options"])
    document["results"] = [ImageResult(**row) for row in document.get("results", [])]
    report = ScanReport(**document)
    positions = {result.source: index for index, result in enumerate(report.results)}
    wal_path = path.with_suffix(".wal.jsonl")
    if wal_path.exists():
        with _open_regular(wal_path) as file:
            for encoded_line in file:
                try:
                    entry = json.loads(encoded_line)
                except (ValueError, UnicodeDecodeError):
                    if not encoded_line.endswith(b"\n"):
                        break  # Only an incomplete final append is recoverable.
                    raise ValueError(f"Operation journal contains a corrupt complete entry: {wal_path}")
                if entry.get("run_id") != report.run_id:
                    raise ValueError("Operation journal belongs to a different review run.")
                entry_sequence = int(entry["sequence"])
                if entry_sequence <= sequence:
                    continue
                result = ImageResult(**entry["result"])
                position = positions.get(result.source)
                if position is None:
                    positions[result.source] = len(report.results)
                    report.results.append(result)
                else:
                    report.results[position] = result
                report.cancelled = bool(entry.get("cancelled", False))
                report.elapsed_seconds = float(entry.get("elapsed_seconds", report.elapsed_seconds))
                sequence = entry_sequence
    report._journal_sequence = sequence
    report.manifest_path = str(path)
    return report


def export_report(report: ScanReport, path: Path) -> None:
    """Export the full review journal as JSON or a spreadsheet-readable CSV."""
    path = Path(os.path.abspath(path))
    if path.suffix.lower() == ".json":
        _atomic_json(path, report.as_dict())
    elif path.suffix.lower() == ".csv":
        _make_safe_directory(path.parent)
        if path.exists() and _is_link(path):
            raise ValueError(f"Refusing to replace a linked report: {path}")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fields = ["source", "relative_path", "size", "mtime_ns", "sha256", "status", "categories", "destinations", "error", "cached", "detections", "output_details"]
        try:
            with temporary.open("x", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                for result in report.results:
                    row = {field: getattr(result, field) for field in fields}
                    for field in ("categories", "destinations", "detections", "output_details"):
                        row[field] = json.dumps(row[field], ensure_ascii=False)
                    # Spreadsheet programs treat formula-leading cell text as
                    # executable formulas, even when CSV quoting is present.
                    for field, value in row.items():
                        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                            row[field] = "'" + value
                    writer.writerow(row)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        raise ValueError("Report filename must end in .json or .csv.")
