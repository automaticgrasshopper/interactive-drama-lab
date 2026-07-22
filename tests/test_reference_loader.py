import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


LOADER_PATH = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "episode-generator"
    / "scripts"
    / "load_reference_bundle.py"
)
SPEC = importlib.util.spec_from_file_location("load_reference_bundle", LOADER_PATH)
loader = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(loader)


class ReferenceLoaderTests(unittest.TestCase):
    def test_phase_bundle_contains_every_manifest_reference(self):
        result = loader.load_bundle("episode-writing", ("suspense", "confrontation"))
        names = {item["name"] for item in result["files"]}
        self.assertEqual(names, {"causal-episode-writing.md", "chinese-dialogue-craft.md"})
        self.assertIn(result["receipt_sha256"], result["bundle"])

    def test_receipt_round_trip_and_tamper_rejection(self):
        directory = Path(tempfile.mkdtemp())
        result = loader.load_bundle("topology")
        path = loader.write_receipt(directory, result)
        self.assertEqual(loader.verify_receipts(directory, ("topology",)), [])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["receipt_sha256"] = "0" * 64
        path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(loader.verify_receipts(directory, ("topology",)))


if __name__ == "__main__":
    unittest.main()
