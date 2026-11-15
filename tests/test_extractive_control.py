"""仅离线自测；篡改用内存覆盖，不改旧文件，不创建交付目录或清理文件。"""

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
from cocoon.budget import BudgetLedger
from cocoon.runtime import _json_text, _parse_json, _verify_blob, canonical_hash

RUN = ROOT / "experiment/delivery_probes/delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
POLICY = ROOT / "configs/extractive_control_v1.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hashes(root):
    # 来源目录另有既存 HTML 预览；测试快照也保护它，不把它当作审计 JSON 输入。
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


@contextmanager
def overlay(path, data):
    reader = Path.read_bytes
    with mock.patch.object(Path, "read_bytes", lambda p: data if p == path else reader(p)):
        yield


@contextmanager
def offline():
    """构建和验证期间同时禁止传输、凭据、当前账本、环境读取及文件写入。"""
    with ExitStack() as stack:
        forbidden = []
        for obj, name in (
            (delivery, "run_delivery"), (delivery, "load_key"), (mimo, "load_key"),
            (mimo, "credential_status"), (delivery, "mimo_status"), (delivery, "_ledger"),
            (delivery, "_guard"), (delivery.MiMoTransport, "complete"),
            (delivery.MiMoTransport, "_exchange"), (BudgetLedger, "__init__"),
            (dosage, "make_dose_pair"), (delivery_audit, "make_dose_pair"),
            (os, "getenv"), (os, "open"), (Path, "write_bytes"), (Path, "write_text"),
            (Path, "mkdir"), (Path, "unlink"), (Path, "rename"), (Path, "replace"),
        ):
            forbidden.append(stack.enter_context(mock.patch.object(
                obj, name, side_effect=AssertionError("禁止调用或写入"))))
        for target in (
            "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
            "socket.socket.sendall", "socket.socket.sendto", "socket.getaddrinfo",
            "urllib.request.urlopen", "urllib.request.build_opener", "urllib.request.OpenerDirector.open",
        ):
            forbidden.append(stack.enter_context(mock.patch(target, side_effect=AssertionError("禁止联网"))))

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
            return original_open(file, mode, *args, **kwargs)

        stack.enter_context(mock.patch("io.open", guarded_open))
        stack.enter_context(mock.patch("builtins.open", guarded_open))
        try:
            yield
        finally:
            for guard in forbidden:
                guard.assert_not_called()


class ExtractiveControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_bytes = POLICY.read_bytes()
        cls.policy_sha = sha(cls.policy_bytes)
        cls.policy = _parse_json(cls.policy_bytes)
        cls.summary = _parse_json((RUN / "summary.json").read_bytes())
        cls.parent = Path(cls.summary["parent_source_path"])
        cls.build_root = Path(cls.summary["packet_input"]["manifest_source_path"]).parent
        cls.roots = [RUN, cls.parent, cls.build_root] + [
            RUN.parent / name for name in (
                "delivery_20261108T090523Z_fb38842073a64ca4ae95b42c082d6f7e",
                "delivery_20261108T101439Z_99cfd0db64294c3a8c51b1248b03eb98",
            )
        ]
        cls.snapshots = {str(root): hashes(root) for root in cls.roots}
        cls.protected = {p: sha(p.read_bytes()) for p in (
            POLICY, Path(cls.summary["policy_input"]["source_path"]),
            ROOT / "cocoon/dosage.py", ROOT / "cocoon/delivery.py", ROOT / "cocoon/delivery_audit.py",
            ROOT / "cocoon/runtime.py", ROOT / "cocoon/extractive_control.py", Path(__file__),
        )}
        with offline():
            cls.audited = delivery_audit.inspect_delivery_run(RUN)
            cls.packet = _verify_blob(RUN, cls.summary["inputs"]["packet"])
            cls.index = _verify_blob(RUN, cls.summary["inputs"]["source_index"])
            cls.result = control.build_extractive_alternative(RUN, POLICY, cls.policy_sha)

    @classmethod
    def tearDownClass(cls):
        for root in cls.roots:
            if hashes(root) != cls.snapshots[str(root)]:
                raise AssertionError("旧运行或来源文件集合改变：" + str(root))
        for path, digest in cls.protected.items():
            if sha(path.read_bytes()) != digest:
                raise AssertionError("原文件改变：" + str(path))

    def build(self):
        with offline():
            return control.build_extractive_alternative(RUN, POLICY, self.policy_sha)

    def verify(self, data):
        with offline():
            return control.verify_extractive_alternative(data, RUN, POLICY, self.policy_sha)

    def test_01_real_lengths_first_lexicographic_candidate_and_all_eight_logs(self):
        result = self.result
        self.assertEqual(result["status"], "structurally_matched_candidate")
        self.assertEqual([result["pair"][arm]["char_count"] for arm in ("I4", "I5")], [632, 637])
        self.assertEqual([result["pair"][arm]["additional_chars"] for arm in ("I4", "I5")], [175, 180])
        self.assertEqual(result["selected_sentence_indices"], [0, 0, 1])
        self.assertEqual(result["candidate_combination_count"], 8)
        logs = result["candidate_log"]
        self.assertEqual([tuple(row["sentence_indices"]) for row in logs], list(product(range(2), repeat=3)))
        self.assertEqual([row["combination_index"] for row in logs], list(range(8)))
        self.assertEqual(next(row["sentence_indices"] for row in logs if row["valid"]), [0, 0, 1])
        for row in logs:
            size = 0
            for entry, slot in zip(self.packet["evidence"], row["offsets"]):
                self.assertEqual(slot["evidence_id"], entry["evidence_id"])
                size += len(entry["text"][slot["start"]:slot["end"]]) + len(entry["evidence_id"]) + 2
            self.assertEqual(row["additional_chars"], size)
            self.assertEqual(row["visible_chars"], 457 + size)
            self.assertEqual(row["additional_difference_ratio"], abs(175 - size) / min(175, size))
            self.assertEqual(row["total_difference_ratio"], abs(632 - (457 + size)) / min(632, 457 + size))
            self.assertEqual(row["valid"], 600 <= 457 + size <= 800
                             and row["additional_difference_ratio"] <= .1 and row["total_difference_ratio"] <= .1)
        self.assertTrue(all(result["checks"][name] for name in control._CHECK_NAMES))
        self.assertEqual(result["checks"]["total_difference_ratio"], 5 / 632)
        self.assertEqual(result["checks"]["additional_difference_ratio"], 5 / 175)

    def test_02_exact_whole_sentences_offsets_ids_and_frozen_hashes(self):
        selected = self.result["selected_sentences"]
        self.assertEqual(len(selected), 3)
        for entry, sentence, chosen_index in zip(self.packet["evidence"], selected, (0, 0, 1)):
            self.assertEqual(set(sentence), {"evidence_id", "text", "start", "end", "source_url", "source_family_id"})
            matches = list(re.finditer(r'.*?[。！？](?:[”’"])?', entry["text"], re.DOTALL))
            self.assertEqual((sentence["start"], sentence["end"]), matches[chosen_index].span())
            self.assertEqual(sentence["text"], entry["text"][sentence["start"]:sentence["end"]])
            for key in ("evidence_id", "source_url", "source_family_id"):
                self.assertEqual(sentence[key], entry[key])
            self.assertEqual(sha(entry["text"].encode("utf-8")), entry["content_sha256"])
        rendered = "".join(s["text"] + "（" + s["evidence_id"] + "）" for s in selected)
        self.assertEqual(self.result["pair"]["I5"]["additional_text"], rendered)
        self.assertEqual(self.result["packet_sha256"], canonical_hash(self.packet))
        self.assertEqual(self.result["source_index_sha256"], canonical_hash(self.index))
        self.assertEqual(self.result["source_summary_sha256"], sha((RUN / "summary.json").read_bytes()))
        self.assertEqual(self.result["policy_sha256"], self.policy_sha)
        compact = dosage.compact_evidence(self.packet, self.index)
        self.assertEqual(self.result["evidence_block"], compact["evidence_block"])
        self.assertEqual(self.result["source_map"], compact["source_map"])
        for entry in self.packet["evidence"]:
            self.assertIn("\n" + entry["text"], self.result["evidence_block"])

    def test_03_organized_original_raw_render_and_distinct_origins(self):
        row = self.summary["additions"]["organized_reflection"]
        raw = _verify_blob(RUN, row["response"])
        parsed = _parse_json(raw["raw_text"])
        expected = "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in parsed["sentences"])
        self.assertEqual(self.result["pair"]["I4"]["additional_text"], expected)
        self.assertEqual(expected, row["rendered"]["additional_text"])
        self.assertEqual(self.result["organized_source_event_ids"], [row["source_event_id"]])
        self.assertEqual(self.result["content_origins"], {
            "organized": "recorded_assistant_output", "plain": "deterministic_exact_source_sentences"})
        for arm, mode in (("I4", "organized"), ("I5", "plain")):
            self.assertEqual(self.result["pair"][arm]["content_origin"], self.result["content_origins"][mode])
        self.assertEqual(self.result["sentence_counts"], {"organized": 4, "plain": 3})
        self.assertIn("不是四句模型生成", self.result["protocol_note"])
        self.assertFalse(self.result["plain_generated_by_model"])
        self.assertNotIn("provenance", self.result["pair"])

    def test_04_sentence_partition_preserves_whitespace_quotes_and_excludes_questions(self):
        for text in ('  首句。\n次句！\t ', '开头。尾句。”\n', '句子！’\t', '句子。"  '):
            spans = control._sentence_spans(text)
            self.assertEqual("".join(text[start:end] for start, end in spans), text)
            self.assertEqual(spans[0][0], 0)
            self.assertEqual(spans[-1][1], len(text))
            self.assertTrue(all(a[1] == b[0] for a, b in zip(spans, spans[1:])))
        for text in ("", "仅有未完成正文", "完成。非空白残片", "   ", "句子。\n残片"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                control._sentence_spans(text)
        evidence = deepcopy(self.packet["evidence"])
        evidence[0]["text"] = "问句？ASCII?问句。\n" + "甲" * 20 + "。\t "
        output = control.select_exact_sentences(evidence, 175, 457, self.policy)
        self.assertTrue(output["candidate_log"])
        self.assertTrue(all(row["sentence_indices"][0] == 2 for row in output["candidate_log"]))
        for row in output["candidate_log"]:
            self.assertEqual(row["offsets"][0]["end"], len(evidence[0]["text"]))
        evidence[0]["text"] = "只有问句？\n"
        empty = control.select_exact_sentences(evidence, 175, 457, self.policy)
        self.assertEqual(empty["status"], "no_matching_candidate")
        self.assertEqual(empty["candidate_log"], [])

    def test_05_no_match_never_pads_and_combination_limit_precedes_product(self):
        original = deepcopy(self.packet["evidence"])
        output = control.select_exact_sentences(original, 175, 0, self.policy)
        self.assertEqual(output["status"], "no_matching_candidate")
        self.assertEqual(output["selected_sentences"], [])
        self.assertIsNone(output["selected_sentence_indices"])
        self.assertEqual(len(output["candidate_log"]), 8)
        self.assertFalse(any(row["valid"] for row in output["candidate_log"]))
        self.assertEqual(original, self.packet["evidence"])
        with mock.patch.object(control, "select_exact_sentences", return_value=output):
            no_pair = self.build()
        self.assertEqual(no_pair["status"], "no_matching_candidate")
        self.assertIsNone(no_pair["pair"])
        self.assertFalse(no_pair["checks"]["assembly_valid"])
        too_many = deepcopy(original)
        for entry in too_many:
            entry["text"] = "整句。" * 22
        with mock.patch.object(control, "product", side_effect=AssertionError("超限不得枚举")) as enumeration:
            with self.assertRaisesRegex(ValueError, "combination_limit_exceeded"):
                control.select_exact_sentences(too_many, 175, 457, self.policy)
            enumeration.assert_not_called()

    def test_06_bad_policy_hash_and_byte_binding_before_audit(self):
        with mock.patch.object(delivery_audit, "inspect_delivery_run", side_effect=AssertionError("不得进入审计")) as audit:
            for bad in ("0" * 64, "", None, True, self.policy_sha.upper()):
                with self.subTest(bad=bad), offline(), self.assertRaisesRegex(ValueError, "policy_hash_mismatch"):
                    control.build_extractive_alternative(RUN, POLICY, bad)
            with overlay(POLICY, self.policy_bytes + b" "), offline():
                with self.assertRaisesRegex(ValueError, "policy_hash_mismatch"):
                    control.build_extractive_alternative(RUN, POLICY, self.policy_sha)
            audit.assert_not_called()
        alternative_bytes = json.dumps(self.policy, ensure_ascii=False, indent=4).encode("utf-8")
        with overlay(POLICY, alternative_bytes):
            self.assertEqual(control._load_policy(POLICY, sha(alternative_bytes)), self.policy)

    def test_07_policy_exact_whitelist_fixed_values_and_types(self):
        variants = [{**self.policy, "extra": False}]
        for key, value in self.policy.items():
            missing = deepcopy(self.policy)
            del missing[key]
            variants.append(missing)
            wrong = deepcopy(self.policy)
            wrong[key] = not value if type(value) is bool else value + 1 if type(value) in (int, float) else value + " "
            variants.append(wrong)
            if type(value) in (int, bool):
                variants.append({**self.policy, key: float(value)})
        for number, policy in enumerate(variants):
            data = _json_text(policy).encode("utf-8")
            with self.subTest(number=number), overlay(POLICY, data):
                with self.assertRaisesRegex(ValueError, "policy_(fields|value)_mismatch"):
                    control._load_policy(POLICY, sha(data))
        for data in (b'{"schema_version":"1.0","schema_version":"1.0"}', b'{"match_tolerance":NaN}'):
            with overlay(POLICY, data), self.assertRaises(ValueError):
                control._load_policy(POLICY, sha(data))

    def test_08_deterministic_rebuild_verification_and_only_timestamp_ignored(self):
        again = self.build()
        self.assertEqual(_json_text(again), _json_text(self.result))
        self.assertNotIn("created_at_utc", again)
        decorated = {**deepcopy(again), "created_at_utc": "2026-11-08T00:00:00Z"}
        self.assertTrue(self.verify(decorated))
        self.assertIn("created_at_utc", decorated)
        for key, value in (("status", "no_matching_candidate"), ("formal_eligible", True),
                           ("new_model_calls", False), ("source_summary_sha256", "0" * 64),
                           ("unexpected_field", "不能忽略")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "result_mismatch"):
                self.verify({**deepcopy(again), key: value})
        for target in ("pair", "selected_sentences", "candidate_log", "checks", "content_origins", "source_map"):
            changed = deepcopy(again)
            if target == "pair":
                changed[target]["I4"]["additional_text"] += " "
            elif target == "selected_sentences":
                changed[target][0]["start"] += 1
            elif target == "candidate_log":
                changed[target][-1]["valid"] = not changed[target][-1]["valid"]
            elif target == "checks":
                changed[target]["additional_difference_ratio"] = 0
            elif target == "content_origins":
                changed[target]["plain"] = "recorded_assistant_output"
            else:
                changed[target][0]["publication_date"] = None
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "result_mismatch"):
                self.verify(changed)

    def test_09_old_v3_failure_and_all_source_snapshots_unchanged(self):
        result = self.build()
        current = _parse_json((RUN / "summary.json").read_bytes())
        self.assertEqual(current, self.summary)
        self.assertEqual(current["status"], "delivery_failed")
        self.assertEqual(current["additions"]["plain_restatement"]["status"], "failed")
        self.assertIsNone(current["additions"]["plain_restatement"]["rendered"])
        self.assertIsNone(current["pair"])
        self.assertEqual(current["payloads"], {})
        self.assertEqual(result["source_original_run_status"], "delivery_failed")
        self.assertEqual(result["source_plain_attempt_status"], "failed")
        self.assertEqual(result["scope"], "new_offline_exploratory_variant_not_repaired_v3")
        self.assertTrue(result["source_audit"]["inputs_match"])
        self.assertTrue(result["source_audit"]["internal_report_preserved"])
        snapshots = result["source_snapshots"]
        self.assertTrue(snapshots["unchanged"])
        self.assertEqual(snapshots["before_sha256"], snapshots["after_sha256"])
        for root in self.roots:
            self.assertEqual(hashes(root), self.snapshots[str(root)])
        self.assertEqual(POLICY.read_bytes(), self.policy_bytes)

    def test_10_full_raw_and_input_verification_rejects_memory_tampering(self):
        refs = self.summary["inputs"]
        paths = [RUN / refs[name]["ref"] for name in ("packet", "source_index")]
        for mode in ("organized_reflection", "plain_restatement"):
            row = self.summary["additions"][mode]
            paths.append(RUN / row["response"]["ref"])
            paths.append(RUN / row["transport_records"][1]["output"]["ref"])
        paths.extend([RUN / "summary.json", RUN / "events/events.jsonl", self.parent / "summary.json",
                      Path(self.summary["source_index_input"]["source_path"])])
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.name), overlay(path, original + b"\n"), self.assertRaises(ValueError):
                self.verify(self.result)
            self.assertEqual(path.read_bytes(), original)
        # 即便审计返回值已缓存，构建也必须通过运行时 blob 校验重新读取包和来源索引。
        for name in ("packet", "source_index"):
            path = RUN / refs[name]["ref"]
            with mock.patch.object(delivery_audit, "inspect_delivery_run", return_value=deepcopy(self.audited)):
                with overlay(path, path.read_bytes() + b" "), self.assertRaises(ValueError):
                    self.build()

    def test_11_audit_identity_hash_and_mapping_must_align(self):
        variants = []
        for key, value in (("audit_status", "failed"), ("inputs_match", False),
                           ("internal_report_preserved", False), ("policy_version", "report_to_delivery_v2"),
                           ("run_status", "generated_structurally_valid"), ("pair_available", True),
                           ("summary_sha256", "0" * 64), ("fixed_chars", 458)):
            variants.append({**deepcopy(self.audited), key: value})
        for mode in ("organized_reflection", "plain_restatement"):
            wrong = deepcopy(self.audited)
            wrong["by_mode"][mode]["accepted"] = not wrong["by_mode"][mode]["accepted"]
            variants.append(wrong)
        wrong = deepcopy(self.audited)
        wrong["source_map"][0]["source_family_id"] = "WRONG_FAMILY"
        variants.append(wrong)
        for name in ("packet_sha256", "source_index_sha256"):
            wrong = deepcopy(self.audited)
            wrong["source_inputs"]["configuration"][name] = "0" * 64
            variants.append(wrong)
        for number, audited in enumerate(variants):
            with self.subTest(number=number), mock.patch.object(delivery_audit, "inspect_delivery_run", return_value=audited):
                with self.assertRaises(ValueError):
                    self.build()

    def test_12_selection_sees_only_evidence_and_lengths_not_model_plain_text(self):
        class StatusOnly(dict):
            def __getitem__(self, key):
                if key not in ("status", "accepted"):
                    raise AssertionError("不得读取失败对照正文作选句")
                return super().__getitem__(key)

        audited = deepcopy(self.audited)
        audited["by_mode"]["plain_restatement"] = StatusOnly(status="failed", accepted=False)
        with mock.patch.object(delivery_audit, "inspect_delivery_run", return_value=audited):
            with mock.patch.object(control, "select_exact_sentences", wraps=control.select_exact_sentences) as selector:
                result = self.build()
                selector.assert_called_once()
                evidence, size, fixed, policy = selector.call_args.args
                self.assertEqual(evidence, self.packet["evidence"])
                self.assertIs(type(size), int)
                self.assertEqual((size, fixed, policy), (175, 457, self.policy))
        self.assertEqual(result["selected_sentences"], self.result["selected_sentences"])
        self.assertEqual(result["candidate_log"], self.result["candidate_log"])

    def test_13_public_message_projection_excludes_arm_labels_source_metadata_and_gold(self):
        for arm in ("I4", "I5"):
            piece = self.result["pair"][arm]
            payload = {"messages": [{"role": "user", "content": piece["visible_text"]}]}
            encoded = _json_text(payload)
            self.assertEqual(len(payload["messages"]), 1)
            self.assertNotRegex(encoded, r"(?i)I[45]|https?://|source_url|source_family_id|source_map|gold|stance_axis|content_origin")
            self.assertEqual(piece["visible_text"], "阅读材料\n" + self.result["evidence_block"]
                             + "\n\n阅读说明\n" + piece["additional_text"] + "\n\n" + dosage.RESPONSE_PROMPT)
            self.assertEqual(piece["visible_text"].count(dosage.RESPONSE_PROMPT), 1)
            self.assertEqual(piece["char_count"], len(piece["visible_text"]))
            self.assertEqual(piece["additional_chars"], len(piece["additional_text"]))
        for key in ("full_I4_eligible", "formal_eligible", "subject_delivered"):
            self.assertIs(self.result[key], False)
        self.assertEqual(self.result["human_reviews_completed"], 0)
        self.assertEqual(self.result["semantic_review"], "pending_independent_review")

    def test_14_no_transport_credentials_ledger_or_writes_in_build_and_verify(self):
        source = (ROOT / "cocoon/extractive_control.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotRegex(source, r"(?i)mimo|ledger|load_key|make_dose_pair|urlopen|requests\.")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                self.assertNotIn(name, {"write_text", "write_bytes", "mkdir", "unlink", "open", "getenv", "utc_now"})
        with offline():
            result = control.build_extractive_alternative(RUN, POLICY, self.policy_sha)
            self.assertTrue(control.verify_extractive_alternative(result, RUN, POLICY, self.policy_sha))
        self.assertEqual(result["new_model_calls"], 0)
        self.assertEqual(result["source_audit"]["audit_status"], "passed")

    def test_15_changes_during_selection_detected_by_final_snapshot(self):
        selector = control.select_exact_sentences
        reader = Path.read_bytes
        for path in (RUN / "events/events.jsonl", self.parent / "summary.json", POLICY,
                     Path(self.summary["source_index_input"]["source_path"])):
            active = [False]

            def select_then_change(*args):
                result = selector(*args)
                active[0] = True
                return result

            def changed_read(current):
                data = reader(current)
                return data + b" " if active[0] and current == path else data

            with self.subTest(path=path.name), mock.patch.object(Path, "read_bytes", changed_read):
                with mock.patch.object(control, "select_exact_sentences", side_effect=select_then_change):
                    with self.assertRaisesRegex(ValueError, "source_snapshot_changed"):
                        self.build()


if __name__ == "__main__":
    unittest.main(verbosity=2)
