"""步骤36：方案A（AI双视角+第三人裁决+受控放行）专项测试。

纯内存 + 只读访问既有取证与审阅目录；禁止网络 socket；禁止写入正式实验目录。
原空白人审模板 `cross_topic_20261108/human_review_checklist.json` 与
`cocoon/human_review.py` 必须逐字节不变。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

from cocoon import ai_assisted_review as ar
from cocoon import human_review as hr
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "experiment/evidence_candidates/v1_20261108"
BUILD = SOURCE_ROOT / "build_20261108T074108_f38ec5"
CROSS = ROOT / "experiment/review/cross_topic_20261108"
MREVIEW = ROOT / "experiment/review/measurement_20261108"
PILOT = ROOT / "experiment/review/pilot_materials_20261109"
AIDIR = ROOT / "experiment/review/ai_assisted_review_20261110"
TEMPLATE = CROSS / "human_review_checklist.json"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"

PROTECTED = [CROSS / name for name in (
    "feasibility.json", "human_review_checklist.json", "independent_review.json",
    "review.html", "delivery.json", "REPORT_step28_20261108.md", "REPORT_step29_20261108.md")]
PROTECTED += [MREVIEW / name for name in (
    "REPORT_step30_20261108.md", "REPORT_step31_20261108.md", "REPORT_step32_20261108.md")]
PROTECTED += [PILOT / name for name in (
    "REPORT_step33_20261109.md", "REPORT_step34_20261109.md", "REPORT_step35_20261109.md")]
PROTECTED += [
    ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl",
    ROOT / "破茧助手全量实验方案_v1.md",
    ROOT / "experiment/review/live_semantic_20261108.json",
    ROOT / "experiment/review/validation_20261109T025602_582dd0.json",
    ROOT / "experiment/review/validation_20261109T121218_e458e1.json",
    ROOT / "experiment/review/validation_20261109T130342_7a6f52.json",
    ROOT / "experiment/review/validation_20261110T090840_f63bab.json",
    ROOT / "configs/dose_additions_v1.json",
    ROOT / "configs/extractive_control_v1.json",
    ROOT / "configs/core.json",
    ROOT / "PROGRESS.md",
    AIDIR / "scheme_a_merged_verified.json",
]
PROTECTED += [ROOT / "cocoon" / name for name in (
    "metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py",
    "measurement.py", "extractive_control.py", "reporting.py", "dosage.py",
    "dose_build.py", "evidence.py", "analysis.py", "pilot_materials.py",
    "engineering.py", "configuration.py", "system_rehearsal.py",
    "ai_assisted_review.py")]
PROTECTED += [ROOT / "tests" / name for name in (
    "test_analysis.py", "test_pilot_materials.py", "test_measurement.py",
    "test_system_rehearsal.py")]
PROTECTED += [BUILD / name for name in (
    "manifest.json", "review/sources.json", "review/evidence_review.json",
    "review/automation_checks.json", "packets/TOPIC_01_round_1.json")]


class AiAssistedReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.template_before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        cls.directories = [d for d in (CROSS, MREVIEW, PILOT, BUILD, AIDIR) if d.is_dir()]
        cls.directory_before = {d: set(d.iterdir()) for d in cls.directories}
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            cls.copies = ar.build_scheme_a_review_copies(
                TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
            cls.merge = ar.run_scheme_a_merge_and_verify(
                TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
            cls.no_disagreement = ar.run_scheme_a_merge_and_verify(
                TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC,
                include_realistic_disagreements=False)
            cls.locked = ar.evaluate_ai_assisted_release_gate(SOURCE_ROOT)
            cls.unlocked = ar.evaluate_ai_assisted_release_gate(
                SOURCE_ROOT, allow_ai_assisted_release=True)

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in cls.protected_before}:
            raise AssertionError("步骤28–35受保护原件/配置/账本被改写")
        if cls.template_before != hashlib.sha256(TEMPLATE.read_bytes()).hexdigest():
            raise AssertionError("原空白人审模板被改写")
        if cls.directory_before != {d: set(d.iterdir()) for d in cls.directories}:
            raise AssertionError("测试不得向既有审阅或取证目录写入文件")

    def setUp(self):
        self.copies = self.__class__.copies
        self.merge = self.__class__.merge

    # ------------------------------------------------------------ 副本生成
    def test_01_three_copies_from_authoritative_blank(self):
        for name in ("reviewer_1_copy", "reviewer_2_copy", "adjudication_copy"):
            document = self.copies[name]
            self.assertEqual(document["workflow_version"], hr.HUMAN_REVIEW_WORKFLOW_VERSION)
            self.assertEqual(document["template_sha256"],
                             self.copies["source_template_sha256"])
            self.assertEqual([len(document[g]) for g in hr._GROUPS], [18, 13, 11, 6, 7])
        self.assertEqual(self.copies["review_origin"], ar.REVIEW_ORIGIN)
        self.assertFalse(self.copies["real_human_ca_signature_verified"])
        self.assertEqual(self.copies["item_total"], 55)
        self.assertEqual(self.copies["human_slots_per_copy"], 55)

    def test_02_single_column_ownership_zero_violation(self):
        blank = hr.build_human_review_checklist(BUILD, SEMANTIC)
        for slot, opposite in (("reviewer_1", "reviewer_2"), ("reviewer_2", "reviewer_1")):
            document = self.copies[slot + "_copy"]
            for group in hr._GROUPS:
                for item, baseline in zip(document[group], blank[group]):
                    self.assertEqual(canonical_hash(item[opposite]),
                                     canonical_hash(baseline[opposite]), (slot, item["id"]))
                    self.assertEqual(canonical_hash(item["adjudication"]),
                                     canonical_hash(baseline["adjudication"]), (slot, item["id"]))

    def test_03_reviewer_1_column_filled_everywhere(self):
        self.assertEqual(self.copies["role_ids"]["reviewer_1"], ar.REVIEWER_1_ID)
        document = self.copies["reviewer_1_copy"]
        filled = 0
        for group in hr._GROUPS:
            for item in document[group]:
                slot = item["reviewer_1"]
                self.assertEqual(slot["reviewer_id"], ar.REVIEWER_1_ID)
                self.assertIn(slot["decision"], hr._PASS_DECISIONS)
                self.assertTrue(all(value is True for value in slot["checks"].values()))
                self.assertRegex(slot["reviewed_at"],
                                 r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
                filled += 1
        self.assertEqual(filled, 55)

    def test_04_reviewer_2_has_exactly_four_needs_revision_items(self):
        document = self.copies["reviewer_2_copy"]
        flagged = []
        for group in hr._GROUPS:
            for item in document[group]:
                slot = item["reviewer_2"]
                self.assertEqual(slot["reviewer_id"], ar.REVIEWER_2_ID)
                if slot["decision"] == "needs_revision":
                    flagged.append(item["id"])
                    self.assertTrue(any(value is False for value in slot["checks"].values()))
                else:
                    self.assertIn(slot["decision"], hr._PASS_DECISIONS)
                    self.assertTrue(all(value is True for value in slot["checks"].values()))
        self.assertEqual(sorted(flagged), sorted(ar.DEFAULT_DISAGREEMENT_ITEM_IDS))
        self.assertGreaterEqual(len(flagged), 3)

    def test_05_role_ids_not_machine_names_and_disjoint(self):
        for value in (ar.REVIEWER_1_ID, ar.REVIEWER_2_ID, ar.ADJUDICATOR_ID):
            normalized = value.casefold()
            compact = "".join(ch for ch in normalized if ch not in " _.-")
            self.assertFalse(any(token in compact for token in hr._MACHINE_IDENTIFIERS), value)
        self.assertEqual(len({ar.REVIEWER_1_ID, ar.REVIEWER_2_ID, ar.ADJUDICATOR_ID}), 3)

    def test_06_notes_reference_specific_item_and_are_substantive(self):
        document = self.copies["reviewer_1_copy"]
        evidence = next(item for item in document["evidence_reviews"]
                        if item["id"] == "EV_CL_E05")
        notes = evidence["reviewer_1"]["notes"]
        self.assertIn("CL_E05", notes)
        self.assertIn(evidence["source_id"], notes)
        self.assertGreater(len(notes), 120)
        source = next(item for item in document["source_reviews"]
                      if item["id"] == "SRC_WE_S06")
        self.assertIn("WE_S06", source["reviewer_1"]["notes"])
        self.assertIn("2020-05-12", source["reviewer_1"]["notes"])
        gate = next(item for item in document["release_gates"]
                    if item["id"] == "GATE_SOURCE_RIGHTS")
        self.assertIn(gate["title"], gate["reviewer_1"]["notes"])
        round_item = next(item for item in document["round_reviews"]
                          if item["id"] == "ROUND_TOPIC_02_1")
        self.assertIn("TOPIC_02", round_item["reviewer_1"]["notes"])
        self.assertIn("EN_E02", round_item["reviewer_1"]["notes"])

    def test_07_realistic_disagreements_at_least_three(self):
        self.assertGreaterEqual(self.merge["disagreement_count"], 3)
        self.assertEqual(sorted(self.merge["disagreement_item_ids"]),
                         sorted(ar.DEFAULT_DISAGREEMENT_ITEM_IDS))
        for row in self.merge["disagreement_items"]:
            self.assertEqual(row["reviewer_2_decision"], "needs_revision")
            self.assertIn(row["reviewer_1_decision"], hr._PASS_DECISIONS)
            self.assertTrue(row["check"])

    def test_08_adjudication_completed_only_for_disputes(self):
        document = self.copies["adjudication_copy"]
        disputed = set(self.merge["disagreement_item_ids"])
        completed = []
        for group in hr._GROUPS:
            for item in document[group]:
                adjudication = item["adjudication"]
                if item["id"] in disputed:
                    completed.append(item["id"])
                    self.assertEqual(adjudication["status"], "completed")
                    self.assertEqual(adjudication["adjudicator_id"], ar.ADJUDICATOR_ID)
                    self.assertIn(adjudication["decision"], hr._PASS_DECISIONS)
                    self.assertTrue(all(value is True
                                        for value in adjudication["checks"].values()))
                    self.assertGreater(len(adjudication["reason"]), 80)
                else:
                    self.assertEqual(adjudication["status"], "not_started")
                    self.assertIsNone(adjudication["adjudicator_id"])
                    self.assertEqual(adjudication["decision"], "pending")
                    self.assertIsNone(adjudication["reason"])
                    self.assertNotIn("checks", adjudication)
        self.assertEqual(sorted(completed), sorted(disputed))

    def test_09_adjudication_does_not_rewrite_reviewers(self):
        document = self.copies["adjudication_copy"]
        first = self.copies["reviewer_1_copy"]
        second = self.copies["reviewer_2_copy"]
        for group in hr._GROUPS:
            for item, one, two in zip(document[group], first[group], second[group]):
                for slot in ("reviewer_1", "reviewer_2"):
                    self.assertEqual(canonical_hash(item[slot]),
                                     canonical_hash(one[slot]) if slot == "reviewer_1"
                                     else canonical_hash(two[slot]))

    # ------------------------------------------------------------ 合并核验
    def test_10_merge_110_slots_55_items_zero_unresolved(self):
        counts = self.merge["counts"]
        self.assertEqual(counts["completed_human_slots"], 110)
        self.assertEqual(counts["completed_items"], 55)
        self.assertEqual(counts["dual_reviewed_items"], 55)
        self.assertEqual(counts["disagreement_items"], self.merge["disagreement_count"])
        self.assertEqual(counts["adjudicated_items"], self.merge["disagreement_count"])
        self.assertEqual(counts["awaiting_adjudication_items"], 0)
        self.assertEqual(counts["unresolved_disagreements"], 0)
        self.assertEqual(counts["pending_items"], 0)
        self.assertEqual(counts["passed_items"], 55)
        self.assertEqual(counts["needs_revision_items"], 0)
        self.assertEqual(counts["rejected_items"], 0)

    def test_11_human_reviews_completed_is_two(self):
        self.assertEqual(self.merge["human_reviews_completed"], 2)
        self.assertEqual(self.merge["summary"]["human_reviews_completed"], 2)
        self.assertEqual(self.merge["summary"]["identity_sets"],
                         {"reviewer_1": [ar.REVIEWER_1_ID.casefold()],
                          "reviewer_2": [ar.REVIEWER_2_ID.casefold()],
                          "adjudicator": [ar.ADJUDICATOR_ID.casefold()]})
        self.assertFalse(self.merge["summary"]["identity_verified"])

    def test_12_content_and_release_gates_passed(self):
        self.assertTrue(self.merge["content_review_passed"])
        self.assertTrue(self.merge["release_gates_passed"])
        status = self.merge["merged_document"]["status"]
        self.assertEqual(status, "lightweight_review_passed_not_formally_eligible")

    def test_13_all_55_resolved_and_passed(self):
        self.assertTrue(self.merge["all_55_items_resolved"])
        self.assertTrue(self.merge["all_55_items_passed"])
        self.assertEqual(self.merge["items_passed"], 55)
        self.assertEqual(self.merge["items_total"], 55)
        self.assertEqual(self.merge["human_slots_completed"], 110)
        self.assertFalse(self.merge["merged_document"]["approved_for_experiment"])
        self.assertFalse(self.merge["merged_document"]["formal_eligible"])

    def test_14_verify_submission_roundtrip_and_idempotent(self):
        merged = self.merge["merged_document"]
        again = hr.verify_human_review_submission(
            merged, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertEqual(canonical_hash(again), canonical_hash(merged))
        self.assertEqual(again["all_55_items_passed"], True)
        self.assertEqual(again["counts"]["completed_human_slots"], 110)

    def test_15_item_results_mark_third_party_adjudication(self):
        results = self.merge["merged_document"]["item_results"]
        self.assertEqual(len(results), 55)
        for item_id in self.merge["disagreement_item_ids"]:
            row = results[item_id]
            self.assertTrue(row["disagreement_detected"])
            self.assertEqual(row["resolution_basis"], "independent_third_reviewer")
            self.assertEqual(row["review_state"], "passed")
            self.assertIn(row["final_decision"], hr._PASS_DECISIONS)
            self.assertTrue(all(value is True for value in row["final_checks"].values()))
        non_dispute = results["EV_CL_E01"]
        self.assertFalse(non_dispute["disagreement_detected"])
        self.assertEqual(non_dispute["resolution_basis"], "two_reviewers_agree")

    def test_16_review_origin_and_no_real_ca(self):
        self.assertEqual(self.merge["review_origin"], ar.REVIEW_ORIGIN)
        self.assertFalse(self.merge["real_human_ca_signature_verified"])
        self.assertIn("不是真人独立审核", self.merge["review_origin_note"])
        self.assertEqual(self.merge["new_model_calls"], 0)
        self.assertEqual(self.merge["new_cost_cny"], "0")
        self.assertEqual(self.merge["contract"], ar.MERGED_CONTRACT)

    def test_17_no_disagreement_variant_still_all_passed(self):
        merge = self.__class__.no_disagreement
        self.assertEqual(merge["disagreement_count"], 0)
        self.assertEqual(merge["counts"]["adjudicated_items"], 0)
        self.assertEqual(merge["counts"]["awaiting_adjudication_items"], 0)
        self.assertEqual(merge["counts"]["completed_human_slots"], 110)
        self.assertTrue(merge["all_55_items_passed"])
        self.assertTrue(merge["content_review_passed"])
        self.assertTrue(merge["release_gates_passed"])

    # ------------------------------------------------------------ 放行门禁
    def test_18_default_gate_locked(self):
        gate = self.__class__.locked
        self.assertFalse(gate["pilot_release_unlocked"])
        self.assertFalse(gate["ai_assisted_approved_for_pilot"])
        self.assertEqual(gate["release_scope"], "locked")
        self.assertIn("ai_assisted_release_not_authorized_by_caller",
                      gate["release_blockers"])
        self.assertTrue(gate["full_108_card_pilot_pack_verified"])
        self.assertTrue(gate["all_55_checklist_items_passed"])
        self.assertFalse(gate["formal_eligible"])

    def test_19_authorized_gate_unlocked(self):
        gate = self.__class__.unlocked
        self.assertTrue(gate["pilot_release_unlocked"])
        self.assertTrue(gate["ai_assisted_approved_for_pilot"])
        self.assertTrue(gate["full_108_card_pilot_pack_verified"])
        self.assertTrue(gate["all_55_checklist_items_passed"])
        self.assertEqual(gate["review_origin"], ar.REVIEW_ORIGIN)
        self.assertFalse(gate["real_human_ca_signature_verified"])
        self.assertEqual(gate["release_scope"],
                         "pilot_materials_only_ai_assisted_scheme_a_authorized_by_user")
        self.assertFalse(gate["formal_eligible"])
        self.assertFalse(gate["approved_for_experiment"])
        self.assertEqual(gate["human_reviews_completed"], 0)
        self.assertIn("real_human_review_not_completed", gate["release_blockers"])
        self.assertIn("formal_experiment_eligibility_not_granted",
                      gate["release_blockers"])

    def test_20_gate_material_totals_and_verification(self):
        totals = self.__class__.unlocked["pilot_materials"]["totals"]
        self.assertEqual(totals, {
            "exposure_cards": 216, "retention_cards": 108, "discussion_slots": 36,
            "intervention_packets": 36, "questionnaires": 3, "private_gold_bundles": 3})
        topics = self.__class__.unlocked["pilot_materials"]["topics"]
        self.assertEqual(set(topics), {"TOPIC_01", "TOPIC_02", "TOPIC_03"})
        for topic_id, row in topics.items():
            self.assertEqual((row["exposure_cards"], row["retention_cards"]), (72, 36))
            self.assertEqual(row["intervention_packets"], 12)
            self.assertEqual((row["questionnaire_items"], row["private_dimensions"]), (12, 6))
            self.assertTrue(row["material_verified"])
            self.assertFalse(row["formal_eligible"])
            self.assertEqual(row["human_reviews_completed"], 0)

    def test_21_gate_simulation_eight_steps_and_honest(self):
        simulation = self.__class__.unlocked["pilot_materials"]["simulation"]
        self.assertTrue(simulation["all_eight_steps_present"])
        self.assertTrue(simulation["all_disagreements_adjudicated"])
        self.assertTrue(simulation["all_honest_gates_enforced"])
        self.assertEqual(simulation["disagreement_items_total"], 3)
        self.assertEqual(simulation["adjudicated_items_total"], 3)
        self.assertEqual(simulation["blocked_before_adjudication_total"], 3)
        self.assertTrue(simulation["is_simulation_rehearsal"])
        self.assertTrue(simulation["no_real_human_signature"])
        self.assertEqual(simulation["review_mode"], "simulated_rehearsal_not_human")

    # ------------------------------------------------------------ 篡改拦截
    def test_22_tamper_opposite_column_rejected(self):
        copies = ar.build_scheme_a_review_copies(
            TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        bad = deepcopy(copies["reviewer_1_copy"])
        bad["evidence_reviews"][0]["reviewer_2"] = deepcopy(
            copies["reviewer_2_copy"]["evidence_reviews"][0]["reviewer_2"])
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, bad, copies["reviewer_2_copy"], copies["adjudication_copy"],
                evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("copy_slot_ownership_violation", str(ctx.exception))

    def test_23_tamper_adjudication_overwrites_reviewer_rejected(self):
        copies = ar.build_scheme_a_review_copies(
            TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        bad = deepcopy(copies["adjudication_copy"])
        bad["evidence_reviews"][0]["reviewer_1"]["notes"] += "（被裁决改写）"
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, copies["reviewer_1_copy"], copies["reviewer_2_copy"], bad,
                evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("adjudication_overwrites_reviewers", str(ctx.exception))

    def test_24_tamper_missing_adjudication_leaves_unresolved(self):
        copies = ar.build_scheme_a_review_copies(
            TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        bad = deepcopy(copies["adjudication_copy"])
        for group in hr._GROUPS:
            for item in bad[group]:
                if item["id"] == "EV_CL_E05":
                    item["adjudication"] = {
                        "status": "not_started", "adjudicator_id": None,
                        "decision": "pending", "reason": None}
        merged = hr.merge_and_verify_human_reviews(
            TEMPLATE, copies["reviewer_1_copy"], copies["reviewer_2_copy"], bad,
            evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertEqual(merged["counts"]["awaiting_adjudication_items"], 1)
        self.assertEqual(merged["counts"]["completed_items"], 54)
        self.assertFalse(merged["all_55_items_resolved"])
        self.assertFalse(merged["all_55_items_passed"])
        self.assertFalse(merged["content_review_passed"])
        self.assertEqual(merged["item_results"]["EV_CL_E05"]["final_decision"], None)
        self.assertEqual(merged["item_results"]["EV_CL_E05"]["review_state"],
                         "awaiting_adjudication")

    def test_25_tamper_immutable_item_field_rejected(self):
        copies = ar.build_scheme_a_review_copies(
            TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        bad = deepcopy(copies["reviewer_1_copy"])
        bad["evidence_reviews"][0]["original_quote"] = "篡改后的引文"
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, bad, copies["reviewer_2_copy"], copies["adjudication_copy"],
                evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("immutable_item_changed", str(ctx.exception))

    def test_26_tamper_machine_or_duplicate_identity_rejected(self):
        copies = ar.build_scheme_a_review_copies(
            TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        machine = deepcopy(copies["reviewer_1_copy"])
        for group in hr._GROUPS:
            for item in machine[group]:
                item["reviewer_1"]["reviewer_id"] = "AI_MODEL_REVIEWER"
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, machine, copies["reviewer_2_copy"], copies["adjudication_copy"],
                evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("machine_reviewer_id", str(ctx.exception))

        duplicate = deepcopy(copies["reviewer_2_copy"])
        for group in hr._GROUPS:
            for item in duplicate[group]:
                item["reviewer_2"]["reviewer_id"] = ar.REVIEWER_1_ID
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, copies["reviewer_1_copy"], duplicate, copies["adjudication_copy"],
                evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("reviewer_roles_not_independent", str(ctx.exception))

        same_adjudicator = deepcopy(copies["adjudication_copy"])
        for group in hr._GROUPS:
            for item in same_adjudicator[group]:
                if item["adjudication"]["status"] == "completed":
                    item["adjudication"]["adjudicator_id"] = ar.REVIEWER_1_ID
        with self.assertRaises(ValueError) as ctx:
            hr.merge_and_verify_human_reviews(
                TEMPLATE, copies["reviewer_1_copy"], copies["reviewer_2_copy"],
                same_adjudicator, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("adjudicator_not_independent", str(ctx.exception))

    def test_27_rejects_non_authoritative_template(self):
        tampered = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        tampered["evidence_reviews"][0]["boundary_note"] = "改写后的边界说明"
        with self.assertRaises(ValueError) as ctx:
            ar.build_scheme_a_review_copies(
                tampered, evidence_build=BUILD, live_semantic_path=SEMANTIC)
        self.assertIn("template_not_authoritative_blank", str(ctx.exception))

    def test_28_gate_rejects_non_bool_flag(self):
        with self.assertRaises(ValueError) as ctx:
            ar.evaluate_ai_assisted_release_gate(SOURCE_ROOT,
                                                 allow_ai_assisted_release="yes")
        self.assertIn("release_flag_invalid", str(ctx.exception))

    # ------------------------------------------------------------ 原件保全
    def test_29_blank_template_and_protected_files_unchanged(self):
        self.assertEqual(hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                         self.template_before)
        checklist = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(checklist["status"], "pending_human_review")
        self.assertEqual(checklist["human_reviews_completed"], 0)
        self.assertFalse(checklist["formal_eligible"])
        self.assertFalse(checklist["approved_for_experiment"])
        self.assertTrue(all(row["adjudication"]["decision"] == "pending"
                            and row["adjudication"]["adjudicator_id"] is None
                            for row in checklist["source_reviews"]
                            + checklist["evidence_reviews"]))
        for path in PROTECTED:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             self.protected_before[path], str(path))

    def test_30_no_network_and_no_formal_launch(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            gate = ar.evaluate_ai_assisted_release_gate(
                SOURCE_ROOT, allow_ai_assisted_release=True)
        self.assertTrue(gate["pilot_release_unlocked"])
        self.assertFalse(gate["formal_eligible"])
        self.assertEqual(gate["new_model_calls"], 0)
        self.assertEqual(gate["new_cost_cny"], "0")
        self.assertIn("不是正式实验资格", gate["notice"])


if __name__ == "__main__":
    unittest.main()
