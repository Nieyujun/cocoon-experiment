"""助手 API 的纯标准库离线验收；运行产物保留在 tests 下供复核。"""

import ast
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "configs" / "assistant_prompts_v1.json"
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextmanager
def offline_network():
    targets = (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.socket.sendto",
        "socket.socket",
        "socket.create_connection",
        "socket.getaddrinfo",
        "urllib.request.urlopen",
        "urllib.request.OpenerDirector.open",
        "urllib.request.HTTPHandler.http_open",
        "urllib.request.HTTPSHandler.https_open",
    )
    with ExitStack() as stack:
        guards = {
            target: stack.enter_context(mock.patch(
                target, side_effect=AssertionError("离线测试禁止任何网络调用")
            ))
            for target in targets
        }
        yield guards


# 导入生产模块也置于网络封锁内，退出后只恢复本测试设置的 mock。
with offline_network() as import_guards:
    import cocoon.assistant as assistant_module
    from cocoon.assistant import AdapterError, AssistantAdapter
    from cocoon.prompts import load_prompt_bundle
    from cocoon.runtime import EventStore, canonical_hash, verify_events
    for import_guard in import_guards.values():
        import_guard.assert_not_called()


STAGES = ("search", "label", "blindspot", "integrate", "mirror")
SECTIONS = {
    "search": ("evidence_overview", "coverage_gaps"),
    "label": ("source_assessment",),
    "blindspot": ("missing_dimensions", "possible_biases", "counterpoints"),
    "integrate": ("consensus", "disagreements", "blindspots", "sources"),
    "mirror": ("stance_analysis", "possible_biases", "blindspot_alert", "suggestions"),
}
USER_VIEW = "我更愿意使用园区接驳车，但也想比较步行换乘的等待时间。"
MODEL_SETTINGS = {
    "provider": "fixture",
    "model": "fixture-requested-alias",
    "temperature": 0,
    "max_tokens": 2048,
}
STORE_CONFIG = {"schema_version": "1.0", "suite": "assistant_offline_unittest"}
DEFAULT = object()


