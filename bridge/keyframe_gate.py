"""Start-of-stream handling for NVR live video (pure, no I/O).

Two things made the first seconds of every live view grey:

1. On channels whose stream is not 1920x1080 (for example 720p cameras) the
   NVR opens with a small 1920x1080 placeholder keyframe and sends the real
   keyframe about 0.4 s later. Channels at 1080p or above start directly with
   a real keyframe.
2. The bridge feeds ffmpeg with ``-probesize 32 -analyzeduration 0 -f hevc -i
   pipe:``. ffmpeg's startup probe consumes the first access unit, so the
   decoder starts on reference-less P-frames and renders grey until the next
   NVR keyframe (a GOP is about 2.5-3.5 s).

``KeyframeGate`` drops only the placeholder and writes the first real keyframe
twice, so the probe consumes one copy and the decoder gets the other. Video is
then real from the first frame instead of after the next GOP.
"""
import time

# HEVC NAL unit types that start a decodable picture (IRAP) or carry its
# parameter sets (VPS/SPS/PPS).
_KEY_TYPES = frozenset((16, 17, 18, 19, 20, 21, 32, 33, 34))

# The placeholder is a 1920x1080 keyframe far smaller than any real 1080p one
# (about 19 KB, against 100-300 KB for real 1080p+ keyframes).
PLACEHOLDER_SIZE = (1920, 1080)
PLACEHOLDER_MAX_BYTES = 60000
# How long to hold a suspected placeholder waiting for the real keyframe.
PLACEHOLDER_WAIT_S = 2.0


def is_keyframe(nal: bytes) -> bool:
    """True when the Annex-B buffer starts with a parameter set or IRAP NAL."""
    i = nal.find(b"\x00\x00\x01")
    if i < 0 or i + 3 >= len(nal):
        return False
    return ((nal[i + 3] >> 1) & 0x3F) in _KEY_TYPES


def frame_size(media_header: bytes):
    """(width, height) from the 22-byte NVR media header, or None."""
    if len(media_header) < 14:
        return None
    return (int.from_bytes(media_header[10:12], "little"),
            int.from_bytes(media_header[12:14], "little"))


class KeyframeGate:
    """Decide which NAL buffers to pass to ffmpeg at the start of a stream."""

    def __init__(self, duplicate_first=True, clock=time.monotonic, log=None):
        self.duplicate_first = duplicate_first
        self._clock = clock
        self._log = log or (lambda *_: None)
        self._open = False
        self._held = []
        self._held_at = 0.0
        self.dropped = 0

    @property
    def open(self) -> bool:
        return self._open

    def _first(self, nal):
        return [nal, nal] if self.duplicate_first else [nal]

    def push(self, nal: bytes, size=None) -> list:
        """Return the buffers to write now for one incoming NAL buffer.

        ``size`` is the (width, height) from the media header when known.
        """
        if self._open:
            return [nal]
        key = is_keyframe(nal)
        if not self._held:
            if not key:
                # Nothing decodable yet: frames before the first keyframe only
                # produce grey output.
                self.dropped += 1
                return []
            if size == PLACEHOLDER_SIZE and len(nal) < PLACEHOLDER_MAX_BYTES:
                self._held = [nal]
                self._held_at = self._clock()
                return []
            self._open = True
            self._log(f"KEYFRAME_GATE started on first keyframe ({len(nal)} B)")
            return self._first(nal)
        if key:
            self.dropped += len(self._held)
            self._held = []
            self._open = True
            self._log(f"KEYFRAME_GATE skipped {self.dropped} placeholder frame(s)")
            return self._first(nal)
        self._held.append(nal)
        if self._clock() - self._held_at > PLACEHOLDER_WAIT_S:
            out, self._held, self._open = self._held, [], True
            self._log(f"KEYFRAME_GATE no second keyframe in {PLACEHOLDER_WAIT_S:g} s; "
                      f"released {len(out)} frame(s)")
            return (self._first(out[0]) + out[1:]) if out else []
        return []
