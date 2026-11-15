"""助手适配层离线验收。所有返回内容均显式工程夹具，不是模型回复。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
from unittest.mock import patch

from .assistant import AssistantAdapter, adapter_fingerprint
from .contracts import STAGE_SECTIONS
from .prompts import load_prompt_bundle, verify_original_sources
from .runtime import EventStore, canonical_hash, utc_now, verify_events, write_json_new

NOTICE = "五阶段离线契约验收，不是真实模型预实验；没有联网检索或认知效果测量。"
FIXTURE_SETTINGS = {"provider": "offline_fixture", "model": "contract-fixture-v1", "temperature": 0, "max_tokens": 2000}
FIXTURE_VIEW = "我认为应先核对纸环线虚构座位规则，再讨论是否增加班次。"
COMPONENTS = ("cocoon/assistant.py", "cocoon/contracts.py", "cocoon/prompts.py", "cocoon/runtime.py", "cocoon/assistant_checks.py", "cocoon/mimo.py", "cocoon/budget.py", "cocoon/mimo_probe.py")
SCENARIOS = (
    ("five_stage_success", None, None, True, "succeeded", None, 5),
    ("four_stage_success", None, None, False, "succeeded", None, 4),
    ("search_timeout", "search", "timeout", True, "failed", "timeout", 1),
    ("label_connection", "label", "connection", True, "failed", "connection_error", 2),
    ("blindspot_invalid_json", "blindspot", "invalid_json", True, "failed", "invalid_output", 3),
    ("integrate_unknown_citation", "integrate", "unknown_citation", True, "failed", "invalid_output", 4),
    ("mirror_quote_mismatch", "mirror", "bad_quote", True, "failed", "invalid_output", 5),
    ("integrate_truncated", "integrate", "truncated", True, "failed", "output_truncated", 4),
    ("label_refused", "label", "refused", True, "failed", "refused", 2),
    ("context_overflow", None, None, True, "failed", "context_too_large", 0),
    ("private_label_rejected", None, None, True, "failed", "invalid_input", 0),
    ("live_mode_blocked", None, None, True, "failed", "live_disabled", 0),
    ("error_message_private", "search", "private_error", True, "failed", "transport_error", 1),
)


def component_hashes(root):
    return {name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in COMPONENTS}


def fixture_packet():
    text = "【工程夹具，虚构材料】纸环线每班四个座位，每个观察周期两班，共八个座位；这一规则与现实无关。"
    return {"schema_version": "1.0", "packet_id": "ENGINEERING_PACKET", "topic_id": "SYNTHETIC",
            "topic": "仅用于工程测试的纸环线虚构班次规则",
            "stance_axis": {"name": "虚构班次调整", "negative_label": "维持班次", "positive_label": "增加班次"},
            "evidence": [{"evidence_id": "E001", "text": text, "source_url": "synthetic://fixture/traffic-rule",
                          "source_family_id": "F001", "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}],
            "is_demo": True}


class FixtureTransport:
    """只计算确定性测试响应，不连接服务；严禁用于正式实验结果。"""
    execution_mode = "offline_fixture"
    def __init__(self, fail_stage=None, failure=None):
        self.fail_stage, self.failure = fail_stage, failure
        self.requests = []

    def complete(self, request):
        self.requests.append(deepcopy(request))
        stage = request["stage"]
        data = json.loads(request["messages"][1]["content"])
        packet = data["frozen_evidence_packet"]
        if stage == self.fail_stage:
            if self.failure == "timeout":
                raise TimeoutError("injected timeout")
            if self.failure == "connection":
                raise ConnectionError("injected connection error")
            if self.failure == "private_error":
                raise RuntimeError("CONFIDENTIAL_TEST_SENTINEL_NOT_TO_BE_LOGGED")
        sections = [{"name": name, "items": []} for name in STAGE_SECTIONS[stage]]
        item = {"text": "【合成夹具响应，非模型生成】冻结虚构规则给出的总座位数为八。",
                "citations": [packet["evidence"][0]["evidence_id"]], "epistemic_status": "evidence_based"}
        if stage == "mirror":
            item["user_quote"] = data["user_view"]
        sections[0]["items"].append(item)
        if stage == self.fail_stage and self.failure == "unknown_citation":
            item["citations"] = ["UNKNOWN_EVIDENCE"]
        if stage == self.fail_stage and self.failure == "bad_quote":
            item["user_quote"] = "并非用户原句的错误测试引文"
        structured = {"schema_version": "1.0", "stage": stage, "packet_id": packet["packet_id"], "sections": sections}
        raw = json.dumps(structured, ensure_ascii=False)
        if stage == self.fail_stage and self.failure == "invalid_json":
            raw = "{invalid_json_for_test_only"
        finish = "stop"
        if stage == self.fail_stage and self.failure == "truncated":
            finish = "length"
        if stage == self.fail_stage and self.failure == "refused":
            finish = "content_filter"
        return {"raw_text": raw, "provider": "offline_fixture", "model": "contract-fixture-v1",
                "response_id": request["request_id"] + ":fixture", "finish_reason": finish,
                "is_demo": True, "usage": None}


def _verify_case_trace(directory, result, spec, fingerprint):
    """从记录推导实际阶段/调用，不采信终态自报数字。不是数字签名认证。"""
    directory = Path(directory)
    with (directory / "events/events.jsonl").open("rb") as file:
        rows = [json.loads(line) for line in file]
    name, fail_stage, failure, mirror, expected_status, expected_error, expected_calls = spec
    stages = list(STAGE_SECTIONS) if mirror else list(STAGE_SECTIONS)[:4]
    request_stages = stages[:expected_calls]
    validated_stages = stages if expected_status == "succeeded" else stages[:max(expected_calls - 1, 0)]
    expected_types = ["assistant_task_started"]
    if name not in ("private_label_rejected", "live_mode_blocked"):
        expected_types.append("assistant_preflight_passed")
    for stage in request_stages:
        expected_types.append("assistant_request_started")
        if stage == fail_stage and failure in ("timeout", "connection", "private_error"):
            break
        expected_types.append("assistant_response_received")
        if stage in validated_stages:
            expected_types.append("assistant_stage_validated")
    if name == "context_overflow":
        expected_types.append("assistant_context_rejected")
    expected_types.append("assistant_task_completed" if expected_status == "succeeded" else "assistant_task_failed")
    if [row["event_type"] for row in rows] != expected_types:
        raise ValueError("事件轨迹没有完整记录预期执行过程")
    if any(row.get("task_id") != result["task_id"] or row.get("attempt_id") != 1 or row["is_demo"] is not True for row in rows):
        raise ValueError("轨迹任务、尝试次数或模式不一致")
    for i, row in enumerate(rows):
        parents = [] if i == 0 else [rows[i - 1]["event_id"]]
        if row.get("parent_event_ids", []) != parents:
            raise ValueError("阶段因果链不连续")
    preflight = [row for row in rows if row["event_type"] == "assistant_preflight_passed"]
    if preflight and preflight[0]["payload"]["fingerprint"] != fingerprint:
        raise ValueError("本次运行未绑定当前适配指纹")
    if result.get("formal_eligible") is not False or result.get("actual_model_calls") != 0 or result.get("demo_fallback") is not False:
        raise ValueError("终态资格/调用声明有误")
    requests = [row for row in rows if row["event_type"] == "assistant_request_started"]
    validated = [row for row in rows if row["event_type"] == "assistant_stage_validated"]
    if [row["phase"] for row in requests] != request_stages or [row["phase"] for row in validated] != validated_stages:
        raise ValueError("阶段顺序或失败停止点有误")
    if result["completed_stages"] != validated_stages or set(result["stage_results"]) != set(validated_stages):
        raise ValueError("终态与实际完成阶段不匹配")
    for request in requests:
        stage = request["phase"]
        original = json.loads((directory / request["input_ref"]).read_text(encoding="utf-8"))
        if (original["stage"] != stage or original["is_demo"] is not True
                or request["payload"]["effective_prompt_sha256"] != fingerprint["effective_prompt_sha256"][stage]):
            raise ValueError("请求的阶段/提示词指纹不一致")
        response_rows = [row for row in rows if row["event_type"] == "assistant_response_received" and row["phase"] == stage]
        for response in response_rows:
            if response["input_ref"] != request["input_ref"] or response["input_sha256"] != request["input_sha256"]:
                raise ValueError("响应没有绑定同阶段输入")
        if stage in validated_stages:
            saved = result["stage_results"][stage]
            response = response_rows[0]
            original_response = json.loads((directory / response["output_ref"]).read_text(encoding="utf-8"))
            verified_output = next(row["payload"] for row in validated if row["phase"] == stage)
            if (saved["input"]["ref"] != request["input_ref"] or saved["response"]["ref"] != response["output_ref"]
                    or saved["raw_text"] != original_response["raw_text"]
                    or saved["output"] != verified_output["structured_output"]
                    or saved["citation_audit"] != verified_output["citation_audit"]):
                raise ValueError("成功产物与原始输入/输出或校验记录不一致")
    if rows[-1]["payload"] != result:
        raise ValueError("缺少与结果一致的终态")


def inspect_adapter(config, root):
    """只在原件/提示词/组件与成功验收记录全部吻合时报告离线通过。"""
    root = Path(root)
    a = config.get("assistant", {})
    state = {"code_available": (root / "cocoon/assistant.py").is_file(), "prompts_frozen": False,
             "source_verified": False, "offline_verified": False, "live_verified": False,
             "formal_eligible": False, "notice": NOTICE, "errors": []}
    try:
        bundle = load_prompt_bundle(a["prompt_bundle_path"], a["prompt_bundle_sha256"])
        source = verify_original_sources(bundle, a["entry_path"], a["source_path"])
        state.update(prompts_frozen=True, source_verified=True, prompt_count=source["stage_count"],
                     bundle_sha256=bundle["bundle_sha256"], fingerprint=adapter_fingerprint(bundle))
        check_dir = a.get("offline_check_directory")
        if not check_dir:
            return state
        folder = Path(check_dir).resolve()
        if not folder.is_relative_to((root / "experiment/adapter_checks").resolve()):
            raise ValueError("验收目录越界")
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        if (summary.get("status") != "passed" or summary.get("component_hashes") != component_hashes(root)
                or summary.get("prompt_bundle_sha256") != bundle["bundle_sha256"]
                or summary.get("is_demo") is not True or summary.get("actual_model_calls") != 0
                or summary.get("run_id") != folder.name):
            raise ValueError("验收状态、模式或组件指纹不匹配")
        saved_bundle = load_prompt_bundle(folder / "prompt_bundle.json", bundle["bundle_sha256"])
        saved_fingerprint = json.loads((folder / "adapter_fingerprint.json").read_text(encoding="utf-8"))
        if saved_bundle != bundle or saved_fingerprint != state["fingerprint"] or summary.get("fingerprint") != state["fingerprint"]:
            raise ValueError("运行副本的版本指纹不一致")
        expected = {row[0]: row for row in SCENARIOS}
        if {case["name"] for case in summary["scenarios"]} != set(expected) or len(summary["scenarios"]) != len(expected):
            raise ValueError("验收场景集合不完整")
        for case in summary["scenarios"]:
            name = case["name"]
            current = verify_events(folder / name)
            result = json.loads((folder / name / "result.json").read_text(encoding="utf-8"))
            spec = expected[name]
            if (not case["passed"] or current != case["verification"] or result["status"] != spec[4]
                    or result.get("error", {}).get("code") != spec[5] or result["fixture_calls"] != spec[6]):
                raise ValueError("场景结果与日志不匹配")
            _verify_case_trace(folder / name, result, spec, state["fingerprint"])
            if name == "five_stage_success" and summary.get("successful_five_stage_example") != result:
                raise ValueError("示例产物与实际日志不匹配")
        if summary.get("fixture_calls") != sum(case["fixture_calls"] for case in summary["scenarios"]):
            raise ValueError("调用总数与逐场景记录不匹配")
        state.update(offline_verified=True, check_directory=str(folder), scenario_count=len(summary["scenarios"]),
                     fixture_calls=summary["fixture_calls"], recorded_at_utc=summary["completed_at_utc"],
                     actual_model_calls=0, summary=summary)
    except (KeyError, ValueError, TypeError, OSError, IndexError):
        state["errors"].append("原件、提示词或离线验收记录缺失/变化，须重新校验，不视为通过。")
    return state


def run_adapter_checks(config, root):
    root = Path(root).resolve()
    if not (root / "experiment").is_dir():
        raise FileNotFoundError("experiment父目录不存在")
    settings = config["assistant"]
    bundle = load_prompt_bundle(settings["prompt_bundle_path"], settings["prompt_bundle_sha256"])
    before = verify_original_sources(bundle, settings["entry_path"], settings["source_path"])
    fingerprint = adapter_fingerprint(bundle)
    parent = root / "experiment/adapter_checks"
    parent.mkdir(exist_ok=True)
    run_id = "adapter_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = parent / run_id
    out.mkdir()
    write_json_new(out / "prompt_bundle.json", bundle)
    write_json_new(out / "adapter_fingerprint.json", fingerprint)
    packet = fixture_packet()
    write_json_new(out / "fixture_packet.json", packet)
    cases = []
    success = None
    for name, fail_stage, failure, mirror, expected_status, expected_error, expected_calls in SCENARIOS:
        directory = out / name
        directory.mkdir()
        transport = FixtureTransport(fail_stage, failure)
        frozen = deepcopy(packet)
        if name == "private_label_rejected":
            frozen["gold"] = "must_not_reach_transport"
        mode = "research" if name == "live_mode_blocked" else "offline_fixture"
        params = {"execution_mode": mode, "scenario": name, "fixture_settings": FIXTURE_SETTINGS,
                  "fingerprint": fingerprint}
        store = EventStore(directory, params, canonical_hash(frozen), True, f"{run_id}:{name}")
        adapter = AssistantAdapter(store, settings["prompt_bundle_path"], settings["prompt_bundle_sha256"],
                 FIXTURE_SETTINGS, transport, execution_mode=mode,
                 max_context_chars=1 if name == "context_overflow" else 80000)
        # 明确禁用网络，并验证所有路径均可在网络不可用时完成。
        with patch("socket.socket", side_effect=AssertionError("network_disabled_for_offline_check")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network_disabled_for_offline_check")):
            result = adapter.run(frozen, f"{run_id}:{name}", include_mirror=mirror,
                                 user_view=FIXTURE_VIEW if mirror else None)
        write_json_new(directory / "result.json", result)
        verified = verify_events(directory)
        secret_absent = True
        for file in (directory / "raw/blobs").iterdir():
            if b"CONFIDENTIAL_TEST_SENTINEL_NOT_TO_BE_LOGGED" in file.read_bytes():
                secret_absent = False
        passed = (result["status"] == expected_status and result.get("error", {}).get("code") == expected_error
                  and len(transport.requests) == expected_calls and result["fixture_calls"] == expected_calls
                  and result["actual_model_calls"] == 0 and result["demo_fallback"] is False and secret_absent)
        cases.append({"name": name, "passed": passed, "expected_status": expected_status,
                      "actual_status": result["status"], "expected_error": expected_error,
                      "actual_error": result.get("error", {}).get("code"),
                      "fixture_calls": len(transport.requests), "actual_model_calls": 0,
                      "completed_stages": result["completed_stages"], "verification": verified,
                      "directory": name, "secret_exception_text_absent": secret_absent})
        if name == "five_stage_success":
            success = result
    after = verify_original_sources(bundle, settings["entry_path"], settings["source_path"])
    if before != after:
        raise AssertionError("原件在验收时发生变化")
    passed = all(case["passed"] for case in cases)
    summary = {"schema_version": "1.0", "run_id": run_id, "run_directory": str(out),
               "status": "passed" if passed else "failed", "notice": NOTICE, "is_demo": True,
               "formal_eligible": False, "completed_at_utc": utc_now(), "component_hashes": component_hashes(root),
               "prompt_bundle_sha256": bundle["bundle_sha256"], "source_verification": after,
               "fingerprint": fingerprint, "scenarios": cases,
               "fixture_calls": sum(case["fixture_calls"] for case in cases), "actual_model_calls": 0,
               "actual_cost_cny": 0, "original_files_unchanged": True, "successful_five_stage_example": success,
               "remaining": ["真实API传输与预算执行", "真实证据来源及双人审核", "语义引用审查",
                             "OASIS接入与权限隔离", "I4/I5真实交付及等证据语义审计", "模型预实验"]}
    write_json_new(out / "summary.json", summary)
    return summary
