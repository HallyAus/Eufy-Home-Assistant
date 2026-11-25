"""Live camera audio for the NVR bridge.

With audio enabled (EUFY_AUDIO_CHN), the NVR interleaves audio frames with video on the
live link: command 1301, a 16-byte media header, then one ADTS AAC-LC frame (16 kHz,
mono, 1024 samples). AudioFeed writes those frames to ffmpeg's second input pipe.
"""
import os
import time

# One ADTS frame of silence in the NVR's own format (AAC-LC, 16 kHz, mono, 1024 samples).
SILENT_AAC_FRAME = bytes.fromhex("fff16040017ffc01182007")
AUDIO_RATE = 16000
AUDIO_FRAME_SAMPLES = 1024
AUDIO_HEADER_BYTES = 16


def is_adts(frame):
    return len(frame) >= 7 and frame[0] == 0xFF and (frame[1] & 0xF0) == 0xF0


class AudioFeed:
    """Feed the NVR's ADTS audio to ffmpeg on a second pipe without ever stalling video.

    ffmpeg numbers samples from its first audio frame, so audio starts with the first
    video frame (both inputs then begin at 0 together). Gaps are filled with silence
    so the audio clock keeps up with real time, and late bursts are trimmed.
    """

    def __init__(self, fd, clock=time.monotonic):
        self.fd = fd
        self.clock = clock
        self.t0 = None
        self.written = 0
        self.last_real = 0.0
        self.real_frames = 0
        self.dropped = 0

    def _write(self, data):
        try:
            os.write(self.fd, data)
            return True
        except (BlockingIOError, BrokenPipeError, OSError):
            self.dropped += 1
            return False

    def _expected(self, now):
        return int((now - self.t0) * AUDIO_RATE / AUDIO_FRAME_SAMPLES)

    def start(self):
        if self.t0 is None:
            self.t0 = self.clock()
            if self._write(SILENT_AAC_FRAME):
                self.written += 1

    def fill(self):
        """Called on every video frame: pad silence while the microphone is quiet."""
        if self.t0 is None:
            return
        now = self.clock()
        if now - self.last_real < 0.25:
            return
        target = self._expected(now) - 1
        while self.written < target and self._write(SILENT_AAC_FRAME):
            self.written += 1

    def push(self, adts):
        if self.t0 is None or not is_adts(adts):
            return False
        now = self.clock()
        self.last_real = now
        if self.written > self._expected(now) + 8:
            self.dropped += 1          # more than ~0.5 s ahead: trim to stay in sync
            return False
        if self._write(adts):
            self.written += 1
            self.real_frames += 1
            return True
        return False

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
