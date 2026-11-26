"""Offline tests for the start-of-stream keyframe gate."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("keyframe_gate", ROOT / "bridge/keyframe_gate.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


def nal(nal_type, size=100):
    head = b"\x00\x00\x00\x01" + bytes([(nal_type << 1) & 0x7E, 0x01])
    return head + b"\x00" * max(0, size - len(head))


VPS_IDR = lambda size=100: nal(32, size)   # parameter set + IDR access unit
P = lambda: nal(1)                          # trailing (P) picture


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def header(w, h):
    hdr = bytearray(22)
    hdr[10:12] = w.to_bytes(2, "little")
    hdr[12:14] = h.to_bytes(2, "little")
    return bytes(hdr)


def test_keyframe_detection():
    assert kg.is_keyframe(nal(32)) and kg.is_keyframe(nal(19)) and kg.is_keyframe(nal(20))
    assert not kg.is_keyframe(nal(1)) and not kg.is_keyframe(nal(39))
    assert not kg.is_keyframe(b"") and not kg.is_keyframe(b"\x00\x00\x01")


def test_frame_size_from_media_header():
    assert kg.frame_size(header(1280, 720)) == (1280, 720)
    assert kg.frame_size(b"\x00" * 5) is None


def test_real_first_keyframe_starts_immediately_and_is_doubled():
    g = kg.KeyframeGate()
    key = VPS_IDR(150000)
    assert g.push(key, (1920, 1080)) == [key, key]
    p = P()
    assert g.push(p, (1920, 1080)) == [p]


def test_small_first_keyframe_at_other_resolution_is_real():
    g = kg.KeyframeGate()
    key = VPS_IDR(9800)
    assert g.push(key, (1280, 720)) == [key, key]


def test_frames_before_first_keyframe_are_dropped():
    g = kg.KeyframeGate()
    assert g.push(P(), (1280, 720)) == []
    key = VPS_IDR()
    assert g.push(key, (1280, 720)) == [key, key]
    assert g.dropped == 1


def test_placeholder_is_skipped_for_the_real_keyframe():
    clock = Clock()
    g = kg.KeyframeGate(clock=clock)
    assert g.push(VPS_IDR(19000), (1920, 1080)) == []
    clock.t = 0.2
    assert g.push(P(), (1920, 1080)) == []
    clock.t = 0.4
    real = VPS_IDR(9800)
    assert g.push(real, (1280, 720)) == [real, real]
    assert g.open and g.dropped == 2


def test_placeholder_is_released_if_no_second_keyframe_arrives():
    clock = Clock()
    g = kg.KeyframeGate(clock=clock)
    first = VPS_IDR(19000)
    assert g.push(first, (1920, 1080)) == []
    p1 = P()
    assert g.push(p1, (1920, 1080)) == []
    clock.t = kg.PLACEHOLDER_WAIT_S + 0.1
    p2 = P()
    assert g.push(p2, (1920, 1080)) == [first, first, p1, p2]
    assert g.open


def test_duplicate_can_be_disabled():
    g = kg.KeyframeGate(duplicate_first=False)
    key = VPS_IDR()
    assert g.push(key, (1280, 720)) == [key]


def test_bridge_uses_the_gate():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    assert "import keyframe_gate" in stream
    assert "io[\"gate\"].push(nal, keyframe_gate.frame_size(payload[:22]))" in stream
