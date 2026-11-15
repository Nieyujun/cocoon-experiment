"""仅运行本文件；所有篡改均在内存覆盖，不写冻结输入或生成交付文件。"""

import sys
sys.dont_write_bytecode = True

import ast
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import io
from itertools import product
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon import delivery, delivery_audit, dosage, extractive_control as control, mimo
from cocoon.dose_build import source_index_from_review
from cocoon.runtime import _parse_json, canonical_hash

BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
POLICY = ROOT / "configs/extractive_control_v1.json"
POLICY_SHA = "b96e2a12d9b85189e6765c0aa23b04a6f2ad2bec6d57a8d73194d34c2fe5413a"
RUN = ROOT / "experiment/delivery_probes/delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
ALT = ROOT / "experiment/review/delivery_v3_20261108/extractive_alternative.json"
CASE_KEYS = [(topic, number) for topic in ("TOPIC_01", "TOPIC_02", "TOPIC_03") for number in (1, 2)]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hashes(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def expand(ranges):
    return [value for low, high in ranges for value in range(low, high + 1)]


@contextmanager
def overlay(path, data):
    reader = Path.read_bytes
    with mock.patch.object(Path, "read_bytes", lambda p: data if p == path else reader(p)):
        yield


@contextmanager
def offline(recorded=False):
    """禁止传输、环境、凭据和写入；未传旧备选时连旧响应文件也不读取。"""
    with ExitStack() as stack:
        guards = []
        for obj, name in (
            (delivery.MiMoTransport, "complete"), (delivery.MiMoTransport, "_exchange"),
            (delivery, "run_delivery"), (delivery, "load_key"), (mimo, "load_key"),
            (mimo, "credential_status"), (delivery, "_ledger"), (delivery, "_guard"),
            (dosage, "make_dose_pair"), (delivery_audit, "make_dose_pair"),
            (os, "getenv"), (os, "open"), (Path, "write_bytes"), (Path, "write_text"),
            (Path, "mkdir"), (Path, "unlink"), (Path, "rename"), (Path, "replace"),
        ):
            guards.append(stack.enter_context(mock.patch.object(
                obj, name, side_effect=AssertionError("禁止调用或写入"))))
        for name in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                     "socket.socket.sendall", "socket.socket.sendto", "socket.getaddrinfo",
                     "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            guards.append(stack.enter_context(mock.patch(name, side_effect=AssertionError("禁止联网"))))

        class NoEnvironment(dict):
            def __getitem__(self, key):
                raise AssertionError("禁止读取环境")

            def get(self, key, default=None):
                raise AssertionError("禁止读取环境")

        stack.enter_context(mock.patch.object(os, "environ", NoEnvironment()))
        original_open = io.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("禁止写文件")
            if isinstance(file, (str, os.PathLike)):
                path = Path(file)
                if path.suffix.lower() == ".md" or any(
                    re.search(r"(?i)credential|secret|\.env|api[_-]?key|ledger", part) for part in path.parts
                ):
                    raise AssertionError("禁止读取凭据、账本或文档")
                if not recorded and any(part in ("delivery_probes", "mimo_probes") for part in path.parts):
                    raise AssertionError("理论可行性不得读取旧模型响应")
            return original_open(file, mode, *args, **kwargs)

        stack.enter_context(mock.patch("io.open", guarded_open))
        stack.enter_context(mock.patch("builtins.open", guarded_open))
        try:
            yield
        finally:
            for guard in guards:
                guard.assert_not_called()


class ExtractiveFeasibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_bytes = POLICY.read_bytes()
        cls.policy = _parse_json(cls.policy_bytes)
        cls.manifest = _parse_json((BUILD / "manifest.json").read_bytes())
        cls.packets = [_parse_json((BUILD / f"packets/{topic}_round_{number}.json").read_bytes())
                       for topic, number in CASE_KEYS]
        cls.index = source_index_from_review(BUILD)
        cls.run_summary = _parse_json((RUN / "summary.json").read_bytes())
        cls.parent = Path(cls.run_summary["parent_source_path"])
        cls.roots = [BUILD, RUN, cls.parent] + [RUN.parent / name for name in (
            "delivery_20261108T090523Z_fb38842073a64ca4ae95b42c082d6f7e",
            "delivery_20261108T101439Z_99cfd0db64294c3a8c51b1248b03eb98",
        )]
        cls.snapshots = {str(root): hashes(root) for root in cls.roots}
        cls.protected = {p: sha(p.read_bytes()) for p in (
            POLICY, ALT, Path(cls.run_summary["policy_input"]["source_path"]), Path(__file__),
            ROOT / "cocoon/extractive_control.py", ROOT / "cocoon/dosage.py", ROOT / "cocoon/dose_build.py",
            ROOT / "cocoon/evidence.py", ROOT / "cocoon/delivery.py", ROOT / "cocoon/delivery_audit.py",
            ROOT / "cocoon/runtime.py",
        )}
        with offline():
            cls.result = control.inspect_extractive_feasibility(BUILD, POLICY, POLICY_SHA)
        with offline(recorded=True):
            cls.recorded = control.inspect_extractive_feasibility(
                BUILD, POLICY, POLICY_SHA, recorded_alternative_path=ALT, recorded_run_directory=RUN)

    @classmethod
    def tearDownClass(cls):
        for root in cls.roots:
            if hashes(root) != cls.snapshots[str(root)]:
                raise AssertionError("冻结来源或旧运行文件集合改变：" + str(root))
        for path, digest in cls.protected.items():
            if sha(path.read_bytes()) != digest:
                raise AssertionError("原文件发生写入：" + str(path))

    def inspect(self, recorded=False):
        with offline(recorded):
            return control.inspect_extractive_feasibility(
                BUILD, POLICY, POLICY_SHA, recorded_alternative_path=ALT if recorded else None,
                recorded_run_directory=RUN if recorded else None)

    def verify(self, data, recorded=False):
        with offline(recorded):
            return control.verify_extractive_feasibility(
                data, BUILD, POLICY, POLICY_SHA, recorded_alternative_path=ALT if recorded else None,
                recorded_run_directory=RUN if recorded else None)

    def test_01_all_six_cases_are_conditional_without_organized_output(self):
        cases = self.result["cases"]
        self.assertEqual([(c["topic_id"], c["round"]) for c in cases], CASE_KEYS)
        self.assertEqual([c["fixed_chars"] for c in cases], [457, 459, 492, 475, 513, 557])
        self.assertEqual([c["feasible_length_count"] for c in cases], [92, 74, 113, 98, 74, 88])
        self.assertEqual([c["total_combinations"] for c in cases], [8, 12, 8, 8, 18, 18])
        self.assertEqual([c["dose_eligible_combinations"] for c in cases], [7, 11, 8, 8, 18, 18])
        self.assertEqual(self.result["summary"]["conditional_only_cases"], 6)
        self.assertEqual(self.result["summary"]["actual_pair_available_cases"], 0)
        for case, packet in zip(cases, self.packets):
            self.assertEqual(case["title"], packet["topic"])
            self.assertEqual(case["title_metadata"]["packet_topic"], packet["topic"])
            self.assertEqual(case["packet_id"], packet["packet_id"])
            self.assertEqual(case["status"], "conditional_only")
            self.assertTrue(case["conditional_length_feasible"])
            self.assertIs(case["actual_organized_output_available"], False)
            self.assertIs(case["actual_pair_available"], False)
            for key in case:
                if key.startswith("actual_") and not key.endswith("available"):
                    self.assertIsNone(case[key], key)

    def test_02_only_canonical_first_packet_binds_recorded_exploratory_candidate(self):
        first = self.recorded["cases"][0]
        self.assertEqual(first["status"], "recorded_exploratory_candidate")
        self.assertIs(first["actual_organized_output_available"], True)
        self.assertIs(first["actual_pair_available"], True)
        self.assertEqual([first[k] for k in ("actual_organized_chars", "actual_plain_chars",
                                           "actual_organized_total_chars", "actual_plain_total_chars")],
                         [175, 180, 632, 637])
        self.assertEqual(first["actual_combination_index"], 1)
        self.assertEqual(first["actual_source_run_id"], self.run_summary["run_id"])
        self.assertEqual(first["actual_source_event_ids"], self.run_summary["source_event_ids"])
        self.assertEqual(first["packet_sha256"], canonical_hash(self.packets[0]))
        self.assertIn("not_generated_this_inspection", first["actual_content_origin"])
        self.assertEqual(self.recorded["cases"][1:], self.result["cases"][1:])
        self.assertEqual(self.recorded["summary"]["actual_pair_available_cases"], 1)
        self.assertEqual(self.recorded["summary"]["recorded_exploratory_candidate_cases"], 1)
        for case in self.recorded["cases"]:
            self.assertIs(case["full_I4_eligible"], False)
            self.assertIs(case["formal_eligible"], False)

    def test_03_every_integer_and_every_combination_match_independent_enumeration(self):
        for case, packet in zip(self.result["cases"], self.packets):
            with self.subTest(case=case["case_id"]):
                fixed = len(control._visible(dosage.compact_evidence(packet, self.index)["evidence_block"], ""))
                self.assertEqual(case["fixed_chars"], fixed)
                low, high = max(1, 600 - fixed), 800 - fixed
                self.assertEqual(case["allowed_org_range"], [low, high])
                pools = []
                for entry in packet["evidence"]:
                    spans = [m.span() for m in re.finditer(r'.*?[。！？](?:[”’"])?', entry["text"], re.DOTALL)]
                    spans[-1] = (spans[-1][0], len(entry["text"]))
                    pools.append([(i, start, end, end - start + len(entry["evidence_id"]) + 2)
                                  for i, (start, end) in enumerate(spans)
                                  if "？" not in entry["text"][start:end] and "?" not in entry["text"][start:end]])
                all_combos = list(product(*pools))
                self.assertEqual(len(all_combos), case["total_combinations"])
                matching, choices = [], {}
                for number, (combo, row) in enumerate(zip(all_combos, case["candidate_combinations"])):
                    size = sum(s[3] for s in combo)
                    self.assertEqual(row["combination_index"], number)
                    self.assertEqual(row["combination_id"], f"combo_{number:05d}")
                    self.assertEqual(row["sentence_indices"], [s[0] for s in combo])
                    self.assertEqual(row["offsets"], [{"evidence_id": e["evidence_id"], "start": s[1], "end": s[2]}
                                                     for e, s in zip(packet["evidence"], combo)])
                    self.assertEqual(row["plain_additional_chars"], size)
                    self.assertEqual(row["plain_visible_chars"], fixed + size)
                    self.assertEqual(row["dose_in_range"], 600 <= fixed + size <= 800)
                    lengths = [k for k in range(low, high + 1)
                               if 600 <= fixed + size <= 800 and 600 <= fixed + k <= 800
                               and 10 * abs(k - size) <= min(k, size)
                               and 10 * abs((fixed + k) - (fixed + size)) <= min(fixed + k, fixed + size)]
                    self.assertEqual(expand(row["matching_organized_ranges"]), lengths)
                    matching.append(set(lengths))
                for k in range(low, high + 1):
                    choices[str(k)] = next((i for i, values in enumerate(matching) if k in values), None)
                union = sorted(set().union(*matching))
                self.assertEqual(case["first_selection_by_organized_length"], choices)
                self.assertEqual(expand(case["feasible_organized_ranges"]), union)
                self.assertEqual(case["feasible_length_count"], len(union))
                self.assertEqual(case["total_allowed_length_count"], high - low + 1)
                self.assertEqual(case["coverage_ratio"], len(union) / (high - low + 1))

    def test_04_all_sentence_spans_preserve_frozen_chinese_body_and_sources(self):
        for case, packet in zip(self.result["cases"], self.packets):
            for pool, entry in zip(case["sentence_pools"], packet["evidence"]):
                self.assertEqual(pool["content_origin"], "冻结中文证据正文整句")
                self.assertEqual(pool["text_sha256"], entry["content_sha256"])
                self.assertEqual("".join(s["text"] for s in pool["sentences"]), entry["text"])
                for i, sentence in enumerate(pool["sentences"]):
                    self.assertEqual(sentence["sentence_index"], i)
                    self.assertEqual(sentence["text"], entry["text"][sentence["start"]:sentence["end"]])
                    for key in ("evidence_id", "source_url", "source_family_id"):
                        self.assertEqual(sentence[key], entry[key])
                        self.assertEqual(pool[key], entry[key])
                self.assertEqual(pool["sentences"][0]["start"], 0)
                self.assertEqual(pool["sentences"][-1]["end"], len(entry["text"]))
        self.assertIn("不标作英文来源原句", self.result["protocol_limits"]["sentence_origin"])

    def test_05_selector_is_reused_for_every_allowed_integer_without_model_text(self):
        with mock.patch.object(control, "select_exact_sentences", wraps=control.select_exact_sentences) as selector:
            result = self.inspect()
        expected = [(canonical_hash(p["evidence"]), k, c["fixed_chars"])
                    for p, c in zip(self.packets, result["cases"])
                    for k in range(c["allowed_org_range"][0], c["allowed_org_range"][1] + 1)]
        actual = []
        for call in selector.call_args_list:
            evidence, k, fixed, policy = call.args
            self.assertIs(type(k), int)
            self.assertEqual(policy, self.policy)
            self.assertEqual(call.kwargs, {})
            actual.append((canonical_hash(evidence), k, fixed))
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 1206)

    def test_06_integer_tolerance_and_dose_endpoints_are_inclusive(self):
        for fixed, left, right, valid in (
            (500, 100, 110, True), (500, 110, 100, True),
            (500, 100, 111, False), (500, 111, 100, False),
            (500, 99, 100, False), (500, 300, 300, True), (500, 301, 300, False),
            (0, 600, 660, True), (0, 600, 661, False), (0, 800, 800, True),
            (600, 0, 1, False),
        ):
            with self.subTest(values=(fixed, left, right)):
                self.assertIs(control._feasibility_length_match(fixed, left, right), valid)
        for case in self.result["cases"]:
            low, high = case["allowed_org_range"]
            self.assertEqual(case["fixed_chars"] + low, 600)
            self.assertEqual(case["fixed_chars"] + high, 800)
            self.assertEqual(set(case["first_selection_by_organized_length"]),
                             {str(k) for k in range(low, high + 1)})

    def test_07_disjoint_integer_ranges_never_merge_across_gaps(self):
        self.assertEqual(control._feasibility_ranges([7, 2, 1, 2, 8, 10]), [[1, 2], [7, 8], [10, 10]])
        self.assertEqual(control._feasibility_ranges([]), [])
        entry = deepcopy(self.packets[0]["evidence"][0])
        entry["text"] = "甲" * 91 + "。" + "乙" * 191 + "。"
        output = control._inspect_extractive_lengths([entry], 500, self.policy)
        self.assertEqual(output["feasible_organized_ranges"], [[100, 110], [182, 220]])
        self.assertEqual(output["feasible_length_count"], 50)
        self.assertTrue(all(output["first_selection_by_organized_length"][str(k)] is None for k in range(111, 182)))
        self.assertFalse(output["actual_pair_available"])

    def test_08_impossible_dose_empty_budget_and_question_only_never_create_pairs(self):
        evidence = deepcopy(self.packets[0]["evidence"])
        impossible = control._inspect_extractive_lengths(evidence, 0, self.policy)
        overfull = control._inspect_extractive_lengths(evidence, 801, self.policy)
        evidence[0]["text"] = "只有问句？ASCII?也排除。\n"
        questions = control._inspect_extractive_lengths(evidence, 457, self.policy)
        for row in (impossible, overfull, questions):
            self.assertEqual(row["status"], "no_length_solution")
            self.assertFalse(row["conditional_length_feasible"])
            self.assertFalse(row["actual_pair_available"])
            self.assertIsNone(row["actual_organized_chars"])
            self.assertEqual(row["feasible_organized_ranges"], [])
            self.assertEqual(row["coverage_ratio"], 0)
            self.assertTrue(all(value is None for value in row["first_selection_by_organized_length"].values()))
        self.assertEqual(impossible["dose_eligible_combinations"], 0)
        self.assertIsNone(overfull["allowed_org_range"])
        self.assertEqual(overfull["total_allowed_length_count"], 0)
        self.assertEqual(questions["total_combinations"], 0)
        self.assertEqual(len(questions["sentence_pools"][0]["sentences"]), 2)
        self.assertFalse(any(s["selector_eligible"] for s in questions["sentence_pools"][0]["sentences"]))
        evidence[0]["text"] = "问句？\n短句。\t "
        allowed = control._inspect_extractive_lengths([evidence[0]], 650, self.policy)
        self.assertEqual(allowed["allowed_org_range"], [1, 150])
        self.assertEqual(allowed["candidate_combinations"][0]["sentence_indices"], [1])
        self.assertEqual(allowed["sentence_pools"][0]["sentences"][-1]["end"], len(evidence[0]["text"]))

    def test_09_combination_limit_is_checked_before_product_and_allows_10000(self):
        evidence = [deepcopy(self.packets[0]["evidence"][0]) for _ in range(4)]
        for entry in evidence:
            entry["text"] = "完整句。" * 11
        with mock.patch.object(control, "product", side_effect=AssertionError("超限不得枚举")) as enumeration:
            with self.assertRaisesRegex(ValueError, "combination_limit_exceeded"):
                control._inspect_extractive_lengths(evidence, 457, self.policy)
            enumeration.assert_not_called()
        for entry in evidence:
            entry["text"] = "完整句。" * 10
        output = control._inspect_extractive_lengths(evidence, 800, self.policy)
        self.assertEqual(output["total_combinations"], 10000)
        self.assertEqual(len(output["candidate_combinations"]), 10000)
        self.assertEqual(output["first_selection_by_organized_length"], {})

    def test_10_policy_hash_bytes_and_fixed_policy_values_are_enforced(self):
        with mock.patch("cocoon.evidence.verify_bundle", side_effect=AssertionError("错误策略不得读取证据")) as verifier:
            for bad in ("0" * 64, "", None, True, POLICY_SHA.upper()):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "policy_hash_mismatch"), offline():
                    control.inspect_extractive_feasibility(BUILD, POLICY, bad)
            with overlay(POLICY, self.policy_bytes + b" "), self.assertRaisesRegex(ValueError, "policy_hash_mismatch"):
                self.inspect()
            policy = {**self.policy, "match_tolerance": 0.2}
            data = json.dumps(policy).encode("utf-8")
            with overlay(POLICY, data), self.assertRaisesRegex(ValueError, "policy_value_mismatch"):
                control.inspect_extractive_feasibility(BUILD, POLICY, sha(data))
            verifier.assert_not_called()

    def test_11_manifest_all_packet_bytes_and_sources_are_checked_before_use(self):
        paths = [BUILD / f"packets/{topic}_round_{number}.json" for topic, number in CASE_KEYS]
        paths.append(BUILD / "review/sources.json")
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.name), overlay(path, original + b" "):
                with self.assertRaisesRegex(ValueError, "feasibility_input_hash_mismatch"):
                    self.inspect()
            self.assertEqual(path.read_bytes(), original)
        variants = []
        wrong = deepcopy(self.manifest)
        wrong["files"][1]["bytes"] += 1
        variants.append(wrong)
        wrong = deepcopy(self.manifest)
        wrong["topics"][0]["round_2_packet"] = wrong["topics"][0]["round_1_packet"]
        variants.append(wrong)
        wrong = deepcopy(self.manifest)
        wrong["topics"][1]["topic_id"] = "TOPIC_01"
        variants.append(wrong)
        for manifest in variants:
            with overlay(BUILD / "manifest.json", json.dumps(manifest).encode("utf-8")), self.assertRaises(ValueError):
                self.inspect()

    def test_12_changes_during_selection_are_detected_without_touching_original_files(self):
        reader, selector = Path.read_bytes, control.select_exact_sentences
        paths = [POLICY, BUILD / "manifest.json", BUILD / "packets/TOPIC_03_round_2.json",
                 BUILD / "review/sources.json", ALT, RUN / "events/events.jsonl", self.parent / "summary.json"]
        for path in paths:
            active = [False]
            original = path.read_bytes()

            def select_then_change(*args):
                selected = selector(*args)
                active[0] = True
                return selected

            def changed_read(current):
                data = reader(current)
                return data + b" " if active[0] and current == path else data

            with self.subTest(path=str(path)), mock.patch.object(Path, "read_bytes", changed_read):
                with mock.patch.object(control, "select_exact_sentences", side_effect=select_then_change):
                    with self.assertRaises(ValueError):
                        self.inspect(recorded=path in paths[4:])
            self.assertEqual(path.read_bytes(), original)

    def test_13_no_network_environment_raw_model_read_or_writes_and_snapshots_match(self):
        with mock.patch.object(control, "_verify_blob", side_effect=AssertionError("理论检查不得读取旧响应")) as raw:
            result = self.inspect()
            self.assertTrue(self.verify(result))
            raw.assert_not_called()
        self.assertEqual(result["new_model_calls"], 0)
        self.assertEqual(result["cost"], 0)
        self.assertEqual(result["human_reviews_completed"], 0)
        self.assertTrue(result["source_unchanged"])
        snapshot = result["source_snapshots"]
        self.assertTrue(snapshot["unchanged"])
        self.assertEqual(snapshot["before_sha256"], snapshot["after_sha256"])
        self.assertEqual(snapshot["before_sha256"], canonical_hash(result["input_hashes"]["files"]))
        for root in self.roots:
            self.assertEqual(hashes(root), self.snapshots[str(root)])

    def test_14_case_input_hashes_cover_packet_manifest_policy_sources_and_optional_record(self):
        for output in (self.result, self.recorded):
            all_files = output["input_hashes"]["files"]
            for relative in ["manifest.json"] + [r["path"] for r in self.manifest["files"]]:
                path = BUILD / relative
                self.assertEqual(all_files[str(path)], sha(path.read_bytes()))
            self.assertEqual(all_files[str(POLICY)], POLICY_SHA)
            self.assertEqual(output["input_hashes"]["source_index_sha256"], canonical_hash(self.index))
            for case, packet, (topic, number) in zip(output["cases"], self.packets, CASE_KEYS):
                inputs = case["input_hashes"]
                self.assertEqual(case["input_sha256"], canonical_hash(inputs))
                self.assertEqual(inputs["packet_sha256"], canonical_hash(packet))
                for path in (POLICY, BUILD / "manifest.json", BUILD / "review/sources.json",
                             BUILD / f"packets/{topic}_round_{number}.json"):
                    self.assertEqual(inputs["files"][str(path)], sha(path.read_bytes()))
        self.assertIsNone(self.result["input_hashes"]["recorded_alternative_sha256"])
        self.assertEqual(self.recorded["input_hashes"]["recorded_alternative_sha256"], sha(ALT.read_bytes()))
        self.assertEqual(self.recorded["cases"][0]["input_hashes"]["files"][str(ALT)], sha(ALT.read_bytes()))
        self.assertTrue(all(str(ALT) not in c["input_hashes"]["files"] for c in self.recorded["cases"][1:]))
        self.assertIn(str(RUN / "summary.json"), self.recorded["input_hashes"]["files"])
        self.assertIn(str(self.parent / "summary.json"), self.recorded["input_hashes"]["files"])

    def test_15_verifier_rebuilds_canonical_and_rejects_fabricated_actual_other_case(self):
        self.assertTrue(self.verify(self.result))
        self.assertTrue(self.verify(self.recorded, recorded=True))
        changed = deepcopy(self.recorded)
        changed["cases"][1].update(actual_pair_available=True, actual_organized_output_available=True,
                                   actual_organized_chars=175, status="recorded_exploratory_candidate")
        with self.assertRaisesRegex(ValueError, "feasibility_result_mismatch"):
            self.verify(changed, recorded=True)
        for key in ("first_selection", "ranges", "input_hash", "summary", "extra"):
            changed = deepcopy(self.result)
            if key == "first_selection":
                changed["cases"][0]["first_selection_by_organized_length"]["175"] = 2
            elif key == "ranges":
                changed["cases"][0]["feasible_organized_ranges"][0][1] += 1
            elif key == "input_hash":
                changed["cases"][3]["input_sha256"] = "0" * 64
            elif key == "summary":
                changed["summary"]["actual_pair_available_cases"] = 6
            else:
                changed["created_at_utc"] = "不得忽略未定义字段"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "feasibility_result_mismatch"):
                self.verify(changed)
        self.assertIsNone(self.recorded["cases"][1]["actual_organized_chars"])

    def test_16_optional_alternative_requires_true_run_verification_and_first_packet_match(self):
        for kwargs in ({"recorded_alternative_path": ALT}, {"recorded_run_directory": RUN}):
            with self.assertRaisesRegex(ValueError, "recorded_arguments_incomplete"):
                control.inspect_extractive_feasibility(BUILD, POLICY, POLICY_SHA, **kwargs)
        with mock.patch.object(control, "verify_extractive_alternative", wraps=control.verify_extractive_alternative) as verify:
            self.inspect(recorded=True)
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[1:], (RUN, POLICY, POLICY_SHA))
        alternative = _parse_json(ALT.read_bytes())
        alternative["pair"]["I4"]["additional_chars"] += 1
        with overlay(ALT, json.dumps(alternative).encode("utf-8")), self.assertRaises(ValueError):
            self.inspect(recorded=True)
        alternative = _parse_json(ALT.read_bytes())
        alternative["packet_sha256"] = canonical_hash(self.packets[1])
        with overlay(ALT, json.dumps(alternative).encode("utf-8")):
            with mock.patch.object(control, "verify_extractive_alternative", return_value=True):
                with self.assertRaisesRegex(ValueError, "recorded_packet_mismatch"):
                    self.inspect(recorded=True)

    def test_17_independent_check_rejects_selector_validity_or_first_choice_drift(self):
        original = control.select_exact_sentences

        def bad_valid(*args):
            selected = original(*args)
            selected["candidate_log"][0]["valid"] = not selected["candidate_log"][0]["valid"]
            return selected

        def bad_first(*args):
            selected = original(*args)
            if selected["selected_sentence_indices"] is not None:
                selected["selected_sentence_indices"] = [99, 99, 99]
            return selected

        for selector, code in ((bad_valid, "selector_integer_mismatch"), (bad_first, "selector_first_mismatch")):
            with mock.patch.object(control, "select_exact_sentences", side_effect=selector):
                with self.assertRaisesRegex(ValueError, code):
                    control._inspect_extractive_lengths(self.packets[0]["evidence"], 457, self.policy)
        evidence = deepcopy(self.packets[0]["evidence"])
        before = deepcopy(evidence)
        control._inspect_extractive_lengths(evidence, 457, self.policy)
        self.assertEqual(evidence, before)

    def test_18_no_subject_payload_or_approval_and_summary_is_length_coverage_only(self):
        def check_no_payload(value):
            if isinstance(value, dict):
                self.assertTrue({"pair", "payloads", "subject_payloads", "visible_text", "additional_text", "raw_text"}.isdisjoint(value))
                for item in value.values():
                    check_no_payload(item)
            elif isinstance(value, list):
                for item in value:
                    check_no_payload(item)

        for output in (self.result, self.recorded):
            check_no_payload(output)
            self.assertEqual(output["schema_version"], "1.0")
            self.assertEqual(output["version"], "extractive_feasibility_v1")
            self.assertEqual(output["summary"]["feasible_length_count"], 539)
            self.assertEqual(output["summary"]["total_allowed_length_count"], 1206)
            self.assertEqual(output["summary"]["coverage_ratio"], 539 / 1206)
            self.assertIn("不是模型成功率", output["summary"]["coverage_note"])
            self.assertFalse(output["formal_eligible"])
            self.assertFalse(output["full_I4_eligible"])
            self.assertTrue(output["no_semantic_approval"])
        source = (ROOT / "cocoon/extractive_control.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                self.assertNotIn(name, {"complete", "write_bytes", "write_text", "open", "mkdir", "unlink", "getenv"})
        self.assertEqual(_parse_json((RUN / "summary.json").read_bytes()), self.run_summary)
        self.assertEqual(self.run_summary["status"], "delivery_failed")
        self.assertIsNone(self.run_summary["pair"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
