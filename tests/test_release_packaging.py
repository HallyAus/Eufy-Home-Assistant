import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
