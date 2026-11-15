"""步骤35：三议题全链路离线系统压力预演专项测试。

纯内存 + 只读访问既有取证构建目录；禁止网络 socket、禁止写入任何正式实验目录，
尤其保持步骤28–34受保护原件、空白人审模板与计费账本字节不变。
所有测量回答/盲评/统计数值都是显式标注的确定性合成演练数据。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

from cocoon import analysis, measurement, pilot_materials as pm, scheduling
from cocoon import system_rehearsal as sr
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
    ROOT / "experiment/review/validation_20261109T130342_7a6f52.json",
    ROOT / "configs/dose_additions_v1.json",
    ROOT / "configs/extractive_control_v1.json",
    ROOT / "configs/core.json",
    ROOT / "PROGRESS.md",
    PILOT_DIR / "REPORT_step33_20261109.md",
    PILOT_DIR / "REPORT_step34_20261109.md",
]
PROTECTED += [ROOT / "cocoon" / name for name in (
    "metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py",
    "measurement.py", "extractive_control.py", "reporting.py", "dosage.py",
    "dose_build.py", "evidence.py", "analysis.py", "pilot_materials.py",
    "engineering.py", "configuration.py")]
PROTECTED += [ROOT / "tests" / name for name in (
    "test_analysis.py", "test_pilot_materials.py", "test_measurement.py")]
PROTECTED += [BUILD / name for name in (
    "manifest.json", "review/sources.json", "review/evidence_review.json",
    "review/automation_checks.json", "packets/TOPIC_01_full.json",
    "packets/TOPIC_01_round_1.json", "packets/TOPIC_01_round_2.json",
    "packets/TOPIC_02_round_1.json", "packets/TOPIC_03_round_1.json")]


def resign(result):
    """篡改后重算整体摘要与门禁汇总，让改动抵达具体核验分支。"""
    result["acceptance_gates"] = sr._acceptance_gates(
        result["stage1_materials"]["topics"], result["stage2_exposure"],
        result["stage3_branches"], result["stage4_measurement"], result["analysis"])
    body = {key: value for key, value in result.items() if key != "integrity_sha256"}
    result["integrity_sha256"] = canonical_hash(body)
    return result


def tampered(result, *, resign_digest=True):
    copy = deepcopy(result)
    if resign_digest:
        resign(copy)
    return copy


class SystemRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.directory_before = {d: set(d.iterdir()) for d in (CROSS, MREVIEW, PILOT_DIR, BUILD)}
        # 全链路运行时禁止任何 socket / 外连；一旦有网络调用立刻失败。
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            cls.result = sr.run_pilot_system_rehearsal(SOURCE_ROOT)
        cls.verification = sr.verify_pilot_system_rehearsal(cls.result)

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in cls.protected_before}:
            raise AssertionError("步骤28–34受保护原件/配置/账本被改写")
        if cls.directory_before != {d: set(d.iterdir()) for d in cls.directory_before}:
            raise AssertionError("测试不得向既有审阅或取证目录写入文件")

    def setUp(self):
        self.r = self.result
        self.v = self.verification

    def reject(self, result, code):
        with self.assertRaises(ValueError) as ctx:
            sr.verify_pilot_system_rehearsal(result)
        self.assertIn("system_rehearsal:" + code, str(ctx.exception))

    # ---------------------------------------------------------------- 基本门禁
    def test_01_contract_and_all_honest_gates_locked(self):
        self.assertEqual(self.r["contract"], sr.REHEARSAL_VERSION)
        self.assertEqual(self.r["rehearsal_mode"], "offline_system_stress_rehearsal")
        for key, expected in (("is_demo", True), ("synthetic_rehearsal_response", True),
                              ("actual_model_calls", 0), ("actual_api_calls", 0),
                              ("new_cost_cny", "0"), ("human_reviews_completed", 0),
                              ("approved_for_experiment", False),
                              ("formal_eligible", False),
                              ("formal_launch_enabled", False),
                              ("subject_delivered", False),
                              ("statistical_outputs_are_synthetic", True)):
            self.assertEqual(self.r[key], expected, key)
        self.assertTrue(self.r["gold_never_read_by_response_generator"])
        self.assertIn("不是真实模型输出", self.r["notice"])

    def test_02_section15_1_gates_all_true_and_verify_passed(self):
        self.assertTrue(self.v["passed"])
        self.assertEqual(self.v["section"], "15.1")
        self.assertEqual(self.v["rehearsal_mode"], "offline_system_stress_rehearsal")
        self.assertTrue(all(value for value in self.v["checks"].values()))
        for name in ("three_zone_zero_gold_leak", "six_arms_nonempty_600_to_800",
                     "parent_child_pre_treatment_hash_match", "zero_cross_arm_mutable_sharing",
                     "measurement_zero_memory_writeback", "full_panel_reconciled",
                     "network_aggregation_valid", "honest_gates_locked",
                     "formal_launch_remains_closed"):
            self.assertIs(self.r["acceptance_gates"][name], True, name)
        self.assertTrue(self.v["rehearsal_is_not_real_experiment"])
        self.assertEqual(self.v["new_cost_cny"], "0")

    def test_03_integrity_hash_is_self_contained(self):
        body = {k: v for k, v in self.r.items() if k != "integrity_sha256"}
        self.assertEqual(self.r["integrity_sha256"], canonical_hash(body))
        self.assertEqual(len(self.r["integrity_sha256"]), 64)

    def test_04_verify_is_idempotent_and_deterministic(self):
        again = sr.verify_pilot_system_rehearsal(self.r)
        self.assertEqual(again, self.v)
        self.assertEqual(self.r["integrity_sha256"],
                         canonical_hash({k: v for k, v in self.r.items()
                                         if k != "integrity_sha256"}))

    # ------------------------------------------------------------ 阶段1：材料
    def test_05_stage1_three_topics_and_totals(self):
        stage = self.r["stage1_materials"]
        self.assertEqual(tuple(stage["demo_questionnaires"]), sr.TOPIC_ORDER)
        self.assertEqual(stage["topic_count"], 3)
        self.assertEqual(stage["totals"], {
            "exposure_cards": 216, "retention_cards": 108, "discussion_slots": 36,
            "intervention_packets": 36, "questionnaires": 3, "private_gold_bundles": 3,
        })
        for tid in sr.TOPIC_ORDER:
            topic = stage["topics"][tid]
            self.assertTrue(topic["material_verification_passed"])
            self.assertEqual((topic["exposure_cards"], topic["retention_cards"],
                              topic["discussion_slots"], topic["intervention_packets"]),
                             (72, 36, 12, 12))
            self.assertEqual((topic["anchor_items"], topic["transfer_items"],
                              topic["private_dimensions"]), (8, 4, 6))
            self.assertEqual(topic["questionnaire_sha256"],
                             canonical_hash(stage["demo_questionnaires"][tid]))

    def test_06_three_zone_zero_gold_leak(self):
        for tid in sr.TOPIC_ORDER:
            scan = self.r["stage1_materials"]["topics"][tid]["zone_leak_scan"]
            self.assertTrue(scan["leak_free"])
            self.assertEqual(scan["forbidden_keys_found"], [])
            self.assertFalse(scan["gold_text_leak"])
            self.assertTrue(scan["three_zone_hashes_distinct"])
            self.assertTrue(scan["private_zone_only"])
        self.assertTrue(self.r["acceptance_gates"]["three_zone_zero_gold_leak"])

    def test_07_six_arms_all_have_nonempty_600_800_visible_text(self):
        for tid in sr.TOPIC_ORDER:
            rows = self.r["stage1_materials"]["topics"][tid]["dose_rows"]
            self.assertEqual({(row["arm"], row["round"]) for row in rows},
                             {(arm, rnd) for arm in sr.ARMS for rnd in sr.ROUNDS})
            for row in rows:
                self.assertTrue(row["nonempty"] and row["in_range"])
                self.assertGreaterEqual(row["char_count"], 600)
                self.assertLessEqual(row["char_count"], 800)
                self.assertEqual(row["response_opportunities"], 1)
                self.assertTrue(row["extractive_control_constraint_satisfied"])
                self.assertEqual(len(row["visible_text_sha256"]), 64)
        self.assertEqual(sum(1 for tid in sr.TOPIC_ORDER
                             for _ in self.r["stage1_materials"]["topics"][tid]["dose_rows"]), 36)
        self.assertTrue(self.r["acceptance_gates"]["six_arms_nonempty_600_to_800"])

    def test_08_old_engineering_dry_run_source_unchanged(self):
        # 不调用会写 experiment/demo 的旧 dry_run；只确认其源码与旧行为入口保持原样。
        source = (ROOT / "cocoon" / "engineering.py").read_bytes()
        self.assertIn(b"def dry_run(", source)
        self.assertIn(b"visible_text\": None", source)
        self.assertNotIn(b"system_rehearsal", source)

    # ------------------------------------------------------- 阶段2：曝光与 mirror
    def test_09_four_environments_shared_base_state(self):
        worlds = self.r["stage2_exposure"]["worlds"]
        self.assertEqual(len(worlds), 3 * 2 * 4)
        for tid in sr.TOPIC_ORDER:
            for bid in ("B01", "B02"):
                group = [w for w in worlds if w["topic_id"] == tid and w["block_id"] == bid]
                self.assertEqual({w["environment"] for w in group}, set(sr.ENVIRONMENTS))
                self.assertEqual(len({w["base_population_network_sha256"] for w in group}), 1)
                self.assertEqual(len({w["initial_state_core_sha256"] for w in group}), 1)
                for w in group:
                    self.assertEqual(w["exposure_delivered_events"], 64)
                    self.assertEqual(w["routine_speech_entries"], 64)
                    self.assertEqual(w["discussion_slots_used"], 8)

    def test_10_manipulation_balanced_vs_homogeneous_and_error_cards(self):
        summary = self.r["stage2_exposure"]["manipulation_summary"]
        for env in ("A", "B"):
            self.assertAlmostEqual(summary[env]["mean_X"], 100 / 3, places=9)
        for env in ("C", "D"):
            self.assertAlmostEqual(summary[env]["mean_X"], 100 / 6, places=9)
        self.assertGreater(summary["A"]["mean_Hn"], summary["C"]["mean_Hn"])
        self.assertEqual(summary["A"]["mean_error_ratio"], 0)
        self.assertEqual(summary["C"]["mean_error_ratio"], 0)
        self.assertGreater(summary["B"]["mean_error_ratio"], 0)
        self.assertGreater(summary["D"]["mean_error_ratio"], 0)
        for env in sr.ENVIRONMENTS:
            self.assertLessEqual(summary[env]["max_repeat"], 2)

    def test_11_t1_routine_speech_to_mirror_for_every_world_agent(self):
        stage = self.r["stage2_exposure"]
        self.assertEqual(stage["mirror_check_count"], 3 * 2 * 4 * 8)
        self.assertEqual(len(stage["mirror_checks"]), stage["mirror_check_count"])
        for check in stage["mirror_checks"]:
            self.assertEqual(check["selected_entry_count"], 8)
            self.assertEqual(check["user_view_sha256"],
                             hashlib.sha256(check["user_view"].encode()).hexdigest())
            self.assertIn("曝光阶段常规发言", check["user_view"])
            self.assertEqual(check["citation_audit"]["schema_valid"], True)
            self.assertEqual(check["citation_audit"]["formal_eligible"], False)
            self.assertEqual(check["packet_id"],
                             self.r["stage1_materials"]["frozen_packets"][check["topic_id"]]["packet_id"])
        self.assertTrue(self.v["checks"]["t1_routine_speech_to_mirror_replayed"])

    def test_12_mirror_user_quote_is_exact_routine_speech_substring(self):
        check = self.r["stage2_exposure"]["mirror_checks"][0]
        payload = json.loads(check["raw_text"])
        self.assertEqual(payload["stage"], "mirror")
        self.assertEqual([s["name"] for s in payload["sections"]],
                         list(sr.STAGE_SECTIONS["mirror"]))
        item = payload["sections"][0]["items"][0]
        self.assertEqual(item["epistemic_status"], "evidence_based")
        self.assertIn(item["user_quote"], check["user_view"])
        packet = self.r["stage1_materials"]["frozen_packets"][check["topic_id"]]
        self.assertEqual(item["citations"], [packet["evidence"][0]["evidence_id"]])

    # --------------------------------------------------------- 阶段3：六臂分支
    def test_13_parent_child_pre_treatment_hash_match(self):
        branches = self.r["stage3_branches"]["branches"]
        self.assertEqual(len(branches), 3 * 2 * 6)
        for row in branches:
            self.assertEqual(row["branch_pre_treatment_core_sha256"],
                             row["parent_t1_core_sha256"])
            self.assertEqual(len(row["parent_t1_sha256"]), 64)
        self.assertTrue(self.r["acceptance_gates"]["parent_child_pre_treatment_hash_match"])

    def test_14_cross_arm_zero_mutable_state_sharing(self):
        stage = self.r["stage3_branches"]
        self.assertTrue(stage["identity_distinct"])
        self.assertTrue(stage["isolation_probe_clean"])
        self.assertTrue(stage["all_parent_hashes_unchanged"])
        self.assertTrue(self.r["acceptance_gates"]["zero_cross_arm_mutable_sharing"])

    def test_15_all_six_arms_two_rounds_delivered_to_every_agent(self):
        stage = self.r["stage3_branches"]
        self.assertEqual(stage["intervention_delivery_count"], 36 * 2 * 8)
        for row in stage["branches"]:
            self.assertEqual(len(row["intervention_deliveries"]), 16)
            self.assertEqual(len(row["intervention_history"]), 16)
            self.assertEqual({d["round"] for d in row["intervention_deliveries"]}, {1, 2})
            for item in row["intervention_deliveries"]:
                self.assertGreaterEqual(item["char_count"], 600)
                self.assertLessEqual(item["char_count"], 800)
                self.assertEqual(item["response_opportunities"], 1)
                self.assertEqual(len(item["visible_text_sha256"]), 64)
        arms_by_block = {}
        for row in stage["branches"]:
            arms_by_block.setdefault((row["topic_id"], row["block_id"]), set()).add(row["arm"])
        self.assertEqual(len(arms_by_block), 6)
        self.assertTrue(all(value == set(sr.ARMS) for value in arms_by_block.values()))

    def test_16_retention_uses_independent_card_pool_and_four_rounds(self):
        stage = self.r["stage3_branches"]
        self.assertEqual(stage["retention_delivery_count"], 36 * 4 * 8)
        for row in stage["branches"]:
            self.assertEqual(row["retention_delivered_events"], 4 * 8)
            self.assertEqual(row["t2_t3_measurement_contexts"], 16)
        # 保持卡与曝光卡ID不相交，且干预包不占用保持库存。
        bundle = pm.build_all_topics_pilot_materials(SOURCE_ROOT)
        for tid in sr.TOPIC_ORDER:
            pub = bundle["topics"][tid]["zones"]["zone_1_subject_public"]
            exp = {c["card_id"] for c in pub["public_exposure_cards"]}
            ret = {c["card_id"] for c in pub["public_retention_cards"]}
            self.assertFalse(exp & ret)
            self.assertEqual((len(exp), len(ret)), (72, 36))
            for arm in sr.ARMS:
                for rnd in sr.ROUNDS:
                    self.assertIs(pub["intervention_packets"][arm][str(rnd)]["retention_cards_used"],
                                  False)

    def test_17_allocation_rows_reproduce_scheduling_allocate(self):
        registry = self.r["analysis"]["allocations"]
        self.assertEqual(registry["contract"], "allocation_registry_v1")
        self.assertEqual(len(registry["blocks"]), 6)
        for block in registry["blocks"]:
            rebuilt = scheduling.allocate(list(sr.ARMS), block["seed"])
            rows = sorted(block["rows"], key=lambda row: row["slot"])
            expected = sorted(rebuilt, key=lambda row: row["slot"])
            self.assertEqual([(r["slot"], r["arm"], r["response_seed"]) for r in rows],
                             [(r["slot"], r["arm"], r["response_seed"]) for r in expected])
            self.assertEqual({r["arm"] for r in rows}, set(sr.ARMS))
            self.assertTrue(all(r["world_id"].startswith("W_") for r in rows))

    # ---------------------------------------------------- 阶段4：双盲与统计合龙
    def test_18_blind_registry_anonymous_and_no_condition_leaks(self):
        stage = self.r["stage4_measurement"]
        self.assertEqual(stage["blind_packet_sendable_field"], "public_blind_packets")
        self.assertEqual(stage["blind_packet_leaks_found"], 0)
        self.assertEqual(stage["blind_packet_leak_scan_count"], 1248)
        self.assertEqual(stage["registry_entry_count"], 1248)
        for name in ("registry_sha256", "source_entries_sha256",
                     "public_packets_sha256", "private_mapping_sha256"):
            self.assertEqual(len(stage[name]), 64)
        panel = self.r["analysis"]["reconciled_panel"]
        self.assertTrue(panel["trusted_registry_digest_supplied"])
        self.assertEqual(panel["registry_sha256"], stage["registry_sha256"])
        self.assertTrue(panel["send_to_evaluator"] is False)
        self.assertTrue(panel["controller_private_only"] is True)

    def test_19_full_panel_1248_reconciled_and_zero_writeback(self):
        stage = self.r["stage4_measurement"]
        self.assertEqual(stage["expected_samples"], 1248)
        self.assertEqual(stage["complete_panel_records"], 1248)
        self.assertEqual(stage["complete_blind_evaluations"], 1248)
        self.assertEqual(stage["pending_blind_evaluations"], 0)
        self.assertEqual(stage["synthetic_response_count"], 1248)
        self.assertTrue(stage["all_synthetic_rehearsal_responses"])
        self.assertTrue(stage["measurement_zero_writeback_all_passed"])
        self.assertEqual(stage["full_state_exclusion_audits"], 12)
        panel = self.r["analysis"]["reconciled_panel"]
        self.assertEqual(len(panel["panel_records"]), 1248)
        self.assertEqual(panel["panel_sha256"], canonical_hash(panel["panel_records"]))
        self.assertEqual(panel["status"], "all_blind_evaluations_reconciled")
        self.assertTrue(all(row["row_complete"] and row["objective_complete"]
                            and row["is_demo"] and not row["formal_eligible"]
                            for row in panel["panel_records"]))

    def test_20_network_aggregation_valid_fixed_denominator(self):
        agg = self.r["analysis"]["network_aggregation"]
        self.assertEqual(len(agg["network_records"]), 156)
        self.assertEqual(agg["expected_agents_per_world"], 8)
        self.assertTrue(agg["is_demo"] and not agg["formal_eligible"])
        self.assertEqual(agg["independent_unit"], "network_world_timepoint")
        self.assertEqual(agg["network_records_sha256"],
                         canonical_hash(agg["network_records"]))
        self.assertEqual(sum(row["missing_agents"] for row in agg["network_records"]), 0)
        self.assertTrue(all(row["valid"] and row["blind_valid"] and row["observed_agents"] == 8
                            for row in agg["network_records"]))
        rebuilt = analysis.aggregate_panel_to_networks(
            self.r["analysis"]["reconciled_panel"], expected_agents_per_world=8, allow_demo=True)
        self.assertEqual(canonical_hash(rebuilt), canonical_hash(agg))

    def test_21_exposure_2x2_estimable_for_three_topics(self):
        outcome = self.r["analysis"]["exposure_2x2"]
        self.assertEqual(outcome["contract"], "exposure_2x2_v1")
        self.assertEqual(outcome["target_topics"], list(sr.TOPIC_ORDER))
        self.assertEqual(len(outcome["per_block"]), 6)
        self.assertTrue(outcome["overall"]["estimable"])
        self.assertEqual(outcome["missing_topics"], [])
        self.assertEqual(set(outcome["overall"]["contrasts"]),
                         {"homogeneous", "misinformation", "interaction"})
        self.assertEqual(outcome["underlying_operator"], "metrics.exposure_contrasts")
        self.assertTrue(outcome["is_demo"] and not outcome["formal_eligible"])

    def test_22_intervention_T2_T3_randomization_holm(self):
        for endpoint in ("T2", "T3"):
            outcome = self.r["analysis"]["intervention"][endpoint]
            self.assertEqual(outcome["endpoint_timepoint"], endpoint)
            self.assertEqual(outcome["environment"], "D")
            self.assertEqual(set(outcome["comparisons"]), {"I4_vs_I0", "I4_vs_I5"})
            for name in ("I4_vs_I0", "I4_vs_I5"):
                self.assertIsNotNone(outcome["p_raw"][name])
                self.assertIsNotNone(outcome["p_adjusted"][name])
                self.assertGreaterEqual(outcome["p_raw"][name], 0)
                self.assertLessEqual(outcome["p_raw"][name], 1)
                self.assertGreaterEqual(outcome["p_adjusted"][name], outcome["p_raw"][name])
                self.assertEqual(outcome["comparisons"][name]["p_value_status"],
                                 "computed_from_actual_allocation")
            self.assertEqual(outcome["randomization"]["holm_operator"], "metrics.holm")
            self.assertEqual(outcome["randomization"]["method"],
                             "within_block_six_arm_slot_label_permutation")
            self.assertEqual(outcome["randomization"]["complete_blocks"], 6)
            self.assertEqual(outcome["allocation_validation"]["registered_blocks"], 6)
            self.assertEqual(outcome["allocation_validation"]["complete_blocks"], 6)
            self.assertEqual(outcome["randomization_scope"], "all_registered_blocks_complete")
            self.assertEqual(outcome["bootstrap_reps"], 200)
            self.assertEqual(outcome["permutation_reps"], 200)
        self.assertTrue(self.r["analysis"]["statistical_outputs_are_synthetic"])

    def test_23_end_to_end_numeric_reproducibility(self):
        """第二次完整独立运行：整体摘要与全部关键数值逐位一致。"""
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            second = sr.run_pilot_system_rehearsal(SOURCE_ROOT)
        self.assertEqual(second["integrity_sha256"], self.r["integrity_sha256"])
        self.assertEqual(second["analysis"]["reconciled_panel"]["panel_sha256"],
                         self.r["analysis"]["reconciled_panel"]["panel_sha256"])
        self.assertEqual(second["analysis"]["network_aggregation"]["network_records_sha256"],
                         self.r["analysis"]["network_aggregation"]["network_records_sha256"])
        self.assertEqual(second["analysis"]["exposure_2x2"]["overall"]["contrasts"],
                         self.r["analysis"]["exposure_2x2"]["overall"]["contrasts"])
        for endpoint in ("T2", "T3"):
            self.assertEqual(second["analysis"]["intervention"][endpoint]["p_raw"],
                             self.r["analysis"]["intervention"][endpoint]["p_raw"])
            self.assertEqual(second["analysis"]["intervention"][endpoint]["p_adjusted"],
                             self.r["analysis"]["intervention"][endpoint]["p_adjusted"])
        self.assertTrue(sr.verify_pilot_system_rehearsal(second)["passed"])

    # ------------------------------------------------------------- 篡改拦截
    def test_24_tamper_integrity_hash_rejected(self):
        bad = tampered(self.r, resign_digest=False)
        bad["integrity_sha256"] = "0" * 64
        self.reject(bad, "integrity_hash_mismatch")

    def test_25_tamper_honest_gate_flags_rejected(self):
        for key, value in (("is_demo", False), ("formal_eligible", True),
                           ("actual_model_calls", 1), ("human_reviews_completed", 1),
                           ("approved_for_experiment", True),
                           ("subject_delivered", True),
                           ("rehearsal_mode", "real_model_pilot")):
            bad = tampered(self.r)
            bad[key] = value
            resign(bad)
            with self.assertRaises(ValueError) as ctx:
                sr.verify_pilot_system_rehearsal(bad)
            self.assertIn("system_rehearsal:honest_gate", str(ctx.exception))

    def test_26_tamper_dose_char_count_rejected(self):
        bad = tampered(self.r)
        bad["stage1_materials"]["topics"]["TOPIC_01"]["dose_rows"][0]["char_count"] = 500
        resign(bad)
        self.reject(bad, "six_arm_dose_invalid")

    def test_27_tamper_zone_leak_flag_rejected(self):
        bad = tampered(self.r)
        scan = bad["stage1_materials"]["topics"]["TOPIC_02"]["zone_leak_scan"]
        scan["forbidden_keys_found"] = ["gold_answers"]
        scan["leak_free"] = True
        resign(bad)
        self.reject(bad, "three_zone_gold_leak")

    def test_28_tamper_mirror_user_view_rejected(self):
        bad = tampered(self.r)
        bad["stage2_exposure"]["mirror_checks"][0]["user_view"] += "额外注入文字"
        resign(bad)
        self.reject(bad, "mirror_user_view_hash_invalid")

    def test_29_tamper_branch_parent_hash_rejected(self):
        bad = tampered(self.r)
        bad["stage3_branches"]["branches"][0]["branch_pre_treatment_core_sha256"] = "f" * 64
        resign(bad)
        self.reject(bad, "parent_child_pre_treatment_hash_mismatch")

    def test_30_tamper_isolation_and_panel_counts_rejected(self):
        for path, value, code in (
                (("stage3_branches", "identity_distinct"), False, "cross_arm_isolation_invalid"),
                (("stage4_measurement", "complete_panel_records"), 1247,
                 "blind_panel_coverage_invalid"),
                (("stage4_measurement", "all_networks_valid"), False,
                 "network_aggregation_validity_invalid")):
            bad = tampered(self.r)
            bad[path[0]][path[1]] = value
            resign(bad)
            self.reject(bad, code)

    def test_31_tamper_statistical_p_values_rejected(self):
        bad = tampered(self.r)
        bad["analysis"]["intervention"]["T3"]["p_raw"]["I4_vs_I0"] = 0.0001
        resign(bad)
        self.reject(bad, "intervention_statistics_not_reproducible")

    def test_32_tamper_acceptance_gate_summary_rejected(self):
        bad = deepcopy(self.r)
        bad["acceptance_gates"]["honest_gates_locked"] = False
        resign(bad)  # 会被重新计算覆盖，改用不重签的方式
        bad["acceptance_gates"]["honest_gates_locked"] = False
        bad["integrity_sha256"] = canonical_hash(
            {k: v for k, v in bad.items() if k != "integrity_sha256"})
        self.reject(bad, "acceptance_gate_summary_mismatch")

    def test_33_verify_rejects_empty_or_foreign_result(self):
        with self.assertRaises(ValueError):
            sr.verify_pilot_system_rehearsal({})
        with self.assertRaises(ValueError):
            sr.verify_pilot_system_rehearsal({"contract": "something_else"})

    def test_34_no_network_sockets_and_no_formal_launch(self):
        # setUpClass 已在 socket 全面禁用下完成整条链路；这里再对纯校验入口复验。
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            self.assertTrue(sr.verify_pilot_system_rehearsal(self.r)["passed"])
        self.assertIs(self.r["formal_launch_enabled"], False)
        self.assertEqual(self.r["actual_model_calls"], 0)
        self.assertEqual(self.r["new_cost_cny"], "0")

    def test_35_protected_files_and_blank_checklist_untouched(self):
        checklist = json.loads(CHECKLIST.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(CHECKLIST.read_bytes()).hexdigest(),
                         self.protected_before[CHECKLIST])
        self.assertEqual(checklist["status"], "pending_human_review")
        self.assertEqual(checklist["human_reviews_completed"], 0)
        self.assertFalse(checklist["formal_eligible"])
        self.assertFalse(checklist["approved_for_experiment"])
        self.assertTrue(checklist["source_reviews"] and checklist["evidence_reviews"])
        self.assertTrue(all(row["adjudication"]["decision"] == "pending"
                            and row["adjudication"]["adjudicator_id"] is None
                            for row in checklist["source_reviews"] + checklist["evidence_reviews"]))
        for path in PROTECTED:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             self.protected_before[path], str(path))

    def test_36_parameters_and_protocol_sections(self):
        params = self.r["parameters"]
        self.assertEqual(params["topics"], ["TOPIC_01", "TOPIC_02", "TOPIC_03"])
        self.assertEqual(params["environments"], list(sr.ENVIRONMENTS))
        self.assertEqual(params["arms"], ["I0", "I1", "I2", "I3", "I4", "I5"])
        self.assertEqual(params["timepoints"], ["T0", "T1", "T2", "T3"])
        self.assertEqual((params["blocks_per_topic"], params["agents_per_world"]), (2, 8))
        self.assertEqual((params["exposure_rounds"], params["intervention_rounds"],
                          params["retention_rounds"]), (8, 2, 4))
        self.assertEqual(self.r["protocol_sections"], ["6", "7", "8", "9", "10", "11", "13.4", "15.1"])


if __name__ == "__main__":
    unittest.main()
