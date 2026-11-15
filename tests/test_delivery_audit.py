"""只读审计测试；真实记录不改，副本仅置于 tests 下的新临时目录且不清理。

合成成功轨迹仅测试本地一致性，不是新增真实 HTTP 或模型调用。
"""

import sys
sys.dont_write_bytecode = True

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon import delivery, delivery_audit, mimo
from cocoon.budget import BudgetLedger
from cocoon.dosage import make_dose_pair, public_payload
from cocoon.runtime import _json_text, _parse_json, canonical_hash, verify_events

BASE = ROOT / "experiment/delivery_probes"
V1 = BASE / "delivery_20261108T090523Z_fb38842073a64ca4ae95b42c082d6f7e"
V2 = BASE / "delivery_20261108T101439Z_99cfd0db64294c3a8c51b1248b03eb98"
MODES = delivery.MODES


def read(path):
    return _parse_json(Path(path).read_bytes())


def blob(root, ref):
    return read(root / ref["ref"])


def hashes(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@contextmanager
def overlay(path, value):
    original = Path.read_bytes
    data = value if type(value) is bytes else _json_text(value).encode("utf-8")
    with mock.patch.object(Path, "read_bytes", lambda p: data if p == path else original(p)):
        yield


class DeliveryAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "tests").is_dir():
            raise AssertionError("测试父目录必须已经存在")
        cls.temp = Path(tempfile.mkdtemp(prefix="delivery_audit_only_", dir=ROOT / "tests")).resolve()
        cls.summary1, cls.summary2 = read(V1 / "summary.json"), read(V2 / "summary.json")
        cls.parent = Path(cls.summary2["parent_source_path"])
        cls.build = Path(cls.summary2["packet_input"]["manifest_source_path"]).parent
        cls.protected = {**hashes(V1), **hashes(V2), **hashes(cls.parent), **hashes(cls.build)}
        for p in (ROOT / "cocoon/delivery.py", ROOT / "cocoon/dosage.py", ROOT / "cocoon/runtime.py",
                  ROOT / "cocoon/delivery_audit.py", ROOT / "tests/test_delivery.py", Path(__file__),
                  Path(cls.summary1["policy_input"]["source_path"]), Path(cls.summary2["policy_input"]["source_path"])):
            cls.protected[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        for name, digest in cls.protected.items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                raise AssertionError("原文件被修改：" + name)

    def inspect(self, root):
        """每次审计均禁止网络、模型、凭据、账本操作及文件写入。"""
        with ExitStack() as stack:
            forbidden = []
            for obj, name in ((delivery, "run_delivery"), (delivery, "load_key"), (mimo, "load_key"),
                              (mimo, "credential_status"), (delivery, "mimo_status"), (delivery, "_ledger"),
                              (delivery, "_guard"), (delivery.MiMoTransport, "complete"),
                              (delivery.MiMoTransport, "_exchange"), (BudgetLedger, "__init__"),
                              (os, "getenv"), (os, "open"), (Path, "write_bytes"), (Path, "write_text"),
                              (Path, "mkdir"), (Path, "unlink")):
                forbidden.append(stack.enter_context(mock.patch.object(obj, name, side_effect=AssertionError("禁止调用或写入"))))
            for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                           "socket.socket.sendall", "socket.socket.sendto", "socket.getaddrinfo",
                           "urllib.request.urlopen", "urllib.request.build_opener", "urllib.request.OpenerDirector.open"):
                forbidden.append(stack.enter_context(mock.patch(target, side_effect=AssertionError("禁止联网"))))
            # 不读取、复制或枚举真实环境；此映射的任何读取都失败。
            class NoEnvironment(dict):
                def __getitem__(self, key):
                    raise AssertionError("禁止读取环境")

                def get(self, key, default=None):
                    raise AssertionError("禁止读取环境")

            stack.enter_context(mock.patch.object(os, "environ", NoEnvironment()))
            original_open = io.open

            def guarded_open(file, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    raise AssertionError("审计禁止写文件")
                if isinstance(file, (str, os.PathLike)):
                    p = Path(file)
                    if p.suffix.lower() == ".md" or any(word in p.name.lower() for word in (".env", "credential", "secret", "api_key")):
                        raise AssertionError("禁止读取凭据或文档")
                return original_open(file, mode, *args, **kwargs)

            stack.enter_context(mock.patch("io.open", guarded_open))
            stack.enter_context(mock.patch("builtins.open", guarded_open))
            try:
                return delivery_audit.inspect_delivery_run(root)
            finally:
                for guard in forbidden:
                    guard.assert_not_called()

    def reject(self, root, code):
        with self.assertRaisesRegex(ValueError, "delivery_audit:" + code):
            self.inspect(root)

    def capture(self, root, value):
        digest = canonical_hash(value)
        ref = {"ref": f"raw/blobs/{digest}.json", "sha256": digest}
        path = root / ref["ref"]
        self.assertTrue(path.parent.is_dir())
        self.assertTrue(path.resolve().is_relative_to(self.temp))
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(_json_text(value).encode("utf-8"))
        return ref

    def replace_ref(self, value, old, new):
        if type(value) is dict:
            for k, v in value.items():
                if type(v) is str and v in (old["ref"], old["sha256"]):
                    value[k] = new["ref"] if v == old["ref"] else new["sha256"]
                else:
                    self.replace_ref(v, old, new)
        elif type(value) is list:
            for v in value:
                self.replace_ref(v, old, new)

    def seal(self, root, summary, events):
        """只在测试副本重算链，保证篡改测试能到达语义关联校验。"""
        self.assertTrue(root.is_relative_to(self.temp))
        default_outputs = {"delivery_started", "delivery_request_started", "mimo_request_prepared",
                           "mimo_request_completed", "delivery_failed", "delivery_completed"}
        for e in events[:-1]:
            ref = self.capture(root, e["payload"])
            if e["event_type"] in default_outputs:
                e["output_ref"], e["output_sha256"] = ref["ref"], ref["sha256"]
        for row in summary["additions"].values():
            transport = [e for e in events if e.get("task_id") == row["request_id"] and e["event_type"].startswith("mimo_")]
            row["transport_records"] = delivery_audit._transport_records(transport)
        terminal = events[-1]
        terminal.update(event_type="delivery_failed" if summary["status"] == "delivery_failed" else "delivery_completed",
                        status=summary["status"], payload={k: deepcopy(v) for k, v in summary.items() if k != "verification"})
        ref = self.capture(root, terminal["payload"])
        terminal["output_ref"], terminal["output_sha256"] = ref["ref"], ref["sha256"]
        previous = None
        for number, e in enumerate(events, 1):
            e.update(logical_seq=number, previous_event_hash=previous)
            e.pop("event_hash", None)
            e["event_hash"] = canonical_hash(e)
            previous = e["event_hash"]
        (root / "events/events.jsonl").write_bytes("".join(_json_text(e) + "\n" for e in events).encode("utf-8"))
        summary["verification"] = verify_events(root)
        (root / "summary.json").write_bytes(_json_text(summary).encode("utf-8"))

    def copy_run(self, source=V2):
        self.assertTrue(self.temp.is_dir())
        holder = Path(tempfile.mkdtemp(prefix="copy_only_", dir=self.temp))
        root = holder / source.name
        shutil.copytree(source, root)
        summary = read(root / "summary.json")
        events = [_parse_json(line) for line in (root / "events/events.jsonl").read_bytes().splitlines()]
        summary["run_directory"] = str(root)
        self.seal(root, summary, events)
        return root, summary, events

    def change_request(self, root, summary, events, mode, change):
        row = summary["additions"][mode]
        old = deepcopy(row["request"])
        request = blob(root, old)
        change(request)
        self.replace_ref([summary, events], old, self.capture(root, request))
        prepared = next(e for e in events if e["event_type"] == "mimo_request_prepared" and e["task_id"] == row["request_id"])
        old_wire = delivery_audit._ref(prepared, "input")
        wire = blob(root, old_wire)
        wire["messages"] = deepcopy(request["messages"])
        self.replace_ref([summary, events], old_wire, self.capture(root, wire))

    def test_01_real_v1_403_chars_literal_output_and_not_attempted(self):
        result = self.inspect(V1)
        self.assertEqual(result["audit_status"], "passed")
        self.assertEqual(result["policy_version"], "report_to_delivery_v1")
        self.assertEqual((result["actual_http_attempts"], result["completed_model_calls"], result["stage_count"]), (1, 1, 0))
        row = result["by_mode"][MODES[0]]
        original = blob(V1, self.summary1["additions"][MODES[0]]["response"])
        self.assertEqual(row["raw_text"], original["raw_text"])
        self.assertEqual(row["parsed_output"], _parse_json(original["raw_text"]))
        expected = "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in row["parsed_output"]["sentences"])
        self.assertEqual(row["diagnostic_additional_text"], expected)
        self.assertEqual((row["diagnostic_additional_chars"], row["diagnostic_visible_chars"]), (403, 860))
        self.assertEqual([s["text_chars"] for s in row["sentence_lengths"]], [59, 88, 74, 77, 37])
        self.assertEqual(row["usage"], {"prompt_tokens": 2283, "completion_tokens": 338, "total_tokens": 2621})
        self.assertEqual(row["response_id"], original["response_id"])
        self.assertEqual(result["error"], {"code": "length_out_of_range", "safe_details": {"len": 403, "min": 225, "max": 245}})
        self.assertTrue(row["modeled_candidate"] and row["diagnostic_only"])
        self.assertFalse(row["accepted"])
        self.assertIsNone(row["budget"])
        self.assertEqual(result["not_attempted"], [MODES[1]])
        self.assertEqual(result["by_mode"][MODES[1]]["status"], "not_attempted")
        self.assertIsNone(result["by_mode"][MODES[1]]["raw_text"])
        self.assertNotIn("pair", result)
        self.assertNotIn("subject_payloads", result)

    def test_02_real_v2_numeric_schema_and_each_slot_lengths(self):
        result = self.inspect(V2)
        first, second = [result["by_mode"][m] for m in MODES]
        self.assertEqual((result["actual_http_attempts"], result["completed_model_calls"], result["stage_count"]), (2, 2, 1))
        self.assertEqual((first["diagnostic_additional_chars"], first["diagnostic_visible_chars"]), (173, 630))
        self.assertEqual([s["text_chars"] for s in first["sentence_lengths"]], [30, 39, 40, 32])
        self.assertEqual([s["text_chars"] for s in second["sentence_lengths"]], [29, 29, 30, 37])
        self.assertTrue(first["accepted"])
        self.assertFalse(first["diagnostic_only"])
        self.assertEqual(second["range"], {"min": 158, "max": 190})
        self.assertIs(type(second["parsed_output"]["schema_version"]), float)
        self.assertEqual(second["parsed_output"]["schema_version"], 1.0)
        self.assertEqual(result["error"], {"code": "invalid_schema", "safe_details": {}})
        for mode, row in result["by_mode"].items():
            original = blob(V2, self.summary2["additions"][mode]["response"])
            self.assertEqual(row["raw_text"], original["raw_text"])
            self.assertEqual(row["usage"], original["usage"])
            self.assertEqual(row["response_id"], original["response_id"])
            texts = row["parsed_output"]["sentences"]
            self.assertEqual(row["diagnostic_additional_text"], "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in texts))
            self.assertEqual(len(row["sentence_lengths"]), 4)
            low, high = (28, 64) if mode == MODES[0] else (32, 39)
            for sentence, lengths in zip(texts, row["sentence_lengths"]):
                self.assertEqual(lengths["text_chars"], len(sentence["text"]))
                self.assertEqual(lengths["rendered_chars"], len(sentence["text"]) + 8)
                self.assertEqual((lengths["text_min_chars"], lengths["text_max_chars"]), (low, high))
                self.assertEqual(lengths["within_range"], low <= len(sentence["text"]) <= high)
        self.assertTrue(second["diagnostic_only"] and second["modeled_candidate"])
        self.assertFalse(second["accepted"])
        self.assertEqual((second["diagnostic_additional_chars"], second["diagnostic_visible_chars"]), (157, 614))
        self.assertIn("schema_version_type", [v["code"] for v in second["violations"]])
        self.assertIn({"code": "additional_length_out_of_range", "len": 157, "min": 158, "max": 190}, second["violations"])
        below = [v["index"] for v in second["violations"] if v["code"] == "sentence_below_min"]
        self.assertEqual(below, [0, 1, 2])
        self.assertFalse(result["pair_available"])
        self.assertNotIn("pair", result)
        self.assertNotIn("subject_payloads", result)
        self.assertNotIn("exported_payload", second)
        self.assertEqual(result["run_status"], "delivery_failed")

    def test_03_wrong_verification_and_parent_summary_hash_rejected(self):
        wrong = deepcopy(self.summary2)
        wrong["verification"]["blob_count"] += 1
        with overlay(V2 / "summary.json", wrong):
            self.reject(V2, "summary_verification_mismatch")
        root, summary, events = self.copy_run()
        summary["parent_summary_sha256"] = "0" * 64
        self.seal(root, summary, events)
        self.reject(root, "parent_summary_hash_mismatch")
        descriptor = summary["additions"][MODES[1]]["response"]
        with overlay(root / descriptor["ref"], (root / descriptor["ref"]).read_bytes() + b" "):
            self.reject(root, "event_verification_failed")

    def test_04_summary_terminal_must_be_completely_equal(self):
        for key, value in (("completed_at_utc", "2026-11-08T00:00:00Z"), ("unexpected_field", True)):
            with self.subTest(key=key):
                wrong = {**deepcopy(self.summary2), key: value}
                with overlay(V2 / "summary.json", wrong):
                    self.reject(V2, "summary_terminal_mismatch")

    def test_05_memory_overlay_and_stable_snapshot_without_original_writes(self):
        original = (self.parent / "summary.json").read_bytes()
        with overlay(self.parent / "summary.json", original + b"\n"):
            self.reject(V2, "parent_files_changed")
        self.assertEqual((self.parent / "summary.json").read_bytes(), original)
        reader, calls = Path.read_bytes, []
        def changed_log(path):
            data = reader(path)
            if path == V2 / "events/events.jsonl":
                calls.append(path)
                if len(calls) >= 3:
                    return data + b"\n"
            return data
        with mock.patch.object(Path, "read_bytes", changed_log):
            self.reject(V2, "run_files_changed")
        self.assertGreaterEqual(len(calls), 3)

    def test_06_failed_schema_not_cast_or_rendered_into_summary(self):
        before = (V2 / "summary.json").read_bytes()
        with mock.patch.object(delivery, "render_addition", wraps=delivery.render_addition) as renderer:
            result = self.inspect(V2)
        self.assertEqual(renderer.call_count, 2)
        self.assertIs(type(renderer.call_args_list[1].args[0]["schema_version"]), float)
        self.assertEqual(renderer.call_args_list[1].kwargs["sentence_budget"], self.summary2["additions"][MODES[1]]["sentence_budget"])
        self.assertEqual((V2 / "summary.json").read_bytes(), before)
        self.assertIsNone(read(V2 / "summary.json")["additions"][MODES[1]]["rendered"])
        self.assertIs(type(result["by_mode"][MODES[1]]["parsed_output"]["schema_version"]), float)
        root, summary, events = self.copy_run()
        summary["schema_version"] = 1.0
        self.seal(root, summary, events)
        self.reject(root, "summary_identity_mismatch")

    def test_07_signed_http_association_tampering_rejected(self):
        for change in ("task_id", "wire", "raw_text", "response_id", "model", "usage", "response_request_id"):
            with self.subTest(change=change):
                root, summary, events = self.copy_run()
                row = summary["additions"][MODES[1]]
                if change == "task_id":
                    next(e for e in events if e["event_type"] == "mimo_response_received" and e["task_id"] == row["request_id"])["task_id"] = "other-request"
                elif change == "wire":
                    e = next(e for e in events if e["event_type"] == "mimo_request_prepared" and e["task_id"] == row["request_id"])
                    old = delivery_audit._ref(e, "input")
                    wire = blob(root, old)
                    wire["messages"][1]["content"] += " "
                    self.replace_ref([summary, events], old, self.capture(root, wire))
                else:
                    old = deepcopy(row["response"])
                    response = blob(root, old)
                    if change == "usage":
                        response["usage"]["prompt_tokens"] += 1
                        response["usage"]["total_tokens"] += 1
                    elif change == "response_request_id":
                        response["request_id"] = row["request_id"]
                    else:
                        response[change] += " altered"
                    self.replace_ref([summary, events], old, self.capture(root, response))
                self.seal(root, summary, events)
                self.reject(root, {"task_id": "http_request_id_mismatch", "wire": "http_payload_mismatch"}.get(change, "normalized_response_mismatch"))

    def test_08_counts_derived_from_events_not_summary(self):
        for change in ("summary", "prepared", "completed"):
            with self.subTest(change=change):
                root, summary, events = self.copy_run()
                if change == "summary":
                    summary["actual_http_attempts"] = 3
                    summary["actual_model_calls"] = 3
                    summary["last_counts"]["actual_http_attempts"] = 3
                else:
                    kind = "mimo_request_" + change
                    next(e for e in events if e["event_type"] == kind)["payload"]["actual_model_calls"] = True
                self.seal(root, summary, events)
                self.reject(root, {"summary": "actual_count_mismatch", "prepared": "prepared_counter_mismatch",
                                   "completed": "completed_payload_mismatch"}[change])

    def test_09_user_json_exact_keys_order_independent_and_plain_isolated(self):
        for source in (V1, V2):
            root, summary, events = self.copy_run(source)
            for mode in summary["additions"]:
                def reorder(request):
                    user = _parse_json(request["messages"][1]["content"])
                    request["messages"][1]["content"] = json.dumps(dict(reversed(list(user.items()))), ensure_ascii=False)
                self.change_request(root, summary, events, mode, reorder)
            self.seal(root, summary, events)
            self.assertEqual(self.inspect(root)["policy_version"], "report_to_delivery_v1" if source == V1 else "report_to_delivery_v2")
        for change in ("internal_report", "other_group_output", "system", "target_chars", "range", "budget"):
            with self.subTest(change=change):
                root, summary, events = self.copy_run()
                def contaminate(request):
                    if change == "system":
                        request["messages"][0]["content"] += " "
                        return
                    user = _parse_json(request["messages"][1]["content"])
                    if change == "range":
                        user["additional_char_range"]["min"] -= 1
                    elif change == "budget":
                        user["sentence_budget"]["slots"][0]["text_min_chars"] -= 1
                    else:
                        user[change] = 235 if change == "target_chars" else {"text": "不应传入普通侧"}
                    request["messages"][1]["content"] = _json_text(user)
                self.change_request(root, summary, events, MODES[1], contaminate)
                self.seal(root, summary, events)
                self.reject(root, "request_payload_mismatch" if change == "system" else "user_context_mismatch")

    def test_10_synthetic_success_only_exports_fully_accepted_pair(self):
        root, summary, events = self.copy_run()
        packet = blob(root, summary["inputs"]["packet"])
        index = blob(root, summary["inputs"]["source_index"])
        ids = [s["evidence_id"] for s in packet["evidence"]]
        for mode in MODES:
            row = summary["additions"][mode]
            sentences = [{"text": "甲乙丙丁"[i] * (width - 1) + ("？" if mode == MODES[0] and i == 3 else "。"),
                          "citations": [cid], "kind": "question" if mode == MODES[0] and i == 3 else "paraphrase"}
                         for i, (width, cid) in enumerate(zip((36, 35, 35, 35), (ids[0], ids[1], ids[2], ids[2])))]
            parsed = {"schema_version": "1.0", "packet_id": packet["packet_id"], "mode": mode, "sentences": sentences}
            old = deepcopy(row["response"])
            response = blob(root, old)
            response.update(raw_text=_json_text(parsed), response_id="offline-synthetic-" + mode)
            self.replace_ref([summary, events], old, self.capture(root, response))
            http = next(e for e in events if e["event_type"] == "mimo_response_received" and e["task_id"] == row["request_id"])
            old_http = delivery_audit._ref(http, "output")
            envelope = _parse_json(blob(root, old_http)["raw_http_text"])
            envelope["choices"][0]["message"]["content"] = response["raw_text"]
            envelope["id"] = response["response_id"]
            raw_http = _json_text(envelope)
            self.replace_ref([summary, events], old_http, self.capture(root, {"raw_http_text": raw_http}))
            for e in events:
                if e.get("task_id") == row["request_id"] and e["event_type"] in ("mimo_response_received", "mimo_request_completed"):
                    e["payload"].update(response_bytes=len(raw_http.encode("utf-8")), response_sha256=hashlib.sha256(raw_http.encode("utf-8")).hexdigest())
            rendered = delivery.render_addition(parsed, packet, mode, row["additional_char_range"], sentence_budget=row["sentence_budget"])
            row.update(status="validated", rendered=rendered, structured=self.capture(root, parsed))
            valid = next((e for e in events if e["event_type"] == "delivery_stage_validated" and e["phase"] == row["stage"]), None)
            if valid is None:
                valid = deepcopy(next(e for e in events if e["event_type"] == "delivery_stage_validated"))
                valid.update(event_id="offline-synthetic-plain-validation", phase=row["stage"])
                events.insert(-1, valid)
            valid.update(output_ref=row["structured"], payload={"response": row["response"], "rendered": rendered, "syntax_only": True})
        source_ids = [summary["additions"][m]["source_event_id"] for m in MODES]
        pair = make_dose_pair(packet, index, *(summary["additions"][m]["rendered"]["additional_text"] for m in MODES),
                              provenance={"content_origin": "recorded_assistant_output", "natural_view_used": False, "source_event_ids": source_ids})
        payloads = {arm: public_payload(pair, arm) for arm in ("I4", "I5")}
        summary.update(status="generated_structurally_valid", failed_stage=None, error=None, validated_stages=2,
                       expected_counts_passed=True, source_event_ids=source_ids, pair=self.capture(root, pair), pair_checks=pair["checks"],
                       payloads={arm: self.capture(root, payload) for arm, payload in payloads.items()},
                       lengths={arm: {"visible_chars": pair[arm]["char_count"], "additional_chars": pair[arm]["additional_chars"]} for arm in payloads},
                       expected_assembly={k: True for k in ("evidence_equal", "source_equal", "dose_within_range", "length_matched",
                                          "additional_length_matched", "responses_equal", "assembly_valid", "exact_assembly")})
        summary.pop("failed_raw_response")
        summary["last_counts"]["validated_stages"] = 2
        self.seal(root, summary, events)
        result = self.inspect(root)
        self.assertTrue(result["pair_available"])
        self.assertEqual(result["pair"], pair)
        self.assertEqual(result["subject_payloads"], payloads)
        self.assertEqual(result["stage_count"], 2)
        self.assertTrue(all(row["accepted"] for row in result["by_mode"].values()))
        self.assertFalse(result["subject_delivered"])
        self.assertFalse(result["full_I4_eligible"])

    def test_11_failure_code_rendered_and_frozen_budget_must_match(self):
        for change in ("error", "rendered", "budget", "status"):
            with self.subTest(change=change):
                root, summary, events = self.copy_run()
                row = summary["additions"][MODES[1]]
                if change == "error":
                    summary["error"]["code"] = "sentence_length_out_of_range"
                elif change == "rendered":
                    row["rendered"] = {"additional_text": "不能把失败候选伪装为通过"}
                elif change == "budget":
                    row["sentence_budget"]["slots"][0]["text_min_chars"] -= 1
                    row["sentence_budget_sha256"] = canonical_hash(row["sentence_budget"])
                else:
                    row["status"] = True
                self.seal(root, summary, events)
                self.reject(root, {"error": "failure_error_mismatch", "rendered": "failed_stage_has_rendered",
                                   "budget": "sentence_budget_mismatch", "status": "failed_stage_status_mismatch"}[change])

    def test_12_no_calls_credentials_or_writes_and_conservative_claims(self):
        result = self.inspect(V2)
        self.assertEqual(result["summary_sha256"], hashlib.sha256((V2 / "summary.json").read_bytes()).hexdigest())
        self.assertTrue(result["inputs_match"] and result["internal_report_preserved"])
        self.assertEqual(result["original_snapshot"]["file_count"], len(hashes(self.parent)))
        self.assertEqual(result["cost"]["total_cost_cny"], "0.004389")
        self.assertFalse(result["cost"]["independent_financial_accounting"])
        self.assertIn("不是独立金融核算", result["cost"]["basis"])
        self.assertEqual(result["budget_after"], self.summary2["billing_after"])
        self.assertEqual((result["audit_new_calls"], result["human_reviews_completed"]), (0, 0))
        self.assertFalse(result["full_I4_eligible"] or result["subject_delivered"] or result["network_called_by_audit"])
        self.assertNotIn("authorization", _json_text(result).casefold())
        result["by_mode"][MODES[1]]["parsed_output"]["schema_version"] = "测试调用者改自己的返回值"
        again = self.inspect(V2)
        self.assertIs(type(again["by_mode"][MODES[1]]["parsed_output"]["schema_version"]), float)


if __name__ == "__main__":
    unittest.main(verbosity=2)
