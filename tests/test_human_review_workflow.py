"""审核流程规则测试。所有签署仅为内存夹具；不写已填副本，不代表任何真人审核。"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest import mock

from cocoon import human_review as hr

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "experiment/review/cross_topic_20261108"
TEMPLATE = BASE / "human_review_checklist.json"
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"
ARGS = dict(evidence_build=BUILD, live_semantic_path=SEMANTIC)
GROUPS = ("evidence_reviews", "source_reviews", "model_issue_reviews", "round_reviews", "release_gates")
PROTECTED = [BASE / n for n in ("feasibility.json", "human_review_checklist.json", "independent_review.json",
                              "review.html", "delivery.json", "REPORT_step28_20261108.md")]
PROTECTED.append(ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def items(document):
    for group in GROUPS:
        for item in document[group]:
            yield group, item


def sign(document, slot, group="evidence_reviews", index=0, identifier=None, decision=None, checks=None):
    """仅修改测试在内存中的deepcopy。"""
    card = document[group][index]
    slot_data = card[slot]
    slot_data.update(reviewer_id=identifier or ("Human-One" if slot == "reviewer_1" else "Human-Two"),
                     reviewed_at="2026-11-08T22:00:00+08:00",
                     decision=decision or ("supported" if group == "model_issue_reviews" else "approved"),
                     notes="内存单元测试意见；不是实际人审或真实签署。")
    slot_data["checks"] = dict.fromkeys(slot_data["checks"], True) if checks is None else deepcopy(checks)
    return card


def adjudicate(document, group="evidence_reviews", index=0, identifier="Human-Three", decision="approved", checks=None):
    card = document[group][index]
    card["adjudication"] = dict(status="completed", adjudicator_id=identifier, decision=decision,
                                reason="内存测试裁决：仅验证流程，不构成真实人工裁决。")
    if checks is not None:
        card["adjudication"]["checks"] = deepcopy(checks)
    return card


class HumanReviewWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {p: digest(p) for p in PROTECTED}
        cls.base_files = set(BASE.iterdir())
        with mock.patch.object(socket, "socket", side_effect=AssertionError("禁止联网")):
            cls.template = hr.build_human_review_checklist(BUILD, SEMANTIC)
        assert cls.template == json.loads(TEMPLATE.read_bytes())
        cls.all_source_hashes = {Path(p): digest(Path(p)) for p in cls.template["source_inputs"]}

    @classmethod
    def tearDownClass(cls):
        if cls.before != {p: digest(p) for p in cls.before}:
            raise AssertionError("六份产物或账本被修改")
        if cls.all_source_hashes != {p: digest(p) for p in cls.all_source_hashes}:
            raise AssertionError("冻结源文件被修改")
        if cls.base_files != set(BASE.iterdir()):
            raise AssertionError("测试不应向正式输出目录写文件")

    def setUp(self):
        self.network = mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络请求"))
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.network_mock.assert_not_called)
        original = Path.open

        def guarded(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("本文件测试不得写出已签署副本或其他文件")
            return original(path, mode, *args, **kwargs)

        self.writing = mock.patch.object(Path, "open", guarded)
        self.writing.start()
        self.addCleanup(self.writing.stop)

    @contextmanager
    def authority(self):
        # 大量非法字段子用例复用本次setUpClass实际重建的权威模板；不mock被测流程。
        with mock.patch.object(hr, "build_human_review_checklist", side_effect=lambda *a, **kw: deepcopy(self.template)) as build:
            yield build

    def verify(self, document):
        with self.authority():
            return hr.verify_human_review_submission(document, **ARGS)

    def merge(self, first, second, third=None, template=None):
        with self.authority():
            return hr.merge_and_verify_human_reviews(self.template if template is None else template,
                                                    first, second, third, **ARGS)

    def empty_copies(self):
        return deepcopy(self.template), deepcopy(self.template)

    def pair(self, group="evidence_reviews", index=0, left="approved", right="approved"):
        a, b = self.empty_copies()
        sign(a, "reviewer_1", group, index, decision=left)
        sign(b, "reviewer_2", group, index, decision=right)
        return a, b

    def full_pair(self, gates=True):
        a, b = self.empty_copies()
        for group in GROUPS if gates else GROUPS[:-1]:
            for index in range(len(a[group])):
                sign(a, "reviewer_1", group, index)
                sign(b, "reviewer_2", group, index)
        return a, b

    def rejected(self, document, pattern="human_review:"):
        with self.assertRaisesRegex(ValueError, pattern):
            self.verify(document)

    def test_01_real_empty_template_uses_authority_and_preserves_old_validators(self):
        before = deepcopy(self.template)
        result = hr.verify_human_review_submission(TEMPLATE, **ARGS)
        self.assertEqual(result["counts"]["completed_human_slots"], 0)
        self.assertEqual(result["counts"]["pending_items"], 55)
        self.assertFalse(result["formal_eligible"])
        self.assertEqual(self.template, before)
        self.assertTrue(hr.validate_human_review_template(before, BUILD, SEMANTIC))
        self.assertTrue(hr.validate_human_review_checklist(before, BUILD, SEMANTIC))
        signed = deepcopy(before)
        sign(signed, "reviewer_1")
        self.assertFalse(hr.validate_human_review_template(signed, BUILD, SEMANTIC))
        self.assertFalse(hr.validate_human_review_checklist(signed, BUILD, SEMANTIC))

    def test_02_all_55_consistent_pass_remains_not_formal(self):
        a, b = self.full_pair()
        result = hr.merge_and_verify_human_reviews(TEMPLATE, a, b, **ARGS)
        c = result["counts"]
        self.assertEqual((c["completed_human_slots"], c["completed_items"], c["passed_items"]), (110, 55, 55))
        self.assertEqual((c["disagreement_items"], c["adjudicated_items"], c["pending_items"]), (0, 0, 0))
        self.assertTrue(result["content_review_passed"] and result["release_gates_passed"])
        self.assertTrue(result["all_55_items_resolved"] and result["all_55_items_passed"])
        self.assertEqual(result["human_reviews_completed"], 2)
        self.assertFalse(result["full_108_card_review"] or result["formal_eligible"] or result["approved_for_experiment"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(self.verify(result), result)

    def test_03_first_48_pass_but_release_gates_pending(self):
        result = self.merge(*self.full_pair(gates=False))
        c = result["counts"]
        self.assertEqual((c["completed_human_slots"], c["passed_items"], c["pending_items"]), (96, 48, 7))
        self.assertTrue(result["content_review_passed"])
        self.assertFalse(result["release_gates_passed"] or result["all_55_items_resolved"] or result["formal_eligible"])
        self.assertEqual(result["status"], "content_review_passed_pending_release")
        self.assertIn("release_gates_not_passed", result["release_blockers"])

    def test_04_partial_pending_progress_is_not_completed(self):
        a, b = self.empty_copies()
        sign(a, "reviewer_1")
        draft = a["source_reviews"][0]["reviewer_1"]
        draft.update(reviewer_id="Human-One", notes="尚未完成的测试笔记")
        draft["checks"][next(iter(draft["checks"]))] = False
        result = self.merge(a, b)
        self.assertEqual(result["counts"]["completed_human_slots"], 1)
        self.assertEqual(result["counts"]["completed_items"], 0)
        self.assertEqual(result["counts"]["pending_items"], 55)
        self.assertEqual(result["item_results"]["EV_CL_E01"]["review_state"], "partially_reviewed")
        self.assertEqual(result["human_reviews_completed"], 0)

    def test_05_disagreement_decision_waits_for_adjudicator(self):
        a, b = self.pair(left="approved", right="needs_revision")
        result = self.merge(a, b)
        card = result["item_results"]["EV_CL_E01"]
        self.assertTrue(card["disagreement_detected"])
        self.assertEqual(card["review_state"], "awaiting_adjudication")
        self.assertIsNone(card["final_decision"])
        c = result["counts"]
        self.assertEqual((c["completed_human_slots"], c["dual_reviewed_items"], c["disagreement_items"],
                          c["awaiting_adjudication_items"], c["completed_items"], c["passed_items"]), (2, 1, 1, 1, 0, 0))

    def test_06_disagreement_check_value_even_same_decision(self):
        a, b = self.pair(left="needs_revision", right="needs_revision")
        check = next(iter(b["evidence_reviews"][0]["reviewer_2"]["checks"]))
        b["evidence_reviews"][0]["reviewer_2"]["checks"][check] = False
        result = self.merge(a, b)
        self.assertEqual(result["counts"]["disagreement_items"], 1)
        self.assertEqual(result["counts"]["awaiting_adjudication_items"], 1)
        self.assertEqual(result["counts"]["needs_revision_items"], 0)

    def test_07_valid_third_review_resolves_decision_and_keeps_originals(self):
        a, b = self.pair(left="approved", right="needs_revision")
        third = deepcopy(self.template)
        adjudicate(third)
        result = self.merge(a, b, third)
        self.assertEqual(result["counts"]["disagreement_items"], 1)
        self.assertEqual(result["counts"]["adjudicated_items"], 1)
        self.assertEqual(result["counts"]["passed_items"], 1)
        self.assertEqual(result["counts"]["awaiting_adjudication_items"], 0)
        self.assertEqual(result["evidence_reviews"][0]["reviewer_2"]["decision"], "needs_revision")
        self.assertEqual(result["item_results"]["EV_CL_E01"]["resolution_basis"], "independent_third_reviewer")
        self.assertFalse(result["formal_eligible"])

    def test_08_third_can_use_merged_snapshot_without_overwriting_reviewers(self):
        a, b = self.pair(left="approved", right="needs_revision")
        third = self.merge(a, b)
        adjudicate(third)
        merged = self.merge(a, b, third)
        self.assertEqual(merged["counts"]["adjudicated_items"], 1)
        third["evidence_reviews"][0]["reviewer_2"]["notes"] += "修改原意见"
        with self.assertRaisesRegex(ValueError, "adjudication_overwrites_reviewers"):
            self.merge(a, b, third)

    def test_09_check_dispute_requires_explicit_third_bool_checks(self):
        a, b = self.pair(left="approved", right="needs_revision")
        checks = b["evidence_reviews"][0]["reviewer_2"]["checks"]
        checks[next(iter(checks))] = False
        third = deepcopy(self.template)
        adjudicate(third)
        with self.assertRaisesRegex(ValueError, "adjudication_checks_required"):
            self.merge(a, b, third)
        adjudicate(third, checks=dict.fromkeys(checks, True))
        result = self.merge(a, b, third)
        self.assertEqual(result["counts"]["passed_items"], 1)
        self.assertTrue(all(result["item_results"]["EV_CL_E01"]["final_checks"].values()))

    def test_10_third_never_approves_false_or_non_bool_checks(self):
        a, b = self.pair(left="approved", right="needs_revision")
        for value in (False, None, 1, 0):
            with self.subTest(value=value):
                third = deepcopy(self.template)
                checks = dict.fromkeys(a["evidence_reviews"][0]["reviewer_1"]["checks"], True)
                checks[next(iter(checks))] = value
                adjudicate(third, checks=checks)
                with self.assertRaisesRegex(ValueError, "human_review:"):
                    self.merge(a, b, third)

    def test_11_consistent_negative_resolutions_and_counts(self):
        a, b = self.empty_copies()
        examples = [("evidence_reviews", 0, "needs_revision"), ("source_reviews", 0, "rejected"),
                    ("model_issue_reviews", 0, "overstated")]
        for group, index, decision in examples:
            for slot, copy in (("reviewer_1", a), ("reviewer_2", b)):
                sign(copy, slot, group, index, decision=decision)
        result = self.merge(a, b)
        self.assertEqual((result["counts"]["completed_items"], result["counts"]["needs_revision_items"],
                          result["counts"]["rejected_items"]), (3, 2, 1))
        self.assertEqual(result["counts"]["passed_items"], 0)

    def test_12_two_ids_equal_after_case_space_nfkc_rejected(self):
        for identity in ("Human-One", " human-ONE ", "Ｈｕｍａｎ-Ｏｎｅ"):
            with self.subTest(identity=identity):
                a, b = self.pair()
                b["evidence_reviews"][0]["reviewer_2"]["reviewer_id"] = identity
                with self.assertRaisesRegex(ValueError, "reviewer_roles_not_independent"):
                    self.merge(a, b)

    def test_13_cross_item_role_swap_and_pending_overlap_rejected(self):
        a, b = self.empty_copies()
        sign(a, "reviewer_1", index=0, identifier="Human-One")
        sign(b, "reviewer_2", index=1, identifier=" human-ONE ")
        with self.assertRaisesRegex(ValueError, "reviewer_roles_not_independent"):
            self.merge(a, b)
        b["evidence_reviews"][1]["reviewer_2"]["decision"] = "pending"
        with self.assertRaisesRegex(ValueError, "reviewer_roles_not_independent"):
            self.merge(a, b)

    def test_14_third_cannot_match_reviewers_locally_or_globally(self):
        for identifier in (" human-one ", "HUMAN-TWO", "Other-Person"):
            a, b = self.pair(left="approved", right="needs_revision")
            sign(a, "reviewer_1", index=1, identifier="Other-Person")
            third = deepcopy(self.template)
            adjudicate(third, identifier=identifier)
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "adjudicator_not_independent"):
                self.merge(a, b, third)

    def test_15_machine_ids_rejected_in_reviewers_and_adjudicators(self):
        bad = ("ai", "X-LLM", "Gpt-Reviewer", "Gemini", "mimo", "claude", "assistant", "bot",
               "auto", "machine", "ＡＩ", "g.p.t", "机器人", "审查模型", "人工智能")
        for identifier in bad:
            with self.subTest(identifier=identifier):
                a, b = self.pair()
                a["evidence_reviews"][0]["reviewer_1"]["reviewer_id"] = identifier
                with self.assertRaisesRegex(ValueError, "machine_reviewer_id"):
                    self.merge(a, b)
                a, b = self.pair(left="approved", right="needs_revision")
                third = deepcopy(self.template)
                adjudicate(third, identifier=identifier)
                with self.assertRaisesRegex(ValueError, "machine_reviewer_id"):
                    self.merge(a, b, third)

    def test_16_invalid_empty_and_invisible_ids_rejected(self):
        for identity in (None, "", "  ", 5, "...", "Human\u200bOne", "Human\nOne", "X" * 129):
            d = deepcopy(self.template)
            sign(d, "reviewer_1")
            d["evidence_reviews"][0]["reviewer_1"]["reviewer_id"] = identity
            with self.subTest(identity=identity):
                self.rejected(d)

    def test_17_timestamp_must_be_iso_datetime_with_timezone(self):
        bad = (None, "", "2026-11-08", "2026-11-08T22:00:00", "2026-02-30T22:00:00Z",
               "2026-11-08T25:00:00Z", "2026-11-08T22:00:00+25:00", "2026-11-08T22:00:00+08:60", 1)
        for stamp in bad:
            d = deepcopy(self.template)
            sign(d, "reviewer_1")
            d["evidence_reviews"][0]["reviewer_1"]["reviewed_at"] = stamp
            with self.subTest(stamp=stamp):
                self.rejected(d)
        for stamp in ("2026-11-08T14:00:00Z", "2026-11-08T22:00:00.123456+08:00", "2026-11-08T09:00:00-05:00"):
            d = deepcopy(self.template)
            sign(d, "reviewer_1")
            d["evidence_reviews"][0]["reviewer_1"]["reviewed_at"] = stamp
            self.assertEqual(self.verify(d)["counts"]["completed_human_slots"], 1)

    def test_18_completed_notes_required_for_all_decisions(self):
        for group, decision in (("evidence_reviews", "approved"), ("evidence_reviews", "needs_revision"),
                                ("evidence_reviews", "rejected"), ("model_issue_reviews", "overstated")):
            for notes in (None, "", " \t", 0):
                d = deepcopy(self.template)
                sign(d, "reviewer_1", group=group, decision=decision)
                d[group][0]["reviewer_1"]["notes"] = notes
                with self.subTest(group=group, decision=decision, notes=notes):
                    self.rejected(d)

    def test_19_complete_checks_bool_not_none_or_int(self):
        for value in (None, 0, 1, "true", [], {}):
            d = deepcopy(self.template)
            card = sign(d, "reviewer_1")
            card["reviewer_1"]["checks"][next(iter(card["reviewer_1"]["checks"]))] = value
            with self.subTest(value=value):
                self.rejected(d, "check_values_invalid")

    def test_20_false_check_cannot_approve_support_or_accept(self):
        for group, decision in (("evidence_reviews", "approved"), ("model_issue_reviews", "supported"),
                                ("model_issue_reviews", "acceptable")):
            d = deepcopy(self.template)
            card = sign(d, "reviewer_1", group=group, decision=decision)
            card["reviewer_1"]["checks"][next(iter(card["reviewer_1"]["checks"]))] = False
            with self.subTest(decision=decision):
                self.rejected(d, "passing_decision_has_false_check")
            card["reviewer_1"]["decision"] = "needs_revision"
            self.assertEqual(self.verify(d)["counts"]["completed_human_slots"], 1)

    def test_21_group_specific_decision_whitelists(self):
        for group in GROUPS:
            for decision in (("approved", "rejected") if group == "model_issue_reviews"
                             else ("supported", "acceptable", "overstated")):
                d = deepcopy(self.template)
                sign(d, "reviewer_1", group=group, decision=decision)
                with self.subTest(group=group, decision=decision):
                    self.rejected(d, "decision_invalid")
        for decision in ("pass", "APPROVED", "unknown", True, None):
            d = deepcopy(self.template)
            sign(d, "reviewer_1", decision="approved")
            d["evidence_reviews"][0]["reviewer_1"]["decision"] = decision
            self.rejected(d)

    def test_22_copy_cannot_fill_opposite_slot_or_adjudication(self):
        for role in ("reviewer_1", "reviewer_2"):
            a, b = self.empty_copies()
            target = a if role == "reviewer_1" else b
            opposite = "reviewer_2" if role == "reviewer_1" else "reviewer_1"
            sign(target, opposite)
            with self.assertRaisesRegex(ValueError, "copy_slot_ownership_violation"):
                self.merge(a, b)
        a, b = self.empty_copies()
        a["evidence_reviews"][0]["adjudication"]["status"] = "pending"
        with self.assertRaisesRegex(ValueError, "copy_slot_ownership_violation"):
            self.merge(a, b)

    def test_23_immutable_evidence_quotes_source_and_hashes_rejected(self):
        changes = (
            lambda d: d["evidence_reviews"][0].update(text="被替换的证据"),
            lambda d: d["evidence_reviews"][0].update(original_quote="改变原引文"),
            lambda d: d["evidence_reviews"][0]["source_input_descriptor"].update(sha256="0" * 64),
            lambda d: d["source_reviews"][0].update(url="https://example.invalid/changed"),
            lambda d: d["model_issue_reviews"][0].update(reason="替换AI意见"),
            lambda d: d["round_reviews"][0]["context_checks"].update({next(iter(d["round_reviews"][0]["context_checks"])): True}),
            lambda d: d["source_inputs"].update({next(iter(d["source_inputs"])): "0" * 64}),
            lambda d: d.update(full_108_card_review=True),
            lambda d: d["review_policy"].update(ai_can_be_human_reviewer=True),
            lambda d: d["input_integrity"].update(unchanged=False),
        )
        for index, change in enumerate(changes):
            d = deepcopy(self.template)
            change(d)
            with self.subTest(index=index):
                self.rejected(d)
                with self.assertRaisesRegex(ValueError, "human_review:"):
                    self.merge(d, deepcopy(self.template))

    def test_24_item_deletion_addition_reorder_duplicate_and_unknown_keys_rejected(self):
        for change in (
            lambda d: d["evidence_reviews"].pop(),
            lambda d: d["evidence_reviews"].append(deepcopy(d["evidence_reviews"][0])),
            lambda d: d["evidence_reviews"].reverse(),
            lambda d: d["evidence_reviews"].__setitem__(1, deepcopy(d["evidence_reviews"][0])),
            lambda d: d["evidence_reviews"][0].update(extra=True),
            lambda d: d.update(fake_signature="ok"),
        ):
            d = deepcopy(self.template)
            change(d)
            self.rejected(d)

    def test_25_check_keys_and_slot_fields_must_match_authority(self):
        for change in (
            lambda s: s["checks"].pop(next(iter(s["checks"]))),
            lambda s: s["checks"].update(extra=True),
            lambda s: s.update(extra="fake"),
            lambda s: s.pop("notes"),
        ):
            d = deepcopy(self.template)
            change(d["evidence_reviews"][0]["reviewer_1"])
            self.rejected(d)

    def test_26_adjudication_only_for_completed_disputed_item(self):
        for setup in ("blank", "one", "agreed"):
            d = deepcopy(self.template)
            if setup != "blank":
                sign(d, "reviewer_1")
            if setup == "agreed":
                sign(d, "reviewer_2")
            adjudicate(d)
            with self.subTest(setup=setup):
                self.rejected(d, "adjudication_without_disagreement")
        a, b = self.pair(left="approved", right="needs_revision")
        third = deepcopy(self.template)
        third["evidence_reviews"][0]["adjudication"]["status"] = "pending"
        self.assertEqual(self.merge(a, b, third)["counts"]["awaiting_adjudication_items"], 1)

    def test_27_adjudication_reason_state_decision_and_fields_are_strict(self):
        a, b = self.pair(left="approved", right="needs_revision")
        for field, value in (("reason", None), ("reason", " "), ("status", "approved"),
                             ("decision", "supported"), ("adjudicator_id", None), ("extra", "bad")):
            third = deepcopy(self.template)
            adjudicate(third)
            third["evidence_reviews"][0]["adjudication"][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "human_review:"):
                self.merge(a, b, third)
        third = deepcopy(self.template)
        third["evidence_reviews"][0]["adjudication"].update(status="pending", reason="不能偷偷预填生效裁决")
        with self.assertRaisesRegex(ValueError, "pending_adjudication_has_content"):
            self.merge(a, b, third)

    def test_28_derived_counters_and_approval_claims_never_trusted(self):
        a, b = self.pair()
        result = self.merge(a, b)
        result["counts"].update(completed_human_slots=110, completed_items=55, passed_items=55)
        result.update(approved_for_experiment=True, formal_eligible=True, content_review_passed=True,
                      all_55_items_resolved=True, identity_verified=True, human_reviews_completed=2)
        result["item_results"]["EV_CL_E01"]["final_decision"] = "rejected"
        verified = self.verify(result)
        self.assertEqual(verified["counts"]["completed_human_slots"], 2)
        self.assertEqual(verified["counts"]["passed_items"], 1)
        self.assertFalse(verified["formal_eligible"] or verified["approved_for_experiment"] or verified["identity_verified"])
        self.assertEqual(verified["human_reviews_completed"], 0)
        for field, value in (("completed_human_slots", 111), ("completed_items", -1), ("completed_items", True)):
            wrong = deepcopy(self.template)
            wrong["counts"][field] = value
            self.rejected(wrong)
        d = deepcopy(self.template)
        d["counts"]["evidence_reviews"] = 19
        self.rejected(d)

    def test_29_all_resolved_is_not_all_passed_and_negative_gates_block(self):
        a, b = self.full_pair()
        for slot, copy in (("reviewer_1", a), ("reviewer_2", b)):
            sign(copy, slot, group="release_gates", index=0, decision="rejected")
        result = self.merge(a, b)
        self.assertTrue(result["all_55_items_resolved"])
        self.assertFalse(result["all_55_items_passed"] or result["release_gates_passed"])
        self.assertTrue(result["content_review_passed"])
        self.assertEqual(result["counts"]["rejected_items"], 1)
        self.assertFalse(result["formal_eligible"])

    def test_30_model_issue_third_resolution_preserves_overstated_opinion(self):
        a, b = self.pair(group="model_issue_reviews", left="acceptable", right="overstated")
        third = deepcopy(self.template)
        adjudicate(third, group="model_issue_reviews", decision="needs_revision")
        result = self.merge(a, b, third)
        self.assertEqual(result["counts"]["adjudicated_items"], 1)
        self.assertEqual(result["counts"]["needs_revision_items"], 1)
        self.assertEqual(result["model_issue_reviews"][0]["reviewer_2"]["decision"], "overstated")

    def test_31_blank_source_template_required_and_input_not_mutated(self):
        a, b = self.pair()
        before = (deepcopy(a), deepcopy(b), deepcopy(self.template))
        result = self.merge(a, b)
        self.assertEqual((a, b, self.template), before)
        result["evidence_reviews"][0]["reviewer_1"]["notes"] = "仅改变结果副本"
        self.assertEqual((a, b, self.template), before)
        with self.assertRaisesRegex(ValueError, "source_template_not_blank_or_changed"):
            self.merge(a, b, template=a)

    def test_32_input_paths_supported_without_writing_signed_files(self):
        signed = deepcopy(self.template)
        sign(signed, "reviewer_1")
        raw = json.dumps(signed, ensure_ascii=False).encode("utf-8")
        original = Path.read_bytes
        with self.authority(), mock.patch.object(Path, "read_bytes", lambda p: raw if p == TEMPLATE else original(p)):
            result = hr.verify_human_review_submission(TEMPLATE, **ARGS)
        self.assertEqual(result["counts"]["completed_human_slots"], 1)
        self.assertEqual(digest(TEMPLATE), self.before[TEMPLATE])
        # 三个输入路径均为真实现有路径，提交内容只在内存中替换。
        a, b = self.pair()
        p1, p2 = BASE / "feasibility.json", BASE / "independent_review.json"
        overlay = {p1: json.dumps(a, ensure_ascii=False).encode(), p2: json.dumps(b, ensure_ascii=False).encode()}
        with self.authority(), mock.patch.object(Path, "read_bytes", lambda p: overlay[p] if p in overlay else original(p)):
            result = hr.merge_and_verify_human_reviews(TEMPLATE, p1, p2, **ARGS)
        self.assertEqual(result["counts"]["passed_items"], 1)

    def test_33_bad_json_duplicate_keys_and_input_types_rejected_safely(self):
        original = Path.read_bytes
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'not json', b'[]'):
            with self.authority(), mock.patch.object(Path, "read_bytes", lambda p: raw if p == TEMPLATE else original(p)):
                with self.assertRaisesRegex(ValueError, "^human_review:"):
                    hr.verify_human_review_submission(TEMPLATE, **ARGS)
        for value in (None, [], 1, "https://example.invalid/review.json", "//server/review.json"):
            self.rejected(value)

    def test_34_submission_file_and_source_change_before_return_rejected(self):
        original = Path.read_bytes
        reads = 0

        def changing(path):
            nonlocal reads
            raw = original(path)
            if path == TEMPLATE:
                reads += 1
                return raw if reads == 1 else raw + b"\n"
            return raw

        with self.authority(), mock.patch.object(Path, "read_bytes", changing):
            with self.assertRaisesRegex(ValueError, "submission_changed"):
                hr.verify_human_review_submission(TEMPLATE, **ARGS)
        target = Path(next(iter(self.template["source_inputs"])))
        with self.authority(), mock.patch.object(Path, "read_bytes", lambda p: original(p) + (b" " if p == target else b"")):
            with self.assertRaisesRegex(ValueError, "source_changed_during_verification"):
                hr.verify_human_review_submission(self.template, **ARGS)

    def test_35_summary_types_cannot_bypass_immutable_fields(self):
        for field, value in (("approved_for_experiment", 1), ("formal_eligible", 0),
                             ("human_reviews_completed", False), ("human_reviews_completed", 3),
                             ("status", "approved"), ("status", {})):
            d = deepcopy(self.template)
            d[field] = value
            self.rejected(d)
        for value in (True, -1, 3):
            d = deepcopy(self.template)
            d["evidence_reviews"][0]["human_reviews_completed"] = value
            self.rejected(d)

    def test_36_wrong_workflow_version_or_template_binding_rejected(self):
        result = self.merge(*self.pair())
        for key, value in (("workflow_version", "untrusted-v2"), ("template_sha256", "0" * 64)):
            forged = deepcopy(result)
            forged[key] = value
            self.rejected(forged)

    def test_37_same_decision_different_notes_is_not_a_dispute(self):
        a, b = self.pair()
        b["evidence_reviews"][0]["reviewer_2"]["notes"] += "独立说明可以不同"
        result = self.merge(a, b)
        self.assertEqual(result["counts"]["disagreement_items"], 0)
        self.assertEqual(result["counts"]["passed_items"], 1)

    def test_38_adjudication_check_keys_cannot_be_added_or_removed(self):
        a, b = self.pair(left="approved", right="needs_revision")
        for operation in ("add", "remove"):
            third = deepcopy(self.template)
            checks = dict.fromkeys(a["evidence_reviews"][0]["reviewer_1"]["checks"], True)
            if operation == "add":
                checks["unexpected"] = True
            else:
                checks.pop(next(iter(checks)))
            adjudicate(third, checks=checks)
            with self.assertRaisesRegex(ValueError, "adjudication_check_keys_changed"):
                self.merge(a, b, third)

    def test_39_old_module_prefix_preserved_and_merged_output_is_json_safe(self):
        code = (ROOT / "cocoon/human_review.py").read_bytes()
        self.assertEqual(hashlib.sha256(code[:25853]).hexdigest(), "b93cfab68063c236f6d0b70aa5f209130bf4aaec40fd9a41aab2af33ff4fb846")
        result = self.merge(*self.pair())
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False)), result)
        self.assertFalse(hr.validate_human_review_template(result, BUILD, SEMANTIC))
        self.assertEqual(self.before, {p: digest(p) for p in self.before})


if __name__ == "__main__":
    unittest.main(verbosity=2)
