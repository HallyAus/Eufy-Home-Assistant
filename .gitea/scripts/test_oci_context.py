import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('context', Path(__file__).with_name('prepare_oci_context.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ContextTests(unittest.TestCase):
    def test_only_source_fetch_is_replaced(self):
        original = Path('eufy_nvr/Dockerfile').read_text()
        transformed = module.local_dockerfile(original)
        before, after = original.split('ARG REPO_URL=', 1)
        remainder = after[after.index('\nWORKDIR /opt/eufy/bridge'):]
        self.assertEqual(transformed, before + 'COPY bridge /opt/eufy/bridge\n' + remainder)
        self.assertNotIn('git -C /tmp/repo fetch', transformed)

    def test_changed_fetch_contract_requires_review(self):
        original = Path('eufy_nvr/Dockerfile').read_text()
        with self.assertRaises(ValueError):
            module.local_dockerfile(original.replace('--depth 1', '--depth 2'))


if __name__ == '__main__':
    unittest.main()
