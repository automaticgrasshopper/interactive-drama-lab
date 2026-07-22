import json
import unittest
from pathlib import Path

from backend.production_worker import ProductionManager, action_skeleton, current_episode_skill_version, dialogue_review_packet, normalize_scene_heading, public_error_message, script_digest


def valid_script(dialogue: str = "这件事我现在就去办。") -> str:
    actions = "\n".join(f"△ 他完成第{i}个清晰可拍的动作，并让现场局势继续发生真实变化。" for i in range(1, 17))
    padding = "局势没有停在解释上，人物立刻根据眼前结果调整下一步行动。" * 18
    return f"【场一 · 办公室 · 日 · 内】\n出场：甲\n{actions}\n甲：{dialogue}\n△ {padding}"


class FakeManager(ProductionManager):
    def __init__(self):
        self.review_calls = []
        self.repair_calls = []

    def _check_stop(self, project_id, run_id):
        return None

    def _log(self, project_id, run_id, message):
        return None

    def _review(self, project_id, run_id, kind, data, node, predecessors, topology, digest):
        self.review_calls.append(kind)
        return {"name": kind, "pass": True, "issues": [], "note": "通过", "digest": digest}

    def _repair(self, project_id, run_id, data, node, predecessors, topology, audit, layer):
        self.repair_calls.append(layer)
        node["script"] = valid_script()


class DialogueManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.dialogue_round = 0

    def _review(self, project_id, run_id, kind, data, node, predecessors, topology, digest):
        self.review_calls.append(kind)
        if kind == "dialogue":
            self.dialogue_round += 1
            return {"name": kind, "pass": self.dialogue_round > 1, "issues": [] if self.dialogue_round > 1 else ["【场一】台词生硬"], "note": "", "digest": digest}
        return {"name": kind, "pass": True, "issues": [], "note": "通过", "digest": digest}

    def _repair(self, project_id, run_id, data, node, predecessors, topology, audit, layer):
        self.repair_calls.append(layer)
        before = node["script"]
        node["script"] = before.replace("这件事我现在就去办。", "行，我现在去办。")


class ParseFailManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.chat_calls = 0

    def _chat(self, project_id, run_id, messages, **kwargs):
        self.chat_calls += 1
        return '{"broken":'


class RecordingRuns:
    def __init__(self):
        self.checkpoints = []

    def update(self, project_id, run_id, **changes):
        expanded = [node["id"] for node in changes["result_data"]["nodes"] if node.get("production_card_expanded")]
        self.checkpoints.append(expanded)


class CardBatchManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.runs = RecordingRuns()
        self.batches = []

    def _json_chat(self, project_id, run_id, messages, **kwargs):
        payload = __import__("json").loads(messages[-1]["content"].split("\n返回：", 1)[0])
        ids = [node["id"] for node in payload["nodes"]]
        self.batches.append(ids)
        return {"cards": [{"id": node_id, "production_card": {"episode_goal": node_id}} for node_id in ids]}


class ReviewCaptureManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.system_messages = []

    def _json_chat(self, project_id, run_id, messages, **kwargs):
        self.system_messages.append(messages[0]["content"])
        return {"covered_checks": ["来源与权限", "首次出场与感知", "行动阻力结果", "物件路径", "后续入口"], "issues": [], "evidence": [], "note": "", "owner": "scene_facts"}


class SelfSigningReviewManager(ReviewCaptureManager):
    def _json_chat(self, project_id, run_id, messages, **kwargs):
        return {"pass": True, "issues": [], "evidence": [], "note": "模型自称通过", "owner": "scene_facts"}


class DialogueReviewCaptureManager(FakeManager):
    def __init__(self, covered_checks):
        super().__init__()
        self.covered_checks = covered_checks
        self.messages = []

    def _json_chat(self, project_id, run_id, messages, **kwargs):
        self.messages = messages
        return {
            "covered_checks": self.covered_checks,
            "issues": [],
            "note": "",
            "owner": "character_exchange",
        }


class DialogueRepairCaptureManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.messages = []

    def _json_chat(self, project_id, run_id, messages, **kwargs):
        self.messages = messages
        return {"script": valid_script("你还能走吗？")}


