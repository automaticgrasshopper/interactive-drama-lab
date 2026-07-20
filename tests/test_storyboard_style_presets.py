import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "h5" / "分镜工作台.html").read_text(encoding="utf-8")
PRESETS = (ROOT / "h5" / "style-presets.js").read_text(encoding="utf-8")


class StoryboardStylePresetTests(unittest.TestCase):
    def test_smart_recommendation_and_eight_executable_presets_are_present(self):
        ids = re.findall(r"\bid:\s*'([^']+)'", PRESETS)
        self.assertEqual(
            ids,
            [
                "smart_recommendation_v1",
                "filmic_realism_v1",
                "neo_noir_v1",
                "gothic_mystery_v1",
                "retro_cinematic_film_v1",
                "neo_chinese_wuxia_v1",
                "cel_anime_v1",
                "stylized_3d_v1",
                "pixel_narrative_v1",
            ],
        )
        self.assertEqual(PRESETS.count("stylePrompt:"), 8)
        self.assertEqual(PRESETS.count("videoTemporalPrompt:"), 8)
        self.assertEqual(PRESETS.count("negativeStylePrompt:"), 8)

    def test_smart_recommendation_is_the_versioned_default(self):
        self.assertIn("DEFAULT_STYLE_ID='smart_recommendation_v1'", HTML)
        self.assertIn("function recommendStyle(text)", HTML)
        self.assertIn("generationStylePreset=chosenStyle", HTML)

    def test_manual_style_field_is_replaced_by_preset_cards(self):
        self.assertIn('id="stylegrid"', HTML)
        self.assertIn('src="style-presets.js"', HTML)
        self.assertNotIn('id="orstyle"', HTML)
        self.assertNotIn("全片画风锁点", HTML)

    def test_style_is_compiled_and_frozen_with_records(self):
        self.assertIn("compileStillPrompt", HTML)
        self.assertIn("compileVideoPrompt", HTML)
        self.assertIn("stylePreset:styleSnapshot(chosenStyle)", HTML)
        self.assertIn("stylePreset:r.stylePreset||null", HTML)
        self.assertIn("stylePreset:p.stylePreset||null", HTML)
        self.assertIn("|style:", HTML)

    def test_character_and_scene_hammers_are_isolated_by_style(self):
        self.assertIn("function hamStyleKey()", HTML)
        self.assertIn("'hammer|'+hamStyleKey()", HTML)
        self.assertIn("hamCountForStyle()", HTML)


if __name__ == "__main__":
    unittest.main()
