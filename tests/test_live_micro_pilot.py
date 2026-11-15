"""步骤40：TOPIC_01 真实微型实证的离线专项测试。

只做离线重放核验、篡改拦截与零调用门禁：socket 全面禁用，不发起网络请求，
不改写账本、空白人审模板与步骤28–39 受保护原件。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cocoon import live_micro_pilot as lm
from cocoon import budget as lm_budget
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / lm.MANIFEST_RELATIVE
LEDGER = ROOT / lm.LEDGER_RELATIVE
PROTECTED = [
    ROOT / "experiment/review/cross_topic_20261108/human_review_checklist.json",
    ROOT / "experiment/review/live_cross_topic_20261111/six_round_live_extractive_pairs.json",
    ROOT / "experiment/review/live_cross_topic_20261111/REPORT_step39_20261111.md",
    ROOT / "cocoon/human_review.py",
    ROOT / "cocoon/measurement.py",
    ROOT / "cocoon/contracts.py",
    ROOT / "破茧助手全量实验方案_v1.md",
]


def _no_network():
    return (mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")),
            mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")))


class OfflineContractTests(unittest.TestCase):
    """不依赖真实运行产物：设计、展开规则、校验器与零调用门禁。"""
    @classmethod
    def setUpClass(cls):
        first, second = _no_network()
        with first, second:
            cls.materials = lm.load_materials()

    def test_01_materials_design(self):
        self.assertEqual(self.materials["stances"]["S01"], 1)
        self.assertEqual(self.materials["stances"]["S05"], -1)
        for subject in lm.SUBJECTS:
            exposure = self.materials["schedules"][subject]["exposure"]
            self.assertEqual(len(exposure), 8)
            self.assertTrue(all(len(batch) == 6 for batch in exposure))
            self.assertEqual(len(self.materials["schedules"][subject]["retention"]), 4)
        for arm in lm.ARMS:
            for round_no in lm.INTERVENTION_ROUNDS:
                self.assertTrue(600 <= self.materials["interventions"][arm][round_no]["char_count"] <= 800)
        self.assertEqual(self.materials["interventions"]["I4"][1]["content_origin"], "live_model_organized_output")

    def test_02_exposure_and_retention_pools_disjoint(self):
        for subject in lm.SUBJECTS:
            exp = {c["card_id"] for b in self.materials["schedules"][subject]["exposure"] for c in b}
            ret = {c["card_id"] for b in self.materials["schedules"][subject]["retention"] for c in b}
            self.assertFalse(exp & ret)
            for batch in self.materials["schedules"][subject]["exposure"]:
                for card in batch:
                    self.assertNotIn("stance", card)
                    self.assertNotIn("is_accurate", card)

    def test_03_flatten_is_deterministic_two_messages(self):
        messages = [{"role": "system", "content": "甲"}, {"role": "user", "content": "乙"},
                    {"role": "assistant", "content": "丙"}, {"role": "system", "content": "丁"},
                    {"role": "user", "content": "戊"}]
        flat = lm._flatten(messages)
        self.assertEqual([m["role"] for m in flat], ["system", "user"])
        self.assertEqual(flat[0]["content"], "甲\n\n丁")
        self.assertIn("【你读到的内容】\n乙", flat[1]["content"])
        self.assertIn("【你自己发表过的话】\n丙", flat[1]["content"])
        self.assertTrue(flat[1]["content"].endswith("【当前任务】\n戊"))
        self.assertEqual(flat, lm._flatten(deepcopy(messages)))

    def test_04_single_line_rendering(self):
        self.assertEqual(lm._single_line("甲\n\n乙\n丙"), "甲 ｜ 乙 ｜ 丙")

    def test_19_execute_required_zero_calls(self):
        with mock.patch.object(lm.MiMoTransport, "complete", side_effect=AssertionError("no_call")):
            result = lm.run_topic01_live_micro_pilot(execute=False)
        self.assertFalse(result["executed"])
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertTrue({"execute_required", "missing_MIMO_API_KEY"} & set(result["blockers"]))

    def test_20_missing_key_blocks(self):
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("MIMO_API_KEY", None)
            result = lm.run_topic01_live_micro_pilot(execute=True)
        self.assertFalse(result["executed"])
        self.assertIn("missing_MIMO_API_KEY", result["blockers"])

    def test_22_summary_absent_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = lm.summarize_live_micro_pilot(manifest_path=Path(tmp) / "none.json")
        self.assertFalse(summary["available"])
        self.assertEqual(summary["reason"], "manifest_absent")

    def test_24_validators_reject_bad_text(self):
        check = lm._plain_validator(*lm.RESPONSE_RANGE)
        self.assertIsNotNone(check("太短")[1])
        self.assertIsNotNone(check("包含换行\n" + "测" * 30)[1])
        self.assertIsNotNone(check("包含英文ABC" + "测" * 30)[1])
        self.assertIsNone(check("测" * 40)[1])

    def test_25_measure_validator_rejects_unseen_card(self):
        q = self.materials["questionnaire"]
        answers = [{"item_id": i["item_id"], "p_true": 0.5, "brief_reason": "依据材料。",
                    "cited_card_ids": ["T01EXP_P_A_01"]} for i in q["anchor_items"] + q["transfer_items"]]
        raw = json.dumps({"answers": answers,
                          "evidence_notes": [{"known_evidence": "已知内容。", "remaining_question": "待查内容。"}],
                          "opposing_view": {"summary": "甲。", "main_evidence": "乙。", "boundary": "丙。"}},
                         ensure_ascii=False)
        value, error, diag = lm._measure_validator(self.materials, [], "T0")(raw)
        self.assertIsNone(value)
        self.assertIn("cited_unseen_card", error)
        self.assertEqual(diag["cited_unseen_card_ids"], ["T01EXP_P_A_01"])


# 真实运行清单存在时才注册重放核验类（真实运行前不伪造产物、也不制造 skip）。
if MANIFEST.is_file():
    class LiveManifestReplayTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
            cls.ledger_before = hashlib.sha256(LEDGER.read_bytes()).hexdigest()
            first, second = _no_network()
            with first, second:
                cls.materials = lm.load_materials()
                cls.verification = lm.verify_topic01_live_micro_pilot(MANIFEST, materials=cls.materials)
                cls.summary = lm.summarize_live_micro_pilot(materials=cls.materials)
            cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        @classmethod
        def tearDownClass(cls):
            if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}:
                raise AssertionError("受保护原件被改写")
            if cls.ledger_before != hashlib.sha256(LEDGER.read_bytes()).hexdigest():
                raise AssertionError("测试不得改写账本")

        def _tampered(self, mutate):
            bad = deepcopy(self.manifest)
            mutate(bad)
            bad["manifest_sha256"] = canonical_hash({k: v for k, v in bad.items() if k != "manifest_sha256"})
            path = Path(tempfile.mkdtemp(prefix="step40_")) / "manifest.json"
            path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
            return lm.verify_topic01_live_micro_pilot(path, materials=self.materials)
        def test_05_manifest_verifies_offline(self):
            self.assertTrue(self.verification["passed"], self.verification["problems"])
            self.assertEqual(self.verification["measurements_revalidated"], 16)
            self.assertEqual(self.verification["panel_records"], 16)

        def test_06_comparison_complete(self):
            rows = self.manifest["comparison"]
            self.assertEqual(len(rows), 24)
            for row in rows:
                for key in ("BS", "Acc", "S", "O", "U"):
                    self.assertIsNotNone(row[key], (row["subject"], row["arm"], row["timepoint"], key))
            shared = [r for r in rows if r["timepoint"] in ("T0", "T1")]
            self.assertTrue(all(r["shared_parent"] for r in shared))

        def test_07_branch_parent_hashes(self):
            branches = self.manifest["branches"]
            self.assertEqual(len(branches), 6)
            for branch in branches:
                self.assertEqual(branch["pre_treatment_core_sha256"],
                                 self.manifest["parent_t1_core_sha256"][branch["subject"]])
                self.assertEqual(len(branch["responses"]), 2)

        def test_08_speech_and_mirror_contract(self):
            for subject in lm.SUBJECTS:
                speech = next(n for n in self.manifest["nodes"] if n["node_id"] == f"speech_{subject}_T1")
                self.assertTrue(lm.SPEECH_RANGE[0] <= len(speech["accepted_text"]) <= lm.SPEECH_RANGE[1])
                view = self.manifest["user_views"][subject]["user_view"]
                self.assertIn(speech["accepted_text"], view)
                mirror = self.manifest["mirrors"][subject]
                self.assertEqual(mirror["stage"], "mirror")
                evidence_ids = {e["evidence_id"] for e in self.materials["packet"]["evidence"]}
                for section in mirror["sections"]:
                    for item in section["items"]:
                        self.assertTrue(set(item["citations"]) <= evidence_ids)
                        if "user_quote" in item:
                            self.assertIn(item["user_quote"], view)

        def test_09_every_attempt_recorded(self):
            for node in self.manifest["nodes"]:
                self.assertGreaterEqual(len(node["attempts"]), 1)
                self.assertLessEqual(len(node["attempts"]), lm.MAX_ATTEMPTS)
                self.assertEqual(node["attempts"][-1]["status"], "accepted")
                for attempt in node["attempts"][:-1]:
                    self.assertEqual(attempt["status"], "rejected")
                    self.assertTrue(attempt["error"])

        def test_10_honest_gates(self):
            gates = self.manifest["honest_gates"]
            self.assertIs(gates["formal_eligible"], False)
            self.assertIs(gates["approved_for_experiment"], False)
            self.assertIs(gates["full_I4_eligible"], False)
            self.assertEqual(gates["human_reviews_completed"], 0)
            self.assertEqual(gates["real_human_subjects"], 0)
            self.assertEqual(gates["statistical_inference"], "none_mechanism_demo_only")
            self.assertIs(self.manifest["design"]["mirror_delivered_to_branches"], False)

        def test_11_ledger_reconciled(self):
            rows = lm._ledger_rows(ROOT)
            totals = lm._ledger_totals(rows, self.manifest["run_id"])
            self.assertEqual(totals["charged_cny"], self.manifest["totals"]["charged_cny"])
            self.assertEqual(totals["settle_count"], self.manifest["totals"]["ledger_settles"])
            self.assertEqual(totals["settle_count"], self.manifest["totals"]["actual_model_calls"])

        def test_12_tamper_metric_rejected(self):
            def mutate(doc):
                doc["comparison"][10]["BS"] = 0.0
            result = self._tampered(mutate)
            self.assertFalse(result["passed"])
            self.assertIn("comparison_table_mismatch", result["problems"])

        def test_13_tamper_speech_rejected(self):
            def mutate(doc):
                node = next(n for n in doc["nodes"] if n["node_id"] == "speech_S01_T1")
                node["accepted_text"] = node["accepted_text"][:-2] + "篡改"
            result = self._tampered(mutate)
            self.assertFalse(result["passed"])

        def test_14_tamper_mirror_rejected(self):
            def mutate(doc):
                doc["mirrors"]["S05"]["sections"][0]["items"] = []
            result = self._tampered(mutate)
            self.assertFalse(result["passed"])
            self.assertTrue(any(p.startswith("mirror_revalidation_failed") for p in result["problems"]))

        def test_15_tamper_charge_rejected(self):
            def mutate(doc):
                doc["totals"]["charged_cny"] = "0.00000001"
            result = self._tampered(mutate)
            self.assertIn("ledger_charge_mismatch", result["problems"])

        def test_16_tamper_gate_rejected(self):
            def mutate(doc):
                doc["honest_gates"]["formal_eligible"] = True
            result = self._tampered(mutate)
            self.assertIn("honest_gate_formal_eligible", result["problems"])

        def test_17_tamper_branch_hash_rejected(self):
            def mutate(doc):
                doc["branches"][0]["pre_treatment_core_sha256"] = "0" * 64
            result = self._tampered(mutate)
            self.assertIn("branch_parent_hash_mismatch", result["problems"])

        def test_18_manifest_hash_rejected(self):
            bad = deepcopy(self.manifest)
            bad["run_id"] = bad["run_id"] + "x"
            path = Path(tempfile.mkdtemp(prefix="step40h_")) / "manifest.json"
            path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
            with self.assertRaises(ValueError) as ctx:
                lm.verify_topic01_live_micro_pilot(path, materials=self.materials)
            self.assertIn("manifest_hash_mismatch", str(ctx.exception))

        def test_21_summary_for_dashboard(self):
            self.assertTrue(self.summary["available"])
            self.assertEqual(len(self.summary["subjects"]), 2)
            self.assertEqual(len(self.summary["comparison"]), 24)
            for subject in self.summary["subjects"]:
                self.assertTrue(subject["speech"])
                self.assertTrue(subject["mirror_items"])
                self.assertEqual(set(subject["t3_opposing_view"]), set(lm.ARMS))

        def test_23_no_network_during_verify(self):
            first, second = _no_network()
            with first, second:
                result = lm.verify_topic01_live_micro_pilot(MANIFEST, materials=self.materials)
            self.assertTrue(result["passed"])

        def test_26_superseded_runs_disclosed_and_reconciled(self):
            sup = self.manifest["superseded_runs"]
            self.assertGreaterEqual(len(sup), 3)
            rows = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()]
            for row in sup:
                totals = lm._ledger_totals(rows, row["run_id"])
                self.assertEqual(totals["charged_cny"], row["charged_cny"])
                self.assertTrue((lm.ROOT / lm.RUNS_RELATIVE / row["run_id"] / "aborted.json").is_file())
            spend = self.manifest["step_spend"]
            self.assertEqual(spend["step_charged_cny"], "%.8f" % (
                float(self.manifest["totals"]["charged_cny"]) + sum(float(r["charged_cny"]) for r in sup)))
            held = spend["held_interrupted_requests"]
            self.assertEqual([h["reason"] for h in held], ["interrupted"])
            self.assertIn(held[0]["request_id"], lm_budget.HELD_INTERRUPTED_REQUEST_IDS)

        def test_27_tamper_superseded_rejected(self):
            def mutate(doc):
                doc["superseded_runs"] = doc["superseded_runs"][1:]
            result = self._tampered(mutate)
            self.assertIn("superseded_runs_mismatch", result["problems"])

        def test_28_prompt_revision_recorded(self):
            self.assertEqual(self.manifest["design"]["prompt_revision"], lm.PROMPT_REVISION)



# ---------------------------------------------------------------- 步骤41：三议题
TOPIC01_MANIFEST_FILE_SHA256 = "166aea62aa48ccdef72250f18a2490b20c0308fc1f73f83e9754900303529dc0"
MATRIX = ROOT / lm.MATRIX_RELATIVE


class ThreeTopicSpecTests(unittest.TestCase):
    """离线：议题规格、受试规则、材料加载、TOPIC_01 冻结与零调用门禁。"""

    def test_41_subject_rule_reproduces_topic01_and_opposes(self):
        expected = {"TOPIC_01": ("S01", "S05"), "TOPIC_02": ("S01", "S07"), "TOPIC_03": ("S01", "S07")}
        for index, topic_id in enumerate(lm.TOPIC_IDS):
            spec = lm.topic_spec(topic_id)
            self.assertEqual(spec["subjects"], expected[topic_id])
            first, second = spec["subjects"]
            self.assertEqual(spec["stances"][first], -spec["stances"][second])
            self.assertEqual(spec["block_seed"], lm.BASE_SEED + index * 10007 + 101)
            self.assertEqual(spec["block_id"], "B02")
        self.assertEqual(lm.topic_spec("TOPIC_01")["contract"], lm.CONTRACT)
        self.assertEqual(lm.topic_spec("TOPIC_01")["manifest_relative"], lm.MANIFEST_RELATIVE)
        with self.assertRaises(ValueError):
            lm.topic_spec("TOPIC_09")

    def test_42_topic_labels_follow_frozen_packets(self):
        first, second = _no_network()
        with first, second:
            for topic_id, keyword in (("TOPIC_02", "能源"), ("TOPIC_03", "天气")):
                materials = lm.load_materials(topic_id=topic_id)
                self.assertIn(keyword, materials["topic"])
                self.assertIn(keyword, lm.TOPIC_LABELS[topic_id])
                self.assertEqual(materials["packet"]["packet_id"], f"CAND_{topic_id}_FULL_V1")
                for subject in materials["spec"]["subjects"]:
                    plan = materials["schedules"][subject]
                    self.assertEqual(sum(len(b) for b in plan["exposure"]), 48)
                    self.assertEqual(sum(len(b) for b in plan["retention"]), 24)
                self.assertTrue(all(materials["interventions"]["I4"][r]["content_origin"]
                                    == "live_model_organized_output" for r in lm.INTERVENTION_ROUNDS))

    def test_43_topic01_manifest_bytes_frozen(self):
        self.assertEqual(hashlib.sha256(MANIFEST.read_bytes()).hexdigest(), TOPIC01_MANIFEST_FILE_SHA256)

    def test_44_superseded_runs_filtered_by_topic(self):
        rows = lm._ledger_rows(lm.ROOT)
        for topic_id in lm.TOPIC_IDS:
            for row in lm._superseded_runs(lm.ROOT, rows, "none", topic_id):
                self.assertTrue(row["run_id"].startswith(f"live_micro_pilot_{topic_id}_"))

    def test_45_zero_call_gate_for_new_topics(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MIMO_API_KEY", None)
            first, second = _no_network()
            with first, second:
                for topic_id in ("TOPIC_02", "TOPIC_03"):
                    result = lm.run_topic_live_micro_pilot(topic_id, execute=True)
                    self.assertFalse(result["executed"])
                    self.assertEqual(result["actual_model_calls"], 0)
                    self.assertIn("missing_MIMO_API_KEY", result["blockers"])


def _topic_replay_case(topic_id):
    spec = lm.topic_spec(topic_id)
    path = ROOT / spec["manifest_relative"]

    class TopicReplay(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.ledger_before = hashlib.sha256(LEDGER.read_bytes()).hexdigest()
            first, second = _no_network()
            with first, second:
                cls.materials = lm.load_materials(topic_id=topic_id)
                cls.verification = lm.verify_topic_live_micro_pilot(topic_id, path, materials=cls.materials)
            cls.manifest = json.loads(path.read_text(encoding="utf-8"))

        @classmethod
        def tearDownClass(cls):
            if cls.ledger_before != hashlib.sha256(LEDGER.read_bytes()).hexdigest():
                raise AssertionError("测试不得改写账本")

        def _tampered(self, mutate):
            bad = deepcopy(self.manifest)
            mutate(bad)
            bad["manifest_sha256"] = canonical_hash({k: v for k, v in bad.items() if k != "manifest_sha256"})
            target = Path(tempfile.mkdtemp(prefix="step41_")) / "manifest.json"
            target.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
            return target

        def test_replay_passes(self):
            self.assertTrue(self.verification["passed"], self.verification["problems"])
            self.assertEqual(self.verification["measurements_revalidated"], 16)
            self.assertEqual(len(self.manifest["comparison"]), 24)
            self.assertEqual(tuple(self.manifest["design"]["subjects"]), spec["subjects"])
            self.assertEqual(self.manifest["design"]["subject_stances"], spec["stances"])
            self.assertEqual(self.manifest["design"]["prompt_revision"], lm.PROMPT_REVISION)
            kinds = {}
            for node in self.manifest["nodes"]:
                kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
            self.assertEqual(kinds, {"subject_measurement": 16, "blind_evaluation": 16, "mirror": 2,
                                     "routine_speech": 2, "intervention_response": 12})
            self.assertIs(self.manifest["honest_gates"]["formal_eligible"], False)

        def test_tamper_metric_and_charge_rejected(self):
            def metric(doc):
                doc["comparison"][5]["BS"] = 0.5
            def charge(doc):
                doc["totals"]["charged_cny"] = "0.00000001"
            for mutate, code in ((metric, "comparison_table_mismatch"), (charge, "ledger_charge_mismatch")):
                result = lm.verify_topic_live_micro_pilot(topic_id, self._tampered(mutate), materials=self.materials)
                self.assertIn(code, result["problems"])

        def test_cross_topic_manifest_rejected(self):
            other = "TOPIC_03" if topic_id == "TOPIC_02" else "TOPIC_02"
            with self.assertRaises(ValueError) as ctx:
                lm.verify_topic_live_micro_pilot(other, path)
            self.assertIn("manifest_topic_mismatch", str(ctx.exception))

    TopicReplay.__name__ = TopicReplay.__qualname__ = f"{topic_id.title().replace('_', '')}ReplayTests"
    return TopicReplay


for _topic in ("TOPIC_02", "TOPIC_03"):
    if (ROOT / lm.topic_spec(_topic)["manifest_relative"]).is_file():
        globals()[f"{_topic.title().replace('_', '')}ReplayTests"] = _topic_replay_case(_topic)


if MATRIX.is_file():
    class ThreeTopicMatrixTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            first, second = _no_network()
            with first, second:
                cls.cache = {t: lm.load_materials(topic_id=t) for t in lm.TOPIC_IDS}
                cls.verification = lm.verify_three_topic_live_micro_pilot_matrix(materials_by_topic=cls.cache)
            cls.matrix = json.loads(MATRIX.read_text(encoding="utf-8"))

        def test_matrix_verifies_and_totals(self):
            self.assertTrue(self.verification["passed"], self.verification["problems"])
            tot = self.matrix["totals"]
            self.assertEqual((tot["topics"], tot["branches"], tot["panel_records"], tot["comparison_rows"],
                              tot["blind_evaluations"], tot["subject_measurements"], tot["mirrors"],
                              tot["intervention_responses"]), (3, 18, 48, 72, 48, 48, 6, 36))
            self.assertEqual(tot["final_run_model_calls"],
                             sum(t["final_run_model_calls"] for t in self.matrix["topics"]))
            self.assertEqual(tot["accepted_calls"], 156)
            self.assertEqual(tot["accepted_calls"] + tot["extra_calls_rejected_or_redone"],
                             tot["final_run_model_calls"])
            self.assertEqual(len(self.matrix["descriptive_by_topic_arm"]), 9)
            self.assertEqual([t["topic_id"] for t in self.matrix["topics"]], list(lm.TOPIC_IDS))
            self.assertEqual(self.matrix["topics"][0]["manifest_file_sha256"], TOPIC01_MANIFEST_FILE_SHA256)

        def test_matrix_ledger_reconciles(self):
            rows = lm._ledger_rows(lm.ROOT)
            total = 0.0
            for topic in self.matrix["topics"]:
                self.assertEqual(lm._ledger_totals(rows, topic["run_id"])["charged_cny"], topic["charged_cny"])
                total += float(topic["charged_cny"])
            self.assertEqual("%.8f" % total, self.matrix["totals"]["final_runs_charged_cny"])

        def test_matrix_tamper_rejected(self):
            bad = deepcopy(self.matrix)
            bad["totals"]["final_run_model_calls"] += 1
            bad["matrix_sha256"] = canonical_hash({k: v for k, v in bad.items() if k != "matrix_sha256"})
            target = Path(tempfile.mkdtemp(prefix="step41m_")) / "matrix.json"
            target.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
            result = lm.verify_three_topic_live_micro_pilot_matrix(target, materials_by_topic=self.cache)
            self.assertIn("matrix_mismatch:totals", result["problems"])
            unsigned = deepcopy(self.matrix)
            unsigned["descriptive_by_topic_arm"][0]["BS_T3_mean"] = 0.9
            target2 = Path(tempfile.mkdtemp(prefix="step41h_")) / "matrix.json"
            target2.write_text(json.dumps(unsigned, ensure_ascii=False), encoding="utf-8", newline="\n")
            with self.assertRaises(ValueError) as ctx:
                lm.verify_three_topic_live_micro_pilot_matrix(target2, materials_by_topic=self.cache)
            self.assertIn("matrix_hash_mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
