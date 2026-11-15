"""纯标准库离线集成测试；所有响应、usage 和金额均为单元夹具。

is_demo=False 仅用于覆盖 live_probe 契约，绝不表示真实模型或真实来源。
只读 core、提示词 bundle 和 vendor 原件；不读取真实凭据、不使用真实账本。
所有运行产物保留在 tests 下的 mkdtemp 目录，不清理、不作为探测结果交付。
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
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_KEY = "sk-fake-offline-only"
PRIVATE_ERROR = "离线异常原文不得进入日志：" + FAKE_KEY
USER_VIEW = "这是单元测试观点：我希望虚构图书角允许一次借两本书，不用于真实研究。"
GOLD_SENTINEL = "OFFLINE_UNIT_GOLD_MUST_NOT_REACH_MODEL"
USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 80,
    "total_tokens": 1080,
    "prompt_tokens_details": {"cached_tokens": 600},
    "completion_tokens_details": {"reasoning_tokens": 8},
    "web_search_usage": {"tool_usage": 0, "page_usage": 0},
}
# 推理 token 已包含于 completion_tokens，不能重复计费。
UNIT_CHARGE = Decimal("0.000572")


@contextmanager
def offline_scope():
    with ExitStack() as stack:
        # 先替换整个映射，再 patch.dict，避免后者复制真实环境中的任何值。
        stack.enter_context(mock.patch.object(os, "environ", {}))
        stack.enter_context(mock.patch.dict(
            os.environ, {"MIMO_API_KEY": "sk-fake-offline-only"}
        ))
        targets = (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.create_connection", "socket.getaddrinfo", "urllib.request.urlopen",
            "urllib.request.OpenerDirector.open", "urllib.request.HTTPHandler.http_open",
            "urllib.request.HTTPSHandler.https_open",
        )
        guards = [stack.enter_context(mock.patch(
            target, side_effect=AssertionError("离线集成测试禁止任何网络调用")
        )) for target in targets]
        yield guards


with offline_scope() as import_guards:
    from cocoon.assistant import AssistantAdapter
    from cocoon.budget import BudgetLedger
    from cocoon.contracts import output_contract, validate_packet
    from cocoon.mimo import MiMoTransport
    import cocoon.mimo_probe as probe_module
    from cocoon.mimo_probe import APPROVED_LIMIT, LEDGER_RELATIVE, mimo_status, run_probe
    from cocoon.prompts import STAGES
    from cocoon.runtime import EventStore, canonical_hash, verify_events, write_json_new

    for import_guard in import_guards:
        import_guard.assert_not_called()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def unit_packet():
    text = "单元夹具：虚构图书角每次可借两本书。仅供离线测试，不是真实规则，不得交付或用于研究。"
    return {
        "schema_version": "1.0",
        "packet_id": "offline_unit_packet",
        "topic_id": "offline_unit_topic",
        "topic": "虚构图书角借阅规则的单元夹具，仅测试不交付",
        "stance_axis": {
            "name": "单元夹具借阅数量偏好",
            "negative_label": "一次借一本",
            "positive_label": "一次借两本",
        },
        "evidence": [{
            "evidence_id": "unit_rules",
            "text": text,
            "source_url": "https://example.invalid/rules",
            "source_family_id": "offline_unit_family",
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }],
        "is_demo": False,
    }


class OfflineResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.status = 200
        self.headers = {"x-request-id": "offline-unit-http"}


class MiMoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guards = self.stack.enter_context(offline_scope())
        self.opener = mock.Mock()
        self.build = self.stack.enter_context(mock.patch(
            "urllib.request.build_opener", return_value=self.opener
        ))
        self.opener.open.side_effect = self.respond
        core = json.loads((ROOT / "configs/core.json").read_text(encoding="utf-8"))
        self.config = deepcopy(core)
        self.assertEqual(self.config["models"]["assistant"]["max_tokens"], 4096)
        self.assertEqual(self.config["budget"]["initial_probe_limit_cny"], "5.00")
        self.assertEqual(self.config["budget"]["initial_probe_ledger"], LEDGER_RELATIVE)
        self.packet = validate_packet(unit_packet(), allow_demo=False)
        self.temp_roots = []
        self.audit_roots = []
        self.root = self.temp_root()
        for relative in ("experiment", "experiment/billing", "experiment/mimo_probes"):
            target = self.root / relative
            self.assertTrue(target.parent.is_dir())
            target.mkdir()
        self.ledger_path = self.root / LEDGER_RELATIVE
        self.assertTrue(self.ledger_path.is_relative_to(ROOT / "tests"))
        self.assertNotEqual(self.ledger_path, ROOT / LEDGER_RELATIVE)
        self.wires = []
        self.responses = []
        self.fault = None
        self.usage = deepcopy(USAGE)

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()
        for root in self.audit_roots:
            with self.subTest(审计目录=str(root)):
                self.assertTrue(verify_events(root)["chain_valid"])
                for event in self.events(root):
                    self.assertIs(event["is_demo"], False)
        for root in self.temp_roots:
            for path in root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(FAKE_KEY.encode(), data, str(path))
                    self.assertNotIn(PRIVATE_ERROR.encode(), data, str(path))
                    self.assertNotIn(json.dumps(PRIVATE_ERROR)[1:-1].encode(), data, str(path))

    def temp_root(self):
        self.assertTrue((ROOT / "tests").is_dir())
        root = Path(tempfile.mkdtemp(prefix="mimo_integration_unit_only_", dir=ROOT / "tests"))
        self.temp_roots.append(root)
        return root

    def make_runner(self, *, mode="live_probe"):
        store = EventStore(
            self.temp_root(),
            {"suite": "mimo_offline_integration", "notice": "单元夹具，仅测试不交付"},
            canonical_hash(self.packet), False, "offline_unit_experiment",
        )
        self.audit_roots.append(store.root)
        ledger = BudgetLedger(self.ledger_path, APPROVED_LIMIT)
        model = deepcopy(self.config["models"]["assistant"])
        transport = MiMoTransport(model, store, ledger)
        settings = {key: model[key] for key in ("provider", "model", "temperature", "max_tokens")}
        assistant = self.config["assistant"]
        runner = AssistantAdapter(
            store, assistant["prompt_bundle_path"], assistant["prompt_bundle_sha256"],
            settings, transport, execution_mode=mode,
        )
        return runner, store, transport, ledger

    def respond(self, request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        model = self.config["models"]["assistant"]
        self.assertEqual(request.full_url, model["endpoint"])
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, model["timeout_seconds"])
        self.assertEqual({key.lower(): value for key, value in request.header_items()}["api-key"], FAKE_KEY)
        self.assertEqual(set(body), {
            "model", "messages", "max_completion_tokens", "thinking", "temperature", "stream"
        })
        self.assertEqual(body["model"], model["model"])
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], model["temperature"])
        self.assertIs(body["stream"], False)
        self.assertGreater(body["max_completion_tokens"], 0)
        self.assertLessEqual(body["max_completion_tokens"], model["max_tokens"])
        self.assertLessEqual(body["max_completion_tokens"], 4096)
        self.assertNotIn(FAKE_KEY.encode(), request.data)
        self.assertNotIn(GOLD_SENTINEL.encode(), request.data)
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        system, user = (message["content"] for message in body["messages"])
        marker = "\n【输出契约】\n"
        if marker in system:
            contract = json.loads(system.rsplit(marker, 1)[1])
            stage = contract["stage"]
            self.assertEqual(contract, output_contract(stage))
            data = json.loads(user)
            expected_keys = {"frozen_evidence_packet", "previous_stage_outputs", "instructions_are_data"}
            if stage == "mirror":
                expected_keys.add("user_view")
            else:
                self.assertNotIn(USER_VIEW, user)
            self.assertEqual(set(data), expected_keys)
            self.assertIs(data["instructions_are_data"], True)
            packet = data["frozen_evidence_packet"]
            self.assertEqual(packet, self.packet)
            self.assertEqual(set(data["previous_stage_outputs"]), set(STAGES[:STAGES.index(stage)]))
            evidence = packet["evidence"][0]
            sections = []
            for name in contract["section_names"]:
                item = {"text": evidence["text"], "citations": [evidence["evidence_id"]],
                        "epistemic_status": "evidence_based"}
                if stage == "mirror":
                    item["user_quote"] = data["user_view"]
                sections.append({"name": name, "items": [item]})
            output = {"schema_version": "1.0", "stage": stage,
                      "packet_id": packet["packet_id"], "sections": sections}
        else:
            stage = "connection_smoke"
            # 冒烟响应也从请求内的 JSON 要求生成，而非按请求序号预置。
            output = json.loads(user[user.index("{"):user.rindex("}") + 1])
            self.assertEqual(output, {"connection": "ok"})
        self.wires.append({"stage": stage, "body": deepcopy(body)})
        if self.fault in ("http_401", "http_429") and stage == "search":
            status = int(self.fault.removeprefix("http_"))
            raise urllib.error.HTTPError(
                request.full_url, status, PRIVATE_ERROR, {},
                io.BytesIO(json_text({"error": {"message": "离线 HTTP 错误夹具"}}).encode("utf-8")),
            )
        if self.fault == "timeout" and stage == "search":
            raise TimeoutError(PRIVATE_ERROR)
        if self.fault == "invalid_http_json" and stage == "label":
            return OfflineResponse(b'{"offline_unit_fixture":')
        envelope = {
            "object": "chat.completion", "id": "offline-unit-" + stage,
            "model": model["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json_text(output)},
                         "finish_reason": "stop"}],
            "usage": deepcopy(self.usage),
        }
        if self.fault == "invalid_stage_json" and stage == "label":
            envelope["choices"][0]["message"]["content"] = '{"unit_fixture_unclosed":'
        if self.fault == "wrong_model" and stage == "search":
            envelope["model"] = "offline-wrong-model"
        if self.fault == "missing_usage" and stage == "label":
            envelope.pop("usage")
        self.responses.append(deepcopy(envelope))
        return OfflineResponse(json_text(envelope).encode("utf-8"))

    def events(self, root):
        return [json.loads(line) for line in
                (root / "events/events.jsonl").read_text(encoding="utf-8").splitlines()]

    def ledger_rows(self):
        return [json.loads(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines()]

    def blob(self, root, reference):
        if isinstance(reference, dict):
            reference = reference["ref"]
        return json.loads((root / reference).read_text(encoding="utf-8"))

    def assert_failed(self, result, store, code, stage, calls, *, completed=(), upstream=None):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        if upstream is not None:
            self.assertEqual(result["error"]["upstream_code"], upstream)
        self.assertEqual(result["failed_stage"], stage)
        self.assertEqual(result["actual_model_calls"], calls)
        self.assertEqual(result["fixture_calls"], 0)
        self.assertEqual(result["external_retrieval_calls"], 0)
        self.assertEqual(result["completed_stages"], list(completed))
        self.assertEqual(list(result["stage_results"]), list(completed))
        self.assertIs(result["demo_fallback"], False)
        self.assertIs(result["formal_eligible"], False)
        self.assertIs(result["is_demo"], False)
        self.assertIsNone(result["report"])
        self.assertIsNone(result["mirror"])
        self.assertNotIn(FAKE_KEY, json_text(result))
        self.assertNotIn(PRIVATE_ERROR, json_text(result))
        events = self.events(store.root)
        self.assertEqual(events[-1]["event_type"], "assistant_task_failed")
        self.assertEqual(events[-1]["payload"], result)
        self.assertNotIn("assistant_task_completed", [event["event_type"] for event in events])
        self.assertEqual([event["phase"] for event in events
                          if event["event_type"] == "assistant_stage_validated"], list(completed))
        attempted = [] if stage == "preflight" else list(completed) + [stage]
        self.assertEqual([event["phase"] for event in events
                          if event["event_type"] == "assistant_request_started"], attempted)

    def assert_success(self, result, count):
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(result["actual_model_calls"], count)
        self.assertEqual(result["fixture_calls"], 0)
        self.assertEqual(result["completed_stages"], list(STAGES[:count]))
        self.assertEqual(list(result["stage_results"]), list(STAGES[:count]))
        self.assertEqual(result["packet_hash"], canonical_hash(self.packet))
        self.assertIs(result["is_demo"], False)
        self.assertIs(result["demo_fallback"], False)
        self.assertIs(result["formal_eligible"], False)
        self.assertEqual(result["external_retrieval_calls"], 0)
        self.assertIsNone(result["actual_cost_cny"])
        self.assertEqual(result["semantic_review"], "pending_independent_review")
        self.assertIs(result["usage_complete"], True)
        self.assertEqual(result["reported_usage"], {
            key: count * self.usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        })
        for stage in STAGES[:count]:
            self.assertEqual(result["stage_results"][stage]["output"]["stage"], stage)
            self.assertTrue(result["stage_results"][stage]["citation_audit"]["known_evidence_only"])
        self.assertEqual(result["report"], result["stage_results"]["integrate"]["output"])

    def assert_settled(self, ledger, count):
        billing = ledger.status()
        self.assertEqual(Decimal(billing["limit_cny"]), Decimal("5.00"))
        self.assertEqual(billing["request_count"], count)
        self.assertEqual(billing["unresolved_request_count"], 0)
        self.assertEqual(Decimal(billing["reserved_cny"]), 0)
        self.assertEqual(Decimal(billing["charged_cny"]), UNIT_CHARGE * count)
        self.assertEqual(Decimal(billing["total_cost_cny"]), UNIT_CHARGE * count)
        self.assertGreater(Decimal(billing["charged_cny"]), 0)
        self.assertEqual(billing["cache_assumed_zero_request_count"], 0)
        self.assertFalse(billing["blocked"])
        self.assertFalse(billing["needs_reconciliation"])
        self.assertEqual(sum(row["type"] == "settle" for row in self.ledger_rows()), count)

    def assert_first_stage_http_failure(self, fault, upstream):
        self.fault = fault
        runner, store, transport, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_first_failure", include_mirror=True, user_view=USER_VIEW)
        self.assert_failed(result, store, "mimo_error", "search", 1, upstream=upstream)
        self.assertEqual(transport.actual_model_calls, 1)
        self.opener.open.assert_called_once()
        self.assertEqual([wire["stage"] for wire in self.wires], ["search"])
        failed = [event for event in self.events(store.root) if event["event_type"] == "mimo_request_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"]["code"], upstream)
        billing = ledger.status()
        self.assertEqual(billing["uncertain_request_count"], 1)
        self.assertEqual(billing["unresolved_request_count"], 1)
        self.assertGreater(Decimal(billing["reserved_cny"]), 0)
        self.assertIsNone(billing["total_cost_cny"])
        self.assertEqual(Decimal(billing["charged_cny"]), 0)
        self.assertEqual([row["type"] for row in self.ledger_rows()], ["init", "reserve", "uncertain"])

    def test_four_stage_pipeline_uses_real_transport_store_and_ledger(self):
        runner, store, transport, ledger = self.make_runner()
        original = deepcopy(self.packet)
        result = runner.run(self.packet, "offline_four_stages")
        self.assert_success(result, 4)
        self.assertIsNone(result["mirror"])
        self.assertEqual(self.packet, original)
        self.assertEqual(store.manifest_hash, canonical_hash(original))
        self.assertEqual(transport.actual_model_calls, 4)
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertEqual([wire["stage"] for wire in self.wires], list(STAGES[:4]))
        self.assertEqual(self.events(store.root)[-1]["payload"], result)
        self.assert_settled(ledger, 4)

    def test_five_stages_full_usage_cache_charge_and_context_isolation(self):
        runner, store, transport, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_five_stages", include_mirror=True, user_view=USER_VIEW)
        self.assert_success(result, 5)
        self.assertEqual(transport.actual_model_calls, 5)
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual([wire["stage"] for wire in self.wires], list(STAGES))
        self.assert_settled(ledger, 5)
        expected_charge = (Decimal(1000 - 600) + Decimal(600) * Decimal("0.02") + Decimal(80) * 2) / 1000000
        self.assertEqual(UNIT_CHARGE, expected_charge)
        for wire in self.wires:
            data = json.loads(wire["body"]["messages"][1]["content"])
            for stage, previous in data["previous_stage_outputs"].items():
                self.assertEqual(previous, {
                    "raw_text": result["stage_results"][stage]["raw_text"],
                    "structured_output": result["stage_results"][stage]["output"],
                })
            if wire["stage"] == "mirror":
                self.assertEqual(data["user_view"], USER_VIEW)
            self.assertEqual(wire["body"]["max_completion_tokens"], 4096)
        self.assertEqual(result["mirror"]["sections"][0]["items"][0]["user_quote"], USER_VIEW)
        raw_events = [event for event in self.events(store.root) if event["event_type"] == "mimo_response_received"]
        self.assertEqual(len(raw_events), 5)
        for event in raw_events:
            raw = json.loads(self.blob(store.root, event["output_ref"])["raw_http_text"])
            self.assertEqual(raw["usage"], USAGE)
        for row in self.ledger_rows():
            if row["type"] == "settle":
                self.assertEqual(row["usage"]["prompt_tokens_details"], {"cached_tokens": 600})
                self.assertEqual(row["usage"]["web_search_usage"], {"tool_usage": 0, "page_usage": 0})
                self.assertEqual(Decimal(row["charged_cny"]), UNIT_CHARGE)

    def test_http_401_stops_first_stage_and_records_safe_failure(self):
        self.assert_first_stage_http_failure("http_401", "auth_failed")

    def test_http_429_stops_first_stage_without_retry(self):
        self.assert_first_stage_http_failure("http_429", "rate_limited")

    def test_timeout_stops_first_stage_without_leaking_exception(self):
        self.assert_first_stage_http_failure("timeout", "timeout")

    def test_invalid_stage_json_second_stage_is_still_billed(self):
        self.fault = "invalid_stage_json"
        runner, store, _, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_invalid_stage", include_mirror=True, user_view=USER_VIEW)
        self.assert_failed(result, store, "invalid_output", "label", 2, completed=STAGES[:1])
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual([wire["stage"] for wire in self.wires], list(STAGES[:2]))
        self.assert_settled(ledger, 2)
        received = [event for event in self.events(store.root)
                    if event["event_type"] == "assistant_response_received" and event["phase"] == "label"]
        self.assertEqual(len(received), 1)
        response = self.blob(store.root, received[0]["output_ref"])
        self.assertEqual(response["raw_text"], '{"unit_fixture_unclosed":')
        self.assertEqual(response["usage"]["total_tokens"], USAGE["total_tokens"])

    def test_invalid_http_json_second_stage_keeps_first_bill_and_unknown_reserve(self):
        self.fault = "invalid_http_json"
        runner, store, _, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_invalid_http")
        self.assert_failed(result, store, "mimo_error", "label", 2,
                           completed=STAGES[:1], upstream="invalid_json")
        self.assertEqual(self.opener.open.call_count, 2)
        billing = ledger.status()
        self.assertEqual(Decimal(billing["charged_cny"]), UNIT_CHARGE)
        self.assertEqual(billing["unresolved_request_count"], 1)
        self.assertGreater(Decimal(billing["reserved_cny"]), 0)
        self.assertIsNone(billing["total_cost_cny"])
        received = [event for event in self.events(store.root) if event["event_type"] == "mimo_response_received"]
        captured = self.blob(store.root, received[-1]["output_ref"])
        self.assertNotIn("raw_http_text", captured)
        self.assertTrue(captured["omitted_due_to_incomplete_security_scan"])
        self.assertEqual(captured["response_sha256"], hashlib.sha256(b'{"offline_unit_fixture":').hexdigest())
        self.assertEqual(sum(row["type"] == "settle" for row in self.ledger_rows()), 1)

    def test_demo_response_at_adapter_boundary_is_not_accepted(self):
        runner, store, transport, ledger = self.make_runner()
        complete = transport.complete

        def mislabel(request):
            response = complete(request)
            # HTTP 与结算仍走真实实现，仅模拟传输边界误标。
            response["is_demo"] = True
            return response

        with mock.patch.object(transport, "complete", side_effect=mislabel):
            result = runner.run(self.packet, "offline_response_demo")
        self.assert_failed(result, store, "mode_mismatch", "search", 1)
        self.opener.open.assert_called_once()
        self.assert_settled(ledger, 1)
        received = next(event for event in self.events(store.root)
                        if event["event_type"] == "assistant_response_received")
        self.assertIs(self.blob(store.root, received["output_ref"])["is_demo"], True)

    def test_research_mode_remains_blocked_with_real_transport(self):
        runner, store, transport, ledger = self.make_runner(mode="research")
        result = runner.run(self.packet, "offline_research_blocked")
        self.assert_failed(result, store, "live_disabled", "preflight", 0)
        self.assertEqual(transport.actual_model_calls, 0)
        self.assertEqual(ledger.status()["request_count"], 0)
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_missing_credential_returns_safe_preflight_failure_with_zero_calls(self):
        runner, store, transport, ledger = self.make_runner()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = runner.run(self.packet, "offline_missing_credential")
        self.assert_failed(result, store, "mimo_error", "preflight", 0, upstream="missing_credential")
        self.assertEqual(transport.actual_model_calls, 0)
        self.assertEqual(ledger.status()["request_count"], 0)
        self.assertNotIn("Traceback", json_text(result))
        self.assertEqual([event["event_type"] for event in self.events(store.root)],
                         ["assistant_task_started", "assistant_task_failed"])
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_status_and_execute_with_missing_key_make_zero_http_calls(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status = mimo_status(self.config, self.root)
            result = run_probe(self.config, self.root, execute=True)
        self.assertEqual(status["status"], "blocked")
        self.assertIn("missing_MIMO_API_KEY", status["blockers"])
        self.assertEqual(status["credential"], {"env": "MIMO_API_KEY", "present": False, "usable_format": False})
        self.assertIs(status["network_called"], False)
        self.assertIs(status["live_connection_verified"], False)
        self.assertIs(status["formal_launch_enabled"], False)
        self.assertIs(result["executed"], False)
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertEqual(result["billing"]["request_count"], 0)
        self.assertEqual(list((self.root / "experiment/mimo_probes").iterdir()), [])
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_wrong_response_model_stops_pipeline_after_settlement(self):
        self.fault = "wrong_model"
        runner, store, _, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_wrong_model", include_mirror=True, user_view=USER_VIEW)
        self.assert_failed(result, store, "mimo_error", "search", 1, upstream="model_mismatch")
        self.opener.open.assert_called_once()
        self.assert_settled(ledger, 1)
        self.assertEqual([wire["stage"] for wire in self.wires], ["search"])

    def test_execute_false_only_returns_status_without_source_or_packet_reads(self):
        source_check = probe_module._source_check
        with mock.patch.object(probe_module, "_source_check", wraps=source_check) as checked:
            result = run_probe(self.config, self.root, execute=False,
                               packet_path=self.root / "deliberately_absent_packet.json")
        checked.assert_not_called()
        self.assertEqual(result["status"], "ready_for_explicit_smoke")
        self.assertIs(result["executed"], False)
        self.assertIs(result["network_called"], False)
        self.assertIs(result["live_connection_verified"], False)
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertEqual(list((self.root / "experiment/mimo_probes").iterdir()), [])
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_smoke_uses_one_request_with_128_token_cap(self):
        result = run_probe(self.config, self.root, execute=True)
        root = Path(result["run_directory"])
        self.audit_roots.append(root)
        self.assertTrue(root.is_relative_to(self.root))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["kind"], "connection_smoke")
        self.assertEqual(result["actual_model_calls"], 1)
        self.assertIs(result["exact_output_check"], True)
        self.assertIs(result["formal_eligible"], False)
        self.assertIs(result["demo_fallback"], False)
        self.opener.open.assert_called_once()
        self.assertEqual(self.wires[0]["stage"], "connection_smoke")
        self.assertEqual(self.wires[0]["body"]["max_completion_tokens"], 128)
        self.assertTrue(result["verification"]["chain_valid"])
        self.assertFalse(any(event["event_type"].startswith("assistant_") for event in self.events(root)))
        self.assert_settled(BudgetLedger(self.ledger_path, APPROVED_LIMIT), 1)
        reserved = next(row for row in self.ledger_rows() if row["type"] == "reserve")
        self.assertEqual(reserved["max_completion_tokens"], 128)

    def test_explicit_packet_probe_verifies_real_sources_readonly_and_excludes_gold(self):
        packet_path = self.root / "unit_packet.json"
        view_path = self.root / "unit_view.txt"
        gold_path = self.root / "unit_gold.json"
        write_json_new(packet_path, self.packet)
        with view_path.open("x", encoding="utf-8") as stream:
            stream.write(USER_VIEW)
        write_json_new(gold_path, {"unit_gold": GOLD_SENTINEL})
        config = deepcopy(self.config)
        config["gold_standard"] = {"path": str(gold_path), "answers": GOLD_SENTINEL}
        source_paths = [Path(config["assistant"][key])
                        for key in ("prompt_bundle_path", "entry_path", "source_path")]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
        verify_sources = probe_module.verify_original_sources
        path_open = Path.open

        def guard_gold(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), gold_path.resolve(), "探测流程不得读取金标准夹具")
            return path_open(path, *args, **kwargs)

        with mock.patch.object(probe_module, "verify_original_sources", wraps=verify_sources) as verified:
            with mock.patch.object(Path, "open", guard_gold):
                result = run_probe(config, self.root, execute=True,
                                   packet_path=packet_path, user_view_path=view_path)
        verified.assert_called_once()
        self.assertEqual(Path(verified.call_args.args[1]), source_paths[1])
        self.assertEqual(Path(verified.call_args.args[2]), source_paths[2])
        self.assertEqual(verified.call_args.args[0]["bundle_sha256"], config["assistant"]["prompt_bundle_sha256"])
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths})
        root = Path(result["run_directory"])
        self.audit_roots.append(root)
        self.assertTrue(root.is_relative_to(self.root))
        self.assert_success(result, 5)
        self.assertEqual(result["kind"], "assistant_probe")
        self.assertTrue(result["verification"]["chain_valid"])
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual(json.loads((root / "summary.json").read_text(encoding="utf-8")), result)
        self.assertEqual(result["verification"], verify_events(root))
        self.assertTrue(all(event["material_manifest_hash"] == canonical_hash(self.packet)
                            for event in self.events(root)))
        self.assertNotIn(GOLD_SENTINEL, json_text(self.wires))
        self.assert_settled(BudgetLedger(self.ledger_path, APPROVED_LIMIT), 5)

    def test_five_yuan_cap_survives_runner_and_ledger_reconstruction(self):
        self.usage = {
            "prompt_tokens": 1048576, "completion_tokens": 4096, "total_tokens": 1052672,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
            "web_search_usage": {"tool_usage": 0, "page_usage": 0},
        }
        first, _, _, ledger = self.make_runner()
        result = first.run(self.packet, "offline_spend_four_reservations")
        self.assert_success(result, 4)
        billing = ledger.status()
        self.assertEqual(Decimal(billing["charged_cny"]), Decimal("4.227072"))
        self.assertEqual(Decimal(billing["available_cny"]), Decimal("0.772928"))
        self.assertEqual(Decimal(billing["limit_cny"]), Decimal("5.00"))
        original_log = self.ledger_path.read_bytes()
        for attempt in range(2):
            with self.subTest(重建次数=attempt + 1):
                runner, store, transport, reopened = self.make_runner()
                blocked = runner.run(self.packet, "offline_capped_" + str(attempt))
                self.assert_failed(blocked, store, "budget_error", "search", 0, upstream="budget_exceeded")
                self.assertEqual(transport.actual_model_calls, 0)
                self.assertEqual(reopened.status(), billing)
                self.assertEqual(self.ledger_path.read_bytes(), original_log)
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertEqual(self.build.call_count, 4)
        self.assertEqual(sum(row["type"] == "init" for row in self.ledger_rows()), 1)
        self.assertEqual(sum(row["type"] == "reserve" for row in self.ledger_rows()), 4)

    def test_unknown_usage_keeps_reserve_and_blocks_rebuilt_client_and_probe(self):
        self.fault = "missing_usage"
        runner, store, _, ledger = self.make_runner()
        result = runner.run(self.packet, "offline_unknown_usage")
        self.assert_failed(result, store, "mimo_error", "label", 2,
                           completed=STAGES[:1], upstream="missing_usage")
        billing = ledger.status()
        self.assertEqual(Decimal(billing["charged_cny"]), UNIT_CHARGE)
        self.assertEqual(Decimal(billing["reserved_cny"]), Decimal("1.056768"))
        self.assertEqual(billing["unresolved_request_count"], 1)
        self.assertEqual(billing["uncertain_request_count"], 1)
        self.assertIsNone(billing["total_cost_cny"])
        original_log = self.ledger_path.read_bytes()
        self.fault = None
        fresh, fresh_store, transport, reopened = self.make_runner()
        blocked = fresh.run(self.packet, "offline_rebuilt_unknown")
        self.assert_failed(blocked, fresh_store, "budget_error", "search", 0, upstream="unresolved_attempt")
        self.assertEqual(transport.actual_model_calls, 0)
        status = mimo_status(self.config, self.root)
        probe = run_probe(self.config, self.root, execute=True)
        self.assertEqual(status["status"], "blocked")
        self.assertIn("billing_reconciliation_required", status["blockers"])
        self.assertIs(probe["executed"], False)
        self.assertEqual(probe["actual_model_calls"], 0)
        self.assertEqual(reopened.status(), billing)
        self.assertEqual(self.ledger_path.read_bytes(), original_log)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.build.call_count, 2)
        self.assertEqual(list((self.root / "experiment/mimo_probes").iterdir()), [])
        self.assertEqual(sum(row["type"] == "init" for row in self.ledger_rows()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
