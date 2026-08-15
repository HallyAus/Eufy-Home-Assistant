import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

import eufy_cloud as ec  # noqa: E402


class RegionEndpointsTest(unittest.TestCase):
    def test_normalizes_addon_region_values(self):
        self.assertEqual(ec.normalize_region("US"), "us-pr")
        self.assertEqual(ec.normalize_region(" eu "), "eu-pr")
        self.assertEqual(ec.normalize_region("ie-pr"), "ie-pr")

    def test_selects_each_smart_service_host(self):
        expected_hosts = {
            "US": "security-smart.eufylife.com",
            "EU": "security-smart-eu.eufylife.com",
            "IE": "security-smart-ie.eufylife.com",
        }
        for region, host in expected_hosts.items():
            with self.subTest(region=region):
                ws_url, sign_url = ec.smart_urls("T8N00TEST", region)
                self.assertEqual(
                    ws_url,
                    f"wss://{host}/v1/rtc/ws/join?reqtype=nvr",
                )
                self.assertEqual(
                    sign_url,
                    f"https://{host}/v1/smart/nvr/ws/sign?station_sn=T8N00TEST",
                )

    def test_unknown_region_keeps_existing_us_fallback(self):
        ws_url, sign_url = ec.smart_urls("T8N00TEST", "unknown")
        self.assertTrue(ws_url.startswith("wss://security-smart.eufylife.com/"))
        self.assertTrue(sign_url.startswith("https://security-smart.eufylife.com/"))


if __name__ == "__main__":
    unittest.main()
