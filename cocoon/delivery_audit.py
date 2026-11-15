"""只读复核短投递记录；失败候选不修复、不放行、不导出受试 payload。

passed 只表示本地记录、冻结输入与当前结构规则一致，不证明真实网络、语义、
账单或完整 I4。普通哈希不能防止整套记录被重写；请在写入者停止后使用。
"""

from copy import deepcopy
import hashlib
from pathlib import Path
import re

from . import delivery
from .dose_build import source_index_from_review
from .dosage import RESPONSE_PROMPT, compact_evidence, make_dose_pair, public_payload
from .live_audit import audit_live_run
from .runtime import _json_text, _parse_json, _reject_sensitive_keys, _verify_blob, canonical_hash, verify_events


_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _require(condition, code):
    if not condition:
        raise ValueError("delivery_audit:" + code)


def _equal(actual, expected, code):
    _require(canonical_hash(actual) == canonical_hash(expected), code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    value = _parse_json(data)
    canonical_hash(value)
    _reject_sensitive_keys(value)
    return value


def _file(path):
    path = Path(path)
    _require(path.suffix.lower() in (".json", ".jsonl") and not path.is_symlink()
             and not any(re.search(r"(?i)credential|secret|\.env|api[_-]?key", p) for p in path.parts),
             "unsafe_input_path")
    return path.resolve(strict=True)


def _hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        _require(not path.is_symlink(), "unsafe_input_path")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha(_file(path).read_bytes())
    return result


def _blob(root, ref):
    _require(type(ref) is dict and set(ref) == {"ref", "sha256"}, "invalid_reference")
    return _verify_blob(root, ref)


def _ref(event, prefix):
    value = event[prefix + "_ref"]
    return value if type(value) is dict else {"ref": value, "sha256": event[prefix + "_sha256"]}


def _transport_records(events):
    return [{"event_id": e["event_id"], "event_type": e["event_type"],
             "input": _ref(e, "input") if "input_ref" in e else None,
             "output": _ref(e, "output") if "output_ref" in e else None} for e in events]


def _normalize(envelope, model):
    """独立核对原 HTTP 正文到七字段响应的投影，不调用传输器。"""
    _require(type(envelope) is dict and envelope.get("object") == "chat.completion", "invalid_http_response")
    _require(all(type(envelope.get(k)) is str and envelope[k].strip() for k in ("id", "model")),
             "invalid_http_response")
    _require(envelope["model"] == model or envelope["model"].startswith(model + "-"), "http_model_mismatch")
    choices = envelope.get("choices")
    _require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict, "invalid_http_response")
    choice = choices[0]
    message = choice.get("message")
    _require(type(message) is dict and message.get("role") == "assistant"
             and type(message.get("content")) is str and choice.get("finish_reason") == "stop"
             and not any(message.get(k) for k in ("refusal", "tool_calls", "function_call")), "invalid_http_response")
    usage = envelope.get("usage")
    _require(type(usage) is dict and all(type(usage.get(k)) is int and usage[k] >= 0 for k in _TOKEN_KEYS)
             and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"], "invalid_http_usage")
    return {"raw_text": message["content"], "provider": "xiaomi_mimo", "model": envelope["model"],
            "response_id": envelope["id"], "usage": {k: usage[k] for k in _TOKEN_KEYS},
            "finish_reason": "stop", "is_demo": False}


def _diagnose(parsed, bounds, budget, fixed, failure):
    """只读 text/citations 计字；绝不将诊断拼接结果送回 render_addition。"""
    violations = [deepcopy(failure)] if failure else []
    lengths, pieces = [], []
    if type(parsed) is dict and "schema_version" in parsed and type(parsed["schema_version"]) is not str:
        violations.append({"code": "schema_version_type", "path": "schema_version", "expected_type": "str",
                           "actual_type": type(parsed["schema_version"]).__name__, "actual_value": parsed["schema_version"]})
    sentences = parsed.get("sentences") if type(parsed) is dict else None
    available = type(sentences) is list and bool(sentences)
    for number, sentence in enumerate(sentences if type(sentences) is list else []):
        text = sentence.get("text") if type(sentence) is dict else None
        citations = sentence.get("citations") if type(sentence) is dict else None
        readable = type(text) is str and type(citations) is list and all(type(c) is str for c in citations)
        slot = budget["slots"][number] if budget and number < len(budget["slots"]) else None
        piece = text + "（" + "、".join(citations) + "）" if readable else None
        available = available and readable
        if readable:
            pieces.append(piece)
        size = len(text) if type(text) is str else None
        low, high = (slot["text_min_chars"], slot["text_max_chars"]) if slot else (None, None)
        lengths.append({"index": number, "text_chars": size,
                        "citation_chars": len(piece) - size if readable else None,
                        "rendered_chars": len(piece) if readable else None,
                        "text_min_chars": low, "text_max_chars": high,
                        "target_text_chars": slot["target_text_chars"] if slot else None,
                        "within_range": low <= size <= high if slot and size is not None else None})
        if slot and size is not None and not low <= size <= high:
            violations.append({"code": "sentence_below_min" if size < low else "sentence_above_max",
                               "index": number, "len": size, "min": low, "max": high})
        if slot and citations != [slot["citation_id"]]:
            violations.append({"code": "sentence_citation_mismatch", "index": number})
    text = "".join(pieces) if available else None
    if text is not None and not bounds["min"] <= len(text) <= bounds["max"]:
        violations.append({"code": "additional_length_out_of_range", "len": len(text), **bounds})
    return {"diagnostic_additional_text": text, "diagnostic_additional_chars": len(text) if text is not None else None,
            "diagnostic_visible_chars": fixed + len(text) if text is not None else None,
            "sentence_lengths": lengths, "violations": violations}


def prepare_delivery_review(run_directory, semantic_path, previous_run_directory=None):
    """绑定逐句AI复核到原响应。语义可接受不改变结构失败、配对及人审状态。"""
    from collections import Counter
    from .runtime import utc_now
    audited = inspect_delivery_run(run_directory)
    root = Path(run_directory)
    semantic_bytes = _file(semantic_path).read_bytes()
    semantic = _json(semantic_bytes)
    for name, expected in {"review_type": "independent_ai_not_human", "run_id": audited["run_id"],
                           "summary_sha256": audited["summary_sha256"], "human_reviews_completed": 0,
                           "accepted_pair": audited["pair_available"], "additional_model_calls": 0}.items():
        _equal(semantic[name], expected, "semantic_identity_mismatch")
    _equal(semantic["packet_sha256"], audited["source_inputs"]["packet_input"]["canonical_sha256"],
           "semantic_packet_mismatch")
    summary = _json((root / "summary.json").read_bytes())
    expected_items = {}
    for mode, output in audited["by_mode"].items():
        if output["parsed_output"] is None:
            continue
        ref = summary["additions"][mode]["response"]
        _equal(semantic["raw_response_sha256_by_mode"][mode], ref["sha256"], "semantic_response_mismatch")
        for index, sentence in enumerate(output["parsed_output"]["sentences"]):
            expected_items[mode, index] = sentence
    seen = set()
    for item in semantic["items"]:
        key = item["mode"], item["index"]
        _require(type(key[1]) is int and key in expected_items and key not in seen, "semantic_item_coverage")
        sentence = expected_items[key]
        _equal(item["original_text"], sentence["text"], "semantic_text_mismatch")
        _equal(item["citations"], sentence["citations"], "semantic_citations_mismatch")
        _equal(item["kind"], sentence["kind"], "semantic_kind_mismatch")
        _require(item["verdict"] in ("supported", "acceptable_question", "overstated", "unsupported", "needs_review"),
                 "semantic_verdict_invalid")
        seen.add(key)
    _require(seen == set(expected_items), "semantic_item_coverage")
    counts = Counter(item["verdict"] for item in semantic["items"])
    for verdict in ("supported", "acceptable_question", "overstated", "unsupported", "needs_review"):
        _equal(semantic["counts"][verdict], counts[verdict], "semantic_count_mismatch")
    _equal(semantic["counts"]["reviewed_items"], len(seen), "semantic_count_mismatch")
    problem_count = sum(counts[k] for k in ("overstated", "unsupported", "needs_review"))
    _equal(semantic["counts"]["content_issue_items"], problem_count, "semantic_count_mismatch")
    status = "content_issues_found" if problem_count else "pass_content_with_structure_blocker"
    _equal(semantic["status"], status, "semantic_status_mismatch")
    known_usage = {k: sum(row["usage"][k] for row in audited["by_mode"].values() if row["usage"])
                   for k in _TOKEN_KEYS}
    type_issue = any(v.get("code") == "schema_version_type"
                     for row in audited["by_mode"].values() for v in row["violations"])
    pending = []
    if type_issue:
        pending.append("原回复版本字段类型错误；后续版本应明确字符串\"1.0\"，保留失败原文，不能静默转换。")
    pending.extend([
        "按本运行实际分句及附加段预算诊断失败；不使用此前版本的固定字数，不靠人工补字或放宽10%阈值通过。",
        "仅加强提示词尚未保证精确计字；停止连续收费尝试，优先离线评估可重复的普通阅读对照实现。",
        "完整600–800与类型、引用、附加段匹配分别验证；总长合格不等于整对通过。",
        "内部长报告独立保留；T1个性化镜像、双人人工核查与正式资格仍未完成。"])
    return {"schema_version": "1.0", "created_at_utc": utc_now(), "status": "pair_not_accepted",
            "audit": audited, "semantic_review": semantic,
            "semantic_file_sha256": _sha(semantic_bytes), "semantic_source_path": str(Path(semantic_path).resolve()),
            "previous_audit": inspect_delivery_run(previous_run_directory) if previous_run_directory else None,
            "reported_usage": known_usage, "content_issue_items": problem_count,
            "formal_eligible": False, "human_reviews_completed": 0, "new_model_calls": 0,
            "pending_fixes": pending}


def inspect_delivery_run(run_directory: Path) -> dict:
    """审计已结束的 v1/v2/v3 投递；不一致或不能证明的轨迹抛出固定码 ValueError。

    stage_count 为独立重验且有 validated 事件的阶段数，不是请求数。
    原始合法 JSON 保留原类型；失败诊断不是 rendered、pair 或受试 payload。
    不读取环境、凭据或当前账本，不重跑模型；费用仅核对摘要内累计差值。
    """
    scope = "invalid_record"
    try:
        root = Path(run_directory).resolve(strict=True)
        scope = "event_verification_failed"
        run_hashes = _hashes(root)
        verification = verify_events(root)
        event_bytes = (root / "events/events.jsonl").read_bytes()
        events = [_json(line) for line in event_bytes.splitlines()]
        summary_bytes = _file(root / "summary.json").read_bytes()
        summary = _json(summary_bytes)
        _equal(summary["verification"], verification, "summary_verification_mismatch")
        _require(len(events) == verification["event_count"] and len(events) >= 2, "incomplete_events")
        run_id, status = summary["run_id"], summary["status"]
        _require(type(run_id) is str and run_id == root.name
                 and Path(summary["run_directory"]).resolve() == root, "run_identity_mismatch")
        _require(type(status) is str and status in ("generated_structurally_valid", "delivery_failed"), "invalid_run_status")
        terminal_type = "delivery_failed" if status == "delivery_failed" else "delivery_completed"
        _require(events[0]["event_type"] == "delivery_started" and events[-1]["event_type"] == terminal_type,
                 "terminal_order_mismatch")
        _equal(events[-1]["status"], status, "terminal_status_mismatch")
        _equal(events[-1]["payload"], {k: v for k, v in summary.items() if k != "verification"},
               "summary_terminal_mismatch")
        for k, v in {"schema_version": "1.0", "executed": True, "is_demo": False, "demo_fallback": False,
                     "syntax_only": True, "semantic_guarantee": False, "full_I4_eligible": False,
                     "formal_eligible": False, "mirror": False, "mirror_executed": False, "natural_view_used": False,
                     "received_actual_subject": False, "human_reviews_completed": 0, "external_retrieval_calls": 0,
                     "automatic_retries": 0, "blockers": [], "internalreport_unchanged": True}.items():
            _equal(summary[k], v, "summary_identity_mismatch")

        scope = "source_inputs_invalid"
        refs = summary["inputs"]
        _require(type(refs) is dict and set(refs) == {"policy", "packet", "source_index", "internal_report",
                                                    "parent_file_hashes", "configuration"}, "input_refs_mismatch")
        inputs = {name: _blob(root, ref) for name, ref in refs.items()}
        policy_path = _file(summary["policy_input"]["source_path"])
        policy_sha = summary["expected_policy_sha256"]
        _equal(summary["policy_input"]["file_sha256"], policy_sha, "policy_hash_mismatch")
        policy = delivery.load_delivery_policy(policy_path, policy_sha)
        _equal(inputs["policy"], policy, "stored_policy_mismatch")
        v2 = policy["version"] in delivery._POLICIES_WITH_SENTENCE_BUDGET
        if v2 or "policy_version" in summary:
            _equal(summary["policy_version"], policy["version"], "policy_version_mismatch")
        if v2 or "inputs_unchanged" in summary:
            _equal(summary["inputs_unchanged"], True, "source_changed")
        info = summary["packet_input"]
        packet_path = _file(info["source_path"])
        manifest_path = _file(info["manifest_source_path"])
        build = manifest_path.parent
        _equal(_sha(manifest_path.read_bytes()), info["manifest_sha256"], "manifest_hash_mismatch")
        manifest = _json(manifest_path.read_bytes())
        sources = {policy_path: policy_sha, manifest_path: info["manifest_sha256"], packet_path: info["file_sha256"]}
        for entry in manifest["files"]:
            path = _file(build / entry["path"])
            _require(path.is_relative_to(build), "unsafe_input_path")
            _equal(_sha(path.read_bytes()), entry["sha256"], "source_file_hash_mismatch")
            sources[path] = entry["sha256"]
        packet, bound_info = delivery._bind_packet(build, packet_path)
        _equal(bound_info, info, "source_packet_mismatch")
        _equal(inputs["packet"], packet, "stored_packet_mismatch")
        index = source_index_from_review(build)
        _equal(inputs["source_index"], index, "stored_source_index_mismatch")
        source_index_input = {"source_path": str(build / "review/sources.json"),
                              "file_sha256": sources[build / "review/sources.json"]}
        if v2 or "source_index_input" in summary:
            _equal(summary["source_index_input"], source_index_input, "source_index_input_mismatch")

        parent = Path(summary["parent_source_path"]).resolve(strict=True)
        parent_hashes = inputs["parent_file_hashes"]
        _equal(_hashes(parent), parent_hashes, "parent_files_changed")
        _equal(parent_hashes["summary.json"], summary["parent_summary_sha256"], "parent_summary_hash_mismatch")
        # 父审计使用其已存 prompt bundle，不读取当前配置、源代码或凭据。
        parent_events = [_json(line) for line in (parent / "events/events.jsonl").read_bytes().splitlines()]
        preflight = [e for e in parent_events if e["event_type"] == "assistant_preflight_passed"]
        _require(len(preflight) == 1, "parent_preflight_mismatch")
        pre = preflight[0]["payload"]
        bundle = _blob(parent, pre["prompt_bundle"])
        parent_audit = audit_live_run(parent, packet_path, parent / pre["prompt_bundle"]["ref"],
                                     bundle["bundle_sha256"], index)
        for k, v in {"audit_status": "passed", "run_status": "succeeded", "four_stage_structure_passed": True,
                     "actual_http_attempts": 4, "mirror_executed": False, "natural_view_used": False,
                     "run_id": summary["parent_run_id"], "summary_sha256": summary["parent_summary_sha256"]}.items():
            _equal(parent_audit[k], v, "parent_audit_mismatch")
        original = parent_audit["stage_results"]["integrate"]
        report = original["structured_output"]
        _equal(inputs["internal_report"], report, "internal_report_mismatch")
        _equal(summary["original_internal_report"], {
            "structured_chars": len(_json_text(report)), "rendered_chars": parent_audit["report_chars"],
            "raw_chars": len(original["raw_text"]), "raw_text_sha256": _sha(original["raw_text"].encode("utf-8")),
            "length_constraint": None, "source_event_ids": original["source_event_ids"]}, "original_snapshot_mismatch")
        settings = summary["model_settings"]
        _require(type(settings) is dict and set(settings) == {"provider", "model", "temperature", "max_tokens"}
                 and settings["provider"] == "xiaomi_mimo" and settings["model"] == "mimo-v2.6-flash"
                 and type(settings["temperature"]) in (int, float) and 0 <= settings["temperature"] <= 1.5,
                 "model_settings_mismatch")
        _equal(settings["max_tokens"], policy["max_completion_tokens"], "model_settings_mismatch")
        metadata = {"policy_sha256": policy_sha, "model_settings": settings, "parent_run_id": parent_audit["run_id"],
                    "parent_summary_sha256": parent_audit["summary_sha256"], "parent_report_sha256": canonical_hash(report),
                    "packet_sha256": canonical_hash(packet), "source_index_sha256": canonical_hash(index),
                    "execution_mode": "delivery_probe", "thinking": "disabled", "automatic_retries": 0}
        if v2 or "policy_version" in inputs["configuration"]:
            metadata["policy_version"] = policy["version"]
        _equal(inputs["configuration"], metadata, "configuration_mismatch")
        _equal(summary["parent_report_sha256"], metadata["parent_report_sha256"], "parent_report_hash_mismatch")
        _equal(events[0]["payload"], {"inputs": refs, "configuration": metadata,
               "billing_before": summary["billing_before"], "received_actual_subject": False}, "start_inputs_mismatch")
        for event in events:
            _require(event["experiment_id"] == run_id and event["is_demo"] is False, "event_identity_mismatch")
            _equal(event["config_hash"], canonical_hash(metadata), "event_configuration_mismatch")
            _equal(event["material_manifest_hash"], canonical_hash(packet), "event_packet_mismatch")

        compact = compact_evidence(packet, index)
        overhead = len("阅读材料\n\n\n阅读说明\n\n\n" + RESPONSE_PROMPT)
        fixed = compact["char_count"] + overhead
        allowed = {"min": policy["total_min_chars"] - fixed, "max": policy["total_max_chars"] - fixed}
        for k, v in {"fixed_chars": fixed, "evidence_chars": compact["char_count"], "public_constant_chars": overhead,
                     "allowed_additional_char_range": allowed}.items():
            _equal(summary[k], v, "fixed_assembly_mismatch")
        by_mode = {mode: {"status": "not_attempted", "raw_text": None, "parsed_output": None,
                         "response_id": None, "usage": None, "diagnostic_additional_text": None,
                         "diagnostic_additional_chars": None, "diagnostic_visible_chars": None,
                         "accepted": False, "diagnostic_only": False, "modeled_candidate": False,
                         "violations": [], "sentence_lengths": [], "range": None, "budget": None} for mode in delivery.MODES}
        position, actual, completed, validated = 1, 0, 0, 0
        rendered_by_mode, source_ids, attempted = {}, [], []
        observed_failure = None

        def take(kind, phase):
            nonlocal position
            _require(position < len(events) - 1 and events[position]["event_type"] == kind, "event_order_mismatch")
            event = events[position]
            position += 1
            _equal(event.get("phase"), phase, "event_phase_mismatch")
            return event

        for number, (mode, stage) in enumerate(zip(delivery.MODES, delivery.STAGES)):
            if position == len(events) - 1:
                break
            scope = "stage_record_invalid"
            _require(observed_failure is None, "attempt_after_failure")
            row = summary["additions"][mode]
            attempted.append(mode)
            request_id = f"{run_id}:{stage}:1"
            start = take("delivery_request_started", stage)
            _equal(row["stage"], stage, "stage_mismatch")
            _equal(row["request_id"], request_id, "request_id_mismatch")
            _equal(_ref(start, "input"), row["request"], "request_reference_mismatch")
            _equal(start["payload"], {"request_id": request_id, "http_attempted": False}, "request_start_mismatch")
            if number == 0:
                bounds = delivery._intersection(policy["first_addition_min_chars"], policy["first_addition_max_chars"], allowed)
                target = (min(max(policy["preferred_target_chars"], bounds["min"]), bounds["max"])
                          if v2 else (bounds["min"] + bounds["max"]) // 2)
            else:
                size = rendered_by_mode[delivery.MODES[0]]["char_count"]
                bounds, target = delivery._intersection((10 * size + 10) // 11, 11 * size // 10, allowed), size
            budget = delivery.plan_sentence_budget(packet, bounds, target) if v2 else None
            for k, v in {"additional_char_range": bounds, "target_chars": target}.items():
                _equal(row[k], v, "range_or_target_mismatch")
            if v2 or "policy_version" in row:
                _equal(row["policy_version"], policy["version"], "row_policy_mismatch")
            if v2 or "sentence_budget" in row or "sentence_budget_sha256" in row:
                _equal(row["sentence_budget"], budget, "sentence_budget_mismatch")
                _equal(row["sentence_budget_sha256"], canonical_hash(budget) if v2 else None, "sentence_budget_hash_mismatch")
            user = {"frozen_evidence_packet": packet, "mode": mode, "additional_char_range": bounds, "target_chars": target}
            if v2:
                user["sentence_budget"] = budget
            if number == 0:
                user["internal_report"] = report
            request = _blob(root, row["request"])
            messages = request["messages"]
            _require(type(messages) is list and len(messages) == 2, "request_messages_mismatch")
            _equal(_json(messages[1]["content"]), user, "user_context_mismatch")
            _equal(request, {"request_id": request_id, "stage": stage, "is_demo": False, "model_settings": settings,
                   "messages": [{"role": "system", "content": policy["common_prompt"] + "\n" + policy["mode_prompts"][mode]},
                                {"role": "user", "content": messages[1]["content"]}]}, "request_payload_mismatch")
            transport = []
            while position < len(events) - 1 and events[position]["event_type"].startswith("mimo_"):
                event = take(events[position]["event_type"], "live_probe")
                _require(event.get("task_id") == request_id, "http_request_id_mismatch")
                for k, v in {"actor_id": "mimo_transport", "actor_role": "transport", "status": "recorded"}.items():
                    _equal(event[k], v, "transport_identity_mismatch")
                transport.append(event)
            # 此接口要求有完整 HTTP/归一化响应证据；缺失或未完成的传输不猜测放行。
            _equal([e["event_type"] for e in transport],
                   ["mimo_request_prepared", "mimo_response_received", "mimo_request_completed"], "incomplete_transport")
            prepared, received_http, done = transport
            _equal(_blob(root, _ref(prepared, "input")), {
                "model": settings["model"], "messages": messages, "temperature": settings["temperature"],
                "max_completion_tokens": settings["max_tokens"], "thinking": {"type": "disabled"}, "stream": False},
                "http_payload_mismatch")
            _equal(prepared["payload"], {"actual_model_calls": actual, "max_completion_tokens": settings["max_tokens"],
                   "attempt_is_not_billing_confirmation": True}, "prepared_counter_mismatch")
            http = received_http["payload"]
            _equal(http["actual_model_calls"], actual + 1, "http_counter_mismatch")
            _equal(http["attempt_is_not_billing_confirmation"], True, "http_counter_basis_mismatch")
            _require(type(http["status"]) is int and 200 <= http["status"] <= 299, "http_status_mismatch")
            captured = _blob(root, _ref(received_http, "output"))
            _require(type(captured) is dict and set(captured) == {"raw_http_text"}
                     and type(captured["raw_http_text"]) is str, "http_body_unavailable")
            raw_http = captured["raw_http_text"].encode("utf-8")
            _equal(len(raw_http), http["response_bytes"], "http_size_mismatch")
            _equal(_sha(raw_http), http["response_sha256"], "http_hash_mismatch")
            envelope = _json(raw_http)
            normalized = _normalize(envelope, settings["model"])
            _equal(done["payload"], {"status": http["status"], "response_bytes": len(raw_http), "response_sha256": _sha(raw_http),
                   "usage": normalized["usage"], "usage_settled": True, "actual_model_calls": actual + 1,
                   "attempt_is_not_billing_confirmation": True}, "completed_payload_mismatch")
            actual += 1
            completed += 1
            received = take("delivery_response_received", stage)
            _equal(_ref(received, "input"), row["request"], "response_request_mismatch")
            _equal(_ref(received, "output"), row["response"], "response_reference_mismatch")
            _equal(received["payload"], {"validated": False}, "response_event_mismatch")
            response = _blob(root, row["response"])
            _equal(response, normalized, "normalized_response_mismatch")
            _equal(row["source_event_id"], received["event_id"], "response_event_id_mismatch")
            _equal(row["transport_records"], _transport_records(transport), "transport_records_mismatch")
            _equal(row["raw_response"], row["response"], "raw_response_reference_mismatch")
            raw, parsed, failure = response["raw_text"], None, None
            try:
                parsed = _parse_json(raw)
            except (ValueError, RecursionError):
                failure = {"code": "invalid_json", "safe_details": {}}
            if failure is None:
                _reject_sensitive_keys(parsed)
                canonical_hash(parsed)
                try:
                    rendered = delivery.render_addition(parsed, packet, mode, bounds, sentence_budget=budget)
                except delivery.DeliveryError as exc:
                    failure = {"code": exc.code, "safe_details": exc.safe_details}
            if failure:
                _equal(row["status"], "failed", "failed_stage_status_mismatch")
                _equal(row["rendered"], None, "failed_stage_has_rendered")
                _require("structured" not in row, "failed_stage_has_structured")
                observed_failure = {"stage": stage, "error": failure, "response": row["response"]}
            else:
                valid = take("delivery_stage_validated", stage)
                _equal(row["status"], "validated", "validated_stage_status_mismatch")
                _equal(row["rendered"], rendered, "rendered_mismatch")
                _equal(_ref(valid, "output"), row["structured"], "structured_reference_mismatch")
                _equal(_blob(root, row["structured"]), parsed, "structured_output_mismatch")
                _equal(valid["payload"], {"response": row["response"], "rendered": rendered, "syntax_only": True},
                       "validated_payload_mismatch")
                rendered_by_mode[mode] = rendered
                source_ids.append(received["event_id"])
                validated += 1
            by_mode[mode].update(status="failed" if failure else "validated", raw_text=raw, parsed_output=parsed,
                                 response_id=response["response_id"], usage=response["usage"],
                                 accepted=failure is None, diagnostic_only=failure is not None, modeled_candidate=True,
                                 range=bounds, budget=budget,
                                 **_diagnose(parsed, bounds, budget, fixed, failure))

        scope = "terminal_reconciliation_failed"
        _require(position == len(events) - 1, "trailing_events")
        _equal(sorted(summary["additions"]), sorted(attempted), "additions_mismatch")
        counts = {"actual_http_attempts": actual, "requests_started": len(attempted),
                  "completed_model_calls": completed, "validated_stages": validated}
        for k, v in {**counts, "actual_model_calls": actual}.items():
            _equal(summary[k], v, "actual_count_mismatch")
        _equal(summary["last_counts"], counts, "last_counts_mismatch")
        expected_counts = {"actual_http_attempts": 2, "completed_model_calls": 2, "validated_stages": 2}
        _equal(summary["expected_counts"], expected_counts, "expected_counts_mismatch")
        _equal(summary["expected_counts_passed"], all(counts[k] == v for k, v in expected_counts.items()), "expected_counts_result_mismatch")
        _equal(summary["source_event_ids"], source_ids, "source_event_ids_mismatch")
        pair, payloads = None, None
        if status == "delivery_failed":
            _require(observed_failure is not None, "unproven_failure")
            _equal(summary["failed_stage"], observed_failure["stage"], "failed_stage_mismatch")
            _equal(summary["error"], observed_failure["error"], "failure_error_mismatch")
            _equal(summary["failed_raw_response"], observed_failure["response"], "failed_response_mismatch")
            for k, v in {"pair": None, "payloads": {}, "lengths": {}, "expected_assembly": {}}.items():
                _equal(summary[k], v, "failed_run_has_export")
        else:
            _require(validated == completed == actual == 2 and observed_failure is None, "incomplete_success")
            _equal(summary["failed_stage"], None, "success_has_failure")
            _equal(summary["error"], None, "success_has_error")
            pair = make_dose_pair(packet, index, *(rendered_by_mode[m]["additional_text"] for m in delivery.MODES),
                                  provenance={"content_origin": "recorded_assistant_output", "natural_view_used": False,
                                              "source_event_ids": source_ids},
                                  min_chars=policy["total_min_chars"], max_chars=policy["total_max_chars"],
                                  tolerance=policy["match_tolerance"])
            _equal(_blob(root, summary["pair"]), pair, "pair_mismatch")
            _equal(summary["pair_checks"], pair["checks"], "pair_checks_mismatch")
            checks = {k: pair["checks"][k] is True for k in ("evidence_equal", "source_equal", "dose_within_range",
                      "length_matched", "additional_length_matched", "responses_equal", "assembly_valid")}
            payloads = {arm: public_payload(pair, arm) for arm in ("I4", "I5")}
            checks["exact_assembly"] = all(payloads[arm]["messages"][0]["content"] == "阅读材料\n" + compact["evidence_block"]
                + "\n\n阅读说明\n" + rendered_by_mode[mode]["additional_text"] + "\n\n" + RESPONSE_PROMPT
                and pair[arm]["char_count"] == fixed + rendered_by_mode[mode]["char_count"]
                for arm, mode in zip(("I4", "I5"), delivery.MODES))
            _require(all(checks.values()), "pair_not_accepted")
            _equal(summary["expected_assembly"], checks, "assembly_checks_mismatch")
            _equal({arm: _blob(root, ref) for arm, ref in summary["payloads"].items()}, payloads, "subject_payloads_mismatch")
            _equal(summary["lengths"], {arm: {"visible_chars": len(payloads[arm]["messages"][0]["content"]),
                   "additional_chars": len(pair[arm]["additional_text"])} for arm in payloads}, "pair_lengths_mismatch")

        cost = delivery._cost(summary["billing_before"], summary["billing_after"])
        _equal(summary["cost"], cost, "summary_cost_mismatch")
        _equal(summary["billing_delta"], cost, "billing_delta_mismatch")
        if pair is not None:
            _require(cost["total_cost_cny"] is not None, "success_cost_unavailable")
        cost.update(basis="仅重算 summary 中本地累计账本快照差值；未读取当前账本，不是独立金融核算或供应商最终账单",
                    independent_financial_accounting=False)
        result = {"schema_version": "1.0", "audit_status": "passed", "run_id": run_id, "runpath": str(root),
                  "summary_sha256": _sha(summary_bytes), "policy_version": policy["version"], "policy_sha256": policy_sha,
                  "verification": verification, "events_sha256": _sha(event_bytes), "inputs_match": True,
                  "internal_report_preserved": True, "original_snapshot": {"file_hashes_match": True,
                      "file_count": len(parent_hashes), "report_sha256": canonical_hash(report)},
                  **counts, "stage_count": validated, "run_status": status,
                  "failed_stage": summary["failed_stage"], "error": summary["error"], "by_mode": by_mode,
                  "not_attempted": [m for m in delivery.MODES if m not in attempted],
                  "evidence_block": compact["evidence_block"], "source_map": compact["source_map"], "fixed_chars": fixed,
                  "pair_available": pair is not None, "cost": cost, "budget_after": summary["billing_after"],
                  "source_inputs": {"stored_refs": refs, "policy_input": summary["policy_input"], "packet_input": info,
                      "source_index_input": source_index_input, "configuration": metadata,
                      "parent_source_path": str(parent), "parent_summary_sha256": parent_audit["summary_sha256"]},
                  "full_I4_eligible": False, "human_reviews_completed": 0, "subject_delivered": False,
                  "received_actual_subject": False, "formal_eligible": False, "audit_new_calls": 0,
                  "model_calls_this_audit": 0, "network_called_by_audit": False,
                  "audit_notice": "仅本地一致性及结构核验；非网络真实性、语义、独立计费或完整 I4 证明。失败候选仅供诊断。"}
        if pair is not None:
            result.update(pair=pair, subject_payloads=payloads)
        scope = "files_changed_during_audit"
        _equal(verify_events(root), verification, "verification_changed")
        _equal(_hashes(root), run_hashes, "run_files_changed")
        _equal(_hashes(parent), parent_hashes, "parent_files_changed")
        for path, digest in sources.items():
            _equal(_sha(path.read_bytes()), digest, "source_files_changed")
        _require((root / "events/events.jsonl").read_bytes() == event_bytes
                 and (root / "summary.json").read_bytes() == summary_bytes, "files_changed_during_audit")
        _reject_sensitive_keys(result)
        return deepcopy(result)
    except ValueError as exc:
        if str(exc).startswith("delivery_audit:"):
            raise
        raise ValueError("delivery_audit:" + scope) from None
    except (OSError, KeyError, TypeError, IndexError, OverflowError, RecursionError):
        raise ValueError("delivery_audit:" + scope) from None
