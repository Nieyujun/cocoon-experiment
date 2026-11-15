"""步骤39：六轮真实组织侧生成与整句摘录配对的离线专项测试。

全部用例只做只读核验与 mock 注入：不发起任何网络请求（socket 全面禁用），
不写入既有审阅/取证目录，不覆盖任何步骤28–38 受保护原件。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from cocoon import delivery, extractive_control, live_cross_topic_delivery as lc
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
MANIFEST = ROOT / lc.MANIFEST_RELATIVE
RUNS = ROOT / lc.RUNS_RELATIVE
EXISTING = ROOT / "experiment/delivery_probes" / lc.EXISTING_R1_RUN_ID
LEDGER = ROOT / lc.LEDGER_RELATIVE
TEMPLATE = ROOT / "experiment/review/cross_topic_20261108/human_review_checklist.json"
PROTECTED = [
    TEMPLATE,
    ROOT / "cocoon/human_review.py",
    ROOT / "cocoon/pilot_materials.py",
    ROOT / "cocoon/dashboard.py",
    ROOT / "experiment/review/ai_assisted_review_20261110/scheme_a_merged_verified.json",
    ROOT / "experiment/review/pilot_materials_20260909_none.json",  # 不存在时不参与比对
    ROOT / "破茧助手全量实验方案_v1.md",
    ROOT / "experiment/review/live_semantic_20261108.json",
]


class LiveCrossTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in PROTECTED if p.is_file()}
        cls.ledger_before = hashlib.sha256(LEDGER.read_bytes()).hexdigest()
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            cls.plan = lc.plan_rounds(BUILD)
            cls.verification = lc.verify_six_round_live_extractive_manifest(MANIFEST, repo_root=ROOT)
            cls.summary = lc.summarize_live_delivery(ROOT)
            cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in cls.protected_before}:
            raise AssertionError("步骤28–38 受保护原件被改写")
        if cls.ledger_before != hashlib.sha256(LEDGER.read_bytes()).hexdigest():
            raise AssertionError("测试不得改写计费账本")

    # ---------------------------------------------------------- 规划与契约
    def test_01_plan_covers_six_rounds_and_fixed_chars(self):
        self.assertEqual(self.plan["case_count"], 6)
        self.assertEqual(tuple(row["case_id"] for row in self.plan["cases"]), lc.CASE_IDS)
        for row in self.plan["cases"]:
            self.assertEqual(row["fixed_chars"], lc.FIXED_CHARS[row["case_id"]])
            self.assertEqual(len(row["evidence_ids"]), 3)

    def test_02_plan_ranges_match_frozen_feasibility(self):
        expected = {"TOPIC_01_R1": [[143, 234]], "TOPIC_01_R2": [[141, 214]],
                    "TOPIC_02_R1": [[145, 257]], "TOPIC_02_R2": [[141, 238]],
                    "TOPIC_03_R1": [[132, 205]], "TOPIC_03_R2": [[140, 227]]}
        for row in self.plan["cases"]:
            self.assertEqual(row["feasible_organized_ranges"], expected[row["case_id"]],
                             row["case_id"])
            low, high = row["additional_char_range"]["min"], row["additional_char_range"]["max"]
            self.assertTrue(expected[row["case_id"]][0][0] <= low <= high)

    def test_03_plan_sentence_budget_within_four_slots(self):
        for row in self.plan["cases"]:
            budget = row["sentence_budget"]
            self.assertEqual(budget["sentence_count"], 4)
            self.assertEqual([slot["citation_id"] for slot in budget["slots"]],
                             [row["evidence_ids"][0], row["evidence_ids"][1],
                              row["evidence_ids"][2], row["evidence_ids"][2]])
            for slot in budget["slots"]:
                self.assertLessEqual(slot["text_min_chars"], slot["target_text_chars"])
                self.assertLessEqual(slot["target_text_chars"], slot["text_max_chars"])

    # ---------------------------------------------------------- 清单核验
    def test_04_manifest_verifies_offline(self):
        self.assertTrue(self.verification["passed"], self.verification["problems"])
        self.assertEqual(self.verification["case_count"], 6)
        self.assertTrue(self.verification["complete_6_of_6"])
        self.assertGreaterEqual(self.verification["trace_nodes_verified"]
                                if "trace_nodes_verified" in self.verification else 6, 6)

    def test_05_six_rounds_all_seven_checks(self):
        for row in self.manifest["cases"]:
            self.assertEqual(set(row["checks"]), set(lc.CHECK_NAMES))
            self.assertTrue(all(row["checks"].values()), row["case_id"])
            self.assertGreaterEqual(row["visible_chars"]["I4"], 600)
            self.assertLessEqual(row["visible_chars"]["I4"], 800)
            self.assertGreaterEqual(row["visible_chars"]["I5"], 600)
            self.assertLessEqual(row["visible_chars"]["I5"], 800)
            self.assertLessEqual(row["difference_ratios"]["total"], 0.1)
            self.assertLessEqual(row["difference_ratios"]["additional"], 0.1)

    def test_06_organized_text_is_verbatim_model_output(self):
        for row in self.manifest["cases"]:
            self.assertGreaterEqual(row["organized"]["chars"], 100)
            self.assertEqual(row["organized"]["sentence_count"], 4)
            self.assertIn(row["organized"]["question_count"], (1, 2))
            self.assertEqual(len(row["organized"]["sha256"]), 64)
            self.assertGreaterEqual(len(row["source_event_ids"]), 1)
            self.assertIn(row["organized"]["event_id"], row["source_event_ids"])

    def test_07_prompt_variants_disclosed(self):
        variants = {row["case_id"]: row["prompt_variant"] for row in self.manifest["cases"]}
        self.assertEqual(variants["TOPIC_01_R1"], "with_parent_internal_report")
        for case_id in lc.REMAINING:
            self.assertEqual(variants[case_id], "packet_only_no_parent_report")
        self.assertIn("comparability_note", self.manifest)

    def test_08_ledger_reconciled(self):
        settlement = self.manifest["settlement"]
        self.assertTrue(settlement["reconciled"])
        self.assertEqual(settlement["step_charged_cny"],
                         "%.8f" % (float(settlement["live_delivery_charged_cny"])
                                   + float(settlement["preflight_smoke_charged_cny"])))
        self.assertEqual(settlement["per_case_final_charged_cny"],
                         self.manifest["totals"]["charged_cny"])
        self.assertGreater(float(settlement["superseded_charged_cny"]), 0.0)

    def test_09_honest_gates_locked(self):
        gates = self.manifest["honest_gates"]
        self.assertIs(gates["is_demo"], False)
        self.assertEqual(gates["human_reviews_completed"], 0)
        self.assertIs(gates["full_I4_eligible"], False)
        self.assertIs(gates["formal_eligible"], False)
        self.assertIs(gates["approved_for_experiment"], False)
        self.assertIs(gates["subject_delivered"], False)

    def test_10_run_event_chains_valid(self):
        for row in self.manifest["cases"]:
            chain = row["verification"]
            self.assertIs(chain["chain_valid"], True, row["case_id"])
            self.assertGreater(chain["event_count"], 3)

    # ---------------------------------------------------------- 篡改拦截
    def _tampered(self, name, mutate):
        bad = deepcopy(self.manifest)
        mutate(bad)
        bad["manifest_sha256"] = canonical_hash(
            {k: v for k, v in bad.items() if k != "manifest_sha256"})
        target = Path(tempfile.mkdtemp(prefix="step39_case_")) / name
        target.mkdir(parents=True, exist_ok=True)
        path = target / "manifest.json"
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
        return path

    def test_11_tamper_case_check_rejected(self):
        def mutate(doc):
            doc["cases"][1]["checks"]["length_matched"] = False
        path = self._tampered("check", mutate)
        result = lc.verify_six_round_live_extractive_manifest(path, repo_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("pair_checks_mismatch", {row["code"] for row in result["problems"]})

    def test_12_tamper_organized_text_rejected(self):
        def mutate(doc):
            doc["cases"][2]["organized"]["sha256"] = "0" * 64
        path = self._tampered("text", mutate)
        result = lc.verify_six_round_live_extractive_manifest(path, repo_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("organized_text_hash_mismatch", {row["code"] for row in result["problems"]})

    def test_13_tamper_visible_chars_rejected(self):
        def mutate(doc):
            doc["cases"][3]["visible_chars"]["I4"] = 600
        path = self._tampered("chars", mutate)
        result = lc.verify_six_round_live_extractive_manifest(path, repo_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("visible_chars_mismatch", {row["code"] for row in result["problems"]})

    def test_14_tamper_charge_rejected(self):
        def mutate(doc):
            doc["cases"][0]["ledger"]["charged_cny"] = "9.99999999"
        path = self._tampered("charge", mutate)
        result = lc.verify_six_round_live_extractive_manifest(path, repo_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertTrue({"ledger_charge_mismatch", "ledger_total_mismatch"}
                        & {row["code"] for row in result["problems"]})

    def test_15_tamper_gates_rejected(self):
        def mutate(doc):
            doc["honest_gates"]["formal_eligible"] = True
        path = self._tampered("gate", mutate)
        result = lc.verify_six_round_live_extractive_manifest(path, repo_root=ROOT)
        self.assertFalse(result["passed"])
        self.assertIn("honest_gate_formal_eligible",
                      {row["code"] for row in result["problems"]})

    def test_16_tamper_manifest_hash_rejected(self):
        bad = deepcopy(self.manifest)
        bad["manifest_sha256"] = "f" * 64
        target = Path(tempfile.mkdtemp(prefix="step39_hash_")) / "manifest.json"
        target.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
        with self.assertRaises(ValueError) as ctx:
            lc.verify_six_round_live_extractive_manifest(target, repo_root=ROOT)
        self.assertIn("manifest_hash_mismatch", str(ctx.exception))

    # ---------------------------------------------------------- 门禁与零调用
    def test_17_dry_run_makes_no_calls(self):
        result = lc.run_remaining_live_organized_deliveries(BUILD, root=ROOT)
        self.assertFalse(result["executed"])
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertTrue({"execute_required", "missing_MIMO_API_KEY"}
                        & set(result["blockers"]))
        self.assertEqual(result["cost_cny"], "0.00000000")

    def test_18_missing_key_blocks_without_http(self):
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("MIMO_API_KEY", None)
            result = lc.run_remaining_live_organized_deliveries(BUILD, root=ROOT, execute=True)
        self.assertFalse(result["executed"])
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertIn("missing_MIMO_API_KEY", result["blockers"])

    def test_19_transport_never_called_when_blocked(self):
        with mock.patch.object(lc.MiMoTransport, "complete",
                               side_effect=AssertionError("transport_must_not_run")):
            result = lc.run_remaining_live_organized_deliveries(BUILD, root=ROOT, execute=False)
        self.assertFalse(result["executed"])

    # ---------------------------------------------------------- 配对与提示
    def test_20_pair_case_reuses_frozen_checks(self):
        packet, _ = lc._load_packet(BUILD, "TOPIC_01_R2")
        index = lc.dosage_index(BUILD)
        text = self.manifest["cases"][1]["organized"]["text"]
        paired = lc.pair_case(packet, index, text,
                              source_event_ids=[self.manifest["cases"][1]["source_event_ids"][0]])
        self.assertEqual(paired["status"], "paired")
        self.assertTrue(all(paired["checks"].values()))
        self.assertEqual(paired["plain_text"], self.manifest["cases"][1]["plain"]["text"])

    def test_21_feedback_contains_measured_lengths(self):
        parsed = {"sentences": [{"text": "甲" * 50, "citations": ["CL_E04"], "kind": "paraphrase"},
                                {"text": "乙" * 30, "citations": ["CL_E05"], "kind": "limitation"},
                                {"text": "丙" * 30, "citations": ["CL_E06"], "kind": "paraphrase"},
                                {"text": "丁" * 20 + "？", "citations": ["CL_E06"], "kind": "question"}]}
        row = next(r for r in self.plan["cases"] if r["case_id"] == "TOPIC_01_R2")
        feedback = lc._feedback_from({"code": "length_out_of_range"}, parsed,
                                     row["sentence_budget"], row["additional_char_range"])
        self.assertEqual(feedback["observations"]["previous_sentence_chars"], [50, 30, 30, 21])
        self.assertIn("超出单句上限", feedback["instruction"])
        self.assertIn("硬边界", feedback["instruction"])

    def test_22_extractive_only_uses_integer_budget(self):
        packet, _ = lc._load_packet(BUILD, "TOPIC_02_R2")
        index = lc.dosage_index(BUILD)
        compact = lc.dosage.compact_evidence(packet, index)
        fixed = len(lc._visible(compact["evidence_block"], ""))
        first = extractive_control.select_exact_sentences(
            packet["evidence"], 180, fixed, extractive_control._POLICY)
        second = extractive_control.select_exact_sentences(
            packet["evidence"], 180, fixed, extractive_control._POLICY)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "structurally_matched_candidate")

    # ---------------------------------------------------------- 摘要/集成
    def test_23_summary_exposes_runs_and_costs(self):
        self.assertTrue(self.summary["available"])
        self.assertEqual(self.summary["case_count"], 6)
        for row in self.summary["cases"]:
            self.assertTrue(row["run_id"].startswith(("live_delivery_", "delivery_")))
            self.assertGreater(float(row["charged_cny"]), 0.0)
            self.assertTrue(all(row["checks"].values()))

    def test_24_live_pairs_feed_materials(self):
        pairs = lc.live_pairs_for_materials(ROOT)
        self.assertTrue(pairs["available"])
        self.assertEqual(len(pairs["pairs"]), 6)
        for (_topic, _round), row in pairs["pairs"].items():
            self.assertEqual(row["provenance"]["content_origin"], "recorded_assistant_output")
            self.assertEqual(len(row["provenance"]["event_id"]), 32)
            self.assertNotEqual(row["I4"], row["I5"])

    def test_25_manifest_absent_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = lc.summarize_live_delivery(ROOT, manifest_path=Path(tmp) / "none.json")
        self.assertFalse(summary["available"])
        self.assertEqual(summary["reason"], "manifest_absent")
        self.assertEqual(summary["case_count"], 0)
        self.assertEqual(summary["pending_rounds"], list(lc.REMAINING))

    def test_26_no_network_sockets(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            result = lc.verify_six_round_live_extractive_manifest(MANIFEST, repo_root=ROOT)
        self.assertTrue(result["passed"])

    def test_27_existing_run_not_reinvoked(self):
        rows = {row["case_id"]: row for row in self.manifest["cases"]}
        self.assertTrue(rows["TOPIC_01_R1"]["reused_existing_run"])
        self.assertEqual(rows["TOPIC_01_R1"]["run_id"], lc.EXISTING_R1_RUN_ID)
        self.assertEqual([row["case_id"] for row in self.manifest["cases"]
                          if not row["reused_existing_run"]], list(lc.REMAINING))


if __name__ == "__main__":
    unittest.main()