def strict_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def traffic_packet():
    texts = (
        "合成交通记录甲：封闭园区甲站至乙站的接驳车行程为四分钟，候车为三分钟。",
        "合成交通记录乙：同一封闭园区甲站至乙站的步行行程为九分钟，未记录周末情况。",
    )
    return {
        "schema_version": "1.0",
        "packet_id": "traffic_fixture_packet",
        "topic_id": "traffic_fixture_topic",
        "topic": "封闭合成园区接驳车与步行换乘的比较",
        "stance_axis": {
            "name": "合成园区出行方式偏好",
            "negative_label": "偏好步行换乘",
            "positive_label": "偏好接驳车换乘",
        },
        "evidence": [
            {
                "evidence_id": f"traffic_e{index}",
                "text": text,
                "source_url": f"synthetic://fixture/traffic/record-{index}",
                "source_family_id": f"traffic_family_{index}",
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for index, text in enumerate(texts, 1)
        ],
        "is_demo": True,
    }


def stage_output(stage, packet, user_view=None):
    sections = []
    for index, name in enumerate(SECTIONS[stage]):
        evidence = packet["evidence"][index % len(packet["evidence"])]
        if name in {"coverage_gaps", "missing_dimensions", "possible_biases", "blindspot_alert"}:
            item = {
                "text": "现有合成记录不足以判断周末换乘情况。",
                "citations": [],
                "epistemic_status": "uncertainty",
            }
        elif name == "suggestions":
            item = {
                "text": "下一步可比较合成记录中的候车与步行时间。",
                "citations": [],
                "epistemic_status": "suggestion",
            }
        else:
            item = {
                "text": evidence["source_url"] if name == "sources" else evidence["text"],
                "citations": [evidence["evidence_id"]],
                "epistemic_status": "evidence_based",
            }
            if stage == "mirror":
                item["user_quote"] = user_view
        sections.append({"name": name, "items": [item]})
    return {
        "schema_version": "1.0",
        "stage": stage,
        "packet_id": packet["packet_id"],
        "sections": sections,
    }


class FixtureTransport:
    execution_mode = "offline_fixture"

    def __init__(self, behavior=None):
        self.behavior = behavior
        self.requests = []
        self.responses = []

    def complete(self, request):
        self.requests.append(deepcopy(request))
        user_data = json.loads(request["messages"][1]["content"])
        packet = user_data["frozen_evidence_packet"]
        # 实际 API 将 stage 放在请求顶层，而不是第二条 message 的 JSON 内。
        stage = request["stage"]
        response = {
            "raw_text": strict_json(stage_output(stage, packet, user_data.get("user_view"))),
            "provider": "fixture",
            "model": "fixture-v1",
            "response_id": "fixture:" + request["request_id"],
            "usage": None,
            "finish_reason": "stop",
            "is_demo": True,
        }
        if self.behavior is not None:
            response = self.behavior(request, response)
        self.responses.append(deepcopy(response))
        return response


class AssistantOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with offline_network() as guards:
            declared = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
            cls.bundle_sha = declared["bundle_sha256"]
            cls.bundle = load_prompt_bundle(BUNDLE_PATH, cls.bundle_sha)
            for guard in guards.values():
                guard.assert_not_called()

    def setUp(self):
        self.network_context = offline_network()
        self.network_guards = self.network_context.__enter__()
        self.stores = []

    def tearDown(self):
        try:
            for store in self.stores:
                with self.subTest(保留目录=str(store.root)):
                    audit = verify_events(store.root)
                    self.assertIs(audit["chain_valid"], True)
                    self.assertEqual(audit["event_count"], len(self.events(store)))
                    self.assertEqual(
                        audit["blob_count"], len(list((store.root / "raw/blobs").glob("*.json")))
                    )
            for target, guard in self.network_guards.items():
                with self.subTest(网络入口=target):
                    guard.assert_not_called()
        finally:
            self.network_context.__exit__(None, None, None)

    def temp_root(self):
        return Path(tempfile.mkdtemp(prefix="assistant_offline_", dir=ROOT / "tests"))

    def make_adapter(self, *, packet=None, transport=None, settings=DEFAULT,
                     store_demo=True, manifest_hash=None, bundle_path=None,
                     expected_sha=None, **options):
        packet = traffic_packet() if packet is None else packet
        transport = FixtureTransport() if transport is None else transport
        settings = deepcopy(MODEL_SETTINGS) if settings is DEFAULT else settings
        store = EventStore(
            self.temp_root(), STORE_CONFIG,
            canonical_hash(packet) if manifest_hash is None else manifest_hash,
            store_demo, "assistant_fixture_experiment",
        )
        self.stores.append(store)
        options.setdefault("execution_mode", "offline_fixture")
        adapter = AssistantAdapter(
            store, BUNDLE_PATH if bundle_path is None else bundle_path,
            self.bundle_sha if expected_sha is None else expected_sha,
            settings, transport, **options,
        )
        return adapter, store, transport, packet

    def events(self, store):
        return [json.loads(line) for line in
                (store.root / "events/events.jsonl").read_text(encoding="utf-8").splitlines()]

    def blob(self, store, reference, expected_sha=None):
        if isinstance(reference, dict):
            expected_sha = reference["sha256"]
            reference = reference["ref"]
        self.assertTrue(reference.startswith("raw/blobs/"))
        path = store.root / reference
        self.assertTrue(path.resolve().is_relative_to(store.root.resolve()))
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(digest, path.stem)
        if expected_sha is not None:
            self.assertEqual(digest, expected_sha)
        value = json.loads(data)
        self.assertEqual(canonical_hash(value), digest)
        return value

    def assert_success(self, result, transport, stages):
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["completed_stages"], list(stages))
        self.assertEqual(list(result["stage_results"]), list(stages))
        self.assertEqual([request["stage"] for request in transport.requests], list(stages))
        self.assertEqual(result["fixture_calls"], len(stages))
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertEqual(result["external_retrieval_calls"], 0)
        self.assertEqual(result["actual_cost_cny"], 0)
        self.assertIs(result["is_demo"], True)
        self.assertIs(result["formal_eligible"], False)
        self.assertIs(result["demo_fallback"], False)
        self.assertEqual(result["semantic_review"], "pending_independent_review")
        self.assertEqual(result["report"], result["stage_results"]["integrate"]["output"])
        if "mirror" in stages:
            self.assertEqual(result["mirror"], result["stage_results"]["mirror"]["output"])
        else:
            self.assertIsNone(result["mirror"])
        for stage in stages:
            row = result["stage_results"][stage]
            self.assertEqual(row["output"]["stage"], stage)
            self.assertEqual([section["name"] for section in row["output"]["sections"]],
                             list(SECTIONS[stage]))
            audit = row["citation_audit"]
            self.assertIs(audit["schema_valid"], True)
            self.assertIs(audit["known_evidence_only"], True)
            self.assertIs(audit["formal_eligible"], False)
            self.assertEqual(audit["semantic_entailment"], "pending_independent_review")
            cited = {citation for section in row["output"]["sections"]
                     for item in section["items"] for citation in item["citations"]}
            self.assertEqual(audit["cited_ids"], sorted(cited))
            self.assertGreaterEqual(row["latency_ms"], 0)

    def assert_failed(self, result, store, transport, code, stage, completed=()):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        self.assertEqual(result["failed_stage"], stage)
        self.assertEqual(result["completed_stages"], list(completed))
        self.assertEqual(list(result["stage_results"]), list(completed))
        attempted = [] if stage == "preflight" else list(completed) + [stage]
        self.assertEqual([request["stage"] for request in transport.requests], attempted)
        self.assertEqual(result["fixture_calls"], len(attempted))
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertEqual(result["external_retrieval_calls"], 0)
        self.assertIs(result["formal_eligible"], False)
        self.assertIs(result["demo_fallback"], False)
        self.assertIsNone(result["report"])
        self.assertIsNone(result["mirror"])
        events = self.events(store)
        self.assertEqual(events[-1]["event_type"], "assistant_task_failed")
        self.assertEqual(events[-1]["payload"], result)
        self.assertEqual(events[-1]["error"], result["error"])
        self.assertNotIn("assistant_task_completed", [event["event_type"] for event in events])
        self.assertEqual([event["phase"] for event in events
                          if event["event_type"] == "assistant_stage_validated"], list(completed))

    def assert_response_preserved(self, store, transport, stage):
        events = self.events(store)
        received = [event for event in events
                    if event["event_type"] == "assistant_response_received" and event["phase"] == stage]
        self.assertEqual(len(received), 1)
        event = received[0]
        self.assertIs(event["payload"]["validated"], False)
        self.assertEqual(self.blob(store, event["output_ref"], event["output_sha256"]),
                         transport.responses[-1])
        self.assertEqual(self.blob(store, event["input_ref"], event["input_sha256"]),
                         transport.requests[-1])
        self.assertEqual(self.blob(store, event["payload"]["response"]), transport.responses[-1])
        self.assertEqual(self.blob(store, events[-1]["output_ref"]), transport.responses[-1])

    def assert_not_persisted(self, store, text):
        markers = {text.encode("utf-8"), json.dumps(text, ensure_ascii=True)[1:-1].encode("ascii")}
        for path in store.root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for marker in markers:
                    self.assertNotIn(marker, data, str(path))

    def test_four_stages_succeed_with_actual_prompt_bundle(self):
        adapter, store, transport, packet = self.make_adapter()
        before = deepcopy(packet)
        result = adapter.run(packet, "four_stages")
        self.assert_success(result, transport, STAGES[:4])
        self.assertEqual(packet, before)
        self.assertEqual(result["packet_hash"], canonical_hash(before))
        self.assertIs(result["usage_complete"], False)
        self.assertIsNone(result["reported_usage"])
        fingerprint = result["fingerprint"]
        self.assertEqual(fingerprint["original_bundle_sha256"], self.bundle_sha)
        self.assertEqual(fingerprint["adapter_fingerprint"], canonical_hash({
            key: value for key, value in fingerprint.items() if key != "adapter_fingerprint"
        }))
        self.assertEqual(fingerprint["retry_policy"], "stop_on_first_failure")
        self.assertEqual(fingerprint["evidence_mode"], "frozen_packet_only")
        self.assertIs(fingerprint["automatic_retrieval"], False)
        self.assertIs(fingerprint["live_transport_enabled"], False)
        for request in transport.requests:
            self.assertEqual([message["role"] for message in request["messages"]], ["system", "user"])
            prompt = self.bundle["prompts"][request["stage"]]
            self.assertIn(prompt["text"], request["messages"][0]["content"])
            self.assertEqual(hashlib.sha256(prompt["text"].encode("utf-8")).hexdigest(), prompt["sha256"])
            self.assertEqual(
                hashlib.sha256(request["messages"][0]["content"].encode("utf-8")).hexdigest(),
                fingerprint["effective_prompt_sha256"][request["stage"]],
            )
        self.assertEqual(self.events(store)[-1]["payload"], result)

    def test_five_stages_expose_user_view_only_to_mirror(self):
        adapter, store, transport, packet = self.make_adapter()
        result = adapter.run(packet, "five_stages", include_mirror=True, user_view=USER_VIEW)
        self.assert_success(result, transport, STAGES)
        for index, request in enumerate(transport.requests):
            user_data = json.loads(request["messages"][1]["content"])
            expected_keys = {"frozen_evidence_packet", "previous_stage_outputs", "instructions_are_data"}
            if request["stage"] == "mirror":
                expected_keys.add("user_view")
                self.assertEqual(user_data["user_view"], USER_VIEW)
            else:
                self.assertNotIn("user_view", user_data)
                self.assertNotIn(USER_VIEW, strict_json(request))
                self.assertNotIn(USER_VIEW, strict_json(self.blob(
                    store, result["stage_results"][request["stage"]]["input"])))
            self.assertEqual(set(user_data), expected_keys)
            self.assertIs(user_data["instructions_are_data"], True)
            self.assertEqual(user_data["frozen_evidence_packet"], packet)
            self.assertEqual(set(user_data["previous_stage_outputs"]), set(STAGES[:index]))
            for previous_stage in STAGES[:index]:
                previous = result["stage_results"][previous_stage]
                self.assertEqual(user_data["previous_stage_outputs"][previous_stage], {
                    "raw_text": previous["raw_text"], "structured_output": previous["output"]
                })
        quote = result["mirror"]["sections"][0]["items"][0]["user_quote"]
        self.assertEqual(quote, USER_VIEW)

    def test_each_stage_failure_stops_all_later_stages(self):
        for index, failed_stage in enumerate(STAGES):
            with self.subTest(阶段=failed_stage):
                def fail(request, response):
                    if request["stage"] == failed_stage:
                        response["raw_text"] = "{阶段输出未闭合"
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(fail))
                result = adapter.run(packet, "stage_failure", include_mirror=True, user_view=USER_VIEW)
                self.assert_failed(result, store, transport, "invalid_output", failed_stage, STAGES[:index])
                self.assert_response_preserved(store, transport, failed_stage)

    def test_transport_exceptions_are_sanitized_and_never_retried(self):
        cases = ((TimeoutError, "timeout"), (ConnectionError, "connection_error"),
                 (RuntimeError, "transport_error"))
        for exception_type, code in cases:
            with self.subTest(异常=exception_type.__name__):
                secret = "fixture-private-credential-93817"
                detail = "Authorization: Bearer " + secret + "; password=" + secret
                def fail(request, response):
                    if request["stage"] == "blindspot":
                        raise exception_type(detail)
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(fail))
                result = adapter.run(packet, "exception_redaction", include_mirror=True, user_view=USER_VIEW)
                self.assert_failed(result, store, transport, code, "blindspot", STAGES[:2])
                self.assertEqual(len(transport.responses), 2)
                self.assertNotIn(secret, strict_json(result))
                self.assertNotIn(detail, result["error"]["message"])
                self.assert_not_persisted(store, secret)
                last = self.events(store)[-1]
                self.assertEqual(self.blob(store, last["input_ref"]), transport.requests[-1])
                self.assertIsNone(last["output_ref"])
                self.assertFalse(any(event["event_type"] == "assistant_response_received"
                                     and event["phase"] == "blindspot" for event in self.events(store)))

    def test_invalid_json_is_not_repaired_and_raw_response_survives(self):
        transforms = {
            "破损": lambda raw: raw[:-1],
            "代码围栏": lambda raw: "```json\n" + raw + "\n```",
            "前置说明": lambda raw: "以下为结果：\n" + raw,
            "尾随内容": lambda raw: raw + "\n额外说明",
            "重复字段": lambda raw: raw.replace("{", '{"stage":"search",', 1),
            "非有限数": lambda raw: raw.replace('"schema_version": "1.0"', '"schema_version": NaN', 1),
            "非对象": lambda raw: "[" + raw + "]",
        }
        for label, transform in transforms.items():
            with self.subTest(格式=label):
                def corrupt(request, response):
                    response["raw_text"] = transform(response["raw_text"])
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(corrupt))
                result = adapter.run(packet, "invalid_json")
                self.assert_failed(result, store, transport, "invalid_output", "search")
                self.assert_response_preserved(store, transport, "search")

    def test_output_schema_citations_and_html_are_strict(self):
        cases = ("未知引用", "重复引用", "事实无引用", "HTML", "错阶段", "错包ID",
                 "缺章节", "错章节顺序", "额外字段", "非法认识标签")
        for label in cases:
            with self.subTest(契约=label):
                def corrupt(request, response):
                    value = json.loads(response["raw_text"])
                    item = value["sections"][0]["items"][0]
                    if label == "未知引用":
                        item["citations"] = ["traffic_unknown"]
                    elif label == "重复引用":
                        item["citations"] *= 2
                    elif label == "事实无引用":
                        item["citations"] = []
                    elif label == "HTML":
                        item["text"] = "<strong>合成交通结果</strong>"
                    elif label == "错阶段":
                        value["stage"] = "label"
                    elif label == "错包ID":
                        value["packet_id"] = "wrong_packet"
                    elif label == "缺章节":
                        value["sections"].pop()
                    elif label == "错章节顺序":
                        value["sections"].reverse()
                    elif label == "额外字段":
                        value["repaired"] = True
                    else:
                        item["epistemic_status"] = "certain"
                    response["raw_text"] = strict_json(value)
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(corrupt))
                result = adapter.run(packet, "output_contract")
                self.assert_failed(result, store, transport, "invalid_output", "search")
                self.assert_response_preserved(store, transport, "search")

    def test_mirror_requires_an_exact_original_user_quote(self):
        for label in ("错原句", "缺原句"):
            with self.subTest(引用=label):
                def corrupt(request, response):
                    if request["stage"] == "mirror":
                        value = json.loads(response["raw_text"])
                        item = value["sections"][0]["items"][0]
                        if label == "错原句":
                            item["user_quote"] = "我已经决定只步行，不考虑接驳车。"
                        else:
                            item.pop("user_quote")
                        response["raw_text"] = strict_json(value)
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(corrupt))
                result = adapter.run(packet, "mirror_quote", include_mirror=True, user_view=USER_VIEW)
                self.assert_failed(result, store, transport, "invalid_output", "mirror", STAGES[:4])
                self.assert_response_preserved(store, transport, "mirror")

    def test_finish_reasons_reject_truncation_refusal_and_tools(self):
        cases = {
            "length": "output_truncated",
            "refusal": "refused",
            "content_filter": "refused",
            "tool_calls": "unsupported_tool_call",
            "function_call": "unsupported_tool_call",
            "unexpected": "invalid_response",
        }
        for finish_reason, code in cases.items():
            with self.subTest(结束原因=finish_reason):
                def change(request, response):
                    response["finish_reason"] = finish_reason
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(change))
                result = adapter.run(packet, "finish_reason")
                self.assert_failed(result, store, transport, code, "search")
                self.assert_response_preserved(store, transport, "search")

    def test_response_character_limit_preserves_untruncated_raw_output(self):
        adapter, store, transport, packet = self.make_adapter(max_response_chars=10)
        result = adapter.run(packet, "response_limit")
        self.assert_failed(result, store, transport, "output_truncated", "search")
        self.assertGreater(len(transport.responses[0]["raw_text"]), 10)
        self.assert_response_preserved(store, transport, "search")

    def test_missing_stage_usage_never_produces_fabricated_totals(self):
        def usage(request, response):
            if request["stage"] != "blindspot":
                response["usage"] = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
            return response
        adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(usage))
        result = adapter.run(packet, "partial_usage")
        self.assert_success(result, transport, STAGES[:4])
        self.assertIs(result["usage_complete"], False)
        self.assertIsNone(result["reported_usage"])
        self.assertIsNone(result["stage_results"]["blindspot"]["usage"])
        self.assertEqual(result["stage_results"]["search"]["usage"]["total_tokens"], 14)
        self.assertEqual(result["usage_scope"], "fixture_transport_reported_not_measured_model_tokens")
        self.assertEqual(self.events(store)[-1]["payload"]["reported_usage"], None)

    def test_invalid_usage_rejects_missing_fields_booleans_and_wrong_sum(self):
        valid = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        cases = {
            "空对象": {},
            "缺字段": {"prompt_tokens": 10, "completion_tokens": 4},
            "额外字段": dict(valid, cached_tokens=2),
            "非对象": [10, 4, 14],
            "负数": {"prompt_tokens": -1, "completion_tokens": 4, "total_tokens": 3},
            "浮点数": dict(valid, completion_tokens=4.0),
            "和不一致": dict(valid, total_tokens=15),
            "prompt布尔": {"prompt_tokens": True, "completion_tokens": 4, "total_tokens": 5},
            "completion布尔": {"prompt_tokens": 10, "completion_tokens": False, "total_tokens": 10},
            "total布尔": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": True},
        }
        for label, usage in cases.items():
            with self.subTest(用量=label):
                def change(request, response):
                    response["usage"] = deepcopy(usage)
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(change))
                result = adapter.run(packet, "invalid_usage")
                self.assert_failed(result, store, transport, "invalid_response", "search")
                self.assert_response_preserved(store, transport, "search")

    def test_returned_model_version_and_complete_usage_are_recorded(self):
        expected_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        def metadata(request, response):
            index = STAGES.index(request["stage"]) + 1
            response["provider"] = "fixture-returned-provider"
            response["model"] = f"fixture-v1-build-{index}"
            response["response_id"] = f"fixture-response-{index}"
            response["usage"] = {"prompt_tokens": index * 10, "completion_tokens": index,
                                 "total_tokens": index * 11}
            return response
        adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(metadata))
        result = adapter.run(packet, "returned_versions", include_mirror=True, user_view=USER_VIEW)
        self.assert_success(result, transport, STAGES)
        validated = [event for event in self.events(store) if event["event_type"] == "assistant_stage_validated"]
        for index, (stage, response, event) in enumerate(zip(STAGES, transport.responses, validated), 1):
            row = result["stage_results"][stage]
            self.assertEqual(row["requested_model"], MODEL_SETTINGS["model"])
            self.assertEqual(row["returned_model"], f"fixture-v1-build-{index}")
            self.assertEqual(event["model"], {"provider": response["provider"], "model": response["model"]})
            self.assertEqual(event["usage"], response["usage"])
            self.assertEqual(row["usage"], response["usage"])
            self.assertEqual(self.blob(store, row["response"])["response_id"], f"fixture-response-{index}")
            for key in expected_usage:
                expected_usage[key] += response["usage"][key]
        self.assertIs(result["usage_complete"], True)
        self.assertEqual(result["reported_usage"], expected_usage)

    def test_response_envelope_is_strict_and_recordable_errors_are_retained(self):
        for label in ("缺模型", "额外字段", "空模型", "非字符串ID", "空正文", "非对象"):
            with self.subTest(信封=label):
                def corrupt(request, response):
                    if label == "缺模型":
                        response.pop("model")
                    elif label == "额外字段":
                        response["extra"] = "不在白名单中"
                    elif label == "空模型":
                        response["model"] = " "
                    elif label == "非字符串ID":
                        response["response_id"] = 17
                    elif label == "空正文":
                        response["raw_text"] = " "
                    else:
                        return [response]
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(corrupt))
                result = adapter.run(packet, "response_envelope")
                self.assert_failed(result, store, transport, "invalid_response", "search")
                self.assert_response_preserved(store, transport, "search")

    def test_unrecordable_responses_do_not_leak_sensitive_fields(self):
        for label in ("敏感键", "不可序列化"):
            with self.subTest(响应=label):
                secret = "fixture-private-response-52683"
                def corrupt(request, response):
                    if label == "敏感键":
                        response["Authorization"] = secret
                    else:
                        response["usage"] = {secret}
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(corrupt))
                result = adapter.run(packet, "unrecordable_response")
                self.assert_failed(result, store, transport, "response_not_recordable", "search")
                self.assert_not_persisted(store, secret)
                self.assertNotIn(secret, strict_json(result))
                events = self.events(store)
                self.assertNotIn("assistant_response_received", [event["event_type"] for event in events])
                self.assertIsNone(events[-1]["output_ref"])

    def test_none_expected_bundle_hash_fails_preflight_without_calls(self):
        packet = traffic_packet()
        transport = FixtureTransport()
        store = EventStore(self.temp_root(), STORE_CONFIG, canonical_hash(packet), True,
                           "assistant_fixture_experiment")
        self.stores.append(store)
        adapter = AssistantAdapter(
            store=store, prompt_bundle_path=BUNDLE_PATH, expected_bundle_sha256=None,
            model_settings=deepcopy(MODEL_SETTINGS), transport=transport,
            execution_mode="offline_fixture",
        )
        self.assertIsNone(adapter.expected_bundle_sha)
        result = adapter.run(packet, "none_expected_bundle_hash")
        self.assert_failed(result, store, transport, "prompt_integrity", "preflight")
        self.assertEqual(transport.responses, [])
        events = self.events(store)
        self.assertEqual([event["event_type"] for event in events],
                         ["assistant_task_started", "assistant_task_failed"])
        self.assertEqual(events[-1]["status"], "failed")

    def test_huge_integer_temperature_returns_a_terminal_failure_without_calls(self):
        adapter, store, transport, packet = self.make_adapter(
            settings=dict(MODEL_SETTINGS, temperature=10 ** 400))
        result = adapter.run(packet, "huge_integer_temperature")
        self.assert_failed(result, store, transport, "invalid_input", "preflight")
        self.assertEqual(transport.responses, [])
        events = self.events(store)
        self.assertEqual([event["event_type"] for event in events],
                         ["assistant_task_started", "assistant_task_failed"])
        self.assertEqual(events[-1]["status"], "failed")

    def test_nested_sensitive_response_keys_are_rejected_but_raw_strings_are_preserved(self):
        sensitive_keys = (
            "refresh_token", "id_token", "client_secret", "Set-Cookie", "X-API-Key",
            "private_key", "api_secret", "Cookie", "Proxy-Authorization",
        )
        for index, key in enumerate(sensitive_keys):
            with self.subTest(嵌套敏感键=key):
                secret = f"fixture-nested-private-value-64019-{index}"
                def inject(request, response):
                    response["usage"] = {"metadata": [{"headers": {key: secret}}]}
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(inject))
                result = adapter.run(packet, "nested_sensitive_response")
                self.assert_not_persisted(store, secret)
                self.assertNotIn(secret, strict_json(result))
                self.assert_failed(result, store, transport, "response_not_recordable", "search")
                self.assertEqual(len(transport.responses), 1)
                events = self.events(store)
                self.assertNotIn("assistant_response_received", [event["event_type"] for event in events])
                self.assertIsNone(events[-1]["output_ref"])
                self.assertEqual(self.blob(store, events[-1]["input_ref"]), transport.requests[0])

        # 这些是普通字符串的合成内容，不是响应对象中的敏感字段，必须原样留存。
        ordinary_value = "fixture-ordinary-string-example-82517"
        raw_cases = {
            "普通字符串": "合成普通文本：" + "; ".join(
                f"{key}={ordinary_value}" for key in sensitive_keys),
            "JSON字符串": strict_json({key: ordinary_value for key in sensitive_keys}),
        }
        for label, raw_text in raw_cases.items():
            with self.subTest(保留原文=label):
                def preserve(request, response):
                    response["raw_text"] = raw_text
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(preserve))
                result = adapter.run(packet, "sensitive_words_in_raw_string")
                self.assert_failed(result, store, transport, "invalid_output", "search")
                self.assert_response_preserved(store, transport, "search")
                stored = self.blob(store, self.events(store)[-1]["output_ref"])
                self.assertEqual(stored["raw_text"], raw_text)
                self.assertIn(ordinary_value, stored["raw_text"])

    def test_context_limit_sends_nothing_and_retains_complete_input(self):
        packet = traffic_packet()
        packet["evidence"][0]["text"] += "保留的合成交通尾部标记。" * 200
        packet["evidence"][0]["content_sha256"] = hashlib.sha256(
            packet["evidence"][0]["text"].encode("utf-8")).hexdigest()
        adapter, store, transport, packet = self.make_adapter(packet=packet, max_context_chars=1)
        result = adapter.run(packet, "context_limit")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "context_too_large")
        self.assertEqual(result["failed_stage"], "search")
        self.assertEqual(result["fixture_calls"], 0)
        self.assertEqual(transport.requests, [])
        self.assertEqual(result["completed_stages"], [])
        self.assertIsNone(result["report"])
        self.assertIs(result["demo_fallback"], False)
        events = self.events(store)
        rejected = [event for event in events if event["event_type"] == "assistant_context_rejected"]
        self.assertEqual(len(rejected), 1)
        request = self.blob(store, rejected[0]["input_ref"], rejected[0]["input_sha256"])
        user_data = json.loads(request["messages"][1]["content"])
        self.assertEqual(user_data["frozen_evidence_packet"], packet)
        self.assertIn(self.bundle["prompts"]["search"]["text"], request["messages"][0]["content"])
        actual_chars = sum(len(message["content"]) for message in request["messages"])
        self.assertGreater(actual_chars, 1)
        self.assertEqual(rejected[0]["payload"], {"context_chars": actual_chars, "limit": 1, "truncated": False})
        self.assertEqual(events[-1]["event_type"], "assistant_task_failed")
        self.assertEqual(self.blob(store, events[-1]["input_ref"]), request)
        self.assertIsNone(events[-1]["output_ref"])
        self.assertNotIn("assistant_request_started", [event["event_type"] for event in events])
        self.assertNotIn("assistant_response_received", [event["event_type"] for event in events])

    def test_bad_packet_labels_hash_and_demo_marker_are_rejected(self):
        for label in ("顶层标签", "证据标签", "坏哈希", "改正文", "重复证据ID", "非布尔演示", "未标演示"):
            with self.subTest(证据包=label):
                packet = traffic_packet()
                if label == "顶层标签":
                    packet["stance_label"] = "预先标注"
                elif label == "证据标签":
                    packet["evidence"][0]["credibility"] = "high"
                elif label == "坏哈希":
                    packet["evidence"][0]["content_sha256"] = "0" * 64
                elif label == "改正文":
                    packet["evidence"][0]["text"] += "修改但未重算哈希。"
                elif label == "重复证据ID":
                    packet["evidence"][1]["evidence_id"] = packet["evidence"][0]["evidence_id"]
                elif label == "非布尔演示":
                    packet["is_demo"] = 1
                else:
                    # 合成 URI 只对 is_demo=True 有效；此处首先触发 packet 契约拒绝。
                    packet["is_demo"] = False
                adapter, store, transport, packet = self.make_adapter(packet=packet)
                result = adapter.run(packet, "bad_packet")
                self.assert_failed(result, store, transport, "invalid_input", "preflight")
                self.assertEqual([event["event_type"] for event in self.events(store)],
                                 ["assistant_task_started", "assistant_task_failed"])

    def test_missing_and_invalid_model_settings_never_call_transport(self):
        cases = {"无配置": None, "空配置": {}, "空模型": dict(MODEL_SETTINGS, model=" "),
                 "错误provider类型": dict(MODEL_SETTINGS, provider=1),
                 "额外字段": dict(MODEL_SETTINGS, api_key="fixture-config-private-73591"),
                 "布尔温度": dict(MODEL_SETTINGS, temperature=True),
                 "温度过大": dict(MODEL_SETTINGS, temperature=2.1),
                 "非有限温度": dict(MODEL_SETTINGS, temperature=float("nan")),
                 "布尔token": dict(MODEL_SETTINGS, max_tokens=True),
                 "零token": dict(MODEL_SETTINGS, max_tokens=0),
                 "浮点token": dict(MODEL_SETTINGS, max_tokens=2048.0)}
        for key in MODEL_SETTINGS:
            cases["缺少" + key] = {name: value for name, value in MODEL_SETTINGS.items() if name != key}
        for label, settings in cases.items():
            with self.subTest(配置=label):
                adapter, store, transport, packet = self.make_adapter(settings=settings)
                result = adapter.run(packet, "bad_model_settings")
                self.assert_failed(result, store, transport, "invalid_input", "preflight")
                self.assert_not_persisted(store, "fixture-config-private-73591")

    def test_invalid_mirror_options_are_rejected_before_any_request(self):
        cases = (
            {"user_view": USER_VIEW},
            {"include_mirror": 1, "user_view": USER_VIEW},
            {"include_mirror": True},
            {"include_mirror": True, "user_view": "太短"},
            {"include_mirror": True, "user_view": "<b>我想比较合成交通的换乘时间。</b>"},
        )
        for options in cases:
            with self.subTest(参数=options):
                adapter, store, transport, packet = self.make_adapter()
                result = adapter.run(packet, "bad_mirror_options", **options)
                self.assert_failed(result, store, transport, "invalid_input", "preflight")

    def test_transport_request_mutation_cannot_change_frozen_inputs(self):
        settings = deepcopy(MODEL_SETTINGS)
        def mutate(request, response):
            request["model_settings"].clear()
            request["stage"] = "mutated"
            request["messages"][0]["content"] = "修改传输副本"
            request["messages"].clear()
            return response
        adapter, store, transport, packet = self.make_adapter(
            settings=settings, transport=FixtureTransport(mutate))
        original_packet = deepcopy(packet)
        settings["model"] = "caller-mutated-model"
        settings["max_tokens"] = 0
        result = adapter.run(packet, "request_deepcopy")
        self.assert_success(result, transport, STAGES[:4])
        self.assertEqual(packet, original_packet)
        self.assertEqual(adapter.settings, MODEL_SETTINGS)
        for stage, request in zip(STAGES, transport.requests):
            self.assertEqual(request["model_settings"], MODEL_SETTINGS)
            self.assertEqual(json.loads(request["messages"][1]["content"])["frozen_evidence_packet"], original_packet)
            self.assertEqual(self.blob(store, result["stage_results"][stage]["input"]), request)

    def test_caller_packet_mutation_during_call_cannot_change_later_stages(self):
        packet = traffic_packet()
        frozen = deepcopy(packet)
        def mutate(request, response):
            if request["stage"] == "search":
                packet["evidence"][0]["text"] = "调用方修改了原始对象。"
                packet["evidence"].clear()
                packet["stance_axis"]["name"] = "调用方的新轴"
            return response
        adapter, store, transport, packet = self.make_adapter(packet=packet, transport=FixtureTransport(mutate))
        result = adapter.run(packet, "packet_deepcopy")
        self.assert_success(result, transport, STAGES[:4])
        self.assertNotEqual(packet, frozen)
        self.assertEqual(result["packet_hash"], canonical_hash(frozen))
        for request in transport.requests:
            self.assertEqual(json.loads(request["messages"][1]["content"])["frozen_evidence_packet"], frozen)
        preflight = next(event for event in self.events(store) if event["event_type"] == "assistant_preflight_passed")
        self.assertEqual(self.blob(store, preflight["payload"]["packet"]), frozen)

    def test_prompt_hash_corruption_and_missing_bundle_fail_closed(self):
        for label in ("预期哈希错误", "原文哈希错误", "bundle哈希错误", "文件缺失"):
            with self.subTest(提示词=label):
                path = BUNDLE_PATH
                expected_sha = self.bundle_sha
                if label == "预期哈希错误":
                    expected_sha = "0" * 64
                elif label == "文件缺失":
                    path = self.temp_root() / "missing_bundle.json"
                else:
                    value = deepcopy(self.bundle)
                    if label == "原文哈希错误":
                        value["prompts"]["search"]["text"] += "合成测试篡改。"
                        value["bundle_sha256"] = canonical_hash({
                            key: item for key, item in value.items() if key != "bundle_sha256"
                        })
                        expected_sha = value["bundle_sha256"]
                    else:
                        value["source_entry_sha256"] = "0" * 64
                    path = self.temp_root() / "corrupt_bundle.json"
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(strict_json(value))
                adapter, store, transport, packet = self.make_adapter(bundle_path=path, expected_sha=expected_sha)
                result = adapter.run(packet, "prompt_integrity")
                self.assert_failed(result, store, transport, "prompt_integrity", "preflight")

    def test_store_transport_and_packet_manifest_mismatches_stop_preflight(self):
        for label in ("非演示store", "transport模式错误", "transport无模式", "manifest不一致", "无complete"):
            with self.subTest(模式=label):
                transport = FixtureTransport()
                options = {}
                if label == "非演示store":
                    options["store_demo"] = False
                elif label == "transport模式错误":
                    transport.execution_mode = "research"
                elif label == "transport无模式":
                    transport.execution_mode = None
                elif label == "manifest不一致":
                    options["manifest_hash"] = "0" * 64
                else:
                    transport.complete = None
                adapter, store, transport, packet = self.make_adapter(transport=transport, **options)
                result = adapter.run(packet, "mode_mismatch")
                code = "invalid_input" if label == "无complete" else "mode_mismatch"
                self.assert_failed(result, store, transport, code, "preflight")
                self.assertIs(result["is_demo"], store.is_demo)

    def test_response_demo_flag_requires_literal_true(self):
        for marker in (False, 1, "true", None):
            with self.subTest(响应演示标记=marker):
                def change(request, response):
                    response["is_demo"] = marker
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(change))
                result = adapter.run(packet, "response_demo")
                self.assert_failed(result, store, transport, "mode_mismatch", "search")
                self.assert_response_preserved(store, transport, "search")

    def test_research_mode_is_always_disabled_with_zero_calls(self):
        for transport_mode in ("offline_fixture", "research"):
            with self.subTest(调用端模式=transport_mode):
                transport = FixtureTransport()
                transport.execution_mode = transport_mode
                adapter, store, transport, packet = self.make_adapter(
                    transport=transport, execution_mode="research",
                    settings=dict(MODEL_SETTINGS, max_tokens=1000000))
                result = adapter.run(packet, "research_disabled", include_mirror=True, user_view=USER_VIEW)
                self.assert_failed(result, store, transport, "live_disabled", "preflight")
                self.assertEqual([event["event_type"] for event in self.events(store)],
                                 ["assistant_task_started", "assistant_task_failed"])
                self.assertIs(self.events(store)[0]["payload"]["live_transport_enabled"], False)

    def test_task_id_cannot_be_replayed_even_with_a_new_adapter_and_store(self):
        for first_fails in (False, True):
            with self.subTest(首次失败=first_fails):
                def behavior(request, response):
                    if first_fails:
                        raise TimeoutError("合成超时")
                    return response
                adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(behavior))
                first = adapter.run(packet, "never_replay")
                self.assertEqual(first["status"], "failed" if first_fails else "succeeded")
                files_before = {path.relative_to(store.root).as_posix(): path.read_bytes()
                                for path in store.root.rglob("*") if path.is_file()}
                calls_before = len(transport.requests)
                with self.assertRaises(AdapterError) as same_error:
                    adapter.run(packet, "never_replay")
                self.assertEqual(same_error.exception.code, "duplicate_task")
                self.assertEqual(len(transport.requests), calls_before)
                reopened = EventStore(store.root, STORE_CONFIG, canonical_hash(packet), True,
                                      "assistant_fixture_experiment")
                fresh_transport = FixtureTransport()
                fresh = AssistantAdapter(reopened, BUNDLE_PATH, self.bundle_sha, deepcopy(MODEL_SETTINGS),
                                         fresh_transport, execution_mode="offline_fixture")
                with self.assertRaises(AdapterError) as fresh_error:
                    fresh.run(packet, "never_replay")
                self.assertEqual(fresh_error.exception.code, "duplicate_task")
                self.assertEqual(fresh_transport.requests, [])
                files_after = {path.relative_to(store.root).as_posix(): path.read_bytes()
                               for path in store.root.rglob("*") if path.is_file()}
                self.assertEqual(files_after, files_before)

    def test_raw_requests_responses_and_event_references_are_complete(self):
        adapter, store, transport, packet = self.make_adapter()
        result = adapter.run(packet, "audit_references", include_mirror=True, user_view=USER_VIEW)
        self.assert_success(result, transport, STAGES)
        events = self.events(store)
        expected_types = ["assistant_task_started", "assistant_preflight_passed"]
        expected_types += [kind for _ in STAGES for kind in
                           ("assistant_request_started", "assistant_response_received", "assistant_stage_validated")]
        expected_types.append("assistant_task_completed")
        self.assertEqual([event["event_type"] for event in events], expected_types)
        for index, event in enumerate(events):
            self.assertEqual(event["logical_seq"], index + 1)
            self.assertEqual(event["task_id"], "audit_references")
            self.assertIs(event["is_demo"], True)
            self.assertEqual(event["attempt_id"], 1)
            self.assertEqual(event["previous_event_hash"], events[index - 1]["event_hash"] if index else None)
            self.assertEqual(event["event_hash"], canonical_hash({
                key: value for key, value in event.items() if key != "event_hash"
            }))
            if index:
                self.assertEqual(event["parent_event_ids"], [events[index - 1]["event_id"]])
            for prefix in ("input", "output"):
                if event.get(prefix + "_ref") is not None:
                    self.blob(store, event[prefix + "_ref"], event.get(prefix + "_sha256"))
        preflight = events[1]["payload"]
        self.assertEqual(self.blob(store, preflight["packet"]), packet)
        self.assertEqual(self.blob(store, preflight["prompt_bundle"]), self.bundle)
        self.assertEqual(preflight["model_settings"], MODEL_SETTINGS)
        for index, stage in enumerate(STAGES):
            started, received, validated = events[2 + 3 * index:5 + 3 * index]
            row = result["stage_results"][stage]
            self.assertEqual(self.blob(store, row["input"]), transport.requests[index])
            self.assertEqual(self.blob(store, row["response"]), transport.responses[index])
            self.assertEqual(row["raw_text"], transport.responses[index]["raw_text"])
            self.assertEqual(row["output"], json.loads(row["raw_text"]))
            self.assertEqual(started["input_ref"], row["input"]["ref"])
            self.assertEqual(received["input_ref"], row["input"]["ref"])
            self.assertEqual(received["output_ref"], row["response"]["ref"])
            self.assertEqual(received["payload"]["response"], row["response"])
            self.assertEqual(validated["payload"]["response"], row["response"])
            self.assertEqual(validated["payload"]["structured_output"], row["output"])
            self.assertEqual(validated["payload"]["citation_audit"], row["citation_audit"])
            self.assertEqual(started["model"], MODEL_SETTINGS)
            self.assertIs(started["payload"]["sent_to_live_model"], False)
            self.assertIs(started["payload"]["truncated"], False)
            self.assertEqual(started["payload"]["original_prompt_sha256"], self.bundle["prompts"][stage]["sha256"])
            self.assertEqual(started["payload"]["packet_hash"], canonical_hash(packet))
        self.assertEqual(events[-1]["payload"], result)
        self.assertEqual(verify_events(store.root)["event_count"], 18)

    def test_valid_json_whitespace_escapes_and_text_are_never_repaired(self):
        def unusual_json(request, response):
            value = json.loads(response["raw_text"])
            value["sections"][0]["items"][0]["text"] = "合成对比值 <2；保留 `普通文本` 与换行\n以及双引号\"。"
            response["raw_text"] = " \r\n" + json.dumps(
                value, ensure_ascii=True, indent=2, allow_nan=False) + "\r\n\t "
            return response
        adapter, store, transport, packet = self.make_adapter(transport=FixtureTransport(unusual_json))
        result = adapter.run(packet, "valid_json_unchanged", include_mirror=True, user_view=USER_VIEW)
        self.assert_success(result, transport, STAGES)
        for index, stage in enumerate(STAGES):
            original = transport.responses[index]["raw_text"]
            row = result["stage_results"][stage]
            self.assertEqual(row["raw_text"], original)
            self.assertEqual(row["output"], json.loads(original))
            self.assertNotEqual(original, strict_json(json.loads(original)))
            self.assertEqual(self.blob(store, row["response"])["raw_text"], original)
            for later_request in transport.requests[index + 1:]:
                prior = json.loads(later_request["messages"][1]["content"])["previous_stage_outputs"][stage]
                self.assertEqual(prior["raw_text"], original)
                self.assertEqual(prior["structured_output"], json.loads(original))

    def test_adapter_does_not_use_simulated_agents_or_topic_matching(self):
        source = Path(assistant_module.__file__).read_text(encoding="utf-8")
        forbidden = {"SIMULATED_AGENTS", "matchTopic"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden)
            elif isinstance(node, ast.alias):
                self.assertNotIn(node.name.split(".")[-1], forbidden)
                self.assertNotIn(node.asname, forbidden)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for name in forbidden:
                    self.assertNotIn(name, node.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