class QualityReviewCaptureManager(FakeManager):
    def __init__(self, covered_checks=None, issues=None):
        super().__init__()
        self.covered_checks = covered_checks or [
            "事实来源与知情",
            "首次出现与必要交代",
            "因果相邻与可见后果",
            "话茬与现场目的",
            "口语组织与反过度压缩",
            "普通话表达与叙述可读",
            "结尾画面与下一入口",
        ]
        self.issues = issues or []
        self.kwargs = {}

    def _json_chat(self, project_id, run_id, messages, **kwargs):
        self.kwargs = kwargs
        return {
            "covered_checks": self.covered_checks,
            "issues": self.issues,
            "evidence": [{
                "check": "普通话表达与叙述可读",
                "dialogue_location": "甲：你还能走吗？",
                "dialogue_proof": "台词采用自然普通话语序并承接现场。",
                "narration_location": "他走到门边",
                "narration_proof": "描述写清人物动作与位置变化。",
            }],
            "note": "",
        }


class ProductionWorkerTests(unittest.TestCase):
    def test_backend_reference_loader_issues_private_receipt(self):
        manager = object.__new__(ProductionManager)
        manager._reference_receipts = {}
        bundle = manager._load_reference_context("p", "r", "episode-writing")
        self.assertIn("episode-generator-reference-bundle", bundle)
        self.assertIn("episode-writing", manager._reference_receipts[("p", "r")])

    def test_backend_tracks_current_reference_manifest_version(self):
        manifest = json.loads((Path(__file__).parents[1] / "skill" / "episode-generator" / "reference-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(current_episode_skill_version(), manifest["skill_version"])

    def test_completion_gate_rejects_missing_reference_phase(self):
        manager = object.__new__(ProductionManager)
        manager._reference_receipts = {("p", "r"): {"topology": "digest"}}
        with self.assertRaisesRegex(RuntimeError, "装载门禁"):
            manager._assert_reference_receipts("p", "r")

    def test_action_skeleton_ignores_dialogue_only(self):
        before = valid_script("这件事我现在就去办。")
        after = before.replace("这件事我现在就去办。", "行，我现在去办。")
        self.assertEqual(action_skeleton(before), action_skeleton(after))
        self.assertNotEqual(script_digest(before), script_digest(after))

    def test_dialogue_packet_covers_every_real_line(self):
        packet = dialogue_review_packet(valid_script("你看见了吗？\n乙：没有。"))
        self.assertEqual(packet["dialogue_count"], 2)
        self.assertEqual(packet["coverage"], "COMPLETE")
        self.assertEqual(packet["script_sha256"], script_digest(valid_script("你看见了吗？\n乙：没有。")))

    def test_choice_results_and_merge_are_one_atomic_group(self):
        data = {"nodes": [
            {"id": "1", "kind": "normal", "next": [{"to": "2"}]},
            {"id": "2", "kind": "choice", "next": [{"to": "3A"}, {"to": "3B"}]},
            {"id": "3A", "kind": "normal", "next": [{"to": "4"}]},
            {"id": "3B", "kind": "normal", "next": [{"to": "4"}]},
            {"id": "4", "kind": "normal", "next": [{"to": "E"}]},
            {"id": "E", "kind": "major", "next": []},
        ]}
        groups = ProductionManager._execution_groups(data)
        atomic = next(group for group in groups if "2" in group["nodes"])
        self.assertEqual(set(atomic["nodes"]), {"2", "3A", "3B", "4"})
        self.assertEqual(atomic["type"], "互动组")

    def test_mechanical_failure_does_not_run_semantic_review(self):
        manager = FakeManager()
        node = {"id": "1", "script": "太短"}
        manager._ensure_layer("p", "r", {"nodes": [node]}, node, {"1": []}, [], "mechanical")
        self.assertEqual(manager.review_calls, [])
        self.assertEqual(manager.repair_calls, ["mechanical"])

    def test_markdown_heading_is_normalized_without_model_repair(self):
        manager = FakeManager()
        script = valid_script().replace("【场一 · 办公室 · 日 · 内】\n出场：甲", "**1-1 开门｜办公室｜日·内**", 1)
        node = {"id": "1-1", "loc": "办公室", "time": "日 内", "cast": "甲", "script": script}
        manager._ensure_layer("p", "r", {"nodes": [node]}, node, {"1-1": []}, [], "mechanical")
        self.assertTrue(node["script"].startswith("【场一 · 办公室 · 日 · 内】\n出场：甲"))
        self.assertEqual(manager.repair_calls, [])

    def test_scene_heading_normalizer_is_idempotent(self):
        node = {"loc": "办公室", "time": "日 内", "cast": "甲", "script": valid_script()}
        self.assertFalse(normalize_scene_heading(node))

    def test_dialogue_repair_reruns_scene_facts_on_new_digest(self):
        manager = DialogueManager()
        node = {"id": "1", "script": valid_script(), "episode_audit": {"reviews": []}}
        manager._ensure_layer("p", "r", {"nodes": [node]}, node, {"1": []}, [], "dialogue")
        reviews = manager._audit_map(node)
        self.assertEqual(manager.repair_calls, ["dialogue"])
        self.assertEqual(reviews["causal"]["digest"], script_digest(node["script"]))
        self.assertEqual(manager.review_calls, ["dialogue", "causal", "dialogue"])

    def test_locked_output_contains_nine_field_contract_details(self):
        data = {
            "props": [{"name": "红色钥匙", "appears": "1"}],
            "nodes": [{"id": "1", "node_title": "开门", "kind": "normal", "cast": "甲", "loc": "门厅", "text": "梗概", "script": "甲拿起红色钥匙", "next": [{"to": "2"}]}],
        }
        ProductionManager._sync_episodes(data, {"1": []})
        episode = data["episodes"][0]
        self.assertEqual(episode["关联道具"], ["红色钥匙"])
        self.assertEqual(episode["互动节点"]["默认下一分集编号"], "2")

    def test_invalid_json_stops_after_three_transport_attempts(self):
        manager = ParseFailManager()
        with self.assertRaisesRegex(RuntimeError, "连续三次"):
            manager._json_chat(
                "p",
                "r",
                [{"role": "user", "content": "test"}],
                reference_phase="episode-writing",
            )
        self.assertEqual(manager.chat_calls, 3)

    def test_large_atomic_group_expands_one_card_per_persisted_task(self):
        manager = CardBatchManager()
        nodes = [{"id": str(index), "production_card": {"compact": True}, "next": []} for index in range(1, 8)]
        data = {"nodes": nodes, "logline": "测试"}
        manager._expand_group_cards("p", "r", data, nodes, {node["id"]: [] for node in nodes})
        self.assertEqual(manager.batches, [["1"], ["2"], ["3"], ["4"], ["5"], ["6"], ["7"]])
        self.assertEqual(manager.runs.checkpoints[-1], ["1", "2", "3", "4", "5", "6", "7"])
        self.assertTrue(all(node.get("production_card_expanded") for node in nodes))

    def test_passed_layer_with_same_digest_is_not_repeated(self):
        manager = FakeManager()
        script = valid_script()
        digest = script_digest(script)
        node = {"id": "1", "script": script, "episode_audit": {"digest": digest, "reviews": [{"name": "causal", "pass": True, "digest": digest}]}}
        manager._ensure_layer("p", "r", {"nodes": [node]}, node, {"1": []}, [], "causal")
        self.assertEqual(manager.review_calls, [])

    def test_freeze_reconciles_stale_review_digests_after_resume(self):
        manager = FakeManager()
        script = valid_script()
        digest = script_digest(script)
        node = {
            "id": "1",
            "script": script,
            "episode_audit": {
                "digest": digest,
                "locked": False,
                "reviews": [
                    {"name": "mechanical", "pass": True, "digest": digest},
                    {"name": "causal", "pass": True, "digest": "stale"},
                    {"name": "dialogue", "pass": True, "digest": "stale"},
                    {"name": "cold", "pass": True, "digest": digest},
                ],
            },
        }
        manager._ensure_current_reviews("p", "r", {"nodes": [node]}, node, {"1": []}, [])
        reviews = manager._audit_map(node)
        self.assertEqual(manager.review_calls, ["causal", "dialogue"])
        self.assertTrue(all(reviews[name]["digest"] == digest for name in ("mechanical", "causal", "dialogue", "cold")))

    def test_public_error_hides_provider_payload_but_keeps_status(self):
        message = public_error_message('OpenRouter 请求失败：429 {"error":{"message":"secret provider payload"}}')
        self.assertEqual(message, "OpenRouter 请求失败（HTTP 429）")

    def test_scene_review_initializes_its_system_instruction(self):
        manager = ReviewCaptureManager()
        node = {"id": "1", "script": valid_script(), "production_card": {}, "next": []}
        review = ProductionManager._review(manager, "p", "r", "causal", {"nodes": [node]}, node, {"1": []}, [], script_digest(node["script"]))
        self.assertTrue(review["pass"])
        self.assertIn("场面事实复核员", manager.system_messages[0])

    def test_backend_ignores_model_self_signed_pass_without_coverage(self):
        manager = SelfSigningReviewManager()
        node = {"id": "1", "script": valid_script(), "production_card": {}, "next": []}
        review = ProductionManager._review(manager, "p", "r", "causal", {"nodes": [node]}, node, {"1": []}, [], script_digest(node["script"]))
        self.assertFalse(review["pass"])
        self.assertTrue(any("覆盖不完整" in issue for issue in review["issues"]))

    def test_dialogue_review_requires_oral_organization_and_anti_compression(self):
        legacy_checks = ["话茬承接", "单句信息负载", "现场目的", "人物声音", "口语自然度"]
        manager = DialogueReviewCaptureManager(legacy_checks)
        node = {"id": "1", "script": valid_script("你还能走吗？"), "relationship_state": "同伴"}
        review = ProductionManager._review(manager, "p", "r", "dialogue", {"nodes": [node]}, node, {"1": []}, [], script_digest(node["script"]))
        self.assertFalse(review["pass"])
        self.assertTrue(any("口语组织、反过度压缩" in issue for issue in review["issues"]))
        self.assertIn("不得仅因句子短而判错", manager.messages[0]["content"])

    def test_dialogue_review_passes_only_after_all_seven_checks_are_covered(self):
        checks = ["话茬承接", "单句信息负载", "现场目的", "人物声音", "口语自然度", "口语组织", "反过度压缩"]
        manager = DialogueReviewCaptureManager(checks)
        node = {"id": "1", "script": valid_script("你还能走吗？"), "relationship_state": "同伴"}
        review = ProductionManager._review(manager, "p", "r", "dialogue", {"nodes": [node]}, node, {"1": []}, [], script_digest(node["script"]))
        self.assertTrue(review["pass"])
        self.assertEqual(review["required_checks"], checks)

    def test_dialogue_repair_prompt_preserves_functional_spoken_markers(self):
        manager = DialogueRepairCaptureManager()
        node = {"id": "1", "script": valid_script("你还能走吗？")}
        ProductionManager._repair(manager, "p", "r", {"nodes": [node]}, node, {"1": []}, [], {"issues": ["S01-L01｜反过度压缩｜关系指向不足"]}, "dialogue")
        system = manager.messages[0]["content"]
        self.assertIn("不得默认追求最短表达", system)
        self.assertIn("承担对象、承接、态度或关系功能的口语成分不得删除", system)
        self.assertIn("不得为显得口语而强行加口水", system)

    def test_independent_quality_review_uses_its_own_reference_and_digest(self):
        manager = QualityReviewCaptureManager()
        node = {"id": "episode-001", "script": valid_script(), "next": []}
        data = {"nodes": [node], "characters": [], "scenes": [], "props": []}
        review = ProductionManager._episode_quality_review(manager, "p", "r", data, node, {"episode-001": []}, [])
        self.assertTrue(review["pass"])
        self.assertEqual(review["digest"], script_digest(node["script"]))
        self.assertEqual(manager.kwargs["reference_phase"], "episode-quality-review")

    def test_independent_quality_review_rejects_incomplete_coverage(self):
        manager = QualityReviewCaptureManager(covered_checks=["事实来源与知情"])
        node = {"id": "episode-001", "script": valid_script(), "next": []}
        data = {"nodes": [node], "characters": [], "scenes": [], "props": []}
        review = ProductionManager._episode_quality_review(manager, "p", "r", data, node, {"episode-001": []}, [])
        self.assertFalse(review["pass"])
        self.assertTrue(any("复检覆盖不完整" in issue for issue in review["issues"]))


if __name__ == "__main__":
    unittest.main()
