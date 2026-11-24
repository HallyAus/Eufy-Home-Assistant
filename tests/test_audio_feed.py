"""Offline tests for live camera audio (bridge/audio_feed.py and its wiring)."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audio_feed", ROOT / "bridge/audio_feed.py")
audio_feed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio_feed)

# A real frame header from a T8N00 (AAC-LC, 16 kHz, mono, 193 bytes).
REAL = bytes.fromhex("fff160401821c4") + bytes(186)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def feed_with_pipe(clock):
    r, w = os.pipe()
    os.set_blocking(w, False)
    return audio_feed.AudioFeed(w, clock=clock), r


def frames(data):
    out, i = [], 0
    while i < len(data):
        n = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        out.append(data[i:i + n])
        i += n
    return out


def test_silent_frame_is_valid_adts_in_the_nvr_format():
    f = audio_feed.SILENT_AAC_FRAME
    assert audio_feed.is_adts(f)
    assert frames(f) == [f]
    assert f[2:4] == REAL[2:4]          # same profile, rate and channel layout


def test_nothing_is_written_before_the_first_video_frame():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    assert feed.push(REAL) is False
    feed.fill()
    os.set_blocking(r, False)
    try:
        assert os.read(r, 100) == b""
    except BlockingIOError:
        pass
    feed.close()
    os.close(r)


def test_start_primes_ffmpeg_then_real_audio_flows():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    feed.start()
    clock.now += 0.064
    assert feed.push(REAL)
    data = os.read(r, 1000)
    assert frames(data) == [audio_feed.SILENT_AAC_FRAME, REAL]
    feed.close()
    os.close(r)


def test_a_quiet_microphone_is_padded_with_silence_in_real_time():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    feed.start()
    clock.now += 2.0
    feed.fill()
    got = frames(os.read(r, 100000))
    expected = int(2.0 * 16000 / 1024)
    assert expected - 2 <= len(got) <= expected
    assert set(got) == {audio_feed.SILENT_AAC_FRAME}
    feed.close()
    os.close(r)


def test_no_padding_while_real_audio_arrives():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    feed.start()
    for _ in range(20):
        clock.now += 0.064
        feed.push(REAL)
        feed.fill()
    got = frames(os.read(r, 100000))
    assert got.count(audio_feed.SILENT_AAC_FRAME) == 1      # only the priming frame
    assert feed.real_frames == 20
    feed.close()
    os.close(r)


def test_a_late_burst_is_trimmed_to_stay_in_sync():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    feed.start()
    for _ in range(40):                 # 40 frames (2.5 s of audio) arrive at once
        feed.push(REAL)
    assert feed.written <= 10
    assert feed.dropped >= 30
    feed.close()
    os.close(r)


def test_a_full_or_closed_pipe_never_raises():
    clock = Clock()
    feed, r = feed_with_pipe(clock)
    os.close(r)
    feed.start()
    clock.now += 1.0
    feed.fill()
    assert feed.push(REAL) is False
    feed.close()
    feed.close()


def test_stream_wiring():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    assert "from audio_feed import AudioFeed" in stream
    assert '"audio_chn": audio_chn_for(' in stream
    assert '"-c:a", "libopus"' in stream
    assert "pass_fds=(audio_rfd,)" in stream
    # Audio frames never reach the HEVC pipe.
    assert stream.index("feed.push(payload[16:])") < stream.index("nal = payload[22:]")
    run = (ROOT / "eufy_nvr/run.sh").read_text()
    assert "EUFY_AUDIO_CHN" in run
    config = (ROOT / "eufy_nvr/config.yaml").read_text()
    assert "audio:" in config
