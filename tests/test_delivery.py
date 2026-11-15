"""短投递离线测试；仅假凭据和 HTTP mock，临时产物保留、不清理。

父运行仅只读复用；合成句子只测固定语法和边界，不声称语义或真实模型验证。
"""
import sys

sys.dont_write_bytecode = True

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon import delivery
from cocoon.budget import BudgetError, BudgetLedger
from cocoon.dose_build import source_index_from_review
from cocoon.live_audit import audit_live_run
from cocoon.mimo_probe import LEDGER_RELATIVE
from cocoon.runtime import _json_text, _parse_json, canonical_hash, verify_events

REAL_RUN = ROOT / "experiment/mimo_probes/assistant_probe_20261108T082120_9ad78cf8"
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
PACKET_PATH = BUILD / "packets/TOPIC_01_round_1.json"
POLICY_PATH = ROOT / "configs/delivery_prompts_v1.json"
POLICY_V2_PATH = ROOT / "configs/delivery_prompts_v2.json"
POLICY_V3_PATH = ROOT / "configs/delivery_prompts_v3.json"
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
FAKE_KEY = "sk-delivery-offline-only-test"
USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
         "prompt_tokens_details": {"cached_tokens": 600}}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return _parse_json(Path(path).read_bytes())


def fixture(packet, mode, size=235):
    """预先构造指定边界的合成模型响应，应用不得对它修补或截断。"""
    ids = [row["evidence_id"] for row in packet["evidence"]]
    count = size - sum(len(cid) + 2 for cid in ids)
    widths = [count // 3, count // 3, count - 2 * (count // 3)]
    sentences = []
    for number, (cid, width) in enumerate(zip(ids, widths)):
        question = number == 2 and mode == delivery.MODES[0]
        text = ("甲乙丙"[number] * (width - 1)) + ("？" if question else "。")
        sentences.append({"text": text, "citations": [cid],
                          "kind": "question" if question else "paraphrase"})
    return {"schema_version": "1.0", "packet_id": packet["packet_id"], "mode": mode, "sentences": sentences}


def fixture_v2(packet, mode, size=235):
    """四槽合成响应仅用于离线边界测试，不代表真实正文或语义验证。"""
    ids = [row["evidence_id"] for row in packet["evidence"]]
    citations = [ids[0], ids[1], ids[2], ids[2]]
    text_chars = size - sum(len(cid) + 2 for cid in citations)
    width, extra = divmod(text_chars, 4)
    sentences = []
    for number, cid in enumerate(citations):
        question = number == 3 and mode == delivery.MODES[0]
        text = "甲乙丙丁"[number] * (width + (number < extra) - 1) + ("？" if question else "。")
        sentences.append({"text": text, "citations": [cid],
                          "kind": "question" if question else "paraphrase"})
    return {"schema_version": "1.0", "packet_id": packet["packet_id"], "mode": mode, "sentences": sentences}


class HTTPResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.status = 200
        self.headers = {"x-request-id": "offline-delivery-only"}


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = read_json(ROOT / "configs/core.json")
        cls.packet = read_json(PACKET_PATH)
        cls.policy_sha = sha(POLICY_PATH)
        cls.frozen_policy_bytes = {str(path): path.read_bytes() for path in (POLICY_PATH, POLICY_V2_PATH)}
        cls.protected = list((ROOT / "cocoon").glob("*.py")) + list((ROOT / "tests").glob("*.py"))
        cls.protected += list((ROOT / "configs").glob("*.json"))
        cls.protected += [p for directory in (REAL_RUN, BUILD, ROOT / "experiment/billing")
                          for p in directory.rglob("*") if p.is_file()]
        cls.protected += [Path(cls.config["assistant"][k]) for k in ("entry_path", "source_path")]
        cls.before = {str(path): sha(path) for path in cls.protected}
        cls.addClassCleanup(cls.check_protected)

    @classmethod
    def check_protected(cls):
        if {str(path): sha(path) for path in cls.protected} != cls.before:
            raise AssertionError("原文件发生变化")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # 替换整个环境对象，不读取、复制或枚举实际环境中的密钥。
        self.stack.enter_context(mock.patch.object(os, "environ", {"MIMO_API_KEY": FAKE_KEY}))
        targets = ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                   "socket.socket.sendall", "socket.create_connection", "socket.getaddrinfo",
                   "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                   "urllib.request.HTTPHandler.http_open", "urllib.request.HTTPSHandler.https_open")
        self.guards = [self.stack.enter_context(mock.patch(target, side_effect=AssertionError("禁止网络访问")))
                       for target in targets]
        self.addCleanup(self.check_network)
        self.assertTrue(TEMP_ROOT.is_dir())
        self.root = Path(tempfile.mkdtemp(prefix="delivery_offline_only_", dir=TEMP_ROOT)).resolve()
        self.assertTrue(self.root.is_dir())
        (self.root / "experiment").mkdir()
        (self.root / "experiment/billing").mkdir()
        (self.root / "experiment/delivery_probes").mkdir()
        original_open, original_os_open = Path.open, os.open

        def guarded_open(path, mode="r", *args, **kwargs):
            if path.suffix.casefold() == ".md" or path.name.startswith(".env") or "semantic" in path.name.casefold():
                raise AssertionError("不读取文档、凭据或语义审阅文件")
            if any(flag in mode for flag in "wax+"):
                self.assertTrue(path.resolve().is_relative_to(self.root), "只写测试新目录")
            return original_open(path, mode, *args, **kwargs)

        def guarded_os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                self.assertTrue(Path(path).resolve().is_relative_to(self.root), "账本只写测试新目录")
            return original_os_open(path, flags, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(Path, "open", guarded_open))
        self.stack.enter_context(mock.patch.object(os, "open", guarded_os_open))
        self.ledger = BudgetLedger(self.root / LEDGER_RELATIVE, "5.00")
        self.wires, self.raws = [], []
        self.fault = None
        self.fault_at = 1
        self.size = 235
        self.opener = mock.Mock()
        self.opener.open.side_effect = self.respond
        self.build_opener = self.stack.enter_context(mock.patch("urllib.request.build_opener", return_value=self.opener))

    def check_network(self):
        for guard in self.guards:
            guard.assert_not_called()

    def respond(self, request, timeout):
        wire = _parse_json(request.data)
        self.wires.append(wire)
        user = _parse_json(wire["messages"][1]["content"])
        size = self.size if user["mode"] == delivery.MODES[0] else getattr(self, "plain_size", self.size)
        make_fixture = fixture_v2 if "sentence_budget" in user else fixture
        output = make_fixture(self.packet, user["mode"], size)
        fault = self.fault if len(self.wires) == self.fault_at else None
        if fault == "timeout":
            raise TimeoutError("不持久化此异常原文")
        if fault == "schema":
            output["extra"] = "禁止扩展字段"
        if fault == "numeric_schema":
            output["schema_version"] = 1.0
        if fault == "sentence_min":
            width = user["sentence_budget"]["slots"][0]["text_min_chars"] - 1
            output["sentences"][0]["text"] = "甲" * (width - 1) + "。"
        if fault == "length":
            output = make_fixture(self.packet, user["mode"], 500)
        if fault == "sentence_length":
            output["sentences"][0]["text"] = "甲" * 64 + "。"
        raw = _json_text(output)
        if fault == "json":
            raw = '{"schema_version":"1.0","schema_version":"1.0"}'
        if fault == "credential":
            raw += FAKE_KEY
        self.raws.append(raw)
        envelope = {"id": "offline-response-" + str(len(self.wires)), "model": self.config["models"]["assistant"]["model"],
                    "object": "chat.completion", "choices": [{"finish_reason": "stop", "message": {
                        "role": "assistant", "content": raw}}], "usage": deepcopy(USAGE)}
        if fault == "usage":
            envelope.pop("usage")
        return HTTPResponse(_json_text(envelope).encode("utf-8"))

    def run_fake(self, **changes):
        options = dict(run_directory=REAL_RUN, packet_path=PACKET_PATH, evidence_build=BUILD,
                       policy_path=POLICY_PATH, expected_policy_sha=self.policy_sha, execute=True)
        options.update(changes)
        return delivery.run_delivery(deepcopy(self.config), self.root, **options)

    def v2_policy(self):
        policy = read_json(POLICY_PATH)
        policy.update(version="report_to_delivery_v2", first_addition_min_chars=1,
                      first_addition_max_chars=800, sentence_count=4, preferred_target_chars=235)
        path = self.root / "policy_v2.json"
        path.write_text(_json_text(policy), encoding="utf-8")
        return path

    def run_v2(self):
        path = self.v2_policy()
        return self.run_fake(policy_path=path, expected_policy_sha=sha(path))

    def render_v2(self, output=None, mode=delivery.MODES[0], bounds=(143, 343), budget=None):
        if budget is None:
            budget = delivery.plan_sentence_budget(self.packet, bounds, 235)
        return delivery.render_addition(output if output is not None else fixture_v2(self.packet, mode),
                                        self.packet, mode, bounds, sentence_budget=budget)

    def changed_on_response(self, changed_path, call_no, *, unreadable=False):
        original_read = Path.read_bytes
        active = False
        self.wires.clear()
        self.raws.clear()

        def respond(request, timeout):
            nonlocal active
            response = self.respond(request, timeout)
            if len(self.wires) == call_no:
                active = True
            return response

        before_runs = set((self.root / "experiment/delivery_probes").iterdir())
        # 多个子用例共享测试账本，只定位本次新增运行。
        def current_overlay(path):
            if active and path == changed_path:
                current = set((self.root / "experiment/delivery_probes").iterdir()) - before_runs
                self.assertEqual(len(current), 1)
                out = current.pop()
                events = [_parse_json(line) for line in original_read(out / "events/events.jsonl").splitlines()]
                received = [event for event in events if event["event_type"] == "delivery_response_received"]
                self.assertEqual(len(received), call_no)
                self.assertTrue(all((out / event["output_ref"]["ref"]).is_file() for event in received))
                if unreadable:
                    raise OSError("测试输入暂不可读")
                return original_read(path) + b"\n"
            return original_read(path)

        self.opener.open.side_effect = respond
        with mock.patch.object(Path, "read_bytes", current_overlay):
            return self.run_fake()

    def assert_source_failure(self, result, calls, *, parent_changed=False):
        self.assertEqual(result["status"], "delivery_failed")
        self.assertEqual(result["failed_stage"], "source_integrity")
        self.assertEqual(result["error"], {"code": "source_changed", "safe_details": {}})
        self.assertFalse(result["inputs_unchanged"])
        self.assertEqual(result["internalreport_unchanged"], not parent_changed)
        self.assertEqual(result["actual_http_attempts"], calls)
        self.assertEqual(result["requests_started"], calls)
        self.assertEqual(result["completed_model_calls"], calls)
        for row, raw in zip(result["additions"].values(), self.raws):
            self.assertEqual(self.blob(result, row["raw_response"])["raw_text"], raw)
        self.assertEqual(Decimal(result["cost"]["known_cost_cny"]), Decimal("0.000612") * calls)
        self.assert_saved(result)

    def render(self, output=None, mode=delivery.MODES[0], bounds=(225, 245)):
        return delivery.render_addition(output if output is not None else fixture(self.packet, mode),
                                        self.packet, mode, bounds)

    def assert_code(self, code, callback):
        with self.assertRaises(delivery.DeliveryError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        return caught.exception

    def blob(self, result, descriptor):
        path = Path(result["run_directory"]) / descriptor["ref"]
        self.assertEqual(sha(path), descriptor["sha256"])
        return read_json(path)

    def assert_saved(self, result):
        out = Path(result["run_directory"])
        self.assertEqual(read_json(out / "summary.json"), result)
        self.assertEqual(verify_events(out), result["verification"])
        for path in out.rglob("*"):
            if path.is_file():
                self.assertNotIn(FAKE_KEY.encode(), path.read_bytes())

    def test_01_current_policy_exact_bytes(self):
        self.assertEqual(delivery.load_delivery_policy(POLICY_PATH, self.policy_sha), read_json(POLICY_PATH))
        self.assertEqual(delivery.load_delivery_policy(POLICY_PATH, self.policy_sha)["max_completion_tokens"], 1024)

    def test_02_policy_hash_not_canonical(self):
        path = self.root / "policy.json"
        path.write_bytes(POLICY_PATH.read_bytes() + b"\n")
        self.assert_code("policy_hash_mismatch", lambda: delivery.load_delivery_policy(path, self.policy_sha))
        self.assertEqual(delivery.load_delivery_policy(path, sha(path)), read_json(POLICY_PATH))

    def test_03_policy_rejects_shape_fixed_values_duplicates(self):
        for field, value in (("automatic_retries", 1), ("max_completion_tokens", 4096),
                             ("human_reviews_completed", False), ("formal_eligible", True), ("extra", 1)):
            with self.subTest(field=field):
                policy = read_json(POLICY_PATH)
                policy[field] = value
                path = self.root / (field + ".json")
                path.write_text(_json_text(policy), encoding="utf-8")
                self.assert_code("invalid_policy", lambda: delivery.load_delivery_policy(path, sha(path)))
        path = self.root / "duplicate.json"
        path.write_bytes(b'{"schema_version":"1.0","schema_version":"1.0"}')
        self.assert_code("invalid_policy", lambda: delivery.load_delivery_policy(path, sha(path)))

    def test_04_exact_output_and_sentence_shape(self):
        for change in ("extra", "missing", "wrong_packet", "wrong_version", "extra_item", "few", "many"):
            output = fixture(self.packet, delivery.MODES[0])
            if change == "extra":
                output["extra"] = 1
            elif change == "missing":
                del output["mode"]
            elif change == "wrong_packet":
                output["packet_id"] = "different"
            elif change == "wrong_version":
                output["schema_version"] = 1.0
            elif change == "extra_item":
                output["sentences"][0]["extra"] = 1
            elif change == "few":
                output["sentences"].pop()
            else:
                output["sentences"] *= 2
            with self.subTest(change=change):
                self.assert_code("invalid_schema", lambda: self.render(output))

    def test_05_json_duplicate_keys_rejected(self):
        raw = _json_text(fixture(self.packet, delivery.MODES[0]))
        for changed in (raw.replace('"schema_version":"1.0"', '"schema_version":"1.0","schema_version":"1.0"'),
                        raw.replace('"kind":"paraphrase"', '"kind":"paraphrase","kind":"paraphrase"', 1)):
            self.assert_code("invalid_json", lambda: self.render(changed))

    def test_06_packet_demo_or_wrong_evidence_count_rejected(self):
        for change in ("demo", "count", "hash"):
            packet = deepcopy(self.packet)
            if change == "demo":
                packet["is_demo"] = True
            elif change == "count":
                packet["evidence"].pop()
            else:
                packet["evidence"][0]["text"] += "变动"
            self.assert_code("invalid_packet", lambda: delivery.render_addition(
                fixture(self.packet, delivery.MODES[0]), packet, delivery.MODES[0], (225, 245)))

    def test_07_citation_unknown_duplicate_empty_types(self):
        cid = self.packet["evidence"][0]["evidence_id"]
        for citations in ([], [cid, cid], ["UNKNOWN_E01"], [True], cid, [[cid]]):
            output = fixture(self.packet, delivery.MODES[0])
            output["sentences"][0]["citations"] = citations
            self.assert_code("invalid_citations", lambda: self.render(output))

    def test_08_covers_all_three_and_keeps_citation_order(self):
        output = fixture(self.packet, delivery.MODES[0])
        output["sentences"][2]["citations"] = output["sentences"][0]["citations"]
        self.assert_code("incomplete_citations", lambda: self.render(output))
        output = fixture(self.packet, delivery.MODES[0])
        output["sentences"][0]["citations"] = [self.packet["evidence"][1]["evidence_id"], self.packet["evidence"][0]["evidence_id"]]
        rendered = self.render(output, bounds=(0, 1000))
        self.assertIn("（CL_E02、CL_E01）", rendered["additional_text"])
        self.assertEqual(len(rendered["covered_evidence_ids"]), 3)

    def test_09_fixed_text_rules(self):
        for text in ("CL_E01说明。", "可看http地址。", "可看WWW地址。", "I4文本。", "i5文本。",
                     "<b>文字</b>", "<img", "你认为。", "您认为。", "用户认为。", "权威来源。",
                     "可信结论。", "CL_S01说明。", "F_CL_NOAA_CARBON_CYCLE_EXPLAINERS说明。",
                     "请回答问题。", "已审核。", "```文本```", "\x00文本", "文本\r", "English only."):
            output = fixture(self.packet, delivery.MODES[0])
            output["sentences"][0]["text"] = text
            with self.subTest(text=text):
                self.assert_code("forbidden_text", lambda: self.render(output, bounds=(0, 1000)))

    def test_10_organized_question_counts_one_or_two(self):
        for count in range(4):
            output = fixture(self.packet, delivery.MODES[0])
            for number, sentence in enumerate(output["sentences"]):
                sentence["kind"] = "question" if number < count else "limitation"
            if count in (1, 2):
                self.assertEqual(self.render(output)["question_count"], count)
            else:
                self.assert_code("invalid_questions", lambda: self.render(output))

    def test_11_plain_never_question_or_question_mark(self):
        for change in ("kind", "?", "？"):
            output = fixture(self.packet, delivery.MODES[1])
            if change == "kind":
                output["sentences"][0]["kind"] = "question"
            else:
                output["sentences"][0]["text"] += change
            self.assert_code("invalid_questions", lambda: self.render(output, mode=delivery.MODES[1]))
        self.assert_code("invalid_mode", lambda: self.render(mode="unknown"))

    def test_12_render_no_trimming_repair_or_padding(self):
        output = fixture(self.packet, delivery.MODES[0])
        output["sentences"][0]["text"] = " \n" + output["sentences"][0]["text"] + "\t "
        before = deepcopy(output)
        result = self.render(output, bounds=(0, 1000))
        self.assertEqual(output, before)
        self.assertEqual(result["structured_output"], before)
        expected = "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in output["sentences"])
        self.assertEqual(result["additional_text"], expected)
        self.assertTrue(result["syntax_only"])
        self.assertFalse(result["semantic_guarantee"])

    def test_13_long_output_rejected_not_truncated(self):
        output = fixture(self.packet, delivery.MODES[0], 500)
        before = deepcopy(output)
        error = self.assert_code("length_out_of_range", lambda: self.render(output))
        self.assertEqual(error.safe_details, {"len": 500, "min": 225, "max": 245})
        self.assertEqual(before, output)

    def test_14_closed_integer_bounds(self):
        for size in (225, 245):
            self.assertEqual(self.render(fixture(self.packet, delivery.MODES[0], size))["char_count"], size)
        for size in (224, 246):
            self.assert_code("length_out_of_range", lambda: self.render(fixture(self.packet, delivery.MODES[0], size)))
        for bounds in ((True, 245), (225, 245.0), (246, 225), (-1, 245), {"min": 225}, "225-245"):
            self.assert_code("invalid_char_range", lambda: self.render(bounds=bounds))

    def test_15_two_fake_success_assembly_usage_and_cost(self):
        result = self.run_fake()
        self.assertEqual(result["status"], "generated_structurally_valid", result.get("error"))
        self.assertEqual(result["actual_http_attempts"], 2)
        self.assertTrue(result["expected_counts_passed"])
        self.assertTrue(all(result["expected_assembly"].values()))
        self.assertEqual(result["allowed_additional_char_range"], {"min": 143, "max": 343})
        self.assertEqual(result["fixed_chars"], 457)
        self.assertEqual(result["public_constant_chars"], 39)
        for values in result["lengths"].values():
            self.assertTrue(600 <= values["visible_chars"] <= 800)
        self.assertLessEqual(result["pair_checks"]["additional_difference_ratio"], 0.1)
        self.assertEqual(Decimal(result["cost"]["known_cost_cny"]), Decimal("0.001224"))
        self.assertEqual(result["cost"]["total_cost_cny"], result["cost"]["known_cost_cny"])
        pair = self.blob(result, result["pair"])
        self.assertEqual(pair["provenance"]["source_event_ids"], result["source_event_ids"])
        self.assertEqual(len(result["source_event_ids"]), 2)
        self.assertFalse(result["received_actual_subject"])
        self.assertFalse(result["full_I4_eligible"])
        self.assertFalse(result["mirror_executed"])
        self.assert_saved(result)
        original_pair = delivery.make_dose_pair
        def stricter_pair(*args, **kwargs):
            return original_pair(*args, **kwargs, min_chars=700)
        with mock.patch.object(delivery, "make_dose_pair", side_effect=stricter_pair):
            deviated = self.run_fake()
        self.assertEqual(deviated["status"], "generated_with_deviations")
        self.assertFalse(deviated["expected_assembly"]["dose_within_range"])
        self.assert_saved(deviated)

    def test_16_plain_context_isolated_and_exact_requests(self):
        self.size, self.plain_size = 229, 251
        result = self.run_fake()
        self.assertEqual(result["status"], "generated_structurally_valid")
        policy = read_json(POLICY_PATH)
        for number, (mode, stage) in enumerate(zip(delivery.MODES, delivery.STAGES)):
            row = result["additions"][mode]
            request = self.blob(result, row["request"])
            self.assertEqual(request["request_id"], result["run_id"] + ":" + stage + ":1")
            self.assertEqual(request["stage"], stage)
            self.assertEqual(set(request["model_settings"]), {"provider", "model", "temperature", "max_tokens"})
            self.assertEqual(request["model_settings"]["max_tokens"], 1024)
            self.assertEqual(self.wires[number]["max_completion_tokens"], 1024)
            self.assertEqual(request["messages"][0]["content"], policy["common_prompt"] + "\n" + policy["mode_prompts"][mode])
            user = _parse_json(request["messages"][1]["content"])
            expected = {"frozen_evidence_packet", "mode", "additional_char_range", "target_chars"}
            self.assertEqual(set(user), expected | ({"internal_report"} if number == 0 else set()))
            self.assertEqual(user["frozen_evidence_packet"], self.packet)
            if number == 0:
                self.assertEqual(user["internal_report"], self.blob(result, result["inputs"]["internal_report"]))
                self.assertEqual(user["additional_char_range"], {"min": 225, "max": 245})
            else:
                self.assertEqual(user["target_chars"], 229)
                self.assertEqual(user["additional_char_range"], {"min": (2290 + 10) // 11, "max": 251})
                self.assertNotIn(self.raws[0], request["messages"][1]["content"])
                self.assertNotIn("internal_report", request["messages"][1]["content"])
        self.assertEqual(len(self.blob(result, result["inputs"]["source_index"])), 13)

    def test_17_dry_run_no_directory_or_request(self):
        before = sha(self.root / LEDGER_RELATIVE)
        result = self.run_fake(execute=False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertIsNone(result["run_directory"])
        self.assertFalse(list((self.root / "experiment/delivery_probes").iterdir()))
        self.assertEqual(before, sha(self.root / LEDGER_RELATIVE))
        self.build_opener.assert_not_called()

    def test_18_missing_key_no_requests(self):
        with mock.patch.object(os, "environ", {}):
            result = self.run_fake()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_MIMO_API_KEY", result["blockers"])
        self.assertEqual(result["actual_http_attempts"], 0)
        self.assertIsNone(result["run_directory"])
        self.build_opener.assert_not_called()

    def test_19_policy_mismatch_before_ledger_or_charging(self):
        before = sha(self.root / LEDGER_RELATIVE)
        with mock.patch.object(delivery, "mimo_status", side_effect=AssertionError("不应读取账本")):
            result = self.run_fake(expected_policy_sha="0" * 64)
        self.assertEqual(result["error"]["code"], "policy_hash_mismatch")
        self.assertEqual(before, sha(self.root / LEDGER_RELATIVE))
        self.build_opener.assert_not_called()

    def test_20_packet_must_match_frozen_and_same_parent(self):
        for change in ("text", "canonical_only", "other_round"):
            packet = deepcopy(self.packet)
            if change == "text":
                packet["evidence"][0]["text"] += "变化。"
                packet["evidence"][0]["content_sha256"] = hashlib.sha256(packet["evidence"][0]["text"].encode()).hexdigest()
            path = self.root / (change + ".json")
            if change == "other_round":
                path.write_bytes((BUILD / "packets/TOPIC_01_round_2.json").read_bytes())
            else:
                path.write_bytes(_json_text(packet).encode() + (b"\n" if change == "canonical_only" else b""))
            result = self.run_fake(packet_path=path)
            self.assertEqual(result["error"]["code"], "source_audit_failed" if change == "other_round" else "source_packet_mismatch")
            self.assertIsNone(result["run_directory"])
        self.build_opener.assert_not_called()

    def test_21_parent_original_hash_and_unconstrained_report(self):
        before = delivery._run_hashes(REAL_RUN)
        result = self.run_fake()
        self.assertTrue(result["internalreport_unchanged"])
        self.assertEqual(before, delivery._run_hashes(REAL_RUN))
        audit = audit_live_run(REAL_RUN, PACKET_PATH, self.config["assistant"]["prompt_bundle_path"],
                               self.config["assistant"]["prompt_bundle_sha256"], source_index_from_review(BUILD))
        self.assertEqual(result["parent_summary_sha256"], audit["summary_sha256"])
        self.assertEqual(result["parent_report_sha256"], canonical_hash(audit["stage_results"]["integrate"]["structured_output"]))
        self.assertGreater(result["original_internal_report"]["rendered_chars"], 800)
        self.assertIsNone(result["original_internal_report"]["length_constraint"])
        self.assertEqual(self.blob(result, result["inputs"]["parent_file_hashes"]), before)

    def test_22_first_schema_failure_only_one_call_raw_retained(self):
        self.fault = "schema"
        result = self.run_fake()
        self.assertEqual(result["status"], "delivery_failed")
        self.assertEqual(result["error"]["code"], "invalid_schema")
        self.assertEqual(result["actual_model_calls"], 1)
        self.assertEqual(result["validated_stages"], 0)
        self.assertNotIn(delivery.MODES[1], result["additions"])
        response = self.blob(result, result["failed_raw_response"])
        self.assertEqual(response["raw_text"], self.raws[0])
        self.assertEqual(set(response), {"raw_text", "provider", "model", "response_id", "usage", "finish_reason", "is_demo"})
        self.assert_saved(result)

    def test_23_first_length_failure_no_next_call_or_repair(self):
        self.fault = "length"
        result = self.run_fake()
        self.assertEqual(result["actual_http_attempts"], 1)
        self.assertEqual(result["error"], {"code": "length_out_of_range", "safe_details": {"len": 500, "min": 225, "max": 245}})
        self.assertEqual(self.blob(result, result["failed_raw_response"])["raw_text"], self.raws[0])
        self.assertIsNotNone(result["cost"]["total_cost_cny"])
        self.assert_saved(result)

    def test_24_second_json_failure_preserves_both_raw_results(self):
        self.fault, self.fault_at = "json", 2
        result = self.run_fake()
        self.assertEqual(result["failed_stage"], delivery.STAGES[1])
        self.assertEqual(result["error"]["code"], "invalid_json")
        self.assertEqual(result["actual_http_attempts"], 2)
        self.assertEqual(result["validated_stages"], 1)
        self.assertIsNone(result["pair"])
        for mode, raw in zip(delivery.MODES, self.raws):
            self.assertEqual(self.blob(result, result["additions"][mode]["response"])["raw_text"], raw)
        self.assert_saved(result)

    def test_25_timeout_uncertainty_survives_next_run_no_retry(self):
        self.fault = "timeout"
        result = self.run_fake()
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertEqual(result["actual_http_attempts"], 1)
        self.assertTrue(result["cost"]["ledger_uncertain"])
        self.assertIsNone(result["cost"]["total_cost_cny"])
        self.assertEqual(result["billing_after"]["uncertain_request_count"], 1)
        before = sha(self.root / LEDGER_RELATIVE)
        self.assert_saved(result)
        next_result = self.run_fake()
        self.assertEqual(next_result["status"], "blocked")
        self.assertEqual(next_result["actual_http_attempts"], 0)
        self.assertEqual(before, sha(self.root / LEDGER_RELATIVE))
        self.assertEqual(len(list((self.root / "experiment/delivery_probes").iterdir())), 1)
        self.assertEqual(len(self.wires), 1)

    def test_26_missing_usage_preserves_http_and_uncertainty(self):
        self.fault = "usage"
        result = self.run_fake()
        self.assertEqual(result["error"]["code"], "missing_usage")
        self.assertEqual(result["actual_http_attempts"], 1)
        self.assertIsNone(result["cost"]["total_cost_cny"])
        raw = self.blob(result, result["failed_raw_response"])
        self.assertIn("raw_http_text", raw)
        self.assertIn(self.raws[0], _parse_json(raw["raw_http_text"])["choices"][0]["message"]["content"])
        self.assert_saved(result)

    def test_27_budget_error_counts_started_not_used(self):
        with mock.patch.object(BudgetLedger, "reserve", side_effect=BudgetError("budget_exceeded")):
            result = self.run_fake()
        self.assertEqual(result["error"]["code"], "budget_exceeded")
        self.assertEqual(result["requests_started"], 1)
        self.assertEqual(result["actual_http_attempts"], 0)
        self.assertEqual(result["completed_model_calls"], 0)
        self.assertEqual(result["billing_after"]["request_count"], 0)
        self.build_opener.assert_not_called()
        self.assert_saved(result)

    def test_28_credentials_never_recorded_in_input_or_response(self):
        config = deepcopy(self.config)
        config["test_input"] = FAKE_KEY
        result = delivery.run_delivery(config, self.root, run_directory=REAL_RUN, packet_path=PACKET_PATH,
            evidence_build=BUILD, policy_path=POLICY_PATH, expected_policy_sha=self.policy_sha, execute=True)
        self.assertEqual(result["error"]["code"], "credential_in_request")
        self.assertIsNone(result["run_directory"])
        self.build_opener.assert_not_called()
        policy = read_json(POLICY_PATH)
        policy["common_prompt"] += FAKE_KEY
        policy_bytes = _json_text(policy).encode("utf-8")
        original_read = Path.read_bytes
        def policy_overlay(path):
            return policy_bytes if path == POLICY_PATH else original_read(path)
        with mock.patch.object(Path, "read_bytes", policy_overlay):
            result = self.run_fake(expected_policy_sha=hashlib.sha256(policy_bytes).hexdigest())
        self.assertEqual(result["error"]["code"], "credential_in_request")
        self.assertIsNone(result["run_directory"])
        original_audit = delivery.audit_live_run
        def contaminated_report(*args):
            audit = original_audit(*args)
            audit["stage_results"]["integrate"]["structured_output"]["sections"][0]["items"][0]["text"] += FAKE_KEY
            return audit
        with mock.patch.object(delivery, "audit_live_run", side_effect=contaminated_report):
            result = self.run_fake()
        self.assertEqual(result["error"]["code"], "credential_in_request")
        self.assertIsNone(result["run_directory"])
        self.assertFalse(list((self.root / "experiment/delivery_probes").iterdir()))
        self.build_opener.assert_not_called()
        self.fault = "credential"
        result = self.run_fake()
        self.assertEqual(result["error"]["code"], "response_not_recordable")
        self.assertEqual(result["actual_http_attempts"], 1)
        self.assert_saved(result)

    def test_29_cli_short_summary_no_raw_stdout(self):
        result = self.run_fake()
        output = io.StringIO()
        args = ["--source-run", str(REAL_RUN), "--packet", str(PACKET_PATH), "--evidence-build", str(BUILD),
                "--policy", str(POLICY_PATH), "--policy-sha", self.policy_sha]
        with mock.patch.object(delivery, "run_delivery", return_value=result), redirect_stdout(output):
            self.assertEqual(delivery.main(args), 0)
        short = _parse_json(output.getvalue())
        self.assertEqual(set(short), {"status", "runpath", "counts", "lengths", "cost", "failedstage"})
        self.assertNotIn("raw_text", output.getvalue())
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_30_missing_parents_ledger_and_audit_failure_block(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = delivery.run_delivery(deepcopy(self.config), empty, run_directory=REAL_RUN, packet_path=PACKET_PATH,
            evidence_build=BUILD, policy_path=POLICY_PATH, expected_policy_sha=self.policy_sha, execute=True)
        self.assertEqual(result["error"]["code"], "existing_ledger_required")
        original_is_dir = Path.is_dir
        def is_dir(path):
            return False if path == self.root / "experiment/delivery_probes" else original_is_dir(path)
        with mock.patch.object(Path, "is_dir", is_dir):
            result = self.run_fake()
        self.assertEqual(result["error"]["code"], "missing_delivery_parent")
        with mock.patch.object(delivery, "audit_live_run", side_effect=ValueError("只返回安全短码")):
            result = self.run_fake()
        self.assertEqual(result["error"]["code"], "source_audit_failed")
        self.build_opener.assert_not_called()
        self.assertFalse(list((self.root / "experiment/delivery_probes").iterdir()))

    def test_31_v2_policy_from_temporary_v1_copy(self):
        path = self.v2_policy()
        policy = delivery.load_delivery_policy(path, sha(path))
        original = read_json(POLICY_PATH)
        self.assertEqual(set(policy), set(original) | {"sentence_count", "preferred_target_chars"})
        expected_changes = {"version": "report_to_delivery_v2", "first_addition_min_chars": 1,
                            "first_addition_max_chars": 800, "sentence_count": 4, "preferred_target_chars": 235}
        self.assertEqual(policy, {**original, **expected_changes})
        self.assertEqual(policy["mode_prompts"], original["mode_prompts"])
        digest = sha(path)
        path.write_bytes(path.read_bytes() + b"\n")
        self.assert_code("policy_hash_mismatch", lambda: delivery.load_delivery_policy(path, digest))
        self.assertEqual(delivery.load_delivery_policy(path, sha(path)), policy)

    def test_32_v2_policy_exact_fields_types_and_values(self):
        path = self.v2_policy()
        valid = read_json(path)
        cases = []
        for key, value in delivery._POLICY_FIXED_V2.items():
            wrong_type = (False if type(value) is int else 0 if type(value) is bool
                          else str(value) if type(value) is float else [value])
            cases.append((key + "_type", {**valid, key: wrong_type}))
        for key, value in (("version", "unknown"), ("first_addition_min_chars", 225),
                           ("first_addition_max_chars", 245), ("sentence_count", 3),
                           ("preferred_target_chars", 234), ("max_completion_tokens", 2048),
                           ("automatic_retries", 1), ("extra", 1)):
            cases.append((key + "_value", {**valid, key: value}))
        for key in ("sentence_count", "preferred_target_chars", "common_prompt"):
            missing = deepcopy(valid)
            del missing[key]
            cases.append((key + "_missing", missing))
        for key in ("sentence_count", "preferred_target_chars"):
            cases.append(("v1_" + key, {**read_json(POLICY_PATH), key: valid[key]}))
        for name, policy in cases:
            with self.subTest(case=name):
                path.write_text(_json_text(policy), encoding="utf-8")
                self.assert_code("invalid_policy", lambda: delivery.load_delivery_policy(path, sha(path)))

    def test_33_budget_exact_shape_and_rounding(self):
        ids = [row["evidence_id"] for row in self.packet["evidence"]]
        for bounds, target, low, high, wanted in (((143, 343), 235, 28, 64, 51),
                                                 ((204, 246), 224, 43, 53, 48),
                                                 ((143, 343), 234, 28, 64, 50),
                                                 ((143, 343), -100, 28, 64, 28),
                                                 ((143, 343), 10 ** 400, 28, 64, 64),
                                                 ((0, 800), 32, 1, 64, 1)):
            with self.subTest(bounds=bounds, target=target):
                budget = delivery.plan_sentence_budget(self.packet, bounds, target)
                self.assertEqual(budget, {"version": "four_sentence_budget_v2", "slots": [
                    {"index": number, "citation_id": cid, "text_min_chars": low,
                     "text_max_chars": high, "target_text_chars": wanted}
                    for number, cid in enumerate([ids[0], ids[1], ids[2], ids[2]])],
                    "rendered_citation_chars": 32, "sentence_count": 4})
                self.assertGreaterEqual(4 * low + 32, bounds[0])
                self.assertLessEqual(4 * high + 32, bounds[1])
                for slot in budget["slots"]:
                    self.assertTrue(all(type(slot[key]) is int for key in
                        ("index", "text_min_chars", "text_max_chars", "target_text_chars")))

    def test_34_budget_rejects_empty_ranges_types_and_unverified_packet(self):
        for bounds in ((0, 35), (145, 147), (289, 343)):
            self.assert_code("empty_char_range", lambda: delivery.plan_sentence_budget(self.packet, bounds, 235))
        for target in (True, 235.0, "235", None):
            self.assert_code("invalid_char_range", lambda: delivery.plan_sentence_budget(self.packet, (143, 343), target))
        for bounds in ((True, 343), (143, 343.0), (344, 343), {"min": 143}):
            self.assert_code("invalid_char_range", lambda: delivery.plan_sentence_budget(self.packet, bounds, 235))
        for change in ("hash", "demo", "count"):
            packet = deepcopy(self.packet)
            if change == "hash":
                packet["evidence"][0]["text"] += "变动。"
            elif change == "demo":
                packet["is_demo"] = True
            else:
                packet["evidence"].pop()
            self.assert_code("invalid_packet", lambda: delivery.plan_sentence_budget(packet, (143, 343), 235))

    def test_35_v2_render_preserves_composite_sentences_and_budget(self):
        for mode in delivery.MODES:
            output = fixture_v2(self.packet, mode)
            output["sentences"][0]["text"] = " 甲。乙，丙；" + "甲" * 40 + "。 "
            output["sentences"][1]["kind"] = "limitation"
            if mode == delivery.MODES[0]:
                output["sentences"][3]["text"] = output["sentences"][3]["text"][:-1] + "?"
            budget = delivery.plan_sentence_budget(self.packet, (143, 343), 235)
            before, before_budget = deepcopy(output), deepcopy(budget)
            rendered = self.render_v2(output, mode=mode, budget=budget)
            self.assertEqual(output, before)
            self.assertEqual(budget, before_budget)
            self.assertEqual(rendered["structured_output"], before)
            self.assertEqual(rendered["additional_text"], "".join(
                row["text"] + "（" + row["citations"][0] + "）" for row in output["sentences"]))
            self.assertTrue(rendered["text_unchanged"])
            self.assertEqual(rendered["question_count"], int(mode == delivery.MODES[0]))

    def test_36_v2_four_slots_v1_still_accepts_three_to_five(self):
        for count in (3, 5):
            output = fixture_v2(self.packet, delivery.MODES[0])
            if count == 3:
                output["sentences"].pop(0)
            else:
                output["sentences"].append({"text": "额外复述。", "kind": "paraphrase",
                                            "citations": [self.packet["evidence"][0]["evidence_id"]]})
            self.assert_code("invalid_schema", lambda: self.render_v2(output))
        for count in (3, 4, 5):
            output = fixture(self.packet, delivery.MODES[0]) if count == 3 else fixture_v2(self.packet, delivery.MODES[0])
            if count == 5:
                output["sentences"].append({"text": "额外复述。", "kind": "paraphrase",
                                            "citations": [self.packet["evidence"][0]["evidence_id"]]})
            self.assertEqual(len(self.render(output, bounds=(0, 1000))["structured_output"]["sentences"]), count)

    def test_37_v2_exact_single_citation_per_slot(self):
        ids = [row["evidence_id"] for row in self.packet["evidence"]]
        for number in range(4):
            for citations in ([], [True], [ids[0], ids[1]], [ids[0], ids[0]],
                              ["UNKNOWN_E01"], [ids[1] if number != 1 else ids[0]]):
                output = fixture_v2(self.packet, delivery.MODES[0])
                output["sentences"][number]["citations"] = citations
                with self.subTest(slot=number, citations=citations):
                    self.assert_code("invalid_citations", lambda: self.render_v2(output))

    def test_38_v2_question_positions_and_plain_rules(self):
        for number in range(3):
            output = fixture_v2(self.packet, delivery.MODES[0])
            output["sentences"][number]["kind"] = "question"
            self.assert_code("invalid_questions", lambda: self.render_v2(output))
        for change in ("kind", "ending", "trailing_space"):
            output = fixture_v2(self.packet, delivery.MODES[0])
            last = output["sentences"][3]
            if change == "kind":
                last["kind"] = "limitation"
            elif change == "ending":
                last["text"] = last["text"][:-1] + "。"
            else:
                last["text"] += " "
            self.assert_code("invalid_questions", lambda: self.render_v2(output))
        for number in range(4):
            for change in ("kind", "?", "？"):
                output = fixture_v2(self.packet, delivery.MODES[1])
                if change == "kind":
                    output["sentences"][number]["kind"] = "question"
                else:
                    output["sentences"][number]["text"] += change
                self.assert_code("invalid_questions", lambda: self.render_v2(output, mode=delivery.MODES[1]))

    def test_39_v2_caller_budget_cannot_relax_shape_types_or_coverage(self):
        valid = delivery.plan_sentence_budget(self.packet, (143, 343), 235)
        cases = [False, [], {}, {**valid, "extra": 1}, {**valid, "sentence_count": True},
                 {**valid, "sentence_count": 4.0}, {**valid, "version": "other"},
                 {**valid, "rendered_citation_chars": 32.0}, {**valid, "rendered_citation_chars": 0},
                 {**valid, "slots": tuple(valid["slots"])}, {**valid, "slots": valid["slots"][:3]}]
        for field, values in {"index": [False, 0.0, "0", 1], "citation_id": [True, [], "UNKNOWN_E01"],
                              "text_min_chars": [True, 28.0, "28", 0, 65],
                              "text_max_chars": [False, 64.0, "64", 27, 65, 800],
                              "target_text_chars": [True, 51.0, "51", 0, 65]}.items():
            for value in values:
                budget = deepcopy(valid)
                budget["slots"][0][field] = value
                cases.append(budget)
        for change in ("duplicate_index", "all_same_citation", "extra", "missing", "non_dict", "different_target"):
            budget = deepcopy(valid)
            if change == "duplicate_index":
                budget["slots"][1]["index"] = 0
            elif change == "all_same_citation":
                for slot in budget["slots"]:
                    slot["citation_id"] = budget["slots"][0]["citation_id"]
            elif change == "extra":
                budget["slots"][0]["extra"] = 1
            elif change == "missing":
                del budget["slots"][0]["text_min_chars"]
            elif change == "non_dict":
                budget["slots"][0] = []
            else:
                budget["slots"][1]["target_text_chars"] = 50
            cases.append(budget)
        for number, budget in enumerate(cases):
            with self.subTest(case=number):
                self.assert_code("invalid_sentence_budget", lambda: self.render_v2(budget=budget))

    def test_40_v2_sentence_and_whole_length_errors_no_repair(self):
        for width in (27, 28, 64, 65):
            output = fixture_v2(self.packet, delivery.MODES[0])
            output["sentences"][0]["text"] = "甲" * (width - 1) + "。"
            before = deepcopy(output)
            if width in (28, 64):
                self.assertEqual(self.render_v2(output)["structured_output"], before)
            else:
                error = self.assert_code("sentence_length_out_of_range", lambda: self.render_v2(output))
                self.assertEqual(error.safe_details, {"len": width, "min": 28, "max": 64})
                self.assertTrue(all(type(value) is int for value in error.safe_details.values()))
            self.assertEqual(output, before)
        output = fixture_v2(self.packet, delivery.MODES[0], 500)
        before = deepcopy(output)
        error = self.assert_code("length_out_of_range", lambda: self.render_v2(output))
        self.assertEqual(error.safe_details, {"len": 500, "min": 143, "max": 343})
        self.assertEqual(output, before)
        self.assertEqual(delivery.DeliveryError("sentence_length_out_of_range", {
            "len": True, "min": 1.0, "max": 64, "text": "不应回显"}).safe_details, {"max": 64})

    def test_41_v2_two_fake_calls_budget_messages_assembly_and_ledger(self):
        self.size = 224
        result = self.run_v2()
        self.assertEqual(result["status"], "generated_structurally_valid", result.get("error"))
        self.assertEqual(result["policy_version"], "report_to_delivery_v2")
        self.assertEqual(result["actual_http_attempts"], 2)
        self.assertEqual(result["validated_stages"], 2)
        self.assertTrue(result["expected_counts_passed"])
        self.assertTrue(result["inputs_unchanged"])
        self.assertTrue(result["internalreport_unchanged"])
        self.assertTrue(all(result["expected_assembly"].values()))
        self.assertEqual(Decimal(result["cost"]["known_cost_cny"]), Decimal("0.001224"))
        self.assertEqual(result["billing_source_path"], str(self.root / LEDGER_RELATIVE))
        self.assertEqual(result["source_index_input"], {"source_path": str(BUILD / "review/sources.json"),
                                                       "file_sha256": sha(BUILD / "review/sources.json")})
        for number, mode in enumerate(delivery.MODES):
            row = result["additions"][mode]
            request = self.blob(result, row["request"])
            user = _parse_json(request["messages"][1]["content"])
            bounds = {"min": 143, "max": 343} if number == 0 else {"min": 204, "max": 246}
            target = 235 if number == 0 else 224
            expected_keys = {"frozen_evidence_packet", "mode", "additional_char_range", "target_chars", "sentence_budget"}
            self.assertEqual(set(user), expected_keys | ({"internal_report"} if number == 0 else set()))
            self.assertEqual(user["additional_char_range"], bounds)
            self.assertEqual(user["target_chars"], target)
            self.assertEqual(row["policy_version"], "report_to_delivery_v2")
            self.assertEqual(row["sentence_budget"], delivery.plan_sentence_budget(self.packet, bounds, target))
            self.assertEqual(row["sentence_budget"], user["sentence_budget"])
            self.assertEqual(row["sentence_budget_sha256"], canonical_hash(row["sentence_budget"]))
            self.assertEqual(self.wires[number]["max_completion_tokens"], 1024)
            self.assertEqual(row["rendered"]["char_count"], 224)
            self.assertEqual(self.blob(result, row["response"])["raw_text"], self.raws[number])
        self.assert_saved(result)

    def test_42_v2_slot_failures_save_raw_and_never_retry(self):
        self.fault = "sentence_length"
        for call in (1, 2):
            self.wires.clear()
            self.raws.clear()
            self.fault_at = call
            result = self.run_v2()
            self.assertEqual(result["status"], "delivery_failed")
            self.assertEqual(result["actual_http_attempts"], call)
            self.assertEqual(result["validated_stages"], call - 1)
            self.assertEqual(result["error"], {"code": "sentence_length_out_of_range", "safe_details":
                {"len": 65, "min": 28 if call == 1 else 46, "max": 64 if call == 1 else 56}})
            self.assertEqual(self.blob(result, result["failed_raw_response"])["raw_text"], self.raws[-1])
            self.assertTrue(result["inputs_unchanged"])
            self.assertEqual(result["automatic_retries"], 0)
            self.assert_saved(result)

    def test_43_each_input_changed_during_second_response_retains_both_raws(self):
        for path in (PACKET_PATH, BUILD / "manifest.json", POLICY_PATH, BUILD / "review/sources.json", REAL_RUN / "summary.json"):
            with self.subTest(path=path.name, parent=path.parent == REAL_RUN):
                result = self.changed_on_response(path, 2)
                self.assert_source_failure(result, 2, parent_changed=path.parent == REAL_RUN)
                self.assertEqual(result["validated_stages"], 1)
                self.assertEqual(result["prior_error"]["code"], "source_changed")
                self.assertIsNone(result["pair"])

    def test_44_each_input_changed_on_first_response_blocks_second(self):
        for path in (PACKET_PATH, BUILD / "manifest.json", POLICY_PATH, BUILD / "review/sources.json", REAL_RUN / "summary.json"):
            with self.subTest(path=path.name, parent=path.parent == REAL_RUN):
                result = self.changed_on_response(path, 1)
                self.assert_source_failure(result, 1, parent_changed=path.parent == REAL_RUN)
                self.assertEqual(result["validated_stages"], 0)
                self.assertNotIn(delivery.MODES[1], result["additions"])

    def test_45_final_check_catches_sources_change_after_success_without_rollback(self):
        original_read, original_billing = Path.read_bytes, delivery._billing
        active = False
        def billing(ledger):
            nonlocal active
            value = original_billing(ledger)
            active = True
            return value
        def overlay(path):
            data = original_read(path)
            return data + b"\n" if active and path == BUILD / "review/sources.json" else data
        with mock.patch.object(delivery, "_billing", side_effect=billing), mock.patch.object(Path, "read_bytes", overlay):
            result = self.run_v2()
        self.assert_source_failure(result, 2)
        self.assertEqual(result["validated_stages"], 2)
        self.assertNotIn("prior_error", result)
        self.assertIsNotNone(result["pair"])
        self.assertEqual(set(result["payloads"]), {"I4", "I5"})
        self.assertEqual(self.blob(result, result["failed_raw_response"])["raw_text"], self.raws[1])

    def test_46_final_check_preserves_prior_errors_and_both_raws(self):
        original_read, original_billing = Path.read_bytes, delivery._billing
        for fault, code, path in (("json", "invalid_json", PACKET_PATH),
                                  ("schema", "invalid_schema", BUILD / "manifest.json"),
                                  ("length", "length_out_of_range", POLICY_PATH),
                                  ("json", "invalid_json", BUILD / "review/sources.json")):
            with self.subTest(fault=fault, path=path.name):
                active = False
                self.wires.clear()
                self.raws.clear()
                self.fault, self.fault_at = fault, 2
                def billing(ledger):
                    nonlocal active
                    value = original_billing(ledger)
                    active = True
                    return value
                def overlay(candidate):
                    data = original_read(candidate)
                    return data + b"\n" if active and candidate == path else data
                with mock.patch.object(delivery, "_billing", side_effect=billing), mock.patch.object(Path, "read_bytes", overlay):
                    result = self.run_fake()
                self.assert_source_failure(result, 2)
                self.assertEqual(result["prior_error"]["code"], code)
                self.assertEqual(result["validated_stages"], 1)
                self.assertEqual(self.blob(result, result["failed_raw_response"])["raw_text"], self.raws[1])

    def test_47_unreadable_input_on_last_response_is_source_changed(self):
        for path in (PACKET_PATH, BUILD / "manifest.json", POLICY_PATH, BUILD / "review/sources.json"):
            with self.subTest(path=path.name):
                result = self.changed_on_response(path, 2, unreadable=True)
                self.assert_source_failure(result, 2)

    def test_48_send_check_blocks_changes_after_first_validation(self):
        original_read, original_append = Path.read_bytes, delivery._DeliveryStore.append
        for path in (PACKET_PATH, BUILD / "manifest.json", POLICY_PATH, BUILD / "review/sources.json"):
            with self.subTest(path=path.name):
                active = False
                self.wires.clear()
                self.raws.clear()
                def append(store, event_type, **fields):
                    nonlocal active
                    event = original_append(store, event_type, **fields)
                    if event_type == "delivery_stage_validated":
                        active = True
                    return event
                def overlay(candidate):
                    data = original_read(candidate)
                    return data + b"\n" if active and candidate == path else data
                with mock.patch.object(delivery._DeliveryStore, "append", append), mock.patch.object(Path, "read_bytes", overlay):
                    result = self.run_fake()
                self.assert_source_failure(result, 1)
                self.assertEqual(result["validated_stages"], 1)
                self.assertNotIn(delivery.MODES[1], result["additions"])

    def test_49_observed_change_cannot_be_cleared_by_later_original_bytes(self):
        original_read = Path.read_bytes
        active = False
        observed = []
        def respond(request, timeout):
            nonlocal active
            response = self.respond(request, timeout)
            if len(self.wires) == 2:
                active = True
            return response
        def overlay(path):
            nonlocal active
            data = original_read(path)
            if active and path == BUILD / "review/sources.json":
                active = False
                observed.append(path)
                return data + b"\n"
            return data
        self.opener.open.side_effect = respond
        with mock.patch.object(Path, "read_bytes", overlay):
            result = self.run_fake()
        self.assert_source_failure(result, 2)
        self.assertEqual(len(observed), 1)

    def test_50_v2_budget_hash_detects_mutation_after_request(self):
        original_plan = delivery.plan_sentence_budget
        plans = []
        def plan(*args):
            budget = original_plan(*args)
            plans.append(budget)
            return budget
        def respond(request, timeout):
            response = self.respond(request, timeout)
            for slot in plans[0]["slots"]:
                slot["target_text_chars"] -= 1
            return response
        self.opener.open.side_effect = respond
        with mock.patch.object(delivery, "plan_sentence_budget", side_effect=plan):
            result = self.run_v2()
        self.assertEqual(result["error"]["code"], "invalid_sentence_budget")
        self.assertEqual(result["actual_http_attempts"], 1)
        self.assertEqual(result["validated_stages"], 0)
        self.assertEqual(self.blob(result, result["failed_raw_response"])["raw_text"], self.raws[0])
        self.assert_saved(result)

    def test_51_final_transport_error_keeps_http_raw_and_uncertain_charge(self):
        original_read, original_billing = Path.read_bytes, delivery._billing
        active = False
        self.fault, self.fault_at = "usage", 2
        def billing(ledger):
            nonlocal active
            value = original_billing(ledger)
            active = True
            return value
        def overlay(path):
            data = original_read(path)
            return data + b"\n" if active and path == BUILD / "review/sources.json" else data
        with mock.patch.object(delivery, "_billing", side_effect=billing), mock.patch.object(Path, "read_bytes", overlay):
            result = self.run_fake()
        self.assertEqual(result["failed_stage"], "source_integrity")
        self.assertEqual(result["error"]["code"], "source_changed")
        self.assertEqual(result["prior_error"]["code"], "missing_usage")
        self.assertTrue(result["internalreport_unchanged"])
        self.assertFalse(result["inputs_unchanged"])
        self.assertEqual(result["actual_http_attempts"], 2)
        self.assertEqual(result["completed_model_calls"], 1)
        self.assertTrue(result["cost"]["ledger_uncertain"])
        self.assertIsNone(result["cost"]["total_cost_cny"])
        self.assertEqual(Decimal(result["cost"]["known_cost_cny"]), Decimal("0.000612"))
        first = self.blob(result, result["additions"][delivery.MODES[0]]["raw_response"])
        second = self.blob(result, result["failed_raw_response"])
        self.assertEqual(first["raw_text"], self.raws[0])
        self.assertEqual(_parse_json(second["raw_http_text"])["choices"][0]["message"]["content"], self.raws[1])
        self.assert_saved(result)

    def test_52_preparation_recheck_blocks_changed_inputs_before_run_creation(self):
        original_read, original_audit = Path.read_bytes, delivery.audit_live_run
        before = sha(self.root / LEDGER_RELATIVE)
        for path in (PACKET_PATH, BUILD / "manifest.json", POLICY_PATH, BUILD / "review/sources.json"):
            with self.subTest(path=path.name):
                active = False
                def audit(*args):
                    nonlocal active
                    value = original_audit(*args)
                    active = True
                    return value
                def overlay(candidate):
                    data = original_read(candidate)
                    return data + b"\n" if active and candidate == path else data
                with mock.patch.object(delivery, "audit_live_run", side_effect=audit), mock.patch.object(Path, "read_bytes", overlay):
                    result = self.run_fake()
                self.assertEqual(result["error"]["code"], "source_changed")
                self.assertFalse(result["inputs_unchanged"])
                self.assertTrue(result["internalreport_unchanged"])
                self.assertIsNone(result["run_directory"])
                self.assertEqual(result["actual_http_attempts"], 0)
        self.assertEqual(sha(self.root / LEDGER_RELATIVE), before)
        self.build_opener.assert_not_called()

    def test_53_v3_real_policy_byte_hash_and_explicit_string_schema_prompts(self):
        digest = sha(POLICY_V3_PATH)
        policy = delivery.load_delivery_policy(POLICY_V3_PATH, digest)
        self.assertEqual(policy, read_json(POLICY_V3_PATH))
        self.assertEqual(policy["version"], "report_to_delivery_v3")
        for prompt in (policy["common_prompt"], policy["mode_prompts"]["plain_restatement"]):
            self.assertIn('"schema_version":"1.0"', prompt)
        self.assert_code("policy_hash_mismatch", lambda: delivery.load_delivery_policy(POLICY_V3_PATH, "0" * 64))
        path = self.root / "policy_v3_whitespace.json"
        path.write_bytes(POLICY_V3_PATH.read_bytes() + b"\n")
        self.assertEqual(read_json(path), policy)
        self.assert_code("policy_hash_mismatch", lambda: delivery.load_delivery_policy(path, digest))
        self.assertEqual(delivery.load_delivery_policy(path, sha(path)), policy)

    def test_54_v3_numeric_policy_values_equal_v2_and_cannot_change(self):
        v2 = delivery.load_delivery_policy(POLICY_V2_PATH, sha(POLICY_V2_PATH))
        v3 = delivery.load_delivery_policy(POLICY_V3_PATH, sha(POLICY_V3_PATH))
        numbers = {key: (type(value), value) for key, value in v2.items() if type(value) in (int, float)}
        self.assertEqual({key: (type(value), value) for key, value in v3.items()
                          if type(value) in (int, float)}, numbers)
        self.assertEqual(delivery._POLICY_FIXED_V3,
                         {**delivery._POLICY_FIXED_V2, "version": "report_to_delivery_v3"})
        path = self.root / "policy_v3_numeric.json"
        for key, (kind, value) in numbers.items():
            step = 1 if kind is int else 0.01
            for changed in (value - step, value + step):
                with self.subTest(field=key, value=changed):
                    path.write_text(_json_text({**v3, key: changed}), encoding="utf-8")
                    self.assert_code("invalid_policy", lambda: delivery.load_delivery_policy(path, sha(path)))

    def test_55_v3_two_fake_calls_keep_v2_budgets_and_exact_context_isolation(self):
        self.size = 224
        baseline = self.run_v2()
        self.assertEqual(baseline["status"], "generated_structurally_valid", baseline.get("error"))
        v2_users = [_parse_json(wire["messages"][1]["content"]) for wire in self.wires]
        self.wires.clear()
        self.raws.clear()
        self.opener.reset_mock()
        digest = sha(POLICY_V3_PATH)
        result = self.run_fake(policy_path=POLICY_V3_PATH, expected_policy_sha=digest)
        policy = read_json(POLICY_V3_PATH)
        self.assertEqual(result["status"], "generated_structurally_valid", result.get("error"))
        self.assertEqual(result["policy_version"], "report_to_delivery_v3")
        for count in ("actual_http_attempts", "requests_started", "completed_model_calls", "validated_stages"):
            self.assertEqual(result[count], 2)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(len(self.wires), 2)
        self.assertTrue(result["expected_counts_passed"])
        self.assertTrue(all(result["expected_assembly"].values()))
        self.assertTrue(result["inputs_unchanged"])
        self.assertTrue(result["internalreport_unchanged"])
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(result["policy_input"], {"source_path": str(POLICY_V3_PATH.resolve()), "file_sha256": digest})
        self.assertEqual(self.blob(result, result["inputs"]["policy"]), policy)
        for number, mode in enumerate(delivery.MODES):
            row = result["additions"][mode]
            request = self.blob(result, row["request"])
            self.assertEqual(self.wires[number]["messages"], request["messages"])
            self.assertEqual([message["role"] for message in request["messages"]], ["system", "user"])
            self.assertEqual(request["messages"][0]["content"], policy["common_prompt"] + "\n" + policy["mode_prompts"][mode])
            content = request["messages"][1]["content"]
            user = _parse_json(content)
            bounds = {"min": 143, "max": 343} if number == 0 else {"min": 204, "max": 246}
            target = 235 if number == 0 else 224
            budget = delivery.plan_sentence_budget(self.packet, bounds, target)
            expected = {"frozen_evidence_packet": self.packet, "mode": mode,
                        "additional_char_range": bounds, "target_chars": target, "sentence_budget": budget}
            if number == 0:
                expected["internal_report"] = self.blob(result, result["inputs"]["internal_report"])
            else:
                self.assertNotIn("internal_report", content)
                self.assertNotIn(self.raws[0], content)
                self.assertNotIn(result["additions"][delivery.MODES[0]]["rendered"]["additional_text"], content)
            self.assertEqual(user, expected)
            self.assertEqual(user, v2_users[number])
            self.assertEqual(row["policy_version"], "report_to_delivery_v3")
            self.assertEqual(row["sentence_budget"], budget)
            self.assertEqual(row["sentence_budget_sha256"], canonical_hash(budget))
            self.assertEqual(row["rendered"]["char_count"], 224)
            self.assertEqual(row["rendered"]["structured_output"], fixture_v2(self.packet, mode, 224))
            self.assertEqual(self.wires[number]["max_completion_tokens"], 1024)
            self.assertEqual(self.blob(result, row["response"])["raw_text"], self.raws[number])
        self.assert_saved(result)

    def test_56_v3_numeric_schema_rejected_with_otherwise_valid_output(self):
        self.fault = "numeric_schema"
        for call in (1, 2):
            with self.subTest(call=call):
                self.wires.clear()
                self.raws.clear()
                self.fault_at = call
                result = self.run_fake(policy_path=POLICY_V3_PATH, expected_policy_sha=sha(POLICY_V3_PATH))
                mode = delivery.MODES[call - 1]
                row = result["additions"][mode]
                valid = fixture_v2(self.packet, mode)
                self.assertEqual(self.render_v2(valid, mode=mode, bounds=row["additional_char_range"],
                                               budget=row["sentence_budget"])["structured_output"], valid)
                raw = self.blob(result, result["failed_raw_response"])["raw_text"]
                self.assertEqual(raw, self.raws[-1])
                self.assertEqual(_parse_json(raw), {**valid, "schema_version": 1.0})
                self.assertIn('"schema_version":1.0', raw)
                self.assertEqual(result["status"], "delivery_failed")
                self.assertEqual(result["failed_stage"], delivery.STAGES[call - 1])
                self.assertEqual(result["error"], {"code": "invalid_schema", "safe_details": {}})
                self.assertEqual(result["actual_http_attempts"], call)
                self.assertEqual(len(self.wires), call)
                self.assertEqual(result["validated_stages"], call - 1)
                self.assertEqual(result["automatic_retries"], 0)
                self.assertIsNone(row["rendered"])
                self.assertIsNone(result["pair"])
                self.assertTrue(result["inputs_unchanged"])
                self.assert_saved(result)

    def test_57_v3_slot_minimum_rejected_even_when_whole_length_valid(self):
        self.fault = "sentence_min"
        for call in (1, 2):
            with self.subTest(call=call):
                self.wires.clear()
                self.raws.clear()
                self.fault_at = call
                result = self.run_fake(policy_path=POLICY_V3_PATH, expected_policy_sha=sha(POLICY_V3_PATH))
                mode = delivery.MODES[call - 1]
                row = result["additions"][mode]
                valid = fixture_v2(self.packet, mode)
                self.assertEqual(self.render_v2(valid, mode=mode, bounds=row["additional_char_range"],
                                               budget=row["sentence_budget"])["structured_output"], valid)
                low, high = (28, 64) if call == 1 else (46, 56)
                expected = deepcopy(valid)
                expected["sentences"][0]["text"] = "甲" * (low - 2) + "。"
                raw = self.blob(result, result["failed_raw_response"])["raw_text"]
                self.assertEqual(raw, self.raws[-1])
                output = _parse_json(raw)
                self.assertEqual(output, expected)
                whole = self.render(output, mode=mode, bounds=row["additional_char_range"])
                self.assertEqual(whole["structured_output"], expected)
                self.assertEqual(whole["char_count"], 211 if call == 1 else 229)
                self.assertEqual(result["status"], "delivery_failed")
                self.assertEqual(result["failed_stage"], delivery.STAGES[call - 1])
                self.assertEqual(result["error"], {"code": "sentence_length_out_of_range", "safe_details":
                    {"len": low - 1, "min": low, "max": high}})
                self.assertEqual(result["actual_http_attempts"], call)
                self.assertEqual(len(self.wires), call)
                self.assertEqual(result["validated_stages"], call - 1)
                self.assertEqual(result["automatic_retries"], 0)
                self.assertIsNone(row["rendered"])
                self.assertIsNone(result["pair"])
                self.assertTrue(result["inputs_unchanged"])
                self.assert_saved(result)

    def test_58_frozen_v1_v2_policy_bytes_and_hashes_unchanged_after_v3_tests(self):
        for path in (POLICY_PATH, POLICY_V2_PATH):
            with self.subTest(policy=path.name):
                self.assertEqual(path.read_bytes(), self.frozen_policy_bytes[str(path)])
                self.assertEqual(sha(path), self.before[str(path)])
        self.assertIn(POLICY_V3_PATH, self.protected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
