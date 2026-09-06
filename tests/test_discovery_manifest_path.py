import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"


class DiscoveryManifestPathTest(unittest.TestCase):
    def test_writer_and_reader_share_one_override(self):
        """eufy_stream.py (writes cameras.json) and gen_go2rtc.py (reads it) must
        resolve the same path, or discovery 'succeeds' and gen_go2rtc.py crashes."""
        writer = (BRIDGE / "eufy_stream.py").read_text()
        reader = (BRIDGE / "gen_go2rtc.py").read_text()
        for src in (writer, reader):
            self.assertIn(
                'os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json"))',
                src,
            )

    def test_run_sh_points_the_manifest_at_data(self):
        run_script = (ROOT / "eufy_nvr" / "run.sh").read_text()
        self.assertIn('EUFY_CAMERAS="${STATE_DIR}/cameras.json"', run_script)
        # The post-hoc "copy bridge/cameras.json into /data" hack is gone.
        self.assertNotIn('install -m 600 "${BRIDGE_DIR}/cameras.json"', run_script)

    def test_gen_go2rtc_reports_a_missing_manifest_cleanly(self):
        env = dict(os.environ, EUFY_CAMERAS=str(BRIDGE / "does-not-exist.json"))
        proc = subprocess.run(
            [sys.executable, "gen_go2rtc.py", "127.0.0.1"],
            cwd=BRIDGE,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("--discover", proc.stderr)


if __name__ == "__main__":
    unittest.main()
