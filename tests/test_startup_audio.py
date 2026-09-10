"""Cue lifecycle checks use fake timers/players and never play sound."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from PySide6.QtMultimedia import QMediaPlayer

from platinum_sorter.startup_audio import AudioCueSettings, StartupAudioController, one_bar_ms


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots[:]:
            slot(*args)


class FakeTimer:
    def __init__(self, parent):
        self.timeout = FakeSignal()
        self.active = False
        self.intervals = []

    def setSingleShot(self, value):
        pass

    def setTimerType(self, value):
        pass

    def start(self, milliseconds):
        self.intervals.append(milliseconds)
        self.active = True

    def stop(self):
        self.active = False

    def fire(self):
        if self.active:
            self.active = False
            self.timeout.emit()


class FakeAudio:
    def setMuted(self, value):
        self.muted = value

    def setVolume(self, value):
        self.volume = value


class FakePlayer:
    def __init__(self):
        self.mediaStatusChanged = FakeSignal()
        self.positionChanged = FakeSignal()
        self.playbackStateChanged = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.position_value = 0
        self.duration_value = 30_000
        self.seekable = True
        self.play_count = 0
        self.stop_count = 0
        self.seek_calls = []

    def setAudioOutput(self, audio):
        self.audio = audio

    def setSource(self, url):
        self.source = url

    def setPosition(self, position):
        self.seek_calls.append(position)
        self.position_value = position
        self.positionChanged.emit(position)

    def position(self):
        return self.position_value

    def duration(self):
        return self.duration_value

    def isSeekable(self):
        return self.seekable

    def play(self):
        self.play_count += 1
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)

    def stop(self):
        self.stop_count += 1
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)


class StartupAudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="platinum-cue-test-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "synthetic.wav"
        self.path.write_bytes(b"not real audio; fake player only")
        self.player = FakePlayer()
        self.audio = FakeAudio()
        self.factory_calls = 0

        def factory(parent):
            self.factory_calls += 1
            return self.player, self.audio

        self.controller = StartupAudioController(data_dir=self.root / "data", media_factory=factory, timer_factory=FakeTimer)
        self.cue = AudioCueSettings(enabled=True, file_path=str(self.path), start_seconds=5.0, bpm=120.0, volume=.4)

    def tearDown(self):
        self.controller.stop()
        self.temporary.cleanup()

    def load(self, cue=None):
        self.assertTrue(self.controller.preview(cue or self.cue))
        self.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)

    def test_silent_by_default_does_not_initialize_audio_or_write_settings(self):
        self.assertFalse(self.controller.play_startup())
        self.assertEqual(0, self.factory_calls)
        self.assertFalse((self.root / "data").exists())

    def test_bar_duration_and_untrusted_settings_validation(self):
        self.assertEqual(2000, one_bar_ms(120))
        self.assertEqual(6000, one_bar_ms(40))
        self.assertEqual(1000, one_bar_ms(240))
        for bpm in (0, True, float("nan"), float("inf"), 241, "120"):
            with self.subTest(bpm=bpm), self.assertRaises(ValueError):
                one_bar_ms(bpm)
        for changes in ({"file_path": "https://example.org/track.mp3"}, {"file_path": "relative.wav"},
                        {"file_path": "\\\\server\\share\\song.mp3"}, {"start_seconds": -1},
                        {"volume": 1.1}, {"enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.cue, **changes).validated()

    def test_seek_happens_after_load_and_wall_timer_starts_on_playing(self):
        self.assertTrue(self.controller.preview(self.cue))
        self.assertEqual([], self.player.seek_calls)
        self.assertEqual(0, self.player.play_count)
        self.assertTrue(self.audio.muted)
        self.assertTrue(self.controller._load_timer.active)
        self.assertFalse(self.controller._wall_timer.active)
        self.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
        self.assertEqual([5000], self.player.seek_calls)
        self.assertEqual(1, self.player.play_count)
        self.assertEqual([2000], self.controller._wall_timer.intervals)
        self.assertFalse(self.controller._load_timer.active)
        self.assertFalse(self.audio.muted)
        self.assertEqual(.4, self.audio.volume)

    def test_media_position_boundary_stops_and_releases_file(self):
        self.load()
        self.player.positionChanged.emit(6999)
        self.assertEqual("playing", self.controller._phase)
        self.player.positionChanged.emit(7000)
        self.assertEqual("idle", self.controller._phase)
        self.assertTrue(self.audio.muted)
        self.assertTrue(self.player.source.isEmpty())
        self.assertFalse(self.controller._wall_timer.active)

    def test_stall_cannot_extend_wall_deadline_or_restart_the_cue(self):
        self.load()
        self.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.StalledMedia)
        self.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)
        self.assertEqual([2000], self.controller._wall_timer.intervals)
        self.controller._wall_timer.fire()
        self.assertEqual("idle", self.controller._phase)
        self.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertEqual(1, self.player.play_count)
        self.assertTrue(self.audio.muted)

    def test_load_timeout_and_decode_errors_stop_cleanly(self):
        self.assertTrue(self.controller.preview(self.cue))
        self.controller._load_timer.fire()
        self.assertEqual("idle", self.controller._phase)
        self.assertIn("too long", self.controller.last_status)
        self.load()
        self.player.errorOccurred.emit(QMediaPlayer.Error.FormatError, "bad synthetic format")
        self.assertEqual("idle", self.controller._phase)
        self.assertIn("bad synthetic format", self.controller.last_status)
        self.assertTrue(self.player.source.isEmpty())

    def test_too_short_or_unseekable_file_never_plays(self):
        self.player.duration_value = 6000
        self.load()
        self.assertEqual(0, self.player.play_count)
        self.assertIn("one full bar", self.controller.last_status)
        self.player.duration_value = 30_000
        self.player.seekable = False
        self.load()
        self.assertEqual(0, self.player.play_count)
        self.assertIn("cannot seek", self.controller.last_status)

    def test_local_persistence_preview_is_unsaved_and_startup_attempts_once(self):
        self.load()
        self.assertFalse(self.controller.settings_path.exists())
        self.controller.save_settings(self.cue)
        payload = json.loads(self.controller.settings_path.read_text())
        self.assertEqual(str(self.path), payload["file_path"])
        self.assertEqual(.4, payload["volume"])
        self.assertTrue(self.controller.play_startup())
        self.assertFalse(self.controller.play_startup())
        reloaded = StartupAudioController(data_dir=self.root / "data", timer_factory=FakeTimer)
        self.assertEqual(self.cue, reloaded.settings)

    def test_muted_or_zero_volume_never_initializes_audio(self):
        self.assertFalse(self.controller.preview(replace(self.cue, muted=True)))
        self.assertFalse(self.controller.preview(replace(self.cue, volume=0)))
        self.assertEqual(0, self.factory_calls)

    def test_corrupt_settings_fail_closed_without_a_startup_popup(self):
        self.controller.settings_path.parent.mkdir()
        self.controller.settings_path.write_text('{"enabled":true,"bpm":0}')
        reloaded = StartupAudioController(data_dir=self.root / "data", timer_factory=FakeTimer)
        self.assertFalse(reloaded.settings.enabled)
        self.assertFalse(reloaded.play_startup())


if __name__ == "__main__":
    unittest.main()
