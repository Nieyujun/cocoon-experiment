"""真实记录只读复核与离线失败前缀测试；不证明网络真实性或账单。

只使用隔离环境中的假凭据和模拟 HTTP，传输、控制器、事件库使用原实现。
生成物永保留于 tests/live_audit_offline_only_*；篡改仅存在于内存读取视图。
不修改生产文件、不生成镜像、不核算费用、不继承或重复运行其他测试套件。
"""
import sys

sys.dont_write_bytecode = True

from contextlib import ExitStack, contextmanager
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
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REAL_RUN = ROOT / "experiment/mimo_probes/assistant_probe_20261108T082120_9ad78cf8"
EVIDENCE_BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
PACKET_PATH = EVIDENCE_BUILD / "packets/TOPIC_01_round_1.json"
BUNDLE_PATH = ROOT / "configs/assistant_prompts_v1.json"
FAKE_KEY = "sk-offline-audit-test"
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
UNIT_USAGE = {
    "prompt_tokens": 1000, "completion_tokens": 80, "total_tokens": 1080,
    "prompt_tokens_details": {"cached_tokens": 600},
    "completion_tokens_details": {"reasoning_tokens": 8},
    "web_search_usage": {"tool_usage": 0, "page_usage": 0},
}


@contextmanager
def offline_scope():
    with ExitStack() as stack:
        # 不让 patch.dict 复制原环境，更不枚举或读取真实凭据。
        stack.enter_context(mock.patch.object(os, "environ", {}))
        stack.enter_context(mock.patch.dict(os.environ, {"MIMO_API_KEY": FAKE_KEY}))
        targets = (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.socket.sendall", "socket.create_connection", "socket.getaddrinfo",
            "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
            "urllib.request.HTTPHandler.http_open", "urllib.request.HTTPSHandler.https_open",
        )
        guards = [stack.enter_context(mock.patch(
            target, side_effect=AssertionError("本测试禁止网络访问")
        )) for target in targets]
        try:
            yield
        finally:
            for guard in guards:
                guard.assert_not_called()


with offline_scope():
    import cocoon.assistant as assistant_module
    import cocoon.live_audit as audit_module
    import cocoon.mimo as mimo_module
    import cocoon.mimo_probe as probe_module
    from cocoon.budget import BudgetLedger
    from cocoon.contracts import output_contract, validate_packet
    from cocoon.dose_build import source_index_from_review
    from cocoon.dosage import RESPONSE_PROMPT
    from cocoon.mimo import MiMoTransport as OriginalMiMoTransport
    from cocoon.prompts import STAGES
    from cocoon.runtime import EventStore, canonical_hash, verify_events

FOUR_STAGES = tuple(STAGES[:4])


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_json(path):
    return json.loads(Path(path).read_bytes())


def events_at(root):
    return [json.loads(line) for line in (root / "events/events.jsonl").read_bytes().splitlines()]


def run_hashes(root):
    paths = [root / "summary.json", root / "events/events.jsonl"]
    paths.extend(sorted((root / "raw/blobs").glob("*.json")))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


@contextmanager
def read_overlay(files):
    """替换文件读取，不替换 verify_events/_verify_blob 的验证逻辑。"""
    files = {Path(path).resolve(): data for path, data in files.items()}
    original_open, original_exists = Path.open, Path.exists
    original_is_file, original_iterdir = Path.is_file, Path.iterdir

    def opened(path, mode="r", *args, **kwargs):
        data = files.get(path.resolve())
        if data is not None:
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("内存篡改视图只允许读取")
            if "b" in mode:
                return io.BytesIO(data)
            encoding = kwargs.get("encoding") or (args[1] if len(args) > 1 else "utf-8")
            return io.StringIO(data.decode(encoding))
        return original_open(path, mode, *args, **kwargs)

    def exists(path, *args, **kwargs):
        return path.resolve() in files or original_exists(path, *args, **kwargs)

    def is_file(path, *args, **kwargs):
        return path.resolve() in files or original_is_file(path, *args, **kwargs)

    def iterdir(path):
        children = list(original_iterdir(path))
        known = {child.resolve() for child in children}
        children.extend(target for target in files
                        if target.parent == path.resolve() and target not in known)
        return iter(children)

    with mock.patch.object(Path, "open", opened), mock.patch.object(Path, "exists", exists), \
            mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "iterdir", iterdir):
        yield


class OfflineResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.status = 200
        self.headers = {"x-request-id": "offline-audit-only"}


class LiveAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with offline_scope():
            cls.config = read_json(ROOT / "configs/core.json")
            cls.bundle_sha = cls.config["assistant"]["prompt_bundle_sha256"]
            cls.packet = validate_packet(read_json(PACKET_PATH), allow_demo=False)
            cls.source_index = source_index_from_review(EVIDENCE_BUILD)
            cls.summary = read_json(REAL_RUN / "summary.json")
            cls.events = events_at(REAL_RUN)
            cls.protected_paths = [ROOT / "configs/core.json", BUNDLE_PATH, PACKET_PATH,
                                   EVIDENCE_BUILD / "manifest.json",
                                   EVIDENCE_BUILD / "review/sources.json",
                                   ROOT / "tests/test_mimo_integration.py"]
            cls.protected_paths.extend(sorted((ROOT / "cocoon").glob("*.py")))
            cls.protected_paths.extend(Path(cls.config["assistant"][name])
                                       for name in ("entry_path", "source_path"))
            cls.protected_before = cls.protected_hashes()
        cls.addClassCleanup(cls.assert_protected_unchanged)

    @classmethod
    def protected_hashes(cls):
        hashes = run_hashes(REAL_RUN)
        hashes.update({str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in cls.protected_paths})
        return hashes

    @classmethod
    def assert_protected_unchanged(cls):
        if cls.protected_hashes() != cls.protected_before:
            raise AssertionError("真实日志、冻结输入或生产文件发生变化")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline_scope())
        self.temp_roots = []
        self.wires = []
        self.responses = {}
        self.fault = None
        self.fault_stage = "label"
        self.opener = mock.Mock()
        self.opener.open.side_effect = self.respond
        self.build = self.stack.enter_context(mock.patch(
            "urllib.request.build_opener", return_value=self.opener))
        original_open = Path.open

        def guarded_open(path, mode="r", *args, **kwargs):
            resolved = path.resolve()
            if path.suffix.lower() == ".md" or path.name.lower().startswith(".env"):
                raise AssertionError("测试不读取文档或凭据文件")
            if any(flag in mode for flag in "wax+"):
                self.assertTrue(any(resolved.is_relative_to(root) for root in self.temp_roots),
                                "写入必须局限于本测试新建的临时目录")
            return original_open(path, mode, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(Path, "open", guarded_open))

    def tearDown(self):
        for root in self.temp_roots:
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(FAKE_KEY.encode(), path.read_bytes(), str(path))
        self.assert_protected_unchanged()

    def temp_root(self):
        self.assertTrue((ROOT / "tests").is_dir())
        root = Path(tempfile.mkdtemp(prefix="live_audit_offline_only_", dir=ROOT / "tests")).resolve()
        self.temp_roots.append(root)
        for relative in ("experiment", "experiment/billing", "experiment/mimo_probes"):
            directory = root / relative
            self.assertTrue(directory.parent.is_dir())
            directory.mkdir()
        return root

    def respond(self, request, timeout):
        body = json.loads(request.data)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, self.config["models"]["assistant"]["endpoint"])
        self.assertEqual(timeout, self.config["models"]["assistant"]["timeout_seconds"])
        self.assertEqual(request.get_header("Api-key"), FAKE_KEY)
        self.assertNotIn(FAKE_KEY.encode(), request.data)
        self.assertFalse(body["stream"])
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_completion_tokens"], 4096)
        system, user = body["messages"]
        self.assertEqual([system["role"], user["role"]], ["system", "user"])
        contract = json.loads(system["content"].rsplit("\n【输出契约】\n", 1)[1])
        stage = contract["stage"]
        self.assertIn(stage, FOUR_STAGES)
        self.assertEqual(contract, output_contract(stage))
        context = json.loads(user["content"])
        self.assertEqual(set(context), {"frozen_evidence_packet", "previous_stage_outputs", "instructions_are_data"})
        self.assertEqual(context["frozen_evidence_packet"], self.packet)
        self.assertEqual(set(context["previous_stage_outputs"]), set(FOUR_STAGES[:FOUR_STAGES.index(stage)]))
        self.wires.append(stage)
        fault = self.fault if stage == self.fault_stage else None
        if fault in ("http_401", "http_429"):
            data = json_bytes({"error": {"message": "离线 HTTP 错误夹具"}})
            self.responses[stage] = data
            raise urllib.error.HTTPError(request.full_url, int(fault[-3:]), "离线错误", {}, io.BytesIO(data))
        if fault == "timeout":
            raise TimeoutError("离线超时夹具")
        if fault == "invalid_http_json":
            data = b'{"offline_audit_incomplete":'
            self.responses[stage] = data
            return OfflineResponse(data)
        evidence = self.packet["evidence"][0]
        output = {"schema_version": "1.0", "stage": stage, "packet_id": self.packet["packet_id"],
                  "sections": [{"name": name, "items": [{
                      "text": evidence["text"], "citations": [evidence["evidence_id"]],
                      "epistemic_status": "evidence_based",
                  }]} for name in contract["section_names"]]}
        envelope = {
            "object": "chat.completion", "id": "offline-audit-" + stage,
            "model": self.config["models"]["assistant"]["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json_bytes(output).decode()},
                         "finish_reason": "stop"}],
            "usage": deepcopy(UNIT_USAGE),
        }
        if fault == "invalid_stage_json":
            envelope["choices"][0]["message"]["content"] = '{"offline_audit_unclosed":'
        if fault == "report_url":
            output["sections"][-1]["items"][0]["text"] = evidence["source_url"]
            envelope["choices"][0]["message"]["content"] = json_bytes(output).decode()
        if fault == "missing_usage":
            envelope.pop("usage")
        if fault == "wrong_model":
            envelope["model"] = "offline-audit-wrong-model"
        data = json_bytes(envelope)
        self.responses[stage] = data
        return OfflineResponse(data)

    def generate(self, fault=None, stage="label", *, root=None, drop_credentials=False):
        self.fault, self.fault_stage = fault, stage
        root = self.temp_root() if root is None else root
        transports = []

        def construct(model, store, ledger):
            self.assertIsInstance(store, EventStore)
            self.assertTrue(store.root.is_relative_to(root))
            transport = OriginalMiMoTransport(model, store, ledger)
            transports.append(transport)
            if drop_credentials:
                # 模拟状态检查通过后、助手 preflight 前凭据消失；不跳过任何验证。
                os.environ.clear()
            return transport

        with mock.patch.object(probe_module, "MiMoTransport", side_effect=construct):
            result = probe_module.run_probe(deepcopy(self.config), root, execute=True, packet_path=PACKET_PATH)
        self.assertTrue(result["executed"])
        self.assertEqual(len(transports), 1)
        self.assertIs(type(transports[0]), OriginalMiMoTransport)
        self.assertEqual(transports[0].actual_model_calls, self.opener.open.call_count)
        run = Path(result["run_directory"])
        self.assertTrue(run.is_relative_to(root))
        self.assertEqual(result["verification"], verify_events(run))
        self.assertEqual(read_json(run / "summary.json"), result)
        return result, run

    def audit(self, run=REAL_RUN, *, bundle_sha=None, packet_path=PACKET_PATH):
        before = run_hashes(run)
        index = deepcopy(self.source_index)
        guards = []
        try:
            with ExitStack() as stack:
                for target, name in (
                    (mimo_module, "load_key"), (mimo_module, "credential_status"),
                    (assistant_module, "load_key"), (probe_module, "load_key"),
                    (probe_module, "credential_status"), (OriginalMiMoTransport, "complete"),
                    (EventStore, "capture"), (EventStore, "append"),
                ):
                    guards.append(stack.enter_context(mock.patch.object(
                        target, name, side_effect=AssertionError("audit 必须只读且不能读取凭据或调用传输"))))
                guards.append(stack.enter_context(mock.patch(
                    "urllib.request.build_opener", side_effect=AssertionError("audit 不得构建网络传输"))))
                return audit_module.audit_live_run(run, packet_path, BUNDLE_PATH,
                                                  self.bundle_sha if bundle_sha is None else bundle_sha, index)
        finally:
            for guard in guards:
                guard.assert_not_called()
            self.assertEqual(index, self.source_index)
            self.assertEqual(run_hashes(run), before, "审核不得修改任何日志或 blob")

    def assert_failed_prefix(self, result, run, *, stage, calls, completed=(), code="mimo_error", upstream=None):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        if upstream is not None:
            self.assertEqual(result["error"]["upstream_code"], upstream)
        self.assertEqual(result["failed_stage"], stage)
        self.assertEqual(result["actual_model_calls"], calls)
        self.assertEqual(self.opener.open.call_count, calls)
        self.assertEqual(self.build.call_count, calls)
        self.assertEqual(self.wires, list(FOUR_STAGES[:calls]))
        self.assertEqual(result["completed_stages"], list(completed))
        self.assertEqual(result["fixture_calls"], 0)
        self.assertEqual(result["external_retrieval_calls"], 0)
        self.assertFalse(result["demo_fallback"])
        self.assertIsNone(result["report"])
        self.assertIsNone(result["mirror"])
        events = events_at(run)
        self.assertEqual([event["event_type"] for event in events[-2:]],
                         ["assistant_task_failed", "probe_failed"])
        self.assertNotIn("assistant_task_completed", [event["event_type"] for event in events])
        self.assertEqual([event["phase"] for event in events if event["event_type"] == "assistant_stage_validated"],
                         list(completed))
        audited = self.audit(run)
        self.assertEqual((audited["audit_status"], audited["run_status"]), ("passed", "failed"))
        self.assertEqual(audited["failure_info"]["terminal_error"], result["error"])
        self.assertEqual(audited["failure_info"]["failed_stage"], stage)
        self.assertEqual(audited["completed_stages"], list(completed))
        self.assertEqual(audited["actual_model_calls"], calls)
        self.assertFalse(audited["four_stage_structure_passed"])
        self.assertIsNone(audited["rendered_report"])
        self.assertIsNone(audited["candidate_visible_text"])
        self.assertEqual(audited["rendered_items"], [])
        self.assertFalse(audited["mirror_executed"])
        self.assertFalse(audited["i5_output_available"])
        for name, row in audited["stage_results"].items():
            expected = "validated" if name in completed else "failed" if name == stage else "not_attempted"
            self.assertEqual(row["status"], expected)
        return audited

    def reseal(self, events, summary):
        """仅在内存重做事件链及 payload blob；原验证器仍检查每一个哈希。"""
        files = {}
        previous = None
        for sequence, event in enumerate(events, 1):
            event["logical_seq"] = sequence
            event["previous_event_hash"] = previous
            payload = event["payload"]
            digest = canonical_hash(payload)
            files[REAL_RUN / f"raw/blobs/{digest}.json"] = json_bytes(payload)
            if event["event_type"] in ("assistant_task_completed", "probe_completed", "mimo_request_completed"):
                event["output_ref"] = f"raw/blobs/{digest}.json"
                event["output_sha256"] = digest
            event.pop("event_hash", None)
            event["event_hash"] = canonical_hash(event)
            previous = event["event_hash"]
        blob_paths = {path.resolve() for path in (REAL_RUN / "raw/blobs").glob("*.json")}
        blob_paths.update(path.resolve() for path in files)
        summary["verification"] = {"event_count": len(events), "blob_count": len(blob_paths), "chain_valid": True}
        files[REAL_RUN / "events/events.jsonl"] = b"".join(json_bytes(event) + b"\n" for event in events)
        files[REAL_RUN / "summary.json"] = json_bytes(summary)
        return files

    def assert_rejected(self, files, code, *, hash_valid=False):
        before = run_hashes(REAL_RUN)
        with read_overlay(files):
            if hash_valid:
                self.assertEqual(verify_events(REAL_RUN), read_json(REAL_RUN / "summary.json")["verification"])
            else:
                with self.assertRaises(ValueError):
                    verify_events(REAL_RUN)
            with self.assertRaisesRegex(ValueError, "^live_audit:" + code + "$"):
                self.audit()
        self.assertEqual(run_hashes(REAL_RUN), before)

    def test_real_four_stages_and_all_log_hashes_unchanged(self):
        result = self.audit()
        self.assertEqual((result["audit_status"], result["run_status"]), ("passed", "succeeded"))
        self.assertTrue(result["four_stage_structure_passed"])
        self.assertTrue(result["preflight_passed"])
        self.assertEqual(result["completed_stages"], list(FOUR_STAGES))
        self.assertEqual(list(result["stage_results"]), list(FOUR_STAGES))
        for name in ("actual_model_calls", "actual_http_attempts", "prepared_requests", "completed_model_calls"):
            self.assertEqual(result[name], 4)
        self.assertEqual(result["verification"], verify_events(REAL_RUN))
        self.assertEqual(result["source_event_ids"], [event["event_id"] for event in self.events])
        self.assertEqual(result["terminal_event_hash"], self.events[-1]["event_hash"])
        self.assertEqual(result["prompt_bundle_sha256"], self.bundle_sha)
        self.assertEqual(result["expected_packet_hash"], canonical_hash(self.packet))
        self.assertEqual(result["summary_sha256"], run_hashes(REAL_RUN)[str(REAL_RUN / "summary.json")])

    def test_real_http_content_and_input_output_total_usage_preserved(self):
        result = self.audit()
        totals = dict.fromkeys(TOKEN_KEYS, 0)
        for stage in FOUR_STAGES:
            row = result["stage_results"][stage]
            self.assertEqual(row["status"], "validated")
            self.assertTrue(row["structure_passed"])
            event = next(event for event in self.events if event["event_type"] == "mimo_response_received"
                         and event["task_id"] == row["metadata"]["request_id"])
            raw = read_json(REAL_RUN / event["output_ref"])["raw_http_text"]
            envelope = json.loads(raw)
            self.assertEqual(row["raw_text"], envelope["choices"][0]["message"]["content"])
            self.assertEqual(row["raw_text"], self.summary["stage_results"][stage]["raw_text"])
            self.assertEqual(row["structured_output"], json.loads(row["raw_text"]))
            self.assertEqual(row["http"]["response_bytes"], len(raw.encode()))
            self.assertEqual(row["http"]["response_sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertTrue(row["http"]["body_available"])
            self.assertEqual(row["usage"], {key: envelope["usage"][key] for key in TOKEN_KEYS})
            self.assertEqual(row["usage"]["total_tokens"], row["usage"]["prompt_tokens"] + row["usage"]["completion_tokens"])
            for key in TOKEN_KEYS:
                totals[key] += envelope["usage"][key]
        self.assertEqual(totals, {"prompt_tokens": 15729, "completion_tokens": 3387, "total_tokens": 19116})
        self.assertEqual(result["reported_usage"], totals)
        self.assertEqual(result["known_reported_usage"], totals)
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["usage_observed_attempts"], 4)
        self.assertEqual(result["usage_missing_attempts"], 0)

    def test_real_rendered_items_full_urls_and_2051_characters_over_limit(self):
        result = self.audit()
        expected = [{"section": section["name"], "item_index": index, **deepcopy(item)}
                    for section in self.summary["report"]["sections"]
                    for index, item in enumerate(section["items"])]
        self.assertEqual(result["rendered_items"], expected)
        for item in expected:
            self.assertIn(item["text"], result["rendered_report"])
        original_text = "\n".join(item["text"] for item in expected)
        found_url = False
        for url in {item["source_url"] for item in self.packet["evidence"]}:
            if url in original_text:
                found_url = True
                self.assertEqual(result["rendered_report"].count(url), original_text.count(url))
                self.assertIn(url, result["candidate_visible_text"])
        # 真实模型本轮未必输出完整URL；不凭预期捏造它，应逐字保留实际内容。
        self.assertEqual(found_url, any(entry["source_url"] in original_text for entry in self.packet["evidence"]))
        candidate = "阅读材料\n" + result["evidence_block"] + "\n\n阅读说明\n" + result["rendered_report"] + "\n\n" + RESPONSE_PROMPT
        self.assertEqual(result["candidate_visible_text"], candidate)
        self.assertEqual(result["candidate_visible_chars"], len(candidate))
        self.assertEqual(len(candidate), 2051)
        self.assertGreater(len(candidate), 800)
        self.assertEqual(result["candidate_visible_sha256"], hashlib.sha256(candidate.encode()).hexdigest())
        self.assertEqual(result["report_chars"], len(result["rendered_report"]))
        self.assertEqual(result["dose"]["status"], "out_of_range")
        self.assertFalse(result["dose"]["dose_within_range"])
        self.assertIn({"code": "dose_out_of_range", "char_count": len(candidate), "min_chars": 600, "max_chars": 800}, result["deviations"])
        self.assertTrue(result["report_text_unchanged"])
        self.assertFalse(result["report_urls_removed"])
        self.assertFalse(result["automatic_compression"])

    def test_real_no_mirror_i5_network_or_experiment_approval(self):
        result = self.audit()
        for name in ("mirror_executed", "i5_output_available", "full_I4_eligible", "natural_view_used",
                     "network_called_by_audit", "formal_eligible", "approved_for_experiment"):
            self.assertFalse(result[name])
        self.assertEqual(result["model_calls_this_audit"], 0)
        self.assertEqual(result["human_reviews_completed"], 0)
        self.assertEqual(result["response_opportunities"], 1)
        self.assertEqual(result["semantic_review"], "pending")
        self.assertNotIn("mirror", result["stage_results"])
        self.assertNotRegex(result["candidate_visible_text"], r"(?i)I5")
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_offline_success_uses_original_transport_event_store_and_usage_details(self):
        summary, run = self.generate()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(self.wires, list(FOUR_STAGES))
        self.assertEqual(self.opener.open.call_count, 4)
        result = self.audit(run)
        self.assertEqual(result["audit_status"], "passed")
        self.assertEqual(result["reported_usage"], {key: UNIT_USAGE[key] * 4 for key in TOKEN_KEYS})
        for row in result["stage_results"].values():
            self.assertEqual(row["usage_details"]["prompt_tokens_details"], {"cached_tokens": 600})
            self.assertEqual(row["usage_details"]["completion_tokens_details"], {"reasoning_tokens": 8})
        self.assertFalse(result["mirror_executed"])

    def test_recorded_report_url_remains_in_candidate_message(self):
        summary, run = self.generate("report_url", "integrate")
        audited = self.audit(run)
        url = self.packet["evidence"][0]["source_url"]
        self.assertIn(url, summary["stage_results"]["integrate"]["raw_text"])
        self.assertIn(url, audited["rendered_report"])
        self.assertIn(url, audited["candidate_visible_text"])
        self.assertFalse(audited["report_urls_removed"])

    def test_http_401_failed_first_stage_audits_without_retry(self):
        result, run = self.generate("http_401", "search")
        audited = self.assert_failed_prefix(result, run, stage="search", calls=1, upstream="auth_failed")
        self.assertEqual(audited["stage_results"]["search"]["http"]["status"], 401)
        self.assertEqual(audited["usage_missing_attempts"], 1)
        self.assertIsNone(audited["reported_usage"])

    def test_http_429_preserves_successful_prefix_without_retry(self):
        result, run = self.generate("http_429")
        audited = self.assert_failed_prefix(result, run, stage="label", calls=2, completed=("search",), upstream="rate_limited")
        self.assertEqual(audited["stage_results"]["label"]["http"]["status"], 429)
        self.assertEqual(audited["known_reported_usage"], {key: UNIT_USAGE[key] for key in TOKEN_KEYS})
        self.assertFalse(audited["usage_complete"])

    def test_timeout_preserves_two_stage_prefix_without_retry(self):
        result, run = self.generate("timeout", "blindspot")
        audited = self.assert_failed_prefix(result, run, stage="blindspot", calls=3,
                                            completed=FOUR_STAGES[:2], upstream="timeout")
        self.assertIsNone(audited["stage_results"]["blindspot"]["http"])
        self.assertEqual(audited["completed_model_calls"], 2)
        self.assertEqual(audited["known_reported_usage"], {key: UNIT_USAGE[key] * 2 for key in TOKEN_KEYS})

    def test_invalid_http_json_keeps_digest_and_interrupted_prefix(self):
        result, run = self.generate("invalid_http_json")
        event = next(event for event in events_at(run) if event["event_type"] == "mimo_response_received"
                     and ":label:" in event["task_id"])
        captured = read_json(run / event["output_ref"])
        self.assertNotIn("raw_http_text", captured)
        self.assertTrue(captured["omitted_due_to_incomplete_security_scan"])
        self.assertEqual(captured["response_sha256"], hashlib.sha256(self.responses["label"]).hexdigest())
        self.assertEqual(captured["response_bytes"], len(self.responses["label"]))
        audited = self.assert_failed_prefix(result, run, stage="label", calls=2, completed=("search",), upstream="invalid_json")
        self.assertFalse(audited["stage_results"]["label"]["http"]["body_available"])
        self.assertEqual(audited["failure_info"]["terminal_error"]["upstream_code"], "invalid_json")

    def test_invalid_stage_json_includes_failed_stage_input_and_output_tokens(self):
        result, run = self.generate("invalid_stage_json")
        audited = self.assert_failed_prefix(result, run, stage="label", calls=2, completed=("search",), code="invalid_output")
        row = audited["stage_results"]["label"]
        self.assertEqual(row["raw_text"], '{"offline_audit_unclosed":')
        self.assertFalse(row["structure_passed"])
        self.assertEqual(row["usage"], {key: UNIT_USAGE[key] for key in TOKEN_KEYS})
        self.assertEqual(audited["reported_usage"], {key: UNIT_USAGE[key] * 2 for key in TOKEN_KEYS})
        self.assertTrue(audited["usage_complete"])
        self.assertEqual(audited["completed_model_calls"], 2)

    def test_missing_usage_keeps_reservation_and_audits_failed_prefix(self):
        result, run = self.generate("missing_usage")
        billing = result["billing_cumulative"]
        self.assertEqual(billing["unresolved_request_count"], 1)
        self.assertGreater(Decimal(billing["reserved_cny"]), 0)
        self.assertIsNone(billing["total_cost_cny"])
        audited = self.assert_failed_prefix(result, run, stage="label", calls=2, completed=("search",), upstream="missing_usage")
        self.assertEqual(audited["stage_results"]["label"]["usage_error_code"], "missing_usage")
        self.assertIsNone(audited["stage_results"]["label"]["usage"])
        self.assertEqual(audited["known_reported_usage"], {key: UNIT_USAGE[key] for key in TOKEN_KEYS})
        self.assertIsNone(audited["reported_usage"])
        self.assertEqual(audited["usage_missing_attempts"], 1)

    def test_wrong_model_still_includes_usage_from_failed_http_stage(self):
        result, run = self.generate("wrong_model")
        audited = self.assert_failed_prefix(result, run, stage="label", calls=2, completed=("search",), upstream="model_mismatch")
        self.assertIsNone(audited["stage_results"]["label"]["raw_text"])
        self.assertEqual(audited["completed_model_calls"], 1)
        self.assertEqual(audited["reported_usage"], {key: UNIT_USAGE[key] * 2 for key in TOKEN_KEYS})
        self.assertTrue(audited["usage_complete"])

    def test_budget_blocks_after_prepared_with_zero_http_calls(self):
        root = self.temp_root()
        ledger = BudgetLedger(root / probe_module.LEDGER_RELATIVE, probe_module.APPROVED_LIMIT)
        # 仅种入测试历史，让状态冒烟额度足够、正式请求预留不足；不模拟 reserve。
        for index in range(4):
            request_id = "offline-audit-history-" + str(index)
            ledger.reserve(request_id, 4096)
            ledger.settle(request_id, {"prompt_tokens": 987500, "completion_tokens": 0,
                                       "total_tokens": 987500, "prompt_tokens_details": {"cached_tokens": 0}})
        original = (root / probe_module.LEDGER_RELATIVE).read_bytes()
        result, run = self.generate(root=root)
        audited = self.assert_failed_prefix(result, run, stage="search", calls=0,
                                            code="budget_error", upstream="budget_exceeded")
        self.assertEqual(audited["prepared_requests"], 1)
        self.assertEqual(audited["completed_model_calls"], 0)
        self.assertEqual((root / probe_module.LEDGER_RELATIVE).read_bytes(), original)

    def test_missing_credentials_at_probe_gate_returns_zero_calls_without_run(self):
        root = self.temp_root()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = probe_module.run_probe(deepcopy(self.config), root, execute=True, packet_path=PACKET_PATH)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["executed"])
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertIn("missing_MIMO_API_KEY", result["blockers"])
        self.assertNotIn("run_directory", result)
        self.assertEqual(list((root / "experiment/mimo_probes").iterdir()), [])
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_credentials_disappear_before_assistant_preflight_returns_auditable_prefix(self):
        result, run = self.generate(drop_credentials=True)
        audited = self.assert_failed_prefix(result, run, stage="preflight", calls=0, upstream="missing_credential")
        self.assertFalse(audited["preflight_passed"])
        self.assertEqual(audited["prepared_requests"], 0)
        self.assertEqual([event["event_type"] for event in events_at(run)],
                         ["probe_started", "assistant_task_started", "assistant_task_failed", "probe_failed"])

    def test_summary_actual_count_changed_without_terminal_is_rejected(self):
        summary = deepcopy(self.summary)
        summary["actual_model_calls"] = 3
        self.assert_rejected({REAL_RUN / "summary.json": json_bytes(summary)},
                             "summary_probe_terminal_mismatch", hash_valid=True)

    def test_summary_report_item_changed_without_terminal_is_rejected(self):
        summary = deepcopy(self.summary)
        summary["report"]["sections"][0]["items"][0]["text"] += "离线篡改"
        self.assert_rejected({REAL_RUN / "summary.json": json_bytes(summary)},
                             "summary_probe_terminal_mismatch", hash_valid=True)

    def test_different_valid_packet_is_rejected(self):
        packet = deepcopy(self.packet)
        packet["packet_id"] = "OFFLINE_WRONG_PACKET"
        validate_packet(packet, allow_demo=False)
        with read_overlay({PACKET_PATH: json_bytes(packet)}):
            with self.assertRaisesRegex(ValueError, "^live_audit:event_packet_mismatch$"):
                self.audit()

    def test_wrong_or_malformed_expected_bundle_sha_is_rejected(self):
        for digest, code in (("0" * 64, "external_inputs_invalid"), ("not-a-sha", "invalid_expected_bundle_sha")):
            with self.subTest(校验类型=code):
                with self.assertRaisesRegex(ValueError, "^live_audit:" + code + "$"):
                    self.audit(bundle_sha=digest)

    def test_http_blob_bytes_and_declared_hash_tampering_is_rejected(self):
        received = next(event for event in self.events if event["event_type"] == "mimo_response_received")
        path = REAL_RUN / received["output_ref"]
        self.assert_rejected({path: path.read_bytes() + b" "}, "event_verification_failed")
        changed = read_json(path)
        changed["raw_http_text"] += " "
        self.assert_rejected({path: json_bytes(changed)}, "event_verification_failed")
        for field, value, code in (("response_bytes", 1, "raw_http_bytes_mismatch"),
                                    ("response_sha256", "0" * 64, "raw_http_sha_mismatch")):
            with self.subTest(HTTP字段=field):
                events, summary = deepcopy(self.events), deepcopy(self.summary)
                event = next(event for event in events if event["event_id"] == received["event_id"])
                event["payload"][field] = value
                self.assert_rejected(self.reseal(events, summary), code, hash_valid=True)

    def test_duplicate_stage_with_rehashed_chain_is_rejected(self):
        events, summary = deepcopy(self.events), deepcopy(self.summary)
        event = next(event for event in events if event["event_type"] == "assistant_stage_validated"
                     and event["phase"] == "label")
        event["phase"] = "search"
        self.assert_rejected(self.reseal(events, summary), "event_phase_mismatch", hash_valid=True)

    def test_ghost_completion_without_http_completed_passes_hash_but_is_rejected(self):
        events, summary = deepcopy(self.events), deepcopy(self.summary)
        events = [event for event in events if not (event["event_type"] == "mimo_request_completed"
                                                    and ":integrate:" in event["task_id"])]
        self.assert_rejected(self.reseal(events, summary), "response_without_completed_http", hash_valid=True)

    def test_rehashed_summary_and_terminals_cannot_forge_actual_count_or_usage(self):
        for field in ("actual_model_calls", "reported_usage"):
            with self.subTest(篡改字段=field):
                events, summary = deepcopy(self.events), deepcopy(self.summary)
                if field == "actual_model_calls":
                    summary[field] = 3
                    code = "summary_model_calls_mismatch"
                else:
                    summary[field]["prompt_tokens"] += 1
                    summary[field]["total_tokens"] += 1
                    code = "terminal_usage_mismatch"
                events[-2]["payload"][field] = deepcopy(summary[field])
                events[-1]["payload"][field] = deepcopy(summary[field])
                self.assert_rejected(self.reseal(events, summary), code, hash_valid=True)

    def test_rehashed_summary_and_terminals_cannot_forge_report_items(self):
        events, summary = deepcopy(self.events), deepcopy(self.summary)
        summary["report"]["sections"][0]["items"][0]["text"] += "离线篡改"
        events[-2]["payload"]["report"] = deepcopy(summary["report"])
        events[-1]["payload"]["report"] = deepcopy(summary["report"])
        self.assert_rejected(self.reseal(events, summary), "report_mismatch", hash_valid=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
