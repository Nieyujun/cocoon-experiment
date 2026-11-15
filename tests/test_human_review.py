"""仅核查离线人审模板；临时副本保留在tests，不修改任何原始输入。"""

from collections import Counter
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from cocoon.evidence import verify_bundle
from cocoon.human_review import (
    build_human_review_checklist, validate_human_review_checklist, validate_human_review_template,
)
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"
RUN = ROOT / "experiment/mimo_probes/assistant_probe_20261108T082120_9ad78cf8"
GROUPS = ("evidence_reviews", "source_reviews", "model_issue_reviews", "round_reviews", "release_gates")


def load(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cards(data):
    return [item for group in GROUPS for item in data[group]]


class HumanReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load(BUILD / "manifest.json")
        cls.evidence = load(BUILD / "review/evidence_review.json")["items"]
        cls.sources = load(BUILD / "review/sources.json")["sources"]
        cls.semantic = load(SEMANTIC)
        cls.summary = load(RUN / "summary.json")
        inputs = {BUILD / "manifest.json", SEMANTIC, RUN / "summary.json"}
        inputs.update(BUILD / row["path"] for row in cls.manifest["files"])
        source_root = Path(cls.manifest["source_root"])
        inputs.update(source_root / topic["source_material"] for topic in cls.manifest["topics"])
        inputs.update(source_root / source[key] for source in cls.sources
                      for key in ("intake_meta", "intake_raw", "intake_text"))
        inputs.update(RUN / row[key]["ref"] for row in cls.summary["stage_results"].values()
                      for key in ("input", "response"))
        cls.before = {path.resolve().as_posix(): sha(path) for path in inputs}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络调用")):
            cls.data = build_human_review_checklist(BUILD, SEMANTIC)
        if not (ROOT / "tests").is_dir():
            raise AssertionError("测试父目录不存在")
        cls.temp = Path(tempfile.mkdtemp(prefix="human-review-", dir=ROOT / "tests"))
        cls.sequence = itertools.count()

    def semantic_copy(self, change):
        value = deepcopy(self.semantic)
        change(value)
        path = self.temp / f"semantic_{next(self.sequence)}.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        return path

    def assert_rejected(self, semantic=SEMANTIC, build=BUILD):
        with self.assertRaises(ValueError):
            build_human_review_checklist(build, semantic)
        self.assertFalse(validate_human_review_checklist(self.data, build, semantic))

    def test_01_counts_and_stable_ids(self):
        expected = dict(zip(GROUPS, (18, 13, 11, 6, 7)))
        self.assertEqual({group: len(self.data[group]) for group in GROUPS}, expected)
        self.assertEqual(self.data["counts"], {
            **expected, "total_items": 55, "double_human_slots": 110,
            "completed_items": 0, "completed_human_slots": 0,
        })
        self.assertEqual(len({item["id"] for item in cards(self.data)}), 55)
        self.assertEqual(self.data["live_semantic_items_reviewed"], 39)
        self.assertEqual(self.data["live_semantic_counts"], self.semantic["counts"])
        self.assertIn("EV_CL_E01", {item["id"] for item in self.data["evidence_reviews"]})
        for item in cards(self.data):
            self.assertRegex(item["id"], r"^[A-Za-z][A-Za-z0-9_]*$")
            descriptor = item["source_input_descriptor"]
            self.assertEqual(descriptor["sha256"], self.data["source_inputs"][descriptor["path"]])
            self.assertRegex(descriptor["object_sha256"], r"^[0-9a-f]{64}$")

    def test_02_all_human_fields_pending_and_independent(self):
        for item in cards(self.data):
            with self.subTest(item=item["id"]):
                self.assertEqual(item["human_reviews_completed"], 0)
                for name in ("reviewer_1", "reviewer_2"):
                    reviewer = item[name]
                    self.assertEqual(set(reviewer), {"reviewer_id", "reviewed_at", "decision", "notes", "checks"})
                    self.assertEqual(reviewer["decision"], "pending")
                    self.assertTrue(all(reviewer[key] is None for key in ("reviewer_id", "reviewed_at", "notes")))
                    self.assertTrue(reviewer["checks"])
                    self.assertTrue(all(value is None for value in reviewer["checks"].values()))
                self.assertIsNot(item["reviewer_1"], item["reviewer_2"])
                self.assertIsNot(item["reviewer_1"]["checks"], item["reviewer_2"]["checks"])
                self.assertEqual(item["adjudication"], {
                    "status": "not_started", "adjudicator_id": None, "decision": "pending", "reason": None,
                })
        changed = deepcopy(self.data)
        first = changed["evidence_reviews"][0]
        first["reviewer_1"]["checks"]["原文支持"] = True
        self.assertIsNone(first["reviewer_2"]["checks"]["原文支持"])
        self.assertIsNone(changed["evidence_reviews"][1]["reviewer_1"]["checks"]["原文支持"])

    def test_03_original_evidence_and_quote_provenance(self):
        for original, item in zip(self.evidence, self.data["evidence_reviews"]):
            for key in ("text", "original_quote", "boundary_note", "source_id", "quote_start", "quote_end",
                        "content_sha256", "review_dimension", "transformation_type"):
                self.assertEqual(item[key], original[key])
            source = original["source"]
            for key in ("title", "url", "publication_date", "source_family_id"):
                self.assertEqual(item[key], source[key])
            self.assertEqual(item["source_input_descriptor"]["object_sha256"], canonical_hash(original))
            blob = item["blob_paths"]["text"]
            text = load(Path(blob["path"]))["text"]
            self.assertEqual(text[item["quote_start"]:item["quote_end"]], item["original_quote"])
            self.assertEqual(sha(Path(blob["path"])), blob["sha256"])
            self.assertLessEqual(item["quote_budget"]["direct_quote_units"], item["quote_budget"]["direct_quote_limit"])
            self.assertEqual(set(item["reviewer_1"]["checks"]),
                             {"原文支持", "中文释义", "数字日期否定模态", "引用对应", "语境保留"})

    def test_04_shared_source_families_are_not_independent_votes(self):
        sources = {row["source_id"]: row for row in self.data["source_reviews"]}
        for left, right in (("CL_S01", "CL_S03"), ("EN_S07", "EN_S08")):
            self.assertEqual(sources[left]["source_family_id"], sources[right]["source_family_id"])
            self.assertEqual(set(sources[left]["same_family_source_ids"]), {left, right})
        self.assertEqual(self.data["source_family_count"], 11)
        self.assertIs(self.data["source_family_count_is_independence_proof"], False)
        self.assertEqual(sources["EN_S07"]["source_level"], "secondary_report")
        self.assertEqual(sources["EN_S08"]["source_level"], "secondary_report")
        self.assertIsNone(sources["CL_S03"]["publication_date"])
        for original in self.sources:
            item = sources[original["source_id"]]
            self.assertEqual(item["metadata"], original)
            self.assertEqual(item["license_note"], original["license_note"])
            self.assertEqual(item["rights_status"], "pending_human_review_no_legal_conclusion")
            self.assertIsNone(item["reviewer_1"]["checks"]["独立性及同源转引"])

    def test_05_eleven_issues_are_exact_original_items(self):
        expected = [row for row in self.semantic["items"] if row["verdict"] in ("unsupported_fact", "overstated")]
        actual = self.data["model_issue_reviews"]
        self.assertEqual(Counter(item["verdict"] for item in actual), {"unsupported_fact": 3, "overstated": 8})
        self.assertEqual(len(actual), len(expected))
        for original, item in zip(expected, actual):
            self.assertEqual({key: item[key] for key in original}, original)
            self.assertEqual(item["id"], f"MODEL_{original['stage']}_{original['section']}_{original['item_index']}")
            sections = self.summary["stage_results"][item["stage"]]["output"]["sections"]
            section = next(section for section in sections if section["name"] == item["section"])
            self.assertEqual({key: item[key] for key in ("text", "citations", "epistemic_status")},
                             section["items"][item["item_index"]])
            self.assertEqual(item["stage_response_descriptor"]["sha256"],
                             self.summary["stage_results"][item["stage"]]["response"]["sha256"])
            self.assertEqual(item["assessment_role"], "ai_opinion_only_human_decision_pending")
            self.assertEqual(item["packet_descriptor"]["object_sha256"], self.semantic["packet_sha256"])

    def test_06_round_contexts_and_release_gates_remain_pending(self):
        hashes = set()
        for item in self.data["round_reviews"]:
            packet = load(Path(item["source_input_descriptor"]["path"]))
            self.assertEqual(item["evidence_ids"], [row["evidence_id"] for row in packet["evidence"]])
            self.assertEqual(len(item["evidence_ids"]), 3)
            self.assertEqual(item["evidence"], packet["evidence"])
            self.assertEqual(item["packet_sha256"], canonical_hash(packet))
            self.assertEqual(item["packet_file_sha256"], item["source_input_descriptor"]["sha256"])
            hashes.add(item["packet_sha256"])
            self.assertIsNone(item["selected_sentences"])
            self.assertIsNone(item["natural_T1_available"])
            self.assertIsNone(item["main_page_cross_mapping"]["organized_output_descriptor"])
            self.assertIsNone(item["main_page_cross_mapping"]["extractive_output_descriptor"])
            self.assertTrue(all(value is None for value in item["context_checks"].values()))
            self.assertEqual(item["reviewer_1"]["checks"], item["context_checks"])
            self.assertEqual(item["dose"], {"min_chars": 600, "max_chars": 800,
                                           "total_match_tolerance": 0.1, "additional_match_tolerance": 0.1,
                                           "status": "pending"})
            self.assertIs(item["full_I4_eligible"], False)
        self.assertEqual(len(hashes), 6)
        self.assertEqual({item["id"] for item in self.data["release_gates"]}, {
            "GATE_DISTINCT_REVIEWERS", "GATE_THIRD_ADJUDICATOR", "GATE_SOURCE_RIGHTS", "GATE_TOPIC_AXIS",
            "GATE_GOLD_ISOLATION", "GATE_REAL_T1_FULL_I4", "GATE_OASIS_MODEL_BUDGET",
        })
        for gate in self.data["release_gates"]:
            self.assertIs(gate["automatic_completion"], False)
            self.assertEqual(gate["scope"], "outside_current_lightweight_template")
        self.assertIs(self.data["full_108_card_review"], False)

    def test_07_strict_template_validation_never_auto_approves(self):
        self.assertTrue(validate_human_review_template(self.data, BUILD, SEMANTIC))
        self.assertTrue(validate_human_review_checklist(self.data, BUILD, SEMANTIC))
        changes = (
            lambda d: d.update(human_reviews_completed=False),
            lambda d: d.update(approved_for_experiment=True),
            lambda d: d.update(machine_signature="passed"),
            lambda d: d["evidence_reviews"][0]["reviewer_1"]["checks"].update(原文支持=False),
            lambda d: d["counts"].update(double_human_slots=0),
            lambda d: d["source_inputs"].update({str(SEMANTIC): "0" * 64}),
        )
        for number, change in enumerate(changes):
            with self.subTest(case=number):
                altered = deepcopy(self.data)
                change(altered)
                self.assertFalse(validate_human_review_template(altered, BUILD, SEMANTIC))
        self.assertEqual(self.data["human_reviews_completed"], 0)
        self.assertIs(self.data["approved_for_experiment"], False)
        self.assertIs(self.data["review_policy"]["machine_checks_can_approve"], False)
        self.assertIs(self.data["input_integrity"]["is_digital_signature"], False)

    def test_08_machine_checks_and_same_person_double_signatures_rejected(self):
        machine_only = deepcopy(self.data)
        for item in cards(machine_only):
            for name in ("reviewer_1", "reviewer_2"):
                item[name]["checks"] = dict.fromkeys(item[name]["checks"], True)
        self.assertFalse(validate_human_review_template(machine_only, BUILD, SEMANTIC))
        for second_id in ("test-human-1", "test-human-2"):
            signed = deepcopy(machine_only)
            for item in cards(signed):
                for name, identifier in (("reviewer_1", "test-human-1"), ("reviewer_2", second_id)):
                    item[name].update(reviewer_id=identifier, reviewed_at="2026-11-08T00:00:00Z", decision="acceptable")
            self.assertFalse(validate_human_review_checklist(signed, BUILD, SEMANTIC))
        self.assertIs(self.data["review_policy"]["same_person_double_signing_allowed"], False)
        self.assertIs(self.data["review_policy"]["ai_can_be_human_reviewer"], False)
        self.assertEqual(self.data["review_policy"]["validation_scope"], "initial_unfilled_template_only")

    def test_09_corrupt_hashes_and_original_blobs_rejected(self):
        for field in ("summary_sha256", "packet_sha256"):
            with self.subTest(field=field):
                self.assert_rejected(self.semantic_copy(lambda value: value.update({field: "0" * 64})))
        copied = self.temp / "tampered_build"
        copied.mkdir()
        (copied / "manifest.json").write_bytes((BUILD / "manifest.json").read_bytes())
        for entry in self.manifest["files"]:
            path = copied / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((BUILD / entry["path"]).read_bytes())
        path = copied / "review/sources.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.assert_rejected(build=copied)
        read_bytes = Path.read_bytes
        source_root = Path(self.manifest["source_root"])
        paths = [RUN / "summary.json", source_root / self.sources[0]["intake_raw"],
                 source_root / self.sources[0]["intake_text"],
                 RUN / self.summary["stage_results"]["search"]["input"]["ref"],
                 RUN / self.summary["stage_results"]["search"]["response"]["ref"]]
        for target in paths:
            with self.subTest(path=target):
                def tampered(path, target=target.resolve()):
                    raw = read_bytes(path)
                    if path.resolve() != target:
                        return raw
                    if path.name.endswith(".text.json"):
                        value = json.loads(raw)
                        value["text"] += "非原文"
                        return json.dumps(value, ensure_ascii=False).encode("utf-8")
                    return raw + b" "
                with mock.patch.object(Path, "read_bytes", tampered):
                    self.assert_rejected()

    def test_10_all_39_items_and_all_count_breakdowns_bound(self):
        changes = (
            lambda d: d["items"][0].update(text="非原始模型文本"),
            lambda d: d["items"][0].update(citations=[]),
            lambda d: d["items"][0].update(epistemic_status="uncertainty"),
            lambda d: d["items"].pop(),
            lambda d: d["items"].append(deepcopy(d["items"][0])),
            lambda d: d["counts"]["by_stage"]["search"].update(total=8),
            lambda d: d["counts"]["by_severity"].update(high=0),
            lambda d: d["counts"].update(unsupported_fact=0),
            lambda d: d["counts"].update(report_duplicate_items_not_recounted=0),
            lambda d: d["counts"].update(raw_text_output_equal_stages=0),
            lambda d: d.update(items_reviewed=38),
            lambda d: d.update(status="pass_with_limitations"),
        )
        for number, change in enumerate(changes):
            with self.subTest(case=number):
                self.assert_rejected(self.semantic_copy(change))
        read_bytes = Path.read_bytes
        for field in ("raw_text", "output", "report"):
            altered = deepcopy(self.summary)
            if field == "raw_text":
                altered["stage_results"]["search"]["raw_text"] += " "
            else:
                output = altered["report"] if field == "report" else altered["stage_results"]["search"]["output"]
                output["sections"][0]["items"][0]["text"] += "非原文"
            raw = json.dumps(altered, ensure_ascii=False).encode("utf-8")
            semantic_path = self.semantic_copy(lambda d: d.update(summary_sha256=hashlib.sha256(raw).hexdigest()))
            def replacement(path, raw=raw):
                return raw if path.resolve() == (RUN / "summary.json").resolve() else read_bytes(path)
            with self.subTest(summary_field=field), mock.patch.object(Path, "read_bytes", replacement):
                self.assert_rejected(semantic_path)

    def test_11_unsafe_run_and_blob_paths_rejected(self):
        for run_id in ("../summary", "assistant_probe_../../outside", "//outside/summary"):
            self.assert_rejected(self.semantic_copy(lambda d: d.update(run_id=run_id)))
        altered = deepcopy(self.summary)
        altered["stage_results"]["search"]["response"]["ref"] = "../outside.json"
        raw = json.dumps(altered, ensure_ascii=False).encode("utf-8")
        semantic_path = self.semantic_copy(lambda d: d.update(summary_sha256=hashlib.sha256(raw).hexdigest()))
        read_bytes = Path.read_bytes
        def replacement(path):
            return raw if path.resolve() == (RUN / "summary.json").resolve() else read_bytes(path)
        with mock.patch.object(Path, "read_bytes", replacement):
            self.assert_rejected(semantic_path)

    def test_12_read_only_deterministic_unicode_and_inputs_unchanged(self):
        original_open = Path.open
        def read_only(path, mode="r", *args, **kwargs):
            if any(character in mode for character in "wax+"):
                raise AssertionError("模板构建不得写文件")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", read_only), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络调用")), \
                mock.patch("cocoon.human_review.verify_bundle", wraps=verify_bundle) as verifier:
            rebuilt = build_human_review_checklist(BUILD, SEMANTIC)
        verifier.assert_called_once_with(BUILD.resolve())
        self.assertEqual(rebuilt, self.data)
        serialized = json.dumps(rebuilt, ensure_ascii=False, allow_nan=False)
        self.assertIn("原文支持", serialized)
        self.assertEqual(json.loads(serialized), rebuilt)
        self.assertEqual(self.before, {name: sha(Path(name)) for name in self.before})
        self.assertEqual(rebuilt["source_inputs"], self.before)
        self.assertEqual(rebuilt["input_integrity"]["before"], self.before)
        self.assertEqual(rebuilt["input_integrity"]["after"], self.before)
        self.assertIs(rebuilt["input_integrity"]["unchanged"], True)
        self.assertEqual(rebuilt["use_scope"], "reviewer_only_never_subject")
        self.assertEqual(rebuilt["new_model_calls"], 0)
        self.assertIs(rebuilt["no_model_output_edits"], True)
        self.assertIs(rebuilt["contains_final_test_answers"], False)
        self.assertIs(rebuilt["formal_eligible"], False)


if __name__ == "__main__":
    unittest.main()
