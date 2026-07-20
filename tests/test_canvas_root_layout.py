import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "episode-generator"
    / "scripts"
)


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cache_paths = load_module("cache_paths")


class CanvasRootLayoutTests(unittest.TestCase):
    def test_shared_canvas_root_resolves_manifest_selected_private_cache(self):
        workspace = Path(tempfile.mkdtemp())
        canvas = workspace / "canvas"
        cache = workspace / ".episode-generator-cache" / "project-a"
        canvas.mkdir()
        cache.mkdir(parents=True)
        (cache / "manifest.md").write_text(
            f"# manifest\n\n## 公开 Canvas\n{canvas}\n\n## 缓存版本\nepisode-cache-v0.1.24\n",
            encoding="utf-8",
        )
        self.assertEqual(cache_paths.cache_root_for(canvas), cache.resolve())

    def test_shared_canvas_allows_unrelated_files_but_rejects_legacy_structure(self):
        import sys

        sys.path.insert(0, str(SCRIPTS))
        try:
            validator = load_module("validate_episode_cache")
        finally:
            sys.path.pop(0)
        canvas = Path(tempfile.mkdtemp())
        (canvas / "other-module.md").write_text("# other", encoding="utf-8")
        errors = []
        validator.validate_shared_canvas_root(canvas, errors)
        self.assertEqual(errors, [])
        (canvas / "episode-structure.md").write_text("legacy", encoding="utf-8")
        validator.validate_shared_canvas_root(canvas, errors)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
