import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTest(unittest.TestCase):
    EUFY_API_PORT = 1985
    EUFY_RTSP_PORT = 8556
    EUFY_WEBRTC_PORT = 8557

    def test_integration_addon_and_image_ref_share_one_version(self):
        manifest_version = json.loads(
            (ROOT / "custom_components/eufy_nvr/manifest.json").read_text()
        )["version"]
        config = (ROOT / "eufy_nvr/config.yaml").read_text()
        addon_version = re.search(r'^version: "([^"]+)"$', config, re.MULTILINE).group(
            1
        )
        dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()

        self.assertEqual(addon_version, manifest_version)
        self.assertIn(f'ARG REPO_REF="v{addon_version}"', dockerfile)
        self.assertNotIn('ARG REPO_REF="main"', dockerfile)

    def test_addon_recovers_after_a_host_restart(self):
        config = (ROOT / "eufy_nvr/config.yaml").read_text()
        self.assertIn("boot: auto", config)

    def test_addon_does_not_request_unused_home_assistant_apis(self):
        config = (ROOT / "eufy_nvr/config.yaml").read_text()
        self.assertNotRegex(config, r"(?m)^(hassio_api|homeassistant_api):")

    def test_runtime_state_is_persisted_in_addon_data(self):
        run_script = (ROOT / "eufy_nvr/run.sh").read_text()
        self.assertIn('STATE_DIR="/data"', run_script)
        self.assertIn('CONFIG_PATH="${STATE_DIR}/go2rtc.yaml"', run_script)
        self.assertIn('EUFY_AUTH="${STATE_DIR}/auth.json"', run_script)
        self.assertIn('if [ -s "${EUFY_AUTH}" ]', run_script)
        self.assertNotIn('rm -f "${EUFY_AUTH}"', run_script)
        self.assertIn('EUFY_CAMERAS="${STATE_DIR}/cameras.json"', run_script)
        self.assertIn('[ -s "${EUFY_CAMERAS}" ]', run_script)

    def test_addon_uses_ports_dedicated_to_eufy(self):
        config = (ROOT / "eufy_nvr/config.yaml").read_text()
        run_script = (ROOT / "eufy_nvr/run.sh").read_text()
        dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()
        constants = (ROOT / "custom_components/eufy_nvr/const.py").read_text()

        self.assertIn(f"DEFAULT_API_PORT = {self.EUFY_API_PORT}", constants)
        self.assertIn(f"DEFAULT_RTSP_PORT = {self.EUFY_RTSP_PORT}", constants)
        self.assertIn(f"{self.EUFY_API_PORT}/tcp: {self.EUFY_API_PORT}", config)
        self.assertIn(f"{self.EUFY_RTSP_PORT}/tcp: {self.EUFY_RTSP_PORT}", config)
        self.assertIn(f"{self.EUFY_WEBRTC_PORT}/tcp: {self.EUFY_WEBRTC_PORT}", config)
        self.assertIn(f'GO2RTC_API_PORT="{self.EUFY_API_PORT}"', run_script)
        self.assertIn(f'GO2RTC_RTSP_PORT="{self.EUFY_RTSP_PORT}"', run_script)
        self.assertIn(f'GO2RTC_WEBRTC_PORT="{self.EUFY_WEBRTC_PORT}"', run_script)
        self.assertIn(f"127.0.0.1:{self.EUFY_API_PORT}/api", dockerfile)

        for occupied_port in (1984, 8554, 8555):
            self.assertNotRegex(config, rf"(?m)^\s+{occupied_port}/(?:tcp|udp):")

    def test_addon_fails_build_without_current_sctp_runtime(self):
        fetch_script = (ROOT / "bridge/fetch_deps.js").read_text()
        oracle = (ROOT / "bridge/sctp_oracle.js").read_text()
        dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()
        run_script = (ROOT / "eufy_nvr/run.sh").read_text()

        for content in (fetch_script, oracle, dockerfile, run_script):
            self.assertIn("0_0_2", content)
            self.assertNotIn("0_0_1", content)

        self.assertIn("workerFailures", fetch_script)
        self.assertIn("process.exitCode = 1", fetch_script)
        self.assertNotIn("fetch_deps.js ||", dockerfile)
        self.assertIn("node sctp_oracle.js selftest", dockerfile)

    def test_keep_warm_preserves_one_stream_per_line(self):
        run_script = (ROOT / "eufy_nvr/run.sh").read_text()

        self.assertIn("mapfile -t streams", run_script)
        self.assertIn("sed 's/^[[:space:]]*//'", run_script)
        self.assertNotIn("tr -d '[:space:]'", run_script)

    def test_token_refresh_does_not_depend_on_keep_warm(self):
        run_script = (ROOT / "eufy_nvr/run.sh").read_text()
        background_block = re.search(
            r'if \[ "\$\{BACKGROUND_TASKS_STARTED\}" -eq 0 \]; then(?P<body>.*?)\n\s*fi',
            run_script,
            re.DOTALL,
        ).group("body")

        self.assertIn("start_relogin_timer", background_block)
        self.assertLess(
            background_block.index("start_relogin_timer"),
            background_block.index('if [ "${KEEP_WARM}" = \'true\' ]; then'),
        )

    def test_signing_credentials_are_not_logged(self):
        stream_script = (ROOT / "bridge/eufy_stream.py").read_text()
        auth_script = (ROOT / "bridge/auth_login.py").read_text()

        self.assertNotIn("txt[:120]", stream_script)
        self.assertNotIn("sign_token[:20]", stream_script)
        self.assertNotIn('log("sign token:"', stream_script)
        self.assertIn('log("sign token acquired; channels", CHANNELS)', stream_script)
        self.assertIn("ws/sign rejected the current auth session", stream_script)
        self.assertIn("os.replace(temp_path, out_path)", auth_script)
        self.assertNotIn("user_id {user_id[:6]}", auth_script)
        self.assertNotIn("station_sn {station_sn}", auth_script)

    def test_known_hevc_input_skips_ffmpeg_probe_delay(self):
        stream_script = (ROOT / "bridge/eufy_stream.py").read_text()

        self.assertIn('"-probesize", "32", "-analyzeduration", "0"', stream_script)
        self.assertIn("await asyncio.sleep(0.15)", stream_script)
        self.assertNotIn("await asyncio.sleep(1.0)", stream_script)


if __name__ == "__main__":
    unittest.main()
