import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from platinum_sorter.clip_library import scan_clip_folder


class ClipLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test_clip_library_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        prepare_patch = patch(
            "platinum_sorter.clip_library.prepare_candidate_clips",
            side_effect=lambda clips: [dict(clip, duration_s=4.0, width=320, height=240) for clip in clips],
        )
        self.prepare = prepare_patch.start()
        self.addCleanup(prepare_patch.stop)

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic metadata fixture")
        return path

    def test_recursive_discovery_filters_extensions_and_orders_progress(self):
        self.file("z.MP4")
        self.file("a.mov")
        self.file("nested/b.mkv")
        self.file("nested/not-video.txt")
        self.file("song.mp3")
        updates = []
        result = scan_clip_folder(self.root, progress=lambda *args: updates.append(args))
        self.assertEqual([Path(clip["path"]).relative_to(self.root).as_posix() for clip in result["clips"]],
                         ["a.mov", "nested/b.mkv", "z.MP4"])
        self.assertEqual([(done, total) for done, total, _ in updates], [(1, 3), (2, 3), (3, 3)])
        self.assertTrue(all(isinstance(path, Path) for _, _, path in updates))
        self.assertEqual(result["skipped"], [])
        self.assertFalse(result["cancelled"])
        flat = scan_clip_folder(self.root, recursive=False)
        self.assertEqual([Path(clip["path"]).name for clip in flat["clips"]], ["a.mov", "z.MP4"])

    def test_invalid_root_and_empty_folder(self):
        with self.assertRaises(ValueError):
            scan_clip_folder(self.root / "missing")
        with self.assertRaises(ValueError):
            scan_clip_folder(self.file("file.mp4"))
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(scan_clip_folder(empty), {"clips": [], "skipped": [], "cancelled": False})
        self.prepare.assert_not_called()

    def test_broken_and_unreadable_files_do_not_discard_good_clips(self):
        for name in ("a-broken.mp4", "b-unreadable.mp4", "c-good.mp4"):
            self.file(name)

        def prepare(clips):
            name = Path(clips[0]["path"]).name
            if name.startswith("a-"):
                raise ValueError("No readable video stream")
            if name.startswith("b-"):
                raise PermissionError("Access denied")
            return [{**clips[0], "duration_s": 4.0}]

        self.prepare.side_effect = prepare
        result = scan_clip_folder(self.root)
        self.assertEqual([Path(clip["path"]).name for clip in result["clips"]], ["c-good.mp4"])
        self.assertEqual([Path(item["path"]).name for item in result["skipped"]],
                         ["a-broken.mp4", "b-unreadable.mp4"])
        self.assertIn("Access denied", result["skipped"][1]["reason"])

    def test_cancelled_before_discovery_does_not_probe(self):
        self.file("a.mp4")
        stop = threading.Event()
        stop.set()
        result = scan_clip_folder(self.root, cancel_event=stop)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["clips"], [])
        self.prepare.assert_not_called()

    def test_cancellation_between_files_preserves_completed_clips(self):
        self.file("a.mp4")
        self.file("b.mp4")
        stop = threading.Event()
        result = scan_clip_folder(self.root, cancel_event=stop, progress=lambda *args: stop.set())
        self.assertTrue(result["cancelled"])
        self.assertEqual([Path(clip["path"]).name for clip in result["clips"]], ["a.mp4"])
        self.assertEqual(self.prepare.call_count, 1)

    def test_changed_file_is_not_accepted(self):
        changed = self.file("a.mp4")
        self.file("b.mp4")

        def prepare(clips):
            if Path(clips[0]["path"]) == changed:
                changed.write_bytes(b"changed size during mocked probe")
            return clips

        self.prepare.side_effect = prepare
        result = scan_clip_folder(self.root)
        self.assertEqual([Path(clip["path"]).name for clip in result["clips"]], ["b.mp4"])
        self.assertIn("changed", result["skipped"][0]["reason"])

    def test_links_and_reparse_files_and_directories_are_skipped(self):
        self.file("good.mp4")
        actual_scandir = os.scandir
        fake_entries = [
            SimpleNamespace(name="linked.mp4", path=str(self.root / "linked.mp4"),
                            stat=lambda **kwargs: SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)),
            SimpleNamespace(name="reparse.mp4", path=str(self.root / "reparse.mp4"),
                            stat=lambda **kwargs: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)),
            SimpleNamespace(name="junction", path=str(self.root / "junction"),
                            stat=lambda **kwargs: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)),
        ]

        class Entries:
            def __enter__(self):
                with actual_scandir(self_root) as real:
                    return list(real) + fake_entries

            def __exit__(self, *args):
                return False

        self_root = self.root
        with patch("platinum_sorter.clip_library.os.scandir", return_value=Entries()) as scan:
            result = scan_clip_folder(self.root)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual([Path(clip["path"]).name for clip in result["clips"]], ["good.mp4"])
        self.assertEqual({Path(item["path"]).name for item in result["skipped"]},
                         {"linked.mp4", "reparse.mp4", "junction"})
        self.assertEqual(self.prepare.call_count, 1)

    def test_directory_replaced_by_link_after_discovery_is_not_probed(self):
        source = self.file("nested/a.mp4")
        original_lstat = Path.lstat
        calls = 0

        def lstat(path, *args, **kwargs):
            nonlocal calls
            if path == source.parent:
                calls += 1
                if calls > 1:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            result = scan_clip_folder(self.root)
        self.assertEqual(result["clips"], [])
        self.assertIn("containing folder", result["skipped"][0]["reason"])
        self.prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
