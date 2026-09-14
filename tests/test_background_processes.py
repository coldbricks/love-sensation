"""GUI media helpers capture output without opening a Windows console."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from platinum_sorter import video_engine
from platinum_sorter.process_options import background_process_options


def test_background_options_disable_console_and_interactive_input():
    options = background_process_options()
    assert options['stdin'] == subprocess.DEVNULL
    assert options['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@pytest.mark.skipif(os.name != 'nt', reason='Windows console behavior')
def test_media_child_really_has_no_console_and_keeps_captured_output():
    result = video_engine._run_media([
        sys.executable, '-c',
        'import ctypes,sys; print(ctypes.windll.kernel32.GetConsoleWindow()); print("captured",file=sys.stderr)',
    ])
    assert result.returncode == 0
    assert result.stdout.strip() == b'0'
    assert result.stderr.strip() == b'captured'


def test_probe_and_encoder_checks_use_background_options(tmp_path):
    path = tmp_path / 'placeholder.mp4'
    path.write_bytes(b'synthetic')
    with patch.object(video_engine.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"streams":[]}', '')) as run:
        video_engine.probe_media_file(path)
        assert all(run.call_args.kwargs[key] == value for key, value in background_process_options().items())
    video_engine._h264_encoder.cache_clear()
    try:
        with patch.object(video_engine.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
            assert video_engine._h264_encoder() == 'h264_nvenc'
            assert all(run.call_args.kwargs[key] == value for key, value in background_process_options().items())
    finally:
        video_engine._h264_encoder.cache_clear()


def test_audio_decoder_uses_background_options():
    from platinum_sorter import audio_grid
    with patch.object(audio_grid.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'\x00'*4, b'')) as run:
        assert len(audio_grid.load_mono_audio(Path('synthetic.wav'))) == 1
        assert all(run.call_args.kwargs[key] == value for key, value in background_process_options().items())
