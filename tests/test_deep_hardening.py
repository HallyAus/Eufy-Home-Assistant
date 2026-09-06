import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeepHardeningTest(unittest.TestCase):
    def test_go2rtc_version_supports_exec_start_timeout(self):
        build = (ROOT / "eufy_nvr/build.yaml").read_text()
        dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()

        self.assertIn('GO2RTC_VERSION: "v1.9.14"', build)
        self.assertIn('ARG GO2RTC_VERSION="v1.9.14"', dockerfile)

    def test_generated_go2rtc_config_is_restricted(self):
        generator = (ROOT / "bridge/gen_go2rtc.py").read_text()

        self.assertIn("modules: [api, rtsp, webrtc, exec]", generator)
        self.assertIn("allow_paths: [python]", generator)
        self.assertIn(
            "allow_paths: [/api, /api/streams, /api/webrtc, /api/frame.jpeg]",
            generator,
        )
        self.assertIn("#starttimeout=", generator)
        self.assertIn("#killtimeout=5", generator)

    def test_stream_names_are_collision_safe(self):
        generator = (ROOT / "bridge/gen_go2rtc.py").read_text()

        self.assertIn("Counter(base)", generator)
        self.assertIn('f"{name}_ch{camera.get(\'channel\')}"', generator)
        self.assertIn("while unique in used:", generator)

    def test_config_flow_validates_both_ports_and_updates_identity(self):
        flow = (ROOT / "custom_components/eufy_nvr/config_flow.py").read_text()

        self.assertGreaterEqual(flow.count("validate_port("), 3)
        self.assertIn("vol.Range(min=1, max=65535)", flow)
        self.assertIn("unique_id=new_unique_id", flow)
        self.assertIn('return self.async_abort(reason="already_configured")', flow)

    def test_config_flow_no_longer_registers_double_reload_listener(self):
        init = (ROOT / "custom_components/eufy_nvr/__init__.py").read_text()

        self.assertNotIn("add_update_listener", init)
        self.assertNotIn("async_reload", init)

    def test_ci_runs_tests_compile_and_shell_validation(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()

        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("python -m compileall", workflow)
        self.assertRegex(workflow, r"bash -n eufy_nvr/run\.sh")


if __name__ == "__main__":
    unittest.main()
