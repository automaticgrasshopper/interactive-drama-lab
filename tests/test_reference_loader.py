import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


LOADER_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "episode_pipeline"
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

    def test_dialogue_review_bundle_includes_dialogue_craft_rules(self):
        result = loader.load_bundle("dialogue-review")
        names = {item["name"] for item in result["files"]}
        self.assertEqual(names, {"causal-episode-writing.md", "chinese-dialogue-craft.md"})
        self.assertIn("补口语组织（反过度压缩）", result["bundle"])

    def test_written_text_profile_is_conditional_and_small(self):
        base = loader.load_bundle("episode-writing")
        profiled = loader.load_bundle("episode-writing", ("written-text",))
        self.assertNotIn("written-text-to-dialogue.md", {item["name"] for item in base["files"]})
        self.assertIn("written-text-to-dialogue.md", {item["name"] for item in profiled["files"]})
        reference = next(item for item in profiled["files"] if item["name"] == "written-text-to-dialogue.md")
        self.assertLess(reference["chars"], 1400)
        for phase in ("dialogue-polish", "episode-quality-review"):
            names = {
                item["name"]
                for item in loader.load_bundle(phase, ("written-text",))["files"]
            }
            self.assertIn("written-text-to-dialogue.md", names)

    def test_written_text_reference_forbids_text_only_action_coordinates(self):
        reference = (
            Path(__file__).parents[1]
            / "backend"
            / "execution"
            / "references"
            / "written-text-to-dialogue.md"
        ).read_text(encoding="utf-8")
        self.assertIn("画内文字也不能单独充当动作坐标", reference)
        self.assertIn("若没有后续作用，直接删除", reference)
        self.assertIn("删除叙述中引号里的文字后", reference)
        self.assertIn("他把还能辨清的内容复述完", reference)

    def test_episode_quality_review_is_small_and_isolated(self):
        result = loader.load_bundle("episode-quality-review")
        self.assertEqual({item["name"] for item in result["files"]}, {"episode-quality-review.md"})
        reference = result["bundle"]
        self.assertIn("七项完整覆盖", reference)
        self.assertIn("正文 SHA-256", reference)
        self.assertLessEqual(result["files"][0]["chars"], 1200)
        self.assertGreaterEqual(result["files"][0]["chars"], 800)

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
