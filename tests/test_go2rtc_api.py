import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "eufy_nvr"
    / "go2rtc_api.py"
)
SPEC = importlib.util.spec_from_file_location("eufy_nvr_go2rtc_api", MODULE_PATH)
api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(api)


class EndpointTest(unittest.TestCase):
    def test_normalizes_host_or_url(self):
        self.assertEqual(api.normalize_host(" 192.168.1.177 "), "192.168.1.177")
        self.assertEqual(
            api.normalize_host("http://nvr-bridge.local/"), "nvr-bridge.local"
        )
        self.assertEqual(api.normalize_host("[fd00::10]"), "fd00::10")

    def test_rejects_host_with_path_or_embedded_port(self):
        for value in ("", "http://bridge.local/path", "bridge.local:1984"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.normalize_host(value)

    def test_builds_ipv4_and_ipv6_urls(self):
        self.assertEqual(
            api.api_url("192.168.1.177", 1984),
            "http://192.168.1.177:1984/api/streams",
        )
        self.assertEqual(
            api.rtsp_url("fd00::10", 8554, "eufy_front_gate"),
            "rtsp://[fd00::10]:8554/eufy_front_gate",
        )

    def test_rejects_invalid_ports(self):
        for port in (0, 65536, "1984"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                api.validate_port(port)


class StreamPayloadTest(unittest.TestCase):
    PAYLOAD = {
        "eufy_garage": {
            "producers": [{"url": "exec:secret command"}],
            "consumers": [{"format_name": "rtsp"}],
        },
        "eufy_front_gate": {"producers": [{}], "consumers": None},
        "unrelated": {"producers": [{}]},
        42: {},
    }

    def test_extracts_only_eufy_streams(self):
        streams = api.extract_streams(self.PAYLOAD)
        self.assertEqual(set(streams), {"eufy_garage", "eufy_front_gate"})

    def test_accepts_wrapped_payload(self):
        streams = api.extract_streams({"streams": self.PAYLOAD})
        self.assertEqual(set(streams), {"eufy_garage", "eufy_front_gate"})

    def test_rejects_invalid_payload(self):
        for payload in (None, [], {"streams": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                api.extract_streams(payload)

    def test_summarizes_without_exposing_producer_urls(self):
        summary = api.stream_summary(self.PAYLOAD["eufy_garage"])
        self.assertEqual(summary, {"producers": 1, "consumers": 1, "streaming": True})
        self.assertNotIn("url", repr(summary))

    def test_idle_stream_is_available_but_not_streaming(self):
        summary = api.stream_summary(self.PAYLOAD["eufy_front_gate"])
        self.assertEqual(summary, {"producers": 1, "consumers": 0, "streaming": False})

    def test_diagnostic_summary_is_sorted_and_redacted(self):
        summary = api.summarize_streams(api.extract_streams(self.PAYLOAD))
        self.assertEqual(list(summary), ["eufy_front_gate", "eufy_garage"])
        self.assertEqual(summary["eufy_garage"]["consumers"], 1)
        self.assertNotIn("secret command", repr(summary))


if __name__ == "__main__":
    unittest.main()
