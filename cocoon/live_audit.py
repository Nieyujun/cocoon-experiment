"""只读复核 MiMo 四阶段探测，并对原模型整合报告做离线剂量检查。

仅证明所提供本地文件在当前契约下相互一致，不是数字签名、外部网络真实性、
来源真实性或账单证明。可重写整个日志的人也能重算所有普通 SHA-256；离线
mockrun 即使声明 is_demo=False，也不能凭本模块变成真实网络调用的证明。
本模块不读凭据、不调用传输、不重试、不修日志、不写文件、不产生 I5。
"""

from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re

from .assistant import ADAPTER_VERSION, adapter_fingerprint, effective_system_prompt
from .contracts import parse_stage_output, validate_packet
from .dosage import DISPLAY_POLICY, RESPONSE_PROMPT, compact_evidence
from .mimo import MIMO_MODEL
from .prompts import STAGES, load_prompt_bundle
from .runtime import (
    _existing_root, _log_path, _parse_json, _reject_sensitive_keys, _verify_blob,
    canonical_hash, verify_events,
)


def prepare_live_review(run_directory, packet_path, expected_bundle_path, expected_bundle_sha,
                        source_index, semantic_path, ledger_path):
    """把已核验轨迹、逐项AI意见和本机台账关联；不写回原始结果，不替代人审。"""
    from collections import Counter
    from decimal import Decimal
    from .budget import BudgetLedger
    from .runtime import utc_now
    audit = audit_live_run(run_directory, packet_path, expected_bundle_path, expected_bundle_sha, source_index)
    semantic_bytes = Path(semantic_path).read_bytes()
    semantic = _parse_json(semantic_bytes)
    if (semantic.get("review_type") != "independent_ai_not_human" or semantic.get("run_id") != audit["run_id"]
            or semantic.get("summary_sha256") != audit["summary_sha256"]
            or semantic.get("packet_sha256") != audit["expected_packet_hash"]
            or type(semantic.get("human_reviews_completed")) is not int or semantic["human_reviews_completed"] != 0
            or semantic.get("formal_eligible") is not False or semantic.get("no_model_calls") is not True):
        raise ValueError("AI语义审阅身份或输入哈希未匹配")
    expected_items = {}
    for stage, row in audit["stage_results"].items():
        if row["structure_passed"]:
            for section in row["structured_output"]["sections"]:
                for index, item in enumerate(section["items"]):
                    expected_items[stage, section["name"], index] = item
    seen = set()
    verdicts = ("supported", "acceptable_gap_or_question", "unsupported_fact", "overstated", "needs_review")
    for item in semantic["items"]:
        key = item["stage"], item["section"], item["item_index"]
        if type(key[2]) is not int or key not in expected_items or key in seen:
            raise ValueError("AI审核条目重复、缺失或越界")
        original = expected_items[key]
        if any(item[k] != original[k] for k in ("text", "citations", "epistemic_status")):
            raise ValueError("AI审核引用的不是原模型逐项文本")
        if item["verdict"] not in verdicts or item["severity"] not in ("none", "moderate", "high"):
            raise ValueError("AI审核分类不合法")
        seen.add(key)
    if seen != set(expected_items) or semantic["items_reviewed"] != len(seen):
        raise ValueError("AI审核未覆盖全部输出项")
    counts = Counter(item["verdict"] for item in semantic["items"])
    for verdict in verdicts:
        if semantic["counts"][verdict] != counts[verdict]:
            raise ValueError("AI审核分类计数不一致")
    problems = counts["unsupported_fact"] + counts["overstated"] + counts["needs_review"]
    if semantic["counts"]["problem_items"] != problems:
        raise ValueError("AI审核问题总数不一致")
    if semantic["status"] != ("protocol_deviations_found" if problems else "pass_with_limitations"):
        raise ValueError("AI审核状态与逐条结论不一致")
    ledger_path = Path(ledger_path)
    if not ledger_path.is_file():
        raise ValueError("需要实际已有预算台账，不自动创建新额度")
    ledger = BudgetLedger(ledger_path, "5.00")
    budget = ledger.status()
    if budget.get("total_cost_cny") is None:
        # 存在未决（如步骤40中断持有）预留时总额未定：保留原始 null，展示值如实写明“已结算＋待对账”。
        budget = {**budget, "total_cost_cny_raw": None,
                  "total_cost_cny": (budget["charged_cny"] + "（已结算；另有未决预留¥"
                                     + budget["reserved_cny"] + "待对账）")}
    ledger_bytes = ledger_path.read_bytes()
    records = [_parse_json(line) for line in ledger_bytes.splitlines()]
    request_ids = {row["metadata"]["request_id"] for row in audit["stage_results"].values()
                   if row["metadata"]["request_id"] is not None}
    settlements = [row for row in records if row["type"] == "settle" and row["request_id"] in request_ids]
    if len({row["request_id"] for row in settlements}) != len(settlements):
        raise ValueError("台账请求重复结算")
    for settlement in settlements:
        stage = next(row for row in audit["stage_results"].values() if row["metadata"]["request_id"] == settlement["request_id"])
        if stage["usage"] != {k: settlement["usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}:
            raise ValueError("实际响应与计费台账用量不一致")
    known_cost = sum((Decimal(row["charged_cny"]) for row in settlements), Decimal("0"))
    all_settled = len(settlements) == audit["actual_http_attempts"]
    if ledger_path.read_bytes() != ledger_bytes:
        raise ValueError("审阅中台账变动，请在无写入时重验")
    review = {"schema_version": "1.0", "created_at_utc": utc_now(),
              "status": "not_ready_for_intervention", "audit": audit, "semantic_review": semantic,
              "semantic_review_sha256": hashlib.sha256(semantic_bytes).hexdigest(),
              "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
              "billing": {"run_known_cost_cny": str(known_cost),
                          "run_all_attempts_settled": all_settled,
                          "run_cost_cny": str(known_cost) if all_settled else None,
                          "cumulative": budget, "basis": "本机台账按公开单价及usage保守记账，非平台最终账单"},
              "problem_items": problems, "human_reviews_completed": 0, "formal_eligible": False,
              "next_changes_not_executed": [
                  "在新适配版本中禁止仅凭来源域名认证权威或推断机构立场。",
                  "无自然用户观点时禁止推断个体采信倾向；科学机制与价值偏好分开。",
                  "保留原文的主要、可能及不能仅据此推断，不改成确定结果。",
                  "内部分析报告与受试投递摘要分开；摘要须另列生成和审计记录，不能剪掉旧报告冒充原输出。",
                  "在证据块418字符及公共提示39字符后，新增投递段应为143–343字符；完整镜像仍需真实T1观点。"
              ],
              "original_run_modified": False, "model_calls_during_review": 0}
    _reject_sensitive_keys(review)
    return review


def audit_live_run(run_directory, packet_path, expected_bundle_path,
                   expected_bundle_sha, source_index) -> dict:
    """审核一个已结束的 assistant_probe；唯一副作用是读取指定本地文件。

    expected_bundle_sha 是 load_prompt_bundle 使用的内容哈希，不是文件字节
    哈希。packet_path 是调用方指定的冻结包；source_index 接受
    source_index_from_review 的返回对象，不读取旧编辑稿或自动查找来源表。
    调用方须自行信任/冻结这些外部输入；本接口没有独立的 expected_packet_sha。

    合法的前缀失败仍返回 audit_status=passed、run_status=failed 和原终态
    错误。记录缺失、串阶段、声明矛盾或哈希错误抛出带 live_audit 错误码的
    ValueError，不把审核失败伪装成探测失败。应在写入者关闭后审核；读取前后
    复核只能发现普通并发变化，不提供文件系统锁或对抗性快照保证。

    stage_results 总是包含四个阶段；只有有 validated 事件且独立复核通过
    的 integrate 才能渲染。rendered_items.item_index 从 0 开始，text 原样
    保留。候选全文包含证据、标题、标签、引用、原报告和同一 RESPONSE_PROMPT，
    字数为 Python len；超限只报告 deviations，不截断、压缩或重新生成。
    """
    def require(condition, code):
        if not condition:
            raise ValueError("live_audit:" + code)

    def equal(actual, expected, code):
        # 规范 JSON 比较区分 True 与 1，且不依赖对象键的存储顺序。
        require(canonical_hash(actual) == canonical_hash(expected), code)

    def strict_json(data):
        value = _parse_json(data)
        canonical_hash(value)  # 同时拒绝 1e999 等浮点溢出。
        _reject_sensitive_keys(value)
        return value

    def nonnegative_int(value):
        return type(value) is int and value >= 0

    def descriptor(event, prefix):
        ref = event[prefix + "_ref"]
        digest = event[prefix + "_sha256"]
        require(type(ref) is str and type(digest) is str, "invalid_reference")
        return {"ref": ref, "sha256": digest}

    def blob(ref):
        require(type(ref) is dict and set(ref) == {"ref", "sha256"}, "invalid_descriptor")
        return _verify_blob(root, ref)

    def usage_info(envelope):
        value = envelope.get("usage") if type(envelope) is dict else None
        basic = None
        code = "missing_usage" if value is None else "invalid_usage"
        if (type(value) is dict and all(nonnegative_int(value.get(k)) for k in token_keys)
                and value["total_tokens"] == value["prompt_tokens"] + value["completion_tokens"]):
            basic = {k: value[k] for k in token_keys}
            code = None
        # 仅导出 token 数字/空值和 token 详情字典，不扩散未知信封或 HTTP headers。
        safe = {}
        if type(value) is dict:
            for name, count in value.items():
                if re.fullmatch(r"[a-z_]+_tokens", name) and (count is None or nonnegative_int(count)):
                    safe[name] = count
                elif re.fullmatch(r"[a-z_]+_tokens_details", name):
                    if count is None:
                        safe[name] = None
                    elif type(count) is dict:
                        safe[name] = {k: v for k, v in count.items()
                                      if re.fullmatch(r"[a-z_]+_tokens", k)
                                      and (v is None or nonnegative_int(v))}
        return basic, safe, code

    def normalize_http(envelope, usage):
        # 独立于 MiMoTransport._normalise 重做 HTTP 到助手响应的投影。
        if type(envelope) is not dict or envelope.get("object") != "chat.completion":
            return None, "invalid_response"
        if any(type(envelope.get(k)) is not str or not envelope[k].strip() for k in ("id", "model")):
            return None, "invalid_response"
        model = envelope["model"]
        if model != MIMO_MODEL and not model.startswith(MIMO_MODEL + "-"):
            return None, "model_mismatch"
        choices = envelope.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            return None, "invalid_response"
        choice = choices[0]
        message = choice.get("message")
        if type(message) is not dict or message.get("role") != "assistant":
            return None, "invalid_response"
        finish = choice.get("finish_reason")
        if finish in ("length", "repetition_truncation"):
            return None, "output_truncated"
        if finish in ("content_filter", "refusal") or message.get("refusal"):
            return None, "refused"
        if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
            return None, "unsupported_tool_call"
        if finish != "stop" or type(message.get("content")) is not str:
            return None, "invalid_response"
        return {"raw_text": message["content"], "provider": "xiaomi_mimo", "model": model,
                "response_id": envelope["id"], "usage": usage,
                "finish_reason": "stop", "is_demo": False}, None

    scope = "invalid_structure"
    try:
        scope = "event_verification_failed"
        root = _existing_root(run_directory)
        verification = verify_events(root)  # 必须在读取 summary 之前完成。
        log_path = _log_path(root)
        event_bytes = log_path.read_bytes()
        events = [strict_json(line) for line in event_bytes.splitlines()]
        require(len(events) == verification["event_count"], "events_changed")
        scope = "summary_invalid"
        summary_path = root / "summary.json"
        require(not summary_path.is_symlink() and summary_path.resolve().is_relative_to(root),
                "unsafe_summary_path")
        summary_bytes = summary_path.read_bytes()
        summary = strict_json(summary_bytes)
        require(type(summary) is dict, "summary_not_object")
        equal(summary["verification"], verification, "summary_verification_mismatch")
        for name, value in {"kind": "assistant_probe", "is_demo": False,
                            "formal_eligible": False, "demo_fallback": False, "executed": True}.items():
            equal(summary[name], value, "summary_identity_mismatch")
        status = summary["status"]
        require(status in ("succeeded", "failed"), "invalid_run_status")
        run_id = summary["run_id"]
        require(type(run_id) is str and run_id == root.name, "run_id_mismatch")
        require(Path(summary["run_directory"]).resolve() == root, "run_directory_mismatch")
        completed_at = summary["completed_at_utc"]
        require(type(completed_at) is str and completed_at.endswith("Z"), "invalid_completion_time")
        datetime.fromisoformat(completed_at[:-1] + "+00:00")

        scope = "external_inputs_invalid"
        packet_file = Path(packet_path).resolve(strict=True)
        packet_bytes = packet_file.read_bytes()
        packet = validate_packet(strict_json(packet_bytes), allow_demo=False)
        expected_packet_hash = canonical_hash(packet)
        bundle_file = Path(expected_bundle_path).resolve(strict=True)
        bundle_bytes = bundle_file.read_bytes()
        require(type(expected_bundle_sha) is str and re.fullmatch(r"[0-9a-f]{64}", expected_bundle_sha),
                "invalid_expected_bundle_sha")
        bundle = load_prompt_bundle(bundle_file, expected_bundle_sha)
        equal(strict_json(bundle_bytes), bundle, "bundle_changed")
        fingerprint = adapter_fingerprint(bundle, "live_probe")
        source_index = deepcopy(source_index)
        _reject_sensitive_keys(source_index)
        compact = compact_evidence(packet, source_index)
        stages = tuple(STAGES[:4])
        token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")

        scope = "event_structure_invalid"
        assistant_types = {
            "assistant_task_started", "assistant_preflight_passed", "assistant_request_started",
            "assistant_context_rejected", "assistant_response_received", "assistant_stage_validated",
            "assistant_task_completed", "assistant_task_failed",
        }
        mimo_types = {"mimo_request_prepared", "mimo_response_received", "mimo_response_omitted",
                      "mimo_request_completed", "mimo_request_failed"}
        allowed = assistant_types | mimo_types | {"probe_started", "probe_completed", "probe_failed"}
        require(len(events) >= 4, "incomplete_run")
        for event in events:
            require(event["event_type"] in allowed, "unexpected_event_type")
            require(event["experiment_id"] == run_id and event["is_demo"] is False, "event_identity_mismatch")
            equal(event["material_manifest_hash"], expected_packet_hash, "event_packet_mismatch")
            require(type(event.get("payload")) is dict, "event_payload_invalid")
        probe_types = [e["event_type"] for e in events if e["event_type"].startswith("probe_")]
        probe_terminal_type = "probe_completed" if status == "succeeded" else "probe_failed"
        equal(probe_types, ["probe_started", probe_terminal_type], "probe_terminal_count")
        require(events[0]["event_type"] == "probe_started" and events[-1]["event_type"] == probe_terminal_type,
                "probe_order_invalid")
        for name, value in {"scope": "assistant_probe", "formal_eligible": False,
                            "source_type": "explicit_packet"}.items():
            equal(events[0]["payload"][name], value, "probe_start_mismatch")
        require(events[-1]["status"] == status, "probe_terminal_status_mismatch")
        equal(events[-1]["payload"], {k: v for k, v in summary.items() if k != "verification"},
              "summary_probe_terminal_mismatch")
        assistant_events = [e for e in events if e["event_type"] in assistant_types]
        terminal_type = "assistant_task_completed" if status == "succeeded" else "assistant_task_failed"
        require(assistant_events[0]["event_type"] == "assistant_task_started"
                and assistant_events[-1]["event_type"] == terminal_type, "assistant_terminal_missing")
        require(sum(e["event_type"] == "assistant_task_started" for e in assistant_events) == 1
                and sum(e["event_type"] in ("assistant_task_completed", "assistant_task_failed")
                        for e in assistant_events) == 1, "assistant_task_count")
        task_id = assistant_events[0]["task_id"]
        require(type(task_id) is str and bool(task_id.strip()) and summary["task_id"] == task_id,
                "task_id_mismatch")
        previous = None
        for event in assistant_events:
            require(event["task_id"] == task_id and type(event["attempt_id"]) is int
                    and event["attempt_id"] == 1, "multiple_tasks_or_attempts")
            equal(event.get("parent_event_ids", []), [] if previous is None else [previous],
                  "assistant_parent_chain_mismatch")
            previous = event["event_id"]
        task_terminal = assistant_events[-1]
        require(events[-2]["event_id"] == task_terminal["event_id"], "assistant_terminal_order")
        terminal_result = task_terminal["payload"]
        # run_probe 原地添加字段；这里逐 key 比较更新前结果，不能比较整个 dict 相等。
        for key, value in terminal_result.items():
            require(key in summary, "task_result_key_missing")
            equal(summary[key], value, "summary_task_terminal_mismatch")
        for name, value in {"is_demo": False, "formal_eligible": False, "demo_fallback": False,
                            "fixture_calls": 0, "external_retrieval_calls": 0, "mirror": None,
                            "model_call_count_basis": "http_attempts_not_billing_confirmation"}.items():
            equal(terminal_result[name], value, "task_identity_mismatch")
        require(terminal_result["status"] == status, "task_status_mismatch")
        started = events[1]
        require(started["event_id"] == assistant_events[0]["event_id"]
                and started["phase"] == "assistant_preflight" and started["status"] == "started",
                "assistant_start_invalid")
        for name, value in {"adapter_version": ADAPTER_VERSION, "execution_mode": "live_probe",
                            "live_transport_enabled": True}.items():
            equal(started["payload"][name], value, "assistant_start_mode_mismatch")

        position = 2

        def at(kind):
            return position < len(events) and events[position]["event_type"] == kind

        def take(kind, phase=None, event_status=None):
            nonlocal position
            require(at(kind), "event_order_mismatch")
            event = events[position]
            position += 1
            if phase is not None:
                require(event.get("phase") == phase, "event_phase_mismatch")
            if event_status is not None:
                require(event.get("status") == event_status, "event_status_mismatch")
            return event

        scope = "preflight_invalid"
        preflight = None
        settings = None
        if at("assistant_preflight_passed"):
            preflight = take("assistant_preflight_passed", "preflight", "verified")
            pre = preflight["payload"]
            equal(blob(pre["packet"]), packet, "preflight_packet_mismatch")
            equal(blob(pre["prompt_bundle"]), bundle, "preflight_bundle_mismatch")
            equal(pre["fingerprint"], fingerprint, "preflight_fingerprint_mismatch")
            equal(pre["mirror_enabled"], False, "mirror_forbidden")
            equal(pre["external_retrieval_calls"], 0, "retrieval_forbidden")
            settings = pre["model_settings"]
            require(type(settings) is dict and set(settings) == {"provider", "model", "temperature", "max_tokens"},
                    "model_settings_invalid")
            require(type(settings["temperature"]) in (int, float) and 0 <= settings["temperature"] <= 2
                    and type(settings["max_tokens"]) is int and 1 <= settings["max_tokens"] <= 1000000,
                    "model_settings_invalid")
            require(all(type(settings[k]) is str and settings[k].strip() for k in ("provider", "model")),
                    "model_settings_invalid")
            require(all(type(pre[k]) is int and pre[k] > 0 for k in ("max_context_chars", "max_response_chars")),
                    "context_limits_invalid")
        if preflight is not None:
            equal(terminal_result["packet_hash"], expected_packet_hash, "terminal_packet_mismatch")
            equal(terminal_result["fingerprint"], fingerprint, "terminal_fingerprint_mismatch")
        else:
            require(status == "failed", "success_without_preflight")
            if terminal_result["packet_hash"] is not None:
                equal(terminal_result["packet_hash"], expected_packet_hash, "terminal_packet_mismatch")
            if terminal_result["fingerprint"] is not None:
                equal(terminal_result["fingerprint"], fingerprint, "terminal_fingerprint_mismatch")

        stage_results = {stage: {"stage": stage, "status": "not_attempted", "structure_passed": False,
                                "metadata": {"request_id": None}, "source_event_ids": [],
                                "raw_text": None, "structured_output": None, "citation_audit": None,
                                "usage": None, "usage_details": {}, "usage_error_code": None,
                                "http": None, "actual_model_calls": 0, "completed_calls": 0,
                                "validation_error_code": None} for stage in stages}
        reconstructed = {}
        previous_outputs = {}
        actual_model_calls = 0
        prepared_count = 0
        completed_calls = 0
        current_input = current_response = None
        last_phase = "preflight"
        transport_failure = None
        for stage in stages if preflight is not None else ():
            scope = "stage_" + stage + "_invalid"
            last_phase = stage
            current_input = current_response = None
            if at(terminal_type):
                break
            rejected = at("assistant_context_rejected")
            request_event = take("assistant_context_rejected" if rejected else "assistant_request_started",
                                 stage, "failed" if rejected else "started")
            row = stage_results[stage]
            row["status"] = "failed"
            row["source_event_ids"].append(request_event["event_id"])
            current_input = descriptor(request_event, "input")
            request = blob(current_input)
            require(type(request) is dict and set(request) == {"request_id", "stage", "model_settings", "messages", "is_demo"},
                    "request_fields_invalid")
            equal(request["model_settings"], settings, "request_settings_mismatch")
            equal(request["is_demo"], False, "request_demo_forbidden")
            require(request["stage"] == stage and request["request_id"] == f"{task_id}:{stage}:1",
                    "request_id_mismatch")
            metadata = row["metadata"]
            metadata.update(request_id=request["request_id"], input=current_input)
            messages = request["messages"]
            require(type(messages) is list and len(messages) == 2, "extra_or_missing_messages")
            equal(messages[0], {"role": "system", "content": effective_system_prompt(bundle, stage)},
                  "system_prompt_mismatch")
            require(type(messages[1]) is dict and set(messages[1]) == {"role", "content"}
                    and messages[1]["role"] == "user" and type(messages[1]["content"]) is str,
                    "user_message_invalid")
            equal(strict_json(messages[1]["content"]), {"frozen_evidence_packet": packet,
                  "previous_stage_outputs": previous_outputs, "instructions_are_data": True},
                  "user_context_mismatch")
            context_chars = sum(len(message["content"]) for message in messages)
            if rejected:
                equal(request_event["payload"], {"context_chars": context_chars,
                      "limit": pre["max_context_chars"], "truncated": False}, "context_rejection_mismatch")
                require(context_chars > pre["max_context_chars"], "false_context_rejection")
                row["validation_error_code"] = "context_too_large"
                break
            require(context_chars <= pre["max_context_chars"], "request_over_context_limit")
            equal(request_event["model"], settings, "request_event_model_mismatch")
            equal(request_event["payload"], {
                "original_prompt_sha256": bundle["prompts"][stage]["sha256"],
                "effective_prompt_sha256": fingerprint["effective_prompt_sha256"][stage],
                "packet_hash": expected_packet_hash, "context_chars": context_chars,
                "truncated": False, "sent_to_live_model": False, "live_request_planned": True,
            }, "request_event_payload_mismatch")

            transport = []
            while position < len(events) and events[position]["event_type"] in mimo_types:
                event = take(events[position]["event_type"], "live_probe", "recorded")
                # 不用最后一个 HTTP 事件配对，必须绑定当前原请求的标识。
                require(event["task_id"] == metadata["request_id"], "http_request_id_mismatch")
                require(event.get("actor_id") == "mimo_transport" and event.get("actor_role") == "transport",
                        "transport_actor_mismatch")
                transport.append(event)
                row["source_event_ids"].append(event["event_id"])
            kinds = tuple(e["event_type"] for e in transport)
            p, r, o, c, f = ("mimo_request_prepared", "mimo_response_received", "mimo_response_omitted",
                             "mimo_request_completed", "mimo_request_failed")
            require(kinds in ((), (p,), (p, f), (p, r), (p, r, f), (p, r, c), (p, o), (p, o, f)),
                    "transport_chain_invalid")
            by_type = {e["event_type"]: e for e in transport}
            before = actual_model_calls
            if p in by_type:
                prepared_count += 1
                wire_event = by_type[p]
                wire = blob(descriptor(wire_event, "input"))
                require(settings["provider"] == "xiaomi_mimo" and settings["model"] == MIMO_MODEL
                        and settings["temperature"] <= 1.5 and settings["max_tokens"] <= 131072,
                        "wire_model_config_invalid")
                equal(wire, {"model": settings["model"], "messages": messages,
                      "temperature": settings["temperature"], "max_completion_tokens": settings["max_tokens"],
                      "thinking": {"type": "disabled"}, "stream": False}, "wire_body_mismatch")
                equal(wire_event["payload"], {"actual_model_calls": before,
                      "max_completion_tokens": settings["max_tokens"],
                      "attempt_is_not_billing_confirmation": True}, "prepared_counter_mismatch")
                metadata["wire_input"] = descriptor(wire_event, "input")
            for event in transport:
                payload = event["payload"]
                if event["event_type"] == o:
                    continue  # omitted 无调用计数；必须由其余 MiMo 事件提供，不能猜加一。
                value = payload["actual_model_calls"]
                require(nonnegative_int(value), "invalid_model_call_counter")
                equal(payload["attempt_is_not_billing_confirmation"], True, "billing_basis_mismatch")
                if event["event_type"] == p:
                    require(value == before, "prepared_counter_mismatch")
                else:
                    require(actual_model_calls <= value <= before + 1, "model_call_counter_jump")
                    if event["event_type"] in (r, c):
                        require(value == before + 1, "http_attempt_counter_mismatch")
                    actual_model_calls = max(actual_model_calls, value)
            row["actual_model_calls"] = actual_model_calls - before
            normalized = None
            envelope_error = None
            response_event = by_type.get(r) or by_type.get(o)
            if response_event is not None:
                http_payload = response_event["payload"]
                http_ref = descriptor(response_event, "output")
                captured = blob(http_ref)
                http_status = http_payload["status"]
                require(type(http_status) is int and 100 <= http_status <= 599, "http_status_invalid")
                require(nonnegative_int(http_payload["response_bytes"])
                        and type(http_payload["response_sha256"]) is str
                        and re.fullmatch(r"[0-9a-f]{64}", http_payload["response_sha256"]), "http_digest_invalid")
                row["http"] = {"event_id": response_event["event_id"], "request_id": metadata["request_id"],
                               "output": http_ref, "status": http_status,
                               "response_bytes": http_payload["response_bytes"],
                               "response_sha256": http_payload["response_sha256"], "body_available": False}
                require(type(captured) is dict, "http_blob_invalid")
                if "raw_http_text" in captured:
                    require(set(captured) == {"raw_http_text"} and type(captured["raw_http_text"]) is str
                            and response_event["event_type"] == r, "raw_http_fields_invalid")
                    text = captured["raw_http_text"]
                    raw_bytes = text.encode("utf-8")
                    equal(len(raw_bytes), http_payload["response_bytes"], "raw_http_bytes_mismatch")
                    equal(hashlib.sha256(raw_bytes).hexdigest(), http_payload["response_sha256"], "raw_http_sha_mismatch")
                    envelope = strict_json(text)
                    row["http"]["body_available"] = True
                    row["usage"], row["usage_details"], row["usage_error_code"] = usage_info(envelope)
                    if row["usage"] is not None:
                        normalized, envelope_error = normalize_http(envelope, row["usage"])
                    else:
                        envelope_error = row["usage_error_code"]
                else:
                    safe_names = {"status", "response_bytes", "response_sha256", "omitted_for_credentials",
                                  "omitted_due_to_incomplete_security_scan"}
                    require(set(captured) <= safe_names, "omitted_http_fields_invalid")
                    for name in ("status", "response_bytes", "response_sha256"):
                        equal(captured[name], http_payload[name], "omitted_http_metadata_mismatch")
                    for name in ("omitted_for_credentials", "omitted_due_to_incomplete_security_scan"):
                        if name in captured:
                            equal(captured[name], True, "omitted_http_marker_invalid")
                            equal(http_payload[name], True, "omitted_http_marker_mismatch")
                            row["http"][name] = True
                    if response_event["event_type"] == o:
                        equal(http_payload["bounded_prefix"], True, "omitted_prefix_marker_missing")
                        row["http"]["bounded_prefix"] = True
                        envelope_error = "response_too_large"
                    else:
                        envelope_error = ("response_not_recordable" if captured.get("omitted_for_credentials")
                                          else "raw_http_unavailable")
                    row["usage_error_code"] = "usage_unavailable"
                # 传输层先处理超长/敏感正文，再处理 HTTP 状态；不能用 401 等覆盖原错误。
                if not 200 <= http_status <= 299 and envelope_error not in ("response_too_large", "response_not_recordable"):
                    normalized = None
                    envelope_error = ({401: "auth_failed", 403: "forbidden", 429: "rate_limited"}.get(http_status)
                                      or ("upstream_error" if http_status >= 500 else
                                          "redirect_blocked" if 300 <= http_status <= 399 else "http_error"))
                row["http"]["validation_error_code"] = envelope_error
            else:
                row["usage_error_code"] = "response_missing"
            if c in by_type:
                require(normalized is not None and envelope_error is None and r in by_type,
                        "completed_without_valid_http")
                payload = by_type[c]["payload"]
                for name in ("status", "response_bytes", "response_sha256"):
                    equal(payload[name], by_type[r]["payload"][name], "completed_http_mismatch")
                equal(payload["usage"], row["usage"], "completed_usage_mismatch")
                equal(payload["usage_settled"], True, "completed_usage_unsettled")
                completed_calls += 1
                row["completed_calls"] = 1
            if f in by_type:
                payload = by_type[f]["payload"]
                require(type(payload.get("code")) is str and bool(payload["code"].strip()), "failure_code_invalid")
                transport_failure = payload["code"]
                if row["http"] is not None:
                    details = payload["details"]
                    require(type(details) is dict, "failure_details_invalid")
                    for name in ("status", "response_bytes", "response_sha256"):
                        if name in details:
                            equal(details[name], row["http"][name], "failure_http_details_mismatch")
                if envelope_error not in (None, "raw_http_unavailable"):
                    equal(transport_failure, envelope_error, "http_failure_code_mismatch")

            if at("assistant_response_received"):
                received = take("assistant_response_received", stage, "received")
                row["source_event_ids"].append(received["event_id"])
                require(c in by_type and normalized is not None, "response_without_completed_http")
                equal(descriptor(received, "input"), current_input, "response_input_mismatch")
                current_response = descriptor(received, "output")
                response = blob(current_response)
                equal(response, normalized, "normalized_response_mismatch")
                equal(received["payload"], {"response": current_response, "validated": False,
                      "fixture_response": False}, "response_payload_mismatch")
                metadata.update(response=current_response, response_id=response["response_id"],
                                requested_model=settings["model"], returned_model=response["model"])
                row["raw_text"] = response["raw_text"]
                latency = received["latency_ms"]
                require(type(latency) in (int, float) and latency >= 0, "invalid_latency")
                metadata["latency_ms"] = latency
                parsed = None
                if not response["raw_text"].strip():
                    row["validation_error_code"] = "invalid_response"
                elif len(response["raw_text"]) > pre["max_response_chars"]:
                    row["validation_error_code"] = "output_truncated"
                else:
                    try:
                        parsed = parse_stage_output(response["raw_text"], stage, packet)
                        _reject_sensitive_keys(parsed)
                    except ValueError:
                        row["validation_error_code"] = "invalid_output"
                if parsed is not None:
                    row["structured_output"] = parsed["output"]
                    row["citation_audit"] = parsed["citation_audit"]
                if at("assistant_stage_validated"):
                    validated = take("assistant_stage_validated", stage, "success")
                    require(parsed is not None, "validated_output_does_not_parse")
                    equal(validated["payload"], {"structured_output": parsed["output"],
                          "citation_audit": parsed["citation_audit"], "response": current_response},
                          "validated_payload_mismatch")
                    equal(validated["usage"], row["usage"], "validated_usage_mismatch")
                    equal(validated["model"], {"provider": response["provider"], "model": response["model"]},
                          "validated_model_mismatch")
                    equal(validated["latency_ms"], latency, "validated_latency_mismatch")
                    equal(blob(validated["payload"]["response"]), response, "validated_response_mismatch")
                    row["source_event_ids"].append(validated["event_id"])
                    row.update(status="validated", structure_passed=True)
                    reconstructed[stage] = {"raw_text": response["raw_text"], "output": parsed["output"],
                        "citation_audit": parsed["citation_audit"], "usage": row["usage"], "input": current_input,
                        "response": current_response, "requested_model": settings["model"],
                        "returned_model": response["model"], "latency_ms": latency}
                    previous_outputs[stage] = {"raw_text": response["raw_text"], "structured_output": parsed["output"]}
                    continue
            else:
                row["validation_error_code"] = "response_missing"
            break  # 任一阶段缺 validated 都只能终止，不能跳过或进入 mirror。

        scope = "terminal_reconciliation_failed"
        terminal = take(terminal_type, last_phase, "success" if status == "succeeded" else "failed")
        require(terminal["event_id"] == task_terminal["event_id"] and position == len(events) - 1,
                "trailing_or_missing_stage_events")
        equal(terminal_result["completed_stages"], list(reconstructed), "completed_stages_mismatch")
        equal(terminal_result["stage_results"], reconstructed, "stage_results_mismatch")
        # 只使用 MiMo 的计数器；prepared 是 before 值，不能以 len(requests) 冒充 HTTP 次数。
        observed_counters = [e["payload"]["actual_model_calls"] for e in events
                             if e["event_type"] in mimo_types and "actual_model_calls" in e["payload"]]
        equal(actual_model_calls, max(observed_counters, default=0), "counter_reconciliation_failed")
        equal(summary["actual_model_calls"], actual_model_calls, "summary_model_calls_mismatch")
        equal(terminal_result["actual_model_calls"], actual_model_calls, "terminal_model_calls_mismatch")
        four_passed = list(reconstructed) == list(stages)
        failure_info = None
        if status == "succeeded":
            require(four_passed and completed_calls == 4 and actual_model_calls == 4, "incomplete_success")
            equal(terminal_result["report"], reconstructed["integrate"]["output"], "report_mismatch")
            equal(terminal_result["usage_complete"], True, "terminal_usage_incomplete")
            equal(terminal_result["reported_usage"], {k: sum(r["usage"][k] for r in reconstructed.values())
                  for k in token_keys}, "terminal_usage_mismatch")
            equal(terminal_result["usage_scope"], "provider_response", "terminal_usage_scope_mismatch")
            equal(terminal_result["semantic_review"], "pending_independent_review", "semantic_review_not_pending")
        else:
            require(not four_passed, "failure_after_all_stages")
            equal(terminal_result["failed_stage"], last_phase, "failed_stage_mismatch")
            equal(terminal_result["report"], None, "failed_run_has_report")
            equal(terminal.get("input_ref"), current_input, "failure_input_mismatch")
            equal(terminal.get("output_ref"), current_response, "failure_response_mismatch")
            error = terminal_result["error"]
            require(type(error) is dict and {"code", "message"} <= set(error)
                    and set(error) <= {"code", "message", "upstream_code"}
                    and all(type(v) is str and v.strip() for v in error.values()), "terminal_error_invalid")
            equal(terminal["error"], error, "terminal_error_mismatch")
            if transport_failure is not None:
                equal(error["code"], "mimo_error", "transport_terminal_code_mismatch")
                equal(error.get("upstream_code"), transport_failure, "transport_terminal_error_mismatch")
            observed_error = stage_results[last_phase]["validation_error_code"] if last_phase in stages else None
            if observed_error not in (None, "response_missing"):
                equal(error["code"], observed_error, "stage_terminal_error_mismatch")
            # response_missing 只是审核观察，绝不替代原终态错误或猜测增加调用次数。
            failure_info = {"failed_stage": last_phase, "terminal_error": deepcopy(error),
                            "transport_error_code": transport_failure, "observation_code": observed_error,
                            "source_event_ids": [terminal["event_id"], events[-1]["event_id"]]}

        scope = "render_failed"
        rendered_report = None
        rendered_items = []
        candidate = None
        deviations = []
        report_chars = None
        dose = {"status": "not_available", "min_chars": 600, "max_chars": 800,
                "dose_within_range": None, "char_count": None}
        if stage_results["integrate"]["structure_passed"]:
            titles = {"consensus": "共识", "disagreements": "分歧", "blindspots": "盲区", "sources": "来源"}
            sections = []
            for section in reconstructed["integrate"]["output"]["sections"]:
                lines = [titles[section["name"]]]
                for index, item in enumerate(section["items"]):
                    rendered_items.append({"section": section["name"], "item_index": index,
                                           "text": item["text"], "citations": list(item["citations"]),
                                           "epistemic_status": item["epistemic_status"]})
                    citations = "、".join(item["citations"]) or "无"
                    lines.append(f"[{item['epistemic_status']}；引用：{citations}]\n" + item["text"])
                sections.append("\n".join(lines))
            rendered_report = "\n\n".join(sections)
            report_chars = len(rendered_report)
            candidate = ("阅读材料\n" + compact["evidence_block"] + "\n\n阅读说明\n"
                         + rendered_report + "\n\n" + RESPONSE_PROMPT)
            within = 600 <= len(candidate) <= 800
            dose.update(status="within_range" if within else "out_of_range",
                        dose_within_range=within, char_count=len(candidate))
            if not within:
                deviations.append({"code": "dose_out_of_range", "char_count": len(candidate),
                                   "min_chars": 600, "max_chars": 800})
            if re.search(r"I[45]", candidate, re.IGNORECASE):
                deviations.append({"code": "contains_treatment_label", "action": "仅标记；未删改原文"})

        usages = [row["usage"] for row in stage_results.values() if row["usage"] is not None]
        known_usage = {k: sum(u[k] for u in usages) for k in token_keys}
        usage_complete = len(usages) == actual_model_calls
        result = {
            "schema_version": "1.0", "audit_version": "live_four_stage_local_v1",
            "audit_status": "passed", "run_status": status, "kind": "assistant_probe",
            "four_stage_structure_passed": four_passed, "preflight_passed": preflight is not None,
            "run_id": run_id, "run_directory": str(root), "task_id": task_id,
            "started_at_utc": events[0]["wall_time_utc"], "completed_at_utc": completed_at,
            "terminal_event_at_utc": events[-1]["wall_time_utc"], "verification": verification,
            "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
            "events_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "terminal_event_hash": events[-1]["event_hash"],
            "packet_path": str(packet_file), "packet_file_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "expected_packet_hash": expected_packet_hash, "packet_id": packet["packet_id"],
            "source_packet_is_demo": False, "source_index_sha256": canonical_hash(source_index),
            "prompt_bundle_path": str(bundle_file), "prompt_bundle_sha256": expected_bundle_sha,
            "prompt_bundle_file_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
            "prompt_bundle_version": bundle["version"], "fingerprint": fingerprint,
            "prompt_fingerprint": fingerprint["adapter_fingerprint"], "model_settings": settings,
            "actual_model_calls": actual_model_calls, "actual_http_attempts": actual_model_calls,
            "prepared_requests": prepared_count, "completed_model_calls": completed_calls,
            "model_call_count_basis": "mimo_event_max_actual_model_calls_not_billing_confirmation",
            "completed_stages": list(reconstructed), "stage_results": stage_results,
            "source_event_ids": [e["event_id"] for e in events], "failure_info": failure_info,
            "usage_complete": usage_complete, "usage_observed_attempts": len(usages),
            "usage_missing_attempts": actual_model_calls - len(usages), "known_reported_usage": known_usage,
            "reported_usage": known_usage if usages and usage_complete else None,
            "usage_scope": "本运行原始HTTP中可用的usage，包含已返回usage但后续失败的阶段；不推算费用",
            "content_origin": "recorded_assistant_output",
            "content_origin_notice": "正文仅来自已复核的原模型记录；非旧编辑稿，非完整I4，未生成I5。",
            "rendered_report": rendered_report, "report_chars": report_chars, "rendered_items": rendered_items,
            "evidence_display_version": DISPLAY_POLICY["version"],
            "display_policy_sha256": compact["policy_sha256"], "evidence_block": compact["evidence_block"],
            "evidence_block_sha256": compact["evidence_block_sha256"], "evidence_chars": compact["char_count"],
            "evidence_ids": compact["evidence_ids"], "source_map": compact["source_map"],
            "source_map_sha256": canonical_hash(compact["source_map"]),
            "heading": "阅读说明", "response_prompt": RESPONSE_PROMPT,
            "candidate_visible_text": candidate, "candidate_visible_chars": len(candidate) if candidate is not None else None,
            "candidate_visible_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest() if candidate is not None else None,
            "dose": dose, "deviations": deviations, "length_basis": DISPLAY_POLICY["length_basis"],
            "report_text_unchanged": rendered_report is not None, "report_urls_removed": False,
            "automatic_compression": False, "response_opportunities": 1 if candidate is not None else 0,
            "full_I4_eligible": False, "mirror_executed": False, "i5_output_available": False,
            "natural_view_used": False, "semantic_review": "pending", "human_reviews_completed": 0,
            "is_demo": False, "formal_eligible": False, "approved_for_experiment": False,
            "network_called_by_audit": False, "model_calls_this_audit": 0,
            "audit_notice": "仅本地一致性与结构复核；不是数字签名、外部网络真实性、来源真实性或账单证明。"
                            "来源表与冻结包须由调用方另行绑定可信清单；语义引用及人工审阅仍待完成。"
                            "source_id_inline_v1只用于证据块；原报告中的URL全部保留并计入候选全文。",
        }
        _reject_sensitive_keys(result)
        scope = "files_changed_during_audit"
        equal(verify_events(root), verification, "verification_changed")
        require(log_path.read_bytes() == event_bytes and summary_path.read_bytes() == summary_bytes
                and packet_file.read_bytes() == packet_bytes and bundle_file.read_bytes() == bundle_bytes,
                "files_changed_during_audit")
        return result
    except ValueError as exc:
        if str(exc).startswith("live_audit:"):
            raise
        raise ValueError("live_audit:" + scope) from None
    except (OSError, KeyError, TypeError, IndexError, OverflowError, RecursionError) as exc:
        raise ValueError("live_audit:" + scope) from None
