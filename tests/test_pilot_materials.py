"""步骤33：TOPIC_01 Pilot 三区材料包与 §5.2 模拟演练的离线专项测试。

全部为只读访问既有取证构建目录 + 内存合成演练；禁止网络、禁止写受保护产物，
尤其不得改写 cross_topic_20261108/human_review_checklist.json 的空白状态。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

from cocoon import measurement, pilot_materials as pm, scheduling
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "experiment/evidence_candidates/v1_20261108"
BUILD = SOURCE_ROOT / "build_20261108T074108_f38ec5"
CROSS = ROOT / "experiment/review/cross_topic_20261108"
MREVIEW = ROOT / "experiment/review/measurement_20261108"
PILOT_DIR = ROOT / "experiment/review/pilot_materials_20261109"
CHECKLIST = CROSS / "human_review_checklist.json"
PROTECTED = [CROSS / name for name in (
    "feasibility.json", "human_review_checklist.json", "independent_review.json",
    "review.html", "delivery.json", "REPORT_step28_20261108.md", "REPORT_step29_20261108.md")]
PROTECTED += [MREVIEW / name for name in (
    "REPORT_step30_20261108.md", "REPORT_step31_20261108.md", "REPORT_step32_20261108.md")]
PROTECTED += [
    ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl",
    ROOT / "破茧助手全量实验方案_v1.md",
    ROOT / "experiment/review/validation_20261109T025602_582dd0.json",
    ROOT / "experiment/review/validation_20261109T121218_e458e1.json",
    ROOT / "configs/dose_additions_v1.json",
    ROOT / "configs/extractive_control_v1.json",
    PILOT_DIR / "REPORT_step33_20261109.md",
]
PROTECTED += [ROOT / "cocoon" / name for name in (
    "metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py",
    "measurement.py", "extractive_control.py", "reporting.py", "dosage.py",
    "dose_build.py", "evidence.py", "analysis.py")]
PROTECTED += [ROOT / "tests" / "test_analysis.py"]
PROTECTED += [BUILD / name for name in (
    "manifest.json", "review/sources.json", "review/evidence_review.json",
    "review/automation_checks.json", "packets/TOPIC_01_full.json",
    "packets/TOPIC_01_round_1.json", "packets/TOPIC_01_round_2.json")]


def resign(bundle):
    """测试专用：重算摘要让篡改到达具体校验分支，而非只被整体哈希挡住。"""
    body = {key: value for key, value in bundle.items() if key != "integrity_sha256"}
    bundle["integrity_sha256"] = canonical_hash(body)
    return bundle


class PilotMaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.directory_before = {d: set(d.iterdir()) for d in (CROSS, MREVIEW, PILOT_DIR)}
        cls.bundle = pm.build_topic01_pilot_materials(BUILD)
        cls.verified = pm.verify_topic01_pilot_materials(cls.bundle)
        cls.simulation = pm.simulate_section52_dual_review(cls.bundle)
        cls.all_topics = pm.build_all_topics_pilot_materials(SOURCE_ROOT)
        cls.all_sim = pm.simulate_all_topics_dual_review(cls.all_topics)

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in cls.protected_before}:
            raise AssertionError("步骤28-33受保护原件被改写")
        if cls.directory_before != {d: set(d.iterdir()) for d in cls.directory_before}:
            raise AssertionError("测试不得向旧审阅目录写入文件")

    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络请求"))
        self.network = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.network.assert_not_called)
        real_open = Path.open

        def read_only(path, mode="r", *args, **kwargs):
            if any(letter in mode for letter in "wax+"):
                raise AssertionError("禁止落盘正式材料或审核结果")
            return real_open(path, mode, *args, **kwargs)

        patcher = mock.patch.object(Path, "open", read_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def zone1(self):
        return self.bundle["zones"]["zone_1_subject_public"]

    def reject(self, function, *args, **kwargs):
        with self.assertRaisesRegex(ValueError, "^pilot_materials:"):
            function(*args, **kwargs)

    # ---------- 1. 构建与独立校验 ----------
    def test_01_build_and_verify_summary(self):
        self.assertTrue(self.verified["passed"])
        self.assertEqual(self.bundle["contract"], pm.PILOT_VERSION)
        self.assertEqual(self.verified["source_count"], 4)
        self.assertEqual(self.verified["evidence_count"], 6)
        self.assertEqual(self.verified["exposure_cards"], 72)
        self.assertEqual(self.verified["retention_cards"], 36)
        self.assertEqual(self.verified["schedule_calls"], 96)
        self.assertEqual(self.verified["discussion_slots"], 12)
        self.assertEqual(self.verified["intervention_packets"], 12)
        self.assertTrue(self.verified["three_zone_isolation_verified"])
        self.assertTrue(self.verified["questionnaire_and_gold_validators_passed"])
        self.assertEqual(self.verified["human_reviews_completed"], 0)
        self.assertIs(self.verified["formal_eligible"], False)
        self.assertIs(self.verified["approved_for_experiment"], False)
        self.assertEqual(self.verified["new_model_calls"], 0)
        self.assertEqual(self.verified["new_cost_cny"], "0")

    def test_02_four_real_source_bindings_match_review_sources(self):
        sources = json.loads((BUILD / "review/sources.json").read_bytes())["sources"]
        by_id = {row["source_id"]: row for row in sources}
        bindings = self.bundle["source_bindings"]
        self.assertEqual([row["source_id"] for row in bindings],
                         ["CL_S01", "CL_S02", "CL_S03", "CL_S04"])
        for row in bindings:
            source = by_id[row["source_id"]]
            self.assertEqual(row["url"], source["url"])
            self.assertEqual(row["raw_sha256"], source["raw_sha256"])
            self.assertEqual(row["text_sha256"], source["text_sha256"])
            self.assertEqual(row["retrieved_at_utc"], source["retrieved_at_utc"])
            self.assertEqual(row["source_family_id"], source["source_family_id"])
            self.assertIs(row["redistribution_cleared"], False)
        self.assertEqual(self.bundle["evidence_manifest_sha256"],
                         hashlib.sha256((BUILD / "manifest.json").read_bytes()).hexdigest())

    def test_03_all_108_cards_text_length_180_to_220(self):
        cards = self.zone1()["exposure_cards"] + self.zone1()["retention_cards"]
        self.assertEqual(len(cards), 108)
        self.assertTrue(all(180 <= len(card["text"]) <= 220 for card in cards))
        self.assertTrue(all(card["transformation_type"] == "translated_adapted" for card in cards))
        self.assertTrue(all(card["original_quote"] for card in cards))
        self.assertTrue(all(card["is_demo"] is False for card in cards))
        self.assertTrue(all("摘编" in card["text"] for card in cards))
        for card in cards:
            self.assertEqual(card["text"],
                             scheduling.public_card(card)["text"])

    def test_04_card_strata_counts_and_phase_id_separation(self):
        from collections import Counter
        for phase, expected in (("exposure", {True: 16, False: 8}),
                                ("retention", {True: 8, False: 4})):
            cards = self.zone1()[f"{phase}_cards"]
            counts = Counter((card["stance"], card["is_accurate"]) for card in cards)
            for stance in (1, -1, 0):
                for accurate in (True, False):
                    self.assertEqual(counts[(stance, accurate)], expected[accurate])
            self.assertTrue(all(card["card_id"].startswith(
                "T01EXP" if phase == "exposure" else "T01RET") for card in cards))
        exposure_ids = {c["card_id"] for c in self.zone1()["exposure_cards"]}
        retention_ids = {c["card_id"] for c in self.zone1()["retention_cards"]}
        self.assertFalse(exposure_ids & retention_ids)
        self.assertEqual(len(exposure_ids) + len(retention_ids), 108)

    def test_05_public_card_exports_strip_all_private_labels(self):
        for phase in ("exposure", "retention"):
            exports = self.zone1()[f"public_{phase}_cards"]
            self.assertEqual(len(exports), 72 if phase == "exposure" else 36)
            for exported in exports:
                self.assertEqual(set(exported), {
                    "card_id", "topic_id", "text", "source_family_id",
                    "source_url", "transformation_type", "is_demo"})
                self.assertNotIn("stance", exported)
                self.assertNotIn("is_accurate", exported)
                self.assertNotIn("original_quote", exported)
                self.assertNotIn("analysis", exported)

    def test_06_schedule_all_96_calls_solvable_with_max_repeats_two(self):
        schedule = self.zone1()["schedule_verification"]
        self.assertEqual(schedule["total_calls"], 96)
        self.assertEqual(schedule["passed_calls"], 96)
        self.assertTrue(all(row["passed"] and row["max_observed_repeats"] <= 2
                            and row["cards_per_round"] == 6 for row in schedule["checks"]))
        self.assertEqual({(row["phase"], row["environment"]) for row in schedule["checks"]},
                         {(phase, env) for phase in ("exposure", "retention")
                          for env in "ABCD"})
        self.assertEqual({row["rounds"] for row in schedule["checks"]}, {8, 4})
        # 直接用真实排程器再解一遍曝光8轮 / 保持4轮。
        for env in "ABCD":
            for stance in (1, -1):
                for phase, rounds, pool in (
                        ("exposure", 8, self.zone1()["exposure_cards"]),
                        ("retention", 4, self.zone1()["retention_cards"])):
                    result = scheduling.schedule_cards(pool, stance, env, rounds, 3, max_repeats=2)
                    self.assertEqual(len(result), rounds)
                    self.assertTrue(all(len(row) == 6 for row in result))

    def test_07_three_zone_isolation_no_gold_or_truth_leak(self):
        zone1 = self.zone1()
        public = {
            "exposure": zone1["public_exposure_cards"],
            "retention": zone1["public_retention_cards"],
            "discussion": zone1["discussion_slots"],
            "visible": [zone1["intervention_packets"][arm][str(r)]["visible_text"]
                        for arm in pm.ARMS for r in (1, 2)],
        }
        assistant = self.bundle["zones"]["zone_2_assistant_evidence"]
        forbidden = ("gold_answers", "opposing_rubric", "dimension_id", "is_accurate",
                     "\"stance\"", "真值", "金标准", "标准答案", "六维清单")
        for payload in (json.dumps(public, ensure_ascii=False),
                        json.dumps(assistant, ensure_ascii=False)):
            for token in forbidden:
                self.assertNotIn(token, payload)
        private = self.bundle["zones"]["zone_3_evaluator_private_gold"]
        self.assertTrue(private["private_only"])
        self.assertTrue(private["evaluator_and_human_reviewers_only"])
        self.assertEqual(len({canonical_hash(self.bundle["zones"][key])
                              for key in self.bundle["zones"]}), 3)

    def test_08_questionnaire_and_private_gold_pass_measurement_validators(self):
        private = self.bundle["zones"]["zone_3_evaluator_private_gold"]
        q = private["public_questionnaire"]
        gold = private["private_gold_bundle"]
        self.assertEqual(measurement.validate_public_questionnaire(q), q)
        validated = measurement.validate_private_gold_bundle(gold, q)
        self.assertEqual(validated, private["validated_private_gold"])
        self.assertEqual(len(q["anchor_items"]), 8)
        self.assertEqual(len(q["transfer_items"]), 4)
        anchors = [row for row in gold["gold_answers"] if row["partition"] == "anchor"]
        transfers = [row for row in gold["gold_answers"] if row["partition"] == "transfer"]
        self.assertEqual(sum(row["truth"] for row in anchors), 4)
        self.assertEqual(sum(1 - row["truth"] for row in anchors), 4)
        self.assertEqual(len(transfers), 4)
        self.assertTrue(all(type(row["truth"]) is int and row["truth"] in (0, 1)
                            for row in gold["gold_answers"]))
        self.assertEqual(len(gold["dimensions"]), 6)
        self.assertEqual(set(gold["opposing_rubric"]), {"summary", "main_evidence", "boundary"})
        self.assertEqual({row["dimension_id"] for row in private["statement_dimension_map"]},
                         {f"D0{i}" for i in range(1, 7)})

    def test_09_assistant_zone_binds_three_public_packets_by_hash(self):
        assistant = self.bundle["zones"]["zone_2_assistant_evidence"]
        self.assertTrue(assistant["no_gold_labels"])
        self.assertTrue(assistant["assistant_reads_only"])
        for key, relative in (("full", "packets/TOPIC_01_full.json"),
                              ("round_1", "packets/TOPIC_01_round_1.json"),
                              ("round_2", "packets/TOPIC_01_round_2.json")):
            path = BUILD / relative
            self.assertEqual(assistant["packets"][key]["path"], relative)
            self.assertEqual(assistant["packets"][key]["byte_sha256"],
                             hashlib.sha256(path.read_bytes()).hexdigest())
            packet = json.loads(path.read_bytes())
            self.assertEqual(assistant["packets"][key]["canonical_sha256"], canonical_hash(packet))
            self.assertEqual(assistant["packets"][key]["evidence_ids"],
                             [row["evidence_id"] for row in packet["evidence"]])
        self.assertEqual(len(assistant["packets"]["full"]["evidence_ids"]), 6)

    def test_10_all_twelve_intervention_packets_within_600_800(self):
        zone = self.zone1()["intervention_packets"]
        for arm in pm.ARMS:
            total = 0
            for round_no in (1, 2):
                item = zone[arm][str(round_no)]
                self.assertEqual(item["char_count"], len(item["visible_text"]))
                self.assertTrue(600 <= item["char_count"] <= 800)
                self.assertFalse(item["contains_treatment_label"])
                self.assertNotRegex(item["visible_text"], r"I[45]", )
                self.assertIs(item["retention_cards_used"], False)
                self.assertEqual(item["response_opportunities"], 1)
                self.assertEqual(item["response_prompt"], "请用一段简短文字回应上述材料；如不确定可直接说明。")
                self.assertEqual(pm._visible_text(item["evidence_block"], item["additional_text"]),
                                 item["visible_text"])
                total += item["char_count"]
            self.assertTrue(1200 <= total <= 1600)
        self.assertEqual(len({zone[arm][str(r)]["additional_text"]
                              for arm in ("I0", "I1", "I2", "I3") for r in (1, 2)}), 8)

    def test_11_i4_i5_equal_evidence_and_extractive_control_binding(self):
        zone = self.zone1()["intervention_packets"]
        for round_no in (1, 2):
            i4, i5 = zone["I4"][str(round_no)], zone["I5"][str(round_no)]
            self.assertEqual(i4["evidence_block_sha256"], i5["evidence_block_sha256"])
            self.assertEqual(i4["source_map_sha256"], i5["source_map_sha256"])
            self.assertTrue(i4["checks"]["assembly_valid"])
            self.assertTrue(i4["checks"]["evidence_equal"])
            self.assertTrue(i4["checks"]["dose_within_range"])
            self.assertTrue(i4["checks"]["length_matched"])
            self.assertTrue(i4["checks"]["additional_length_matched"])
            self.assertEqual(i4["extractive_control_binding"], i5["extractive_control_binding"])
            binding = i4["extractive_control_binding"]
            self.assertEqual(binding["selection_status"], "structurally_matched_candidate")
            self.assertGreater(binding["feasible_length_count"], 0)
            self.assertTrue(600 <= binding["extractive_visible_chars"] <= 800)
            self.assertEqual(binding["extractive_visible_chars"],
                             len(binding["extractive_visible_text"]))
            self.assertEqual(pm._visible_text(i4["evidence_block"],
                                              binding["extractive_additional_text"]),
                             binding["extractive_visible_text"])
        # 步骤39 起 R2 使用 6/6 真实组织侧：长度落在可行区间、不再有 232 字符偏差；
        # 旧编辑稿仍是历史事实，但不再作为本轮 I4 正文。
        r2 = zone["I4"]["2"]["extractive_control_binding"]
        self.assertIs(r2["organized_within_extractive_feasible_range"], True)
        self.assertIsNone(r2["deviation"])
        self.assertIsNone(r2["editorial_organized_chars"])
        self.assertEqual(r2["organized_origin"], "live_model_organized_output")
        self.assertTrue(141 <= r2["organized_chars"] <= 214)

    def test_12_discussion_slots_are_frozen_and_blank_fallback_exists(self):
        slots = self.zone1()["discussion_slots"]
        self.assertEqual(len(slots), 12)
        self.assertEqual(sum(row["phase"] == "exposure" for row in slots), 8)
        self.assertEqual(sum(row["phase"] == "retention" for row in slots), 4)
        self.assertTrue(all(row["frozen"] is True and row["generated_live"] is False
                            and row["relayed_to_other_agents"] is False
                            and row["text"] for row in slots))
        self.assertTrue(self.zone1()["blank_discussion"]["frozen"])

    # ---------- 2. §5.2 模拟演练 ----------
    def test_13_eight_steps_complete_and_honest_gate_ironclad(self):
        s = self.simulation
        self.assertEqual(s["step_count"], 8)
        self.assertEqual([row["step"] for row in s["steps"]], list(range(1, 9)))
        self.assertTrue(s["all_eight_steps_present"])
        self.assertTrue(all(row["passed"] for row in s["steps"]))
        self.assertEqual(s["review_mode"], "simulated_rehearsal_not_human")
        self.assertIs(s["is_simulation_rehearsal"], True)
        self.assertIs(s["no_real_human_signature"], True)
        self.assertEqual(s["human_reviews_completed"], 0)
        self.assertIs(s["approved_for_experiment"], False)
        self.assertIs(s["formal_eligible"], False)
        self.assertIs(s["subject_delivered"], False)
        self.assertEqual(s["new_model_calls"], 0)
        self.assertEqual(s["new_cost_cny"], "0")
        self.assertIs(s["honest_gate"]["real_human_double_review_completed"], False)
        self.assertIs(s["honest_gate"]["formal_release_allowed"], False)
        self.assertIs(s["honest_gate"]["semantic_entailment_proven"], False)
        self.assertEqual(canonical_hash(
            {k: v for k, v in s.items() if k != "result_sha256"}), s["result_sha256"])

    def test_14_disagreement_blocked_then_independent_third_party_adjudicates(self):
        s = self.simulation
        self.assertTrue(s["disagreement_case_included"])
        self.assertEqual(len(s["disagreement_items"]), 1)
        self.assertEqual(s["disagreement_items"][0]["item_id"], "CL_E05")
        self.assertTrue(s["disagreement_items"][0]["disagreement_detected"])
        step3 = s["steps"][2]
        self.assertEqual([row["reviewer_id"] for row in step3["reviewer_slots"]],
                         ["SIM_REVIEWER_A", "SIM_REVIEWER_B"])
        self.assertTrue(all(row["is_human"] is False for row in step3["reviewer_slots"]))
        step8 = s["steps"][7]
        before = step8["release_gate_before_adjudication"]
        self.assertEqual(before["unresolved_items"], 1)
        self.assertIs(before["blocked_from_pass"], True)
        self.assertIs(before["simulated_candidate_structure_pass_before_adjudication"], False)
        self.assertTrue(step8["disagreement_blocked_before_adjudication"])
        adjudication = step8["adjudication"]
        self.assertEqual(adjudication["status"], "resolved_by_independent_third_party")
        self.assertEqual(adjudication["adjudicator_id"], "SIM_ADJUDICATOR_C")
        self.assertNotIn(adjudication["adjudicator_id"],
                         ["SIM_REVIEWER_A", "SIM_REVIEWER_B"])
        self.assertTrue(adjudication["adjudicator_distinct_from_both_reviewers"])
        self.assertIs(adjudication["is_human"], False)
        self.assertTrue(adjudication["reason"])
        self.assertTrue(all(type(value) is bool for value in adjudication["checks"].values()))
        self.assertEqual(adjudication["adjudicated_items"], 1)
        self.assertTrue(step8["adjudication_closed"])
        self.assertEqual(s["approved_for_experiment"], False)

    def test_15_no_disagreement_branch_still_never_approves(self):
        s = pm.simulate_section52_dual_review(self.bundle, include_disagreement_case=False)
        self.assertFalse(s["disagreement_case_included"])
        self.assertEqual(s["disagreement_items"], [])
        self.assertEqual(s["steps"][7]["release_gate_before_adjudication"]["unresolved_items"], 0)
        self.assertEqual(s["steps"][7]["adjudication"]["status"], "not_started")
        self.assertIs(s["approved_for_experiment"], False)
        self.assertIs(s["formal_eligible"], False)
        self.assertEqual(s["human_reviews_completed"], 0)

    def test_16_all_five_claim_types_marked_and_value_axis_not_a_fact(self):
        propositions = self.simulation["steps"][1]["propositions"]
        self.assertEqual(len(propositions), 13)
        self.assertEqual({row["claim_type"] for row in propositions},
                         {"fact", "value", "prediction", "definition", "causal"})
        value = next(row for row in propositions if row["claim_type"] == "value")
        self.assertIs(value["verifiable"], False)
        self.assertIsNone(value["evidence_id"])

    def test_17_translation_comparison_covers_entities_numbers_negation_conditions_uncertainty(self):
        step5 = self.simulation["steps"][4]
        self.assertEqual(len(step5["items"]), 6)
        self.assertTrue(step5["all_preserved"])
        for item in step5["items"]:
            self.assertTrue(item["all_preserved"])
            self.assertTrue(all(row["marker_in_original_quote"]
                                and row["counterpart_in_chinese_text"]
                                for row in item["entity_number_negation_condition_uncertainty_checks"]))
        markers = {row["english_marker"] for item in step5["items"]
                   for row in item["entity_number_negation_condition_uncertainty_checks"]}
        for expected in ("mostly", "it appears", "until the plant reaches a limit",
                         "continues to absorb more CO2", "based on models"):
            self.assertIn(expected, markers)

    def test_18_time_window_rejects_2026_observation_statistics_claims(self):
        step4 = self.simulation["steps"][3]
        self.assertTrue(step4["no_2026_observation_statistics_claim"])
        self.assertTrue(step4["retrieval_date_is_not_observation_year"])
        self.assertIn(2016, step4["years_appearing_in_chinese_evidence_text"])
        self.assertEqual(step4["source_publication_years"],
                         ["2025-05-21", "2011-06-16", None, "2016-04-26"])
        step6 = self.simulation["steps"][5]
        self.assertEqual(step6["excluded_items"], [])
        self.assertTrue(step6["rights_gate_pending"])
        step7 = self.simulation["steps"][6]
        self.assertTrue(step7["no_posthoc_subject_selection"])
        self.assertTrue(step7["transfer_reported_separately"])

    # ---------- 3. 防篡改、保全与确定性 ----------
    def test_19_tampering_is_rejected_even_after_resigning_integrity_hash(self):
        mutations = [
            lambda b: b["zones"]["zone_1_subject_public"]["exposure_cards"][0].update(text="过短"),
            lambda b: b["zones"]["zone_1_subject_public"]["public_exposure_cards"][0].update(
                stance="private_label_leak"),
            lambda b: b["zones"]["zone_1_subject_public"]["retention_cards"][0].update(
                card_id="T01EXP_P_A_01"),
            lambda b: b["zones"]["zone_1_subject_public"]["intervention_packets"]["I4"]["1"].update(
                char_count=999),
            lambda b: b["zones"]["zone_3_evaluator_private_gold"]["private_gold_bundle"][
                "gold_answers"][0].update(truth=9),
            lambda b: b.update(human_reviews_completed=2),
            lambda b: b.update(formal_eligible=True),
            lambda b: b["zones"]["zone_2_assistant_evidence"]["packets"]["full"].update(
                byte_sha256="0" * 64),
            lambda b: b["zones"]["zone_1_subject_public"]["discussion_slots"][0].update(
                generated_live=True),
        ]
        for mutate in mutations:
            bundle = deepcopy(self.bundle)
            mutate(bundle)
            resign(bundle)
            self.reject(pm.verify_topic01_pilot_materials, bundle)
        bad = deepcopy(self.bundle)
        bad["integrity_sha256"] = "0" * 64
        self.reject(pm.verify_topic01_pilot_materials, bad)
        self.reject(pm.simulate_section52_dual_review, bad)

    def test_20_human_review_checklist_remains_blank_and_all_protected_bytes_unchanged(self):
        checklist = json.loads(CHECKLIST.read_bytes())
        self.assertEqual(checklist["human_reviews_completed"], 0)
        self.assertEqual(checklist["counts"]["completed_human_slots"], 0)
        self.assertEqual(checklist["counts"]["double_human_slots"], 110)
        self.assertEqual(checklist["counts"]["total_items"], 55)
        for group in ("evidence_reviews", "source_reviews", "model_issue_reviews",
                      "round_reviews", "release_gates"):
            for item in checklist[group]:
                for reviewer in ("reviewer_1", "reviewer_2"):
                    self.assertIsNone(item[reviewer]["reviewer_id"])
                    self.assertIsNone(item[reviewer]["reviewed_at"])
                    self.assertEqual(item[reviewer]["decision"], "pending")
                self.assertEqual(item["adjudication"]["status"], "not_started")
        for path, before in self.protected_before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, str(path))

    def test_21_build_is_deterministic_and_inputs_are_never_mutated(self):
        again = pm.build_topic01_pilot_materials(BUILD)
        self.assertEqual(again["integrity_sha256"], self.bundle["integrity_sha256"])
        self.assertEqual(again, self.bundle)
        self.assertTrue(pm.verify_topic01_pilot_materials(again)["passed"])
        before = self.protected_before[BUILD / "manifest.json"]
        pm.simulate_section52_dual_review(self.bundle)
        self.assertEqual(hashlib.sha256((BUILD / "manifest.json").read_bytes()).hexdigest(), before)

    def test_22_public_api_and_identity_flags(self):
        self.assertEqual(set(pm.__all__), {
            "build_topic01_pilot_materials", "verify_topic01_pilot_materials",
            "simulate_section52_dual_review", "build_topic_pilot_materials",
            "build_all_topics_pilot_materials", "verify_topic_pilot_materials",
            "simulate_all_topics_dual_review"})
        bundle = self.bundle
        for flag in ("is_simulation_rehearsal", "human_reviews_completed",
                     "approved_for_experiment", "formal_eligible", "subject_delivered"):
            self.assertIn(flag, bundle)
        self.assertEqual(bundle["review_mode"], "simulated_rehearsal_not_human")
        self.assertEqual(bundle["new_model_calls"], 0)
        self.assertEqual(bundle["new_api_calls"], 0)
        self.assertEqual(bundle["new_cost_cny"], "0")
        self.assertEqual(bundle["topic_id"], "TOPIC_01")
        self.assertEqual(bundle["protocol_sections"],
                         ["5.1", "5.2", "5.3", "5.4", "6.4", "7.1", "7.2"])


    # ---------- 4. 步骤34：三议题并行标准化 ----------
    def topics(self):
        return self.all_topics["topics"]

    def zone_of(self, topic_id):
        return self.topics()[topic_id]["zones"]["zone_1_subject_public"]

    def test_23_all_topics_build_and_uniform_pilot_manifest(self):
        a = self.all_topics
        self.assertEqual(a["contract"], pm.ALL_TOPICS_VERSION)
        self.assertEqual(a["topic_ids"], ["TOPIC_01", "TOPIC_02", "TOPIC_03"])
        self.assertEqual(set(a["topics"]), set(a["topic_ids"]))
        self.assertEqual(a["execution_mode"], "parallel_threads_deterministic_ordered_output")
        self.assertTrue(all(row["passed"] for row in a["verifications"].values()))
        manifest = a["pilot_manifest"]
        self.assertEqual(manifest["topic_count"], 3)
        self.assertEqual(manifest["build_id"], "build_20261108T074108_f38ec5")
        self.assertEqual(manifest["protocol_sha256"],
                         "3d3ba6f0e21237cb288fe4a9558ab015a09b56f1a9538af81e5c9075637f6253")
        self.assertEqual(manifest["human_reviews_completed"], 0)
        self.assertIs(manifest["formal_eligible"], False)
        for row in manifest["topics"]:
            self.assertTrue(row["verified"])
            self.assertEqual(row["build_id"], f"{manifest['build_id']}__{row['topic_id']}")
            self.assertEqual(row["protocol_sha256"], manifest["protocol_sha256"])
            for key in ("exposure_cards", "retention_cards", "schedule_calls",
                        "discussion_slots", "intervention_packets", "anchor_items",
                        "transfer_items", "private_dimensions"):
                self.assertEqual(row[key], manifest["uniform_thresholds"][key])
        self.assertEqual([row["topic_folder"] for row in manifest["topics"]], list(pm.TOPICS))
        self.assertEqual([row["source_count"] for row in manifest["topics"]], [4, 5, 4])
        self.assertEqual(canonical_hash(
            {k: v for k, v in a.items() if k != "integrity_sha256"}), a["integrity_sha256"])

    def test_24_three_evidence_root_forms_and_parallel_sequential_determinism(self):
        from_intake = pm.build_all_topics_pilot_materials(SOURCE_ROOT / "intake", parallel=False)
        from_build = pm.build_all_topics_pilot_materials(BUILD, parallel=False)
        from_root = pm.build_all_topics_pilot_materials(SOURCE_ROOT, parallel=False)
        self.assertEqual(from_intake["execution_mode"], "sequential_deterministic")
        # 整体哈希含execution_mode审计字段；相同执行模式下的三种入参必须完全一致。
        self.assertEqual(from_intake["integrity_sha256"], from_build["integrity_sha256"])
        self.assertEqual(from_intake["integrity_sha256"], from_root["integrity_sha256"])
        self.assertEqual(from_intake["pilot_manifest"], self.all_topics["pilot_manifest"])
        self.assertEqual(from_intake["verifications"], self.all_topics["verifications"])
        for topic_id in self.all_topics["topic_ids"]:
            self.assertEqual(from_intake["topics"][topic_id], self.all_topics["topics"][topic_id])

    def test_25_verify_topic_pilot_materials_uniform_thresholds(self):
        for topic_id in self.all_topics["topic_ids"]:
            result = pm.verify_topic_pilot_materials(self.topics()[topic_id], topic_id)
            self.assertTrue(result["passed"])
            self.assertEqual(result["topic_id"], topic_id)
            self.assertEqual(result["pilot_build_id"],
                             self.topics()[topic_id]["pilot_build_id"])
            self.assertEqual(result["exposure_cards"], 72)
            self.assertEqual(result["retention_cards"], 36)
            self.assertEqual(result["schedule_calls"], 96)
            self.assertEqual(result["discussion_slots"], 12)
            self.assertEqual(result["intervention_packets"], 12)
            self.assertEqual(result["anchor_items"], 8)
            self.assertEqual(result["transfer_items"], 4)
            self.assertEqual(result["private_dimensions"], 6)
            self.assertEqual(result["anchor_dimension_bindings"], 8)
            self.assertEqual(result["arms_with_extractive_binding"], 6)
            self.assertEqual(result["extractive_binding_packets"], 12)
            self.assertTrue(result["rights_warnings_complete"])
            self.assertTrue(result["three_zone_isolation_verified"])
            self.assertEqual(result["human_reviews_completed"], 0)
            self.assertIs(result["formal_eligible"], False)
        with self.assertRaisesRegex(ValueError, "^pilot_materials:"):
            pm.verify_topic_pilot_materials(self.topics()["TOPIC_02"], "TOPIC_01")
        with self.assertRaisesRegex(ValueError, "^pilot_materials:"):
            pm.verify_topic_pilot_materials(self.topics()["TOPIC_01"], "NO_SUCH_TOPIC")

    def test_26_topic02_topic03_card_strata_length_and_id_separation(self):
        from collections import Counter
        for topic_id, prefix in (("TOPIC_02", "T02"), ("TOPIC_03", "T03")):
            zone = self.zone_of(topic_id)
            self.assertEqual(len(zone["exposure_cards"]), 72)
            self.assertEqual(len(zone["retention_cards"]), 36)
            for phase in ("exposure", "retention"):
                cards = zone[f"{phase}_cards"]
                expected = {True: 16, False: 8} if phase == "exposure" else {True: 8, False: 4}
                counts = Counter((c["stance"], c["is_accurate"]) for c in cards)
                for stance in (1, -1, 0):
                    for accurate in (True, False):
                        self.assertEqual(counts[(stance, accurate)], expected[accurate])
                self.assertTrue(all(180 <= len(c["text"]) <= 220 for c in cards))
                self.assertTrue(all(c["transformation_type"] == "translated_adapted" for c in cards))
                self.assertTrue(all(c["card_id"].startswith(prefix +
                                    ("EXP" if phase == "exposure" else "RET")) for c in cards))
                self.assertTrue(all(c["original_quote"] and c["source_url"] for c in cards))
                self.assertTrue(all(set(scheduling.public_card(c)) <= {
                    "card_id", "topic_id", "text", "source_family_id",
                    "source_url", "transformation_type", "is_demo"} for c in cards))
            self.assertFalse({c["card_id"] for c in zone["exposure_cards"]}
                             & {c["card_id"] for c in zone["retention_cards"]})

    def test_27_all_three_topics_schedule_96_calls_solvable(self):
        for topic_id in self.all_topics["topic_ids"]:
            schedule = self.zone_of(topic_id)["schedule_verification"]
            self.assertEqual(schedule["total_calls"], 96)
            self.assertEqual(schedule["passed_calls"], 96)
            self.assertTrue(all(row["passed"] and row["max_observed_repeats"] <= 2
                                for row in schedule["checks"]))
            for env in "ABCD":
                for stance in (1, -1):
                    out = scheduling.schedule_cards(
                        self.zone_of(topic_id)["exposure_cards"], stance, env, 8, 4, max_repeats=2)
                    self.assertEqual(len(out), 8)
                    self.assertTrue(all(len(row) == 6 for row in out))

    def test_28_all_three_topics_twelve_packets_dose_and_distinct_additions(self):
        for topic_id in self.all_topics["topic_ids"]:
            zone = self.zone_of(topic_id)
            additions = []
            for arm in pm.ARMS:
                total = 0
                for round_no in (1, 2):
                    item = zone["intervention_packets"][arm][str(round_no)]
                    self.assertEqual(item["char_count"], len(item["visible_text"]))
                    self.assertTrue(600 <= item["char_count"] <= 800)
                    self.assertFalse(item["contains_treatment_label"])
                    self.assertIs(item["retention_cards_used"], False)
                    self.assertEqual(item["response_opportunities"], 1)
                    additions.append(item["additional_text"])
                    total += item["char_count"]
                self.assertTrue(1200 <= total <= 1600)
            # 12 个包两轮：I5 确定性摘录在个别轮次与中性臂选中同一组原句，
            # 已如实登记为 dose_arm_collisions，不通过调参或改字规避。
            collisions = zone.get("dose_arm_collisions") or []
            self.assertEqual(len(set(additions)), 12 - len(collisions))
            self.assertEqual(zone.get("dose_arm_collision_count"), len(collisions))
            self.assertTrue(all(row["collides_with"] == "I5" for row in collisions))

    def test_29_every_arm_of_every_topic_has_extractive_control_binding(self):
        expected_policy = canonical_hash(pm.extractive_control._POLICY)
        for topic_id in self.all_topics["topic_ids"]:
            zone = self.zone_of(topic_id)
            for arm in pm.ARMS:
                for round_no in (1, 2):
                    item = zone["intervention_packets"][arm][str(round_no)]
                    self.assertIs(item["extractive_control_constraint_satisfied"], True)
                    binding = item["extractive_control_binding"]
                    self.assertEqual(binding["policy_sha256"], expected_policy)
                    self.assertEqual(binding["selection_status"],
                                     "structurally_matched_candidate")
                    self.assertGreater(binding["feasible_length_count"], 0)
                    self.assertTrue(600 <= binding["extractive_visible_chars"] <= 800)
                    self.assertEqual(binding["extractive_visible_chars"],
                                     len(binding["extractive_visible_text"]))
                    self.assertEqual(pm._visible_text(
                        item["evidence_block"], binding["extractive_additional_text"]),
                        binding["extractive_visible_text"])
            i4 = zone["intervention_packets"]["I4"]["1"]
            i5 = zone["intervention_packets"]["I5"]["1"]
            self.assertEqual(i4["extractive_control_binding"], i5["extractive_control_binding"])
            self.assertTrue(i4["checks"]["assembly_valid"])

    def test_30_all_anchor_items_bound_to_all_six_private_dimensions(self):
        for topic_id in self.all_topics["topic_ids"]:
            private = self.topics()[topic_id]["zones"]["zone_3_evaluator_private_gold"]
            q, gold = private["public_questionnaire"], private["private_gold_bundle"]
            self.assertEqual(measurement.validate_public_questionnaire(q), q)
            validated = measurement.validate_private_gold_bundle(gold, q)
            self.assertEqual(validated, private["validated_private_gold"])
            anchors = [row for row in gold["gold_answers"] if row["partition"] == "anchor"]
            self.assertEqual(len(anchors), 8)
            self.assertEqual(sum(row["truth"] for row in anchors), 4)
            self.assertEqual(sum(1 - row["truth"] for row in anchors), 4)
            self.assertEqual(len([row for row in gold["gold_answers"]
                                  if row["partition"] == "transfer"]), 4)
            dimension_ids = {row["dimension_id"] for row in gold["dimensions"]}
            self.assertEqual(len(dimension_ids), 6)
            mapping = {row["item_id"]: row["dimension_id"] for row in
                       private["statement_dimension_map"] if row["partition"] == "anchor"}
            self.assertEqual(set(mapping), {row["item_id"] for row in anchors})
            self.assertEqual(set(mapping.values()), dimension_ids)

    def test_31_rights_warnings_complete_for_every_real_source(self):
        sources = {row["source_id"]: row for row in
                   json.loads((BUILD / "review/sources.json").read_bytes())["sources"]}
        for topic_id in self.all_topics["topic_ids"]:
            bundle = self.topics()[topic_id]
            warnings = bundle["rights_warnings"]
            self.assertEqual({row["source_id"] for row in warnings},
                             set(pm._spec_for(topic_id)["source_ids"]))
            self.assertEqual(len(warnings), len(bundle["source_bindings"]))
            for row in warnings:
                self.assertIs(row["redistribution_cleared"], False)
                self.assertTrue(row["license_note"])
                self.assertEqual(row["license_note"],
                                 sources[row["source_id"]]["license_note"])
                self.assertEqual(row["url"], sources[row["source_id"]]["url"])
            self.assertTrue(bundle["rights_warning_note"])
            self.assertIs(bundle["intake_material_matches_manifest"], True)
            self.assertEqual(bundle["intake_material_sha256"], hashlib.sha256(
                (SOURCE_ROOT / bundle["intake_material_path"]).read_bytes()).hexdigest())
        self.assertEqual(len(self.topics()["TOPIC_02"]["source_family_exclusions"]), 9)
        self.assertEqual(len(self.topics()["TOPIC_03"]["source_family_exclusions"]), 2)
        simulation = self.all_sim["simulations"]["TOPIC_03"]
        self.assertEqual(simulation["steps"][0]["rights_warnings"],
                         self.topics()["TOPIC_03"]["rights_warnings"])
        self.assertTrue(simulation["steps"][5]["rights_gate_pending"])

    def test_32_all_topics_simulation_counts_and_honest_gate(self):
        s = self.all_sim
        self.assertEqual(s["topic_count"], 3)
        self.assertEqual(s["disagreement_items_total"], 3)
        self.assertEqual(s["adjudicated_items_total"], 3)
        self.assertEqual(s["blocked_before_adjudication_total"], 3)
        self.assertTrue(s["all_topics_honest_gate_enforced"])
        self.assertIs(s["is_simulation_rehearsal"], True)
        self.assertIs(s["no_real_human_signature"], True)
        self.assertEqual(s["human_reviews_completed"], 0)
        self.assertIs(s["approved_for_experiment"], False)
        self.assertIs(s["formal_eligible"], False)
        self.assertEqual(s["new_model_calls"], 0)
        self.assertEqual(canonical_hash(
            {k: v for k, v in s.items() if k != "result_sha256"}), s["result_sha256"])
        for topic_id in s["topic_ids"]:
            row = s["per_topic"][topic_id]
            self.assertEqual(row["step_count"], 8)
            self.assertTrue(row["all_eight_steps_present"])
            self.assertEqual(row["disagreement_items"], 1)
            self.assertEqual(row["adjudicated_items"], 1)
            self.assertTrue(row["blocked_before_adjudication"])
            self.assertTrue(row["adjudication_closed"])
            self.assertIs(row["honest_gate"]["formal_release_allowed"], False)
            self.assertEqual(row["human_reviews_completed"], 0)
            self.assertIs(row["formal_eligible"], False)

    def test_33_disagreement_adjudication_closed_on_every_topic(self):
        expected = {"TOPIC_01": "CL_E05", "TOPIC_02": "EN_E05", "TOPIC_03": "WE_E05"}
        for topic_id, evidence_id in expected.items():
            sim = self.all_sim["simulations"][topic_id]
            self.assertEqual(sim["topic_id"], topic_id)
            self.assertEqual(sim["protocol_sha256"],
                             self.topics()[topic_id]["protocol_sha256"])
            self.assertEqual(sim["disagreement_items"][0]["item_id"], evidence_id)
            step8 = sim["steps"][7]
            self.assertTrue(step8["disagreement_blocked_before_adjudication"])
            self.assertIs(step8["release_gate_before_adjudication"]["blocked_from_pass"], True)
            self.assertEqual(step8["adjudication"]["item_id"], evidence_id)
            self.assertEqual(step8["adjudication"]["adjudicator_id"], "SIM_ADJUDICATOR_C")
            self.assertNotIn("SIM_ADJUDICATOR_C", ["SIM_REVIEWER_A", "SIM_REVIEWER_B"])
            self.assertTrue(step8["adjudication"]["reason"])
            self.assertTrue(all(type(v) is bool
                                for v in step8["adjudication"]["checks"].values()))
            self.assertEqual(step8["adjudication"]["status"],
                             "resolved_by_independent_third_party")
            self.assertIs(sim["approved_for_experiment"], False)

    def test_34_differentiated_review_simulation_by_topic(self):
        for topic_id in self.all_topics["topic_ids"]:
            sim = self.all_sim["simulations"][topic_id]
            self.assertEqual([row["step"] for row in sim["steps"]], list(range(1, 9)))
            self.assertEqual(set(sim["steps"][1]["claim_types"]),
                             {"fact", "value", "prediction", "definition", "causal"})
            self.assertTrue(sim["steps"][4]["all_preserved"])
            self.assertEqual(len(sim["steps"][4]["items"]), 6)
            self.assertTrue(sim["steps"][3]["no_2026_observation_statistics_claim"])
        climate = self.all_sim["simulations"]["TOPIC_01"]
        self.assertEqual(climate["source_language"], "en")
        self.assertEqual(climate["steps"][4]["items"][0]["check_kind"],
                         "english_to_chinese_translation")
        energy = self.all_sim["simulations"]["TOPIC_02"]
        self.assertIn("二手转引", energy["steps"][3]["historical_cross_section_policy"])
        self.assertIn("81%", energy["steps"][3]["historical_cross_section_policy"])
        self.assertIn("LCOE", energy["steps"][4]["note"])
        weather = self.all_sim["simulations"]["TOPIC_03"]
        self.assertEqual(weather["source_language"], "zh")
        self.assertEqual(weather["steps"][4]["items"][0]["check_kind"],
                         "chinese_original_paraphrase_fidelity")
        self.assertIn("不从URL年月推断", weather["steps"][3]["historical_cross_section_policy"])
        self.assertEqual(weather["steps"][3]["source_publication_years"],
                         ["2022-07-06", "2023-06-17", "2023-01-06", "2020-05-12"])

    def test_35_no_disagreement_and_sequential_mode_still_never_release(self):
        s = pm.simulate_all_topics_dual_review(
            self.all_topics, include_disagreement_case=False, parallel=False)
        self.assertEqual(s["execution_mode"], "sequential_deterministic")
        self.assertEqual(s["disagreement_items_total"], 0)
        self.assertEqual(s["adjudicated_items_total"], 0)
        self.assertEqual(s["blocked_before_adjudication_total"], 0)
        self.assertTrue(s["all_topics_honest_gate_enforced"])
        self.assertIs(s["approved_for_experiment"], False)
        self.assertIs(s["formal_eligible"], False)
        self.assertEqual(s["human_reviews_completed"], 0)
        sequential = pm.simulate_all_topics_dual_review(self.all_topics, parallel=False)
        plain_bundles = pm.simulate_all_topics_dual_review(self.all_topics["topics"],
                                                           parallel=False)
        self.assertEqual(sequential["result_sha256"], plain_bundles["result_sha256"])
        for marker in ("per_topic", "simulations", "disagreement_items_total",
                       "adjudicated_items_total", "blocked_before_adjudication_total",
                       "all_topics_honest_gate_enforced"):
            self.assertEqual(sequential[marker], self.all_sim[marker])
            self.assertEqual(plain_bundles[marker], self.all_sim[marker])

    def test_36_uniform_bundle_tampering_rejected(self):
        cases = [
            ("TOPIC_02", lambda b: b["rights_warnings"].pop()),
            ("TOPIC_03", lambda b: b["zones"]["zone_3_evaluator_private_gold"][
                "statement_dimension_map"].pop()),
            ("TOPIC_02", lambda b: b["zones"]["zone_1_subject_public"][
                "exposure_cards"][0].update(text="过短")),
            ("TOPIC_03", lambda b: b["zones"]["zone_1_subject_public"][
                "intervention_packets"]["I0"]["1"].pop("extractive_control_binding")),
            ("TOPIC_01", lambda b: b.update(human_reviews_completed=2)),
            ("TOPIC_02", lambda b: b.update(formal_eligible=True)),
            ("TOPIC_03", lambda b: b.update(intake_material_sha256="0" * 64)),
            ("TOPIC_01", lambda b: b["zones"]["zone_1_subject_public"][
                "intervention_packets"]["I4"]["1"].update(char_count=999)),
        ]
        for topic_id, mutate in cases:
            bundle = deepcopy(self.topics()[topic_id])
            mutate(bundle)
            resign(bundle)
            self.reject(pm.verify_topic_pilot_materials, bundle, topic_id)
        for topic_id in self.all_topics["topic_ids"]:
            bundle = deepcopy(self.topics()[topic_id])
            bundle["rights_warnings"].pop()
            resign(bundle)
            self.reject(pm.verify_topic_pilot_materials, bundle, topic_id)

    def test_37_step33_and_step34_protected_artifacts_unchanged(self):
        self.assertEqual(json.loads(CHECKLIST.read_bytes())["human_reviews_completed"], 0)
        self.assertEqual(json.loads(CHECKLIST.read_bytes())["counts"]["total_items"], 55)
        for path, before in self.protected_before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, str(path))
        self.assertEqual(set(PILOT_DIR.iterdir()), self.directory_before[PILOT_DIR])


if __name__ == "__main__":
    unittest.main(verbosity=2)
