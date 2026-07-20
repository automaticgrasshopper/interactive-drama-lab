import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "episode-generator"
    / "scripts"
    / "prepare_dialogue_review.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_dialogue_review", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


OLD_V18_SAMPLE = """【场一 · 研究所 · 夜 · 内】
出场：沈砚、贺兰舟
沈砚：刚才那一轮，你也看见了吧？
△ 贺兰舟按下主控台的封存键。
贺兰舟：先停外传。今晚的记录不要发出去。
沈砚：设备没重启，原始数据也还在。
贺兰舟：所以更不能发。
△ 沈砚取出裂纹怀表。
沈砚：这不是研究所的东西。
贺兰舟：给我。
沈砚：你认识它？
贺兰舟：我认识失控的东西。先放下。"""


class DialogueReviewPacketTests(unittest.TestCase):
    def test_old_v18_sample_is_numbered_in_full_scene_without_duplication(self):
        packet = MODULE.build_dialogue_packet(OLD_V18_SAMPLE)
        self.assertEqual(packet["coverage"], "COMPLETE")
        self.assertEqual(packet["dialogue_count"], 8)
        self.assertEqual(packet["dialogue_ids"], [f"S01-L{i:02d}" for i in range(1, 9)])
        self.assertEqual(packet["numbered_script"].count("[S01-L"), 8)
        self.assertIn("△ 贺兰舟按下主控台的封存键。", packet["numbered_script"])

    def test_turn_candidates_link_questions_and_replies(self):
        packet = MODULE.build_dialogue_packet(OLD_V18_SAMPLE)
        candidates = {item["id"]: item for item in packet["turn_candidates"]}
        self.assertEqual(candidates["S01-L01"]["next"], "S01-L02")
        self.assertIn("question", candidates["S01-L01"]["signals"])
        self.assertIn("request_or_command", candidates["S01-L02"]["signals"])
        self.assertIn("causal_or_turn_connector", candidates["S01-L04"]["signals"])
        self.assertEqual(candidates["S01-L07"]["next"], "S01-L08")

    def test_dialogue_hash_changes_when_only_one_line_changes(self):
        before = MODULE.build_dialogue_packet(OLD_V18_SAMPLE)
        after = MODULE.build_dialogue_packet(OLD_V18_SAMPLE.replace("给我。", "先给我。"))
        self.assertNotEqual(before["dialogue_sha256"], after["dialogue_sha256"])
        self.assertEqual(before["dialogue_count"], after["dialogue_count"])
